import json
import os
import shutil
import subprocess
import sys

import pytest

from binharic import update

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="needs git")


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repos(tmp_path, monkeypatch):
    """A GitHub stand-in, a developer's clone that pushes to it, and the user's clone
    binharic was installed from."""
    for k, v in {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                 "GIT_COMMITTER_EMAIL": "t@t"}.items():
        monkeypatch.setenv(k, v)
    origin, dev, user = tmp_path / "origin.git", tmp_path / "dev", tmp_path / "user"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(dev)], check=True, capture_output=True)
    git(dev, "checkout", "-q", "-b", "main")
    (dev / "README.md").write_text("v1\n")
    git(dev, "add", ".")
    git(dev, "commit", "-q", "-m", "first")
    git(dev, "push", "-q", "origin", "main")
    subprocess.run(["git", "clone", "-q", str(origin), str(user)], check=True, capture_output=True)
    mark = tmp_path / "installed"
    monkeypatch.setattr(update, "source_dir", lambda: user)
    monkeypatch.setattr(update.osinfo, "WINDOWS", False)
    monkeypatch.setattr(update, "installer", lambda repo: [
        sys.executable, "-c",
        f"import os; open({str(mark)!r}, 'a').write(os.environ.get('BINHARIC_UPDATE', '') + ' ' + "
        f"open(os.path.join({str(repo)!r}, 'README.md')).read())"])
    return dev, user, mark


def push(dev, text, msg, branch="main"):
    (dev / "README.md").write_text(text)
    git(dev, "commit", "-q", "-am", msg)
    git(dev, "push", "-q", "origin", branch)


def test_check_only_lists_whats_new(repos):
    dev, user, mark = repos
    push(dev, "v2\n", "decoder: hear Discord over music")
    out = []
    assert update.run(check=True, say=out.append) == 0
    assert any("1 change to install" in line for line in out) and any("hear Discord over music" in line for line in out)
    assert (user / "README.md").read_text() == "v1\n" and not mark.exists()


def test_update_pulls_and_reinstalls_without_the_mic_test(repos):
    dev, user, mark = repos
    out = []
    assert update.run(say=out.append) == 0 and any("up to date" in line for line in out)
    assert not mark.exists()  # nothing new: nothing reinstalled
    push(dev, "v2\n", "two")
    assert update.run(say=out.append) == 0
    assert (user / "README.md").read_text() == "v2\n"
    assert mark.read_text() == "1 v2\n"  # the installer ran on the new code, told it's an update


def test_switching_to_the_experimental_branch_and_back(repos):
    dev, user, mark = repos
    git(dev, "checkout", "-q", "-b", "experimental")
    push(dev, "exp\n", "try something", branch="experimental")
    assert update.run(branch="experimental", say=lambda t: None) == 0
    assert git(user, "rev-parse", "--abbrev-ref", "HEAD") == "experimental"
    assert (user / "README.md").read_text() == "exp\n"
    assert update.run(branch="main", say=lambda t: None) == 0
    assert (user / "README.md").read_text() == "v1\n"
    with pytest.raises(RuntimeError, match="no branch 'nope'.*experimental"):
        update.run(branch="nope", say=lambda t: None)


def test_local_changes_are_never_overwritten(repos):
    dev, user, mark = repos
    push(dev, "v2\n", "two")
    (user / "README.md").write_text("my own edit\n")
    with pytest.raises(RuntimeError, match="changes of its own"):
        update.run(say=lambda t: None)
    assert (user / "README.md").read_text() == "my own edit\n" and not mark.exists()


def test_the_clone_is_found_from_the_install_record(tmp_path, monkeypatch):
    clone = tmp_path / "my clone"
    (clone / ".git").mkdir(parents=True)

    class Dist:
        def read_text(self, name):
            assert name == "direct_url.json"
            return json.dumps({"url": clone.as_uri(), "dir_info": {}})
    monkeypatch.setattr(update.metadata, "distribution", lambda name: Dist())
    assert os.path.samefile(update.source_dir(), clone)
    (clone / ".git").rmdir()
    assert update.source_dir() is None  # installed from a plain folder: can't update itself
