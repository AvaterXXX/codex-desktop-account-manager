#!/usr/bin/env python3
"""Publish the audited Git index through GitHub's Git Data API when git push is blocked."""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
from typing import Any


def run(*args: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        input=input_text,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def gh_api(
    repo: str,
    endpoint: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    allow_missing: bool = False,
) -> dict[str, Any] | None:
    command = ["gh", "api", f"repos/{repo}/{endpoint}", "-X", method]
    input_text = None
    if payload is not None:
        command.extend(["--input", "-"])
        input_text = json.dumps(payload, ensure_ascii=False)
    result = run(*command, input_text=input_text)
    if result.returncode != 0:
        if allow_missing and ("404" in result.stderr or "409" in result.stderr):
            return None
        raise RuntimeError(
            f"GitHub API {method} {endpoint} failed: {result.stderr.strip()}"
        )
    if not result.stdout.strip():
        return {}
    parsed = json.loads(result.stdout)
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return [
        item.decode("utf-8", errors="strict")
        for item in result.stdout.split(b"\0")
        if item
    ]


def committed_bytes(path: str) -> bytes:
    result = subprocess.run(
        ["git", "show", f"HEAD:{path}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Could not read committed file: {path}")
    return result.stdout


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--branch", default="main")
    args = parser.parse_args()

    status = run("git", "status", "--porcelain")
    if status.returncode != 0 or status.stdout.strip():
        raise RuntimeError("Working tree must be clean before API publication")

    files = tracked_files()
    if not files:
        raise RuntimeError("No tracked files to publish")

    existing = gh_api(
        args.repo,
        f"git/ref/heads/{args.branch}",
        allow_missing=True,
    )
    if not existing:
        gh_api(
            args.repo,
            "contents/.bootstrap",
            method="PUT",
            payload={
                "message": "Initialize repository for audited source upload",
                "content": base64.b64encode(b"bootstrap\n").decode("ascii"),
                "branch": args.branch,
            },
        )
        existing = gh_api(
            args.repo,
            f"git/ref/heads/{args.branch}",
        )

    tree: list[dict[str, str]] = []
    for index, path in enumerate(files, 1):
        blob = gh_api(
            args.repo,
            "git/blobs",
            method="POST",
            payload={
                "content": base64.b64encode(committed_bytes(path)).decode("ascii"),
                "encoding": "base64",
            },
        )
        assert blob is not None
        tree.append(
            {
                "path": path.replace("\\", "/"),
                "mode": "100644",
                "type": "blob",
                "sha": str(blob["sha"]),
            }
        )
        print(f"Uploaded audited file {index}/{len(files)}: {path}")

    tree_result = gh_api(
        args.repo,
        "git/trees",
        method="POST",
        payload={"tree": tree},
    )
    assert tree_result is not None

    parents: list[str] = []
    if existing:
        sha = ((existing.get("object") or {}).get("sha"))
        if sha:
            parents.append(str(sha))

    message_result = run("git", "log", "-1", "--pretty=%B")
    message = message_result.stdout.strip() or "Publish audited source"
    commit = gh_api(
        args.repo,
        "git/commits",
        method="POST",
        payload={
            "message": message,
            "tree": str(tree_result["sha"]),
            "parents": parents,
        },
    )
    assert commit is not None

    if existing:
        gh_api(
            args.repo,
            f"git/refs/heads/{args.branch}",
            method="PATCH",
            payload={"sha": str(commit["sha"]), "force": False},
        )
    else:
        gh_api(
            args.repo,
            "git/refs",
            method="POST",
            payload={
                "ref": f"refs/heads/{args.branch}",
                "sha": str(commit["sha"]),
            },
        )

    print(
        f"Published {len(files)} audited files to "
        f"https://github.com/{args.repo}/tree/{args.branch}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
