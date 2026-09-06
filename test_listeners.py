"""Read Clash listener configuration and probe HTTP / SOCKS5 proxy access."""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from getpass import getpass
import ipaddress
import json
import os
from pathlib import Path
import socket
import sys
import time
from urllib.parse import quote, urlsplit

import requests
import yaml


class Console:
    def __init__(self, mode='auto'):
        self.enabled = mode == 'always' or (
            mode == 'auto' and sys.stdout.isatty() and 'NO_COLOR' not in os.environ)
        if self.enabled and os.name == 'nt' and sys.stdout.isatty():
            # Enable ANSI colors in Windows Console / PowerShell without dependencies.
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.GetStdHandle.argtypes = [wintypes.DWORD]
            kernel.GetStdHandle.restype = wintypes.HANDLE
            kernel.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            handle = kernel.GetStdHandle(-11)
            flags = wintypes.DWORD()
            if not (kernel.GetConsoleMode(handle, ctypes.byref(flags)) and
                    kernel.SetConsoleMode(handle, flags.value | 0x0004)):
                self.enabled = mode == 'always'

    def paint(self, text, color):
        codes = {'green': 32, 'red': 31, 'yellow': 33, 'cyan': 36}
        return f'\033[{codes[color]}m{text}\033[0m' if self.enabled else str(text)


def query_exit_ip(session, url, timeout):
    """Use the already configured proxy session; never retry directly."""
    started = time.monotonic()
    result = {'exit_ip': None, 'ip_error': None, 'ip_elapsed_ms': None}
    try:
        with session.get(url, timeout=timeout, allow_redirects=False, stream=True) as response:
            if response.status_code != 200:
                raise ValueError(f'HTTP {response.status_code}')
            # The expected JSON is tiny; cap input before parsing untrusted responses.
            body = bytearray()
            for chunk in response.iter_content(chunk_size=1024):
                body.extend(chunk)
                if len(body) > 4096:
                    raise ValueError('IP 响应过大')
            payload = json.loads(body)
            if not isinstance(payload, dict) or not isinstance(payload.get('ip'), str):
                raise ValueError('响应缺少 ip 字符串')
            try:
                address = ipaddress.ip_address(payload['ip'].strip())
            except ValueError:
                raise ValueError('响应不是有效 IP 地址') from None
            if not address.is_global:
                raise ValueError('响应不是公网 IP 地址')
            result['exit_ip'] = str(address)
    except requests.RequestException as exc:
        result['ip_error'] = f'{type(exc).__name__}: 出口 IP 查询失败'
    except (ValueError, UnicodeError) as exc:
        result['ip_error'] = ('IP 响应不是有效 JSON' if isinstance(exc, json.JSONDecodeError)
                              else str(exc))
    result['ip_elapsed_ms'] = round((time.monotonic() - started) * 1000)
    return result


def api_get(session, controller, endpoint, timeout):
    response = session.get(controller.rstrip('/') + endpoint, timeout=timeout,
                           allow_redirects=False)
    if response.status_code != 200:
        raise ValueError(f'{endpoint}: HTTP {response.status_code}')
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError(f'{endpoint}: expected JSON object')
    return payload


def load_listeners(controller, secret, config_path, timeout):
    with requests.Session() as session:
        session.trust_env = False
        if secret:
            session.headers['Authorization'] = f'Bearer {secret}'
        version = api_get(session, controller, '/version', timeout)
        runtime = api_get(session, controller, '/configs', timeout)

    # Standard Mihomo /configs exposes basic settings, not the full YAML.
    if 'listeners' in runtime:
        config, source = runtime, 'API /configs'
    else:
        path = Path(config_path).expanduser() if config_path else None
        if path is None:
            host = urlsplit(controller).hostname
            if host not in ('127.0.0.1', 'localhost', '::1'):
                raise ValueError('API 未返回 listeners；远程控制器请用 --config 指定其配置副本')
            appdata = os.environ.get('APPDATA')
            if not appdata:
                raise ValueError('API 未返回 listeners；请用 --config 指定运行配置 YAML')
            path = Path(appdata) / 'io.github.clash-verge-rev.clash-verge-rev' / 'clash-verge.yaml'
        with path.open(encoding='utf-8-sig') as stream:
            config = yaml.safe_load(stream)
        if not isinstance(config, dict):
            raise ValueError('配置文件根节点必须是映射')
        if not config_path:
            configured = str(config.get('external-controller', ''))
            configured_port = urlsplit('http://' + configured).port
            api_url = urlsplit(controller)
            api_port = api_url.port or (443 if api_url.scheme == 'https' else 80)
            if configured_port != api_port or config.get('mixed-port', 0) != runtime.get('mixed-port', 0):
                raise ValueError('自动发现的配置与 API 端口设置不匹配；请用 --config 明确指定')
        source = str(path.resolve())
        print('API 未返回 listeners，回退读取本地 YAML（不是 API 下载的配置）。')
    listeners = config.get('listeners')
    if not isinstance(listeners, list) or not listeners:
        raise ValueError('配置中没有非空 listeners 列表')
    return listeners, source, version.get('version', 'unknown')


def connection_host(listener, override, controller):
    if override:
        return override
    host = str(listener.get('listen', '0.0.0.0')).strip('[]')
    if host in ('0.0.0.0', '::', '*', ''):
        return urlsplit(controller).hostname
    if host in ('127.0.0.1', '::1', 'localhost') and urlsplit(controller).hostname not in ('127.0.0.1', '::1', 'localhost'):
        raise ValueError('远程 listener 仅监听回环地址；请建立端口转发并指定 --proxy-host')
    return host


def probe(listener, protocol, args):
    result = {key: listener.get(key) for key in ('name', 'type', 'listen', 'port', 'proxy')}
    result.update(protocol=protocol, ok=False, tcp_ok=False, status_code=None, error=None)
    result.update(exit_ip=None, ip_error=None, ip_elapsed_ms=None)
    started = time.monotonic()
    try:
        port = int(listener['port'])
        if not 1 <= port <= 65535:
            raise ValueError('port 必须介于 1 和 65535')
        host = connection_host(listener, args.proxy_host, args.controller)
        result['host'] = host
        with socket.create_connection((host, port), timeout=args.timeout):
            result['tcp_ok'] = True
        credentials = ''
        users = listener.get('users') or []
        if users:
            user = users[0]
            credentials = quote(str(user['username']), safe='') + ':' + quote(str(user['password']), safe='') + '@'
        address = f'[{host}]' if ':' in host else host
        scheme = 'http' if protocol == 'http' else 'socks5h'
        proxy_url = f'{scheme}://{credentials}{address}:{port}'
        with requests.Session() as session:
            # Ignore environment proxies, NO_PROXY, and netrc; never fall back to direct.
            session.trust_env = False
            session.proxies = {'http': proxy_url, 'https': proxy_url}
            with session.get(args.url, timeout=args.timeout, allow_redirects=False,
                             stream=True) as response:
                result['status_code'] = response.status_code
                result['ok'] = response.status_code == args.expected_status
                if not result['ok']:
                    result['error'] = f'预期 HTTP {args.expected_status}，实际 {response.status_code}'
            result['elapsed_ms'] = round((time.monotonic() - started) * 1000)
            if result['ok'] and not args.no_ip:
                result.update(query_exit_ip(session, args.ip_url, args.timeout))
    except Exception as exc:
        # Avoid exception URLs exposing proxy credentials. Keep useful failure categories.
        if isinstance(exc, requests.exceptions.ProxyError):
            error = 'ProxyError: 代理连接或 HTTP CONNECT 失败'
        elif isinstance(exc, requests.exceptions.SSLError):
            error = 'SSLError: TLS 握手或证书校验失败'
        elif isinstance(exc, requests.exceptions.Timeout):
            error = 'Timeout: 请求超时'
        elif isinstance(exc, requests.exceptions.ConnectionError):
            error = 'ConnectionError: 代理握手或目标连接失败'
        elif isinstance(exc, OSError):
            error = f'{type(exc).__name__}: TCP/文件系统错误，errno={exc.errno}'
        else:
            error = f'{type(exc).__name__}: 配置或依赖错误'
        result['error'] = error
    result.setdefault('elapsed_ms', round((time.monotonic() - started) * 1000))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--controller', default='http://127.0.0.1:9097')
    parser.add_argument('--config', help='API 缺少 listeners 时使用的运行配置 YAML')
    parser.add_argument('--proxy-host', help='覆盖代理连接地址，例如端口转发后的地址')
    parser.add_argument('--url', default='https://www.gstatic.com/generate_204')
    parser.add_argument('--expected-status', type=int, default=204)
    parser.add_argument('--timeout', type=float, default=10, help='每次连接/读取超时秒数')
    parser.add_argument('--workers', type=int, default=8)
    parser.add_argument('--ports', help='只测试这些端口，例如 42002,42007')
    parser.add_argument('--output', default='listener-test-results.json')
    parser.add_argument('--color', choices=('auto', 'always', 'never'), default='auto')
    parser.add_argument('--ip-url', default='https://api64.ipify.org?format=json',
                        help='出口 IP 查询接口，需返回包含 ip 字符串的 JSON')
    parser.add_argument('--no-ip', action='store_true', help='跳过出口 IP 查询')
    args = parser.parse_args()
    console = Console(args.color)
    if args.timeout <= 0 or args.workers < 1:
        parser.error('timeout 和 workers 必须大于 0')
    for url in (args.controller, args.url, args.ip_url):
        parsed = urlsplit(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            parser.error('URL 必须为 HTTP(S)，且不含用户名/密码')
    secret = os.environ.get('CLASH_SECRET')
    if secret is None:
        secret = getpass('Clash 访问密钥（隐藏输入，无密钥直接回车）: ')
    try:
        listeners, source, version = load_listeners(args.controller, secret, args.config, args.timeout)
        selected_ports = {int(p.strip()) for p in args.ports.split(',')} if args.ports else None
        selected = []
        for listener in listeners:
            if not isinstance(listener, dict):
                raise ValueError('listener 必须是映射')
            if selected_ports is None or int(listener.get('port', 0)) in selected_ports:
                selected.append(listener)
        if selected_ports:
            missing = selected_ports - {int(item.get('port', 0)) for item in selected}
            if missing:
                raise ValueError(f'配置中找不到端口: {sorted(missing)}')
        jobs, skipped = [], []
        for item in selected:
            kind = item.get('type')
            protocols = {'mixed': ('http', 'socks5'), 'http': ('http',), 'socks': ('socks5',)}.get(kind, ())
            if not protocols:
                skipped.append({'name': item.get('name'), 'type': kind, 'reason': '不支持的监听器类型'})
            jobs.extend((item, protocol) for protocol in protocols)
        if not jobs:
            raise ValueError('没有可测试的 HTTP/SOCKS/mixed 监听器')
        print(console.paint(f'内核: {version}\n配置来源: {source}\n测试: {len(jobs)} 项；目标: {args.url}', 'cyan'), flush=True)
        results = []
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = [executor.submit(probe, item, protocol, args) for item, protocol in jobs]
            for future in as_completed(futures):
                result = future.result()
                results.append(result)
                status = 'PASS' if result['ok'] else 'FAIL'
                status = console.paint(status, 'green' if result['ok'] else 'red')
                progress = console.paint(f'[{len(results)}/{len(jobs)}]', 'cyan')
                if args.no_ip:
                    ip_text = 'IP=跳过'
                elif result['exit_ip']:
                    ip_text = console.paint(f"IP={result['exit_ip']}", 'cyan')
                else:
                    ip_text = console.paint(f"IP=未获取 ({result['ip_error'] or '测试 FAIL，跳过查询'})", 'yellow')
                print(f"{progress} {result['port']} {result['protocol']:6} {status} "
                      f"{result['elapsed_ms']}ms {ip_text} "
                      f"{console.paint(result['error'], 'red') if result['error'] else ''}", flush=True)
        results.sort(key=lambda item: (int(item['port']), item['protocol']))
        passed = sum(result['ok'] for result in results)
        report = {
            'timestamp': datetime.now(timezone.utc).isoformat(), 'controller': args.controller,
            'version': version, 'config_source': source, 'test_url': args.url,
            'expected_status': args.expected_status,
            'ip_url': None if args.no_ip else args.ip_url,
            'summary': {'total': len(results), 'passed': passed, 'failed': len(results) - passed,
                        'ip_obtained': sum(bool(result['exit_ip']) for result in results),
                        'skipped_listeners': len(skipped)},
            'results': results, 'skipped': skipped,
        }
        output = Path(args.output)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(console.paint(f'完成: {passed}/{len(results)} 通过；跳过 {len(skipped)} 个 listener',
                            'green' if passed == len(results) else 'red'))
        if not args.no_ip:
            print(console.paint(f"出口 IP: {report['summary']['ip_obtained']}/{passed} 项 PASS 测试获取成功",
                                'cyan' if report['summary']['ip_obtained'] == passed else 'yellow'))
        print(f'报告: {output.resolve()}')
        return 0 if passed == len(results) else 1
    except (ValueError, OSError, requests.RequestException, yaml.YAMLError) as exc:
        # Do not print HTTP response bodies or whole YAML, which may contain secrets.
        print(f'无法完成测试: {type(exc).__name__}: ' +
              (str(exc) if isinstance(exc, ValueError) and not isinstance(exc, requests.RequestException)
               else '请检查 API 鉴权、连接、配置路径及 YAML 格式'), file=sys.stderr)
        return 2


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    raise SystemExit(main())
