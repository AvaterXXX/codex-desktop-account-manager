#!/usr/bin/env python3
"""Codex 账户管理器 — 一键切换桌面版登录账户（支持自动重启）。"""
from __future__ import annotations

import sys
import threading
import time
import traceback
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog
from typing import Any

import customtkinter as ctk

from manager import AccountProfile, CodexAccountManager, quota_window_bounds
from token_usage import format_tokens
from usage import format_reset_local, left_color

# 行内操作区固定宽度（切换+复制+限额+用量 + 间距）
# 56+4+48+4+48+4+48 ≈ 212
ROW_ACTIONS_W = 220


# ---- 白色主题 ----
ctk.set_appearance_mode("light")
ctk.set_default_color_theme("blue")

BG = "#FFFFFF"
BG_SOFT = "#F7F8FA"
BG_SIDE = "#F3F4F6"
BORDER = "#E5E7EB"
TEXT = "#111827"
TEXT_MUTED = "#6B7280"
PRIMARY = "#2563EB"
PRIMARY_HOVER = "#1D4ED8"
DANGER = "#DC2626"
SUCCESS = "#059669"
WARN = "#D97706"
ACTIVE_BG = "#EEF2FF"
ACTIVE_BORDER = "#93C5FD"
CARD = "#FFFFFF"
OAUTH = "#7C3AED"          # 紫色，区分 OAuth
OAUTH_HOVER = "#6D28D9"


def profile_uses_api_key(profile: AccountProfile) -> bool:
    mode = (profile.auth_mode or "").lower()
    return mode in ("apikey", "api_key") or (profile.identity_key or "").startswith(
        "apikey:"
    )


class CodexAccountApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Codex 账户管理器")
        self._app_icon: tk.PhotoImage | None = None
        self._set_window_icon()
        self.geometry("1080x640")
        self.minsize(960, 520)
        self.configure(fg_color=BG)

        self.mgr = CodexAccountManager()
        self._busy = False
        self._selected_id: str | None = None
        self._card_widgets: dict[str, ctk.CTkFrame] = {}
        self._card_views: dict[str, dict[str, Any]] = {}
        self._empty_state: ctk.CTkFrame | None = None
        self._startup_checked = False
        self._usage_cache: dict[str, dict] = {}
        self._usage_cache_loading = False
        self._codex_running_cache: tuple[float, bool] | None = None
        self._codex_check_generation = 0
        self._switch_verify_generation = 0
        self._activation_path: Path | None = None
        self._activation_token = ""
        self._focus_probe_after_id: str | None = None
        self._window_was_foreground = False
        self._focus_refresh_pending = False
        self._focus_quota_refresh_running = False

        self._build_ui()
        # 子控件焦点事件会经过顶层 bindtag；延迟检查真正的前台进程，
        # 只有从其他应用重新聚焦本程序时才刷新，不会因点内部按钮反复请求。
        self.bind("<FocusIn>", self._on_window_focus_event, add="+")
        self.bind("<FocusOut>", self._on_window_focus_event, add="+")
        self.bind("<Map>", self._on_window_focus_event, add="+")
        self.bind("<Unmap>", self._on_window_focus_event, add="+")
        # 首屏只读 auth/config 并立即绘制；SQLite 汇总和 tasklist 都放到后台。
        self.refresh_ui_light(keep_selection=False, load_usage=False)
        self.set_status("正在后台同步…")
        # 后台：同步当前账户 + 仅查当前限额（不全量扫）
        self.after(100, self.startup_auth_check)

    def _set_window_icon(self) -> None:
        """设置标题栏和任务栏图标；保留 PhotoImage 引用避免被 Tk 回收。"""
        assets = Path(__file__).resolve().parent / "assets"
        png_path = assets / "app_icon.png"
        ico_path = assets / "app_icon.ico"
        try:
            if png_path.exists():
                self._app_icon = tk.PhotoImage(file=str(png_path))
                self.iconphoto(True, self._app_icon)
            if sys.platform == "win32" and ico_path.exists():
                self.iconbitmap(str(ico_path))
        except (OSError, tk.TclError):
            # 图标异常不应阻止主窗口启动。
            self._app_icon = None

    def start_activation_listener(self, path: Path) -> None:
        """监听第二次点击启动器发来的唤醒请求。"""
        self._activation_path = Path(path)
        self._activation_token = ""
        self.after(200, self._poll_activation_request)

    def _poll_activation_request(self) -> None:
        if not self._activation_path:
            return
        from atomic_io import read_activation_token

        token = read_activation_token(self._activation_path)
        if token and token != self._activation_token:
            self._activation_token = token
            self.restore_window()
        try:
            self.after(250, self._poll_activation_request)
        except tk.TclError:
            pass

    def restore_window(self) -> None:
        """可靠地恢复最小化窗口，并把活动模态对话框一并带到前台。"""
        try:
            if self.state() in ("iconic", "withdrawn"):
                self.state("normal")
            self.deiconify()
            self.update_idletasks()

            grabbed = self.grab_current()
            target = grabbed.winfo_toplevel() if grabbed else self
            try:
                target.deiconify()
            except (AttributeError, tk.TclError):
                target = self

            if sys.platform == "win32":
                from windows_app import activate_window_by_title

                activate_window_by_title()
            target.lift()
            target.attributes("-topmost", True)
            target.after(120, lambda w=target: w.attributes("-topmost", False))
            target.focus_force()
        except (AttributeError, tk.TclError):
            pass

    def _on_window_focus_event(self, _event: Any = None) -> None:
        """合并焦点/显示事件，稍后判断本程序是否真正成为前台。"""
        if self._focus_probe_after_id is not None:
            try:
                self.after_cancel(self._focus_probe_after_id)
            except tk.TclError:
                pass
        try:
            self._focus_probe_after_id = self.after(
                120,
                self._probe_window_foreground,
            )
        except tk.TclError:
            self._focus_probe_after_id = None

    def _probe_window_foreground(self) -> None:
        self._focus_probe_after_id = None
        try:
            visible = self.state() not in ("iconic", "withdrawn")
        except tk.TclError:
            return
        if not visible:
            self._window_was_foreground = False
            return
        try:
            from windows_app import is_current_process_foreground

            foreground = is_current_process_foreground()
        except Exception:
            foreground = bool(self.focus_displayof())
        if not foreground:
            self._window_was_foreground = False
            return
        became_foreground = not self._window_was_foreground
        self._window_was_foreground = True
        if became_foreground and self._startup_checked:
            self._request_focus_quota_refresh()

    def _request_focus_quota_refresh(self) -> None:
        """前台且非最小化时静默刷新全部限额；忙碌时合并为一次待刷新。"""
        try:
            if self.state() in ("iconic", "withdrawn"):
                return
            from windows_app import is_current_process_foreground

            if not is_current_process_foreground():
                return
        except (tk.TclError, OSError):
            return
        self._window_was_foreground = True
        if self._busy or self._focus_quota_refresh_running:
            self._focus_refresh_pending = True
            return
        self._focus_refresh_pending = False
        self.refresh_all_quotas(quiet=True)

    def _sync_startup_usage_when_idle(self, attempt: int = 0) -> None:
        """首次限额刷新期间不丢掉启动用量同步，空闲后再执行。"""
        if self._busy and attempt < 30:
            self.after(
                1000,
                lambda n=attempt + 1: self._sync_startup_usage_when_idle(n),
            )
            return
        self.sync_token_usage(quiet=True)

    # ---------- UI ----------
    def _build_ui(self) -> None:
        # ========== 顶栏：标题 + 当前登录（单行） + 快捷按钮 ==========
        header = ctk.CTkFrame(self, fg_color=BG, corner_radius=0)
        header.pack(fill="x", padx=16, pady=(10, 4))

        ctk.CTkLabel(
            header,
            text="Codex 账户管理",
            font=ctk.CTkFont(size=18, weight="bold"),
            text_color=TEXT,
        ).pack(side="left")

        # 当前登录紧贴标题
        self.lbl_live_main = ctk.CTkLabel(
            header,
            text="当前：读取中…",
            font=ctk.CTkFont(size=12),
            text_color=TEXT,
            anchor="w",
        )
        self.lbl_live_main.pack(side="left", padx=(12, 0))
        self.lbl_live_sub = ctk.CTkLabel(
            header,
            text="",
            font=ctk.CTkFont(size=11),
            text_color=TEXT_MUTED,
            anchor="w",
        )
        self.lbl_live_sub.pack(side="left", padx=(8, 0))

        # 顶栏快捷（短中文，固定够宽）
        # 字数×14 + 左右内边距约 24
        top_btns = [
            ("刷新", 52, self.refresh_all, False),
            ("同步用量", 76, self.sync_token_usage, False),
            ("刷限额", 64, self.refresh_all_quotas, False),
            ("刷凭证", 64, self.refresh_all_auth, True),
        ]
        for text, w, cmd, primary in reversed(top_btns):
            ctk.CTkButton(
                header,
                text=text,
                width=w,
                height=30,
                corner_radius=6,
                fg_color=PRIMARY if primary else BG_SOFT,
                hover_color=PRIMARY_HOVER if primary else BORDER,
                text_color="#FFFFFF" if primary else TEXT,
                border_width=0 if primary else 1,
                border_color=BORDER,
                font=ctk.CTkFont(size=12),
                command=cmd,
            ).pack(side="right", padx=(6, 0))

        # 第二行：状态 + 自动重启（很矮）
        subbar = ctk.CTkFrame(self, fg_color="transparent")
        subbar.pack(fill="x", padx=16, pady=(0, 6))
        self.lbl_status = ctk.CTkLabel(
            subbar,
            text="",
            font=ctk.CTkFont(size=11),
            text_color=SUCCESS,
            anchor="w",
        )
        self.lbl_status.pack(side="left")
        self.chk_restart = ctk.CTkCheckBox(
            subbar,
            text="切换后自动重启",
            font=ctk.CTkFont(size=12),
            text_color=TEXT,
            fg_color=PRIMARY,
            hover_color=PRIMARY_HOVER,
            checkbox_width=18,
            checkbox_height=18,
            command=self._on_toggle_restart,
        )
        self.chk_restart.pack(side="right")
        if self.mgr.config.auto_restart:
            self.chk_restart.select()
        else:
            self.chk_restart.deselect()

        # ========== 主体：列表 + 右侧精简操作 ==========
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=16, pady=(0, 8))

        list_panel = ctk.CTkFrame(
            body,
            fg_color=CARD,
            corner_radius=10,
            border_width=1,
            border_color=BORDER,
        )
        list_panel.pack(side="left", fill="both", expand=True)

        list_header = ctk.CTkFrame(list_panel, fg_color="transparent")
        list_header.pack(fill="x", padx=12, pady=(8, 2))
        ctk.CTkLabel(
            list_header,
            text="已保存账户",
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color=TEXT,
        ).pack(side="left")
        self.lbl_count = ctk.CTkLabel(
            list_header,
            text="0",
            font=ctk.CTkFont(size=12),
            text_color=TEXT_MUTED,
        )
        self.lbl_count.pack(side="left", padx=(8, 0))
        ctk.CTkLabel(
            list_header,
            text="单击选中 · 双击看用量",
            font=ctk.CTkFont(size=11),
            text_color=TEXT_MUTED,
        ).pack(side="right")

        col = ctk.CTkFrame(list_panel, fg_color="transparent")
        col.pack(fill="x", padx=12, pady=(0, 2))
        ctk.CTkLabel(
            col, text="账户 / 套餐 / 状态", width=300, anchor="w",
            font=ctk.CTkFont(size=11), text_color=TEXT_MUTED,
        ).pack(side="left")
        ctk.CTkLabel(
            col, text="周限额", width=138, anchor="w",
            font=ctk.CTkFont(size=11), text_color=TEXT_MUTED,
        ).pack(side="left", padx=(8, 0))
        ctk.CTkLabel(
            col, text="本期用量（悬停看详情）", anchor="w",
            font=ctk.CTkFont(size=11), text_color=TEXT_MUTED,
        ).pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(
            col, text="切换 / 复制 / 限额 / 用量", width=ROW_ACTIONS_W, anchor="e",
            font=ctk.CTkFont(size=11), text_color=TEXT_MUTED,
        ).pack(side="right")

        self.scroll = ctk.CTkScrollableFrame(list_panel, fg_color=BG, corner_radius=8)
        self.scroll.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        # 右侧只放高频入口；行内/顶栏已有的操作不再重复。
        SIDE_W = 164
        side = ctk.CTkFrame(
            body,
            width=SIDE_W,
            fg_color=BG_SIDE,
            corner_radius=10,
            border_width=1,
            border_color=BORDER,
        )
        side.pack(side="right", fill="y", padx=(10, 0))
        side.pack_propagate(False)

        ctk.CTkLabel(
            side,
            text="操作",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=TEXT,
        ).pack(anchor="w", padx=10, pady=(8, 4))

        side_actions = ctk.CTkFrame(
            side,
            fg_color="transparent",
        )
        side_actions.pack(fill="x", padx=4, pady=(0, 8))

        self._side_btn(
            side_actions,
            "OAuth 登录",
            self.start_oauth_login,
            oauth=True,
        )
        self._side_btn(side_actions, "保存当前", self.save_current, primary=True)
        import_btn = self._side_btn(side_actions, "导入账户", lambda: None)
        import_btn.configure(
            command=lambda button=import_btn: self._show_import_menu(button)
        )
        self._side_btn(side_actions, "重命名", self.rename_selected)
        self._side_btn(
            side_actions,
            "删除账户",
            self.delete_selected,
            danger=True,
        )
        more_btn = self._side_btn(side_actions, "更多操作", lambda: None)
        more_btn.configure(
            command=lambda button=more_btn: self._show_more_menu(button)
        )

        ctk.CTkLabel(
            side,
            text="切换、复制、限额、用量\n请用账户行内按钮",
            justify="left",
            font=ctk.CTkFont(size=10),
            text_color=TEXT_MUTED,
        ).pack(anchor="w", padx=10, pady=(2, 8))

        # 底栏路径
        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.pack(fill="x", padx=16, pady=(0, 8))
        self.lbl_path = ctk.CTkLabel(
            footer,
            text="",
            font=ctk.CTkFont(size=10),
            text_color=TEXT_MUTED,
            anchor="w",
        )
        self.lbl_path.pack(fill="x")

    def _side_btn(
        self,
        parent: Any,
        text: str,
        command: Any,
        primary: bool = False,
        danger: bool = False,
        oauth: bool = False,
    ) -> ctk.CTkButton:
        if primary:
            fg, hover, tc, bw, bc = PRIMARY, PRIMARY_HOVER, "#FFFFFF", 0, PRIMARY
        elif oauth:
            fg, hover, tc, bw, bc = OAUTH, OAUTH_HOVER, "#FFFFFF", 0, OAUTH
        elif danger:
            fg, hover, tc, bw, bc = BG, "#FEE2E2", DANGER, 1, "#FECACA"
        else:
            fg, hover, tc, bw, bc = BG, BORDER, TEXT, 1, BORDER
        btn = ctk.CTkButton(
            parent,
            text=text,
            height=32,
            corner_radius=6,
            fg_color=fg,
            hover_color=hover,
            text_color=tc,
            border_width=bw,
            border_color=bc,
            font=ctk.CTkFont(size=13),
            command=command,
        )
        btn.pack(fill="x", padx=6, pady=3)
        return btn

    def _show_popup_menu(
        self,
        anchor: Any,
        items: list[tuple[str, Any | None]],
    ) -> None:
        menu = tk.Menu(self, tearoff=False)
        for label, command in items:
            if command is None:
                menu.add_separator()
            else:
                menu.add_command(label=label, command=command)
        try:
            x = anchor.winfo_rootx()
            y = anchor.winfo_rooty() + anchor.winfo_height()
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def _show_import_menu(self, anchor: Any) -> None:
        self._show_popup_menu(
            anchor,
            [
                ("导入单个 auth.json", self.import_file),
                ("批量导入 auth.json", self.import_files),
                ("粘贴 auth.json", self.import_paste),
                ("扫描本机备份", self.scan_backups),
            ],
        )

    def _show_more_menu(self, anchor: Any) -> None:
        self._show_popup_menu(
            anchor,
            [
                ("复制选中凭证", self.copy_selected_auth),
                ("导出选中凭证", self.export_selected_auth),
                ("刷新选中凭证", self.refresh_selected_auth),
                ("刷新选中限额", self.refresh_selected_quota),
                ("查看选中用量", self.show_selected_usage),
                ("", None),
                ("同步本地用量", self.sync_token_usage),
                ("重启 Codex", self.restart_only),
                ("打开 Codex 目录", self.open_codex_home),
            ],
        )

    # ---------- helpers ----------
    def set_status(self, text: str, ok: bool = True) -> None:
        self.lbl_status.configure(text=text, text_color=SUCCESS if ok else DANGER)

    def set_busy(self, busy: bool, msg: str = "") -> None:
        self._busy = busy
        if msg:
            self.set_status(msg, ok=True)
        if not busy and self._focus_refresh_pending:
            self._focus_refresh_pending = False
            try:
                self.after(150, self._request_focus_quota_refresh)
            except tk.TclError:
                pass

    def _on_toggle_restart(self) -> None:
        self.mgr.config.auto_restart = bool(self.chk_restart.get())
        self.mgr.save_config()

    def refresh_ui_light(
        self,
        *,
        keep_selection: bool = True,
        load_usage: bool = True,
    ) -> None:
        """只在原位同步界面，不拉网、不销毁已有账户行。"""
        sel = self._selected_id if keep_selection else None
        if load_usage:
            self._refresh_usage_cache()
        self._render_live()
        self._render_list()
        if sel and sel in self._card_widgets:
            self._selected_id = sel
            self._highlight_selected()
        elif self.mgr.detect_active_match():
            self._selected_id = self.mgr.detect_active_match().id  # type: ignore[union-attr]
            self._highlight_selected()
        self.lbl_path.configure(
            text=f"Codex 目录: {self.mgr.codex_home}    |    账户库: {self.mgr.store_dir}"
        )

    def is_codex_running_cached(self, ttl: float = 5.0) -> bool:
        """缓存 tasklist 结果，避免每次刷新都卡半秒。"""
        now = time.time()
        if self._codex_running_cache and now - self._codex_running_cache[0] < ttl:
            return self._codex_running_cache[1]
        running = self.mgr.is_codex_running()
        self._codex_running_cache = (now, running)
        return running

    def refresh_all(self) -> None:
        try:
            # 同步当前登录：自动导入/更新换号后的账户
            live_profile = self.mgr.sync_live_account_into_store(source="refresh")
            if live_profile:
                self._selected_id = live_profile.id
        except Exception:
            try:
                self.mgr.sync_active_from_live()
            except Exception:
                pass
        self.refresh_ui_light(keep_selection=True, load_usage=False)
        self._refresh_usage_cache_async()
        marker = "正在后台检查 Codex…"
        self.set_status(marker)
        self._refresh_codex_running_async(expected_status=marker)

    def _build_usage_cache(self) -> dict[str, dict]:
        """只缓存各账户当前限额周期的本地用量；全部历史按需在详情中查询。"""
        cache: dict[str, dict] = {}
        try:
            store = self.mgr.token_store()
            for p in self.mgr.list_profiles():
                if profile_uses_api_key(p):
                    cache[p.id] = {
                        "period_available": False,
                        "reason": "API Key 没有 ChatGPT 订阅限额周期",
                    }
                    continue
                bounds = quota_window_bounds(p)
                if bounds is None:
                    cache[p.id] = {
                        "period_available": False,
                        "reason": "限额周期尚未查询，或缓存周期已经结束",
                    }
                    continue
                start_epoch, reset_epoch = bounds
                try:
                    period = store.summarize(
                        profile_id=p.id,
                        account_id=p.account_id or "",
                        email=p.email or "",
                        since_epoch=start_epoch,
                    )
                    period.update(
                        {
                            "period_available": True,
                            "period_label": "本周"
                            if "周" in (p.week_window_label or "")
                            else "本期",
                            "period_start_epoch": start_epoch,
                            "period_reset_epoch": reset_epoch,
                        }
                    )
                    cache[p.id] = period
                except Exception:
                    cache[p.id] = {
                        "period_available": False,
                        "reason": "读取本期限额用量失败",
                    }
        except Exception:
            pass
        return cache

    def _refresh_usage_cache(self) -> None:
        self._usage_cache = self._build_usage_cache()

    def _refresh_usage_cache_async(self) -> None:
        """后台读取 SQLite 汇总；首屏和后台同步完成时都不阻塞 Tk 主线程。"""
        if self._usage_cache_loading:
            return
        self._usage_cache_loading = True

        def work() -> None:
            cache = self._build_usage_cache()

            def apply() -> None:
                self._usage_cache_loading = False
                self._usage_cache = cache
                self._update_usage_views()

            try:
                self.after(0, apply)
            except Exception:
                self._usage_cache_loading = False

        threading.Thread(target=work, daemon=True).start()

    def _refresh_codex_running_async(self, *, expected_status: str = "") -> None:
        """后台运行 tasklist；只有状态栏未被新操作改写时才显示结果。"""
        self._codex_check_generation += 1
        generation = self._codex_check_generation

        def work() -> None:
            try:
                running = self.mgr.is_codex_running()
            except Exception:
                running = False

            def apply() -> None:
                if generation != self._codex_check_generation:
                    return
                self._codex_running_cache = (time.time(), running)
                current = str(self.lbl_status.cget("text") or "")
                if not expected_status or current == expected_status:
                    self.set_status(f"Codex {'运行中' if running else '未运行'}", ok=True)

            try:
                self.after(0, apply)
            except Exception:
                pass

        threading.Thread(target=work, daemon=True).start()

    def _render_live(self) -> None:
        info = self.mgr.live_account_info()
        if not info.get("exists"):
            self.lbl_live_main.configure(text="当前：未登录")
            self.lbl_live_sub.configure(text="")
            return
        email = info.get("email") or "(无邮箱)"
        plan = (info.get("plan") or "").upper() or "?"
        # 标题旁一行：当前：邮箱  PLUS
        self.lbl_live_main.configure(text=f"当前：{email}")
        bits = [plan]
        match = self.mgr.detect_active_match()
        if match and match.week_left_percent is not None:
            if match.limit_reached or match.week_left_percent <= 0:
                bits.append("周已用尽")
            else:
                bits.append(f"周剩{match.week_left_percent:.0f}%")
            if match.week_reset_at:
                bits.append(f"重置{format_reset_local(match.week_reset_at)}")
        elif match and match.usage_error:
            bits.append("限额查询失败")
        self.lbl_live_sub.configure(text=" · ".join(bits))

    def _render_list(self) -> None:
        """差异同步账户行；已有行原位更新，避免整表销毁造成闪烁。"""
        profiles = self.mgr.list_profiles()
        self.lbl_count.configure(text=f"{len(profiles)} 个")

        wanted_ids = {profile.id for profile in profiles}
        for profile_id in list(self._card_widgets):
            if profile_id in wanted_ids:
                continue
            card = self._card_widgets.pop(profile_id)
            self._card_views.pop(profile_id, None)
            card.destroy()

        if not profiles:
            self._selected_id = None
            if self._empty_state is None:
                empty = ctk.CTkFrame(self.scroll, fg_color="transparent")
                empty.pack(fill="x", pady=40)
                ctk.CTkLabel(
                    empty,
                    text="还没有保存任何账户",
                    font=ctk.CTkFont(size=15, weight="bold"),
                    text_color=TEXT,
                ).pack()
                ctk.CTkLabel(
                    empty,
                    text="请先在 Codex 登录一个账号，然后点右侧「保存当前登录为账户」",
                    font=ctk.CTkFont(size=12),
                    text_color=TEXT_MUTED,
                ).pack(pady=(6, 0))
                self._empty_state = empty
            return

        if self._empty_state is not None:
            self._empty_state.destroy()
            self._empty_state = None

        # 当前匹配
        live_match = self.mgr.detect_active_match()
        live_id = live_match.id if live_match else None

        # 最近使用优先
        profiles_sorted = sorted(
            profiles,
            key=lambda p: (p.last_used_at or p.updated_at or p.created_at or ""),
            reverse=True,
        )

        # 只创建新增账户。逆序插入可让新增项出现在正确位置，同时不移动已有行。
        insert_before: ctk.CTkFrame | None = None
        for profile in reversed(profiles_sorted):
            card = self._card_widgets.get(profile.id)
            if card is None:
                self._add_card(
                    profile,
                    is_live=(profile.id == live_id),
                    before=insert_before,
                )
                card = self._card_widgets[profile.id]
            insert_before = card

        # 凭证、限额、当前标记和用量都直接改现有控件，不重建列表。
        for profile in profiles_sorted:
            self._update_card_view(profile, is_live=(profile.id == live_id))

        if self._selected_id and self._selected_id in self._card_widgets:
            self._highlight_selected()
        elif profiles_sorted:
            self._selected_id = profiles_sorted[0].id
            self._highlight_selected()

    def _quota_brief(self, profile: AccountProfile) -> tuple[str, str, str]:
        """返回限额胶囊的 (文本, 文字色, 背景色)。"""
        if profile_uses_api_key(profile) or profile.week_window_label == "API":
            return "密钥模式", TEXT_MUTED, "#F3F4F6"
        # 最近查询失败：直接失败，不显示过期数字
        if profile.usage_error:
            return "查询失败", DANGER, "#FEE2E2"
        left = profile.week_left_percent
        label = profile.week_window_label or "额度"
        if left is not None:
            if profile.limit_reached or left <= 0:
                text = f"{label}已用尽"
                return text, DANGER, "#FEE2E2"
            color = left_color(left)
            text = f"{label}剩 {left:.0f}%"
            if left <= 10:
                return text, color, "#FEE2E2"
            if left <= 40:
                return text, color, "#FEF3C7"
            return text, color, "#D1FAE5"
        return "未查询", TEXT_MUTED, "#F3F4F6"

    def _quota_detail_text(self, profile: AccountProfile) -> str:
        if profile_uses_api_key(profile):
            return "API Key 按量计费，没有 ChatGPT 订阅周限额"
        if profile.usage_error:
            return f"限额查询失败：{profile.usage_error[:100]}"
        if profile.week_left_percent is None:
            return "限额尚未查询，点击该行「限额」获取"
        used = profile.week_used_percent
        lines = [f"剩余 {profile.week_left_percent:.0f}%"]
        if used is not None:
            lines.append(f"已用 {used:.0f}%")
        reset = format_reset_local(profile.week_reset_at)
        if reset:
            lines.append(f"重置时间 {reset}")
        return "\n".join(lines)

    @staticmethod
    def _auth_badge_style(
        profile: AccountProfile,
        *,
        is_live: bool,
    ) -> tuple[str, str, str]:
        """当前账户占用唯一状态位，不再同时显示“有效”和“当前”。"""
        if is_live:
            return "当前", PRIMARY, "#DBEAFE"
        label, kind = profile.auth_badge()
        if kind == "ok":
            return label, SUCCESS, "#D1FAE5"
        if kind == "warn":
            return label, WARN, "#FEF3C7"
        if kind == "bad":
            return label, DANGER, "#FEE2E2"
        return label, TEXT_MUTED, "#F3F4F6"

    def _token_total_text(self, profile: AccountProfile) -> str:
        if profile_uses_api_key(profile):
            return "无订阅周期"
        data = self._usage_cache.get(profile.id)
        if data is None:
            return "本期 …"
        if not data.get("period_available"):
            return "本期 —"
        ut = data.get("totals") or {}
        label = str(data.get("period_label") or "本期")
        if not ut.get("turns"):
            return f"{label} —"
        return f"{label} {format_tokens(ut.get('total_tokens'))}"

    def _token_badge_style(self, profile: AccountProfile) -> tuple[str, str]:
        data = self._usage_cache.get(profile.id) or {}
        if profile_uses_api_key(profile) or not data.get("period_available"):
            return TEXT_MUTED, "#F3F4F6"
        if not (data.get("totals") or {}).get("turns"):
            return TEXT_MUTED, "#F3F4F6"
        return PRIMARY, "#DBEAFE"

    def _token_detail_text(self, profile: AccountProfile) -> str:
        data = self._usage_cache.get(profile.id) or {}
        if not data.get("period_available"):
            reason = data.get("reason") or "正在读取限额周期"
            return f"{reason}\n点击「用量」查看该账户全部历史用量"
        ut = data.get("totals") or {}
        models = data.get("by_model") or []
        label = str(data.get("period_label") or "本期")
        start = time.strftime(
            "%m-%d %H:%M",
            time.localtime(float(data.get("period_start_epoch") or 0)),
        )
        reset = time.strftime(
            "%m-%d %H:%M",
            time.localtime(float(data.get("period_reset_epoch") or 0)),
        )
        if not ut.get("turns"):
            return (
                f"{label}暂无已归属用量（{start} → {reset}）\n"
                "点击「用量」查看该账户全部历史用量"
            )
        top = "\n".join(
            f"  · {m.get('model')}: 调用 {m.get('turns')} 次 / 合计 {format_tokens(m.get('total_tokens'))}"
            for m in models[:8]
        ) or "  · 无模型明细"
        return (
            f"{label}用量（{start} → {reset}）\n"
            f"调用 {ut.get('turns', 0)} 次\n"
            f"输入 {format_tokens(ut.get('input_tokens'))}\n"
            f"缓存 {format_tokens(ut.get('cached_input_tokens'))}\n"
            f"输出 {format_tokens(ut.get('output_tokens'))}\n"
            f"推理 {format_tokens(ut.get('reasoning_output_tokens'))}\n"
            f"合计 {format_tokens(ut.get('total_tokens'))}\n"
            f"按模型：\n{top}\n"
            "点击「用量」查看全部历史\n"
            "注：这里是本机已归属 token；官方限额百分比以左侧限额为准"
        )

    def _add_card(
        self,
        profile: AccountProfile,
        is_live: bool,
        before: ctk.CTkFrame | None = None,
    ) -> None:
        """单行：账户 | 限额胶囊 | 本期用量胶囊 | 固定操作按钮。"""
        card = ctk.CTkFrame(
            self.scroll,
            fg_color=BG_SOFT,
            corner_radius=8,
            border_width=1,
            border_color=BORDER,
            height=42,
        )
        pack_options: dict[str, Any] = {"fill": "x", "pady": 2, "padx": 2}
        if before is not None:
            pack_options["before"] = before
        card.pack(**pack_options)
        card.pack_propagate(False)

        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="both", expand=True, padx=6, pady=4)

        # 先 pack 右侧操作（固定宽），保证按钮永远可见
        actions = ctk.CTkFrame(row, fg_color="transparent", width=ROW_ACTIONS_W)
        actions.pack(side="right", fill="y")
        actions.pack_propagate(False)

        btn_h = 28
        action_btn = ctk.CTkButton(
            actions,
            text="",
            width=56,
            height=btn_h,
            corner_radius=6,
            fg_color=PRIMARY,
            hover_color=PRIMARY_HOVER,
            font=ctk.CTkFont(size=12),
        )
        action_btn.pack(side="left", padx=(0, 4))
        ctk.CTkButton(
            actions,
            text="复制",
            width=48,
            height=btn_h,
            corner_radius=6,
            fg_color=BG,
            hover_color=BORDER,
            text_color=TEXT,
            border_width=1,
            border_color=BORDER,
            font=ctk.CTkFont(size=12),
            command=lambda pid=profile.id: self.copy_auth(pid),
        ).pack(side="left", padx=(0, 4))
        ctk.CTkButton(
            actions,
            text="限额",
            width=48,
            height=btn_h,
            corner_radius=6,
            fg_color=BG,
            hover_color=BORDER,
            text_color=TEXT,
            border_width=1,
            border_color=BORDER,
            font=ctk.CTkFont(size=12),
            command=lambda pid=profile.id: self.refresh_one_quota(pid),
        ).pack(side="left", padx=(0, 4))
        ctk.CTkButton(
            actions,
            text="用量",
            width=48,
            height=btn_h,
            corner_radius=6,
            fg_color=BG,
            hover_color=BORDER,
            text_color=TEXT,
            border_width=1,
            border_color=BORDER,
            font=ctk.CTkFont(size=12),
            command=lambda pid=profile.id: self.show_usage(pid),
        ).pack(side="left")

        # 左：账户
        left = ctk.CTkFrame(row, fg_color="transparent")
        left.pack(side="left", fill="y")

        title = profile.email or profile.display_title()
        if len(title) > 22:
            title = title[:21] + "…"
        title_lbl = ctk.CTkLabel(
            left,
            text=title,
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=TEXT,
            width=168,
            anchor="w",
        )
        title_lbl.pack(side="left")

        plan = (profile.plan or "").upper() or (
            "密钥" if profile_uses_api_key(profile) else "-"
        )
        plan_lbl = ctk.CTkLabel(
            left,
            text=plan,
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=PRIMARY,
            fg_color="#EFF6FF",
            corner_radius=4,
            width=48,
        )
        plan_lbl.pack(side="left", padx=(4, 0))

        auth_label, a_tc, a_fg = self._auth_badge_style(
            profile,
            is_live=is_live,
        )
        auth_lbl = ctk.CTkLabel(
            left,
            text=auth_label,
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=a_tc,
            fg_color=a_fg,
            corner_radius=4,
            width=44,
        )
        auth_lbl.pack(side="left", padx=(4, 0))

        # 中：限额与本期用量分别作为信息胶囊，避免两段普通文字挤在一起。
        mid = ctk.CTkFrame(row, fg_color="transparent")
        mid.pack(side="left", fill="both", expand=True, padx=(8, 6))

        q_text, q_color, q_bg = self._quota_brief(profile)
        quota_lbl = ctk.CTkLabel(
            mid,
            text=q_text,
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=q_color,
            fg_color=q_bg,
            corner_radius=6,
            width=128,
            height=28,
            anchor="center",
        )
        quota_lbl.pack(side="left")
        HoverTip(quota_lbl, lambda p=profile: self._quota_detail_text(p))

        usage_color, usage_bg = self._token_badge_style(profile)
        usage_lbl = ctk.CTkLabel(
            mid,
            text=self._token_total_text(profile),
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=usage_color,
            fg_color=usage_bg,
            corner_radius=6,
            width=118,
            height=28,
            anchor="center",
            cursor="hand2",
        )
        usage_lbl.pack(side="left", padx=(6, 0))
        HoverTip(usage_lbl, lambda p=profile: self._token_detail_text(p))

        def on_select(_event: Any = None, pid: str = profile.id) -> None:
            self._selected_id = pid
            self._highlight_selected()

        for w in (card, row, left, mid):
            w.bind("<Button-1>", on_select)
        card.bind("<Double-Button-1>", lambda _e, pid=profile.id: self.show_usage(pid))
        usage_lbl.bind("<Double-Button-1>", lambda _e, pid=profile.id: self.show_usage(pid))

        self._card_widgets[profile.id] = card
        self._card_views[profile.id] = {
            "action": action_btn,
            "title": title_lbl,
            "plan": plan_lbl,
            "auth": auth_lbl,
            "quota": quota_lbl,
            "usage": usage_lbl,
        }
        self._update_card_view(profile, is_live=is_live)

    def _update_usage_views(self) -> None:
        """只更新各行的本地用量文本，不触碰列表结构或当前标记。"""
        for profile in self.mgr.list_profiles():
            view = self._card_views.get(profile.id)
            if view:
                usage_color, usage_bg = self._token_badge_style(profile)
                view["usage"].configure(
                    text=self._token_total_text(profile),
                    text_color=usage_color,
                    fg_color=usage_bg,
                )

    def _update_card_view(self, profile: AccountProfile, *, is_live: bool) -> bool:
        """原位更新一个账户卡片；不销毁列表，也不触发其他账户联网刷新。"""
        view = self._card_views.get(profile.id)
        if not view:
            return False

        title = profile.email or profile.display_title()
        if len(title) > 22:
            title = title[:21] + "…"
        view["title"].configure(text=title)

        plan = (profile.plan or "").upper() or (
            "密钥" if profile_uses_api_key(profile) else "-"
        )
        view["plan"].configure(text=plan)

        auth_label, a_tc, a_fg = self._auth_badge_style(
            profile,
            is_live=is_live,
        )
        view["auth"].configure(text=auth_label, text_color=a_tc, fg_color=a_fg)

        invalid = (profile.auth_status or "").lower() == "invalid"
        action = view["action"]
        if invalid:
            action.configure(
                text="授权",
                state="normal",
                fg_color=OAUTH,
                hover_color=OAUTH_HOVER,
                command=lambda pid=profile.id: self.reauth_profile(pid),
            )
        else:
            action.configure(
                text="使用中" if is_live else "切换",
                state="disabled" if is_live else "normal",
                fg_color="#93C5FD" if is_live else PRIMARY,
                hover_color=PRIMARY_HOVER,
                command=lambda pid=profile.id: self.switch_to(pid),
            )

        q_text, q_color, q_bg = self._quota_brief(profile)
        view["quota"].configure(
            text=q_text,
            text_color=q_color,
            fg_color=q_bg,
        )
        usage_color, usage_bg = self._token_badge_style(profile)
        view["usage"].configure(
            text=self._token_total_text(profile),
            text_color=usage_color,
            fg_color=usage_bg,
        )
        return True

    def _update_profile_card(self, profile_id: str, *, update_live: bool = False) -> None:
        """更新指定卡片；切换时只额外更新各卡片的“当前/切换”状态。"""
        profile = self.mgr.get_profile(profile_id)
        if not profile or profile_id not in self._card_views:
            self.refresh_ui_light(keep_selection=True, load_usage=False)
            return
        live = self.mgr.detect_active_match()
        live_id = live.id if live else None
        if update_live:
            for item in self.mgr.list_profiles():
                self._update_card_view(item, is_live=(item.id == live_id))
            self._render_live()
        else:
            self._update_card_view(profile, is_live=(profile.id == live_id))
            if profile.id == live_id:
                self._render_live()
        self._highlight_selected()

    def _highlight_selected(self) -> None:
        for pid, card in self._card_widgets.items():
            if pid == self._selected_id:
                card.configure(border_color=ACTIVE_BORDER, fg_color=ACTIVE_BG, border_width=2)
            else:
                card.configure(border_color=BORDER, fg_color=BG_SOFT, border_width=1)

    # ---------- actions ----------
    def save_current(self) -> None:
        info = self.mgr.live_account_info()
        if not info.get("exists"):
            messagebox.showerror("错误", f"未找到当前登录文件：\n{self.mgr.auth_path}")
            return
        default_name = info.get("email") or info.get("name") or "我的账户"
        name = simpledialog.askstring(
            "保存当前登录",
            "给这个账户起个名字（方便识别）：",
            initialvalue=default_name,
            parent=self,
        )
        if name is None:
            return
        name = name.strip() or default_name
        try:
            profile = self.mgr.save_current_as(name=name, make_active=True)
            self._selected_id = profile.id
            self.refresh_ui_light(keep_selection=True, load_usage=False)
            self._refresh_usage_cache_async()
            self.set_status(f"已保存：{profile.display_title()}")
            messagebox.showinfo(
                "已保存",
                f"账户「{profile.display_title()}」已保存。\n"
                f"邮箱：{profile.email or '(未知)'}\n"
                f"套餐：{(profile.plan or '?').upper()}",
            )
        except Exception as e:
            messagebox.showerror("保存失败", str(e))

    def import_file(self) -> None:
        path = filedialog.askopenfilename(
            title="选择 Codex auth.json",
            filetypes=[("JSON", "*.json"), ("All", "*.*")],
        )
        if not path:
            return
        name = simpledialog.askstring(
            "导入账户",
            "账户显示名称（可留空自动识别）：",
            parent=self,
        )
        if name is None:
            return
        try:
            profile = self.mgr.import_auth_file(
                path,
                name=name.strip() or None,
                make_active=False,
            )
            self._selected_id = profile.id
            self.refresh_ui_light(keep_selection=True, load_usage=False)
            self._refresh_usage_cache_async()
            self.set_status(f"已导入：{profile.display_title()}")
            if messagebox.askyesno("查询限额", "导入成功。是否立即查询该账户周限额？"):
                self.refresh_one_quota(profile.id)
        except Exception as e:
            messagebox.showerror("导入失败", str(e))

    def import_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="选择一个或多个 auth.json",
            filetypes=[("JSON", "*.json"), ("All", "*.*")],
        )
        if not paths:
            return
        try:
            profiles = self.mgr.import_auth_files(list(paths))
            if profiles:
                self._selected_id = profiles[0].id
            self.refresh_ui_light(keep_selection=True, load_usage=False)
            self._refresh_usage_cache_async()
            self.set_status(f"已批量导入 {len(profiles)} 个账户")
            if messagebox.askyesno(
                "查询限额",
                f"已导入 {len(profiles)} 个账户。\n是否立即刷新全部周限额？",
            ):
                self.refresh_all_quotas()
        except Exception as e:
            messagebox.showerror("批量导入失败", str(e))

    def import_paste(self) -> None:
        PasteAuthDialog(self, on_done=self._after_paste_import)

    def _after_paste_import(self, count: int) -> None:
        self.refresh_ui_light(keep_selection=True, load_usage=False)
        self._refresh_usage_cache_async()
        self.set_status(f"粘贴导入 {count} 个")
        if count > 0 and messagebox.askyesno("查询限额", "是否立即刷新全部周限额？"):
            self.refresh_all_quotas()

    def refresh_selected_quota(self) -> None:
        if not self._selected_id:
            messagebox.showwarning("提示", "请先选中一个账户")
            return
        self.refresh_one_quota(self._selected_id)

    def copy_selected_auth(self) -> None:
        if not self._selected_id:
            messagebox.showwarning("提示", "请先选中一个账户")
            return
        self.copy_auth(self._selected_id)

    def copy_auth(self, profile_id: str) -> None:
        profile = self.mgr.get_profile(profile_id)
        if not profile:
            messagebox.showerror("错误", "账户不存在")
            return
        try:
            text = self.mgr.get_profile_auth_text(profile_id, pretty=True)
        except Exception as e:
            messagebox.showerror("复制失败", str(e))
            return
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
            self.update()  # 确保剪贴板内容保留
        except Exception as e:
            messagebox.showerror("剪贴板失败", str(e))
            return
        self._selected_id = profile_id
        self._highlight_selected()
        self.set_status(f"已复制 auth：{profile.display_title()}")
        # 轻量提示，不打断操作
        self.bell()

    def export_selected_auth(self) -> None:
        if not self._selected_id:
            messagebox.showwarning("提示", "请先选中一个账户")
            return
        profile = self.mgr.get_profile(self._selected_id)
        if not profile:
            return
        safe = "".join(c if c.isalnum() or c in "-_@." else "_" for c in profile.display_title())[:40]
        path = filedialog.asksaveasfilename(
            title="导出 auth.json",
            defaultextension=".json",
            initialfile=f"auth_{safe or profile.id[:8]}.json",
            filetypes=[("JSON", "*.json"), ("All", "*.*")],
        )
        if not path:
            return
        try:
            self.mgr.export_profile_auth(profile.id, path)
            self.set_status(f"已导出：{path}")
            messagebox.showinfo("已导出", f"已保存到：\n{path}")
        except Exception as e:
            messagebox.showerror("导出失败", str(e))

    def refresh_selected_auth(self) -> None:
        if not self._selected_id:
            messagebox.showwarning("提示", "请先选中一个账户")
            return
        self.refresh_one_auth(self._selected_id, force=True)

    def refresh_one_auth(self, profile_id: str, force: bool = True) -> None:
        if self._busy:
            return
        self.set_busy(True, "刷新 Auth 中…")

        def work() -> None:
            try:
                result = self.mgr.check_or_refresh_profile_auth(
                    profile_id, force=force, also_usage=True
                )
                self.after(0, lambda: self._on_auth_one_done(result))
            except Exception as e:
                err = str(e)
                self.after(
                    0,
                    lambda err=err: self._on_auth_one_done(
                        {"ok": False, "profile_id": profile_id, "status": "invalid", "error": err}
                    ),
                )

        threading.Thread(target=work, daemon=True).start()

    def _on_auth_one_done(self, result: dict[str, Any]) -> None:
        self.set_busy(False)
        profile_id = str(result.get("profile_id") or self._selected_id or "")
        if profile_id:
            self._selected_id = profile_id
            self._update_profile_card(profile_id)
            self._refresh_usage_cache_async()
        status = result.get("status") or ("ok" if result.get("ok") else "invalid")
        if result.get("ok"):
            extra = "（已续期）" if result.get("refreshed") else ""
            self.set_status(f"Auth 有效{extra}")
        else:
            self.set_status(f"Auth {status}: {result.get('error') or ''}"[:80], ok=False)

    def refresh_all_auth(self) -> None:
        if self._busy:
            return
        if not self.mgr.list_profiles():
            messagebox.showinfo("提示", "还没有账户")
            return
        if not messagebox.askyesno(
            "批量刷新 Auth",
            "将强制刷新全部 ChatGPT 账户的 token，并同步查询周限额。\n"
            "API Key 账户会跳过。\n\n是否继续？",
        ):
            return
        self._run_auth_batch(force=True, title="批量刷新 Auth")

    def startup_auth_check(self) -> None:
        """
        启动轻量后台任务（不阻塞 UI）：
        1) 本地同步当前 auth → 列表
        2) 主窗口在前台时静默刷新全部账户限额
        3) 延迟再同步会话用量（可选，后台）
        """
        if self._startup_checked:
            return
        self._startup_checked = True

        # tasklist 在部分 Windows 机器上会耗时 1 秒以上，绝不在 Tk 主线程执行。
        self._refresh_codex_running_async(expected_status="正在后台同步…")

        def work() -> None:
            live = None
            status_msg = "就绪"
            try:
                live = self.mgr.sync_live_account_into_store(source="startup")
                if live:
                    status_msg = f"已同步当前：{live.email or live.display_title()}"
            except Exception as e:
                status_msg = f"同步当前账户失败：{e}"

            def apply_local() -> None:
                if live:
                    self._selected_id = live.id
                self.refresh_ui_light(keep_selection=True, load_usage=False)
                self.set_status(status_msg or "就绪", ok=("失败" not in (status_msg or "")))
                self._refresh_usage_cache_async()
                # 启动也算一次聚焦。复用前台状态探测，避免 Map 事件与
                # 启动回调各触发一轮相同的限额请求。
                if not self._window_was_foreground:
                    self._on_window_focus_event()
                # 限额刷新期间先等待，避免启动用量同步被 _busy 直接丢弃。
                self.after(1200, self._sync_startup_usage_when_idle)

            self.after(0, apply_local)

        threading.Thread(target=work, daemon=True).start()

    def sync_token_usage(self, quiet: bool = False) -> None:
        # 用户已开始 OAuth/切换/刷新时，放弃这次启动期静默同步，避免抢状态栏和磁盘。
        if self._busy:
            return
        if not quiet:
            self.set_busy(True, "同步会话用量…")
        else:
            self.set_status("后台同步会话用量…")

        def work() -> None:
            try:
                # 启动后台同步只处理新增事件；手动点击时才执行一次全量修复归属。
                result = self.mgr.sync_token_usage(reattribute=not quiet)
                self.after(0, lambda: self._on_usage_sync_done(result, None, quiet))
            except Exception as e:
                err = str(e)
                self.after(0, lambda err=err: self._on_usage_sync_done(None, err, quiet))

        threading.Thread(target=work, daemon=True).start()

    def _on_usage_sync_done(
        self,
        result: dict[str, Any] | None,
        err: str | None,
        quiet: bool,
    ) -> None:
        if not quiet:
            self.set_busy(False)
        self._refresh_usage_cache_async()
        if err:
            self.set_status("用量同步失败", ok=False)
            if not quiet:
                messagebox.showerror("同步失败", err)
            return
        assert result is not None
        attr = result.get("attribution") or {}
        msg = (
            f"用量同步: 扫描 {result.get('scanned_files', 0)} 文件, "
            f"新增 {result.get('inserted_events', 0)} 条"
        )
        if result.get("reattributed"):
            msg += f", 重归属 {result.get('reattributed')}"
        if result.get("cleared_to_unknown"):
            msg += f", 清误归属 {result.get('cleared_to_unknown')}"
        self.set_status(msg)
        if not quiet:
            g = self.mgr.token_store().global_summary()
            lines = [
                msg,
                "",
                "说明：只统计「本工具切换/记录之后」能明确归属到账户的用量。",
                "更早的历史会话无法区分账户，会计入「未归属」。",
                "",
                f"事件总数 {attr.get('total_events', 0)}  ·  "
                f"已归属 {attr.get('attributed_events', 0)}  ·  "
                f"未归属 {attr.get('unknown_events', 0)}",
                "",
                "按账户（仅已归属）：",
            ]
            shown = 0
            for a in g.get("by_account") or []:
                label = str(a.get("label") or "")
                if label in ("(unknown)", "") or not (
                    a.get("account_id") or a.get("email") or a.get("profile_id")
                ):
                    continue
                shown += 1
                lines.append(
                    f"· {label}: 调用 {a.get('turns')} 次  "
                    f"合计 {format_tokens(a.get('total_tokens'))}  "
                    f"输入 {format_tokens(a.get('input_tokens'))}  "
                    f"输出 {format_tokens(a.get('output_tokens'))}  "
                    f"缓存 {format_tokens(a.get('cached_input_tokens'))}"
                )
            if shown == 0:
                lines.append("· 暂无已归属账户用量（切换账户后再用 Codex，然后同步）")
            messagebox.showinfo("用量同步完成", "\n".join(lines))

    def show_selected_usage(self) -> None:
        if not self._selected_id:
            messagebox.showwarning("提示", "请先选中一个账户")
            return
        self.show_usage(self._selected_id)

    def show_usage(self, profile_id: str) -> None:
        profile = self.mgr.get_profile(profile_id)
        if not profile:
            return
        try:
            data = self.mgr.get_profile_token_usage(profile_id)
        except Exception as e:
            messagebox.showerror("读取失败", str(e))
            return
        UsageDetailDialog(self, profile, data)

    def _run_auth_batch(self, *, force: bool, title: str, quiet: bool = False) -> None:
        if self._busy:
            return
        self.set_busy(True, f"{title}…")

        def work() -> None:
            try:
                results = self.mgr.check_or_refresh_all_auth(force=force, also_usage=True)
                self.after(0, lambda: self._on_auth_batch_done(results, None, title, quiet))
            except Exception as e:
                err = str(e)
                self.after(0, lambda err=err: self._on_auth_batch_done(None, err, title, quiet))

        threading.Thread(target=work, daemon=True).start()

    def _on_auth_batch_done(
        self,
        results: list[dict[str, Any]] | None,
        err: str | None,
        title: str,
        quiet: bool,
    ) -> None:
        self.set_busy(False)
        self.refresh_ui_light(keep_selection=True, load_usage=False)
        self._refresh_usage_cache_async()
        if err:
            self.set_status(f"{title}失败", ok=False)
            if not quiet:
                messagebox.showerror(title, err)
            return
        assert results is not None
        ok = sum(1 for r in results if r.get("ok"))
        refreshed = sum(1 for r in results if r.get("refreshed"))
        invalid = sum(1 for r in results if r.get("status") == "invalid")
        expired = sum(1 for r in results if r.get("status") == "expired")
        self.set_status(
            f"{title}完成：有效 {ok}/{len(results)}，续期 {refreshed}，失效 {invalid}",
            ok=(invalid == 0 and expired == 0),
        )
        # 静默启动：仅在有失效/过期时弹窗，避免吓到用户
        if quiet and invalid == 0 and expired == 0:
            return
        lines = [
            f"有效 {ok} / 共 {len(results)}",
            f"自动续期 {refreshed} · 失效 {invalid} · 过期未续上 {expired}",
            "",
        ]
        live = self.mgr.detect_active_match()
        if live:
            left = (
                f"周剩 {live.week_left_percent:.0f}%"
                if live.week_left_percent is not None
                else ("查询失败" if live.usage_error else "未查询")
            )
            lines.append(f"当前登录：{live.email or live.display_title()}  [{live.auth_badge()[0]}]  {left}")
            lines.append("")
        for p in self.mgr.list_profiles():
            if (p.auth_mode or "").lower() in ("apikey", "api_key"):
                continue
            badge, _ = p.auth_badge()
            if p.week_left_percent is not None:
                left = f"周剩 {p.week_left_percent:.0f}%" if p.week_left_percent > 0 and not p.limit_reached else "周已用尽"
            elif p.usage_error:
                left = "限额查询失败"
            else:
                left = "未查询"
            mark = " ←当前" if live and p.id == live.id else ""
            lines.append(f"· [{badge}] {p.email or p.display_title()}  {left}{mark}")
            if p.auth_status == "invalid":
                lines.append("    → 该邮箱需在 Codex 重新登录后，再打开本工具同步")
        if not quiet or invalid or expired:
            messagebox.showinfo(title, "\n".join(lines[:40]))

    def refresh_one_quota(self, profile_id: str) -> None:
        """只刷新指定账户的限额（不会刷全部）。"""
        if self._busy:
            return
        profile = self.mgr.get_profile(profile_id)
        label = (profile.email or profile.display_title()) if profile else profile_id[:8]
        self._selected_id = profile_id
        self.set_busy(True, f"查询限额：{label}…")

        def work() -> None:
            try:
                # 明确只查这一个 profile_id
                result = self.mgr.refresh_profile_usage(profile_id, force_token_refresh=False)
                result = dict(result or {})
                result["profile_id"] = profile_id
                result["label"] = label
                self.after(0, lambda: self._on_quota_one_done(result))
            except Exception as e:
                err = str(e)
                self.after(
                    0,
                    lambda err=err: self._on_quota_one_done(
                        {
                            "ok": False,
                            "profile_id": profile_id,
                            "label": label,
                            "error": err,
                        }
                    ),
                )

        threading.Thread(target=work, daemon=True).start()

    def _on_quota_one_done(self, result: dict[str, Any]) -> None:
        self.set_busy(False)
        # 原位更新指定卡片，不销毁或重建整张账户列表。
        profile_id = str(result.get("profile_id") or self._selected_id or "")
        if profile_id:
            self._selected_id = profile_id
            self._update_profile_card(profile_id)
            self._refresh_usage_cache_async()
        label = result.get("label") or ""
        if result.get("ok"):
            if result.get("api_key"):
                self.set_status(f"{label}：密钥模式无周限额")
            else:
                summary = result.get("summary") or {}
                left = summary.get("week_left_percent")
                if summary.get("limit_reached") or (left is not None and left <= 0):
                    self.set_status(f"{label}：周已用尽")
                elif left is not None:
                    self.set_status(f"{label}：周剩 {left:.0f}%")
                else:
                    self.set_status(f"{label}：限额已更新")
        else:
            err = (result.get("error") or "")[:60]
            self.set_status(f"{label}：限额失败 {err}".strip(), ok=False)

    def reauth_profile(self, profile_id: str) -> None:
        """对失效账户：引导 OAuth，用同一邮箱重新授权。"""
        profile = self.mgr.get_profile(profile_id)
        if not profile:
            return
        email = profile.email or profile.display_title()
        if not messagebox.askyesno(
            "重新授权",
            f"账户「{email}」凭证已失效。\n\n"
            "将打开 OAuth 浏览器登录。\n"
            f"请务必使用该邮箱登录：\n{email}\n\n"
            "授权成功后会更新此账户凭证。是否继续？",
        ):
            return
        self._selected_id = profile_id
        self._oauth_expect_profile_id = profile_id
        self._oauth_expect_email = (profile.email or "").lower()
        OAuthLoginDialog(self, on_done=self._after_oauth_reauth)

    def _after_oauth_reauth(self, result: dict[str, Any] | None) -> None:
        expect_email = getattr(self, "_oauth_expect_email", "") or ""
        self._oauth_expect_profile_id = None
        self._oauth_expect_email = None
        if result and result.get("cancelled"):
            self.set_status("重新授权已取消")
            return
        if not result or not result.get("ok"):
            err = (result or {}).get("error") or "未知错误"
            self.set_status(f"重新授权失败：{err}"[:80], ok=False)
            return
        if result.get("profile_id"):
            self._selected_id = result["profile_id"]
        self.refresh_ui_light(keep_selection=True, load_usage=False)
        got_email = (result.get("email") or "").lower()
        if expect_email and got_email and got_email != expect_email:
            messagebox.showwarning(
                "邮箱不一致",
                f"你登录的是：{result.get('email')}\n"
                f"原先失效账户是：{expect_email}\n\n"
                "已按新登录保存为账户；原失效账户仍在列表中。",
            )
        else:
            self.set_status(f"已重新授权：{result.get('email') or ''}")

    def refresh_all_quotas(self, quiet: bool = False) -> None:
        if self._busy:
            if quiet:
                self._focus_refresh_pending = True
            return
        if not self.mgr.list_profiles():
            if not quiet:
                messagebox.showinfo("提示", "还没有账户可查询")
            return
        if quiet:
            self._focus_quota_refresh_running = True
        self.set_busy(
            True,
            "窗口已聚焦，正在刷新限额…" if quiet else "正在刷新全部限额…",
        )

        def work() -> None:
            try:
                results = self.mgr.refresh_all_usage()
                self.after(
                    0,
                    lambda: self._on_quota_all_done(results, None, quiet),
                )
            except Exception as e:
                err = str(e)
                self.after(
                    0,
                    lambda err=err: self._on_quota_all_done(None, err, quiet),
                )

        threading.Thread(target=work, daemon=True).start()

    def _on_quota_all_done(
        self,
        results: list[dict[str, Any]] | None,
        err: str | None,
        quiet: bool = False,
    ) -> None:
        if quiet:
            self._focus_quota_refresh_running = False
        self.set_busy(False)
        self.refresh_ui_light(keep_selection=True, load_usage=False)
        self._refresh_usage_cache_async()
        if err:
            self.set_status(
                "聚焦后限额刷新失败" if quiet else "刷新限额失败",
                ok=False,
            )
            if not quiet:
                messagebox.showerror("失败", err)
            return
        assert results is not None
        ok = sum(1 for r in results if r.get("ok"))
        fail = len(results) - ok
        prefix = "聚焦自动刷新" if quiet else "限额刷新完成"
        self.set_status(f"{prefix}：成功 {ok}，失败 {fail}", ok=(fail == 0))
        if quiet:
            return
        # 简表
        lines = [f"成功 {ok} / 共 {len(results)}"]
        for p in self.mgr.list_profiles():
            lines.append(f"· {p.display_title()}: {p.usage_line()}")
        messagebox.showinfo("限额刷新完成", "\n".join(lines[:30]))

    def scan_backups(self) -> None:
        """扫描 ~/.codex/backups 里历史 auth.json 并导入可切换账户。"""
        if self._busy:
            return
        if not messagebox.askyesno(
            "扫描历史备份",
            "将扫描 Codex 数据目录 backups 中的历史 auth.json，\n"
            "自动导入可恢复的 ChatGPT 登录与 API Key。\n\n"
            "已存在的同账号会更新快照，不会重复建档。是否继续？",
        ):
            return

        self.set_busy(True, "正在扫描备份…")
        self.update_idletasks()

        def work() -> None:
            try:
                from recover_from_backups import recover

                items = recover(self.mgr)
                self.after(0, lambda: self._on_scan_done(items, None))
            except Exception as e:
                err = str(e)
                self.after(0, lambda err=err: self._on_scan_done(None, err))

        threading.Thread(target=work, daemon=True).start()

    def start_oauth_login(self) -> None:
        """本地 PKCE OAuth：浏览器登录后自动保存独立账户。"""
        if self._busy:
            return
        # 若当前选中失效账户，引导按该邮箱重授权
        sel = self.mgr.get_profile(self._selected_id) if self._selected_id else None
        if sel and (sel.auth_status or "").lower() == "invalid":
            self.reauth_profile(sel.id)
            return
        if not messagebox.askyesno(
            "OAuth 登录",
            "将启动 Codex 浏览器 OAuth 授权：\n\n"
            "1. 只打开一个无痕/隐私浏览器窗口\n"
            "2. 用要添加的 ChatGPT 账户正常登录\n"
            "3. 本机校验回调后自动保存到账户列表\n\n"
            "若要修复「已失效」账户：在列表点该行「授权」，\n"
            "或先选中失效账户再点本按钮。\n\n"
            "不会运行 codex login，不需要设备代码授权，也不会覆盖当前 auth.json。\n"
            "当前 Codex 无需退出或重启。\n"
            "是否继续？",
        ):
            return

        OAuthLoginDialog(self, on_done=self._after_oauth_login)

    def _after_oauth_login(self, result: dict[str, Any] | None) -> None:
        if result and result.get("ok"):
            self._selected_id = result.get("profile_id")
            self.refresh_ui_light(keep_selection=True, load_usage=False)
            self._refresh_usage_cache_async()
            self.set_status(f"OAuth 成功：{result.get('email') or result.get('name')}")
        elif result and result.get("cancelled"):
            self.set_status("OAuth 登录已取消")
        else:
            err = (result or {}).get("error") or "未知错误"
            self.set_status(f"OAuth 失败：{err}"[:80], ok=False)

    def _on_scan_done(self, items: list[dict] | None, err: str | None) -> None:
        self.set_busy(False)
        if err:
            self.set_status("扫描失败", ok=False)
            messagebox.showerror("扫描失败", err)
            return
        assert items is not None
        self.refresh_ui_light(keep_selection=True, load_usage=False)
        self._refresh_usage_cache_async()
        chatgpt = [i for i in items if i.get("email")]
        apikeys = [i for i in items if not i.get("email")]
        lines = [f"共找到/更新 {len(items)} 个可恢复项：", ""]
        if chatgpt:
            lines.append("ChatGPT 登录：")
            for i in chatgpt:
                plan = (i.get("plan") or "?").upper()
                lines.append(f"  · {i.get('email')}  ({plan})")
            lines.append("")
        if apikeys:
            lines.append(f"API Key：{len(apikeys)} 个（列表中显示尾号）")
        self.set_status(f"已导入 {len(items)} 个历史账户")
        messagebox.showinfo("扫描完成", "\n".join(lines))

    def rename_selected(self) -> None:
        if not self._selected_id:
            messagebox.showwarning("提示", "请先选中一个账户")
            return
        profile = self.mgr.get_profile(self._selected_id)
        if not profile:
            return
        name = simpledialog.askstring(
            "重命名",
            "新的账户名称：",
            initialvalue=profile.name,
            parent=self,
        )
        if not name:
            return
        try:
            self.mgr.rename_profile(profile.id, name.strip())
            self._update_profile_card(profile.id)
            self.set_status("已重命名")
        except Exception as e:
            messagebox.showerror("失败", str(e))

    def delete_selected(self) -> None:
        if self._busy:
            return
        if not self._selected_id:
            messagebox.showwarning("提示", "请先选中一个账户")
            return
        profile = self.mgr.get_profile(self._selected_id)
        if not profile:
            return
        live = self.mgr.detect_active_match()
        remove_live_key = bool(
            profile_uses_api_key(profile) and live and live.id == profile.id
        )
        if remove_live_key:
            prompt = (
                f"确定删除 API Key 账户「{profile.display_title()}」？\n\n"
                "该 Key 当前正在使用。删除时将：\n"
                "• 删除账户管理器中的 Key 快照\n"
                "• 关闭 Codex 并清除本机当前 auth.json 中的 Key\n"
                "• 删除身份匹配的切换备份，然后重新打开 Codex\n\n"
                "不会撤销 OpenAI/中转平台后台的远端 Key。"
            )
        else:
            prompt = (
                f"确定删除账户「{profile.display_title()}」？\n\n"
                "将删除账户管理器保存的凭证快照，不影响当前 Codex 登录，"
                "也不会注销账号或撤销远端 API Key。"
            )
        if not messagebox.askyesno(
            "确认删除",
            prompt,
        ):
            return
        profile_id = profile.id
        profile_title = profile.display_title()
        self.set_busy(True, f"正在删除：{profile_title}…")

        def work() -> None:
            try:
                result = self.mgr.delete_profile(
                    profile_id,
                    remove_live_credentials=remove_live_key,
                    relaunch_codex=remove_live_key,
                )
                self.after(
                    0,
                    lambda: self._on_delete_done(profile_title, result, None),
                )
            except Exception as exc:
                error = str(exc)
                self.after(
                    0,
                    lambda error=error: self._on_delete_done(
                        profile_title, None, error
                    ),
                )

        threading.Thread(target=work, daemon=True).start()

    def _on_delete_done(
        self,
        profile_title: str,
        result: dict[str, Any] | None,
        error: str | None,
    ) -> None:
        self.set_busy(False)
        if error:
            self.set_status("删除失败", ok=False)
            messagebox.showerror("删除失败", error)
            return
        assert result is not None
        self._selected_id = None
        # 删除只需本地重绘；不要调用 refresh_all 把仍在 live auth 的快照立即导回来。
        self.refresh_ui_light(keep_selection=False, load_usage=False)
        if result.get("removed_live_credentials"):
            restart = result.get("restart") or {}
            suffix = " · Codex 已重新打开" if restart.get("launched") else ""
            self.set_status(f"已删除本机 Key：{profile_title}{suffix}")
        else:
            self.set_status(f"已删除：{profile_title}")

    def switch_selected(self) -> None:
        if not self._selected_id:
            messagebox.showwarning("提示", "请先选中一个账户")
            return
        self.switch_to(self._selected_id)

    def switch_to(self, profile_id: str) -> None:
        if self._busy:
            return
        profile = self.mgr.get_profile(profile_id)
        if not profile:
            messagebox.showerror("错误", "账户不存在")
            return

        # 已是当前
        match = self.mgr.detect_active_match()
        if match and match.id == profile_id:
            if messagebox.askyesno(
                "已是当前账户",
                "这个账户看起来已经是当前登录。\n是否仍要写入并重启 Codex？",
            ):
                pass
            else:
                return

        restart = bool(self.chk_restart.get())
        api_key_mode = profile_uses_api_key(profile)
        if api_key_mode:
            tip = (
                "即将切换到 API Key 模式：\n\n"
                f"  {profile.display_title()}\n"
                "  认证：API Key（按 API 用量单独计费）\n\n"
                "本次只切换 auth.json 中的密钥，不修改 config.toml 里的 "
                "model_provider / API 地址。\n"
                "当前配置若为 OpenAI，密钥将用于 OpenAI API；自定义代理还需要匹配的供应商配置。\n\n"
            )
        else:
            tip = (
                f"即将切换到 ChatGPT 账户：\n\n"
                f"  {profile.display_title()}\n"
                f"  {profile.email or ''}\n"
                f"  套餐 {(profile.plan or '?').upper()}\n\n"
            )
        if restart:
            tip += "将结束 Codex 相关进程并自动重新启动。\n进行中的对话可能中断，是否继续？"
        else:
            tip += "不会自动重启。请手动完全退出并重新打开 Codex 后生效。\n是否继续？"

        if not messagebox.askyesno("确认切换", tip):
            return

        self.set_busy(True, "正在切换…")
        self.update_idletasks()

        def work() -> None:
            try:
                result = self.mgr.switch_to(profile_id, restart=restart)
                self.after(0, lambda: self._on_switch_done(result, None))
            except Exception as e:
                err = f"{e}\n\n{traceback.format_exc()}"
                self.after(0, lambda: self._on_switch_done(None, err))

        threading.Thread(target=work, daemon=True).start()

    def _on_switch_done(self, result: dict[str, Any] | None, err: str | None) -> None:
        self.set_busy(False)
        if err:
            self.set_status("切换失败", ok=False)
            messagebox.showerror("切换失败", err)
            self.refresh_ui_light(keep_selection=True, load_usage=False)
            return

        assert result is not None
        profile: AccountProfile = result["profile"]
        restart_info = result.get("restart") or {}
        msg = f"已切换到 {profile.display_title()}"
        if profile_uses_api_key(profile):
            msg += " · API Key 按量计费"
        if restart_info.get("restarted"):
            if restart_info.get("launched"):
                msg += " · Codex 已重启"
            else:
                launch = restart_info.get("launch") or {}
                msg += " · 进程已结束，但自动启动可能失败，请手动打开 Codex"
                if launch.get("error"):
                    msg += f"\n{launch.get('error')}"
        self.set_status(msg.split("\n")[0], ok=True)
        self._selected_id = profile.id
        # manager 已完成写入校验；直接原位更新当前标记，不再依赖手动全局刷新。
        self._update_profile_card(profile.id, update_live=True)
        self.update_idletasks()
        self._switch_verify_generation += 1
        generation = self._switch_verify_generation
        self.after(
            2500,
            lambda pid=profile.id, gen=generation: self._verify_switched_profile(
                pid, gen, 1
            ),
        )
        messagebox.showinfo("切换完成", msg)

    def _verify_switched_profile(
        self,
        profile_id: str,
        generation: int,
        attempt: int,
    ) -> None:
        """延迟确认新 Codex 没有在启动阶段把 auth.json 改回旧账户。"""
        if generation != self._switch_verify_generation:
            return
        match = self.mgr.detect_active_match()
        if match and match.id == profile_id:
            self._selected_id = profile_id
            self._update_profile_card(profile_id, update_live=True)
            return
        if attempt < 2:
            self.after(
                2500,
                lambda pid=profile_id, gen=generation: self._verify_switched_profile(
                    pid, gen, 2
                ),
            )
            return

        actual = match.display_title() if match else "未知账户"
        self._update_profile_card(profile_id, update_live=True)
        self.set_status(
            f"切换后凭据被改回 {actual}；请关闭其他 Codex 实例后重试",
            ok=False,
        )

    def restart_only(self) -> None:
        if self._busy:
            return
        if not messagebox.askyesno("重启 Codex", "确定结束并重新启动 Codex 桌面端？"):
            return
        self.set_busy(True, "正在重启…")

        def work() -> None:
            try:
                info = self.mgr.restart_codex()
                self.after(0, lambda: self._on_restart_done(info, None))
            except Exception as e:
                err = str(e)
                self.after(0, lambda err=err: self._on_restart_done(None, err))

        threading.Thread(target=work, daemon=True).start()

    def _on_restart_done(self, info: dict[str, Any] | None, err: str | None) -> None:
        self.set_busy(False)
        if err:
            messagebox.showerror("重启失败", err)
            self.set_status("重启失败", ok=False)
            return
        assert info is not None
        if info.get("launched"):
            self.set_status("Codex 已重启")
            messagebox.showinfo("完成", "Codex 已重新启动。")
        else:
            launch = info.get("launch") or {}
            messagebox.showwarning(
                "部分完成",
                "已尝试结束 Codex 进程，但自动启动可能失败。\n"
                "请从开始菜单手动打开 Codex / ChatGPT。\n"
                f"{launch.get('error') or ''}",
            )
            self.set_status("请手动打开 Codex", ok=False)
        self._render_live()

    def open_codex_home(self) -> None:
        path = self.mgr.codex_home
        path.mkdir(parents=True, exist_ok=True)
        try:
            if sys.platform == "win32":
                import os

                os.startfile(str(path))  # type: ignore[attr-defined]
            else:
                import subprocess

                subprocess.Popen(["xdg-open", str(path)])
        except Exception as e:
            messagebox.showerror("打开失败", str(e))


class HoverTip:
    """鼠标悬停显示详情（延迟显示，避免闪烁）。"""

    def __init__(self, widget: Any, text_fn: Any, delay_ms: int = 350) -> None:
        self.widget = widget
        self.text_fn = text_fn
        self.delay_ms = delay_ms
        self._after_id: str | None = None
        self._tip: Any = None
        widget.bind("<Enter>", self._schedule)
        widget.bind("<Leave>", self._hide)
        widget.bind("<ButtonPress>", self._hide)

    def _schedule(self, _event: Any = None) -> None:
        self._cancel()
        self._after_id = self.widget.after(self.delay_ms, self._show)

    def _cancel(self) -> None:
        if self._after_id is not None:
            try:
                self.widget.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    def _hide(self, _event: Any = None) -> None:
        self._cancel()
        if self._tip is not None:
            try:
                self._tip.destroy()
            except Exception:
                pass
            self._tip = None

    def _show(self) -> None:
        self._hide()
        try:
            text = self.text_fn()
        except Exception as e:
            text = str(e)
        if not text:
            return
        tip = ctk.CTkToplevel(self.widget)
        tip.wm_overrideredirect(True)
        tip.attributes("-topmost", True)
        try:
            x = self.widget.winfo_rootx() + 12
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        except Exception:
            x, y = 100, 100
        tip.geometry(f"+{x}+{y}")
        frame = ctk.CTkFrame(tip, fg_color="#111827", corner_radius=8, border_width=0)
        frame.pack(fill="both", expand=True)
        ctk.CTkLabel(
            frame,
            text=text,
            font=ctk.CTkFont(size=12),
            text_color="#F9FAFB",
            justify="left",
            anchor="w",
        ).pack(padx=12, pady=10)
        self._tip = tip


class UsageDetailDialog(ctk.CTkToplevel):
    """账户全部已归属 token 用量明细。"""

    def __init__(self, master: CodexAccountApp, profile: AccountProfile, data: dict[str, Any]) -> None:
        super().__init__(master)
        self.title(f"全部用量 · {profile.display_title()}")
        self.geometry("760x560")
        self.configure(fg_color=BG)
        self.transient(master)

        totals = data.get("totals") or {}
        by_model = data.get("by_model") or []
        recent = data.get("recent") or []

        ctk.CTkLabel(
            self,
            text=profile.display_title(),
            font=ctk.CTkFont(size=18, weight="bold"),
            text_color=TEXT,
        ).pack(anchor="w", padx=18, pady=(16, 2))
        ctk.CTkLabel(
            self,
            text=(
                f"{profile.email or ''}  ·  {(profile.plan or '').upper()}  ·  "
                "全部历史（本机已明确归属）"
            ),
            font=ctk.CTkFont(size=12),
            text_color=TEXT_MUTED,
        ).pack(anchor="w", padx=18, pady=(0, 10))

        summary = ctk.CTkFrame(self, fg_color=BG_SOFT, corner_radius=10, border_width=1, border_color=BORDER)
        summary.pack(fill="x", padx=18, pady=(0, 10))
        box = ctk.CTkFrame(summary, fg_color="transparent")
        box.pack(fill="x", padx=14, pady=12)
        lines = [
            "统计范围：全部已记录历史（不受当前周限额周期过滤）",
            f"调用次数：{totals.get('turns', 0)}",
            f"输入：{format_tokens(totals.get('input_tokens'))}   "
            f"缓存输入：{format_tokens(totals.get('cached_input_tokens'))}   "
            f"缓存写入：{format_tokens(totals.get('cache_write_input_tokens'))}",
            f"输出：{format_tokens(totals.get('output_tokens'))}   "
            f"推理：{format_tokens(totals.get('reasoning_output_tokens'))}   "
            f"合计：{format_tokens(totals.get('total_tokens'))}",
        ]
        for t in lines:
            ctk.CTkLabel(box, text=t, font=ctk.CTkFont(size=13), text_color=TEXT, anchor="w").pack(fill="x", pady=1)

        ctk.CTkLabel(
            self,
            text="按模型汇总",
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color=TEXT,
        ).pack(anchor="w", padx=18, pady=(4, 4))

        text = ctk.CTkTextbox(
            self,
            font=ctk.CTkFont(size=12),
            fg_color="#0B1220",
            text_color="#E5E7EB",
        )
        text.pack(fill="both", expand=True, padx=18, pady=(0, 12))

        buf: list[str] = []
        if by_model:
            buf.append(
                f"{'模型':28} {'次数':>6} {'输入':>10} {'缓存':>10} "
                f"{'输出':>10} {'推理':>10} {'合计':>10}"
            )
            buf.append("-" * 90)
            for m in by_model:
                buf.append(
                    f"{str(m.get('model')):28} "
                    f"{int(m.get('turns') or 0):6d} "
                    f"{format_tokens(m.get('input_tokens')):>10} "
                    f"{format_tokens(m.get('cached_input_tokens')):>10} "
                    f"{format_tokens(m.get('output_tokens')):>10} "
                    f"{format_tokens(m.get('reasoning_output_tokens')):>10} "
                    f"{format_tokens(m.get('total_tokens')):>10}"
                )
        else:
            buf.append("暂无模型数据。请先点「同步用量」。")

        buf.append("")
        buf.append("最近 30 条调用：")
        buf.append(
            f"{'时间':22} {'模型':20} {'输入':>8} {'缓存':>8} "
            f"{'输出':>8} {'推理':>8} {'合计':>8}"
        )
        buf.append("-" * 90)
        for r in recent:
            ts = str(r.get("event_ts") or "")[:19].replace("T", " ")
            buf.append(
                f"{ts:22} {str(r.get('model') or '-'):20} "
                f"{format_tokens(r.get('input_tokens')):>8} "
                f"{format_tokens(r.get('cached_input_tokens')):>8} "
                f"{format_tokens(r.get('output_tokens')):>8} "
                f"{format_tokens(r.get('reasoning_output_tokens')):>8} "
                f"{format_tokens(r.get('total_tokens')):>8}"
            )

        text.insert("1.0", "\n".join(buf))
        text.configure(state="disabled")

        ctk.CTkButton(
            self,
            text="关闭",
            width=100,
            height=34,
            fg_color=PRIMARY,
            hover_color=PRIMARY_HOVER,
            command=self.destroy,
        ).pack(pady=(0, 14))


class PasteAuthDialog(ctk.CTkToplevel):
    """粘贴一个或多个 auth.json。"""

    def __init__(self, master: CodexAccountApp, on_done: Any = None) -> None:
        super().__init__(master)
        self.master_app = master
        self.on_done = on_done
        self.title("粘贴 auth.json")
        self.geometry("720x520")
        self.configure(fg_color=BG)
        self.transient(master)
        self.grab_set()

        ctk.CTkLabel(
            self,
            text="粘贴 auth.json 内容",
            font=ctk.CTkFont(size=16, weight="bold"),
            text_color=TEXT,
        ).pack(anchor="w", padx=18, pady=(16, 4))
        ctk.CTkLabel(
            self,
            text="支持：单个 JSON 对象 / JSON 数组 / 多段 JSON（用 --- 或空行分隔）",
            font=ctk.CTkFont(size=12),
            text_color=TEXT_MUTED,
        ).pack(anchor="w", padx=18, pady=(0, 8))

        self.text = ctk.CTkTextbox(
            self,
            font=ctk.CTkFont(size=12),
            fg_color=BG_SOFT,
            border_width=1,
            border_color=BORDER,
            text_color=TEXT,
        )
        self.text.pack(fill="both", expand=True, padx=18, pady=(0, 10))

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(fill="x", padx=18, pady=(0, 16))
        ctk.CTkButton(
            btns,
            text="导入",
            height=36,
            fg_color=PRIMARY,
            hover_color=PRIMARY_HOVER,
            command=self.do_import,
        ).pack(side="left")
        ctk.CTkButton(
            btns,
            text="取消",
            height=36,
            width=80,
            fg_color=BG_SOFT,
            hover_color=BORDER,
            text_color=TEXT,
            border_width=1,
            border_color=BORDER,
            command=self.destroy,
        ).pack(side="left", padx=(8, 0))

    def do_import(self) -> None:
        raw = self.text.get("1.0", "end").strip()
        if not raw:
            messagebox.showwarning("提示", "内容为空", parent=self)
            return
        try:
            profiles = self.master_app.mgr.import_auth_text(raw, note="粘贴导入")
        except Exception as e:
            messagebox.showerror("导入失败", str(e), parent=self)
            return
        messagebox.showinfo("完成", f"已导入 {len(profiles)} 个账户", parent=self)
        if self.on_done:
            self.on_done(len(profiles))
        self.destroy()


class OAuthLoginDialog(ctk.CTkToplevel):
    """本地 PKCE OAuth 登录进度窗口。"""

    def __init__(self, master: CodexAccountApp, on_done: Any = None) -> None:
        super().__init__(master)
        self.master_app = master
        self.on_done = on_done
        self._result: dict[str, Any] | None = None
        self._started = False
        self._finished = False
        self._closed = False
        self._callback_sent = False
        self._cancel_event = threading.Event()
        self._worker_running = False
        self._retry_requested = False
        self._run_id = 0
        self.title("OAuth 登录")
        self.geometry("560x390")
        self.configure(fg_color=BG)
        self.transient(master)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        ctk.CTkLabel(
            self,
            text="ChatGPT OAuth 授权登录",
            font=ctk.CTkFont(size=16, weight="bold"),
            text_color=TEXT,
        ).pack(anchor="w", padx=16, pady=(14, 4))
        ctk.CTkLabel(
            self,
            text="只打开一个无痕/隐私窗口，按正常 ChatGPT 流程登录。\n"
            "授权结果仅回传到本机 127.0.0.1，并校验随机 state 与 PKCE。\n"
            "不需要启用设备代码授权，也不会改变当前 Codex 登录。",
            font=ctk.CTkFont(size=12),
            text_color=TEXT_MUTED,
            justify="left",
            anchor="w",
        ).pack(fill="x", padx=16, pady=(0, 8))

        self.log = ctk.CTkTextbox(
            self,
            font=ctk.CTkFont(size=12),
            fg_color="#0B1220",
            text_color="#E5E7EB",
        )
        self.log.pack(fill="both", expand=True, padx=16, pady=(0, 10))
        self.log.configure(state="disabled")

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(fill="x", padx=16, pady=(0, 14))
        self.btn_retry = ctk.CTkButton(
            btns,
            text="重新开始登录",
            height=34,
            width=126,
            fg_color=OAUTH,
            hover_color=OAUTH_HOVER,
            command=self._retry_login,
        )
        self.btn_retry.pack(side="left")
        self.btn_close = ctk.CTkButton(
            btns,
            text="取消并关闭",
            height=34,
            width=120,
            fg_color=BG_SOFT,
            text_color=TEXT,
            border_width=1,
            border_color=BORDER,
            command=self._on_close,
        )
        self.btn_close.pack(side="right")

        self.master_app.set_busy(True, "OAuth 登录中…")
        self.after(200, self._start)

    def append_log(self, msg: str) -> None:
        if self._closed:
            return
        try:
            self.log.configure(state="normal")
            self.log.insert("end", msg + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")
        except Exception:
            pass

    def _start(self) -> None:
        if self._closed or self._worker_running:
            return
        self._started = True
        self._worker_running = True
        self._finished = False
        self._run_id += 1
        run_id = self._run_id
        self._cancel_event = threading.Event()
        cancel_event = self._cancel_event
        try:
            self.btn_retry.configure(state="normal")
        except Exception:
            pass

        def log_fn(msg: str) -> None:
            if not self._closed:
                self.master_app.after(0, lambda m=msg: self.append_log(m))

        def work() -> None:
            try:
                from oauth_login import oauth_login_and_save

                result = oauth_login_and_save(
                    mgr=self.master_app.mgr,
                    open_browser=True,
                    timeout_sec=300,
                    log=log_fn,
                    cancel_event=cancel_event,
                )
                self.master_app.after(0, lambda r=result: self._done(r, run_id))
            except Exception as e:
                err = str(e)
                self.master_app.after(
                    0,
                    lambda err=err: self._done(
                        {"ok": False, "error": err}, run_id
                    ),
                )

        threading.Thread(target=work, daemon=True).start()

    def _done(self, result: dict[str, Any], run_id: int) -> None:
        if run_id != self._run_id:
            return
        self._worker_running = False
        if self._retry_requested and not self._closed:
            self._retry_requested = False
            self._result = None
            self.append_log("—— 正在重新开始 OAuth 登录 ——")
            self.master_app.set_busy(True, "正在重新开始 OAuth 登录…")
            self.after(150, self._start)
            return
        if self._finished:
            return
        self._finished = True
        self._result = result
        self.master_app.set_busy(False)
        if self._closed:
            self._notify_done()
            return
        try:
            self.btn_close.configure(
                state="normal", text="关闭", fg_color=PRIMARY, text_color="#FFFFFF"
            )
            if result.get("ok"):
                self.append_log("—— 全部完成 ——")
                messagebox.showinfo(
                    "OAuth 成功",
                    f"账户已保存：\n{result.get('email') or result.get('name')}\n"
                    f"套餐：{(result.get('plan') or '?').upper()}\n\n"
                    "当前 Codex 登录未改变，无需退出或重启 Codex。",
                    parent=self,
                )
            elif result.get("cancelled"):
                self.append_log("—— 已取消 ——")
            else:
                self.append_log("—— 失败 ——")
                stage = result.get("stage") or "未知阶段"
                messagebox.showerror(
                    "OAuth 失败",
                    f"阶段：{stage}\n\n{result.get('error') or '未知错误'}",
                    parent=self,
                )
        except Exception:
            pass

    def _retry_login(self) -> None:
        if self._closed:
            return
        self._finished = False
        self._result = None
        self.master_app.set_busy(True, "正在重新开始 OAuth 登录…")
        if self._worker_running:
            self._retry_requested = True
            self._cancel_event.set()
            self.append_log("正在取消旧登录，请稍候…")
            try:
                self.btn_retry.configure(state="disabled")
            except Exception:
                pass
            return
        self.after(50, self._start)

    def _on_close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if not self._finished:
            self._cancel_event.set()
            self._result = {
                "ok": False,
                "cancelled": True,
                "error": "用户取消了 OAuth 登录",
            }
            self.master_app.set_busy(False)
            self.set_status_safe("OAuth 登录已取消")
            # 已启动时等工作线程确认取消，避免与最后一步导入发生竞态。
            if not self._worker_running:
                self._notify_done()
        else:
            self._notify_done()
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()

    def _notify_done(self) -> None:
        if self._callback_sent:
            return
        self._callback_sent = True
        if self.on_done:
            self.on_done(self._result)

    def set_status_safe(self, msg: str) -> None:
        try:
            self.master_app.set_status(msg, ok=False)
        except Exception:
            pass


def main() -> None:
    app_dir = Path(__file__).resolve().parent
    if str(app_dir) not in sys.path:
        sys.path.insert(0, str(app_dir))

    from atomic_io import SingleInstanceLock, signal_activation
    from manager import default_store_dir
    from windows_app import activate_window_by_title, set_process_app_identity

    set_process_app_identity()
    store_dir = default_store_dir()
    activation_path = store_dir / "activate.request"
    lock = SingleInstanceLock(store_dir / "app.lock")
    if not lock.acquire(blocking=False):
        # 再点启动器应恢复已有窗口，而不是创建一个隐藏提示框。
        try:
            signal_activation(activation_path)
        except OSError:
            pass
        activate_window_by_title()
        return

    try:
        app = CodexAccountApp()
        app._instance_lock = lock  # type: ignore[attr-defined]
        app.start_activation_listener(activation_path)

        def _on_exit() -> None:
            try:
                lock.release()
            except Exception:
                pass
            app.destroy()

        app.protocol("WM_DELETE_WINDOW", _on_exit)
        app.mainloop()
    finally:
        try:
            lock.release()
        except Exception:
            pass


if __name__ == "__main__":
    main()
