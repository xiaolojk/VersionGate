# -*- coding: utf-8 -*-
"""
DeepTranslator - 跨大版本（1.21.x ↔ 1.26.x）逐包深度翻译引擎
================================================================
不同于 protocol_bridge 的浅层字段复制（只适合 1.26.x 相邻小版本），
本模块针对 1.21.x 到 2225 的**结构性差异**做逐字段、逐嵌套对象的转换。

设计依据（来自 bedrock-protocol 库全量实测，见 diff_report.json）：
  - 201 个共有包，packet_id 零漂移
  - 每档位 51~59 个差异包，模式高度重合
  - 差异类型：嵌套对象改名（NetworkBlockPosition→BlockPos）、
    物品描述符改名（NetworkItemStackDescriptor→Serialized...）、
    int↔enum、字段增删、序列化格式重写

核心策略：
  1) 服务端独有包（老客户端不认识）→ 丢弃（返回 DROP 哨兵）
  2) 共有包 → 通用递归字段适配：
     - 同名同类型 → 直接复制
     - int↔enum → 转换
     - 嵌套对象 → 按目标类型递归构造，逐字段填充
     - list → 逐元素转换后整体赋值
     - 源有目标无的字段 → 丢弃（新字段无法向下兼容）
     - 目标有源无的字段 → 保持默认
  3) 少数序列化完全重写的包（StartGame/PlayerAuthInput/PlayerSkin/
     LevelChunk 等）→ 特殊处理器

方向：
  - send（2225→1.21）：src=服务端, dst=老客户端
  - recv（1.21→2225）：src=老客户端, dst=服务端

依赖：bedrock-protocol >= 0.5.1
"""
from __future__ import annotations

import enum
import logging
from typing import Any, Dict, Optional, Tuple

import bedrock_protocol as bp

try:
    # 作为包加载时（wheel/Endstone）用相对导入
    from .protocol_bridge import _module, _packet_class, _fields
except ImportError:  # 直接运行/测试（把 translator 目录加入 sys.path）
    from protocol_bridge import _module, _packet_class, _fields

log = logging.getLogger("DeepTranslator")

# 丢弃哨兵：表示该包应对老客户端隐藏
DROP = object()

# 最大递归深度，防止异常结构
MAX_DEPTH = 6

# 字段别名映射：{pid: {源版本字段名: 目标版本字段名}}
# 用于新旧版本对同一语义字段改名的情况（如 TextPacket body↔message）。
# 双向通用：翻译时先按别名把源字段名映射到目标字段名再复制。
FIELD_ALIASES = {
    9: {"body": "message", "message": "body"},  # TextPacket
}


class DeepTranslator:
    """
    跨大版本深度翻译器。每个 (client_protocol) 一个实例，内部缓存类型映射。
    """

    # 物品描述符类名特征：命中这些类型的对象才会做物品 ID 映射。
    _ITEM_DESC_HINTS = ("ItemStackDescriptor", "ItemDescriptor")

    def __init__(self, server_protocol: int = 2225, client_protocol: int = 776,
                 item_id_map: Optional[Dict[int, int]] = None,
                 fallback_item_id: int = 1,
                 enable_item_remap: bool = False,
                 debug_log: bool = False):
        self.server_protocol = int(server_protocol)
        self._debug_log = bool(debug_log)
        self.client_protocol = int(client_protocol)
        self.srv_mod = _module(self.server_protocol)
        self.cli_mod = _module(self.client_protocol)
        # 玩家自定义物品 ID 映射（1.26新物品id / block_runtime_id → 1.21已有物品id）
        self.item_id_map: Dict[int, int] = {}
        if isinstance(item_id_map, dict):
            for k, v in item_id_map.items():
                try:
                    ki, vi = int(k), int(v)
                    if ki > 0 and vi > 0:
                        self.item_id_map[ki] = vi
                except Exception:
                    continue
        # 方块物品(id=0)无映射时的兜底 id：>0 才会被老版本编码器保留；
        # 设为 0 表示不兜底（可能仍显示空气）。默认 1（minecraft:stone）。
        self.fallback_item_id = int(fallback_item_id)
        self.enable_item_remap = bool(enable_item_remap)
        # 统计
        self.dropped_send = 0
        self.dropped_recv = 0
        self.translated_send = 0
        self.translated_recv = 0
        self.failed_send = 0
        self.failed_recv = 0
        self.remapped_items = 0
        # 特殊处理器注册表 pid -> callable
        self._special_send = {}
        self._special_recv = {}
        self._register_special()

    # ---------- 独有包判定 ----------
    def _only_server_pids(self) -> set:
        sc = set((getattr(self.srv_mod, "packets", {}) or {}).keys())
        cc = set((getattr(self.cli_mod, "packets", {}) or {}).keys())
        return sc - cc

    def _only_client_pids(self) -> set:
        sc = set((getattr(self.srv_mod, "packets", {}) or {}).keys())
        cc = set((getattr(self.cli_mod, "packets", {}) or {}).keys())
        return cc - sc

    # ---------- 通用递归字段适配 ----------
    @staticmethod
    def _scalarize(v: Any):
        """把 nanobind 枚举 / 单值包装对象 转成 Python 标量。转换不了返回原值。"""
        # 标准 enum
        if isinstance(v, enum.Enum):
            return v.value
        # nanobind 枚举：有 value 属性且非容器
        if hasattr(v, "value") and not isinstance(v, (list, tuple, dict, str, bytes)):
            try:
                return v.value
            except Exception:
                pass
        # 单字段包装对象（如 ItemStackLegacyRequestId.raw_id）
        for attr in ("raw_id", "raw", "value_", "v"):
            if hasattr(v, attr):
                got = getattr(v, attr)
                if isinstance(got, (int, str, float, bool)):
                    return got
        # UUID → 字符串
        if type(v).__name__ == "UUID":
            try:
                return str(v)
            except Exception:
                pass
        return v

    @classmethod
    def _adapt_scalar(cls, v: Any, target_type_name: str):
        """把标量 v 适配成目标类型；返回 (ok, value)。"""
        if v is None:
            return True, None
        v = cls._scalarize(v)
        cur = type(v).__name__
        if cur == target_type_name:
            return True, v
        # bool 是 int 子类，注意顺序
        if target_type_name == "bool":
            return True, bool(v)
        if target_type_name == "int":
            try:
                return True, int(v)
            except Exception:
                return False, None
        if target_type_name == "str":
            return True, str(v)
        if target_type_name == "float":
            try:
                return True, float(v)
            except Exception:
                return False, None
        if target_type_name == "bytes":
            if isinstance(v, (bytes, bytearray)):
                return True, bytes(v)
            return False, None
        return False, None  # 嵌套对象，需递归

    def _build_nested(self, v: Any, target_cls, depth: int):
        """递归构造一个 target_cls 对象，用 v 的同名字段填充。"""
        if depth > MAX_DEPTH:
            return None
        try:
            dst = target_cls()
        except Exception:
            return None
        self._copy_fields(v, dst, depth + 1)
        return dst

    def _find_class(self, proto: int, type_name: str):
        """在某协议模块中按类名找类。"""
        m = _module(proto)
        return getattr(m, type_name, None)

    @classmethod
    def _is_item_descriptor(cls, obj) -> bool:
        """判断对象是否为物品描述符（Network/Serialized ItemStackDescriptor）。"""
        try:
            name = type(obj).__name__
        except Exception:
            return False
        return any(h in name for h in cls._ITEM_DESC_HINTS)

    def _remap_item_descriptor(self, src_item, built_item, depth: int) -> None:
        """
        把一个物品描述符从 1.26 结构适配到 1.21 时，修正物品 id，避免"变空气"。

        根因（实测 bedrock-protocol 0.5.1）：
          - 1.26 方块物品 = id:0 + block_runtime_id:N（新结构允许）
          - 1.21 老版本 NetworkItemStackDescriptor 编码器把 id:0 当"空物品哨兵"，
            直接输出 6 字节空包，丢弃 block_runtime_id/stack_size → 玩家看到空气。

        解决：
          - 若源 id=0 且 block_runtime_id>0（方块物品）：
              优先用玩家映射 block_runtime_id→物品id；无映射用 fallback_item_id(默认1)。
          - 若源 id>0 且在玩家映射里：直接替换为映射后的 1.21 物品 id。
          - 保证写回目标的 id 一定 >0，老编码器就不会清空整个描述符。

        注意 built_item 是新建的目标对象（非临时副本），直接 setattr 有效。
        """
        if not self.enable_item_remap:
            return
        if depth > MAX_DEPTH:
            return
        # 只在降级发包方向生效（目标=老客户端协议 < 服务端协议）。
        # 收包方向（老客户端→服务端）不做映射，避免破坏服务端识别。
        if getattr(self, "_dst_proto", self.server_protocol) >= self.server_protocol:
            return
        try:
            src_id = int(getattr(src_item, "id", 0) or 0)
        except Exception:
            src_id = 0
        try:
            src_bri = int(getattr(src_item, "block_runtime_id", 0) or 0)
        except Exception:
            src_bri = 0

        new_id = None
        reason = None
        if src_id == 0 and src_bri > 0:
            # 方块物品：block_runtime_id 是关键键（不是 wiki 上的方块数字 ID）
            # debug 模式打印真实 bri，玩家照此填「替换方块」右边的 1.26 方块 ID 即可命中
            if self._debug_log:
                hint = ("已映射→" + str(self.item_id_map[src_bri])) if src_bri in self.item_id_map else "未映射(走兜底)"
                log.info("[方块ID诊断] 服务端方块物品 block_runtime_id=%s（%s）。若要替换它，请在 config.json 的「替换方块」填：\"1.21显示用的方块id\": \"%s\"",
                         src_bri, hint, src_bri)
            new_id = self.item_id_map.get(src_bri)
            if new_id is None and self.fallback_item_id > 0:
                new_id = self.fallback_item_id
                reason = "方块兜底"
        elif src_id > 0:
            new_id = self.item_id_map.get(src_id)
            if new_id is not None:
                reason = "物品映射"

        if new_id is None or new_id <= 0:
            return
        try:
            cur_id = int(getattr(built_item, "id", 0) or 0)
        except Exception:
            cur_id = 0
        if cur_id == new_id:
            return
        try:
            setattr(built_item, "id", new_id)
            self.remapped_items += 1
            if self._debug_log and reason:
                log.info("物品ID映射(%s): bri=%s id=%s -> %s", reason, src_bri, src_id, new_id)
        except Exception:
            pass

    # 调试开关（由外部按需打开）
    _debug_log = False

    def _copy_fields(self, src, dst, depth: int = 0, aliases: Optional[dict] = None) -> int:
        """把 src 的字段复制到 dst（dst 已实例化）。返回成功复制数。
        aliases: 包级字段别名映射，仅顶层包翻译时传入。
        """
        copied = 0
        aliases = aliases or {}
        # 已成功写入的目标字段（避免别名重复写）
        written = set()
        for f in _fields(src):
            try:
                v = getattr(src, f)
            except Exception:
                continue
            # 目标字段名：先原名，原名不存在时用别名
            target_f = f
            try:
                tv = getattr(dst, f)
            except Exception:
                target_f = aliases.get(f, f)
                if target_f == f:
                    continue  # 目标无此字段且无别名 → 丢弃
                try:
                    tv = getattr(dst, target_f)
                except Exception:
                    continue
            if target_f in written:
                continue
            ttn = type(tv).__name__
            # 标量适配
            ok, av = self._adapt_scalar(v, ttn)
            if ok:
                try:
                    setattr(dst, target_f, av)
                    written.add(target_f)
                    copied += 1
                except Exception:
                    pass
                continue
            # 嵌套对象：类型名不同的同名类（NetworkBlockPosition/BlockPos 等）
            if hasattr(v, "__dict__") or (hasattr(v, "_fields") and not isinstance(v, (list, tuple))):
                pass
            # 用 dir 判断是否为对象（非标量非容器）
            is_obj = not isinstance(v, (str, bytes, bytearray, int, float, bool, type(None), list, tuple, dict, enum.Enum))
            if is_obj:
                # 尝试1：目标字段类型对应的类（同名）
                tcls = self._find_class(self._dst_proto, type(tv).__name__)
                if tcls is not None:
                    built = self._build_nested(v, tcls, depth + 1)
                    if built is not None:
                        # 物品描述符：修正物品 id，避免方块物品变空气
                        if self._is_item_descriptor(v) or self._is_item_descriptor(built):
                            self._remap_item_descriptor(v, built, depth + 1)
                        try:
                            setattr(dst, target_f, built)
                            written.add(target_f)
                            copied += 1
                        except Exception:
                            pass
                    continue
                # 尝试2：目标类未作为模块顶层导出（BlockPos/Vec3/CopyableDataList 等）。
                # tv=getattr(dst,f) 是 nanobind 只读临时副本，直接改它不会写回 dst。
                # 用 type(tv)() 构造真正的目标对象，填好后整体赋值回 dst。
                try:
                    built = type(tv)()
                except Exception:
                    continue
                self._copy_fields(v, built, depth + 1)
                # 物品描述符：修正物品 id，避免方块物品变空气
                if self._is_item_descriptor(v) or self._is_item_descriptor(built):
                    self._remap_item_descriptor(v, built, depth + 1)
                try:
                    setattr(dst, target_f, built)
                    written.add(target_f)
                    copied += 1
                except Exception:
                    pass
                continue
            # list/tuple：逐元素转换
            if isinstance(v, (list, tuple)):
                newlist = []
                for item in v:
                    item_ok, item_av = self._adapt_scalar(item, ttn)
                    if item_ok:
                        newlist.append(item_av)
                    else:
                        # 元素是对象
                        is_item_obj = not isinstance(item, (str, bytes, bytearray, int, float, bool, type(None), dict, enum.Enum))
                        if is_item_obj and hasattr(tv, "__iter__"):
                            # 目标 list 元素类型难取，尝试用源元素类型名在目标模块找同名类
                            itcls = self._find_class(self._dst_proto, type(item).__name__)
                            if itcls:
                                built = self._build_nested(item, itcls, depth + 1)
                                if built is not None:
                                    # 物品描述符列表元素：修正 id 避免变空气
                                    if self._is_item_descriptor(item) or self._is_item_descriptor(built):
                                        self._remap_item_descriptor(item, built, depth + 1)
                                    newlist.append(built)
                        elif isinstance(item, enum.Enum) and ttn == "int":
                            newlist.append(int(item))
                if newlist:
                    try:
                        setattr(dst, target_f, newlist)
                        written.add(target_f)
                        copied += 1
                    except Exception:
                        pass
        return copied

    # ---------- 主翻译入口 ----------
    def _translate_generic(self, pid: int, payload: bytes,
                           src_proto: int, dst_proto: int) -> Optional[bytes]:
        """通用递归翻译：src_proto 编码的 payload → dst_proto 编码。"""
        self._dst_proto = dst_proto  # 供 _copy_fields 查找目标类
        src_cls = _packet_class(src_proto, pid)
        dst_cls = _packet_class(dst_proto, pid)
        if src_cls is None or dst_cls is None:
            return None
        try:
            src_obj = _module(src_proto).decode(src_cls, bytes(payload))
        except Exception as e:
            log.debug("deep decode失败 pid=%d %d->%d: %s", pid, src_proto, dst_proto, e)
            return None
        try:
            dst_obj = dst_cls()
        except Exception:
            return None
        try:
            self._copy_fields(src_obj, dst_obj, 0, aliases=FIELD_ALIASES.get(pid))
        except Exception as e:
            log.debug("deep copy失败 pid=%d: %s", pid, e)
            return None
        try:
            return bytes(_module(dst_proto).encode(dst_obj))
        except Exception as e:
            log.debug("deep encode失败 pid=%d: %s", pid, e)
            return None

    def translate_send(self, pid: int, payload: bytes) -> Tuple[str, Optional[bytes]]:
        """
        服务器(2225) → 老客户端。返回 (动作, 数据)：
          ('pass', payload)  字节兼容/无需翻译，原样放行
          ('drop', None)     老客户端独有包，丢弃
          ('translate', bytes)  已翻译
          ('fail', payload)  翻译失败，放行原包（保守）
        """
        if pid in self._only_server_pids():
            self.dropped_send += 1
            return ("drop", None)
        if pid in self._special_send:
            out = self._special_send[pid](pid, payload)
            if out is not None:
                self.translated_send += 1
                return ("translate", out)
            # 特殊处理器不适用（如目标本身就是 body 结构）→ 降级走通用递归
        out = self._translate_generic(pid, payload, self.server_protocol, self.client_protocol)
        if out is None:
            self.failed_send += 1
            return ("fail", payload)
        self.translated_send += 1
        return ("translate", out)

    def translate_recv(self, pid: int, payload: bytes) -> Tuple[str, Optional[bytes]]:
        """老客户端 → 服务器(2225)。"""
        if pid in self._only_client_pids():
            self.dropped_recv += 1
            return ("drop", None)
        if pid in self._special_recv:
            out = self._special_recv[pid](pid, payload)
            if out is not None:
                self.translated_recv += 1
                return ("translate", out)
            # 特殊处理器不适用（如源本身就是 body 结构）→ 降级走通用递归
        out = self._translate_generic(pid, payload, self.client_protocol, self.server_protocol)
        if out is None:
            self.failed_recv += 1
            return ("fail", payload)
        self.translated_recv += 1
        return ("translate", out)

    # ---------- 特殊包处理器 ----------
    def _register_special(self):
        # TextPacket(pid=9)：2225 把 type/message/params 打包进 body(MessageOnly)，
        # 老版本是平铺字段。需要显式拆包。
        self._special_send[9] = self._text_send
        self._special_recv[9] = self._text_recv

    def _text_send(self, pid, payload):
        """body 结构 → 平铺 message 结构：从 body 拆出 message/type。

        仅当源是 body 结构、目标是平铺结构时才需要特殊处理。
        两端结构相同（都平铺或都 body）时返回 None，交给通用递归——
        这样同一个翻译器可用于任意相邻版本阶梯，而非写死 2225。
        """
        scls = _packet_class(self.server_protocol, pid)
        ccls = _packet_class(self.client_protocol, pid)
        if not scls or not ccls:
            return None
        try:
            src = _module(self.server_protocol).decode(scls, bytes(payload))
        except Exception:
            return None
        try:
            dst = ccls()
            src_has_body = hasattr(src, "body")
            dst_has_body = hasattr(dst, "body")
            # 结构相同 → 通用递归即可正确保真，无需特殊拆包
            if src_has_body == dst_has_body:
                return None
            # 目标是 body 结构（源平铺）→ 不是本处理器职责，交给通用递归
            if dst_has_body:
                return None
            body = getattr(src, "body", None)
            if body is not None:
                # MessageOnly 有 message/type
                msg = getattr(body, "message", "")
                typ = getattr(body, "type", None)
                setattr(dst, "message", msg if isinstance(msg, str) else "")
                if typ is not None:
                    try:
                        setattr(dst, "type", typ)
                    except Exception:
                        pass
            # 其余平铺字段
            for f in ("platform_id", "xuid", "localize", "filtered_message"):
                try:
                    v = getattr(src, f)
                    if v is not None:
                        setattr(dst, f, v)
                except Exception:
                    pass
            return bytes(_module(self.client_protocol).encode(dst))
        except Exception:
            return None

    def _text_recv(self, pid, payload):
        """平铺 message 结构 → body 结构：把 message/type 打包进 body。

        仅当源是平铺结构、目标是 body 结构时才需要特殊处理，
        结构相同则交给通用递归（支持任意相邻版本阶梯）。
        """
        ccls = _packet_class(self.client_protocol, pid)
        scls = _packet_class(self.server_protocol, pid)
        if not ccls or not scls:
            return None
        try:
            src = _module(self.client_protocol).decode(ccls, bytes(payload))
        except Exception:
            return None
        src_has_body = hasattr(src, "body")
        # 源已经是 body 结构 → 交给通用递归
        if src_has_body:
            return None
        try:
            dst = scls()
            # 目标没有 body（两端都平铺）→ 交给通用递归
            if not hasattr(dst, "body"):
                return None
            body = getattr(dst, "body", None)
            if body is not None:
                msg = getattr(src, "message", "")
                typ = getattr(src, "type", None)
                # nanobind 的 obj.body 是只读临时副本：直接改它的字段不会写回 obj。
                # 必须新建同类型对象、填好后整体赋值回 dst.body。
                body_cls = type(body)
                new_body = body_cls()
                if isinstance(msg, str):
                    try:
                        setattr(new_body, "message", msg)
                    except Exception:
                        pass
                if typ is not None:
                    try:
                        setattr(new_body, "type", typ)
                    except Exception:
                        pass
                try:
                    setattr(dst, "body", new_body)
                except Exception:
                    pass
            for f in ("platform_id", "xuid", "localize"):
                try:
                    v = getattr(src, f)
                    if v is not None:
                        setattr(dst, f, v)
                except Exception:
                    pass
            return bytes(_module(self.server_protocol).encode(dst))
        except Exception:
            return None
