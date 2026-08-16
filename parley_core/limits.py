"""Classifying recoverable provider usage limits (PARLEY-V2-002L).

A limit is only worth waiting out if it is *temporally* recoverable. A missing
session does not become valid by sleeping, and an unsupported model never will.
So this module's job is mostly to say NO: everything it cannot positively
identify as a plan or rate limit is an ordinary failure.

Evidence, from `tests/fixtures/codex-0.147.0/` (real captures, see PROVENANCE.md):

    {"type":"turn.failed","error":{"message":"{\\"status\\":400,
      \\"error\\":{\\"type\\":\\"invalid_request_error\\", ...}}"}}

The `message` is a JSON *string* holding a nested payload with an HTTP status and
an error type. That is a structured discriminator, not prose, so detection sits
at precedence level 1 of the spec's contract.

EVIDENCE GRADES. No HTTP 429 exhaustion has been captured here -- forcing one
means deliberately burning a plan allowance -- so the 429 branch infers from the
proven envelope. Every LimitInfo therefore carries BOTH:

    source   = where the signal came from        (json)
    evidence = how strong that signal is         (observed | structural)

They are independent, because `source="json"` alone would let inference read as
capture. Regrade to `observed` and commit the fixture the first time a real
exhaustion occurs.

Prose-only stderr detection is deliberately NOT implemented: unlike the JSON
envelope, no stderr limit signature has captured evidence for its shape, and
matching unproven wording is how "any error" becomes "sleep for an hour".

This module classifies and computes wait bounds. It never sleeps and never
retries: PARLEY-V2-007 owns that, behind an explicit opt-in.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass

DETECTOR_VERSION = "codex/0.147.0/1"

_RATE_STATUS = 429
_LIMIT_TYPES = frozenset(
    {"rate_limit_error", "rate_limit_exceeded", "insufficient_quota"}
)

# Deliberately narrow. Broad matching is how "any error" becomes "sleep for an
# hour", which is the failure mode this whole design exists to avoid.
_RESET_HINT = re.compile(
    r"(?:try again|retry|resets?)\D{0,20}?(\d+)\s*(second|minute|hour)s?", re.IGNORECASE
)
_SECONDS = {"second": 1, "minute": 60, "hour": 3600}


@dataclass(frozen=True)
class LimitInfo:
    kind: str  # "plan" | "rate"
    reason: str
    source: str  # where the signal came from: "json"
    evidence: str  # how strong it is: "observed" | "structural"
    retry_after_seconds: int | None
    # Recorded when a payload carries one; never used for timing. No captured
    # evidence establishes its format, so deriving a wait from it would be
    # guessing dressed as provider instruction.
    reset_at: str | None
    detector_version: str

    def to_json(self) -> dict:
        return asdict(self)


def _payloads(stdout: str):
    """Yield every nested error payload found in the event stream."""
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        if evt.get("type") not in ("error", "turn.failed"):
            continue
        raw = evt.get("message")
        if raw is None:
            raw = (evt.get("error") or {}).get("message")
        if not isinstance(raw, str):
            continue
        try:
            yield json.loads(raw)  # the message is itself JSON
        except json.JSONDecodeError:
            yield {"message": raw}  # plain prose; still inspectable


def _retry_after(text: str) -> int | None:
    m = _RESET_HINT.search(text or "")
    if not m:
        return None
    return int(m.group(1)) * _SECONDS[m.group(2).lower()]


def classify(
    stdout: str, stderr: str = "", cli_version: str | None = None
) -> LimitInfo | None:
    """Return LimitInfo only for a positively identified recoverable limit.

    Everything else -- authentication, expired session, unsupported model,
    malformed invocation, network failure, arbitrary non-zero exit -- returns
    None and is handled as an ordinary failure. An exit code alone is never
    sufficient.
    """
    # 1. machine-readable, from the proven envelope
    for payload in _payloads(stdout or ""):
        status = payload.get("status")
        err = payload.get("error") or {}
        etype = (err.get("type") or "").lower()
        msg = err.get("message") or payload.get("message") or ""
        if status == _RATE_STATUS or etype in _LIMIT_TYPES:
            return LimitInfo(
                kind="plan" if "quota" in etype or "usage" in msg.lower() else "rate",
                reason=msg[:300] or f"status {status} {etype}".strip(),
                source="json",
                # The envelope is fixture-backed; this status value is not.
                evidence="structural",
                retry_after_seconds=_retry_after(msg),
                reset_at=payload.get("reset_at") or err.get("reset_at"),
                detector_version=DETECTOR_VERSION,
            )

    # 2. prose-only detection is unauthorised -- see the module docstring.

    # 3. not a limit
    return None


class WaitRefused(Exception):
    """A provider-directed delay exceeds a configured bound.

    Parley refuses rather than shortening it: retrying before the provider says
    the limit clears is not a smaller wait, it is a wasted call that re-fails.
    """


# Exactly the §11 bounds. Named here rather than inlined so a drift is visible.
BLIND_BASE = 300  # 5 minutes
BLIND_MULTIPLIER = 3
MAX_ONE_WAIT = 7200  # 2 hours
SAFETY_MARGIN = 5  # added to any provider-supplied time


def wait_seconds(
    info: LimitInfo,
    attempt: int,
    *,
    max_one_wait: int = MAX_ONE_WAIT,
    remaining_budget: int | None = None,
    base: int = BLIND_BASE,
    multiplier: int = BLIND_MULTIPLIER,
) -> int:
    """How long a caller MAY wait. Calculation only -- this never sleeps.

    A provider-directed delay wins over guesswork and gains a safety margin, but
    if it exceeds the per-wait cap or the remaining budget the wait is REFUSED,
    not truncated.
    """
    provider = info.retry_after_seconds
    if provider and provider > 0:
        delay = provider + SAFETY_MARGIN
        if delay > max_one_wait:
            raise WaitRefused(
                f"provider asks for {delay}s, above the {max_one_wait}s per-wait cap"
            )
        if remaining_budget is not None and delay > remaining_budget:
            raise WaitRefused(
                f"provider asks for {delay}s, above the {remaining_budget}s remaining budget"
            )
        return delay

    # Blind backoff: 5, 15, 45, 120, 120 ... minutes, clamped.
    delay = min(base * (multiplier ** max(0, attempt - 1)), max_one_wait)
    if remaining_budget is not None and delay > remaining_budget:
        raise WaitRefused(
            f"blind backoff of {delay}s exceeds the {remaining_budget}s remaining budget"
        )
    return delay


def is_blind(info: LimitInfo) -> bool:
    """True when any wait derived from this would be a guess, not instruction."""
    return not info.retry_after_seconds
