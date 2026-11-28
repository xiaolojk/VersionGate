# -*- coding: utf-8 -*-
"""
ConnectionState - 玩家级连接状态（借鉴 ViaVersion 的 Storable 设计）
==================================================================
问题：跨版本翻译不是"逐包孤立"的。StartGame 包会下发一整套 registry 上下文
（方块属性表、runtime id 基线、物品栈管理模式、registry 校验和），后续
UpdateBlock / InventorySlot / 实体等包里的数字 id 都依赖这套上下文才有意义。

ViaVersion 的做法：每个 UserConnection 挂一套 Storable，跨包保存状态
（SignStorage 记住牌子坐标、NonceStorage 记住聊天验证码、RegistryDataRewriter
记住动态 registry）。无状态逐包翻译必然导致"能进服但方块/物品错乱"。

本模块提供等价能力：
  - 每个 (address, sub_client_id) 一个 ConnectionState
  - on_start_game(): 捕获 StartGame 的 registry 上下文
  - 描述符类型别名：物品描述符在 2225 改名，提供跨版本类型查询
  - registry_known(): 明确标记"上下文是否已建立"，未知则降级而非伪装成功

诚实边界（经 bedrock-protocol 实测修正）：
- 物品描述符用的是跨版本稳定的 legacy id（id + aux_value），776/898/2225
  完全一致；物品包双向翻译已验证保真，无需外部映射表。
- 方块 id 在 block_network_ids_are_hashes=False（默认）时用固定注册表，
  True 时是每次连接的 hash——后者才需要映射，但 block_properties 的元素类
  （BlockEntry/ServerBlockProperty）未导出、取不到 identifier，沙箱无法学习。
- 因此本模块聚焦：上下文标志保真（hashes/netmgr/checksum）+ 明确降级，
  不凭空捏造 id 映射；这些标志翻译器已验证跨版本保真。
"""
from __future__ import annotations

import threading
from typing import Any, Dict, Optional


# 物品/方块描述符在各版本的类名差异（已由 bedrock-protocol 实测确认）
DESCRIPTOR_ALIASES = {
    "NetworkItemStackDescriptor": "SerializedNetworkItemStackDescriptor",
    "SerializedNetworkItemStackDescriptor": "NetworkItemStackDescriptor",
    "NetworkBlockPosition": "BlockPos",
    "BlockPos": "NetworkBlockPosition",
}

# StartGame 中与 registry/id 换算相关的上下文字段
CONTEXT_FIELDS = (
    "runtime_id",
    "block_properties",
    "block_network_ids_are_hashes",
    "enable_item_stack_net_manager",
    "server_block_type_registry_checksum",
    "server_enabled_client_side_generation",
)


class ConnectionState:
    """单个玩家连接的翻译状态（Storable 等价物）。"""

    __slots__ = ("client_protocol", "start_game", "registry_known",
                 "start_game_seen", "unknown_ids", "notes")

    def __init__(self, client_protocol: int):
        self.client_protocol = int(client_protocol)
        self.start_game: Dict[str, Any] = {}
        self.registry_known = False      # 是否已捕获到 StartGame 上下文
        self.start_game_seen = 0         # 收到 StartGame 的次数（重连/换世界会重置）
        self.unknown_ids = 0             # 遇到无法换算的 id 的次数（诚实计数）
        self.notes: list = []

    def on_start_game(self, fields: Dict[str, Any]) -> None:
        """捕获 StartGame 下发的 registry 上下文。"""
        self.start_game_seen += 1
        self.start_game = {k: fields.get(k) for k in CONTEXT_FIELDS if k in fields}
        # 只要能拿到 block_properties / checksum 任一，即认为上下文已建立
        self.registry_known = any(
            self.start_game.get(k) not in (None, "", [], 0)
            for k in ("block_properties", "server_block_type_registry_checksum")
        )
        if not self.registry_known:
            self.notes.append("StartGame 未携带可识别的 registry 上下文，id 换算进入保守模式")

    def descriptor_alias(self, type_name: str) -> Optional[str]:
        """返回描述符类型在目标版本应使用的类名；无别名返回 None。"""
        return DESCRIPTOR_ALIASES.get(type_name)

    def note_unknown_id(self, where: str) -> None:
        """记录一次无法换算的 id（不伪装成功）。"""
        self.unknown_ids += 1
        if self.unknown_ids <= 5:  # 防止无限增长
            self.notes.append("无法换算的 id: " + where)

    def summary(self) -> str:
        return (
            "协议%d | registry=%s | StartGame×%d | 未知id=%d"
            % (self.client_protocol,
               "已知" if self.registry_known else "未知",
               self.start_game_seen, self.unknown_ids)
        )


class ConnectionStateRegistry:
    """按 (address, sub_client_id) 管理所有玩家连接的 ConnectionState。"""

    def __init__(self):
        self._states: Dict[tuple, ConnectionState] = {}
        self._lock = threading.Lock()

    @staticmethod
    def key(address, sub_client_id: int = 0) -> tuple:
        try:
            host = getattr(address, "host", None)
            port = getattr(address, "port", None)
        except Exception:
            host, port = None, None
        return (str(host), port, sub_client_id)

    def get(self, address, sub_client_id: int = 0,
            client_protocol: Optional[int] = None, create: bool = True) -> Optional[ConnectionState]:
        k = self.key(address, sub_client_id)
        with self._lock:
            st = self._states.get(k)
            if st is None and create:
                st = ConnectionState(client_protocol or 0)
                self._states[k] = st
            return st

    def remove(self, address, sub_client_id: int = 0) -> None:
        k = self.key(address, sub_client_id)
        with self._lock:
            self._states.pop(k, None)

    def all_states(self) -> list:
        with self._lock:
            return list(self._states.values())
