"""caption_cache.py - SQLite 描述缓存 (WAL 模式)。

数据: image_md5 + description + image_url + image_b64 + created_at + 缓存字段
API: get / put / delete / list / clean_expired / count / daily_buckets
作者: uuutt
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

_log = logging.getLogger("astrbot_plugin_vision_text_bridge")


@dataclass
class CaptionEntry:
    """缓存中的一条记录。"""

    image_id: str  # 图片唯一标识(md5 of image bytes)
    image_url: str  # 原始 URL / 本地路径
    description: str  # mmx 图像理解结果
    created_at: float
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
    oldest_at: float | None
    newest_at: float | None
    db_size_bytes: int

    def to_dict(self) -> dict:
        return {
            "total": self.total,
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
        created_at REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_captions_created_at
        ON image_captions(created_at DESC);

    CREATE TABLE IF NOT EXISTS call_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        time TEXT NOT NULL,
        url TEXT NOT NULL,
        source TEXT NOT NULL,
        status TEXT NOT NULL,
        duration_ms INTEGER NOT NULL DEFAULT 0,
        cached INTEGER NOT NULL DEFAULT 0,
        error TEXT,
        created_at REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_call_log_created_at
        ON call_log(created_at DESC);
    """

    # 后续添加的列(用于老 DB 升级)
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
            # **schema 升级逻辑**：
            # 1. 检查 image_captions 表是否存在
            # 2. 存在 → 可能是老库 (用 image_key 为主键)，可能是新库 (image_id 为主键)
            # 3. 不存在 → 全新创建
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='image_captions'"
            ).fetchone()
            table_exists = row is not None
            if table_exists:
                existing_cols = {
                    r[1]
                    for r in conn.execute(
                        "PRAGMA table_info(image_captions)"
                    ).fetchall()
                }
                if "image_key" in existing_cols and "image_id" not in existing_cols:
                    # 升级路径 1：老库 (用 image_key)。重命名表后用新 schema 重建，
                    # 再把数据迁过来。**老库的 image_key 需要拷贝到新库的 image_id**（两者是同一个东西）。
                    conn.execute(
                        "ALTER TABLE image_captions RENAME TO image_captions__legacy"
                    )
                    conn.executescript(self.SCHEMA)
                    conn.execute(
                        "INSERT INTO image_captions "
                        "(image_id, image_url, description, created_at) "
                        "SELECT image_key, image_url, description, created_at "
                        "FROM image_captions__legacy"
                    )
                    conn.execute("DROP TABLE image_captions__legacy")
                else:
                    # 升级路径 2：已用新 schema (image_id 为主键)，但可能缺新加的列
                    for col_name, col_def in self._ALT_COLUMNS:
                        if col_name not in existing_cols:
                            conn.execute(
                                f"ALTER TABLE image_captions ADD COLUMN {col_name} {col_def}"
                            )
            else:
                # 全新创建
                conn.executescript(self.SCHEMA)

            # **call_log 表升级**：旧 DB 可能只有 image_captions，没有 call_log
            # （call_log 是在 image_captions 之后追加的功能）。无论上面走哪条路径，
            # 都额外单独确保 call_log 表存在，幂等。
            self._ensure_call_log_table(conn)
            conn.commit()

    def _ensure_call_log_table(self, conn: sqlite3.Connection) -> None:
        """确保 call_log 表 + 索引存在（幂等），用于旧 DB 升级场景。"""
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='call_log'"
        ).fetchone()
        if row is not None:
            return
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS call_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                time TEXT NOT NULL,
                url TEXT NOT NULL,
                source TEXT NOT NULL,
                status TEXT NOT NULL,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                cached INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_call_log_created_at
                ON call_log(created_at DESC);
            """
        )
        _log.info("[vision_text_bridge] 已为旧 DB 补建 call_log 表")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False, timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------ key utils

    @staticmethod
    def make_id_from_url(url_or_path: str) -> str:
        """从 URL/path 生成 image_id(退化的 key 生成,
        实际推荐用 make_id_from_bytes 拿 md5)。"""
        return hashlib.md5((url_or_path or "").encode("utf-8")).hexdigest()

    @staticmethod
    def make_id_from_bytes(data: bytes) -> str:
        """从图片字节生成 image_id(**推荐**:同一张图内容不变 id 不变)。"""
        return hashlib.md5(data).hexdigest()

    # ------------------------------------------------------------------ CRUD

    def get(self, image_id: str, with_b64: bool = False) -> CaptionEntry | None:
        """根据 image_id 查询一条记录(纯读, 不增统计)。

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
            return self._row_to_entry(row, with_b64=with_b64)

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
    ) -> bool:
        """插入或更新一条记录(基于 image_id 主键)。

        - 主键 image_id 是 md5(由调用方从图片内容算出)
        - url 保留原始 URL/path
        - 额外字段:image_b64 (base64)、mime_type、file_size、width、height
        """
        if not image_id or not description:
            # 之前静默 return 掩盖了“为何 SQLite total=0”的问题。
            # 现在打 warning。
            _log.warning(
                "[caption_cache] put() 被调用但 image_id=%r description_len=%d，**未写入**。",
                image_id,
                len(description) if description else 0,
            )
            return False
        with self._lock, self._connect() as conn:
            now = time.time()
            conn.execute(
                """
                INSERT INTO image_captions
                    (image_id, image_url, description, mime_type, file_size,
                     width, height, image_b64, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                (
                    image_id,
                    url,
                    description,
                    mime_type,
                    file_size,
                    width,
                    height,
                    image_b64,
                    now,
                ),
            )
            conn.commit()
        return True

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
        # : 用列下标 (1=created_at) + ASC/DESC 标识 - 避免 f-string 拼 order_by
        #   白名单兜底 - 未知值默认 created_at DESC
        _order_dir = (
            "ASC"
            if order_by.endswith("_asc")
            else "DESC"
            if order_by.endswith("_desc")
            else "DESC"
        )
        # 只认 2 个列下标
        order_clause = f"ORDER BY 1 {_order_dir}"

        with self._lock, self._connect() as conn:
            if search:
                like = f"%{search}%"
                rows = conn.execute(
                    f"SELECT * FROM image_captions "
                    f"WHERE image_url LIKE ? OR description LIKE ? "
                    f"{order_clause} LIMIT ? OFFSET ?",
                    (like, like, limit, offset),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT * FROM image_captions {order_clause} LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
        return [self._row_to_entry(r, with_b64=include_b64) for r in rows]

    def _row_to_entry(self, r, with_b64: bool = False) -> CaptionEntry:
        b64 = r["image_b64"] if with_b64 else ""
        return CaptionEntry(
            image_id=r["image_id"],
            image_url=r["image_url"],
            description=r["description"],
            created_at=r["created_at"],
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
                row = conn.execute(
                    "SELECT COUNT(*) AS c FROM image_captions"
                ).fetchone()
            return int(row["c"])

    def stats(self) -> CacheStats:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    MIN(created_at) AS oldest,
                    MAX(created_at) AS newest
                FROM image_captions
                """
            ).fetchone()
            total = int(row["total"])
            oldest = row["oldest"]
            newest = row["newest"]
        size = 0
        try:
            size = os.path.getsize(self._db_path)
        except OSError:
            pass
        return CacheStats(
            total=total,
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
        """: 清理超过 max_age_days 天未更新的条目(按 created_at 判断)。

        Args:
            max_age_days: 最大保留天数。<=0 不清理。

        Returns:
            删掉的条数。
        """
        if max_age_days <= 0:
            return 0
        cutoff = time.time() - max_age_days * 86400
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM image_captions WHERE created_at < ?",
                (cutoff,),
            )
            deleted = cur.rowcount
            conn.commit()
        return deleted

    def daily_buckets(self, days: int = 30) -> list[dict]:
        """: 按天统计缓存创建量，返回 ``[{date: 'YYYY-MM-DD', count: N}, ...]``。

        用 ``strftime('%Y-%m-%d', created_at, 'unixepoch')`` 按天分组，
        缺天补 0（让 webui 画连续的 30 天柱状图）。
        """
        # 算 window 起点（UTC 0 点）
        now = datetime.datetime.utcnow()
        start_of_today = datetime.datetime(now.year, now.month, now.day)
        start_ts = (start_of_today - datetime.timedelta(days=days - 1)).timestamp()
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT strftime('%Y-%m-%d', created_at, 'unixepoch') AS d, "
                "       COUNT(*) AS c "
                "FROM image_captions "
                "WHERE created_at >= ? "
                "GROUP BY d "
                "ORDER BY d",
                (start_ts,),
            ).fetchall()
        # 缺天补 0
        counts = {r["d"]: r["c"] for r in rows}
        out = []
        for i in range(days - 1, -1, -1):
            day = (start_of_today - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
            out.append({"date": day, "count": counts.get(day, 0)})
        return out

    # ------------------------------------------------------------------ call_log persistence

    def insert_call_log(self, entry: dict) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO call_log (time, url, source, status, duration_ms, cached, error, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entry.get("time", ""),
                    entry.get("url", ""),
                    entry.get("source", ""),
                    entry.get("status", "unknown"),
                    entry.get("duration_ms", 0),
                    1 if entry.get("cached") else 0,
                    entry.get("error"),
                    entry.get("created_at", time.time()),
                ),
            )
            conn.commit()

    def load_call_logs(self, limit: int = 200) -> list[dict]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT time, url, source, status, duration_ms, cached, error "
                "FROM call_log ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "time": r["time"],
                "url": r["url"],
                "source": r["source"],
                "status": r["status"],
                "duration_ms": r["duration_ms"],
                "cached": bool(r["cached"]),
                "error": r["error"],
            }
            for r in rows
        ]

    def clean_call_logs(self, keep: int = 200) -> int:
        deleted = 0
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM call_log").fetchone()
            total = int(row["c"])
            if total > keep:
                excess = total - keep
                cur = conn.execute(
                    "DELETE FROM call_log WHERE id IN ("
                    "  SELECT id FROM call_log ORDER BY id ASC LIMIT ?"
                    ")",
                    (excess,),
                )
                deleted = cur.rowcount
                conn.commit()
        return deleted
