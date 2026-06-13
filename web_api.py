"""
vision_text_bridge.web_api
============================

注册到 AstrBot ``context.register_web_api`` 的全部 web 后端 API。

设计要点:
- 全部 handler 在 ``register_all_routes(context, plugin)`` 里挂,
  不再嵌套在 plugin 类的 ``_register_web_apis`` 闭包里 (深度从 4 减到 1)
- ``quart_request`` 是从 ``from quart import request`` 拿的全局对象
  — angel_memory 的同款, 也是 AstrBot 框架的真实 web 层
- ``plugin`` 是 :class:`VisionTextBridgePlugin` 实例, 通过它读
  ``_caption_cache`` / ``_description_cache`` / ``_last_clean_at`` / ``config`` /
  ``_describe_one``

注意: ``api_list`` 之前有死代码 ``body = await self.context.request.json``
( 时代的残骸), 已在  整理中删除。
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .main import VisionTextBridgePlugin

from astrbot.api import logger
from config_helpers import cfg_int
import chat_archive_integration  # : 顶部 import, 避免函数内 import 重复


# 顶层常量
PLUGIN_NAME = "astrbot_plugin_vision_text_bridge"


# ---------------------------------------------------------------------------
# Quart 全局 request 注入 ( 改用, 之前 self.context.request 是错的)
# ---------------------------------------------------------------------------
try:
    from quart import jsonify, request as quart_request
except ImportError:  # 测试沙箱没装 quart
    def jsonify(o):  # type: ignore
        return o

    class _MockQuartRequest:
        args: dict = {}
        _json = None
        form = {}
        bytes_body: bytes = b""

        async def get_json(self, silent: bool = True):
            return self._json

        async def get_data(self, as_text: bool = False):
            return self.bytes_body.decode() if as_text else self.bytes_body

        async def form(self):
            return self.form

    quart_request = _MockQuartRequest()


# ---------------------------------------------------------------------------
# 响应包装
# ---------------------------------------------------------------------------
def ok(data):
    return jsonify({"ok": True, "data": data})


def err(message: str, status: int = 400):
    return jsonify({"ok": False, "error": message}), status


def _require_caption_cache(plugin):
    """: 检查 plugin._caption_cache 是否初始化。未初始化返 err tuple, 否则返 None。

    用法:  handler 顶部
        miss = _require_caption_cache(plugin)
        if miss is not None:
            return miss
    """
    if plugin._caption_cache is None:
        return err("SQLite 缓存未初始化", 500)
    return None


def _build_thumbnail_payload(image_id: str, mime: str, b64: str, w: int, h: int, size: int):
    """: 统一组装缩略图 dict —— 避免 3 个分支手搓重复字段。

    b64 为空时 mime 保留原值 (老条目可能未存 mime, 返回空串);
    b64 非空时 mime 退到 "image/jpeg"。
    """
    effective_mime = (mime or "image/jpeg") if b64 else mime
    return {
        "image_id": image_id,
        "mime_type": effective_mime,
        "data_url": f"data:{effective_mime};base64,{b64}" if b64 else "",
        "width": w, "height": h, "file_size": size,
        "has_image": bool(b64),
    }


def read_args() -> dict:
    """读 query string 全部参数。"""
    try:
        return quart_request.args or {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# key 提取 — 4 路径防御 (~37 调试沉淀)
# ---------------------------------------------------------------------------
async def read_key_from_request(context, debug: bool = True) -> str:
    """从请求体里读 'key' 参数。

    4 路径按顺序:
        1. query string (?key=xxx) — GET 主路径
        2. json body ({"key": "..."}) — bridge.apiPost 主路径
        3. form body (key=xxx) — sendBeacon
        4. raw text (key=xxx) — 兜底

    返回 key (空字符串 = 没找到)。

    注:  加 debug=True 打到 warning level, 调试 backend 接收问题时用。
    """
    key = ""
    debug_lines: list[str] = []

    # 1. query
    try:
        query = quart_request.args or {}
        if hasattr(query, "get"):
            key = (query.get("key") or "").strip()
        debug_lines.append(
            f"query={dict(query) if hasattr(query, 'items') else query!r}"
        )
    except Exception as e:
        debug_lines.append(f"query-err={e}")
    if key:
        if debug:
            logger.warning(f"[vtb-debug] key from query: {key[:12]}  ({'; '.join(debug_lines)})")
        return key

    # 2. json body
    try:
        body = await quart_request.get_json(silent=True)
        if body is None:
            body = {}
        # 兼容两种 key 名: 'key' (我的代码) / 'id' (angel_memory 风格)
        key = (body.get("key") or body.get("id") or "").strip()
        debug_lines.append(f"json-body={body!r}")
    except Exception as e:
        debug_lines.append(f"json-err={type(e).__name__}: {e}")
    if key:
        if debug:
            logger.warning(f"[vtb-debug] key from json: {key[:12]}  ({'; '.join(debug_lines)})")
        return key

    # 3. form body
    try:
        form = await quart_request.form
        if form and hasattr(form, "get"):
            key = (form.get("key") or form.get("id") or "").strip()
            debug_lines.append(
                f"form-body={dict(form) if hasattr(form, 'items') else form!r}"
            )
    except Exception as e:
        debug_lines.append(f"form-err={type(e).__name__}")
    if key:
        if debug:
            logger.warning(f"[vtb-debug] key from form: {key[:12]}  ({'; '.join(debug_lines)})")
        return key

    # 4. raw text 兜底
    try:
        raw = await quart_request.get_data(as_text=True)
        if raw:
            debug_lines.append(f"raw-text={raw[:200]!r}")
            if "=" in raw:
                from urllib.parse import parse_qs
                parsed = parse_qs(raw)
                vals = parsed.get("key", []) or parsed.get("id", [])
                if vals:
                    key = vals[0].strip()
                    if debug:
                        logger.warning(
                            f"[vtb-debug] key from raw-text: {key[:12]}  "
                            f"({'; '.join(debug_lines)})"
                        )
                    return key
        else:
            debug_lines.append("raw-text=empty")
    except Exception as e:
        debug_lines.append(f"raw-err={type(e).__name__}")

    if debug:
        logger.warning(f"[vtb-debug] NO KEY FOUND ({'; '.join(debug_lines)})")
    return key


# ---------------------------------------------------------------------------
# 缩略图 (共用)
# ---------------------------------------------------------------------------
async def _do_thumbnail(plugin, image_id: str):
    """缩略图核心逻辑。供 /cache/thumbnail/<id> 路径参数版调用。

    协同 chat_archive:
      1. 先看本插件 SQLite 里 image_b64 (chat_archive 未装 或 老条目)
      2. 拿不到 → 看 chat_archive 装没装, 装了去它 web_cache 读
      3. 两边都没有 → 返 has_image=False
    """
    miss = _require_caption_cache(plugin)
    if miss is not None:
        return miss
    image_id = (image_id or "").strip()
    if not image_id:
        return err("缺少参数 image_id")
    e = plugin._caption_cache.get(image_id, with_b64=True)
    if e is None:
        return err("未找到该 image_id", 404)

    # 路径 1: 本插件 SQLite 存了 b64 (老数据 / chat_archive 未装)
    if e.image_b64:
        return ok(_build_thumbnail_payload(
            image_id, e.mime_type, e.image_b64, e.width, e.height, e.file_size,
        ))

    # 路径 2: 走 chat_archive
    try:
        if chat_archive_integration.is_chat_archive_installed():
            found = chat_archive_integration.find_chat_archive_image(e.image_url)
            if found:
                import base64
                data, mime, w, h = found
                b64 = base64.b64encode(data).decode("ascii")
                return ok(_build_thumbnail_payload(
                    image_id, mime, b64, w or e.width, h or e.height, len(data),
                ))
    except Exception as ex:
        logger.debug(f"[vision_text_bridge] chat_archive 读图失败: {ex}")

    # 路径 3: 都没有
    return ok(_build_thumbnail_payload(
        image_id, e.mime_type, "", e.width, e.height, e.file_size,
    ))


# ---------------------------------------------------------------------------
# handlers
# ---------------------------------------------------------------------------
async def api_stats(plugin):
    miss = _require_caption_cache(plugin)
    if miss is not None:
        return miss
    s = plugin._caption_cache.stats().to_dict()
    s["in_memory_cache_size"] = len(plugin._description_cache)
    s["memory_cache_ttl_seconds"] = int(plugin.config.get("memory_cache_ttl_seconds", 300) or 0)
    s["memory_cache_max_size"] = int(plugin.config.get("memory_cache_max_size", 500) or 0)
    s["sqlite_cache_ttl_days"] = cfg_int(plugin.config, "sqlite_cache_ttl_days", 7)
    s["sqlite_clean_interval_hours"] = cfg_int(plugin.config, "sqlite_clean_interval_hours", 1)

    # 下次后台清理预计时间 (UTC 戳)
    last = getattr(plugin, "_last_clean_at", 0.0) or 0.0
    interval_h = cfg_int(plugin.config, "sqlite_clean_interval_hours", 1)
    if interval_h > 0 and last > 0:
        s["next_clean_at"] = last + interval_h * 3600
    else:
        s["next_clean_at"] = None
    return ok(s)


async def api_stats_timeline(plugin):
    """: 按天创建的缓存条数 (默认 30 天) — webui 画柱状图用。"""
    miss = _require_caption_cache(plugin)
    if miss is not None:
        return miss
    try:
        a = read_args()
        days = int(a.get("days", 30) or 30)
        days = max(1, min(365, days))
    except Exception:
        days = 30
    buckets = plugin._caption_cache.daily_buckets(days=days)
    return ok({"days": days, "buckets": buckets})


async def api_list(plugin):
    miss = _require_caption_cache(plugin)
    if miss is not None:
        return miss
    a = read_args()
    limit = int(a.get("limit", 50) or 50)
    offset = int(a.get("offset", 0) or 0)
    search = (a.get("search", "") or "").strip()
    order_by = a.get("order_by", "created_at_desc") or "created_at_desc"
    items = plugin._caption_cache.list(
        limit=limit, offset=offset, search=search, order_by=order_by,
    )
    return ok({
        "total": plugin._caption_cache.count(search=search),
        "limit": limit, "offset": offset,
        "items": [e.to_dict() for e in items],
    })


async def api_delete(plugin):
    miss = _require_caption_cache(plugin)
    if miss is not None:
        return miss
    key = await read_key_from_request(plugin.context)
    if not key:
        return err("缺少参数 key")
    try:
        plugin._description_cache.pop(key, None)
    except Exception as e:
        logger.debug("[vision_text_bridge] _description_cache.pop 失败: %s", e)
    try:
        deleted = plugin._caption_cache.delete(key)
    except Exception as e:
        logger.exception("[vision_text_bridge] _caption_cache.delete 异常: %s", e)
        return err(f"删除失败: {e}", 500)
    return ok({"deleted": deleted, "key": key})


async def api_clear(plugin):
    miss = _require_caption_cache(plugin)
    if miss is not None:
        return miss
    n = plugin._caption_cache.clear()
    plugin._description_cache.clear()
    try:
        plugin._caption_cache.vacuum()
    except Exception as e:
        logger.warning("[vision_text_bridge] VACUUM 失败: %s", e)
    return ok({"cleared": n})


async def api_regenerate(plugin):
    miss = _require_caption_cache(plugin)
    if miss is not None:
        return miss
    key = await read_key_from_request(plugin.context)
    if not key:
        return err("缺少参数 key")
    # 【修复】 key 是 image_id (md5 hex), 不是 URL/路径. mmx 要 URL, 不接受 image_id.
    # 先查 SQLite 拿原始 url, 再 调 _describe_one(url) 走正常 mmx 路径.
    entry = plugin._caption_cache.get(key)
    if entry is None:
        return err(f"未找到 image_id={key[:16]}... 的缓存条目 (已被删除?)", 404)
    url = entry.image_url
    try:
        plugin._description_cache.pop(key, None)
        plugin._caption_cache.delete(key)
    except Exception as e:
        logger.debug("[vision_text_bridge] regenerate 清理旧缓存失败: %s", e)
    new_desc = await plugin._describe_one(url)
    return ok({"key": key, "description": new_desc, "ok": bool(new_desc)})


async def api_export(plugin):
    miss = _require_caption_cache(plugin)
    if miss is not None:
        return miss
    entries = plugin._caption_cache.list(limit=10000, offset=0)
    return ok({
        "exported_at": time.time(),
        "count": len(entries),
        "items": [e.to_dict() for e in entries],
    })


async def api_thumbnail(plugin, image_id: str = ""):
    """路径参数版 (/cache/thumbnail/<image_id>)。"""
    return await _do_thumbnail(plugin, image_id)


async def api_clean_expired(plugin):
    """: 手动触发过期清理 (返删除条数)。"""
    miss = _require_caption_cache(plugin)
    if miss is not None:
        return miss
    ttl_days = cfg_int(plugin.config, "sqlite_cache_ttl_days", 7)
    if ttl_days <= 0:
        return err("sqlite_cache_ttl_days=0，未启用过期清理", 400)
    try:
        deleted = plugin._caption_cache.clean_expired(ttl_days)
        plugin._last_clean_at = time.time()
        # 同步清内存热缓存的过期项 (LRU 全量访问开销大, 直接遍历 _m)
        mem_size_before = len(plugin._description_cache)
        expired_keys = [
            k for k, (_, exp) in getattr(plugin._description_cache, "_m", {}).items()
            if plugin._description_cache._ttl > 0 and time.time() >= exp
        ]
        for k in expired_keys:
            plugin._description_cache.pop(k)
        purged_mem = mem_size_before - len(plugin._description_cache)
        logger.info(
            "[vision_text_bridge] 手动清理过期: SQLite=%d条, 内存=%d条",
            deleted, purged_mem,
        )
        return ok({"deleted_sqlite": deleted, "purged_memory": purged_mem, "ttl_days": ttl_days})
    except Exception as e:
        return err(f"清理失败: {e}", 500)


async def api_diag(plugin):
    """.1: 诊断 endpoint — DB 路径/schema/最近 3 条。

    webui 看不到数据时调用, 验证 SQLite 里到底有没有东西。
    """
    if plugin._caption_cache is None:
        return ok({
            "cache_initialized": False,
            "hint": "SQLite 缓存未初始化——请看 AstrBot 启动日志里 [vision_text_bridge] 初始化描述缓存",
        })
    try:
        conn = sqlite3.connect(plugin._caption_cache._db_path)
        conn.row_factory = sqlite3.Row
        cols = [r[1] for r in conn.execute("PRAGMA table_info(image_captions)").fetchall()]
        total = conn.execute("SELECT COUNT(*) FROM image_captions").fetchone()[0]
        recent = []
        for row in conn.execute(
            "SELECT image_id, length(description) AS desc_len, image_b64, "
            "mime_type, file_size, width, height, created_at "
            "FROM image_captions ORDER BY created_at DESC LIMIT 3"
        ).fetchall():
            recent.append({
                "image_id": row["image_id"],
                "desc_len": row["desc_len"],
                "has_b64": bool(row["image_b64"]),
                "b64_len": len(row["image_b64"]) if row["image_b64"] else 0,
                "mime_type": row["mime_type"],
                "file_size": row["file_size"],
                "width": row["width"],
                "height": row["height"],
                "created_at": row["created_at"],
            })
        conn.close()
    except Exception as e:
        return ok({"cache_initialized": True, "error": str(e)})
    return ok({
        "cache_initialized": True,
        "db_path": plugin._caption_cache._db_path,
        "schema_columns": cols,
        "total_entries": total,
        "in_memory_cache_size": len(plugin._description_cache),
        "recent_3": recent,
    })


# ---------------------------------------------------------------------------
# 路由表
# ---------------------------------------------------------------------------
_ROUTES = [
    # path (相对 PLUGIN_NAME), handler, methods, description
    ("/cache/stats", api_stats, ["GET"], "Cache stats"),
    ("/cache/stats/timeline", api_stats_timeline, ["GET"], "按天创建量（柱状图）"),
    ("/cache/list", api_list, ["GET"], "Cache list"),
    ("/cache/delete", api_delete, ["GET", "POST"],
     "POST 主路径 (bridge.apiPost 带 body, angel_memory 同款)"),
    ("/cache/clear", api_clear, ["GET", "POST"], "POST 主路径"),
    ("/cache/regenerate", api_regenerate, ["GET", "POST"], "POST 主路径"),
    ("/cache/export", api_export, ["GET"], "Export JSON"),
    ("/cache/thumbnail/<image_id>", api_thumbnail, ["GET"],
     "缩略图：image_id 走路径参数 (GET)"),
    ("/cache/diag", api_diag, ["GET"], "诊断：DB 路径/schema/最近 3 条"),
    ("/cache/clean_expired", api_clean_expired, ["GET", "POST"], "POST 主路径"),
]


def register_all_routes(context, plugin) -> None:
    """把全部 web API 挂到 AstrBot context。

    接受 ``context`` (AstrBot plugin context) 和 ``plugin`` (plugin 实例)
    两个参数, handler 内部通过 ``plugin._caption_cache`` / ``_description_cache``
    / ``_last_clean_at`` / ``config`` / ``_describe_one`` 等访问 plugin state。
    """
    for path, handler, methods, desc in _ROUTES:
        # 把 handler 闭包绑到 plugin 上 — handler(plugin) 形式
        bound = _bind_handler(handler, plugin)
        full_path = f"/{PLUGIN_NAME}{path}"
        context.register_web_api(full_path, bound, methods, desc)
        logger.debug("[vision_text_bridge] 已注册 web API: %s %s", methods, full_path)


def _bind_handler(handler, plugin):
    """把 ``handler(plugin)`` 闭包包成 ``async def wrapper(**kwargs)`` —
    AstrBot 框架调的是无参或 path-param 形式 (e.g. /cache/thumbnail/<image_id>
    会传 image_id 进来), 透传给 handler.
    """

    async def wrapper(**kwargs):
        return await handler(plugin, **kwargs)

    # 保留 handler 的 __name__ / __doc__ (debug 友好)
    wrapper.__name__ = getattr(handler, "__name__", "handler")
    wrapper.__doc__ = getattr(handler, "__doc__", None)
    return wrapper
