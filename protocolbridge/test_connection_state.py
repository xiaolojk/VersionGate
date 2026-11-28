# -*- coding: utf-8 -*-
"""
连接级状态测试（借鉴 ViaVersion Storable）
  - StartGame 上下文被正确捕获（不再丢弃）
  - 描述符类型别名可查（NetworkItemStackDescriptor ↔ Serialized...）
  - 未知 id 明确计数，不伪装成功
  - 插件发包路径：服务端 StartGame 会写入玩家连接状态
"""
from __future__ import annotations

import sys
import types

import pytest

# ---------- endstone mock ----------
def _install_mock():
    if "endstone" in sys.modules:
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
    ev.event_handler = event_handler
    ev.PacketReceiveEvent = type("PacketReceiveEvent", (_B,), {})
    ev.PacketSendEvent = type("PacketSendEvent", (_B,), {})
    ev.PlayerJoinEvent = type("PlayerJoinEvent", (_B,), {})
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

from connection_state import (  # noqa: E402
    ConnectionState, ConnectionStateRegistry, DESCRIPTOR_ALIASES, CONTEXT_FIELDS,
)
from bridge_plugin import ProtocolBridgePlugin, DEFAULT_ALLOWED_PROTOCOLS  # noqa: E402
from protocol_bridge import _module, _packet_class  # noqa: E402
from endstone.event import PacketSendEvent  # noqa: E402


class Addr:
    def __init__(self): self.host = "10.0.0.9"; self.port = 41111

class Log:
    def __init__(self): self.lines = []
    def info(self, m, *a): self.lines.append(m % a if a else m)
    def warning(self, m, *a): self.lines.append("W:" + (m % a if a else m))

class Srv:
    protocol_version = 2225
    minecraft_version = "1.26.65.1"
    def broadcast_message(self, m): pass


def test_start_game_context_captured():
    st = ConnectionState(776)
    assert st.registry_known is False
    st.on_start_game({
        "runtime_id": 1,
        "block_properties": [{"name": "minecraft:stone"}],
        "server_block_type_registry_checksum": 123456,
        "enable_item_stack_net_manager": True,
    })
    assert st.registry_known is True
    assert st.start_game_seen == 1
    assert st.start_game["server_block_type_registry_checksum"] == 123456
    assert st.start_game["enable_item_stack_net_manager"] is True


def test_empty_start_game_is_honest():
    """空上下文不能伪装成已知。"""
    st = ConnectionState(776)
    st.on_start_game({"runtime_id": 0, "block_properties": []})
    assert st.registry_known is False
    assert st.notes, "应记录降级说明"


def test_descriptor_aliases():
    assert ConnectionState(776).descriptor_alias("NetworkItemStackDescriptor") == \
        "SerializedNetworkItemStackDescriptor"
    assert ConnectionState(776).descriptor_alias("NetworkBlockPosition") == "BlockPos"
    assert ConnectionState(776).descriptor_alias("SomeRandomType") is None


def test_unknown_id_counted_not_faked():
    st = ConnectionState(776)
    for _ in range(10):
        st.note_unknown_id("UpdateBlock.runtime_id")
    assert st.unknown_ids == 10
    assert len(st.notes) <= 5, "说明条数应设上限，防止无限增长"


def test_registry_keyed_by_connection():
    reg = ConnectionStateRegistry()
    a1, a2 = Addr(), Addr()
    a2.port = 41112
    s1 = reg.get(a1, 0, client_protocol=776)
    s2 = reg.get(a2, 0, client_protocol=898)
    assert s1 is not s2
    assert s1.client_protocol == 776 and s2.client_protocol == 898
    assert reg.get(a1, 0) is s1  # 同一连接复用
    reg.remove(a1, 0)
    assert reg.get(a1, 0, client_protocol=776) is not s1


def test_adjacent_version_text_preserved():
    """Text 特殊处理器通用化：相邻版本(776→786 同结构)不能清空消息。
    旧实现把特殊处理器写死针对 2225，会在同结构相邻步把 message 清空。"""
    from deep_translator import DeepTranslator
    obj = _packet_class(776, 9)()
    obj.message = "相邻版本保真"
    obj.type = 1
    payload = bytes(_module(776).encode(obj))
    dt = DeepTranslator(786, 776)  # recv: 776→786
    act, out = dt.translate_recv(9, payload)
    assert act == "translate"
    dec = _module(786).decode(_packet_class(786, 9), out)
    assert dec.message == "相邻版本保真"


def test_text_structure_transition():
    """跨结构改变 859(message) → 898(body) 也要正确打包。"""
    from deep_translator import DeepTranslator
    obj = _packet_class(859, 9)()
    obj.message = "跨结构转换"
    obj.type = 1
    payload = bytes(_module(859).encode(obj))
    dt = DeepTranslator(898, 859)  # recv: 859→898
    act, out = dt.translate_recv(9, payload)
    assert act == "translate"
    dec = _module(898).decode(_packet_class(898, 9), out)
    assert dec.body.message == "跨结构转换"


def test_plugin_captures_start_game_on_send():
    """插件发包路径：服务端 StartGame 会写入该玩家的连接状态。"""
    p = ProtocolBridgePlugin()
    p.config = {
        "enable_version_bypass": True, "enable_join_announcement": False,
        "enable_translation": True, "enable_deep_translation": True,
        "enable_connection_context": True,
        "allowed_protocols": DEFAULT_ALLOWED_PROTOCOLS,
        "debug": False, "enable_stats": False,
    }
    p.server = Srv(); p.logger = Log()
    p.on_enable()
    p._plaintext_confirmed = True
    addr = Addr()
    p._client_proto[(addr.host, addr.port, 0)] = 776

    # 构造服务端 StartGame（pid=11），带 registry 上下文
    obj = _packet_class(2225, 11)()
    obj.runtime_id = 1
    obj.server_block_type_registry_checksum = 987654
    payload = bytes(_module(2225).encode(obj))
    ev = PacketSendEvent(packet_id=11, payload=payload, address=addr, sub_client_id=0)
    p.on_packet_send(ev)

    st = p._connections.get(addr, 0, client_protocol=776, create=False)
    assert st is not None and st.start_game_seen == 1, "StartGame 应被捕获"
    assert st.start_game.get("server_block_type_registry_checksum") == 987654


# ============ 数据驱动 ID 保真（实测 bedrock-protocol 得出）============
# 结论：物品用跨版本稳定的 legacy id（id+aux_value），无需外部映射表；
# 外部映射表的旧假设已证伪。以下测试锁定这个已验证能力，防止回归。

def _inventory_payload(proto, item_id, aux, slot, count):
    obj = _packet_class(proto, 50)()
    obj.inventory_id = 0
    obj.slot = slot
    obj.item.id = item_id
    obj.item.aux_value = aux
    obj.item.stack_size = count
    return bytes(_module(proto).encode(obj))


def test_item_legacy_id_recv_fidelity():
    """776 老客户端放物品 → 2225 服务端：legacy id/aux/数量/slot 全部保真。"""
    from deep_translator import DeepTranslator
    dt = DeepTranslator(2225, 776)
    act, out = dt.translate_recv(50, _inventory_payload(776, 5, 3, 2, 7))
    assert act == "translate"
    dec = _module(2225).decode(_packet_class(2225, 50), out)
    assert (dec.item.id, dec.item.aux_value, dec.item.stack_size, dec.slot) == (5, 3, 7, 2)


def test_item_legacy_id_send_fidelity():
    """2225 服务端下发物品 → 776 老客户端：legacy id/aux/数量/slot 全部保真。"""
    from deep_translator import DeepTranslator
    dt = DeepTranslator(2225, 776)
    act, out = dt.translate_send(50, _inventory_payload(2225, 361, 0, 1, 64))
    assert act == "translate"
    dec = _module(776).decode(_packet_class(776, 50), out)
    assert (dec.item.id, dec.item.aux_value, dec.item.stack_size, dec.slot) == (361, 0, 64, 1)


def test_start_game_registry_flags_fidelity():
    """StartGame 的 hashes/netmgr/checksum/runtime_id 四个上下文标志必须跨版本保真。"""
    from deep_translator import DeepTranslator
    dt = DeepTranslator(2225, 776)
    obj = _packet_class(2225, 11)()
    obj.block_network_ids_are_hashes = True
    obj.enable_item_stack_net_manager = True
    obj.server_block_type_registry_checksum = 123456789
    obj.runtime_id = 99
    payload = bytes(_module(2225).encode(obj))
    act, out = dt.translate_send(11, payload)
    assert act == "translate"
    dec = _module(776).decode(_packet_class(776, 11), out)
    assert dec.block_network_ids_are_hashes is True
    assert dec.enable_item_stack_net_manager is True
    assert dec.server_block_type_registry_checksum == 123456789
    assert dec.runtime_id == 99


def test_descriptor_legacy_id_stable_across_versions():
    """物品描述符的核心字段集合在 776/898/2225 完全一致（id+aux+stack_size+block_runtime_id）。"""
    from protocol_bridge import _fields
    core = {"id", "aux_value", "stack_size", "block_runtime_id"}
    for proto in (776, 898, 2225):
        obj = _packet_class(proto, 50)()
        assert core.issubset(set(_fields(obj.item))), f"proto {proto} 描述符缺核心字段"


def test_proto_924_uses_deep_translation_for_block_and_item():
    """协议924(1.26.0)必须走深度翻译：浅复制会把 BlockPos.y 算错、物品描述符丢字段。"""
    from deep_translator import DeepTranslator
    import bridge_plugin as bp
    # 边界语义：924 应纳入深度路径
    assert bp.DEEP_TRANSLATE_MAX_PROTO == 924
    dt = DeepTranslator(2225, 924)
    # 方块：坐标必须精确保真
    o = _packet_class(2225, 21)()
    o.runtime_id = 123
    pos = type(o.pos)()
    pos.x, pos.y, pos.z = 7, 80, -9
    o.pos = pos
    act, out = dt.translate_send(21, bytes(_module(2225).encode(o)))
    assert act == "translate"
    d = _module(924).decode(_packet_class(924, 21), out)
    assert (d.pos.x, d.pos.y, d.pos.z, d.runtime_id) == (7, 80, -9, 123)
    # 普通物品：legacy id 必须保真
    o = _packet_class(2225, 50)()
    o.inventory_id = 0; o.slot = 0
    o.item.id = 276; o.item.stack_size = 1
    act, out = dt.translate_send(50, bytes(_module(2225).encode(o)))
    assert act == "translate"
    d = _module(924).decode(_packet_class(924, 50), out)
    assert d.item.id == 276
