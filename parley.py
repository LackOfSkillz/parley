"""Direct Claude <-> GPT consultation, driven through the Codex CLI.

Removes the human copy-paste step from a dispatch loop:

    GPT designs dispatch -> Claude implements -> GPT reviews -> repeat

Uses `codex exec`, so it authenticates with a ChatGPT account and draws on the
plan's included allowance. There is no API key and no per-token billing.

WHY CODEX RATHER THAN THE API
-----------------------------
Codex reads the repository itself under a read-only sandbox, so a review can
consult whatever files it needs instead of being handed a guessed-at bundle of
context. This tool consults; it never edits. A dispatch it drafts is a proposal
for a human to accept.

PROJECT + CONVERSATION AWARENESS
--------------------------------
A conversation is keyed by (project, thread). The key is canonicalised -- real
path, OS-normalised case -- and hashed, so two spellings of the same directory
resolve to one conversation, and the chance of two different ones colliding is
negligible (128-bit digest) rather than merely unlikely.
That single id names both the registry entry and the transcript file, so the two
cannot drift apart.

Stdlib only.

Usage:
    python parley.py --mode review --input report.md --project ~/code/my-project
    python parley.py --mode design --input state.md --thread research
    python parley.py --list                 show every thread
    python parley.py --new-thread ...       start the thread over
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from parley_core import codex_runner, runners
from parley_core.models import AccessPolicy

HERE = Path(__file__).resolve().parent
REGISTRY = HERE / "threads.json"
LOGDIR = HERE / "log"
DEFAULT_TIMEOUT = 900
HASH_CHARS = 32  # 128 bits -- collision is not a practical concern

# Each mode is a stance, not a script. Terse on purpose: Codex can read the
# repository, so these fix the role and the output shape, not the facts.
ROLES = {
    "review": (
        "You are the reviewing partner for a bounded engineering dispatch. You designed "
        "the dispatch; the implementer has now reported completion. Review the report "
        "against the dispatch's own acceptance criteria and gates, reading whatever files "
        "in this repository you need to check its claims. Be adversarial about unverified "
        "claims, silently broadened scope, and any result presented as stronger than its "
        "evidence supports. Respond as: VERDICT (accept / accept-with-findings / reject), "
        "FINDINGS (each naming the specific claim and why it does not hold), REQUIRED "
        "CHANGES, NEXT DISPATCH RECOMMENDATION. Cite the files you checked."
    ),
    "design": (
        "You are designing the next bounded dispatch for this project. Read the project's "
        "existing dispatch template and recent dispatches, and follow their structure and "
        "vocabulary exactly. One principal objective, the smallest sufficient change, "
        "explicit acceptance criteria, explicit kill and promotion criteria, no unrelated "
        "cleanup. State every assumption and what evidence would falsify it."
    ),
    "challenge": (
        "You are a skeptic. Attempt to REFUTE the claim you are given, using the repository "
        "as evidence. Default to 'not established' when the evidence is insufficient. Say "
        "plainly which parts survive scrutiny and which do not, and name the specific "
        "additional evidence that would settle each open point."
    ),
    "ask": (
        "You are a senior engineering consultant on this project. Answer directly and "
        "concretely, reading the repository as needed. Distinguish what you verified in "
        "the code from what you are inferring."
    ),
}

# Re-exported from the Codex driver, which owns the event-stream shape.
SESSION_KEYS = codex_runner.SESSION_KEYS


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def warn(msg: str) -> None:
    print(f"warning: {msg}", file=sys.stderr)


def use_utf8_console() -> None:
    """Stop a legacy console code page from withholding a completed answer.

    Windows consoles default to cp1252 here, so printing an answer containing an
    arrow or an em dash raises UnicodeEncodeError. That happens AFTER the turn is
    logged and the registry written, so the result stays durable -- but it is not
    delivered to the caller, who sees a traceback instead. Replace rather than
    fail: a mangled character beats an undelivered answer.

    Streams that cannot be reconfigured are tolerated. The durable record still
    holds, but this function cannot make any promise about such a stream.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass  # redirected to something without reconfigure; nothing to do


def codex_bin() -> str:
    exe = shutil.which("codex")
    if not exe:
        raise SystemExit(
            "codex CLI not found on PATH.\n"
            "  Install it:  npm install -g @openai/codex\n"
            "  Then sign in: codex login"
        )
    return exe


# ------------------------------------------------------------------ identity
def canonical_key(project: Path, thread: str) -> str:
    """The identity of a conversation, resolved so equivalent spellings agree.

    realpath collapses symlinks and relative segments; normcase folds Windows
    case. The NUL separator cannot occur in a path or a thread name, so
    ("a::b", "c") and ("a", "b::c") cannot produce the same key.
    """
    root = os.path.normcase(os.path.realpath(str(project)))
    return f"{root}\x00{thread}"


def thread_id(project: Path, thread: str) -> str:
    """A readable slug plus a hash of the canonical key.

    The slug is for humans scanning a directory listing; the hash is what makes
    the id unique. Truncating the slug is therefore safe -- it carries no
    identity of its own.
    """
    digest = hashlib.sha256(canonical_key(project, thread).encode("utf-8")).hexdigest()
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{Path(project).name}-{thread}")
    return f"{slug[:48].strip('_')}-{digest[:HASH_CHARS]}"


# ------------------------------------------------------------------ registry
def read_registry() -> dict:
    if REGISTRY.is_file():
        try:
            return json.loads(REGISTRY.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def write_registry(reg: dict) -> None:
    REGISTRY.write_text(
        json.dumps(reg, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def append_log(tid: str, record: dict) -> None:
    """Append-only conversation log -- the live viewer tails this."""
    LOGDIR.mkdir(parents=True, exist_ok=True)
    with (LOGDIR / f"{tid}.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


# -------------------------------------------------------------------- codex
# The Codex mechanics now live in parley_core.codex_runner. These wrappers keep
# this module's existing surface -- the names the characterization suite pins --
# while the implementation sits behind the runner contract.
#
# codex_bin() deliberately stays here: it is the single patchable seam for binary
# resolution, and the runner is pure with respect to PATH lookup, taking the
# resolved binary as an argument.


def find_session_id(stream: str) -> str | None:
    """Pull the session id out of the --json event stream."""
    return codex_runner.find_session_id(stream)


def build_cmd(
    project: Path, resume_id: str | None, model: str | None, out_file: Path
) -> list[str]:
    """The Codex argv for a read-only consultation turn."""
    return codex_runner.build_argv(
        codex_bin(), project, resume_id, model, out_file, AccessPolicy.READ_ONLY
    )


def run_codex(
    prompt: str, project: Path, resume_id: str | None, model: str | None, timeout: int
) -> tuple[str, str | None, bool]:
    """Run one consultation turn through the runner contract.

    Returns the legacy (answer, session, partial) triple. Consultation is
    read-only, and admission refuses anything else before a process launches.
    """
    spec = runners.resolve_spec(driver="codex", provider="openai", model=model)
    runners.admit(spec, AccessPolicy.READ_ONLY)
    runner = codex_runner.CodexRunner(
        spec, binary=codex_bin(), timeout=timeout, warn=warn
    )
    try:
        result = runner.run(prompt, project, resume_id, AccessPolicy.READ_ONLY)
    except runners.RunnerError as e:
        # The legacy surface reports failure as SystemExit with a plain message.
        raise SystemExit(str(e)) from None
    return result.answer, result.session, result.partial


# ------------------------------------------------------------------- prompt
def orientation(project: Path, thread: str) -> str:
    """Sent once, at the head of a new thread, so GPT knows what it is looking at."""
    return (
        f"[NEW CONVERSATION]\n"
        f"Project: {project.name}\nWorking root: {project}\nThread: {thread}\n\n"
        "This conversation is scoped to that project only. Claude Code is the implementer "
        "and is relaying on the user's behalf; the user reads along but no longer "
        "copy-pastes between us. You have read-only access to the repository -- read what "
        "you need rather than assuming, and say so when something you need is missing.\n"
    )


def build_prompt(
    project: Path,
    thread: str,
    mode: str,
    turn: int,
    body: str,
    contexts: list[Path],
    fresh: bool,
) -> str:
    parts = []
    if fresh:
        parts.append(orientation(project, thread))
    parts.append(ROLES[mode] + "\n\n")
    parts.append(
        f"[project={project.name} thread={thread} mode={mode} turn={turn}]\n\n"
    )
    parts.append(body)
    if contexts:
        parts.append(
            "\n\n--- FILES TO READ (paths relative to the working root) ---\n"
            + "\n".join(str(p) for p in contexts)
        )
    return "".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Consult GPT (via Codex) about a dispatch."
    )
    ap.add_argument("--mode", choices=sorted(ROLES), default="ask")
    ap.add_argument("--input", help="file holding the report, question, or claim")
    ap.add_argument(
        "--context",
        action="append",
        default=[],
        help="repository file Codex should read (repeatable)",
    )
    ap.add_argument("--project", default=".", help="project root this concerns")
    ap.add_argument(
        "--thread", default="default", help="named conversation within the project"
    )
    ap.add_argument("--model", default=None, help="override the Codex model")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    ap.add_argument("--new-thread", action="store_true", help="start this thread over")
    ap.add_argument("--list", action="store_true", help="list all threads and exit")
    args = ap.parse_args()

    use_utf8_console()

    reg = read_registry()

    if args.list:
        if not reg:
            print("no threads yet")
            return 0
        for tid, v in sorted(reg.items(), key=lambda kv: str(kv[1].get("project"))):
            print(f"{v.get('project')}  [{v.get('thread')}]")
            print(
                f"    turns={v.get('turns', 0)}  session={v.get('session_id')}  "
                f"updated={v.get('updated_at')}\n    id={tid}"
            )
        return 0

    if not args.input:
        ap.error("--input is required unless --list is used")

    project = Path(args.project).resolve()
    if not project.is_dir():
        raise SystemExit(f"project root is not a directory: {project}")

    tid = thread_id(project, args.thread)
    entry = {} if args.new_thread else reg.get(tid, {})
    resume_id = entry.get("session_id")
    turn = int(entry.get("turns", 0)) + 1

    body_path = Path(args.input)
    if not body_path.is_file():
        raise SystemExit(f"input file not found: {body_path}")
    contexts = [Path(c) for c in args.context]

    prompt = build_prompt(
        project,
        args.thread,
        args.mode,
        turn,
        body_path.read_text(encoding="utf-8", errors="replace"),
        contexts,
        fresh=not resume_id,
    )

    # Fail on a missing binary before writing to the log, so a setup error never
    # leaves an orphan question in the transcript.
    codex_bin()

    print(
        f"[{'new thread' if not resume_id else f'resuming, turn {turn}'}] "
        f"{project.name} / {args.thread} / {args.mode}",
        file=sys.stderr,
    )

    append_log(
        tid,
        {
            "ts": now(),
            "role": "claude",
            "mode": args.mode,
            "turn": turn,
            "project": str(project),
            "thread": args.thread,
            "attached": [p.name for p in contexts],
            "text": prompt,
        },
    )

    # Everything after the outbound record is guarded. BaseException so a
    # KeyboardInterrupt closes the visible turn rather than leaving it reading as
    # "still thinking". `answered` prevents a misleading second failure record
    # once the turn has already been terminated by a real answer.
    answered = False
    try:
        answer, session_id, partial = run_codex(
            prompt, project, resume_id, args.model, args.timeout
        )
        append_log(
            tid,
            {
                "ts": now(),
                "role": "gpt",
                "turn": turn,
                "project": str(project),
                "thread": args.thread,
                "session_id": session_id or resume_id,
                "partial": partial,
                "text": answer,
            },
        )
        answered = True

        reg[tid] = {
            "session_id": session_id or resume_id,
            "project": str(project),
            "thread": args.thread,
            "turns": turn,
            "created_at": entry.get("created_at") or now(),
            "updated_at": now(),
        }
        write_registry(reg)
        print(answer)
    except BaseException as exc:
        if not answered:
            try:
                append_log(
                    tid,
                    {
                        "ts": now(),
                        "role": "gpt",
                        "turn": turn,
                        "project": str(project),
                        "thread": args.thread,
                        "error": True,
                        "text": f"[TURN FAILED]\n\n{exc}",
                    },
                )
            except Exception as log_exc:  # noqa: BLE001 - see below
                # Deliberately broad: recording the failure is best-effort and
                # must never replace the exception the caller needs to see.
                warn(f"could not record the failed turn: {log_exc}")
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
