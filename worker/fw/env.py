"""Load worker/.env into the process, without a dependency.

Hosted deployments set real environment variables; this exists so local runs
have somewhere to keep FW_ENCRYPTION_KEY and the Xero app credentials that is
not the shell history and not the repo. Existing environment variables always
win, so a deployed value is never overridden by a stray file.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


def load(path: Path | None = None) -> None:
    path = path or ENV_FILE
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)
