"""Updater integration tests: local edits must survive a normal update."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="git is required")


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr or proc.stdout}")
    return proc.stdout.strip()


def make_remote(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Return (bare remote, work clone, upstream clone) with one commit."""
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    other = tmp_path / "other"
    subprocess.run(["git", "-c", "init.defaultBranch=main", "init", "--bare", str(remote)],
                   check=True, capture_output=True)
    subprocess.run(["git", "-c", "init.defaultBranch=main", "clone", str(remote), str(work)],
                   check=True, capture_output=True)
    for repo in (work,):
        git(repo, "config", "user.email", "test@example.com")
        git(repo, "config", "user.name", "DarkSec Test")
    (work / "app.py").write_text("a = 1\nb = 2\nc = 3\nd = 4\n", encoding="utf-8")
    (work / "README.md").write_text("initial\n", encoding="utf-8")
    git(work, "add", ".")
    git(work, "commit", "-m", "initial")
    git(work, "push", "-u", "origin", "main")
    subprocess.run(["git", "clone", str(remote), str(other)], check=True, capture_output=True)
    git(other, "config", "user.email", "test@example.com")
    git(other, "config", "user.name", "DarkSec Test")
    return remote, work, other


def publish_upstream(work: Path, other: Path, *, app_py: str,
                     new_files: dict[str, str] | None = None) -> None:
    (other / "app.py").write_text(app_py, encoding="utf-8")
    for name, text in (new_files or {}).items():
        (other / name).write_text(text, encoding="utf-8")
    git(other, "add", ".")
    git(other, "commit", "-m", "upstream update")
    git(other, "push", "origin", "main")
    git(work, "fetch", "origin")


def setup_updater(monkeypatch, tmp_path: Path, work: Path) -> None:
    from darksec import config

    monkeypatch.setattr(config, "RESOURCE_ROOT", work)
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "backups")


def test_dirty_tracked_changes_are_backed_up_and_reapplied(tmp_path, monkeypatch):
    from darksec import updater

    _remote, work, other = make_remote(tmp_path)
    publish_upstream(work, other, app_py="a = 2\nb = 2\nc = 3\nd = 4\n",
                     new_files={"new.txt": "upstream\n"})
    (work / "app.py").write_text("a = 1\nb = 2\nc = 3\nd = 40\n", encoding="utf-8")
    setup_updater(monkeypatch, tmp_path, work)

    st = updater.status()
    assert st["dirty"] is True
    assert st["can_update"] is True
    assert "backed up" in st["reason"]

    result = updater.apply()
    assert result["ok"] is True, result
    assert result["updated"] is True
    assert result["local_changes"] == "reapplied"
    text = (work / "app.py").read_text(encoding="utf-8")
    assert "a = 2" in text
    assert "d = 40" in text
    assert (work / "new.txt").read_text(encoding="utf-8") == "upstream\n"

    backup = Path(result["backup_dir"])
    assert backup.is_dir()
    assert (backup / "local-changes.patch").is_file()
    assert (backup / "files" / "app.py").is_file()
    assert (backup / "manifest.json").is_file()


def test_conflicting_local_edit_keeps_a_clean_updated_tree(tmp_path, monkeypatch):
    from darksec import updater

    _remote, work, other = make_remote(tmp_path)
    publish_upstream(work, other, app_py="a = 2\nb = 2\nc = 3\nd = 4\n")
    (work / "app.py").write_text("a = 99\nb = 2\nc = 3\nd = 4\n", encoding="utf-8")
    setup_updater(monkeypatch, tmp_path, work)

    result = updater.apply()
    assert result["ok"] is True, result
    assert result["updated"] is True
    assert result["local_changes"] == "saved"
    assert "could not be reapplied cleanly" in result["message"]
    # The updated tree must be clean so DarkSec can still start.
    assert git(work, "status", "--porcelain") == ""
    assert (work / "app.py").read_text(encoding="utf-8") == "a = 2\nb = 2\nc = 3\nd = 4\n"
    backup = Path(result["backup_dir"])
    assert (backup / "local-changes.patch").is_file()
    assert "a = 99" in (backup / "local-changes.patch").read_text(encoding="utf-8")


def test_untracked_file_that_upstream_adds_is_moved_aside(tmp_path, monkeypatch):
    from darksec import updater

    _remote, work, other = make_remote(tmp_path)
    publish_upstream(work, other, app_py="a = 2\nb = 2\nc = 3\nd = 4\n",
                     new_files={"new.txt": "upstream\n"})
    (work / "new.txt").write_text("local untracked\n", encoding="utf-8")
    setup_updater(monkeypatch, tmp_path, work)

    st = updater.status()
    assert st["dirty"] is False
    result = updater.apply()
    assert result["ok"] is True, result
    assert result["updated"] is True
    assert result["untracked_moved"] == ["new.txt"]
    assert (work / "new.txt").read_text(encoding="utf-8") == "upstream\n"
    backup = Path(result["backup_dir"])
    saved = backup / "untracked-conflicts" / "new.txt"
    assert saved.read_text(encoding="utf-8") == "local untracked\n"


def test_clean_checkout_still_updates_without_backup(tmp_path, monkeypatch):
    from darksec import updater

    _remote, work, other = make_remote(tmp_path)
    publish_upstream(work, other, app_py="a = 3\nb = 2\nc = 3\nd = 4\n")
    setup_updater(monkeypatch, tmp_path, work)

    result = updater.apply()
    assert result["ok"] is True
    assert result["updated"] is True
    assert result["local_changes"] == "none"
    assert result["backup_dir"] is None
    assert (work / "app.py").read_text(encoding="utf-8") == "a = 3\nb = 2\nc = 3\nd = 4\n"


def test_ignored_local_file_is_not_silently_overwritten(tmp_path, monkeypatch):
    from darksec import updater

    _remote, work, other = make_remote(tmp_path)
    (work / ".gitignore").write_text("*.local\n", encoding="utf-8")
    git(work, "add", ".gitignore")
    git(work, "commit", "-m", "ignore local files")
    git(work, "push", "origin", "main")
    git(other, "pull", "--ff-only")

    (other / "notes.local").write_text("upstream version\n", encoding="utf-8")
    git(other, "add", "-f", "notes.local")
    git(other, "commit", "-m", "add notes")
    git(other, "push", "origin", "main")
    git(work, "fetch", "origin")

    (work / "notes.local").write_text("local ignored version\n", encoding="utf-8")
    setup_updater(monkeypatch, tmp_path, work)

    result = updater.apply()
    assert result["ok"] is True, result
    assert result["untracked_moved"] == ["notes.local"]
    assert (work / "notes.local").read_text(encoding="utf-8") == "upstream version\n"
    backup = Path(result["backup_dir"])
    saved = backup / "untracked-conflicts" / "notes.local"
    assert saved.read_text(encoding="utf-8") == "local ignored version\n"


def test_background_apply_job_reports_progress_and_log(tmp_path, monkeypatch):
    import time

    from darksec import updater

    _remote, work, other = make_remote(tmp_path)
    publish_upstream(work, other, app_py="a = 5\nb = 2\nc = 3\nd = 4\n")
    setup_updater(monkeypatch, tmp_path, work)

    job = updater.start_apply()
    assert job["state"] == "running"
    assert job["id"]
    selected = job
    deadline = time.time() + 15
    while time.time() < deadline:
        selected = updater.apply_job_status(job["id"])
        if selected["state"] != "running":
            break
        time.sleep(0.05)

    assert selected["state"] == "done", selected
    assert selected["percent"] == 100
    assert selected["result"]["updated"] is True
    messages = " | ".join(line["message"] for line in selected["log"])
    assert "Fetching from origin" in messages
    assert "Fast-forwarding" in messages
    assert "Updated" in messages
    assert any(line["level"] == "success" for line in selected["log"])
