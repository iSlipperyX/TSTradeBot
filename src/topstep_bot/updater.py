"""Keep the bot up to date from GitHub: find newer code, describe it, install it, undo it.

The bot follows one branch of its GitHub repository (``updates.branch``, normally ``main``). This
module does the mechanics; update_service.py decides *when* (only with your confirmation, and only
while no trade or order is open) and restarts the bot afterwards.

Two kinds of bot folder are supported:

  * **git** - the folder was cloned with git (it has a ``.git`` folder and git is installed):
    ``git fetch``, then a fast-forward to the new commit. Your git login is used, so no token is needed.
  * **download** - the folder came from a ZIP download: the GitHub API is asked what changed and
    only the files that differ are replaced. The repository is private, so this needs a read-only
    GitHub token (``GITHUB_TOKEN`` in ``.env``; ``topstep-bot update --token`` sets it up).

Every install can be undone. If a new Python package can't be installed, the new code doesn't
start, or it rejects your config.yaml, the previous version is put back automatically (git: the
previous commit; download: the replaced files, kept in ``data/updates/backup_*``).

Your own files are never touched: config.yaml, .env, data/, logs/, reports/ and the Python environment.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import tomllib
import zipfile
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

import httpx

from topstep_bot import __version__

log = logging.getLogger(__name__)

STATE_FILE = "updates.json"
GITHUB_API = "https://api.github.com"
MAX_CHANGES = 30  # commits listed per update
MAX_FILES = 200
KEEP_BACKUPS = 3
# Never written or deleted by an update, even if the repository contained them.
PROTECTED_DIRS = (".git", ".venv", "venv", "data", "logs", "reports", "__pycache__")
PROTECTED_FILES = (".env", "config.yaml", "config.yaml.bak", "KILL")
VERSION_FILE = "src/topstep_bot/__init__.py"
# Run with the new code after an install: it must import and accept your config.yaml.
VERIFY = (
    "import sys\n"
    "import topstep_bot, topstep_bot.cli, topstep_bot.controller, topstep_bot.live, topstep_bot.engine\n"
    "from topstep_bot.config import load_config\n"
    "load_config(sys.argv[1] or None)\n"
    "print(topstep_bot.__version__)\n"
)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # no flashing console windows on Windows


class UpdateError(Exception):
    """An update could not be checked or installed. The message is written for the user."""


# ------------------------------------------------------------------------------ results

@dataclass
class Change:
    sha: str
    title: str
    author: str = ""
    date: str = ""  # ISO time of the commit

    @property
    def short(self) -> str:
        return self.sha[:7]


@dataclass
class UpdateInfo:
    """The result of one check."""

    method: str  # "git" or "download"
    branch: str
    checked_at: str = ""
    current: str | None = None  # commit this folder is on (None: unknown, e.g. a ZIP download never updated)
    current_date: str | None = None
    latest: str | None = None  # newest commit on GitHub
    latest_date: str | None = None
    available: bool = False
    changes: list[Change] = field(default_factory=list)  # newest first
    change_count: int = 0
    exact: bool = True  # False: the installed version is unknown, so ``changes`` are simply the latest ones
    files: list[str] = field(default_factory=list)
    file_count: int = 0
    dependencies: bool = False  # the update needs new Python packages (installed with it)
    version: str = __version__
    new_version: str | None = None
    problem: str | None = None  # why it can't be installed automatically (until you fix it)
    error: str | None = None  # the check itself failed

    def to_dict(self) -> dict:
        d = asdict(self)
        d["changes"] = [{**asdict(c), "short": c.short} for c in self.changes]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> UpdateInfo:
        d = dict(d)
        d["changes"] = [Change(c["sha"], c["title"], c.get("author", ""), c.get("date", "")) for c in d.get("changes", [])]
        known = cls.__dataclass_fields__
        return cls(**{k: v for k, v in d.items() if k in known})

    @property
    def can_install(self) -> bool:
        return self.available and not self.problem and not self.error

    def headline(self) -> str:
        if self.error:
            return f"Could not check for updates: {self.error}"
        if not self.available:
            return f"Topstep Bot is up to date (version {version_label(self.version, self.current)})."
        n = self.change_count
        what = f"{n} change{'s' if n != 1 else ''}" if self.exact else f"{self.file_count} changed file{'s' if self.file_count != 1 else ''}"
        version = f" ({self.version} → {self.new_version})" if self.new_version and self.new_version != self.version else ""
        return f"Update available for Topstep Bot: {what}{version}."


def version_label(version: str, sha: str | None) -> str:
    return f"{version} · {sha[:7]}" if sha else version


def describe(info: UpdateInfo, limit: int = 10) -> str:
    """Plain-text summary for Telegram and alerts."""
    lines = [("⬆️ " if info.available else "✅ " if not info.error else "⚠️ ") + info.headline()]
    if info.available:
        if not info.exact:
            lines.append("This copy was downloaded as a ZIP, so its exact version is unknown. Latest changes on GitHub:")
        for c in info.changes[:limit]:
            lines.append(f"• {c.title} ({c.short}{', ' + c.date[:10] if c.date else ''})")
        total = max(info.change_count, len(info.changes)) if info.exact else len(info.changes)
        if total > limit:
            lines.append(f"• ...and {total - limit} more")
        if info.dependencies:
            lines.append("It also installs new Python packages the new version needs.")
        if info.problem:
            lines.append(f"⚠️ Can't install it yet: {info.problem}")
    return "\n".join(lines)


# ------------------------------------------------------------------------------ helpers

def project_root() -> Path | None:
    """The bot folder (with pyproject.toml), or None when the bot was installed as a regular package."""
    root = Path(__file__).resolve().parents[2]
    return root if (root / "pyproject.toml").is_file() and (root / "src" / "topstep_bot").is_dir() else None


def is_protected(rel: str) -> bool:
    parts = PurePosixPath(rel).parts
    return not parts or parts[0] in PROTECTED_DIRS or rel in PROTECTED_FILES or "__pycache__" in parts


def blob_sha(data: bytes) -> str:
    """The id git gives a file's content, so a local file can be compared with GitHub without downloading it."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def same_content(path: Path, sha: str) -> bool:
    try:
        data = path.read_bytes()
    except OSError:
        return False
    # Windows editors (and git's autocrlf) may have turned LF into CRLF: that is not a code change.
    return blob_sha(data) == sha or (b"\r\n" in data and blob_sha(data.replace(b"\r\n", b"\n")) == sha)


def dependencies_of(pyproject: str | None) -> list[str]:
    if not pyproject:
        return []
    try:
        return list(tomllib.loads(pyproject).get("project", {}).get("dependencies", []))
    except tomllib.TOMLDecodeError:
        return []


def version_of(init_py: str | None) -> str | None:
    m = re.search(r"""__version__\s*=\s*["']([^"']+)["']""", init_py or "")
    return m.group(1) if m else None


def commit_title(message: str) -> str:
    """A commit's one-line title; for a merged pull request, the pull request's title."""
    lines = [line.strip() for line in (message or "").strip().splitlines()]
    title = lines[0] if lines else ""
    m = re.match(r"Merge pull request (#\d+)", title)
    if m:
        body = next((line for line in lines[1:] if line), "")
        return f"{body} ({m.group(1)})" if body else title
    return title


def first_parent_chain(commits: list[dict], head: str) -> list[dict]:
    """Newest first, following first parents from ``head``: the merges and direct commits on the branch itself
    (what was merged, one line per pull request), not every commit inside each pull request."""
    by_sha = {c["sha"]: c for c in commits}
    chain, sha = [], head
    while sha in by_sha:
        chain.append(by_sha[sha])
        parents = by_sha[sha].get("parents") or []
        sha = parents[0]["sha"] if parents else None
    return chain


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _tail(text: str, lines: int = 6) -> str:
    return " ".join((text or "").strip().splitlines()[-lines:])


def load_state(state_dir: Path) -> dict:
    try:
        return json.loads((Path(state_dir) / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state_dir: Path, state: dict) -> None:
    path = Path(state_dir) / STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def cached_info(state_dir: Path) -> UpdateInfo | None:
    """The last check's result (no network) - for the menu's 'update available' line."""
    data = load_state(state_dir).get("check")
    try:
        return UpdateInfo.from_dict(data) if data else None
    except (TypeError, KeyError):
        return None


# ------------------------------------------------------------------------------ updater

class Updater:
    def __init__(
        self,
        root: Path,
        repo: str,
        branch: str,
        state_dir: Path,
        *,
        token: str | None = None,
        method: str | None = None,
        transport: httpx.BaseTransport | None = None,
        python: str = sys.executable,
        verify: Callable[[str | None], None] | None = None,
        api: str = GITHUB_API,
    ):
        self.root = Path(root)
        self.repo = repo
        self.branch = branch
        self.state_dir = Path(state_dir)
        self.token = token
        self.method = method or ("git" if (self.root / ".git").exists() and shutil.which("git") else "download")
        self.python = python
        self._verify = verify
        self._mutex = threading.RLock()  # one git/GitHub operation at a time (a manual check can meet the automatic one)
        self.api = api
        self.transport = transport
        self._client: httpx.Client | None = None  # opened when needed, closed after each check/install

    @classmethod
    def for_config(cls, cfg: Any, token: str | None = None, **kw: Any) -> Updater | None:
        root = project_root()
        if root is None:
            return None
        return cls(root, cfg.updates.repo, cfg.updates.branch, Path(cfg.data_dir), token=token, **kw)

    @property
    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                base_url=self.api, timeout=30, follow_redirects=True, transport=self.transport,
                headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                         "User-Agent": f"topstep-bot/{__version__}",
                         **({"Authorization": f"Bearer {self.token}"} if self.token else {})},
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # ---------------------------------------------------------------- state

    def state(self) -> dict:
        return load_state(self.state_dir)

    def _save(self, **changes: Any) -> dict:
        state = self.state()
        state.update(changes)
        save_state(self.state_dir, state)
        return state

    # ---------------------------------------------------------------- check

    def check(self) -> UpdateInfo:
        """Ask GitHub whether there is newer code. Never raises: problems end up in ``info.error``."""
        with self._mutex:
            try:
                return self._check()
            finally:
                self.close()

    def _check(self) -> UpdateInfo:
        info = UpdateInfo(method=self.method, branch=self.branch, checked_at=_now(),
                          version=version_of(self._read(VERSION_FILE)) or __version__)
        try:
            if self.method == "git":
                self._check_git(info)
            else:
                self._check_download(info)
        except UpdateError as exc:
            info.error = str(exc)
        except (httpx.HTTPError, OSError, subprocess.SubprocessError, ValueError, KeyError) as exc:
            log.warning("Update check failed", exc_info=True)
            info.error = f"{type(exc).__name__}: {exc}"
        info.change_count = max(info.change_count, len(info.changes))
        info.file_count = max(info.file_count, len(info.files))
        self._save(check=info.to_dict())
        return info

    # git ---------------------------------------------------------------

    def _git(self, *args: str, timeout: float = 60, check: bool = True) -> str:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "LC_ALL": "C"}
        try:
            r = subprocess.run(["git", *args], cwd=self.root, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=timeout, env=env, creationflags=NO_WINDOW)
        except subprocess.TimeoutExpired:
            raise UpdateError(f"git {args[0]} took too long - check the internet connection") from None
        if check and r.returncode:
            raise UpdateError(f"git {args[0]} failed: {_tail(r.stderr or r.stdout, 3)}")
        return r.stdout.strip()

    def _check_git(self, info: UpdateInfo) -> None:
        if not self._git("remote", check=False):
            raise UpdateError("this git folder has no 'origin' remote to update from")
        try:
            self._git("fetch", "--quiet", "--no-tags", "origin", self.branch, timeout=90)
        except UpdateError as exc:
            text = str(exc).lower()
            if "couldn't find remote ref" in text:
                raise UpdateError(f"GitHub has no '{self.branch}' branch (updates.branch in config.yaml)") from None
            if any(s in text for s in ("authentication", "could not read username", "permission denied", "not found",
                                       "terminal prompts disabled")):
                raise UpdateError("git could not sign in to GitHub. Run 'git pull' once in the bot folder to sign in, "
                                  "then try again") from None
            raise
        head, latest = self._git("rev-parse", "HEAD"), self._git("rev-parse", "FETCH_HEAD")
        info.current, info.latest = head, latest
        info.current_date = self._git("log", "-1", "--format=%cI", head)
        info.latest_date = self._git("log", "-1", "--format=%cI", latest)
        if not int(self._git("rev-list", "--count", f"{head}..{latest}")):
            return
        info.available = True
        # --first-parent: one line per merged pull request (with its title), not every commit inside it
        info.change_count = int(self._git("rev-list", "--count", "--first-parent", f"{head}..{latest}"))
        out = self._git("log", f"-{MAX_CHANGES}", "--first-parent", "--format=%H%x1f%an%x1f%cI%x1f%B%x1e",
                        f"{head}..{latest}")
        for record in out.split("\x1e"):
            fields = record.strip().split("\x1f")
            if len(fields) == 4:
                info.changes.append(Change(fields[0], commit_title(fields[3]), fields[1], fields[2]))
        files = self._git("diff", "--name-only", f"{head}...{latest}").splitlines()
        info.files, info.file_count = files[:MAX_FILES], len(files)
        if "pyproject.toml" in files:
            info.dependencies = (dependencies_of(self._git("show", f"{head}:pyproject.toml", check=False))
                                 != dependencies_of(self._git("show", f"{latest}:pyproject.toml", check=False)))
        info.new_version = version_of(self._git("show", f"{latest}:{VERSION_FILE}", check=False))
        info.problem = self._git_problem(head, latest)

    def _git_problem(self, head: str, latest: str) -> str | None:
        branch = self._git("rev-parse", "--abbrev-ref", "HEAD")
        if branch != self.branch:
            return (f"this folder is on the '{branch}' branch, but updates follow '{self.branch}'. Switch with "
                    f"'git checkout {self.branch}', or set updates.branch: {branch} in config.yaml")
        ahead = int(self._git("rev-list", "--count", f"{latest}..{head}"))
        if ahead:
            return f"this folder has {ahead} commit(s) that are not on GitHub. Push or remove them first"
        changed = [line[3:] for line in self._git("status", "--porcelain", "--untracked-files=no").splitlines() if line]
        if changed:
            shown = ", ".join(changed[:5]) + (" ..." if len(changed) > 5 else "")
            return (f"bot files were edited on this PC ({shown}). Undo those edits (or commit them) first - "
                    "config.yaml and your data are not affected")
        return None

    # download ----------------------------------------------------------

    def _get(self, path: str, **params: Any) -> Any:
        try:
            r = self._http.get(path, params=params or None)
        except httpx.HTTPError as exc:
            raise UpdateError(f"could not reach GitHub ({type(exc).__name__}) - check the internet connection") from None
        if r.status_code == 401:
            raise UpdateError("GitHub rejected the token (GITHUB_TOKEN in .env) - it may have expired. "
                              "Make a new one with: topstep-bot update --token")
        if r.status_code == 403 and r.headers.get("x-ratelimit-remaining") == "0":
            raise UpdateError("GitHub's hourly limit for update checks was reached - it is tried again later")
        if r.status_code in (403, 404):
            if not self.token:
                raise UpdateError(f"GitHub did not show the repository {self.repo}. It is private, so the bot needs a "
                                  "read-only GitHub token: run 'topstep-bot update --token' (menu: update) on the PC")
            raise UpdateError(f"the GitHub token can't read {self.repo} (or '{self.branch}' doesn't exist). Give the "
                              "token read access to the repository's Contents: topstep-bot update --token")
        if r.status_code >= 400:
            raise UpdateError(f"GitHub answered {r.status_code}: {r.text[:120]}")
        return r.json() if "json" in r.headers.get("content-type", "") else r.content

    def _file_at(self, sha: str, path: str) -> str | None:
        try:
            data = self._get(f"/repos/{self.repo}/contents/{path}", ref=sha)
        except UpdateError:
            return None
        return base64.b64decode(data.get("content", "")).decode("utf-8", "replace") if isinstance(data, dict) else None

    def _tree(self, sha: str) -> dict[str, str]:
        """path -> blob sha of every file in a commit."""
        data = self._get(f"/repos/{self.repo}/git/trees/{sha}", recursive="1")
        if data.get("truncated"):
            raise UpdateError("GitHub returned an incomplete file list")
        return {e["path"]: e["sha"] for e in data.get("tree", []) if e.get("type") == "blob"}

    @staticmethod
    def _change(c: dict) -> Change:
        commit = c.get("commit", {})
        return Change(c["sha"], commit_title(commit.get("message", "")), (commit.get("author") or {}).get("name", ""),
                      (commit.get("committer") or {}).get("date", ""))

    def _check_download(self, info: UpdateInfo) -> None:
        head = self._get(f"/repos/{self.repo}/commits/{self.branch}")
        info.latest = latest = head["sha"]
        info.latest_date = (head["commit"].get("committer") or {}).get("date")
        installed = self.state().get("installed")
        if installed == latest:
            info.current, info.current_date = latest, info.latest_date
            return
        if installed:
            try:
                cmp = self._get(f"/repos/{self.repo}/compare/{installed}...{latest}")
            except UpdateError:
                cmp = None  # the installed commit is gone from GitHub (branch rewritten): compare files instead
            if cmp and cmp.get("status") == "ahead":
                info.current = installed
                info.current_date = (cmp.get("base_commit", {}).get("commit", {}).get("committer") or {}).get("date")
                info.available = True
                chain = first_parent_chain(cmp.get("commits", []), latest)
                info.change_count = len(chain)
                info.changes = [self._change(c) for c in chain][:MAX_CHANGES]
                files = [f["filename"] for f in cmp.get("files", [])]
                info.files, info.file_count = files[:MAX_FILES], len(files)
                self._finish_download_check(info, files)
                return
        # Unknown version (a ZIP download): compare every file with the newest commit.
        tree = self._tree(latest)
        differ = [p for p, sha in sorted(tree.items()) if not is_protected(p) and not same_content(self.root / p, sha)]
        if not differ:
            self._save(installed=latest, installed_at=_now())  # this copy *is* the latest version
            info.current, info.current_date = latest, info.latest_date
            return
        info.available, info.exact = True, False
        info.files, info.file_count = differ[:MAX_FILES], len(differ)
        recent = self._get(f"/repos/{self.repo}/commits", sha=self.branch, per_page=40)
        info.changes = [self._change(c) for c in first_parent_chain(recent, latest)][:10]
        info.change_count = len(info.changes)
        self._finish_download_check(info, differ)

    def _finish_download_check(self, info: UpdateInfo, files: list[str]) -> None:
        if "pyproject.toml" in files:
            local = (self.root / "pyproject.toml").read_text(encoding="utf-8") if (self.root / "pyproject.toml").exists() else None
            info.dependencies = dependencies_of(local) != dependencies_of(self._file_at(info.latest, "pyproject.toml"))
        info.new_version = version_of(self._file_at(info.latest, VERSION_FILE)) if VERSION_FILE in files else info.version

    # ---------------------------------------------------------------- install

    def install(self, info: UpdateInfo, config_path: str | None = None, progress: Callable[[str], None] | None = None) -> dict:
        """Install the version ``info`` found. On any failure the previous version is restored and
        UpdateError explains what happened. Returns the install record."""
        with self._mutex:
            try:
                return self._install(info, config_path, progress)
            finally:
                self.close()

    def _install(self, info: UpdateInfo, config_path: str | None, progress: Callable[[str], None] | None) -> dict:
        say = progress or (lambda _: None)
        if not info.available or not info.latest:
            raise UpdateError("Already up to date - nothing to install.")
        if info.problem:
            raise UpdateError(f"Can't install: {info.problem}.")
        old_pyproject = self._read("pyproject.toml")
        say("Downloading the new version...")
        try:
            record = self._apply_git(info) if self.method == "git" else self._apply_download(info)
        except OSError as exc:  # e.g. the disk is full while backing up: nothing was replaced yet
            raise UpdateError(f"the update could not be prepared ({exc})") from None
        try:
            if dependencies_of(old_pyproject) != dependencies_of(self._read("pyproject.toml")):
                say("Installing new Python packages...")
                self._install_dependencies()
            say("Checking that the new version starts...")
            self.verify(config_path)
        except Exception as exc:
            log.error("Update failed after it was applied - restoring the previous version: %s", exc)
            self.rollback(record)
            raise UpdateError(f"{exc}. Nothing changed: the previous version was restored.") from None
        record.update(status="installed", at=_now())
        self._save(last_install=record, check=None)  # the last check described the old version
        log.info("Update installed: %s -> %s (%s)", (record.get("from") or "unknown")[:7], record["to"][:7], self.method)
        return record

    def _read(self, rel: str) -> str | None:
        try:
            return (self.root / rel).read_text(encoding="utf-8")
        except OSError:
            return None

    def _install_dependencies(self) -> None:
        deps = dependencies_of(self._read("pyproject.toml"))
        try:
            r = subprocess.run([self.python, "-m", "pip", "install", "--disable-pip-version-check", "--quiet", *deps],
                               capture_output=True, text=True, timeout=900, creationflags=NO_WINDOW)
        except subprocess.TimeoutExpired:
            raise UpdateError("installing the new Python packages took too long") from None
        if r.returncode:
            raise UpdateError(f"the new Python packages could not be installed ({_tail(r.stderr or r.stdout, 3)})")

    def verify(self, config_path: str | None = None) -> None:
        """Start the new code in a fresh Python: it must import and accept config.yaml."""
        if self._verify is not None:
            self._verify(config_path)
            return
        env = {**os.environ, "PYTHONPATH": str(self.root / "src") + os.pathsep + os.environ.get("PYTHONPATH", "")}
        try:
            r = subprocess.run([self.python, "-c", VERIFY, config_path or ""], capture_output=True, text=True,
                               timeout=180, env=env, creationflags=NO_WINDOW)
        except subprocess.TimeoutExpired:
            raise UpdateError("the new version did not start in time") from None
        if r.returncode:
            raise UpdateError(f"the new version did not start ({_tail(r.stderr or r.stdout, 4)})")

    # git ---------------------------------------------------------------

    def _apply_git(self, info: UpdateInfo) -> dict:
        old = self._git("rev-parse", "HEAD")
        record = {"method": "git", "from": old, "to": info.latest, "status": "applying", "started": _now()}
        self._save(last_install=record)
        try:
            self._git("merge", "--ff-only", "--quiet", info.latest, timeout=120)
        except UpdateError as exc:
            self._save(last_install={**record, "status": "failed"})
            raise UpdateError(f"the new version could not be applied ({exc})") from None
        return record

    # download ----------------------------------------------------------

    def _apply_download(self, info: UpdateInfo) -> dict:
        latest = info.latest
        try:
            r = self._http.get(f"/repos/{self.repo}/zipball/{latest}", timeout=120)
        except httpx.HTTPError as exc:
            raise UpdateError(f"the download failed ({type(exc).__name__}) - check the internet connection") from None
        if r.status_code >= 400:
            raise UpdateError(f"GitHub refused the download ({r.status_code})")
        new = self._unzip(r.content)
        installed = self.state().get("installed")
        old_paths: set[str] = set()
        if installed:
            try:
                old_paths = set(self._tree(installed))
            except UpdateError:
                old_paths = set()  # unknown: leave files the new version no longer has in place (harmless)
        write = {p: data for p, data in new.items() if self._differs(p, data)}
        delete = sorted(p for p in old_paths - set(new) if not is_protected(p) and (self.root / p).is_file())
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = self.state_dir / "updates" / f"backup_{stamp}_{latest[:7]}"
        record = {"method": "download", "from": installed, "to": latest, "status": "applying", "started": _now(),
                  "backup": str(backup), "created": [], "replaced": [], "deleted": []}
        for rel in sorted(set(write) | set(delete)):
            src = self.root / rel
            if src.is_file():
                dst = backup / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                record["deleted" if rel in delete else "replaced"].append(rel)
            else:
                record["created"].append(rel)
        self._save(last_install=record)  # saved before anything changes, so even a crash can be undone
        try:
            for rel, data in write.items():
                dst = self.root / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                tmp = dst.with_name(dst.name + ".tsb-new")
                tmp.write_bytes(data)
                os.replace(tmp, dst)
            for rel in delete:
                (self.root / rel).unlink(missing_ok=True)
        except OSError as exc:
            for rel in write:  # a half-written temporary file
                with contextlib.suppress(OSError):
                    (self.root / rel).with_name(PurePosixPath(rel).name + ".tsb-new").unlink(missing_ok=True)
            self.rollback(record)
            raise UpdateError(f"a file could not be replaced ({exc}) - is it open in another program? "
                              "Nothing changed: the previous version was restored") from None
        self._save(installed=latest, installed_at=_now())
        self._prune_backups()
        return record

    def _differs(self, rel: str, data: bytes) -> bool:
        try:
            current = (self.root / rel).read_bytes()
        except OSError:
            return True
        return current != data and current.replace(b"\r\n", b"\n") != data

    @staticmethod
    def _unzip(content: bytes) -> dict[str, bytes]:
        """GitHub's zipball: one top folder (owner-repo-sha/) holding the repository's files."""
        try:
            zf = zipfile.ZipFile(io.BytesIO(content))
        except zipfile.BadZipFile:
            raise UpdateError("the downloaded update is damaged - try again") from None
        files: dict[str, bytes] = {}
        for item in zf.infolist():
            if item.is_dir():
                continue
            parts = PurePosixPath(item.filename).parts[1:]
            if not parts or any(p in ("..", "") or ":" in p for p in parts) or item.filename.startswith("/"):
                raise UpdateError("the downloaded update contains an unsafe file name - not installed")
            rel = "/".join(parts)
            if not is_protected(rel):
                files[rel] = zf.read(item)
        if "pyproject.toml" not in files or VERSION_FILE not in files:
            raise UpdateError("the download does not look like the Topstep Bot - not installed")
        return files

    def _prune_backups(self) -> None:
        folder = self.state_dir / "updates"
        backups = sorted(p for p in folder.glob("backup_*") if p.is_dir())
        for old in backups[:-KEEP_BACKUPS]:
            shutil.rmtree(old, ignore_errors=True)

    # ---------------------------------------------------------------- undo

    def can_roll_back(self) -> bool:
        rec = self.state().get("last_install") or {}
        return rec.get("status") in ("installed", "applying") and bool(rec.get("from") or rec.get("method") == "download")

    def rollback(self, record: dict | None = None) -> dict:
        """Put back the version from before the last install."""
        with self._mutex:
            return self._rollback(record)

    def _rollback(self, record: dict | None) -> dict:
        rec = dict(record or self.state().get("last_install") or {})
        if not rec:
            raise UpdateError("There is no update to undo.")
        if rec["method"] == "git":
            if not rec.get("from"):
                raise UpdateError("The previous version is unknown - it can't be restored automatically.")
            self._git("reset", "--keep", "--quiet", rec["from"], timeout=120)
        else:
            backup = Path(rec["backup"])
            for rel in rec.get("created", []):
                (self.root / rel).unlink(missing_ok=True)
                for folder in (self.root / rel).parents:  # and the folders the update made for them
                    if folder == self.root or not folder.is_dir() or any(folder.iterdir()):
                        break
                    folder.rmdir()
            for rel in [*rec.get("replaced", []), *rec.get("deleted", [])]:
                src, dst = backup / rel, self.root / rel
                if src.is_file():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
            state = self.state()
            if rec.get("from"):
                state["installed"] = rec["from"]
            else:
                state.pop("installed", None)
            save_state(self.state_dir, state)
        rec.update(status="rolled_back", rolled_back_at=_now())
        self._save(last_install=rec)
        log.warning("Update undone: back to %s", (rec.get("from") or "the previous files")[:7])
        return rec

