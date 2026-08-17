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
# Every kind the v2 envelope permits, with the EXACT `data` keys each carries.
# Equality is enforced, not containment: a missing key is as wrong as an extra
# one, because a consumer reading an absent field cannot tell "not set" from
# "this producer forgot". Where the schema permits absence, the value is an
# explicit null.
RECORD_KINDS: dict[str, frozenset[str]] = {
    # -- consultation (§4)
    "consult.prompt": frozenset({"mode", "attached", "access_policy", "session_in"}),
    "consult.response": frozenset({"session_out", "run_status", "metadata"}),
    "consult.failure": frozenset({"error_type", "diagnostic", "retained_output_path"}),
    # -- orchestration (§4)
    "run.created": frozenset(
        {
            "source_commit",
            "worktree",
            "max_iterations",
            "allow_write",
            "maker_spec",
            "reviewer_spec",
        }
    ),
    "run.finished": frozenset({"outcome", "source_commit", "worktree", "diff_sha256"}),
    "steering.set": frozenset({"author", "supersedes"}),
    "steering.clear": frozenset({"author", "supersedes"}),
    "stop.requested": frozenset({"author"}),
    "state.changed": frozenset({"from", "to", "reason"}),
    "invocation.prompt": frozenset({"access_policy", "session_in", "diff_sha256"}),
    "invocation.response": frozenset(
        {"session_out", "run_status", "metadata", "diff_sha256"}
    ),
    "invocation.failure": frozenset(
        {"error_type", "diagnostic", "retained_output_path", "diff_sha256"}
    ),
    "review.verdict": frozenset(
        {"verdict", "summary", "required_changes", "diff_sha256"}
    ),
    # -- limits (§11)
    "invocation.limited": frozenset(
        {
            "logical_turn_id",
            "attempt",
            "kind",
            "reason",
            "source",
            "evidence",
            "retry_after_seconds",
            "reset_at",
            "detector_version",
        }
    ),
    "limit.wait": frozenset(
        {
            "logical_turn_id",
            "attempt",
            "detected_at",
            "reason",
            "source",
            "evidence",
            "blind",
            "resume_after",
            "wait_seconds",
            "cumulative_wait_seconds",
        }
    ),
    "limit.resumed": frozenset({"logical_turn_id", "next_attempt", "waited_seconds"}),
    "limit.exhausted": frozenset(
        {"logical_turn_id", "attempts", "cumulative_wait_seconds", "reason"}
    ),
}

# Kinds produced by a model invocation. These MUST carry a runner snapshot:
# a transcript that cannot say which runner produced a turn cannot support the
# provenance the whole project rests on.
MODEL_KINDS = frozenset(
    {
        "consult.prompt",
        "consult.response",
        "consult.failure",
        "invocation.prompt",
        "invocation.response",
        "invocation.failure",
        "invocation.limited",
        "review.verdict",
    }
)

PARTICIPANTS = frozenset({"maker", "reviewer", "human", "system"})


class SchemaError(ValueError):
    """A record does not satisfy the declared v2 envelope."""


class RegistryCorrupt(RuntimeError):
    """The authoritative v2 registry exists but cannot be read.

    Deliberately fatal rather than falling back to v1. v1 has no representation
    for runs or per-lane sessions, so a silent fallback would present a
    v2 conversation as though those had never existed -- losing exactly the
    state a run depends on, without saying so.
    """


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
    allowed = RECORD_KINDS[kind]
    extra = set(data) - allowed
    missing = allowed - set(data)
    if extra:
        raise SchemaError(
            f"{kind} carries undeclared data fields: {', '.join(sorted(extra))}"
        )
    if missing:
        # Absence is not permitted implicitly. A consumer reading an absent field
        # cannot distinguish "not set" from "this producer forgot".
        raise SchemaError(
            f"{kind} is missing required data fields: {', '.join(sorted(missing))}"
        )
    if participant in ("human", "system") and any((driver, provider, model)):
        raise SchemaError("human and system records must have null runner fields")
    if kind in MODEL_KINDS and not (driver and provider):
        # model may legitimately be None -- it means "the runner's configured
        # default". driver and provider cannot be: without them the record
        # cannot say what produced it.
        raise SchemaError(
            f"{kind} is model-generated and must carry a runner snapshot: "
            "driver and provider are required (model may be null)"
        )
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
        kind = record.get("kind")
        out["_display_role"] = record.get("participant")
        out["_error"] = kind in ("consult.failure", "invocation.failure")
        data = record.get("data") or {}
        # Limit evidence must reach the viewer whether it arrives as a dedicated
        # invocation.limited record or inside a response's metadata.
        if kind == "invocation.limited":
            out["_limit"] = {
                k: data.get(k)
                for k in (
                    "kind",
                    "reason",
                    "source",
                    "evidence",
                    "retry_after_seconds",
                    "reset_at",
                    "detector_version",
                )
            }
        elif isinstance(data.get("metadata"), dict) and data["metadata"].get("limit"):
            out["_limit"] = data["metadata"]["limit"]
        # An unknown kind from a future producer renders as a neutral system
        # note rather than being dropped or mislabelled as a participant turn.
        if kind not in RECORD_KINDS:
            out["_display_role"] = "system"
            out["_unknown_kind"] = kind
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
    """The authoritative v2 registry, importing v1 lazily only when v2 is ABSENT.

    A v2 registry that exists but cannot be parsed raises. Falling back to v1
    there would silently discard every v2-only run and lane session, because v1
    cannot represent them -- the user would see a conversation that looks intact
    and is not. Failing closed keeps the damage visible and recoverable from the
    transcripts, which are the durable record.
    """
    if v2_path.is_file():

        def corrupt(why: str):
            return RegistryCorrupt(
                f"{v2_path} {why}. Refusing to fall back to the v1 registry, which "
                "cannot represent runs or lane sessions. Move the file aside to "
                "start fresh; the transcripts in log/ are intact."
            )

        try:
            raw = v2_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            raise corrupt(f"could not be read: {e}") from None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise corrupt(f"is not valid JSON: {e}") from None
        # Validate the shape before returning it. Handing back a structurally
        # wrong registry is the same data loss as reading a corrupt one, just
        # deferred to whichever caller trips over it first.
        if not isinstance(data, dict):
            raise corrupt("is not a JSON object")
        if data.get("schema") != SCHEMA_V2:
            raise corrupt(f"does not declare schema {SCHEMA_V2}")
        if not isinstance(data.get("conversations"), dict):
            raise corrupt("has a missing or non-object 'conversations'")
        return data
    if v1_path.is_file():
        # v1 is a compatibility projection, not authoritative state: an
        # unreadable one degrades to empty rather than being fatal. Every read
        # failure is treated alike, not just malformed JSON.
        try:
            return import_v1(json.loads(v1_path.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return blank_v2()
        except AttributeError:
            return blank_v2()  # a non-object root
    return blank_v2()
