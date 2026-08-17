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
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import serve
from parley_core.storage import make_record

HASH = "b" * 32
RUNNER = {"driver": "codex", "provider": "openai", "model": "gpt-5"}
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
                {"logical_turn_id": "u", "attempt": 1, **dict(LIMIT)},
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
                    "metadata": {"limit": dict(LIMIT)},
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


class WaitingLimitRenders(unittest.TestCase):
    """§11 requires a wait in progress to be a DISTINCT state from a classified one."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.logdir = Path(self.tmp.name)
        patch = mock.patch.object(serve, "LOGDIR", self.logdir)
        patch.start()
        self.addCleanup(patch.stop)

    WAIT = types.MappingProxyType(
        {
            "logical_turn_id": "u-1",
            "attempt": 2,
            "detected_at": "2026-08-17T00:00:00Z",
            "reason": "rate limited",
            "source": "json",
            "evidence": "structural",
            "blind": True,
            "resume_after": "2026-08-17T00:05:00Z",
            "wait_seconds": 300,
            "cumulative_wait_seconds": 300,
        }
    )

    def served(self, kind, data):
        rec = make_record(
            conversation_id="c",
            project="/p",
            thread="t",
            kind=kind,
            participant="reviewer",
            data=data,
            **RUNNER,
        )
        (self.logdir / f"w-{HASH}.jsonl").write_text(
            json.dumps(rec) + "\n", encoding="utf-8"
        )
        return serve.messages(f"w-{HASH}", 0)["messages"][0]

    def test_a_wait_record_declares_the_waiting_state(self):
        m = self.served("limit.wait", dict(self.WAIT))
        self.assertEqual(m["_limit_state"], "WAITING_LIMIT")

    def test_a_classified_but_unwaited_limit_stays_limited(self):
        m = self.served(
            "invocation.limited",
            {
                "logical_turn_id": "u",
                "attempt": 1,
                "kind": "rate",
                "reason": "r",
                "source": "json",
                "evidence": "structural",
                "retry_after_seconds": None,
                "reset_at": None,
                "detector_version": "codex/0.147.0/1",
            },
        )
        self.assertEqual(m["_limit_state"], "LIMITED")

    def test_blind_is_carried_explicitly_not_inferred(self):
        m = self.served("limit.wait", dict(self.WAIT))
        self.assertIs(m["_limit"]["blind"], True)

    def test_provider_directed_waits_say_so(self):
        w = dict(self.WAIT, blind=False)
        self.assertIs(self.served("limit.wait", w)["_limit"]["blind"], False)

    def test_wait_seconds_and_resume_time_are_served(self):
        m = self.served("limit.wait", dict(self.WAIT))
        self.assertEqual(m["_wait"]["wait_seconds"], 300)
        self.assertEqual(m["_wait"]["resume_after"], "2026-08-17T00:05:00Z")

    def test_evidence_grade_is_served_for_a_wait(self):
        self.assertEqual(
            self.served("limit.wait", dict(self.WAIT))["_limit"]["evidence"],
            "structural",
        )


class RenderBodyOnly(unittest.TestCase):
    """Scope the static assertions to render(), so a comment cannot satisfy them."""

    def setUp(self):
        src = (Path(__file__).resolve().parent / "serve.py").read_text(encoding="utf-8")
        body = re.search(r"function render\(m\)\{(.*?)\n\}", src, re.DOTALL)
        assert body, "could not isolate render()"
        # strip // comments so prose cannot satisfy a check
        self.body = re.sub(r"//[^\n]*", "", body.group(1))

    def test_render_uses_display_role(self):
        self.assertIn("m._display_role", self.body)

    def test_render_uses_the_error_flag(self):
        self.assertIn("m._error === true", self.body)

    def test_render_uses_the_limit_field(self):
        self.assertIn("m._limit", self.body)

    def test_render_uses_the_limit_state_for_the_label(self):
        self.assertIn("limState", self.body)

    def test_render_uses_the_explicit_blind_field(self):
        self.assertIn("lim.blind", self.body)

    def test_render_shows_the_resume_time(self):
        self.assertIn("resume_after", self.body)

    def test_render_never_reads_the_removed_role_field(self):
        self.assertNotIn("m.role", self.body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
