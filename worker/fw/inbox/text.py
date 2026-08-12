"""Turning a raw Gmail message into the words the customer just wrote.

Two cheap rules that both save money and prevent a class of bug:

  * **Suppression** happens before anything else. Out-of-office replies, bounce
    notices and bulk mail are recognised from their headers and never reach the
    classifier. That both stops a reply loop and removes the largest slice of
    per-email model spend on a typical finance inbox.

  * **Quote stripping** happens before classification. A sixty-message thread
    carries fifty-nine messages of history that were already classified when
    they arrived; paying to read them again on every reply is the difference
    between a workable per-email cost and an unworkable one.

Stripping is a heuristic, so both the raw and the stripped text are stored and
the review pane shows the classifier's actual input. A heuristic you can see is
debuggable; one you cannot is a mystery about why a category was wrong.
"""

from __future__ import annotations

import re

# Markers that begin quoted history. Everything from the first hit onwards is
# previous correspondence.
_QUOTE_MARKERS = [
    re.compile(r"^\s*On .{5,120}\bwrote:\s*$", re.IGNORECASE),
    re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}\s*$", re.IGNORECASE),
    re.compile(r"^\s*-{2,}\s*Forwarded message\s*-{2,}\s*$", re.IGNORECASE),
    re.compile(r"^\s*From:\s*.+$", re.IGNORECASE),
    re.compile(r"^\s*_{10,}\s*$"),
    re.compile(r"^\s*Sent from my \w+", re.IGNORECASE),
]

# Signature block. Kept separate because a signature is not history — it is
# just noise, and cutting at the first one is safe.
_SIGNATURE = re.compile(r"^\s*--\s*$")

_AUTO_SUBMITTED_OK = {"", "no"}

_BOUNCE_SENDERS = (
    "mailer-daemon@",
    "postmaster@",
    "no-reply@",
    "noreply@",
    "donotreply@",
)


def strip_quoted(body: str) -> str:
    """The new words in a reply, without the thread hanging off the bottom.

    Conservative on purpose: if stripping would leave nothing, the original is
    returned. A blank classifier input guarantees a wrong answer, whereas an
    over-long one merely costs a little more.
    """
    if not body:
        return ""

    lines = body.replace("\r\n", "\n").split("\n")
    kept: list[str] = []
    for line in lines:
        if any(marker.match(line) for marker in _QUOTE_MARKERS):
            break
        if _SIGNATURE.match(line):
            break
        # A run of quoted lines ends the new content just as reliably as a
        # marker, and many clients emit no marker at all.
        if line.startswith(">"):
            break
        kept.append(line)

    stripped = "\n".join(kept).strip()
    return stripped or body.strip()


def html_to_text(html: str) -> str:
    """Plain text from an HTML part, for messages that carry no text part.

    Quoted history in HTML mail lives in <blockquote>, so those are dropped
    outright before the tags are stripped.
    """
    if not html:
        return ""
    text = re.sub(r"<blockquote.*?</blockquote>", " ",
                  html, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<(script|style).*?</\1>", " ",
                  text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<br\s*/?>|</p>|</div>|</tr>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">")
                .replace("&quot;", '"').replace("&#39;", "'"))
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()


def suppression_reason(headers: dict[str, str], from_email: str,
                       own_addresses: set[str]) -> str | None:
    """Why this message should never reach the classifier, or None.

    Header-driven and hard-coded: this is a rule, not a judgement, and it must
    not depend on a model being reachable.
    """
    lower = {k.lower(): (v or "") for k, v in headers.items()}

    auto = lower.get("auto-submitted", "").strip().lower()
    if auto and auto not in _AUTO_SUBMITTED_OK:
        return "Automatic reply (Auto-Submitted header)"

    if lower.get("x-autoreply") or lower.get("x-autorespond"):
        return "Automatic reply"

    precedence = lower.get("precedence", "").strip().lower()
    if precedence in {"bulk", "auto_reply", "junk", "list"}:
        return f"Bulk or automated mail (Precedence: {precedence})"

    if lower.get("list-unsubscribe") or lower.get("list-id"):
        return "Mailing list message"

    if lower.get("x-failed-recipients") or "report-type=delivery-status" in \
            lower.get("content-type", "").lower():
        return "Delivery failure notice"

    sender = (from_email or "").strip().lower()
    if any(sender.startswith(prefix) for prefix in _BOUNCE_SENDERS):
        return "Automated sender"

    if sender and sender in {a.lower() for a in own_addresses}:
        return "Sent from a connected mailbox"

    return None


def display_name(from_header: str) -> tuple[str | None, str]:
    """Split a From header into (display name, address)."""
    raw = (from_header or "").strip()
    match = re.match(r"^\s*(.*?)\s*<([^>]+)>\s*$", raw)
    if match:
        name = match.group(1).strip().strip('"').strip()
        return (name or None), match.group(2).strip().lower()
    return None, raw.lower()
