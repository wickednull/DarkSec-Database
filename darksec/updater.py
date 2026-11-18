"""Program updates from the installation's git remote.

The updater rolls the program files forward from ``origin`` using a
fast-forward-only merge. It refuses to run on a frozen build, outside a git
checkout, on a detached HEAD, inside a larger repository, or without an
``origin`` remote. Local work is preserved: tracked edits are saved as a
binary patch plus file copies under ``fielddata/backups/pre-update-*/`` and
reapplied after the merge; if they conflict, the updated tree stays clean and
the patch is kept for manual recovery. Untracked or ignored files that the
incoming commit would overwrite are moved into the same backup first. Local
commits ahead of the remote still require a push or reset because the updater
never rewrites history or creates a merge commit. Runtime data lives in
``fielddata/`` and is ignored by git, so updates never touch operator data.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
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


# ---------------------------------------------------------------------------
# Background apply job + human-readable log
# ---------------------------------------------------------------------------
_apply_lock = threading.RLock()
_apply_jobs: dict[str, dict[str, Any]] = {}
_apply_current: dict[str, Any] | None = None


def _log(job: dict[str, Any] | None, message: str, level: str = "info",
         percent: int | None = None, step: str | None = None) -> None:
    if job is None:
        return
    line = {"ts": _now(), "level": level, "message": str(message)[:500]}
    with _apply_lock:
        job["log"].append(line)
        if len(job["log"]) > 600:
            del job["log"][:200]
        if percent is not None:
            job["percent"] = max(0, min(100, int(percent)))
        if step:
            job["step"] = str(step)
        job["updated_at"] = line["ts"]


def _snapshot(job: dict[str, Any] | None) -> dict[str, Any] | None:
    if job is None:
        return None
    with _apply_lock:
        return {**job, "log": [dict(item) for item in job.get("log", [])]}


def start_apply() -> dict[str, Any]:
    """Run apply() in a thread and return a pollable job with a live log."""
    global _apply_current
    with _apply_lock:
        if _apply_current and _apply_current.get("state") == "running":
            return _snapshot(_apply_current) or {}
        job = {
            "id": uuid.uuid4().hex[:12],
            "state": "running",
            "percent": 0,
            "step": "Starting",
            "log": [],
            "result": None,
            "error": None,
            "started": _now(),
            "updated_at": _now(),
            "finished": None,
        }
        _apply_jobs[job["id"]] = job
        _apply_current = job
    threading.Thread(target=_run_apply_job, args=(job,), daemon=True).start()
    return _snapshot(job) or {}


def apply_job_status(job_id: str | None = None) -> dict[str, Any] | None:
    with _apply_lock:
        job = _apply_jobs.get(job_id) if job_id else _apply_current
        return _snapshot(job)


def _run_apply_job(job: dict[str, Any]) -> None:
    try:
        result = apply(job)
        job["result"] = result
        job["error"] = None if result.get("ok") else result.get("error")
        if result.get("ok"):
            _log(job, result.get("message") or "Update complete.", "success", 100, "Complete")
            job["state"] = "done"
        else:
            error = result.get("error") or "Update failed."
            if not job["log"] or job["log"][-1].get("message") != error:
                _log(job, error, "error", 100, "Failed")
            job["state"] = "error"
    except Exception as exc:  # never leave the job stuck
        job["error"] = f"{type(exc).__name__}: {exc}"[:400]
        _log(job, job["error"], "error", 100, "Failed")
        job["state"] = "error"
    finally:
        job["finished"] = _now()


def _git_bytes(root: Path, args: list[str], timeout: int = LOCAL_TIMEOUT) -> tuple[int, bytes, str]:
    """Run git with raw stdout, for patches that may not be valid UTF-8."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, timeout=timeout, env=_env(), check=False,
        )
    except FileNotFoundError:
        return 127, b"", "git is not installed"
    except subprocess.TimeoutExpired:
        return 124, b"", f"git command timed out after {timeout}s"
    except Exception as exc:  # pragma: no cover - defensive
        return 1, b"", str(exc)
    err = proc.stderr.decode("utf-8", "replace").strip()
    return proc.returncode, proc.stdout, err


def _git_stream(root: Path, args: list[str], job: dict[str, Any] | None,
                timeout: int = FETCH_TIMEOUT) -> tuple[int, str, str]:
    """Run git while streaming progress lines into the update log."""
    try:
        proc = subprocess.Popen(
            ["git", "-C", str(root), *args],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            env=_env(), bufsize=1,
        )
    except FileNotFoundError:
        return 127, "", "git is not installed"
    except Exception as exc:
        return 1, "", str(exc)
    lines: list[str] = []
    timed_out = threading.Event()

    def _kill() -> None:
        timed_out.set()
        try:
            proc.kill()
        except Exception:
            pass

    timer = threading.Timer(timeout, _kill)
    timer.daemon = True
    timer.start()
    try:
        assert proc.stdout is not None
        while True:
            line = proc.stdout.readline()
            if line:
                text = "\r".join(part.strip() for part in line.split("\r") if part.strip())
                if not text:
                    continue
                text = re.sub(r"(https?://)[^/\s@]+@", r"\1", text)[:300]
                lines.append(text)
                _log(job, f"git: {text}")
                continue
            if proc.poll() is not None:
                break
    finally:
        timer.cancel()
        try:
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    if timed_out.is_set():
        return 124, "", f"git command timed out after {timeout}s"
    return proc.returncode or 0, "\n".join(lines[-20:]), ""


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
        "preserve_local": True,
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
    elif not st["upstream"]:
        st["reason"] = "No upstream branch is configured for this checkout."
    else:
        st["can_update"] = True
    if st["dirty"] and st["can_update"]:
        # A dirty tree is a warning, not a blocker: apply() snapshots it first.
        st["reason"] = (
            "Local changes are present. They will be backed up and reapplied after the update."
        )
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


def _backup_path() -> Path:
    """Create a unique pre-update backup folder under fielddata/backups."""
    config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = config.BACKUP_DIR / f"pre-update-{stamp}"
    counter = 1
    while path.exists():
        counter += 1
        path = config.BACKUP_DIR / f"pre-update-{stamp}-{counter}"
    path.mkdir(parents=True)
    return path


def _lines(text: str) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def _incoming_added_files(root: Path, upstream: str) -> set[str]:
    rc, out, _err = _git(
        root, ["diff", "--name-only", "--diff-filter=A", "HEAD", upstream]
    )
    return set(_lines(out)) if rc == 0 else set()


def _untracked_conflicts(root: Path, incoming: set[str]) -> list[str]:
    """Existing local paths the incoming commit would create or overwrite.

    This includes ignored files, which a fast-forward could otherwise replace
    silently, not just files reported by --exclude-standard.
    """
    if not incoming:
        return []
    rc, out, _err = _git(root, ["ls-files"])
    tracked = set(_lines(out)) if rc == 0 else set()
    conflicts = []
    for rel in incoming:
        if rel in tracked:
            continue
        try:
            if (root / rel).exists():
                conflicts.append(rel)
        except OSError:
            continue
    return sorted(conflicts)


def _backup_local_changes(root: Path, backup: Path) -> dict[str, Any]:
    """Save a binary-safe patch plus copies of every modified tracked file."""
    patch_path = backup / "local-changes.patch"
    rc, patch, err = _git_bytes(root, ["diff", "--binary", "HEAD"])
    if rc != 0:
        raise RuntimeError(f"could not save local changes: {_clean_error(err)}")
    # Keep the raw bytes: git apply needs the final newline and local files may
    # not be valid UTF-8.
    if patch and not patch.endswith(b"\n"):
        patch += b"\n"
    patch_path.write_bytes(patch)
    rc, names, _err = _git(root, ["diff", "--name-only", "HEAD"])
    copied: list[str] = []
    deleted: list[str] = []
    for rel in _lines(names):
        src = root / rel
        dst = backup / "files" / rel
        try:
            if src.is_file():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                copied.append(rel)
            else:
                deleted.append(rel)
        except OSError as exc:
            raise RuntimeError(f"could not back up {rel}: {exc}") from exc
    return {
        "patch": str(patch_path),
        "patch_empty": not bool(patch.strip()),
        "files": copied,
        "deleted": deleted,
    }


def _move_untracked_conflicts(root: Path, backup: Path, conflicts: list[str]) -> list[str]:
    """Move untracked files that the incoming commit wants to create."""
    moved: list[str] = []
    for rel in conflicts:
        src = root / rel
        dst = backup / "untracked-conflicts" / rel
        if not src.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        moved.append(rel)
    return moved


def _restore_untracked(root: Path, backup: Path, moved: list[str]) -> None:
    for rel in moved:
        src = backup / "untracked-conflicts" / rel
        dst = root / rel
        if src.exists() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))


def _prune_backups(keep: int = 10) -> None:
    """Keep a short rollback history without letting backups fill the disk."""
    try:
        folders = sorted(
            (p for p in config.BACKUP_DIR.glob("pre-update-*") if p.is_dir()),
            key=lambda p: p.name,
        )
    except OSError:
        return
    for old in folders[:-keep]:
        shutil.rmtree(old, ignore_errors=True)


def _write_manifest(backup: Path, st: dict[str, Any], info: dict[str, Any],
                    moved: list[str]) -> None:
    manifest = {
        "created_at": _now(),
        "reason": "DarkSec update with local changes",
        "branch": st.get("branch"),
        "upstream": st.get("upstream"),
        "old_commit": st.get("commit_full"),
        "old_commit_short": st.get("commit"),
        "modified_files": info.get("files") or [],
        "deleted_files": info.get("deleted") or [],
        "untracked_moved": moved,
        "patch": info.get("patch"),
        "restore": (
            "Apply local-changes.patch with: git apply --3way --binary local-changes.patch. "
            "Copies of the modified files are under files/ and moved untracked files under "
            "untracked-conflicts/."
        ),
    }
    (backup / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    _prune_backups()


def apply(job: dict[str, Any] | None = None) -> dict[str, Any]:
    """Fast-forward the checkout from origin. Never force or create a merge."""
    def note(message: str, level: str = "info", percent: int | None = None,
             step: str | None = None) -> None:
        _log(job, message, level, percent, step)

    note("Checking installation and git state…", percent=2, step="Checking")
    st = status()
    st["checked_at"] = _now()
    if st.get("frozen"):
        note("Packaged builds cannot self-update.", "warn")
    elif st.get("repo"):
        note(f"Repository: {st.get('root')}")
        note(f"Branch {st.get('branch') or '?'} · commit {st.get('commit') or '?'} "
             f"· upstream {st.get('upstream') or 'none'}")
        if st.get("dirty"):
            note(f"{st.get('dirty_count')} modified tracked file(s) will be backed up first.",
                 "warn")
    if not st["can_update"]:
        error = st["reason"] or "Update is not available for this installation."
        note(error, "error", 100, "Blocked")
        st.update({"ok": False, "error": error, "updated": False, "restart_required": False})
        return st

    root = Path(st["root"])
    note("Fetching from origin (up to 90s)…", percent=12, step="Fetching")
    rc, out, err = _git_stream(root, ["fetch", "--prune", "--progress", "origin"],
                               job, timeout=FETCH_TIMEOUT)
    if rc != 0:
        error = _clean_error(err or out)
        note(f"Fetch failed: {error}", "error", 100, "Failed")
        st.update({"ok": False, "error": error, "updated": False, "restart_required": False})
        return st
    note("Fetch complete.", "success", 22, "Fetching")

    ahead, behind, count_err = _counts(root, st["upstream"])
    if ahead is None or behind is None:
        note(f"Could not compare with upstream: {count_err}", "error", 100, "Failed")
        st.update({"ok": False, "error": count_err, "updated": False, "restart_required": False})
        return st
    st["ahead"] = ahead
    st["behind"] = behind
    st["latest"] = _commit_info(root, st["upstream"])
    st["update_available"] = behind > 0
    old_commit = st["commit"]
    old_full = st["commit_full"]
    note(f"{behind} commit(s) behind, {ahead} local commit(s) ahead.", percent=28,
         step="Comparing")

    if behind == 0:
        note("Already up to date.", "success", 100, "Complete")
        st.update({
            "ok": True,
            "updated": False,
            "message": "Already up to date.",
            "restart_required": False,
        })
        return st
    if ahead > 0:
        error = (
            f"The branch has {ahead} local commit(s) not on {st['upstream']}; "
            "a fast-forward update is not possible. Push or reset manually."
        )
        note(error, "error", 100, "Blocked")
        st.update({
            "ok": False,
            "error": error,
            "updated": False,
            "restart_required": False,
        })
        return st

    # Snapshot anything local that the update could disturb. Tracked edits are
    # stored as a binary-safe patch plus file copies; untracked files that the
    # incoming commit would create are moved aside first so the fast-forward
    # cannot fail on them.
    incoming_added = _incoming_added_files(root, st["upstream"])
    untracked_conflicts = _untracked_conflicts(root, incoming_added)
    backup: Path | None = None
    backup_info: dict[str, Any] = {}
    moved: list[str] = []
    if st["dirty"] or untracked_conflicts:
        try:
            note("Saving local changes before touching the working tree…", percent=34,
                 step="Backing up")
            backup = _backup_path()
            if st["dirty"]:
                backup_info = _backup_local_changes(root, backup)
                note(f"Saved patch: {backup_info.get('patch')}")
                note(f"Copied {len(backup_info.get('files') or [])} modified file(s); "
                     f"{len(backup_info.get('deleted') or [])} deleted file(s).")
            if untracked_conflicts:
                moved = _move_untracked_conflicts(root, backup, untracked_conflicts)
                note(f"Moved {len(moved)} untracked/ignored file(s) into the backup: "
                     f"{', '.join(moved[:6])}{'…' if len(moved) > 6 else ''}", "warn")
            _write_manifest(backup, st, backup_info, moved)
            note(f"Backup ready at {backup}", "success", 46, "Backing up")
        except Exception as exc:
            error = f"Could not back up local changes, update aborted: {exc}"
            note(error, "error", 100, "Failed")
            st.update({
                "ok": False,
                "error": error,
                "updated": False,
                "restart_required": False,
            })
            return st

    if st["dirty"]:
        note("Resetting tracked files to HEAD (local copy is in the backup)…", percent=52,
             step="Preparing")
        rc, out, err = _git(root, ["reset", "--hard", "HEAD"])
        if rc != 0:
            error = f"Could not prepare the working tree: {_clean_error(err or out)}"
            note(error, "error", 100, "Failed")
            st.update({
                "ok": False,
                "error": error,
                "updated": False,
                "restart_required": False,
                "backup_dir": str(backup) if backup else None,
            })
            return st
        note("Working tree is clean.", "success", 56, "Preparing")

    note(f"Fast-forwarding to {st['upstream']}…", percent=62, step="Merging")
    rc, out, err = _git(root, ["merge", "--ff-only", st["upstream"]], timeout=PULL_TIMEOUT)
    if rc != 0:
        error = _clean_error(err or out)
        note(f"Fast-forward failed: {error}", "error", 100, "Failed")
        restored = None
        if backup:
            # Put the old tree and the operator's changes back if the update
            # itself could not be applied.
            note("Restoring the previous tree and local changes…", "warn", 90, "Restoring")
            _git(root, ["reset", "--hard", st["commit_full"] or "HEAD"])
            _restore_untracked(root, backup, moved)
            if backup_info.get("patch") and not backup_info.get("patch_empty"):
                rc2, _out2, _err2 = _git(
                    root, ["apply", "--3way", "--binary", backup_info["patch"]]
                )
                restored = rc2 == 0
                if not restored:
                    # Never leave conflict markers in code that must still run.
                    _git(root, ["reset", "--hard", st["commit_full"] or "HEAD"])
        st.update({
            "ok": False,
            "error": error,
            "updated": False,
            "restart_required": False,
            "backup_dir": str(backup) if backup else None,
            "local_changes_restored": restored,
        })
        return st
    note("Fast-forward complete.", "success", 76, "Merging")

    # Reapply the operator's tracked edits on top of the new code. If they do
    # not apply cleanly the updated tree is reset to a clean state so DarkSec
    # still starts; the patch and file copies remain in the backup folder.
    local_state = "untracked-moved" if moved and not st["dirty"] else "none"
    if backup and st["dirty"]:
        note("Reapplying your local changes on top of the new code…", percent=82,
             step="Reapplying")
        patch_path = backup_info.get("patch") or ""
        if not patch_path or backup_info.get("patch_empty"):
            local_state = "reapplied"
            note("No tracked changes needed reapplying.", "success")
        else:
            rc, out, err = _git(
                root,
                ["apply", "--3way", "--binary", "--whitespace=nowarn", patch_path],
            )
            unmerged = _lines(_git(root, ["diff", "--name-only", "--diff-filter=U"])[1])
            if rc == 0 and not unmerged:
                # Leave the operator's edits unstaged, as they were before.
                _git(root, ["reset"])
                local_state = "reapplied"
                note("Local changes reapplied cleanly.", "success", 90, "Reapplying")
            else:
                _git(root, ["reset", "--hard", "HEAD"])
                local_state = "saved"
                note("Local changes conflicted with the update. The tree was reset to the "
                     f"new clean code; the patch and file copies are in {backup}.", "warn",
                     90, "Reapplying")

    new_st = status()
    updated = bool(old_full and new_st["commit_full"] != old_full)
    message = (
        f"Updated {old_commit or '?'} → {new_st['commit'] or '?'}. Restart DarkSec to run the new code."
        if updated else "Already up to date."
    )
    if local_state == "reapplied":
        message += " Local changes were backed up and reapplied."
    elif local_state == "saved":
        message += (
            f" Local changes could not be reapplied cleanly; the updated tree is clean and "
            f"the backup is at {backup}. Reapply local-changes.patch manually if needed."
        )
    if moved:
        message += (
            f" Conflicting untracked file(s) were moved to {backup} before the update."
        )
    if updated:
        note(f"Updated {old_commit or '?'} → {new_st['commit'] or '?'}.", "success", 98,
             "Complete")
        note("Restart DarkSec to run the new code.", "warn", 100, "Complete")
    else:
        note(message, "success", 100, "Complete")
    new_st.update({
        "ok": True,
        "updated": updated,
        "old_commit": old_commit,
        "new_commit": new_st["commit"],
        "message": message,
        "restart_required": updated,
        "checked_at": _now(),
        "backup_dir": str(backup) if backup else None,
        "backup_patch": backup_info.get("patch"),
        "local_changes": local_state,
        "untracked_moved": moved,
        "local_changes_backed_up": bool(backup),
        "local_changes_saved": local_state in ("saved", "reapplied", "untracked-moved"),
    })
    return new_st
