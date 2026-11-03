"""读取、校验 devices.yaml 与 commands.yaml。

所有设备信息与凭据只从这里进入程序，其他模块不保存明文密码。
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_DEVICE_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]*$")

_ROOT_KEYS = {"defaults", "devices", "workers"}
_DEFAULTS_KEYS = {"port", "timeout", "retries", "device_type"}
_DEVICE_KEYS = {
    "name",
    "host",
    "username",
    "password",
    "password_env",
    "key_file",
    "secret",
    "device_type",
    "port",
    "timeout",
    "retries",
    "commands",
    "extra_commands",
    "description",
}


class ConfigError(Exception):
    """配置文件缺失、格式错误或内容不合法。"""


@dataclass(frozen=True)
class Device:
    """一台待巡检设备。

    password / secret 使用 ``repr=False``，避免意外出现在日志或异常回显里。
    """

    name: str
    host: str
    username: str
    device_type: str = "cisco_ios"
    port: int = 22
    timeout: float = 15.0
    retries: int = 0
    password: str | None = field(default=None, repr=False)
    key_file: str | None = None
    secret: str | None = field(default=None, repr=False)
    commands: tuple[str, ...] | None = None
    extra_commands: tuple[str, ...] = ()
    description: str = ""

    @property
    def auth_label(self) -> str:
        if self.key_file and self.password:
            return "密码 + SSH 密钥"
        if self.key_file:
            return "SSH 密钥"
        return "密码"


@dataclass(frozen=True)
class Inventory:
    devices: tuple[Device, ...]
    workers: int
    path: Path


def load_inventory(path: str | Path) -> Inventory:
    """读取并校验设备清单。"""

    source = Path(path)
    if not source.is_file():
        raise ConfigError(f"设备清单不存在: {source}")

    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{source} 不是 UTF-8 文本: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{source} 不是合法的 YAML: {_yaml_error(exc)}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"{source} 顶层必须是键值映射（包含 devices 列表）")
    _reject_unknown(raw, _ROOT_KEYS, "顶层")

    defaults_raw = raw.get("defaults") or {}
    if not isinstance(defaults_raw, dict):
        raise ConfigError("defaults 必须是键值映射")
    _reject_unknown(defaults_raw, _DEFAULTS_KEYS, "defaults")

    base: dict[str, Any] = {
        "port": 22,
        "timeout": 15,
        "retries": 0,
        "device_type": "cisco_ios",
    }
    base.update(defaults_raw)

    workers = _as_int(raw.get("workers", 4), "workers", 1, 64)

    devices_raw = raw.get("devices")
    if not isinstance(devices_raw, list) or not devices_raw:
        raise ConfigError("devices 必须是非空列表")

    base_dir = source.resolve().parent
    devices: list[Device] = []
    seen: dict[str, str] = {}

    for index, item in enumerate(devices_raw, start=1):
        where = f"devices[{index}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{where} 必须是键值映射")

        name = _as_name(item.get("name"), f"{where}.name")
        where = f"{where}（{name}）"
        _reject_unknown(item, _DEVICE_KEYS, where)

        folded = name.casefold()
        if folded in seen:
            raise ConfigError(f"设备名重复: {name} 与 {seen[folded]}")
        seen[folded] = name

        merged = {key: item.get(key, base.get(key)) for key in _DEVICE_KEYS}
        host = _as_text(merged["host"], f"{where}.host")
        username = _as_text(merged["username"], f"{where}.username")
        device_type = _as_device_type(merged["device_type"], f"{where}.device_type")
        port = _as_int(merged["port"], f"{where}.port", 1, 65535)
        timeout = _as_float(merged["timeout"], f"{where}.timeout", 0.1, 600.0)
        retries = _as_int(merged["retries"], f"{where}.retries", 0, 10)

        password = _resolve_password(item, where)
        key_file = _resolve_key_file(merged.get("key_file"), base_dir, where)
        if not password and not key_file:
            raise ConfigError(
                f"{where} 缺少认证信息：password / password_env 或 key_file 至少提供一个"
            )
        secret = _as_secret(item.get("secret"), f"{where}.secret") if "secret" in item else None

        commands = (
            _as_commands(item.get("commands"), f"{where}.commands")
            if "commands" in item
            else None
        )
        extra_commands = (
            _as_commands(item.get("extra_commands"), f"{where}.extra_commands")
            if "extra_commands" in item
            else ()
        )
        description = (
            _as_text(item.get("description"), f"{where}.description", required=False)
            if "description" in item
            else ""
        )

        devices.append(
            Device(
                name=name,
                host=host,
                username=username,
                device_type=device_type,
                port=port,
                timeout=timeout,
                retries=retries,
                password=password,
                key_file=key_file,
                secret=secret,
                commands=commands,
                extra_commands=extra_commands,
                description=description,
            )
        )

    return Inventory(devices=tuple(devices), workers=workers, path=source)


def load_commands(path: str | Path) -> dict[str, tuple[str, ...]]:
    """读取 commands.yaml，返回 {device_type: (命令, ...)}。"""

    source = Path(path)
    if not source.is_file():
        raise ConfigError(f"命令模板不存在: {source}")

    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{source} 不是 UTF-8 文本: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{source} 不是合法的 YAML: {_yaml_error(exc)}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"{source} 顶层必须是键值映射（包含 templates）")
    _reject_unknown(raw, {"templates"}, "顶层")

    templates_raw = raw.get("templates")
    if not isinstance(templates_raw, dict) or not templates_raw:
        raise ConfigError("templates 必须是非空映射（device_type -> 命令列表）")

    templates: dict[str, tuple[str, ...]] = {}
    for device_type, commands_raw in templates_raw.items():
        name = _as_device_type(device_type, "templates 的键")
        templates[name] = _as_commands(commands_raw, f"templates.{name}")

    return templates


def plan_commands(
    inventory: Inventory, templates: dict[str, tuple[str, ...]]
) -> dict[str, tuple[str, ...]]:
    """为每台设备解析最终命令列表：模板（或覆盖）+ 额外命令。"""

    plans: dict[str, tuple[str, ...]] = {}
    for device in inventory.devices:
        if device.commands is not None:
            commands = list(device.commands)
            source = "设备覆盖"
        else:
            template = templates.get(device.device_type)
            if template is None:
                available = "、".join(sorted(templates)) or "（无）"
                raise ConfigError(
                    f"设备 {device.name} 的 device_type={device.device_type} "
                    f"在 commands.yaml 中没有对应模板；可用模板：{available}"
                )
            commands = list(template)
            source = "模板"

        commands.extend(device.extra_commands)

        unique: list[str] = []
        for command in commands:
            if command not in unique:
                unique.append(command)
        if not unique:
            raise ConfigError(f"设备 {device.name} 没有任何可执行命令（来源：{source}）")

        plans[device.name] = tuple(unique)

    return plans


def _resolve_password(item: dict[str, Any], where: str) -> str | None:
    has_password = "password" in item and item["password"] not in (None, "")
    has_env = "password_env" in item and item["password_env"] not in (None, "")

    if has_password and has_env:
        raise ConfigError(f"{where} 不能同时设置 password 与 password_env")

    if has_password:
        return _as_secret(item["password"], f"{where}.password")

    if has_env:
        env_name = _as_text(item["password_env"], f"{where}.password_env")
        value = os.environ.get(env_name)
        if not value:
            raise ConfigError(f"{where} 引用的环境变量 {env_name} 未设置或为空")
        return value

    return None


def _resolve_key_file(value: Any, base_dir: Path, where: str) -> str | None:
    if value in (None, ""):
        return None
    text = _as_text(value, f"{where}.key_file")
    expanded = Path(os.path.expandvars(os.path.expanduser(text)))
    if not expanded.is_absolute():
        expanded = base_dir / expanded
    return str(expanded)


def _as_commands(value: Any, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{where} 必须是非空列表（每项一条命令）")

    commands: list[str] = []
    for index, raw in enumerate(value, start=1):
        if not isinstance(raw, str):
            raise ConfigError(f"{where}[{index}] 必须是字符串")
        command = raw.strip()
        if not command:
            raise ConfigError(f"{where}[{index}] 不能为空")
        if "\n" in command or "\r" in command or "\x00" in command:
            raise ConfigError(f"{where}[{index}] 必须是单行命令")
        commands.append(command)
    return tuple(commands)


def _as_name(value: Any, where: str) -> str:
    text = _as_text(value, where)
    if not _NAME_RE.match(text):
        raise ConfigError(
            f"{where}={text!r} 不合法：只允许字母、数字、点、下划线、短横线，"
            "以字母或数字开头，最长 64 字符"
        )
    return text


def _as_device_type(value: Any, where: str) -> str:
    text = _as_text(value, where)
    if not _DEVICE_TYPE_RE.match(text):
        raise ConfigError(
            f"{where}={text!r} 不合法：只允许小写字母、数字、下划线，以字母开头"
        )
    return text


def _as_text(value: Any, where: str, *, required: bool = True) -> str:
    if value is None:
        if required:
            raise ConfigError(f"{where} 不能为空")
        return ""
    if not isinstance(value, str):
        raise ConfigError(f"{where} 必须是字符串")
    text = value.strip()
    if required and not text:
        raise ConfigError(f"{where} 不能为空")
    if "\n" in text or "\r" in text:
        raise ConfigError(f"{where} 不能包含换行")
    return text


def _as_secret(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{where} 必须是字符串（数字请加引号）")
    if not value:
        raise ConfigError(f"{where} 不能为空")
    if "\n" in value or "\r" in value:
        raise ConfigError(f"{where} 不能包含换行")
    return value


def _as_int(value: Any, where: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where} 必须是整数")
    if not minimum <= value <= maximum:
        raise ConfigError(f"{where} 必须在 {minimum}..{maximum} 之间，当前为 {value}")
    return value


def _as_float(value: Any, where: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where} 必须是数字")
    number = float(value)
    if not math.isfinite(number):
        raise ConfigError(f"{where} 必须是有限数字")
    if not minimum <= number <= maximum:
        raise ConfigError(f"{where} 必须在 {minimum}..{maximum} 之间，当前为 {value}")
    return number


def _reject_unknown(mapping: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(str(key) for key in set(mapping) - allowed)
    if unknown:
        raise ConfigError(f"{where} 包含未知字段：{', '.join(unknown)}")


def _yaml_error(exc: yaml.YAMLError) -> str:
    mark = getattr(exc, "problem_mark", None)
    problem = getattr(exc, "problem", None) or str(exc)
    if mark is not None:
        return f"第 {mark.line + 1} 行第 {mark.column + 1} 列：{problem}"
    return problem
