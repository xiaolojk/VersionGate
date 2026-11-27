# ProtocolBridge v2.0 — 跨版本完整版插件（握手准入 + 包翻译，单插件无需配套）

## 这是什么

旧版 VersionGate 只改登录包的 4 字节协议号，能骗过版本校验让客户端"进服"，但游戏包结构对不上会导致功能异常。
**v2.0 是一个完整版插件：先把旧版的握手准入能力原样内置（改协议号 + 进服通报），再新增真正的数据包字段翻译——
把旧版客户端的游戏包按字段翻译给新版服务器，反向也翻译。一个 `.whl` 全搞定，无需 v1.x 配套，也不要同时装 v1.3。**

配置文件 `config.toml` 的三个开关默认全开：`enable_version_bypass`（握手准入）、`enable_join_announcement`（进服通报）、`enable_translation`（包翻译）。

核心基于 Endstone 官方维护的 [`bedrock-protocol`](https://github.com/EndstoneMC/bedrock-protocol) 库（v0.5.1），
它提供按协议版本的 `decode`/`encode`，覆盖 649~2259 全部协议（含服务器的 2225 和 2193 等 1.26.x 全部档位）。

## 关键实证数据（2193 → 2225）

用真实库对 231 个共有包做全量字段差异 + 跨版本解码探测：

| 指标 | 结果 |
|---|---|
| 共有包 | 231 |
| 字节级兼容（无需翻译） | **223（96.5%）** |
| 需要翻译 | **7~8 个** |
| 删除字段的包 | 0 |

需要翻译的包及变化：

| 包 ID | 包名 | 2225 的变化 |
|---|---|---|
| 11 | StartGamePacket | 内部枚举/序列化 |
| 12 | AddPlayerPacket | +`passenger_of_block_data` |
| 13 | AddActorPacket | +`passenger_of_block_data` |
| 44 | AnimatePacket | +`hand_slot` |
| 58 | LevelChunkPacket | +`is_client_biome_update` |
| 77 | CommandRequestPacket | 枚举值变化 |
| 93 | PlayerSkinPacket | 皮肤字段序列化 |
| 348 | ClientboundUpdateSoundDataPacket | 尾部字段 |

这推翻了"跨版本必须重写数百个包"的悲观判断：**相邻版本其实高度兼容，真正要翻译的是极少数包。**

## 翻译算法

```
旧版 payload --[旧协议 decode]--> 旧版对象
       --[逐字段复制到新版对象，新字段用默认值]-->
新版对象 --[新协议 encode]--> 新版 payload
```

实测性能：**单次翻译 13µs**（5000 次 66.5ms），完全可用于热路径。
且只对上述 8 个包翻译，其余 223 个包零开销放行。

## 测试结果

`test_protocol_bridge.py` 9 项全过，包括：
- 两版本库/包类可用性
- AddPlayer/Animate 翻译后字段保留
- 双向翻译（2193↔2225）
- 全量 200+ 包翻译往返不崩溃且可解码
- 兼容性报告生成

端到端演示：7/8 个需翻译包"旧版编码→翻译→新版服务器成功解析"全通过。

## 文件

- `protocol_bridge.py` — 翻译核心（纯 Python，可独立测试）
- `bridge_plugin.py` — Endstone 插件层（**握手协议号改写 + 进服通报 + 双向包翻译** + 自适应明文检测 + 客户端版本追踪，完整版无需 v1.x）
- `config.toml` — 完整配置（三个功能开关默认全开）
- `test_protocol_bridge.py` — 翻译核心测试
- `.venv/` — Python 3.12 + bedrock-protocol 0.5.1（沙箱）

## 部署前提与风险（重要）

1. **Python 版本冲突**：`bedrock-protocol` 需要 Python ≥3.12，而生产服务器 Endstone 跑在 Python 3.11。
   实测 3.11 无法加载其 `abi3.so`（缺 `PyErr_SetRaisedException` 符号）。
   → 部署需先把服务器 Endstone 环境升级到 Python 3.12（uv 管理的解释器可装）。
   → **注意**：即使 Python <3.12，握手协议号改写和进服通报仍正常工作，只有包翻译不启动，不会搞崩服务器。
2. **加密层未定论**：Endstone 的 `PacketReceiveEvent.payload` 是否在解密后暴露明文，文档未明确。
   → 插件内置**自适应自检**：运行时 decode 成功=明文→激活翻译；连续失败=密文→自动进入被动模式，
   只跳过包翻译、协议号改写仍生效，不影响服务器。因此即使环境不满足也不会搞崩服务器。
3. **未做真实客户端端到端**：沙箱无真实基岩客户端，未验证"旧版客户端真能正常玩"。
   → 需部署后用真实 1.26.51 客户端实测。

## 边界（诚实说明）

- 本翻译器针对 **1.26.x 相邻档位**（如 2193↔2225）设计，字段差异小、可靠。
- **跨大版本**（如 924→2225，差 1301）字段变化多得多，不在可靠范围——需要逐版本翻译链，工作量大。
- Bedrock 协议闭源逆向，Mojang 每次更新都可能改包结构，需持续跟进维护。
