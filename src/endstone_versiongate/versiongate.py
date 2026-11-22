# -*- coding: utf-8 -*-
"""
VersionGate - Endstone 插件
功能：
1. 版本准入：拦截客户端登录握手包，将客户端上报的协议号改写为服务器协议号，
   让 Minecraft Bedrock 1.26.x 各小版本客户端都能进入服务器（类似轻量 ViaVersion）。
2. 进服通报：玩家进入服务器后，全服广播
   "[名称]该玩家是[游戏版本]，欢迎你加入服务器！"
"""
import struct

from endstone import ColorFormat
from endstone.event import event_handler, PlayerJoinEvent, PacketReceiveEvent
from endstone.plugin import Plugin

# 基岩版数据包 ID（不含包头）
PID_LOGIN = 0x01                     # Login 包
PID_REQUEST_NETWORK_SETTINGS = 0xC1  # RequestNetworkSettings 包（协议协商）


# ---------------- VarInt 编解码 ----------------
def read_varint(data: bytes, offset: int = 0):
    """读取一个 Minecraft VarInt，返回 (值, 读取后的偏移)。最多 5 字节。"""
    value = 0
    shift = 0
    i = offset
    while i < len(data) and shift < 35:
        b = data[i]
        value |= (b & 0x7F) << shift
        i += 1
        if not (b & 0x80):
            return value, i
        shift += 7
    raise ValueError("invalid VarInt")


def encode_varint(value: int) -> bytes:
    """把整数编码为 Minecraft VarInt 字节。"""
    out = bytearray()
    value &= 0xFFFFFFFF
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)


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

        self.register_events(self)
        self.logger.info(
            "VersionGate 已启用 | 服务器协议号=%d | MC版本=%s | 版本绕行=%s | 进服通报=%s"
            % (self._server_protocol, self._minecraft_version,
               self._bypass_enabled, self._announce_enabled)
        )

    def on_disable(self) -> None:
        self.logger.info("VersionGate 已禁用")

    # ---------------- 版本准入：改写登录包协议号 ----------------
    @event_handler
    def on_packet_receive(self, event: PacketReceiveEvent) -> None:
        if not self._bypass_enabled:
            return
        pid = event.packet_id
        if pid not in (PID_LOGIN, PID_REQUEST_NETWORK_SETTINGS):
            return
        payload = event.payload
        if not payload:
            return
        try:
            new_payload = self._rewrite(payload, pid)
            if new_payload is not None and new_payload != payload:
                event.payload = new_payload
                if self._debug:
                    self.logger.info("已改写协议号: 包ID=%d 长度 %d -> %d"
                                     % (pid, len(payload), len(new_payload)))
        except Exception as e:
            # 绝不因改写失败而阻断玩家连接：原样放行
            if self._debug:
                self.logger.warning("协议号改写跳过(包ID=%d): %s" % (pid, e))

    def _rewrite(self, payload: bytes, pid: int):
        """把 payload 开头的协议号改写为服务器协议号。失败返回 None。

        依据 gophertunnel 协议定义，RequestNetworkSettings 与 Login 包的
        ClientProtocol 均为 big-endian int32（4 字节）。
        """
        target = self._server_protocol
        if len(payload) < 4:
            return None

        # 读取大端 int32 协议号
        client_proto = struct.unpack_from(">i", payload, 0)[0]
        label = "RequestNetworkSettings" if pid == PID_REQUEST_NETWORK_SETTINGS else "Login"
        if self._debug:
            self.logger.info("%s 客户端协议号=%d 服务器=%d" % (label, client_proto, target))

        if client_proto == target or client_proto <= 0 or client_proto > 100000:
            return None
        if abs(client_proto - target) > self._max_protocol_diff:
            if self._debug:
                self.logger.warning("协议号差异过大(%d)，跳过改写" % abs(client_proto - target))
            return None

        # 用大端 int32 写回服务器协议号，保留后续 payload
        return struct.pack(">i", target) + payload[4:]

    # ---------------- 进服通报 ----------------
    @event_handler
    def on_player_join(self, event: PlayerJoinEvent) -> None:
        if not self._announce_enabled:
            return
        player = event.player
        try:
            name = player.name
        except Exception:
            name = "未知玩家"
        try:
            version = player.game_version or self._minecraft_version
        except Exception:
            version = self._minecraft_version

        try:
            message = self._template.format(name=name, version=version)
        except Exception:
            message = "[%s]该玩家是[%s]，欢迎你加入服务器！" % (name, version)

        self.server.broadcast_message(
            ColorFormat.YELLOW + message + ColorFormat.RESET
        )
        self.logger.info("进服通报: %s (版本 %s)" % (name, version))
