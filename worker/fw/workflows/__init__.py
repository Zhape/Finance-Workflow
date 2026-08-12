"""Workflow registry.

Adding a workflow means adding a module with SPEC/run/finalise and one line
here.  The web app reads this registry to build the tile grid and the launch
forms, so no platform code changes.
"""

from types import ModuleType

from . import weekly_payrun

REGISTRY: dict[str, ModuleType] = {
    weekly_payrun.SPEC.key: weekly_payrun,
}


def get(key: str) -> ModuleType:
    try:
        return REGISTRY[key]
    except KeyError:
        raise KeyError(f"Unknown workflow {key!r}") from None


def specs():
    return [m.SPEC for m in REGISTRY.values()]
