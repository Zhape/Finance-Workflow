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


def test_a_404_names_models_that_would_work(monkeypatch):
    """Being told a name is wrong without being told a right one is what turned
    a one-line fix into an afternoon."""
    monkeypatch.setattr(gemini.requests, "post", lambda *a, **k: FakeResponse(
        404, {"error": {"message": "gemini-2.5-flash is no longer available"}}))
    monkeypatch.setattr(gemini.requests, "get", lambda *a, **k: FakeResponse(
        200, {"models": [
            {"name": "models/gemini-3-flash",
             "supportedGenerationMethods": ["generateContent"]},
        ]}))

    client = gemini.GeminiClassifier(api_key="k", model="gemini-2.5-flash")
    with pytest.raises(ClassificationError) as caught:
        client.classify("s", "b", list(SYSTEM_CATEGORIES))

    assert "gemini-3-flash" in str(caught.value)


def test_the_default_model_is_discovered_not_hardcoded(monkeypatch):
    """A pinned name rots. This one shipped and was withdrawn weeks later."""
    monkeypatch.delenv("FW_GEMINI_MODEL", raising=False)
    monkeypatch.setenv("FW_GEMINI_API_KEY", "k")
    monkeypatch.setattr(gemini, "_discovered", None)
    monkeypatch.setattr(gemini.requests, "get", lambda *a, **k: FakeResponse(
        200, {"models": [
            {"name": "models/gemini-3-pro",
             "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-3-flash",
             "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/text-embedding-9",
             "supportedGenerationMethods": ["embedContent"]},
        ]}))

    assert gemini.model_name() == "gemini-3-flash", \
        "should prefer a flash-class model for a short labelling call"


def test_discovery_falls_back_when_the_api_cannot_be_asked(monkeypatch):
    monkeypatch.delenv("FW_GEMINI_MODEL", raising=False)
    monkeypatch.delenv("FW_GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(gemini, "_discovered", None)
    assert gemini.model_name() == gemini.DEFAULT_MODEL


def test_an_explicit_environment_override_still_wins(monkeypatch):
    monkeypatch.setenv("FW_GEMINI_MODEL", "pinned-model")
    monkeypatch.setattr(gemini, "_discovered", None)
    assert gemini.model_name() == "pinned-model"


def test_an_orgs_chosen_model_overrides_the_platform_default(monkeypatch):
    monkeypatch.setenv("FW_GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("FW_GEMINI_MODEL", "platform-default")

    assert gemini.client().model_version == "platform-default"
    assert gemini.client("org-choice").model_version == "org-choice"


def test_no_key_means_no_client(monkeypatch):
    """Absent a key the inbox degrades to manual rather than erroring per email."""
    monkeypatch.delenv("FW_GEMINI_API_KEY", raising=False)
    assert gemini.client() is None


# ---------------------------------------------------------------------------
# Falling through to another model when one runs out of quota
# ---------------------------------------------------------------------------

MODELS = [
    "gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite", "gemini-3.1-pro", "antigravity-preview-05-2026",
    "deep-research-pro-preview-12-2025", "text-embedding-004",
]


@pytest.fixture(autouse=True)
def fresh_cooldowns():
    gemini._cooling.clear()
    yield
    gemini._cooling.clear()


def _answer():
    return FakeResponse(200, {"candidates": [{"content": {"parts": [
        {"text": '{"category":"InvoiceQuery","confidence":0.9,"language":"en"}'}
    ]}}]})


def test_capable_models_lead_and_lite_models_back_them_up(monkeypatch):
    """Ordered by capability first, headroom second.

    The free tier's limits differ by an order of magnitude: the full Flash
    models allow roughly twenty requests a day, the Lite ones several hundred.
    One model cannot categorise a week of mail, so the good one leads and the
    cheap one finishes the job.
    """
    monkeypatch.setattr(gemini, "available_models", lambda *a, **k: MODELS)
    order = gemini.candidates()

    assert order[0] == "gemini-3.6-flash"
    assert "gemini-3.5-flash-lite" in order
    # A model that advertises generateContent but refuses a response schema is
    # unusable here: the schema is the whole guarantee.
    assert "antigravity-preview-05-2026" not in order
    assert "deep-research-pro-preview-12-2025" not in order
    assert "text-embedding-004" not in order
    # Lite comes after full flash, never before.
    assert order.index("gemini-3.5-flash") < order.index("gemini-3.5-flash-lite")


def test_an_orgs_chosen_model_leads_but_still_has_fallbacks(monkeypatch):
    monkeypatch.setattr(gemini, "available_models", lambda *a, **k: MODELS)
    order = gemini.candidates("gemini-3.1-flash-lite")

    assert order[0] == "gemini-3.1-flash-lite"
    assert len(order) > 1, "a pinned model should still have somewhere to fall"
    assert order.count("gemini-3.1-flash-lite") == 1


def test_a_rate_limited_model_falls_through_to_the_next(monkeypatch):
    """The sequencer: one model's quota must not stop the batch."""
    asked: list[str] = []

    def fake_post(url, **kwargs):
        model = url.split("/")[-1].split(":")[0]
        asked.append(model)
        if model == "gemini-3.6-flash":
            return FakeResponse(429, {"error": {
                "message": "Quota exceeded. Please retry in 33 seconds."}})
        return _answer()

    monkeypatch.setattr(gemini.requests, "post", fake_post)
    client = gemini.GeminiClassifier(
        api_key="k", models=["gemini-3.6-flash", "gemini-3.5-flash-lite"])

    result = client.classify("s", "b", list(SYSTEM_CATEGORIES))
    assert result["category"] == "InvoiceQuery"
    assert asked == ["gemini-3.6-flash", "gemini-3.5-flash-lite"]
    # The classification must record which model actually answered.
    assert client.model_version == "gemini-3.5-flash-lite"


def test_a_rested_model_is_not_asked_again_until_its_cooldown_passes(monkeypatch):
    asked: list[str] = []

    def fake_post(url, **kwargs):
        model = url.split("/")[-1].split(":")[0]
        asked.append(model)
        if model == "gemini-3.6-flash":
            return FakeResponse(429, {"error": {"message": "Quota exceeded."}})
        return _answer()

    monkeypatch.setattr(gemini.requests, "post", fake_post)
    models = ["gemini-3.6-flash", "gemini-3.5-flash-lite"]

    gemini.GeminiClassifier(api_key="k", models=models).classify(
        "s", "b", list(SYSTEM_CATEGORIES))
    asked.clear()

    # A second email skips the exhausted model rather than spending a request
    # rediscovering that it is exhausted.
    gemini.GeminiClassifier(api_key="k", models=models).classify(
        "s", "b", list(SYSTEM_CATEGORIES))
    assert asked == ["gemini-3.5-flash-lite"]


def test_a_rate_limit_does_not_open_the_circuit(monkeypatch):
    """A quota is not a fault. Opening the breaker would turn a pause of
    seconds into five minutes of refusing to try."""
    monkeypatch.setattr(gemini.requests, "post", lambda *a, **k: FakeResponse(
        429, {"error": {"message": "Quota exceeded."}}))
    client = gemini.GeminiClassifier(api_key="k", models=["gemini-3.6-flash"])

    with pytest.raises(ClassificationError) as caught:
        client.classify("s", "b", list(SYSTEM_CATEGORIES))

    assert caught.value.code == Code.CLS_RATE_LIMITED
    assert gemini.CIRCUIT.is_open is False
    assert gemini.CIRCUIT.failures == 0


def test_exhausting_every_model_names_what_was_tried(monkeypatch):
    monkeypatch.setattr(gemini.requests, "post", lambda *a, **k: FakeResponse(
        429, {"error": {"message": "Quota exceeded."}}))
    client = gemini.GeminiClassifier(
        api_key="k", models=["gemini-3.6-flash", "gemini-3.5-flash-lite"])

    with pytest.raises(ClassificationError) as caught:
        client.classify("s", "b", list(SYSTEM_CATEGORIES))

    message = str(caught.value)
    assert "gemini-3.6-flash" in message and "gemini-3.5-flash-lite" in message
