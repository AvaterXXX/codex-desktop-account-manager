#!/usr/bin/env python3
"""Codex ChatGPT OAuth：本地 PKCE 回调、独立保存账户、不改当前登录态。"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import secrets
import shutil
import subprocess
import threading
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import requests

from manager import CodexAccountManager, decode_jwt_payload, extract_account_info

LogFn = Callable[[str], None]

# 与当前 Codex CLI 浏览器登录使用的公开 OAuth 客户端配置一致。
OAUTH_AUTH_URL = "https://auth.openai.com/oauth/authorize"
OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
OAUTH_REDIRECT_URI = "http://localhost:1455/auth/callback"
OAUTH_SCOPES = (
    "openid profile email offline_access "
    "api.connectors.read api.connectors.invoke"
)
OAUTH_ORIGINATOR = "Codex Desktop"
CALLBACK_BIND_HOST = "127.0.0.1"
CALLBACK_PORT = 1455
CALLBACK_PATH = "/auth/callback"


def _log(msg: str, log: LogFn | None) -> None:
    if log:
        log(msg)
    else:
        print(msg, flush=True)


def _which_browser_exe() -> list[tuple[str, str, list[str]]]:
    """返回候选浏览器：(名称, 可执行路径, 无痕参数)。"""
    local = os.environ.get("LOCALAPPDATA", "")
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    candidates: list[tuple[str, list[str], list[str]]] = [
        (
            "Chrome",
            [
                rf"{pf}\Google\Chrome\Application\chrome.exe",
                rf"{pf86}\Google\Chrome\Application\chrome.exe",
                rf"{local}\Google\Chrome\Application\chrome.exe",
            ],
            ["--incognito"],
        ),
        (
            "Edge",
            [
                rf"{pf}\Microsoft\Edge\Application\msedge.exe",
                rf"{pf86}\Microsoft\Edge\Application\msedge.exe",
            ],
            ["--inprivate"],
        ),
        (
            "Firefox",
            [
                rf"{pf}\Mozilla Firefox\firefox.exe",
                rf"{pf86}\Mozilla Firefox\firefox.exe",
            ],
            ["-private-window"],
        ),
    ]
    found: list[tuple[str, str, list[str]]] = []
    for name, paths, flags in candidates:
        for path in paths:
            if path and Path(path).exists():
                found.append((name, path, flags))
                break
    return found


def open_private_browser(url: str, log: LogFn | None = None) -> str:
    """
    只用无痕/隐私窗口打开 URL。

    不回退到系统默认浏览器，避免 Codex CLI 和普通浏览器重复打开。
    """
    for name, exe, flags in _which_browser_exe():
        try:
            subprocess.Popen(
                [exe, *flags, url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            how = f"{name} 无痕/隐私窗口"
            _log(f"已用 {how} 打开授权页", log)
            return how
        except Exception as exc:
            _log(f"{name} 无痕启动失败: {exc}", log)

    raise RuntimeError(
        "找不到可启动的 Chrome/Edge/Firefox 无痕窗口；请从日志复制授权链接，手动在无痕窗口打开"
    )


@dataclass(frozen=True)
class PKCECodes:
    verifier: str
    challenge: str


@dataclass(frozen=True)
class OAuthCallback:
    code: str = ""
    state: str = ""
    error: str = ""
    error_description: str = ""


def generate_oauth_state() -> str:
    """生成不可预测、可安全放进 URL 的 OAuth state。"""
    return secrets.token_urlsafe(32)


def generate_pkce_codes() -> PKCECodes:
    """生成 RFC 7636 S256 verifier/challenge。"""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode("ascii").rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return PKCECodes(verifier=verifier, challenge=challenge)


def build_authorization_url(
    state: str,
    code_challenge: str,
    *,
    redirect_uri: str = OAUTH_REDIRECT_URI,
) -> str:
    """构造 Codex 的标准 Authorization Code + PKCE 授权地址。"""
    if not state or not code_challenge:
        raise ValueError("OAuth state 和 PKCE challenge 不能为空")
    # 参数集合、顺序和空格编码均与本机当前 Codex Desktop CLI 保持一致。
    query = urllib.parse.urlencode(
        [
            ("response_type", "code"),
            ("client_id", OAUTH_CLIENT_ID),
            ("redirect_uri", redirect_uri),
            ("scope", OAUTH_SCOPES),
            ("code_challenge", code_challenge),
            ("code_challenge_method", "S256"),
            ("id_token_add_organizations", "true"),
            ("codex_cli_simplified_flow", "true"),
            ("state", state),
            ("originator", OAUTH_ORIGINATOR),
        ],
        quote_via=urllib.parse.quote,
    )
    return f"{OAUTH_AUTH_URL}?{query}"


def _safe_compare_state(received: str, expected: str) -> bool:
    try:
        return secrets.compare_digest(received.encode("utf-8"), expected.encode("utf-8"))
    except Exception:
        return False


class _LoopbackOAuthServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        expected_state: str,
    ) -> None:
        self.expected_state = expected_state
        self.callback_event = threading.Event()
        self.callback: OAuthCallback | None = None
        super().__init__(server_address, _OAuthCallbackHandler)


class _OAuthCallbackHandler(BaseHTTPRequestHandler):
    server: _LoopbackOAuthServer

    def log_message(self, _format: str, *_args: object) -> None:
        # 不把授权 code/state 写入控制台或日志。
        return

    def _send_html(self, status: int, title: str, message: str) -> None:
        body = (
            "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title>"
            "<style>body{font-family:system-ui,sans-serif;background:#0f172a;color:#e2e8f0;"
            "display:grid;place-items:center;min-height:100vh;margin:0}.card{max-width:560px;"
            "padding:32px;border:1px solid #334155;border-radius:16px;background:#111827}"
            "h1{font-size:22px;margin:0 0 12px}p{line-height:1.65;color:#cbd5e1}</style>"
            f"</head><body><main class='card'><h1>{html.escape(title)}</h1>"
            f"<p>{html.escape(message)}</p></main></body></html>"
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _first(params: dict[str, list[str]], key: str) -> str:
        values = params.get(key) or []
        return values[0] if values else ""

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path != CALLBACK_PATH:
            self._send_html(404, "未找到", "这不是 Codex OAuth 回调地址。")
            return

        params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        received_state = self._first(params, "state")
        if not received_state or not _safe_compare_state(
            received_state, self.server.expected_state
        ):
            self._send_html(400, "授权校验失败", "state 不匹配，请回到账户管理器重新登录。")
            return

        if self.server.callback_event.is_set():
            self._send_html(409, "授权已接收", "本次授权已经处理，可以关闭此页面。")
            return

        code = self._first(params, "code")
        error = self._first(params, "error")
        error_description = self._first(params, "error_description")
        if error:
            self.server.callback = OAuthCallback(
                state=received_state,
                error=error,
                error_description=error_description,
            )
            self.server.callback_event.set()
            self._send_html(
                400,
                "授权未完成",
                error_description or error or "OpenAI 没有完成本次授权。",
            )
            return
        if not code:
            self.server.callback = OAuthCallback(
                state=received_state,
                error="missing_code",
                error_description="回调中缺少 authorization code",
            )
            self.server.callback_event.set()
            self._send_html(400, "授权回调无效", "回调中缺少授权代码，请重新登录。")
            return

        self.server.callback = OAuthCallback(code=code, state=received_state)
        self.server.callback_event.set()
        self._send_html(
            200,
            "Codex 授权已接收",
            "账户管理器正在安全保存凭据。你现在可以关闭这个无痕窗口。",
        )


class OAuthCallbackListener:
    """只监听本机回环地址的短生命周期 OAuth 回调服务。"""

    def __init__(
        self,
        expected_state: str,
        *,
        host: str = CALLBACK_BIND_HOST,
        port: int = CALLBACK_PORT,
    ) -> None:
        if not expected_state:
            raise ValueError("expected_state 不能为空")
        try:
            self._server = _LoopbackOAuthServer((host, port), expected_state)
        except OSError as exc:
            if getattr(exc, "winerror", None) == 10048 or getattr(exc, "errno", None) in {
                48,
                98,
                10048,
            }:
                raise RuntimeError(
                    f"本地 OAuth 回调端口 {port} 已被占用，请关闭其他登录窗口或占用该端口的程序后重试"
                ) from exc
            raise RuntimeError(f"无法启动本地 OAuth 回调服务：{exc}") from exc
        self._closed = False
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.1},
            name="codex-oauth-loopback",
            daemon=True,
        )
        self._thread.start()

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def wait(
        self,
        *,
        timeout_sec: float,
        cancel_event: threading.Event | None = None,
    ) -> OAuthCallback:
        deadline = time.monotonic() + timeout_sec
        while True:
            if cancel_event and cancel_event.is_set():
                raise InterruptedError("用户取消了 OAuth 登录")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("等待 OAuth 登录超时，请重新开始登录")
            if self._server.callback_event.wait(timeout=min(0.1, remaining)):
                callback = self._server.callback
                if callback is None:
                    raise RuntimeError("OAuth 回调状态异常")
                if callback.error:
                    detail = callback.error_description or callback.error
                    raise RuntimeError(f"OpenAI 授权未完成：{detail}")
                if not callback.code:
                    raise RuntimeError("OAuth 回调缺少授权代码")
                return callback

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)

    def __enter__(self) -> "OAuthCallbackListener":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _oauth_error_message(raw: str, content_type: str, fallback: str = "") -> str:
    if raw.lstrip().lower().startswith(("<!doctype", "<html")):
        return (
            "OpenAI token 端点返回了 HTML 而不是 JSON"
            f"（Content-Type: {content_type or '未知'}）；请关闭旧授权页后重新登录"
        )
    try:
        payload = json.loads(raw)
    except Exception:
        payload = {}
    if isinstance(payload, dict):
        detail = payload.get("error_description") or payload.get("error")
        if isinstance(detail, dict):
            detail = detail.get("message") or detail.get("code")
        if detail:
            detail_text = str(detail)[:500]
            detail_low = detail_text.lower()
            if "unexpected token '<'" in detail_low and "doctype" in detail_low:
                return (
                    "OpenAI 授权服务返回了 HTML 而不是 JSON；"
                    "授权参数或登录会话已失效，请关闭旧授权页后重新登录"
                )
            return detail_text
    return raw[:500] or fallback or "未知错误"


def oauth_proxy_settings() -> dict[str, str]:
    """读取与 requests 兼容的系统/环境代理，但不把代理地址写入日志。"""
    found = requests.utils.get_environ_proxies(OAUTH_TOKEN_URL)
    return {
        key: str(value)
        for key, value in found.items()
        if key in {"http", "https", "all"} and value
    }


def create_oauth_http_session(
    proxies: dict[str, str] | None = None,
) -> requests.Session:
    """创建贯穿一次 OAuth 流程的持久会话，复用代理 CONNECT/TLS 连接池。"""
    route = oauth_proxy_settings() if proxies is None else dict(proxies)
    session = requests.Session()
    session.proxies.update(route)
    session.headers.update({"User-Agent": "codex-account-manager/1.0"})
    return session


def _oauth_tls_verify() -> bool | str:
    """兼容 Codex 官方的自定义 CA 环境变量。"""
    return (
        os.environ.get("CODEX_CA_CERTIFICATE")
        or os.environ.get("REQUESTS_CA_BUNDLE")
        or os.environ.get("CURL_CA_BUNDLE")
        or os.environ.get("SSL_CERT_FILE")
        or True
    )


def probe_oauth_network(
    *,
    proxies: dict[str, str] | None = None,
    session: requests.Session | None = None,
    target_url: str = OAUTH_AUTH_URL,
    timeout_sec: float = 8,
    attempts: int = 3,
) -> dict[str, Any]:
    """
    在打开浏览器前用无敏感数据探测 OAuth HTTPS 链路。

    GET 可安全重试；authorization code 的 POST 仍严格只发送一次。
    """
    route = oauth_proxy_settings() if proxies is None else dict(proxies)
    client = session if session is not None else requests
    last_error: BaseException | None = None
    for attempt in range(max(1, attempts)):
        try:
            response = client.get(
                target_url,
                headers={
                    "Accept": "text/html,application/json",
                    "User-Agent": "codex-account-manager/1.0",
                },
                proxies=route or None,
                verify=_oauth_tls_verify(),
                timeout=timeout_sec,
                allow_redirects=False,
            )
            try:
                if response.status_code == 407:
                    raise RuntimeError("系统代理需要额外认证（HTTP 407）")
                if response.status_code >= 500:
                    raise RuntimeError(f"OAuth 网关暂时不可用（HTTP {response.status_code}）")
                return {
                    "ok": True,
                    "status": response.status_code,
                    "via_proxy": bool(route),
                }
            finally:
                response.close()
        except (requests.RequestException, RuntimeError) as exc:
            last_error = exc
            if attempt + 1 < max(1, attempts):
                time.sleep(0.25 * (attempt + 1))
    raise RuntimeError(
        "OAuth 网络预检失败，尚未打开登录页："
        f"{type(last_error).__name__}: {last_error}"
    ) from last_error


def auth_from_token_response(token_response: dict[str, Any]) -> dict[str, Any]:
    """把 OAuth token 响应转换为 Codex auth.json 的兼容结构。"""
    if not isinstance(token_response, dict):
        raise RuntimeError("OAuth token 响应不是 JSON 对象")
    access_token = str(token_response.get("access_token") or "")
    refresh_token = str(token_response.get("refresh_token") or "")
    id_token = str(token_response.get("id_token") or "")
    if not access_token or not refresh_token or not id_token:
        raise RuntimeError("OAuth token 响应缺少 access_token、refresh_token 或 id_token")

    account_id = str(token_response.get("account_id") or "")
    for token in (id_token, access_token):
        if account_id:
            break
        claims = decode_jwt_payload(token)
        openai_auth = claims.get("https://api.openai.com/auth") or {}
        if isinstance(openai_auth, dict):
            account_id = str(
                openai_auth.get("chatgpt_account_id")
                or openai_auth.get("account_id")
                or ""
            )
        if not account_id:
            account_id = str(claims.get("chatgpt_account_id") or "")
    if not account_id:
        raise RuntimeError("OAuth token 中缺少 ChatGPT account_id，无法安全保存为独立账户")

    return {
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": id_token,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "account_id": account_id,
        },
        "last_refresh": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "auth_mode": "chatgpt",
    }


def _parse_oauth_token_http_response(
    status_code: int,
    raw: str,
    content_type: str,
    reason: str = "",
) -> dict[str, Any]:
    """统一校验 requests 与 Windows 原生通道返回的 token 响应。"""
    if not 200 <= status_code < 300:
        detail = _oauth_error_message(raw, content_type, reason)
        raise RuntimeError(f"OAuth token 交换失败（HTTP {status_code}）：{detail}")

    if raw.lstrip().lower().startswith(("<!doctype", "<html")):
        raise RuntimeError(
            "OAuth token 端点返回了 HTML 而不是 JSON"
            f"（Content-Type: {content_type or '未知'}）；请关闭旧授权页后重新登录"
        )
    try:
        token_response = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("OAuth token 端点返回了无效 JSON") from exc
    return auth_from_token_response(token_response)


def _is_proxy_connect_failure_before_request(exc: BaseException) -> bool:
    """仅识别尚未建立代理连接、因此请求体尚未发送的 ProxyError。"""
    if not isinstance(exc, requests.exceptions.ProxyError):
        return False
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "cannot connect to proxy",
            "unable to connect to proxy",
            "failed to establish a new connection",
        )
    )


def _exception_chain_has_proxy_connect_failure(exc: BaseException) -> bool:
    """检查包装后的异常链，供无敏感数据的网络探测自动选路。"""
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if _is_proxy_connect_failure_before_request(current):
            return True
        current = current.__cause__ or current.__context__
    return False


def _find_windows_powershell() -> str | None:
    if os.name != "nt":
        return None
    return shutil.which("powershell.exe") or shutil.which("pwsh.exe")


def _windows_native_oauth_request(
    *,
    method: str,
    url: str,
    timeout_sec: float,
    body: str = "",
    content_type: str = "",
    warmup_url: str = "",
    warmup_attempts: int = 1,
) -> tuple[int, str, str, str]:
    """
    通过 Windows 原生 HTTPS 栈执行 OAuth 请求并返回 HTTP 结果。

    请求参数只从子进程 stdin 传入；token 表单不会出现在命令行或临时文件中。
    """
    powershell = _find_windows_powershell()
    if not powershell:
        raise RuntimeError("找不到 Windows PowerShell，无法启用原生 HTTPS 备用通道")

    native_timeout = max(1, min(300, round(timeout_sec)))
    script = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$request = [Console]::In.ReadToEnd() | ConvertFrom-Json
$warmupOk = $true
$warmupError = ''
if ([string]$request.warmup_url) {
    $warmupOk = $false
    $warmup = @{
        UseBasicParsing = $true
        Method = 'GET'
        Uri = [string]$request.warmup_url
        Headers = @{'Accept'='application/json'; 'User-Agent'='codex-account-manager/1.0'}
        TimeoutSec = [int]$request.warmup_timeout
    }
    for ($attempt = 0; $attempt -lt [int]$request.warmup_attempts; $attempt++) {
        try {
            $warmupResponse = Invoke-WebRequest @warmup
            $warmupOk = $true
            break
        } catch {
            $warmupFailure = $_
            # HTTP 错误响应仍证明代理 CONNECT、TLS 和 HTTP 通道已经建立。
            if ($null -ne $warmupFailure.Exception.Response) {
                $warmupOk = $true
                try { $warmupFailure.Exception.Response.Close() } catch {}
                break
            }
            $warmupError = [string]$warmupFailure.Exception.Message
            if ($attempt + 1 -lt [int]$request.warmup_attempts) {
                Start-Sleep -Milliseconds (200 * ($attempt + 1))
            }
        }
    }
}
if (-not $warmupOk) {
    $envelope = [ordered]@{
        transport_ok = $false
        status = 0
        content_type = ''
        reason = "Windows 原生 HTTPS 预热失败: $warmupError"
        body = ''
    }
    [Console]::Out.Write(($envelope | ConvertTo-Json -Compress -Depth 4))
    exit 0
}
$invoke = @{
    UseBasicParsing = $true
    Method = [string]$request.method
    Uri = [string]$request.url
    Headers = @{'Accept'='application/json'; 'User-Agent'='codex-account-manager/1.0'}
    TimeoutSec = [int]$request.timeout
}
if ([string]$request.body) { $invoke.Body = [string]$request.body }
if ([string]$request.content_type) { $invoke.ContentType = [string]$request.content_type }
try {
    $response = Invoke-WebRequest @invoke
    $envelope = [ordered]@{
        transport_ok = $true
        status = [int]$response.StatusCode
        content_type = [string]$response.Headers['Content-Type']
        reason = [string]$response.StatusDescription
        body = [string]$response.Content
    }
} catch {
    $failure = $_
    $status = 0
    $contentType = ''
    if ($null -ne $failure.Exception.Response) {
        try { $status = [int]$failure.Exception.Response.StatusCode } catch {}
        try { $contentType = [string]$failure.Exception.Response.Headers['Content-Type'] } catch {}
    }
    $raw = ''
    if ($null -ne $failure.ErrorDetails -and $failure.ErrorDetails.Message) {
        $raw = [string]$failure.ErrorDetails.Message
    }
    if (-not $raw) { $raw = [string]$failure.Exception.Message }
    $envelope = [ordered]@{
        transport_ok = ($status -gt 0)
        status = $status
        content_type = $contentType
        reason = [string]$failure.Exception.Message
        body = $raw
    }
}
[Console]::Out.Write(($envelope | ConvertTo-Json -Compress -Depth 4))
"""

    try:
        completed = subprocess.run(
            [
                powershell,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            input=json.dumps(
                {
                    "method": method,
                    "url": url,
                    "timeout": native_timeout,
                    "body": body,
                    "content_type": content_type,
                    "warmup_url": warmup_url,
                    "warmup_timeout": min(5, native_timeout),
                    "warmup_attempts": max(1, warmup_attempts),
                }
            ),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_sec + (min(5, native_timeout) * max(1, warmup_attempts)) + 10,
            creationflags=subprocess.CREATE_NO_WINDOW,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Windows 原生 HTTPS 请求超时") from exc
    except OSError as exc:
        raise RuntimeError(f"无法启动 Windows 原生 HTTPS 备用通道：{exc}") from exc

    output = completed.stdout.strip().lstrip("\ufeff")
    if completed.returncode != 0:
        detail = (completed.stderr or output or "未知错误").strip()[:500]
        raise RuntimeError(f"Windows 原生 HTTPS 备用通道启动失败：{detail}")
    try:
        envelope = json.loads(output)
    except json.JSONDecodeError as exc:
        detail = (completed.stderr or output or "无输出").strip()[:500]
        raise RuntimeError(f"Windows 原生 HTTPS 备用通道返回无效结果：{detail}") from exc
    if not isinstance(envelope, dict):
        raise RuntimeError("Windows 原生 HTTPS 备用通道返回格式错误")

    status_code = int(envelope.get("status") or 0)
    raw = str(envelope.get("body") or "")
    content_type = str(envelope.get("content_type") or "")
    reason = str(envelope.get("reason") or "")
    if not envelope.get("transport_ok") or status_code <= 0:
        raise RuntimeError(f"Windows 原生 HTTPS 网络失败：{reason or raw or '未知错误'}")
    return status_code, raw, content_type, reason


def probe_oauth_network_windows_native(
    *,
    timeout_sec: float = 8,
) -> dict[str, Any]:
    """以无敏感数据的 GET 验证 Windows 原生 token 网络通道。"""
    status_code, _raw, _content_type, _reason = _windows_native_oauth_request(
        method="GET",
        url=OAUTH_TOKEN_URL,
        timeout_sec=timeout_sec,
    )
    if status_code == 407:
        raise RuntimeError("系统代理需要额外认证（HTTP 407）")
    if status_code >= 500:
        raise RuntimeError(f"OAuth 网关暂时不可用（HTTP {status_code}）")
    return {
        "ok": True,
        "status": status_code,
        "via_proxy": True,
        "transport": "windows_native",
    }


def select_oauth_token_transport(
    *,
    proxies: dict[str, str] | None = None,
    session: requests.Session | None = None,
    timeout_sec: float = 5,
    attempts: int = 1,
) -> dict[str, Any]:
    """安全探测 token 端点；代理 CONNECT 失败时自动选择 Windows 原生栈。"""
    route = oauth_proxy_settings() if proxies is None else dict(proxies)
    try:
        result = probe_oauth_network(
            proxies=route,
            session=session,
            target_url=OAUTH_TOKEN_URL,
            timeout_sec=timeout_sec,
            attempts=attempts,
        )
        result["transport"] = "requests"
        return result
    except RuntimeError as exc:
        if not (
            route
            and _exception_chain_has_proxy_connect_failure(exc)
            and _find_windows_powershell()
        ):
            raise
        try:
            return probe_oauth_network_windows_native(timeout_sec=max(8, timeout_sec))
        except Exception as native_exc:
            raise RuntimeError(
                "requests 代理通道连接失败，且 Windows 原生 HTTPS 预检也失败："
                f"{native_exc}"
            ) from native_exc


def _exchange_oauth_code_windows_native(
    form: dict[str, str],
    *,
    timeout_sec: float,
) -> dict[str, Any]:
    """通过已验证的 Windows 原生 HTTPS 栈交换 token。"""
    status_code, raw, content_type, reason = _windows_native_oauth_request(
        method="POST",
        url=OAUTH_TOKEN_URL,
        timeout_sec=timeout_sec,
        body=urllib.parse.urlencode(form),
        content_type="application/x-www-form-urlencoded",
        warmup_url=OAUTH_TOKEN_URL,
        warmup_attempts=3,
    )
    return _parse_oauth_token_http_response(status_code, raw, content_type, reason)


def exchange_oauth_code(
    code: str,
    code_verifier: str,
    *,
    redirect_uri: str = OAUTH_REDIRECT_URI,
    timeout_sec: float = 30,
    proxies: dict[str, str] | None = None,
    session: requests.Session | None = None,
    log: LogFn | None = None,
    use_windows_native: bool = False,
) -> dict[str, Any]:
    """用 authorization code + PKCE verifier 单次交换 Codex token。"""
    if not code or not code_verifier:
        raise ValueError("authorization code 和 PKCE verifier 不能为空")
    form = {
        "grant_type": "authorization_code",
        "client_id": OAUTH_CLIENT_ID,
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }
    route = oauth_proxy_settings() if proxies is None else dict(proxies)
    if use_windows_native:
        _log("使用已通过预检的 Windows 原生 HTTPS 通道交换 token", log)
        return _exchange_oauth_code_windows_native(form, timeout_sec=timeout_sec)

    client = session if session is not None else requests
    # requests/urllib3 在 Windows HTTPS-over-proxy 上比 urllib 的 CONNECT 实现更兼容。
    # 此 POST 不配置自动重试，确保一次性 authorization code 最多提交一次。
    try:
        response = client.post(
            OAUTH_TOKEN_URL,
            data=form,
            headers={
                "Accept": "application/json",
                "User-Agent": "codex-account-manager/1.0",
            },
            proxies=route or None,
            verify=_oauth_tls_verify(),
            timeout=timeout_sec,
            allow_redirects=False,
        )
    except requests.exceptions.ProxyError as exc:
        if (
            route
            and _is_proxy_connect_failure_before_request(exc)
            and _find_windows_powershell()
        ):
            _log(
                "系统代理连接在提交 authorization code 前被重置；"
                "切换 Windows 原生 HTTPS 通道…",
                log,
            )
            try:
                return _exchange_oauth_code_windows_native(
                    form,
                    timeout_sec=timeout_sec,
                )
            except Exception as native_exc:
                raise RuntimeError(
                    "OAuth token 交换网络失败；代理连接未建立，且 Windows 原生备用通道失败："
                    f"{native_exc}"
                ) from native_exc
        raise RuntimeError(
            "OAuth token 交换网络失败；authorization code 未自动重试，请重新登录："
            f"{type(exc).__name__}: {exc}"
        ) from exc
    except requests.Timeout as exc:
        raise RuntimeError("OAuth token 交换超时；authorization code 未自动重试，请重新登录") from exc
    except requests.RequestException as exc:
        raise RuntimeError(
            "OAuth token 交换网络失败；authorization code 未自动重试，请重新登录："
            f"{type(exc).__name__}: {exc}"
        ) from exc

    raw = response.text
    content_type = str(response.headers.get("Content-Type") or "")
    status_code = int(response.status_code)
    reason = str(response.reason or "")
    response.close()
    return _parse_oauth_token_http_response(status_code, raw, content_type, reason)


def oauth_login_and_save(
    *,
    mgr: CodexAccountManager | None = None,
    open_browser: bool = True,
    timeout_sec: float = 300,
    log: LogFn | None = None,
    name: str | None = None,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """
    完成本地 PKCE OAuth，并把凭据保存到账户库。

    全程不执行 ``codex login``，也不写当前 Codex 的 auth.json/active_id。
    """
    mgr = mgr or CodexAccountManager()
    listener: OAuthCallbackListener | None = None
    http_session: requests.Session | None = None
    oauth_url = ""
    stage = "准备授权"
    try:
        stage = "检查 OAuth 网络"
        proxies = oauth_proxy_settings()
        http_session = create_oauth_http_session(proxies)
        network = probe_oauth_network(proxies=proxies, session=http_session)
        route_label = "系统代理" if network.get("via_proxy") else "直连"
        _log(f"OAuth 网络预检通过（{route_label}）", log)

        state = generate_oauth_state()
        pkce = generate_pkce_codes()
        listener = OAuthCallbackListener(state)
        oauth_url = build_authorization_url(state, pkce.challenge)

        _log("使用本地 PKCE OAuth；不会启动 codex login 或覆盖当前 Codex 账户", log)
        _log(f"本地回调仅监听 {CALLBACK_BIND_HOST}:{CALLBACK_PORT}", log)
        stage = "打开授权页"
        if open_browser:
            try:
                how = open_private_browser(oauth_url, log=log)
                _log(f"授权页打开方式：{how}", log)
            except Exception as exc:
                _log(f"自动打开无痕浏览器失败：{exc}", log)
                _log(f"请手动在无痕窗口打开：{oauth_url}", log)
        else:
            _log(f"授权链接：{oauth_url}", log)

        stage = "等待浏览器回调"
        callback = listener.wait(timeout_sec=timeout_sec, cancel_event=cancel_event)
        if cancel_event and cancel_event.is_set():
            raise InterruptedError("用户取消了 OAuth 登录")
        _log("已收到并校验本地 OAuth 回调，正在确认 token 网络通道…", log)
        stage = "确认 OAuth token 网络"
        token_network = select_oauth_token_transport(
            proxies=proxies,
            session=http_session,
        )
        use_windows_native = token_network.get("transport") == "windows_native"
        if use_windows_native:
            _log(
                "requests 代理通道不稳定；Windows 原生 HTTPS 预检通过，"
                "将用原生通道交换凭据…",
                log,
            )
        else:
            _log("OAuth token 网络通道已确认，正在交换凭据…", log)
        stage = "交换 OAuth token"
        auth = exchange_oauth_code(
            callback.code,
            pkce.verifier,
            proxies=proxies,
            session=http_session,
            log=log,
            use_windows_native=use_windows_native,
        )
        if cancel_event and cancel_event.is_set():
            raise InterruptedError("用户取消了 OAuth 登录")

        info = extract_account_info(auth)
        _log(f"OAuth 完成：{info.get('email') or '(未知邮箱)'}", log)
        stage = "保存账户"
        profile = mgr.import_auth_dict(
            auth,
            name=name,
            note="本地 PKCE OAuth 登录获取",
            make_active=False,
        )
        _log(
            f"已保存：{profile.email or profile.name}  "
            f"套餐 {(profile.plan or '?').upper()}  · 当前 Codex 登录保持不变",
            log,
        )
        return {
            "ok": True,
            "profile_id": profile.id,
            "email": profile.email,
            "name": profile.name,
            "plan": profile.plan,
            "oauth_url": oauth_url,
            "flow": "pkce_loopback",
            "stage": "完成",
            "live_auth_unchanged": True,
        }
    except InterruptedError as exc:
        _log(str(exc), log)
        return {
            "ok": False,
            "cancelled": True,
            "stage": stage,
            "error": str(exc),
        }
    except Exception as exc:
        _log(f"OAuth 失败（{stage}）：{exc}", log)
        return {
            "ok": False,
            "stage": stage,
            "error": str(exc),
            "oauth_url": oauth_url,
        }
    finally:
        if listener is not None:
            listener.close()
        if http_session is not None:
            http_session.close()
