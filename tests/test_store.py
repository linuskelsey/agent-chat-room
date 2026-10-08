import unittest

from acm.db import Store, parse_mentions
from acm.errors import AcmError


def code_of(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except AcmError as e:
        return e.code
    return None


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.s = Store(":memory:")
        self.addCleanup(self.s.close)
        self.s.create_room("feat", "ann", "a feature")

    def test_ordering_and_ids(self):
        ids = [self.s.post("feat", a, f"m{i}")["id"] for i, a in enumerate("abcab")]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(set(ids)), 5)
        got = [m["body"] for m in self.s.read("feat", "ann", since=0)["messages"]]
        self.assertEqual(got, ["m0", "m1", "m2", "m3", "m4"])

    def test_cursor_advances_and_returns_only_new(self):
        self.s.post("feat", "bob", "one")
        self.s.post("feat", "bob", "two")
        first = self.s.read("feat", "ann")
        self.assertEqual([m["body"] for m in first["messages"]], ["one", "two"])
        self.assertEqual(self.s.read("feat", "ann")["messages"], [])
        self.s.post("feat", "bob", "three")
        self.assertEqual([m["body"] for m in self.s.read("feat", "ann")["messages"]], ["three"])

    def test_peek_and_explicit_since_leave_cursor_alone(self):
        self.s.post("feat", "bob", "one")
        self.assertEqual(len(self.s.read("feat", "ann", peek=True)["messages"]), 1)
        self.assertEqual(len(self.s.read("feat", "ann", since=0)["messages"]), 1)
        self.assertEqual(len(self.s.read("feat", "ann")["messages"]), 1)

    def test_cursors_are_per_member(self):
        self.s.post("feat", "ann", "hi")
        self.assertEqual(len(self.s.read("feat", "bob")["messages"]), 1)
        self.assertEqual(len(self.s.read("feat", "ann")["messages"]), 1)

    def test_closed_room_rejects_posts_and_joins(self):
        self.s.close_room("feat", "ann")
        self.assertEqual(code_of(self.s.post, "feat", "ann", "x"), "room_closed")
        self.assertEqual(code_of(self.s.join, "feat", "bob"), "room_closed")
        self.assertEqual(self.s.get_room("feat")["status"], "closed")
        self.assertEqual(self.s.read("feat", "ann", since=0)["messages"][-1]["kind"], "system")

    def test_only_creator_can_close_but_kill_forces(self):
        self.assertEqual(code_of(self.s.close_room, "feat", "bob"), "forbidden")
        self.assertEqual(self.s.get_room("feat")["status"], "open")
        self.s.close_room("feat", "bob", force=True)
        self.assertEqual(self.s.get_room("feat")["closed_by"], "bob")

    def test_decisions_returned_separately_and_always(self):
        self.s.post("feat", "ann", "use sqlite", kind="decision")
        self.s.post("feat", "bob", "ok")
        self.s.read("feat", "ann")
        self.s.post("feat", "bob", "later")
        res = self.s.read("feat", "ann")
        self.assertEqual([m["body"] for m in res["messages"]], ["later"])
        self.assertEqual([d["body"] for d in res["decisions"]], ["use sqlite"])

    def test_exclude_own_skips_posts_but_moves_cursor(self):
        self.s.post("feat", "bob", "b1")
        self.s.post("feat", "ann", "mine")
        res = self.s.read("feat", "ann", exclude_own=True)
        self.assertEqual([m["body"] for m in res["messages"]], ["b1"])
        self.s.post("feat", "ann", "mine again")
        res = self.s.read("feat", "ann", exclude_own=True)
        self.assertEqual(res["messages"], [])
        self.assertEqual(self.s.members("feat")["ann"]["cursor"], res["cursor"])
        self.s.post("feat", "bob", "b2")
        self.assertEqual([m["body"] for m in self.s.read("feat", "ann", exclude_own=True)["messages"]], ["b2"])

    def test_limit_pages_without_skipping(self):
        for i in range(5):
            self.s.post("feat", "bob", f"m{i}")
        got = []
        for _ in range(3):
            got += [m["body"] for m in self.s.read("feat", "ann", limit=2, exclude_own=True)["messages"]]
        self.assertEqual(got, ["m0", "m1", "m2", "m3", "m4"])

    def test_auto_decisions_only_when_new_or_fresh(self):
        self.s.post("feat", "ann", "use sqlite", kind="decision")
        first = self.s.read("feat", "bob", decisions="auto")
        self.assertEqual(len(first["decisions"]), 1)  # fresh reader gets them
        self.s.post("feat", "ann", "chatter")
        self.assertEqual(self.s.read("feat", "bob", decisions="auto")["decisions"], [])
        self.s.post("feat", "ann", "use passkeys", kind="decision")
        res = self.s.read("feat", "bob", decisions="auto")
        self.assertEqual([d["body"] for d in res["decisions"]], ["use sqlite", "use passkeys"])
        self.assertEqual(len(self.s.read("feat", "bob")["decisions"]), 2)  # default mode is all

    def test_muted_member_cannot_post(self):
        self.s.join("feat", "bob", "agent")
        self.s.set_muted("feat", "bob", True)
        self.assertEqual(code_of(self.s.post, "feat", "bob", "x", from_kind="agent"), "muted")
        self.s.set_muted("feat", "bob", False)
        self.s.post("feat", "bob", "x", from_kind="agent")

    def test_validation(self):
        self.assertEqual(code_of(self.s.create_room, "Bad Name", "ann"), "bad_name")
        self.assertEqual(code_of(self.s.create_room, "feat", "ann"), "exists")
        self.assertEqual(code_of(self.s.post, "feat", "ann", "   "), "bad_request")
        self.assertEqual(code_of(self.s.post, "nope", "ann", "x"), "not_found")
        self.assertEqual(code_of(self.s.post, "feat", "bad name", "x"), "bad_name")
        self.assertEqual(code_of(self.s.post, "feat", "bob", "x", kind="human_approve", from_kind="agent"), "forbidden")

    def test_mentions_parsed(self):
        self.assertEqual(parse_mentions("hey @arx-0d and @hub-e9. also a@b.com @arx-0d"), ["arx-0d", "hub-e9"])
        m = self.s.post("feat", "ann", "ping @bob")
        self.assertEqual(m["mentions"], ["bob"])

    def test_list_rooms_unread(self):
        self.s.post("feat", "bob", "a")
        self.s.post("feat", "bob", "b")
        self.assertEqual(self.s.list_rooms(member="ann")[0]["unread"], 2)
        self.s.read("feat", "ann")
        self.assertEqual(self.s.list_rooms(member="ann")[0]["unread"], 0)
        self.assertEqual(self.s.list_rooms(status="closed"), [])

    def test_tail(self):
        for i in range(5):
            self.s.post("feat", "ann", f"m{i}")
        self.assertEqual([m["body"] for m in self.s.tail("feat", 2)], ["m3", "m4"])

    def test_agents_joining_and_leaving_leave_a_line_in_the_room(self):
        seen = []
        self.s.on_system = lambda room, msg: seen.append(msg["body"])
        self.s.catch_up("feat", "arx")  # an agent shows up
        self.s.catch_up("feat", "arx")  # again: nothing new
        self.s.join("feat", "bob", "human")  # humans do not get a line
        self.s.leave("feat", "arx")
        self.s.leave("feat", "bob")
        self.assertEqual(seen, ["arx joined", "arx left"])
        bodies = [m["body"] for m in self.s.read("feat", "ann", since=0)["messages"]]
        self.assertEqual(bodies, ["arx joined", "arx left"])

    def test_invited_agents_join_when_they_first_act(self):
        seen = []
        self.s.on_system = lambda room, msg: seen.append(msg["body"])
        self.s.join("feat", "newbie", "agent", pending=True)
        self.assertEqual(seen, [])
        self.assertFalse(self.s.members("feat")["newbie"]["joined"])
        self.s.read("feat", "newbie", kind="agent")  # first action
        self.assertEqual(seen, ["newbie joined"])
        self.assertTrue(self.s.members("feat")["newbie"]["joined"])
        self.s.post("feat", "newbie", "hi", from_kind="agent")
        self.assertEqual(seen, ["newbie joined"])  # only once

    def test_system_lines_are_not_unread_messages(self):
        self.s.catch_up("feat", "arx")
        self.s.catch_up("feat", "zed")
        self.assertEqual(self.s.unread_count("feat", "arx"), 0)
        self.assertEqual(self.s.list_rooms(member="arx")[0]["unread"], 0)
        self.s.post("feat", "ann", "real message")
        self.assertEqual(self.s.unread_count("feat", "arx"), 1)

    def test_wake_cost_separates_cold_and_warm_wakes(self):
        self.assertEqual(self.s.wake_cost("arx"), {"warm": 25000.0, "cold": 90000.0, "n": 0})  # nothing known yet
        self.s.add_usage("feat", "arx", "wake", 10, 90, 0, 1000)  # warm: 200
        self.s.add_usage("feat", "arx", "wake", 10, 190, 0, 1000)  # warm: 300
        self.s.add_usage("feat", "arx", "wake", 0, 0, 80000, 0)  # cold (it wrote its whole context to the cache)
        self.s.add_usage("feat", "arx", "post", 0, 50, 0, 0)  # posts are not wakes
        cost = self.s.wake_cost("arx")
        self.assertEqual((cost["warm"], cost["cold"], cost["n"]), (250.0, 80000.0, 3))
        self.s.catch_up("feat", "arx")
        self.assertEqual(self.s.members("feat")["arx"]["wake_cost"]["n"], 3)
        self.assertIsNone(self.s.members("feat")["ann"]["wake_cost"])  # humans have none

    def test_limits_layering_and_validation(self):
        import tempfile, os
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            cfg = os.path.join(d, "config.toml")
            with open(cfg, "w") as f:
                f.write('[defaults]\nmax_messages = 50\nstyle = "normal"\n')
            with mock.patch.dict(os.environ, {"ACM_CONFIG": cfg}):
                lim = self.s.limits("feat")
                self.assertEqual((lim["max_messages"], lim["style"], lim["max_minutes"]), (50, "normal", 240))
                self.assertEqual(self.s.set_limits("feat", {"max_messages": "10"})["max_messages"], 10)  # room beats file
                self.assertEqual(self.s.set_limits("feat", {"max_messages": "default"})["max_messages"], 50)
                self.assertIsNone(self.s.set_limits("feat", {"pause_session_pct": "none"})["pause_session_pct"])
            with open(cfg, "w") as f:
                f.write('[defaults]\nbogus = 1\n')
            with mock.patch.dict(os.environ, {"ACM_CONFIG": cfg}):
                self.assertEqual(code_of(self.s.limits, "feat"), "bad_request")
        self.assertEqual(code_of(self.s.set_limits, "feat", {"style": "loud"}), "bad_request")
        self.assertEqual(code_of(self.s.set_limits, "feat", {"max_messages": "-1"}), "bad_request")
        self.assertEqual(code_of(self.s.set_limits, "feat", {"max_messages": "many"}), "bad_request")
        self.assertEqual(code_of(self.s.set_limits, "feat", {"nonsense": "1"}), "bad_request")

    def test_migrations_are_idempotent(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.db")
            first = Store(p)
            first.create_room("r", "ann")
            first.close()
            again = Store(p)
            self.assertEqual(again.get_room("r")["created_by"], "ann")
            again.close()


if __name__ == "__main__":
    unittest.main()
