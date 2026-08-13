#!/usr/bin/env python3
"""Codex 多账户核心逻辑：auth.json 快照、切换、重启桌面端。"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from atomic_io import atomic_write_json, atomic_write_text

log = logging.getLogger("codex_account_manager")

# Codex Desktop (Windows Store / AppX) 相关进程名
CODEX_PROCESS_NAMES = (
    "ChatGPT",
    "Codex",
    "codex",
    "codex-code-mode-host",
    "codex-command-runner",
)

# 不杀这些（避免误伤）
PROTECTED_PROCESS_NAMES = {
    "codex-plus-plus",
    "codex-plus-plus-manager",
}

# 每账户刷新锁
_profile_locks: dict[str, threading.RLock] = {}
_profile_locks_guard = threading.Lock()


def _profile_lock(profile_id: str) -> threading.RLock:
    with _profile_locks_guard:
        if profile_id not in _profile_locks:
            _profile_locks[profile_id] = threading.RLock()
        return _profile_locks[profile_id]


def default_codex_home() -> Path:
    env = os.environ.get("CODEX_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".codex"


def default_store_dir() -> Path:
    return Path.home() / ".codex-account-manager"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _b64url_json(segment: str) -> dict[str, Any]:
    pad = "=" * ((4 - len(segment) % 4) % 4)
    raw = base64.urlsafe_b64decode(segment + pad)
    return json.loads(raw.decode("utf-8"))


def decode_jwt_payload(token: str | None) -> dict[str, Any]:
    if not token or not isinstance(token, str):
        return {}
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    try:
        return _b64url_json(parts[1])
    except Exception:
        return {}


def extract_account_info(auth: dict[str, Any]) -> dict[str, Any]:
    """从 auth.json 解析邮箱、套餐等信息（尽量完整，不抛异常）。"""
    tokens = auth.get("tokens") or {}
    if not isinstance(tokens, dict):
        tokens = {}

    id_claims = decode_jwt_payload(tokens.get("id_token"))
    access_claims = decode_jwt_payload(tokens.get("access_token"))

    openai_auth = id_claims.get("https://api.openai.com/auth") or {}
    if not isinstance(openai_auth, dict):
        openai_auth = {}

    email = id_claims.get("email") or access_claims.get("email") or ""
    name = id_claims.get("name") or access_claims.get("name") or ""
    plan = openai_auth.get("chatgpt_plan_type") or ""
    account_id = (
        tokens.get("account_id")
        or openai_auth.get("chatgpt_account_id")
        or ""
    )
    user_id = openai_auth.get("chatgpt_user_id") or ""
    sub_until = openai_auth.get("chatgpt_subscription_active_until") or ""

    auth_mode = auth.get("auth_mode") or ""
    if not auth_mode and auth.get("OPENAI_API_KEY"):
        auth_mode = "api_key"

    return {
        "email": email,
        "name": name,
        "plan": plan,
        "account_id": account_id,
        "user_id": user_id,
        "subscription_until": sub_until,
        "auth_mode": auth_mode,
        "last_refresh": auth.get("last_refresh") or "",
        "identity_key": account_identity_key(auth),
    }


def api_key_fingerprint(api_key: str) -> str:
    raw = (api_key or "").strip()
    if not raw:
        return ""
    return "apikey:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def account_identity_key(auth: dict[str, Any]) -> str:
    """
    稳定身份键：
    - ChatGPT: chatgpt:<account_id>
    - API Key: apikey:<sha256 前缀>
    - 其它: 空
    禁止用邮箱当唯一键（同邮箱多 workspace 会冲突）。
    """
    tokens = auth.get("tokens") if isinstance(auth.get("tokens"), dict) else {}
    info_account = ""
    if tokens.get("account_id"):
        info_account = str(tokens.get("account_id"))
    else:
        claims = decode_jwt_payload(str(tokens.get("id_token") or ""))
        oa = claims.get("https://api.openai.com/auth") or {}
        if isinstance(oa, dict):
            info_account = str(oa.get("chatgpt_account_id") or "")
    if info_account:
        return f"chatgpt:{info_account}"
    key = auth.get("OPENAI_API_KEY")
    if key:
        return api_key_fingerprint(str(key))
    return ""


def mask_email(email: str) -> str:
    if not email or "@" not in email:
        return email or "(未知)"
    local, domain = email.split("@", 1)
    if len(local) <= 2:
        masked = local[0] + "*" if local else "*"
    else:
        masked = local[0] + "***" + local[-1]
    return f"{masked}@{domain}"


@dataclass
class AccountProfile:
    id: str
    name: str
    email: str = ""
    plan: str = ""
    account_id: str = ""
    auth_mode: str = ""
    note: str = ""
    created_at: str = ""
    updated_at: str = ""
    last_used_at: str = ""
    # 用量缓存（来自 backend-api/codex/usage）
    week_left_percent: float | None = None
    week_used_percent: float | None = None
    week_reset_at: str = ""
    week_window_label: str = ""
    week_window_seconds: int | None = None
    usage_fetched_at: str = ""
    usage_error: str = ""
    credits_balance: str = ""
    limit_reached: bool = False
    # auth 健康状态：ok / expired / invalid / api_key / unknown
    auth_status: str = "unknown"
    auth_status_msg: str = ""
    auth_checked_at: str = ""
    token_exp_at: str = ""
    # 稳定身份键 chatgpt:<id> / apikey:<hash>
    identity_key: str = ""

    def display_title(self) -> str:
        return self.name or self.email or self.id[:8]

    def subtitle(self) -> str:
        bits = []
        if self.email and self.email != self.name:
            bits.append(self.email)
        if self.plan:
            bits.append(str(self.plan).upper())
        if self.auth_mode:
            bits.append(self.auth_mode)
        return " · ".join(bits) if bits else self.account_id[:12]

    def usage_line(self) -> str:
        if self.usage_error:
            return f"限额查询失败: {self.usage_error[:40]}"
        if self.week_left_percent is None and not self.usage_fetched_at:
            if (self.auth_mode or "").lower() in ("apikey", "api_key"):
                return "API Key 模式（无订阅周限额）"
            return "限额: 未查询"
        if self.limit_reached:
            label = self.week_window_label or "周"
            return f"{label}限额已用尽"
        if self.week_left_percent is None:
            return "限额: 无数据"
        label = self.week_window_label or "周"
        return f"{label}剩余 {self.week_left_percent:.0f}%"

    def auth_badge(self) -> tuple[str, str]:
        """返回 (短标签, 颜色提示: ok/warn/bad/muted)。"""
        s = (self.auth_status or "unknown").lower()
        if s == "ok":
            return "有效", "ok"
        if s == "api_key":
            return "密钥", "muted"
        if s == "expired":
            return "已过期", "warn"
        if s == "invalid":
            return "已失效", "bad"
        return "未检测", "muted"


def quota_window_bounds(
    profile: AccountProfile,
    *,
    now_epoch: float | None = None,
) -> tuple[float, float] | None:
    """返回当前限额周期的 (开始, 重置) epoch；过期或字段不足时返回 None。"""
    reset_text = str(profile.week_reset_at or "").strip()
    if not reset_text:
        return None
    try:
        reset_epoch = datetime.fromisoformat(
            reset_text.replace("Z", "+00:00")
        ).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None

    seconds = profile.week_window_seconds
    try:
        window_seconds = int(seconds) if seconds is not None else 0
    except (TypeError, ValueError):
        window_seconds = 0

    # 兼容升级前保存的配置：旧数据只有“周/5小时”标签。
    if window_seconds <= 0:
        label = str(profile.week_window_label or "")
        if "周" in label:
            window_seconds = 7 * 24 * 60 * 60
        elif "小时" in label:
            try:
                hours = float(label.split("小时", 1)[0])
                window_seconds = int(hours * 60 * 60)
            except (TypeError, ValueError):
                window_seconds = 0
    if window_seconds <= 0:
        return None

    start_epoch = reset_epoch - window_seconds
    now = time.time() if now_epoch is None else float(now_epoch)
    # 缓存周期已经结束，或本机时间明显不在该周期内时，不展示成“本期”。
    if now < start_epoch or now >= reset_epoch:
        return None
    return start_epoch, reset_epoch


@dataclass
class AppConfig:
    active_id: str | None = None
    codex_home: str = ""
    auto_restart: bool = True
    profiles: list[AccountProfile] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "active_id": self.active_id,
            "codex_home": self.codex_home,
            "auto_restart": self.auto_restart,
            "profiles": [asdict(p) for p in self.profiles],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppConfig":
        profiles = []
        for item in data.get("profiles") or []:
            if not isinstance(item, dict):
                continue
            week_left = item.get("week_left_percent")
            week_used = item.get("week_used_percent")
            try:
                week_left_f = float(week_left) if week_left is not None else None
            except Exception:
                week_left_f = None
            try:
                week_used_f = float(week_used) if week_used is not None else None
            except Exception:
                week_used_f = None
            week_seconds = item.get("week_window_seconds")
            try:
                week_seconds_i = (
                    int(week_seconds) if week_seconds is not None else None
                )
                if week_seconds_i is not None and week_seconds_i <= 0:
                    week_seconds_i = None
            except Exception:
                week_seconds_i = None
            profiles.append(
                AccountProfile(
                    id=str(item.get("id") or uuid.uuid4()),
                    name=str(item.get("name") or ""),
                    email=str(item.get("email") or ""),
                    plan=str(item.get("plan") or ""),
                    account_id=str(item.get("account_id") or ""),
                    auth_mode=str(item.get("auth_mode") or ""),
                    note=str(item.get("note") or ""),
                    created_at=str(item.get("created_at") or ""),
                    updated_at=str(item.get("updated_at") or ""),
                    last_used_at=str(item.get("last_used_at") or ""),
                    week_left_percent=week_left_f,
                    week_used_percent=week_used_f,
                    week_reset_at=str(item.get("week_reset_at") or ""),
                    week_window_label=str(item.get("week_window_label") or ""),
                    week_window_seconds=week_seconds_i,
                    usage_fetched_at=str(item.get("usage_fetched_at") or ""),
                    usage_error=str(item.get("usage_error") or ""),
                    credits_balance=str(item.get("credits_balance") or ""),
                    limit_reached=bool(item.get("limit_reached", False)),
                    auth_status=str(item.get("auth_status") or "unknown"),
                    auth_status_msg=str(item.get("auth_status_msg") or ""),
                    auth_checked_at=str(item.get("auth_checked_at") or ""),
                    token_exp_at=str(item.get("token_exp_at") or ""),
                    identity_key=str(item.get("identity_key") or ""),
                )
            )
        return cls(
            active_id=data.get("active_id"),
            codex_home=str(data.get("codex_home") or ""),
            auto_restart=bool(data.get("auto_restart", True)),
            profiles=profiles,
        )


class CodexAccountManager:
    """管理 ~/.codex/auth.json 的多账户快照，并可选重启 Codex 桌面端。"""

    def __init__(self, store_dir: Path | None = None, codex_home: Path | None = None):
        self.store_dir = Path(store_dir) if store_dir else default_store_dir()
        self.profiles_dir = self.store_dir / "profiles"
        self.config_path = self.store_dir / "config.json"
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self.profiles_dir.mkdir(parents=True, exist_ok=True)

        self.config = self._load_config()
        if codex_home is not None:
            self.config.codex_home = str(codex_home)
        elif not self.config.codex_home:
            self.config.codex_home = str(default_codex_home())
        self._token_store = None

    # ---------- paths ----------
    @property
    def codex_home(self) -> Path:
        return Path(self.config.codex_home).expanduser()

    @property
    def auth_path(self) -> Path:
        return self.codex_home / "auth.json"

    def profile_auth_path(self, profile_id: str) -> Path:
        return self.profiles_dir / profile_id / "auth.json"

    def token_store(self):
        """懒加载 token 用量库。"""
        if self._token_store is None:
            from token_usage import TokenUsageStore

            self._token_store = TokenUsageStore(
                self.store_dir,
                sessions_dir=self.codex_home / "sessions",
            )
        return self._token_store

    def profile_fallback_list(self) -> list[dict[str, str]]:
        return [
            {
                "profile_id": p.id,
                "account_id": p.account_id or "",
                "email": p.email or "",
                "auth_mode": p.auth_mode or "",
            }
            for p in self.config.profiles
        ]

    def sync_live_account_into_store(self, source: str = "open") -> AccountProfile | None:
        """
        打开/刷新时：读取当前 ~/.codex/auth.json，
        - 已存在则更新快照并标为当前
        - 不存在则自动导入
        - 若账户相对上次有变化，写入用量归属时间线
        """
        auth = self.read_live_auth()
        if not auth:
            return None
        info = extract_account_info(auth)
        if not info.get("account_id") and not info.get("email") and not auth.get("OPENAI_API_KEY"):
            return None

        prev = self.detect_active_match() or self.get_active_profile()

        # 保存/更新快照（只按 identity_key 合并）
        name = None
        identity = info.get("identity_key") or account_identity_key(auth)
        existing = self._find_profile_by_identity(identity) if identity else None
        if existing:
            name = existing.name
        profile = self.save_current_as(name=name, make_active=True)

        def _id_tuple(p: AccountProfile | None) -> tuple[str, str]:
            if not p:
                return ("", "")
            return (
                (p.identity_key or p.account_id or ""),
                (p.email or ""),
            )

        changed = prev is None or _id_tuple(prev) != _id_tuple(profile)
        if changed:
            try:
                self.token_store().log_switch(
                    profile_id=profile.id,
                    account_id=profile.account_id or "",
                    email=profile.email or "",
                    source=source if source != "open" else "detect-change",
                )
            except Exception:
                pass
        else:
            # 未变也确保有 baseline / 活跃记录
            try:
                self.log_account_active(profile, source=source)
            except Exception:
                pass
        return profile

    def log_account_active(self, profile: AccountProfile | None = None, source: str = "active") -> None:
        """记录当前活跃账户到切换时间线（用于用量归属）。仅在账户变化或无记录时写入。"""
        store = self.token_store()
        p = profile or self.get_active_profile() or self.detect_active_match()
        if p is None:
            info = self.live_account_info()
            if not info.get("exists"):
                return
            profile_id, account_id, email = "", str(info.get("account_id") or ""), str(info.get("email") or "")
        else:
            profile_id, account_id, email = p.id, p.account_id or "", p.email or ""

        # 无记录 → baseline；有记录且账户未变 → 跳过
        switches = store._load_switches()
        if not switches:
            store.log_switch(
                profile_id=profile_id,
                account_id=account_id,
                email=email,
                source="baseline" if source == "startup" else source,
            )
            return
        last = switches[-1]
        same = (
            (last["account_id"] or "") == account_id
            and (last["profile_id"] or "") == profile_id
            and (last["email"] or "") == email
        )
        if same and source in ("startup", "active", "baseline"):
            return
        store.log_switch(
            profile_id=profile_id,
            account_id=account_id,
            email=email,
            source=source,
        )

    def sync_token_usage(
        self,
        reattribute: bool = True,
        *,
        full: bool = False,
        recent_days: int = 14,
    ) -> dict[str, Any]:
        """从 Codex sessions 增量同步 token 用量。默认只扫近 14 天、最多 40 个文件。"""
        store = self.token_store()
        # 确保有基线
        match = self.detect_active_match() or self.get_active_profile()
        if match:
            store.ensure_baseline_switch(
                profile_id=match.id,
                account_id=match.account_id or "",
                email=match.email or "",
            )
        else:
            info = self.live_account_info()
            if info.get("exists"):
                store.ensure_baseline_switch(
                    account_id=str(info.get("account_id") or ""),
                    email=str(info.get("email") or ""),
                )
        result = store.sync_sessions(
            fallback_profiles=self.profile_fallback_list(),
            full=full,
            recent_days=None if full else recent_days,
            max_files=None if full else 40,
        )
        if reattribute:
            # 严格按切换时间线重算归属，避免「单账户吞掉全部历史」
            attr = store.reattribute_all(self.profile_fallback_list())
            result["reattributed"] = attr.get("updated", 0)
            result["cleared_to_unknown"] = attr.get("cleared_to_unknown", 0)
        result["attribution"] = store.count_stats()
        return result

    def get_profile_token_usage(self, profile_id: str) -> dict[str, Any]:
        profile = self.get_profile(profile_id)
        if not profile:
            raise KeyError("账户不存在")
        return self.token_store().summarize(
            profile_id=profile.id,
            account_id=profile.account_id or "",
            email=profile.email or "",
        )

    # ---------- config ----------
    def _load_config(self) -> AppConfig:
        if not self.config_path.exists():
            return AppConfig()
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                log.warning("config.json 根类型不是 object，忽略")
                return AppConfig()
            return AppConfig.from_dict(data)
        except Exception as e:
            # 损坏时备份，避免静默清空后用户无感知
            try:
                bak = self.config_path.with_suffix(".json.corrupt")
                shutil.copy2(self.config_path, bak)
                log.error("config.json 损坏已备份到 %s: %s", bak, e)
            except Exception:
                log.error("config.json 解析失败: %s", e)
            return AppConfig()

    def save_config(self) -> None:
        atomic_write_json(self.config_path, self.config.to_dict())

    def list_profiles(self) -> list[AccountProfile]:
        return list(self.config.profiles)

    def get_profile(self, profile_id: str) -> AccountProfile | None:
        for p in self.config.profiles:
            if p.id == profile_id:
                return p
        return None

    def get_active_profile(self) -> AccountProfile | None:
        if not self.config.active_id:
            return None
        return self.get_profile(self.config.active_id)

    # ---------- auth read/write ----------
    def read_live_auth(self) -> dict[str, Any] | None:
        if not self.auth_path.exists():
            return None
        try:
            data = json.loads(self.auth_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def write_live_auth(self, auth: dict[str, Any]) -> None:
        self.codex_home.mkdir(parents=True, exist_ok=True)
        if self.auth_path.exists():
            bak = self.auth_path.with_suffix(".json.bak-switcher")
            try:
                shutil.copy2(self.auth_path, bak)
            except Exception as e:
                log.warning("备份 live auth 失败: %s", e)
        atomic_write_json(self.auth_path, auth)

    def live_account_info(self) -> dict[str, Any]:
        auth = self.read_live_auth()
        if not auth:
            return {
                "email": "",
                "name": "",
                "plan": "",
                "account_id": "",
                "user_id": "",
                "subscription_until": "",
                "auth_mode": "",
                "last_refresh": "",
                "exists": False,
            }
        info = extract_account_info(auth)
        info["exists"] = True
        return info

    def _read_profile_auth(self, profile_id: str) -> dict[str, Any]:
        path = self.profile_auth_path(profile_id)
        if not path.exists():
            raise FileNotFoundError(f"账户凭证不存在: {path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("凭证文件格式无效")
        return data

    def _write_profile_auth(self, profile_id: str, auth: dict[str, Any]) -> None:
        folder = self.profiles_dir / profile_id
        folder.mkdir(parents=True, exist_ok=True)
        atomic_write_json(folder / "auth.json", auth)

    def _find_profile_by_identity(self, identity_key: str) -> AccountProfile | None:
        if not identity_key:
            return None
        for p in self.config.profiles:
            if p.identity_key and p.identity_key == identity_key:
                return p
            # 兼容旧数据：无 identity_key 时用 account_id / apikey 后缀反推
            if identity_key.startswith("chatgpt:") and p.account_id:
                if f"chatgpt:{p.account_id}" == identity_key:
                    return p
        return None

    # ---------- CRUD ----------
    def save_current_as(
        self,
        name: str | None = None,
        note: str = "",
        make_active: bool = True,
    ) -> AccountProfile:
        """把当前 ~/.codex/auth.json 保存为命名账户。"""
        auth = self.read_live_auth()
        if not auth:
            raise FileNotFoundError(f"未找到当前登录文件: {self.auth_path}")

        info = extract_account_info(auth)
        identity = info.get("identity_key") or account_identity_key(auth)
        existing = self._find_profile_by_identity(identity)

        now = _utc_now_iso()
        if existing:
            profile = existing
            if name:
                profile.name = name
            profile.email = info.get("email") or profile.email
            profile.plan = info.get("plan") or profile.plan
            profile.account_id = info.get("account_id") or profile.account_id
            profile.auth_mode = info.get("auth_mode") or profile.auth_mode
            profile.identity_key = identity or profile.identity_key
            if note:
                profile.note = note
            profile.updated_at = now
            self._write_profile_auth(profile.id, auth)
        else:
            pid = uuid.uuid4().hex
            default_name = name or info.get("email") or info.get("name") or f"账户-{pid[:6]}"
            if not info.get("email") and auth.get("OPENAI_API_KEY"):
                key = str(auth.get("OPENAI_API_KEY"))
                default_name = name or f"API Key …{key[-8:]}"
            profile = AccountProfile(
                id=pid,
                name=default_name,
                email=info.get("email") or "",
                plan=info.get("plan") or "",
                account_id=info.get("account_id") or "",
                auth_mode=info.get("auth_mode") or "",
                note=note or "",
                created_at=now,
                updated_at=now,
                identity_key=identity,
            )
            self.config.profiles.append(profile)
            self._write_profile_auth(profile.id, auth)

        if make_active:
            self.config.active_id = profile.id
            profile.last_used_at = now
        self.save_config()
        return profile

    def import_auth_dict(
        self,
        auth: dict[str, Any],
        name: str | None = None,
        note: str = "",
        make_active: bool = False,
    ) -> AccountProfile:
        """从内存中的 auth 对象导入/更新账户。只按 identity_key 合并。"""
        if not isinstance(auth, dict):
            raise ValueError("auth.json 格式无效")
        if "tokens" not in auth and not auth.get("OPENAI_API_KEY"):
            raise ValueError("看起来不是 Codex 的 auth.json（缺少 tokens / API Key）")

        info = extract_account_info(auth)
        identity = info.get("identity_key") or account_identity_key(auth)
        if not identity:
            raise ValueError("无法识别账户身份（缺少 account_id / API Key）")

        now = _utc_now_iso()
        existing = self._find_profile_by_identity(identity)

        if existing:
            profile = existing
            if name:
                profile.name = name
            profile.email = info.get("email") or profile.email
            profile.plan = info.get("plan") or profile.plan
            profile.account_id = info.get("account_id") or profile.account_id
            profile.auth_mode = info.get("auth_mode") or profile.auth_mode
            profile.identity_key = identity
            if note:
                profile.note = note
            profile.updated_at = now
            self._write_profile_auth(profile.id, auth)
        else:
            pid = uuid.uuid4().hex
            default_name = name or info.get("email") or info.get("name") or f"导入-{pid[:6]}"
            if not info.get("email") and auth.get("OPENAI_API_KEY"):
                key = str(auth.get("OPENAI_API_KEY"))
                default_name = name or f"API Key …{key[-8:]}"
            profile = AccountProfile(
                id=pid,
                name=default_name,
                email=info.get("email") or "",
                plan=info.get("plan") or "",
                account_id=info.get("account_id") or "",
                auth_mode=info.get("auth_mode") or "",
                note=note or "",
                created_at=now,
                updated_at=now,
                identity_key=identity,
            )
            self.config.profiles.append(profile)
            self._write_profile_auth(profile.id, auth)

        if make_active:
            self.config.active_id = profile.id
            profile.last_used_at = now
        self.save_config()
        return profile

    def import_auth_file(
        self,
        path: Path | str,
        name: str | None = None,
        note: str = "",
        make_active: bool = False,
    ) -> AccountProfile:
        src = Path(path)
        auth = json.loads(src.read_text(encoding="utf-8"))
        note2 = note or f"导入文件: {src.name}"
        return self.import_auth_dict(auth, name=name, note=note2, make_active=make_active)

    def import_auth_files(
        self,
        paths: list[Path | str],
        note_prefix: str = "批量导入",
    ) -> list[AccountProfile]:
        results: list[AccountProfile] = []
        for path in paths:
            p = Path(path)
            profile = self.import_auth_file(p, note=f"{note_prefix}: {p.name}")
            results.append(profile)
        return results

    def import_auth_text(self, text: str, note: str = "粘贴导入") -> list[AccountProfile]:
        """
        支持：
        - 单个 auth.json 对象
        - JSON 数组 [ {...}, {...} ]
        - 多段 JSON 用 --- 或空行分隔
        """
        raw = text.strip()
        if not raw:
            return []

        payloads: list[dict[str, Any]] = []

        # 先尝试整体解析
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                payloads = [data]
            elif isinstance(data, list):
                payloads = [x for x in data if isinstance(x, dict)]
            else:
                raise ValueError("JSON 必须是对象或对象数组")
        except json.JSONDecodeError:
            # 分段
            chunks = []
            for part in raw.replace("\r\n", "\n").split("\n---\n"):
                part = part.strip()
                if part:
                    chunks.append(part)
            if len(chunks) == 1:
                # 再按双空行切
                chunks = [c.strip() for c in raw.split("\n\n") if c.strip()]
            for chunk in chunks:
                obj = json.loads(chunk)
                if not isinstance(obj, dict):
                    raise ValueError("每一段都必须是 auth.json 对象")
                payloads.append(obj)

        results = []
        for i, auth in enumerate(payloads, 1):
            results.append(self.import_auth_dict(auth, note=f"{note} #{i}"))
        return results

    def apply_usage_to_profile(self, profile: AccountProfile, summary: dict[str, Any], error: str = "") -> None:
        if error:
            profile.usage_error = error
            profile.usage_fetched_at = _utc_now_iso()
            # 查询失败时清空旧剩余，避免假的「周剩 3%」误导
            profile.week_left_percent = None
            profile.week_used_percent = None
            profile.limit_reached = False
            return
        profile.usage_error = ""
        left = summary.get("week_left_percent")
        used = summary.get("week_used_percent")
        try:
            left_f = float(left) if left is not None else None
        except Exception:
            left_f = None
        try:
            used_f = float(used) if used is not None else None
        except Exception:
            used_f = None
        reached = bool(summary.get("limit_reached", False))
        if reached and left_f is not None:
            left_f = 0.0
        if left_f is not None and left_f < 0.5:
            left_f = 0.0
            reached = True
        profile.week_left_percent = left_f
        profile.week_used_percent = used_f
        profile.week_reset_at = str(summary.get("week_reset_at") or "")
        profile.week_window_label = str(summary.get("week_window_label") or "")
        window_seconds = summary.get("week_window_seconds")
        try:
            profile.week_window_seconds = (
                int(window_seconds) if window_seconds is not None else None
            )
        except (TypeError, ValueError):
            profile.week_window_seconds = None
        profile.usage_fetched_at = str(summary.get("fetched_at") or _utc_now_iso())
        profile.credits_balance = str(summary.get("credits_balance") or "")
        profile.limit_reached = reached
        if summary.get("plan_type"):
            profile.plan = str(summary["plan_type"])
        if summary.get("email") and not profile.email:
            profile.email = str(summary["email"])

    def _is_api_key_auth(self, auth: dict[str, Any], profile: AccountProfile | None = None) -> bool:
        mode = ((profile.auth_mode if profile else "") or auth.get("auth_mode") or "").lower()
        tokens = auth.get("tokens") if isinstance(auth.get("tokens"), dict) else {}
        if mode in ("apikey", "api_key"):
            return True
        if not tokens.get("access_token") and not tokens.get("refresh_token") and auth.get("OPENAI_API_KEY"):
            return True
        return False

    def _sync_live_if_same_account(self, profile: AccountProfile, auth: dict[str, Any]) -> None:
        live = self.read_live_auth()
        if not live:
            return
        live_key = account_identity_key(live)
        prof_key = profile.identity_key or account_identity_key(auth)
        if live_key and prof_key and live_key == prof_key:
            self.write_live_auth(auth)

    def _set_token_exp(self, profile: AccountProfile, auth: dict[str, Any]) -> None:
        from usage import access_token_exp

        tokens = auth.get("tokens") if isinstance(auth.get("tokens"), dict) else {}
        exp = access_token_exp(str(tokens.get("access_token") or ""))
        if exp:
            profile.token_exp_at = datetime.fromtimestamp(exp, tz=timezone.utc).replace(microsecond=0).isoformat()
        else:
            profile.token_exp_at = ""

    def get_profile_auth_text(self, profile_id: str, pretty: bool = True) -> str:
        """返回账户 auth.json 文本，便于复制。"""
        auth = self._read_profile_auth(profile_id)
        if pretty:
            return json.dumps(auth, ensure_ascii=False, indent=2)
        return json.dumps(auth, ensure_ascii=False, separators=(",", ":"))

    def export_profile_auth(self, profile_id: str, dest: Path | str) -> Path:
        dest_path = Path(dest)
        text = self.get_profile_auth_text(profile_id, pretty=True)
        if not text.endswith("\n"):
            text += "\n"
        atomic_write_text(dest_path, text)
        return dest_path

    def check_or_refresh_profile_auth(
        self,
        profile_id: str,
        *,
        force: bool = False,
        also_usage: bool = True,
    ) -> dict[str, Any]:
        """
        检查并刷新单个账户 auth：
        - 过期/临近过期：自动 refresh_token
        - force=True：强制刷新
        - also_usage=True：顺带拉周限额；仅 401/过期时才 refresh，不因查限额而强制刷 token
        """
        from usage import (
            ensure_fresh_auth,
            fetch_usage_with_auth_repair,
            needs_refresh,
            parse_usage_summary,
        )

        profile = self.get_profile(profile_id)
        if not profile:
            raise KeyError("账户不存在")

        try:
            auth = self._read_profile_auth(profile_id)
        except Exception as e:
            profile.auth_status = "invalid"
            profile.auth_status_msg = f"凭证文件缺失: {e}"
            profile.auth_checked_at = _utc_now_iso()
            self.save_config()
            return {"ok": False, "profile_id": profile_id, "status": "invalid", "error": str(e)}

        if self._is_api_key_auth(auth, profile):
            profile.auth_status = "api_key"
            profile.auth_status_msg = "密钥模式"
            profile.auth_checked_at = _utc_now_iso()
            profile.usage_error = ""
            profile.week_window_label = "API"
            profile.week_left_percent = None
            profile.week_used_percent = None
            profile.week_reset_at = ""
            profile.week_window_seconds = None
            profile.usage_fetched_at = _utc_now_iso()
            self.save_config()
            return {"ok": True, "api_key": True, "profile_id": profile_id, "status": "api_key"}

        tokens = auth.get("tokens") if isinstance(auth.get("tokens"), dict) else {}
        if not tokens.get("refresh_token") and not tokens.get("access_token"):
            profile.auth_status = "invalid"
            profile.auth_status_msg = "缺少 access/refresh token"
            profile.auth_checked_at = _utc_now_iso()
            self.save_config()
            return {"ok": False, "profile_id": profile_id, "status": "invalid", "error": profile.auth_status_msg}

        was_expired = needs_refresh(auth)
        refreshed = False
        lock = _profile_lock(profile_id)
        try:
            with lock:
                # 仅过期或 force 时刷新；查限额本身不 force
                do_force = bool(force or was_expired)
                fresh, refreshed = ensure_fresh_auth(auth, force=do_force)
                if refreshed:
                    self._write_profile_auth(profile_id, fresh)
                    self._sync_live_if_same_account(profile, fresh)
                    auth = fresh
                    info = extract_account_info(auth)
                    profile.email = info.get("email") or profile.email
                    profile.plan = info.get("plan") or profile.plan
                    profile.account_id = info.get("account_id") or profile.account_id
                    profile.auth_mode = info.get("auth_mode") or profile.auth_mode or "chatgpt"
                    profile.identity_key = info.get("identity_key") or profile.identity_key
                    profile.updated_at = _utc_now_iso()

                self._set_token_exp(profile, auth)

                if also_usage:
                    def _persist(new_auth: dict[str, Any]) -> None:
                        self._write_profile_auth(profile_id, new_auth)
                        self._sync_live_if_same_account(profile, new_auth)

                    usage, auth2, repaired = fetch_usage_with_auth_repair(
                        auth, on_refreshed=_persist
                    )
                    if repaired:
                        refreshed = True
                        auth = auth2
                        info = extract_account_info(auth)
                        profile.email = info.get("email") or profile.email
                        profile.plan = info.get("plan") or profile.plan
                        profile.account_id = info.get("account_id") or profile.account_id
                        profile.identity_key = info.get("identity_key") or profile.identity_key
                        self._set_token_exp(profile, auth)
                    summary = parse_usage_summary(usage)
                    self.apply_usage_to_profile(profile, summary)

                profile.auth_status = "ok"
                if refreshed:
                    profile.auth_status_msg = "已自动刷新凭证"
                else:
                    profile.auth_status_msg = "凭证有效"
                profile.auth_checked_at = _utc_now_iso()
                self.save_config()
                return {
                    "ok": True,
                    "profile_id": profile_id,
                    "status": "ok",
                    "refreshed": refreshed,
                    "was_expired": was_expired,
                    "summary": {
                        "week_left_percent": profile.week_left_percent,
                        "week_used_percent": profile.week_used_percent,
                        "week_reset_at": profile.week_reset_at,
                        "limit_reached": profile.limit_reached,
                        "week_window_label": profile.week_window_label,
                        "week_window_seconds": profile.week_window_seconds,
                    },
                }
        except Exception as e:
            err = str(e)
            low = err.lower()
            if any(
                k in low
                for k in (
                    "refresh_token_expired",
                    "refresh_token_reused",
                    "refresh_token_invalidated",
                    "token_invalidated",
                    "token_revoked",
                    "invalid_grant",
                    "session has ended",
                    "your session has ended",
                )
            ):
                status = "invalid"
                msg = "凭证已失效：请在 Codex 用该邮箱重新登录，再打开本工具自动同步"
            elif was_expired or "401" in low or "unauthorized" in low:
                status = "expired" if was_expired else "invalid"
                msg = err[:160]
            else:
                if was_expired:
                    status = "expired"
                    msg = f"已过期且刷新失败: {err[:120]}"
                else:
                    # 网络失败：凭证未必坏
                    status = "ok" if not was_expired else "unknown"
                    msg = f"限额网络失败: {err[:120]}"

            profile.auth_status = status if status != "ok" else profile.auth_status or "ok"
            if status == "ok":
                profile.auth_status = "ok"
            profile.auth_status_msg = msg
            profile.auth_checked_at = _utc_now_iso()
            try:
                self._set_token_exp(profile, auth)
            except Exception:
                pass
            if also_usage:
                self.apply_usage_to_profile(profile, {}, error=err[:240])
            self.save_config()
            return {
                "ok": False,
                "profile_id": profile_id,
                "status": status,
                "error": err,
                "was_expired": was_expired,
                "refreshed": refreshed,
            }

    def refresh_profile_usage(self, profile_id: str, force_token_refresh: bool = False) -> dict[str, Any]:
        """刷新单个账户 token（如需要）并查询周限额。"""
        result = self.check_or_refresh_profile_auth(
            profile_id,
            force=force_token_refresh,
            also_usage=True,
        )
        # 兼容旧返回字段
        if result.get("api_key"):
            return result
        if result.get("ok"):
            return {
                "ok": True,
                "profile_id": profile_id,
                "summary": result.get("summary"),
                "refreshed": result.get("refreshed", False),
            }
        return {
            "ok": False,
            "profile_id": profile_id,
            "error": result.get("error") or result.get("status"),
        }

    def refresh_all_usage(self, force_token_refresh: bool = False) -> list[dict[str, Any]]:
        results = []
        # 当前登录账户优先
        live = self.detect_active_match()
        ordered = list(self.config.profiles)
        if live:
            ordered = [live] + [p for p in ordered if p.id != live.id]
        for p in ordered:
            results.append(self.refresh_profile_usage(p.id, force_token_refresh=force_token_refresh))
            time.sleep(0.35)  # 轻微节流，避免打太猛
        return results

    def check_or_refresh_all_auth(
        self,
        *,
        force: bool = False,
        also_usage: bool = True,
    ) -> list[dict[str, Any]]:
        """批量检查账户。默认不强制刷 token；仅过期/401 才 refresh。"""
        try:
            self.sync_live_account_into_store(source="pre-check")
        except Exception as e:
            log.warning("pre-check 同步 live 失败: %s", e)
        live = self.detect_active_match()
        ordered = list(self.config.profiles)
        if live:
            ordered = [live] + [p for p in ordered if p.id != live.id]

        results = []
        for p in ordered:
            results.append(
                self.check_or_refresh_profile_auth(
                    p.id,
                    force=force,  # 启动检查 force=False
                    also_usage=also_usage,
                )
            )
            time.sleep(0.2)
        return results

    def rename_profile(self, profile_id: str, new_name: str) -> AccountProfile:
        profile = self.get_profile(profile_id)
        if not profile:
            raise KeyError("账户不存在")
        profile.name = new_name.strip() or profile.name
        profile.updated_at = _utc_now_iso()
        self.save_config()
        return profile

    def update_note(self, profile_id: str, note: str) -> AccountProfile:
        profile = self.get_profile(profile_id)
        if not profile:
            raise KeyError("账户不存在")
        profile.note = note
        profile.updated_at = _utc_now_iso()
        self.save_config()
        return profile

    @staticmethod
    def _auth_file_identity(path: Path) -> str:
        """只读取身份指纹，不把 API Key/token 写入日志。"""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ""
        return account_identity_key(data) if isinstance(data, dict) else ""

    def delete_profile(
        self,
        profile_id: str,
        *,
        remove_live_credentials: bool = False,
        relaunch_codex: bool = False,
    ) -> dict[str, Any]:
        """
        删除账户管理器快照。

        ``remove_live_credentials`` 仅在 live auth 与目标身份严格匹配时生效；
        删除前会完全结束 Codex，避免退出阶段把 Key/token 写回来。
        这不会撤销 OpenAI 或第三方平台上的远端 API Key。
        """
        profile = self.get_profile(profile_id)
        if not profile:
            raise KeyError("账户不存在")

        folder = self.profiles_dir / profile_id
        snapshot_path = self.profile_auth_path(profile_id)
        target_identity = profile.identity_key or self._auth_file_identity(snapshot_path)
        live_identity = self._auth_file_identity(self.auth_path)
        live_matches = bool(target_identity and live_identity == target_identity)
        removed_live = False
        removed_backup = False
        stopped_for_delete = False
        restart_info: dict[str, Any] = {
            "restarted": False,
            "killed": [],
            "launched": False,
        }

        try:
            if remove_live_credentials and live_matches:
                stopped = self.stop_codex()
                restart_info.update(
                    {
                        "restarted": True,
                        "killed": stopped.get("killed") or [],
                        "remaining": stopped.get("remaining") or [],
                    }
                )
                if not stopped.get("stopped"):
                    remaining = stopped.get("remaining") or []
                    names = ", ".join(
                        str(item.get("name") or item.get("pid")) for item in remaining
                    )
                    raise RuntimeError(
                        "无法完全结束 Codex，已取消删除当前 Key"
                        + (f"（仍在运行：{names}）" if names else "")
                    )
                stopped_for_delete = True

                # 停止后再次校验身份，防止竞态删除了刚切换到的其他账户。
                if self._auth_file_identity(self.auth_path) == target_identity:
                    self.auth_path.unlink(missing_ok=True)
                    removed_live = True

                switch_backup = self.auth_path.with_suffix(".json.bak-switcher")
                if self._auth_file_identity(switch_backup) == target_identity:
                    switch_backup.unlink(missing_ok=True)
                    removed_backup = True

            self.config.profiles = [
                p for p in self.config.profiles if p.id != profile_id
            ]
            if self.config.active_id == profile_id:
                self.config.active_id = None
            if folder.exists():
                shutil.rmtree(folder)
            self.save_config()
        except Exception:
            if stopped_for_delete and relaunch_codex:
                self.launch_codex()
            raise

        if stopped_for_delete and relaunch_codex:
            launched = self.launch_codex()
            restart_info.update(
                {
                    "launched": bool(launched.get("ok")),
                    "launch": launched,
                }
            )

        return {
            "profile_id": profile_id,
            "deleted": True,
            "removed_live_credentials": removed_live,
            "removed_switch_backup": removed_backup,
            "remote_key_revoked": False,
            "restart": restart_info,
        }

    def sync_active_from_live(self) -> AccountProfile | None:
        """
        仅当 live auth 与某个已保存账户 identity 明确匹配时，回写 token。
        禁止：API Key live 覆盖 ChatGPT 档案；禁止仅凭 active_id 盲写。
        """
        auth = self.read_live_auth()
        if not auth:
            return None
        info = extract_account_info(auth)
        identity = info.get("identity_key") or account_identity_key(auth)
        if not identity:
            log.info("live auth 无稳定 identity，跳过 sync_active_from_live")
            return None

        target = self._find_profile_by_identity(identity)
        if target is None:
            return None

        self._write_profile_auth(target.id, auth)
        target.email = info.get("email") or target.email
        target.plan = info.get("plan") or target.plan
        target.account_id = info.get("account_id") or target.account_id
        target.auth_mode = info.get("auth_mode") or target.auth_mode
        target.identity_key = identity
        target.updated_at = _utc_now_iso()
        self.config.active_id = target.id
        self.save_config()
        return target

    # ---------- switch ----------
    def switch_to(
        self,
        profile_id: str,
        restart: bool | None = None,
        wait_after_kill: float = 1.2,
    ) -> dict[str, Any]:
        """
        切换到指定账户：
        1) 先把当前 live auth 回写到对应已保存账户（保留刷新后的 token）
        2) 自动重启时先完全结束旧 Codex，避免退出阶段把旧凭据写回来
        3) 写入并校验目标账户 auth.json
        4) 可选：重新启动 Codex 桌面端
        """
        profile = self.get_profile(profile_id)
        if not profile:
            raise KeyError("账户不存在")

        auth = self._read_profile_auth(profile_id)
        do_restart = self.config.auto_restart if restart is None else restart

        # 切换前尽量同步当前登录态，避免 token 丢失
        try:
            self.sync_active_from_live()
        except Exception:
            pass

        restart_info: dict[str, Any] = {"restarted": False, "killed": [], "launched": False}
        stopped_for_switch = False
        if do_restart:
            stopped = self.stop_codex(wait_after_kill=wait_after_kill)
            restart_info.update(
                {
                    "restarted": True,
                    "killed": stopped.get("killed") or [],
                    "remaining": stopped.get("remaining") or [],
                }
            )
            if not stopped.get("stopped"):
                remaining = stopped.get("remaining") or []
                names = ", ".join(str(item.get("name") or item.get("pid")) for item in remaining)
                raise RuntimeError(
                    "无法完全结束旧 Codex，已取消凭据写入"
                    + (f"（仍在运行：{names}）" if names else "")
                )
            stopped_for_switch = True

        try:
            self.write_live_auth(auth)
            written = self.read_live_auth() or {}
            expected_key = account_identity_key(auth)
            written_key = account_identity_key(written)
            if expected_key and written_key != expected_key:
                raise RuntimeError("目标凭据写入后校验失败，未启动 Codex")

            now = _utc_now_iso()
            profile.last_used_at = now
            profile.updated_at = now
            # 用目标凭证刷新元数据
            info = extract_account_info(auth)
            profile.email = info.get("email") or profile.email
            profile.plan = info.get("plan") or profile.plan
            profile.account_id = info.get("account_id") or profile.account_id
            profile.auth_mode = info.get("auth_mode") or profile.auth_mode
            self.config.active_id = profile.id
            self.save_config()

            # 记录切换时间线，后续 session token 可归属到该账户
            try:
                self.token_store().log_switch(
                    profile_id=profile.id,
                    account_id=profile.account_id or info.get("account_id") or "",
                    email=profile.email or info.get("email") or "",
                    source="switch",
                )
            except Exception:
                pass
        except Exception:
            # 已结束 Codex 后即使切换失败，也尽量恢复应用窗口供用户处理。
            if stopped_for_switch:
                self.launch_codex()
            raise

        if do_restart:
            launched = self.launch_codex()
            restart_info.update(
                {
                    "launched": bool(launched.get("ok")),
                    "launch": launched,
                }
            )

        return {
            "profile": profile,
            "info": info,
            "restart": restart_info,
            "live_verified": True,
        }

    # ---------- process control ----------
    def is_codex_running(self) -> bool:
        return bool(self._list_codex_pids())

    def _list_codex_pids(self) -> list[tuple[int, str]]:
        """返回 [(pid, name), ...]。用 tasklist 避免额外依赖。"""
        try:
            completed = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except Exception:
            return []

        found: list[tuple[int, str]] = []
        for line in completed.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            # "name.exe","pid","session","session#","mem"
            parts = [p.strip().strip('"') for p in line.split('","')]
            if not parts:
                continue
            name = parts[0]
            if name.endswith(".exe"):
                name = name[:-4]
            # 跳过保护进程
            if name.lower() in {n.lower() for n in PROTECTED_PROCESS_NAMES}:
                continue
            if name not in CODEX_PROCESS_NAMES:
                continue
            try:
                pid = int(parts[1])
            except Exception:
                continue
            found.append((pid, name))
        return found

    def kill_codex(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        pids = self._list_codex_pids()
        for pid, name in pids:
            ok = False
            err = ""
            try:
                r = subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="ignore",
                    check=False,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                ok = r.returncode == 0
                if not ok:
                    err = (r.stderr or r.stdout or "").strip()
            except Exception as e:
                err = str(e)
            results.append({"pid": pid, "name": name, "ok": ok, "error": err})
        return results

    def find_codex_launch_command(self) -> list[str] | None:
        """优先用 AppX AUMID 启动官方 Codex 桌面端。"""
        # 1) 动态解析 StartApps
        try:
            ps = (
                "Get-StartApps | Where-Object { "
                "$_.AppID -like 'OpenAI.Codex*' -or $_.Name -eq 'ChatGPT' -or $_.Name -eq 'Codex' "
                "} | Select-Object -First 1 -ExpandProperty AppID"
            )
            r = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    ps,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            app_id = (r.stdout or "").strip().splitlines()
            app_id = app_id[0].strip() if app_id else ""
            if app_id and "OpenAI" in app_id:
                return [
                    "explorer.exe",
                    f"shell:AppsFolder\\{app_id}",
                ]
        except Exception:
            pass

        # 2) 常见固定 AUMID
        fallback = "OpenAI.Codex_2p2nqsd0c76g0!App"
        return ["explorer.exe", f"shell:AppsFolder\\{fallback}"]

    def launch_codex(self) -> dict[str, Any]:
        cmd = self.find_codex_launch_command()
        if not cmd:
            return {"ok": False, "error": "找不到 Codex 启动入口", "cmd": None}
        try:
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            return {"ok": True, "error": "", "cmd": cmd}
        except Exception as e:
            return {"ok": False, "error": str(e), "cmd": cmd}

    def stop_codex(self, wait_after_kill: float = 1.2) -> dict[str, Any]:
        """完全结束 Codex，并返回残留进程；切换凭据必须在此步骤之后。"""
        killed = self.kill_codex()
        time.sleep(max(0.3, wait_after_kill))
        # 再扫一遍残留
        remaining_pairs = self._list_codex_pids()
        if remaining_pairs:
            killed.extend(self.kill_codex())
            time.sleep(0.8)
        remaining_pairs = self._list_codex_pids()
        remaining = [{"pid": pid, "name": name} for pid, name in remaining_pairs]
        return {
            "stopped": not remaining,
            "killed": killed,
            "remaining": remaining,
        }

    def restart_codex(self, wait_after_kill: float = 1.2) -> dict[str, Any]:
        stopped = self.stop_codex(wait_after_kill=wait_after_kill)
        launched = self.launch_codex()
        return {
            "restarted": True,
            "killed": stopped.get("killed") or [],
            "remaining": stopped.get("remaining") or [],
            "launched": bool(launched.get("ok")),
            "launch": launched,
        }

    def detect_active_match(self) -> AccountProfile | None:
        """根据 live auth 的 identity_key 匹配已保存账户（不用邮箱兜底合并）。"""
        auth = self.read_live_auth()
        if not auth:
            return None
        key = account_identity_key(auth)
        if key:
            hit = self._find_profile_by_identity(key)
            if hit:
                return hit
        # 兼容旧档案：仅 chatgpt account_id
        info = extract_account_info(auth)
        account_id = info.get("account_id") or ""
        if account_id:
            for p in self.config.profiles:
                if p.account_id and p.account_id == account_id:
                    return p
        return None
