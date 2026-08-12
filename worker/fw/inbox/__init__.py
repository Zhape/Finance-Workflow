"""Invoice Inbox — customer invoice email, read, checked and answered.

The platform's first capability that is not a batch workflow. A pay run starts
from a launch form and ends in a file; mail arrives on its own schedule and
ends in a reply, so it does not fit `SPEC + run() + finalise()` and is not
pretending to. What it does reuse is everything that matters: the connections
table and its Fernet envelope, the per-org provider apps, the org-workflow
access gate, the template override rule, and the Principal/role model.

Two departures from the original design, both deliberate:

  * **Sync is a button.** No Gmail watch, no Pub/Sub topic, no daily renewal
    job, no always-on host. Someone presses Sync and waits a few seconds. That
    trades away "within one minute of the Gmail timestamp" for being able to
    run on a free instance that sleeps, and it removes three moving parts that
    would each have to work perfectly before a single email arrived.

  * **Nothing is sent.** The reply is created as a Gmail *draft* inside the
    customer's own thread, and a person presses send from Gmail. There is no
    auto-send mode and no `auto_send_enabled` column to turn one on. The Gmail
    scope requested cannot send at all, so this is a ceiling Google enforces
    rather than a promise this package makes.

What survives from the design either way is the part with the judgement in it:
the classifier can only answer with a category from a schema, Xero is the only
source of any factual value in a reply, and the renderer that fills the
approved template cannot reach a language model.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import KEY
from .templates import PLACEHOLDERS, default_templates

NAME = "Invoice Inbox"
DESCRIPTION = (
    "Read customer invoice emails, check them against Xero, and prepare a "
    "reply from an approved template — as a Gmail draft you send yourself."
)


@dataclass(frozen=True)
class ToolSpec:
    """What the tile grid needs. The run-registry equivalent for a tool."""

    key: str
    name: str
    description: str
    integrations: list[str]
    href: str

    def to_json(self) -> dict:
        return {
            "key": self.key,
            "name": self.name,
            "description": self.description,
            "integrations": list(self.integrations),
            "href": self.href,
        }


SPEC = ToolSpec(
    key=KEY,
    name=NAME,
    description=DESCRIPTION,
    integrations=["gmail", "xero"],
    href="/inbox",
)

__all__ = ["KEY", "NAME", "DESCRIPTION", "SPEC", "PLACEHOLDERS",
           "default_templates"]
