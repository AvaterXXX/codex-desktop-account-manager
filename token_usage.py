#!/usr/bin/env python3
"""
从 ~/.codex/sessions 的 rollout jsonl 解析 token 用量，并按账户归档。

归因规则：
1. 使用本工具的「账户切换时间线」判断某时刻活跃账户
2. 若时间线为空且只有 1 个 ChatGPT 账户，则全部归到该账户
3. 否则记为 unknown（仍可查看总计）
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _parse_ts(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        s = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


@dataclass
class UsageTotals:
    turns: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    total_tokens: int = 0

    def add(self, d: dict[str, Any]) -> None:
        self.turns += 1
        self.input_tokens += int(d.get("input_tokens") or 0)
        self.cached_input_tokens += int(d.get("cached_input_tokens") or 0)
        self.cache_write_input_tokens += int(d.get("cache_write_input_tokens") or 0)
        self.output_tokens += int(d.get("output_tokens") or 0)
        self.reasoning_output_tokens += int(d.get("reasoning_output_tokens") or 0)
        # total 有时只在 total_tokens；优先字段，否则估算
        t = d.get("total_tokens")
        if t is None:
            t = (
                int(d.get("input_tokens") or 0)
                + int(d.get("output_tokens") or 0)
            )
        self.total_tokens += int(t or 0)

    def as_dict(self) -> dict[str, int]:
        return {
            "turns": self.turns,
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "cache_write_input_tokens": self.cache_write_input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_output_tokens": self.reasoning_output_tokens,
            "total_tokens": self.total_tokens,
        }


class TokenUsageStore:
    def __init__(self, store_dir: Path, sessions_dir: Path | None = None):
        self.store_dir = Path(store_dir)
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.store_dir / "token_usage.sqlite"
        self.sessions_dir = Path(sessions_dir) if sessions_dir else (Path.home() / ".codex" / "sessions")
        # 会话扫描会先读取整份切换时间线。扫描与账户切换必须串行，
        # 否则扫描可能拿着旧时间线，把切换后的新事件仍写到旧账户。
        self._write_lock = threading.RLock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.db_path), timeout=30)
        con.row_factory = sqlite3.Row
        return con

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """同时管理事务和连接生命周期，避免 GUI 长时间运行时积累句柄。"""
        con = self._connect()
        try:
            with con:
                yield con
        finally:
            con.close()

    def _init_db(self) -> None:
        with self._connection() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS scan_state (
                    path TEXT PRIMARY KEY,
                    offset INTEGER NOT NULL DEFAULT 0,
                    mtime REAL NOT NULL DEFAULT 0,
                    last_line INTEGER NOT NULL DEFAULT 0
                );
                -- 兼容旧库缺列
                """
            )
            for col_sql in (
                "ALTER TABLE scan_state ADD COLUMN last_line INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE scan_state ADD COLUMN last_session_id TEXT DEFAULT ''",
                "ALTER TABLE scan_state ADD COLUMN last_model TEXT DEFAULT ''",
                "ALTER TABLE scan_state ADD COLUMN last_provider TEXT DEFAULT ''",
            ):
                try:
                    con.execute(col_sql)
                except sqlite3.OperationalError:
                    pass
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS account_switches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    ts_iso TEXT NOT NULL,
                    profile_id TEXT,
                    account_id TEXT,
                    email TEXT,
                    source TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_switches_ts ON account_switches(ts);
                CREATE TABLE IF NOT EXISTS usage_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT,
                    event_ts TEXT,
                    event_epoch REAL,
                    profile_id TEXT,
                    account_id TEXT,
                    email TEXT,
                    model TEXT,
                    provider TEXT,
                    input_tokens INTEGER DEFAULT 0,
                    cached_input_tokens INTEGER DEFAULT 0,
                    cache_write_input_tokens INTEGER DEFAULT 0,
                    output_tokens INTEGER DEFAULT 0,
                    reasoning_output_tokens INTEGER DEFAULT 0,
                    total_tokens INTEGER DEFAULT 0,
                    source_file TEXT NOT NULL,
                    source_line INTEGER NOT NULL,
                    UNIQUE(source_file, source_line)
                );
                CREATE INDEX IF NOT EXISTS idx_usage_account ON usage_events(account_id);
                CREATE INDEX IF NOT EXISTS idx_usage_profile ON usage_events(profile_id);
                CREATE INDEX IF NOT EXISTS idx_usage_model ON usage_events(model);
                CREATE INDEX IF NOT EXISTS idx_usage_epoch ON usage_events(event_epoch);
                """
            )

    # ---------- switch timeline ----------
    def log_switch(
        self,
        *,
        profile_id: str = "",
        account_id: str = "",
        email: str = "",
        source: str = "switch",
        ts: float | None = None,
    ) -> None:
        epoch = ts if ts is not None else time.time()
        ts_iso = datetime.fromtimestamp(epoch, tz=timezone.utc).replace(microsecond=0).isoformat()
        with self._write_lock:
            with self._connection() as con:
                con.execute(
                    """
                    INSERT INTO account_switches(ts, ts_iso, profile_id, account_id, email, source)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (epoch, ts_iso, profile_id or "", account_id or "", email or "", source),
                )
                # 若切换记录晚于会话扫描写入（例如外部换号后才重新打开本工具），
                # 立即修正该时间段内已经入库的事件，不必等用户手动全量同步。
                next_row = con.execute(
                    "SELECT MIN(ts) AS ts FROM account_switches WHERE ts > ?",
                    (epoch,),
                ).fetchone()
                next_epoch = next_row["ts"] if next_row else None
                if next_epoch is None:
                    con.execute(
                        """
                        UPDATE usage_events
                        SET profile_id=?, account_id=?, email=?
                        WHERE event_epoch >= ?
                        """,
                        (
                            profile_id or "",
                            account_id or "",
                            email or "",
                            epoch,
                        ),
                    )
                else:
                    con.execute(
                        """
                        UPDATE usage_events
                        SET profile_id=?, account_id=?, email=?
                        WHERE event_epoch >= ? AND event_epoch < ?
                        """,
                        (
                            profile_id or "",
                            account_id or "",
                            email or "",
                            epoch,
                            next_epoch,
                        ),
                    )

    def ensure_baseline_switch(
        self,
        *,
        profile_id: str = "",
        account_id: str = "",
        email: str = "",
    ) -> None:
        """
        若还没有任何切换记录，写入当前账户作为「从此刻起」的基线。
        注意：基线时间戳 = 现在，更早的 session 不会被归到该账户。
        """
        with self._write_lock:
            with self._connection() as con:
                n = con.execute("SELECT COUNT(*) AS c FROM account_switches").fetchone()["c"]
                if n == 0 and (account_id or profile_id or email):
                    epoch = time.time()
                    con.execute(
                        """
                        INSERT INTO account_switches(ts, ts_iso, profile_id, account_id, email, source)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            epoch,
                            datetime.fromtimestamp(epoch, tz=timezone.utc)
                            .replace(microsecond=0)
                            .isoformat(),
                            profile_id or "",
                            account_id or "",
                            email or "",
                            "baseline",
                        ),
                    )

    def _load_switches(self, con: sqlite3.Connection | None = None) -> list[sqlite3.Row]:
        if con is not None:
            return list(
                con.execute(
                    "SELECT ts, profile_id, account_id, email, source FROM account_switches ORDER BY ts ASC"
                )
            )
        with self._connection() as c:
            return list(
                c.execute(
                    "SELECT ts, profile_id, account_id, email, source FROM account_switches ORDER BY ts ASC"
                )
            )

    def resolve_account_at(
        self,
        epoch: float | None,
        *,
        fallback_profiles: list[dict[str, str]] | None = None,
        switches: list[sqlite3.Row] | None = None,
    ) -> tuple[str, str, str]:
        """
        返回 (profile_id, account_id, email)。
        严格归属：仅时间线覆盖到的事件才归属；不做单账户兜底。
        """
        del fallback_profiles
        if epoch is None:
            return "", "", ""
        rows = switches if switches is not None else self._load_switches()
        if not rows:
            return "", "", ""

        chosen = None
        for row in rows:
            if row["ts"] <= epoch:
                chosen = row
            else:
                break
        if chosen is None:
            return "", "", ""
        return (
            chosen["profile_id"] or "",
            chosen["account_id"] or "",
            chosen["email"] or "",
        )

    @staticmethod
    def _usage_total(usage: dict[str, Any]) -> int:
        """total_tokens 缺失时回退 input+output。"""
        if usage.get("total_tokens") is not None:
            try:
                t = int(usage.get("total_tokens") or 0)
                if t > 0:
                    return t
            except Exception:
                pass
        try:
            return int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
        except Exception:
            return 0

    # ---------- scan sessions ----------
    def iter_session_files(self) -> Iterable[Path]:
        if not self.sessions_dir.exists():
            return []
        return sorted(self.sessions_dir.rglob("rollout-*.jsonl"))

    def sync_sessions(
        self,
        *,
        fallback_profiles: list[dict[str, str]] | None = None,
        max_files: int | None = None,
        recent_days: int | None = 45,
        full: bool = False,
    ) -> dict[str, Any]:
        with self._write_lock:
            return self._sync_sessions_locked(
                fallback_profiles=fallback_profiles,
                max_files=max_files,
                recent_days=recent_days,
                full=full,
            )

    def _sync_sessions_locked(
        self,
        *,
        fallback_profiles: list[dict[str, str]] | None = None,
        max_files: int | None = None,
        recent_days: int | None = 45,
        full: bool = False,
    ) -> dict[str, Any]:
        """
        增量扫描 session 文件，写入 usage_events。

        默认只扫 recent_days 内有改动的文件（首次也避免扫数年历史卡死）。
        full=True 时扫描全部 rollout。
        """
        files = list(self.iter_session_files())
        now = time.time()
        if not full and recent_days is not None and recent_days > 0:
            cutoff = now - recent_days * 86400
            files = [p for p in files if p.stat().st_mtime >= cutoff]
        # 仍很大时只保留最近 max_files
        limit = max_files if max_files is not None else (None if full else 80)
        if limit is not None and len(files) > limit:
            files = sorted(files, key=lambda p: p.stat().st_mtime)[-limit:]

        inserted = 0
        scanned_files = 0
        errors = 0
        skipped = 0

        with self._connection() as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=NORMAL")
            switches = self._load_switches(con)
            for path in files:
                try:
                    st = path.stat()
                except OSError:
                    continue
                rel = str(path)
                row = con.execute(
                    "SELECT * FROM scan_state WHERE path=?", (rel,)
                ).fetchone()
                offset = int(row["offset"]) if row else 0
                old_mtime = float(row["mtime"]) if row else 0.0
                start_line = int(row["last_line"]) if row and "last_line" in row.keys() else 0
                resume_session = str(row["last_session_id"]) if row and "last_session_id" in row.keys() else ""
                resume_model = str(row["last_model"]) if row and "last_model" in row.keys() else ""
                resume_provider = str(row["last_provider"]) if row and "last_provider" in row.keys() else ""

                # 文件被截断/重写
                if st.st_size < offset:
                    offset = 0
                    start_line = 0
                    resume_session = resume_model = resume_provider = ""
                # 无新内容
                if offset == st.st_size and abs(st.st_mtime - old_mtime) < 0.001:
                    skipped += 1
                    continue

                scanned_files += 1
                try:
                    n, end_offset, end_line, sid, model, provider = self._scan_file(
                        con,
                        path,
                        start_offset=offset,
                        start_line=start_line,
                        resume_session=resume_session,
                        resume_model=resume_model,
                        resume_provider=resume_provider,
                        switches=switches,
                    )
                    inserted += n
                    # 用扫描结束后的实际文件位置，避免扫描期间增长导致重复/漏扫
                    try:
                        mtime_after = path.stat().st_mtime
                    except OSError:
                        mtime_after = st.st_mtime
                    con.execute(
                        """
                        INSERT INTO scan_state(path, offset, mtime, last_line, last_session_id, last_model, last_provider)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(path) DO UPDATE SET
                          offset=excluded.offset,
                          mtime=excluded.mtime,
                          last_line=excluded.last_line,
                          last_session_id=excluded.last_session_id,
                          last_model=excluded.last_model,
                          last_provider=excluded.last_provider
                        """,
                        (rel, end_offset, mtime_after, end_line, sid, model, provider),
                    )
                    con.commit()
                except Exception:
                    errors += 1
                    continue

        return {
            "scanned_files": scanned_files,
            "inserted_events": inserted,
            "errors": errors,
            "skipped_files": skipped,
            "total_files": len(files),
            "recent_days": None if full else recent_days,
            "synced_at": _utc_now_iso(),
        }

    def _scan_file(
        self,
        con: sqlite3.Connection,
        path: Path,
        *,
        start_offset: int,
        start_line: int = 0,
        resume_session: str = "",
        resume_model: str = "",
        resume_provider: str = "",
        switches: list[sqlite3.Row] | None = None,
    ) -> tuple[int, int, int, str, str, str]:
        """
        返回 (inserted, end_offset, end_line, session_id, model, provider)
        end_offset 为「完整行」结束位置；半行不推进游标。
        """
        inserted = 0
        session_id = resume_session or ""
        current_model = resume_model or ""
        current_provider = resume_provider or ""
        line_no = start_line if start_offset > 0 else 0
        batch: list[tuple] = []
        end_offset = start_offset

        with path.open("rb") as fh:
            if start_offset > 0:
                fh.seek(start_offset)
            else:
                fh.seek(0)
                line_no = 0

            while True:
                raw = fh.readline()
                if not raw:
                    break
                # 半行：不推进游标，下次再读
                if not raw.endswith(b"\n") and not raw.endswith(b"\r"):
                    # 文件可能仍在增长；保持 end_offset 在 pos_before
                    break
                end_offset = fh.tell()
                line_no += 1
                try:
                    line = raw.decode("utf-8", "replace").strip()
                except Exception:
                    continue
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    # 坏行：跳过但游标已前进，避免卡死
                    continue
                if not isinstance(obj, dict):
                    continue

                etype = obj.get("type")
                payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
                event_ts = obj.get("timestamp") or payload.get("timestamp") or ""
                epoch = _parse_ts(str(event_ts) if event_ts else None)

                if etype == "session_meta":
                    session_id = (
                        payload.get("session_id")
                        or payload.get("id")
                        or session_id
                        or ""
                    )
                    current_provider = payload.get("model_provider") or current_provider
                    continue

                if etype == "turn_context":
                    m = payload.get("model") or payload.get("model_name")
                    if m:
                        current_model = str(m)
                    if payload.get("model_provider"):
                        current_provider = str(payload.get("model_provider"))
                    continue

                if etype == "event_msg" and payload.get("type") == "token_count":
                    info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
                    last = info.get("last_token_usage") if isinstance(info.get("last_token_usage"), dict) else {}
                    total = info.get("total_token_usage") if isinstance(info.get("total_token_usage"), dict) else {}
                    usage = last or total or {}
                    if not usage:
                        continue
                    inp = int(usage.get("input_tokens") or 0)
                    out = int(usage.get("output_tokens") or 0)
                    cached = int(usage.get("cached_input_tokens") or 0)
                    reason = int(usage.get("reasoning_output_tokens") or 0)
                    tot = self._usage_total(usage)
                    if inp + out + cached + reason + tot <= 0:
                        continue

                    profile_id, account_id, email = self.resolve_account_at(
                        epoch, switches=switches
                    )
                    batch.append(
                        (
                            session_id,
                            str(event_ts or ""),
                            epoch,
                            profile_id,
                            account_id,
                            email,
                            current_model or "",
                            current_provider or "",
                            inp,
                            cached,
                            int(usage.get("cache_write_input_tokens") or 0),
                            out,
                            reason,
                            tot,
                            str(path),
                            line_no,
                        )
                    )
                    if len(batch) >= 200:
                        inserted += self._flush_batch(con, batch)
                        batch.clear()

        if batch:
            inserted += self._flush_batch(con, batch)
        return inserted, end_offset, line_no, session_id, current_model, current_provider

    def _flush_batch(self, con: sqlite3.Connection, batch: list[tuple]) -> int:
        before = con.total_changes
        con.executemany(
            """
            INSERT OR IGNORE INTO usage_events(
                session_id, event_ts, event_epoch,
                profile_id, account_id, email,
                model, provider,
                input_tokens, cached_input_tokens, cache_write_input_tokens,
                output_tokens, reasoning_output_tokens, total_tokens,
                source_file, source_line
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            batch,
        )
        return max(0, con.total_changes - before)

    # ---------- queries ----------
    def _where_for_account(
        self,
        *,
        profile_id: str = "",
        account_id: str = "",
        email: str = "",
    ) -> tuple[str, list[Any]]:
        clauses = []
        args: list[Any] = []
        if account_id:
            clauses.append("account_id = ?")
            args.append(account_id)
        if profile_id:
            clauses.append("profile_id = ?")
            args.append(profile_id)
        if email:
            clauses.append("LOWER(email) = LOWER(?)")
            args.append(email)
        if not clauses:
            return "1=0", []
        return "(" + " OR ".join(clauses) + ")", args

    def summarize(
        self,
        *,
        profile_id: str = "",
        account_id: str = "",
        email: str = "",
        since_epoch: float | None = None,
    ) -> dict[str, Any]:
        where, args = self._where_for_account(
            profile_id=profile_id, account_id=account_id, email=email
        )
        if since_epoch is not None:
            where += " AND event_epoch >= ?"
            args.append(since_epoch)

        with self._connection() as con:
            row = con.execute(
                f"""
                SELECT
                  COUNT(*) AS turns,
                  COALESCE(SUM(input_tokens),0) AS input_tokens,
                  COALESCE(SUM(cached_input_tokens),0) AS cached_input_tokens,
                  COALESCE(SUM(cache_write_input_tokens),0) AS cache_write_input_tokens,
                  COALESCE(SUM(output_tokens),0) AS output_tokens,
                  COALESCE(SUM(reasoning_output_tokens),0) AS reasoning_output_tokens,
                  COALESCE(SUM(total_tokens),0) AS total_tokens
                FROM usage_events
                WHERE {where}
                """,
                args,
            ).fetchone()

            by_model = []
            for m in con.execute(
                f"""
                SELECT
                  COALESCE(NULLIF(model,''), '(unknown)') AS model,
                  COUNT(*) AS turns,
                  COALESCE(SUM(input_tokens),0) AS input_tokens,
                  COALESCE(SUM(cached_input_tokens),0) AS cached_input_tokens,
                  COALESCE(SUM(output_tokens),0) AS output_tokens,
                  COALESCE(SUM(reasoning_output_tokens),0) AS reasoning_output_tokens,
                  COALESCE(SUM(total_tokens),0) AS total_tokens
                FROM usage_events
                WHERE {where}
                GROUP BY COALESCE(NULLIF(model,''), '(unknown)')
                ORDER BY total_tokens DESC
                """,
                args,
            ):
                by_model.append(dict(m))

            recent = []
            for m in con.execute(
                f"""
                SELECT event_ts, model, session_id,
                       input_tokens, cached_input_tokens, output_tokens,
                       reasoning_output_tokens, total_tokens
                FROM usage_events
                WHERE {where}
                ORDER BY event_epoch DESC
                LIMIT 30
                """,
                args,
            ):
                recent.append(dict(m))

        totals = {
            "turns": int(row["turns"] or 0),
            "input_tokens": int(row["input_tokens"] or 0),
            "cached_input_tokens": int(row["cached_input_tokens"] or 0),
            "cache_write_input_tokens": int(row["cache_write_input_tokens"] or 0),
            "output_tokens": int(row["output_tokens"] or 0),
            "reasoning_output_tokens": int(row["reasoning_output_tokens"] or 0),
            "total_tokens": int(row["total_tokens"] or 0),
        }
        return {"totals": totals, "by_model": by_model, "recent": recent}

    def global_summary(self) -> dict[str, Any]:
        with self._connection() as con:
            total = con.execute(
                """
                SELECT COUNT(*) AS turns,
                       COALESCE(SUM(total_tokens),0) AS total_tokens,
                       COALESCE(SUM(input_tokens),0) AS input_tokens,
                       COALESCE(SUM(output_tokens),0) AS output_tokens,
                       COALESCE(SUM(cached_input_tokens),0) AS cached_input_tokens
                FROM usage_events
                """
            ).fetchone()
            by_account = [
                dict(r)
                for r in con.execute(
                    """
                    SELECT
                      COALESCE(NULLIF(email,''), NULLIF(account_id,''), '(unknown)') AS label,
                      account_id, email, profile_id,
                      COUNT(*) AS turns,
                      COALESCE(SUM(total_tokens),0) AS total_tokens,
                      COALESCE(SUM(input_tokens),0) AS input_tokens,
                      COALESCE(SUM(output_tokens),0) AS output_tokens,
                      COALESCE(SUM(cached_input_tokens),0) AS cached_input_tokens
                    FROM usage_events
                    GROUP BY label, account_id, email, profile_id
                    ORDER BY total_tokens DESC
                    """
                )
            ]
            by_model = [
                dict(r)
                for r in con.execute(
                    """
                    SELECT COALESCE(NULLIF(model,''),'(unknown)') AS model,
                           COUNT(*) AS turns,
                           COALESCE(SUM(total_tokens),0) AS total_tokens
                    FROM usage_events
                    GROUP BY 1
                    ORDER BY total_tokens DESC
                    LIMIT 20
                    """
                )
            ]
        return {
            "totals": dict(total),
            "by_account": by_account,
            "by_model": by_model,
        }

    def reattribute_all(self, fallback_profiles: list[dict[str, str]] | None = None) -> dict[str, int]:
        """按当前严格时间线重算全部事件归属（修正历史误归属）。"""
        with self._write_lock:
            return self._reattribute_all_locked(fallback_profiles)

    def _reattribute_all_locked(
        self,
        fallback_profiles: list[dict[str, str]] | None = None,
    ) -> dict[str, int]:
        updated = 0
        cleared = 0
        with self._connection() as con:
            # 一次加载并复用。旧实现每条事件都会重新连接 SQLite、重查时间线，
            # 几万条事件时会把启动后的后台同步拖到几十秒。
            switches = self._load_switches(con)
            rows = con.execute(
                "SELECT id, event_epoch, profile_id, account_id, email FROM usage_events"
            ).fetchall()
            for row in rows:
                pid, aid, email = self.resolve_account_at(
                    row["event_epoch"],
                    fallback_profiles=fallback_profiles,
                    switches=switches,
                )
                old = (
                    row["profile_id"] or "",
                    row["account_id"] or "",
                    row["email"] or "",
                )
                new = (pid, aid, email)
                if old == new:
                    continue
                con.execute(
                    """
                    UPDATE usage_events
                    SET profile_id=?, account_id=?, email=?
                    WHERE id=?
                    """,
                    (pid, aid, email, row["id"]),
                )
                updated += 1
                if not (pid or aid or email) and any(old):
                    cleared += 1
        return {"updated": updated, "cleared_to_unknown": cleared}

    def reattribute_unknown(self, fallback_profiles: list[dict[str, str]]) -> int:
        """兼容旧接口：全量重归属后返回更新条数。"""
        return int(self.reattribute_all(fallback_profiles).get("updated") or 0)

    def count_stats(self) -> dict[str, int]:
        with self._connection() as con:
            total = con.execute("SELECT COUNT(*) AS c FROM usage_events").fetchone()["c"]
            attributed = con.execute(
                """
                SELECT COUNT(*) AS c FROM usage_events
                WHERE (account_id IS NOT NULL AND account_id!='')
                   OR (profile_id IS NOT NULL AND profile_id!='')
                   OR (email IS NOT NULL AND email!='')
                """
            ).fetchone()["c"]
        return {
            "total_events": int(total or 0),
            "attributed_events": int(attributed or 0),
            "unknown_events": int((total or 0) - (attributed or 0)),
        }


def format_tokens(n: int | float | None) -> str:
    try:
        v = int(n or 0)
    except Exception:
        return "0"
    if v >= 1_000_000:
        return f"{v/1_000_000:.2f}M"
    if v >= 1_000:
        return f"{v/1_000:.1f}K"
    return str(v)
