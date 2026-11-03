"""CLI 入口：python -m netcheck --inventory devices.yaml --commands commands.yaml"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from . import __version__
from .collector import ERROR_LABELS, DeviceResult, collect_device
from .diff import build_diffs
from .inventory import (
    ConfigError,
    Inventory,
    load_commands,
    load_inventory,
    plan_commands,
)
from .report import DeviceReport, render_report, write_report
from .snapshot import find_previous, save_snapshot, unique_path

LOGGER = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    try:
        return _run(argv)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("已中断。", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI 不向外抛未捕获异常
        if LOGGER.isEnabledFor(logging.DEBUG):
            LOGGER.exception("未预期的错误")
        print(f"错误：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


def _run(argv: list[str] | None) -> int:
    args = _parse_args(argv)
    _configure_logging(args.verbose)

    inventory = load_inventory(args.inventory)
    templates = load_commands(args.commands)
    plans = plan_commands(inventory, templates)

    workers = args.workers if args.workers is not None else inventory.workers
    if not 1 <= workers <= 64:
        raise ConfigError(f"--workers 必须在 1..64 之间，当前为 {workers}")

    if args.dry_run:
        _print_dry_run(inventory, plans, workers)
        return 0

    snapshot_root = Path(args.snapshot_dir)
    report_dir = Path(args.report_dir)
    try:
        snapshot_root.mkdir(parents=True, exist_ok=True)
        report_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"错误：无法创建输出目录：{exc}", file=sys.stderr)
        return 1

    run_at = datetime.now().astimezone()
    started = time.monotonic()
    LOGGER.info(
        "开始巡检 %d 台设备（并发 %d），共 %d 条命令",
        len(inventory.devices),
        workers,
        sum(len(plans[device.name]) for device in inventory.devices),
    )

    results = _collect_all(inventory, plans, workers)
    reports = _build_reports(inventory, plans, results, snapshot_root, run_at)
    duration = time.monotonic() - started

    report_path = unique_path(
        report_dir, run_at.strftime("%Y%m%d_%H%M%S"), ".md"
    )
    try:
        write_report(report_path, render_report(reports, run_at, duration))
    except OSError as exc:
        print(f"错误：报告写入失败：{exc}", file=sys.stderr)
        return 1

    failed = [report for report in reports if not report.ok]
    if failed:
        LOGGER.warning("有 %d 台设备采集失败，其余设备已正常出报告", len(failed))

    print()
    print(
        f"巡检完成：成功 {len(reports) - len(failed)} 台，失败 {len(failed)} 台，"
        f"用时 {duration:.1f} 秒"
    )
    for report in failed:
        print(f"  ❌ {report.device.name}（{report.status_label}）")
    print(f"快照目录：{snapshot_root.resolve()}")
    print(f"报告文件：{report_path.resolve()}")

    if failed and args.fail_on_error:
        return 1
    return 0


def _collect_all(
    inventory: Inventory,
    plans: dict[str, tuple[str, ...]],
    workers: int,
) -> dict[str, DeviceResult]:
    results: dict[str, DeviceResult] = {}
    if workers == 1:
        for device in inventory.devices:
            results[device.name] = collect_device(device, plans[device.name])
        return results

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="netcheck") as pool:
        futures = {
            pool.submit(collect_device, device, plans[device.name]): device
            for device in inventory.devices
        }
        for future in as_completed(futures):
            device = futures[future]
            try:
                results[device.name] = future.result()
            except Exception as exc:  # noqa: BLE001 - 线程异常也不能拖垮整轮巡检
                LOGGER.exception("[%s] 工作线程异常", device.name)
                results[device.name] = DeviceResult(
                    device=device,
                    ok=False,
                    error_kind="unknown",
                    error=f"内部错误：{type(exc).__name__}: {exc}",
                    attempts=0,
                )
    return results


def _build_reports(
    inventory: Inventory,
    plans: dict[str, tuple[str, ...]],
    results: dict[str, DeviceResult],
    snapshot_root: Path,
    run_at: datetime,
) -> list[DeviceReport]:
    reports: list[DeviceReport] = []

    for device in inventory.devices:
        result = results.get(device.name)
        if result is None:
            result = DeviceResult(
                device=device,
                ok=False,
                error_kind="unknown",
                error="没有拿到采集结果",
                attempts=0,
            )

        if not result.ok:
            reports.append(
                DeviceReport(
                    device=device,
                    ok=False,
                    status_label=result.label,
                    command_count=0,
                    attempts=result.attempts,
                    error=result.error,
                    snapshot_path=None,
                    previous=None,
                    diffs=(),
                )
            )
            continue

        try:
            snapshot_path = save_snapshot(
                snapshot_root, device, result.outputs, run_at
            )
        except OSError as exc:
            message = f"{type(exc).__name__}: {exc}"
            LOGGER.error("[%s] 快照写入失败：%s", device.name, message)
            reports.append(
                DeviceReport(
                    device=device,
                    ok=False,
                    status_label=ERROR_LABELS["snapshot"],
                    command_count=0,
                    attempts=result.attempts,
                    error=message,
                    snapshot_path=None,
                    previous=None,
                    diffs=(),
                )
            )
            continue

        previous = find_previous(snapshot_root, device.name, snapshot_path)
        diffs = build_diffs(previous, result.outputs, plans[device.name])
        reports.append(
            DeviceReport(
                device=device,
                ok=True,
                status_label="成功",
                command_count=len(result.outputs),
                attempts=result.attempts,
                error=None,
                snapshot_path=snapshot_path,
                previous=previous,
                diffs=diffs,
            )
        )

    return reports


def _print_dry_run(
    inventory: Inventory,
    plans: dict[str, tuple[str, ...]],
    workers: int,
) -> None:
    total_commands = sum(len(plans[device.name]) for device in inventory.devices)
    print("[dry-run] 只打印执行计划，不建立 SSH 连接，不写入快照或报告。")
    print(f"设备：{len(inventory.devices)} 台，命令：{total_commands} 条，并发：{workers}")
    for device in inventory.devices:
        print()
        print(
            f"- {device.name}（{device.device_type}） "
            f"{device.username}@{device.host}:{device.port}  "
            f"认证：{device.auth_label}  超时：{device.timeout:g}s  重试：{device.retries}"
        )
        if device.description:
            print(f"  说明：{device.description}")
        for index, command in enumerate(plans[device.name], start=1):
            print(f"  {index}. {command}")
        if device.key_file and not Path(device.key_file).is_file():
            print("  ! 密钥文件不存在，真实运行时该设备会失败")
    print()
    print("配置校验通过。")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="netcheck",
        description="网络设备批量巡检与配置变更审计工具（只读，基于 Netmiko）",
    )
    parser.add_argument(
        "--inventory",
        default="devices.yaml",
        help="设备清单 YAML，默认 devices.yaml",
    )
    parser.add_argument(
        "--commands",
        default="commands.yaml",
        help="命令模板 YAML，默认 commands.yaml",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="并发 worker 数，默认取 devices.yaml 的 workers（缺省 4）",
    )
    parser.add_argument(
        "--snapshot-dir",
        default="snapshots",
        help="快照根目录，默认 snapshots",
    )
    parser.add_argument(
        "--report-dir",
        default="reports",
        help="报告目录，默认 reports",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印将执行的命令，不连接设备、不写文件",
    )
    parser.add_argument(
        "--fail-on-error",
        action="store_true",
        help="有设备失败时以退出码 1 结束（默认仍返回 0）",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="输出调试日志"
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    return parser.parse_args(argv)


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )
    logging.getLogger("paramiko").setLevel(logging.WARNING)
    logging.getLogger("netmiko").setLevel(logging.DEBUG if verbose else logging.WARNING)


if __name__ == "__main__":
    raise SystemExit(main())
