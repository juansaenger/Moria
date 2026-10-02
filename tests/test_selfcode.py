from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from homebot.selfcode import (
    RepoConfig,
    SelfCodeError,
    SelfCodeTools,
    check_branch,
    safe_relative,
)


class Approver:
    def __init__(self, answer: bool) -> None:
        self.answer = answer
        self.asked: list[str] = []

    async def __call__(self, summary: str) -> bool:
        self.asked.append(summary)
        return self.answer


class RepoPath(type(Path())):  # type: ignore[misc]
    """A Path that also remembers where its origin lives."""
    bare_origin: str


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real git repo, because the tool shells out to real git."""
    root = RepoPath(tmp_path / "repo")
    root.mkdir()
    env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1")
    run = lambda *a: subprocess.run(["git", "-C", str(root), *a], check=True, capture_output=True, env=env)
    subprocess.run(["git", "init", "-q", "-b", "work", str(root)], check=True, capture_output=True, env=env)
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "Test")
    run("config", "core.autocrlf", "false")
    (root / "src").mkdir()
    # LF explicitly: on Windows the default stores CRLF, and then every
    # comparison against the tool's LF output looks like a change.
    (root / "src" / "thing.py").write_text("value = 1\n", encoding="utf-8", newline="\n")
    (root / "README.md").write_text("hello\n", encoding="utf-8", newline="\n")
    (root / ".github").mkdir()
    (root / ".github" / "workflows.yml").write_text("on: push\n", encoding="utf-8", newline="\n")
    run("add", "-A")
    run("commit", "-q", "-m", "initial")
    # A real origin, because the tool branches from origin/<base>.
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True, capture_output=True, env=env)
    run("remote", "add", "origin", str(bare))
    run("push", "-q", "origin", "work")
    run("fetch", "-q", "origin")
    root.bare_origin = str(bare)  # type: ignore[attr-defined]
    return root


def _tools(root: Path, **kw) -> SelfCodeTools:
    return SelfCodeTools(RepoConfig(path=str(root), **kw))


# ---- the guards, which are the whole point


def test_branch_names_that_would_be_dangerous_are_refused():
    for bad in ("main", "MASTER", "production", "work"):
        with pytest.raises(SelfCodeError, match="Refusing|lowercase"):
            check_branch(bad, base="work")


def test_branch_names_must_look_like_branch_names():
    for bad in ("a", "has space", "UPPER", "-leading", "x" * 90):
        with pytest.raises(SelfCodeError):
            check_branch(bad, base="work")
    assert check_branch("jarvis/fix-the-thing", base="work") == "jarvis/fix-the-thing"


def test_paths_cannot_escape_the_repository(repo):
    for bad in ("../outside.py", "src/../../x", "../../etc/passwd"):
        with pytest.raises(SelfCodeError, match="outside|off limits"):
            safe_relative(repo, bad)


def test_an_absolute_path_is_treated_as_repo_relative_not_as_the_filesystem(repo):
    # "/etc/passwd" must not reach the real /etc/passwd; it becomes repo/etc/passwd.
    assert safe_relative(repo, "/etc/passwd").as_posix() == "etc/passwd"
    assert safe_relative(repo, "/src/thing.py").as_posix() == "src/thing.py"


def test_paths_that_decide_what_runs_are_off_limits(repo):
    for bad in (".github/workflows.yml", ".git/config", "deploy/run.sh"):
        with pytest.raises(SelfCodeError, match="off limits"):
            safe_relative(repo, bad)


# ---- reading


def test_list_and_read(repo):
    tools = _tools(repo)
    listed = tools._list(None)
    assert "src/thing.py" in listed and "README.md" in listed
    assert ".git/" not in listed
    assert tools._read("src/thing.py") == "value = 1\n"


def test_reading_a_missing_file_says_so(repo):
    with pytest.raises(SelfCodeError, match="not a file"):
        _tools(repo)._read("src/nope.py")


def test_read_only_without_a_token(repo):
    names = _tools(repo).names
    assert names == {"list_own_code", "read_own_code"}


def test_no_tools_at_all_without_a_repo(tmp_path):
    assert SelfCodeTools(RepoConfig(path=str(tmp_path))).definitions == []


def test_propose_appears_once_configured(repo):
    tools = _tools(repo, token="t", repo="o/r", base_branch="work")
    assert "propose_change" in tools.names


# ---- proposing


def _full(root: Path, **kw) -> SelfCodeTools:
    return _tools(root, token="tok", repo="owner/name", base_branch="work", **kw)


@pytest.mark.asyncio
async def test_a_denied_proposal_pushes_nothing(repo, monkeypatch):
    pushed: list[tuple] = []
    real = __import__("homebot.selfcode", fromlist=["git"]).git

    async def fake_git(root, *args, **kw):
        if args[0] == "push":
            pushed.append(args)
            return ""
        if args[0] == "clone":
            dest = args[-1]
            return await real(root, "clone", "--quiet", "--branch", "work", repo.bare_origin, dest)
        return await real(root, *args, **kw)

    monkeypatch.setattr("homebot.selfcode.git", fake_git)
    tools = _full(repo)
    approver = Approver(False)
    out = await tools.run(
        "propose_change",
        {"branch": "jarvis/try", "title": "T", "body": "B",
         "files": [{"path": "src/thing.py", "content": "value = 2\n"}]},
        approver,
    )
    assert "DENIED" in out
    assert not any(a[0] == "push" for a in pushed)
    assert approver.asked and "value = 2" in approver.asked[0]


@pytest.mark.asyncio
async def test_the_approval_shows_a_diff_and_the_branch(repo, monkeypatch):
    real = __import__("homebot.selfcode", fromlist=["git"]).git

    async def fake_git(root, *args, **kw):
        if args[0] == "push":
            return ""
        if args[0] == "clone":
            dest = args[-1]
            return await real(root, "clone", "--quiet", "--branch", "work", repo.bare_origin, dest)
        return await real(root, *args, **kw)

    monkeypatch.setattr("homebot.selfcode.git", fake_git)
    approver = Approver(False)
    await _full(repo).run(
        "propose_change",
        {"branch": "jarvis/try", "title": "Bump the value", "body": "B",
         "files": [{"path": "src/thing.py", "content": "value = 99\n"}]},
        approver,
    )
    summary = approver.asked[0]
    assert "Bump the value" in summary and "jarvis/try" in summary
    assert "-value = 1" in summary and "+value = 99" in summary


@pytest.mark.asyncio
async def test_a_no_op_change_is_refused_before_bothering_the_user(repo, monkeypatch):
    real = __import__("homebot.selfcode", fromlist=["git"]).git

    async def fake_git(root, *args, **kw):
        if args[0] == "push":
            return ""
        if args[0] == "clone":
            dest = args[-1]
            return await real(root, "clone", "--quiet", "--branch", "work", repo.bare_origin, dest)
        return await real(root, *args, **kw)

    monkeypatch.setattr("homebot.selfcode.git", fake_git)

    async def no_network(*a, **k):
        raise AssertionError("a no-op proposal must never reach GitHub")

    monkeypatch.setattr(SelfCodeTools, "_open_pr", no_network)
    approver = Approver(True)
    with pytest.raises(SelfCodeError, match="changes nothing"):
        await _full(repo).run(
            "propose_change",
            {"branch": "jarvis/try", "title": "T", "body": "B",
             "files": [{"path": "src/thing.py", "content": "value = 1\n"}]},
            approver,
        )
    assert approver.asked == []


@pytest.mark.asyncio
async def test_proposing_onto_the_live_branch_is_refused(repo):
    with pytest.raises(SelfCodeError, match="Refusing"):
        await _full(repo).run(
            "propose_change",
            {"branch": "work", "title": "T", "body": "B",
             "files": [{"path": "src/thing.py", "content": "x\n"}]},
            Approver(True),
        )


@pytest.mark.asyncio
async def test_proposing_a_workflow_change_is_refused(repo):
    with pytest.raises(SelfCodeError, match="off limits"):
        await _full(repo).run(
            "propose_change",
            {"branch": "jarvis/ci", "title": "T", "body": "B",
             "files": [{"path": ".github/workflows.yml", "content": "on: push\n"}]},
            Approver(True),
        )


@pytest.mark.asyncio
async def test_binary_ish_paths_are_refused(repo):
    with pytest.raises(SelfCodeError, match="not a kind of file"):
        await _full(repo).run(
            "propose_change",
            {"branch": "jarvis/x", "title": "T", "body": "B",
             "files": [{"path": "src/logo.png", "content": "x"}]},
            Approver(True),
        )


@pytest.mark.asyncio
async def test_preview_describes_the_push_without_doing_it(repo):
    tools = _full(repo)
    preview = await tools.preview(
        "propose_change",
        {"branch": "jarvis/x", "title": "T", "body": "B",
         "files": [{"path": "src/thing.py", "content": "y\n"}]},
    )
    assert preview is not None and "jarvis/x" in preview and "src/thing.py" in preview
    assert await tools.preview("read_own_code", {"path": "src/thing.py"}) is None


@pytest.mark.asyncio
async def test_a_failed_git_command_does_not_leak_the_token(repo, monkeypatch):
    from homebot.selfcode import git

    url = "https://x-access-token:supersecret@github.com/o/r.git"
    with pytest.raises(SelfCodeError) as caught:
        await git(repo, "push", url, "HEAD:refs/heads/nope", token_url=url)
    assert "supersecret" not in str(caught.value)


@pytest.mark.asyncio
async def test_the_server_checkout_is_never_written_to(repo, monkeypatch):
    """The whole point of the rewrite: proposing must need only read access."""
    import os as _os
    import stat as _stat

    real = __import__("homebot.selfcode", fromlist=["git"]).git

    async def fake_git(root, *args, **kw):
        if args[0] == "push":
            return ""
        if args[0] == "clone":
            dest = args[-1]
            return await real(root, "clone", "--quiet", "--branch", "work", repo.bare_origin, dest)
        return await real(root, *args, **kw)

    monkeypatch.setattr("homebot.selfcode.git", fake_git)

    async def fake_pr(self, branch, title, body):
        return f"https://github.test/pull/1 ({branch})"

    monkeypatch.setattr(SelfCodeTools, "_open_pr", fake_pr)

    before = {}
    for path in sorted(repo.rglob("*")):
        try:
            before[str(path)] = path.stat().st_mtime_ns
        except OSError:
            pass

    approver = Approver(True)
    out = await _full(repo).run(
        "propose_change",
        {"branch": "jarvis/readonly", "title": "T", "body": "B",
         "files": [{"path": "src/thing.py", "content": "value = 7\n"}]},
        approver,
    )
    assert "pull request" in out or "Pushed" in out

    after = {}
    for path in sorted(repo.rglob("*")):
        try:
            after[str(path)] = path.stat().st_mtime_ns
        except OSError:
            pass
    assert after == before, "the checkout on the server was modified"
    # And the branch exists only on the remote, never in the server's checkout.
    branches = subprocess.run(
        ["git", "-C", str(repo), "branch", "--list"], capture_output=True, text=True
    ).stdout
    assert "jarvis/readonly" not in branches


@pytest.mark.asyncio
async def test_the_scratch_clone_is_cleaned_up_even_on_failure(repo, monkeypatch):
    import tempfile as _tempfile

    real = __import__("homebot.selfcode", fromlist=["git"]).git

    async def fake_git(root, *args, **kw):
        if args[0] == "clone":
            dest = args[-1]
            return await real(root, "clone", "--quiet", "--branch", "work", repo.bare_origin, dest)
        if args[0] == "push":
            raise SelfCodeError("push exploded")
        return await real(root, *args, **kw)

    monkeypatch.setattr("homebot.selfcode.git", fake_git)
    before = set(Path(_tempfile.gettempdir()).glob("homebot-proposal-*"))
    with pytest.raises(SelfCodeError, match="push exploded"):
        await _full(repo).run(
            "propose_change",
            {"branch": "jarvis/boom", "title": "T", "body": "B",
             "files": [{"path": "src/thing.py", "content": "value = 8\n"}]},
            Approver(True),
        )
    assert set(Path(_tempfile.gettempdir()).glob("homebot-proposal-*")) == before
