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
    """

    def __init__(self) -> None:
        self.failures = 0
        self.opened_at = 0.0

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

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= FAILURE_THRESHOLD:
            self.opened_at = time.time()


CIRCUIT = _Circuit()


def configured() -> bool:
    return bool(os.environ.get("FW_GEMINI_API_KEY", "").strip())


def model_name() -> str:
    return os.environ.get("FW_GEMINI_MODEL", "").strip() or DEFAULT_MODEL


def status() -> dict[str, Any]:
    """What the settings screen and the degraded-mode banner read."""
    return {
        "configured": configured(),
        "model": model_name(),
        "circuitOpen": CIRCUIT.is_open,
        "consecutiveFailures": CIRCUIT.failures,
    }


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
            raise ClassificationError(
                "Classification is in degraded mode after repeated failures.",
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
            CIRCUIT.record_failure()
            raise ClassificationError(
                f"Gemini did not answer within {self._timeout}s.",
                Code.CLS_TIMEOUT,
            ) from exc
        except requests.RequestException as exc:
            CIRCUIT.record_failure()
            raise ClassificationError(
                f"Could not reach Gemini: {type(exc).__name__}.",
                Code.CLS_UNAVAILABLE,
            ) from exc

        if resp.status_code != 200:
            CIRCUIT.record_failure()
            raise ClassificationError(
                f"Gemini returned {resp.status_code}.", Code.CLS_UNAVAILABLE
            )

        try:
            text = (resp.json()["candidates"][0]["content"]["parts"][0]["text"])
            parsed = json.loads(text)
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            CIRCUIT.record_failure()
            raise ClassificationError(
                "Gemini's answer was not the structured object it was asked "
                "for.",
                Code.CLS_MALFORMED,
            ) from exc

        CIRCUIT.record_success()
        if not isinstance(parsed, dict):
            raise ClassificationError(
                "Gemini returned a non-object answer.", Code.CLS_MALFORMED
            )
        return parsed


def client() -> GeminiClassifier | None:
    """The configured classifier, or None when the platform has no key."""
    return GeminiClassifier() if configured() else None
