# VersionGate / ProtocolBridge v3.1.0 更新说明

本次解决两个问题：方块物品在 1.21 客户端变成空气；玩家可在配置界面自己添加物品 ID 映射。

## 一、方块物品不再变空气

原因（实测 bedrock-protocol 库确认）：

- 1.26 服务端的方块物品在物品描述符里是 `id=0 + block_runtime_id=N`。
- 1.21 老客户端的编码器把 `id=0` 当"空物品"，直接把整个物品清成 6 字节空包，
  `block_runtime_id` 和数量全部丢弃，所以玩家看到空气。

修复：深度翻译发包方向（1.26→1.21）对物品描述符做 ID 修正，保证写回的 `id` 大于 0：

- 方块物品（`id=0 + block_runtime_id`）：优先用玩家映射里的 `block_runtime_id → 物品id`；
  没配映射就用兜底 ID（默认 1，minecraft:stone），不再变空气。
- 普通新物品：命中映射时替换为指定的 1.21 物品 ID。
- 只改发包方向，收包方向不动，不影响服务端识别。
- 可用 `enable_item_remap` 开关，`fallback_item_id` 调兜底物品。

## 二、配置界面加"物品 ID 映射"页

面板 → 插件管理 → ProtocolBridge 配置 → 新增「物品 ID 映射」标签页：

- 表格增删，左边填 1.26 新物品 ID（方块物品填 block_runtime_id），右边填 1.21 已有物品 ID。
- 保存时校验两列必须为正整数；写入配置文件的 `[item_id_map]` 段，重启服务器生效。
- 「可视化配置」页可开关 `enable_item_remap`、调整 `fallback_item_id`。

对应配置项（config.toml，也可手动编辑）：

```
enable_item_remap = true
fallback_item_id = 1

[item_id_map]
20001 = 5      # 例：方块 bri 20001 → 1.21 木板(id=5)
12345 = 1      # 例：新物品 12345 → 1.21 石头(id=1)
```

## 三、其他

- 修复 v3.0.0 遗留的打包导入问题：`deep_translator.py` 原用绝对导入，装成 wheel 后会
  找不到 `protocol_bridge`，已改为相对导入优先，打包后实测可正常加载。
- 新增回归测试 `test_item_remap.py`（6 项）。全量测试 95 项通过。
- 产物：`dist/endstone_protocolbridge-3.1.0-py3-none-any.whl`（已打包 config.toml）。
- 生产服务器当前仍是 v2.0.1，本次改动仅在沙箱验证，未部署。需要部署时再单独操作。
