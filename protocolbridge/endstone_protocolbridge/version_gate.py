# -*- coding: utf-8 -*-
"""
VersionGate 配置与物品替换模块（ProtocolBridge v2.1 新增）
==========================================================
职责：
  1. 插件加载时在 data_folder/VersionGate/ 生成配置文件（含物品映射示例）；
  2. 每次服务器启动/重启（on_enable）都从该文件夹读取最新配置；
  3. 服务器→客户端发包时，按配置把 1.26 新物品名替换为 1.21 存在的物品名。

设计原则（绝不搞崩服务器）：
  - 配置文件缺失/损坏时回退到内置默认映射，不抛异常；
  - 物品替换只作用于物品注册表包（ItemRegistryPacket 162 / 早期 StartGame.items），
    且仅在字节兼容可 decode 时执行；任何失败都原样放行；
  - 替换是"字符串名→字符串名"，不改动 runtime id（数字），客户端按新名解析显示。

依赖：Python 3.12 内置 tomllib
作者：橘子
"""
import os
import shutil
import logging
import tomllib
from pathlib import Path
from typing import Dict, Optional, Tuple

log = logging.getLogger("ProtocolBridge.VersionGate")

# 配置子文件夹名（位于插件 data_folder 下）
FOLDER_NAME = "VersionGate"
CONFIG_NAME = "config.toml"

# 内置兜底映射：1.26 新增物品名 → 1.21 存在的替代物品名。
# 仅在配置文件不存在/损坏时启用。默认给出几类常见替代，避免玩家看到 unknown。
BUILTIN_ITEM_MAP: Dict[str, str] = {
    # 示例：1.26 的新物品替换为 1.21 已有的等价/近似物品
    # 真实项目以 VersionGate/config.toml 为准，用户可自由增删。
}

# 默认配置文件模板（中文注释，开箱即改）
DEFAULT_CONFIG_TEMPLATE = '''# ============================================================
# VersionGate 物品兼容配置（ProtocolBridge v2.1）
# ------------------------------------------------------------
# 作用：服务器是 1.26.65，1.21.x 老客户端不认识 1.26 新增的物品。
#       在这里把"1.26 的物品名"映射到"1.21 存在的物品名"，
#       老客户端进服时就会把新物品显示成你指定的替代物品，
#       不会因无法识别而出错。
#
# 生效时机：服务器每次【启动/重启】都会重新读取本文件。
#           改完配置后重启服务器即可生效。
#
# 写法：键 = 新版物品ID   值 = 老版本存在的物品ID（都要加引号）
#   左边写 1.26 里的物品命名空间ID（可用 /give 查看，如 minecraft:xxx）
#   右边写 1.21 里确实存在的物品ID（写错会导致该物品显示异常）
#
# 只想注释掉某条，前面加 # 即可。
# ============================================================

[item_map]
# ---- 写法示例（都保持注释，按需取消注释即可生效）----
# 左边 = 你服务器上 1.26 新物品/Add-on 物品的真实 ID
# 右边 = 1.21 客户端确实存在的原版物品 ID
# "minecraft:某新物品" = "minecraft:stone"

# ---- 异界(OtherWorldly)模组物品 → 1.21 原版替代 ----
# "dungeon:moonstone" = "minecraft:emerald"
# "dungeon:heart_amulet" = "minecraft:heart_of_the_sea"
# "dungeon:amulets_bag" = "minecraft:chest"
# "dungeon:crystal_arrow" = "minecraft:arrow"

# ---- 机械动力(Create)物品 → 1.21 原版替代 ----
# "create:brass_ingot" = "minecraft:iron_ingot"
# "create:copper_ingot" = "minecraft:copper_ingot"
# "create:brass_casing" = "minecraft:light_weighted_pressure_plate"

# ---- 通用开关 ----
[settings]
# 是否启用物品名替换（false 则只翻译包、不替换物品）
enable_item_replacement = true

# 是否在服务器控制台打印每次物品替换的日志（调试用）
log_replacements = false
'''


class VersionGateConfig:
    """
    VersionGate 配置管理：生成/读取 data_folder/VersionGate/config.toml。

    用法：
        vg = VersionGateConfig(data_folder)
        vg.ensure_config()            # 首次生成默认配置
        item_map, settings = vg.load()  # 每次启动读取
    """

    def __init__(self, data_folder):
        self.data_folder = Path(data_folder)
        self.folder = self.data_folder / FOLDER_NAME
        self.config_path = self.folder / CONFIG_NAME

    def ensure_config(self) -> bool:
        """
        确保 VersionGate 文件夹和配置文件存在。已存在则不覆盖用户修改。
        返回 True 表示本次新建了配置。
        """
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            if not self.config_path.exists():
                self.config_path.write_text(DEFAULT_CONFIG_TEMPLATE, encoding="utf-8")
                return True
        except Exception as e:
            log.warning("生成 VersionGate 配置失败: %s", e)
        return False

    def reset_config(self) -> bool:
        """把配置重置为默认模板（供管理命令调用）。"""
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.config_path, self.config_path.with_suffix(".toml.bak")) \
                if self.config_path.exists() else None
            self.config_path.write_text(DEFAULT_CONFIG_TEMPLATE, encoding="utf-8")
            return True
        except Exception as e:
            log.warning("重置 VersionGate 配置失败: %s", e)
            return False

    def load(self) -> Tuple[Dict[str, str], dict]:
        """
        读取配置，返回 (item_map, settings)。
        配置文件缺失或解析失败时回退到内置默认，不抛异常。
        """
        item_map: Dict[str, str] = dict(BUILTIN_ITEM_MAP)
        settings: dict = {"enable_item_replacement": True, "log_replacements": False}
        if not self.config_path.exists():
            return item_map, settings
        try:
            with self.config_path.open("rb") as f:
                data = tomllib.load(f)
        except Exception as e:
            log.warning("VersionGate 配置解析失败，使用内置默认: %s", e)
            return item_map, settings

        raw_map = data.get("item_map", {}) or {}
        if isinstance(raw_map, dict):
            for k, v in raw_map.items():
                if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip():
                    item_map[k.strip()] = v.strip()
        raw_settings = data.get("settings", {}) or {}
        if isinstance(raw_settings, dict):
            for k, v in raw_settings.items():
                settings[k] = v
        return item_map, settings


def rebuild_items_with_map(items, item_map: Dict[str, str], logger=None):
    """
    按 item_map 重建物品表列表。

    【重要】bedrock-protocol 是 nanobind 扩展类型，obj.items[i] 返回的是临时副本，
    直接改它的 .name 不会影响包内部存储。必须整体重建列表再赋值才能生效。

    策略：对每个条目，读它的 name/id 等字段，若 name 命中映射则新建一个同类型
    对象并设置好所有字段（name 用替换值），否则复用原对象。返回 (新列表, 替换次数)。
    """
    if not item_map or not isinstance(items, (list, tuple)):
        return items, 0
    new_items = []
    changed = 0
    for entry in items:
        name = getattr(entry, "name", None)
        if isinstance(name, str) and name in item_map:
            new_name = item_map[name]
            try:
                clone = _clone_item_entry(entry)
                clone.name = new_name
                new_items.append(clone)
                changed += 1
                if logger and getattr(logger, "_vg_log", False):
                    logger.info("VersionGate 物品替换: %s -> %s", name, new_name)
                continue
            except Exception:
                pass
        new_items.append(entry)
    return new_items, changed


def _clone_item_entry(entry):
    """克隆一个物品表条目（nanobind 对象不能直接改内部副本，需新建同类型对象）。"""
    cls = type(entry)
    clone = cls()
    # 复制所有可写字段（跳过私有、方法、ID）
    for attr in dir(entry):
        if attr.startswith("_") or attr == "ID":
            continue
        try:
            val = getattr(entry, attr)
        except Exception:
            continue
        if callable(val):
            continue
        try:
            setattr(clone, attr, val)
        except Exception:
            pass
    return clone
