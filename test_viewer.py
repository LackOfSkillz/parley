"""Viewer normalisation tests (PARLEY-V2-004).

The viewer is the only place a human sees what happened, so a record that
renders as something it is not is the most dangerous defect this project can
have. These pin that v1 and v2 both render, that old transcripts keep working,
and that the server never serves a file outside its log directory.

    python -m unittest discover -p "test_*.py"
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import serve
from parley_core.storage import make_record

HASH = "a" * 32


def v1(role, text, **kw):
    rec = {
        "ts": "2026-08-01T00:00:00Z",
        "role": role,
        "turn": 1,
        "project": "C:\\Dev\\proj",
        "thread": "main",
        "text": text,
    }
    rec.update(kw)
    return json.dumps(rec)


def v2(text, **kw):
    base = {
        "conversation_id": "c-1",
        "project": "C:\\Dev\\proj",
        "thread": "main",
        "kind": "consult.response",
        "participant": "reviewer",
        "data": {"session_out": "s", "run_status": "completed", "metadata": {}},
        "text": text,
        "driver": "codex",
        "provider": "openai",
        "model": "gpt-5",
    }
    base.update(kw)
    return json.dumps(make_record(**base))


class ViewerHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.logdir = Path(self.tmp.name)
        p = mock.patch.object(serve, "LOGDIR", self.logdir)
        p.start()
        self.addCleanup(p.stop)

    def write(self, name, *lines):
        (self.logdir / f"{name}.jsonl").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )


class OldTranscriptsStillRender(ViewerHarness):
    """The kill criterion for this dispatch: never make v1 unreadable."""

    def test_a_pure_v1_transcript_still_lists_and_reads(self):
        self.write(f"proj-main-{HASH}", v1("claude", "q"), v1("gpt", "a"))
        threads = serve.threads()
        self.assertEqual(len(threads), 1)
        self.assertEqual(threads[0]["messages"], 2)
        msgs = serve.messages(f"proj-main-{HASH}", 0)["messages"]
        self.assertEqual([m["text"] for m in msgs], ["q", "a"])

    def test_v1_project_and_thread_survive_into_the_listing(self):
        self.write(f"proj-main-{HASH}", v1("gpt", "a"))
        t = serve.threads()[0]
        self.assertEqual(t["project"], "C:\\Dev\\proj")
        self.assertEqual(t["thread"], "main")

    def test_a_v1_error_record_is_still_marked(self):
        self.write(f"proj-main-{HASH}", v1("gpt", "boom", error=True))
        m = serve.messages(f"proj-main-{HASH}", 0)["messages"][0]
        self.assertTrue(m["_error"])


class MixedTranscriptsRender(ViewerHarness):
    def test_a_mixed_file_renders_every_record_in_file_order(self):
        self.write(
            f"proj-main-{HASH}", v1("claude", "one"), v2("two"), v1("gpt", "three")
        )
        msgs = serve.messages(f"proj-main-{HASH}", 0)["messages"]
        self.assertEqual([m["text"] for m in msgs], ["one", "two", "three"])

    def test_every_record_exposes_a_lane_regardless_of_schema(self):
        self.write(f"proj-main-{HASH}", v1("claude", "q"), v2("a"))
        msgs = serve.messages(f"proj-main-{HASH}", 0)["messages"]
        self.assertEqual([m["participant"] for m in msgs], ["maker", "reviewer"])

    def test_v2_records_expose_runner_identity_and_v1_records_do_not(self):
        self.write(f"proj-main-{HASH}", v1("gpt", "old"), v2("new"))
        old, new = serve.messages(f"proj-main-{HASH}", 0)["messages"]
        self.assertIsNone(old["driver"])
        self.assertEqual(new["driver"], "codex")
        self.assertEqual(new["model"], "gpt-5")

    def test_the_since_cursor_works_across_schemas(self):
        self.write(f"proj-main-{HASH}", v1("claude", "one"), v2("two"))
        page = serve.messages(f"proj-main-{HASH}", 1)
        self.assertEqual(page["total"], 2)
        self.assertEqual([m["text"] for m in page["messages"]], ["two"])


class ServingIsConfined(ViewerHarness):
    def test_a_traversal_id_is_refused(self):
        self.assertEqual(
            serve.messages(f"../../etc/passwd-{HASH}", 0), {"total": 0, "messages": []}
        )

    def test_an_id_without_the_digest_is_refused(self):
        self.write("legacy_name", v1("gpt", "a"))
        self.assertEqual(serve.messages("legacy_name", 0), {"total": 0, "messages": []})

    def test_pre_hash_transcripts_are_not_listed(self):
        self.write("C_Dev_proj_main", v1("gpt", "a"))
        self.assertEqual(serve.threads(), [])

    def test_a_missing_transcript_is_empty_not_an_error(self):
        self.assertEqual(
            serve.messages(f"absent-{HASH}", 0), {"total": 0, "messages": []}
        )


class ParticipantIsNeverMisattributed(unittest.TestCase):
    """A turn rendered as the wrong participant is this project's worst defect.

    It happened: normalisation replaced the v1 `role` field with `participant`,
    and a browser still running the previous JavaScript read `role`, found
    nothing, and fell back to the default -- so every reviewer verdict displayed
    as though the implementer had written it. The API contract is pinned here so
    the failure cannot recur silently on the server side.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.logdir = Path(self.tmp.name)
        p = mock.patch.object(serve, "LOGDIR", self.logdir)
        p.start()
        self.addCleanup(p.stop)

    def write(self, *lines):
        (self.logdir / f"t-{HASH}.jsonl").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )

    def msgs(self):
        return serve.messages(f"t-{HASH}", 0)["messages"]

    def test_every_served_record_carries_participant(self):
        """The field the viewer keys on must always be present."""
        self.write(v1("claude", "q"), v1("gpt", "a"), v2("v2 reply"))
        for m in self.msgs():
            self.assertIn(m.get("participant"), ("maker", "reviewer"))

    def test_reviewer_turns_are_never_attributed_to_the_maker(self):
        self.write(v1("claude", "q"), v1("gpt", "VERDICT reject"))
        maker, reviewer = self.msgs()
        self.assertEqual(maker["participant"], "maker")
        self.assertEqual(reviewer["participant"], "reviewer")
        self.assertIn("VERDICT", reviewer["text"])

    def test_an_alternating_transcript_alternates_exactly(self):
        lines = []
        for i in range(6):
            lines.append(v1("claude", f"q{i}"))
            lines.append(v1("gpt", f"a{i}"))
        self.write(*lines)
        lanes = [m["participant"] for m in self.msgs()]
        self.assertEqual(lanes, ["maker", "reviewer"] * 6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
