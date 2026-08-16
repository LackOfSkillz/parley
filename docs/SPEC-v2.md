# Parley v2 — Engineering Spec and Stage Plan

Status: **APPROVED** by the owner. Single normative source of truth for v2.
Co-authored by Claude Code and GPT (via Codex) in the `v2` thread of this repo;
the transcript in `log/` is the provenance for every decision here.

Owner rulings, applied in place throughout:
- Writable runs refuse **modified tracked** files but **tolerate untracked** ones.
- Worktree root is **`D:\ParleyWorktrees`**.
- Claude Code support stays **last and conditional**, and may never ship.

Normative terms such as “must” describe implementation requirements. Any
remaining marked judgment call must be raised rather than guessed during
implementation.

## 1. Module layout

Parley v2 remains stdlib-only, but production logic moves out of the two current files.

```text
parley.py
serve.py

parley_core/
    __init__.py
    cli.py
    models.py
    runners.py
    codex_runner.py
    storage.py
    prompts.py
    worktrees.py
    orchestrator.py
    viewer.py

web/
    index.html
    app.js
    style.css

tests remain top-level:
    test_parley.py
    test_contract.py
    test_runners.py
    test_storage.py
    test_worktrees.py
    test_orchestrator.py
    test_cli_v2.py
    test_viewer.py

conditional after PARLEY-V2-010:
    parley_core/claude_runner.py
```

Ownership:

- `parley.py`: thin executable wrapper calling `parley_core.cli.main()`. It retains no orchestration, storage, subprocess, or prompt logic.
- `serve.py`: thin loopback-only viewer entry point.
- `cli.py`: argument parsing, legacy-form detection, command dispatch, and user-facing output/error mapping.
- `models.py`: immutable enums and dataclasses for RunnerSpec, access policies, runner results, transcript records, registry state, and orchestration state.
- `runners.py`: runner protocol, trusted runner resolution, capability admission, and shared runner errors.
- `codex_runner.py`: Codex argv construction, subprocess execution, session extraction, timeout handling, answer preservation, and Codex-specific diagnostics.
- `storage.py`: canonical conversation identity, v1 compatibility projection, v2 registry, JSONL transcript writes, control inboxes, and atomic file replacement.
- `prompts.py`: consultation, maker, reviewer, safety-contract, steering, and verdict prompt rendering.
- `worktrees.py`: Git preflight, dedicated worktree creation, validation, metadata, and recovery instructions.
- `orchestrator.py`: bounded maker/reviewer state machine only. It does not parse CLI arguments or invoke Git directly.
- `viewer.py`: transcript discovery, v1/v2 normalization, traversal protection, and HTTP API response construction.
- `web/*`: presentation only. No state transitions or security decisions occur in JavaScript.
- `claude_runner.py`: added only if PARLEY-V2-009 proves the required Claude Code behavior.

Runtime state remains outside source modules:

```text
threads.json                    v1 compatibility registry
registry-v2.json                authoritative v2 registry
log/<conversation-id>.jsonl     append-only transcripts
runs/<run-id>/controls/*.json   atomic external control inbox

D:\ParleyWorktrees\<project-name>-<run-id>\
                                default retained run worktree on Windows

<--worktree-root>\<project-name>-<run-id>\
                                retained run worktree when explicitly overridden
```

No source-size cap is imposed. The ownership boundaries above are the control against another god file.

Assumptions:

- Flat executable wrappers plus an internal package remain compatible with `python parley.py`. Falsified if Python import resolution conflicts with the wrapper name or supported invocation environment.
- External web assets can be served safely by exact allowlisted paths. Falsified if moving them out of `serve.py` introduces unrestricted filesystem serving.

Judgment call: none. Module ownership changes require owner approval if implementation reveals a necessary cycle or materially different boundary.

## 2. Runner contract

### RunnerSpec

`RunnerSpec` is immutable and JSON-serializable:

```python
RunnerSpec(
    driver: str,
    provider: str,
    model: str | None,
    capabilities: RunnerCapabilities,
)

RunnerCapabilities(
    persistent_sessions: bool,
    read_only: bool,
    workspace_write: bool,
)
```

Meanings:

- `driver`: Parley’s built-in adapter, initially `codex`; conditionally `claude-code`.
- `provider`: provider understood by that driver, initially `openai`, `ollama`, or `lmstudio`.
- `model`: exact provider-specific model identifier, or `None` for the runner’s configured default.
- `capabilities`: what the adapter has demonstrated it can mechanically request and enforce.

Capabilities are not user-supplied. CLI configuration supplies driver, provider, and model. The trusted built-in adapter resolves those into a RunnerSpec and declares capabilities in code. A registry or transcript snapshot never grants a capability merely by claiming it.

### Invocation contract

A runner instance is constructed with its validated RunnerSpec and timeout. Its exact interface is:

```python
run(
    prompt: str,
    cwd: Path,
    session: str | None,
    access_policy: AccessPolicy,
) -> RunResult
```

Exact return shape:

```python
RunResult(
    answer: str,
    session: str | None,
    status: RunStatus,
    metadata: RunMetadata,
)

RunStatus = completed | partial | failed | timed_out

RunMetadata(
    exit_code: int | None,
    duration_ms: int,
    diagnostic: str | None,
    output_retained_at: str | None,
)
```

Rules:

- `session` is the newly observed session ID, or the supplied session if no replacement was emitted.
- `partial` means a non-zero process exit produced a usable final answer.
- `failed` means no usable final answer.
- `timed_out` is distinct from other failure.
- An output file that contains a completed answer must not be destroyed by cleanup failure.
- Failure to read a possible answer raises `RunnerError` naming the retained output path; it must not return a guessed result.
- Unexpected infrastructure exceptions raise `RunnerError`. The caller records them without masking the original cause.
- Runner metadata must be JSON-safe. Raw event streams are not persisted by default.

Before process launch, `runners.py` verifies that the requested access policy is supported by the resolved adapter-owned capability set. Excess access is refused at runner admission and again defensively inside the runner. No subprocess launches and no prompt record is written for an inadmissible policy.

Assumptions:

- Codex’s current clean, partial, timeout, session, and output behavior fits this shape. PARLEY-V2-002 falsifies this if compatibility wrappers cannot reproduce the accepted contract tests.
- Timeout can remain runner-construction configuration rather than a `run()` parameter. Falsified if one runner instance must safely execute concurrent calls with different timeouts.

Judgment call: if a runner exposes an outcome that cannot map without losing safety-relevant information, stop and amend `RunStatus` or `RunMetadata`; do not hide it in a free-form diagnostic.

## 3. Access policy

The complete v2 enumeration is:

```text
read_only
workspace_write
```

`danger-full-access`, approval bypasses, unrestricted execution, and “best effort” sandboxing are not valid Parley policies.

Participant rules:

| Workflow | Participant | Required policy |
|---|---|---|
| Legacy consultation | reviewer | `read_only` |
| Explicit consultation | reviewer | `read_only` |
| Orchestrated run | maker | `workspace_write` |
| Orchestrated run | reviewer | `read_only` |

V2 has no patch-only or read-only maker mode. An orchestrated run is writable and therefore requires `--allow-write` on every new `run` invocation.

Before creating or launching a writable run, Parley must verify:

1. `--allow-write` is present.
2. The project resolves to an existing Git worktree.
3. `HEAD` resolves to a commit.
4. No tracked file has staged or unstaged changes, including tracked deletions, renames, type changes, conflict entries, or dirty tracked submodules. Untracked files do not fail admission.
5. All non-ignored untracked paths are enumerated before worktree creation. If any exist, Parley warns on stderr and appends a `source.untracked` transcript event before the first model invocation.
6. The maker runner supports `persistent_sessions` and `workspace_write`.
7. The reviewer runner supports `persistent_sessions` and `read_only`.
8. The configured worktree root is absolute; exists or can be created; resolves to a writable, available local filesystem; and remains outside the canonical source project after symlink/junction resolution.
9. The target is a direct child of the resolved worktree root, does not exist, is not registered as another Git worktree, and remains outside the canonical source project after resolving its existing parent.
10. On Windows, every target component fits the destination filesystem’s component limit. Parley computes the longest resulting committed checkout path. If it reaches the traditional 260-character boundary and Git `core.longpaths` is not enabled, admission refuses. If `core.longpaths` is enabled, admission may proceed but emits a warning because downstream tools may still lack long-path support.
11. `git worktree add --detach --lock --no-relative-paths <target> <source-commit>` succeeds.
12. After creation, `git worktree list --porcelain -z` reports the intended target and source commit; `git rev-parse --show-toplevel` from the new worktree equals the intended target; and `git status --porcelain` succeeds.
13. Both participants will receive that exact target as `cwd`.
14. Run metadata, source warnings, and recovery location are durable before the first maker launch.

Implementation tests for precondition 4 must use:

```text
git diff --quiet --ignore-submodules=none --
git diff --cached --quiet --ignore-submodules=none HEAD --
```

Exit code 0 means clean, 1 means tracked changes, and any other exit is an admission error. Untracked discovery must use the NUL-delimited equivalent of:

```text
git ls-files --others --exclude-standard -z
```

Ignored files are not enumerated. They were already outside the prior clean-tree rule and remain a known environment-parity blind spot.

The code-defined default worktree root on Windows is:

```text
D:\ParleyWorktrees
```

The default target is:

```text
D:\ParleyWorktrees\<project-name>-<run-id>
```

`--worktree-root ABSOLUTE_DIR` may override the root for one new run. There is no environment-variable override in v2. The resolved root is persisted in `run.created` and cannot change during the run.

Parley creates the root when absent, then resolves and validates it before deriving the target. The target name is generated from a sanitized project slug and Parley-generated run ID; user-controlled path separators and `..` are forbidden.

A different drive is supported because Git worktrees accept absolute paths. Parley explicitly uses `--no-relative-paths` so a repository-level `worktree.useRelativePaths` setting cannot produce invalid cross-volume linkage. The worktree is created with `--lock` because v2 retains it for manual disposition.

The linked worktree still shares Git administrative data and object storage with the source repository. Moving the checkout to D: provides capacity and change isolation; it does not create repository-metadata or security isolation.

The source commit and worktree path are immutable for the run.

The worktree isolates ordinary edits and preserves a recoverable diff. It is not a security boundary. It does not by itself prevent an unsandboxed process from reading credentials, reaching the network, accessing sibling directories, or modifying the main repository. The runner’s enforced sandbox is the write boundary.

Parley must never:

- Fall back to a less restrictive policy.
- Launch an adapter that cannot enforce the requested policy.
- Merge, push, cherry-pick, promote, delete, or automatically clean the worktree.
- Treat runner capability declarations from CLI input or registry data as trusted.

Assumption: refusing tracked staged or unstaged changes prevents the source-fidelity failure this gate is intended to prevent, because the run is constructed from committed HEAD. Falsified by a demonstrated tracked working-tree change that is absent from HEAD but not detected by the two explicit diff checks.

Assumption: tolerating untracked files is safe for source preservation because they were never part of the source commit. Falsified by evidence that worktree creation copies or mutates an untracked source path.

Assumption: warning about non-ignored untracked paths provides adequate visibility into environment differences. Falsified if a supported workflow materially depends on ignored or untracked local files and requires them to be copied into the run. Copying such files requires a separate approved snapshot design.

Assumption: `D:\ParleyWorktrees` is an available fixed local volume with adequate capacity on the owner’s host. Falsified by runtime volume, writability, availability, or checkout validation.

Assumption: explicit absolute linkage and post-create validation are sufficient for cross-drive worktrees. Falsified if controlled PARLEY-V2-006 tests show invalid Git administration links, checkout failures, or incorrect top-level resolution.

Settled by owner ruling: untracked files are tolerated, the worktree root is
`D:\ParleyWorktrees`, and there is no network isolation. Dirty-tree snapshotting
remains out of scope and requires its own design.

## 4. Transcript schema v2

V2 continues using append-only JSONL named by the canonical conversation ID ending in the existing 32-hex digest.

Every v2 record has this exact envelope:

```json
{
  "schema": 2,
  "event_id": "uuid",
  "ts": "UTC RFC3339 timestamp",
  "conversation_id": "canonical conversation id",
  "project": "canonical absolute project path",
  "thread": "thread name",
  "run_id": "run id or null",
  "kind": "record kind",
  "participant": "maker | reviewer | human | system",
  "driver": "driver or null",
  "provider": "provider or null",
  "model": "model or null",
  "lane_turn": "integer or null",
  "iteration": "integer or null",
  "steering_id": "steering id or null",
  "text": "exact rendered text/answer/note or null",
  "data": {}
}
```

For every model prompt, response, or failure, `participant`, `driver`, `provider`, and `model` snapshot the invoked lane’s resolved RunnerSpec. Human and system records use null runner fields.

Allowed kinds and exact `data` fields:

```text
consult.prompt
  {mode, attached, access_policy, session_in}

consult.response
  {session_out, run_status, metadata}

consult.failure
  {error_type, diagnostic, retained_output_path}

run.created
  {source_commit, worktree, max_iterations, allow_write,
   maker_spec, reviewer_spec}

steering.set
  {author, supersedes}

steering.clear
  {author, supersedes}

stop.requested
  {author}

state.changed
  {from, to, reason}

invocation.prompt
  {access_policy, session_in, diff_sha256}

invocation.response
  {session_out, run_status, metadata, diff_sha256}

invocation.failure
  {error_type, diagnostic, retained_output_path, diff_sha256}

review.verdict
  {verdict, summary, required_changes, diff_sha256}

run.finished
  {outcome, source_commit, worktree, diff_sha256}
```

`metadata` inside response records is the exact RunMetadata shape from section 2.

Compatibility policy is dual-read, not migration and not a hard cut:

- Existing v1 JSONL files are never rewritten.
- Legacy bare `--mode` invocations continue writing their exact characterized v1 record shapes.
- Explicit `consult` and `run` commands write v2 records.
- One transcript may contain v1 and v2 records.
- The viewer continues accepting only filenames with the canonical 32-hex suffix.
- The viewer normalizes v1 `role=gpt` to reviewer, `role=claude` to maker, and `error=true` to a failed display.
- V2 records render from `participant`, `kind`, runner snapshot, and status.
- Unknown v2 kinds render as neutral system records; they are not discarded.
- Existing generation-based stale-poll rejection remains unchanged.

This deliberate v1 writer compatibility is required by the accepted characterization tests. “Stop encoding vendors as roles” applies to v2 records; the permanent legacy format remains historical compatibility.

Assumptions:

- Mixed-schema records can be normalized independently without changing JSONL cursor semantics. Falsified by viewer tests showing missed, duplicated, or reordered records.
- A single writer owns each JSONL transcript during orchestration. Falsified if supported workflows require concurrent transcript appenders; that would require an approved locking protocol.

Judgment call: adding fields or record kinds requires a schema amendment. Implementers must not place undeclared fields in `data`.

## 5. Registry schema v2

`registry-v2.json` is authoritative for explicit v2 commands. `threads.json` remains an exact v1 compatibility projection for permanent bare-form support.

Exact top-level shape:

```json
{
  "schema": 2,
  "conversations": {
    "<conversation-id>": {
      "project": "canonical absolute path",
      "thread": "thread name",
      "created_at": "timestamp",
      "updated_at": "timestamp",
      "consult": {
        "turns": 0,
        "reviewer": {
          "runner": {},
          "session": "string or null",
          "session_origin": "{} or null",
          "invocations": 0
        }
      },
      "runs": {
        "<run-id>": {
          "created_at": "timestamp",
          "updated_at": "timestamp",
          "state": "state",
          "iteration": 0,
          "max_iterations": 3,
          "source_commit": "commit",
          "worktree": "absolute path",
          "allow_write": true,
          "active_steering_id": "string or null",
          "stop_requested": false,
          "last_event_id": "uuid or null",
          "lanes": {
            "maker": {
              "runner": {},
              "session": "string or null",
              "session_origin": "{} or null",
              "invocations": 0
            },
            "reviewer": {
              "runner": {},
              "session": "string or null",
              "session_origin": "{} or null",
              "invocations": 0
            }
          }
        }
      }
    }
  }
}
```

Each `runner` is an exact RunnerSpec snapshot. `session_origin` is the driver/provider/model identity under which that session was created; capabilities are excluded because adapter capabilities can change between versions.

Migration is lazy and non-destructive:

- Reading a v1 entry creates an in-memory v2 consultation entry.
- Its session moves to `consult.reviewer.session`.
- Its turns and timestamps are preserved.
- `runner` is Codex/OpenAI with the requested current model or null.
- `session_origin` is null because v1 did not persist enough evidence to reconstruct it.
- The v1 entry remains unchanged in `threads.json`.
- The next successful v2 operation atomically writes `registry-v2.json`.
- Legacy operations continue writing the exact six-field v1 entry and mirror compatible state into v2.
- Legacy mode may resume a session with unknown or changed model provenance because that is characterized behavior.
- Explicit v2 consultation starts a new session when driver, provider, or model differs or session provenance is unknown.
- Runner configuration cannot change inside an orchestrated run.

Registry writes use temp-file-plus-`os.replace`. Transcript events are the audit source of truth; the registry is a resumable index. On mismatch, v2 run state is reconstructed from valid transcript events rather than inferred from mutable settings.

Assumptions:

- Maintaining a v1 compatibility projection is preferable to weakening the accepted on-disk contract. Falsified only by explicit owner approval to version-break the bare form.
- Atomic replacement is supported on the registry filesystem. Falsified by supported-filesystem testing.

Judgment call: no automatic deletion or compaction policy is specified. Raise storage-growth concerns separately.

## 6. Steering

External steering commands write one complete JSON control file into:

```text
runs/<run-id>/controls/<event-id>.json
```

The file is written to a temporary sibling and atomically renamed. The orchestrator is the sole transcript writer. At the next invocation boundary it consumes controls deterministically, appends `steering.set`, `steering.clear`, or `stop.requested` events to the transcript, and updates registry state.

`steering.set` contains a new steering ID, exact text, author, and the prior steering ID in `supersedes`. `steering.clear` contains its own event ID and the active steering ID in `supersedes`.

Each model invocation snapshots:

- Active `steering_id`.
- Exact rendered steering text.
- Runner identity.
- Access policy.
- Iteration and lane turn.

Prompt order is fixed:

```text
1. Immutable Parley role and safety contract
2. Optional delimited human steering block
3. Turn metadata
4. Task, diff, prior verdict, or repair material
```

Delimiter:

```text
--- HUMAN STEERING <steering-id> ---
<exact text>
--- END HUMAN STEERING ---
```

Steering becomes effective only for the next invocation whose prompt has not yet been snapshotted. It does not mutate or restart an invocation already running.

Steering may never override:

- Access policy or sandbox.
- Runner capability admission.
- Project or worktree path.
- Source commit.
- Maker/reviewer identity or runner configuration.
- Iteration count or maximum.
- Structured verdict grammar.
- State-transition rules.
- Stop semantics.
- The prohibition on merge, push, promotion, deletion, and sandbox fallback.

Assumptions:

- Next-invocation steering satisfies the owner’s requirement. Falsified by a requirement for live token-stream intervention.
- Atomic per-control files avoid torn concurrent appends. Falsified by supported-platform tests showing incomplete files can become visible.

Judgment call: cancel-and-retry of a running invocation is outside v2. Do not implement it as an interpretation of steering.

## 7. CLI surface

### Permanent legacy interface

The following remains permanently supported in v2, with no deprecation warning or scheduled removal:

```text
python parley.py --mode ask|review|design|challenge
                 --input FILE
                 [--project DIR]
                 [--thread NAME]
                 [--context FILE ...]
                 [--model MODEL]
                 [--timeout SECONDS]
                 [--new-thread]

python parley.py --list
```

Its characterized prompt, argv, stdout, transcript, registry, session, and failure behavior remain unchanged.

### Explicit consultation

```text
python parley.py consult
                 --mode ask|review|design|challenge
                 --input FILE
                 [--project DIR]
                 [--thread NAME]
                 [--context FILE ...]
                 [--driver codex]
                 [--provider openai|ollama|lmstudio]
                 [--model MODEL]
                 [--timeout SECONDS]
                 [--new-thread]

python parley.py list [--project DIR]
```

Explicit consultation is always read-only. It writes v2 records.

### Orchestrated run

```text
python parley.py run
                 --input FILE
                 --project DIR
                 --thread NAME
                 --allow-write
                 [--max-iterations 1..20]
                 [--maker-driver codex]
                 [--maker-provider PROVIDER]
                 [--maker-model MODEL]
                 [--maker-timeout SECONDS]
                 [--reviewer-driver codex]
                 [--reviewer-provider PROVIDER]
                 [--reviewer-model MODEL]
                 [--reviewer-timeout SECONDS]
```

Default maximum iterations: 3. A run is never unbounded.

### Steering, stop, and status

```text
python parley.py steer
                 --project DIR --thread NAME --run-id ID
                 --input FILE

python parley.py steer
                 --project DIR --thread NAME --run-id ID
                 --clear

python parley.py stop
                 --project DIR --thread NAME --run-id ID

python parley.py status
                 --project DIR --thread NAME --run-id ID
```

`steer --input` and `steer --clear` are mutually exclusive. Control commands fail if the run identity does not match the canonical project/thread entry or the run is already terminal.

There is no `promote`, `merge`, `push`, `cleanup`, or automatic `resume` command in v2.

Assumptions:

- Permanent bare-form support is worth the parser branch. Falsified only by explicit owner approval for a breaking major release.
- Separate control invocations are acceptable while `run` owns the foreground terminal. Falsified by an owner requirement for an interactive TUI or web control plane.

Judgment call: shell completion, configuration files, and environment-variable defaults are outside this spec.

## 8. Orchestration state machine

States:

```text
CREATED
ADMITTED
MAKER_PENDING
MAKER_RUNNING
REVIEW_PENDING
REVIEW_RUNNING
REVISION_PENDING

APPROVED          terminal
BLOCKED           terminal
LIMIT_REACHED     terminal
STOPPED           terminal
FAILED            terminal
INTERRUPTED       terminal/derived after crash
```

Transitions:

```text
CREATED
  -> ADMITTED             preflight and worktree succeed
  -> FAILED               preflight/setup fails after durable run creation

ADMITTED
  -> MAKER_PENDING

MAKER_PENDING
  -> STOPPED              stop already requested
  -> MAKER_RUNNING        maker prompt snapshot persisted

MAKER_RUNNING
  -> REVIEW_PENDING       completed or usable partial maker answer
  -> FAILED               failed/timed out/runner error
  -> INTERRUPTED          process disappears before completion is recorded

REVIEW_PENDING
  -> STOPPED              stop already requested
  -> REVIEW_RUNNING       reviewer prompt snapshot persisted

REVIEW_RUNNING
  -> APPROVED             strict approve verdict
  -> BLOCKED              strict blocked verdict
  -> REVISION_PENDING     strict revise verdict and iterations remain
  -> LIMIT_REACHED        revise verdict at maximum iteration
  -> BLOCKED              malformed or ambiguous verdict
  -> FAILED               failed/timed out/runner error
  -> INTERRUPTED          process disappears before completion is recorded

REVISION_PENDING
  -> STOPPED              stop already requested
  -> MAKER_PENDING        iteration increments
```

One iteration is one maker invocation followed by one reviewer invocation. Iteration begins at 1. Valid bounds are 1–20, default 3.

Reviewer final output must be exactly one JSON object:

```json
{
  "verdict": "approve | revise | blocked",
  "summary": "string",
  "required_changes": ["string"]
}
```

Rules:

- `approve` requires an empty `required_changes`.
- `revise` requires at least one required change.
- Invalid JSON, extra top-level output, unknown keys, invalid verdicts, or inconsistent fields produce `BLOCKED`.
- Reviewer approval is an outcome, not a promotion authority.
- The reviewer receives the worktree path, source commit, current diff hash, and instructions to inspect repository state and the actual diff.
- Parley computes and records the diff hash; the maker’s self-report is never treated as diff evidence.

Human stop paths:

- `parley.py stop` queues a durable stop request. It prevents the next invocation but does not mutate one already running.
- `Ctrl-C` in the foreground run attempts to terminate the current runner process and records `STOPPED` if control returns.
- If the controller dies before recording completion, replay derives `INTERRUPTED`.

Persisted before each launch:

- State transition.
- Run ID and iteration.
- Lane and lane turn.
- RunnerSpec snapshot.
- Input session ID.
- Access policy.
- Steering ID and rendered prompt.
- Source commit, worktree, and diff hash.

Persisted after each launch:

- Answer or failure.
- Effective session ID.
- Run status and metadata.
- Reviewer verdict.
- Resulting diff hash.
- Next state.

V2 is auditable after a crash but does not automatically resume. `status` replays the transcript, reports an unmatched running invocation as `INTERRUPTED`, and prints the retained worktree and recovery details. Automatic resume is withheld because Parley cannot prove whether a crashed runner process is still active or whether its session/worktree state is consistent.

Assumptions:

- Strict JSON verdicts are reliable enough for supported runners. Falsified by controlled runner tests; do not fall back to prose interpretation.
- Auditability plus retained worktree is sufficient for v2 crash recovery. Falsified by an owner requirement for automatic continuation.

Judgment call: automatic crash resume, orphan-process management, and live cancellation require a later safety design.

## 9. What v2 deliberately does not do

V2 does not provide:

- Automatic merge, push, cherry-pick, promotion, or worktree deletion.
- A third approver/promoter lane.
- Unbounded iteration.
- `danger-full-access` or sandbox fallback.
- Container, VM, network, credential, or secret isolation.
- Security claims based solely on Git worktrees.
- Patch-only makers.
- Dirty-source snapshotting.
- Mid-invocation steering mutation.
- Automatic crash resume.
- Generalized plugins or dynamically loaded runner code.
- User-declared capability flags.
- Arbitrary local/frontier model claims.
- Claude Code support unless PARLEY-V2-009 earns it.
- Transcript rewriting, v1 deletion, or a hard schema cut.
- Remote control, authentication, LAN binding, scheduling, or multi-host operation.
- Automatic secret scanning or transcript encryption.

The guaranteed Parley v2 runner is Codex. Its supported provider surface is OpenAI frontier models addressable through Codex and compatible local models addressable through Codex’s Ollama or LM Studio provider routes.

The product claim is:

> Parley supports models addressable through its configured Codex runner and compatible with the participant’s required capabilities.

Parley does not claim support for every local model, every frontier model, or Claude Code. PARLEY-V2-009 is an evidence-only Claude Code feasibility probe. PARLEY-V2-010 exists only if that probe earns promotion. Claude Code support is not a v2 completion criterion and may never ship.

Judgment call: any requested item above requires its own design and dispatch.

## 10. Stage plan

Execution order:

```text
001 → 001A → 002 → 002L → 004 → 003 → 005 → 006 → 007 → 008 → 009 → 010
```

001 and 001A are complete.

The only ordering change is moving PARLEY-V2-004 before PARLEY-V2-003. Characterization established that the v1 transcript and registry shapes are compatibility boundaries. The v2 storage codec and dual-read viewer should therefore exist before the new explicit consultation command begins emitting v2 records. Shipping `consult` first would create a transient new interface whose on-disk format changes immediately afterward.

### PARLEY-V2-001 — Freeze the consultation contract

Status: accepted.

- Spec coverage: sections 4, 5, and 7 baseline.
- Principal objective: executable compatibility boundary.
- Shippable state: v1 behavior unchanged; 33 interface tests added alongside 26 regression tests.
- New user capability: none; future compatibility breaks become detectable.
- Acceptance/promotion: completed and accepted.
- Kill condition: any production change or normalization of ambiguous behavior.
- Out of scope: all v2 runtime behavior.

### PARLEY-V2-001A — Preserve completed answers across console encodings

- Spec coverage: section 12.
- Principal objective: a console code page must not withhold a completed answer.
- Smallest sufficient change: `use_utf8_console()` reconfiguring stdout and stderr to
  UTF-8 with replacement, called from `main()`, tolerating streams that lack or refuse
  reconfiguration.
- Shippable state: consultation unchanged and more robust.
- New user capability: answers containing non-latin-1 characters are delivered.
- Acceptance criteria: the helper reconfigures both streams; unreconfigurable streams are
  tolerated; an integration test proves `main()` itself delivers U+2192 through a fake
  legacy stream; a guard test proves that fake genuinely fails unreconfigured.
- Kill criteria: any change to transcript, registry, argv, or failure semantics.
- Promotion criteria: full suite green under both discovery and direct file execution.
- Status: **COMPLETE.**

### PARLEY-V2-002 — Put Codex behind the runner contract

- Spec coverage: sections 1 and 2.
- Principal objective: route existing consultation through RunnerSpec and the exact runner interface.
- Smallest sufficient change: add `models.py`, `runners.py`, and `codex_runner.py`; keep `parley.py` behavior-compatible through wrappers.
- Shippable state: consultation works exactly as before through the new internal boundary.
- New user capability: none.
- Acceptance: the entire pre-existing suite — 65 tests at the start of PARLEY-V2-002 — plus all new runner-contract tests passes.
- Kill: any characterized external change or Codex-specific leakage into the generic contract.
- Promotion: legacy main path uses the runner contract with no observable regression.
- Out of scope: new CLI, provider selection, v2 records, and writes.
- Assumption: current behavior fits the contract. Falsified by a compatibility test that cannot pass without special-case semantics.

### PARLEY-V2-002L — Characterize and classify recoverable runner limits

- Spec coverage: section 11.
- Principal objective: establish a reliable runner-level `limited` outcome from captured
  Codex evidence.
- Smallest sufficient change: capture version-authentic limit fixtures; add `limited` and
  `LimitInfo` to the runner contract; implement classification and bounded wait-policy
  calculation. No sleep loop and no public flag yet.
- Shippable state: consultation unchanged; the classifier exists but nothing waits.
- New user capability: none yet.
- Acceptance criteria: rate and plan fixtures classify as `limited`; authentication,
  expired-session, malformed-command, timeout, network and arbitrary non-zero fixtures do
  not; reset/retry-after parsing tested where present; blind backoff explicitly marked
  heuristic.
- Kill criteria: no reliable discriminator exists; classification degenerates to generic
  non-zero retry; detection depends on unversioned prose with no fixture.
- Promotion criteria: evidence-backed classifier with false-positive cases covered.
### PARLEY-V2-004 — Add the v2 storage compatibility layer

- Spec coverage: sections 4 and 5, plus viewer portions of section 1.
- Principal objective: make v1 and v2 state coexist without rewriting history.
- Smallest sufficient change: add v2 record/registry codecs, lazy v1 import, v1 projection, and viewer normalization; no command emits v2 operational records yet except tests.
- Shippable state: old transcripts still render; synthetic v2 and mixed transcripts render correctly.
- New user capability: accurate participant/runner display for v2-format test or future data; no new workflow.
- Acceptance: v1 exact-shape tests pass; mixed-schema ordering and stale-poll protection pass; registry migration is lossless and atomic.
- Kill: rewriting v1 logs, changing bare-form records, or making old transcripts unreadable.
- Promotion: storage and viewer accept both schemas before any public v2 producer ships.
- Out of scope: runner selection, steering, worktrees, and orchestration.
- Assumption: dual state can be synchronized safely. Falsified by tests showing legacy operations corrupt or ambiguously resume v2 state.

### PARLEY-V2-003 — Add explicit selectable consultation

- Spec coverage: sections 2 and 7.
- Principal objective: expose Codex/OpenAI/Ollama/LM Studio consultation without claiming orchestration.
- Smallest sufficient change: add `consult` and `list`; retain permanent bare aliases; write v2 records.
- Shippable state: legacy consultation remains intact, and explicit consultation becomes available.
- New user capability: select a supported Codex provider/model for a read-only reviewer.
- Acceptance: valid provider argv are exact; invalid combinations refuse before logging or launch; explicit consult emits valid v2 records.
- Kill: weakened read-only behavior, hidden orchestration, or “v2 complete” claims.
- Promotion: legacy and explicit forms coexist and README calls this pluggable consultation.
- Out of scope: selectable maker, steering, and writes.
- Assumption: Codex non-interactive local-provider flags pass controlled verification. Falsified by installed CLI behavior.

### PARLEY-V2-005 — Add durable steering and stop controls

- Spec coverage: section 6 and control portions of section 8.
- Principal objective: implement immutable next-invocation control semantics.
- Smallest sufficient change: control inbox, steering replay, prompt rendering, and stop-state resolution; no public run loop.
- Shippable state: consultation remains usable; control machinery is inert except in tests.
- New user capability: none yet.
- Acceptance: atomic controls cannot tear; replay is deterministic; prompt ordering and prohibited overrides are tested.
- Kill: mutable history, in-memory-only controls, or steering replacing safety policy.
- Promotion: exact steering snapshot can be reconstructed for every simulated invocation.
- Out of scope: running orchestration and mid-call mutation.
- Assumption: atomic rename works on supported filesystems. Falsified by platform tests.

### PARLEY-V2-006 — Enforce worktree and writable-run admission

- Spec coverage: sections 3, 4, 7, and state persistence from section 8.
- Principal objective: prove that writable execution is admitted only inside a validated dedicated worktree through capable runners.
- Smallest sufficient change: `worktrees.py`, tracked-change detection, untracked-source warnings, configurable absolute worktree root, cross-drive creation, capability gates, recovery metadata, and temporary-repository tests. No public orchestration loop.
- Shippable state: consultation remains usable; internal worktree preparation is complete and tested but is not yet user-exposed.
- New user capability: none.
- Acceptance criteria:
  - Staged or unstaged changes to tracked files refuse admission.
  - Tracked deletions, renames, conflicts, type changes, and dirty tracked submodules refuse admission.
  - Non-ignored untracked files do not refuse admission.
  - Every such untracked path appears in stderr and one exact `source.untracked` event.
  - Ignored paths are neither copied nor represented as enumerated.
  - The default root resolves to `D:\ParleyWorktrees` on Windows.
  - An absolute `--worktree-root` equivalent overrides it for one test run.
  - Relative roots, nonexistent unavailable volumes, unwritable roots, traversal targets, existing targets, registered targets, and targets resolving inside the source project refuse before model launch.
  - Cross-drive creation uses detached, locked, absolute worktree linkage.
  - Windows long-path risk is refused or warned according to the amended admission rule.
  - Post-create Git validation confirms the exact target and source commit.
  - The source checkout remains unchanged.
  - Interrupted or failed setup leaves either no linked worktree or an explicitly recorded recoverable one.
- Kill criteria:
  - A tracked modification can be silently excluded.
  - An untracked path is copied without an approved snapshot rule.
  - Target resolution can escape the configured root or enter the source project.
  - Cross-drive linkage is relative, invalid, or unvalidated.
  - Codex cannot enforce workspace-write at the resulting D: worktree.
  - Cleanup can discard run work.
- Promotion criteria: controlled tests demonstrate tracked-change refusal, untracked-warning admission, cross-drive creation, path validation, correct runner policies, and retained recovery without modifying the source checkout.
- Deliberately out of scope: copying untracked or ignored files, dirty-tree snapshots, automatic cleanup, merge/push/promotion, arbitrary environment-variable configuration, and security claims based on the D: location.
- Assumptions and falsifiers: the amended §3 assumptions apply.

### PARLEY-V2-007 — Implement the bounded state machine

- Spec coverage: sections 6 and 8.
- Principal objective: deterministic maker/reviewer orchestration against injected runners.
- Smallest sufficient change: `orchestrator.py`, strict verdict parser, prompt integration, replay, and fake-runner tests; no public `run`.
- Shippable state: existing user workflows remain usable; the engine is internally complete and testable.
- New user capability: none.
- Acceptance: every state and terminal transition is tested; access policies and lane sessions remain separate; malformed verdicts block; limits and stops are mechanical.
- Kill: prose approval inference, wrong-policy launch, bound overrun, ignored stop, or automatic promotion.
- Promotion: complete state-machine coverage using deterministic runners.
- Out of scope: public CLI and real end-to-end run.
- Assumption: strict verdict JSON is viable. Falsified by controlled model tests.

### PARLEY-V2-008 — Ship the `run` workflow

- Spec coverage: sections 1–9 end to end.
- Principal objective: expose safe, bounded Codex-backed orchestration.
- Smallest sufficient change: public run/control/status commands, viewer run presentation, README safety wording, and controlled end-to-end verification.
- Shippable state: Parley v2 core is complete.
- New user capability: launch a selectable maker/reviewer loop, steer both lanes between invocations, stop it, inspect outcomes, and retain the worktree for manual disposition.
- Acceptance: all section 7 commands work; all section 3 gates hold; every terminal condition is visible; no automatic promotion exists; legacy consultation remains exact.
- Kill: any weakened consultation guarantee, unenforced writable boundary, invisible steering, unbounded loop, or automatic promotion.
- Promotion: one controlled real run reaches a terminal outcome with accurate transcript, registry, viewer, and retained diff.
- Out of scope: everything in section 9.
- Assumption: separate foreground run and control commands are operationally usable. Falsified by end-to-end testing.

### PARLEY-V2-009 — Verify Claude Code runner feasibility

- Spec coverage: conditional extension of sections 2 and 3.
- Principal objective: establish evidence before claiming Claude Code support.
- Smallest sufficient change: versioned probe document and reproducible commands only.
- Shippable state: Parley v2 remains fully usable through Codex regardless of probe result.
- New user capability: none; owner gains an evidence-backed support decision.
- Acceptance: headless I/O, sessions, cwd, policies, timeout, interruption, and exit behavior are each demonstrated or explicitly rejected.
- Kill: ambiguous safety enforcement or dependence on undocumented interactive output.
- Promotion: only demonstrated capabilities advance to PARLEY-V2-010.
- Out of scope: production Claude adapter.
- Assumption: Claude Code exposes a stable headless contract. Falsified by the probe.

### PARLEY-V2-010 — Add Claude Code conditionally

- Spec coverage: sections 1–3 and 7.
- Principal objective: add Claude Code without weakening the runner contract.
- Smallest sufficient change: `claude_runner.py`, shared contract tests, CLI resolver entry, and precise documentation.
- Shippable state: unchanged if 009 is killed; otherwise v2 gains another supported driver.
- New user capability: select Claude Code for only the lanes and policies proven safe.
- Acceptance: shared runner tests and mixed-driver orchestration pass; unsupported policies refuse before launch; transcript identity remains accurate.
- Kill: UI scraping, unrestricted fallback, overstated capability, or weakened safety gate.
- Promotion: controlled end-to-end evidence reproduces PARLEY-V2-009 through production code.
- Out of scope: generalized plugins and additional drivers.
- Assumption: the probed protocol remains stable during implementation. Falsified by version-pinned retesting.

Approval of this document authorizes the architecture and stage boundaries, not silent resolution of marked judgment calls. The next executable stage remains **PARLEY-V2-002**.

---

## 11. Surviving provider usage limits

### Scope and terminology

V2 may wait and retry only for a positively classified, temporally recoverable provider usage limit:

- Plan-usage exhaustion.
- Rate-limit exhaustion.
- A provider-reported reset window.

It must not treat these as waitable limits:

- Missing or expired session.
- Invalid authentication.
- Unsupported model.
- Malformed invocation.
- Context exhaustion without a provider reset.
- Network failure.
- Arbitrary non-zero exit.

A session that no longer exists will not become valid by sleeping. Automatic session rollover requires a transcript-derived handoff design and is not included here unless the owner explicitly adds it.

### Detection contract

Official OpenAI documentation confirms that Codex has plan-based usage limits and that interactive `/usage` exposes usage/reset information. It does **not** document a stable non-interactive `codex exec --json` rate-limit event, exit code, retry-after field, or reset timestamp. The installed package is Codex CLI 0.147.0, but the current review environment cannot force an exhaustion event. [OpenAI pricing documentation](https://learn.chatgpt.com/docs/pricing), [Codex developer commands](https://learn.chatgpt.com/docs/developer-commands?surface=cli)

OWNER AMENDMENT (approved): structural inference is permitted, on condition that
its weaker evidence is recorded rather than disguised. A detector may classify a
limit from a *proven envelope shape* carrying an unobserved value — for example an
HTTP 429 inside the `turn.failed` payload whose structure is fixture-backed but
whose status has never been captured. Every `LimitInfo` therefore carries an
`evidence` grade:

```text
evidence = observed   # this exact condition was captured in a fixture
         | structural # the envelope is fixture-backed; this value is inferred
```

`source` records WHERE the signal came from; `evidence` records HOW STRONG it is.
The two are independent, and `source="json"` alone is not permitted to imply
observation. A transcript must never present inference as capture. When a real
exhaustion is captured, the branch is regraded to `observed` and the fixture
committed.

Prose-only detection remains unauthorised: unlike the JSON envelope, no stderr
limit signature has any captured evidence for its shape, so it is not implemented.

Therefore detection must use this precedence:

1. A machine-readable JSON event proven by a captured, versioned fixture.
2. A version-pinned stderr signature proven by a captured fixture.
3. Otherwise classify the invocation as ordinary failure.

Exit code alone is never sufficient. Arbitrary error prose must not trigger hours of unattended waiting.

The runner contract gains:

```python
RunStatus = completed | partial | limited | failed | timed_out

LimitInfo(
    kind: str,                    # plan | rate
    reason: str,
    source: str,                  # json | stderr_heuristic
    retry_after_seconds: int | None,
    reset_at: str | None,
    detector_version: str,
)
```

`RunMetadata` gains:

```python
limit: LimitInfo | None
```

Rules:

- `status=limited` requires `metadata.limit`.
- A usable final answer wins over a limit marker and remains `partial`; it is not retried automatically.
- A heuristic detector must identify its source honestly in the transcript and be scoped to observed CLI versions.
- Unknown versions may use a machine-readable compatible event, but must not inherit stderr heuristics without verification.

Assumption: a real limit failure exposes enough stable evidence to distinguish it from authentication, session, and network failures. Falsified by an exhaustion capture lacking any reliable discriminator. If falsified, automatic waiting is killed rather than generalized to all failures.

### Explicit opt-in and bounds

Waiting is never default-on.

V2 exposes it only for orchestrated runs:

```text
--wait-on-limit
--max-total-limit-wait SECONDS
--max-limit-wait SECONDS
--max-consecutive-limit-waits N
```

Defaults when `--wait-on-limit` is present:

```text
max total wait per run:       21,600 seconds (6 hours)
max one wait:                  7,200 seconds (2 hours)
max consecutive waits:        6
blind-backoff base:              300 seconds (5 minutes)
blind-backoff multiplier:          3
control polling interval:          5 seconds maximum
```

Blind waits therefore progress approximately as 5, 15, 45, 120, 120, and 120 minutes, subject to the six-hour cumulative cap.

If the provider supplies a reset or retry-after:

```text
resume_after = provider time + 5-second safety margin
```

Parley refuses to wait when that delay exceeds either the remaining total-wait budget or the configured per-wait cap. It does not shorten the delay and retry prematurely.

A successful maker or reviewer invocation resets the consecutive-wait counter. The cumulative per-run wait budget never resets.

Consultation remains fail-fast in v2. Adding wait support to explicit consultation would be a separate small dispatch; the permanent bare form must not acquire new sleeping behavior.

### Transcript and viewer visibility

Add these v2 record kinds:

```text
invocation.limited
limit.wait
limit.resumed
limit.exhausted
```

`invocation.limited.data`:

```json
{
  "logical_turn_id": "uuid",
  "attempt": 1,
  "kind": "plan | rate",
  "reason": "provider diagnostic",
  "source": "json | stderr_heuristic",
  "retry_after_seconds": null,
  "reset_at": null,
  "detector_version": "driver/version"
}
```

`limit.wait.data`:

```json
{
  "logical_turn_id": "uuid",
  "attempt": 1,
  "detected_at": "timestamp",
  "reason": "provider diagnostic",
  "source": "json | stderr_heuristic",
  "resume_after": "timestamp",
  "wait_seconds": 300,
  "cumulative_wait_seconds": 300
}
```

`limit.resumed.data`:

```json
{
  "logical_turn_id": "uuid",
  "next_attempt": 2,
  "waited_seconds": 300
}
```

`limit.exhausted.data`:

```json
{
  "logical_turn_id": "uuid",
  "attempts": 6,
  "cumulative_wait_seconds": 21600,
  "reason": "consecutive | total | per_wait"
}
```

The viewer displays `WAITING_LIMIT`, the lane, reason, detector source, and computed resume time. It calculates the countdown locally; Parley does not append one transcript record per countdown tick.

The foreground process writes a countdown/status line to stderr at most once per minute.

Nothing sleeps silently.

### Resumption correctness

A limit retry is a new invocation attempt within the same logical lane turn. It is not continuation of a partially open RPC.

Identifiers:

```text
iteration       orchestration repair cycle
lane_turn       completed logical turn for one participant
logical_turn_id one intended maker or reviewer turn
attempt         provider invocation attempt within that logical turn
```

On a limit:

1. Persist `invocation.limited`.
2. Preserve the worktree exactly as it stands.
3. Persist `limit.wait` before sleeping.
4. Wait interruptibly.
5. Persist `limit.resumed`.
6. Reinvoke the same logical lane turn with `attempt + 1`.
7. Resume the same session when it remains usable.
8. Render a recovery preamble telling the participant that the previous attempt was interrupted and requiring it to inspect current worktree state and the actual diff before continuing.

The retry prompt is not byte-identical. Reissuing the original prompt blindly could duplicate maker edits. The immutable task and safety contract remain identical, but the recovery preamble and current diff evidence are refreshed.

Each attempt gets one prompt record and exactly one terminal attempt record:

```text
invocation.response
invocation.limited
invocation.failure
```

No record is overwritten. The lane turn advances only after a usable response.

Parley cannot guarantee that the provider did not charge or count work performed before the limit. It guarantees only that Parley does not count the retry as another orchestration iteration or silently duplicate transcript completion records.

If the saved session is rejected after the wait, the run becomes `BLOCKED`; v2 does not silently start a replacement session.

### State-machine interaction

Add:

```text
WAITING_LIMIT       non-terminal
LIMIT_EXHAUSTED     terminal
```

Transitions:

```text
MAKER_RUNNING
  -> WAITING_LIMIT      classified limit, opt-in enabled, budget remains

REVIEW_RUNNING
  -> WAITING_LIMIT      classified limit, opt-in enabled, budget remains

WAITING_LIMIT
  -> MAKER_RUNNING      maker retry
  -> REVIEW_RUNNING     reviewer retry
  -> STOPPED            control request or Ctrl-C
  -> LIMIT_EXHAUSTED    wait bounds reached
  -> FAILED             wait/control/storage failure
```

A limit wait consumes:

- No orchestration iteration.
- No completed lane turn.
- One invocation attempt.
- One consecutive-limit allowance.
- Its actual duration from the cumulative wait budget.

Without `--wait-on-limit`, a classified limit terminates as `LIMIT_EXHAUSTED` immediately.

### Stoppability without a daemon

The foreground `run` process must not call one hours-long `sleep()`.

During `WAITING_LIMIT`, it waits in intervals of no more than five seconds and checks:

- The durable control inbox for `stop.requested`.
- `Ctrl-C`.
- The monotonic wait deadline.
- The cumulative wait budget.

A separate invocation remains the external stop mechanism:

```text
python parley.py stop --project DIR --thread NAME --run-id ID
```

That command atomically writes a control file. The waiting foreground process consumes it within five seconds, records the stop, and terminates while retaining the worktree.

Wall-clock time is used for the viewer’s `resume_after`; monotonic time controls elapsed waiting so clock changes do not accidentally extend the authorized wait.

### Safety invariants

Limit handling may never:

- Retry an unclassified failure.
- Treat exit code alone as a limit.
- Exceed any configured wait bound.
- Reset the iteration counter.
- Change access policy, runner identity, worktree, or session without an explicit state transition.
- Start a fresh session automatically.
- Hide detector source or blind-backoff status.
- Prevent stop controls or Ctrl-C.
- Rewrite an earlier attempt record.
- Convert exhaustion into approval, revision, or promotion.

---

## 12. Output encoding

Parley configures standard output and error as UTF-8 with replacement when the host
streams support reconfiguration. A console encoding mismatch must not prevent delivery of
a completed answer. Transcript and registry state are persisted before answer printing.
If a host-supplied stream refuses reconfiguration, Parley preserves the durable record
but cannot guarantee that stream's behaviour.