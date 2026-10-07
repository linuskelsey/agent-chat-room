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
