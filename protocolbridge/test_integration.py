# -*- coding: utf-8 -*-
"""
端到端集成测试：mock Endstone 运行环境，验证 v2.0.1 插件完整链路
  握手协议号改写 → 记录真实协议号 → 游戏包双向翻译 → 多客户端并发不串味
本地无 endstone 库，此处注入最小 mock 后再导入插件。
"""
import sys, types, struct, threading
import pytest

# ---------- 构造 endstone mock ----------
def _install_endstone_mock():
    if "endstone" in sys.modules:
        return
    endstone = types.ModuleType("endstone")
    class ColorFormat:
        YELLOW = "§e"; RESET = "§r"
    endstone.ColorFormat = ColorFormat

    ev = types.ModuleType("endstone.event")
    def event_handler(fn): return fn
    class _Base:
        def __init__(self, **kw):
            for k, v in kw.items(): setattr(self, k, v)
    class PacketReceiveEvent(_Base): pass
    class PacketSendEvent(_Base): pass
    class PlayerJoinEvent(_Base): pass
    ev.event_handler = event_handler
    ev.PacketReceiveEvent = PacketReceiveEvent
    ev.PacketSendEvent = PacketSendEvent
    ev.PlayerJoinEvent = PlayerJoinEvent

    plug = types.ModuleType("endstone.plugin")
    class Plugin:
        api_version = "0.11"
        def __init__(self): pass
        def save_default_config(self): pass
        def register_events(self, obj): pass
    plug.Plugin = Plugin

    root = types.ModuleType("endstone")
    root.event = ev; root.plugin = plug
    sys.modules["endstone"] = root
    sys.modules["endstone.event"] = ev
    sys.modules["endstone.plugin"] = plug
    root.ColorFormat = ColorFormat

_install_endstone_mock()

import bedrock_protocol as bp
from protocol_bridge import _module, _packet_class
from bridge_plugin import ProtocolBridgePlugin, _HANDSHAKE_PIDS, PID_REQUEST_NETWORK_SETTINGS
import struct as _struct

S32BE = _struct.Struct(">i")
SERVER_PROTO = 2225
ALLOWED = [924, 944, 975, 1001, 2168, 2169, 2193, 2225]

class _Addr:
    def __init__(self, host, port): self.host, self.port = host, port

class _Logger:
    def __init__(self): self.lines = []
    def info(self, m): self.lines.append(("I", m))
    def warning(self, m): self.lines.append(("W", m))

class _Server:
    protocol_version = SERVER_PROTO
    minecraft_version = "1.26.65.1"
    def broadcast_message(self, m): pass

def make_plugin(cfg=None):
    p = ProtocolBridgePlugin()
    base = {
        "enable_version_bypass": True, "enable_join_announcement": False,
        "enable_translation": True, "allowed_protocols": ALLOWED,
        "debug": False, "enable_stats": True,
    }
    base.update(cfg or {})
    p.config = base
    p.server = _Server()
    p.logger = _Logger()
    p.on_enable()
    return p

def recv_event(pid, payload, host="1.2.3.4", port=12345, sub=0):
    from endstone.event import PacketReceiveEvent
    return PacketReceiveEvent(packet_id=pid, payload=payload,
                              address=_Addr(host, port), sub_client_id=sub)

def send_event(pid, payload, host="1.2.3.4", port=12345, sub=0):
    from endstone.event import PacketSendEvent
    return PacketSendEvent(packet_id=pid, payload=payload,
                           address=_Addr(host, port), sub_client_id=sub)

# ============ 测试用例 ============

def test_handshake_rewrite_keeps_allowed_protocols():
    """1.26.x 白名单内协议号被改写为服务器协议号，跨代版本不改。"""
    p = make_plugin()
    for proto in ALLOWED:
        if proto == SERVER_PROTO: continue
        payload = S32BE.pack(proto) + b"\x00rest"
        for pid in _HANDSHAKE_PIDS:
            e = recv_event(pid, payload)
            p.on_packet_receive(e)
            assert S32BE.unpack_from(e.payload, 0)[0] == SERVER_PROTO, (proto, pid)
    # 跨代版本 1.21.x (协议号 766) 不在白名单，不得改写
    e = recv_event(PID_REQUEST_NETWORK_SETTINGS, S32BE.pack(766) + b"x")
    p.on_packet_receive(e)
    assert S32BE.unpack_from(e.payload, 0)[0] == 766

def test_client_proto_recorded_before_rewrite():
    """握手改写前必须记录真实协议号，供后续翻译使用。"""
    p = make_plugin()
    e = recv_event(PID_REQUEST_NETWORK_SETTINGS, S32BE.pack(2193) + b"xyz", host="9.9.9.9", port=5555)
    p.on_packet_receive(e)
    key = ("9.9.9.9", 5555, 0)
    assert p._client_proto[key] == 2193

def test_game_packet_translate_roundtrip():
    """握手后游戏包：旧版客户端发包→翻译为2225可解；服务器发包→译为旧版可解。"""
    p = make_plugin()
    proto = 2193
    e = recv_event(PID_REQUEST_NETWORK_SETTINGS, S32BE.pack(proto) + b"x", host="5.5.5.5", port=1111)
    p.on_packet_receive(e)
    # 强制确认明文（自检默认前3包确认，这里模拟已确认）
    p._plaintext_confirmed = True
    # 收包：构造 2193 的 AddPlayer(12)
    C = _packet_class(proto, 12); obj = C(); obj.name = "E2E"; obj.runtime_id = 42
    data = _module(proto).encode(obj)
    er = recv_event(12, data, host="5.5.5.5", port=1111)
    p.on_packet_receive(er)
    dec = _module(SERVER_PROTO).decode(_packet_class(SERVER_PROTO, 12), er.payload)
    assert dec.name == "E2E" and dec.runtime_id == 42
    # 发包：2225 StartGame 之外用一个简单包，这里用 AddPlayer 反向
    C2 = _packet_class(SERVER_PROTO, 12); o2 = C2(); o2.name = "FROM_SERVER"; o2.runtime_id = 99
    d2 = _module(SERVER_PROTO).encode(o2)
    es = send_event(12, d2, host="5.5.5.5", port=1111)
    p.on_packet_send(es)
    dec2 = _module(proto).decode(_packet_class(proto, 12), es.payload)
    assert dec2.name == "FROM_SERVER" and dec2.runtime_id == 99

def test_multi_client_no_proto_mixup():
    """多客户端并发：不同档位客户端的包必须用各自协议号翻译，不能串味。"""
    p = make_plugin()
    p._plaintext_confirmed = True
    clients = [(2193, "10.0.0.1", 1001), (924, "10.0.0.2", 1002), (2168, "10.0.0.3", 1003)]
    for proto, host, port in clients:
        e = recv_event(PID_REQUEST_NETWORK_SETTINGS, S32BE.pack(proto)+b"x", host=host, port=port)
        p.on_packet_receive(e)
    # 逐客户端发 AddPlayer，验证各自被正确翻译
    for proto, host, port in clients:
        C = _packet_class(proto, 12); obj = C(); obj.name = host; obj.runtime_id = port
        data = _module(proto).encode(obj)
        er = recv_event(12, data, host=host, port=port)
        p.on_packet_receive(er)
        dec = _module(SERVER_PROTO).decode(_packet_class(SERVER_PROTO, 12), er.payload)
        assert dec.name == host and dec.runtime_id == port, (proto, host, dec.name)
    # 确认记录的协议号彼此独立
    for proto, host, port in clients:
        assert p._client_proto[(host, port, 0)] == proto

def test_unsupported_2169_degrades_to_rewrite_only():
    """2169 库不支持：握手仍改写可进服，游戏包不被错误处理。"""
    p = make_plugin()
    p._plaintext_confirmed = True
    e = recv_event(PID_REQUEST_NETWORK_SETTINGS, S32BE.pack(2169)+b"x", host="7.7.7.7", port=2222)
    p.on_packet_receive(e)
    assert S32BE.unpack_from(e.payload, 0)[0] == SERVER_PROTO  # 改写生效
    assert p._client_proto[("7.7.7.7", 2222, 0)] == 2169
    # 2169 不在分档表，翻译会走兜底 only_ids(2193的8包)，但库无法 decode 2169
    # 关键：不得抛异常、不得破坏 payload
    C = _packet_class(2225, 12); obj = C(); obj.name = "X"
    data = _module(2225).encode(obj)
    er = recv_event(12, data, host="7.7.7.7", port=2222)
    p.on_packet_receive(er)  # 不应抛异常
    assert isinstance(er.payload, bytes)

def test_probe_stats_reported():
    """分档翻译表应在启动时生成并统计。"""
    p = make_plugin()
    assert p._probe_count_by_proto, "应有分档统计"
    # 924 档位差异包应远多于 2193
    assert p._probe_count_by_proto.get(924, 0) > p._probe_count_by_proto.get(2193, 0)
