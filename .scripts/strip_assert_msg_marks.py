#!/usr/bin/env python3
"""
strip_assert_msg_marks.py
================================

清掉 ``assert ..., "v0.X.Y ..msg"`` 这种 assert 错误消息里的版本号。

assert 字面量 (如 ``assert "v0.8.36" in m_webapi``) 保留 ——
那是测试行为本身, 清了就 break。

只清 assert 的 第二个字符串参数 (错误消息) 里的版本号引用。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PATTERN = re.compile(r"v\d+\.\d+(?:\.\d+)?(?:\.[xX]|\+)?", re.UNICODE)

# assert X, "v0.x.y msg"  或  assert X, 'v0.x.y msg'
ASSERT_MSG = re.compile(
    r"""(,\s*)(['"])(.*?)\2(\s*$)""",
    re.MULTILINE,
)


def strip_in_text(text: str) -> str:
    return PATTERN.sub("", text)


def strip_file(path: Path) -> tuple[int, str]:
    raw = path.read_text(encoding="utf-8")
    n_before = len(PATTERN.findall(raw))

    new_lines = []
    for line in raw.splitlines(keepends=True):
        stripped = line.rstrip("\n")
        if not stripped.lstrip().startswith("assert "):
            new_lines.append(line)
            continue
        # 找 ", " 或 ', ' (assert msg 起始)
        idx = None
        for sep in (', "', ", '", ",  \"", ",  '"):
            i = stripped.find(sep)
            if i >= 0:
                idx = i + 1  # 指向引号
                break
        if idx is None:
            new_lines.append(line)
            continue
        # 找匹配的收尾引号 (简化: 末尾")
        tail = stripped[idx+1:].rstrip()
        if not tail or tail[0] not in ('"', "'"):
            new_lines.append(line)
            continue
        # 清版本号
        new_tail = strip_in_text(tail)
        # 收尾的引号和括号还原
        # 简化: 整行只处理一次
        new_line = stripped[:idx+1] + new_tail + stripped[idx+1+len(tail):]
        # 加回换行
        if line.endswith("\n"):
            new_line += "\n"
        new_lines.append(new_line)
    text = "".join(new_lines)
    n_after = len(PATTERN.findall(text))
    return n_before - n_after, text


def main(roots: list[Path]) -> None:
    files = []
    for root in roots:
        if root.is_file():
            files.append(root)
        else:
            files.extend(root.rglob("*.py"))
    total = 0
    for f in sorted(files):
        n, new = strip_file(f)
        if n > 0:
            f.write_text(new, encoding="utf-8")
            print(f"{f}: -{n} version marks in assert msg")
            total += n
    print(f"---")
    print(f"Total: -{total} assert-msg version marks removed across {len(files)} files")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: strip_assert_msg_marks.py <file_or_dir> [...]")
        sys.exit(1)
    main([Path(p) for p in sys.argv[1:]])
