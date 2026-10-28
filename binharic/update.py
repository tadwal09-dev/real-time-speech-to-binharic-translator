"""`binharic update`: get the newest version without going to the repo folder.

The installer installs from a git clone, and pip records which folder that was
(``direct_url.json``). Updating is: fetch, show what's new, fast-forward, and run
that folder's installer again (it skips the "talk to test your mic" step).
``--branch`` switches what you follow: ``main`` (shipped after CI passed three
times) or ``experimental``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from importlib import metadata
from pathlib import Path
from urllib.parse import unquote, urlparse

from . import osinfo


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


def _git(repo: Path, *args: str, check: bool = True) -> str:
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and p.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed: {(p.stderr or p.stdout).strip().splitlines()[-1:]}")
    return p.stdout.strip()


def installer(repo: Path) -> list[str]:
    if osinfo.ISH:
        return ["sh", str(repo / "install-ish.sh")]
    if osinfo.WINDOWS:
        return ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(repo / "install.ps1"), "-Update"]
    return ["bash", str(repo / "install.sh")]


def run(check: bool = False, branch: str | None = None, say=print) -> int:
    if not shutil.which("git"):
        raise RuntimeError("git isn't installed (sudo pacman -S git)")
    repo = source_dir()
    if repo is None:
        raise RuntimeError("binharic wasn't installed from a git clone, so it can't update itself: "
                           "clone the repo and run its installer once")
    say(f"⚙ {repo}")
    _git(repo, "fetch", "--quiet", "origin")
    current = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    target = branch or current
    if target == "HEAD":
        raise RuntimeError(f"{repo} isn't on a branch: pick one with `binharic update --branch main`")
    remote = f"origin/{target}"
    if not _git(repo, "rev-parse", "--verify", "--quiet", remote, check=False):
        names = [b.strip().removeprefix("origin/") for b in _git(repo, "branch", "-r").splitlines()
                 if "->" not in b]
        raise RuntimeError(f"there's no branch '{target}' (there are: {', '.join(names)})")
    new = _git(repo, "log", "--oneline", "--no-decorate", f"HEAD..{remote}").splitlines()
    switching = target != current
    if not new and not switching:
        say(f"✔ up to date ({target}, {_git(repo, 'log', '-1', '--format=%h %s')})")
        return 0
    plural = lambda n: f"{n} change{'s' if n != 1 else ''}"  # noqa: E731
    if switching:
        lacks = len(_git(repo, "log", "--oneline", f"{remote}..HEAD").splitlines())
        say(f"switching from {current} to {target}: " + (f"{plural(len(new))} that {current} doesn't have"
                                                         if new else f"nothing that {current} doesn't have")
            + (f", and it lacks {plural(lacks)} that {current} has" if lacks else ""))
    else:
        say(f"{target}: {plural(len(new))} to install")
    for line in new[:15]:
        say(f"   {line}")
    if len(new) > 15:
        say(f"   … and {len(new) - 15} more")
    if check:
        say(f"run `binharic update{' --branch ' + target if switching else ''}` to install")
        return 0
    if _git(repo, "status", "--porcelain", "--untracked-files=no"):
        raise RuntimeError(f"{repo} has changes of its own; commit or stash them first (git -C {repo} status)")
    if switching:
        _git(repo, "checkout", "--quiet", target)
    _git(repo, "merge", "--quiet", "--ff-only", remote)
    say(f"✔ {target} is now at {_git(repo, 'log', '-1', '--format=%h %s')}; reinstalling")
    env = dict(os.environ, BINHARIC_UPDATE="1")  # the installers skip the interactive mic test
    if osinfo.WINDOWS:
        # Windows can't replace binharic.exe while it runs (this is it): the installer
        # goes on in its own window once we've exited
        subprocess.Popen(installer(repo), env=env, creationflags=subprocess.CREATE_NEW_CONSOLE)
        say("the installer continues in a new window")
        return 0
    code = subprocess.run(installer(repo), env=env).returncode
    if code:
        say(f"✖ the installer failed (exit {code}); fix what it says, then run `binharic update` again")
        return code
    say("✔ updated. If binharic is running, restart it to use the new version")
    return 0
