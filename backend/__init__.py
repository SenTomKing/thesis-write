from __future__ import annotations

import site
import sys
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOCAL_PACKAGES = PROJECT_ROOT / ".python_packages"

try:
    user_site = site.getusersitepackages()
except Exception:  # pragma: no cover - environment-specific startup guard
    user_site = ""

if user_site and user_site not in sys.path:
    sys.path.append(user_site)

if LOCAL_PACKAGES.exists() and str(LOCAL_PACKAGES) not in sys.path:
    # Keep local vendored packages as a fallback. Putting them first can shadow
    # the active Python version with incompatible compiled wheels.
    sys.path.append(str(LOCAL_PACKAGES))


def _strip_optional_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _load_project_env() -> None:
    for env_path in (PROJECT_ROOT / ".env", PROJECT_ROOT / ".env.local"):
        if not env_path.exists():
            continue
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if not key or key in os.environ:
                continue
            os.environ[key] = _strip_optional_quotes(value)


_load_project_env()
