#!/usr/bin/env python3
"""针对审查问题的单元测试（临时目录，不碰真实 ~/.codex）。"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import auto_login
import oauth_login
from main import CodexAccountApp
from atomic_io import (
    SingleInstanceLock,
    atomic_write_json,
    read_activation_token,
    signal_activation,
)
from manager import (
    AccountProfile,
    AppConfig,
    CodexAccountManager,
    account_identity_key,
    api_key_fingerprint,
    extract_account_info,
    quota_window_bounds,
)
from token_usage import TokenUsageStore
from usage import (
    HttpStatusError,
    fetch_usage_with_auth_repair,
    is_auth_error,
    left_color,
    needs_refresh,
    parse_usage_summary,
)


class TestAtomicIo(unittest.TestCase):
    def test_atomic_write_json(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a.json"
            atomic_write_json(p, {"x": 1})
            self.assertEqual(json.loads(p.read_text(encoding="utf-8"))["x"], 1)

    def test_single_instance_lock(self):
        with tempfile.TemporaryDirectory() as td:
            lock_path = Path(td) / "app.lock"
            a = SingleInstanceLock(lock_path)
            b = SingleInstanceLock(lock_path)
            self.assertTrue(a.acquire())
            self.assertFalse(b.acquire())
            a.release()
            self.assertTrue(b.acquire())
            b.release()

    def test_activation_signal_changes_each_time(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "activate.request"
            first = signal_activation(path)
            second = signal_activation(path)
            self.assertNotEqual(first, second)
            self.assertEqual(read_activation_token(path), second)


class TestIdentity(unittest.TestCase):
    def test_chatgpt_key(self):
        auth = {
            "auth_mode": "chatgpt",
            "tokens": {
                "account_id": "acc-1",
                "id_token": "",
                "access_token": "",
                "refresh_token": "rt",
            },
        }
        self.assertEqual(account_identity_key(auth), "chatgpt:acc-1")

    def test_apikey_key_stable(self):
        k1 = api_key_fingerprint("sk-test-aaa")
        k2 = api_key_fingerprint("sk-test-aaa")
        k3 = api_key_fingerprint("sk-test-bbb")
        self.assertEqual(k1, k2)
        self.assertNotEqual(k1, k3)
        self.assertTrue(k1.startswith("apikey:"))

    def test_no_email_merge_different_account_ids(self):
        with tempfile.TemporaryDirectory() as td:
            store = Path(td) / "store"
            home = Path(td) / "codex"
            home.mkdir()
            mgr = CodexAccountManager(store_dir=store, codex_home=home)
            a1 = {
                "auth_mode": "chatgpt",
                "tokens": {
                    "account_id": "id-1",
                    "access_token": "a",
                    "refresh_token": "r1",
                    "id_token": "",
                },
            }
            a2 = {
                "auth_mode": "chatgpt",
                "tokens": {
                    "account_id": "id-2",
                    "access_token": "a",
                    "refresh_token": "r2",
                    "id_token": "",
                },
            }
            # 伪造同邮箱 claims 不走 jwt；直接写 email 到 profile 后导入
            p1 = mgr.import_auth_dict(a1, name="p1")
            p1.email = "same@x.com"
            mgr.save_config()
            p2 = mgr.import_auth_dict(a2, name="p2")
            p2.email = "same@x.com"
            mgr.save_config()
            self.assertEqual(len(mgr.list_profiles()), 2)
            self.assertNotEqual(p1.id, p2.id)

    def test_apikey_dedup(self):
        with tempfile.TemporaryDirectory() as td:
            store = Path(td) / "store"
            home = Path(td) / "codex"
            home.mkdir()
            mgr = CodexAccountManager(store_dir=store, codex_home=home)
            auth = {"auth_mode": "apikey", "OPENAI_API_KEY": "sk-dedup-key-001"}
            p1 = mgr.import_auth_dict(auth, name="k1")
            p2 = mgr.import_auth_dict(auth, name="k2")
            self.assertEqual(p1.id, p2.id)
            self.assertEqual(len(mgr.list_profiles()), 1)

    def test_sync_active_wont_overwrite_chatgpt_with_apikey(self):
        with tempfile.TemporaryDirectory() as td:
            store = Path(td) / "store"
            home = Path(td) / "codex"
            home.mkdir()
            mgr = CodexAccountManager(store_dir=store, codex_home=home)
            chatgpt = {
                "auth_mode": "chatgpt",
                "tokens": {
                    "account_id": "acc-cg",
                    "access_token": "tok",
                    "refresh_token": "rt",
                    "id_token": "",
                },
            }
            p = mgr.import_auth_dict(chatgpt, name="cg", make_active=True)
            # live 变成 API Key
            atomic_write_json(
                home / "auth.json",
                {"auth_mode": "apikey", "OPENAI_API_KEY": "sk-should-not-overwrite"},
            )
            out = mgr.sync_active_from_live()
            self.assertIsNone(out)
            saved = json.loads((store / "profiles" / p.id / "auth.json").read_text(encoding="utf-8"))
            self.assertEqual(saved.get("auth_mode"), "chatgpt")
            self.assertEqual(saved["tokens"]["account_id"], "acc-cg")


class TestSwitching(unittest.TestCase):
    @staticmethod
    def _chatgpt_auth(account_id: str) -> dict:
        return {
            "auth_mode": "chatgpt",
            "tokens": {
                "account_id": account_id,
                "access_token": f"access-{account_id}",
                "refresh_token": f"refresh-{account_id}",
                "id_token": "",
            },
        }

    def test_restart_switch_stops_old_codex_before_writing_target(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            store = Path(td) / "store"
            home = Path(td) / "codex"
            home.mkdir()
            mgr = CodexAccountManager(store_dir=store, codex_home=home)
            current_auth = self._chatgpt_auth("current")
            target_auth = self._chatgpt_auth("target")
            mgr.import_auth_dict(current_auth, name="current", make_active=True)
            target = mgr.import_auth_dict(target_auth, name="target")
            atomic_write_json(home / "auth.json", current_auth)

            events: list[str] = []
            original_write = mgr.write_live_auth

            def write_target(auth: dict) -> None:
                events.append("write")
                original_write(auth)

            def stop_old(**_kwargs):
                events.append("stop")
                return {"stopped": True, "killed": [], "remaining": []}

            def launch_new():
                events.append("launch")
                return {"ok": True, "error": "", "cmd": ["codex"]}

            with (
                mock.patch.object(
                    mgr,
                    "sync_active_from_live",
                    side_effect=lambda: events.append("sync"),
                ),
                mock.patch.object(mgr, "stop_codex", side_effect=stop_old),
                mock.patch.object(mgr, "write_live_auth", side_effect=write_target),
                mock.patch.object(mgr, "launch_codex", side_effect=launch_new),
                mock.patch.object(mgr, "token_store", return_value=mock.Mock()),
            ):
                result = mgr.switch_to(target.id, restart=True)

            self.assertLess(events.index("sync"), events.index("stop"))
            self.assertLess(events.index("stop"), events.index("write"))
            self.assertLess(events.index("write"), events.index("launch"))
            self.assertEqual(account_identity_key(mgr.read_live_auth() or {}), target.identity_key)
            self.assertTrue(result["live_verified"])
            self.assertTrue(result["restart"]["launched"])

    def test_restart_switch_refuses_to_write_if_old_codex_remains(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            store = Path(td) / "store"
            home = Path(td) / "codex"
            home.mkdir()
            mgr = CodexAccountManager(store_dir=store, codex_home=home)
            target = mgr.import_auth_dict(
                self._chatgpt_auth("target"), name="target"
            )
            with (
                mock.patch.object(mgr, "sync_active_from_live"),
                mock.patch.object(
                    mgr,
                    "stop_codex",
                    return_value={
                        "stopped": False,
                        "killed": [],
                        "remaining": [{"pid": 123, "name": "Codex"}],
                    },
                ),
                mock.patch.object(mgr, "write_live_auth") as write,
            ):
                with self.assertRaises(RuntimeError) as cm:
                    mgr.switch_to(target.id, restart=True)
            write.assert_not_called()
            self.assertIn("已取消凭据写入", str(cm.exception))

    def test_api_key_switch_replaces_chatgpt_tokens_with_key_mode(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            store = Path(td) / "store"
            home = Path(td) / "codex"
            home.mkdir()
            mgr = CodexAccountManager(store_dir=store, codex_home=home)
            current_auth = self._chatgpt_auth("current")
            mgr.import_auth_dict(current_auth, name="current", make_active=True)
            key_auth = {
                "auth_mode": "apikey",
                "OPENAI_API_KEY": "sk-test-switch-key",
            }
            key_profile = mgr.import_auth_dict(key_auth, name="key")
            atomic_write_json(home / "auth.json", current_auth)

            with mock.patch.object(mgr, "token_store", return_value=mock.Mock()):
                result = mgr.switch_to(key_profile.id, restart=False)

            live = mgr.read_live_auth() or {}
            self.assertEqual(live.get("OPENAI_API_KEY"), "sk-test-switch-key")
            self.assertNotIn("tokens", live)
            self.assertEqual(mgr.detect_active_match().id, key_profile.id)
            self.assertEqual(result["profile"].auth_mode, "apikey")


class TestDeletion(unittest.TestCase):
    def test_delete_active_api_key_removes_live_and_matching_backup(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            store = Path(td) / "store"
            home = Path(td) / "codex"
            home.mkdir()
            mgr = CodexAccountManager(store_dir=store, codex_home=home)
            key_auth = {
                "auth_mode": "apikey",
                "OPENAI_API_KEY": "sk-delete-active-key",
            }
            profile = mgr.import_auth_dict(
                key_auth, name="active-key", make_active=True
            )
            atomic_write_json(home / "auth.json", key_auth)
            backup = home / "auth.json.bak-switcher"
            atomic_write_json(backup, key_auth)

            with (
                mock.patch.object(
                    mgr,
                    "stop_codex",
                    return_value={"stopped": True, "killed": [], "remaining": []},
                ) as stop,
                mock.patch.object(
                    mgr,
                    "launch_codex",
                    return_value={"ok": True, "error": "", "cmd": ["codex"]},
                ) as launch,
            ):
                result = mgr.delete_profile(
                    profile.id,
                    remove_live_credentials=True,
                    relaunch_codex=True,
                )

            stop.assert_called_once()
            launch.assert_called_once()
            self.assertFalse((home / "auth.json").exists())
            self.assertFalse(backup.exists())
            self.assertIsNone(mgr.get_profile(profile.id))
            self.assertFalse(mgr.profile_auth_path(profile.id).exists())
            self.assertTrue(result["removed_live_credentials"])
            self.assertTrue(result["removed_switch_backup"])
            self.assertFalse(result["remote_key_revoked"])

    def test_delete_api_key_does_not_remove_unrelated_live_credentials(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            store = Path(td) / "store"
            home = Path(td) / "codex"
            home.mkdir()
            mgr = CodexAccountManager(store_dir=store, codex_home=home)
            key_profile = mgr.import_auth_dict(
                {"auth_mode": "apikey", "OPENAI_API_KEY": "sk-delete-snapshot"},
                name="saved-key",
            )
            live_auth = {
                "auth_mode": "chatgpt",
                "tokens": {
                    "account_id": "live-account",
                    "access_token": "access",
                    "refresh_token": "refresh",
                    "id_token": "",
                },
            }
            atomic_write_json(home / "auth.json", live_auth)

            with mock.patch.object(mgr, "stop_codex") as stop:
                result = mgr.delete_profile(
                    key_profile.id,
                    remove_live_credentials=True,
                    relaunch_codex=True,
                )

            stop.assert_not_called()
            self.assertEqual(
                account_identity_key(mgr.read_live_auth() or {}),
                "chatgpt:live-account",
            )
            self.assertFalse(result["removed_live_credentials"])

    def test_delete_active_key_aborts_if_codex_cannot_stop(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            store = Path(td) / "store"
            home = Path(td) / "codex"
            home.mkdir()
            mgr = CodexAccountManager(store_dir=store, codex_home=home)
            key_auth = {
                "auth_mode": "apikey",
                "OPENAI_API_KEY": "sk-keep-on-stop-failure",
            }
            profile = mgr.import_auth_dict(key_auth, name="key", make_active=True)
            atomic_write_json(home / "auth.json", key_auth)

            with mock.patch.object(
                mgr,
                "stop_codex",
                return_value={
                    "stopped": False,
                    "killed": [],
                    "remaining": [{"pid": 42, "name": "Codex"}],
                },
            ):
                with self.assertRaises(RuntimeError) as cm:
                    mgr.delete_profile(
                        profile.id,
                        remove_live_credentials=True,
                        relaunch_codex=True,
                    )

            self.assertIn("已取消删除当前 Key", str(cm.exception))
            self.assertIsNotNone(mgr.get_profile(profile.id))
            self.assertTrue((home / "auth.json").exists())


class TestIncrementalUiRefresh(unittest.TestCase):
    class _Widget:
        def __init__(self):
            self.destroyed = False
            self.options: dict = {}

        def destroy(self):
            self.destroyed = True

        def configure(self, **kwargs):
            self.options.update(kwargs)

    @staticmethod
    def _profile(profile_id: str, updated: str = "2026-01-01"):
        return SimpleNamespace(
            id=profile_id,
            last_used_at=updated,
            updated_at=updated,
            created_at=updated,
        )

    def test_unchanged_refresh_keeps_existing_card_widgets(self):
        profile = self._profile("keep")
        card = self._Widget()
        count = self._Widget()
        updates: list[tuple[str, bool]] = []
        highlights: list[bool] = []
        app = SimpleNamespace(
            mgr=SimpleNamespace(
                list_profiles=lambda: [profile],
                detect_active_match=lambda: profile,
            ),
            lbl_count=count,
            _card_widgets={profile.id: card},
            _card_views={profile.id: {}},
            _empty_state=None,
            _selected_id=profile.id,
            _add_card=lambda *_args, **_kwargs: self.fail(
                "unchanged refresh must not recreate a card"
            ),
            _update_card_view=lambda item, is_live: updates.append(
                (item.id, is_live)
            ),
            _highlight_selected=lambda: highlights.append(True),
        )

        CodexAccountApp._render_list(app)

        self.assertFalse(card.destroyed)
        self.assertEqual(updates, [(profile.id, True)])
        self.assertEqual(count.options["text"], "1 个")
        self.assertEqual(len(highlights), 1)

    def test_refresh_destroys_only_removed_account_card(self):
        profile = self._profile("keep")
        keep = self._Widget()
        removed = self._Widget()
        app = SimpleNamespace(
            mgr=SimpleNamespace(
                list_profiles=lambda: [profile],
                detect_active_match=lambda: None,
            ),
            lbl_count=self._Widget(),
            _card_widgets={"keep": keep, "removed": removed},
            _card_views={"keep": {}, "removed": {}},
            _empty_state=None,
            _selected_id="keep",
            _add_card=lambda *_args, **_kwargs: self.fail(
                "existing account must not be recreated"
            ),
            _update_card_view=lambda *_args, **_kwargs: None,
            _highlight_selected=lambda: None,
        )

        CodexAccountApp._render_list(app)

        self.assertFalse(keep.destroyed)
        self.assertTrue(removed.destroyed)
        self.assertEqual(set(app._card_widgets), {"keep"})
        self.assertEqual(set(app._card_views), {"keep"})


class TestQuotaPeriodUsage(unittest.TestCase):
    def test_quota_window_bounds_and_config_round_trip(self):
        now = 2_000_000_000.0
        reset = now + 3600
        profile = AccountProfile(
            id="p1",
            name="p1",
            week_window_label="周",
            week_window_seconds=604800,
            week_reset_at=datetime.fromtimestamp(
                reset, tz=timezone.utc
            ).isoformat(),
        )

        bounds = quota_window_bounds(profile, now_epoch=now)

        self.assertIsNotNone(bounds)
        assert bounds is not None
        self.assertEqual(bounds, (reset - 604800, reset))
        restored = AppConfig.from_dict(AppConfig(profiles=[profile]).to_dict())
        self.assertEqual(restored.profiles[0].week_window_seconds, 604800)

    def test_old_week_label_can_infer_window_and_expired_cache_is_rejected(self):
        now = 2_000_000_000.0
        profile = AccountProfile(
            id="p1",
            name="p1",
            week_window_label="周",
            week_reset_at=datetime.fromtimestamp(
                now + 60, tz=timezone.utc
            ).isoformat(),
        )
        bounds = quota_window_bounds(profile, now_epoch=now)
        self.assertEqual(bounds, (now + 60 - 604800, now + 60))
        self.assertIsNone(quota_window_bounds(profile, now_epoch=now + 61))

    def test_period_cache_filters_from_current_window_start(self):
        now = time.time()
        reset = now + 3600
        profile = AccountProfile(
            id="p1",
            name="p1",
            email="p1@example.com",
            account_id="acc-1",
            week_window_label="5小时",
            week_window_seconds=18000,
            week_reset_at=datetime.fromtimestamp(
                reset, tz=timezone.utc
            ).isoformat(),
        )
        store = mock.Mock()
        store.summarize.return_value = {
            "totals": {"turns": 2, "total_tokens": 123},
            "by_model": [],
            "recent": [],
        }
        app = SimpleNamespace(
            mgr=SimpleNamespace(
                token_store=lambda: store,
                list_profiles=lambda: [profile],
            )
        )

        cache = CodexAccountApp._build_usage_cache(app)

        kwargs = store.summarize.call_args.kwargs
        self.assertAlmostEqual(kwargs["since_epoch"], reset - 18000, places=2)
        self.assertTrue(cache[profile.id]["period_available"])
        self.assertEqual(cache[profile.id]["period_label"], "本期")

    def test_row_uses_period_total_but_detail_query_has_no_time_filter(self):
        profile = AccountProfile(
            id="p1",
            name="p1",
            email="p1@example.com",
            account_id="acc-1",
        )
        app = SimpleNamespace(
            _usage_cache={
                profile.id: {
                    "period_available": True,
                    "period_label": "本周",
                    "totals": {"turns": 3, "total_tokens": 1200},
                }
            }
        )
        self.assertEqual(
            CodexAccountApp._token_total_text(app, profile),
            "本周 1.2K",
        )

        mgr = CodexAccountManager.__new__(CodexAccountManager)
        store = mock.Mock()
        store.summarize.return_value = {"totals": {"total_tokens": 9999}}
        mgr.get_profile = lambda _profile_id: profile
        mgr.token_store = lambda: store
        mgr.get_profile_token_usage(profile.id)
        store.summarize.assert_called_once_with(
            profile_id=profile.id,
            account_id=profile.account_id,
            email=profile.email,
        )

    def test_current_replaces_valid_badge_and_quota_color_thresholds(self):
        profile = AccountProfile(id="p1", name="p1", auth_status="ok")
        label, text_color, background = CodexAccountApp._auth_badge_style(
            profile,
            is_live=True,
        )
        self.assertEqual(label, "当前")
        self.assertEqual(text_color, "#2563EB")
        self.assertEqual(background, "#DBEAFE")
        self.assertEqual(left_color(9.99), "#DC2626")
        self.assertEqual(left_color(10), "#DC2626")
        self.assertEqual(left_color(39.99), "#D97706")
        self.assertEqual(left_color(40), "#D97706")
        self.assertEqual(left_color(40.01), "#059669")


class TestFocusQuotaRefresh(unittest.TestCase):
    def test_minimized_poll_does_not_refresh_and_keeps_polling(self):
        requested: list[bool] = []
        scheduled: list[tuple[int, object]] = []
        app = SimpleNamespace(
            _foreground_poll_after_id="pending",
            _window_was_foreground=True,
            _startup_checked=True,
            state=lambda: "iconic",
            after=lambda delay, callback: scheduled.append((delay, callback)) or "next",
            _poll_foreground_state=lambda: None,
            _request_focus_quota_refresh=lambda: requested.append(True),
        )

        CodexAccountApp._poll_foreground_state(app)

        self.assertFalse(app._window_was_foreground)
        self.assertEqual(requested, [])
        self.assertEqual(scheduled[0][0], 500)
        self.assertEqual(app._foreground_poll_after_id, "next")

    def test_real_foreground_transition_requests_one_refresh(self):
        requested: list[bool] = []
        app = SimpleNamespace(
            _foreground_poll_after_id="pending",
            _window_was_foreground=False,
            _startup_checked=True,
            state=lambda: "normal",
            focus_displayof=lambda: True,
            after=lambda _delay, _callback: "next",
            _poll_foreground_state=lambda: None,
            _request_focus_quota_refresh=lambda: requested.append(True),
        )
        with mock.patch(
            "windows_app.is_current_process_foreground",
            return_value=True,
        ):
            CodexAccountApp._poll_foreground_state(app)
            CodexAccountApp._poll_foreground_state(app)

        self.assertTrue(app._window_was_foreground)
        self.assertEqual(requested, [True])

    def test_recent_quota_cache_skips_auto_refresh(self):
        profile = AccountProfile(
            id="p1",
            name="p1",
            usage_fetched_at=datetime.fromtimestamp(
                1_000_000 - 60,
                timezone.utc,
            ).isoformat(),
        )
        app = SimpleNamespace(
            mgr=SimpleNamespace(list_profiles=lambda: [profile]),
            _last_auto_quota_attempt_monotonic=0.0,
            _usage_fetched_epoch=CodexAccountApp._usage_fetched_epoch,
        )

        self.assertFalse(
            CodexAccountApp._auto_quota_refresh_due(
                app,
                now_epoch=1_000_000,
                now_monotonic=500,
            )
        )

    def test_stale_quota_cache_allows_auto_refresh(self):
        profile = AccountProfile(
            id="p1",
            name="p1",
            usage_fetched_at=datetime.fromtimestamp(
                1_000_000 - 300,
                timezone.utc,
            ).isoformat(),
        )
        app = SimpleNamespace(
            mgr=SimpleNamespace(list_profiles=lambda: [profile]),
            _last_auto_quota_attempt_monotonic=0.0,
            _usage_fetched_epoch=CodexAccountApp._usage_fetched_epoch,
        )

        self.assertTrue(
            CodexAccountApp._auto_quota_refresh_due(
                app,
                now_epoch=1_000_000,
                now_monotonic=500,
            )
        )

    def test_recent_failed_attempt_blocks_an_immediate_retry(self):
        profile = AccountProfile(id="p1", name="p1")
        app = SimpleNamespace(
            mgr=SimpleNamespace(list_profiles=lambda: [profile]),
            _last_auto_quota_attempt_monotonic=450.0,
            _usage_fetched_epoch=CodexAccountApp._usage_fetched_epoch,
        )

        self.assertFalse(
            CodexAccountApp._auto_quota_refresh_due(
                app,
                now_epoch=1_000_000,
                now_monotonic=500,
            )
        )

    def test_foreground_refresh_is_silent_and_busy_refresh_is_coalesced(self):
        calls: list[bool] = []
        app = SimpleNamespace(
            _busy=False,
            _focus_quota_refresh_running=False,
            _focus_refresh_pending=False,
            _window_was_foreground=False,
            state=lambda: "normal",
            _auto_quota_refresh_due=lambda: True,
            refresh_all_quotas=lambda quiet=False: calls.append(quiet),
        )
        with mock.patch(
            "windows_app.is_current_process_foreground",
            return_value=True,
        ):
            CodexAccountApp._request_focus_quota_refresh(app)
            app._busy = True
            CodexAccountApp._request_focus_quota_refresh(app)

        self.assertEqual(calls, [True])
        self.assertTrue(app._focus_refresh_pending)


class TestTokenRefreshPolicy(unittest.TestCase):
    def test_needs_refresh_expired(self):
        # exp in past
        import base64

        payload = base64.urlsafe_b64encode(
            json.dumps({"exp": int(time.time()) - 100}).encode()
        ).decode().rstrip("=")
        token = f"x.{payload}.y"
        auth = {"tokens": {"access_token": token, "refresh_token": "rt"}}
        self.assertTrue(needs_refresh(auth))

    def test_repair_only_on_auth_error(self):
        auth = {
            "auth_mode": "chatgpt",
            "tokens": {
                "access_token": "a",
                "refresh_token": "r",
                "account_id": "x",
            },
        }
        with mock.patch("usage.fetch_codex_usage", side_effect=RuntimeError("网络错误 timeout")):
            with self.assertRaises(RuntimeError) as cm:
                fetch_usage_with_auth_repair(auth)
            self.assertIn("未刷新凭证", str(cm.exception))

    def test_repair_on_401_persists(self):
        auth = {
            "auth_mode": "chatgpt",
            "tokens": {
                "access_token": "old",
                "refresh_token": "r",
                "account_id": "x",
            },
        }
        fresh = {
            "auth_mode": "chatgpt",
            "tokens": {
                "access_token": "new",
                "refresh_token": "r2",
                "account_id": "x",
            },
        }
        persisted = []

        def on_ref(a):
            persisted.append(a["tokens"]["access_token"])

        calls = {"n": 0}

        def fake_usage(a):
            calls["n"] += 1
            if a["tokens"]["access_token"] == "old":
                raise HttpStatusError(401, "https://x", "unauthorized")
            return {"rate_limit": {"primary_window": {"used_percent": 10, "limit_window_seconds": 604800}}}

        with mock.patch("usage.fetch_codex_usage", side_effect=fake_usage):
            with mock.patch("usage.refresh_auth_tokens", return_value=fresh):
                usage, out, refreshed = fetch_usage_with_auth_repair(auth, on_refreshed=on_ref)
        self.assertTrue(refreshed)
        self.assertEqual(persisted, ["new"])
        self.assertEqual(out["tokens"]["access_token"], "new")

    def test_is_auth_error(self):
        self.assertTrue(is_auth_error(HttpStatusError(401, "u", "x")))
        self.assertFalse(is_auth_error(RuntimeError("网络错误 timeout")))


class TestUsageParse(unittest.TestCase):
    def test_prefers_weekly_window_label(self):
        usage = {
            "email": "a@b.com",
            "plan_type": "plus",
            "rate_limit": {
                "allowed": True,
                "limit_reached": False,
                "primary_window": {
                    "used_percent": 20,
                    "limit_window_seconds": 18000,
                    "reset_at": int(time.time()) + 1000,
                },
                "secondary_window": {
                    "used_percent": 50,
                    "limit_window_seconds": 604800,
                    "reset_at": int(time.time()) + 2000,
                },
            },
        }
        s = parse_usage_summary(usage)
        self.assertEqual(s["week_window_label"], "周")
        self.assertEqual(s["week_used_percent"], 50.0)
        self.assertEqual(s["week_left_percent"], 50.0)


class TestTokenUsageScan(unittest.TestCase):
    def test_total_tokens_fallback(self):
        self.assertEqual(
            TokenUsageStore._usage_total({"input_tokens": 10, "output_tokens": 5}),
            15,
        )
        self.assertEqual(
            TokenUsageStore._usage_total({"total_tokens": 99, "input_tokens": 1, "output_tokens": 1}),
            99,
        )

    def test_half_line_not_advanced(self):
        # Windows 下 sqlite WAL 可能延迟释放文件句柄
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            store = Path(td) / "store"
            sessions = Path(td) / "sessions"
            sessions.mkdir(parents=True)
            f = sessions / "rollout-test.jsonl"
            # 完整一行 + 半行
            line = json.dumps(
                {
                    "timestamp": "2026-08-12T00:00:00Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "last_token_usage": {
                                "input_tokens": 10,
                                "output_tokens": 5,
                                "cached_input_tokens": 0,
                                "reasoning_output_tokens": 0,
                                "total_tokens": 0,
                            }
                        },
                    },
                }
            )
            f.write_bytes((line + "\n").encode() + b'{"timestamp":"2026-08-12T00:00:01Z","type":"event_msg"')
            tus = TokenUsageStore(store, sessions_dir=sessions)
            # 给时间线：从 epoch 0 归属
            tus.log_switch(profile_id="p1", account_id="a1", email="e@x.com", source="baseline", ts=0)
            r = tus.sync_sessions(full=True, recent_days=None, max_files=None)
            self.assertEqual(r["inserted_events"], 1)
            with tus._connect() as con:
                offset1 = con.execute("SELECT offset FROM scan_state").fetchone()[0]
            # 半行未推进到文件末尾
            self.assertLess(offset1, f.stat().st_size)
            # 补全半行
            with f.open("ab") as fh:
                fh.write(
                    b',"payload":{"type":"token_count","info":{"last_token_usage":{"input_tokens":1,"output_tokens":1,"total_tokens":2}}}}\n'
                )
            r2 = tus.sync_sessions(full=True, recent_days=None, max_files=None)
            self.assertGreaterEqual(r2["inserted_events"], 1)
            summary = tus.summarize(profile_id="p1", account_id="a1", email="e@x.com")
            self.assertGreaterEqual(summary["totals"]["turns"], 2)
            self.assertGreaterEqual(summary["totals"]["total_tokens"], 17)
            # 释放 sqlite（Windows 临时目录清理）
            del tus
            import gc

            gc.collect()

    def test_reattribute_reuses_one_switch_timeline(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            tus = TokenUsageStore(Path(td) / "store", sessions_dir=Path(td) / "sessions")
            tus.log_switch(
                profile_id="p1", account_id="a1", email="one@x.com", ts=0
            )
            tus.log_switch(
                profile_id="p2", account_id="a2", email="two@x.com", ts=10
            )
            with tus._connection() as con:
                con.executemany(
                    """
                    INSERT INTO usage_events(
                        event_epoch, profile_id, account_id, email,
                        source_file, source_line
                    ) VALUES (?, '', '', '', ?, ?)
                    """,
                    [
                        (5, "session-a", 1),
                        (15, "session-a", 2),
                        (20, "session-a", 3),
                    ],
                )

            with mock.patch.object(
                tus, "_load_switches", wraps=tus._load_switches
            ) as load_switches:
                result = tus.reattribute_all()

            self.assertEqual(load_switches.call_count, 1)
            self.assertEqual(result["updated"], 3)
            with tus._connection() as con:
                accounts = [
                    row[0]
                    for row in con.execute(
                        "SELECT account_id FROM usage_events ORDER BY source_line"
                    )
                ]
            self.assertEqual(accounts, ["a1", "a2", "a2"])


class TestOAuthIsolation(unittest.TestCase):
    @staticmethod
    def _auth(account_id: str, access: str, refresh: str) -> dict:
        return {
            "auth_mode": "chatgpt",
            "tokens": {
                "account_id": account_id,
                "access_token": access,
                "refresh_token": refresh,
                "id_token": "",
            },
        }

    @staticmethod
    def _jwt(payload: dict) -> str:
        import base64

        encoded = base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
        ).decode("ascii").rstrip("=")
        return f"header.{encoded}.signature"

    def test_pkce_and_authorization_url(self):
        import base64
        import hashlib
        import urllib.parse

        pkce = oauth_login.generate_pkce_codes()
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(pkce.verifier.encode("ascii")).digest()
        ).decode("ascii").rstrip("=")
        self.assertGreaterEqual(len(pkce.verifier), 43)
        self.assertLessEqual(len(pkce.verifier), 128)
        self.assertEqual(pkce.challenge, expected)

        url = oauth_login.build_authorization_url("state-123", pkce.challenge)
        parsed = urllib.parse.urlsplit(url)
        params = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "auth.openai.com")
        self.assertEqual(params["state"], ["state-123"])
        self.assertEqual(params["code_challenge_method"], ["S256"])
        self.assertEqual(params["redirect_uri"], [oauth_login.OAUTH_REDIRECT_URI])
        self.assertEqual(params["scope"], [oauth_login.OAUTH_SCOPES])
        self.assertEqual(params["originator"], ["Codex Desktop"])
        self.assertNotIn("prompt", params)
        self.assertIn("scope=openid%20profile%20email%20offline_access", url)

    def test_loopback_callback_rejects_wrong_state_then_accepts_valid(self):
        import urllib.error
        import urllib.parse
        import urllib.request

        listener = oauth_login.OAuthCallbackListener("correct-state", port=0)
        base = f"http://127.0.0.1:{listener.port}{oauth_login.CALLBACK_PATH}"
        try:
            wrong = base + "?" + urllib.parse.urlencode(
                {"state": "wrong-state", "code": "attacker-code"}
            )
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(wrong, timeout=2)
            self.assertEqual(cm.exception.code, 400)

            good = base + "?" + urllib.parse.urlencode(
                {"state": "correct-state", "code": "valid-code"}
            )
            with urllib.request.urlopen(good, timeout=2) as response:
                self.assertEqual(response.status, 200)
            callback = listener.wait(timeout_sec=2)
            self.assertEqual(callback.code, "valid-code")
            self.assertEqual(callback.state, "correct-state")
        finally:
            listener.close()

    def test_auth_from_token_response_builds_codex_schema(self):
        id_token = self._jwt(
            {
                "email": "new@example.com",
                "https://api.openai.com/auth": {
                    "chatgpt_account_id": "account-new",
                    "chatgpt_plan_type": "plus",
                },
            }
        )
        auth = oauth_login.auth_from_token_response(
            {
                "id_token": id_token,
                "access_token": "access-new",
                "refresh_token": "refresh-new",
            }
        )
        self.assertEqual(auth["auth_mode"], "chatgpt")
        self.assertIsNone(auth["OPENAI_API_KEY"])
        self.assertEqual(auth["tokens"]["account_id"], "account-new")
        self.assertEqual(extract_account_info(auth)["email"], "new@example.com")

    def test_oauth_import_does_not_change_live_auth_or_active_profile(self):
        class FakeListener:
            def __init__(self, expected_state: str):
                self.expected_state = expected_state
                self.closed = False

            def wait(self, **_kwargs):
                return oauth_login.OAuthCallback(
                    code="authorization-code", state=self.expected_state
                )

            def close(self):
                self.closed = True

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            store = Path(td) / "store"
            home = Path(td) / "codex"
            home.mkdir()
            mgr = CodexAccountManager(store_dir=store, codex_home=home)
            live_auth = self._auth("current-account", "current-access", "current-refresh")
            current = mgr.import_auth_dict(live_auth, name="current", make_active=True)
            atomic_write_json(home / "auth.json", live_auth)
            live_before = (home / "auth.json").read_bytes()
            new_auth = self._auth("new-account", "new-access", "new-refresh")
            silent_log = lambda _msg: None
            proxy_route = {"https": "http://127.0.0.1:7897"}
            http_session = mock.Mock()

            with (
                mock.patch("oauth_login.oauth_proxy_settings", return_value=proxy_route),
                mock.patch(
                    "oauth_login.create_oauth_http_session",
                    return_value=http_session,
                ),
                mock.patch(
                    "oauth_login.probe_oauth_network",
                    return_value={"ok": True, "status": 403, "via_proxy": True},
                ) as probe,
                mock.patch("oauth_login.generate_oauth_state", return_value="state-1"),
                mock.patch(
                    "oauth_login.generate_pkce_codes",
                    return_value=oauth_login.PKCECodes("verifier-1", "challenge-1"),
                ),
                mock.patch("oauth_login.OAuthCallbackListener", FakeListener),
                mock.patch(
                    "oauth_login.open_private_browser", return_value="Chrome 无痕窗口"
                ) as open_browser,
                mock.patch("oauth_login.exchange_oauth_code", return_value=new_auth) as exchange,
            ):
                result = oauth_login.oauth_login_and_save(mgr=mgr, log=silent_log)

            self.assertTrue(result["ok"])
            self.assertEqual(result["flow"], "pkce_loopback")
            self.assertTrue(result["live_auth_unchanged"])
            self.assertEqual((home / "auth.json").read_bytes(), live_before)
            self.assertEqual(mgr.config.active_id, current.id)
            self.assertEqual(len(mgr.list_profiles()), 2)
            open_browser.assert_called_once()
            opened_url = open_browser.call_args.args[0]
            self.assertIn("state=state-1", opened_url)
            exchange.assert_called_once_with(
                "authorization-code",
                "verifier-1",
                proxies=proxy_route,
                session=http_session,
                log=silent_log,
                use_windows_native=False,
            )
            self.assertEqual(probe.call_count, 2)
            self.assertEqual(
                probe.call_args.kwargs["target_url"], oauth_login.OAUTH_TOKEN_URL
            )
            http_session.close.assert_called_once()

            # 新凭据进入原有账户库后，仍可走同一套切换逻辑成为活动账户。
            mgr.switch_to(result["profile_id"], restart=False)
            switched = json.loads((home / "auth.json").read_text(encoding="utf-8"))
            self.assertEqual(switched["tokens"]["account_id"], "new-account")
            self.assertEqual(mgr.config.active_id, result["profile_id"])

    def test_private_browser_never_falls_back_to_regular_window(self):
        with mock.patch("oauth_login._which_browser_exe", return_value=[]):
            with self.assertRaises(RuntimeError):
                oauth_login.open_private_browser("https://example.com")

    def test_exchange_posts_form_and_returns_auth(self):
        response = mock.Mock()
        response.status_code = 200
        response.reason = "OK"
        response.headers = {"Content-Type": "application/json"}
        response.text = json.dumps(
            {
                "id_token": TestOAuthIsolation._jwt(
                    {
                        "https://api.openai.com/auth": {
                            "chatgpt_account_id": "exchange-account"
                        }
                    }
                ),
                "access_token": "exchange-access",
                "refresh_token": "exchange-refresh",
            }
        )
        proxy_route = {"https": "http://127.0.0.1:7897"}
        with mock.patch("oauth_login.requests.post", return_value=response) as post:
            auth = oauth_login.exchange_oauth_code(
                "one-code", "one-verifier", proxies=proxy_route
            )
        form = post.call_args.kwargs["data"]
        self.assertEqual(form["code"], "one-code")
        self.assertEqual(form["code_verifier"], "one-verifier")
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs["proxies"], proxy_route)
        self.assertEqual(auth["tokens"]["account_id"], "exchange-account")

    def test_exchange_reports_html_response_without_json_noise(self):
        response = mock.Mock()
        response.status_code = 200
        response.reason = "OK"
        response.headers = {"Content-Type": "text/html; charset=utf-8"}
        response.text = "<!DOCTYPE html><html><body>gateway page</body></html>"
        with mock.patch("oauth_login.requests.post", return_value=response):
            with self.assertRaises(RuntimeError) as cm:
                oauth_login.exchange_oauth_code(
                    "one-code", "one-verifier", proxies={}
                )
        self.assertIn("返回了 HTML 而不是 JSON", str(cm.exception))
        self.assertNotIn("Unexpected token", str(cm.exception))

    def test_exchange_uses_native_fallback_only_for_proxy_connect_failure(self):
        proxy_route = {"https": "http://127.0.0.1:7897"}
        native_auth = self._auth(
            "native-account", "native-access", "native-refresh"
        )
        logs: list[str] = []
        proxy_error = oauth_login.requests.exceptions.ProxyError(
            "Cannot connect to proxy: connection reset"
        )
        with (
            mock.patch("oauth_login.requests.post", side_effect=proxy_error) as post,
            mock.patch(
                "oauth_login._find_windows_powershell",
                return_value=r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
            ),
            mock.patch(
                "oauth_login._exchange_oauth_code_windows_native",
                return_value=native_auth,
            ) as native,
        ):
            auth = oauth_login.exchange_oauth_code(
                "one-code",
                "one-verifier",
                proxies=proxy_route,
                log=logs.append,
            )

        self.assertEqual(auth, native_auth)
        self.assertEqual(post.call_count, 1)
        native.assert_called_once()
        native_form = native.call_args.args[0]
        self.assertEqual(native_form["code"], "one-code")
        self.assertEqual(native_form["code_verifier"], "one-verifier")
        self.assertTrue(any("Windows 原生 HTTPS" in line for line in logs))

    def test_exchange_does_not_retry_ambiguous_connection_failure(self):
        error = oauth_login.requests.exceptions.ConnectionError(
            "connection reset after send"
        )
        with (
            mock.patch("oauth_login.requests.post", side_effect=error),
            mock.patch(
                "oauth_login._exchange_oauth_code_windows_native"
            ) as native,
        ):
            with self.assertRaises(RuntimeError) as cm:
                oauth_login.exchange_oauth_code(
                    "one-code",
                    "one-verifier",
                    proxies={"https": "http://127.0.0.1:7897"},
                )
        native.assert_not_called()
        self.assertIn("未自动重试", str(cm.exception))

    def test_network_probe_retries_safe_get_before_login(self):
        response = mock.Mock()
        response.status_code = 403
        route = {"https": "http://127.0.0.1:7897"}
        with (
            mock.patch(
                "oauth_login.requests.get",
                side_effect=[oauth_login.requests.exceptions.SSLError("EOF"), response],
            ) as get,
            mock.patch("oauth_login.time.sleep"),
        ):
            result = oauth_login.probe_oauth_network(
                proxies=route, timeout_sec=1, attempts=2
            )
        self.assertTrue(result["ok"])
        self.assertTrue(result["via_proxy"])
        self.assertEqual(get.call_count, 2)

    def test_token_transport_selects_native_after_proxy_connect_reset(self):
        proxy_error = oauth_login.requests.exceptions.ProxyError(
            "Cannot connect to proxy: connection reset"
        )
        wrapped = RuntimeError("OAuth 网络预检失败")
        wrapped.__cause__ = proxy_error
        native_result = {
            "ok": True,
            "status": 405,
            "via_proxy": True,
            "transport": "windows_native",
        }
        with (
            mock.patch("oauth_login.probe_oauth_network", side_effect=wrapped),
            mock.patch(
                "oauth_login._find_windows_powershell",
                return_value="powershell.exe",
            ),
            mock.patch(
                "oauth_login.probe_oauth_network_windows_native",
                return_value=native_result,
            ) as native_probe,
        ):
            selected = oauth_login.select_oauth_token_transport(
                proxies={"https": "http://127.0.0.1:7897"},
                session=mock.Mock(),
            )
        self.assertEqual(selected["transport"], "windows_native")
        native_probe.assert_called_once()

    def test_windows_native_request_keeps_sensitive_body_off_command_line(self):
        completed = mock.Mock(
            returncode=0,
            stdout=json.dumps(
                {
                    "transport_ok": True,
                    "status": 405,
                    "content_type": "application/json",
                    "reason": "Method Not Allowed",
                    "body": "",
                }
            ),
            stderr="",
        )
        with (
            mock.patch(
                "oauth_login._find_windows_powershell",
                return_value="powershell.exe",
            ),
            mock.patch("oauth_login.subprocess.run", return_value=completed) as run,
        ):
            result = oauth_login._windows_native_oauth_request(
                method="POST",
                url=oauth_login.OAUTH_TOKEN_URL,
                timeout_sec=30,
                body="code=secret-one-time-code",
                content_type="application/x-www-form-urlencoded",
                warmup_url=oauth_login.OAUTH_TOKEN_URL,
                warmup_attempts=3,
            )

        self.assertEqual(result[0], 405)
        command_line = " ".join(run.call_args.args[0])
        self.assertNotIn("secret-one-time-code", command_line)
        request_payload = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(request_payload["body"], "code=secret-one-time-code")
        self.assertEqual(request_payload["warmup_attempts"], 3)

    def test_automatic_login_uses_pkce_without_changing_live_auth(self):
        class FakeListener:
            def __init__(self, expected_state: str):
                self.expected_state = expected_state

            def wait(self, **_kwargs):
                return oauth_login.OAuthCallback("auto-code", self.expected_state)

            def close(self):
                pass

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            store = Path(td) / "store"
            home = Path(td) / "codex"
            home.mkdir()
            mgr = CodexAccountManager(store_dir=store, codex_home=home)
            live_auth = self._auth("current-account", "current-access", "current-refresh")
            current = mgr.import_auth_dict(live_auth, name="current", make_active=True)
            atomic_write_json(home / "auth.json", live_auth)
            live_before = (home / "auth.json").read_bytes()
            new_auth = self._auth("batch-account", "batch-access", "batch-refresh")
            cred = auto_login.Credential(email="batch@example.com", password="secret")
            proxy_route = {"https": "http://127.0.0.1:7897"}
            http_session = mock.Mock()
            silent_log = lambda _msg: None

            with (
                mock.patch("auto_login.oauth_proxy_settings", return_value=proxy_route),
                mock.patch(
                    "auto_login.create_oauth_http_session",
                    return_value=http_session,
                ),
                mock.patch(
                    "auto_login.probe_oauth_network",
                    return_value={"ok": True, "status": 403, "via_proxy": True},
                ) as probe,
                mock.patch(
                    "auto_login.select_oauth_token_transport",
                    return_value={
                        "ok": True,
                        "status": 405,
                        "via_proxy": True,
                        "transport": "requests",
                    },
                ) as select_transport,
                mock.patch("auto_login.generate_oauth_state", return_value="auto-state"),
                mock.patch(
                    "auto_login.generate_pkce_codes",
                    return_value=oauth_login.PKCECodes("auto-verifier", "auto-challenge"),
                ),
                mock.patch("auto_login.OAuthCallbackListener", FakeListener),
                mock.patch("auto_login.playwright_login") as browser_login,
                mock.patch("auto_login.exchange_oauth_code", return_value=new_auth) as exchange,
            ):
                result = auto_login.login_one(
                    cred,
                    mgr=mgr,
                    codex_cli=Path("codex.exe"),
                    keep_as_active=False,
                    log=silent_log,
                )

            self.assertTrue(result["ok"])
            self.assertTrue(result["live_auth_unchanged"])
            self.assertEqual((home / "auth.json").read_bytes(), live_before)
            self.assertEqual(mgr.config.active_id, current.id)
            self.assertEqual(len(mgr.list_profiles()), 2)
            browser_login.assert_called_once()
            self.assertNotIn("device_code", browser_login.call_args.kwargs)
            exchange.assert_called_once_with(
                "auto-code",
                "auto-verifier",
                proxies=proxy_route,
                session=http_session,
                log=silent_log,
                use_windows_native=False,
            )
            probe.assert_called_once_with(
                proxies=proxy_route,
                session=http_session,
            )
            select_transport.assert_called_once_with(
                proxies=proxy_route,
                session=http_session,
            )
            http_session.close.assert_called_once()


class TestLambdaCapturePattern(unittest.TestCase):
    def test_default_arg_capture(self):
        errs = []

        def after(cb):
            cb()

        try:
            raise ValueError("boom")
        except Exception as e:
            err = str(e)
            after(lambda err=err: errs.append(err))
        self.assertEqual(errs, ["boom"])


if __name__ == "__main__":
    unittest.main()
