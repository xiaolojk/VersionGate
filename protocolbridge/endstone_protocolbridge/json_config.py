# -*- coding: utf-8 -*-
"""
ProtocolBridge JSON 配置读取模块
================================
配置文件从 TOML 改为 JSON（兼容类 JSON 的 # 行尾注释，风格参考泰拉瑞亚配置）。

提供：
  1. loads_jsonc(text)       —— 剥离 # / // 注释与尾随逗号后解析 JSON
  2. ensure_config_json()    —— 首次在 data_folder 生成 config.json
  3. load_config_json()      —— 读取 config.json，返回 Settings 字典

设计原则：解析失败一律回退空字典，绝不抛异常拖垮插件。
作者：橘子
"""
import json
import logging
from pathlib import Path

log = logging.getLogger("ProtocolBridge.JsonConfig")

CONFIG_NAME = "config.json"


# 默认配置模板：类 JSON 结构 + # 行尾注释；最后一段为「替换方块」
DEFAULT_CONFIG_JSON = '''{
  "Settings": {
    "enable_version_bypass": true,              # 版本绕行：让 1.21.x / 1.26.x 旧版客户端连进来
    "enable_join_announcement": true,           # 进服通报：玩家进服时全服广播真实版本
    "announcement_template": "[{name}]该玩家是[{version}]，欢迎你加入服务器！",  # 通报模板，占位符 {name} 玩家名 / {version} 版本
    "allowed_protocols": [685, 686, 712, 729, 748, 766, 776, 786, 800, 818, 819, 827, 844, 859, 860, 898, 924, 944, 975, 1001, 2168, 2169, 2193, 2225],  # 协议号白名单：只放行这些客户端
    "max_protocol_diff": 1540,                  # 协议号差值兜底，主判定以上方白名单为准
    "enable_translation": true,                 # 游戏包跨版本翻译：让双方看得懂
    "enable_deep_translation": true,            # 1.21.x 跨大版本逐包深度翻译
    "strict": false,                            # 严格模式：翻译失败时阻断包（谨慎开启）
    "enable_item_remap": true,                  # 方块防空气：把新版本方块物品换成老版认识的物品
    "fallback_item_id": 1,                      # 兜底物品 ID：没单独配置时用 1 = 石头
    "enable_connection_context": true,          # 连接级状态：追踪每个玩家的注册表上下文
    "debug": false,                             # 调试日志：打印协议判定与翻译细节
    "enable_stats": true,                       # 统计输出：控制台打印命中率
    "替换方块": {
      # 格式："1.21.x方块id": "1.26.x方块id"
      # 含义：服务器 1.26 的方块（右边 id）在 1.21 客户端显示成左边的方块
      # 示例（去掉行首 # 即生效）："1": "12345" 表示 1.26 方块 12345 显示为 1.21 石头(id=1)
      # "1": "12345"
    }
  }
}
'''


def _strip_comments(text):
    """剥离 # 与 // 行尾注释，保留字符串内的符号。"""
    out = []
    i = 0
    n = len(text)
    in_str = False
    esc = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "#":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _remove_trailing_commas(text):
    """去掉对象/数组结尾的尾随逗号，保留字符串内的逗号。"""
    res = []
    i = 0
    n = len(text)
    in_str = False
    esc = False
    while i < n:
        c = text[i]
        if in_str:
            res.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            res.append(c)
            i += 1
            continue
        if c == ",":
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] in "}]":
                i += 1
                continue
        res.append(c)
        i += 1
    return "".join(res)


def loads_jsonc(text):
    """解析带 # 注释的 JSON；失败抛出 ValueError 由调用方兜底。"""
    cleaned = _remove_trailing_commas(_strip_comments(text))
    return json.loads(cleaned)


def ensure_config_json(data_folder):
    """在 data_folder 下生成 config.json，已存在则不覆盖。返回是否新建。"""
    folder = Path(data_folder)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / CONFIG_NAME
        if not path.exists():
            path.write_text(DEFAULT_CONFIG_JSON, encoding="utf-8")
            return True
    except Exception as e:
        log.warning("生成 config.json 失败: %s", e)
    return False


def load_config_json(data_folder):
    """读取 config.json 的 Settings 字典；缺失/损坏返回空字典。"""
    path = Path(data_folder) / CONFIG_NAME
    if not path.exists():
        return {}
    try:
        data = loads_jsonc(path.read_text(encoding="utf-8"))
        settings = data.get("Settings", data) if isinstance(data, dict) else {}
        return settings if isinstance(settings, dict) else {}
    except Exception as e:
        log.warning("config.json 解析失败，使用内置默认: %s", e)
        return {}
