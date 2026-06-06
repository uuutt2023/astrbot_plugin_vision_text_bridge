"""
caption_cache
=============

基于 SQLite 的图片描述缓存(含图片二进制)。

设计要点:

- **image_id 为主键**:用图片内容 md5(hashlib.md5(bytes).hexdigest())作为
  唯一标识。同一张图不论 URL 是什么都会命中同一条缓存。
- **image_b64 存图二进制**:base64 编码后存为 BLOB。webui 可以从这
  个字段出图缩略图,不再依赖外部 Chat Archive 等插件。
- **mime_type / file_size / width / height**:元信息。
- **image_url 保留原始 URL**:用于页面展示与"重新生成"功能。
- **线程安全**:SQLite 默认配置在多线程下不安全。本模块使用
  ``check_same_thread=False`` + 每个调用都开新连接的简单策略,
  适合低并发场景。
- **WAL 模式**:开 WAL 提升并发读性能。
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

    image_id: str  # 图片唯一标识(md5 of image bytes)
    image_url: str  # 原始 URL / 本地路径
    description: str  # mmx 图像理解结果
    created_at: float
    hit_count: int
    last_hit_at: float | None
    mime_type: str = ""  # image/jpeg, image/png, image/webp, image/gif
    file_size: int = 0  # 原始字节数
    width: int = 0  # 图片宽(从字节解析)
    height: int = 0  # 图片高
    image_b64: str = ""  # base64 编码的图片(webui 缩略图用)

    def to_dict(self) -> dict:
        d = {
            "image_id": self.image_id,
            "image_key": self.image_id,  # 向后兼容别名
            "image_url": self.image_url,
            "description": self.description,
            "created_at": self.created_at,
            "hit_count": self.hit_count,
            "last_hit_at": self.last_hit_at,
            "mime_type": self.mime_type,
            "file_size": self.file_size,
            "width": self.width,
            "height": self.height,
        }
        # 列表 API 不返 base64(太大);以独立接口 /cache/thumbnail/<image_id> 取缩略图
        return d


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
    """SQLite-backed image caption cache (含图片二进制)."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS image_captions (
        image_id TEXT PRIMARY KEY,
        image_url TEXT NOT NULL,
        description TEXT NOT NULL,
        mime_type TEXT NOT NULL DEFAULT '',
        file_size INTEGER NOT NULL DEFAULT 0,
        width INTEGER NOT NULL DEFAULT 0,
        height INTEGER NOT NULL DEFAULT 0,
        image_b64 TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL,
        hit_count INTEGER NOT NULL DEFAULT 0,
        last_hit_at REAL
    );
    CREATE INDEX IF NOT EXISTS idx_captions_created_at
        ON image_captions(created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_captions_hit_count
        ON image_captions(hit_count DESC);
    """

    # v0.8.6 起添加的列(用于老 DB 升级)
    _ALT_COLUMNS = [
        ("mime_type", "TEXT NOT NULL DEFAULT ''"),
        ("file_size", "INTEGER NOT NULL DEFAULT 0"),
        ("width", "INTEGER NOT NULL DEFAULT 0"),
        ("height", "INTEGER NOT NULL DEFAULT 0"),
        ("image_b64", "TEXT NOT NULL DEFAULT ''"),
    ]

    def __init__(self, db_path: str | os.PathLike):
        self._db_path = str(db_path)
        self._lock = threading.Lock()
        # ensure dir
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        # init schema
        with self._connect() as conn:
            # **v0.8.6 schema 升级逻辑**：
            # 1. 检查 image_captions 表是否存在
            # 2. 存在 → 可能是老库 (v0.8.5.x 用 image_key 为主键)，可能是新库 (v0.8.6+)
            # 3. 不存在 → 全新创建
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='image_captions'"
            ).fetchone()
            table_exists = row is not None
            if table_exists:
                existing_cols = {
                    r[1] for r in conn.execute("PRAGMA table_info(image_captions)").fetchall()
                }
                if "image_key" in existing_cols and "image_id" not in existing_cols:
                    # 升级路径 1：老库 v0.8.5.x (用 image_key)。重命名表后用新 schema 重建，
                    # 再把数据迁过来。**老库的 image_key 需要拷贝到新库的 image_id**（两者是同一个东西）。
                    conn.execute("ALTER TABLE image_captions RENAME TO image_captions__legacy")
                    conn.executescript(self.SCHEMA)
                    conn.execute(
                        "INSERT INTO image_captions "
                        "(image_id, image_url, description, created_at, hit_count, last_hit_at) "
                        "SELECT image_key, image_url, description, created_at, hit_count, last_hit_at "
                        "FROM image_captions__legacy"
                    )
                    conn.execute("DROP TABLE image_captions__legacy")
                else:
                    # 升级路径 2：已用新 schema (v0.8.6+)，但可能缺新加的列
                    for col_name, col_def in self._ALT_COLUMNS:
                        if col_name not in existing_cols:
                            conn.execute(
                                f"ALTER TABLE image_captions ADD COLUMN {col_name} {col_def}"
                            )
            else:
                # 全新创建
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
    def make_id_from_url(url_or_path: str) -> str:
        """从 URL/path 生成 image_id(v0.8.6 退化的 key 生成,
        实际推荐用 make_id_from_bytes 拿 md5)。"""
        return hashlib.md5((url_or_path or "").encode("utf-8")).hexdigest()

    @staticmethod
    def make_id_from_bytes(data: bytes) -> str:
        """从图片字节生成 image_id(**推荐**:同一张图内容不变 id 不变)。"""
        return hashlib.md5(data).hexdigest()

    # ------------------------------------------------------------------ CRUD

    def get(self, image_id: str, with_b64: bool = False) -> CaptionEntry | None:
        """根据 image_id 查询一条记录,命中后增加 hit_count（上次 hit 5 分钟内不重复加）。

        Args:
            image_id: 图片唯一标识
            with_b64: 是否返回 base64(**不**在 list 中用,列表里**不**含 base64)
        """
        if not image_id:
            return None
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM image_captions WHERE image_id = ?", (image_id,)
            ).fetchone()
            if row is None:
                return None
            now = time.time()
            last_hit = row["last_hit_at"] or 0
            # 去重：5 分钟内的连续 hit 不计（避免 webui 详情页点 10 次就 10）
            if now - last_hit >= 300:
                conn.execute(
                    "UPDATE image_captions SET hit_count = hit_count + 1, last_hit_at = ? "
                    "WHERE image_id = ?",
                    (now, image_id),
                )
                conn.commit()
                return self._row_to_entry(row, hit_count_delta=1, last_hit_at=now, with_b64=with_b64)
            # 5 分钟内重复 hit：不增计数，但更新内存返回值里的 last_hit_at 反映最近查询
            return self._row_to_entry(row, last_hit_at=now, with_b64=with_b64)

    def put(
        self,
        image_id: str,
        url: str,
        description: str,
        *,
        image_b64: str = "",
        mime_type: str = "",
        file_size: int = 0,
        width: int = 0,
        height: int = 0,
    ) -> None:
        """插入或更新一条记录(基于 image_id 主键)。

        v0.8.6 起:
        - 主键 image_id 是 md5(由调用方从图片内容算出)
        - url 保留原始 URL/path
        - 额外字段:image_b64 (base64)、mime_type、file_size、width、height
        """
        if not image_id or not description:
            # v0.8.7.3: 之前静默 return 掩盖了"为何 SQLite total=0"的问题。
            # 现在打 warning。
            import logging
            logging.getLogger("astrbot_plugin_vision_text_bridge").warning(
                "[caption_cache] put() 被调用但 image_id=%r description_len=%d，**未写入**。",
                image_id, len(description) if description else 0,
            )
            return
        with self._lock, self._connect() as conn:
            now = time.time()
            conn.execute(
                """
                INSERT INTO image_captions
                    (image_id, image_url, description, mime_type, file_size,
                     width, height, image_b64, created_at, hit_count, last_hit_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL)
                ON CONFLICT(image_id) DO UPDATE SET
                    image_url = excluded.image_url,
                    description = excluded.description,
                    mime_type = excluded.mime_type,
                    file_size = excluded.file_size,
                    width = excluded.width,
                    height = excluded.height,
                    image_b64 = excluded.image_b64,
                    created_at = excluded.created_at
                """,
                (image_id, url, description, mime_type, file_size, width, height, image_b64, now),
            )
            conn.commit()

    def delete(self, image_id: str) -> bool:
        """删除一条记录。"""
        if not image_id:
            return False
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM image_captions WHERE image_id = ?", (image_id,)
            )
            conn.commit()
            return cur.rowcount > 0

    def clear(self) -> int:
        """清空所有记录,返回删除条数。"""
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
        include_b64: bool = False,
    ) -> list[CaptionEntry]:
        """列出记录,支持分页和搜索(按 url 或 description 模糊匹配)。

        默认**不**返 base64(避免接口 body 过大)。需要时用
        ``include_b64=True``(仍建议用 /cache/thumbnail/<id> 单独 endpoint)。
        """
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
            self._row_to_entry(r, with_b64=include_b64) for r in rows
        ]

    def _row_to_entry(self, r, hit_count_delta: int = 0, last_hit_at: float | None = None, with_b64: bool = False) -> CaptionEntry:
        b64 = r["image_b64"] if with_b64 else ""
        return CaptionEntry(
            image_id=r["image_id"],
            image_url=r["image_url"],
            description=r["description"],
            created_at=r["created_at"],
            hit_count=r["hit_count"] + hit_count_delta,
            last_hit_at=last_hit_at if last_hit_at is not None else r["last_hit_at"],
            mime_type=r["mime_type"],
            file_size=r["file_size"],
            width=r["width"],
            height=r["height"],
            image_b64=b64,
        )

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
        """清理数据库碎片(VACUUM)。调用时机:大量删除后。"""
        with self._lock, self._connect() as conn:
            conn.execute("VACUUM")
            conn.commit()

    def clean_expired(self, max_age_days: int) -> int:
        """v0.8.11: 清理超期未命中的条目。

        语义: ``last_hit_at`` 早于 ``now - max_age_days`` 的条目视为冷数据，删掉。
        刚 put 还没被 get 过的条目用 ``created_at`` 代替 ``last_hit_at`` 判断
        （刚 put 的话 ``last_hit_at=0``，会被误判为过期——这是 bug，不能用 last_hit_at）。

        Args:
            max_age_days: 最大保留天数。<=0 不清理。

        Returns:
            删掉的条数。
        """
        if max_age_days <= 0:
            return 0
        # ``last_hit_at IS NULL OR last_hit_at < 0`` 意味着从未被 get——以 created_at 为准
        cutoff = time.time() - max_age_days * 86400
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM image_captions "
                "WHERE (last_hit_at IS NULL OR last_hit_at = 0) "
                "  AND created_at < ?",
                (cutoff,),
            )
            deleted_unhit = cur.rowcount
            cur = conn.execute(
                "DELETE FROM image_captions WHERE last_hit_at > 0 AND last_hit_at < ?",
                (cutoff,),
            )
            deleted_hit = cur.rowcount
            conn.commit()
        return deleted_unhit + deleted_hit
