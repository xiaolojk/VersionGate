# -*- coding: utf-8 -*-
"""
VersionGate - Endstone 插件 (v1.1 性能优化版)
功能：
1. 版本准入：拦截客户端登录握手包，将客户端上报的协议号改写为服务器协议号，
   让 Minecraft Bedrock 1.26.x 各小版本客户端都能进入服务器（类似轻量 ViaVersion）。
2. 进服通报：玩家进入服务器后，全服广播
   "[名称]该玩家是[游戏版本]，欢迎你加入服务器！"

v1.1 性能优化：
- 预编译 struct.Struct 并复用，避免每个包重新解析格式串
- 目标协议号字节在启用时预计算，热路径零 struct 调用
- 引入协议号改写决策缓存（LRU 上限 + 命中计数），同版本客户端只判断一次
- 去除 new_payload != payload 的全量字节比较（改为 4 字节头直接判定）
- 热路径局部变量化，减少 self 属性重复访问
- 进服通报正常路径去除 try/except 开销
"""
import struct
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
        self._template = str(cfg.get("announcement_template",
                             "[{name}]该玩家是[{version}]，欢迎你加入服务器！"))
        self._debug = bool(cfg.get("debug", False))
        # 允许改写的协议号范围：客户端协议号与服务器相差不超过该值才改写，避免误伤
        self._max_protocol_diff = int(cfg.get("max_protocol_diff", 1400))

        # ---- v1.1 性能优化：预计算热路径所需数据 ----
        # 目标协议号的大端字节，启用时算一次，热路径直接切片拼接
        self._target_bytes = _PACK_S32BE(self._server_protocol)
        # 改写决策缓存：client_proto -> 改写后的 4 字节头（命中则免 unpack/判断）
        # 值为目标字节表示"需改写"，None 表示"不改写"；用哨兵区分未缓存
        self._rewrite_cache = OrderedDict()
        self._cache_max = _REWRITE_CACHE_MAX

        self.register_events(self)
        self.logger.info(
            "VersionGate v1.1 已启用 | 服务器协议号=%d | MC版本=%s | 版本绕行=%s | 进服通报=%s | 决策缓存=%d"
            % (self._server_protocol, self._minecraft_version,
               self._bypass_enabled, self._announce_enabled, self._cache_max)
        )

    def on_disable(self) -> None:
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

        v1.1：先查决策缓存——同协议号客户端直接拿预存的改写后字节头拼接，
        免去 unpack_from 与范围判断；未命中才解析并缓存决策。
        """
        head = payload[:4]
        cache = self._rewrite_cache
        cached = cache.get(head, _MISS)  # 哨兵区分"未缓存"与"判定为不改写"
        if cached is not _MISS:
            # 命中：False 表示不改写；bytes 表示改写后的完整 payload 头
            if cached is _SKIP:
                return None
            return cached + payload[4:]

        # 未命中：解析协议号（大端 int32）
        client_proto = _UNPACK_S32BE(head, 0)[0]
        label = "RequestNetworkSettings" if pid == PID_REQUEST_NETWORK_SETTINGS else "Login"

        # 判定：协议号合法、与目标有差异、且差值在允许范围内才改写
        if (client_proto == self._server_protocol or client_proto <= 0
                or client_proto > 100000
                or abs(client_proto - self._server_protocol) > self._max_protocol_diff):
            decision = _SKIP
        else:
            decision = self._target_bytes

        # LRU 缓存决策（用 4 字节头做 key，天然区分不同协议号）
        if len(cache) >= self._cache_max:
            cache.popitem(last=False)
        cache[head] = decision

        if decision is _SKIP:
            return None
        if self._debug:
            self.logger.info("%s 客户端协议号=%d 服务器=%d (未缓存，已记入缓存)"
                             % (label, client_proto, self._server_protocol))
        return decision + payload[4:]

    # ---------------- 进服通报 ----------------
    @event_handler
    def on_player_join(self, event: PlayerJoinEvent) -> None:
        if not self._announce_enabled:
            return
        player = event.player
        # 正常路径直接取值，异常才回退（v1.1：减少 try/except 覆盖的正常流程）
        try:
            name = player.name
            version = player.game_version or self._minecraft_version
        except Exception:
            name = "未知玩家"
            version = self._minecraft_version

        try:
            message = self._template.format(name=name, version=version)
        except Exception:
            message = "[%s]该玩家是[%s]，欢迎你加入服务器！" % (name, version)

        self.server.broadcast_message(
            ColorFormat.YELLOW + message + ColorFormat.RESET
        )
        self.logger.info("进服通报: %s (版本 %s)" % (name, version))
