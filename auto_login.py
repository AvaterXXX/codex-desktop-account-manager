#!/usr/bin/env python3
"""
用 邮箱|密码|TOTP密钥 自动走本地 PKCE OAuth，登录成功后保存账户。

流程：
1. 生成随机 state 与 PKCE，并在 127.0.0.1:1455 等待回调
2. Playwright 只打开一个隔离窗口，填写邮箱、密码和可选 2FA
3. 校验回调 state，用 code + verifier 直接交换 token
4. 导入账户管理器；默认不改当前 Codex 登录态
"""
from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pyotp
from playwright.sync_api import sync_playwright

from manager import CodexAccountManager, extract_account_info
from oauth_login import (
    OAuthCallbackListener,
    build_authorization_url,
    create_oauth_http_session,
    exchange_oauth_code,
    generate_oauth_state,
    generate_pkce_codes,
    oauth_proxy_settings,
    probe_oauth_network,
    select_oauth_token_transport,
)

LogFn = Callable[[str], None]


def _log(msg: str, log: LogFn | None) -> None:
    if log:
        log(msg)
    else:
        print(msg, flush=True)


@dataclass
class Credential:
    email: str
    password: str
    totp_secret: str = ""
    name: str = ""

    @property
    def label(self) -> str:
        return self.name or self.email


def parse_credentials_text(text: str) -> list[Credential]:
    """
    支持多行，每行：
      邮箱|密码|TOTP密钥
      邮箱|密码|TOTP密钥|备注名
    也兼容用逗号/制表符分隔；TOTP 可带空格。
    """
    items: list[Credential] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # 允许全角竖线
        line = line.replace("｜", "|").replace("，", ",")
        if "|" in line:
            parts = [p.strip() for p in line.split("|")]
        elif "\t" in line:
            parts = [p.strip() for p in line.split("\t")]
        else:
            parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            raise ValueError(f"格式错误（至少 邮箱|密码）：{raw}")
        email, password = parts[0], parts[1]
        totp = parts[2] if len(parts) >= 3 else ""
        name = parts[3] if len(parts) >= 4 else ""
        # TOTP secret 去掉空格
        totp = re.sub(r"\s+", "", totp)
        if not email or not password:
            raise ValueError(f"邮箱/密码不能为空：{raw}")
        items.append(Credential(email=email, password=password, totp_secret=totp, name=name))
    return items


def _fill_first(page, selectors: list[str], value: str, timeout: int = 8000) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            loc.wait_for(state="visible", timeout=timeout)
            loc.fill(value)
            return True
        except Exception:
            continue
    return False


def _click_first(page, selectors: list[str], timeout: int = 8000) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            loc.wait_for(state="visible", timeout=timeout)
            loc.click()
            return True
        except Exception:
            continue
    return False


def _page_has_text(page, texts: list[str]) -> bool:
    try:
        content = page.content().lower()
    except Exception:
        return False
    return any(t.lower() in content for t in texts)


def playwright_login(
    oauth_url: str,
    cred: Credential,
    *,
    headless: bool = False,
    timeout_sec: float = 180,
    log: LogFn | None = None,
) -> None:
    """在唯一的 Playwright 隔离窗口中完成账号和可选 TOTP 登录。"""
    _log(f"打开登录页：{cred.email}", log)
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            viewport={"width": 1100, "height": 860},
            locale="en-US",
        )
        page = context.new_page()
        page.goto(oauth_url, wait_until="domcontentloaded", timeout=60000)

        deadline = time.time() + timeout_sec
        email_done = password_done = totp_done = False
        while time.time() < deadline:
            url = page.url
            # 回调成功
            if "localhost:1455" in url or "/auth/callback" in url:
                _log("已到达 OAuth 回调页", log)
                # 给本地回调处理线程一点时间。
                time.sleep(0.5)
                break

            # Cloudflare / 人工
            if _page_has_text(page, ["verify you are human", "checking your browser", "cf-turnstile"]):
                _log("检测到人机验证，请在弹出的浏览器里手动完成…", log)
                time.sleep(2)
                continue

            # Email
            if not email_done:
                filled = _fill_first(
                    page,
                    [
                        'input[name="email"]',
                        'input[type="email"]',
                        'input[id*="email" i]',
                        'input[autocomplete="username"]',
                        'input[autocomplete="email"]',
                    ],
                    cred.email,
                    timeout=2500,
                )
                if filled:
                    _log("已填写邮箱", log)
                    clicked = _click_first(
                        page,
                        [
                            'button:has-text("Continue")',
                            'button:has-text("继续")',
                            'button[type="submit"]',
                            'input[type="submit"]',
                        ],
                        timeout=2500,
                    )
                    if not clicked:
                        page.keyboard.press("Enter")
                    email_done = True
                    time.sleep(1.2)
                    continue

            # Password
            if email_done and not password_done:
                filled = _fill_first(
                    page,
                    [
                        'input[name="password"]',
                        'input[type="password"]',
                        'input[autocomplete="current-password"]',
                    ],
                    cred.password,
                    timeout=2500,
                )
                if filled:
                    _log("已填写密码", log)
                    clicked = _click_first(
                        page,
                        [
                            'button:has-text("Continue")',
                            'button:has-text("Log in")',
                            'button:has-text("登录")',
                            'button:has-text("继续")',
                            'button[type="submit"]',
                        ],
                        timeout=2500,
                    )
                    if not clicked:
                        page.keyboard.press("Enter")
                    password_done = True
                    time.sleep(1.5)
                    continue

            # TOTP / 2FA
            if password_done and not totp_done:
                code_selectors = [
                    'input[name="code"]',
                    'input[autocomplete="one-time-code"]',
                    'input[inputmode="numeric"]',
                    'input[name="otp"]',
                    'input[placeholder*="code" i]',
                    'input[aria-label*="code" i]',
                ]
                # 是否出现 2FA 框
                has_code = False
                for sel in code_selectors:
                    try:
                        if page.locator(sel).first.is_visible(timeout=500):
                            has_code = True
                            break
                    except Exception:
                        pass
                if has_code:
                    if not cred.totp_secret:
                        _log("需要 2FA，但未提供 TOTP 密钥，请手动输入…", log)
                        # 等用户手动
                        time.sleep(2)
                        continue
                    code = pyotp.TOTP(cred.totp_secret).now()
                    if _fill_first(page, code_selectors, code, timeout=2500):
                        _log("已填写 2FA 验证码", log)
                        clicked = _click_first(
                            page,
                            [
                                'button:has-text("Continue")',
                                'button:has-text("Verify")',
                                'button:has-text("验证")',
                                'button:has-text("继续")',
                                'button[type="submit"]',
                            ],
                            timeout=2500,
                        )
                        if not clicked:
                            page.keyboard.press("Enter")
                        totp_done = True
                        time.sleep(1.5)
                        continue

            # 授权同意页
            if _click_first(
                page,
                [
                    'button:has-text("Allow")',
                    'button:has-text("Accept")',
                    'button:has-text("Continue")',
                    'button:has-text("授权")',
                    'button:has-text("允许")',
                    'button:has-text("继续")',
                ],
                timeout=800,
            ):
                _log("已点击授权/继续", log)
                time.sleep(1)
                continue

            # 错误提示
            if _page_has_text(
                page,
                [
                    "incorrect email or password",
                    "wrong email or password",
                    "invalid code",
                    "too many attempts",
                ],
            ):
                raise RuntimeError(f"登录失败：账号/密码/验证码可能不正确（{cred.email}）")

            time.sleep(0.6)
        else:
            browser.close()
            raise TimeoutError(f"登录超时（{timeout_sec}s）：{cred.email}")

        # 再等一下看是否出现 success 文案
        try:
            page.wait_for_timeout(800)
        except Exception:
            pass
        browser.close()


def login_one(
    cred: Credential,
    *,
    mgr: CodexAccountManager | None = None,
    codex_cli: Path | None = None,
    headless: bool = False,
    keep_as_active: bool = False,
    log: LogFn | None = None,
) -> dict:
    """通过本地 PKCE OAuth 登录单个账户并保存到账户库。"""
    mgr = mgr or CodexAccountManager()
    # 保留参数以兼容旧调用方；PKCE 流程不再启动 Codex CLI。
    _ = codex_cli
    listener: OAuthCallbackListener | None = None
    http_session = None
    try:
        _log(f"=== 开始登录 {cred.label} ===", log)
        proxies = oauth_proxy_settings()
        http_session = create_oauth_http_session(proxies)
        network = probe_oauth_network(proxies=proxies, session=http_session)
        route_label = "系统代理" if network.get("via_proxy") else "直连"
        _log(f"OAuth 网络预检通过（{route_label}）", log)
        state = generate_oauth_state()
        pkce = generate_pkce_codes()
        listener = OAuthCallbackListener(state)
        oauth_url = build_authorization_url(state, pkce.challenge)
        _log("使用本地 PKCE OAuth；当前 Codex 登录不会被覆盖", log)
        _log("授权链接已生成，启动唯一的隔离浏览器窗口…", log)

        playwright_login(
            oauth_url,
            cred,
            headless=headless,
            log=log,
        )
        callback = listener.wait(timeout_sec=90)
        _log("本地回调校验通过，正在确认 token 网络通道…", log)
        token_network = select_oauth_token_transport(
            proxies=proxies,
            session=http_session,
        )
        use_windows_native = token_network.get("transport") == "windows_native"
        if use_windows_native:
            _log(
                "requests 代理通道不稳定；Windows 原生 HTTPS 预检通过，"
                "将用原生通道交换 token…",
                log,
            )
        else:
            _log("OAuth token 网络通道已确认，正在交换 token…", log)
        data = exchange_oauth_code(
            callback.code,
            pkce.verifier,
            proxies=proxies,
            session=http_session,
            log=log,
            use_windows_native=use_windows_native,
        )

        name = cred.name or cred.email
        profile = mgr.import_auth_dict(
            data,
            name=name,
            note="自动 PKCE OAuth 登录获取",
            make_active=False,
        )
        # 强制用填写邮箱修正显示（token 里一般也有）
        if not profile.email:
            profile.email = cred.email
            mgr.save_config()

        # 只有调用者明确要求保持为当前账户时才改 live auth；默认批量模式不改。
        if keep_as_active:
            mgr.switch_to(profile.id, restart=False)

        info = extract_account_info(data)
        _log(
            f"成功：{profile.display_title()} | plan={(profile.plan or info.get('plan') or '?').upper()}",
            log,
        )
        return {
            "ok": True,
            "profile_id": profile.id,
            "name": profile.name,
            "email": profile.email or cred.email,
            "plan": profile.plan or info.get("plan") or "",
            "live_auth_unchanged": not keep_as_active,
        }
    except Exception as e:
        _log(f"失败：{cred.email} → {e}", log)
        return {"ok": False, "email": cred.email, "error": str(e)}
    finally:
        if listener is not None:
            listener.close()
        if http_session is not None:
            http_session.close()


def login_batch(
    credentials: list[Credential],
    *,
    mgr: CodexAccountManager | None = None,
    headless: bool = False,
    restore_original: bool = True,
    log: LogFn | None = None,
) -> list[dict]:
    """批量登录。默认全程不修改当前 Codex auth.json。"""
    mgr = mgr or CodexAccountManager()

    results = []
    for i, cred in enumerate(credentials, 1):
        _log(f"\n[{i}/{len(credentials)}] {cred.email}", log)
        # 最后一个是否保持 active：若要 restore 则全部不当最终 active
        res = login_one(
            cred,
            mgr=mgr,
            headless=headless,
            keep_as_active=not restore_original,
            log=log,
        )
        results.append(res)
        time.sleep(1.0)

    if restore_original:
        _log("批量登录完成；当前 Codex 账户始终未改变", log)

    ok = sum(1 for r in results if r.get("ok"))
    _log(f"\n完成：成功 {ok}/{len(results)}", log)
    return results


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Codex 自动登录并保存 auth.json")
    parser.add_argument(
        "-f",
        "--file",
        help="凭据文件，每行 邮箱|密码|TOTP|备注",
    )
    parser.add_argument(
        "-e",
        "--email",
        help="单个邮箱",
    )
    parser.add_argument("-p", "--password", help="密码")
    parser.add_argument("-t", "--totp", default="", help="TOTP 密钥（不是6位码）")
    parser.add_argument("--headless", action="store_true", help="无头模式（易触发验证，不推荐）")
    parser.add_argument(
        "--no-restore",
        action="store_true",
        help="批量结束后不恢复原账户，保持最后一个登录",
    )
    args = parser.parse_args(argv)

    if args.file:
        text = Path(args.file).read_text(encoding="utf-8")
        creds = parse_credentials_text(text)
    elif args.email and args.password:
        creds = [Credential(email=args.email, password=args.password, totp_secret=args.totp or "")]
    else:
        print("请用 -f 凭据文件，或 -e/-p/-t 指定单个账户")
        print("格式：邮箱|密码|TOTP密钥|备注名")
        return 2

    results = login_batch(
        creds,
        headless=args.headless,
        restore_original=not args.no_restore,
    )
    return 0 if all(r.get("ok") for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
