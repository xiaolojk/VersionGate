# -*- coding: utf-8 -*-
"""
VersionGate - Endstone 插件 (v1.3 1.26.x 精确兼容版)
====================================================
功能：
1. 版本准入：拦截客户端登录握手包，将客户端上报的协议号改写为服务器协议号，
   让 Minecraft Bedrock 1.26.x 各小版本客户端都能进入服务器。
2. 进服通报：玩家进入服务器后，全服广播
   "[名称]该玩家是[游戏版本]，欢迎你加入服务器！"

版本演进：
- v1.0 初版：协议号改写 + 进服通报
- v1.1 性能优化：预编译 struct、预计算目标字节、改写决策 LRU 缓存、热路径局部变量化
- v1.2 健壮性/可观测性：双检锁缓存、通报模板预解析、可选运行统计
- v1.3 1.26.x 精确兼容：
  * 协议号判定从"模糊差值"改为"1.26.x 协议号精确白名单"，杜绝误伤 1.21.x 等跨代版本
  * 内置 1.26.x 协议号 → 版本区间反查表（minecraft.wiki 权威数据）
  * 按协议号分档统计改写/跳过次数，能看到实际进来的是哪些版本
  * 保留 v1.2 全部并发安全与性能优化

关于 1.26.x 协议号（来源 minecraft.wiki Bedrock Protocol version numbers）：
  2225 → 26.60 / 26.61 / 26.62 / 26.63 / 26.65 / 26.66   （服务器本代）
  2193 → 26.50 / 26.51 / 26.52
  2169 → 26.45
  2168 → 26.40 / 26.41 / 26.42 / 26.43 / 26.44
  1001 → 26.30 / 26.31 / 26.32 / 26.33 / 26.34 / 26.35
  975  → 26.20 / 26.21 / 26.22 / 26.23
  944  → 26.10 / 26.11 / 26.12 / 26.13
  924  → 1.26.0 / 26.1 / 26.2 / 26.3
"""
import struct
import threading
from collections import OrderedDict

from endstone import ColorFormat
from endstone.event import event_handler, PlayerJoinEvent, PacketReceiveEvent
from endstone.plugin import Plugin

# 基岩版数据包 ID（不含包头）
PID_LOGIN = 0x01                     # Login 包
PID_REQUEST_NETWORK_SETTINGS = 0xC1  # RequestNetworkSettings 包（协议协商）

# 预编译：大端 int32 结构体，模块级单例
_S32BE = struct.Struct(">i")
_UNPACK_S32BE = _S32BE.unpack_from
_PACK_S32BE = _S32BE.pack

# 改写决策缓存上限
_REWRITE_CACHE_MAX = 128

# 缓存哨兵
_MISS = object()
_SKIP = object()

# 通报模板支持的占位符
_TEMPLATE_FIELDS = ("name", "version")

# ---------------------------------------------------------------------------
# v1.3：1.26.x 协议号 → 版本区间反查表（minecraft.wiki 权威数据）
# ---------------------------------------------------------------------------
PROTOCOL_126X = {
    2225: "26.60~26.66",
    2193: "26.50~26.52",
    2169: "26.45",
    2168: "26.40~26.44",
    1001: "26.30~26.35",
    975:  "26.20~26.23",
    944:  "26.10~26.13",
    924:  "1.26.0~26.3",
}
# 默认白名单 = 上表全部协议号；可由 config.toml 的 allowed_protocols 覆盖
DEFAULT_ALLOWED_PROTOCOLS = sorted(PROTOCOL_126X.keys())


def _proto_label(proto: int) -> str:
    """把协议号转成可读版本标签，未知返回 '协议号N'。"""
    return PROTOCOL_126X.get(proto, "协议号%d" % proto)


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


class VersionGatePlugin(Plugin):
    api_version = "0.11"

    def on_load(self) -> None:
        pass

    def on_enable(self) -> None:
        try:
            self.save_default_config()
        except Exception as e:
            self.logger.warning("生成默认配置失败，使用内置默认值: %s" % e)
        self._server_protocol = int(self.server.protocol_version)
        self._minecraft_version = str(self.server.minecraft_version)
        cfg = self.config or {}

        self._bypass_enabled = bool(cfg.get("enable_version_bypass", True))
        self._announce_enabled = bool(cfg.get("enable_join_announcement", True))
        template = str(cfg.get("announcement_template",
                       "[{name}]该玩家是[{version}]，欢迎你加入服务器！"))
        self._debug = bool(cfg.get("debug", False))

        # v1.3：精确协议号白名单。只改写白名单内的协议号，杜绝误伤 1.21.x 等跨代版本。
        allowed = cfg.get("allowed_protocols", DEFAULT_ALLOWED_PROTOCOLS)
        if not isinstance(allowed, list):
            allowed = DEFAULT_ALLOWED_PROTOCOLS
        self._allowed = set()
        for v in allowed:
            try:
                self._allowed.add(int(v))
            except Exception:
                pass
        # 差值上限作为最后兜底（默认 1400 覆盖 2225-924=1301，足够容纳整个 1.26.x）
        self._max_protocol_diff = int(cfg.get("max_protocol_diff", 1400))

        # ---- v1.1/v1.2 性能优化保留 ----
        self._target_bytes = _PACK_S32BE(self._server_protocol)
        self._rewrite_cache = OrderedDict()
        self._cache_max = _REWRITE_CACHE_MAX
        self._cache_lock = threading.Lock()
        self._enable_stats = bool(cfg.get("enable_stats", False))
        # 统计：[0]命中 [1]未命中 [2]改写 [3]跳过
        self._stats = [0, 0, 0, 0]
        # v1.3：按协议号分档统计 {proto: [改写数, 跳过数]}
        self._proto_stats = {}
        self._stats_lock = threading.Lock()

        self._template_segments = _parse_template(template)
        self._msg_prefix = ColorFormat.YELLOW
        self._msg_suffix = ColorFormat.RESET

        self.register_events(self)
        whitelist = ", ".join(str(p) for p in sorted(self._allowed))
        self.logger.info(
            "VersionGate v1.3 已启用 | 服务器协议号=%d | MC版本=%s | 版本绕行=%s | 进服通报=%s | "
            "1.26.x白名单=[%s] | 决策缓存=%d"
            % (self._server_protocol, self._minecraft_version,
               self._bypass_enabled, self._announce_enabled, whitelist, self._cache_max)
        )

    def on_disable(self) -> None:
        if self._enable_stats:
            hit, miss, rewrite, skip = self._stats
            total = hit + miss
            rate = (100.0 * hit / total) if total else 0.0
            self.logger.info(
                "VersionGate 运行统计 | 缓存命中=%d/%d (%.1f%%) | 改写=%d | 跳过=%d"
                % (hit, total, rate, rewrite, skip)
            )
            if self._proto_stats:
                lines = []
                for proto in sorted(self._proto_stats, reverse=True):
                    rw, sk = self._proto_stats[proto]
                    lines.append("%s(%d):改写%d/跳过%d" % (_proto_label(proto), proto, rw, sk))
                self.logger.info("VersionGate 分档统计 | " + " | ".join(lines))
        self.logger.info("VersionGate 已禁用")

    # ---------------- 版本准入：改写登录包协议号 ----------------
    @event_handler
    def on_packet_receive(self, event: PacketReceiveEvent) -> None:
        if not self._bypass_enabled:
            return
        pid = event.packet_id
        if pid != PID_LOGIN and pid != PID_REQUEST_NETWORK_SETTINGS:
            return
        payload = event.payload
        if payload is None or len(payload) < 4:
            return

        try:
            new_payload = self._rewrite(payload, pid)
            if new_payload is not None:
                event.payload = new_payload
                if self._debug:
                    self.logger.info("已改写协议号: 包ID=%d 长度 %d" % (pid, len(new_payload)))
        except Exception as e:
            # 绝不因改写失败而阻断玩家连接：原样放行
            if self._debug:
                self.logger.warning("协议号改写跳过(包ID=%d): %s" % (pid, e))

    def _rewrite(self, payload: bytes, pid: int = PID_LOGIN):
        """把 payload 开头的协议号改写为服务器协议号。返回新字节或 None。"""
        head = payload[:4]
        cache = self._rewrite_cache
        stats = self._stats
        enable = self._enable_stats

        cached = cache.get(head, _MISS)
        if cached is _MISS:
            cached = self._decide(head, pid, stats, enable)
        elif enable:
            stats[0] += 1  # 命中

        if cached is _SKIP:
            if enable:
                stats[3] += 1  # 跳过
            return None
        if enable:
            stats[2] += 1  # 改写
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
                stats[1] += 1  # 未命中
            client_proto = _UNPACK_S32BE(head, 0)[0]

            # v1.3 精确判定：
            # 1) 协议号合法  2) 必须在 1.26.x 白名单内  3) 与服务器有差异  4) 差值在兜底上限内
            in_whitelist = client_proto in self._allowed
            diff = client_proto - self._server_protocol
            if (client_proto <= 0 or client_proto > 100000
                    or not in_whitelist
                    or client_proto == self._server_protocol
                    or diff > self._max_protocol_diff or diff < -self._max_protocol_diff):
                decision = _SKIP
            else:
                decision = self._target_bytes

            # v1.3：分档统计（独立加锁）
            if enable:
                with self._stats_lock:
                    rec = self._proto_stats.setdefault(client_proto, [0, 0])
                    rec[0 if decision is not _SKIP else 1] += 1

            # LRU 缓存决策
            if len(cache) >= self._cache_max:
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

    # ---------------- 进服通报 ----------------
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
