"""Regression tests for parley.

Every case here corresponds to a defect found in review, so a failure means a
guarantee in the README has stopped being true. Stdlib unittest only.

    python -m unittest discover -p "test_*.py"
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import parley
import serve


class SandboxPinning(unittest.TestCase):
    """The read-only sandbox must be pinned on EVERY turn, not just fresh ones."""

    def test_fresh_turn_pins_read_only(self):
        cmd = parley.build_cmd(Path.cwd(), None, None, Path("out.md"))
        self.assertIn("-s", cmd)
        self.assertEqual(cmd[cmd.index("-s") + 1], "read-only")

    def test_resumed_turn_also_pins_read_only(self):
        cmd = parley.build_cmd(Path.cwd(), "abc-123", None, Path("out.md"))
        self.assertIn("-s", cmd)
        self.assertEqual(cmd[cmd.index("-s") + 1], "read-only")

    def test_exec_options_precede_the_resume_subcommand(self):
        # `resume` accepts only its own arguments; a trailing -s is an
        # "unexpected argument" error rather than an override.
        cmd = parley.build_cmd(Path.cwd(), "abc-123", None, Path("out.md"))
        self.assertLess(cmd.index("-s"), cmd.index("resume"))

    def test_resume_does_not_pass_cd(self):
        cmd = parley.build_cmd(Path.cwd(), "abc-123", None, Path("out.md"))
        self.assertNotIn("-C", cmd)

    def test_fresh_passes_cd(self):
        cmd = parley.build_cmd(Path("/tmp/x"), None, None, Path("out.md"))
        self.assertIn("-C", cmd)


class ThreadIdentity(unittest.TestCase):
    """Distinct conversations must not collide into one transcript.

    "Not" here is probabilistic, not absolute: identity is a 128-bit
    digest, so collision is negligible rather than impossible.
    """

    def test_equivalent_paths_give_one_identity(self):
        with tempfile.TemporaryDirectory() as d:
            a = parley.thread_id(Path(d), "x")
            b = parley.thread_id(Path(d + os.sep), "x")
            self.assertEqual(a, b)

    def test_case_variants_agree_where_the_os_folds_case(self):
        with tempfile.TemporaryDirectory() as d:
            same = parley.canonical_key(Path(d), "x") == parley.canonical_key(
                Path(d.upper()), "x"
            )
            self.assertEqual(same, os.path.normcase("A") == os.path.normcase("a"))

    def test_separator_cannot_be_forged(self):
        # ("a::b", "c") must not collide with ("a", "b::c") -- the old "::" join
        # made exactly that possible.
        self.assertNotEqual(
            parley.canonical_key(Path("/p/a"), "b\x00c"),
            parley.canonical_key(Path("/p/a\x00b"), "c"),
        )

    def test_long_paths_differing_late_do_not_collide(self):
        # The old scheme truncated at 120 chars, so long sibling paths collided.
        long_a = Path("/" + "x" * 200 + "/alpha")
        long_b = Path("/" + "x" * 200 + "/beta")
        self.assertNotEqual(
            parley.thread_id(long_a, "t"), parley.thread_id(long_b, "t")
        )

    def test_punctuation_only_difference_does_not_collide(self):
        # Both slugify to the same characters; only the hash separates them.
        self.assertNotEqual(
            parley.thread_id(Path("/p/a-b"), "t"), parley.thread_id(Path("/p/a.b"), "t")
        )

    def test_id_carries_a_hash_suffix(self):
        tid = parley.thread_id(Path("/p/proj"), "main")
        self.assertRegex(tid, r"-[0-9a-f]{32}$")


class AnswerPreservation(unittest.TestCase):
    """Cleanup must never destroy a completed answer."""

    def _proc(self, rc=0, stdout='{"thread_id":"t-1"}\n'):
        return subprocess.CompletedProcess([], rc, stdout=stdout, stderr="")

    def test_unlink_failure_still_returns_the_answer(self):
        with (
            mock.patch.object(parley, "codex_bin", return_value="codex"),
            mock.patch.object(parley.subprocess, "run", return_value=self._proc()),
            mock.patch.object(Path, "read_text", return_value="THE ANSWER"),
            mock.patch.object(Path, "is_file", return_value=True),
            mock.patch.object(Path, "unlink", side_effect=OSError("locked")),
        ):
            answer, sid, partial = parley.run_codex("p", Path.cwd(), None, None, 10)
        self.assertEqual(answer, "THE ANSWER")
        self.assertEqual(sid, "t-1")
        self.assertFalse(partial)

    def test_read_failure_retains_the_file_and_reports_its_path(self):
        with (
            mock.patch.object(parley, "codex_bin", return_value="codex"),
            mock.patch.object(parley.subprocess, "run", return_value=self._proc()),
            mock.patch.object(Path, "is_file", return_value=True),
            mock.patch.object(Path, "read_text", side_effect=OSError("io")),
            mock.patch.object(Path, "unlink") as unlink,
            self.assertRaises(SystemExit) as ctx,
        ):
            parley.run_codex("p", Path.cwd(), None, None, 10)
        self.assertIn("retained at", str(ctx.exception))
        unlink.assert_not_called()  # the temp file may be the only copy

    def test_nonzero_exit_with_output_is_kept_but_marked_partial(self):
        with (
            mock.patch.object(parley, "codex_bin", return_value="codex"),
            mock.patch.object(parley.subprocess, "run", return_value=self._proc(rc=3)),
            mock.patch.object(Path, "is_file", return_value=True),
            mock.patch.object(Path, "read_text", return_value="PARTIAL"),
            mock.patch.object(Path, "unlink"),
        ):
            answer, _, partial = parley.run_codex("p", Path.cwd(), None, None, 10)
        self.assertEqual(answer, "PARTIAL")
        self.assertTrue(partial)

    def test_nonzero_exit_without_output_raises(self):
        with (
            mock.patch.object(parley, "codex_bin", return_value="codex"),
            mock.patch.object(parley.subprocess, "run", return_value=self._proc(rc=3)),
            mock.patch.object(Path, "is_file", return_value=False),
            mock.patch.object(Path, "unlink"),
            self.assertRaises(SystemExit),
        ):
            parley.run_codex("p", Path.cwd(), None, None, 10)


class SessionExtraction(unittest.TestCase):
    def test_reads_thread_id_from_the_real_event_shape(self):
        stream = (
            '{"type":"thread.started","thread_id":"01a0-abc"}\n'
            '{"type":"turn.completed","usage":{"input_tokens":1}}\n'
        )
        self.assertEqual(parley.find_session_id(stream), "01a0-abc")

    def test_unknown_envelope_degrades_to_none(self):
        self.assertIsNone(parley.find_session_id('{"type":"turn.started"}\n'))

    def test_ignores_non_json_noise(self):
        self.assertEqual(parley.find_session_id('not json\n{"thread_id":"x"}\n'), "x")


class ViewerIsolation(unittest.TestCase):
    """The viewer must not serve or list pre-hash transcripts, or escape LOGDIR."""

    def test_legacy_names_are_not_listed(self):
        self.assertIsNone(serve.CURRENT_LOG.search("C_Dev_Proj_default"))

    def test_current_names_are_listed(self):
        self.assertIsNotNone(serve.CURRENT_LOG.search("proj-main-" + "a" * 32))

    def test_traversal_is_refused(self):
        self.assertEqual(
            serve.messages("../../etc/passwd-" + "a" * 32, 0),
            {"total": 0, "messages": []},
        )

    def test_malformed_id_is_refused(self):
        self.assertEqual(serve.messages("whatever", 0), {"total": 0, "messages": []})


class FailureRecording(unittest.TestCase):
    """Drive main()'s real exception handler -- not a hand-built imitation of it.

    The earlier version of these tests rebuilt a two-record log by hand and
    asserted that a logging error escaped, which is exactly the masking the
    guarantee forbids. These exercise the production path instead.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.logdir = d / "log"
        self.input = d / "q.md"
        self.input.write_text("question", encoding="utf-8")
        self.patches = [
            mock.patch.object(parley, "LOGDIR", self.logdir),
            mock.patch.object(parley, "REGISTRY", d / "threads.json"),
            mock.patch.object(parley, "codex_bin", return_value="codex"),
            mock.patch.object(
                sys,
                "argv",
                ["parley.py", "--input", str(self.input), "--project", str(d)],
            ),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(self.tmp.cleanup)
        for p in self.patches:
            self.addCleanup(p.stop)

    def records(self):
        files = list(self.logdir.glob("*.jsonl"))
        if not files:
            return []
        return [
            json.loads(x) for x in files[0].read_text(encoding="utf-8").splitlines()
        ]

    def test_keyboard_interrupt_is_recorded_and_re_raised(self):
        with (
            mock.patch.object(parley, "run_codex", side_effect=KeyboardInterrupt()),
            self.assertRaises(KeyboardInterrupt),
        ):
            parley.main()
        recs = self.records()
        self.assertEqual(len(recs), 2)
        self.assertEqual(recs[0]["role"], "claude")
        self.assertTrue(recs[1].get("error"))
        self.assertIn("[TURN FAILED]", recs[1]["text"])

    def test_ordinary_exception_is_recorded_and_re_raised(self):
        with (
            mock.patch.object(parley, "run_codex", side_effect=OSError("disk gone")),
            self.assertRaises(OSError),
        ):
            parley.main()
        recs = self.records()
        self.assertTrue(recs[-1].get("error"))
        self.assertIn("disk gone", recs[-1]["text"])

    def test_logging_the_failure_does_not_mask_the_original(self):
        """The ORIGINAL error must escape, not the error from recording it."""
        real = parley.append_log
        calls = {"n": 0}

        def flaky(tid, record):
            calls["n"] += 1
            if calls["n"] == 1:
                return real(tid, record)  # outbound question logs fine
            raise OSError("log device failed")  # the failure record does not

        with (
            mock.patch.object(parley, "run_codex", side_effect=ValueError("original")),
            mock.patch.object(parley, "append_log", side_effect=flaky),
            self.assertRaises(ValueError) as ctx,
        ):
            parley.main()
        self.assertIn("original", str(ctx.exception))
        self.assertNotIsInstance(ctx.exception, OSError)

    def test_no_failure_record_after_a_real_answer(self):
        """A later registry failure must not append a misleading [TURN FAILED]."""
        with (
            mock.patch.object(
                parley, "run_codex", return_value=("THE ANSWER", "sess-1", False)
            ),
            mock.patch.object(parley, "write_registry", side_effect=OSError("no disk")),
            self.assertRaises(OSError),
        ):
            parley.main()
        recs = self.records()
        self.assertEqual(len(recs), 2)
        self.assertEqual(recs[1]["role"], "gpt")
        self.assertNotIn("error", recs[1])
        self.assertEqual(recs[1]["text"], "THE ANSWER")


class ConsoleEncoding(unittest.TestCase):
    """A legacy console code page must not withhold an already-completed answer.

    The record is durable either way; this is about delivery to the caller.
    """

    def test_reconfigures_streams_to_utf8_replace(self):
        calls = []

        class Fake:
            def reconfigure(self, **kw):
                calls.append(kw)

        with (
            mock.patch.object(parley.sys, "stdout", Fake()),
            mock.patch.object(parley.sys, "stderr", Fake()),
        ):
            parley.use_utf8_console()
        self.assertEqual(len(calls), 2)
        for kw in calls:
            self.assertEqual(kw["encoding"], "utf-8")
            self.assertEqual(kw["errors"], "replace")

    def test_streams_without_reconfigure_are_tolerated(self):
        class NoReconfigure:
            pass

        with (
            mock.patch.object(parley.sys, "stdout", NoReconfigure()),
            mock.patch.object(parley.sys, "stderr", NoReconfigure()),
        ):
            parley.use_utf8_console()  # must not raise

    def test_reconfigure_failure_is_swallowed(self):
        class Hostile:
            def reconfigure(self, **kw):
                raise OSError("detached")

        with (
            mock.patch.object(parley.sys, "stdout", Hostile()),
            mock.patch.object(parley.sys, "stderr", Hostile()),
        ):
            parley.use_utf8_console()  # must not raise


if __name__ == "__main__":
    unittest.main(verbosity=2)
