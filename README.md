# VersionGate / ProtocolBridge

Endstone 基岩版 1.21.x ↔ 1.26.x 跨版本插件。作者：橘子。MIT 开源。

## 版本

| 版本 | 能力 | 说明 |
|---|---|---|
| **v3.1.0**（新功能） | 1.21.x/1.26.x 深度翻译 + 1.21.x 支持 + 物品 ID 映射（防空气） | 新增跨大版本支持与方块物品防空气，95 项测试通过 |
| **v2.0.1**（稳定） | 握手准入 + 全 1.26.x 分档翻译 + 进服通报 | 生产环境验证稳定，推荐保守用户继续使用 |
| v2.0.0 | 握手准入 + 包翻译（仅 2193 档位精确） | 已被 v2.0.1 覆盖 |
| v1.3.0 | 仅改协议号 | 旧版，别再装 |

v3.1.0 在 v3.0 深度翻译基础上新增：1.26 方块物品在 1.21 客户端不再变空气（把 `id=0` 修正为非 0），并支持在面板「插件配置 → 物品 ID 映射」里让玩家自己配置 `1.26 新物品 ID = 1.21 已有物品 ID`。详见 [v3.1.0 发行说明](RELEASE_NOTES_v3.1.md)。

装了 v2.0.x / v3.x 就不要再装 v1.3（功能重复）。详见 [版本说明](README_版本说明.md)。

## 快速开始

1. 需要 **Python ≥ 3.12**
2. 下载 whl 放入 `plugins/`，重启：
   - 保守选 `protocolbridge/dist/endstone_protocolbridge-2.0.1-py3-none-any.whl`
   - 要 1.21.x 支持 + 防空气选 `protocolbridge/dist/endstone_protocolbridge-3.1.0-py3-none-any.whl`
3. 日志会打印「分档翻译表」；`debug = true` 可看每次改写与翻译

## 验证

v3.1.0：95 项自动化测试全过（含防空气 / 物品映射专项测试、握手改写、双向翻译、连接级状态、多客户端模拟）。

## 文档

- [版本说明与下载推荐](README_版本说明.md)
- [v3.1.0 发行说明](RELEASE_NOTES_v3.1.md)
- [v2.0.1 发行说明](RELEASE_NOTES_v2.0.md)
- [技术文档](protocolbridge/README.md)

有问题欢迎提 Issue。
