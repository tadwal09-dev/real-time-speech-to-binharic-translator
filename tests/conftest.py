import pytest

from binharic import osinfo


@pytest.fixture(autouse=True)
def private_runtime_dir(tmp_path, monkeypatch):
    """Each test gets its own control socket / port file folder, on every OS
    (Windows keeps it in %LOCALAPPDATA%, which XDG_RUNTIME_DIR doesn't move)."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(osinfo, "runtime_dir", lambda: tmp_path)
