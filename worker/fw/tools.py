"""Tools: org-granted capabilities that are not batch workflows.

`workflows.REGISTRY` holds things with a launch form, a `run()` and an approval
step. A tool has its own screen and its own lifecycle — the Invoice Inbox is
one, and forcing it into `SPEC + run() + finalise()` would have meant a `run()`
that does not run and an `approve()` that does not approve.

Access is granted through the same `org_workflows` table and the same fail-
closed rule: an org with no row does not see the tile and cannot reach the API.
"""

from types import ModuleType

from . import inbox

REGISTRY: dict[str, ModuleType] = {inbox.KEY: inbox}


def get(key: str) -> ModuleType:
    try:
        return REGISTRY[key]
    except KeyError:
        raise KeyError(f"Unknown tool {key!r}") from None


def has(key: str) -> bool:
    return key in REGISTRY


def specs():
    return [m.SPEC for m in REGISTRY.values()]
