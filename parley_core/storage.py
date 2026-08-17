"""Storage: v2 codecs, lazy v1 import, and the v1 compatibility projection.

Two schemas coexist here permanently. v1 is not a legacy format to be migrated
away from -- the bare `--mode` CLI form is supported forever, and it writes v1
records. So this module's job is to let both live side by side without either
corrupting the other, and above all without rewriting history: an append-only
transcript that gets rewritten is no longer evidence of anything.

Spec: sections 4 and 5.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_V2 = 2

# Every kind the v2 envelope permits, with the exact `data` keys each carries.
# Declared rather than free-form: an undeclared field in `data` is how a schema
# quietly becomes "whatever the last writer felt like".
RECORD_KINDS: dict[str, tuple[str, ...]] = {
    "consult.prompt": ("mode", "attached", "access_policy", "session_in"),
    "consult.response": ("session_out", "run_status", "metadata"),
    "consult.failure": ("error_type", "diagnostic", "retained_output_path"),
    "run.created": (
        "source_commit",
        "worktree",
        "max_iterations",
        "allow_write",
        "maker_spec",
        "reviewer_spec",
    ),
    "steering.set": ("author", "supersedes"),
    "steering.clear": ("author", "supersedes"),
    "stop.requested": ("author",),
    "state.changed": ("from", "to", "reason"),
    "invocation.prompt": ("access_policy", "session_in", "diff_sha256"),
}

PARTICIPANTS = frozenset({"maker", "reviewer", "human", "system"})


class SchemaError(ValueError):
    """A record does not satisfy the declared v2 envelope."""


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------- records
def make_record(
    *,
    conversation_id: str,
    project: str,
    thread: str,
    kind: str,
    participant: str,
    data: dict | None = None,
    text: str | None = None,
    run_id: str | None = None,
    driver: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    lane_turn: int | None = None,
    iteration: int | None = None,
    steering_id: str | None = None,
    event_id: str | None = None,
    ts: str | None = None,
) -> dict:
    """Build a v2 record, refusing anything the schema does not declare."""
    if kind not in RECORD_KINDS:
        raise SchemaError(f"unknown record kind: {kind!r}")
    if participant not in PARTICIPANTS:
        raise SchemaError(f"unknown participant: {participant!r}")
    data = dict(data or {})
    allowed = set(RECORD_KINDS[kind])
    extra = set(data) - allowed
    if extra:
        raise SchemaError(
            f"{kind} carries undeclared data fields: {', '.join(sorted(extra))}"
        )
    if participant in ("human", "system") and any((driver, provider, model)):
        raise SchemaError("human and system records must have null runner fields")
    return {
        "schema": SCHEMA_V2,
        "event_id": event_id or str(uuid.uuid4()),
        "ts": ts or now(),
        "conversation_id": conversation_id,
        "project": project,
        "thread": thread,
        "run_id": run_id,
        "kind": kind,
        "participant": participant,
        "driver": driver,
        "provider": provider,
        "model": model,
        "lane_turn": lane_turn,
        "iteration": iteration,
        "steering_id": steering_id,
        "text": text,
        "data": data,
    }


def is_v2(record: dict) -> bool:
    return record.get("schema") == SCHEMA_V2


def normalise(record: dict) -> dict:
    """Present a v1 or v2 record in one shape the viewer can render.

    v1 records are NOT rewritten on disk -- this is a read-time projection.
    The v1 vendor roles are mapped to lanes, because "claude"/"gpt" stop being
    meaningful the moment either side is selectable.
    """
    if is_v2(record):
        out = dict(record)
        out["_display_role"] = record.get("participant")
        return out

    # v1: {ts, role, mode?, turn, project, thread, session_id?, partial?, error?, text}
    role = record.get("role")
    participant = "maker" if role == "claude" else "reviewer"
    return {
        "schema": 1,
        "event_id": None,
        "ts": record.get("ts"),
        "conversation_id": None,
        "project": record.get("project"),
        "thread": record.get("thread"),
        "run_id": None,
        "kind": "consult.failure"
        if record.get("error")
        else "consult.prompt"
        if role == "claude"
        else "consult.response",
        "participant": participant,
        # v1 never recorded which runner produced a turn, and inventing one would
        # be worse than admitting the gap.
        "driver": None,
        "provider": None,
        "model": None,
        "lane_turn": record.get("turn"),
        "iteration": None,
        "steering_id": None,
        "text": record.get("text"),
        "data": {
            k: v
            for k, v in (
                ("mode", record.get("mode")),
                ("attached", record.get("attached")),
                ("session_out", record.get("session_id")),
                ("partial", record.get("partial")),
            )
            if v is not None
        },
        "_display_role": participant,
        "_legacy_role": role,
        "_error": bool(record.get("error")),
    }


def read_transcript(path: Path) -> list[dict]:
    """Every record in a transcript, normalised, in file order.

    Mixed v1/v2 files are expected: a conversation that predates v2 and then
    continues under it lives in one file. Order is the file's order -- these are
    append-only logs, so position IS chronology, and re-sorting by timestamp
    would reorder records that share a whole-second stamp.
    """
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(normalise(json.loads(line)))
        except json.JSONDecodeError:
            continue  # a torn final line must not hide the rest of the history
    return out


# ------------------------------------------------------------------ registry
def blank_v2() -> dict:
    return {"schema": SCHEMA_V2, "conversations": {}}


def import_v1(v1: dict) -> dict:
    """Project a v1 registry into an in-memory v2 one. Lazy and non-destructive.

    The v1 file is never modified. v1 did not persist enough evidence to
    reconstruct the runner identity a session was created under, so
    `session_origin` is null rather than guessed.
    """
    out = blank_v2()
    for cid, e in (v1 or {}).items():
        out["conversations"][cid] = {
            "project": e.get("project"),
            "thread": e.get("thread"),
            "created_at": e.get("created_at"),
            "updated_at": e.get("updated_at"),
            "consult": {
                "turns": e.get("turns", 0),
                "reviewer": {
                    "runner": {
                        "driver": "codex",
                        "provider": "openai",
                        "model": None,
                        "capabilities": None,
                    },
                    "session": e.get("session_id"),
                    "session_origin": None,
                    "invocations": e.get("turns", 0),
                },
            },
            "runs": {},
        }
    return out


def project_v1(v2: dict) -> dict:
    """The exact six-field v1 entry, so the bare form keeps working forever."""
    out = {}
    for cid, c in (v2.get("conversations") or {}).items():
        consult = c.get("consult") or {}
        reviewer = consult.get("reviewer") or {}
        out[cid] = {
            "session_id": reviewer.get("session"),
            "project": c.get("project"),
            "thread": c.get("thread"),
            "turns": consult.get("turns", 0),
            "created_at": c.get("created_at"),
            "updated_at": c.get("updated_at"),
        }
    return out


def write_atomic(path: Path, payload: dict) -> None:
    """Replace a registry in one step.

    A half-written registry loses session continuity for every conversation in
    it, so the new content is written beside the target and moved into place.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".parley-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)  # atomic on both POSIX and Windows
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def load_registry(v2_path: Path, v1_path: Path) -> dict:
    """The authoritative v2 registry, importing v1 lazily when v2 is absent."""
    if v2_path.is_file():
        try:
            data = json.loads(v2_path.read_text(encoding="utf-8"))
            if data.get("schema") == SCHEMA_V2:
                return data
        except json.JSONDecodeError:
            pass  # fall through to v1 rather than losing every session
    if v1_path.is_file():
        try:
            return import_v1(json.loads(v1_path.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            return blank_v2()
    return blank_v2()
