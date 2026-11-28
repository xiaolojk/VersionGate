# -*- coding: utf-8 -*-
"""
JSON 配置测试：
  1) # 注释 / 尾随逗号剥离后能正确解析
  2) 字符串内的 # 与 // 不被误删
  3) 首次自动生成 config.json，已存在不覆盖
  4) 损坏文件回退空字典
  5) 插件加载「替换方块」按 1.21=1.26 转成内部 1.26→1.21 映射
  6) 旧格式 item_id_map 仍兼容
"""
import sys, types, struct
from pathlib import Path

# ---------- endstone mock ----------
def _install_endstone_mock():
    if "endstone" in sys.modules:
        return
    endstone = types.ModuleType("endstone")
    class ColorFormat:
        YELLOW = "§e"; RESET = "§r"
    endstone.ColorFormat = ColorFormat
    ev = types.ModuleType("endstone.event")
    def event_handler(fn): return fn
    class _Base:
        def __init__(self, **kw):
            for k, v in kw.items(): setattr(self, k, v)
    class PacketReceiveEvent(_Base): pass
    class PacketSendEvent(_Base): pass
    class PlayerJoinEvent(_Base): pass
    ev.event_handler = event_handler
    ev.PacketReceiveEvent = PacketReceiveEvent
    ev.PacketSendEvent = PacketSendEvent
    ev.PlayerJoinEvent = PlayerJoinEvent
    plug = types.ModuleType("endstone.plugin")
    class Plugin:
        api_version = "0.11"
        def __init__(self): pass
        def save_default_config(self): pass
        def register_events(self, obj): pass
    plug.Plugin = Plugin
    root = types.ModuleType("endstone")
    root.event = ev; root.plugin = plug; root.ColorFormat = ColorFormat
    sys.modules["endstone"] = root
    sys.modules["endstone.event"] = ev
    sys.modules["endstone.plugin"] = plug

_install_endstone_mock()

import json_config
from json_config import (DEFAULT_CONFIG_JSON, loads_jsonc, ensure_config_json,
                         load_config_json, CONFIG_NAME)
from bridge_plugin import ProtocolBridgePlugin, DEFAULT_ALLOWED_PROTOCOLS


class _Logger:
    def __init__(self): self.lines = []
    def info(self, m, *a): self.lines.append(("I", m % a if a else m))
    def warning(self, m, *a): self.lines.append(("W", m % a if a else m))
class _Server:
    protocol_version = 2225
    minecraft_version = "1.26.65.1"
    def broadcast_message(self, m): pass


def make_plugin(data_folder):
    p = ProtocolBridgePlugin()
    p.config = {}   # 故意留空，验证完全靠 config.json
    p.server = _Server()
    p.logger = _Logger()
    p.data_folder = Path(data_folder)
    p.on_enable()
    return p


# ============ 解析 ============

def test_default_template_parses():
    data = loads_jsonc(DEFAULT_CONFIG_JSON)
    s = data["Settings"]
    assert s["enable_version_bypass"] is True
    assert s["fallback_item_id"] == 1
    assert "替换方块" in s


def test_hash_comment_stripped():
    text = '''{"Settings": {"a": 1,  # 这是注释
                          "b": true # 行尾无逗号
    }}'''
    assert loads_jsonc(text) == {"Settings": {"a": 1, "b": True}}


def test_string_content_preserved():
    text = '''{"Settings": {"t": "a#b // c", "u": "http://x#y"}}'''
    d = loads_jsonc(text)
    assert d["Settings"]["t"] == "a#b // c"
    assert d["Settings"]["u"] == "http://x#y"


def test_trailing_comma():
    assert loads_jsonc('{"a": 1, "b": [1, 2,],}') == {"a": 1, "b": [1, 2]}


# ============ 生成与读取 ============

def test_ensure_and_load(tmp_path):
    assert ensure_config_json(tmp_path) is True
    cfg_path = tmp_path / CONFIG_NAME
    assert cfg_path.exists()
    # 已存在不覆盖
    cfg_path.write_text('{"Settings": {"fallback_item_id": 9}}', encoding="utf-8")
    assert ensure_config_json(tmp_path) is False
    assert load_config_json(tmp_path)["fallback_item_id"] == 9


def test_corrupt_fallback(tmp_path):
    (tmp_path / CONFIG_NAME).write_text("这不是JSON!!!{", encoding="utf-8")
    assert load_config_json(tmp_path) == {}


# ============ 插件方向转换 ============

def test_replace_block_direction(tmp_path):
    """配置 "1.21方块id"="1.26方块id" → 内部 {1.26: 1.21}"""
    (tmp_path / CONFIG_NAME).write_text('''{
      "Settings": {
        "fallback_item_id": 5,
        "替换方块": {
          "1": "12345",   # 1.26方块12345 → 1.21石头(1)
          "5": "20001"
        }
      }
    }''', encoding="utf-8")
    p = make_plugin(tmp_path)
    assert p._item_id_map == {12345: 1, 20001: 5}
    assert p._fallback_item_id == 5
    assert p._enable_item_remap is True


def test_old_item_id_map_compat(tmp_path):
    """旧格式 item_id_map（1.26=1.21）仍兼容"""
    (tmp_path / CONFIG_NAME).write_text('''{
      "Settings": {
        "item_id_map": {"333": "7"}
      }
    }''', encoding="utf-8")
    p = make_plugin(tmp_path)
    assert p._item_id_map == {333: 7}


def test_json_cfg_overrides_default(tmp_path):
    """config.json 里的开关生效（关闭进服通报）"""
    (tmp_path / CONFIG_NAME).write_text('''{
      "Settings": {"enable_join_announcement": false}
    }''', encoding="utf-8")
    p = make_plugin(tmp_path)
    assert p._announce_enabled is False
