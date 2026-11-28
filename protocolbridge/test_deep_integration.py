# -*- coding: utf-8 -*-
"""
深度翻译插件集成测试：mock Endstone 事件，验证 on_packet_send/receive 完整路径
  - 1.21 客户端发包 → 深度翻译后坐标正确
  - 1.21 客户端收包 → 深度翻译后坐标正确
  - 服务端独有包 → event.cancel() 被调用（真正丢弃）
  - 1.26 客户端 → 走原轻量翻译，不受影响
"""
import sys, types
from pathlib import Path
import pytest

# ---- endstone mock（带 cancel）----
def _install_mock():
    if "endstone" in sys.modules:
        # 其他测试可能已注册了精简 mock，补齐 cancel/is_cancelled
        ev = sys.modules.get("endstone.event")
        if ev is not None:
            for cls_name in ("PacketReceiveEvent", "PacketSendEvent", "PlayerJoinEvent"):
                cls = getattr(ev, cls_name, None)
                if cls is not None:
                    if not hasattr(cls, "cancel"):
                        cls.cancel = lambda self: setattr(self, "cancelled", True)
                    if not hasattr(cls, "is_cancelled"):
                        cls.is_cancelled = lambda self: bool(getattr(self, "cancelled", False))
        return
    endstone = types.ModuleType("endstone")
    class ColorFormat:
        YELLOW = "§e"; RESET = "§r"
    endstone.ColorFormat = ColorFormat
    ev = types.ModuleType("endstone.event")
    def event_handler(fn): return fn
    class _B:
        def __init__(self, **kw):
            self.cancelled = False
            for k, v in kw.items(): setattr(self, k, v)
        def cancel(self): self.cancelled = True
        def is_cancelled(self): return self.cancelled
    class PacketReceiveEvent(_B): pass
    class PacketSendEvent(_B): pass
    class PlayerJoinEvent(_B): pass
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
    sys.modules["endstone"] = endstone
    sys.modules["endstone.event"] = ev
    sys.modules["endstone.plugin"] = plug

_install_mock()

import bedrock_protocol as bp
from bridge_plugin import ProtocolBridgePlugin, DEFAULT_ALLOWED_PROTOCOLS
from protocol_bridge import _module, _packet_class
from endstone.event import PacketReceiveEvent, PacketSendEvent

class Addr:
    def __init__(self): self.host = "1.2.3.4"; self.port = 12345
class Log:
    def __init__(self): self.lines = []
    def info(self, m, *a): self.lines.append(m % a if a else m)
    def warning(self, m, *a): self.lines.append("W:" + (m % a if a else m))
class Srv:
    protocol_version = 2225
    minecraft_version = "1.26.65.1"
    def broadcast_message(self, m): pass


def make_plugin(client_proto):
    p = ProtocolBridgePlugin()
    p.config = {
        "enable_version_bypass": True, "enable_join_announcement": False,
        "enable_translation": True, "allowed_protocols": DEFAULT_ALLOWED_PROTOCOLS,
        "debug": False, "enable_stats": True, "enable_deep_translation": True,
    }
    p.server = Srv(); p.logger = Log()
    p.on_enable()
    p._plaintext_confirmed = True
    addr = Addr()
    p._client_proto[(addr.host, addr.port, 0)] = client_proto
    return p, addr


def test_deep_initialized_for_121():
    p, _ = make_plugin(776)
    assert len(p._deep) >= 10, f"深度翻译器档位数不足: {len(p._deep)}"
    assert 776 in p._deep


def test_send_translates_position():
    p, addr = make_plugin(776)
    obj = _packet_class(2225, 21)()
    obj.pos.x, obj.pos.y, obj.pos.z = 50, 60, -70
    payload = _module(2225).encode(obj)
    ev = PacketSendEvent(packet_id=21, payload=payload, address=addr, sub_client_id=0)
    p.on_packet_send(ev)
    dec = _module(776).decode(_packet_class(776, 21), ev.payload)
    assert (dec.pos.x, dec.pos.y, dec.pos.z) == (50, 60, -70)


def test_recv_translates_position():
    p, addr = make_plugin(776)
    obj = _packet_class(776, 21)()
    obj.pos.x, obj.pos.y, obj.pos.z = 11, 22, 33
    payload = _module(776).encode(obj)
    ev = PacketReceiveEvent(packet_id=21, payload=payload, address=addr, sub_client_id=0)
    p.on_packet_receive(ev)
    dec = _module(2225).decode(_packet_class(2225, 21), ev.payload)
    assert (dec.pos.x, dec.pos.y, dec.pos.z) == (11, 22, 33)


def test_server_only_packet_cancelled():
    """服务端独有包(333)对1.21客户端应被 cancel。"""
    p, addr = make_plugin(776)
    ev = PacketSendEvent(packet_id=333, payload=b"dummy", address=addr, sub_client_id=0)
    p.on_packet_send(ev)
    assert ev.is_cancelled(), "服务端独有包应被取消发送"


def test_client_only_packet_cancelled():
    """客户端独有包对1.21客户端应被 cancel（收包方向）。"""
    p, addr = make_plugin(776)
    only = p._deep[776]._only_client_pids()
    assert len(only) > 0
    pid = sorted(only)[0]
    ev = PacketReceiveEvent(packet_id=pid, payload=b"dummy", address=addr, sub_client_id=0)
    p.on_packet_receive(ev)
    assert ev.is_cancelled(), f"客户端独有包 pid={pid} 应被取消"


def test_126_client_unaffected():
    """1.26 客户端(2193)不建深度翻译器，走原路径。"""
    p, _ = make_plugin(2193)
    assert 2193 not in p._deep
