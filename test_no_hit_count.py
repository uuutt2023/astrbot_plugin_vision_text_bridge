"""test_no_hit_count.py — 反向验证: 打开 webui / 重复 get() 不会递增 hit_count。

背景: 用户报告 webui 打开 → hit_count 累加, hit 统计无意义。
修复: CaptionCache.get() 改为纯读, 不写 hit_count/last_hit_at。
"""
import os
import sys
import sqlite3
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# stub (复用 test.py 模式)
import types as _t
stub = _t.ModuleType("astrbot")
api = _t.ModuleType("astrbot.api")
api.AstrBotConfig = dict
api.logger = SimpleNamespace(
    info=lambda *a, **k: None, warning=lambda *a, **k: None,
    error=lambda *a, **k: None, exception=lambda *a, **k: None,
    debug=lambda *a, **k: None,
)
ev = _t.ModuleType("astrbot.api.event")
ev.AstrMessageEvent = SimpleNamespace
ev.filter = SimpleNamespace(
    on_llm_request=lambda *a, **k: (lambda f: f),
    command=lambda *a, **k: (lambda f: f),
    command_group=lambda *a, **k: (lambda f: f),
)
ev.MessageChain = list
pr = _t.ModuleType("astrbot.api.provider")
pr.ProviderRequest = SimpleNamespace
st = _t.ModuleType("astrbot.api.star")
st.Context = SimpleNamespace
st.Star = object
st.register = lambda *a, **k: (lambda c: c)
sys.modules.setdefault("astrbot", stub)
sys.modules.setdefault("astrbot.api", api)
sys.modules.setdefault("astrbot.api.event", ev)
sys.modules.setdefault("astrbot.api.provider", pr)
sys.modules.setdefault("astrbot.api.star", st)
stub.api = api
import main  # noqa: E402
import caption_cache  # noqa: E402


def test_get_does_not_increment_hit_count():
    """: get() 重复调用 100 次, hit_count 仍为 0 (字段已删, 旧库不写)。

    模拟场景: 用户打开 webui → 列表查询触发 cache.get() N 次,
    现在不再让 hit_count 累加 (字段彻底删除)。
    """
    with tempfile.TemporaryDirectory() as tmp:
        cache = main.CaptionCache(Path(tmp) / "no_hit.db")
        cache.put("img1", "https://a.com/1.jpg", "猫")

        # 重复 get 100 次
        for _ in range(100):
            entry = cache.get("img1")
            assert entry is not None
            assert entry.description == "猫"

        # 验证: 内存对象没有 hit_count 属性
        assert not hasattr(entry, "hit_count"), \
            "CaptionEntry 不应有 hit_count 属性 (已删除)"

        # 验证: SQLite 旧库命中时也不应有 (新 schema 不写 hit_count)
        with sqlite3.connect(cache._db_path) as conn:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(image_captions)").fetchall()]
        # 新 schema 不再写 hit_count/last_hit_at — 列为 NULL 或 0
        # 老 schema 可能仍有列但 cache 不写入
        if "hit_count" in cols:
            with sqlite3.connect(cache._db_path) as conn:
                row = conn.execute(
                    "SELECT hit_count FROM image_captions WHERE image_id = ?", ("img1",)
                ).fetchone()
            # 新逻辑: 100 次 get 后 hit_count 不应增加
            # (老库迁移后 hit_count 仍存旧值; 但新 put 设为 0, get 不递增)
            assert row[0] == 0, f"get() 不应递增 hit_count, 实际 hit_count={row[0]}"
    print("✓ test_get_does_not_increment_hit_count")


def test_caption_entry_has_no_hit_count():
    """: CaptionEntry 数据类不再有 hit_count / last_hit_at 字段。"""
    from dataclasses import fields
    field_names = {f.name for f in fields(caption_cache.CaptionEntry)}
    assert "hit_count" not in field_names, "CaptionEntry 不应有 hit_count"
    assert "last_hit_at" not in field_names, "CaptionEntry 不应有 last_hit_at"
    print("✓ test_caption_entry_has_no_hit_count")


def test_cache_stats_has_no_total_hits():
    """: CacheStats 不再含 total_hits。"""
    from dataclasses import fields
    field_names = {f.name for f in fields(caption_cache.CacheStats)}
    assert "total_hits" not in field_names, "CacheStats 不应有 total_hits"

    with tempfile.TemporaryDirectory() as tmp:
        cache = main.CaptionCache(Path(tmp) / "stats.db")
        cache.put("a", "https://a", "猫")
        cache.get("a")
        cache.get("a")
        s = cache.stats()
        d = s.to_dict()
        assert "total_hits" not in d, f"stats dict 不应有 total_hits, 实际: {d}"
    print("✓ test_cache_stats_has_no_total_hits")


def test_list_does_not_include_hit_count_in_result():
    """: list() 返的 entry 不含 hit_count, to_dict 也不含 hit_count。"""
    with tempfile.TemporaryDirectory() as tmp:
        cache = main.CaptionCache(Path(tmp) / "list.db")
        cache.put("a", "https://a.com/1", "猫")
        cache.put("b", "https://a.com/2", "狗")
        items = cache.list(limit=10)
        assert len(items) == 2
        for item in items:
            d = item.to_dict()
            assert "hit_count" not in d, f"to_dict 不应含 hit_count, 实际: {d}"
            assert "last_hit_at" not in d, f"to_dict 不应含 last_hit_at, 实际: {d}"
    print("✓ test_list_does_not_include_hit_count_in_result")


def test_clean_expired_uses_created_at_only():
    """: clean_expired 只看 created_at (不再用 last_hit_at 区分冷数据)。"""
    import time
    with tempfile.TemporaryDirectory() as tmp:
        cache = main.CaptionCache(Path(tmp) / "clean.db")
        cache.put("k1", "https://a.com/1", "猫")
        cache.put("k2", "https://a.com/2", "狗")
        # 把 k1 created_at 调到 100 天前
        with sqlite3.connect(cache._db_path) as c:
            c.execute(
                "UPDATE image_captions SET created_at = ? WHERE image_id = 'k1'",
                (time.time() - 100 * 86400,),
            )
            c.commit()
        # 清理 7 天前: k1 应被删 (按 created_at)
        deleted = cache.clean_expired(max_age_days=7)
        assert deleted == 1, f"应删 1 条 (k1), 实际删 {deleted}"
        assert cache.count() == 1
        # k2 仍存在
        assert cache.get("k2") is not None
    print("✓ test_clean_expired_uses_created_at_only")


if __name__ == "__main__":
    test_get_does_not_increment_hit_count()
    test_caption_entry_has_no_hit_count()
    test_cache_stats_has_no_total_hits()
    test_list_does_not_include_hit_count_in_result()
    test_clean_expired_uses_created_at_only()
    print("---")
    print("ALL NO-HIT-COUNT TESTS PASSED")
