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
    WaitRefused,
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


class EvidenceGradeIsHonest(unittest.TestCase):
    """source says WHERE the signal came from; evidence says HOW STRONG it is.

    They are independent on purpose: source="json" alone would let an inferred
    status read as a captured one.
    """

    def limited(self):
        return classify(
            event(
                {"status": 429, "error": {"type": "rate_limit_error", "message": "x"}}
            )
        )

    def test_inferred_429_is_graded_structural_not_observed(self):
        info = self.limited()
        self.assertEqual(info.source, "json")
        self.assertEqual(info.evidence, "structural")

    def test_evidence_grade_survives_serialisation(self):
        self.assertEqual(self.limited().to_json()["evidence"], "structural")


class ResetAtIsRecordedNotUsed(unittest.TestCase):
    """A provider reset value is preserved verbatim and never drives timing.

    No captured evidence establishes its format, so deriving a wait from it
    would be guessing dressed as provider instruction.
    """

    RESET = "2026-08-17T04:00:00Z"

    def limited_with_reset(self):
        return classify(
            event(
                {
                    "status": 429,
                    "reset_at": self.RESET,
                    "error": {"type": "rate_limit_error", "message": "slow"},
                }
            )
        )

    def test_reset_survives_classification_unchanged(self):
        self.assertEqual(self.limited_with_reset().reset_at, self.RESET)

    def test_reset_survives_serialisation_unchanged(self):
        self.assertEqual(self.limited_with_reset().to_json()["reset_at"], self.RESET)

    def test_reset_does_not_make_the_wait_non_blind(self):
        info = self.limited_with_reset()
        self.assertIsNone(info.retry_after_seconds)
        self.assertTrue(is_blind(info))  # a reset is not a retry-after

    def test_reset_does_not_alter_the_blind_backoff(self):
        with_reset = wait_seconds(self.limited_with_reset(), attempt=2)
        without = wait_seconds(
            classify(
                event(
                    {
                        "status": 429,
                        "error": {"type": "rate_limit_error", "message": "slow"},
                    }
                )
            ),
            attempt=2,
        )
        self.assertEqual(with_reset, without)
        self.assertEqual(with_reset, 900)


class ProseDetectionIsNotImplemented(unittest.TestCase):
    """No stderr limit signature has captured evidence, so none is matched."""

    def test_rate_limit_prose_on_stderr_is_not_a_limit(self):
        for text in (
            "Error: usage limit reached, try again in 2 hours",
            "429 Too Many Requests",
            "rate limit exceeded",
        ):
            with self.subTest(text=text):
                self.assertIsNone(classify("", text, "0.147.0"))


class WaitBounds(unittest.TestCase):
    """Calculation only. Nothing here sleeps.

    The bounds are the ones §11 fixes: 300s blind base, multiplier 3, 7200s
    per-wait cap, and a 5-second safety margin on provider time.
    """

    def info(self, retry=None):
        return LimitInfo(
            kind="rate",
            reason="r",
            source="json",
            evidence="structural",
            retry_after_seconds=retry,
            reset_at=None,
            detector_version=DETECTOR_VERSION,
        )

    def test_provider_time_gains_the_safety_margin(self):
        self.assertEqual(wait_seconds(self.info(300), attempt=1), 305)

    def test_provider_time_above_the_cap_is_refused_not_shortened(self):
        """Retrying early is not a smaller wait, it is a wasted call."""
        with self.assertRaises(WaitRefused):
            wait_seconds(self.info(99999), attempt=1)

    def test_provider_time_above_remaining_budget_is_refused(self):
        with self.assertRaises(WaitRefused):
            wait_seconds(self.info(600), attempt=1, remaining_budget=100)

    def test_blind_backoff_follows_the_specified_progression(self):
        i = self.info()
        # 5, 15, 45, 120, 120 minutes
        self.assertEqual(wait_seconds(i, 1), 300)
        self.assertEqual(wait_seconds(i, 2), 900)
        self.assertEqual(wait_seconds(i, 3), 2700)
        self.assertEqual(wait_seconds(i, 4), 7200)
        self.assertEqual(wait_seconds(i, 9), 7200)

    def test_blind_backoff_over_budget_is_refused(self):
        with self.assertRaises(WaitRefused):
            wait_seconds(self.info(), attempt=1, remaining_budget=60)

    def test_limit_info_is_json_safe(self):
        json.dumps(self.info(60).to_json())


if __name__ == "__main__":
    unittest.main(verbosity=2)
