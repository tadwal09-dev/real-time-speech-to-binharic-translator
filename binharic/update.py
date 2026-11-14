"""`binharic update`: check for a newer version and install it, from any folder.

The installer installs from a git clone, and pip records which folder that was
(``direct_url.json``). Updating is: fetch, show what's new, fast-forward, and run
that folder's installer again in its quick mode (no system packages, no "talk to
test your mic", nothing else upgraded). A copy that wasn't installed from a clone
(a zip download, a plain pip install) gets a clone of its own in the data folder.
``--branch`` switches what you follow: ``main`` (shipped after CI passed three
times) or ``experimental``.

The installers write the commit they installed to ``<environment>/binharic-commit``,
so an update that was pulled but never installed (the installer failed, or someone
ran ``git pull`` by hand) is noticed and finished.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from importlib import metadata
from pathlib import Path
from urllib.parse import unquote, urlparse

from . import osinfo

REPO_URL = "https://github.com/tadwal09-dev/real-time-speech-to-binharic-translator.git"
DEFAULT_BRANCH = "main"


def version() -> str:
    try:
        return metadata.version("binharic")
    except metadata.PackageNotFoundError:
        return "?"


def installed_commit() -> str | None:
    """The commit the installer installed, if it said (older installers didn't)."""
    try:
        return (Path(sys.prefix) / "binharic-commit").read_text().split()[0]
    except (OSError, IndexError):
        return None


def describe() -> str:
    commit = installed_commit()
    return f"binharic {version()}" + (f" ({commit[:7]})" if commit else "")


def source_dir() -> Path | None:
    """The git clone binharic was installed from, if it was."""
    try:
        info = json.loads(metadata.distribution("binharic").read_text("direct_url.json") or "{}")
    except (metadata.PackageNotFoundError, ValueError):
        return None
    url = urlparse(info.get("url", ""))
    if url.scheme != "file":
        return None
    path = Path(unquote(url.path.lstrip("/") if osinfo.WINDOWS else url.path))
    return path if (path / ".git").exists() else None


def managed_dir() -> Path:
    """Where binharic keeps a clone of its own when it wasn't installed from one."""
    from .stt import data_dir
    return Path(data_dir()) / "src"


def _git(repo: Path | None, *args: str, check: bool = True) -> str:
    cmd = ["git", *(["-C", str(repo)] if repo else []), *args]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if check and p.returncode:
        why = (p.stderr or p.stdout).strip().splitlines()
        raise RuntimeError(f"git {args[0]} failed: {why[-1] if why else 'exit ' + str(p.returncode)}")
    return p.stdout.strip()


def installer(repo: Path) -> list[str]:
    if osinfo.ISH:
        return ["sh", str(repo / "install-ish.sh")]
    if osinfo.WINDOWS:
        return ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(repo / "install.ps1"), "-Update"]
    return ["bash", str(repo / "install.sh")]


def _clone(say) -> Path:
    repo = managed_dir()
    if not (repo / ".git").exists():
        say(f"⚙ this copy wasn't installed from a git clone: getting one into {repo}")
        repo.parent.mkdir(parents=True, exist_ok=True)
        try:
            _git(None, "clone", "--quiet", "--branch", DEFAULT_BRANCH, REPO_URL, str(repo))
        except RuntimeError as e:
            raise RuntimeError(f"couldn't download binharic ({e}); are you online?") from None
    return repo


def _restart_running(say) -> None:
    """A running layer still runs the old code: restart the background service, or say so."""
    if not osinfo.WINDOWS and shutil.which("systemctl"):
        active = subprocess.run(["systemctl", "--user", "is-active", "--quiet", "binharic"]).returncode == 0
        if active:
            subprocess.run(["systemctl", "--user", "restart", "binharic"])
            say("✔ restarted the background service (systemctl --user status binharic)")
            return
    try:
        from .core import send_command
        send_command("ping")
    except Exception:
        return
    say("⚠ a binharic is running and still uses the old version: close it and start it again")


def run(check: bool = False, branch: str | None = None, force: bool = False, say=print) -> int:
    if not shutil.which("git"):
        raise RuntimeError("git isn't installed (Arch: sudo pacman -S git · Windows: winget install Git.Git · "
                           "iSH: apk add git)")
    repo = source_dir() or _clone(say)
    say(f"⚙ {describe()}, installed from {repo}")
    try:
        _git(repo, "fetch", "--quiet", "--prune", "origin")
    except RuntimeError as e:
        raise RuntimeError(f"couldn't check for updates ({e}); are you online?") from None
    current = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    target = branch or current
    if target == "HEAD":
        raise RuntimeError(f"{repo} isn't on a branch: pick one with `binharic update --branch {DEFAULT_BRANCH}`")
    remote = f"origin/{target}"
    if not _git(repo, "rev-parse", "--verify", "--quiet", remote, check=False):
        names = [b.strip().removeprefix("origin/") for b in _git(repo, "branch", "-r").splitlines()
                 if "->" not in b]
        raise RuntimeError(f"there's no branch '{target}' (there are: {', '.join(names)})")
    new = _git(repo, "log", "--oneline", "--no-decorate", f"HEAD..{remote}").splitlines()
    switching = target != current
    head = _git(repo, "rev-parse", "HEAD")
    was = installed_commit()
    # pulled but not installed: the installer failed last time, or someone ran `git pull`
    unfinished = bool(was) and was != head and not new and not switching
    if not new and not switching and not unfinished and not force:
        say(f"✔ up to date ({target}, {_git(repo, 'log', '-1', '--format=%h %s')})")
        return 0
    plural = lambda n: f"{n} change{'s' if n != 1 else ''}"  # noqa: E731
    if switching:
        lacks = len(_git(repo, "log", "--oneline", f"{remote}..HEAD").splitlines())
        say(f"switching from {current} to {target}: " + (f"{plural(len(new))} that {current} doesn't have"
                                                         if new else f"nothing that {current} doesn't have")
            + (f", and it lacks {plural(lacks)} that {current} has" if lacks else ""))
    elif new:
        say(f"{target}: {plural(len(new))} to install")
    elif unfinished:
        say(f"{target} is at {head[:7]}, but what's installed is {was[:7]}: the last update didn't finish")
    else:
        say("nothing new; reinstalling anyway (--force)")
    for line in new[:15]:
        say(f"   {line}")
    if len(new) > 15:
        say(f"   … and {len(new) - 15} more")
    if check:
        say(f"run `binharic update{' --branch ' + target if switching else ''}` to install")
        return 0
    if new or switching:
        if _git(repo, "status", "--porcelain", "--untracked-files=no"):
            raise RuntimeError(f"{repo} has changes of its own; commit or stash them first (git -C {repo} status)")
        if switching:
            _git(repo, "checkout", "--quiet", target)
        try:
            _git(repo, "merge", "--quiet", "--ff-only", remote)
        except RuntimeError:
            raise RuntimeError(f"{repo} has commits of its own on {target} that GitHub doesn't have, so it can't "
                               f"simply move forward; sort that out with git (git -C {repo} status)") from None
    say(f"✔ {target} is at {_git(repo, 'log', '-1', '--format=%h %s')}; installing it")
    env = dict(os.environ, BINHARIC_UPDATE="1")  # the installers' quick mode
    if osinfo.WINDOWS:
        # Windows can't replace binharic.exe while it runs (this is it): the installer
        # goes on in its own window once we've exited
        subprocess.Popen(installer(repo), env=env, creationflags=subprocess.CREATE_NEW_CONSOLE)
        say("the installer continues in a new window; close any running binharic first")
        return 0
    code = subprocess.run(installer(repo), env=env).returncode
    if code:
        say(f"✖ the installer failed (exit {code}); fix what it says, then run `binharic update` again")
        return code
    say("✔ updated")
    _restart_running(say)
    return 0
