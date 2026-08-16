# Captured Codex fixtures — CLI 0.147.0

Real output from `codex exec`, captured on this machine. Nothing here is
synthetic. Each file records what the CLI actually emitted.

| file | how it was produced | exit |
|---|---|---|
| `success.stdout.jsonl` | a normal read-only turn | 0 |
| `invalid_model.stdout.jsonl` | `-m definitely-not-a-model-xyz` | 1 |
| `expired_session.stderr.txt` | `resume` with a non-existent thread id | 1 |

## What they establish

**Errors are machine-readable.** A failing turn emits `{"type":"error",...}` and
`{"type":"turn.failed","error":{"message":...}}` on stdout, where `message` is a
JSON *string* containing a nested payload with an HTTP `status` and an
`error.type`. That is a structured discriminator, not error prose.

**Not every failure reaches the stream.** `resume` against a missing thread fails
before the thread starts, so it produces no JSON at all — only stderr. Any
classifier reading solely the JSON stream would see nothing for that case.

**A successful turn carries no limit signal.** `turn.completed` carries `usage`
token counts only: no quota, no rate-limit, no reset, no retry-after.

## What is NOT captured

**A real usage-limit exhaustion (HTTP 429).** It cannot be forced on demand
without deliberately burning a plan allowance. Under the owner-approved spec
amendment, the classifier may infer a limit from the *proven envelope shape*
carrying an unobserved status — but must record that weaker evidence rather than
disguise it. Every `LimitInfo` therefore carries `evidence="structural"` for this
branch, alongside `source="json"`.

`source` says where the signal came from; `evidence` says how strong it is. They
are independent, because `source="json"` alone would let inference read as
capture.

**Regrade on capture.** The first time a real exhaustion occurs in normal use,
commit the fixture here and change that branch to `evidence="observed"`.

The captured non-limit failures ARE used as false-positive coverage: they must
never classify as `limited`.
