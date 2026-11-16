"""Program updates from the installation's git remote.

The updater only rolls the program files forward from ``origin`` using a
fast-forward-only merge. It refuses to run on a frozen build, outside a git
checkout, on a detached HEAD, inside a larger repository, without an
``origin`` remote, or when tracked files have local changes. Untracked files
are kept unless the incoming changes would overwrite one. Runtime data lives
in ``fielddata/`` and is ignored by git, so updates never touch operator data.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__, config

FETCH_TIMEOUT = 90
PULL_TIMEOUT = 120
LOCAL_TIMEOUT = 20
MAX_DIRTY_FILES = 12


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _env() -> dict[str, str]:
    env = os.environ.copy()
    # Never block on an interactive username/password or SSH passphrase prompt.
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    env["LC_ALL"] = "C"
    return env


def _clean_error(text: str, limit: int = 600) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    message = " | ".join(lines[-4:]) or "unknown git error"
    # Avoid echoing credentials from remotes such as https://user:token@host/...
    message = re.sub(r"(https?://)[^/\s@]+@", r"\1", message)
    return message[:limit]


def _git(root: Path, args: list[str], timeout: int = LOCAL_TIMEOUT) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, env=_env(), check=False,
        )
    except FileNotFoundError:
        return 127, "", "git is not installed"
    except subprocess.TimeoutExpired:
        return 124, "", f"git command timed out after {timeout}s"
    except Exception as exc:  # pragma: no cover - defensive
        return 1, "", str(exc)
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def _repo_root() -> Path | None:
    if getattr(sys, "frozen", False):
        return None
    try:
        rc, out, _err = _git(config.RESOURCE_ROOT, ["rev-parse", "--show-toplevel"])
    except Exception:
        return None
    if rc != 0 or not out:
        return None
    try:
        root = Path(out.splitlines()[0]).resolve()
    except Exception:
        return None
    return root if root.is_dir() else None


def _commit_info(root: Path, ref: str = "HEAD") -> dict[str, Any]:
    rc, out, _err = _git(
        root, ["log", "-1", "--format=%H%n%h%n%an%n%ad%n%s", "--date=short", ref]
    )
    if rc != 0 or not out:
        return {}
    lines = out.splitlines()
    while len(lines) < 5:
        lines.append("")
    return {
        "commit_full": lines[0],
        "commit": lines[1],
        "author": lines[2],
        "commit_date": lines[3],
        "subject": lines[4],
    }


def _counts(root: Path, upstream: str) -> tuple[int | None, int | None, str]:
    rc, out, err = _git(
        root, ["rev-list", "--left-right", "--count", f"HEAD...{upstream}"]
    )
    if rc != 0:
        return None, None, _clean_error(err or out)
    parts = out.split()
    if len(parts) != 2:
        return None, None, "could not read ahead/behind counts"
    try:
        ahead, behind = int(parts[0]), int(parts[1])
    except ValueError:
        return None, None, "could not read ahead/behind counts"
    return ahead, behind, ""


def status() -> dict[str, Any]:
    """Local update state. Does not contact the network."""
    st: dict[str, Any] = {
        "app": config.APP_NAME,
        "version": __version__,
        "frozen": bool(getattr(sys, "frozen", False)),
        "repo": False,
        "root": None,
        "branch": None,
        "commit": None,
        "commit_full": None,
        "author": "",
        "commit_date": None,
        "subject": "",
        "dirty": None,
        "dirty_count": 0,
        "dirty_files": [],
        "untracked_count": 0,
        "untracked_files": [],
        "remote": "origin",
        "remote_url": None,
        "upstream": None,
        "can_update": False,
        "reason": "",
        "checked_at": None,
        "behind": None,
        "ahead": None,
        "latest": None,
        "update_available": None,
        "error": None,
        "note": "Program files only. fielddata/ is ignored by git and is never changed by an update.",
    }
    if st["frozen"]:
        st["reason"] = "Packaged builds cannot self-update; install a new release instead."
        return st
    if not shutil.which("git"):
        st["reason"] = "git is not installed; self-update is unavailable."
        return st

    root = _repo_root()
    if root is None:
        st["reason"] = "This installation is not a git checkout."
        return st
    try:
        install_root = config.RESOURCE_ROOT.resolve()
    except Exception:
        install_root = config.RESOURCE_ROOT
    if root != install_root:
        st["repo"] = True
        st["root"] = str(root)
        st["reason"] = (
            "The installation is inside a larger git repository; self-update is disabled."
        )
        return st
    st["repo"] = True
    st["root"] = str(root)

    rc, branch, _err = _git(root, ["rev-parse", "--abbrev-ref", "HEAD"])
    if rc == 0 and branch:
        st["branch"] = branch

    rc, remote_url, _err = _git(root, ["remote", "get-url", "origin"])
    if rc == 0 and remote_url:
        st["remote_url"] = remote_url

    rc, porcelain, _err = _git(root, ["status", "--porcelain", "--untracked-files=normal"])
    if rc == 0:
        entries = [line.strip() for line in porcelain.splitlines() if line.strip()]
        # Untracked library files are normal in a field install: keep them,
        # and let git itself refuse the merge only if one would be overwritten.
        tracked = [entry for entry in entries if not entry.startswith("??")]
        untracked = [entry[3:] for entry in entries if entry.startswith("??")]
        st["dirty"] = bool(tracked)
        st["dirty_count"] = len(tracked)
        st["dirty_files"] = tracked[:MAX_DIRTY_FILES]
        st["untracked_count"] = len(untracked)
        st["untracked_files"] = untracked[:MAX_DIRTY_FILES]

    info = _commit_info(root)
    st.update({k: v for k, v in info.items() if k in st})

    if st["branch"] and st["branch"] != "HEAD":
        rc, upstream, _err = _git(
            root, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"]
        )
        if rc == 0 and upstream:
            st["upstream"] = upstream
        elif st["remote_url"]:
            st["upstream"] = f"origin/{st['branch']}"

    if not st["branch"]:
        st["reason"] = "Could not determine the current branch."
    elif st["branch"] == "HEAD":
        st["reason"] = "The checkout is on a detached HEAD; check out a branch before updating."
    elif not st["remote_url"]:
        st["reason"] = "No origin remote is configured for this checkout."
    elif st["dirty"] is None:
        st["reason"] = "Could not read the working tree state; refusing to update."
    elif st["dirty"]:
        st["reason"] = (
            "Local changes are present; commit or discard them before updating."
        )
    elif not st["upstream"]:
        st["reason"] = "No upstream branch is configured for this checkout."
    else:
        st["can_update"] = True
    return st


def check() -> dict[str, Any]:
    """Fetch origin and report whether a fast-forward update is available."""
    st = status()
    st["checked_at"] = _now()
    if not st["repo"] or st["frozen"]:
        return st
    if not st["remote_url"]:
        st["error"] = st["reason"] or "No origin remote is configured."
        return st
    if not st["upstream"]:
        st["error"] = st["reason"] or "No upstream branch is configured."
        return st

    root = Path(st["root"])
    rc, out, err = _git(root, ["fetch", "--prune", "origin"], timeout=FETCH_TIMEOUT)
    if rc != 0:
        st["error"] = _clean_error(err or out)
        return st

    ahead, behind, count_err = _counts(root, st["upstream"])
    if ahead is None or behind is None:
        st["error"] = count_err
        return st
    st["ahead"] = ahead
    st["behind"] = behind
    st["update_available"] = behind > 0
    st["latest"] = _commit_info(root, st["upstream"])
    return st


def apply() -> dict[str, Any]:
    """Fast-forward the checkout from origin. Never force or create a merge."""
    st = status()
    st["checked_at"] = _now()
    if not st["can_update"]:
        error = st["reason"] or "Update is not available for this installation."
        if st["dirty"]:
            files = ", ".join(st["dirty_files"][:5])
            error = (
                f"Local changes block the update ({st['dirty_count']} file(s): "
                f"{files}). Commit or discard them first."
            )
        st.update({"ok": False, "error": error, "updated": False, "restart_required": False})
        return st

    root = Path(st["root"])
    rc, out, err = _git(root, ["fetch", "--prune", "origin"], timeout=FETCH_TIMEOUT)
    if rc != 0:
        st.update({"ok": False, "error": _clean_error(err or out), "updated": False, "restart_required": False})
        return st

    ahead, behind, count_err = _counts(root, st["upstream"])
    if ahead is None or behind is None:
        st.update({"ok": False, "error": count_err, "updated": False, "restart_required": False})
        return st
    st["ahead"] = ahead
    st["behind"] = behind
    st["latest"] = _commit_info(root, st["upstream"])
    st["update_available"] = behind > 0
    old_commit = st["commit"]
    old_full = st["commit_full"]

    if behind == 0:
        st.update({
            "ok": True,
            "updated": False,
            "message": "Already up to date.",
            "restart_required": False,
        })
        return st
    if ahead > 0:
        st.update({
            "ok": False,
            "error": (
                f"The branch has {ahead} local commit(s) not on {st['upstream']}; "
                "a fast-forward update is not possible. Push or reset manually."
            ),
            "updated": False,
            "restart_required": False,
        })
        return st

    rc, out, err = _git(root, ["merge", "--ff-only", st["upstream"]], timeout=PULL_TIMEOUT)
    if rc != 0:
        st.update({
            "ok": False,
            "error": _clean_error(err or out),
            "updated": False,
            "restart_required": False,
        })
        return st

    new_st = status()
    updated = bool(old_full and new_st["commit_full"] != old_full)
    new_st.update({
        "ok": True,
        "updated": updated,
        "old_commit": old_commit,
        "new_commit": new_st["commit"],
        "message": (
            f"Updated {old_commit or '?'} → {new_st['commit'] or '?'}. Restart DarkSec to run the new code."
            if updated else "Already up to date."
        ),
        "restart_required": updated,
        "checked_at": _now(),
    })
    return new_st
