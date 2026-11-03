"""快照读写：snapshots/<设备名>/<YYYYMMDD_HHMMSS>.txt。

文件是可读的纯文本，结构如下::

    # netcheck snapshot
    # device: core-sw-01
    # device_type: huawei
    # captured_at: 2026-11-03T21:50:09+08:00
    # command_count: 2

    ===== NETCHECK CMD: display version =====
    ...

    ===== NETCHECK CMD: display current-configuration =====
    ...

相邻两次运行若落在同一秒，文件名追加 ``_01``、``_02`` 以避免覆盖历史。
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .inventory import Device

LOGGER = logging.getLogger(__name__)

MARKER_PREFIX = "===== NETCHECK CMD: "
MARKER_SUFFIX = " ====="
_MARKER_RE = re.compile(r"^===== NETCHECK CMD: (.+) =====$")
_SNAPSHOT_NAME_RE = re.compile(r"^\d{8}_\d{6}(?:_\d{2})?\.txt$")


@dataclass(frozen=True)
class Snapshot:
    path: Path
    device: str
    device_type: str
    captured_at: str
    commands: dict[str, str]

    @property
    def captured_display(self) -> str:
        try:
            return datetime.fromisoformat(self.captured_at).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return self.captured_at


def write_text_atomic(path: Path, text: str) -> None:
    """先写同目录临时文件，再替换目标文件，避免留下半个文件。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    finally:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass


def unique_path(directory: Path, stem: str, suffix: str) -> Path:
    """返回不冲突的路径；同一秒重复运行时追加 _01、_02 ……"""

    candidate = directory / f"{stem}{suffix}"
    if not candidate.exists():
        return candidate
    for index in range(1, 100):
        candidate = directory / f"{stem}_{index:02d}{suffix}"
        if not candidate.exists():
            return candidate
    raise OSError(f"无法为 {stem}{suffix} 生成不冲突的文件名")


def save_snapshot(
    root: Path,
    device: Device,
    outputs: dict[str, str],
    captured_at: datetime,
) -> Path:
    """保存一次采集结果，返回快照路径。"""

    directory = root / device.name
    directory.mkdir(parents=True, exist_ok=True)

    timestamp = captured_at.astimezone().strftime("%Y%m%d_%H%M%S")
    path = unique_path(directory, timestamp, ".txt")

    lines = [
        "# netcheck snapshot",
        f"# device: {device.name}",
        f"# device_type: {device.device_type}",
        f"# captured_at: {captured_at.astimezone().isoformat(timespec='seconds')}",
        f"# command_count: {len(outputs)}",
        "",
    ]
    for command, output in outputs.items():
        lines.append(f"{MARKER_PREFIX}{command}{MARKER_SUFFIX}")
        lines.append(output.rstrip("\n"))
        lines.append("")

    write_text_atomic(path, "\n".join(lines))
    return path


def load_snapshot(path: Path) -> Snapshot:
    """读取一份快照；格式不合法时抛出 ValueError。"""

    text = path.read_text(encoding="utf-8")
    meta: dict[str, str] = {}
    commands: dict[str, str] = {}
    current: str | None = None
    buffer: list[str] = []

    for line in text.splitlines():
        marker = _MARKER_RE.match(line)
        if marker:
            if current is not None:
                commands[current] = "\n".join(buffer).strip("\n")
            current = marker.group(1)
            buffer = []
            continue
        if current is None:
            if line.startswith("# ") and ": " in line:
                key, value = line[2:].split(": ", 1)
                meta[key.strip()] = value.strip()
            continue
        buffer.append(line)

    if current is not None:
        commands[current] = "\n".join(buffer).strip("\n")

    if not commands:
        raise ValueError(f"{path} 中没有找到任何命令输出")

    return Snapshot(
        path=path,
        device=meta.get("device", path.parent.name),
        device_type=meta.get("device_type", ""),
        captured_at=meta.get("captured_at", ""),
        commands=commands,
    )


def find_previous(root: Path, device_name: str, current_path: Path) -> Snapshot | None:
    """在设备快照目录中找当前文件之前最近的一份可读快照。"""

    directory = root / device_name
    if not directory.is_dir():
        return None

    try:
        files = [
            path
            for path in directory.iterdir()
            if path.is_file()
            and _SNAPSHOT_NAME_RE.match(path.name)
            and path.resolve() != current_path.resolve()
        ]
    except OSError as exc:
        LOGGER.warning("[%s] 读取快照目录失败：%s", device_name, exc)
        return None

    if not files:
        return None

    def sort_key(path: Path) -> tuple[int, str]:
        try:
            return (path.stat().st_mtime_ns, path.name)
        except OSError:
            return (0, path.name)

    for path in sorted(files, key=sort_key, reverse=True):
        try:
            return load_snapshot(path)
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            LOGGER.warning("[%s] 跳过无法读取的历史快照 %s：%s", device_name, path.name, exc)
    return None


def list_snapshots(root: Path, device_name: str) -> list[Path]:
    """列出某台设备的所有快照，按时间从旧到新排序。"""

    directory = root / device_name
    if not directory.is_dir():
        return []
    paths = [
        path
        for path in directory.iterdir()
        if path.is_file() and _SNAPSHOT_NAME_RE.match(path.name)
    ]
    return sorted(paths, key=lambda path: (path.stat().st_mtime_ns, path.name))
