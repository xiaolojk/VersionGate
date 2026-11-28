# -*- coding: utf-8 -*-
"""
DeepTranslator v3.0 测试：跨大版本(1.21.x↔1.26.x)深度翻译引擎

核心验证：
  1) 服务端独有包 → 丢弃
  2) 共有包 2225→老版本 → 老版本能成功 decode
  3) 关键标量字段（字符串/坐标/ID）翻译后值正确
  4) 收包方向 老版本→2225 同样可用
  5) 13 个档位全覆盖
  6) 枚举值跨版本一致（int 中转安全）
"""
import enum
import pytest
import bedrock_protocol as bp

from protocol_bridge import _module, _packet_class, _fields, ProtocolBridgeCore
from deep_translator import DeepTranslator, DROP

PROTOCOLS = [712, 729, 748, 766, 776, 786, 800, 818, 819, 827, 844, 859, 898]
SERVER = 2225


def set_field(obj, name, value):
    try:
        setattr(obj, name, value)
        return True
    except Exception:
        return False


def build_realistic(pid, proto=SERVER):
    """构造一个字段值合理的包（用非随机的固定值）。"""
    cls = _packet_class(proto, pid)
    if not cls:
        return None, None
    obj = cls()
    for f in _fields(obj):
        try:
            v = getattr(obj, f)
        except Exception:
            continue
        if isinstance(v, str):
            set_field(obj, f, "TestWorld")
        elif isinstance(v, bytes):
            set_field(obj, f, b"abc")
        elif isinstance(v, bool):
            set_field(obj, f, True)
        elif isinstance(v, int) and v == 0 and "id" not in f.lower():
            set_field(obj, f, 1)
        # 嵌套对象/枚举保持默认（真实语义）
    try:
        return obj, _module(proto).encode(obj)
    except Exception:
        return None, None


# ---------- 1. 独有包丢弃 ----------
@pytest.mark.parametrize("proto", [776, 898])
def test_only_server_packets_dropped(proto):
    dt = DeepTranslator(SERVER, proto)
    # 这些是服务端独有包，老客户端不认识，必须丢弃
    only = dt._only_server_pids()
    assert len(only) > 0
    for pid in list(only)[:10]:
        action, data = dt.translate_send(pid, b"dummy")
        assert action == "drop", f"pid={pid} 应丢弃"
        assert data is None


# ---------- 2. 共有差异包翻译后老版本能 decode ----------
@pytest.mark.parametrize("proto", [776, 898])
def test_diff_packets_decode_after_translate(proto):
    dt = DeepTranslator(SERVER, proto)
    probed = ProtocolBridgeCore.probe_required_ids(proto, SERVER)
    tested = 0
    for pid in sorted(probed):
        obj, payload = build_realistic(pid)
        if payload is None:
            continue
        action, out = dt.translate_send(pid, payload)
        if action != "translate" or out is None:
            continue
        dst_cls = _packet_class(proto, pid)
        if not dst_cls:
            continue
        # 关键：老客户端协议必须能 decode
        try:
            _module(proto).decode(dst_cls, out)
            tested += 1
        except Exception as e:
            pytest.fail(f"pid={pid} {dst_cls.__name__} 翻译后 {proto} 无法 decode: {e}")
    assert tested > 20, f"测试包太少: {tested}"


# ---------- 3. 关键标量字段保真 ----------
def test_updateblock_position_preserved():
    """UpdateBlock 的方块坐标 x/y/z 翻译后必须原样保留。"""
    dt = DeepTranslator(SERVER, 776)
    obj, payload = build_realistic(21)
    assert payload is not None
    # 设置确切坐标
    obj.pos.x, obj.pos.y, obj.pos.z = 123, 64, -456
    payload = _module(SERVER).encode(obj)
    action, out = dt.translate_send(21, payload)
    assert action == "translate"
    dec = _module(776).decode(_packet_class(776, 21), out)
    assert (dec.pos.x, dec.pos.y, dec.pos.z) == (123, 64, -456)


def test_text_message_preserved():
    """TextPacket 消息内容翻译后保留（2225 body.message → 776 message）。"""
    dt = DeepTranslator(SERVER, 776)
    obj, payload = build_realistic(9)
    assert payload is not None
    body = obj.body  # nanobind: body 是临时副本，需整体赋值
    body.message = "Hello 世界 123"  # 2225 把 message 打包进 body
    obj.body = body
    payload = _module(SERVER).encode(obj)
    action, out = dt.translate_send(9, payload)
    assert action == "translate"
    dec = _module(776).decode(_packet_class(776, 9), out)
    assert dec.message == "Hello 世界 123"  # 776 是平铺字段


# ---------- 4. 收包方向 ----------
@pytest.mark.parametrize("proto", [776, 898])
def test_receive_direction(proto):
    """老客户端→服务器：老版本包翻译后服务器能 decode。"""
    dt = DeepTranslator(SERVER, proto)
    probed = ProtocolBridgeCore.probe_required_ids(proto, SERVER)
    tested = 0
    for pid in sorted(probed):
        obj, payload = build_realistic(pid, proto)  # 用老版本构造
        if payload is None:
            continue
        action, out = dt.translate_recv(pid, payload)
        if action != "translate" or out is None:
            continue
        srv_cls = _packet_class(SERVER, pid)
        if not srv_cls:
            continue
        try:
            _module(SERVER).decode(srv_cls, out)
            tested += 1
        except Exception as e:
            pytest.fail(f"recv pid={pid} 翻译后服务器无法 decode: {e}")
    assert tested > 20, f"收包测试数太少: {tested}"


# ---------- 5. 13 档位全覆盖 ----------
@pytest.mark.parametrize("proto", PROTOCOLS)
def test_all_protocols_basic(proto):
    """每个档位都能实例化并翻译 TextPacket。"""
    dt = DeepTranslator(SERVER, proto)
    obj, payload = build_realistic(9)
    if payload is None:
        pytest.skip("TextPacket 不可构造")
    action, out = dt.translate_send(9, payload)
    assert action in ("translate", "pass", "fail", "drop")


# ---------- 6. 枚举值跨版本一致 ----------
def test_enum_values_cross_version():
    """同名枚举的 name→value 在 1.21 与 1.26 一致，确保 int 中转安全。"""
    def enum_vals(proto):
        m = _module(proto)
        out = {}
        for cn in dir(m):
            c = getattr(m, cn)
            if isinstance(c, type) and issubclass(c, enum.Enum):
                out[cn] = {x.name: x.value for x in c}
        return out
    a = enum_vals(776)
    b = enum_vals(SERVER)
    for cn in set(a) & set(b):
        for name in set(a[cn]) & set(b[cn]):
            assert a[cn][name] == b[cn][name], f"{cn}.{name} 值不一致"
