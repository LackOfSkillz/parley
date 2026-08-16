"""Characterization tests for Parley's consultation contract (PARLEY-V2-001).

These pin the CURRENT externally observable behaviour so the v2 restructuring has
an executable compatibility boundary to work against. They describe what Parley
does today, not what it ought to do: if one fails during the refactor, that is a
compatibility break to be justified, not a test to be relaxed.

Deliberately separate from test_parley.py. That file pins guarantees found in
review; this one pins the shape of the interface.

    python -m unittest discover -p "test_*.py"
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import parley


class Harness(unittest.TestCase):
    """Runs the real main() with the Codex subprocess and filesystem redirected."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / "proj"
        self.project.mkdir()
        self.logdir = self.root / "log"
        self.registry = self.root / "threads.json"
        self.input = self.root / "q.md"
        self.input.write_text("THE QUESTION", encoding="utf-8")

        for p in (
            mock.patch.object(parley, "LOGDIR", self.logdir),
            mock.patch.object(parley, "REGISTRY", self.registry),
            mock.patch.object(parley, "codex_bin", return_value="codex"),
        ):
            p.start()
            self.addCleanup(p.stop)

    def run_main(self, *argv, answer="THE ANSWER", session="sess-1", partial=False):
        """Invoke main() with the given CLI args; returns (stdout, captured_prompt)."""
        seen = {}

        def fake(prompt, project, resume_id, model, timeout):
            seen["prompt"] = prompt
            seen["project"] = project
            seen["resume_id"] = resume_id
            seen["model"] = model
            seen["timeout"] = timeout
            return answer, session, partial

        buf = io.StringIO()
        with (
            mock.patch.object(sys, "argv", ["parley.py", *argv]),
            mock.patch.object(parley, "run_codex", side_effect=fake),
            redirect_stdout(buf),
        ):
            parley.main()
        return buf.getvalue(), seen

    def records(self):
        files = list(self.logdir.glob("*.jsonl"))
        if not files:
            return []
        return [
            json.loads(x) for x in files[0].read_text(encoding="utf-8").splitlines()
        ]


class CliSurface(Harness):
    """The legacy `python parley.py --mode ...` form must keep working."""

    def test_every_mode_is_accepted(self):
        for mode in ("ask", "review", "design", "challenge"):
            with self.subTest(mode=mode):
                self.run_main(
                    "--mode",
                    mode,
                    "--input",
                    str(self.input),
                    "--project",
                    str(self.project),
                    "--thread",
                    mode,
                )

    def test_mode_defaults_to_ask(self):
        _, seen = self.run_main(
            "--input", str(self.input), "--project", str(self.project)
        )
        self.assertIn("mode=ask", seen["prompt"])

    def test_thread_defaults_to_default(self):
        _, seen = self.run_main(
            "--input", str(self.input), "--project", str(self.project)
        )
        self.assertIn("thread=default", seen["prompt"])

    def test_context_is_repeatable(self):
        a, b = self.root / "a.md", self.root / "b.md"
        a.write_text("A", encoding="utf-8")
        b.write_text("B", encoding="utf-8")
        _, seen = self.run_main(
            "--input",
            str(self.input),
            "--project",
            str(self.project),
            "--context",
            str(a),
            "--context",
            str(b),
        )
        self.assertIn("FILES TO READ", seen["prompt"])
        self.assertIn("a.md", seen["prompt"])
        self.assertIn("b.md", seen["prompt"])

    def test_model_and_timeout_reach_the_runner(self):
        _, seen = self.run_main(
            "--input",
            str(self.input),
            "--project",
            str(self.project),
            "--model",
            "o3",
            "--timeout",
            "42",
        )
        self.assertEqual(seen["model"], "o3")
        self.assertEqual(seen["timeout"], 42)

    def test_input_is_required_without_list(self):
        with (
            mock.patch.object(
                sys, "argv", ["parley.py", "--project", str(self.project)]
            ),
            self.assertRaises(SystemExit),
        ):
            parley.main()

    def test_missing_input_file_exits(self):
        with (
            mock.patch.object(
                sys,
                "argv",
                [
                    "parley.py",
                    "--input",
                    str(self.root / "nope.md"),
                    "--project",
                    str(self.project),
                ],
            ),
            self.assertRaises(SystemExit),
        ):
            parley.main()

    def test_nonexistent_project_exits(self):
        with (
            mock.patch.object(
                sys,
                "argv",
                [
                    "parley.py",
                    "--input",
                    str(self.input),
                    "--project",
                    str(self.root / "nope"),
                ],
            ),
            self.assertRaises(SystemExit),
        ):
            parley.main()

    def test_list_on_empty_registry(self):
        buf = io.StringIO()
        with (
            mock.patch.object(sys, "argv", ["parley.py", "--list"]),
            redirect_stdout(buf),
        ):
            parley.main()
        self.assertIn("no threads yet", buf.getvalue())

    def test_list_reports_project_thread_turns_session_and_id(self):
        self.run_main(
            "--input", str(self.input), "--project", str(self.project), "--thread", "t1"
        )
        buf = io.StringIO()
        with (
            mock.patch.object(sys, "argv", ["parley.py", "--list"]),
            redirect_stdout(buf),
        ):
            parley.main()
        out = buf.getvalue()
        self.assertIn(str(self.project), out)  # the project itself, not just the thread
        self.assertIn("[t1]", out)
        self.assertIn("turns=1", out)
        self.assertIn("session=sess-1", out)
        self.assertIn("id=", out)


class PromptConstruction(Harness):
    """Prompt shape is the contract with the model; order is part of it."""

    def test_fresh_turn_opens_with_the_orientation_block(self):
        _, seen = self.run_main(
            "--input", str(self.input), "--project", str(self.project)
        )
        p = seen["prompt"]
        self.assertTrue(p.startswith("[NEW CONVERSATION]"))
        self.assertIn("Project: proj", p)
        self.assertIn(f"Working root: {self.project}", p)
        self.assertIn("Thread: default", p)

    def test_order_is_orientation_then_role_then_metadata_then_body(self):
        _, seen = self.run_main(
            "--mode",
            "review",
            "--input",
            str(self.input),
            "--project",
            str(self.project),
        )
        p = seen["prompt"]
        i_orient = p.index("[NEW CONVERSATION]")
        i_role = p.index("You are the reviewing partner")
        i_meta = p.index("[project=proj")
        i_body = p.index("THE QUESTION")
        self.assertLess(i_orient, i_role)
        self.assertLess(i_role, i_meta)
        self.assertLess(i_meta, i_body)

    def test_metadata_header_carries_project_thread_mode_and_turn(self):
        _, seen = self.run_main(
            "--mode",
            "design",
            "--input",
            str(self.input),
            "--project",
            str(self.project),
            "--thread",
            "abc",
        )
        self.assertIn("[project=proj thread=abc mode=design turn=1]", seen["prompt"])

    def test_resumed_turn_drops_orientation_but_keeps_the_role(self):
        self.run_main("--input", str(self.input), "--project", str(self.project))
        _, seen = self.run_main(
            "--input", str(self.input), "--project", str(self.project)
        )
        p = seen["prompt"]
        self.assertNotIn("[NEW CONVERSATION]", p)
        self.assertIn(
            "You are a senior engineering consultant", p
        )  # re-sent every turn
        self.assertIn("turn=2", p)
        self.assertEqual(seen["resume_id"], "sess-1")

    def test_new_thread_flag_restores_a_fresh_prompt(self):
        self.run_main("--input", str(self.input), "--project", str(self.project))
        _, seen = self.run_main(
            "--input", str(self.input), "--project", str(self.project), "--new-thread"
        )
        self.assertIn("[NEW CONVERSATION]", seen["prompt"])
        self.assertIsNone(seen["resume_id"])
        self.assertIn("turn=1", seen["prompt"])


class RecordShapes(Harness):
    """Transcript and registry shapes are consumed by the viewer and by --list."""

    def test_outbound_record_shape(self):
        self.run_main(
            "--mode",
            "review",
            "--input",
            str(self.input),
            "--project",
            str(self.project),
        )
        rec = self.records()[0]
        self.assertEqual(
            set(rec),
            {"ts", "role", "mode", "turn", "project", "thread", "attached", "text"},
        )
        self.assertEqual(rec["role"], "claude")
        self.assertEqual(rec["mode"], "review")
        self.assertEqual(rec["turn"], 1)
        self.assertEqual(rec["attached"], [])

    def test_answer_record_shape(self):
        self.run_main("--input", str(self.input), "--project", str(self.project))
        rec = self.records()[1]
        self.assertEqual(
            set(rec),
            {
                "ts",
                "role",
                "turn",
                "project",
                "thread",
                "session_id",
                "partial",
                "text",
            },
        )
        self.assertEqual(rec["role"], "gpt")
        self.assertEqual(rec["session_id"], "sess-1")
        self.assertFalse(rec["partial"])
        self.assertEqual(rec["text"], "THE ANSWER")

    def test_stdout_returns_the_answer_to_the_caller(self):
        out, _ = self.run_main(
            "--input", str(self.input), "--project", str(self.project)
        )
        self.assertEqual(out.strip(), "THE ANSWER")

    def test_partial_answers_are_still_printed(self):
        out, _ = self.run_main(
            "--input", str(self.input), "--project", str(self.project), partial=True
        )
        self.assertEqual(out.strip(), "THE ANSWER")

    def test_partial_answers_are_flagged_in_the_record(self):
        self.run_main(
            "--input", str(self.input), "--project", str(self.project), partial=True
        )
        self.assertTrue(self.records()[1]["partial"])

    def test_failure_record_shape(self):
        with (
            mock.patch.object(
                sys,
                "argv",
                [
                    "parley.py",
                    "--input",
                    str(self.input),
                    "--project",
                    str(self.project),
                ],
            ),
            mock.patch.object(parley, "run_codex", side_effect=OSError("boom")),
            self.assertRaises(OSError),
        ):
            parley.main()
        rec = self.records()[1]
        self.assertEqual(
            set(rec), {"ts", "role", "turn", "project", "thread", "error", "text"}
        )
        self.assertEqual(rec["role"], "gpt")
        self.assertTrue(rec["error"])
        self.assertEqual(rec["turn"], 1)
        self.assertEqual(rec["project"], str(self.project))
        self.assertEqual(rec["thread"], "default")
        self.assertTrue(rec["text"].startswith("[TURN FAILED]"))
        self.assertIn("boom", rec["text"])
        # a failure must not masquerade as an answer
        self.assertNotIn("session_id", rec)
        self.assertNotIn("partial", rec)

    def test_attached_records_context_basenames_only(self):
        c = self.root / "ctx.md"
        c.write_text("C", encoding="utf-8")
        self.run_main(
            "--input",
            str(self.input),
            "--project",
            str(self.project),
            "--context",
            str(c),
        )
        self.assertEqual(self.records()[0]["attached"], ["ctx.md"])

    def test_registry_entry_shape_and_turn_accounting(self):
        self.run_main("--input", str(self.input), "--project", str(self.project))
        reg = json.loads(self.registry.read_text(encoding="utf-8"))
        ((tid, entry),) = reg.items()
        self.assertEqual(
            set(entry),
            {"session_id", "project", "thread", "turns", "created_at", "updated_at"},
        )
        self.assertEqual(entry["turns"], 1)
        first_created = entry["created_at"]

        self.run_main("--input", str(self.input), "--project", str(self.project))
        reg = json.loads(self.registry.read_text(encoding="utf-8"))
        self.assertEqual(reg[tid]["turns"], 2)
        self.assertEqual(
            reg[tid]["created_at"], first_created
        )  # preserved across turns

    def test_transcript_filename_matches_the_registry_key(self):
        self.run_main("--input", str(self.input), "--project", str(self.project))
        reg = json.loads(self.registry.read_text(encoding="utf-8"))
        ((tid, _),) = reg.items()
        self.assertTrue((self.logdir / f"{tid}.jsonl").is_file())


class SessionScoping(Harness):
    """One reviewer session per canonical (project, thread)."""

    def test_same_project_and_thread_reuse_one_session(self):
        self.run_main("--input", str(self.input), "--project", str(self.project))
        _, seen = self.run_main(
            "--input", str(self.input), "--project", str(self.project)
        )
        self.assertEqual(seen["resume_id"], "sess-1")
        self.assertEqual(len(json.loads(self.registry.read_text(encoding="utf-8"))), 1)

    def test_different_threads_do_not_share_a_session(self):
        self.run_main(
            "--input", str(self.input), "--project", str(self.project), "--thread", "a"
        )
        _, seen = self.run_main(
            "--input", str(self.input), "--project", str(self.project), "--thread", "b"
        )
        self.assertIsNone(seen["resume_id"])  # b starts fresh
        self.assertEqual(len(json.loads(self.registry.read_text(encoding="utf-8"))), 2)

    def test_equivalent_project_spellings_reuse_one_session(self):
        self.run_main("--input", str(self.input), "--project", str(self.project))
        _, seen = self.run_main(
            "--input", str(self.input), "--project", str(self.project) + "/./"
        )
        self.assertEqual(seen["resume_id"], "sess-1")
        self.assertEqual(len(json.loads(self.registry.read_text(encoding="utf-8"))), 1)


class CodexCommandSemantics(unittest.TestCase):
    """The exact argv Parley hands to Codex. Asserted whole, not by fragments.

    PARLEY-V2-002 restructures precisely this boundary, so a fragment check that
    tolerated reordering or extra arguments would not protect it.
    """

    OUT = "out.md"

    def cmd(self, resume=None, model=None, project="/p"):
        with mock.patch.object(parley, "codex_bin", return_value="codex"):
            return parley.build_cmd(Path(project), resume, model, Path(self.OUT))

    def test_fresh_argv_exactly(self):
        self.assertEqual(
            self.cmd(project="/p"),
            [
                "codex",
                "exec",
                "-s",
                "read-only",
                "-C",
                str(Path("/p")),
                "--json",
                "-o",
                self.OUT,
                "--skip-git-repo-check",
                "-",
            ],
        )

    def test_resumed_argv_exactly(self):
        self.assertEqual(
            self.cmd(resume="abc"),
            [
                "codex",
                "exec",
                "-s",
                "read-only",
                "resume",
                "abc",
                "--json",
                "-o",
                self.OUT,
                "--skip-git-repo-check",
                "-",
            ],
        )

    def test_fresh_argv_with_model_exactly(self):
        self.assertEqual(
            self.cmd(model="o3", project="/p"),
            [
                "codex",
                "exec",
                "-s",
                "read-only",
                "-C",
                str(Path("/p")),
                "-m",
                "o3",
                "--json",
                "-o",
                self.OUT,
                "--skip-git-repo-check",
                "-",
            ],
        )

    def test_resumed_argv_with_model_exactly(self):
        self.assertEqual(
            self.cmd(resume="abc", model="o3"),
            [
                "codex",
                "exec",
                "-s",
                "read-only",
                "-m",
                "o3",
                "resume",
                "abc",
                "--json",
                "-o",
                self.OUT,
                "--skip-git-repo-check",
                "-",
            ],
        )

    def test_sandbox_flag_precedes_the_resume_subcommand(self):
        c = self.cmd(resume="abc")
        self.assertLess(c.index("-s"), c.index("resume"))

    def test_prompt_is_piped_to_stdin_and_cwd_is_the_project(self):
        with tempfile.TemporaryDirectory() as d:
            captured = {}

            def fake_run(cmd, **kw):
                captured["cmd"] = cmd
                captured.update(kw)
                return subprocess.CompletedProcess(
                    cmd, 0, stdout='{"thread_id":"t"}', stderr=""
                )

            with (
                mock.patch.object(parley, "codex_bin", return_value="codex"),
                mock.patch.object(parley.subprocess, "run", side_effect=fake_run),
                mock.patch.object(Path, "is_file", return_value=True),
                mock.patch.object(Path, "read_text", return_value="A"),
                mock.patch.object(Path, "unlink"),
            ):
                parley.run_codex("THE PROMPT", Path(d), None, None, 10)

            # argv only ends in "-"; the prompt itself travels through stdin
            self.assertEqual(captured["cmd"][-1], "-")
            self.assertEqual(captured["input"], "THE PROMPT")
            self.assertEqual(captured["cwd"], str(Path(d)))
            self.assertIs(captured["check"], False)
            self.assertEqual(captured["timeout"], 10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
