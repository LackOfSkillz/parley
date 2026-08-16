"""The runner protocol, trusted resolution, and capability admission.

This module owns the rule that access is refused BEFORE a process launches. It
knows nothing about Codex argv or any other driver's mechanics -- a driver
detail leaking in here is the failure mode this boundary exists to prevent.

Spec: sections 1 and 2.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from .models import AccessPolicy, RunnerCapabilities, RunnerSpec, RunResult


class RunnerError(RuntimeError):
    """A runner could not produce a result.

    Raised rather than returning a guessed one. When a possible answer exists on
    disk but could not be read, the message names the retained path, because a
    completed answer must never be silently discarded.
    """


class CapabilityError(RunnerError):
    """The requested access policy exceeds what the adapter can enforce."""


class Runner(Protocol):
    """The exact interface every driver implements."""

    spec: RunnerSpec

    def run(
        self,
        prompt: str,
        cwd: Path,
        session: str | None,
        access_policy: AccessPolicy,
    ) -> RunResult: ...


# Capabilities are declared HERE, in code, by driver name. They are not read
# from configuration and cannot be overridden at runtime.
_DECLARED: dict[str, RunnerCapabilities] = {
    "codex": RunnerCapabilities(
        persistent_sessions=True,
        read_only=True,
        # Not yet demonstrated. PARLEY-V2-006 must prove Codex enforces
        # workspace-write at a dedicated worktree before this becomes True;
        # claiming it early would let an inadmissible run be admitted.
        workspace_write=False,
    ),
}

_PROVIDERS: dict[str, frozenset[str]] = {
    "codex": frozenset({"openai", "ollama", "lmstudio"}),
}


def declared_capabilities(driver: str) -> RunnerCapabilities:
    try:
        return _DECLARED[driver]
    except KeyError:
        raise RunnerError(f"unknown driver: {driver!r}") from None


def resolve_spec(
    driver: str = "codex", provider: str = "openai", model: str | None = None
) -> RunnerSpec:
    """Turn user-supplied driver/provider/model into a trusted RunnerSpec.

    The caller chooses driver, provider and model. Capabilities are attached
    from the code-declared table, never from the caller.
    """
    caps = declared_capabilities(driver)
    allowed = _PROVIDERS.get(driver, frozenset())
    if provider not in allowed:
        raise RunnerError(
            f"driver {driver!r} does not support provider {provider!r}; "
            f"supported: {', '.join(sorted(allowed))}"
        )
    return RunnerSpec(driver=driver, provider=provider, model=model, capabilities=caps)


def admit(spec: RunnerSpec, access_policy: AccessPolicy) -> None:
    """Refuse an access policy the adapter cannot enforce.

    Called at admission and again defensively inside the runner. No subprocess
    launches and no prompt record is written for an inadmissible policy.
    """
    if not spec.capabilities.supports(access_policy):
        raise CapabilityError(
            f"runner {spec.driver}/{spec.provider} cannot enforce "
            f"access policy {access_policy.value!r}"
        )
