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
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

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


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def warn(msg: str) -> None:
    print(f"warning: {msg}", file=sys.stderr)


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


def build_cmd(
    project: Path, resume_id: str | None, model: str | None, out_file: Path
) -> list[str]:
    """Assemble the codex argv.

    Exec-level options MUST precede the `resume` subcommand -- `resume` accepts
    only its own arguments, so a trailing `-s` is an "unexpected argument" error
    rather than an override. Placing `-s read-only` first pins the sandbox on
    fresh and resumed turns alike.
    """
    cmd = [codex_bin(), "exec", "-s", "read-only"]
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


def run_codex(
    prompt: str, project: Path, resume_id: str | None, model: str | None, timeout: int
) -> tuple[str, str | None, bool]:
    """Run one non-interactive Codex turn.

    Returns (answer, session_id, partial). `partial` marks a turn that produced
    output despite a non-zero exit.
    """
    # mkstemp hands back an OPEN descriptor; close it before Codex writes to the
    # path, or the later unlink fails on Windows.
    fd, tmp_path = tempfile.mkstemp(prefix="parley-", suffix=".md")
    os.close(fd)
    out_file = Path(tmp_path)

    def discard() -> None:
        """Remove the temp file when it holds nothing worth keeping."""
        try:
            out_file.unlink(missing_ok=True)
        except OSError as e:
            warn(f"could not remove temporary file {out_file}: {e}")

    try:
        proc = subprocess.run(
            build_cmd(project, resume_id, model, out_file),
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,  # a non-zero exit is handled below, not raised
            cwd=str(project),  # resume filters recorded sessions by cwd
        )
    except subprocess.TimeoutExpired:
        discard()
        raise SystemExit(
            f"codex timed out after {timeout}s (raise with --timeout)"
        ) from None
    except FileNotFoundError:
        discard()
        raise SystemExit("could not execute the codex CLI") from None

    # Reading and cleanup have different recovery requirements, so they are NOT
    # under one finally. If the read fails, the temp file may hold the only copy
    # of a completed answer and must survive.
    answer = ""
    if out_file.is_file():
        try:
            answer = out_file.read_text(encoding="utf-8", errors="replace").strip()
        except OSError as e:
            raise SystemExit(
                f"codex finished but its output could not be read: {e}\n"
                f"  The answer is retained at: {out_file}"
            ) from None
    discard()

    partial = False
    if proc.returncode != 0:
        if answer:
            # Output despite a bad exit: keep it, but never present it as clean.
            partial = True
            warn(f"codex exited {proc.returncode} but produced output; marked partial")
        else:
            err = (proc.stderr or proc.stdout or "").strip()[:700]
            low = err.lower()
            # Order matters: a usage error quotes "session" in its help text,
            # which once made a malformed command look like an expired session.
            if "unexpected argument" in low or "usage:" in low:
                hint = "\n  Malformed codex invocation -- a bug in parley.py, not your session."
            elif "login" in low or "not logged in" in low or "unauthor" in low:
                hint = "\n  Run `codex login` and sign in with your ChatGPT account."
            elif "rate" in low or "quota" in low or "limit" in low:
                hint = "\n  Plan allowance may be exhausted for this window."
            elif resume_id and (
                "rollout" in low or "resume" in low or "not found" in low
            ):
                hint = "\n  That stored session is gone -- rerun with --new-thread."
            else:
                hint = ""
            raise SystemExit(f"codex exited {proc.returncode}: {err}{hint}")

    return (
        answer or "(codex returned no final message)",
        find_session_id(proc.stdout or ""),
        partial,
    )


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
