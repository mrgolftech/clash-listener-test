# Clash Listener Test

批量测试 Clash / Mihomo 配置中 `listeners` 的 HTTP 和 SOCKS5 代理可用性，并查询每项通过测试的代理出口公网 IP。

## 测试流程

1. 使用访问密钥连接外部控制接口，读取 `/version` 和 `/configs`。
2. 获取 `listeners`：API 返回该字段时直接使用，否则读取本地运行配置 YAML。
3. 检查监听端口是否可以建立 TCP 连接。
4. 通过对应的 HTTP 或 SOCKS5 代理访问 `https://www.gstatic.com/generate_204`。
5. **只有 HTTP 状态为 204 才判定 PASS**；其他状态码、超时或连接异常均为 FAIL。
6. **只有 PASS 才继续通过同一代理查询出口 IP**。IP 查询失败不改变已经得到的 PASS。
7. 终端彩色输出，并保存 JSON 报告。

`mixed` 监听器分别测试 HTTP、SOCKS5；`http` / `socks` 仅测试对应协议，其他类型明确跳过。
默认 HTTPS 目标通过 HTTP 代理时使用 CONNECT；SOCKS5 使用 `socks5h`，由代理端解析域名。
请求忽略系统代理环境变量及 `NO_PROXY`，不回退直连，保留 TLS 证书校验且不跟随重定向。
脚本不会修改 Clash 配置或切换节点，但会产生少量代理流量。

## 环境与依赖安装

- Python 3.10 或更高版本；当前在 Windows / Python 3.13 / Mihomo v1.19.29 上做过实测。
- 支持 `listeners` 的 Mihomo 内核，例如使用该内核的 Clash Verge Rev。旧版 Clash 的能力可能不同。
- Python 依赖：`requests[socks]`（包含 PySocks）和 `PyYAML`，见 [requirements.txt](requirements.txt)。

### Windows x64 独立 EXE

仓库通过 GitHub Actions 使用 Windows x64 runner + PyInstaller 构建单文件控制台程序：

```text
clash-listener-test-win-x64.exe
```

构建产物 Artifact 名称为：

```text
clash-listener-test-windows-x64
```

其中同时包含：

```text
clash-listener-test-win-x64.exe
clash-listener-test-win-x64.sha256
```

下载并解压后可直接在 64 位 Windows 上运行，不需要另外安装 Python：

```powershell
.\clash-listener-test-win-x64.exe
```

命令行参数与 Python 版本完全一致，例如：

```powershell
.\clash-listener-test-win-x64.exe --ports 42000,42001 --workers 4 --timeout 15
```

GitHub Actions 会先运行单元测试，再构建 EXE，并执行 `--help` 冒烟测试；SHA256 文件用于核对下载产物完整性。
当前 EXE 未做代码签名，Windows SmartScreen 或安全软件可能显示未知发布者提示；如需正式对外分发，建议后续增加代码签名。

本地也可在 Windows x64 环境自行构建：

```powershell
python -m pip install -r requirements.txt
python -m pip install pyinstaller==6.22.2
pyinstaller --clean --noconfirm --onefile --name clash-listener-test-win-x64 test_listeners.py
.\dist\clash-listener-test-win-x64.exe --help
```

### Windows PowerShell

```powershell
git clone https://github.com/mrgolftech/clash-listener-test.git
cd clash-listener-test
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe test_listeners.py
```

无需激活虚拟环境，避免 PowerShell 执行策略影响激活脚本。若没有 `python` 命令，可尝试用 `py -3` 创建虚拟环境。

### Linux / macOS

```bash
git clone https://github.com/mrgolftech/clash-listener-test.git
cd clash-listener-test
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python test_listeners.py --config /path/to/mihomo/config.yaml
```

Linux / macOS 用法尚未在真实系统上验证；需要显式指定运行配置路径。

## Clash 配置

### 1. 开启外部控制与访问密钥

在 Clash 客户端的外部控制设置中配置地址和密钥；或者通过客户端支持的覆写机制将以下字段合入运行配置：

```yaml
external-controller: 127.0.0.1:9097
secret: "REPLACE_WITH_YOUR_OWN_RANDOM_SECRET"
```

- `9097` 是控制 API 端口，不是要测试的代理端口；若使用其他端口，运行时传 `--controller`。
- `secret` 是控制 API 的访问密钥，由你自行设置。脚本用 `Authorization: Bearer <secret>` 鉴权。
- 建议绑定 `127.0.0.1`，与 Clash 在同一台电脑运行测试。不要将未妥善保护的控制 API 暴露到公网。
- 应用配置后确认内核已加载。Clash Verge 等客户端可能重新生成 YAML，持久设置应使用客户端提供的配置方式，不要只修改临时生成文件。

### 2. 配置 listeners

将下面片段合入已有配置；`proxy` 必须替换成当前配置中真实存在的节点或策略组名称：

```yaml
listeners:
  - name: node-a-listener
    type: mixed
    listen: 127.0.0.1
    port: 42000
    proxy: "替换为现有节点A的名称"
  - name: node-b-listener
    type: mixed
    listen: 127.0.0.1
    port: 42001
    proxy: "替换为现有节点B的名称"
```

每个端口必须空闲且不重复。本例中 `42000`、`42001` 分别接受 HTTP 和 SOCKS5；一个端口对应两个测试结果。
这是合并片段，不是包含节点定义的完整配置；请保留自己的 `proxies`、`proxy-groups` 和其他设置。
如果 `proxy` 指向策略组，结果反映当时实际选中的出口；前后请求的出口也可能变化。

### 3. 确认运行配置来源

**标准 Mihomo `/configs` 返回基本运行设置，并不提供完整 YAML；不能仅靠这个接口下载当前完整配置。**
脚本在 API 没有 `listeners` 时使用以下回退：

- 传入 `--config`：读取指定文件。
- 未传入且控制器在 Windows 本机：尝试读取
  `%APPDATA%\io.github.clash-verge-rev.clash-verge-rev\clash-verge.yaml`。
- 其他情况：提示指定 `--config`，不会猜测远程服务器的本地文件路径。

自动发现时会比对控制器端口与 `mixed-port`，这只是辅助校验，不保证文件与内存中的配置完全一致。
请使用客户端当前生成并加载的**运行配置**，而不是可能被覆写、合并前的原始订阅。终端及报告会显示实际读取来源。
API 鉴权失败不会绕过检查直接使用本地文件。

## 访问密钥如何传给脚本

默认运行后会提示隐藏输入：

```text
Clash 访问密钥（隐藏输入，无密钥直接回车）:
```

输入上面 Clash 配置中的 `secret`，按回车；终端不会显示输入字符。脚本不会将密钥写入报告。
如果 Clash 没有设置密钥，可直接回车。

自动化运行可通过调用环境提供 `CLASH_SECRET`；环境变量存在时不再提示输入（空字符串表示无密钥）。
请由本机安全输入或自动化平台的秘密变量注入，避免把真实密钥写进仓库、脚本、命令历史或截图。
脚本没有 `--secret` 命令行参数，也不会自动加载 `.env` 文件。

**控制密钥与代理用户名/密码不同。** 如果某个 listener 配置了 `users`，脚本从该 listener 中读取第一个用户进行代理鉴权；不会把 API 的 `secret` 发送给代理或 IP 查询服务。
当前不自动继承全局 `authentication`；若使用该方式，应确认 listener 的认证设置是否适配脚本。

## 常用命令

以下命令在 Windows 项目目录执行；Linux / macOS 将解释器路径换成 `.venv/bin/python`。

```powershell
# 本机默认设置：所有 listeners，两种协议，PASS 后查询 IP
.\.venv\Scripts\python.exe test_listeners.py

# 明确指定控制地址与运行配置文件
.\.venv\Scripts\python.exe test_listeners.py --controller http://127.0.0.1:9097 --config "C:\path\clash-verge.yaml"

# 仅测试部分端口，降低并发，增加超时
.\.venv\Scripts\python.exe test_listeners.py --ports 42000,42001 --workers 4 --timeout 15

# 只测可用性，不查询出口 IP
.\.venv\Scripts\python.exe test_listeners.py --no-ip

# 强制彩色输出 / 关闭颜色
.\.venv\Scripts\python.exe test_listeners.py --color always
.\.venv\Scripts\python.exe test_listeners.py --color never

# 更换报告位置
.\.venv\Scripts\python.exe test_listeners.py --output run-results.json
```

### 参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--controller` | `http://127.0.0.1:9097` | 控制 API 地址 |
| `--config` | 自动发现（仅 Windows 本机） | API 缺少 listeners 时读取的 YAML |
| `--ports` | 全部 | 逗号分隔的端口，必须存在于配置中 |
| `--workers` | `8` | 并发测试项数 |
| `--timeout` | `10` | 每次连接/读取的超时秒数，不是整个脚本的总时限 |
| `--color` | `auto` | `auto` / `always` / `never` |
| `--no-ip` | 不启用 | 跳过 IP 查询 |
| `--ip-url` | `https://api64.ipify.org?format=json` | 返回 `{"ip":"公网地址"}` 的接口 |
| `--output` | `listener-test-results.json` | JSON 路径；再次运行覆盖同名文件 |
| `--proxy-host` | listener 地址 | 覆盖实际连接的代理主机，端口仍按配置 |
| `--url` | `https://www.gstatic.com/generate_204` | 可用性测试目标 |
| `--expected-status` | `204` | 判定 PASS 的准确状态码 |

`--url` 和 `--expected-status` 支持高级自定义；正常使用保留默认值即可。
远程测试需同时满足 API 和代理端口可达，并通过 `--config` 提供远程运行配置的本地副本。
远程回环 listener 需要先建立端口转发；仅传 `--proxy-host` 并不会创建转发。

## 输出与结果含义

- 绿色：PASS；红色：FAIL；青色：进度及 IP；黄色：IP 查询异常。
- `auto` 在交互终端启用颜色，重定向输出时关闭；也尊重 `NO_COLOR` 环境变量。
- 默认经 [ipify](https://www.ipify.org/) 查询公网 IPv4 或 IPv6，这是该服务观察到的**出口 IP**，不保证是节点连接地址；双栈或动态出口可能让两种协议显示不同 IP。
- `elapsed_ms` 包含 TCP 检查和可用性请求，不包括 IP 查询，不是 ICMP ping。
- IP 查询单独记录 `exit_ip`、`ip_error`、`ip_elapsed_ms`；FAIL 或 `--no-ip` 时这些值为空。
- PASS 只证明测试当时能通过该代理访问指定目标，不保证所有网站可达；FAIL 可能与目标限制、网络或节点有关。

JSON 包含 `config_source`、测试目标、内核版本、汇总 `summary` 以及逐项 `results`。
汇总中的 `ip_obtained` 是成功取得 IP 的测试项数，并不是去重后的 IP 数量。
报告可能包含节点名称、出口 IP 和本机路径，默认已通过 `.gitignore` 排除；使用自定义文件名时请自行检查提交范围。

退出码：`0` = 所有已执行的代理测试 PASS；`1` = 至少一项 FAIL；`2` = 配置、API 或执行错误。
IP 查询失败不影响退出码。不支持的 listener 会记录为 skipped；没有任何可测试项时报错。

## 排错

| 现象 | 检查项 |
|---|---|
| API 401 / 403 | `secret` 与输入密钥是否一致，`CLASH_SECRET` 是否仍保存旧值 |
| API 连接失败 | Clash 是否运行、外部控制端口是否正确、配置是否生效 |
| API 没有 listeners | 正常限制；传入当前运行 YAML 的 `--config` 路径 |
| TCP 连接失败 | listener 是否已加载、端口是否正确、是否存在防火墙或地址限制 |
| TCP 成功但 FAIL | 代理握手、节点出口、目标可达性、TLS 或认证设置 |
| PASS 但 IP 未获取 | 查询服务连接失败或返回异常；代理可用性结果仍为 PASS |
| SOCKS 依赖缺失 | 在实际运行的虚拟环境内重新安装 `requirements.txt` |

## 开发验证

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m py_compile test_listeners.py
```

测试使用模拟网络，不需要真实 Clash 或密钥；验证两种协议的 PASS→IP 顺序、失败跳过、IP 校验与颜色开关。

GitHub Actions 同时执行：

- Linux / Windows，Python 3.10 / 3.13 单元测试；
- `py_compile`；
- Windows x64 PyInstaller 打包；
- 打包后 EXE `--help` 冒烟测试；
- EXE 与 SHA256 作为 workflow artifact 保存。

## 参考

- [Mihomo API：基本运行配置及鉴权](https://wiki.metacubex.one/api/)
- [Mihomo 全局配置：外部控制及 secret](https://wiki.metacubex.one/config/general/)
- [Mihomo mixed listener](https://wiki.metacubex.one/config/inbound/listeners/mixed/)
