"""Table-driven codec tests (PARLEY-V2-004).

Written because the first RECORD_KINDS table silently omitted
`invocation.response`, `invocation.failure`, `review.verdict`, `run.finished`
and all four limit kinds. A codec that rejects records the spec mandates would
have blocked every later dispatch, and nothing would have caught it until a
producer tried to write one.

    python -m unittest discover -p "test_*.py"
"""

from __future__ import annotations

import json
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from parley_core.storage import (
    MODEL_KINDS,
    RECORD_KINDS,
    SchemaError,
    make_record,
    normalise,
)

RUNNER = {"driver": "codex", "provider": "openai", "model": "gpt-5"}

# Everything §4 and §11 declare. Kept literal rather than derived from
# RECORD_KINDS, so an omission from the table fails instead of agreeing with
# itself.
SPEC_KINDS = frozenset(
    {
        "consult.prompt",
        "consult.response",
        "consult.failure",
        "run.created",
        "run.finished",
        "steering.set",
        "steering.clear",
        "stop.requested",
        "state.changed",
        "invocation.prompt",
        "invocation.response",
        "invocation.failure",
        "invocation.limited",
        "review.verdict",
        "limit.wait",
        "limit.resumed",
        "limit.exhausted",
    }
)


def sample(kind, **kw):
    """A record with EXACTLY the declared keys, every one explicitly present."""
    base = {
        "conversation_id": "c-1",
        "project": "/p",
        "thread": "t",
        "kind": kind,
        "participant": "reviewer" if kind in MODEL_KINDS else "system",
        "data": {k: None for k in RECORD_KINDS[kind]},
    }
    if kind in MODEL_KINDS:
        base.update(RUNNER)
    base.update(kw)
    return make_record(**base)


class EveryDeclaredKindRoundTrips(unittest.TestCase):
    def test_every_spec_kind_is_present_in_the_table(self):
        """Guards against exactly the omission that caused this file to exist."""
        self.assertEqual(SPEC_KINDS - set(RECORD_KINDS), set())

    def test_the_table_declares_nothing_the_spec_does_not(self):
        self.assertEqual(set(RECORD_KINDS) - SPEC_KINDS, set())

    def test_every_declared_kind_is_constructible(self):
        for kind in sorted(RECORD_KINDS):
            with self.subTest(kind=kind):
                rec = sample(kind)
                self.assertEqual(rec["kind"], kind)
                self.assertEqual(set(rec["data"]), set(RECORD_KINDS[kind]))

    def test_every_declared_kind_survives_a_json_round_trip(self):
        for kind in sorted(RECORD_KINDS):
            with self.subTest(kind=kind):
                rec = sample(kind)
                self.assertEqual(json.loads(json.dumps(rec)), rec)

    def test_every_declared_kind_normalises_with_a_display_role(self):
        for kind in sorted(RECORD_KINDS):
            with self.subTest(kind=kind):
                out = normalise(sample(kind))
                self.assertEqual(out["kind"], kind)
                self.assertIsNotNone(out["_display_role"])

    def test_dropping_any_single_declared_field_is_refused(self):
        """Missing is as wrong as extra: absence must never be implicit."""
        for kind in sorted(RECORD_KINDS):
            for drop in sorted(RECORD_KINDS[kind]):
                with self.subTest(kind=kind, dropped=drop):
                    data = {k: None for k in RECORD_KINDS[kind] if k != drop}
                    with self.assertRaises(SchemaError):
                        sample(kind, data=data)

    def test_adding_any_undeclared_field_is_refused(self):
        for kind in sorted(RECORD_KINDS):
            with self.subTest(kind=kind):
                data = {k: None for k in RECORD_KINDS[kind]}
                data["smuggled"] = 1
                with self.assertRaises(SchemaError):
                    sample(kind, data=data)

    def test_model_kinds_refuse_a_missing_runner_snapshot(self):
        """A transcript that cannot name the runner behind a turn is not evidence."""
        for kind in sorted(MODEL_KINDS):
            with self.subTest(kind=kind), self.assertRaises(SchemaError):
                make_record(
                    conversation_id="c",
                    project="/p",
                    thread="t",
                    kind=kind,
                    participant="reviewer",
                    data={k: None for k in RECORD_KINDS[kind]},
                )

    def test_non_model_kinds_do_not_require_a_runner(self):
        for kind in sorted(set(RECORD_KINDS) - MODEL_KINDS):
            with self.subTest(kind=kind):
                self.assertIsNone(sample(kind)["driver"])


class LimitEvidenceReachesTheViewer(unittest.TestCase):
    """Section 11: an inference must never be renderable as a capture."""

    LIMIT = types.MappingProxyType(
        {
            "kind": "rate",
            "reason": "slow down",
            "source": "json",
            "evidence": "structural",
            "retry_after_seconds": 300,
            "reset_at": None,
            "detector_version": "codex/0.147.0/1",
        }
    )

    def test_a_dedicated_limited_record_exposes_its_evidence(self):
        rec = sample(
            "invocation.limited",
            data={"logical_turn_id": "u", "attempt": 1, **self.LIMIT},
        )
        out = normalise(rec)
        self.assertEqual(out["_limit"]["evidence"], "structural")
        self.assertEqual(out["_limit"]["retry_after_seconds"], 300)

    def test_a_limit_inside_response_metadata_is_surfaced_too(self):
        rec = sample(
            "consult.response",
            data={
                "session_out": "s",
                "run_status": "limited",
                "metadata": {"limit": self.LIMIT},
            },
        )
        self.assertEqual(normalise(rec)["_limit"]["evidence"], "structural")

    def test_a_record_without_a_limit_exposes_none(self):
        rec = sample(
            "consult.response",
            data={"session_out": "s", "run_status": "completed", "metadata": {}},
        )
        self.assertNotIn("_limit", normalise(rec))


class FailuresAndUnknownKinds(unittest.TestCase):
    def test_v2_failure_kinds_are_marked(self):
        for kind in ("consult.failure", "invocation.failure"):
            with self.subTest(kind=kind):
                self.assertTrue(normalise(sample(kind))["_error"])

    def test_a_non_failure_v2_record_is_not_marked(self):
        self.assertFalse(normalise(sample("consult.response"))["_error"])

    def test_an_unknown_future_kind_renders_as_a_neutral_system_note(self):
        """Forward compatibility: never drop it, never mislabel it as a turn."""
        rec = {
            "schema": 2,
            "kind": "future.thing",
            "participant": "reviewer",
            "text": "hello",
            "data": {},
        }
        out = normalise(rec)
        self.assertEqual(out["_display_role"], "system")
        self.assertEqual(out["_unknown_kind"], "future.thing")
        self.assertEqual(out["text"], "hello")


if __name__ == "__main__":
    unittest.main(verbosity=2)
