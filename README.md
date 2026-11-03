# netcheck

网络设备批量巡检与配置变更审计工具（最小可用版）。

对标开源生态里的 **Oxidized（配置备份 + 历史）** 与 **Netmiko（SSH 连接）**：
通过 Netmiko 以 SSH 只读方式采集命令输出 → 按设备保存文本快照 → 与上一份快照做 diff
→ 生成 Markdown 报告。

> 本工具**只读**：不会向设备下发配置，只执行 `commands.yaml` 中列出的查询命令。

## 功能

- 读取 `devices.yaml`，支持多台设备、并发执行
- 密码 / 环境变量密码 / SSH 密钥文件三种认证来源
- 按设备的 `device_type` 从 `commands.yaml` 取命令模板
- 每台设备可覆盖命令模板或追加额外命令
- 每台设备独立的超时与重试次数
- 单台设备失败不影响其他设备，失败原因写入报告
- 每次执行保存快照：`snapshots/<设备名>/<YYYYMMDD_HHMMSS>.txt`
- 与上一份快照做 unified diff，统计新增 / 删除行
- 生成 Markdown 报告：`reports/<YYYYMMDD_HHMMSS>.md`
- `--dry-run`：只打印将要执行的命令，不连接设备、不写文件

## 目录结构

```
netcheck/
├── README.md
├── requirements.txt
├── devices.yaml          # 设备清单（示例）
├── commands.yaml         # 命令模板
├── netcheck/
│   ├── __init__.py
│   ├── __main__.py       # 支持 python -m netcheck
│   ├── main.py           # CLI 入口与流程编排
│   ├── inventory.py      # 读取与校验 devices.yaml / commands.yaml
│   ├── collector.py      # Netmiko 连接与命令执行
│   ├── snapshot.py       # 快照读写
│   ├── diff.py           # 与上一份快照对比
│   └── report.py         # 生成 Markdown 报告
├── snapshots/            # 运行时生成
└── reports/              # 运行时生成
```

## 安装

要求 Python 3.10+（开发与验证使用 Python 3.13 + Netmiko 4.8）。

```bash
cd netcheck
python -m venv .venv

# Linux / macOS
source .venv/bin/activate
# Windows PowerShell
# .\.venv\Scripts\Activate.ps1

python -m pip install -r requirements.txt
```

依赖只有 `netmiko` 与 `PyYAML`。如果 PowerShell 不允许激活脚本，也可以直接用
`.\.venv\Scripts\python.exe -m netcheck ...`。

## 快速开始

```bash
cd netcheck

# 1. 先看计划，不连设备
python -m netcheck --dry-run

# 2. 修改 devices.yaml 里的地址与凭据后正式执行
python -m netcheck --inventory devices.yaml --commands commands.yaml
```

示例 `devices.yaml` 中的 `REPLACE_ME` 与 `~/.ssh/id_rsa` 只是占位值，直接运行会连接失败；
这是预期行为。请替换成实验环境里真实可用的设备与凭据。

## 配置 `devices.yaml`

```yaml
defaults:
  port: 22
  timeout: 15          # 连接超时 + 单条命令读取超时（秒）
  retries: 0           # 失败后的重试次数（总尝试次数 = retries + 1）
  device_type: cisco_ios

workers: 4             # 并发 worker 数，可被 --workers 覆盖

devices:
  - name: core-sw-01
    host: 192.168.56.101
    username: admin
    password_env: NETCHECK_CORE_SW_01_PASSWORD
    device_type: huawei
    timeout: 20
    retries: 1
    description: 核心交换机

  - name: lab-linux
    host: 192.168.56.102
    username: tester
    key_file: ~/.ssh/id_rsa
    device_type: linux

  - name: edge-01
    host: 192.168.56.103
    username: netops
    password: "REPLACE_ME"
    device_type: cisco_ios
    commands:                 # 可选：整体覆盖 commands.yaml 的模板
      - show version
      - show running-config
    extra_commands:           # 可选：在模板/覆盖命令之后追加
      - show logging | include ERROR
```

### 字段说明

| 字段 | 必填 | 说明 |
|---|---|---|
| `name` | 是 | 设备名，同时作为快照目录名。只允许字母、数字、`.`、`_`、`-`，以字母或数字开头，最长 64 字符；不能重复（不区分大小写） |
| `host` | 是 | 设备地址（IP 或主机名） |
| `username` | 是 | SSH 用户名 |
| `password` | 二选一 | SSH 密码（建议用引号；不要提交到版本库） |
| `password_env` | 二选一 | 从环境变量读取密码的变量名，和 `password` 不能同时设置 |
| `key_file` | 二选一 | SSH 私钥路径，支持 `~` 与环境变量；相对路径相对于 `devices.yaml` 所在目录 |
| `secret` | 否 | 可选的 enable 密码（Cisco 等设备需要进入特权模式时使用） |
| `device_type` | 否 | Netmiko 平台名，默认取 `defaults.device_type`。必须在 `commands.yaml` 里有模板，除非该设备用 `commands` 覆盖 |
| `port` | 否 | SSH 端口，默认 `defaults.port`（22） |
| `timeout` | 否 | 连接与命令读取超时秒数，默认 `defaults.timeout`（15） |
| `retries` | 否 | 重试次数，默认 `defaults.retries`（0），范围 0–10 |
| `commands` | 否 | 覆盖该设备的命令列表（非空字符串列表） |
| `extra_commands` | 否 | 追加到模板/覆盖命令之后的命令列表 |
| `description` | 否 | 备注，会显示在 `--dry-run` 计划里 |

密码与私钥内容不会出现在日志、终端输出或报告中；`Device` 的 `repr` 也隐藏了密码。
仍然建议把含密码的 `devices.yaml` 设为仅自己可读，或使用 `password_env`。

## 配置 `commands.yaml`

```yaml
templates:
  cisco_ios:
    - show version
    - show running-config
    - show ip interface brief
  huawei:
    - display version
    - display current-configuration
    - display ip interface brief
  linux:
    - hostname
    - ip addr
    - cat /etc/os-release
```

- 键名是 Netmiko 的 `device_type`。
- 每个模板必须是非空列表，每项是一条**单行**命令。
- 设备条目的 `commands` 会整体替换模板；`extra_commands` 会追加。
- 重复命令会被去重，执行顺序按配置顺序。

## 运行参数

```bash
python -m netcheck [选项]
```

| 选项 | 默认值 | 说明 |
|---|---|---|
| `--inventory` | `devices.yaml` | 设备清单路径 |
| `--commands` | `commands.yaml` | 命令模板路径 |
| `--workers` | `devices.yaml` 的 `workers`，缺省 4 | 并发数，1–64 |
| `--snapshot-dir` | `snapshots` | 快照根目录 |
| `--report-dir` | `reports` | 报告目录 |
| `--dry-run` | 关闭 | 只打印计划，不连接、不写文件 |
| `--fail-on-error` | 关闭 | 有设备失败时以退出码 1 结束；默认仍然生成报告并返回 0 |
| `-v / --verbose` | 关闭 | 输出调试日志（包含 Netmiko 调试信息） |
| `--version` | | 显示版本 |

退出码：

- `0`：巡检完成并生成报告（即使有设备失败）
- `1`：输出目录/报告写入失败，或加了 `--fail-on-error` 且有设备失败
- `2`：配置错误（YAML 格式、字段缺失、设备名重复、没有命令模板等）
- `130`：Ctrl+C 中断

## 怎么读报告

报告按设备清单顺序生成：

````markdown
# 巡检报告 2026-11-03 21:40

## 总览

| 设备 | 状态 | 命令数 | 变更 |
|---|---|---|---|
| core-sw-01 | ✅ | 3 | +1 / -1 |
| lab-linux | ✅ | 3 | 无变化 |
| edge-01 | ❌ 连接超时 | - | - |

## core-sw-01

- 地址：`192.168.56.101:22`
- 设备类型：`huawei`
- 认证方式：密码
- 快照：`snapshots/core-sw-01/20261103_214000.txt`
- 对比基准：2026-11-02 21:00（`snapshots/core-sw-01/20261102_210000.txt`）

### display current-configuration

与上一份快照（2026-11-02 21:00）对比：**+1 / -1**

```diff
@@ -5,3 +5,3 @@
-ip route-static 0.0.0.0 0.0.0.0 192.168.1.1
+ip route-static 0.0.0.0 0.0.0.0 192.168.1.254
```
````

- **总览**：`✅` 成功；`❌` 后跟失败原因（连接超时 / 认证失败 / 密钥不可用 / 命令执行超时等）。
- **变更列**：`+新增行 / -删除行`；`无变化`；`首次采集`；`新增命令`（某条命令第一次出现）。
- **每台设备小节**：地址、设备类型、认证方式、本次快照、对比基准。
- **每条命令小节**：首次采集、无变化，或 unified diff（`-` 旧行，`+` 新行，`@@` 是上下文范围）。
- 失败设备没有快照，小节会给出**原因**和**尝试次数**，其他设备不受影响。

快照是可直接阅读的文本，含有 `# device:` 等元信息和
`===== NETCHECK CMD: <命令> =====` 分隔符；每次运行生成一份，历史不会被覆盖
（同一秒内重复运行会追加 `_01`、`_02` 后缀）。

## 验收自测

1. **dry-run 能跑通**：
   ```bash
   python -m netcheck --dry-run
   ```
   应打印每台设备的命令计划，且不创建 `snapshots/`、`reports/`。

2. **首次采集**：把示例换成实验环境设备后运行一次，报告里显示“首次采集”。

3. **改配置后重跑**：在设备上手工修改一行配置（例如静态路由下一跳），再运行：
   ```bash
   python -m netcheck --inventory devices.yaml --commands commands.yaml
   ```
   报告应出现正确的 `-` / `+` 行和 `+1 / -1` 统计。

4. **单台失败隔离**：把一台设备的 `host` 改成不可达地址，或把 `password` 改错，再运行。
   该设备在总览里显示 `❌`，其余设备仍正常生成快照和报告；命令仍返回 0（除非加
   `--fail-on-error`）。

5. **确认没有硬编码凭据**：设备地址、用户名、密码只存在于 YAML/环境变量中，
   Python 代码里没有写死。

## 安全与边界

- 只读：只执行 `commands.yaml`/设备 `commands` 中的命令，不修改设备配置，不执行
  `configure`、`write`、`reload` 等操作。请自行确认模板中不含写操作。
- 不实现端口扫描、口令爆破、漏洞探测；只连接清单里明确列出的设备与端口。
- 日志与报告中不记录密码、`secret` 或私钥内容；异常信息里的密码会被替换为 `***`。
- 快照与报告可能包含设备配置（如 SNMP 团体名、邻居信息），请妥善保护
  `snapshots/`、`reports/` 目录的访问权限。
- 不需要 root / 管理员权限；使用普通用户运行即可。
- 私钥文件建议设为仅当前用户可读（Linux `chmod 600`；Windows 用文件属性/ACL 限制）。

## 已知限制

- diff 是**文本行级**比较：`show version` 里的 uptime、时间戳等每次都会变化，
  会产生噪声行；可按需从 `commands.yaml` 去掉这类命令，或后续扩展忽略规则。
- 只比较同一命令在上一份快照中的文本；命令顺序变化不影响匹配，但命令被删除时
  旧快照中的输出不会出现在本次报告中。
- 依赖 Netmiko 的 `device_type` 支持范围；非标准设备请先在 Netmiko 文档中确认平台名。
- 并发是单进程线程池；单台设备的 `timeout × (retries+1)` 会占用一个 worker。
- 快照为纯文本，未做压缩/加密。
