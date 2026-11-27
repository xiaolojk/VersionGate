# ProtocolBridge 生产部署记录（2026-11-27）

> **说明**：这是当时的部署过程记录。当时的 v2.0 还需要 v1.3（VersionGate）配合做协议号改写。
> **现已更新**：v2.0 已是完整版，内置了协议号改写与进服通报，单插件即可运行，**不再需要也不应同时安装 v1.3**。下方记录保留供参考。

## 已完成

服务器从 Python 3.11 升级到 **Python 3.12.16**，并部署跨版本包翻译插件。

| 项目 | 状态 |
|---|---|
| Python | 3.12.16（uv 管理） |
| 新环境 | `/opt/endstone/.venv312`（独立，未动旧环境） |
| 游戏服务器 | 已切到 venv312 启动，运行稳定 |
| 4 个插件 | protocolbridge v2.0 / commandguard / webshop / versiongate 全部加载 |
| 崩溃 | 0 |
| 面板 / 商店端口 | 3566 / 8899 正常 |

## 变更明细

1. **安装 Python 3.12**：`uv python install 3.12`
2. **新建 venv312**：`/opt/endstone/.venv312`，装 endstone 0.11.16（cp312）+ bedrock-protocol 0.5.1 + 面板依赖
   - 服务器到 PyPI 下载慢，改为沙箱下载 15 个 wheel 后离线安装
3. **复制插件数据**：3 个现有插件 + 翻译插件到 `.local/lib/python3.12/site-packages`
4. **面板切换**：`ENDSTONE_BIN` 从 `.venv/bin/endstone` 改为 `.venv312/bin/endstone`（`/opt/panel/app.py` 第 30 行）
5. **翻译插件**：whl 部署到 plugins，配置在 `plugins/protocolbridge/config.toml`

## 回滚方案（万一出问题）

```bash
# 1. 面板切回旧解释器
sed -i 's|\.venv312.*/bin/endstone|.venv/bin/endstone|' /opt/panel/app.py
# 或用备份
cp /opt/panel/app.py.bak_bridge /opt/panel/app.py

# 2. 移除翻译插件
rm -f /opt/endstone/bedrock_server/plugins/endstone_protocolbridge-2.0.0-py3-none-any.whl
rm -rf /opt/endstone/bedrock_server/plugins/protocolbridge

# 3. 重启面板（会自动用旧 venv 拉起服务器）
systemctl restart ops-panel
```

旧环境完整保留在 `/opt/endstone/.venv.bak311`（252M），面板备份 `/opt/panel/app.py.bak_bridge`。

## 翻译插件的保护机制

- **自适应明文检测**：运行时尝试用官方库 decode 收到的包。成功=明文→激活翻译；连续失败=密文→自动转被动模式，不改写任何包。
- **只翻译旧版玩家**：从登录包记录每个玩家真实协议号，只对协议号≠2225 的玩家翻译；2225 玩家零开销。
- **白名单翻译**：只翻译实测需翻译的 8 个包（11/12/13/44/58/77/93/348），其余放行。
- **失败放行**：任何异常都吞掉，绝不阻断连接。

## 未验证项（诚实说明）

1. **端到端"旧版客户端真能玩"**：沙箱无真实基岩客户端，生产当前也全是 2225 玩家，未触发翻译。
   需用一个真实 1.26.51/2193 客户端进服验证。
2. **payload 是否明文层**：Endstone 文档未明确。已用自适应机制覆盖两种可能，但这点需真实旧版客户端上线后看日志确认（会打印"自检：payload 为明文"或"被动模式"）。

## 相关文件

- `protocol_bridge.py` — 翻译核心（9 项测试全过）
- `bridge_plugin.py` — Endstone 插件层
- `test_protocol_bridge.py` — 测试
- `README.md` — 技术说明与实证数据
