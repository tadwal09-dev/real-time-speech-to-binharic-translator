"""NVIDIA GPU support for Whisper.

CTranslate2 needs cuBLAS and cuDNN for CUDA 12. Arch's system ``cuda`` may be
a different major version, so ``install.sh`` puts the CUDA 12 builds from
NVIDIA's pip wheels into binharic's own environment. Here we load them from
there before Whisper asks for them: once a library is loaded, later
``dlopen("libcublas.so.12")`` calls find it by name, so no LD_LIBRARY_PATH
wrapper is needed.
"""

from __future__ import annotations

import ctypes
import glob
import os

_loaded = False


def _wheel_lib_dirs() -> list[str]:
    try:
        import nvidia  # namespace package from nvidia-*-cu12 wheels
    except ImportError:
        return []
    dirs = []
    for base in getattr(nvidia, "__path__", []):
        dirs += sorted(glob.glob(os.path.join(base, "*", "lib")))
    return dirs


def preload_cuda_libs() -> bool:
    """Load cuBLAS/cuDNN from the pip wheels, if installed. Returns True if any loaded."""
    global _loaded
    if _loaded:
        return True
    libs = [p for d in _wheel_lib_dirs() for p in sorted(glob.glob(os.path.join(d, "*.so*")))
            if any(k in os.path.basename(p) for k in ("cublas", "cudnn", "nvrtc", "cudart"))]
    pending = list(libs)
    # cuDNN's pieces depend on each other; keep retrying until nothing new loads
    while pending:
        progress = False
        for p in list(pending):
            try:
                ctypes.CDLL(p, mode=ctypes.RTLD_GLOBAL)
                pending.remove(p)
                progress = True
            except OSError:
                pass
        if not progress:
            break
    _loaded = bool(libs) and len(pending) < len(libs)
    return _loaded


def cuda_devices() -> int:
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count()
    except Exception:
        return 0


def has_nvidia_driver() -> bool:
    return os.path.exists("/proc/driver/nvidia/version")


def libs_available() -> bool:
    preload_cuda_libs()
    for name in ("libcublas.so.12", "libcudnn.so.9"):
        try:
            ctypes.CDLL(name)
        except OSError:
            return False
    return True
