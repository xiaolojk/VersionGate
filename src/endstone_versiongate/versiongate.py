# -*- coding: utf-8 -*-
"""
VersionGate - Endstone 插件 (v1.2 健壮性 + 可观测性优化版)
功能：
1. 版本准入：拦截客户端登录握手包，将客户端上报的协议号改写为服务器协议号，
   让 Minecraft Bedrock 1.26.x 各小版本客户端都能进入服务器（类似轻量 ViaVersion）。
2. 进服通报：玩家进入服务器后，全服广播
   "[名称]该玩家是[游戏版本]，欢迎你加入服务器！"

版本演进：
- v1.0 初版：协议号改写 + 进服通报
- v1.1 性能优化：预编译 struct、预计算目标字节、改写决策 LRU 缓存、
  去除全量字节比较、热路径局部变量化
- v1.2 健壮性/可观测性优化：
  * 决策缓存改为并发安全（命中无锁 + miss 加锁二次确认的双检锁）
  * 通报模板预解析为片段序列，避免每次 str.format 解析格式串
  * 热路径去除浪费计算：label 仅 debug 时构造、abs 改区间比较、颜色前缀预取
  * 新增运行统计（缓存命中/未命中、改写/跳过次数），禁用/调试时输出，优化效果可观测
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

# 预编译：大端 int32 结构体，模块级单例，避免每次 unpack_from 重新解析格式
_S32BE = struct.Struct(">i")
_UNPACK_S32BE = _S32BE.unpack_from
_PACK_S32BE = _S32BE.pack

# 改写决策缓存上限：协议号种类有限，128 足够覆盖所有已知客户端版本
_REWRITE_CACHE_MAX = 128

# 缓存哨兵：区分"未缓存"(_MISS) 与"判定为不改写"(_SKIP)
_MISS = object()
_SKIP = object()

# 通报模板支持的占位符
_TEMPLATE_FIELDS = ("name", "version")


def _parse_template(template: str):
    """把通报模板预解析为片段序列 [(kind, value), ...]。

    kind=0 字面量，kind=1 字段（name/version）。
    解析一次后，每次通报只需按顺序拼接片段，避免 str.format 反复解析格式串。
    仅识别 {name} / {version}，其它内容按字面量保留（兼容第三方自定义文本）。
    """
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
        # 允许改写的协议号范围：客户端协议号与服务器相差不超过该值才改写，避免误伤
        self._max_protocol_diff = int(cfg.get("max_protocol_diff", 1400))

        # ---- v1.1 性能优化：预计算热路径所需数据 ----
        self._target_bytes = _PACK_S32BE(self._server_protocol)
        self._rewrite_cache = OrderedDict()
        self._cache_max = _REWRITE_CACHE_MAX
        # ---- v1.2 健壮性：决策缓存写操作互斥锁（命中路径无锁）----
        self._cache_lock = threading.Lock()

        # ---- v1.2 可观测性：运行统计（默认关闭，零默认开销）----
        # 统计项：[0]命中 [1]未命中 [2]改写 [3]跳过
        self._enable_stats = bool(cfg.get("enable_stats", False))
        self._stats = [0, 0, 0, 0]

        # ---- v1.2 通报优化：预解析模板 + 预取颜色前缀 ----
        self._template_segments = _parse_template(template)
        self._msg_prefix = ColorFormat.YELLOW
        self._msg_suffix = ColorFormat.RESET

        self.register_events(self)
        self.logger.info(
            "VersionGate v1.2 已启用 | 服务器协议号=%d | MC版本=%s | 版本绕行=%s | 进服通报=%s | 决策缓存=%d"
            % (self._server_protocol, self._minecraft_version,
               self._bypass_enabled, self._announce_enabled, self._cache_max)
        )

    def on_disable(self) -> None:
        # v1.2：若开启统计，输出本次运行的汇总，让优化效果可观测
        if self._enable_stats:
            hit, miss, rewrite, skip = self._stats
            total = hit + miss
            rate = (100.0 * hit / total) if total else 0.0
            self.logger.info(
                "VersionGate 运行统计 | 缓存命中=%d/%d (%.1f%%) | 改写=%d | 跳过=%d"
                % (hit, total, rate, rewrite, skip)
            )
        self.logger.info("VersionGate 已禁用")

    # ---------------- 版本准入：改写登录包协议号 ----------------
    @event_handler
    def on_packet_receive(self, event: PacketReceiveEvent) -> None:
        # 热路径：局部变量化，减少重复 self 属性访问
        if not self._bypass_enabled:
            return
        pid = event.packet_id
        if pid != PID_LOGIN and pid != PID_REQUEST_NETWORK_SETTINGS:
            return
        payload = event.payload
        # 协议号占 4 字节；不足则跳过
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
        """把 payload 开头的协议号改写为服务器协议号。返回新字节或 None。

        RequestNetworkSettings 与 Login 包的 ClientProtocol 均为大端 int32（4 字节）。

        v1.2：双检锁并发安全缓存——命中路径无锁读取；未命中才加锁，
        在锁内二次确认后解析并写入决策，避免多玩家同时进服时的 LRU 竞态。
        """
        head = payload[:4]
        cache = self._rewrite_cache
        stats = self._stats
        enable = self._enable_stats

        # 第一重检查（无锁）：绝大多数包在此命中，直接拼接返回
        cached = cache.get(head, _MISS)
        if cached is _MISS:
            # 未命中：加锁做第二重检查 + 解析 + 写入
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
        """（线程安全）解析协议号、判定是否改写，并写入决策缓存。"""
        cache = self._rewrite_cache
        with self._cache_lock:
            # 第二重检查：可能上一个线程已写入同样的决策
            cached = cache.get(head, _MISS)
            if cached is not _MISS:
                if enable:
                    stats[0] += 1  # 命中（并发下另一线程刚写入）
                return cached

            if enable:
                stats[1] += 1  # 未命中
            client_proto = _UNPACK_S32BE(head, 0)[0]

            # 判定：协议号合法、与目标有差异、且差值在允许范围内才改写。
            # v1.2：用区间比较替代 abs() 函数调用。
            diff = client_proto - self._server_protocol
            if (client_proto == self._server_protocol or client_proto <= 0
                    or client_proto > 100000
                    or diff > self._max_protocol_diff or diff < -self._max_protocol_diff):
                decision = _SKIP
            else:
                decision = self._target_bytes

            # LRU 缓存决策（写操作在锁内，保证原子）
            if len(cache) >= self._cache_max:
                cache.popitem(last=False)
            cache[head] = decision

            if self._debug:
                # v1.2：label 仅在 debug 时构造，热路径不再浪费
                label = "RequestNetworkSettings" if pid == PID_REQUEST_NETWORK_SETTINGS else "Login"
                self.logger.info("%s 客户端协议号=%d 服务器=%d (未缓存，已记入缓存)"
                                 % (label, client_proto, self._server_protocol))
            return decision

    # ---------------- 进服通报 ----------------
    @event_handler
    def on_player_join(self, event: PlayerJoinEvent) -> None:
        if not self._announce_enabled:
            return
        player = event.player
        # 正常路径直接取值，异常才回退（减少 try/except 覆盖的正常流程）
        try:
            name = player.name
            version = player.game_version or self._minecraft_version
        except Exception:
            name = "未知玩家"
            version = self._minecraft_version

        # v1.2：用预解析片段拼接通报消息，避免 str.format 每次解析格式串
        parts = []
        for kind, value in self._template_segments:
            parts.append(value if kind == 0 else (name if value == "name" else version))
        message = "".join(parts)

        self.server.broadcast_message(self._msg_prefix + message + self._msg_suffix)
        self.logger.info("进服通报: %s (版本 %s)" % (name, version))
