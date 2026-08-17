"""Storage-layer tests (PARLEY-V2-004).

Two schemas coexist permanently, so the tests that matter most are the ones
proving neither corrupts the other and that v1 history is never rewritten.

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
from parley_core import storage
from parley_core.storage import (
    RegistryCorrupt,
    SchemaError,
    blank_v2,
    import_v1,
    is_v2,
    load_registry,
    make_record,
    normalise,
    project_v1,
    read_transcript,
    write_atomic,
)

V1_ENTRY = {
    "session_id": "sess-1",
    "project": "C:\\Dev\\proj",
    "thread": "main",
    "turns": 3,
    "created_at": "2026-08-01T00:00:00Z",
    "updated_at": "2026-08-02T00:00:00Z",
}


class RecordEnvelope(unittest.TestCase):
    def rec(self, **kw):
        base = {
            "conversation_id": "c-1",
            "project": "/p",
            "thread": "main",
            "kind": "consult.prompt",
            "participant": "maker",
            "data": {
                "mode": "ask",
                "attached": [],
                "access_policy": "read_only",
                "session_in": None,
            },
            "driver": "codex",
            "provider": "openai",
            "model": "gpt-5",
        }
        base.update(kw)
        return make_record(**base)

    def test_envelope_has_exactly_the_declared_keys(self):
        self.assertEqual(
            set(self.rec()),
            {
                "schema",
                "event_id",
                "ts",
                "conversation_id",
                "project",
                "thread",
                "run_id",
                "kind",
                "participant",
                "driver",
                "provider",
                "model",
                "lane_turn",
                "iteration",
                "steering_id",
                "text",
                "data",
            },
        )

    def test_unknown_kind_is_refused(self):
        with self.assertRaises(SchemaError):
            self.rec(kind="consult.invented")

    def test_unknown_participant_is_refused(self):
        with self.assertRaises(SchemaError):
            self.rec(participant="oracle")

    def test_undeclared_data_field_is_refused(self):
        """A free-form data bag is how a schema stops being one."""
        with self.assertRaises(SchemaError):
            self.rec(data={"mode": "ask", "smuggled": 1})

    def test_human_records_may_not_carry_runner_identity(self):
        with self.assertRaises(SchemaError):
            make_record(
                conversation_id="c",
                project="/p",
                thread="t",
                kind="steering.set",
                participant="human",
                data={"author": "gary", "supersedes": None},
                driver="codex",
            )

    def test_records_are_json_safe(self):
        json.dumps(self.rec())

    def test_event_ids_are_unique(self):
        self.assertNotEqual(self.rec()["event_id"], self.rec()["event_id"])


class V1IsNeverRewritten(unittest.TestCase):
    """The one guarantee that cannot be traded away."""

    def test_import_does_not_mutate_the_v1_input(self):
        src = {"c-1": dict(V1_ENTRY)}
        before = json.dumps(src, sort_keys=True)
        import_v1(src)
        self.assertEqual(json.dumps(src, sort_keys=True), before)

    def test_loading_leaves_the_v1_file_byte_identical(self):
        with tempfile.TemporaryDirectory() as d:
            v1 = Path(d) / "threads.json"
            v1.write_text(json.dumps({"c-1": V1_ENTRY}), encoding="utf-8")
            before = v1.read_bytes()
            load_registry(Path(d) / "registry-v2.json", v1)
            self.assertEqual(v1.read_bytes(), before)

    def test_reading_a_transcript_does_not_touch_the_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.jsonl"
            p.write_text(
                json.dumps({"ts": "x", "role": "gpt", "text": "hi"}) + "\n",
                encoding="utf-8",
            )
            before = p.read_bytes()
            read_transcript(p)
            self.assertEqual(p.read_bytes(), before)


class RegistryRoundTrip(unittest.TestCase):
    def test_v1_import_preserves_session_turns_and_timestamps(self):
        v2 = import_v1({"c-1": V1_ENTRY})
        c = v2["conversations"]["c-1"]
        self.assertEqual(c["consult"]["reviewer"]["session"], "sess-1")
        self.assertEqual(c["consult"]["turns"], 3)
        self.assertEqual(c["created_at"], V1_ENTRY["created_at"])
        self.assertEqual(c["updated_at"], V1_ENTRY["updated_at"])

    def test_session_origin_is_null_because_v1_never_recorded_it(self):
        v2 = import_v1({"c-1": V1_ENTRY})
        self.assertIsNone(
            v2["conversations"]["c-1"]["consult"]["reviewer"]["session_origin"]
        )

    def test_projection_back_to_v1_is_lossless(self):
        self.assertEqual(project_v1(import_v1({"c-1": V1_ENTRY})), {"c-1": V1_ENTRY})

    def test_projection_emits_exactly_the_six_v1_fields(self):
        out = project_v1(import_v1({"c-1": V1_ENTRY}))["c-1"]
        self.assertEqual(set(out), set(V1_ENTRY))

    def test_v2_registry_wins_when_both_exist(self):
        with tempfile.TemporaryDirectory() as d:
            v1 = Path(d) / "threads.json"
            v2 = Path(d) / "registry-v2.json"
            v1.write_text(json.dumps({"c-1": V1_ENTRY}), encoding="utf-8")
            authoritative = blank_v2()
            authoritative["conversations"]["c-2"] = {"project": "/x", "thread": "t"}
            write_atomic(v2, authoritative)
            self.assertEqual(list(load_registry(v2, v1)["conversations"]), ["c-2"])

    def test_a_corrupt_v2_registry_fails_closed(self):
        """v1 cannot represent runs or lane sessions.

        Falling back would present a v2 conversation as though those had never
        existed -- intact-looking and wrong. Failing loudly keeps the damage
        visible, and the transcripts remain the durable record.
        """
        with tempfile.TemporaryDirectory() as d:
            v1 = Path(d) / "threads.json"
            v2 = Path(d) / "registry-v2.json"
            v1.write_text(json.dumps({"c-1": V1_ENTRY}), encoding="utf-8")
            v2.write_text("{ not json", encoding="utf-8")
            with self.assertRaises(RegistryCorrupt) as ctx:
                load_registry(v2, v1)
            self.assertIn("Refusing to fall back", str(ctx.exception))

    def test_a_v2_registry_with_the_wrong_schema_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            v2 = Path(d) / "registry-v2.json"
            v2.write_text(json.dumps({"schema": 99}), encoding="utf-8")
            with self.assertRaises(RegistryCorrupt):
                load_registry(v2, Path(d) / "threads.json")

    def test_an_unreadable_v1_degrades_to_empty_rather_than_raising(self):
        """v1 is a compatibility projection, not authoritative state."""
        with tempfile.TemporaryDirectory() as d:
            v1 = Path(d) / "threads.json"
            v1.write_text("{ not json", encoding="utf-8")
            self.assertEqual(
                load_registry(Path(d) / "registry-v2.json", v1), blank_v2()
            )

    def test_absent_registries_give_an_empty_v2(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(
                load_registry(Path(d) / "a.json", Path(d) / "b.json"), blank_v2()
            )


class AtomicWrites(unittest.TestCase):
    def test_write_replaces_in_one_step(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "r.json"
            write_atomic(p, {"schema": 2, "conversations": {}})
            self.assertEqual(json.loads(p.read_text(encoding="utf-8"))["schema"], 2)

    def test_a_failed_write_leaves_the_old_file_intact(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "r.json"
            write_atomic(p, {"schema": 2, "conversations": {"keep": {}}})
            before = p.read_bytes()
            with (
                mock.patch.object(storage.json, "dump", side_effect=OSError("disk")),
                self.assertRaises(OSError),
            ):
                write_atomic(p, {"schema": 2, "conversations": {"new": {}}})
            self.assertEqual(p.read_bytes(), before)

    def test_a_failed_write_leaves_no_temp_file_behind(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "r.json"
            with (
                mock.patch.object(storage.json, "dump", side_effect=OSError("disk")),
                self.assertRaises(OSError),
            ):
                write_atomic(p, {"schema": 2})
            self.assertEqual(list(Path(d).glob(".parley-*")), [])


class MixedSchemaTranscripts(unittest.TestCase):
    """A conversation that predates v2 and continues under it lives in one file."""

    def v1_line(self, role, text, turn=1):
        return json.dumps(
            {
                "ts": "2026-08-01T00:00:00Z",
                "role": role,
                "turn": turn,
                "project": "/p",
                "thread": "main",
                "text": text,
            }
        )

    def v2_line(self, text):
        return json.dumps(
            make_record(
                conversation_id="c-1",
                project="/p",
                thread="main",
                kind="consult.response",
                participant="reviewer",
                data={"session_out": "s", "run_status": "completed", "metadata": {}},
                text=text,
                driver="codex",
                provider="openai",
                model="gpt-5",
            )
        )

    def transcript(self, *lines):
        d = tempfile.mkdtemp()
        p = Path(d) / "t.jsonl"
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return p

    def test_both_schemas_normalise_to_one_shape(self):
        p = self.transcript(self.v1_line("claude", "q"), self.v2_line("a"))
        recs = read_transcript(p)
        self.assertEqual(len(recs), 2)
        for r in recs:
            self.assertIn("participant", r)
            self.assertIn("text", r)

    def test_file_order_is_preserved_across_schemas(self):
        """Position is chronology in an append-only log."""
        p = self.transcript(
            self.v1_line("claude", "first"),
            self.v2_line("second"),
            self.v1_line("gpt", "third"),
        )
        self.assertEqual(
            [r["text"] for r in read_transcript(p)], ["first", "second", "third"]
        )

    def test_v1_vendor_roles_map_to_lanes(self):
        p = self.transcript(self.v1_line("claude", "q"), self.v1_line("gpt", "a"))
        recs = read_transcript(p)
        self.assertEqual(recs[0]["participant"], "maker")
        self.assertEqual(recs[1]["participant"], "reviewer")

    def test_v1_records_admit_they_have_no_runner_identity(self):
        """v1 never recorded it; inventing one would be worse than the gap."""
        p = self.transcript(self.v1_line("gpt", "a"))
        r = read_transcript(p)[0]
        self.assertIsNone(r["driver"])
        self.assertIsNone(r["provider"])
        self.assertIsNone(r["model"])

    def test_v2_records_carry_runner_identity(self):
        p = self.transcript(self.v2_line("a"))
        r = read_transcript(p)[0]
        self.assertEqual(
            (r["driver"], r["provider"], r["model"]), ("codex", "openai", "gpt-5")
        )

    def test_a_v1_error_record_is_recognised(self):
        line = json.dumps(
            {
                "ts": "t",
                "role": "gpt",
                "turn": 1,
                "project": "/p",
                "thread": "m",
                "error": True,
                "text": "[TURN FAILED]",
            }
        )
        r = read_transcript(self.transcript(line))[0]
        self.assertTrue(r["_error"])
        self.assertEqual(r["kind"], "consult.failure")

    def test_a_torn_final_line_does_not_hide_earlier_history(self):
        p = self.transcript(self.v1_line("claude", "kept"), '{"ts": "x", "rol')
        self.assertEqual([r["text"] for r in read_transcript(p)], ["kept"])

    def test_is_v2_discriminates(self):
        self.assertTrue(is_v2(json.loads(self.v2_line("a"))))
        self.assertFalse(is_v2(json.loads(self.v1_line("gpt", "a"))))

    def test_normalise_is_a_read_time_projection_not_a_mutation(self):
        original = json.loads(self.v1_line("gpt", "a"))
        snapshot = json.dumps(original, sort_keys=True)
        normalise(original)
        self.assertEqual(json.dumps(original, sort_keys=True), snapshot)


if __name__ == "__main__":
    unittest.main(verbosity=2)
