"""Workflow requests: naming, the registry patch, and the PR pipeline.

Generation and GitHub are faked throughout — the properties pinned here are
the ones that survive either being swapped out: names that cannot escape the
workflows directory, a registry patch that fails safe, and a pipeline that
records what happened instead of losing it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fw import requests_pr as rp  # noqa: E402
from fw.db import create_db_engine, init_db  # noqa: E402
from fw.stores import OrgStore, WorkflowRequestStore  # noqa: E402

ALICE = "00000000-0000-0000-0000-0000000000a1"


# ---------------------------------------------------------------------------
# Naming — these strings become file paths in the repository
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("title,expected", [
    ("Weekly supplier reconciliation", "weekly_supplier_reconciliation"),
    ("  Chase!! Overdue---Invoices  ", "chase_overdue_invoices"),
    ("2nd reminder run", "wf_2nd_reminder_run"),
    ("../../etc/passwd", "etc_passwd"),
    ("", "requested_workflow"),
    ("!!!", "requested_workflow"),
])
def test_module_names_are_safe_identifiers(title, expected):
    name = rp.module_name(title)
    assert name == expected
    assert name.isidentifier()
    assert "/" not in name and "\\" not in name and ".." not in name


def test_workflow_key_matches_module():
    assert rp.workflow_key("Chase Overdue") == "chase-overdue"


# ---------------------------------------------------------------------------
# Registry patch — deterministic surgery on shared state
# ---------------------------------------------------------------------------

REGISTRY_SRC = '''"""Workflow registry."""

from types import ModuleType

from . import overdue_chaseup, weekly_payrun

REGISTRY: dict[str, ModuleType] = {
    weekly_payrun.SPEC.key: weekly_payrun,
    overdue_chaseup.SPEC.key: overdue_chaseup,
}
'''


def test_registry_patch_inserts_import_and_entry():
    out = rp.patched_registry(REGISTRY_SRC, "supplier_recs")
    assert "from . import overdue_chaseup, supplier_recs, weekly_payrun" in out
    assert "    supplier_recs.SPEC.key: supplier_recs,\n}" in out
    # And the existing entries survived.
    assert "weekly_payrun.SPEC.key: weekly_payrun," in out


def test_registry_patch_refuses_an_unrecognised_file():
    """If the registry's shape has drifted, hand the job to the reviewer
    rather than committing a guess into shared state."""
    assert rp.patched_registry("something completely different", "x") is None


def test_registry_patch_is_idempotent_on_the_entry():
    once = rp.patched_registry(REGISTRY_SRC, "supplier_recs")
    twice = rp.patched_registry(once, "supplier_recs")
    assert twice.count("supplier_recs.SPEC.key") == 1


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------

@pytest.fixture
def engine(tmp_path):
    eng = create_db_engine(f"sqlite:///{tmp_path / 'req.db'}")
    init_db(eng)
    return eng


@pytest.fixture
def org(engine):
    return OrgStore(engine).create("Acme", ALICE, "alice@acme.test")


def test_requests_are_org_scoped(engine, org):
    other = OrgStore(engine).create("Globex",
                                    "00000000-0000-0000-0000-0000000000b1",
                                    "bob@globex.test")
    store = WorkflowRequestStore(engine)
    store.create(org, "Acme thing", "long enough description here",
                 "alice@acme.test")

    assert len(store.list(org)) == 1
    assert store.list(other) == []


def test_resolution_records_the_outcome(engine, org):
    store = WorkflowRequestStore(engine)
    rid = store.create(org, "A", "desc", "alice@acme.test")
    store.resolve(org, rid, pr_url="https://github.com/x/y/pull/1",
                  kind="generated", error=None)
    row = store.list(org)[0]
    assert row["status"] == "pr_opened"
    assert row["prUrl"].endswith("/pull/1")
    assert row["kind"] == "generated"


def test_failure_is_recorded_not_lost(engine, org):
    store = WorkflowRequestStore(engine)
    rid = store.create(org, "A", "desc", "alice@acme.test")
    store.resolve(org, rid, pr_url=None, kind=None, error="GitHub said no")
    row = store.list(org)[0]
    assert row["status"] == "failed"
    assert row["error"] == "GitHub said no"


# ---------------------------------------------------------------------------
# The pipeline, with GitHub and generation faked
# ---------------------------------------------------------------------------

class FakeGitHub:
    def __init__(self):
        self.files: dict[str, str] = {}
        self.branches: list[str] = []
        self.pr = None

    def default_branch(self):
        return "main"

    def create_branch(self, name, from_branch):
        self.branches.append(name)

    def get_file(self, path, ref):
        return REGISTRY_SRC, "sha123"

    def put_file(self, path, text, message, branch, sha=None):
        self.files[path] = text

    def open_pr(self, title, head, base, body):
        self.pr = {"title": title, "head": head, "base": base, "body": body}
        return "https://github.com/x/y/pull/7"


def test_without_generation_the_pr_carries_the_spec_alone(monkeypatch):
    fake = FakeGitHub()
    monkeypatch.setattr(rp, "GitHubClient", lambda: fake)
    monkeypatch.delenv("FW_ANTHROPIC_API_KEY", raising=False)

    out = rp.open_request_pr("Supplier recs", "Reconcile statements monthly.",
                             "Acme", "alice@acme.test", "abcd1234efgh")

    assert out == {"pr_url": "https://github.com/x/y/pull/7", "kind": "spec"}
    assert list(fake.files) == ["docs/workflow-requests/supplier_recs.md"]
    spec = fake.files["docs/workflow-requests/supplier_recs.md"]
    assert "Reconcile statements monthly." in spec
    assert "alice@acme.test" in spec
    assert fake.branches == ["request/supplier_recs-abcd1234"]


def test_with_generation_the_pr_carries_module_tests_and_registry(monkeypatch):
    fake = FakeGitHub()
    monkeypatch.setattr(rp, "GitHubClient", lambda: fake)
    monkeypatch.setenv("FW_ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(rp, "generate",
                        lambda *a: ("SPEC = None\n", "def test_ok(): pass\n"))

    out = rp.open_request_pr("Supplier recs", "Reconcile statements.",
                             "Acme", "alice@acme.test", "abcd1234efgh")

    assert out["kind"] == "generated"
    assert "worker/fw/workflows/supplier_recs.py" in fake.files
    assert "worker/tests/test_supplier_recs.py" in fake.files
    assert "supplier_recs.SPEC.key" in fake.files["worker/fw/workflows/__init__.py"]
    assert "never been executed" in fake.pr["body"]


def test_failed_generation_still_opens_the_spec_pr(monkeypatch):
    """The model failing must not swallow the request itself."""
    fake = FakeGitHub()
    monkeypatch.setattr(rp, "GitHubClient", lambda: fake)
    monkeypatch.setenv("FW_ANTHROPIC_API_KEY", "test-key")

    def boom(*a):
        raise rp.RequestError("model unavailable")

    monkeypatch.setattr(rp, "generate", boom)
    out = rp.open_request_pr("Supplier recs", "Reconcile statements.",
                             "Acme", "alice@acme.test", "abcd1234efgh")

    assert out["kind"] == "spec"
    assert fake.pr is not None
    assert "model unavailable" in fake.pr["body"]


def test_generated_code_must_at_least_be_python(monkeypatch):
    monkeypatch.setenv("FW_ANTHROPIC_API_KEY", "k")

    class Resp:
        status_code = 200

        def json(self):
            return {"content": [{"text":
                "<workflow_module>def broken(:</workflow_module>"
                "<test_module>pass</test_module>"}]}

    monkeypatch.setattr(rp.http, "post", lambda *a, **k: Resp())
    with pytest.raises(rp.RequestError, match="not valid Python"):
        rp.generate("T", "d", "mod", "mod")
