"""The classifier client, and specifically how it reports going wrong.

A real sync produced three honest failures followed by twenty copies of
"degraded mode". The error list renders newest first, so every row a person
could see described the symptom and none named the cause. These tests exist so
that cannot recur: the reason a circuit opened travels with every error it
subsequently produces.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fw.inbox import gemini  # noqa: E402
from fw.inbox.classify import ClassificationError  # noqa: E402
from fw.inbox.models import Code, SYSTEM_CATEGORIES  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_circuit():
    """The circuit is process-global; a leaked failure count would make the
    next test lie."""
    gemini.CIRCUIT.record_success()
    yield
    gemini.CIRCUIT.record_success()


class FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


# ---------------------------------------------------------------------------
# The detail that was being thrown away
# ---------------------------------------------------------------------------

def test_googles_own_explanation_is_kept():
    """A 404 body names the model it objected to. Discarding it turned a
    one-line fix into a guessing game."""
    resp = FakeResponse(404, {"error": {
        "message": "models/gemini-9-ultra is not found for API version v1beta, "
                   "or is not supported for generateContent."
    }})
    assert "gemini-9-ultra" in gemini._detail(resp)


def test_a_non_json_body_still_yields_something():
    assert gemini._detail(FakeResponse(500, None, "upstream boom")) == \
        "upstream boom"


# ---------------------------------------------------------------------------
# The circuit remembers why it opened
# ---------------------------------------------------------------------------

def test_the_circuit_opens_only_after_repeated_failures():
    for _ in range(gemini.FAILURE_THRESHOLD - 1):
        gemini.CIRCUIT.record_failure("nope")
    assert gemini.CIRCUIT.is_open is False

    gemini.CIRCUIT.record_failure("nope")
    assert gemini.CIRCUIT.is_open is True


def test_a_paused_classifier_still_names_the_root_cause(monkeypatch):
    """The actual regression: email four onwards must not say only 'degraded'."""
    reason = "Gemini returned 404 for model 'gemini-9-ultra': not found"
    for _ in range(gemini.FAILURE_THRESHOLD):
        gemini.CIRCUIT.record_failure(reason)

    monkeypatch.setenv("FW_GEMINI_API_KEY", "test-key")
    client = gemini.GeminiClassifier(api_key="test-key")

    with pytest.raises(ClassificationError) as caught:
        client.classify("subject", "body", list(SYSTEM_CATEGORIES))

    message = str(caught.value)
    assert "paused" in message
    assert "gemini-9-ultra" in message, "the cause must travel with the symptom"
    assert caught.value.code == Code.CLS_UNAVAILABLE


def test_status_carries_the_last_error_to_the_screen():
    gemini.CIRCUIT.record_failure("Gemini returned 404 for model 'x'")
    assert "404" in gemini.status()["lastError"]


def test_a_success_clears_the_remembered_failure():
    gemini.CIRCUIT.record_failure("transient")
    gemini.CIRCUIT.record_success()
    assert gemini.CIRCUIT.last_error == ""
    assert gemini.CIRCUIT.failures == 0
    assert gemini.status()["lastError"] == ""


# ---------------------------------------------------------------------------
# A wrong model name is a configuration mistake, not an outage
# ---------------------------------------------------------------------------

def test_a_404_is_reported_as_a_model_problem(monkeypatch):
    def fake_post(*_args, **_kwargs):
        return FakeResponse(404, {"error": {
            "message": "models/nope is not found for API version v1beta"
        }})

    monkeypatch.setattr(gemini.requests, "post", fake_post)
    client = gemini.GeminiClassifier(api_key="test-key", model="nope")

    with pytest.raises(ClassificationError) as caught:
        client.classify("s", "b", list(SYSTEM_CATEGORIES))

    assert caught.value.code == Code.CLS_BAD_MODEL, \
        "a wrong model name should not read as a service outage"
    assert "nope" in str(caught.value)


def test_a_500_is_reported_as_unavailable(monkeypatch):
    monkeypatch.setattr(gemini.requests, "post",
                        lambda *a, **k: FakeResponse(500, None, "boom"))
    client = gemini.GeminiClassifier(api_key="test-key", model="fine")

    with pytest.raises(ClassificationError) as caught:
        client.classify("s", "b", list(SYSTEM_CATEGORIES))
    assert caught.value.code == Code.CLS_UNAVAILABLE


# ---------------------------------------------------------------------------
# Model discovery
# ---------------------------------------------------------------------------

def test_only_models_that_can_classify_are_offered(monkeypatch):
    """Listing includes embedding models, which cannot answer a prompt."""
    payload = {"models": [
        {"name": "models/gemini-2.5-flash",
         "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/text-embedding-004",
         "supportedGenerationMethods": ["embedContent"]},
        {"name": "models/gemini-2.5-pro",
         "supportedGenerationMethods": ["generateContent", "countTokens"]},
    ]}
    monkeypatch.setattr(gemini.requests, "get",
                        lambda *a, **k: FakeResponse(200, payload))

    assert gemini.available_models("test-key") == [
        "gemini-2.5-flash", "gemini-2.5-pro"
    ]


def test_listing_without_a_key_says_so(monkeypatch):
    monkeypatch.delenv("FW_GEMINI_API_KEY", raising=False)
    with pytest.raises(ClassificationError) as caught:
        gemini.available_models()
    assert "FW_GEMINI_API_KEY" in str(caught.value)


def test_an_orgs_chosen_model_overrides_the_platform_default(monkeypatch):
    monkeypatch.setenv("FW_GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("FW_GEMINI_MODEL", "platform-default")

    assert gemini.client().model_version == "platform-default"
    assert gemini.client("org-choice").model_version == "org-choice"


def test_no_key_means_no_client(monkeypatch):
    """Absent a key the inbox degrades to manual rather than erroring per email."""
    monkeypatch.delenv("FW_GEMINI_API_KEY", raising=False)
    assert gemini.client() is None
