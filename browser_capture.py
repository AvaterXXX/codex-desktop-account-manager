#!/usr/bin/env python3
"""Fake browser: write launched URL to a file then exit (for codex login hijack)."""
from __future__ import annotations

import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent / ".login_url.txt"


def main() -> int:
    url = " ".join(sys.argv[1:]).strip().strip('"')
    OUT.write_text(url, encoding="utf-8")
    print(url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
