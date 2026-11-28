# -*- coding: utf-8 -*-
"""
ProtocolBridge Plugin v2.0 — Endstone 基岩版 1.26.x 跨版本完整兼容插件
======================================================================
一个插件完成两件事（无需任何 v1.x 配套）：

  1. 版本准入（握手阶段）：
     拦截登录包，把客户端上报的协议号按 1.26.x 白名单改写为服务器协议号，
     让 1.26.x 各小版本客户端连进来；并在进服时全服通报真实游戏版本。

  2. 数据包翻译（游戏阶段）：
     把旧版客户端的游戏包按字段翻译给新版服务器，反向也翻译，
     解决"能进服但包结构对不上"导致的皮肤错乱/物品异常等问题。

设计原则（绝不搞崩服务器）：
  1. 自适应明文检测：运行时自检 payload 能否被官方库 decode；
     失败（密文/不支持协议）则进入 PASSIVE 模式——仍做协议号改写，
     但跳过包翻译，绝不影响服务器。
  2. 白名单改写：只改写 1.26.x 协议号，杜绝误伤 1.21.x 等跨代版本。
  3. 白名单翻译：只翻译实测需要翻译的 8 个包，其余零开销放行。
  4. 失败放行：任何异常都吞掉并原样放行，绝不阻断玩家连接。

依赖：bedrock-protocol >= 0.5.1（需 Python 3.12）
作者：橘子
"""
import struct
import threading
from collections import OrderedDict
from pathlib import Path

from endstone import ColorFormat
from endstone.event import event_handler, PacketReceiveEvent, PacketSendEvent, PlayerJoinEvent
from endstone.plugin import Plugin

try:
    from .protocol_bridge import ProtocolBridgeCore, SUPPORTED_PROTOCOLS, _packet_class, _module
    from .version_gate import VersionGateConfig, FOLDER_NAME
    from .deep_translator import DeepTranslator
    from .connection_state import ConnectionStateRegistry, CONTEXT_FIELDS
    from .json_config import ensure_config_json, load_config_json, CONFIG_NAME
except ImportError:  # 兼容直接运行/测试
    from protocol_bridge import ProtocolBridgeCore, SUPPORTED_PROTOCOLS, _packet_class, _module
    from version_gate import VersionGateConfig, FOLDER_NAME
    from deep_translator import DeepTranslator
    from connection_state import ConnectionStateRegistry, CONTEXT_FIELDS
    from json_config import ensure_config_json, load_config_json, CONFIG_NAME

# 1.21.x 与 1.26.x 的分界：924 是 1.26.0，以下都是 1.21.x
# 走深度翻译的最大客户端协议号（含）。924=1.26.0 与 2225 的方块描述符/物品
# 嵌套结构差异大，浅字段复制会把 BlockPos.y 算错、物品描述符整体赋值失败；
# 实测深度翻译能正确重建，故把 924 纳入深度路径。944+ 仍走小差异翻译。
DEEP_TRANSLATE_MAX_PROTO = 924


def _module_ok(proto: int) -> bool:
    """官方库是否支持该协议号（有无对应解码模块）。"""
    try:
        from protocol_bridge import _module as _m
    except ImportError:
        from .protocol_bridge import _module as _m
    return _m(proto) is not None

# 基岩版数据包 ID（不含包头）
PID_LOGIN = 0x01                     # Login 包
PID_REQUEST_NETWORK_SETTINGS = 0xC1  # RequestNetworkSettings 包（协议协商）
_HANDSHAKE_PIDS = (PID_LOGIN, PID_REQUEST_NETWORK_SETTINGS)

# 预编译大端 int32
_S32BE = struct.Struct(">i")
_UNPACK_S32BE = _S32BE.unpack_from
_PACK_S32BE = _S32BE.pack

_REWRITE_CACHE_MAX = 128
_MISS = object()
_SKIP = object()
_TEMPLATE_FIELDS = ("name", "version")

# 协议号 → 版本区间（minecraft.wiki 权威数据）
# 覆盖 1.21.x 全系 + 1.26.x 全系，供白名单改写与进服通报识别
PROTOCOL_VERSIONS = {
    # ---- 1.26.x ----
    2225: "26.60~26.66",
    2193: "26.50~26.52",
    2169: "26.45",
    2168: "26.40~26.44",
    1001: "26.30~26.35",
    975:  "26.20~26.23",
    944:  "26.10~26.13",
    924:  "1.26.0~26.3",
    # ---- 1.21.x ----
    898:  "1.21.130~1.21.132",
    860:  "1.21.124",
    859:  "1.21.120~1.21.123",
    844:  "1.21.111~1.21.114",
    827:  "1.21.100~1.21.101",
    819:  "1.21.92~1.21.94",
    818:  "1.21.90~1.21.91",
    800:  "1.21.80~1.21.84",
    786:  "1.21.70~1.21.73",
    776:  "1.21.60~1.21.62",
    766:  "1.21.50~1.21.51",
    748:  "1.21.40~1.21.44",
    729:  "1.21.30~1.21.31",
    712:  "1.21.20~1.21.23",
    686:  "1.21.2~1.21.3",
    685:  "1.21.0~1.21.1",
}
# 兼容旧变量名（外部若有引用）
PROTOCOL_126X = PROTOCOL_VERSIONS

# 默认白名单：1.21.x 全系 + 1.26.x 全系
DEFAULT_ALLOWED_PROTOCOLS = sorted(PROTOCOL_VERSIONS.keys())

# 1.21.x 协议号集合（用于分类判定）
PROTOCOL_121X = {685, 686, 712, 729, 748, 766, 776, 786, 800,
                 818, 819, 827, 844, 859, 860, 898}


def _proto_label(proto: int) -> str:
    """把协议号转成可读版本标签，未知返回 '协议号N'。"""
    return PROTOCOL_VERSIONS.get(proto, "协议号%d" % proto)


def _parse_template(template: str):
    """把通报模板预解析为片段序列 [(kind, value), ...]。kind=0 字面量，1 字段。"""
    segments = []
    buf = []
    i = 0
    n = len(template)
    while i < n:
        if template[i] == "{":
            j = template.find("}", i + 1)
            if j != -1 and template[i + 1:j] in _TEMPLATE_FIELDS:
                if buf:
                    segments.append((0, "".join(buf)))
                    buf = []
                segments.append((1, template[i + 1:j]))
                i = j + 1
                continue
        buf.append(template[i])
        i += 1
    if buf:
        segments.append((0, "".join(buf)))
    return segments


class ProtocolBridgePlugin(Plugin):
    api_version = "0.11"

    def on_enable(self) -> None:
        # 优先从 data_folder 读取 JSON 配置（config.json，支持 # 注释）；
        # 测试环境无 data_folder 时沿用 self.config（Endstone 注入的 config.toml）。
        data_folder = getattr(self, "data_folder", None)
        json_cfg = None
        if data_folder is not None:
            try:
                created = ensure_config_json(data_folder)
                json_cfg = load_config_json(data_folder)
                if created and hasattr(self, "logger"):
                    self.logger.info("已生成 JSON 配置文件: %s（编辑后重启生效）"
                                     % str(Path(data_folder) / CONFIG_NAME))
            except Exception as e:
                if hasattr(self, "logger"):
                    self.logger.warning("读取 config.json 失败，沿用默认配置: %s" % e)
                json_cfg = None
        cfg = json_cfg if json_cfg else (self.config or {})

        self._server_protocol = int(self.server.protocol_version)
        self._minecraft_version = str(self.server.minecraft_version)
        self._debug = bool(cfg.get("debug", False))
        self._enable_stats = bool(cfg.get("enable_stats", True))

        # ---------- 功能 1：握手协议号改写 + 进服通报 ----------
        self._bypass_enabled = bool(cfg.get("enable_version_bypass", True))
        self._announce_enabled = bool(cfg.get("enable_join_announcement", True))
        template = str(cfg.get("announcement_template",
                       "[{name}]该玩家是[{version}]，欢迎你加入服务器！"))
        self._template_segments = _parse_template(template)
        self._msg_prefix = ColorFormat.YELLOW
        self._msg_suffix = ColorFormat.RESET

        allowed = cfg.get("allowed_protocols", DEFAULT_ALLOWED_PROTOCOLS)
        if not isinstance(allowed, list):
            allowed = DEFAULT_ALLOWED_PROTOCOLS
        self._allowed = set()
        for v in allowed:
            try:
                self._allowed.add(int(v))
            except Exception:
                pass
        # 默认 1540 覆盖 1.21.0(685)~1.26.65(2225) 全跨度；主判定以白名单为准
        self._max_protocol_diff = int(cfg.get("max_protocol_diff", 1540))

        self._target_bytes = _PACK_S32BE(self._server_protocol)
        self._rewrite_cache = OrderedDict()
        self._cache_lock = threading.Lock()
        self._rw_stats = [0, 0, 0, 0]  # 命中/未命中/改写/跳过
        self._proto_stats = {}
        self._stats_lock = threading.Lock()

        # ---------- 功能 3b：方块替换映射（防止方块物品变空气）----------
        self._item_id_map = {}
        self._enable_item_remap = bool(cfg.get("enable_item_remap", True))
        self._fallback_item_id = int(cfg.get("fallback_item_id", 1) or 0)

        # 新格式（config.json 的 [替换方块]）：键为 1.21方块id，值为 1.26方块id。
        # 即 "1.21id": "1.26id"。翻译器内部需要 {1.26: 1.21}，故这里反转存储。
        raw_replace = cfg.get("替换方块", None)
        if isinstance(raw_replace, dict):
            for k, v in raw_replace.items():
                try:
                    id_121, id_126 = int(k), int(v)
                    if id_121 > 0 and id_126 > 0:
                        self._item_id_map[id_126] = id_121
                except Exception:
                    continue

        # 兼容旧格式 item_id_map（左边 1.26新id，右边 1.21已有id）
        raw_id_map = cfg.get("item_id_map", None)
        if isinstance(raw_id_map, dict):
            for k, v in raw_id_map.items():
                try:
                    ki, vi = int(k), int(v)
                    if ki > 0 and vi > 0:
                        self._item_id_map[ki] = vi
                except Exception:
                    continue

        # ---------- 功能 3：VersionGate 物品名替换（1.26新物品→1.21替代）----------
        self._vg_config = None
        self._item_map = {}
        self._item_replace_enabled = False
        try:
            data_folder = getattr(self, "data_folder", None)
            if data_folder is None:
                raise RuntimeError("无 data_folder（测试环境）")
            self._vg_config = VersionGateConfig(str(data_folder))
            created = self._vg_config.ensure_config()
            item_map, vg_settings = self._vg_config.load()
            self._item_map = item_map
            self._item_replace_enabled = bool(
                vg_settings.get("enable_item_replacement", True)
            ) and bool(item_map)
            if created:
                self.logger.info(
                    "VersionGate 已生成配置文件夹: %s（物品映射请在此文件编辑，重启生效）"
                    % str(Path(data_folder) / FOLDER_NAME)
                )
            if self._item_replace_enabled:
                self.logger.info("VersionGate 已加载 %d 条物品映射" % len(item_map))
        except Exception as e:
            # 测试环境/无数据目录时静默降级，不影响核心功能
            self.logger.info("VersionGate 未启用（%s）" % e)

        # ---------- 功能 2：游戏包翻译 ----------
        self._translation_enabled = bool(cfg.get("enable_translation", True))
        self._strict = bool(cfg.get("strict", False))
        only = cfg.get("only_packet_ids", None)
        manual_ids = set(int(x) for x in only) if isinstance(only, list) and only else None

        # 按档位预探测各版本到服务器版本的精确差异包表（启动期一次性开销）。
        # 老档位(924~1001)到 2225 有 26~40 个不兼容包，远多于 2193 的 7 个；
        # 单用固定 8 包白名单会导致老档位大量包不翻译。
        by_proto = {}
        unsupported = []
        if self._translation_enabled:
            for proto in sorted(self._allowed):
                if proto == self._server_protocol:
                    continue
                if manual_ids is not None:
                    by_proto[proto] = set(manual_ids)  # 用户显式指定则完全遵循
                    continue
                ids = ProtocolBridgeCore.probe_required_ids(proto, self._server_protocol)
                if ids is None:
                    unsupported.append(proto)  # 如 2169：库不支持，降级为只改协议号
                    continue
                by_proto[proto] = ids
        self._translate_map = by_proto
        self._probe_count_by_proto = {p: len(v) for p, v in by_proto.items()}

        self._core = ProtocolBridgeCore(
            server_protocol=self._server_protocol,
            strict=self._strict,
            only_ids=(manual_ids if manual_ids is not None
                      else ProtocolBridgeCore.known_translate_ids()),  # 兜底
            only_ids_by_proto=by_proto if by_proto else None,
            item_map=self._item_map,
            item_replace_enabled=self._item_replace_enabled,
        )

        # 1.21.x 深度翻译器（跨大版本）：每个档位一个实例，启动时建好，运行期零开销
        self._deep_enabled = bool(cfg.get("enable_deep_translation", True))
        self._deep: dict = {}
        if self._deep_enabled and self._translation_enabled:
            for proto in sorted(self._allowed):
                if proto <= DEEP_TRANSLATE_MAX_PROTO and proto != self._server_protocol:
                    try:
                        if _module_ok(proto):
                            self._deep[proto] = DeepTranslator(
                                self._server_protocol, proto,
                                item_id_map=self._item_id_map,
                                fallback_item_id=self._fallback_item_id,
                                enable_item_remap=self._enable_item_remap,
                                debug_log=self._debug,
                            )
                    except Exception as e:
                        self.logger.warning("深度翻译器初始化失败 协议%d: %s" % (proto, e))

        # 每个地址/subclient 对应的客户端真实协议号（从登录包记录，供翻译用）
        self._client_proto: dict = {}
        self._proto_lock = threading.Lock()

        # 连接级状态（借鉴 ViaVersion Storable）：追踪每个玩家的 StartGame registry 上下文
        self._connections = ConnectionStateRegistry()
        self._track_context = bool(cfg.get("enable_connection_context", True))

        # 自适应模式：None=未检测，True=明文可翻译，False=被动（仍改协议号但跳过翻译）
        self._plaintext_confirmed = None
        self._probe_count = 0

        self.register_events(self)
        whitelist = ", ".join(str(p) for p in sorted(self._allowed))
        vg_state = "启用(%d条)" % len(self._item_map) if self._item_replace_enabled else "关闭"
        remap_state = ("启用(%d条映射,兜底id=%d)" % (len(self._item_id_map), self._fallback_item_id)) \
            if self._enable_item_remap else "关闭"
        self.logger.info(
            "ProtocolBridge v3.2.0 已启用（握手准入+深度包翻译+VersionGate物品替换+防空气，无需 v1.x）| "
            "服务器协议=%d | MC版本=%s | 协议号改写=%s | 进服通报=%s | 包翻译=%s | "
            "物品名替换=%s | 方块替换/防空气=%s | 白名单(1.21.x+1.26.x)=[%s]"
            % (self._server_protocol, self._minecraft_version,
               self._bypass_enabled, self._announce_enabled, self._translation_enabled,
               vg_state, remap_state, whitelist)
        )
        if self._translation_enabled and self._probe_count_by_proto:
            detail = " | ".join(
                "%s:%d包" % (_proto_label(p), n)
                for p, n in sorted(self._probe_count_by_proto.items())
            )
            self.logger.info("ProtocolBridge 分档翻译表（各档位→服务器需翻译包数）: " + detail)
        if unsupported:
            self.logger.warning(
                "以下档位不在官方库支持列表，仅改协议号、不翻译包（可正常进服）: %s"
                % ", ".join("%s(%d)" % (_proto_label(p), p) for p in unsupported)
            )
        if self._server_protocol not in SUPPORTED_PROTOCOLS:
            self.logger.warning(
                "服务器协议号 %d 不在官方库支持列表，包翻译可能不可用；"
                "插件将以被动模式运行（协议号改写仍生效）" % self._server_protocol
            )

    def on_disable(self) -> None:
        if self._enable_stats:
            hit, miss, rewrite, skip = self._rw_stats
            total = hit + miss
            rate = (100.0 * hit / total) if total else 0.0
            self.logger.info(
                "ProtocolBridge 握手统计 | 缓存命中=%d/%d (%.1f%%) | 改写=%d | 跳过=%d"
                % (hit, total, rate, rewrite, skip)
            )
            if self._proto_stats:
                lines = []
                for proto in sorted(self._proto_stats, reverse=True):
                    rw, sk = self._proto_stats[proto]
                    lines.append("%s(%d):改写%d/跳过%d" % (_proto_label(proto), proto, rw, sk))
                self.logger.info("ProtocolBridge 分档统计 | " + " | ".join(lines))
            s = self._core.stats
            self.logger.info(
                "ProtocolBridge 翻译统计 | 收包=%d 发包=%d | 已翻译 收=%d 发=%d | "
                "解码失败=%d 编码失败=%d 无包类=%d"
                % (s.received, s.sent, s.translated_rx, s.translated_tx,
                   s.decode_fail, s.encode_fail, s.no_class)
            )
        if self._plaintext_confirmed is False:
            self.logger.warning(
                "ProtocolBridge 包翻译处于被动模式：Endstone 暴露的 payload 无法被官方库解析"
                "（可能是密文层）。协议号改写仍生效，包翻译未生效，服务器不受影响。"
            )
        self.logger.info("ProtocolBridge 已禁用")

    # ================= 功能 1：握手协议号改写 =================
    def _rewrite(self, payload: bytes, pid: int):
        """把 payload 开头的协议号改写为服务器协议号。返回新字节或 None。"""
        head = payload[:4]
        cache = self._rewrite_cache
        stats = self._rw_stats
        enable = self._enable_stats

        cached = cache.get(head, _MISS)
        if cached is _MISS:
            cached = self._decide(head, pid, stats, enable)
        elif enable:
            stats[0] += 1

        if cached is _SKIP:
            if enable:
                stats[3] += 1
            return None
        if enable:
            stats[2] += 1
        return cached + payload[4:]

    def _decide(self, head: bytes, pid: int, stats, enable: bool):
        """（线程安全）解析协议号、按 1.26.x 白名单判定，并写入决策缓存。"""
        cache = self._rewrite_cache
        with self._cache_lock:
            cached = cache.get(head, _MISS)
            if cached is not _MISS:
                if enable:
                    stats[0] += 1
                return cached
            if enable:
                stats[1] += 1
            client_proto = _UNPACK_S32BE(head, 0)[0]

            in_whitelist = client_proto in self._allowed
            diff = client_proto - self._server_protocol
            if (client_proto <= 0 or client_proto > 100000
                    or not in_whitelist
                    or client_proto == self._server_protocol
                    or diff > self._max_protocol_diff or diff < -self._max_protocol_diff):
                decision = _SKIP
            else:
                decision = self._target_bytes

            if enable:
                with self._stats_lock:
                    rec = self._proto_stats.setdefault(client_proto, [0, 0])
                    rec[0 if decision is not _SKIP else 1] += 1

            if len(cache) >= _REWRITE_CACHE_MAX:
                cache.popitem(last=False)
            cache[head] = decision

            if self._debug:
                label = "RequestNetworkSettings" if pid == PID_REQUEST_NETWORK_SETTINGS else "Login"
                verdict = "改写" if decision is not _SKIP else "跳过"
                self.logger.info(
                    "%s 客户端协议号=%d(%s) 服务器=%d → %s"
                    % (label, client_proto, _proto_label(client_proto),
                       self._server_protocol, verdict)
                )
            return decision

    # ================= 记录客户端真实协议号（供翻译用） =================
    def _addr_key(self, event) -> tuple:
        try:
            addr = event.address
            host = getattr(addr, "host", None)
            port = getattr(addr, "port", None)
            return (str(host), port, getattr(event, "sub_client_id", 0))
        except Exception:
            return ("?", 0, 0)

    def _record_client_proto(self, event) -> None:
        """从登录/网络设置包记录客户端真实协议号（改写前读取）。"""
        payload = event.payload
        if not payload or len(payload) < 4:
            return
        try:
            proto = _UNPACK_S32BE(payload, 0)[0]
        except Exception:
            return
        if 0 < proto < 100000:
            with self._proto_lock:
                self._client_proto[self._addr_key(event)] = proto

    def _client_proto_for(self, event) -> int:
        with self._proto_lock:
            return self._client_proto.get(self._addr_key(event), self._server_protocol)

    # ================= 连接级状态（ViaVersion Storable 等价物） =================
    def _capture_start_game(self, event, client_proto: int) -> None:
        """从服务端 StartGame 包捕获 registry 上下文，写入该玩家的连接状态。"""
        try:
            st = self._connections.get(event.address, getattr(event, "sub_client_id", 0),
                                       client_protocol=client_proto, create=True)
            cls = _packet_class(self._server_protocol, 11)
            if cls is None:
                return
            obj = _module(self._server_protocol).decode(cls, bytes(event.payload))
            fields = {}
            for k in CONTEXT_FIELDS:
                try:
                    v = getattr(obj, k)
                    fields[k] = getattr(v, "value", v) if hasattr(v, "value") else v
                except Exception:
                    pass
            st.on_start_game(fields)
            if self._debug:
                self.logger.info("连接状态已捕获 StartGame 上下文: %s" % st.summary())
        except Exception as e:
            if self._debug:
                self.logger.warning("捕获 StartGame 上下文跳过: %s" % e)

    # ================= 自适应明文检测 =================
    def _try_plaintext_probe(self, pid: int, payload: bytes) -> None:
        if self._plaintext_confirmed is not None:
            return
        try:
            from .protocol_bridge import _packet_class, _module
        except ImportError:
            from protocol_bridge import _packet_class, _module
        cls = _packet_class(self._server_protocol, pid)
        if cls is None:
            return
        self._probe_count += 1
        try:
            _module(self._server_protocol).decode(cls, bytes(payload))
            if self._probe_count >= 3:
                self._plaintext_confirmed = True
                self.logger.info("ProtocolBridge 自检：payload 为明文，包翻译已激活")
        except Exception:
            if self._probe_count >= 8:
                self._plaintext_confirmed = False
                self.logger.warning(
                    "ProtocolBridge 自检：payload 无法解析（密文或协议不支持），"
                    "自动进入被动模式（协议号改写仍生效）"
                )

    # ================= 事件：收包 =================
    @event_handler
    def on_packet_receive(self, event: PacketReceiveEvent) -> None:
        pid = event.packet_id
        payload = event.payload
        if not payload:
            return

        # ---- 握手包：记录真实协议号 + 改写协议号（不做包翻译）----
        if pid in _HANDSHAKE_PIDS:
            self._record_client_proto(event)  # 必须在改写前记录真实协议号
            if self._bypass_enabled:
                try:
                    new_payload = self._rewrite(payload, pid)
                    if new_payload is not None:
                        event.payload = new_payload
                except Exception as e:
                    if self._debug:
                        self.logger.warning("协议号改写跳过(包ID=%d): %s" % (pid, e))
            return

        # ---- 游戏包：包翻译 ----
        if not self._translation_enabled:
            return
        if self._plaintext_confirmed is None:
            self._try_plaintext_probe(pid, payload)
        if self._plaintext_confirmed is False:
            return
        client_proto = self._client_proto_for(event)
        if client_proto == self._server_protocol:
            return
        # 1.21.x 走跨大版本深度翻译器
        deep = self._deep.get(client_proto) if self._deep_enabled else None
        if deep is not None and client_proto <= DEEP_TRANSLATE_MAX_PROTO:
            try:
                action, out = deep.translate_recv(pid, payload)
                if action == "drop":
                    # 客户端独有包（老客户端发、服务端不认识）→ 取消事件
                    try:
                        event.cancel()
                    except Exception:
                        pass
                    return
                if out is not None:
                    event.payload = out
                    if self._debug:
                        self.logger.info("深度翻译收包 pid=%d %d->%d" % (pid, client_proto, self._server_protocol))
                    return
                # action=fail：降级走原核心兜底
            except Exception as e:
                if self._debug:
                    self.logger.warning("深度翻译收包跳过 pid=%d: %s" % (pid, e))
        try:
            out = self._core.translate_receive(pid, payload, client_proto)
            if out is not None:
                event.payload = out
                if self._debug:
                    self.logger.info("翻译收包 pid=%d %d->%d" % (pid, client_proto, self._server_protocol))
        except Exception as e:
            if self._debug:
                self.logger.warning("翻译收包跳过 pid=%d: %s" % (pid, e))

    # ================= 事件：发包 =================
    @event_handler
    def on_packet_send(self, event: PacketSendEvent) -> None:
        if not self._translation_enabled or self._plaintext_confirmed is not True:
            return
        client_proto = self._client_proto_for(event)
        if client_proto == self._server_protocol:
            return
        pid = event.packet_id
        payload = event.payload
        if not payload:
            return
        # ---- 连接级状态：捕获 StartGame(pid=11) 下发的 registry 上下文 ----
        # 借鉴 ViaVersion：后续 UpdateBlock/InventorySlot 的 runtime id 依赖它换算
        if self._track_context and pid == 11 and client_proto <= DEEP_TRANSLATE_MAX_PROTO:
            self._capture_start_game(event, client_proto)
        # 1.21.x 走跨大版本深度翻译器
        deep = self._deep.get(client_proto) if self._deep_enabled else None
        if deep is not None and client_proto <= DEEP_TRANSLATE_MAX_PROTO:
            try:
                action, out = deep.translate_send(pid, payload)
                if action == "drop":
                    # 服务端独有包（1.26新功能包，老客户端不认识）→ 取消发送
                    try:
                        event.cancel()
                    except Exception:
                        pass
                    return
                if out is not None:
                    event.payload = out
                    if self._debug:
                        self.logger.info("深度翻译发包 pid=%d %d->%d" % (pid, self._server_protocol, client_proto))
                    return
                # action=fail：降级走原核心兜底
            except Exception as e:
                if self._debug:
                    self.logger.warning("深度翻译发包跳过 pid=%d: %s" % (pid, e))
        try:
            out = self._core.translate_send(pid, payload, client_proto)
            if out is not None:
                event.payload = out
                if self._debug:
                    self.logger.info("翻译发包 pid=%d %d->%d" % (pid, self._server_protocol, client_proto))
        except Exception as e:
            if self._debug:
                self.logger.warning("翻译发包跳过 pid=%d: %s" % (pid, e))

    # ================= 事件：进服通报 =================
    @event_handler
    def on_player_join(self, event: PlayerJoinEvent) -> None:
        if not self._announce_enabled:
            return
        player = event.player
        try:
            name = player.name
            version = player.game_version or self._minecraft_version
        except Exception:
            name = "未知玩家"
            version = self._minecraft_version

        parts = []
        for kind, value in self._template_segments:
            parts.append(value if kind == 0 else (name if value == "name" else version))
        message = "".join(parts)

        self.server.broadcast_message(self._msg_prefix + message + self._msg_suffix)
        self.logger.info("进服通报: %s (版本 %s)" % (name, version))
