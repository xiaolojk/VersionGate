# VersionGate / ProtocolBridge

Endstone 基岩版跨版本插件。作者：橘子。MIT 开源。

> **请下载 v2.0.0 完整版**：一个插件搞定「改协议号 + 数据包翻译 + 进服通报」，无需 v1.x 配套。

## 版本

| 版本 | 能力 | 说明 |
|---|---|---|
| **v2.0.0**（最新） | 握手准入 + 包翻译 + 进服通报 | ✅ 推荐，单插件 |
| v1.3.0 | 仅改协议号 | 旧版，已被 v2.0 覆盖 |

装了 v2.0 就不要再装 v1.3（功能重复）。详见 [版本说明](README_版本说明.md)。

## 快速开始

1. 需要 **Python ≥ 3.12**
2. 下载 `endstone_protocolbridge-2.0.0-py3-none-any.whl` 放入 `plugins/`，重启
3. 打开 `debug = true`，用旧版客户端进服，看日志确认改写与翻译触发

## 文档

- [版本说明与下载推荐](README_版本说明.md)
- [v2.0 发行说明](RELEASE_NOTES_v2.0.md)
- [ProtocolBridge 技术文档](protocolbridge/README.md)

有问题欢迎提 Issue。
