"""`jobagent sample` ukládá do samples/private/, mimo git.

Skutečná stránka obsahuje plné texty inzerátů a kontakty personalistů. Do
sledovaného samples/ patří jen anonymizované kopie (tools/anonymize_samples.py).
"""

import argparse
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jobagent import cli, paths
from jobagent.fetch import Response

from tests.helpers import SAMPLES

URL = "https://www.example.cz/prace/inzerat_id1"


class FakeFetcher:
    def get(self, url):
        return Response(url=url, text="<html><body>inzerát</body></html>", status=200)


class SampleCommandTests(unittest.TestCase):
    def test_saves_to_private_folder(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(paths, "app_dir", return_value=Path(tmp)), \
                mock.patch.object(cli, "_make_fetcher", return_value=FakeFetcher()), \
                mock.patch.object(cli, "Fetcher", FakeFetcher), \
                self.assertLogs("jobagent", level="INFO") as logs:
            code = cli.cmd_sample(argparse.Namespace(url=URL, name="ukazka"))
            saved = Path(tmp) / "samples" / "private" / "ukazka.html"
            self.assertEqual(code, 0)
            self.assertTrue(saved.exists())
            self.assertFalse((Path(tmp) / "samples" / "ukazka.html").exists())
            self.assertTrue(saved.read_text(encoding="utf-8").startswith(f"<!-- Zdroj: {URL}"))
        self.assertTrue(any("anonymize_samples" in line for line in logs.output))

    def test_private_folder_is_ignored_by_git(self):
        ignored = (SAMPLES.parent / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("samples/private/", ignored)
        self.assertEqual(paths.private_samples_dir(), paths.samples_dir() / "private")


if __name__ == "__main__":
    unittest.main()
