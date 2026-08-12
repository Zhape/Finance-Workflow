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


# Discovered once per process, so the default survives Google retiring a model
# without anyone editing an environment variable. A hardcoded name is a dated
# guess: `gemini-2.5-flash` shipped as the default here and was withdrawn from
# new keys weeks later, which cost a real sync twenty failed classifications.
_discovered: str | None = None


# Models known to advertise `generateContent` while refusing a response
# schema. The schema is the whole "no invented words" guarantee, so a model
# that cannot honour it is unusable here regardless of what the catalogue says.
_NOT_TEXT = (
    "embedding", "vision", "tts", "image", "audio", "live",
    "computer-use", "deep-research", "thinking", "antigravity",
)

# Per-model cooldowns. A 429 is about one model's quota, not the service, so
# it is recorded against that name and the next candidate is tried instead.
# Process-local and in-memory on purpose: it should reset on restart rather
# than persist a stale opinion about somebody's allowance.
_cooling: dict[str, float] = {}

# How long to rest a model when Google does not say.
DEFAULT_COOLDOWN = 60


def _rest(model: str, seconds: int | None) -> None:
    _cooling[model] = time.time() + (seconds or DEFAULT_COOLDOWN)


def _resting(model: str) -> bool:
    until = _cooling.get(model, 0)
    return until > time.time()


def _text_models(names: list[str]) -> list[str]:
    return [n for n in names
            if n.startswith("gemini-")
            and not any(word in n for word in _NOT_TEXT)]


def candidates(preferred: str | None = None) -> list[str]:
    """Models to try, in order, for one classification.

    Ordered by capability first and headroom second, because the free tier's
    limits differ by an order of magnitude between them: the full Flash models
    allow a few requests a minute and around twenty a day, while the Lite ones
    allow several times the rate and twenty-five times the daily volume. A
    single model is therefore not enough to categorise a week of mail — the
    good one runs out, and the cheap one finishes the job.

    An org's chosen model always leads; the rest are its fallbacks rather than
    a silent substitution, and the classification records which one answered.
    """
    try:
        available = _text_models(available_models())
    except Exception:  # noqa: BLE001 — discovery is best effort
        available = []

    if not available:
        return [preferred] if preferred else [DEFAULT_MODEL]

    flash = sorted([n for n in available if "flash" in n and "lite" not in n],
                   reverse=True)
    lite = sorted([n for n in available if "flash" in n and "lite" in n],
                  reverse=True)
    rest = sorted([n for n in available if "flash" not in n], reverse=True)

    ordered = flash + lite + rest
    if preferred:
        ordered = [preferred] + [n for n in ordered if n != preferred]
    return ordered


def _prefer(names: list[str]) -> str | None:
    """The single best model, for callers that want one name."""
    ordered = _text_models(names)
    flash = [n for n in ordered if "flash" in n and "lite" not in n]
    return (sorted(flash, reverse=True) or sorted(ordered, reverse=True)
            or [None])[0]


def model_name() -> str:
    """The model to use when the org has not chosen one.

    Order: an explicit environment override, then whatever the API key can
    actually reach, then the shipped constant as a last resort.
    """
    global _discovered
    override = os.environ.get("FW_GEMINI_MODEL", "").strip()
    if override:
        return override
    if _discovered:
        return _discovered
    try:
        _discovered = _prefer(available_models()) or DEFAULT_MODEL
    except Exception:  # noqa: BLE001 — discovery is best effort
        _discovered = DEFAULT_MODEL
    return _discovered


def status(model: str | None = None) -> dict[str, Any]:
    """What the settings screen and the degraded-mode banner read."""
    return {
        "configured": configured(),
        "model": model or model_name(),
        "circuitOpen": CIRCUIT.is_open,
        "consecutiveFailures": CIRCUIT.failures,
        # The whole chain, and which of them are resting on a quota. A person
        # seeing "classified by gemini-3.5-flash-lite" when they chose
        # something else deserves to know why without reading the log.
        "chain": candidates(model or _configured_model()) if configured() else [],
        "resting": sorted(n for n, until in _cooling.items()
                          if until > time.time()),
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


def _retry_after(resp) -> int | None:
    """How long Google says to wait, when it says so."""
    header = resp.headers.get("Retry-After") if hasattr(resp, "headers") else None
    if header and str(header).isdigit():
        return int(header)
    import re

    match = re.search(r"retry in (\d+)", _detail(resp), re.IGNORECASE)
    return int(match.group(1)) if match else None


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
    """Structured-output classification, over an ordered list of models.

    One model is not enough on a free-tier key: the capable ones allow roughly
    twenty requests a day, which does not cover a week of a finance inbox. So a
    429 moves to the next candidate rather than giving up, and the model that
    actually answered is recorded on the classification — an accuracy question
    later is unanswerable if the row says "gemini" and means "whichever one had
    quota left".
    """

    def __init__(self, api_key: str | None = None, model: str | None = None,
                 models: list[str] | None = None,
                 timeout: int = TIMEOUT_SECONDS):
        self._key = (api_key or os.environ.get("FW_GEMINI_API_KEY", "")).strip()
        self._candidates = models or candidates(model or _configured_model())
        self._used = self._candidates[0] if self._candidates else DEFAULT_MODEL
        self._timeout = timeout

    @property
    def model_version(self) -> str:
        """The model that answered, not the one we hoped would."""
        return self._used

    @property
    def candidates(self) -> list[str]:
        return list(self._candidates)

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
            because = f" The last failure was: {CIRCUIT.last_error}"                 if CIRCUIT.last_error else ""
            raise ClassificationError(
                f"Classification is paused after "
                f"{CIRCUIT.failures} consecutive failures.{because}",
                Code.CLS_UNAVAILABLE,
            )

        rested: list[str] = []
        for name in self._candidates:
            if _resting(name):
                rested.append(name)
                continue
            try:
                answer = self._ask(name, subject, body, categories)
            except _RateLimited as limit:
                # This model's allowance, not the service's. Rest it and try
                # the next one for this very email.
                _rest(name, limit.retry_after)
                rested.append(name)
                continue
            self._used = name
            CIRCUIT.record_success()
            return answer

        # Every candidate is resting. Not a fault to open the circuit over:
        # nothing is misconfigured and it resolves by waiting.
        raise ClassificationError(
            "Every available model is rate limited ("
            + ", ".join(rested[:6]) + ("…" if len(rested) > 6 else "")
            + "). The free tier allows only a few requests a minute and a "
              "few dozen a day per model; the rest of the batch was left "
              "untouched and the next sync will retry it.",
            Code.CLS_RATE_LIMITED,
        )

    def _ask(self, model: str, subject: str, body: str,
             categories: list[CategoryDef]) -> dict[str, Any]:
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

        try:
            resp = requests.post(
                f"{API_ROOT}/{model}:generateContent",
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

        if resp.status_code == 429:
            raise _RateLimited(_retry_after(resp))

        if resp.status_code != 200:
            # A 404 is not "service missing", it is "that model name is not
            # available to this key" — a configuration mistake with a one-line
            # fix, and worth its own code so it reads as one.
            code = (Code.CLS_BAD_MODEL if resp.status_code == 404
                    else Code.CLS_UNAVAILABLE)
            reason = (f"Gemini returned {resp.status_code} for model "
                      f"'{model}': {_detail(resp)}")
            if code == Code.CLS_BAD_MODEL:
                try:
                    usable = available_models(self._key)
                except ClassificationError:
                    usable = []
                if usable:
                    reason += (" Models this key can use: "
                               + ", ".join(usable[:8])
                               + ("…" if len(usable) > 8 else ""))
            CIRCUIT.record_failure(reason)
            raise ClassificationError(reason, code)

        try:
            text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
            parsed = json.loads(text)
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            reason = ("Gemini's answer was not the structured object it was "
                      "asked for.")
            CIRCUIT.record_failure(reason)
            raise ClassificationError(reason, Code.CLS_MALFORMED) from exc

        if not isinstance(parsed, dict):
            raise ClassificationError(
                "Gemini returned a non-object answer.", Code.CLS_MALFORMED
            )
        return parsed


class _RateLimited(Exception):
    """One model's quota, raised internally so the caller can try another."""

    def __init__(self, retry_after: int | None):
        super().__init__("rate limited")
        self.retry_after = retry_after


def _configured_model() -> str | None:
    """An explicit environment pin, if there is one."""
    return os.environ.get("FW_GEMINI_MODEL", "").strip() or None


def client(model: str | None = None) -> GeminiClassifier | None:
    """The configured classifier, or None when the platform has no key.

    `model` is the org's chosen name, which overrides the platform default so
    a wrong one can be corrected from the app rather than from the hosting
    dashboard followed by a redeploy."""
    return GeminiClassifier(model=model) if configured() else None
