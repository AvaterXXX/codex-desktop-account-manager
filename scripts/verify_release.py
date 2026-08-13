#!/usr/bin/env python3
"""Fail a release when source/build output contains likely credentials or local identities."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Iterable


EXCLUDED_DIRS = {
    ".git",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "release",
}
FORBIDDEN_NAMES = {
    "auth.json",
    "config.json",
    "token_usage.sqlite",
    "token_usage.sqlite3",
}
TEXT_SUFFIXES = {
    "",
    ".bat",
    ".gitignore",
    ".json",
    ".md",
    ".ps1",
    ".py",
    ".svg",
    ".txt",
    ".vbs",
    ".yml",
    ".yaml",
}
SENSITIVE_KEYS = {
    "access_token",
    "account_id",
    "api_key",
    "email",
    "id_token",
    "identity_key",
    "openai_api_key",
    "refresh_token",
}
BYTE_PATTERNS = {
    "private key": re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "GitHub token": re.compile(rb"gh[pousr]_[A-Za-z0-9]{20,}"),
    "API key": re.compile(rb"\bsk-[A-Za-z0-9_-]{24,}\b"),
    "JWT": re.compile(
        rb"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\b"
    ),
}
EMAIL_PATTERN = re.compile(
    rb"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b"
)
SAFE_EMAIL_DOMAINS = {"b.com", "example.com", "x.com"}


def iter_source_files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in EXCLUDED_DIRS for part in path.relative_to(root).parts):
            continue
        if path.suffix.lower() in TEXT_SUFFIXES or path.name == ".gitignore":
            yield path


def collect_sensitive_values(value: Any, key: str = "") -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for child_key, child_value in value.items():
            normalized = str(child_key).strip().lower()
            if isinstance(child_value, str):
                text = child_value.strip()
                if (
                    normalized in SENSITIVE_KEYS
                    or (normalized == "name" and "@" in text)
                ) and len(text) >= 6:
                    found.add(text)
            found.update(collect_sensitive_values(child_value, normalized))
    elif isinstance(value, list):
        for child in value:
            found.update(collect_sensitive_values(child, key))
    return found


def load_local_sensitive_values() -> set[str]:
    home = Path.home()
    candidates = [
        home / ".codex" / "auth.json",
        home / ".codex-account-manager" / "config.json",
    ]
    profile_root = home / ".codex-account-manager" / "profiles"
    if profile_root.exists():
        candidates.extend(profile_root.glob("*/auth.json"))

    values: set[str] = set()
    for path in candidates:
        if not path.is_file():
            continue
        try:
            values.update(
                collect_sensitive_values(
                    json.loads(path.read_text(encoding="utf-8"))
                )
            )
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
    return values


def scan_blob(
    path: Path,
    blob: bytes,
    local_values: set[str],
    *,
    check_generic_email: bool = True,
) -> list[str]:
    findings: list[str] = []
    for label, pattern in BYTE_PATTERNS.items():
        if pattern.search(blob):
            findings.append(f"{path}: matches {label} pattern")

    if check_generic_email:
        for match in EMAIL_PATTERN.finditer(blob):
            domain = match.group(1).decode("ascii", errors="ignore").lower()
            if domain not in SAFE_EMAIL_DOMAINS:
                findings.append(f"{path}: contains a non-placeholder email address")
                break

    for value in local_values:
        utf8 = value.encode("utf-8", errors="ignore")
        utf16 = value.encode("utf-16-le", errors="ignore")
        if (utf8 and utf8 in blob) or (utf16 and utf16 in blob):
            findings.append(f"{path}: contains a value from a local credential store")
            break
    return findings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--binary", type=Path)
    args = parser.parse_args()

    root = args.source.resolve()
    local_values = load_local_sensitive_values()
    findings: list[str] = []
    scanned = 0

    for path in iter_source_files(root):
        relative = path.relative_to(root)
        if path.name.lower() in FORBIDDEN_NAMES:
            findings.append(f"{relative}: forbidden local-data filename")
            continue
        try:
            blob = path.read_bytes()
        except OSError as exc:
            findings.append(f"{relative}: could not read ({type(exc).__name__})")
            continue
        scanned += 1
        findings.extend(scan_blob(relative, blob, local_values))

    if args.binary:
        binary = args.binary.resolve()
        if not binary.is_file():
            findings.append(f"{binary}: executable not found")
        else:
            scanned += 1
            findings.extend(
                scan_blob(
                    binary.name,
                    binary.read_bytes(),
                    local_values,
                    check_generic_email=False,
                )
            )

    if findings:
        print("Release verification FAILED:")
        for finding in sorted(set(findings)):
            print(f"- {finding}")
        return 1

    print(
        f"Release verification passed: {scanned} files checked; "
        f"{len(local_values)} local sensitive values compared without disclosure."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
