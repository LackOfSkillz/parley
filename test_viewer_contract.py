"""Viewer-contract tests (PARLEY-V2-004).

The browser reads exactly three normalised fields -- `_display_role`, `_error`
and `_limit` -- and nothing else. These assert the server always supplies them,
and that the rendering code in serve.py consumes only those.

This file exists because an untested fallback to an alternate field name is what
let a browser render every reviewer verdict as though the implementer had
written it, after normalisation renamed `role` to `participant`.

    python -m unittest discover -p "test_*.py"
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import serve
from parley_core.storage import make_record

HASH = "b" * 32
RUNNER = {"driver": "codex", "provider": "openai", "model": "gpt-5"}
LIMIT = {
    "kind": "rate",
    "reason": "slow down",
    "source": "json",
    "evidence": "structural",
    "retry_after_seconds": 300,
    "reset_at": None,
    "detector_version": "codex/0.147.0/1",
}


class ServedShape(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.logdir = Path(self.tmp.name)
        patch = mock.patch.object(serve, "LOGDIR", self.logdir)
        patch.start()
        self.addCleanup(patch.stop)

    def serve_records(self, *records):
        lines = [json.dumps(r) for r in records]
        (self.logdir / f"t-{HASH}.jsonl").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )
        return serve.messages(f"t-{HASH}", 0)["messages"]

    def v2(self, kind, data, participant="reviewer", **kw):
        return make_record(
            conversation_id="c",
            project="/p",
            thread="t",
            kind=kind,
            participant=participant,
            data=data,
            **RUNNER,
            **kw,
        )

    # -- the three fields the browser keys on ------------------------------
    def test_every_record_supplies_display_role(self):
        recs = self.serve_records(
            {"ts": "t", "role": "claude", "text": "q"},
            self.v2(
                "consult.response",
                {"session_out": None, "run_status": None, "metadata": {}},
            ),
        )
        for m in recs:
            self.assertIn(m["_display_role"], ("maker", "reviewer", "system"))

    def test_error_flag_is_a_real_boolean_on_both_schemas(self):
        recs = self.serve_records(
            {"ts": "t", "role": "gpt", "error": True, "text": "boom"},
            self.v2(
                "consult.failure",
                {"error_type": None, "diagnostic": None, "retained_output_path": None},
            ),
            self.v2(
                "consult.response",
                {"session_out": None, "run_status": None, "metadata": {}},
            ),
        )
        self.assertEqual([m["_error"] for m in recs], [True, True, False])

    def test_a_dedicated_limited_record_supplies_limit(self):
        recs = self.serve_records(
            self.v2(
                "invocation.limited",
                {"logical_turn_id": "u", "attempt": 1, **LIMIT},
            )
        )
        self.assertEqual(recs[0]["_limit"]["evidence"], "structural")
        self.assertEqual(recs[0]["_limit"]["retry_after_seconds"], 300)

    def test_a_metadata_limit_supplies_limit_too(self):
        recs = self.serve_records(
            self.v2(
                "consult.response",
                {
                    "session_out": None,
                    "run_status": "limited",
                    "metadata": {"limit": LIMIT},
                },
            )
        )
        self.assertEqual(recs[0]["_limit"]["evidence"], "structural")

    def test_an_unknown_future_kind_is_served_as_system(self):
        raw = {
            "schema": 2,
            "kind": "future.thing",
            "participant": "reviewer",
            "text": "hello",
            "data": {},
        }
        recs = self.serve_records(raw)
        self.assertEqual(recs[0]["_display_role"], "system")
        self.assertEqual(recs[0]["_unknown_kind"], "future.thing")


class RenderingCodeUsesOnlyTheContract(unittest.TestCase):
    """Static assertions over serve.py's client script.

    A JS unit harness would mean adding a framework this project deliberately
    does not have, so the rendering path is pinned by reading it. Crude, but it
    catches exactly the regression that occurred: a fallback to a field the
    server no longer emits.
    """

    SRC = Path(__file__).resolve().parent / "serve.py"

    def setUp(self):
        self.js = self.SRC.read_text(encoding="utf-8")

    def test_lane_comes_from_display_role(self):
        self.assertIn("const lane = m._display_role;", self.js)

    def test_failure_comes_from_the_error_flag(self):
        self.assertIn("const failed = m._error === true;", self.js)

    def test_limit_comes_from_the_limit_field(self):
        self.assertIn("const lim = m._limit || null;", self.js)

    def test_no_fallback_to_the_removed_role_field(self):
        """The exact fallback that caused the misattribution."""
        self.assertNotIn("m.role", self.js)

    def test_no_fallback_to_raw_participant_or_nested_limit(self):
        self.assertNotIn("m.participant ||", self.js)
        self.assertNotIn("m.data && m.data.limit", self.js)

    def test_evidence_and_blind_status_are_rendered(self):
        self.assertIn("evidence: ", self.js)
        self.assertIn("wait would be a guess", self.js)
        self.assertIn("provider asked ", self.js)

    def test_unknown_kinds_are_surfaced_rather_than_hidden(self):
        self.assertIn("unrecognised kind: ", self.js)

    def test_v1_records_are_labelled_as_having_no_runner(self):
        self.assertIn("runner unknown", self.js)

    def test_a_missing_lane_cannot_crash_the_label(self):
        self.assertTrue(re.search(r"\(lane\s*\|\|\s*'system'\)", self.js))


if __name__ == "__main__":
    unittest.main(verbosity=2)
