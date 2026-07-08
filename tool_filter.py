"""tool_filter.py - LLM tool_call 过滤 (禁用某些工具防止幻觉)。"""

from __future__ import annotations

import fnmatch
from typing import Any


# ---------------------------------------------------------------------------
# 名称匹配
# ---------------------------------------------------------------------------

def match_tool_name(name: str, patterns: list[str]) -> bool:
    """判断 name 是否匹配 patterns 任一模式。

    支持:
    - 精确匹配: ``"web_search"``
    - 前缀通配: ``"archive_*"`` 匹配 ``archive_2023`` / ``archive_2024``
    - 后缀通配: ``"*_tool"`` 匹配 ``web_tool`` / ``image_tool``
    - 中间通配: ``"web_*_v2"`` 走 ``fnmatch.fnmatchcase``
    """
    if not name or not patterns:
        return False
    for p in patterns:
        if not p:
            continue
        if p == name:
            return True
        if p.endswith("*") and name.startswith(p[:-1]):
            return True
        if p.startswith("*") and name.endswith(p[1:]):
            return True
        if "*" in p and fnmatch.fnmatchcase(name, p):
            return True
    return False


# ---------------------------------------------------------------------------
# 容器适配
# ---------------------------------------------------------------------------

def _iter_tool_items(tool_container: Any) -> list[tuple[str, Any]]:
    """从各种 AstrBot tool container 接口里抽 (name, tool) 列表。

    兼容:
    - ``.tools`` list (chat_plus ToolSet 风格)
    - ``.func_list`` list (FunctionToolManager 风格)

    dict / object 都支持, 取 ``.name`` 属性 或 dict['name'] 键。
    """
    items: list[tuple[str, Any]] = []
    tools = getattr(tool_container, "tools", None)
    if isinstance(tools, list):
        for t in tools:
            n = getattr(t, "name", None) or (t.get("name") if isinstance(t, dict) else None)
            if n:
                items.append((n, t))
    flist = getattr(tool_container, "func_list", None)
    if isinstance(flist, list):
        for t in flist:
            n = getattr(t, "name", None)
            if n:
                items.append((n, t))
    return items


def _remove_from_container(tool_container: Any, name: str) -> bool:
    """跨容器接口尝试删 name 工具, 任一成功返 True。"""
    did_remove = False

    # 1. 优先用 remove_func (有的容器提供)
    if hasattr(tool_container, "remove_func"):
        try:
            tool_container.remove_func(name)
            did_remove = True
        except Exception:
            pass

    # 2. 直接改 .tools list
    tools = getattr(tool_container, "tools", None)
    if isinstance(tools, list):
        before = len(tools)
        tools[:] = [
            t for t in tools
            if (getattr(t, "name", None)
                or (t.get("name") if isinstance(t, dict) else None)) != name
        ]
        if len(tools) < before:
            did_remove = True

    # 3. 直接改 .func_list list
    flist = getattr(tool_container, "func_list", None)
    if isinstance(flist, list):
        before = len(flist)
        flist[:] = [t for t in flist if getattr(t, "name", None) != name]
        if len(flist) < before:
            did_remove = True

    return did_remove


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def filter_disabled_tools(tool_container: Any, mode: str, names: list[str]) -> int:
    """按 mode 删/保留 names 里的工具, 返实际删除条数。

    mode:
        - ``"off"``  — 不动
        - ``"blacklist"`` — 删 names 里的工具
        - ``"whitelist"`` — 只保留 names 里的工具 (其它删)
    """
    if tool_container is None or mode == "off" or not names:
        return 0

    items = _iter_tool_items(tool_container)
    if not items:
        return 0

    if mode == "blacklist":
        keep = lambda n: not match_tool_name(n, names)  # noqa: E731
    elif mode == "whitelist":
        keep = lambda n: match_tool_name(n, names)  # noqa: E731
    else:
        return 0

    removed = 0
    for name, _t in items:
        if keep(name):
            continue
        if _remove_from_container(tool_container, name):
            removed += 1
    return removed
