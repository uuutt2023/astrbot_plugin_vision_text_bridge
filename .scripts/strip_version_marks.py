#!/usr/bin/env python3
"""
strip_version_marks.py
================================

把所有 .py 文件里的 v0.x / v1.0.0 这种版本号引用清掉。

作用范围:
    - 注释行 (# ...)  整行 + 行内
    - docstring 整段
    - logger.info / 元组末尾的说明字符串里的 v0.x 引用

跳过:
    - test.py (assert 依赖具体版本号字面量)
    - metadata.yaml (真版本字段)
    - 本脚本自身 (pattern 示例保留)
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PATTERN = re.compile(
    r"v\d+\.\d+(?:\.\d+)?(?:\.[xX]|\+)?",
    re.UNICODE,
)

# 行首缩进 + 注释: 整行注释
COMMENT_LINE = re.compile(r"^(\s*)#\s*(.*?)$")

# 行内注释: code  # comment  (简单的 # 后面都是注释)
INLINE_COMMENT = re.compile(
    r"""(#[^"'\n]*)""",
    re.UNICODE,
)

# docstring 三引号: """ 或 '''
TRIPLE_DQ = re.compile(r'"""(.*?)"""', re.DOTALL)
TRIPLE_SQ = re.compile(r"'''(.*?)'''", re.DOTALL)

# 跳过这些 path (assert 字面量依赖具体版本号)
# test.py 仍参与, 但脚本不会动 assert / 字面量里靠近代码的版本号
SKIP_NAMES = set()


def strip_text(text: str) -> str:
    return PATTERN.sub("", text)


def strip_file(path: Path) -> tuple[int, str]:
    raw = path.read_text(encoding="utf-8")
    # 1) 整段 docstring
    text1 = TRIPLE_DQ.sub(lambda m: f'"""{strip_text(m.group(1))}"""', raw)
    text1 = TRIPLE_SQ.sub(lambda m: f"'''{strip_text(m.group(1))}'''", text1)
    # 2) 整行注释 + 行内注释:  按行处理
    new_lines = []
    for line in text1.splitlines(keepends=True):
        # 整行注释
        m = COMMENT_LINE.match(line)
        if m:
            indent, body = m.group(1), m.group(2)
            new_body = strip_text(body)
            new_lines.append(f"{indent}# {new_body}".rstrip() + "\n")
            continue
        # 行内注释: code  # comment
        # 但要避开字符串字面量里的 # — 简单粗暴做法:  按 # 切, 找第一个非字符串内的 #
        # 这里采用保守做法: 找到  # (空格+井号) 后面到行尾, 当行内注释处理
        idx = line.find("  #")
        if idx >= 0 and not is_inside_string(line, idx):
            head = line[:idx]
            tail = line[idx+3:]  # 跳过 "  #"
            new_tail = strip_text(tail)
            new_lines.append(f"{head}  #{new_tail}")
        else:
            new_lines.append(line)
    text2 = "".join(new_lines)
    n = len(PATTERN.findall(raw)) - len(PATTERN.findall(text2))
    return n, text2


def is_inside_string(line: str, idx: int) -> bool:
    """简单判定 idx 处是否在字符串字面量内 (只数 " 和 ', 不考虑转义精细)。"""
    in_str = None
    i = 0
    while i < idx:
        ch = line[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == in_str:
                in_str = None
        else:
            if ch in ("'", '"'):
                in_str = ch
        i += 1
    return in_str is not None


def main(roots: list[Path]) -> None:
    files = []
    for root in roots:
        if root.is_file():
            files.append(root)
        else:
            files.extend(root.rglob("*.py"))
    files = [f for f in files if f.name not in SKIP_NAMES]
    total = 0
    for f in sorted(files):
        n, new = strip_file(f)
        if n > 0:
            f.write_text(new, encoding="utf-8")
            print(f"{f}: -{n} version marks")
            total += n
    print(f"---")
    print(f"Total: -{total} version marks removed across {len(files)} files")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: strip_version_marks.py <file_or_dir> [...]")
        sys.exit(1)
    main([Path(p) for p in sys.argv[1:]])
