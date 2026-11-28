# ProtocolBridge v3.1.0 — 1.21.x ↔ 1.26.x 跨版本插件

Endstone 基岩版插件：协议号改写 + 进服通报 + 深度包翻译 + 物品 ID 映射（防空气），一个插件搞定，无需 v1.x。

## 功能

1. **握手准入**：拦截登录包，按 1.21.x + 1.26.x 白名单改写协议号，让老客户端进服（同时记录真实协议号），进服全服通报版本。
2. **深度包翻译**：v3.0 起，1.21.x 客户端走逐包深度翻译（`DeepTranslator`）：嵌套对象递归重建、字段别名、独有包丢弃，解决跨大版本的世界/移动/方块/物品显示问题；1.26.x 相邻档位仍走轻量翻译。
3. **连接级状态**：借鉴 ViaVersion，捕获 StartGame 下发的 registry 上下文，按连接保存。
4. **物品 ID 映射（v3.1 新增，防空气）**：1.26 方块物品用 `id=0 + block_runtime_id`，1.21 老编码器会把 `id=0` 当空气清空。v3.1 在发包方向把 `id` 修正为非 0：优先用玩家配置的 `block_runtime_id → 物品id` 映射，未配置用兜底 ID（默认 1=石头），方块物品不再变空气。

## 主要文件

- `protocol_bridge.py` — 轻量翻译核心
- `deep_translator.py` — 跨大版本深度翻译器（含物品 ID 映射）
- `connection_state.py` — 连接级状态
- `version_gate.py` — 物品名替换配置
- `bridge_plugin.py` — 插件层（事件入口 + 配置）
- `config.toml` — 配置
- `test_*.py` — 回归测试（全量 95 项通过）

## 配置（物品映射）

编辑 `config.toml`：

```
enable_item_remap = true
fallback_item_id = 1

[item_id_map]
20001 = 5      # 1.26 方块 bri 20001 → 1.21 木板(id=5)
12345 = 1      # 1.26 新物品 12345 → 1.21 石头(id=1)
```

左边填 1.26 新物品 ID（方块填 `block_runtime_id`），右边填 1.21 确实存在的物品 ID。改完重启生效。详见仓库根 [RELEASE_NOTES_v3.1.md](../RELEASE_NOTES_v3.1.md)。

## 部署

需 **Python ≥ 3.12**（bedrock-protocol 要求）。`dist/endstone_protocolbridge-3.1.0-py3-none-any.whl` 放入 `plugins/`，重启。密文环境自动进被动模式——协议号改写仍生效，只跳过翻译，不会搞崩服务器。见 [DEPLOYMENT.md](DEPLOYMENT.md)。

## 限制

物品 ID 映射右边必须是 1.21 真实存在的 ID，填错会显示成别的物品；`block_runtime_id` 换世界/加模组后可能变化。1.26 新物品在 1.21 客户端是"显示为映射的替代物品"，不是原样显示。

作者：橘子 · MIT
