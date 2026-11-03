"""Netmiko 连接与命令执行。

设计要点：
- 所有连接参数来自 ``Device``，模块内不出现任何设备地址或凭据；
- 每台设备的异常在这里被捕获并转成 ``DeviceResult``，绝不向外抛，
  保证单台失败不会影响其他设备；
- 错误信息在返回前会抹掉密码/密钥口令，日志同样不记录这些值。
"""

from __future__ import annotations

import logging
import re
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .inventory import Device

LOGGER = logging.getLogger(__name__)

ERROR_LABELS = {
    "timeout": "连接超时",
    "command_timeout": "命令执行超时",
    "auth": "认证失败",
    "key": "密钥不可用",
    "refused": "连接被拒绝",
    "unreachable": "网络不可达",
    "dns": "域名解析失败",
    "config": "连接参数错误",
    "snapshot": "快照写入失败",
    "unknown": "采集失败",
}

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_BACKSPACE_RUN_RE = re.compile(r"[^\n\x08]\x08")


class CollectorError(Exception):
    """采集前置条件不满足（例如密钥文件不存在）。"""


@dataclass(frozen=True)
class DeviceResult:
    device: Device
    ok: bool
    outputs: dict[str, str] = field(default_factory=dict)
    error_kind: str | None = None
    error: str | None = None
    attempts: int = 0
    duration: float = 0.0

    @property
    def label(self) -> str:
        if self.error_kind:
            return ERROR_LABELS.get(self.error_kind, ERROR_LABELS["unknown"])
        return "成功"


def collect_device(device: Device, commands: tuple[str, ...]) -> DeviceResult:
    """采集一台设备；任何异常都被转换成失败结果。"""

    started = time.monotonic()

    if device.key_file:
        key_path = Path(device.key_file)
        if not key_path.is_file():
            message = f"SSH 密钥文件不存在或不可读: {key_path}"
            LOGGER.error("[%s] %s", device.name, message)
            return DeviceResult(
                device=device,
                ok=False,
                error_kind="key",
                error=message,
                attempts=0,
                duration=time.monotonic() - started,
            )

    attempts = 0
    last_error: tuple[str, str] = ("unknown", "未知错误")
    total_attempts = device.retries + 1

    for attempt in range(1, total_attempts + 1):
        attempts = attempt
        try:
            LOGGER.info(
                "[%s] 连接 %s@%s:%s（%s，认证：%s）",
                device.name,
                device.username,
                device.host,
                device.port,
                device.device_type,
                device.auth_label,
            )
            outputs = _run_session(device, commands)
            duration = time.monotonic() - started
            LOGGER.info(
                "[%s] 采集成功：%d 条命令，用时 %.1f 秒",
                device.name,
                len(outputs),
                duration,
            )
            return DeviceResult(
                device=device,
                ok=True,
                outputs=outputs,
                attempts=attempt,
                duration=duration,
            )
        except Exception as exc:  # noqa: BLE001 - 单台设备失败必须被隔离
            kind, summary = _classify_error(exc, device)
            detail = _sanitize_error(exc, device)
            last_error = (kind, detail)
            LOGGER.warning(
                "[%s] 第 %d/%d 次尝试失败（%s）：%s",
                device.name,
                attempt,
                total_attempts,
                summary,
                detail,
            )
            if attempt < total_attempts:
                time.sleep(min(1.0 * attempt, 5.0))

    duration = time.monotonic() - started
    LOGGER.error("[%s] 采集失败（%s）：%s", device.name, last_error[0], last_error[1])
    return DeviceResult(
        device=device,
        ok=False,
        error_kind=last_error[0],
        error=last_error[1],
        attempts=attempts,
        duration=duration,
    )


def _run_session(device: Device, commands: tuple[str, ...]) -> dict[str, str]:
    netmiko = _require_netmiko()
    params: dict[str, Any] = {
        "device_type": device.device_type,
        "host": device.host,
        "username": device.username,
        "port": device.port,
        "timeout": device.timeout,
        "allow_agent": False,
    }
    if device.password:
        params["password"] = device.password
    if device.secret:
        params["secret"] = device.secret
    if device.key_file:
        params["use_keys"] = True
        params["key_file"] = device.key_file

    connection = None
    try:
        connection = netmiko.ConnectHandler(**params)
        if device.secret:
            connection.enable()
        LOGGER.debug("[%s] 连接建立，开始执行 %d 条命令", device.name, len(commands))

        outputs: dict[str, str] = {}
        for command in commands:
            LOGGER.debug("[%s] 执行命令：%s", device.name, command)
            raw = connection.send_command(
                command,
                read_timeout=device.timeout,
                strip_prompt=True,
                strip_command=True,
            )
            outputs[command] = _clean_output(raw)
        return outputs
    finally:
        if connection is not None:
            try:
                connection.disconnect()
            except Exception as exc:  # noqa: BLE001 - 断开失败不影响采集结果
                LOGGER.debug("[%s] 断开连接时出错：%s", device.name, exc)


def _require_netmiko() -> Any:
    try:
        import netmiko
    except ImportError as exc:
        raise CollectorError(
            "未安装 netmiko，请先执行: python -m pip install -r requirements.txt"
        ) from exc
    return netmiko


def _clean_output(raw: str) -> str:
    """统一换行、去掉终端控制序列，保留行内缩进。"""

    text = str(raw).replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\x00", "")
    text = _ANSI_RE.sub("", text)
    text = _BACKSPACE_RUN_RE.sub("", text)
    return text.strip("\n")


def _classify_error(exc: Exception, device: Device) -> tuple[str, str]:
    name = type(exc).__name__
    lowered = f"{name} {exc}".lower()

    if isinstance(exc, FileNotFoundError) and device.key_file:
        return "key", ERROR_LABELS["key"]
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "timeout", ERROR_LABELS["timeout"]
    if "pattern not detected" in lowered or "read timeout" in lowered:
        return "command_timeout", ERROR_LABELS["command_timeout"]
    if "timed-out" in lowered or "timed out" in lowered or "timeout" in name.lower():
        return "timeout", ERROR_LABELS["timeout"]
    if isinstance(exc, socket.gaierror) or "getaddrinfo failed" in lowered:
        return "dns", ERROR_LABELS["dns"]
    if "connection refused" in lowered or "errno 10061" in lowered:
        return "refused", ERROR_LABELS["refused"]
    if "no route to host" in lowered or "unreachable" in lowered or "errno 10065" in lowered:
        return "unreachable", ERROR_LABELS["unreachable"]
    if (
        "authentication" in lowered
        or "auth failed" in lowered
        or "permission denied" in lowered
        or "bad password" in lowered
        or "invalid password" in lowered
    ):
        return "auth", ERROR_LABELS["auth"]
    if "encrypted" in lowered and "key" in lowered:
        return "key", ERROR_LABELS["key"]
    if "passphrase" in lowered:
        return "key", ERROR_LABELS["key"]
    if "not a valid" in lowered or "unsupported device_type" in lowered:
        return "config", ERROR_LABELS["config"]
    return "unknown", ERROR_LABELS["unknown"]


def _sanitize_error(exc: Exception, device: Device) -> str:
    message = f"{type(exc).__name__}: {exc}"
    for secret in (device.password, device.secret):
        if secret:
            message = message.replace(secret, "***")
    message = message.replace("\r", " ").replace("\n", " ")
    message = re.sub(r"\s+", " ", message).strip()
    if len(message) > 300:
        message = message[:297] + "..."
    return message
