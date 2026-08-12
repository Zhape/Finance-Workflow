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


LOCAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "[::1]")


def is_local_redirect(uri: str) -> bool:
    from urllib.parse import urlparse

    host = (urlparse(uri).hostname or "").lower()
    return host in LOCAL_HOSTS or host.endswith(".local")


def redirect_warning(uri: str, variable: str) -> str | None:
    """A message when a redirect URI cannot possibly work here, else None.

    The URIs default to localhost so a developer can run the whole flow without
    configuration. On a public host that default is silently wrong: the consent
    provider rejects it, and the error names neither the variable nor the
    service. Catching it here means the fix is stated rather than deduced.
    """
    if not is_local_redirect(uri):
        return None
    if os.environ.get("FW_ENV", "production") == "dev":
        return None
    return (
        f"This deployment is sending {uri}, which no hosted provider can call "
        f"back. Set {variable} on the worker to its public callback URL."
    )
