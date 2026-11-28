# -*- coding: utf-8 -*-
"""
VersionGate v2.1 测试：
  1) 1.21.x 全系协议号进服改写 + 版本标签
  2) VersionGate 配置文件夹生成 / 读取 / 容错 / 重置
  3) 物品注册表包(162) 物品名替换（nanobind 整体赋值）
  4) 早期 1.21 StartGame.items 物品名替换
  5) 替换失败时原样放行，不搞崩服务器
"""
import sys, types, struct, tempfile
from pathlib import Path
import pytest

# ---------- endstone mock ----------
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
    root.event = ev; root.plugin = plug; root.ColorFormat = ColorFormat
    sys.modules["endstone"] = root
    sys.modules["endstone.event"] = ev
    sys.modules["endstone.plugin"] = plug

_install_endstone_mock()

import bedrock_protocol as bp
from protocol_bridge import _module, _packet_class, ProtocolBridgeCore
from version_gate import (VersionGateConfig, FOLDER_NAME, rebuild_items_with_map,
                          BUILTIN_ITEM_MAP)
from bridge_plugin import (ProtocolBridgePlugin, DEFAULT_ALLOWED_PROTOCOLS,
                           PROTOCOL_121X, PID_REQUEST_NETWORK_SETTINGS, _proto_label)

S32BE = struct.Struct(">i")
SERVER_PROTO = 2225


class _Addr:
    def __init__(self, host, port): self.host, self.port = host, port
class _Logger:
    def __init__(self): self.lines = []
    def info(self, m, *a): self.lines.append(("I", m % a if a else m))
    def warning(self, m, *a): self.lines.append(("W", m % a if a else m))
class _Server:
    protocol_version = SERVER_PROTO
    minecraft_version = "1.26.65.1"
    def broadcast_message(self, m): pass


def make_plugin(cfg=None, data_folder=None):
    p = ProtocolBridgePlugin()
    base = {"enable_version_bypass": True, "enable_join_announcement": False,
            "enable_translation": True, "allowed_protocols": DEFAULT_ALLOWED_PROTOCOLS,
            "debug": False, "enable_stats": True}
    base.update(cfg or {})
    p.config = base
    p.server = _Server()
    p.logger = _Logger()
    if data_folder is not None:
        p.data_folder = Path(data_folder)
    p.on_enable()
    return p


def recv_event(pid, payload, host="1.2.3.4", port=12345, sub=0):
    from endstone.event import PacketReceiveEvent
    return PacketReceiveEvent(packet_id=pid, payload=payload,
                              address=_Addr(host, port), sub_client_id=sub)


def make_registry_payload(names, proto=2225):
    """构造 ItemRegistryPacket(162) payload，物品名列表。"""
    m = _module(proto); cls = _packet_class(proto, 162)
    obj = cls()
    ItemData = m.ItemData if hasattr(m, "ItemData") else bp.ItemData
    items = []
    for i, n in enumerate(names, 1):
        it = ItemData(); it.name = n; it.id = i
        items.append(it)
    obj.items = items
    return m, cls, m.encode(obj)


# ============ 测试：1.21.x 协议号 ============

def test_121x_all_in_whitelist():
    """1.21.x 全系协议号都在默认白名单。"""
    for proto in PROTOCOL_121X:
        assert proto in DEFAULT_ALLOWED_PROTOCOLS, proto
    # 1.26.x 也仍在内
    for proto in (924, 944, 2193, 2225):
        assert proto in DEFAULT_ALLOWED_PROTOCOLS


def test_121x_handshake_rewrite():
    """1.21.x 各档位握手协议号被改写为服务器协议号。"""
    p = make_plugin()
    for proto in sorted(PROTOCOL_121X):
        payload = S32BE.pack(proto) + b"\x00rest"
        e = recv_event(PID_REQUEST_NETWORK_SETTINGS, payload)
        p.on_packet_receive(e)
        assert S32BE.unpack_from(e.payload, 0)[0] == SERVER_PROTO, (proto, e.payload[:4])
        # 改写前真实协议号被记录
        assert p._client_proto[("1.2.3.4", 12345, 0)] == proto


def test_proto_label_121x():
    """1.21.x 协议号有可读版本标签。"""
    assert "1.21" in _proto_label(898)
    assert "1.21" in _proto_label(712)
    assert "26." in _proto_label(2225)
    assert "协议号" in _proto_label(99999)


def test_unsupported_685_686_degrade_to_rewrite():
    """685/686 库不支持模块：握手改写生效，游戏包翻译不崩。"""
    p = make_plugin()
    p._plaintext_confirmed = True
    for proto in (685, 686):
        e = recv_event(PID_REQUEST_NETWORK_SETTINGS, S32BE.pack(proto) + b"x",
                       host="8.8.8.8", port=3333)
        p.on_packet_receive(e)
        assert S32BE.unpack_from(e.payload, 0)[0] == SERVER_PROTO
        assert p._client_proto[("8.8.8.8", 3333, 0)] == proto


# ============ 测试：配置文件夹 ============

def test_config_folder_generated(tmp_path):
    """插件加载时生成 VersionGate/config.toml。"""
    p = make_plugin(data_folder=tmp_path)
    cfg = tmp_path / FOLDER_NAME / "config.toml"
    assert cfg.exists(), "配置文件应被生成"
    content = cfg.read_text(encoding="utf-8")
    assert "[item_map]" in content and "[settings]" in content
    assert "enable_item_replacement" in content


def test_config_load_mapping(tmp_path):
    """配置文件里的物品映射被正确读取。"""
    folder = tmp_path / FOLDER_NAME
    folder.mkdir(parents=True)
    (folder / "config.toml").write_text('''
[item_map]
"minecraft:new_item" = "minecraft:stone"
"create:brass_ingot" = "minecraft:iron_ingot"

[settings]
enable_item_replacement = true
''', encoding="utf-8")
    vg = VersionGateConfig(tmp_path)
    item_map, settings = vg.load()
    assert item_map["minecraft:new_item"] == "minecraft:stone"
    assert item_map["create:brass_ingot"] == "minecraft:iron_ingot"
    assert settings["enable_item_replacement"] is True


def test_config_corrupt_fallback(tmp_path):
    """配置文件损坏时回退内置默认，不抛异常。"""
    folder = tmp_path / FOLDER_NAME
    folder.mkdir(parents=True)
    (folder / "config.toml").write_text("这不是合法TOML!!![", encoding="utf-8")
    vg = VersionGateConfig(tmp_path)
    item_map, settings = vg.load()
    assert isinstance(item_map, dict)
    assert settings.get("enable_item_replacement") is True


def test_config_disabled_flag(tmp_path):
    """settings 关闭时插件不启用物品替换。"""
    folder = tmp_path / FOLDER_NAME
    folder.mkdir(parents=True)
    (folder / "config.toml").write_text('''
[item_map]
"minecraft:a" = "minecraft:b"
[settings]
enable_item_replacement = false
''', encoding="utf-8")
    p = make_plugin(data_folder=tmp_path)
    assert p._item_replace_enabled is False
    assert p._core.item_replace_enabled is False


# ============ 测试：物品名替换 ============

def test_rebuild_items_with_map():
    """整体重建列表方式替换物品名（nanobind 兼容）。"""
    m, cls, payload = make_registry_payload(
        ["minecraft:new_item_a", "minecraft:stone", "minecraft:new_item_b"])
    obj = m.decode(cls, payload)
    item_map = {"minecraft:new_item_a": "minecraft:iron_ingot",
                "minecraft:new_item_b": "minecraft:diamond"}
    new_items, changed = rebuild_items_with_map(obj.items, item_map)
    assert changed == 2
    names = [x.name for x in new_items]
    assert names == ["minecraft:iron_ingot", "minecraft:stone", "minecraft:diamond"]


def test_item_replace_via_core_776_plus():
    """776+ 档位：ItemRegistryPacket(162) 物品名替换真正写入字节。"""
    m, cls, payload = make_registry_payload(
        ["minecraft:new_item_a", "minecraft:stone", "minecraft:new_item_b"])
    item_map = {"minecraft:new_item_a": "minecraft:iron_ingot",
                "minecraft:new_item_b": "minecraft:diamond"}
    core = ProtocolBridgeCore(server_protocol=2225, item_map=item_map,
                              item_replace_enabled=True)
    out = core.translate_send(162, payload, 827)  # 1.21.100
    assert out is not None
    names = [x.name for x in m.decode(cls, out).items]
    assert names == ["minecraft:iron_ingot", "minecraft:stone", "minecraft:diamond"]
    assert core.item_replaced_count == 2


def test_item_replace_no_change_returns_none():
    """无物品命中映射时物品替换环节返回 None（原样放行，零开销）。"""
    m, cls, payload = make_registry_payload(["minecraft:stone", "minecraft:dirt"])
    core = ProtocolBridgeCore(server_protocol=2225,
                              item_map={"minecraft:new_item": "minecraft:stone"},
                              item_replace_enabled=True)
    assert core._replace_items_in_payload(162, payload, 776) is None


def test_item_replace_non_registry_packet():
    """非物品注册表包不受物品替换影响。"""
    core = ProtocolBridgeCore(server_protocol=2225,
                              item_map={"minecraft:a": "minecraft:b"},
                              item_replace_enabled=True)
    assert core._replace_items_in_payload(12, b"dummy", 776) is None


def test_item_replace_disabled():
    """替换开关关闭时物品替换环节不替换。"""
    m, cls, payload = make_registry_payload(["minecraft:new_item_a"])
    core = ProtocolBridgeCore(server_protocol=2225,
                              item_map={"minecraft:new_item_a": "minecraft:stone"},
                              item_replace_enabled=False)
    assert core._replace_items_in_payload(162, payload, 776) is None


def test_item_replace_bad_payload_no_crash():
    """损坏的 payload 不崩溃，原样放行。"""
    core = ProtocolBridgeCore(server_protocol=2225,
                              item_map={"minecraft:a": "minecraft:b"},
                              item_replace_enabled=True)
    assert core.translate_send(162, b"\x00\x01bad", 776) is None


def test_item_replace_preserves_other_fields():
    """替换物品名时保留 id/component_data 等其他字段。"""
    m, cls, payload = make_registry_payload(["minecraft:new_item_a"])
    orig = m.decode(cls, payload).items[0]
    core = ProtocolBridgeCore(server_protocol=2225,
                              item_map={"minecraft:new_item_a": "minecraft:stone"},
                              item_replace_enabled=True)
    out = core.translate_send(162, payload, 776)
    new = m.decode(cls, out).items[0]
    assert new.name == "minecraft:stone"
    assert new.id == orig.id
