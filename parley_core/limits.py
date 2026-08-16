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

HONEST GAP: no HTTP 429 exhaustion has been captured on this machine -- forcing
one means deliberately burning a plan allowance. The 429 branch is therefore
supported by structural analogy to the proven envelope and is NOT
empirically confirmed. `LimitInfo.source` records which of those it was, so a
transcript never overstates its own evidence.

This module classifies and computes wait bounds. It never sleeps and never
retries: PARLEY-V2-007 owns that, behind an explicit opt-in.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass

DETECTOR_VERSION = "codex/0.147.0/1"

# Observed CLI versions this detector has been checked against. An unknown
# version may still use the machine-readable path -- the envelope is stable
# enough to parse -- but must NOT inherit the stderr heuristic unverified.
VERIFIED_VERSIONS = frozenset({"0.147.0"})

_RATE_STATUS = 429
_LIMIT_TYPES = frozenset(
    {"rate_limit_error", "rate_limit_exceeded", "insufficient_quota"}
)

# Deliberately narrow. Broad matching is how "any error" becomes "sleep for an
# hour", which is the failure mode this whole design exists to avoid.
_STDERR_SIGNATURE = re.compile(
    r"\b(rate limit(?:ed)?|usage limit|quota exceeded|too many requests)\b",
    re.IGNORECASE,
)
_RESET_HINT = re.compile(
    r"(?:try again|retry|resets?)\D{0,20}?(\d+)\s*(second|minute|hour)s?", re.IGNORECASE
)
_SECONDS = {"second": 1, "minute": 60, "hour": 3600}


@dataclass(frozen=True)
class LimitInfo:
    kind: str  # "plan" | "rate"
    reason: str
    source: str  # "json" | "stderr_heuristic"
    retry_after_seconds: int | None
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
    stdout: str, stderr: str, cli_version: str | None = None
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
                retry_after_seconds=_retry_after(msg),
                reset_at=payload.get("reset_at") or err.get("reset_at"),
                detector_version=DETECTOR_VERSION,
            )

    # 2. version-pinned stderr signature. Refused for unverified CLI versions:
    #    prose is not a contract, and inheriting it blindly would let an unknown
    #    build's unrelated wording trigger unattended sleeping.
    if cli_version in VERIFIED_VERSIONS:
        text = stderr or ""
        if _STDERR_SIGNATURE.search(text):
            return LimitInfo(
                kind="rate",
                reason=text.strip()[:300],
                source="stderr_heuristic",
                retry_after_seconds=_retry_after(text),
                reset_at=None,
                detector_version=DETECTOR_VERSION,
            )

    # 3. not a limit
    return None


def wait_seconds(
    info: LimitInfo,
    attempt: int,
    *,
    max_single_wait: int = 3600,
    base_backoff: int = 60,
) -> int:
    """How long a caller MAY wait, bounded. This function does not sleep.

    A provider-supplied retry-after is trusted over guesswork, but is still
    clamped: a malformed or hostile value must not translate into an unbounded
    unattended sleep.
    """
    if info.retry_after_seconds and info.retry_after_seconds > 0:
        return min(info.retry_after_seconds, max_single_wait)
    # Blind exponential backoff. Explicitly a guess -- the caller must surface
    # that this wait is not provider-directed.
    return min(base_backoff * (2 ** max(0, attempt - 1)), max_single_wait)


def is_blind(info: LimitInfo) -> bool:
    """True when any wait derived from this would be a guess, not instruction."""
    return not info.retry_after_seconds
