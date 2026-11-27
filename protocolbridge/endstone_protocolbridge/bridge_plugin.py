# -*- coding: utf-8 -*-
"""
ProtocolBridge Plugin - Endstone 插件层（跨版本包翻译）
========================================================
在 VersionGate 改协议号让客户端"握手通过"之后，本插件负责把后续游戏包
在 旧版客户端 <-> 新版服务器 之间做字段级翻译，解决"能进服但包结构对不上"的问题。

设计原则（绝不搞崩服务器）：
1. 自适应明文检测：运行时自检 payload 是否可被官方库 decode。若失败（说明
   Endstone 暴露的是密文或不支持的协议），自动进入 PASSIVE 模式，只记录不改写。
2. 白名单翻译：只翻译实测需要翻译的 8 个包，其余 223 个包零开销放行。
3. 失败放行：任何 decode/encode 异常都吞掉并原样放行，绝不阻断玩家连接。
4. 只翻译"确认是旧版"的玩家：通过登录包记录每个玩家的真实协议号，
   只对其协议号 != 服务器协议号的玩家做翻译。

依赖：bedrock-protocol >= 0.5.1（需 Python 3.12）
"""

import threading
from endstone import ColorFormat
from endstone.event import event_handler, PacketReceiveEvent, PacketSendEvent, PlayerJoinEvent
from endstone.plugin import Plugin

try:
    from .protocol_bridge import ProtocolBridgeCore, SUPPORTED_PROTOCOLS
except ImportError:  # 兼容直接运行/测试
    from protocol_bridge import ProtocolBridgeCore, SUPPORTED_PROTOCOLS

PID_LOGIN = 0x01
PID_REQUEST_NETWORK_SETTINGS = 0xC1


class ProtocolBridgePlugin(Plugin):
    api_version = "0.11"

    def on_enable(self) -> None:
        try:
            self.save_default_config()
        except Exception as e:
            self.logger.warning("生成默认配置失败: %s" % e)
        cfg = self.config or {}

        self._server_protocol = int(self.server.protocol_version)
        self._enabled = bool(cfg.get("enable_translation", True))
        self._debug = bool(cfg.get("debug", False))
        self._strict = bool(cfg.get("strict", False))
        self._enable_stats = bool(cfg.get("enable_stats", True))

        # 只翻译指定包；默认用实测的 8 个
        only = cfg.get("only_packet_ids", None)
        only_ids = set(int(x) for x in only) if isinstance(only, list) and only else None
        if only_ids is None:
            only_ids = ProtocolBridgeCore.known_translate_ids()

        self._core = ProtocolBridgeCore(
            server_protocol=self._server_protocol,
            strict=self._strict,
            only_ids=only_ids,
        )

        # 每个地址/subclient 对应的客户端协议号（从登录包记录）
        self._client_proto: dict = {}
        self._proto_lock = threading.Lock()

        # 自适应模式：是否确认 payload 为明文。None=未检测，True=明文可翻译，False=被动
        self._plaintext_confirmed = None
        self._probe_count = 0

        self.register_events(self)
        self.logger.info(
            "ProtocolBridge v2.0 已启用 | 服务器协议=%d | 翻译=%s | 白名单包=%s | 被动模式=%s"
            % (self._server_protocol, self._enabled, sorted(only_ids), not self._enabled)
        )
        if self._server_protocol not in SUPPORTED_PROTOCOLS:
            self.logger.warning(
                "服务器协议号 %d 不在官方库支持列表，翻译可能不可用；插件将以被动模式运行"
                % self._server_protocol
            )

    def on_disable(self) -> None:
        if self._enable_stats:
            s = self._core.stats
            self.logger.info(
                "ProtocolBridge 统计 | 收包=%d 发包=%d | 已翻译 收=%d 发=%d | "
                "解码失败=%d 编码失败=%d 无包类=%d"
                % (s.received, s.sent, s.translated_rx, s.translated_tx,
                   s.decode_fail, s.encode_fail, s.no_class)
            )
            if s.per_packet:
                top = sorted(s.per_packet.items(), key=lambda x: -x[1])[:12]
                self.logger.info("ProtocolBridge 翻译包TOP | " + " | ".join("%s:%d" % kv for kv in top))
        if self._plaintext_confirmed is False:
            self.logger.warning(
                "ProtocolBridge 处于被动模式：Endstone 暴露的 payload 无法被官方库解析"
                "（可能是密文层）。翻译未生效，服务器不受影响。"
            )
        self.logger.info("ProtocolBridge 已禁用")

    def _addr_key(self, event) -> tuple:
        try:
            addr = event.address
            host = getattr(addr, "host", None)
            port = getattr(addr, "port", None)
            return (str(host), port, getattr(event, "sub_client_id", 0))
        except Exception:
            return ("?", 0, 0)

    def _record_client_proto(self, event) -> None:
        """从登录/网络设置包记录客户端真实协议号（握手阶段明文，VersionGate 也在读）。"""
        pid = event.packet_id
        if pid not in (PID_LOGIN, PID_REQUEST_NETWORK_SETTINGS):
            return
        payload = event.payload
        if not payload or len(payload) < 4:
            return
        import struct
        try:
            proto = struct.unpack_from(">i", payload, 0)[0]
        except Exception:
            return
        if 0 < proto < 100000:
            with self._proto_lock:
                self._client_proto[self._addr_key(event)] = proto

    def _client_proto_for(self, event) -> int:
        with self._proto_lock:
            return self._client_proto.get(self._addr_key(event), self._server_protocol)

    def _try_plaintext_probe(self, pid: int, payload: bytes) -> None:
        """
        运行时自检：尝试用官方库 decode 一个收到的包。
        连续多次成功 => 确认明文；连续失败 => 判定密文/不支持，进入被动模式。
        """
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
            # 成功一次即可基本确认明文（继续观察几次更稳）
            if self._probe_count >= 3:
                self._plaintext_confirmed = True
                self.logger.info("ProtocolBridge 自检：payload 为明文，翻译已激活")
        except Exception:
            if self._probe_count >= 8:
                self._plaintext_confirmed = False
                self.logger.warning(
                    "ProtocolBridge 自检：payload 无法解析（密文或协议不支持），"
                    "自动进入被动模式，不对服务器造成影响"
                )

    @event_handler
    def on_packet_receive(self, event: PacketReceiveEvent) -> None:
        if not self._enabled:
            return
        self._record_client_proto(event)
        pid = event.packet_id
        payload = event.payload
        if not payload:
            return
        # 自检（仅对常见包，避开登录包）
        if pid not in (PID_LOGIN, PID_REQUEST_NETWORK_SETTINGS) and self._plaintext_confirmed is None:
            self._try_plaintext_probe(pid, payload)
        if self._plaintext_confirmed is False:
            return
        client_proto = self._client_proto_for(event)
        if client_proto == self._server_protocol:
            return
        try:
            out = self._core.translate_receive(pid, payload, client_proto)
            if out is not None:
                event.payload = out
                if self._debug:
                    self.logger.info("翻译收包 pid=%d %d->%d" % (pid, client_proto, self._server_protocol))
        except Exception as e:
            # 绝不因翻译异常阻断连接
            if self._debug:
                self.logger.warning("翻译收包跳过 pid=%d: %s" % (pid, e))

    @event_handler
    def on_packet_send(self, event: PacketSendEvent) -> None:
        if not self._enabled or self._plaintext_confirmed is not True:
            return
        client_proto = self._client_proto_for(event)
        if client_proto == self._server_protocol:
            return
        pid = event.packet_id
        payload = event.payload
        if not payload:
            return
        try:
            out = self._core.translate_send(pid, payload, client_proto)
            if out is not None:
                event.payload = out
                if self._debug:
                    self.logger.info("翻译发包 pid=%d %d->%d" % (pid, self._server_protocol, client_proto))
        except Exception as e:
            if self._debug:
                self.logger.warning("翻译发包跳过 pid=%d: %s" % (pid, e))
