"""Updates from GitHub: checking and installing in a git folder (real git) and in a downloaded ZIP
folder (a fake GitHub API), with automatic undo when anything goes wrong."""

import base64
import io
import json
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

import httpx
import pytest

from topstep_bot import updater as updater_mod
from topstep_bot.updater import UpdateError, Updater, blob_sha, commit_title, describe, load_state, save_state

REPO = "owner/bot"
PYPROJECT = '[project]\nname = "topstep-bot"\ndependencies = ["httpx>=0.27"]\n'
NEW_DEPS = '[project]\nname = "topstep-bot"\ndependencies = ["httpx>=0.27", "numpy>=2"]\n'
V1 = {
    "pyproject.toml": PYPROJECT,
    "src/topstep_bot/__init__.py": '__version__ = "1.0.0"\n',
    "src/topstep_bot/engine.py": "ENGINE = 1\n",
    "src/topstep_bot/old.py": "OLD = 1\n",
    "README.md": "# Bot\n",
}
# Files the user owns: an update must never touch them.
USER_FILES = {"config.yaml": "mode: live\n", ".env": "TOPSTEPX_API_KEY=secret\n", "data/journal_live.db": "trades",
              "logs/bot.log": "log"}


def write_tree(root: Path, files: dict) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text if isinstance(text, bytes) else text.encode())


def read_tree(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): p.read_text() for p in sorted(root.rglob("*"))
            if p.is_file() and ".git" not in p.parts and "updates" not in p.parts and p.name != "updates.json"}


# ------------------------------------------------------------------------------ git folders

def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=Dev", "-c", "user.email=dev@example.com", "-c", "init.defaultBranch=main",
                           *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


class GitWorld:
    """A GitHub stand-in (bare repo), a developer clone that pushes changes, and the bot's own clone."""

    def __init__(self, tmp: Path):
        self.origin, self.dev, self.bot = tmp / "origin.git", tmp / "dev", tmp / "bot"
        git(tmp, "init", "--bare", "-b", "main", str(self.origin))
        git(tmp, "clone", "-q", str(self.origin), str(self.dev))
        git(self.dev, "checkout", "-q", "-b", "main")
        self.commit(V1, "Initial version")
        git(tmp, "clone", "-q", str(self.origin), str(self.bot))
        write_tree(self.bot, USER_FILES)  # ignored by git, like on a real PC
        (self.bot / ".git" / "info" / "exclude").write_text("config.yaml\n.env\ndata/\nlogs/\n")
        self.verified: list = []
        self.updater = Updater(self.bot, REPO, "main", self.bot / "data", method="git", verify=self.verified.append)

    def commit(self, files: dict, message: str, delete: tuple = ()) -> str:
        write_tree(self.dev, files)
        for rel in delete:
            (self.dev / rel).unlink()
        git(self.dev, "add", "-A")
        git(self.dev, "commit", "-q", "-m", message)
        git(self.dev, "push", "-q", "origin", "main")
        return git(self.dev, "rev-parse", "HEAD")

    def merge_pr(self, number: int, title: str, files: dict) -> str:
        git(self.dev, "checkout", "-q", "-b", f"pr{number}")
        write_tree(self.dev, files)
        git(self.dev, "add", "-A")
        git(self.dev, "commit", "-q", "-m", "work in progress")
        git(self.dev, "commit", "-q", "--allow-empty", "-m", "more work")
        git(self.dev, "checkout", "-q", "main")
        git(self.dev, "merge", "-q", "--no-ff", f"pr{number}", "-m", f"Merge pull request #{number} from me/pr{number}\n\n{title}")
        git(self.dev, "push", "-q", "origin", "main")
        return git(self.dev, "rev-parse", "HEAD")


@pytest.fixture
def world(tmp_path):
    w = GitWorld(tmp_path)
    yield w
    w.updater.close()


@needs_git
def test_git_folder_up_to_date_then_update_found_with_readable_changes(world):
    info = world.updater.check()
    assert info.error is None and not info.available and info.current == git(world.bot, "rev-parse", "HEAD")
    world.commit({"src/topstep_bot/engine.py": "ENGINE = 2\n", "src/topstep_bot/__init__.py": '__version__ = "1.1.0"\n'},
                 "Faster engine")
    world.merge_pr(5, "Dashboard: market clock", {"src/topstep_bot/clock.py": "CLOCK = 1\n"})
    info = world.updater.check()
    assert info.available and info.can_install and info.method == "git"
    # one line per pull request, titled like the pull request - not "Merge pull request #5 from ..."
    assert [c.title for c in info.changes] == ["Dashboard: market clock (#5)", "Faster engine"]
    assert info.change_count == 2 and info.new_version == "1.1.0" and not info.dependencies
    assert set(info.files) == {"src/topstep_bot/engine.py", "src/topstep_bot/__init__.py", "src/topstep_bot/clock.py"}
    text = describe(info)
    assert "2 changes (1.0.0 → 1.1.0)" in text and "• Dashboard: market clock (#5)" in text
    assert load_state(world.bot / "data")["check"]["latest"] == info.latest  # remembered for the menu


@needs_git
def test_git_install_fast_forwards_verifies_and_keeps_user_files(world):
    old = git(world.bot, "rev-parse", "HEAD")
    new = world.commit({"src/topstep_bot/engine.py": "ENGINE = 2\n"}, "Faster engine", delete=("src/topstep_bot/old.py",))
    info = world.updater.check()
    record = world.updater.install(info, "config.yaml")
    assert record["from"] == old and record["to"] == new and record["status"] == "installed"
    assert git(world.bot, "rev-parse", "HEAD") == new and world.verified == ["config.yaml"]
    assert (world.bot / "src/topstep_bot/engine.py").read_text() == "ENGINE = 2\n"
    assert not (world.bot / "src/topstep_bot/old.py").exists()
    assert all((world.bot / rel).read_text() == text for rel, text in USER_FILES.items())
    assert not world.updater.check().available
    world.updater.rollback()  # "update --undo"
    assert git(world.bot, "rev-parse", "HEAD") == old and (world.bot / "src/topstep_bot/old.py").exists()


@needs_git
def test_git_install_that_does_not_start_is_undone(world):
    old = git(world.bot, "rev-parse", "HEAD")
    world.commit({"src/topstep_bot/engine.py": "syntax error(\n"}, "Broken change")

    def broken(_config):
        raise UpdateError("the new version did not start (SyntaxError)")
    world.updater._verify = broken
    with pytest.raises(UpdateError, match="previous version was restored"):
        world.updater.install(world.updater.check())
    assert git(world.bot, "rev-parse", "HEAD") == old
    assert (world.bot / "src/topstep_bot/engine.py").read_text() == "ENGINE = 1\n"
    assert load_state(world.bot / "data")["last_install"]["status"] == "rolled_back"


@needs_git
def test_new_python_packages_are_installed_with_the_update(world, monkeypatch):
    calls, real_run = [], subprocess.run

    def run(cmd, **kw):
        if cmd[1:3] != ["-m", "pip"]:
            return real_run(cmd, **kw)
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(updater_mod.subprocess, "run", run)
    world.commit({"pyproject.toml": NEW_DEPS}, "Use numpy")
    info = world.updater.check()
    assert info.dependencies and "new Python packages" in describe(info)
    world.updater.install(info)
    assert calls and calls[0][1:4] == ["-m", "pip", "install"] and "numpy>=2" in calls[0]


@needs_git
@pytest.mark.parametrize("change,problem", [
    ("edit", "edited on this PC"), ("branch", "on the 'experiment' branch"), ("commit", "not on GitHub"),
])
def test_git_folder_that_cannot_fast_forward_says_why_and_is_left_alone(world, change, problem):
    world.commit({"README.md": "# Bot v2\n"}, "New readme")
    if change == "edit":
        (world.bot / "src/topstep_bot/engine.py").write_text("ENGINE = 'mine'\n")
    elif change == "branch":
        git(world.bot, "checkout", "-q", "-b", "experiment")
    else:
        (world.bot / "src/topstep_bot/engine.py").write_text("ENGINE = 'mine'\n")
        git(world.bot, "commit", "-q", "-am", "my own change")
    head = git(world.bot, "rev-parse", "HEAD")
    info = world.updater.check()
    assert info.available and not info.can_install and problem in info.problem
    with pytest.raises(UpdateError, match="Can't install"):
        world.updater.install(info)
    assert git(world.bot, "rev-parse", "HEAD") == head


@needs_git
def test_git_check_failure_is_reported_not_raised(world):
    git(world.bot, "remote", "set-url", "origin", str(world.bot.parent / "missing.git"))
    info = world.updater.check()
    assert info.error and not info.available and "Could not check" in info.headline()


# ------------------------------------------------------------------------------ downloaded (ZIP) folders

class FakeGitHub:
    """Enough of the GitHub REST API for update checks: commits, compare, trees, contents and zipballs."""

    def __init__(self, private: bool = True, token: str = "good-token"):
        self.private, self.token = private, token
        self.commits: dict[str, dict] = {}
        self.order: list[str] = []  # oldest first
        self.requests: list[str] = []

    def push(self, files: dict, message: str, parents: list[str] | None = None) -> str:
        parents = parents if parents is not None else self.order[-1:]
        base = dict(self.commits[parents[0]]["files"]) if parents else {}
        base.update({k: (v if isinstance(v, bytes) else v.encode()) for k, v in files.items() if v is not None})
        for k, v in files.items():
            if v is None:
                base.pop(k, None)
        sha = f"{len(self.order) + 1:02d}" + blob_sha(repr(sorted(base.items())).encode() + message.encode())[2:]
        self.commits[sha] = {"files": base, "message": message, "parents": parents}
        self.order.append(sha)
        return sha

    def _commit_json(self, sha: str) -> dict:
        c = self.commits[sha]
        return {"sha": sha, "parents": [{"sha": p} for p in c["parents"]],
                "commit": {"message": c["message"], "author": {"name": "Dev"}, "committer": {"date": f"2026-10-{len(self.order):02d}T12:00:00Z"}}}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, q = request.url.path, request.url.params
        self.requests.append(path)
        auth = request.headers.get("authorization")
        if auth and auth != f"Bearer {self.token}":
            return httpx.Response(401, json={"message": "Bad credentials"})
        if self.private and not auth:
            return httpx.Response(404, json={"message": "Not Found"})
        prefix = f"/repos/{REPO}"
        if path == f"{prefix}/commits/main":
            return httpx.Response(200, json=self._commit_json(self.order[-1]))
        if path == f"{prefix}/commits":
            return httpx.Response(200, json=[self._commit_json(s) for s in reversed(self.order)][: int(q.get("per_page", 30))])
        if m := re.fullmatch(rf"{prefix}/compare/(\w+)\.\.\.(\w+)", path):
            base, head = m.groups()
            if base not in self.commits:
                return httpx.Response(404, json={"message": "Not Found"})
            between = self.order[self.order.index(base) + 1: self.order.index(head) + 1]
            old, new = self.commits[base]["files"], self.commits[head]["files"]
            changed = sorted(p for p in set(old) | set(new) if old.get(p) != new.get(p))
            return httpx.Response(200, json={"status": "ahead", "ahead_by": len(between), "base_commit": self._commit_json(base),
                                             "commits": [self._commit_json(s) for s in between],
                                             "files": [{"filename": p} for p in changed]})
        if m := re.fullmatch(rf"{prefix}/git/trees/(\w+)", path):
            files = self.commits[m.group(1)]["files"]
            return httpx.Response(200, json={"truncated": False, "tree": [{"path": p, "type": "blob", "sha": blob_sha(d)}
                                                                          for p, d in files.items()]})
        if m := re.fullmatch(rf"{prefix}/contents/(.+)", path):
            data = self.commits[q["ref"]]["files"].get(m.group(1))
            if data is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"content": base64.b64encode(data).decode()})
        if m := re.fullmatch(rf"{prefix}/zipball/(\w+)", path):
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                for p, d in self.commits[m.group(1)]["files"].items():
                    zf.writestr(f"owner-bot-{m.group(1)[:7]}/{p}", d)
            return httpx.Response(200, content=buf.getvalue(), headers={"content-type": "application/zip"})
        return httpx.Response(404, json={"message": "Not Found"})


def downloaded(tmp_path, gh: FakeGitHub, files: dict, token: str | None = "good-token", installed: str | None = None):
    root = tmp_path / "bot"
    write_tree(root, {**files, **USER_FILES})
    if installed:
        save_state(root / "data", {"installed": installed})
    verified: list = []
    up = Updater(root, REPO, "main", root / "data", token=token, method="download",
                 transport=httpx.MockTransport(gh.handler), verify=verified.append)
    return root, up, verified


def test_zip_copy_of_unknown_version_is_compared_file_by_file_and_updated(tmp_path):
    gh = FakeGitHub()
    gh.push(V1, "Initial version")
    v2 = gh.push({"src/topstep_bot/engine.py": "ENGINE = 2\n", "src/topstep_bot/new.py": "NEW = 1\n"}, "Faster engine")
    root, up, verified = downloaded(tmp_path, gh, V1)
    info = up.check()
    assert info.available and not info.exact and info.current is None and info.latest == v2
    assert info.files == ["src/topstep_bot/engine.py", "src/topstep_bot/new.py"]  # only what differs
    assert "exact version is unknown" in describe(info) and "2 changed files" in info.headline()
    record = up.install(info, "config.yaml")
    assert verified == ["config.yaml"] and record["created"] == ["src/topstep_bot/new.py"]
    assert record["replaced"] == ["src/topstep_bot/engine.py"]
    tree = read_tree(root)
    assert tree["src/topstep_bot/engine.py"] == "ENGINE = 2\n" and tree["src/topstep_bot/new.py"] == "NEW = 1\n"
    assert all(tree[rel] == text for rel, text in USER_FILES.items())
    assert load_state(root / "data")["installed"] == v2
    assert not up.check().available


def test_zip_copy_identical_to_latest_is_recognised_even_with_windows_line_endings(tmp_path):
    gh = FakeGitHub()
    v1 = gh.push(V1, "Initial version")
    root, up, _ = downloaded(tmp_path, gh, {**V1, "README.md": b"# Bot\r\n"})
    info = up.check()
    assert not info.available and info.error is None
    assert load_state(root / "data")["installed"] == v1  # from now on, exact changes can be listed


def test_known_version_lists_pull_requests_deletes_removed_files_and_can_be_undone(tmp_path):
    gh = FakeGitHub()
    v1 = gh.push(V1, "Initial version")
    inner = gh.push({"src/topstep_bot/engine.py": "ENGINE = 2\n"}, "work in progress")
    gh.push({"src/topstep_bot/old.py": None, "docs/GUIDE.md": "guide\n"}, "Merge pull request #4 from me/x\n\nManual trade ticket",
            parents=[v1, inner])
    root, up, _ = downloaded(tmp_path, gh, V1, installed=v1)
    before = read_tree(root)
    info = up.check()
    assert info.exact and [c.title for c in info.changes] == ["Manual trade ticket (#4)"] and info.change_count == 1
    record = up.install(info)
    assert record["deleted"] == ["src/topstep_bot/old.py"] and record["created"] == ["docs/GUIDE.md"]
    assert not (root / "src/topstep_bot/old.py").exists()
    assert Path(record["backup"], "src/topstep_bot/old.py").read_text() == "OLD = 1\n"
    up.rollback()
    assert read_tree(root) == before and not (root / "docs").exists()  # empty folders it made are gone too
    assert load_state(root / "data")["installed"] == v1


def test_zip_install_that_does_not_start_restores_every_file(tmp_path):
    gh = FakeGitHub()
    v1 = gh.push(V1, "Initial version")
    gh.push({"src/topstep_bot/engine.py": "broken(\n", "src/topstep_bot/old.py": None, "src/topstep_bot/x.py": "X\n"}, "Oops")
    root, up, _ = downloaded(tmp_path, gh, V1, installed=v1)
    before = read_tree(root)

    def broken(_config):
        raise UpdateError("the new version did not start (SyntaxError)")
    up._verify = broken
    with pytest.raises(UpdateError, match="did not start.*previous version was restored"):
        up.install(up.check())
    assert read_tree(root) == before and load_state(root / "data")["installed"] == v1


def test_private_repository_needs_a_working_token(tmp_path):
    gh = FakeGitHub()
    gh.push(V1, "Initial version")
    _, up, _ = downloaded(tmp_path, gh, V1, token=None)
    info = up.check()
    assert info.error and "update --token" in info.error and "private" in info.error
    _, up, _ = downloaded(tmp_path, gh, V1, token="expired")
    assert "rejected the token" in up.check().error


def test_download_with_an_unsafe_file_name_is_refused_before_anything_changes(tmp_path):
    gh = FakeGitHub()
    gh.push(V1, "Initial version")
    gh.push({"README.md": "# v2\n"}, "v2")
    root, up, _ = downloaded(tmp_path, gh, V1)
    before = read_tree(root)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("owner-bot-1/../../evil.py", "x")
    real = gh.handler
    gh.handler = lambda r: httpx.Response(200, content=buf.getvalue()) if "zipball" in r.url.path else real(r)
    up._http._transport = httpx.MockTransport(gh.handler)
    with pytest.raises(UpdateError, match="unsafe"):
        up.install(up.check())
    assert read_tree(root) == before and not (tmp_path / "evil.py").exists()


def test_protected_paths_and_titles():
    assert updater_mod.is_protected("config.yaml") and updater_mod.is_protected(".env")
    assert updater_mod.is_protected("data/knowledge.json") and updater_mod.is_protected(".venv/x")
    assert not updater_mod.is_protected("src/topstep_bot/engine.py") and not updater_mod.is_protected("config.example.yaml")
    assert commit_title("Merge pull request #9 from a/b\n\nKeep Telegram alive\n\nmore") == "Keep Telegram alive (#9)"
    assert commit_title("Fix a typo\n\ndetails") == "Fix a typo"


def test_state_file_survives_a_corrupt_write(tmp_path):
    (tmp_path / "updates.json").write_text("{not json")
    assert load_state(tmp_path) == {}
    save_state(tmp_path, {"installed": "abc"})
    assert json.loads((tmp_path / "updates.json").read_text()) == {"installed": "abc"}
