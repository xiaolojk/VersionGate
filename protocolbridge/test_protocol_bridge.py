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
