import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest import mock

from acm import config

SRC = os.path.join(os.path.dirname(__file__), "..", "src")


class ExampleConfigTest(unittest.TestCase):
    def test_example_is_valid_toml_with_everything_commented_out(self):
        text = config.example_text()
        self.assertEqual(tomllib.loads(text), {"defaults": {}})  # nothing active
        for key in config.SPEC:
            self.assertIn(f"# {key} = ", text)

    def test_every_example_line_is_valid_when_uncommented(self):
        text = config.example_text()
        keys = tuple(f"# {k} = " for k in config.SPEC)
        active = "\n".join(line[2:].split("  #")[0] if line.startswith(keys) else line for line in text.splitlines())
        parsed = tomllib.loads(active)["defaults"]
        self.assertEqual(set(parsed), set(config.SPEC))
        for key, value in parsed.items():
            config.coerce(key, value)  # raises if an example value is not acceptable

    def test_descriptions_come_from_the_settings_table(self):
        text = config.example_text()
        for key, (_, _, description) in config.SPEC.items():
            self.assertIn(f"# {description}", text)


class FirstUseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        self.dir = os.path.join(self.tmp.name, "newdir")
        self.path = os.path.join(self.dir, "config.toml")
        self.env = {**os.environ, "ACM_CONFIG": self.path, "ACM_RUNTIME": os.path.join(self.tmp.name, "run"),
                    "ACM_DATA": os.path.join(self.tmp.name, "data"), "ACM_ALLOW_NO_TTY": "1", "PYTHONPATH": SRC,
                    "CLAUDE_CONFIG_DIR": os.path.join(self.tmp.name, "claude")}
        self.addCleanup(lambda: subprocess.run([sys.executable, "-m", "acm", "daemon", "stop"], env=self.env, capture_output=True))

    def acm(self, *args, stdin=None):
        return subprocess.run([sys.executable, "-m", "acm", *args], env=self.env, capture_output=True, text=True, input=stdin)

    def test_first_use_creates_the_example_once(self):
        first = self.acm("ls")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn(f"wrote an example config (all commented out) to {self.path}", first.stderr)
        with open(self.path) as f:
            self.assertEqual(f.read(), config.example_text())
        self.assertEqual(self.acm("ls").stderr, "")  # silent afterwards

    def test_a_deleted_file_is_not_recreated_while_the_directory_exists(self):
        self.acm("ls")
        os.remove(self.path)
        self.assertEqual(self.acm("ls").stderr, "")
        self.assertFalse(os.path.exists(self.path))

    def test_an_existing_file_is_never_touched(self):
        os.makedirs(self.dir)
        with open(self.path, "w") as f:
            f.write('[defaults]\nmax_messages = 7\n')
        self.assertEqual(self.acm("ls").stderr, "")
        with open(self.path) as f:
            self.assertIn("max_messages = 7", f.read())

    def test_hooks_and_the_status_line_helper_never_create_it(self):
        self.assertEqual(self.acm("unread", "--hook").stdout, "")
        self.acm("limits-tap", stdin="{}")
        self.assertFalse(os.path.exists(self.dir))

    def test_an_unwritable_location_is_ignored(self):
        env = {**self.env, "ACM_CONFIG": "/proc/nope/acm/config.toml"}
        r = subprocess.run([sys.executable, "-m", "acm", "ls"], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")

    def test_config_subcommands(self):
        self.assertEqual(self.acm("config", "path").stdout.strip(), self.path)
        shown = self.acm("config", "show").stdout
        self.assertIn("max_messages = 200  (built-in default)", shown)
        self.assertIn("(not created yet)", shown)
        self.assertIn(f"wrote {self.path}", self.acm("config", "init").stdout)
        again = self.acm("config", "init")
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("use --force", again.stderr)
        with open(self.path, "w") as f:
            f.write('[defaults]\nmax_messages = 50\n')
        self.assertIn("max_messages = 50  (config file)", self.acm("config", "show").stdout)
        self.assertEqual(self.acm("config", "init", "--force").returncode, 0)
        with open(self.path) as f:
            self.assertEqual(f.read(), config.example_text())


if __name__ == "__main__":
    unittest.main()
