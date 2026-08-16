"""Runner-contract tests (PARLEY-V2-002).

These pin the generic boundary: capabilities are declared in code and cannot be
claimed, an inadmissible policy is refused before any process launches, and no
Codex-specific detail leaks into the generic contract.

    python -m unittest discover -p "test_*.py"
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import parley
from parley_core import codex_runner, runners
from parley_core.models import (
    AccessPolicy,
    RunMetadata,
    RunnerCapabilities,
    RunnerSpec,
    RunResult,
    RunStatus,
)


class CapabilityAdmission(unittest.TestCase):
    """Capabilities are declared in code. A claim must never grant one."""

    def test_codex_declares_read_only(self):
        self.assertTrue(runners.declared_capabilities("codex").read_only)

    def test_codex_does_not_yet_declare_workspace_write(self):
        # PARLEY-V2-006 must demonstrate enforcement before this flips.
        self.assertFalse(runners.declared_capabilities("codex").workspace_write)

    def test_unknown_driver_is_refused(self):
        with self.assertRaises(runners.RunnerError):
            runners.declared_capabilities("hypothetical")

    def test_resolve_attaches_declared_capabilities_not_supplied_ones(self):
        spec = runners.resolve_spec("codex", "openai", "o3")
        self.assertEqual(spec.capabilities, runners.declared_capabilities("codex"))

    def test_unsupported_provider_is_refused(self):
        with self.assertRaises(runners.RunnerError):
            runners.resolve_spec("codex", "anthropic")

    def test_local_providers_are_not_yet_supported(self):
        """V2-002 trusts only the provider whose argv it actually renders.

        build_argv() never emits --oss/--local-provider, so accepting ollama or
        lmstudio here would produce a spec claiming a local provider while
        launching the OpenAI path. PARLEY-V2-003 enables them.
        """
        for prov in ("ollama", "lmstudio"):
            with self.subTest(provider=prov), self.assertRaises(runners.RunnerError):
                runners.resolve_spec("codex", prov)

    def test_read_only_is_admitted(self):
        runners.admit(runners.resolve_spec(), AccessPolicy.READ_ONLY)  # must not raise

    def test_workspace_write_is_refused_today(self):
        with self.assertRaises(runners.CapabilityError):
            runners.admit(runners.resolve_spec(), AccessPolicy.WORKSPACE_WRITE)

    def test_a_forged_capability_object_is_refused_by_admit_under_both_policies(self):
        """A RunnerSpec is a value object; admission must not trust its claims.

        resolve_spec() refusing to PRODUCE a forged spec was never sufficient --
        anyone can construct one. admit() re-derives from the code declaration,
        so a forged spec is refused outright, under either policy.
        """
        forged = FORGED_WRITE_SPEC
        self.assertTrue(forged.capabilities.workspace_write)  # it does claim it
        for policy in (AccessPolicy.WORKSPACE_WRITE, AccessPolicy.READ_ONLY):
            with (
                self.subTest(policy=policy),
                self.assertRaises(runners.CapabilityError),
            ):
                runners.admit(forged, policy)

    def test_resolve_never_produces_a_write_capable_spec(self):
        self.assertFalse(runners.resolve_spec().capabilities.workspace_write)


FORGED_WRITE_SPEC = RunnerSpec(
    "codex",
    "openai",
    None,
    RunnerCapabilities(persistent_sessions=True, read_only=True, workspace_write=True),
)


class NoLaunchWhenInadmissible(unittest.TestCase):
    def test_a_forged_spec_cannot_spawn_a_writable_process(self):
        runner = codex_runner.CodexRunner(FORGED_WRITE_SPEC, binary="codex")
        with (
            mock.patch.object(codex_runner.subprocess, "run") as spawn,
            self.assertRaises(runners.CapabilityError),
        ):
            runner.run("p", Path.cwd(), None, AccessPolicy.WORKSPACE_WRITE)
        spawn.assert_not_called()

    def test_runner_refuses_before_spawning_a_process(self):
        spec = runners.resolve_spec()
        runner = codex_runner.CodexRunner(spec, binary="codex")
        with (
            mock.patch.object(codex_runner.subprocess, "run") as spawn,
            self.assertRaises(runners.CapabilityError),
        ):
            runner.run("p", Path.cwd(), None, AccessPolicy.WORKSPACE_WRITE)
        spawn.assert_not_called()


class GenericContractStaysGeneric(unittest.TestCase):
    """Codex-specific vocabulary must not leak into models.py or runners.py."""

    def test_no_codex_mentions_in_the_generic_modules(self):
        for mod in ("parley_core/models.py",):
            text = Path(mod).read_text(encoding="utf-8").lower()
            self.assertNotIn("codex", text, f"{mod} names a specific driver")

    def test_runners_names_codex_only_as_a_declared_driver(self):
        text = Path("parley_core/runners.py").read_text(encoding="utf-8")
        # allowed: the declaration tables. not allowed: argv, flags, subprocess.
        for forbidden in ("exec", "--json", "subprocess", "-s", "resume"):
            self.assertNotIn(f'"{forbidden}"', text)


class ResultShape(unittest.TestCase):
    def test_partial_and_usable_derive_from_status(self):
        def r(status):
            return RunResult("a", None, status, RunMetadata())

        self.assertTrue(r(RunStatus.PARTIAL).partial)
        self.assertFalse(r(RunStatus.COMPLETED).partial)
        self.assertTrue(r(RunStatus.COMPLETED).usable)
        self.assertTrue(r(RunStatus.PARTIAL).usable)
        self.assertFalse(r(RunStatus.FAILED).usable)
        self.assertFalse(r(RunStatus.TIMED_OUT).usable)

    def test_metadata_is_json_safe(self):
        import json

        json.dumps(RunMetadata(exit_code=1, duration_ms=5).to_json())
        json.dumps(runners.resolve_spec().to_json())


class PolicyReachesArgv(unittest.TestCase):
    def test_read_only_policy_renders_the_read_only_flag(self):
        argv = codex_runner.build_argv(
            "codex", Path("/p"), None, None, Path("o.md"), AccessPolicy.READ_ONLY
        )
        self.assertEqual(argv[argv.index("-s") + 1], "read-only")

    def test_workspace_write_policy_renders_its_flag(self):
        # Constructible, but unreachable through admission until V2-006.
        argv = codex_runner.build_argv(
            "codex", Path("/p"), None, None, Path("o.md"), AccessPolicy.WORKSPACE_WRITE
        )
        self.assertEqual(argv[argv.index("-s") + 1], "workspace-write")


class ExecutionOutcomes(unittest.TestCase):
    """Every declared RunStatus must be reachable through the real runner path.

    Constructing a RunResult by hand cannot detect a branch that still raises,
    which is exactly how an unreachable status survived review once already.
    """

    def runner(self, timeout=900):
        return codex_runner.CodexRunner(
            runners.resolve_spec(), binary="codex", timeout=timeout
        )

    def test_timeout_returns_timed_out(self):
        with mock.patch.object(
            codex_runner.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd="codex", timeout=1),
        ):
            r = self.runner(timeout=1).run(
                "p", Path.cwd(), "sess-in", AccessPolicy.READ_ONLY
            )
        self.assertIs(r.status, RunStatus.TIMED_OUT)
        self.assertEqual(r.answer, "")
        self.assertEqual(r.session, "sess-in")
        self.assertIsNone(r.metadata.exit_code)
        self.assertIn("timed out after 1s", r.metadata.diagnostic)
        self.assertFalse(r.usable)

    def test_nonzero_without_answer_returns_failed(self):
        proc = subprocess.CompletedProcess([], 2, stdout="", stderr="Error: boom")
        with (
            mock.patch.object(codex_runner.subprocess, "run", return_value=proc),
            mock.patch.object(Path, "is_file", return_value=False),
            mock.patch.object(Path, "unlink"),
        ):
            r = self.runner().run("p", Path.cwd(), "sess-in", AccessPolicy.READ_ONLY)
        self.assertIs(r.status, RunStatus.FAILED)
        self.assertEqual(r.answer, "")
        self.assertEqual(r.session, "sess-in")
        self.assertEqual(r.metadata.exit_code, 2)
        self.assertIn("codex exited 2", r.metadata.diagnostic)
        self.assertIn("boom", r.metadata.diagnostic)
        self.assertFalse(r.usable)

    def test_launch_failure_still_raises_runner_error(self):
        with (
            mock.patch.object(
                codex_runner.subprocess, "run", side_effect=FileNotFoundError()
            ),
            self.assertRaises(runners.RunnerError),
        ):
            self.runner().run("p", Path.cwd(), None, AccessPolicy.READ_ONLY)


class LegacyWrapperTranslation(unittest.TestCase):
    """run_codex() must turn unusable results back into the legacy SystemExit."""

    def test_timeout_becomes_systemexit_with_the_legacy_text(self):
        with (
            mock.patch.object(parley, "codex_bin", return_value="codex"),
            mock.patch.object(
                codex_runner.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(cmd="codex", timeout=7),
            ),
            self.assertRaises(SystemExit) as ctx,
        ):
            parley.run_codex("p", Path.cwd(), None, None, 7)
        self.assertIn("codex timed out after 7s", str(ctx.exception))
        self.assertIn("--timeout", str(ctx.exception))

    def test_failure_becomes_systemexit_with_exit_code_and_hint(self):
        proc = subprocess.CompletedProcess(
            [], 1, stdout="", stderr="Error: not logged in"
        )
        with (
            mock.patch.object(parley, "codex_bin", return_value="codex"),
            mock.patch.object(codex_runner.subprocess, "run", return_value=proc),
            mock.patch.object(Path, "is_file", return_value=False),
            mock.patch.object(Path, "unlink"),
            self.assertRaises(SystemExit) as ctx,
        ):
            parley.run_codex("p", Path.cwd(), None, None, 10)
        msg = str(ctx.exception)
        self.assertIn("codex exited 1", msg)
        self.assertIn("codex login", msg)  # the hint survives the translation


if __name__ == "__main__":
    unittest.main(verbosity=2)
