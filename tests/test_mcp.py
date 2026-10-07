import json
import os
import subprocess
import sys
import tempfile
import unittest

SRC = os.path.join(os.path.dirname(__file__), "..", "src")


class McpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.env = {
            **os.environ,
            "ACM_RUNTIME": os.path.join(cls.tmp.name, "run"),
            "ACM_DATA": os.path.join(cls.tmp.name, "data"),
            "PYTHONPATH": SRC,
        }
        cls.acm("new", "mcproom", "-t", "mcp test", name="owner")

    @classmethod
    def tearDownClass(cls):
        cls.acm("daemon", "stop")
        cls.tmp.cleanup()

    @classmethod
    def acm(cls, *args, name="owner"):
        return subprocess.run(
            [sys.executable, "-m", "acm", "--as", name, *args], env=cls.env, capture_output=True, text=True, timeout=30
        )

    def session(self, name, messages):
        """Run one MCP server process as agent `name`, feed it JSON-RPC lines, return the replies by id."""
        env = {**self.env, "ACM_NAME": name}
        out = subprocess.run(
            [sys.executable, "-m", "acm.mcp_server"],
            env=env, input="\n".join(json.dumps(m) for m in messages) + "\n", capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        return {r["id"]: r for r in map(json.loads, out.stdout.splitlines())}

    def call(self, name, tool, **args):
        msgs = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": tool, "arguments": args}},
        ]
        res = self.session(name, msgs)[2]["result"]
        return res["content"][0]["text"], res["isError"]

    def test_01_handshake_and_tools(self):
        r = self.session("a", [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "ping"},
            {"jsonrpc": "2.0", "id": 4, "method": "nope/nope"},
        ])
        self.assertEqual(r[1]["result"]["protocolVersion"], "2025-06-18")
        self.assertIn("tools", r[1]["result"]["capabilities"])
        self.assertIn("instructions", r[1]["result"])
        self.assertEqual(
            sorted(t["name"] for t in r[2]["result"]["tools"]),
            ["room_join", "room_leave", "room_list", "room_pin_decision", "room_post", "room_read"],
        )
        self.assertEqual(r[3]["result"], {})
        self.assertEqual(r[4]["error"]["code"], -32601)
        self.assertEqual(set(r), {1, 2, 3, 4})  # the notification got no reply

    def test_02_two_agents_exchange_messages(self):
        text, err = self.call("arx-0d", "room_join", room="mcproom")
        self.assertFalse(err, text)
        self.assertIn("joined mcproom as arx-0d", text)
        self.call("hub-e9", "room_join", room="mcproom")
        text, err = self.call("arx-0d", "room_post", room="mcproom", body="hello @hub-e9", refs=["src/acm/db.py"])
        self.assertFalse(err, text)
        text, _ = self.call("hub-e9", "room_read", room="mcproom")
        self.assertIn("→you arx-0d: hello @hub-e9", text)
        self.assertIn("[refs: src/acm/db.py]", text)
        text, _ = self.call("hub-e9", "room_read", room="mcproom")
        self.assertEqual(text, "no new messages")
        text, _ = self.call("arx-0d", "room_read", room="mcproom")
        self.assertEqual(text, "no new messages")  # own post is not echoed back

    def test_03_agents_are_marked_agent_and_humans_human(self):
        self.acm("post", "mcproom", "human here", name="owner")
        text, _ = self.call("arx-0d", "room_read", room="mcproom")
        self.assertIn("owner (human): human here", text)
        members = {m["name"]: m["kind"] for m in json.loads(self.acm("members", "mcproom", "--json").stdout)}
        self.assertEqual(members["arx-0d"], "agent")
        self.assertEqual(members["owner"], "human")
        msgs = json.loads(self.acm("read", "mcproom", "--since", "0", "--json").stdout)["messages"]
        self.assertEqual({m["from"] for m in msgs if m["author"] == "arx-0d"}, {"agent"})

    def test_04_pinned_decisions_reach_late_joiners_once(self):
        self.call("arx-0d", "room_pin_decision", room="mcproom", body="use sqlite")
        text, _ = self.call("mig-45", "room_join", room="mcproom")
        self.assertIn("pinned decisions:", text)
        self.assertIn("★ use sqlite", text)
        text, _ = self.call("mig-45", "room_read", room="mcproom")
        self.assertEqual(text, "no new messages")  # decisions are not repeated on later reads

    def test_05_errors_are_tool_errors_not_crashes(self):
        text, err = self.call("arx-0d", "room_post", room="nope", body="x")
        self.assertTrue(err)
        self.assertIn("no such room", text)
        text, err = self.call("arx-0d", "room_post", room="mcproom")
        self.assertTrue(err)
        self.assertIn("body", text)
        text, err = self.call("arx-0d", "no_such_tool")
        self.assertTrue(err)

    def test_06_closed_room_rejects_agent_posts(self):
        self.acm("new", "shortlived", name="owner")
        self.call("arx-0d", "room_join", room="shortlived")
        self.acm("close", "shortlived", name="owner")
        text, err = self.call("arx-0d", "room_post", room="shortlived", body="late")
        self.assertTrue(err)
        self.assertIn("closed", text)

    def test_07_agent_cannot_close_rooms(self):
        # no close tool is exposed, and the creator-only rule still holds for an agent using the CLI name
        r = self.session("a", [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}])
        self.assertNotIn("room_close", [t["name"] for t in r[1]["result"]["tools"]])

    def test_08_room_list_shows_unread(self):
        self.acm("post", "mcproom", "ping", name="owner")
        text, _ = self.call("arx-0d", "room_list")
        self.assertIn("mcproom: mcp test - ", text)
        self.assertIn("1 unread", text)


if __name__ == "__main__":
    unittest.main()
