"""文本快照差异计算。"""

from __future__ import annotations

import difflib
from dataclasses import dataclass

from .snapshot import Snapshot

# 变更类型
FIRST = "first"          # 首次采集
NEW_COMMAND = "new"      # 历史快照里没有这条命令
CHANGED = "changed"      # 有新增/删除行
UNCHANGED = "unchanged"  # 完全一致


@dataclass(frozen=True)
class CommandDiff:
    command: str
    kind: str
    added: int = 0
    removed: int = 0
    lines: tuple[str, ...] = ()


def diff_text(old: str, new: str, context: int = 2) -> tuple[tuple[str, ...], int, int]:
    """返回不含 ---/+++ 文件头的 unified diff 行、新增行数、删除行数。"""

    old_lines = old.splitlines()
    new_lines = new.splitlines()
    diff = list(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile="上一份快照",
            tofile="本次采集",
            n=context,
            lineterm="",
        )
    )
    if diff and diff[0].startswith("---"):
        diff = diff[2:]

    added = sum(1 for line in diff if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff if line.startswith("-") and not line.startswith("---"))
    return tuple(diff), added, removed


def build_diffs(
    previous: Snapshot | None,
    outputs: dict[str, str],
    commands: tuple[str, ...],
) -> tuple[CommandDiff, ...]:
    """按当前命令顺序，生成每条命令相对上一份快照的差异。"""

    results: list[CommandDiff] = []
    for command in commands:
        current = outputs.get(command, "")
        if previous is None:
            results.append(CommandDiff(command=command, kind=FIRST))
            continue
        if command not in previous.commands:
            results.append(CommandDiff(command=command, kind=NEW_COMMAND))
            continue

        lines, added, removed = diff_text(previous.commands[command], current)
        if added == 0 and removed == 0:
            results.append(CommandDiff(command=command, kind=UNCHANGED))
        else:
            results.append(
                CommandDiff(
                    command=command,
                    kind=CHANGED,
                    added=added,
                    removed=removed,
                    lines=lines,
                )
            )
    return tuple(results)
