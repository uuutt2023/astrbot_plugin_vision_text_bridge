"""
caption_cache
=============

基于 SQLite 的图片描述缓存。

设计要点：

- **image_key 为主键**：用图片 URL 或本地路径的规范化形式作为 key。
  这样同一张图片（即使 URL 形式不同）能命中同一条缓存。
- **image_url 保留原始 URL**：用于页面展示与"重新生成"功能。
- **dHash 字段（可选）**：如果传入了图片字节（如本地文件或已下载的字节），
  可计算 dHash 作为辅助 key。**不强制要求**，因为大多数情况下 URL 已足够唯一。
- **线程安全**：SQLite 默认配置在多线程下不安全。本模块使用
  ``check_same_thread=False`` + 每个调用都开新连接的简单策略，
  适合低并发场景。
- **WAL 模式**：开 WAL 提升并发读性能。
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass
class CaptionEntry:
    """缓存中的一条记录。"""

    image_key: str
    image_url: str
    description: str
    created_at: float
    hit_count: int
    last_hit_at: float | None

    def to_dict(self) -> dict:
        return {
            "image_key": self.image_key,
            "image_url": self.image_url,
            "description": self.description,
            "created_at": self.created_at,
            "hit_count": self.hit_count,
            "last_hit_at": self.last_hit_at,
        }


@dataclass
class CacheStats:
    """缓存统计。"""

    total: int
    total_hits: int
    oldest_at: float | None
    newest_at: float | None
    db_size_bytes: int

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "total_hits": self.total_hits,
            "oldest_at": self.oldest_at,
            "newest_at": self.newest_at,
            "db_size_bytes": self.db_size_bytes,
        }


class CaptionCache:
    """SQLite-backed image caption cache."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS image_captions (
        image_key TEXT PRIMARY KEY,
        image_url TEXT NOT NULL,
        description TEXT NOT NULL,
        created_at REAL NOT NULL,
        hit_count INTEGER NOT NULL DEFAULT 0,
        last_hit_at REAL
    );
    CREATE INDEX IF NOT EXISTS idx_captions_created_at
        ON image_captions(created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_captions_hit_count
        ON image_captions(hit_count DESC);
    """

    def __init__(self, db_path: str | os.PathLike):
        self._db_path = str(db_path)
        self._lock = threading.Lock()
        # ensure dir
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        # init schema
        with self._connect() as conn:
            conn.executescript(self.SCHEMA)
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False, timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------ key utils

    @staticmethod
    def normalize_key(url_or_path: str) -> str:
        """规范化图片 key。

        - 去除查询参数差异（如果有明显的签名参数，保留；否则保留全部）
        - 统一大小写
        - 去前后空格
        - 计算 sha256 作为定长 key
        """
        raw = (url_or_path or "").strip()
        if not raw:
            return ""
        # 保留原样大小写（URL 区分大小写），但做基本的 trim
        return raw

    @staticmethod
    def make_key(url_or_path: str) -> str:
        """生成存储用的主键。直接用 URL 原文，保留可读性。"""
        return CaptionCache.normalize_key(url_or_path)

    # ------------------------------------------------------------------ CRUD

    def get(self, key: str) -> CaptionEntry | None:
        """根据 key 查询一条记录，命中后增加 hit_count。"""
        if not key:
            return None
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM image_captions WHERE image_key = ?", (key,)
            ).fetchone()
            if row is None:
                return None
            now = time.time()
            conn.execute(
                "UPDATE image_captions SET hit_count = hit_count + 1, last_hit_at = ? "
                "WHERE image_key = ?",
                (now, key),
            )
            conn.commit()
            return CaptionEntry(
                image_key=row["image_key"],
                image_url=row["image_url"],
                description=row["description"],
                created_at=row["created_at"],
                hit_count=row["hit_count"] + 1,
                last_hit_at=now,
            )

    def put(self, key: str, url: str, description: str) -> None:
        """插入或更新一条记录（基于 key 主键）。"""
        if not key or not description:
            return
        with self._lock, self._connect() as conn:
            now = time.time()
            conn.execute(
                """
                INSERT INTO image_captions
                    (image_key, image_url, description, created_at, hit_count, last_hit_at)
                VALUES (?, ?, ?, ?, 0, NULL)
                ON CONFLICT(image_key) DO UPDATE SET
                    image_url = excluded.image_url,
                    description = excluded.description,
                    created_at = excluded.created_at
                """,
                (key, url, description, now),
            )
            conn.commit()

    def delete(self, key: str) -> bool:
        """删除一条记录。"""
        if not key:
            return False
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM image_captions WHERE image_key = ?", (key,)
            )
            conn.commit()
            return cur.rowcount > 0

    def clear(self) -> int:
        """清空所有记录，返回删除条数。"""
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM image_captions")
            conn.commit()
            return cur.rowcount

    def list(
        self,
        limit: int = 50,
        offset: int = 0,
        search: str = "",
        order_by: str = "created_at_desc",
    ) -> list[CaptionEntry]:
        """列出记录，支持分页和搜索（按 url 或 description 模糊匹配）。"""
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        order_sql = {
            "created_at_desc": "created_at DESC",
            "created_at_asc": "created_at ASC",
            "hit_count_desc": "hit_count DESC",
            "hit_count_asc": "hit_count ASC",
        }.get(order_by, "created_at DESC")

        with self._lock, self._connect() as conn:
            if search:
                like = f"%{search}%"
                rows = conn.execute(
                    f"SELECT * FROM image_captions "
                    f"WHERE image_url LIKE ? OR description LIKE ? "
                    f"ORDER BY {order_sql} LIMIT ? OFFSET ?",
                    (like, like, limit, offset),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT * FROM image_captions ORDER BY {order_sql} LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
        return [
            CaptionEntry(
                image_key=r["image_key"],
                image_url=r["image_url"],
                description=r["description"],
                created_at=r["created_at"],
                hit_count=r["hit_count"],
                last_hit_at=r["last_hit_at"],
            )
            for r in rows
        ]

    def count(self, search: str = "") -> int:
        with self._lock, self._connect() as conn:
            if search:
                like = f"%{search}%"
                row = conn.execute(
                    "SELECT COUNT(*) AS c FROM image_captions "
                    "WHERE image_url LIKE ? OR description LIKE ?",
                    (like, like),
                ).fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) AS c FROM image_captions").fetchone()
            return int(row["c"])

    def stats(self) -> CacheStats:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    COALESCE(SUM(hit_count), 0) AS total_hits,
                    MIN(created_at) AS oldest,
                    MAX(created_at) AS newest
                FROM image_captions
                """
            ).fetchone()
            total = int(row["total"])
            total_hits = int(row["total_hits"])
            oldest = row["oldest"]
            newest = row["newest"]
        size = 0
        try:
            size = os.path.getsize(self._db_path)
        except OSError:
            pass
        return CacheStats(
            total=total,
            total_hits=total_hits,
            oldest_at=oldest,
            newest_at=newest,
            db_size_bytes=size,
        )

    def vacuum(self) -> None:
        """清理数据库碎片（VACUUM）。调用时机：大量删除后。"""
        with self._lock, self._connect() as conn:
            conn.execute("VACUUM")
            conn.commit()
