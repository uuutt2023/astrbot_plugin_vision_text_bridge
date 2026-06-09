"""
vision_text_bridge.image_utils
================================

图片 URL 字段处理工具。

AstrBot 的 ProviderRequest 在三个地方可能携带图片:
- `req.image_urls`: list[str] — 顶层图片 URL
- `req.extra_user_content_parts`: list[dict | TextPart] — 用户额外 content part
- `req.contexts[i].content[j]`: list[dict] — 历史消息里嵌套的 content

这三处每个 image 字段的 shape 不一样 (dict / object / str), 本模块
提供统一 helper 做:
- 检测是不是 image_url 字段
- 从字段抽 URL
- 从 list 抽全部 URL
- 判断是不是 data:base64 内联图
- 按条件删 image_url (用于链末 hook 清 base64 残留)

不依赖 plugin 实例, 纯函数 — 方便测试 / inline 调用。
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
