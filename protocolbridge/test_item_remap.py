# -*- coding: utf-8 -*-
"""
防空气 / 物品 ID 映射回归测试
=============================
实测根因：1.21 老版本 NetworkItemStackDescriptor 编码器把 id=0 当空物品哨兵，
直接输出 6 字节空包，丢弃 block_runtime_id/stack_size → 方块物品变空气。

本测试保证：
  1. 方块物品(id=0+bri)开启映射后，目标 id>0 且保留 bri/stack
  2. 有玩家映射时优先用映射值
  3. 普通新物品 id 命中映射时被替换
  4. 收包方向不做映射（不破坏服务端识别）
  5. 未开启映射时保持原行为（不改 id）
"""
import pytest

import bedrock_protocol as bp
from deep_translator import DeepTranslator

PID = 50  # InventorySlotPacket


def _make_2225_item(item_id, bri, stack):
    o = bp.v2225.packets[PID]()
    o.item.id = item_id
    o.item.block_runtime_id = bri
    o.item.stack_size = stack
    return bytes(bp.v2225.encode(o))


def _decode_776(raw):
    return bp.v776.decode(bp.v776.packets[PID], raw)


def test_block_item_fallback_not_air():
    """方块物品无映射 → 兜底 id>0，不再变空气。"""
    dt = DeepTranslator(2225, 776, fallback_item_id=1, enable_item_remap=True)
    raw = _make_2225_item(0, 12345, 2)
    action, out = dt.translate_send(PID, raw)
    assert action == "translate" and out
    d = _decode_776(out)
    assert d.item.id == 1, "兜底 id 应为 1（石头），不能是 0"
    assert d.item.block_runtime_id == 12345, "block_runtime_id 必须保留"
    assert d.item.stack_size == 2, "stack_size 必须保留"
    assert dt.remapped_items == 1


def test_block_item_player_mapping():
    """方块物品 block_runtime_id 命中玩家映射 → 用映射值。"""
    dt = DeepTranslator(2225, 776, item_id_map={9999: 5},
                        fallback_item_id=1, enable_item_remap=True)
    raw = _make_2225_item(0, 9999, 4)
    action, out = dt.translate_send(PID, raw)
    assert action == "translate" and out
    d = _decode_776(out)
    assert d.item.id == 5
    assert d.item.block_runtime_id == 9999
    assert d.item.stack_size == 4


def test_new_item_id_mapping():
    """普通新物品 id 命中映射 → 替换。"""
    dt = DeepTranslator(2225, 776, item_id_map={12345: 7},
                        fallback_item_id=1, enable_item_remap=True)
    raw = _make_2225_item(12345, 0, 9)
    action, out = dt.translate_send(PID, raw)
    assert action == "translate" and out
    d = _decode_776(out)
    assert d.item.id == 7
    assert d.item.stack_size == 9


def test_recv_direction_no_remap():
    """收包方向（老客户端→服务端）不做映射。"""
    dt = DeepTranslator(2225, 776, item_id_map={5: 9999},
                        fallback_item_id=1, enable_item_remap=True)
    # 老客户端发 id=5
    o = bp.v776.packets[PID]()
    o.item.id = 5
    o.item.block_runtime_id = 0
    o.item.stack_size = 3
    raw = bytes(bp.v776.encode(o))
    action, out = dt.translate_recv(PID, raw)
    assert out is not None
    d = bp.v2225.decode(bp.v2225.packets[PID], out)
    assert d.item.id == 5, "收包方向不应把 5 映射成 9999"
    assert dt.remapped_items == 0


def test_remap_disabled_keeps_behavior():
    """未开启映射时保持原行为（方块物品仍可能变空气，但 id 不动）。"""
    dt = DeepTranslator(2225, 776, item_id_map={9999: 5},
                        fallback_item_id=1, enable_item_remap=False)
    raw = _make_2225_item(0, 9999, 4)
    action, out = dt.translate_send(PID, raw)
    assert action == "translate" and out
    d = _decode_776(out)
    assert d.item.id == 0, "未开启映射时 id 应原样为 0"
    assert dt.remapped_items == 0


def test_fallback_zero_disables_block_fix():
    """fallback_item_id=0 且无映射 → 方块物品保留 id=0（不兜底）。"""
    dt = DeepTranslator(2225, 776, item_id_map={}, fallback_item_id=0,
                        enable_item_remap=True)
    raw = _make_2225_item(0, 12345, 2)
    action, out = dt.translate_send(PID, raw)
    assert action == "translate" and out
    d = _decode_776(out)
    assert d.item.id == 0
    assert dt.remapped_items == 0
