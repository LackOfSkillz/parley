"""Immutable value types shared across Parley.

Everything here is a frozen dataclass or an enum, JSON-serialisable, and free of
behaviour. Nothing in this module imports a runner, storage, or the CLI: it is
the vocabulary the other modules speak, not a participant.

Spec: sections 1 and 2.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from .limits import LimitInfo
from enum import Enum


class AccessPolicy(str, Enum):
    """The complete v2 enumeration. There is no third, looser value.

    `danger-full-access`, approval bypasses and "best effort" sandboxing are
    deliberately not representable -- a policy Parley cannot name is a policy it
    cannot accidentally request.
    """

    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"


class RunStatus(str, Enum):
    COMPLETED = "completed"
    PARTIAL = "partial"  # non-zero exit that still produced a usable answer
    LIMITED = "limited"  # a temporally recoverable provider usage limit
    FAILED = "failed"  # no usable answer
    TIMED_OUT = "timed_out"  # kept distinct from other failure


@dataclass(frozen=True)
class RunnerCapabilities:
    """What an adapter has demonstrated it can request AND enforce.

    Declared in code by the trusted built-in adapter. Never supplied by CLI
    input, a registry entry or a transcript: a stored value claiming a
    capability must never be able to grant one.
    """

    persistent_sessions: bool
    read_only: bool
    workspace_write: bool

    def supports(self, policy: AccessPolicy) -> bool:
        if policy is AccessPolicy.READ_ONLY:
            return self.read_only
        if policy is AccessPolicy.WORKSPACE_WRITE:
            return self.workspace_write
        return False


@dataclass(frozen=True)
class RunnerSpec:
    """A resolved, trusted description of one participant's runner."""

    driver: str
    provider: str
    model: str | None
    capabilities: RunnerCapabilities

    def to_json(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class RunMetadata:
    """JSON-safe execution facts. Raw event streams are not persisted here."""

    exit_code: int | None = None
    duration_ms: int = 0
    diagnostic: str | None = None
    output_retained_at: str | None = None
    # Present if and only if status is LIMITED. A limit marker without evidence
    # would let an ordinary failure masquerade as something worth waiting for.
    limit: LimitInfo | None = None

    def to_json(self) -> dict:
        d = asdict(self)
        return d


@dataclass(frozen=True)
class RunResult:
    answer: str
    session: str | None
    status: RunStatus
    metadata: RunMetadata

    @property
    def partial(self) -> bool:
        return self.status is RunStatus.PARTIAL

    @property
    def usable(self) -> bool:
        return self.status in (RunStatus.COMPLETED, RunStatus.PARTIAL)
