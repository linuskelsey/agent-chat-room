import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest

SRC = os.path.join(os.path.dirname(__file__), "..", "src")
ORIGINAL = """python3 -c 'import sys; d = sys.stdin.read(); print("ORIG", len(d))'"""  # quotes of both kinds on purpose
SAMPLE = json.dumps({"model": {"id": "m"}, "rate_limits": {
    "five_hour": {"used_percentage": 23.5, "resets_at": int(time.time()) + 3600},
    "seven_day": {"used_percentage": 41.2, "resets_at": int(time.time()) + 86400}}})


class StatusLineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        root = self.tmp.name
        os.makedirs(os.path.join(root, "bin"))
        shim = os.path.join(root, "bin", "acm")
        with open(shim, "w") as f:
            f.write(f'#!/bin/sh\nexec {sys.executable} -m acm "$@"\n')
        os.chmod(shim, os.stat(shim).st_mode | stat.S_IEXEC)
        self.claude = os.path.join(root, "claude")
        os.makedirs(self.claude)
        self.settings = os.path.join(self.claude, "settings.json")
        self.limits = os.path.join(root, "limits.json")
        self.env = {
            **os.environ, "CLAUDE_CONFIG_DIR": self.claude, "ACM_CONFIG": os.path.join(root, "acmcfg", "config.toml"),
            "ACM_LIMITS_FILE": self.limits, "ACM_OMARCHY_USAGE_CACHE": "", "PYTHONPATH": SRC,
            "PATH": os.path.join(root, "bin") + os.pathsep + os.environ["PATH"],
        }

    def write_settings(self, data):
        with open(self.settings, "w") as f:
            json.dump(data, f, indent=2)

    def read_settings(self):
        with open(self.settings) as f:
            return json.load(f)

    def acm(self, *args, stdin="", env=None):
        return subprocess.run([sys.executable, "-m", "acm", *args], env=env or self.env, capture_output=True, text=True, input=stdin)

    def run_installed(self, stdin):
        command = self.read_settings()["statusLine"]["command"]
        return subprocess.run(["sh", "-c", command], env=self.env, capture_output=True, text=True, input=stdin)

    def test_install_wraps_the_existing_command_and_keeps_everything_else(self):
        original = {"hooks": {"Stop": []}, "model": "x", "statusLine": {"type": "command", "command": ORIGINAL, "padding": 1}}
        self.write_settings(original)
        out = self.acm("statusline", "install", "-y")
        self.assertEqual(out.returncode, 0, out.stderr)
        after = self.read_settings()
        self.assertEqual({k: v for k, v in after.items() if k != "statusLine"}, {"hooks": {"Stop": []}, "model": "x"})
        self.assertEqual((after["statusLine"]["type"], after["statusLine"]["padding"]), ("command", 1))
        self.assertIn("limits-tap", after["statusLine"]["command"])
        with open(self.settings + ".acm-backup") as f:
            self.assertEqual(json.load(f), original)  # untouched copy of what was there
        # the new command still prints what the old one printed, and also feeds acm
        run = self.run_installed(SAMPLE)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.strip(), f"ORIG {len(SAMPLE)}")
        self.assertIn("5-hour: 24% used", self.acm("limits").stdout)

    def test_install_twice_is_refused_and_uninstall_restores_exactly(self):
        original = {"statusLine": {"type": "command", "command": ORIGINAL}, "other": [1, 2]}
        self.write_settings(original)
        self.acm("statusline", "install", "-y")
        again = self.acm("statusline", "install", "-y")
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("already installed", again.stderr)
        self.assertIn("installed in", self.acm("statusline", "status").stdout)
        out = self.acm("statusline", "uninstall")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("restored your original status line", out.stdout)
        self.assertEqual(self.read_settings(), original)
        self.assertIn("not installed", self.acm("statusline", "status").stdout)

    def test_uninstall_will_not_clobber_a_status_line_changed_since(self):
        self.write_settings({"statusLine": {"type": "command", "command": ORIGINAL}})
        self.acm("statusline", "install", "-y")
        data = self.read_settings()
        data["statusLine"]["command"] = "echo hand edited"
        self.write_settings(data)
        refused = self.acm("statusline", "uninstall")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("was changed after acm installed it", refused.stderr)
        self.assertEqual(self.read_settings()["statusLine"]["command"], "echo hand edited")
        self.assertEqual(self.acm("statusline", "uninstall", "--force").returncode, 0)
        self.assertEqual(self.read_settings()["statusLine"]["command"], ORIGINAL)

    def test_with_no_status_line_acm_adds_a_small_usage_line_and_can_remove_it(self):
        self.write_settings({"model": "x"})
        self.assertEqual(self.acm("statusline", "install", "-y").returncode, 0)
        self.assertIn("limits-tap", self.read_settings()["statusLine"]["command"])
        run = self.run_installed(SAMPLE)
        self.assertEqual(run.stdout.strip(), "5h 24% · 7d 41%")
        self.assertEqual(self.acm("statusline", "uninstall").returncode, 0)
        self.assertEqual(self.read_settings(), {"model": "x"})

    def test_without_a_settings_file_one_is_created(self):
        self.assertFalse(os.path.exists(self.settings))
        self.assertEqual(self.acm("statusline", "install", "-y").returncode, 0)
        self.assertIn("statusLine", self.read_settings())
        self.assertEqual(self.acm("statusline", "uninstall").returncode, 0)
        self.assertEqual(self.read_settings(), {})

    def test_it_asks_first_and_changes_nothing_on_no(self):
        original = {"statusLine": {"type": "command", "command": ORIGINAL}}
        self.write_settings(original)
        for answer in ("n\n", ""):
            out = self.acm("statusline", "install", stdin=answer)
            self.assertIn("apply? [y/N]", out.stdout)
            self.assertEqual(self.read_settings(), original)
            self.assertFalse(os.path.exists(self.settings + ".acm-backup"))
        self.assertEqual(self.acm("statusline", "install", stdin="y\n").returncode, 0)
        self.assertIn("limits-tap", self.read_settings()["statusLine"]["command"])

    def test_it_is_refused_inside_a_claude_session(self):
        original = {"statusLine": {"type": "command", "command": ORIGINAL}}
        self.write_settings(original)
        out = self.acm("statusline", "install", "-y", env={**self.env, "CLAUDECODE": "1"})
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("must be run by you in a terminal", out.stderr)
        self.assertEqual(self.read_settings(), original)
        out = self.acm("statusline", "uninstall", env={**self.env, "CLAUDE_CODE_SESSION_ID": "x"})
        self.assertNotEqual(out.returncode, 0)

    def test_unusable_settings_are_left_alone(self):
        with open(self.settings, "w") as f:
            f.write("{ not json")
        out = self.acm("statusline", "install", "-y")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("cannot read", out.stderr)
        with open(self.settings) as f:
            self.assertEqual(f.read(), "{ not json")
        self.write_settings({"statusLine": {"type": "static", "text": "hi"}})
        out = self.acm("statusline", "install", "-y")
        self.assertIn("not a command", out.stderr)

    def test_hints_point_at_the_install_command_until_limits_are_visible(self):
        self.assertIn("acm statusline install", self.acm("limits").stdout)
        self.acm("new", "x")  # first use only; no daemon needed beyond this
        subprocess.run([sys.executable, "-m", "acm", "daemon", "stop"], env=self.env, capture_output=True)


if __name__ == "__main__":
    unittest.main()
