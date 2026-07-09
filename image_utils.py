"""image_utils.py - 通用图片 URL helper。

API: _is_cacheable_url / extract_image_url / collect_image_urls_from_components / is_bot_avatar_url
作者: uuutt
"""

from __future__ import annotations

from typing import Any, Iterable


# ---------------------------------------------------------------------------
# 检测 / 提取
# ---------------------------------------------------------------------------

def is_image_url_part(part: Any) -> bool:
    """判断一个 content part 是不是 ``type=='image_url'``。

    同时兼容 dict 形式 (Pydantic 序列化后) 和 object 形式
    (AstrBot 内部的 TextPart / MultimodalContent)。
    """
    if isinstance(part, dict):
        return part.get("type") == "image_url"
    return getattr(part, "type", None) == "image_url"


def extract_url_from_item(item: Any) -> str:
    """从 image_url 类型的 part / dict 取 URL。

    ``item.image_url`` 可能是:
    - 字符串 (Pydantic 简化)
    - dict (含 ``url`` 键)
    - object (含 ``url`` 属性)
    """
    if isinstance(item, dict):
        iu = item.get("image_url")
        if isinstance(iu, str):
            return iu
        if isinstance(iu, dict):
            return iu.get("url", "") or ""
        return ""
    iu = getattr(item, "image_url", None)
    if iu is None:
        return ""
    if isinstance(iu, str):
        return iu
    return getattr(iu, "url", "") or ""


def extract_urls_from_parts(parts: Iterable[Any]) -> list[str]:
    """从 parts list 抽全部非空 URL。"""
    urls: list[str] = []
    for p in parts or []:
        u = extract_url_from_item(p)
        if u:
            urls.append(u)
    return urls


def extract_urls_from_context_list(content_list: Any) -> list[str]:
    """从 ``req.contexts[i].content`` list 抽 image_url 字段的 URL。"""
    urls: list[str] = []
    if not isinstance(content_list, list):
        return urls
    for item in content_list:
        if isinstance(item, dict) and item.get("type") == "image_url":
            u = extract_url_from_item(item)
            if u:
                urls.append(u)
    return urls


# ---------------------------------------------------------------------------
# 类型判定
# ---------------------------------------------------------------------------

def is_data_url(url: str) -> bool:
    """判断 URL 是不是 ``data:image/...;base64,`` 形式 (内联图)。

    只看前 64 字符就够 — data URL header 都很短。
    """
    return bool(url) and url.startswith("data:image/") and ";base64," in url[:64]


# ---------------------------------------------------------------------------
# 链末 hook 用: 按条件删 image_url 字段
# ---------------------------------------------------------------------------

def strip_image_urls(req: Any, only_data_url: bool) -> int:
    """从 ``req`` 三处删 image_url 组件, 返回删除条数。

    ``only_data_url=True`` 只删 ``data:base64`` 内联图 (AstrBot 框架
    偶尔把整张图 base64 塞进 image_urls, 太大撑爆上下文, 链末清掉)。
    ``False`` 全删 (本插件主 hook 已经把真 URL 翻译成 caption,
    残留的 image_url 字段需要清空, 否则会二次发图)。
    """
    removed = 0

    # 1. req.image_urls: list[str]
    if getattr(req, "image_urls", None):
        if only_data_url:
            kept = [u for u in req.image_urls if not is_data_url(u)]
            removed += len(req.image_urls) - len(kept)
        else:
            removed += len(req.image_urls)
            kept = []
        req.image_urls = kept

    # 2. req.extra_user_content_parts
    ep = getattr(req, "extra_user_content_parts", None)
    if ep:
        kept = []
        for p in ep:
            if is_image_url_part(p):
                u = extract_url_from_item(p)
                if only_data_url and not is_data_url(u):
                    kept.append(p)
                    continue
                removed += 1
                continue
            kept.append(p)
        ep[:] = kept

    # 3. req.contexts[i].content[j]
    ctxs = getattr(req, "contexts", None) or []
    for c in ctxs:
        if not isinstance(c, dict):
            continue
        content = c.get("content")
        if not isinstance(content, list):
            continue
        kept = []
        for x in content:
            if isinstance(x, dict) and x.get("type") == "image_url":
                u = extract_url_from_item(x)
                if only_data_url and not is_data_url(u):
                    kept.append(x)
                    continue
                removed += 1
                continue
            kept.append(x)
        content[:] = kept

    return removed


# ---------------------------------------------------------------------------
# 从 message chain 里取图 (event.message_obj.message)
# ---------------------------------------------------------------------------

_NESTED_TYPES = frozenset((
    "reply", "Reply", "reference", "Reference",
    "forward", "Forward", "json", "Json", "node", "Node",
))
_NESTED_ATTRS = ("message", "messages", "content", "data", "nodes", "_message", "_data")


async def _extract_image_url_from_component(comp) -> str | None:
    """从一个 component 取图片本地路径 (调 convert_to_file_path)。失败返 None。"""
    if not callable(getattr(comp, "convert_to_file_path", None)):
        return None
    try:
        return await comp.convert_to_file_path()
    except Exception:
        return None


async def collect_image_urls_from_components(components, dedupe: list[str] | None = None) -> int:
    """递归从 message chain (顶层 + 嵌套 reply/Reference/forward/node) 拿图片。

    参数:
        components: message chain (list of components)
        dedupe: 已有的 URL 列表, 收集时会跳过 (用于 "event.message_obj
                里的图不要和 req.image_urls 重复")
    返回: 新增的图片数。
    """
    added = 0
    for comp in components:
        ctype = getattr(comp, "type", None)
        if ctype in _NESTED_TYPES:
            for attr in _NESTED_ATTRS:
                inner = getattr(comp, attr, None)
                if isinstance(inner, list):
                    added += await collect_image_urls_from_components(inner, dedupe)
                    break
            continue
        if ctype in ("image", "Image"):
            fp = await _extract_image_url_from_component(comp)
            if fp and (dedupe is None or fp not in dedupe):
                dedupe.append(fp)
                added += 1
    return added
