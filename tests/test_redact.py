import unittest

from thread_to_skill.redact import redact_text


class RedactTests(unittest.TestCase):
    def test_redacts_tokens_and_emails(self):
        text = (
            "token ghp_FAKEfakeFAKEfakeFAKEfake1234567890 "
            "email priya.nair@acme.example "
            "slack xoxb-1234567890-abcdefghij"
        )
        cleaned, report = redact_text(text)
        self.assertNotIn("ghp_FAKE", cleaned)
        self.assertNotIn("priya.nair@acme.example", cleaned)
        self.assertNotIn("xoxb-", cleaned)
        self.assertIn("[redacted-github-token]", cleaned)
        self.assertIn("[redacted-email]", cleaned)
        self.assertGreaterEqual(report.total, 3)

    def test_keeps_versions_and_dates(self):
        text = "Upgrade from 1.2.3 to 2.0.0 on 2026-10-30, then rerun."
        cleaned, report = redact_text(text)
        self.assertIn("1.2.3", cleaned)
        self.assertIn("2026-10-30", cleaned)
        self.assertEqual(report.total, 0)

    def test_redacts_private_ip_and_basic_auth_url(self):
        text = "host 192.168.1.20 and https://user:pass@example.com/path"
        cleaned, report = redact_text(text)
        self.assertNotIn("192.168.1.20", cleaned)
        self.assertNotIn("user:pass", cleaned)
        self.assertGreaterEqual(report.total, 2)

    def test_custom_pattern(self):
        cleaned, report = redact_text("customer ACME-42 is internal", [r"ACME-\d+"])
        self.assertNotIn("ACME-42", cleaned)
        self.assertIn("custom-1", report.counts)

    def test_is_idempotent(self):
        cleaned, _ = redact_text("token ghp_FAKEfakeFAKEfakeFAKEfake1234567890")
        cleaned_twice, report = redact_text(cleaned)
        self.assertEqual(cleaned, cleaned_twice)
        self.assertEqual(report.total, 0)


if __name__ == "__main__":
    unittest.main()