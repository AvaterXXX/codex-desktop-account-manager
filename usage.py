#!/usr/bin/env python3
"""
查询 Codex / ChatGPT 订阅用量，并安全刷新 OAuth token。

Token 状态机原则：
- 仅 access 过期/临近过期，或 usage 返回明确 401/403 时才 refresh
- refresh POST 不做多代理重试（避免 single-use refresh 被打爆）
- 调用方必须在 refresh 成功后立刻原子落盘
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable

from manager import decode_jwt_payload

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
REFRESH_URL = "https://auth.openai.com/oauth/token"
USAGE_URL = "https://chatgpt.com/backend-api/codex/usage"
REFRESH_SKEW_SECONDS = 120

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)

# 成功过的代理缓存，避免每次启动扫一堆死端口 + 长超时
_cached_proxy: dict[str, str] | None | object = object()  # sentinel=未探测


def _env_proxy() -> dict[str, str]:
    http = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
    https = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or http
    if http or https:
        return {"http": http or https or "", "https": https or http or ""}
    return {}


def _primary_proxy() -> dict[str, str]:
    """refresh 用：环境变量，否则空（直连）。不再默认死连 7897。"""
    return _env_proxy()


def _usage_proxies() -> list[dict[str, str]]:
    """
    GET 候选代理：
    1) 上次成功的缓存
    2) 环境变量
    3) 直连
    4) 本机常见端口（仅端口 open 才加入，避免 25s 超时卡死）
    """
    global _cached_proxy
    items: list[dict[str, str]] = []
    if _cached_proxy is not object and _cached_proxy is not None:
        items.append(_cached_proxy)  # type: ignore[arg-type]
    elif _cached_proxy is None:
        pass  # 已知直连可用
    env = _env_proxy()
    if env and env not in items:
        items.append(env)
    # 探测本机端口是否在听（毫秒级），只有开着才试
    import socket

    for port in (7897, 7890, 10809, 1080):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.15):
                url = f"http://127.0.0.1:{port}"
                entry = {"http": url, "https": url}
                if entry not in items:
                    items.append(entry)
        except OSError:
            continue
    if {} not in items:
        items.append({})
    return items


def _remember_proxy(proxy: dict[str, str]) -> None:
    global _cached_proxy
    _cached_proxy = dict(proxy) if proxy else None


def _make_opener(proxy: dict[str, str]) -> urllib.request.OpenerDirector:
    if proxy:
        handler = urllib.request.ProxyHandler({k: v for k, v in proxy.items() if v})
    else:
        handler = urllib.request.ProxyHandler({})
    return urllib.request.build_opener(handler)


class HttpStatusError(RuntimeError):
    def __init__(self, code: int, url: str, body: str = ""):
        self.code = code
        self.url = url
        self.body = body
        super().__init__(f"HTTP {code} {url}: {body[:500]}")


def _http_once(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    timeout: float = 12,
    proxy: dict[str, str] | None = None,
) -> dict[str, Any]:
    """单次请求，不重试。"""
    data = None
    hdrs = {
        "Accept": "application/json",
        "User-Agent": DEFAULT_UA,
    }
    if headers:
        hdrs.update(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    opener = _make_opener(proxy or {})
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            if not raw:
                return {}
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        raise HttpStatusError(e.code, url, err_body) from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"网络错误 {url}: {e.reason}") from e


def _http_get_with_proxy_fallback(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 10,
    max_proxy_tries: int = 3,
) -> dict[str, Any]:
    """GET：可换代理，但对每个代理短超时；成功则缓存代理。"""
    last_err: Exception | None = None
    for i, proxy in enumerate(_usage_proxies()):
        if i >= max_proxy_tries:
            break
        try:
            data = _http_once("GET", url, headers=headers, timeout=timeout, proxy=proxy)
            _remember_proxy(proxy)
            return data
        except HttpStatusError as e:
            if e.code in (401, 403):
                # 鉴权失败也说明网络通了，缓存该代理
                _remember_proxy(proxy)
                raise
            last_err = e
        except Exception as e:
            last_err = e
    raise last_err or RuntimeError(f"请求失败 {url}")


def access_token_exp(access_token: str) -> int | None:
    claims = decode_jwt_payload(access_token)
    exp = claims.get("exp")
    try:
        return int(exp) if exp is not None else None
    except Exception:
        return None


def needs_refresh(auth: dict[str, Any], skew: int = REFRESH_SKEW_SECONDS) -> bool:
    tokens = auth.get("tokens") if isinstance(auth.get("tokens"), dict) else {}
    access = tokens.get("access_token") or ""
    if not access:
        return bool(tokens.get("refresh_token"))
    exp = access_token_exp(access)
    if exp is None:
        return False
    return time.time() >= (exp - skew)


def refresh_auth_tokens(auth: dict[str, Any]) -> dict[str, Any]:
    """
    用 refresh_token 换新 token。
    - 单次 POST（环境代理失败再直连一次，共最多 2 次）
    - 不做多端口重试风暴
    - 调用方必须立刻把返回值原子写盘
    """
    tokens = auth.get("tokens")
    if not isinstance(tokens, dict):
        raise RuntimeError("auth 中没有 tokens，无法刷新")
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        raise RuntimeError("没有 refresh_token，请重新登录该账户")

    body = {
        "client_id": CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    proxies_try = []
    primary = _primary_proxy()
    if primary:
        proxies_try.append(primary)
    if {} not in proxies_try:
        proxies_try.append({})

    last_err: Exception | None = None
    new: dict[str, Any] | None = None
    for proxy in proxies_try:
        try:
            new = _http_once(
                "POST",
                REFRESH_URL,
                headers={"Content-Type": "application/json"},
                body=body,
                timeout=12,
                proxy=proxy,
            )
            _remember_proxy(proxy)
            break
        except HttpStatusError as e:
            # 明确无效会话：不再换代理重试
            low = (e.body or "").lower()
            if e.code in (400, 401, 403) or "session has ended" in low or "invalid" in low:
                raise RuntimeError(str(e)) from e
            last_err = e
        except Exception as e:
            last_err = e
    if new is None:
        raise RuntimeError(f"Token 刷新失败: {last_err}")

    out = json.loads(json.dumps(auth))
    out_tokens = dict(out.get("tokens") or {})
    if new.get("access_token"):
        out_tokens["access_token"] = new["access_token"]
    if new.get("id_token"):
        out_tokens["id_token"] = new["id_token"]
    if new.get("refresh_token"):
        out_tokens["refresh_token"] = new["refresh_token"]
    # 补 account_id
    if not out_tokens.get("account_id"):
        claims = decode_jwt_payload(str(out_tokens.get("id_token") or ""))
        oa = claims.get("https://api.openai.com/auth") or {}
        if isinstance(oa, dict) and oa.get("chatgpt_account_id"):
            out_tokens["account_id"] = oa["chatgpt_account_id"]
    out["tokens"] = out_tokens
    out["last_refresh"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    out["auth_mode"] = out.get("auth_mode") or "chatgpt"
    return out


def ensure_fresh_auth(auth: dict[str, Any], force: bool = False) -> tuple[dict[str, Any], bool]:
    """
    仅在 force 或 access 临近过期时刷新。
    查限额请用 force=False，让 401 路径再触发 refresh。
    """
    tokens = auth.get("tokens") if isinstance(auth.get("tokens"), dict) else {}
    if not tokens.get("access_token") and not tokens.get("refresh_token"):
        return auth, False
    if force or needs_refresh(auth):
        return refresh_auth_tokens(auth), True
    return auth, False


def _account_id_from_auth(auth: dict[str, Any]) -> str:
    tokens = auth.get("tokens") if isinstance(auth.get("tokens"), dict) else {}
    aid = tokens.get("account_id") or ""
    if aid:
        return str(aid)
    claims = decode_jwt_payload(str(tokens.get("id_token") or ""))
    openai_auth = claims.get("https://api.openai.com/auth") or {}
    if isinstance(openai_auth, dict):
        return str(openai_auth.get("chatgpt_account_id") or "")
    return ""


def fetch_codex_usage(auth: dict[str, Any]) -> dict[str, Any]:
    tokens = auth.get("tokens") if isinstance(auth.get("tokens"), dict) else {}
    access = tokens.get("access_token")
    if not access:
        raise RuntimeError("该账户不是 ChatGPT OAuth（无 access_token），无法查询周限额")
    account_id = _account_id_from_auth(auth)
    headers = {
        "Authorization": f"Bearer {access}",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/codex",
    }
    if account_id:
        headers["ChatGPT-Account-Id"] = str(account_id)
        headers["ChatGPT-Account-ID"] = str(account_id)
    return _http_get_with_proxy_fallback(USAGE_URL, headers=headers, timeout=25)


def is_auth_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    if isinstance(exc, HttpStatusError) and exc.code in (401, 403):
        return True
    keys = (
        "http 401",
        "http 403",
        "token_invalidated",
        "token_revoked",
        "invalid_grant",
        "session has ended",
        "unauthorized",
        "authentication",
    )
    return any(k in text for k in keys)


def fetch_usage_with_auth_repair(
    auth: dict[str, Any],
    *,
    on_refreshed: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    """
    查用量：
    1) 直接 GET
    2) 仅当明确鉴权失败 (401/403 等) 时 refresh 一次
    3) refresh 成功后立刻 on_refreshed(auth) 落盘，再 GET
    返回 (usage, auth, refreshed)
    """
    try:
        return fetch_codex_usage(auth), auth, False
    except Exception as first:
        if not is_auth_error(first):
            # 网络/5xx：绝不刷 token
            raise RuntimeError(
                f"限额查询失败（未刷新凭证）: {first} "
                f"（请确认代理可用）"
            ) from first
        try:
            fresh = refresh_auth_tokens(auth)
        except Exception as refresh_err:
            raise RuntimeError(f"{first}；续期失败: {refresh_err}") from first
        # 必须先落盘，再二次请求
        if on_refreshed is not None:
            on_refreshed(fresh)
        try:
            usage = fetch_codex_usage(fresh)
            return usage, fresh, True
        except Exception as second:
            raise RuntimeError(
                f"限额查询失败: {second}（凭证已刷新并落盘；请检查网络/代理）"
            ) from second


def parse_usage_summary(usage: dict[str, Any]) -> dict[str, Any]:
    """把 usage API 结果整理成 UI 字段；优先用周窗口。"""
    rate = usage.get("rate_limit") if isinstance(usage.get("rate_limit"), dict) else {}
    primary = rate.get("primary_window") if isinstance(rate.get("primary_window"), dict) else {}
    secondary = rate.get("secondary_window") if isinstance(rate.get("secondary_window"), dict) else {}

    def _window_label(sec: int | None) -> str:
        if sec is None:
            return "额度"
        if sec >= 500000:
            return "周"
        if sec >= 14000:
            return "5小时"
        return f"{max(1, sec // 3600)}小时"

    def _parse_window(win: dict[str, Any]) -> dict[str, Any]:
        used = win.get("used_percent")
        try:
            used_f = float(used) if used is not None else None
        except Exception:
            used_f = None
        left_f = None if used_f is None else max(0.0, min(100.0, 100.0 - used_f))
        if bool(rate.get("limit_reached")) and left_f is not None:
            left_f = 0.0
        if left_f is not None and used_f is not None and used_f >= 99.5:
            left_f = 0.0
        try:
            window_sec_i = int(win["limit_window_seconds"]) if win.get("limit_window_seconds") is not None else None
        except Exception:
            window_sec_i = None
        reset_at = win.get("reset_at")
        reset_iso = ""
        if isinstance(reset_at, (int, float)):
            reset_iso = datetime.fromtimestamp(reset_at, tz=timezone.utc).isoformat()
        elif isinstance(reset_at, str):
            reset_iso = reset_at
        try:
            reset_after_i = int(win["reset_after_seconds"]) if win.get("reset_after_seconds") is not None else None
        except Exception:
            reset_after_i = None
        return {
            "used": used_f,
            "left": left_f,
            "window_sec": window_sec_i,
            "label": _window_label(window_sec_i),
            "reset_at": reset_iso,
            "reset_after": reset_after_i,
        }

    p = _parse_window(primary) if primary else {
        "used": None, "left": None, "window_sec": None, "label": "额度",
        "reset_at": "", "reset_after": None,
    }
    s = _parse_window(secondary) if secondary else {
        "used": None, "left": None, "window_sec": None, "label": "",
        "reset_at": "", "reset_after": None,
    }

    # 展示用窗口：优先「周」(>=5天)，否则 primary
    display = p
    if s.get("window_sec") and s["window_sec"] >= 500000:
        display = s
    elif p.get("window_sec") and p["window_sec"] < 500000 and s.get("window_sec") and s["window_sec"] >= 500000:
        display = s

    credits = usage.get("credits") if isinstance(usage.get("credits"), dict) else {}
    resets = (
        usage.get("rate_limit_reset_credits")
        if isinstance(usage.get("rate_limit_reset_credits"), dict)
        else {}
    )

    return {
        "email": usage.get("email") or "",
        "plan_type": usage.get("plan_type") or "",
        "allowed": bool(rate.get("allowed", True)),
        "limit_reached": bool(rate.get("limit_reached", False)) or (
            display.get("left") is not None and display["left"] <= 0
        ),
        "week_used_percent": display.get("used"),
        "week_left_percent": display.get("left"),
        "week_window_label": display.get("label") or "额度",
        "week_window_seconds": display.get("window_sec"),
        "week_reset_at": display.get("reset_at") or "",
        "week_reset_after_seconds": display.get("reset_after"),
        "primary_used_percent": p.get("used"),
        "primary_left_percent": p.get("left"),
        "primary_window_label": p.get("label"),
        "secondary_used_percent": s.get("used"),
        "secondary_left_percent": s.get("left"),
        "secondary_reset_at": s.get("reset_at") or "",
        "credits_balance": str(credits.get("balance") if credits.get("balance") is not None else ""),
        "reset_credits_available": resets.get("available_count"),
        "fetched_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "raw": usage,
    }


def format_reset_local(reset_iso: str) -> str:
    if not reset_iso:
        return ""
    try:
        if reset_iso.endswith("Z"):
            dt = datetime.fromisoformat(reset_iso.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(reset_iso)
        return dt.astimezone().strftime("%m-%d %H:%M")
    except Exception:
        return reset_iso[:16]


def format_left_text(summary: dict[str, Any] | None, error: str = "") -> str:
    if error:
        return "限额: 查询失败"
    if not summary:
        return "限额: 未查询"
    left = summary.get("week_left_percent")
    if left is None:
        return "限额: 无数据"
    label = summary.get("week_window_label") or "额度"
    reset = format_reset_local(str(summary.get("week_reset_at") or ""))
    if summary.get("limit_reached") or left <= 0:
        base = f"{label}已用尽"
    else:
        base = f"{label}剩余 {left:.0f}%"
    if reset:
        base += f" · 重置 {reset}"
    return base


def left_color(left: float | None) -> str:
    if left is None:
        return "#6B7280"
    if left <= 10:
        return "#DC2626"
    if left <= 40:
        return "#D97706"
    return "#059669"
