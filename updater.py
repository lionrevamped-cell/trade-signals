#!/usr/bin/env python3
"""
updater.py — Pulls the latest project version from GitHub on startup.

Designed to run from run.bat BEFORE the server starts. On each launch:

  1. Reads update_config.json for the GitHub user/repo/branch
  2. Calls api.github.com to get the latest commit SHA on the branch
  3. Compares against .last_update_sha (saved last time we updated)
  4. If new: downloads the source zip, extracts to a temp dir, copies
     code files into the project — PRESERVING user data
  5. If requirements.txt changed, reinstalls pip dependencies in .venv
  6. Saves the new SHA so we don't repeat next time

Files we NEVER overwrite (preserve the trader's local state):
  outcomes.json, learned_weights.json, learn_history.json,
  agent_reports/, backtest_history/, .venv/, .last_update_sha,
  update_config.json (in case the user customized it)

If anything fails (no internet, GitHub down, malformed zip), we silently
proceed without updating — the existing local code keeps working. This
script never breaks the user's ability to run the scanner.
"""

from __future__ import annotations

import json
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

PROJECT_DIR  = Path(__file__).parent
CONFIG_FILE  = PROJECT_DIR / "update_config.json"
SHA_FILE     = PROJECT_DIR / ".last_update_sha"
REQS_HASH_FILE = PROJECT_DIR / ".last_reqs_hash"

# Files / folders the updater will NEVER overwrite — these are the user's
# local trading state and Python venv. Loss here would set the trader back.
PRESERVE = {
    "outcomes.json",
    "learned_weights.json",
    "learn_history.json",
    ".learn_paused",
    ".last_update_sha",
    ".last_reqs_hash",
    "update_config.json",
    "agent_reports",
    "backtest_history",
    ".venv",
    "__pycache__",
    ".git",
    ".DS_Store",
}

NETWORK_TIMEOUT = 10  # seconds


def _log(msg: str) -> None:
    print(f"[updater] {msg}", flush=True)


# ── SSL ──────────────────────────────────────────────────────────────────────
# Python on Windows ships without OS root certs, so urllib's default SSL
# context fails on github.com with "unable to get local issuer certificate".
# certifi bundles a fresh Mozilla CA bundle and is a transitive dep of
# requests (which yfinance pulls in), so it's effectively always available.

def _make_ssl_context() -> ssl.SSLContext | None:
    """Return an SSL context that works on Windows. Uses certifi's bundle
    if available; falls back to the default context otherwise."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


# ── config ────────────────────────────────────────────────────────────────────

def _load_config() -> dict | None:
    if not CONFIG_FILE.exists():
        return None
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except Exception as e:
        _log(f"config unreadable: {e}")
        return None


# ── GitHub interaction ────────────────────────────────────────────────────────

def _http_get(url: str, timeout: int = NETWORK_TIMEOUT) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": "trade-signals-updater/1.0",
        "Accept":     "application/vnd.github+json",
    })
    with urllib.request.urlopen(req, timeout=timeout, context=_make_ssl_context()) as r:
        return r.read()


def _get_remote_sha(user: str, repo: str, branch: str) -> str:
    """Latest commit SHA on the branch — changes on every push."""
    url = f"https://api.github.com/repos/{user}/{repo}/commits/{branch}"
    data = json.loads(_http_get(url))
    return data["sha"]


def _download_zip(user: str, repo: str, branch: str, dest_path: Path) -> None:
    """Download the branch as a zip. GitHub serves a fresh zip every time."""
    url = f"https://github.com/{user}/{repo}/archive/refs/heads/{branch}.zip"
    req = urllib.request.Request(url, headers={"User-Agent": "trade-signals-updater/1.0"})
    with urllib.request.urlopen(req, timeout=NETWORK_TIMEOUT * 6,
                                context=_make_ssl_context()) as r, \
         open(dest_path, "wb") as f:
        shutil.copyfileobj(r, f)


# ── apply update ──────────────────────────────────────────────────────────────

def _file_hash(path: Path) -> str | None:
    if not path.exists():
        return None
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _apply_zip(zip_path: Path) -> tuple[bool, str, bool]:
    """
    Extract the zip and copy fresh files into the project.
    Returns (ok, message, requirements_changed).
    """
    old_reqs_hash = _file_hash(PROJECT_DIR / "requirements.txt")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        try:
            with zipfile.ZipFile(zip_path) as z:
                z.extractall(tmp_path)
        except zipfile.BadZipFile as e:
            return False, f"corrupt zip: {e}", False

        # GitHub zips contain ONE top-level dir named e.g. "trade-signals-main"
        children = [p for p in tmp_path.iterdir() if p.is_dir()]
        if not children:
            return False, "extracted folder not found", False
        src = children[0]

        copied = 0
        for item in src.iterdir():
            if item.name in PRESERVE:
                continue
            target = PROJECT_DIR / item.name
            try:
                if item.is_dir():
                    if target.exists():
                        shutil.rmtree(target)
                    shutil.copytree(item, target)
                else:
                    if target.exists():
                        target.unlink()
                    shutil.copy2(item, target)
                copied += 1
            except Exception as e:
                _log(f"  failed to copy {item.name}: {e}")

    new_reqs_hash = _file_hash(PROJECT_DIR / "requirements.txt")
    reqs_changed = old_reqs_hash != new_reqs_hash
    return True, f"copied {copied} items", reqs_changed


def _reinstall_dependencies() -> bool:
    """If requirements.txt changed, reinstall via the project venv."""
    # Cross-platform venv python detection
    candidates = [
        PROJECT_DIR / ".venv" / "Scripts" / "python.exe",  # Windows
        PROJECT_DIR / ".venv" / "bin" / "python",          # macOS / Linux
    ]
    venv_py = next((p for p in candidates if p.exists()), None)
    if venv_py is None:
        _log("no .venv found — skipping pip install (run install.bat first)")
        return False
    try:
        result = subprocess.run(
            [str(venv_py), "-m", "pip", "install", "-r", "requirements.txt", "--quiet"],
            cwd=PROJECT_DIR, timeout=300,
        )
        if result.returncode == 0:
            _log("dependencies reinstalled")
            return True
        _log(f"pip install exited with {result.returncode}")
        return False
    except Exception as e:
        _log(f"pip install failed: {e}")
        return False


# ── main entrypoint ───────────────────────────────────────────────────────────

def check_and_update() -> tuple[bool, str]:
    """
    Public entrypoint. Returns (updated_bool, status_string).
    Never raises — always logs and returns gracefully.
    """
    cfg = _load_config()
    if cfg is None:
        _log("no update_config.json found — skipping")
        return False, "no config"
    if not cfg.get("auto_update", True):
        _log("auto_update disabled in config — skipping")
        return False, "disabled"

    user   = (cfg.get("github_user")   or "").strip()
    repo   = (cfg.get("github_repo")   or "trade-signals").strip()
    branch = (cfg.get("github_branch") or "main").strip()

    if not user or user.startswith("YOUR_") or user == "username":
        _log(f"github_user not configured (got '{user}') — skipping")
        return False, "no user"

    # Check remote
    try:
        remote_sha = _get_remote_sha(user, repo, branch)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            _log(f"repo {user}/{repo} not found on GitHub (404). Check update_config.json.")
        else:
            _log(f"GitHub returned {e.code}: {e}")
        return False, f"http {e.code}"
    except urllib.error.URLError as e:
        _log(f"no internet / GitHub unreachable: {e.reason}")
        return False, "no network"
    except Exception as e:
        _log(f"unexpected error talking to GitHub: {e}")
        return False, str(e)

    local_sha = SHA_FILE.read_text().strip() if SHA_FILE.exists() else None
    if local_sha == remote_sha:
        _log(f"already up to date ({remote_sha[:7]})")
        return False, "up-to-date"

    _log(f"update available: {(local_sha or 'none')[:7]} -> {remote_sha[:7]}, downloading...")

    # Download + apply
    tmp_zip = Path(tempfile.gettempdir()) / "trade-signals-update.zip"
    try:
        _download_zip(user, repo, branch, tmp_zip)
        ok, msg, reqs_changed = _apply_zip(tmp_zip)
        if not ok:
            _log(f"apply failed: {msg}")
            return False, msg
        SHA_FILE.write_text(remote_sha)
        _log(f"updated to {remote_sha[:7]} ({msg})")
        if reqs_changed:
            _log("requirements.txt changed — reinstalling dependencies...")
            _reinstall_dependencies()
        return True, "updated"
    except Exception as e:
        _log(f"download failed: {e}")
        return False, str(e)
    finally:
        try:
            tmp_zip.unlink()
        except Exception:
            pass


if __name__ == "__main__":
    updated, status = check_and_update()
    sys.exit(0)   # never block the launcher even on failure
