import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import tokenlens


class TokenLensTests(unittest.TestCase):
    def test_safe_text_strips_control_characters(self):
        self.assertEqual(tokenlens.safe_text("abc\x1b[31mred"), "abc?[31mred")

    def test_fmt_observed_at_shortens_terminal_display(self):
        self.assertEqual(
            tokenlens.fmt_observed_at("2026-03-22T07:43:23.449328+09:00"),
            "2026-03-22 07:43",
        )

    def test_describe_reset_for_fixed_and_rolling_windows(self):
        observed_at = datetime(2026, 3, 22, 8, 0, tzinfo=timezone.utc)
        today = tokenlens.describe_reset("today", observed_at)
        rolling = tokenlens.describe_reset("5h", observed_at)

        self.assertEqual(today["reset_kind"], "fixed")
        self.assertIsNotNone(today["reset_at"])
        self.assertEqual(rolling["reset_kind"], "rolling")
        self.assertIsNone(rolling["reset_at"])

    def test_parse_threshold_accepts_percent(self):
        self.assertAlmostEqual(tokenlens.parse_threshold("15%"), 0.15)
        self.assertAlmostEqual(tokenlens.parse_threshold("0.2"), 0.2)
        self.assertAlmostEqual(tokenlens.parse_threshold("20"), 0.2)

    def test_normalize_provider_settings_swaps_invalid_threshold_order(self):
        defaults = tokenlens.DEFAULT_PROVIDER_SETTINGS["claude"]
        normalized = tokenlens.normalize_provider_settings(
            {
                "warn_threshold": 0.10,
                "critical_threshold": 0.20,
            },
            defaults,
        )
        self.assertEqual(normalized["warn_threshold"], 0.20)
        self.assertEqual(normalized["critical_threshold"], 0.10)

    def test_compute_status_requires_limit_and_usage(self):
        cfg = tokenlens.DEFAULT_PROVIDER_SETTINGS["claude"]
        self.assertEqual(tokenlens.compute_status("estimated", 50, None, cfg), "unknown")
        self.assertEqual(tokenlens.compute_status("unavailable", 50, 100, cfg), "unknown")
        self.assertEqual(tokenlens.compute_status("estimated", 95, 100, cfg), "critical")
        self.assertEqual(tokenlens.compute_status("estimated", 85, 100, cfg), "warn")
        self.assertEqual(tokenlens.compute_status("estimated", 10, 100, cfg), "ok")

    def test_normalize_provider_result_uses_config_unit(self):
        observed_at = datetime(2026, 3, 22, 8, 0, tzinfo=timezone.utc)
        cfg = dict(tokenlens.DEFAULT_PROVIDER_SETTINGS["gemini"])
        cfg["limit"] = 1000
        raw = {
            "provider": "Gemini CLI",
            "source": "local_logs",
            "total_tokens": 12345,
            "total_requests": 12,
        }
        item = tokenlens.normalize_provider_result(
            "gemini",
            raw,
            cfg,
            "today",
            observed_at,
            observed_at,
            observed_at,
        )
        self.assertEqual(item["unit"], "requests")
        self.assertEqual(item["used"], 12)
        self.assertEqual(item["remaining"], 988)
        self.assertEqual(item["source_kind"], "estimated")
        self.assertEqual(
            item["manual_check"]["url"],
            "https://aistudio.google.com/usage",
        )
        self.assertEqual(item["reset_kind"], "fixed")

    def test_build_secondary_quota_for_claude_weekly(self):
        observed_at = datetime(2026, 3, 22, 8, 0, tzinfo=timezone.utc)
        cfg = dict(tokenlens.DEFAULT_PROVIDER_SETTINGS["claude"])
        quota = tokenlens.build_secondary_quota(
            key="weekly",
            label="Weekly",
            window="7d",
            limit=2000,
            unit="tokens",
            provider_cfg=cfg,
            raw={"source": "local_logs", "total_tokens": 250},
            observed_at=observed_at,
        )
        self.assertEqual(quota["remaining"], 1750)
        self.assertEqual(quota["reset_kind"], "rolling")

    def test_normalize_details_redacts_sensitive_payloads(self):
        details = tokenlens.normalize_details(
            {
                "provider": "GitHub Copilot",
                "source": "official_api",
                "user": "alice",
                "usage": {"foo": "bar"},
                "note": "ok",
            }
        )
        self.assertTrue(details["sensitive_payload_redacted"])
        self.assertNotIn("user", details)
        self.assertNotIn("usage", details)
        self.assertNotIn("unexpected_fields_redacted", details)

    def test_normalize_details_uses_allowlist(self):
        details = tokenlens.normalize_details(
            {
                "provider": "Claude Code",
                "source": "local_logs",
                "sessions": 3,
                "note": "ok",
                "foo": "bar",
            }
        )
        self.assertEqual(details["sessions"], 3)
        self.assertEqual(details["note"], "ok")
        self.assertEqual(details["unexpected_fields_redacted"], ["foo"])
        self.assertNotIn("foo", details)

    def test_classify_cli_error_returns_fixed_messages(self):
        self.assertEqual(
            tokenlens.classify_cli_error("gh: Not Found (HTTP 404)"),
            "Endpoint not available for this account or plan.",
        )
        self.assertEqual(
            tokenlens.classify_cli_error("forbidden"),
            "Authentication or permissions are insufficient.",
        )

    def test_safe_glob_files_rejects_symlinks(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            real = root / "session.json"
            real.write_text("{}", encoding="utf-8")
            symlink = root / "symlink.json"
            symlink.symlink_to(real)

            result = tokenlens.safe_glob_files(root, "*.json")

            self.assertEqual(result, [real])


if __name__ == "__main__":
    unittest.main()
