import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.check_frontend import check
from scripts.scan_repo import scan


class FrontendCheckTests(unittest.TestCase):
    def test_valid_document_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            page = Path(directory, "index.html")
            page.write_text('<div id="app"></div><script>const answer = 42;</script>')
            self.assertEqual(check(page), [])

    def test_duplicate_id_and_invalid_javascript_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            page = Path(directory, "index.html")
            page.write_text('<div id="app"></div><p id="app"></p><script>const =;</script>')
            errors = check(page)
            self.assertTrue(any("duplicate id" in error for error in errors))
            self.assertTrue(any("failed syntax check" in error for error in errors))


class SecretScanTests(unittest.TestCase):
    @patch("scripts.scan_repo.tracked_files")
    def test_reports_secret_without_printing_it(self, files):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "config.txt"
            source.write_text("api_" + 'key = "' + "abcdefghijklmnopqrstuv" + '"\n')
            files.return_value = [source]
            self.assertEqual(scan(root), ["config.txt:1: generic-secret"])

    @patch("scripts.scan_repo.tracked_files")
    def test_allow_comment_suppresses_false_positive(self, files):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "example.txt"
            source.write_text(
                "api_" + 'key = "' + "abcdefghijklmnopqrstuv" + '" # secret-scan: allow\n'
            )
            files.return_value = [source]
            self.assertEqual(scan(root), [])


if __name__ == "__main__":
    unittest.main()
