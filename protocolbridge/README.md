# ProtocolBridge v2.0 — 跨版本完整版插件

Endstone 基岩版插件：协议号改写 + 进服通报 + 数据包翻译，一个插件搞定，无需 v1.x。

## 原理

1. **握手**：拦截登录包，按 1.26.x 白名单改写协议号，让旧版客户端进服（同时记录真实协议号）
2. **翻译**：用官方 [`bedrock-protocol`](https://github.com/EndstoneMC/bedrock-protocol) 按协议版本 decode/encode，把游戏包逐字段翻译成对方能解析的格式，双向

实测 2193→2225：231 个共有包中 223 个字节级兼容，仅 7~8 个需翻译，单次约 13µs。

## 文件

- `protocol_bridge.py` — 翻译核心
- `bridge_plugin.py` — 插件层（握手改写 + 进服通报 + 双向翻译）
- `config.toml` — 配置（三个开关默认全开）
- `test_protocol_bridge.py` — 测试（9 项全过）
- `DEPLOYMENT.md` — 生产部署记录

## 配置开关

| 开关 | 默认 | 作用 |
|---|---|---|
| `enable_version_bypass` | true | 改写协议号让旧版进服 |
| `enable_join_announcement` | true | 进服全服通报版本 |
| `enable_translation` | true | 游戏包跨版本翻译 |

## 部署

需 **Python ≥ 3.12**（bedrock-protocol 要求）。whl 放入 `plugins/`，重启。密文环境自动进被动模式——协议号改写仍生效，只跳过翻译，不会搞崩服务器。见 [DEPLOYMENT.md](DEPLOYMENT.md)。

## 限制

仅 1.26.x 相邻档位；跨大版本不支持；未经真实客户端端到端验证。
