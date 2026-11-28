# -*- coding: utf-8 -*-
"""ProtocolBridge 核心翻译测试（用真实 bedrock-protocol 库）。"""
import pytest
import bedrock_protocol as bp
from protocol_bridge import (
    ProtocolBridgeCore, _packet_class, _module, _fields, SUPPORTED_PROTOCOLS,
)


def test_library_supports_both_versions():
    assert _module(2193) is not None
    assert _module(2225) is not None
    assert bp.ProtocolVersion.get(2225) is not None
    assert bp.ProtocolVersion.get(2193) is not None


def test_packet_class_mapping():
    for pid in (1, 9, 12, 13, 44, 58, 144):
        c21 = _packet_class(2193, pid)
        c22 = _packet_class(2225, pid)
        assert c21 is not None, pid
        assert c22 is not None, pid


def test_addplayer_translate_2193_to_2225():
    """AddPlayer 是 2225 新增 passenger_of_block_data 的包，重点验证。"""
    core = ProtocolBridgeCore(server_protocol=2225)
    pid = 12
    C21 = _packet_class(2193, pid)
    # 构造 2193 包并填字段
    obj = C21()
    obj.name = "TestPlayer"
    obj.runtime_id = 12345
    data = _module(2193).encode(obj)

    out = core.translate_receive(pid, data, client_protocol=2193)
    assert out is not None, "翻译应成功"
    # 用 2225 解码，确认合法且字段保留
    C22 = _packet_class(2225, pid)
    decoded = _module(2225).decode(C22, out)
    assert decoded.name == "TestPlayer"
    assert decoded.runtime_id == 12345
    assert core.stats.translated_rx >= 1


def test_animate_translate_new_field():
    """AnimatePacket 2225 新增 hand_slot，翻译后应能正确解码。"""
    core = ProtocolBridgeCore(server_protocol=2225)
    pid = 44
    C21 = _packet_class(2193, pid)
    obj = C21()
    data = _module(2193).encode(obj)
    out = core.translate_receive(pid, data, client_protocol=2193)
    assert out is not None
    C22 = _packet_class(2225, pid)
    decoded = _module(2225).decode(C22, out)
    assert decoded is not None


def test_reverse_translate_2225_to_2193():
    """反向：服务器 2225 包 -> 旧版客户端 2193。"""
    core = ProtocolBridgeCore(server_protocol=2225)
    pid = 12
    C22 = _packet_class(2225, pid)
    obj = C22()
    obj.name = "ServerSent"
    data = _module(2225).encode(obj)
    out = core.translate_send(pid, data, client_protocol=2193)
    assert out is not None
    C21 = _packet_class(2193, pid)
    decoded = _module(2193).decode(C21, out)
    assert decoded.name == "ServerSent"


def test_passthrough_compatible_packet():
    """Login(1) 等热门包跨版本字段一致，翻译后仍能正确解码。"""
    core = ProtocolBridgeCore(server_protocol=2225)
    pid = 1
    C21 = _packet_class(2193, pid)
    obj = C21()
    data = _module(2193).encode(obj)
    out = core.translate_receive(pid, data, client_protocol=2193)
    # 字段级翻译总会产生新字节（即使结构一致），关键是能解码
    if out is not None:
        C22 = _packet_class(2225, pid)
        assert _module(2225).decode(C22, out) is not None


def test_same_protocol_no_translation():
    """同版本不应翻译。"""
    core = ProtocolBridgeCore(server_protocol=2225)
    C22 = _packet_class(2225, 12)
    data = _module(2225).encode(C22())
    assert core.translate_receive(12, data, client_protocol=2225) is None


def test_probe_compatibility_report():
    """全量兼容性探测应给出合理报告。"""
    core = ProtocolBridgeCore(server_protocol=2225)
    report = core.probe_compatibility(2193)
    assert report["common"] > 200
    assert report["compatible"] > 0
    assert isinstance(report["incompatible_detail"], list)


def test_translate_all_common_packets_no_crash():
    """对全部共有包做一遍翻译，不应抛异常（允许部分返回 None）。"""
    core = ProtocolBridgeCore(server_protocol=2225)
    common = [p for p in range(1, 400)
              if _packet_class(2193, p) and _packet_class(2225, p)]
    translated = 0
    for pid in common:
        C21 = _packet_class(2193, pid)
        try:
            data = _module(2193).encode(C21())
        except Exception:
            continue
        out = core.translate_receive(pid, data, client_protocol=2193)
        if out is not None:
            translated += 1
            C22 = _packet_class(2225, pid)
            # 译出的字节必须能被目标版本解码
            try:
                _module(2225).decode(C22, out)
            except Exception as e:
                pytest.fail("pid=%d %s 翻译后无法解码: %s" % (pid, C21.__name__, e))
    assert translated > 150, "至少应成功翻译大部分包"
    print("成功翻译并回解: %d/%d" % (translated, len(common)))


# ============ v2.0.1 新增：按档位动态翻译白名单测试 ============

def test_probe_required_ids_all_protocols():
    """各 1.26.x 档位到 2225 的探测表应覆盖全部不兼容包，且老档位 > 2193。"""
    sizes = {}
    for proto in (924, 944, 975, 1001, 2168, 2193):
        ids = ProtocolBridgeCore.probe_required_ids(proto, 2225)
        assert ids is not None, proto
        assert 12 in ids and 11 in ids, (proto, ids)  # StartGame/AddPlayer 必在内
        sizes[proto] = len(ids)
    # 老档位差异包明显更多，证明固定 8 包白名单确实不够
    assert sizes[924] > sizes[2193] >= 7, sizes
    print("各档位需翻译包数:", sizes)


def test_probe_required_ids_unsupported_protocol():
    """库不支持的档位(2169)应返回 None 以便降级。"""
    assert ProtocolBridgeCore.probe_required_ids(2169, 2225) is None


def test_probe_table_translates_all_required_packets():
    """用探测出的表翻译该档位全部差异包，必须都能被 2225 回解。"""
    for proto in (924, 1001, 2168, 2193):
        ids = ProtocolBridgeCore.probe_required_ids(proto, 2225)
        core = ProtocolBridgeCore(server_protocol=2225, only_ids_by_proto={proto: ids})
        checked = 0
        for pid in ids:
            cls = _packet_class(proto, pid)
            if cls is None:
                continue
            try:
                data = _module(proto).encode(cls())
            except Exception:
                continue  # 默认对象无法编码的包（如 CommandRequest）
            out = core.translate_receive(pid, data, client_protocol=proto)
            assert out is not None, (proto, pid)
            dst = _module(2225).decode(_packet_class(2225, pid), out)
            assert dst is not None, (proto, pid)
            checked += 1
        assert checked > 0, proto
        print("档位 %d 验证 %d 个差异包全部可翻译回解" % (proto, checked))


def test_only_ids_by_proto_precedence():
    """分档表应优先于全局 only_ids 生效。"""
    # 全局只放行 pid=1；但分档表指定 12 -> 应能翻译 12
    core = ProtocolBridgeCore(server_protocol=2225,
                              only_ids={1},
                              only_ids_by_proto={2193: {12}})
    obj = _packet_class(2193, 12)()
    obj.name = "ByProto"
    data = _module(2193).encode(obj)
    out = core.translate_receive(12, data, client_protocol=2193)
    assert out is not None, "分档表应优先，翻译 pid=12"
    decoded = _module(2225).decode(_packet_class(2225, 12), out)
    assert decoded.name == "ByProto"


def test_manual_only_ids_still_respected():
    """用户显式配置 only_packet_ids 时，不在表内的包不应翻译。"""
    core = ProtocolBridgeCore(server_protocol=2225, only_ids={999})
    obj = _packet_class(2193, 12)()
    data = _module(2193).encode(obj)
    assert core.translate_receive(12, data, client_protocol=2193) is None
