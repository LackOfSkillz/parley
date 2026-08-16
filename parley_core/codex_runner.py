"""The Codex driver: argv, subprocess, session extraction, answer preservation.

Everything Codex-specific lives here so nothing Codex-shaped leaks into the
generic contract in runners.py.

The binary path is INJECTED rather than resolved here. That keeps this module
pure with respect to PATH lookup and lets the caller decide, which is also what
allows the characterization tests to keep patching a single resolver.

Spec: sections 1 and 2.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from .models import (
    AccessPolicy,
    RunMetadata,
    RunnerSpec,
    RunResult,
    RunStatus,
)
from .runners import RunnerError, admit

# Keys the Codex event stream has used for the session identifier. The current
# CLI emits {"type":"thread.started","thread_id":...}; matching a set rather than
# one name keeps resume working across versions.
SESSION_KEYS = (
    "thread_id",
    "session_id",
    "conversation_id",
    "sessionId",
    "conversationId",
)

_POLICY_FLAG = {
    AccessPolicy.READ_ONLY: "read-only",
    AccessPolicy.WORKSPACE_WRITE: "workspace-write",
}


def find_session_id(stream: str) -> str | None:
    """Pull the session id out of the --json event stream.

    Scans every JSON object for any known session-id key at any depth, so a
    change to the event envelope degrades to "start a new thread" rather than
    silently breaking resume.
    """

    def walk(o: object) -> str | None:
        if isinstance(o, dict):
            for key in SESSION_KEYS:
                v = o.get(key)
                if isinstance(v, str) and v:
                    return v
            for v in o.values():
                hit = walk(v)
                if hit:
                    return hit
        elif isinstance(o, list):
            for v in o:
                hit = walk(v)
                if hit:
                    return hit
        return None

    for line in stream.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            hit = walk(json.loads(line))
        except json.JSONDecodeError:
            continue
        if hit:
            return hit
    return None


def build_argv(
    binary: str,
    project: Path,
    resume_id: str | None,
    model: str | None,
    out_file: Path,
    access_policy: AccessPolicy = AccessPolicy.READ_ONLY,
) -> list[str]:
    """Assemble the Codex argv.

    Exec-level options MUST precede the `resume` subcommand -- `resume` accepts
    only its own arguments, so a trailing `-s` is an "unexpected argument" error
    rather than an override. Placing the sandbox flag first pins it on fresh and
    resumed turns alike.
    """
    cmd = [binary, "exec", "-s", _POLICY_FLAG[access_policy]]
    if not resume_id:
        # `resume` restores the session's recorded working root; -C is only
        # meaningful (and only accepted) when starting fresh.
        cmd += ["-C", str(project)]
    if model:
        cmd += ["-m", model]
    if resume_id:
        cmd += ["resume", resume_id]
    cmd += ["--json", "-o", str(out_file), "--skip-git-repo-check", "-"]
    return cmd


def classify_failure(err: str, resume_id: str | None) -> str:
    """A hint for an operator, chosen by the most specific match first.

    Order matters: a usage error quotes "session" in its help text, which once
    made a malformed command look like an expired session.
    """
    low = err.lower()
    if "unexpected argument" in low or "usage:" in low:
        return "\n  Malformed codex invocation -- a bug in Parley, not your session."
    if "login" in low or "not logged in" in low or "unauthor" in low:
        return "\n  Run `codex login` and sign in with your ChatGPT account."
    if "rate" in low or "quota" in low or "limit" in low:
        return "\n  Plan allowance may be exhausted for this window."
    if resume_id and ("rollout" in low or "resume" in low or "not found" in low):
        return "\n  That stored session is gone -- rerun with --new-thread."
    return ""


class CodexRunner:
    """Runs one non-interactive Codex turn."""

    def __init__(
        self,
        spec: RunnerSpec,
        binary: str,
        timeout: int = 900,
        warn=None,
    ) -> None:
        self.spec = spec
        self.binary = binary
        self.timeout = timeout
        self._warn = warn or (lambda _msg: None)

    def run(
        self,
        prompt: str,
        cwd: Path,
        session: str | None,
        access_policy: AccessPolicy = AccessPolicy.READ_ONLY,
    ) -> RunResult:
        # Defence in depth: admission already ran, but a runner must never
        # execute a policy it cannot enforce even if called directly.
        admit(self.spec, access_policy)

        # mkstemp hands back an OPEN descriptor; close it before Codex writes to
        # the path, or the later unlink fails on Windows.
        fd, tmp_path = tempfile.mkstemp(prefix="parley-", suffix=".md")
        os.close(fd)
        out_file = Path(tmp_path)

        def discard() -> None:
            """Remove the temp file only when it holds nothing worth keeping."""
            try:
                out_file.unlink(missing_ok=True)
            except OSError as e:
                self._warn(f"could not remove temporary file {out_file}: {e}")

        argv = build_argv(
            self.binary, cwd, session, self.spec.model, out_file, access_policy
        )
        started = time.monotonic()
        try:
            proc = subprocess.run(
                argv,
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                check=False,  # a non-zero exit is classified below, not raised
                cwd=str(cwd),  # resume filters recorded sessions by cwd
            )
        except subprocess.TimeoutExpired:
            discard()
            raise RunnerError(
                f"codex timed out after {self.timeout}s (raise with --timeout)"
            ) from None
        except FileNotFoundError:
            discard()
            raise RunnerError("could not execute the codex CLI") from None
        elapsed = int((time.monotonic() - started) * 1000)

        # Reading and cleanup have different recovery requirements, so they are
        # NOT under one finally. If the read fails, the temp file may hold the
        # only copy of a completed answer and must survive.
        answer = ""
        if out_file.is_file():
            try:
                answer = out_file.read_text(encoding="utf-8", errors="replace").strip()
            except OSError as e:
                raise RunnerError(
                    f"codex finished but its output could not be read: {e}\n"
                    f"  The answer is retained at: {out_file}"
                ) from None
        discard()

        found = find_session_id(proc.stdout or "")
        if proc.returncode != 0:
            if answer:
                # Output despite a bad exit: keep it, never present it as clean.
                self._warn(
                    f"codex exited {proc.returncode} but produced output; marked partial"
                )
                return RunResult(
                    answer=answer,
                    session=found or session,
                    status=RunStatus.PARTIAL,
                    metadata=RunMetadata(
                        exit_code=proc.returncode, duration_ms=elapsed
                    ),
                )
            err = (proc.stderr or proc.stdout or "").strip()[:700]
            raise RunnerError(
                f"codex exited {proc.returncode}: {err}{classify_failure(err, session)}"
            )

        return RunResult(
            answer=answer or "(codex returned no final message)",
            session=found or session,
            status=RunStatus.COMPLETED,
            metadata=RunMetadata(exit_code=0, duration_ms=elapsed),
        )
