"""QP-24: the sanitize audit masks findings and ignores harmless noise."""
import unittest

import sanitize_audit as audit


class SanitizeAuditTests(unittest.TestCase):
    def test_qq_like_number_is_masked(self):
        findings = audit.scan_text("账号 100000002 发了消息")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["kind"], "qq_like")
        self.assertNotIn("100000002", findings[0]["sample"])
        self.assertTrue(findings[0]["sample"].endswith("***"))

    def test_epoch_timestamps_are_ignored(self):
        self.assertEqual(audit.scan_text("checked_at 1792651679 seconds"), [])

    def test_hex_embedded_digits_are_ignored(self):
        self.assertEqual(audit.scan_text("sha 4f47a1b2c3d4e5f60718293 123456789012345"), [])

    def test_windows_user_and_private_terms_are_masked(self):
        findings = audit.scan_text(r"install C:\Users\alice\AppData", terms=("alice",))
        kinds = {item["kind"] for item in findings}
        self.assertIn("windows_user", kinds)
        self.assertIn("term", kinds)
        self.assertTrue(all("USER" not in item["sample"] for item in findings))

    def test_email_is_masked(self):
        findings = audit.scan_text("author user@example.com")
        emails = [item for item in findings if item["kind"] == "email"]
        self.assertEqual(len(emails), 1)
        self.assertNotIn("100000001", emails[0]["sample"])

    def test_mask_helper(self):
        self.assertEqual(audit.mask("abcdef", 2), "ab***")
        self.assertEqual(audit.mask("a"), "***")


if __name__ == "__main__":
    unittest.main()
