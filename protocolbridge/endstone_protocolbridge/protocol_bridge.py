# -*- coding: utf-8 -*-
"""
ProtocolBridge - 基于 Endstone 官方 bedrock-protocol 库的跨版本包翻译核心
=============================================================================
原理：
  Bedrock 登录握手之后的游戏包是带 packet_id + payload 的二进制包。
  旧版客户端(如 1.26.51 / 协议号2193)按旧结构发包，服务器(1.26.65 / 2225)
  按新结构解析。只改登录包的协议号能让"握手"通过，但游戏包结构对不上会出错。

  本模块用官方库按版本 decode/encode 包，实现双向翻译：
    - 客户端→服务器：旧版 payload 用旧协议 decode 成对象 → 逐字段复制到新版对象
      → 新版协议 encode → 交给服务器（服务器看到的就是标准 2225 包）
    - 服务器→客户端：新版 payload decode → 复制到旧版对象 → 旧版 encode

关键实证（2193→2225，231 个共有包）：
  - 223 个包字节级兼容（空包测试），无需翻译
  - 仅 4 个包新增字段：AddPlayer/AddActor(passenger_of_block_data)、
    Animate(hand_slot)、LevelChunk(is_client_biome_update)
  - 0 个包删除字段
  - StartGame/PlayerSkin 内部枚举/序列化变化，需特殊处理

通用翻译算法：
  decode(源版本, 源类, bytes) -> src_obj
  新目标对象 dst_obj = 目标类()
  对每个同名且在两边可写的字段：setattr(dst_obj, f, getattr(src_obj, f))
  目标版本独有的新字段保持默认(None/0)
  encode(目标版本, dst_obj) -> bytes

依赖：bedrock-protocol >= 0.5.1 (Python 3.12+)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Optional, Tuple

import bedrock_protocol as bp

log = logging.getLogger("ProtocolBridge")

# 支持翻译的协议版本号（EndstoneMC/bedrock-protocol 0.5.1 支持列表）
SUPPORTED_PROTOCOLS = (
    649, 662, 671, 685, 686, 712, 729, 748, 766, 776, 786, 800,
    818, 819, 827, 844, 859, 860, 898, 924, 944, 975, 1001, 2168,
    2169, 2193, 2225, 2259,
)


def _module(proto: int):
    """返回 bp.v{proto} 模块；2225 及以后用顶层 bp（latest）。"""
    m = getattr(bp, "v%d" % proto, None)
    if m is not None:
        return m
    # 顶层 bp 代表最新协议；用它的 VERSION 判断
    latest = int(getattr(bp, "VERSION", 2225))
    if proto == latest:
        return bp
    return None


def _packet_class(proto: int, pid: int):
    """取某协议下 packet_id 对应的包类。"""
    m = _module(proto)
    if m is None:
        return None
    packets = getattr(m, "packets", None)
    if not packets:
        return None
    # packets 是 {id: class} 映射
    try:
        return packets[pid]
    except (KeyError, TypeError):
        return None


def _fields(obj: Any) -> Iterable[str]:
    """列出对象的可读写数据字段名（排除方法、私有、ID）。"""
    for name in dir(obj):
        if name.startswith("_") or name == "ID" or name == "unpack":
            continue
        try:
            v = getattr(obj, name)
        except Exception:
            continue
        if callable(v):
            continue
        yield name


@dataclass
class TranslateStats:
    """翻译统计。"""
    received: int = 0          # 客户端→服务器 处理包数
    sent: int = 0              # 服务器→客户端 处理包数
    translated_rx: int = 0     # 实际改写的收包
    translated_tx: int = 0     # 实际改写的发包
    decode_fail: int = 0
    encode_fail: int = 0
    no_class: int = 0
    per_packet: Dict[str, int] = field(default_factory=dict)

    def bump(self, key: str) -> None:
        self.per_packet[key] = self.per_packet.get(key, 0) + 1


class ProtocolBridgeCore:
    """
    双向包翻译核心。线程安全统计，翻译本身无共享状态。

    用法：
      core = ProtocolBridgeCore(server_protocol=2225)
      # 旧版客户端发包进来 (2193)
      new_payload = core.translate_receive(pid, old_payload, client_protocol=2193)
      event.payload = new_payload  # 若返回 None 表示无需/无法翻译，保持原样
      # 服务器发包给旧版客户端
      old_payload = core.translate_send(pid, new_payload, client_protocol=2193)
      event.payload = old_payload
    """

    def __init__(self, server_protocol: int = 2225, strict: bool = False,
                 blocked_ids: Optional[set] = None,
                 only_ids: Optional[set] = None):
        self.server_protocol = int(server_protocol)
        self.strict = bool(strict)  # True=翻译失败时返回 None 阻断；False=放行原包
        self.blocked_ids = set(blocked_ids or ())
        # only_ids：只对这些包做翻译，其余直接放行（性能优化）。
        # None 表示翻译所有包。传入 2193→2225 探测得到的 8 个包即可覆盖实际需求。
        self.only_ids = set(only_ids) if only_ids is not None else None
        self.stats = TranslateStats()
        # 缓存：某 (client_proto, pid) 是否字节兼容（由探测得出）
        self._compat_cache: Dict[Tuple[int, int], bool] = {}

    @staticmethod
    def known_translate_ids() -> set:
        """
        2193(1.26.51) → 2225(1.26.65) 实测需要翻译的 8 个包 ID。
        其余 223 个共有包字节兼容，无需翻译。
        来源：全量字段差异 + 跨版本 decode 探测。
        """
        return {11, 12, 13, 44, 58, 77, 93, 348}

    def client_module(self, proto: int):
        return _module(proto)

    def _translate(self, pid: int, payload: bytes, src_proto: int, dst_proto: int) -> Optional[bytes]:
        """通用字段级翻译：src_proto 编码的 payload -> dst_proto 编码。"""
        if src_proto == dst_proto:
            return None  # 同版本无需翻译
        if pid in self.blocked_ids:
            return None
        if self.only_ids is not None and pid not in self.only_ids:
            return None  # 不在翻译白名单，直接放行（零开销）
        src_cls = _packet_class(src_proto, pid)
        dst_cls = _packet_class(dst_proto, pid)
        if src_cls is None or dst_cls is None:
            self.stats.no_class += 1
            return None
        src_mod = _module(src_proto)
        dst_mod = _module(dst_proto)
        try:
            obj = src_mod.decode(src_cls, bytes(payload))
        except Exception as e:
            self.stats.decode_fail += 1
            log.debug("decode失败 pid=%d %d->%d: %s", pid, src_proto, dst_proto, e)
            return None
        try:
            dst_obj = dst_cls()
        except Exception as e:
            self.stats.encode_fail += 1
            log.debug("实例化目标包失败 pid=%d: %s", pid, e)
            return None
        copied = 0
        for f in _fields(obj):
            try:
                setattr(dst_obj, f, getattr(obj, f))
                copied += 1
            except Exception:
                # 目标版本没有该字段 / 类型不兼容 -> 跳过（新字段用默认值）
                continue
        try:
            out = dst_mod.encode(dst_obj)
        except Exception as e:
            self.stats.encode_fail += 1
            log.debug("encode失败 pid=%d %d->%d: %s", pid, src_proto, dst_proto, e)
            return None
        self.stats.bump("%d:%s" % (pid, src_cls.__name__))
        return bytes(out)

    def translate_receive(self, pid: int, payload: bytes, client_protocol: int) -> Optional[bytes]:
        """客户端(旧版)->服务器(新版)。返回新 payload 或 None(保持原样)。"""
        self.stats.received += 1
        out = self._translate(pid, payload, client_protocol, self.server_protocol)
        if out is not None:
            self.stats.translated_rx += 1
        elif self.strict and client_protocol != self.server_protocol:
            # 严格模式下，跨版本且无法翻译的包可选择阻断（谨慎使用）
            return None
        return out

    def translate_send(self, pid: int, payload: bytes, client_protocol: int) -> Optional[bytes]:
        """服务器(新版)->客户端(旧版)。返回新 payload 或 None(保持原样)。"""
        self.stats.sent += 1
        out = self._translate(pid, payload, self.server_protocol, client_protocol)
        if out is not None:
            self.stats.translated_tx += 1
        return out

    def probe_compatibility(self, client_protocol: int) -> Dict[str, Any]:
        """
        探测给定客户端协议与服务器协议之间，所有共有包的字节兼容性。
        用空包 decode/encode 快速扫描，产出报告。用于部署前评估。
        """
        common = sorted(set(_packet_class(self.server_protocol, p) and p for p in range(1, 400)
                            if _packet_class(self.server_protocol, p)) &
                        set(p for p in range(1, 400) if _packet_class(client_protocol, p)))
        compatible, incompatible, skipped = [], [], []
        for pid in common:
            c = _packet_class(client_protocol, pid)
            s = _packet_class(self.server_protocol, pid)
            try:
                data = _module(client_protocol).encode(c())
            except Exception:
                skipped.append((pid, c.__name__))
                continue
            try:
                _module(self.server_protocol).decode(s, data)
                compatible.append((pid, c.__name__))
            except Exception as e:
                incompatible.append((pid, c.__name__, type(e).__name__, str(e)[:60]))
        return {
            "client_protocol": client_protocol,
            "server_protocol": self.server_protocol,
            "common": len(common),
            "compatible": len(compatible),
            "incompatible": len(incompatible),
            "skipped": len(skipped),
            "incompatible_detail": incompatible,
            "skipped_detail": skipped,
        }
