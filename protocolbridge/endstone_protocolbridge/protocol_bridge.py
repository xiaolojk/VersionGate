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

    # 物品注册表包 ID：新版(776+)=ItemRegistryPacket，早期(712~766)=ItemComponentPacket
    PID_ITEM_REGISTRY = 162
    # 早期 1.21 物品名表在 StartGame.items 里（712~766）
    PID_START_GAME = 11
    # 1.21.60+ 使用 ItemRegistryPacket(162)；更老的用 StartGame.items
    ITEM_REGISTRY_MIN_PROTO = 776

    def __init__(self, server_protocol: int = 2225, strict: bool = False,
                 blocked_ids: Optional[set] = None,
                 only_ids: Optional[set] = None,
                 only_ids_by_proto: Optional[Dict[int, set]] = None,
                 item_map: Optional[Dict[str, str]] = None,
                 item_replace_enabled: bool = False):
        self.server_protocol = int(server_protocol)
        self.strict = bool(strict)  # True=翻译失败时返回 None 阻断；False=放行原包
        self.blocked_ids = set(blocked_ids or ())
        # only_ids：全局翻译白名单（旧版行为/兜底）。None 表示翻译所有包。
        self.only_ids = set(only_ids) if only_ids is not None else None
        # only_ids_by_proto：按客户端档位分列的精确白名单 {client_protocol: set(pid)}
        # 不同档位到 2225 的差异包集合完全不同（924 有 40 个，2193 只有 7 个），
        # 必须分档才能既覆盖老版本又保持零开销。优先级高于 only_ids。
        self.only_ids_by_proto = dict(only_ids_by_proto) if only_ids_by_proto else None
        # VersionGate 物品名替换：{1.26物品名: 1.21物品名}
        self.item_map = dict(item_map or ())
        self.item_replace_enabled = bool(item_replace_enabled) and bool(self.item_map)
        self.item_replaced_count = 0  # 累计替换次数
        self.stats = TranslateStats()
        # 缓存：某 (client_proto, pid) 是否字节兼容（由探测得出）
        self._compat_cache: Dict[Tuple[int, int], bool] = {}

    def set_item_map(self, item_map: Optional[Dict[str, str]], enabled: bool = True) -> None:
        """运行期更新物品映射（供配置热加载/管理命令调用）。"""
        self.item_map = dict(item_map or ())
        self.item_replace_enabled = bool(enabled) and bool(self.item_map)

    @classmethod
    def needs_item_replacement(cls, pid: int, client_protocol: int) -> bool:
        """判断某发包是否可能需要物品替换。"""
        if client_protocol >= cls.ITEM_REGISTRY_MIN_PROTO:
            return pid == cls.PID_ITEM_REGISTRY
        # 早期 1.21：物品名表在 StartGame.items
        return pid in (cls.PID_ITEM_REGISTRY, cls.PID_START_GAME)

    def _replace_items_in_payload(self, pid: int, payload: bytes,
                                  client_protocol: int) -> Optional[bytes]:
        """
        对服务器→客户端发包做物品名替换。
        用服务器协议(2225)解码（物品注册表字节兼容），替换 name 后再编码回去。
        返回新 payload 或 None（无需/无法替换，原样放行）。
        """
        if not self.item_replace_enabled:
            return None
        # 延迟导入避免循环依赖；相对导入优先（包环境），绝对导入兜底（直接运行/测试）
        try:
            from .version_gate import rebuild_items_with_map
        except ImportError:
            from version_gate import rebuild_items_with_map
        srv_cls = _packet_class(self.server_protocol, pid)
        srv_mod = _module(self.server_protocol)
        if srv_cls is None or srv_mod is None:
            return None
        try:
            obj = srv_mod.decode(srv_cls, bytes(payload))
        except Exception:
            return None
        # 定位物品表字段（nanobind 对象必须整体重新赋值，不能改临时副本）
        if pid == self.PID_ITEM_REGISTRY:
            field = "items"
        elif pid == self.PID_START_GAME and client_protocol < self.ITEM_REGISTRY_MIN_PROTO:
            field = "items"
        else:
            return None
        items = getattr(obj, field, None)
        if not isinstance(items, list):
            return None
        new_items, changed = rebuild_items_with_map(items, self.item_map)
        if changed == 0:
            return None
        try:
            setattr(obj, field, new_items)  # 整体赋值，真正生效
            out = srv_mod.encode(obj)
        except Exception:
            return None
        self.item_replaced_count += changed
        return bytes(out)

    @staticmethod
    def known_translate_ids() -> set:
        """
        2193(1.26.51) → 2225(1.26.65) 实测需要翻译的 8 个包 ID（默认兜底值）。
        其余 223 个共有包字节兼容。来源：全量字段差异 + 跨版本 decode 探测。
        新代码应优先用 probe_required_ids() 按档位生成精确表。
        """
        return {11, 12, 13, 44, 58, 77, 93, 348}

    @staticmethod
    def probe_required_ids(client_protocol: int, server_protocol: int) -> Optional[set]:
        """
        探测某客户端档位到服务器档位【真正需要翻译】的包 ID 集合。

        判定规则（对每个两版本共有的 packet_id）：
          1) 用源版本 encode 一个默认对象；若失败（如 CommandRequest 需必填字段），
             说明该包结构特殊，保守纳入翻译集合；
          2) 用目标版本 decode 这份字节；若失败，说明结构不兼容，纳入翻译集合；
          3) 两者都成功 → 字节级兼容，无需翻译。

        返回 None 表示该客户端档位不被官方库支持（如 2169），此时调用方应
        降级为「只改协议号、不翻译」而非报错。

        探测结果建议在插件 on_enable 时对每个允许档位各跑一次并缓存，
        运行期零开销。
        """
        src_mod = _module(client_protocol)
        dst_mod = _module(server_protocol)
        if src_mod is None or dst_mod is None:
            return None
        required = set()
        for pid in range(1, 400):
            src_cls = _packet_class(client_protocol, pid)
            dst_cls = _packet_class(server_protocol, pid)
            if src_cls is None or dst_cls is None:
                continue
            try:
                data = src_mod.encode(src_cls())
            except Exception:
                # 默认实例无法编码（必填字段/特殊结构）→ 保守纳入
                required.add(pid)
                continue
            try:
                dst_mod.decode(dst_cls, data)
            except Exception:
                required.add(pid)
        return required

    def client_module(self, proto: int):
        return _module(proto)

    def _translate(self, pid: int, payload: bytes, src_proto: int, dst_proto: int,
                   client_proto: Optional[int] = None) -> Optional[bytes]:
        """通用字段级翻译：src_proto 编码的 payload -> dst_proto 编码。"""
        if src_proto == dst_proto:
            return None  # 同版本无需翻译
        if pid in self.blocked_ids:
            return None
        # 按客户端档位选白名单：优先用分档精确表，未命中则用全局 only_ids
        ids = None
        if self.only_ids_by_proto is not None and client_proto is not None:
            ids = self.only_ids_by_proto.get(client_proto)
        if ids is None:
            ids = self.only_ids
        if ids is not None and pid not in ids:
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
        out = self._translate(pid, payload, client_protocol, self.server_protocol, client_protocol)
        if out is not None:
            self.stats.translated_rx += 1
        elif self.strict and client_protocol != self.server_protocol:
            # 严格模式下，跨版本且无法翻译的包可选择阻断（谨慎使用）
            return None
        return out

    def translate_send(self, pid: int, payload: bytes, client_protocol: int) -> Optional[bytes]:
        """服务器(新版)->客户端(旧版)。返回新 payload 或 None(保持原样)。"""
        self.stats.sent += 1
        # VersionGate：物品注册表包先做物品名替换（独立于字段翻译白名单）
        if (self.item_replace_enabled
                and client_protocol != self.server_protocol
                and self.needs_item_replacement(pid, client_protocol)):
            replaced = self._replace_items_in_payload(pid, payload, client_protocol)
            if replaced is not None:
                self.stats.translated_tx += 1
                return replaced
            # 物品替换未改动时继续走正常翻译流程（早期1.21的StartGame仍需字段翻译）
        out = self._translate(pid, payload, self.server_protocol, client_protocol, client_protocol)
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
