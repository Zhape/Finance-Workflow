"""The workflow contract.

Every workflow in the platform is a module that declares a `SPEC` and a
`run()` function.  That is the whole interface -- the web UI, the job runner
and the run-history table are all generated from it, so adding workflow #3
never touches the platform code.

The three-phase shape of the desktop apps (pull -> review -> act) is modelled
here as a run that can come back `NEEDS_APPROVAL` carrying rows, and is then
finalised separately once a human has ticked the ones they want.  That split
is what lets the approver be a different person on a different machine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Literal, Protocol

ParamType = Literal["date", "string", "choice", "bool"]


@dataclass(frozen=True)
class ParamSpec:
    """One input on the workflow's launch form."""

    name: str
    type: ParamType
    label: str
    required: bool = True
    default: Any = None
    options: list[str] = field(default_factory=list)
    help: str | None = None

    def validate(self, value: Any) -> Any:
        if value in (None, "") and self.required:
            raise ValueError(f"{self.label} is required.")
        if value in (None, ""):
            return self.default
        if self.type == "choice" and value not in self.options:
            raise ValueError(
                f"{self.label}: {value!r} is not one of {self.options}."
            )
        if self.type == "bool":
            return bool(value)
        return value


class RunStatus(str, Enum):
    EMPTY = "empty"                      # nothing to do -- not an error
    NEEDS_APPROVAL = "needs_approval"    # rows are waiting on a human
    COMPLETE = "complete"
    FAILED = "failed"


@dataclass
class Column:
    """A column in the review table."""

    key: str
    label: str
    type: Literal["text", "money", "date", "flag"] = "text"


@dataclass
class RunResult:
    status: RunStatus
    columns: list[Column] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    summary: str = ""
    # Set once the run is finalised.
    artifact_name: str | None = None
    artifact_bytes: bytes | None = None


@dataclass
class RunContext:
    """Everything a workflow is allowed to touch besides its own params.

    Deliberately narrow: a workflow gets credentials and org-scoped reference
    data through here, never from the filesystem or a global.  That is what
    makes the same code safe to run for someone else's Xero org.
    """

    org_id: str
    creds: "CredentialProvider"
    bank_details: Any = None          # BankDetailsSource, when the workflow needs one
    # The org's message templates, already merged over the workflow's defaults.
    templates: dict[str, dict] | None = None
    # Creates email drafts when the org has connected a mailbox. None means
    # not connected, and a workflow must degrade rather than fail: producing a
    # file is still useful without email.
    mailer: Any = None
    sender_name: str | None = None
    log: Callable[[str], None] = print


class CredentialProvider(Protocol):
    """Hands out a connection's secrets for one org, and nothing else."""

    def xero(self, connection: str) -> tuple[str, str]:
        """Return (access_token, tenant_id), refreshing if needed."""
        ...


@dataclass(frozen=True)
class WorkflowSpec:
    key: str
    name: str
    description: str
    params: list[ParamSpec]
    integrations: list[str] = field(default_factory=list)
    # True when run() returns rows for a human to approve before anything
    # leaves the building.
    requires_approval: bool = True

    def parse(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Validate and coerce the submitted form into workflow params."""
        parsed: dict[str, Any] = {}
        errors: dict[str, str] = {}
        for p in self.params:
            try:
                parsed[p.name] = p.validate(raw.get(p.name))
            except ValueError as exc:
                errors[p.name] = str(exc)
        if errors:
            raise ParamError(errors)
        return parsed

    def to_json(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "description": self.description,
            "integrations": self.integrations,
            "requiresApproval": self.requires_approval,
            "params": [
                {
                    "name": p.name,
                    "type": p.type,
                    "label": p.label,
                    "required": p.required,
                    "default": p.default,
                    "options": list(p.options),
                    "help": p.help,
                }
                for p in self.params
            ],
        }


class ParamError(Exception):
    def __init__(self, errors: dict[str, str]):
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors
