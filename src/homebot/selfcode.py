"""The bot reading its own source, and proposing changes as a pull request.

Three rules are enforced here in code, not only in the prompt, because a prompt
is a request and this is a boundary:

  * it never commits to the base branch, or to main or master;
  * it never force-pushes;
  * it cannot touch anything that runs automatically on merge, so a pull request
    cannot smuggle in its own execution.

Deploying is deliberately absent. A merge is what ships code, and a human does
that.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

log = logging.getLogger(__name__)

Approver = Callable[[str], Awaitable[bool]]

MAX_FILE_BYTES = 120_000
MAX_LISTED = 200
MAX_FILES_PER_CHANGE = 20
MAX_DIFF_LINES = 50
GIT_TIMEOUT = 120.0

PROTECTED_BRANCHES = {"main", "master", "trunk", "production", "release"}
BRANCH_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._/-]{2,80}$")
# Paths that decide what runs by itself. A pull request that could edit these
# could arrange to execute its own code the moment it is merged.
FORBIDDEN_PREFIXES = (".git/", ".github/", "deploy/")
TEXT_SUFFIXES = {
    ".py", ".md", ".txt", ".toml", ".yml", ".yaml", ".json", ".cfg", ".ini",
    ".sh", ".example", ".gitignore", "Dockerfile", "",
}


class SelfCodeError(RuntimeError):
    """Reported back to Claude as a failed tool result."""


@dataclass(frozen=True)
class RepoConfig:
    path: str = ""
    token: str = ""
    repo: str = ""  # owner/name
    base_branch: str = ""

    @property
    def can_read(self) -> bool:
        return bool(self.path) and os.path.isdir(os.path.join(self.path, ".git"))

    @property
    def can_propose(self) -> bool:
        return self.can_read and bool(self.token and self.repo and self.base_branch)


def safe_relative(repo_root: Path, candidate: str) -> Path:
    """Resolve a path inside the repo, refusing anything that escapes it."""
    candidate = (candidate or "").strip().lstrip("/")
    if not candidate:
        raise SelfCodeError("path is required")
    resolved = (repo_root / candidate).resolve()
    try:
        relative = resolved.relative_to(repo_root.resolve())
    except ValueError as exc:
        raise SelfCodeError(f"{candidate!r} is outside the repository.") from exc
    posix = relative.as_posix()
    if posix == ".git" or any(posix.startswith(p) for p in FORBIDDEN_PREFIXES):
        raise SelfCodeError(
            f"{posix!r} is off limits: it decides what runs automatically. Ask the user to change it by hand."
        )
    return relative


def _remove_tree(path: Path) -> None:
    """Delete a scratch clone. git leaves object files read-only, which stops a
    plain rmtree on some platforms, so make them writable and try again."""

    def retry(func: Any, target: Any, _exc: Any) -> None:
        try:
            os.chmod(target, 0o700)
            func(target)
        except OSError:
            log.warning("could not remove %s", target)

    shutil.rmtree(path, onerror=retry)


def check_branch(name: str, base: str) -> str:
    name = (name or "").strip()
    if not BRANCH_PATTERN.fullmatch(name):
        raise SelfCodeError(
            "Branch names must be lowercase letters, digits, dot, dash, underscore or slash, 3 to 81 characters."
        )
    if name.lower() in PROTECTED_BRANCHES or name == base:
        raise SelfCodeError(f"Refusing to commit to {name!r}. Propose a new branch and open a pull request.")
    return name


async def git(repo: Path, *args: str, token_url: str | None = None) -> str:
    """Run one git command. Never shells out, so nothing is interpolated."""
    # The clone is owned by the host user, not the container user, so git's
    # "dubious ownership" check would refuse every command. Declare it safe for
    # this process only, rather than writing config into the image or the repo.
    env = dict(
        os.environ,
        GIT_TERMINAL_PROMPT="0",
        GIT_ASKPASS="",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_COUNT="4",
        GIT_CONFIG_KEY_0="safe.directory",
        GIT_CONFIG_VALUE_0=str(repo),
        # Cloning reads the .git directory directly, and git checks that path
        # separately from the work tree it sits in.
        GIT_CONFIG_KEY_1="safe.directory",
        GIT_CONFIG_VALUE_1=str(Path(repo) / ".git"),
        GIT_CONFIG_KEY_2="safe.directory",
        GIT_CONFIG_VALUE_2=tempfile.gettempdir() + "/*",
        GIT_CONFIG_KEY_3="safe.directory",
        GIT_CONFIG_VALUE_3=str(repo) + "/*",
    )
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(repo), *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=GIT_TIMEOUT)
    except asyncio.TimeoutError as exc:
        proc.kill()
        raise SelfCodeError(f"git {args[0]} timed out") from exc
    if proc.returncode != 0:
        message = (err or b"").decode(errors="replace").strip()
        if token_url:
            message = message.replace(token_url, "<credentials>")
        raise SelfCodeError(f"git {args[0]} failed: {message[:300]}")
    return (out or b"").decode(errors="replace")


class SelfCodeTools:
    def __init__(self, config: RepoConfig) -> None:
        self._config = config
        self._root = Path(config.path) if config.path else Path(".")

    @property
    def definitions(self) -> list[dict[str, Any]]:
        if not self._config.can_read:
            return []
        defs = [
            {
                "name": "list_own_code",
                "description": (
                    "List the files of your own source repository, so you can find what to read. "
                    "Optionally restrict to a folder."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {"folder": {"type": "string", "description": "e.g. 'src/homebot'."}},
                    "additionalProperties": False,
                },
            },
            {
                "name": "read_own_code",
                "description": (
                    "Read one file of your own source. Use it before proposing any change, so you edit the "
                    "real current contents rather than what you remember."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {"path": {"type": "string", "description": "Repo-relative, e.g. 'src/homebot/media.py'."}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
            },
        ]
        if self._config.can_propose:
            defs.append(
                {
                    "name": "propose_change",
                    "description": (
                        "Propose a change to your own code as a pull request for the user to review. Give the "
                        "COMPLETE new contents of each file you are changing, not a fragment or a diff. Read "
                        "each file first. The user is shown the branch, the files and a diff, and must tap "
                        "Approve before anything is pushed. You cannot merge it and you cannot deploy; a "
                        "human does both. Say the pull request link in your reply."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "branch": {"type": "string", "description": "New branch, e.g. 'jarvis/fix-ups-parsing'."},
                            "title": {"type": "string", "description": "Pull request title."},
                            "body": {"type": "string", "description": "What changed and why, for the reviewer."},
                            "files": {
                                "type": "array",
                                "minItems": 1,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "path": {"type": "string"},
                                        "content": {"type": "string", "description": "The whole file, after your change."},
                                    },
                                    "required": ["path", "content"],
                                    "additionalProperties": False,
                                },
                            },
                        },
                        "required": ["branch", "title", "body", "files"],
                        "additionalProperties": False,
                    },
                }
            )
        return defs

    @property
    def names(self) -> set[str]:
        return {d["name"] for d in self.definitions}

    async def preview(self, name: str, tool_input: dict[str, Any]) -> str | None:
        if name != "propose_change":
            return None
        try:
            branch = check_branch(str(tool_input.get("branch") or ""), self._config.base_branch)
            files = self._clean_files(tool_input.get("files"))
        except SelfCodeError:
            return None
        listed = ", ".join(path for path, _ in files)
        return f"push branch {branch!r} and open a pull request changing {len(files)} file(s): {listed}"

    async def run(self, name: str, tool_input: dict[str, Any], approve: Approver) -> str:
        if name == "list_own_code":
            return self._list(tool_input.get("folder"))
        if name == "read_own_code":
            return self._read(str(tool_input.get("path") or ""))
        if name == "propose_change":
            return await self._propose(tool_input, approve)
        raise SelfCodeError(f"Unknown tool {name!r}")

    # ---- reading

    def _list(self, folder: str | None) -> str:
        root = self._root
        start = root / safe_relative(root, folder) if folder else root
        if not start.is_dir():
            raise SelfCodeError(f"{folder!r} is not a folder in the repository.")
        found = []
        for path in sorted(start.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            if rel.startswith(".git/") or "/__pycache__/" in rel or rel.endswith(".pyc"):
                continue
            found.append(f"{rel} ({path.stat().st_size} bytes)")
        if not found:
            return "No files there."
        if len(found) > MAX_LISTED:
            return "\n".join(found[:MAX_LISTED] + [f"... and {len(found) - MAX_LISTED} more"])
        return "\n".join(found)

    def _read(self, path: str) -> str:
        rel = safe_relative(self._root, path)
        full = self._root / rel
        if not full.is_file():
            raise SelfCodeError(f"{rel.as_posix()!r} is not a file in the repository.")
        size = full.stat().st_size
        if size > MAX_FILE_BYTES:
            raise SelfCodeError(f"{rel.as_posix()!r} is {size} bytes, too big to read whole.")
        try:
            return full.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise SelfCodeError(f"{rel.as_posix()!r} is not a text file.") from exc

    # ---- proposing

    def _clean_files(self, raw: Any) -> list[tuple[str, str]]:
        if not isinstance(raw, list) or not raw:
            raise SelfCodeError("files must be a non-empty list of {path, content}")
        if len(raw) > MAX_FILES_PER_CHANGE:
            raise SelfCodeError(f"At most {MAX_FILES_PER_CHANGE} files in one proposal.")
        out = []
        for entry in raw:
            if not isinstance(entry, dict):
                raise SelfCodeError("each file must be an object with path and content")
            content = entry.get("content")
            if not isinstance(content, str):
                raise SelfCodeError("each file needs its complete new content as a string")
            rel = safe_relative(self._root, str(entry.get("path") or ""))
            if rel.suffix not in TEXT_SUFFIXES and rel.name not in TEXT_SUFFIXES:
                raise SelfCodeError(f"{rel.as_posix()!r} is not a kind of file you may change.")
            out.append((rel.as_posix(), content))
        return out

    async def _propose(self, tool_input: dict[str, Any], approve: Approver) -> str:
        cfg = self._config
        if not cfg.can_propose:
            raise SelfCodeError("Proposing changes is not configured. GITHUB_TOKEN and GITHUB_REPO are needed.")
        branch = check_branch(str(tool_input.get("branch") or ""), cfg.base_branch)
        title = str(tool_input.get("title") or "").strip()
        body = str(tool_input.get("body") or "").strip()
        if not title:
            raise SelfCodeError("title is required")
        files = self._clean_files(tool_input.get("files"))

        # Work in a throwaway clone, never in the checkout on the server. That
        # checkout belongs to the host user and to the deploy watcher; writing to
        # its .git needed permissions the container has no business holding, and
        # a half-finished proposal could have left the next deploy reading a
        # dirty tree. Cloning needs only read access to it.
        url = f"https://x-access-token:{cfg.token}@github.com/{cfg.repo}.git"
        scratch = Path(tempfile.mkdtemp(prefix="homebot-proposal-"))
        clone = scratch / "work"
        try:
            await git(self._root, "clone", "--quiet", "--no-checkout", str(self._root), str(clone))
            # Branch from what GitHub has right now, not from whatever the
            # server last pulled, so a stale checkout cannot silently rebase the
            # proposal onto an old base.
            await git(clone, "fetch", "--quiet", url, cfg.base_branch, token_url=url)
            await git(clone, "checkout", "--quiet", "-b", branch, "FETCH_HEAD")
            for rel, content in files:
                target = clone / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8", newline="\n")
            await git(clone, "add", "--", *(rel for rel, _ in files))
            diff_stat = await git(clone, "diff", "--cached", "--stat")
            if not diff_stat.strip():
                raise SelfCodeError("That changes nothing: the files already have exactly those contents.")
            patch = await git(clone, "diff", "--cached")
            summary = self._summarise(branch, title, files, diff_stat, patch)
            if not await approve(summary):
                return "The user DENIED this change (or did not answer in time). Nothing was pushed."
            await git(clone, "-c", "user.name=Jarvis", "-c", "user.email=jarvis@localhost",
                      "commit", "--quiet", "-m", title, "-m", body or "Proposed from Discord.")
            await git(clone, "push", "--quiet", url, f"HEAD:refs/heads/{branch}", token_url=url)
        finally:
            _remove_tree(scratch)
        pr = await self._open_pr(branch, title, body)
        return f"Pushed {branch} and opened a pull request: {pr}. Review and merge it yourself; I cannot."

    def _summarise(self, branch: str, title: str, files: list[tuple[str, str]], stat: str, patch: str) -> str:
        lines = patch.splitlines()
        if len(lines) > MAX_DIFF_LINES:
            lines = lines[:MAX_DIFF_LINES] + [f"... {len(lines) - MAX_DIFF_LINES} more lines"]
        return (
            f"open a pull request **{title}** on branch `{branch}`\n"
            f"```\n{stat.strip()}\n```\n"
            f"```diff\n{chr(10).join(lines)}\n```"
        )

    async def _open_pr(self, branch: str, title: str, body: str) -> str:
        cfg = self._config
        payload = {
            "title": title,
            "head": branch,
            "base": cfg.base_branch,
            "body": (body or "") + "\n\nProposed by Jarvis from Discord. Not reviewed by a human yet.",
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"https://api.github.com/repos/{cfg.repo}/pulls",
                json=payload,
                headers={
                    "Authorization": f"Bearer {cfg.token}",
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "homebot",
                },
            )
        if response.status_code >= 400:
            raise SelfCodeError(
                f"The branch pushed, but opening the pull request failed ({response.status_code}): "
                f"{response.text[:200]}. Open it by hand from branch {branch}."
            )
        return str(response.json().get("html_url") or "(no url returned)")
