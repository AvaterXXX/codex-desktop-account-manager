#!/usr/bin/env python3
"""从 ~/.codex/backups 扫描并导入历史账户快照。"""
from __future__ import annotations

import json
from pathlib import Path

from manager import CodexAccountManager, extract_account_info


def iter_auth_candidates(codex_home: Path) -> list[Path]:
    files: list[Path] = []
    live = codex_home / "auth.json"
    if live.exists():
        files.append(live)
    backups = codex_home / "backups"
    if backups.exists():
        files.extend(sorted(backups.rglob("auth.json")))
    # 常见手写备份
    for p in codex_home.glob("auth.json*"):
        if p not in files:
            files.append(p)
    return files


def is_junk_api_key(api_key: str) -> bool:
    if not api_key:
        return True
    bad = {"123", "sk-1", "null", "None", "YOUR_API_KEY", "xxx"}
    if api_key in bad:
        return True
    if len(api_key) < 12:
        return True
    return False


def recover(mgr: CodexAccountManager | None = None) -> list[dict]:
    mgr = mgr or CodexAccountManager()
    home = mgr.codex_home

    best: dict[str, tuple] = {}
    for path in iter_auth_candidates(home):
        try:
            auth = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(auth, dict):
            continue

        info = extract_account_info(auth)
        tokens = auth.get("tokens") if isinstance(auth.get("tokens"), dict) else {}
        api_key = auth.get("OPENAI_API_KEY") or ""
        has_refresh = bool(tokens.get("refresh_token"))
        has_access = bool(tokens.get("access_token"))

        if not has_refresh and not has_access and is_junk_api_key(str(api_key)):
            continue

        if info.get("account_id"):
            key = f"chatgpt:{info['account_id']}"
        elif api_key:
            key = f"apikey:{str(api_key)[-16:]}"
        else:
            continue

        score = (100 if has_refresh else 0) + (10 if has_access else 0) + min(path.stat().st_size, 5000) / 100.0
        mtime = path.stat().st_mtime
        prev = best.get(key)
        if prev is None or score > prev[0] or (score == prev[0] and mtime > prev[4]):
            best[key] = (score, auth, info, path, mtime)

    results = []
    for key, (_score, auth, info, path, _mtime) in sorted(best.items(), key=lambda x: -x[1][4]):
        email = info.get("email") or ""
        api = auth.get("OPENAI_API_KEY") or ""
        if email:
            name = email
        elif api:
            name = f"API Key …{str(api)[-8:]}"
        else:
            name = key

        parent = path.parent.name
        note = "当前登录" if path.name == "auth.json" and path.parent == home else f"历史备份: {parent}"

        profile = mgr.import_auth_file(path, name=name, note=note, make_active=False)
        # 若没有邮箱但有 api，补一个 plan 标记
        if not profile.auth_mode:
            profile.auth_mode = info.get("auth_mode") or ("chatgpt" if info.get("account_id") else "apikey")
            mgr.save_config()

        results.append(
            {
                "id": profile.id,
                "name": profile.name,
                "email": profile.email,
                "plan": profile.plan,
                "auth_mode": profile.auth_mode,
                "note": profile.note,
                "source": str(path),
            }
        )

    match = mgr.detect_active_match()
    if match:
        mgr.config.active_id = match.id
        mgr.save_config()

    return results


def main() -> None:
    items = recover()
    print(f"已导入/更新 {len(items)} 个可恢复账户：")
    for it in items:
        print(
            f"- {it['name']} | plan={it['plan'] or '-'} | mode={it['auth_mode'] or '-'} | {it['note']}"
        )


if __name__ == "__main__":
    main()
