"""The one implementation of ClassificationClient.

Gemini is asked for structured output: a response schema pinning the answer to
an object whose `category` is an enum. That is what makes prose unrepresentable
rather than merely discouraged — the constraint is in the request, not in the
wording of a prompt.

A circuit breaker sits in front of it. Repeated failures stop the calls rather
than making every sync wait for the same timeout twenty times over; the inbox
falls into degraded mode, every email routes to a person, and a banner says so.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import requests

from .classify import ClassificationError, prompt, response_schema
from .models import CategoryDef, Code

API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"
DEFAULT_MODEL = "gemini-2.5-flash"

# Per-email budget. The functional requirement is a category within 30 seconds;
# this leaves room for the Xero lookup that follows.
TIMEOUT_SECONDS = 20

# Consecutive failures before the circuit opens, and how long it stays open.
FAILURE_THRESHOLD = 3
COOLDOWN_SECONDS = 300


class _Circuit:
    """Open after repeated failures, half-open after a cooldown.

    Deliberately process-local and in-memory: it protects one worker from
    hammering a service that is already failing, and it should reset on
    restart rather than persist a stale opinion about a model's health.

    It remembers *why* it opened. Without that, a run of failures produces one
    honest error followed by twenty copies of "degraded mode", the queue shows
    the newest first, and the only rows that explain anything are the ones
    pushed off the bottom of the screen.
    """

    def __init__(self) -> None:
        self.failures = 0
        self.opened_at = 0.0
        self.last_error = ""

    @property
    def is_open(self) -> bool:
        if self.failures < FAILURE_THRESHOLD:
            return False
        if time.time() - self.opened_at > COOLDOWN_SECONDS:
            return False        # half-open: let one call through to test
        return True

    def record_success(self) -> None:
        self.failures = 0
        self.opened_at = 0.0
        self.last_error = ""

    def record_failure(self, reason: str = "") -> None:
        self.failures += 1
        if reason:
            self.last_error = reason
        if self.failures >= FAILURE_THRESHOLD:
            self.opened_at = time.time()


CIRCUIT = _Circuit()


def configured() -> bool:
    return bool(os.environ.get("FW_GEMINI_API_KEY", "").strip())


def model_name() -> str:
    return os.environ.get("FW_GEMINI_MODEL", "").strip() or DEFAULT_MODEL


def status(model: str | None = None) -> dict[str, Any]:
    """What the settings screen and the degraded-mode banner read."""
    return {
        "configured": configured(),
        "model": model or model_name(),
        "circuitOpen": CIRCUIT.is_open,
        "consecutiveFailures": CIRCUIT.failures,
        # The reason, carried to the screen. A banner saying only "degraded"
        # tells someone they have a problem without telling them which one.
        "lastError": CIRCUIT.last_error,
    }


def _detail(resp) -> str:
    """Google's own explanation of a failure.

    Worth extracting rather than reporting the bare status code: a 404 from
    this API means "that model name is not available to this key", and the
    body says exactly which name it objected to. Discarding it turns a
    two-second fix into a guessing game.
    """
    try:
        message = ((resp.json() or {}).get("error") or {}).get("message")
        if message:
            return str(message)[:400]
    except ValueError:
        pass
    return (resp.text or "")[:400]


def available_models(api_key: str | None = None) -> list[str]:
    """Models this key can actually use for classification.

    Offered because the alternative is guessing at model names against someone
    else's API key, which is what produced a wall of 404s.
    """
    key = (api_key or os.environ.get("FW_GEMINI_API_KEY", "")).strip()
    if not key:
        raise ClassificationError(
            "No Gemini API key is configured on the worker "
            "(FW_GEMINI_API_KEY).",
            Code.CLS_UNAVAILABLE,
        )
    try:
        resp = requests.get(API_ROOT, headers={"x-goog-api-key": key},
                            params={"pageSize": 200}, timeout=TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise ClassificationError(
            f"Could not reach Gemini: {type(exc).__name__}.",
            Code.CLS_UNAVAILABLE,
        ) from exc
    if resp.status_code != 200:
        raise ClassificationError(
            f"Gemini refused to list models ({resp.status_code}): "
            f"{_detail(resp)}",
            Code.CLS_UNAVAILABLE,
        )
    out: list[str] = []
    for model in (resp.json().get("models") or []):
        methods = model.get("supportedGenerationMethods") or []
        if "generateContent" not in methods:
            continue
        name = str(model.get("name") or "")
        out.append(name[len("models/"):] if name.startswith("models/") else name)
    return sorted(out)


class GeminiClassifier:
    """Structured-output classification. Holds no per-org state."""

    def __init__(self, api_key: str | None = None, model: str | None = None,
                 timeout: int = TIMEOUT_SECONDS):
        self._key = (api_key or os.environ.get("FW_GEMINI_API_KEY", "")).strip()
        self._model = model or model_name()
        self._timeout = timeout

    @property
    def model_version(self) -> str:
        return self._model

    def classify(self, subject: str, body: str,
                 categories: list[CategoryDef]) -> dict[str, Any]:
        if not self._key:
            raise ClassificationError(
                "No Gemini API key is configured on the worker "
                "(FW_GEMINI_API_KEY).",
                Code.CLS_UNAVAILABLE,
            )
        if CIRCUIT.is_open:
            # Carry the original reason. Every email after the third otherwise
            # records an error that describes the symptom and hides the cause.
            because = f" The last failure was: {CIRCUIT.last_error}" \
                if CIRCUIT.last_error else ""
            raise ClassificationError(
                f"Classification is paused after "
                f"{CIRCUIT.failures} consecutive failures.{because}",
                Code.CLS_UNAVAILABLE,
            )

        payload = {
            "contents": [{
                "role": "user",
                "parts": [{"text": prompt(subject, body, categories)}],
            }],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": response_schema(categories),
                # Classification, not composition. Nothing here benefits from
                # sampling variety, and a stable answer makes an accuracy
                # regression attributable to the model rather than to luck.
                "temperature": 0,
            },
        }

        url = f"{API_ROOT}/{self._model}:generateContent"
        try:
            resp = requests.post(
                url,
                headers={"x-goog-api-key": self._key,
                         "Content-Type": "application/json"},
                json=payload,
                timeout=self._timeout,
            )
        except requests.Timeout as exc:
            reason = f"Gemini did not answer within {self._timeout}s."
            CIRCUIT.record_failure(reason)
            raise ClassificationError(reason, Code.CLS_TIMEOUT) from exc
        except requests.RequestException as exc:
            reason = f"Could not reach Gemini: {type(exc).__name__}."
            CIRCUIT.record_failure(reason)
            raise ClassificationError(reason, Code.CLS_UNAVAILABLE) from exc

        if resp.status_code != 200:
            # A 404 here is not "service missing", it is "that model name is
            # not available to this key" — a configuration mistake with a
            # one-line fix, and worth its own code so it reads as one.
            code = (Code.CLS_BAD_MODEL if resp.status_code == 404
                    else Code.CLS_UNAVAILABLE)
            reason = (f"Gemini returned {resp.status_code} for model "
                      f"'{self._model}': {_detail(resp)}")
            CIRCUIT.record_failure(reason)
            raise ClassificationError(reason, code)

        try:
            text = (resp.json()["candidates"][0]["content"]["parts"][0]["text"])
            parsed = json.loads(text)
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            reason = "Gemini's answer was not the structured object it was asked for."
            CIRCUIT.record_failure(reason)
            raise ClassificationError(reason, Code.CLS_MALFORMED) from exc

        CIRCUIT.record_success()
        if not isinstance(parsed, dict):
            raise ClassificationError(
                "Gemini returned a non-object answer.", Code.CLS_MALFORMED
            )
        return parsed


def client(model: str | None = None) -> GeminiClassifier | None:
    """The configured classifier, or None when the platform has no key.

    `model` is the org's chosen name, which overrides the platform default so
    a wrong one can be corrected from the app rather than from the hosting
    dashboard followed by a redeploy."""
    return GeminiClassifier(model=model) if configured() else None
