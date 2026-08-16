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
without deliberately burning a plan allowance. The classifier therefore treats
429 / rate-limit as `limited` **by structural analogy to the proven envelope**,
and that specific status has not been observed here. Until a 429 fixture is
captured, `limited` is a structurally-supported but empirically-unconfirmed
classification, and it is labelled as such in code.

The captured non-limit failures ARE used as false-positive coverage: they must
never classify as `limited`.
