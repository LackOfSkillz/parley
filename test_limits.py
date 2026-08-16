"""Limit-classification tests (PARLEY-V2-002L).

The captured fixtures in tests/fixtures/codex-0.147.0/ are real Codex output.
The false-positive cases matter most: this classifier's job is mostly to say NO,
because a wrong yes means hours of unattended sleeping on an error that will
never resolve itself.

    python -m unittest discover -p "test_*.py"
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from parley_core.limits import (
    DETECTOR_VERSION,
    LimitInfo,
    classify,
    is_blind,
    wait_seconds,
)

FIX = Path(__file__).resolve().parent / "tests" / "fixtures" / "codex-0.147.0"


def event(payload: dict) -> str:
    """A turn.failed line in the exact shape the captured fixtures show."""
    return json.dumps(
        {"type": "turn.failed", "error": {"message": json.dumps(payload)}}
    )


class CapturedFixturesAreNotLimits(unittest.TestCase):
    """Every real failure we could actually capture must classify as NOT a limit."""

    def test_invalid_model_is_not_a_limit(self):
        out = (FIX / "invalid_model.stdout.jsonl").read_text(encoding="utf-8")
        self.assertIsNone(classify(out, "", "0.147.0"))

    def test_expired_session_is_not_a_limit(self):
        err = (FIX / "expired_session.stderr.txt").read_text(encoding="utf-8")
        self.assertIsNone(classify("", err, "0.147.0"))

    def test_a_successful_turn_is_not_a_limit(self):
        out = (FIX / "success.stdout.jsonl").read_text(encoding="utf-8")
        self.assertIsNone(classify(out, "", "0.147.0"))

    def test_the_invalid_model_fixture_really_is_a_400(self):
        """Guards the fixture: it must contain the envelope we claim to parse."""
        out = (FIX / "invalid_model.stdout.jsonl").read_text(encoding="utf-8")
        self.assertIn('"status\\":400', out)
        self.assertIn("turn.failed", out)


class NonLimitFailuresAreRefused(unittest.TestCase):
    """None of these become valid by sleeping, so none may be waitable."""

    CASES = (
        (
            "auth",
            {"status": 401, "error": {"type": "invalid_api_key", "message": "no"}},
        ),
        (
            "forbidden",
            {"status": 403, "error": {"type": "permission_error", "message": "no"}},
        ),
        (
            "bad request",
            {
                "status": 400,
                "error": {"type": "invalid_request_error", "message": "no"},
            },
        ),
        (
            "server",
            {"status": 500, "error": {"type": "server_error", "message": "boom"}},
        ),
        (
            "context",
            {
                "status": 400,
                "error": {"type": "context_length_exceeded", "message": "too long"},
            },
        ),
    )

    def test_none_classify_as_limited(self):
        for label, payload in self.CASES:
            with self.subTest(case=label):
                self.assertIsNone(classify(event(payload), "", "0.147.0"))

    def test_arbitrary_nonzero_prose_is_not_a_limit(self):
        self.assertIsNone(classify("", "Error: something went wrong", "0.147.0"))

    def test_empty_streams_are_not_a_limit(self):
        self.assertIsNone(classify("", "", "0.147.0"))

    def test_network_failure_is_not_a_limit(self):
        self.assertIsNone(classify("", "error sending request: dns error", "0.147.0"))


class RateLimitsAreIdentified(unittest.TestCase):
    """The 429 branch. Structurally supported; not empirically captured yet."""

    def test_http_429_is_limited(self):
        info = classify(
            event(
                {
                    "status": 429,
                    "error": {"type": "rate_limit_error", "message": "slow down"},
                }
            ),
            "",
            "0.147.0",
        )
        self.assertIsNotNone(info)
        self.assertEqual(info.kind, "rate")
        self.assertEqual(info.source, "json")
        self.assertEqual(info.detector_version, DETECTOR_VERSION)

    def test_quota_type_is_a_plan_limit(self):
        info = classify(
            event(
                {
                    "status": 429,
                    "error": {"type": "insufficient_quota", "message": "out"},
                }
            ),
            "",
            "0.147.0",
        )
        self.assertEqual(info.kind, "plan")

    def test_retry_after_is_parsed_from_the_message(self):
        info = classify(
            event(
                {
                    "status": 429,
                    "error": {
                        "type": "rate_limit_error",
                        "message": "Please try again in 20 minutes",
                    },
                }
            ),
            "",
            "0.147.0",
        )
        self.assertEqual(info.retry_after_seconds, 1200)
        self.assertFalse(is_blind(info))

    def test_absent_retry_after_is_marked_blind(self):
        info = classify(
            event(
                {
                    "status": 429,
                    "error": {"type": "rate_limit_error", "message": "slow"},
                }
            ),
            "",
            "0.147.0",
        )
        self.assertIsNone(info.retry_after_seconds)
        self.assertTrue(is_blind(info))


class StderrHeuristicIsVersionPinned(unittest.TestCase):
    """Prose is not a contract. It must not be inherited by unknown builds."""

    TEXT = "Error: usage limit reached, try again in 2 hours"

    def test_verified_version_may_use_the_heuristic(self):
        info = classify("", self.TEXT, "0.147.0")
        self.assertIsNotNone(info)
        self.assertEqual(info.source, "stderr_heuristic")
        self.assertEqual(info.retry_after_seconds, 7200)

    def test_unknown_version_does_not_inherit_the_heuristic(self):
        self.assertIsNone(classify("", self.TEXT, "9.9.9"))

    def test_absent_version_does_not_inherit_the_heuristic(self):
        self.assertIsNone(classify("", self.TEXT, None))

    def test_machine_readable_path_works_for_unknown_versions(self):
        # The envelope is structured, so it does NOT need version pinning.
        info = classify(
            event(
                {"status": 429, "error": {"type": "rate_limit_error", "message": "x"}}
            ),
            "",
            "9.9.9",
        )
        self.assertIsNotNone(info)
        self.assertEqual(info.source, "json")


class WaitBounds(unittest.TestCase):
    """Calculation only. Nothing here sleeps."""

    def info(self, retry=None):
        return LimitInfo("rate", "r", "json", retry, None, DETECTOR_VERSION)

    def test_provider_retry_after_is_preferred(self):
        self.assertEqual(wait_seconds(self.info(300), attempt=1), 300)

    def test_provider_value_is_still_clamped(self):
        self.assertEqual(
            wait_seconds(self.info(999999), attempt=1, max_single_wait=3600), 3600
        )

    def test_blind_backoff_grows_then_clamps(self):
        i = self.info()
        self.assertEqual(wait_seconds(i, 1, base_backoff=60), 60)
        self.assertEqual(wait_seconds(i, 2, base_backoff=60), 120)
        self.assertEqual(wait_seconds(i, 3, base_backoff=60), 240)
        self.assertEqual(
            wait_seconds(i, 99, base_backoff=60, max_single_wait=3600), 3600
        )

    def test_limit_info_is_json_safe(self):
        json.dumps(self.info(60).to_json())


if __name__ == "__main__":
    unittest.main(verbosity=2)
