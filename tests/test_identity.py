import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from acm import identity


class IdentityTest(unittest.TestCase):
    def test_sanitize(self):
        self.assertEqual(identity.sanitize("arx 0d!"), "arx-0d")
        self.assertEqual(identity.sanitize("--x--"), "x")
        self.assertEqual(identity.sanitize("???"), "")

    def test_member_name_precedence(self):
        info = {"pid": 42, "name": "hub-e9"}
        with mock.patch.dict(os.environ, {"ACM_NAME": ""}):
            self.assertEqual(identity.member_name(info), "hub-e9")
            self.assertEqual(identity.member_name({"pid": 42, "name": None}), "agent-42")
        with mock.patch.dict(os.environ, {"ACM_NAME": "override"}):
            self.assertEqual(identity.member_name(info), "override")

    def test_find_session_walks_ancestors(self):
        with tempfile.TemporaryDirectory() as d:
            sessions = Path(d) / "sessions"
            sessions.mkdir()
            (sessions / "100.json").write_text(json.dumps({"pid": 100, "name": "top"}))
            (sessions / "101.json").write_text(json.dumps({"pid": 999, "name": "mismatched"}))
            chain = {500: 400, 400: 101, 101: 100, 100: 1}
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": d}), mock.patch.object(
                identity, "_parent", side_effect=lambda p: chain.get(p, 0)
            ):
                info = identity.find_session(start_pid=500 + 1)  # parent of 501 is 0 -> none
                self.assertIsNone(info)
                chain[501] = 500
                info = identity.find_session(start_pid=501)
                self.assertEqual(info["name"], "top")  # the pid-mismatched record is ignored

    def test_find_session_none_without_ancestor_record(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": d}):
            self.assertIsNone(identity.find_session())


if __name__ == "__main__":
    unittest.main()
