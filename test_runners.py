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

    def test_local_providers_are_accepted(self):
        for p in ("ollama", "lmstudio"):
            with self.subTest(provider=p):
                self.assertEqual(runners.resolve_spec("codex", p).provider, p)

    def test_read_only_is_admitted(self):
        runners.admit(runners.resolve_spec(), AccessPolicy.READ_ONLY)  # must not raise

    def test_workspace_write_is_refused_today(self):
        with self.assertRaises(runners.CapabilityError):
            runners.admit(runners.resolve_spec(), AccessPolicy.WORKSPACE_WRITE)

    def test_a_forged_capability_object_is_not_trusted_by_resolve(self):
        """A spec can be constructed by hand, but resolve_spec never honours it."""
        forged = RunnerSpec(
            "codex",
            "openai",
            None,
            RunnerCapabilities(
                persistent_sessions=True, read_only=True, workspace_write=True
            ),
        )
        self.assertTrue(forged.capabilities.workspace_write)
        self.assertFalse(runners.resolve_spec().capabilities.workspace_write)


class NoLaunchWhenInadmissible(unittest.TestCase):
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

    def test_timeout_maps_to_a_runner_error_not_a_silent_result(self):
        spec = runners.resolve_spec()
        runner = codex_runner.CodexRunner(spec, binary="codex", timeout=1)
        with (
            mock.patch.object(
                codex_runner.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(cmd="codex", timeout=1),
            ),
            self.assertRaises(runners.RunnerError) as ctx,
        ):
            runner.run("p", Path.cwd(), None, AccessPolicy.READ_ONLY)
        self.assertIn("timed out", str(ctx.exception))


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
