import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parent.parent


class CLITests(unittest.TestCase):
    def invoke(self, *arguments):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("TELEGRAM_", "VK_", "PRIVATE_INBOX", "HASHTAG", "DATA_DIR"))}
        env["PYTHONIOENCODING"] = "cp1251"
        return subprocess.run([sys.executable, "-m", "bridge", *arguments], cwd=PROJECT,
                              env=env, input="", text=True, encoding="utf-8", capture_output=True, timeout=10)

    def test_help_with_windows_pipe_encoding(self):
        result = self.invoke("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("discover-owner", result.stdout)
        self.assertIn("Telegram", result.stdout)

    def test_missing_credentials_fail_without_network(self):
        with tempfile.TemporaryDirectory() as temp:
            result = self.invoke("--env-file", str(Path(temp) / "missing"), "check")
        self.assertEqual(result.returncode, 2)
        self.assertIn("TELEGRAM_BOT_TOKEN", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_init_refuses_noninteractive_secret_input(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / ".env"
            result = self.invoke("--env-file", str(path), "init")
            self.assertFalse(path.exists())
        self.assertEqual(result.returncode, 2)
        self.assertIn("интерактивный", result.stderr)


if __name__ == "__main__":
    unittest.main()
