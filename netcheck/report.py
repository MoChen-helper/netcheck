"""生成 Markdown 巡检报告。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .diff import CHANGED, FIRST, NEW_COMMAND, UNCHANGED, CommandDiff
from .inventory import Device
from .snapshot import Snapshot, write_text_atomic


@dataclass(frozen=True)
class DeviceReport:
    device: Device
    ok: bool
    status_label: str
    command_count: int
    attempts: int
    error: str | None
    snapshot_path: Path | None
    previous: Snapshot | None
    diffs: tuple[CommandDiff, ...]


def render_report(reports: list[DeviceReport], run_at: datetime, duration: float) -> str:
    total = len(reports)
    ok_count = sum(1 for report in reports if report.ok)

    lines: list[str] = [
        f"# 巡检报告 {run_at.astimezone().strftime('%Y-%m-%d %H:%M')}",
        "",
        f"- 生成时间：{run_at.astimezone().isoformat(sep=' ', timespec='seconds')}",
        f"- 设备总数：{total}（成功 {ok_count} / 失败 {total - ok_count}）",
        f"- 执行耗时：{duration:.1f} 秒",
        "",
        "## 总览",
        "",
        "| 设备 | 状态 | 命令数 | 变更 |",
        "|---|---|---|---|",
    ]

    for report in reports:
        lines.append(
            f"| {_cell(report.device.name)} | {_cell(_status_cell(report))} | "
            f"{report.command_count if report.ok else '-'} | {_cell(_change_cell(report))} |"
        )

    for report in reports:
        lines.append("")
        lines.extend(_section_lines(report))

    lines.extend(
        [
            "",
            "---",
            "",
            "*本报告由 netcheck 自动生成；差异基于抓取到的文本快照，请人工复核后再操作设备。*",
            "",
        ]
    )
    return "\n".join(lines)


def write_report(path: Path, text: str) -> None:
    write_text_atomic(path, text)


def _section_lines(report: DeviceReport) -> list[str]:
    device = report.device
    lines = [f"## {device.name}", ""]

    if not report.ok:
        lines.extend(
            [
                f"状态：❌ {report.status_label}",
                "",
                f"原因：{_inline(report.error)}",
                "",
                f"尝试次数：{report.attempts}",
                "",
            ]
        )
        if report.snapshot_path is not None:
            lines.extend([f"快照：`{_display_path(report.snapshot_path)}`", ""])
        return lines

    if report.previous is None:
        baseline = "无（首次采集）"
    else:
        baseline = (
            f"{report.previous.captured_display}"
            f"（`{_display_path(report.previous.path)}`）"
        )

    lines.extend(
        [
            f"- 地址：`{device.host}:{device.port}`",
            f"- 设备类型：`{device.device_type}`",
            f"- 认证方式：{device.auth_label}",
            f"- 快照：`{_display_path(report.snapshot_path)}`",
            f"- 对比基准：{baseline}",
            "",
        ]
    )

    for diff in report.diffs:
        lines.extend([f"### {diff.command}", ""])
        if diff.kind == FIRST:
            lines.extend(["首次采集，无历史可比。", ""])
        elif diff.kind == NEW_COMMAND:
            lines.extend(["该命令首次出现在此设备快照中，无历史可比。", ""])
        elif diff.kind == UNCHANGED:
            lines.extend(["与上一份快照一致，无变化。", ""])
        elif diff.kind == CHANGED:
            diff_text = "\n".join(diff.lines)
            fence = _fence(diff_text)
            lines.extend(
                [
                    f"与上一份快照（{report.previous.captured_display}）对比："
                    f"**+{diff.added} / -{diff.removed}**",
                    "",
                    f"{fence}diff",
                    diff_text,
                    fence,
                    "",
                ]
            )

    return lines


def _status_cell(report: DeviceReport) -> str:
    return "✅" if report.ok else f"❌ {report.status_label}"


def _change_cell(report: DeviceReport) -> str:
    if not report.ok:
        return "-"

    added = sum(diff.added for diff in report.diffs)
    removed = sum(diff.removed for diff in report.diffs)
    if added or removed:
        return f"+{added} / -{removed}"
    if any(diff.kind == NEW_COMMAND for diff in report.diffs):
        return "新增命令"
    if report.diffs and all(diff.kind == FIRST for diff in report.diffs):
        return "首次采集"
    return "无变化"


def _cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _inline(value: str | None) -> str:
    return (value or "").replace("\r", " ").replace("\n", " ").strip()


def _fence(text: str) -> str:
    longest = max((len(run) for run in re.findall(r"`+", text)), default=2)
    return "`" * max(3, longest + 1)


def _display_path(path: Path | None) -> str:
    if path is None:
        return ""
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except (ValueError, OSError):
        return path.as_posix()
