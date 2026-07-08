"""test_schema.py — _conf_schema.json 整理验证。

覆盖:
  1. 移除选项注释 (hint/description) 内的版本号 (v0.7 / v0.8.x / v1.0.0)
  2. 嵌套 group 结构 — webui 可分组展示
  3. _flatten_group_config 兼容老读法 (config.get("X") 命中)
"""
import os
import sys
import re
import json
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tests.stub_helpers import install_stubs  # noqa: E402
install_stubs()
import main  # noqa: E402


SCHEMA_PATH = Path(__file__).parent / "_conf_schema.json"


def _walk_schema_items(node, path="root"):
    """: 递归 yield (path, key, item_dict) 涵盖所有 schema 项。"""
    if not isinstance(node, dict):
        return
    if "items" in node and isinstance(node["items"], dict):
        # 这是 group
        for k, v in node["items"].items():
            yield f"{path}.items.{k}", k, v
    for k, v in node.items():
        if k == "items":
            continue
        if isinstance(v, dict):
            yield from _walk_schema_items(v, f"{path}.{k}")


def test_no_version_numbers_in_schema_comments():
    """: schema 的 description / hint 内不应含 v0.7 / v0.8.x / v1.x 版本号。"""
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    bad: list[tuple[str, str, str]] = []
    for path, key, item in _walk_schema_items(schema):
        for field in ("description", "hint"):
            v = item.get(field, "")
            if not v:
                continue
            # 找版本号模式: v0.7 / v0.8.2 / v1.0.0 / v0.8.13 / V4.0.0
            matches = re.findall(r"\b[Vv]\d+\.\d+(\.\d+)?\b", v)
            if matches:
                bad.append((path, field, str(matches)))
    assert not bad, f"以下 schema 项的 {field} 含版本号:\n" + "\n".join(
        f"  {p}.{f}: {m}" for p, f, m in bad
    )
    print("✓ test_no_version_numbers_in_schema_comments")


def test_schema_has_10_groups():
    """: schema 应有 10 个分组 (基础, MiniMax CLI, 并发, 图像理解, 缓存, 输入处理, LLM 提示, 跨插件兼容, 日志, 脱敏)。"""
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    groups = [
        k for k, v in schema.items()
        if isinstance(v, dict) and "items" in v and k != "_doc"
    ]
    expected = {
        "基础", "MiniMax CLI", "并发", "图像理解", "缓存",
        "输入处理", "LLM 提示", "跨插件兼容", "日志", "脱敏",
        "smart_imagechat_hub 兼容",
    }
    assert set(groups) == expected, f"分组不匹配, 实际: {groups}"
    print(f"✓ test_schema_has_10_groups ({len(groups)} groups)")


def test_flatten_nested_config_preserves_groups():
    """: 嵌套 schema 展平后 group 仍存在 (webui 渲染需要)。"""
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    flat = main._flatten_group_config(schema)
    # 嵌套 group 应保留
    assert "基础" in flat and "items" in flat["基础"]
    assert "缓存" in flat and "items" in flat["缓存"]
    print("✓ test_flatten_nested_config_preserves_groups")


def test_flatten_nested_config_exposes_top_level_keys():
    """: 嵌套 schema 展平后, 旧读法 config.get("X") 能命中 (所有 options 提到顶层)。"""
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    flat = main._flatten_group_config(schema)
    # 老读法
    for key in [
        "enabled", "priority", "mmx_path", "minimax_api_key",
        "max_concurrent_vision", "vision_prompt", "cache_descriptions",
        "memory_cache_ttl_seconds", "sqlite_cache_ttl_days",
        "verbose_logging", "redact_sensitive",
    ]:
        assert key in flat, f"展平后顶层缺少 {key} (老读法 config.get('{key}') 失败)"
    # 老读法取值
    assert flat["enabled"]["default"] is True
    assert flat["priority"]["default"] == 100
    assert flat["memory_cache_ttl_seconds"]["default"] == 300
    assert flat["redact_sensitive"]["default"] is True
    print("✓ test_flatten_nested_config_exposes_top_level_keys")


def test_flatten_does_not_change_flat_config():
    """: 扁平 schema (v0.8.x 时代) 展平后应原样不动。"""
    flat_schema = {
        "enabled": {"type": "bool", "default": True},
        "priority": {"type": "int", "default": 100},
        "verbose_logging": {"type": "bool", "default": False},
    }
    result = main._flatten_group_config(flat_schema)
    assert result is flat_schema or result == flat_schema
    # 不应修改原 dict 的 key
    assert "enabled" in result
    assert "items" not in result
    print("✓ test_flatten_does_not_change_flat_config")


def test_flatten_idempotent():
    """: 展平 1 次后, 再展平 1 次应无变化 (幂等)。"""
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    once = main._flatten_group_config(schema)
    twice = main._flatten_group_config(once)
    # 两次展平后顶层 keys 应相同
    assert set(once.keys()) == set(twice.keys())
    # 关键: 重复展平不应把 group 里的 items 二次提到顶层
    # (因为扁平后的 group 没有 "items" 字段在顶层 — items 在 group.items 下)
    # 但 group 的 items 字段仍在 group.items 里 (被重复展平的话会变空)
    assert once["基础"]["items"] == twice["基础"]["items"]
    print("✓ test_flatten_idempotent")


def test_all_schema_items_have_description():
    """: schema 每个选项都应有 description (webui 显示用)。"""
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    missing: list[str] = []
    for path, key, item in _walk_schema_items(schema):
        if "description" not in item or not item["description"].strip():
            missing.append(f"{path}.{key}")
    assert not missing, f"以下选项缺 description:\n" + "\n".join(f"  {p}" for p in missing)
    print("✓ test_all_schema_items_have_description")


def test_all_schema_items_have_type():
    """: schema 每个选项都应有 type。"""
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    missing: list[str] = []
    for path, key, item in _walk_schema_items(schema):
        if "type" not in item:
            missing.append(f"{path}.{key}")
    assert not missing, f"以下选项缺 type:\n" + "\n".join(f"  {p}" for p in missing)
    print("✓ test_all_schema_items_have_type")


def test_all_schema_options_have_default():
    """: schema 每个选项都应有 default (A. 减少用户心智负担, B. AstrBot 不会用 None 触发异常)。"""
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    missing: list[str] = []
    for path, key, item in _walk_schema_items(schema):
        if "default" not in item:
            # bool / list / dict 类型可省 default (AstrBot 会补), 但建议都写
            missing.append(f"{path}.{key} (type={item.get('type')})")
    assert not missing, f"以下选项缺 default:\n" + "\n".join(f"  {p}" for p in missing)
    print("✓ test_all_schema_options_have_default")


def test_schema_top_level_all_values_are_dict():
    """: schema 顶层每个 value 必须是 dict (含 items 字段的 group)。

    背景: AstrBot 框架在 _parse_schema 递归遍历 schema, 期望每个顶层 value
    都能 v["type"] 取到 type. 任何 string value (如 _doc 文档字段) 都会触发
    'string indices must be integers, not str' 错误并让插件加载失败。
    """
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    bad: list[tuple[str, str]] = []
    for k, v in schema.items():
        if not isinstance(v, dict):
            t = type(v).__name__
            bad.append((k, t))
    assert not bad, (
        f"以下 schema 顶层 value 不是 dict (会让 AstrBot 框架 _parse_schema 失败):\n"
        + "\n".join(f"  {k!r}: {t}" for k, t in bad)
        + "\n说明: schema 顶层只允许 group (含 items 的 dict). 文档/说明请放 README 或 metadata.yaml."
    )
    print("✓ test_schema_top_level_all_values_are_dict")


def test_config_helpers_nested_group():
    """: config_helpers.cfg_group_int / cfg_group_str 嵌套读法可用。"""
    from config_helpers import cfg_group_int, cfg_group_str, cfg_group_bool
    config = {"缓存": {"memory_cache_ttl_seconds": 600, "max_b64_size_kb": 1024}}
    assert cfg_group_int(config, "缓存", "memory_cache_ttl_seconds", 300) == 600
    assert cfg_group_int(config, "缓存", "missing_key", 300) == 300  # 走 default
    assert cfg_group_str(config, "基础", "priority_str", "100") == "100"  # 缺 group 走 default
    # 嵌套 bool
    config2 = {"基础": {"enabled": True}}
    assert cfg_group_bool(config2, "基础", "enabled", False) is True
    # 旧扁平兼容
    config3 = {"memory_cache_ttl_seconds": 500}
    assert cfg_group_int(config3, "缓存", "memory_cache_ttl_seconds", 300) == 500
    print("✓ test_config_helpers_nested_group")


if __name__ == "__main__":
    test_no_version_numbers_in_schema_comments()
    test_schema_has_10_groups()
    test_flatten_nested_config_preserves_groups()
    test_flatten_nested_config_exposes_top_level_keys()
    test_flatten_does_not_change_flat_config()
    test_flatten_idempotent()
    test_all_schema_items_have_description()
    test_all_schema_items_have_type()
    test_all_schema_options_have_default()
    test_config_helpers_nested_group()
    print("---")
    print("ALL SCHEMA TESTS PASSED")


def test_flatten_group_config_handles_format_B_no_items():
    """: _flatten_group_config 支持格式 B: 嵌套 dict 无 items 包装 (AstrBot v4.26 实际格式)."""
    from main import _flatten_group_config
    # 模拟 AstrBot v4.26 实际给的 config (无 items 包装, 直接是 group + fields)
    config = {
        "MiniMax CLI": {
            "minimax_api_key": "sk-test-1234",
            "auto_login": True,
            "auto_install_cli": True,
        },
        "基础": {
            "enabled": True,
            "priority": 100,
        },
    }
    flat = _flatten_group_config(config)
    # 顶层能命中 minimax_api_key (用户场景: '我配了但显示未配置')
    assert flat.get("minimax_api_key") == "sk-test-1234",         f"格式 B 应让 config.get('minimax_api_key') 命中, 实际 {flat.get('minimax_api_key')}"
    assert flat.get("auto_login") is True
    assert flat.get("enabled") is True
    assert flat.get("priority") == 100
    print("✓ test_flatten_group_config_handles_format_B_no_items")


def test_flatten_group_config_handles_format_A_with_items():
    """: _flatten_group_config 支持格式 A: 有 items 包装."""
    from main import _flatten_group_config
    config = {
        "基础": {"description": "...", "items": {"enabled": True, "priority": 100}},
        "MiniMax CLI": {"description": "...", "items": {"minimax_api_key": "sk-xxx"}},
    }
    flat = _flatten_group_config(config)
    assert flat.get("enabled") is True
    assert flat.get("priority") == 100
    assert flat.get("minimax_api_key") == "sk-xxx"
    print("✓ test_flatten_group_config_handles_format_A_with_items")


def test_flatten_group_config_handles_format_C_flat():
    """: _flatten_group_config 支持格式 C: 完全扁平."""
    from main import _flatten_group_config
    config = {"minimax_api_key": "sk-xxx", "enabled": True}
    flat = _flatten_group_config(config)
    assert flat.get("minimax_api_key") == "sk-xxx"
    assert flat.get("enabled") is True
    print("✓ test_flatten_group_config_handles_format_C_flat")


def test_flatten_group_config_real_user_scenario():
    """: 用户场景: dashboard 配 minimax_api_key → 应能读到."""
    from main import _flatten_group_config
    # 模拟 dashboard 保存的配置 (AstrBot 把 schema 解析后的 user-data 形式)
    config = {
        "基础": {"enabled": True, "priority": 100},
        "MiniMax CLI": {
            "mmx_path": "",
            "minimax_api_key": "sk-1234567890abcdef",  # 用户填的
            "auto_login": True,
            "auto_install_cli": True,
        },
        "缓存": {
            "cache_descriptions": True,
        },
    }
    flat = _flatten_group_config(config)
    assert flat.get("minimax_api_key") == "sk-1234567890abcdef"
    # 还能读 cache_descriptions
    assert flat.get("cache_descriptions") is True
    print("✓ test_flatten_group_config_real_user_scenario")


if __name__ == "__main__":
    test_flatten_group_config_handles_format_B_no_items()
    test_flatten_group_config_handles_format_A_with_items()
    test_flatten_group_config_handles_format_C_flat()
    test_flatten_group_config_real_user_scenario()
