"""Officer access, privacy masking, timeline, battle plans and KvK. Run: python -m unittest discover tests"""
import unittest

from bot import access as acl
from bot.db import DB
from bot.kvk import compare, parse_players, parse_power
from bot.plans import validate
from bot.reminders import timeline

H = 3600
PRIVATE = {"members_see_activity": False, "mask_for_members": True}
OPEN = {"members_see_activity": True, "mask_for_members": False}


def resolve(**kw):
    base = dict(is_owner=False, is_admin=False, is_member=True, user_id=9, role_ids=set(), grants=[], prefs=PRIVATE)
    base.update(kw)
    return acl.resolve(**base)


class TestAccess(unittest.TestCase):
    def test_admin_gets_everything(self):
        p = resolve(is_admin=True)
        self.assertTrue(all(p[s] == 2 for s in acl.SECTIONS))
        self.assertEqual((p["access"], p["mask"], p["role"]), (2, False, "admin"))

    def test_member_default_is_private(self):
        p = resolve()
        self.assertEqual((p["overview"], p["plans"], p["kvk"]), (1, 1, 1))     # can look
        self.assertEqual((p["claims"], p["players"], p["settings"], p["access"]), (0, 0, 0, 0))
        self.assertEqual(p["role"], "member")

    def test_server_can_open_claims_to_members(self):
        p = resolve(prefs=OPEN)
        self.assertEqual((p["claims"], p["mask"]), (1, False))
        p = resolve(prefs={"members_see_activity": True, "mask_for_members": True})
        self.assertEqual((p["claims"], p["mask"]), (1, True))

    def test_officer_role_grant(self):
        grants = [{"target_type": "role", "target_id": "44", "perms": {"reminders": "edit", "claims": "view"}}]
        p = resolve(role_ids={44}, grants=grants)
        self.assertEqual((p["reminders"], p["claims"], p["players"], p["access"]), (2, 1, 0, 0))
        self.assertEqual(p["role"], "officer")
        self.assertTrue(p["mask"])                 # no player access -> still masked

    def test_officer_user_grant_with_players_sees_names(self):
        grants = [{"target_type": "user", "target_id": "9", "perms": {"players": "view", "claims": "view"}}]
        p = resolve(grants=grants)
        self.assertEqual((p["players"], p["mask"]), (1, False))

    def test_grant_for_someone_else_does_nothing(self):
        grants = [{"target_type": "user", "target_id": "123", "perms": {"players": "edit"}}]
        self.assertEqual(resolve(grants=grants)["players"], 0)

    def test_not_in_server(self):
        p = resolve(is_member=False)
        self.assertTrue(all(p[s] == 0 for s in acl.SECTIONS))

    def test_masking(self):
        self.assertEqual(acl.mask_id("315357276"), "3153•••276")
        self.assertEqual(acl.mask_name("Commander"), "C•••")
        self.assertEqual(acl.mask_name(None), "•••")


class TestKvK(unittest.TestCase):
    def test_power_formats(self):
        for text, want in [("152.3M", 152_300_000), ("98,500,000", 98_500_000), ("1.2B", 1_200_000_000),
                           ("950K", 950_000), ("1,234.5M", 1_234_500_000)]:
            self.assertEqual(parse_power(text), want, text)

    def test_parse_and_compare(self):
        ours, bad = parse_players("Blade, 152.3M\nNova 98,500,000\n[ABC] Rook | 1.2B\nno number")
        self.assertEqual([p["name"] for p in ours], ["[ABC] Rook", "Blade", "Nova"])
        self.assertEqual(bad, ["no number"])
        theirs, _ = parse_players("Titan 1.1B\nAce 160M\nBolt 60M")
        r = compare(ours, theirs, 10)
        self.assertEqual((r["n"], r["wins"], r["mood"]), (3, 2, "good"))
        self.assertAlmostEqual(r["ratio"], 1450.8 / 1320, places=2)

    def test_empty_side(self):
        self.assertEqual(compare([], [], 10)["mood"], "neutral")


class TestPlans(unittest.TestCase):
    def test_validate_trims_and_limits(self):
        data = validate({"when": "123", "notes": "x" * 999,
                         "squads": [{"name": "", "leader": "Ace", "joiners": "A\n\nB\n"}] * 25})
        self.assertEqual(len(data["squads"]), 20)
        self.assertEqual(data["squads"][0]["joiners"], ["A", "B"])
        self.assertEqual(data["squads"][0]["name"], "Squad 1")
        self.assertEqual((data["when"], len(data["notes"])), (123, 500))


class TestTimeline(unittest.TestCase):
    def test_next_seven_days(self):
        rems = [dict(id=1, enabled=1, kind="bear_trap", name="Bear 1", start_at=10 * H, repeat_minutes=48 * 60,
                     before_minutes="30,0"),
                dict(id=2, enabled=1, kind="arena", name="Arena", start_at=11 * H, repeat_minutes=24 * 60,
                     before_minutes="60"),
                dict(id=3, enabled=0, kind="custom", name="Off", start_at=12 * H, repeat_minutes=0, before_minutes="0")]
        ev = timeline(rems, 9 * H, 7)
        self.assertEqual(sum(e["rem"]["id"] == 1 for e in ev), 4)     # every 48h over 7 days
        self.assertEqual(sum(e["rem"]["id"] == 2 for e in ev), 7)     # daily
        self.assertFalse(any(e["rem"]["id"] == 3 for e in ev))        # paused ones hidden
        self.assertEqual(ev[0]["occ"], 10 * H)
        self.assertEqual(ev[0]["alerts"], [10 * H - 1800, 10 * H])


class TestNewTables(unittest.TestCase):
    def setUp(self):
        self.db = DB(":memory:")

    def test_prefs_default_private(self):
        self.assertEqual(self.db.guild_settings(1)["prefs"], PRIVATE)
        self.db.set_prefs(1, members_see_activity=True, nonsense=1)
        self.assertEqual(self.db.guild_settings(1)["prefs"]["members_see_activity"], True)
        self.assertNotIn("nonsense", self.db.guild_settings(1)["prefs"])

    def test_grants_per_server(self):
        self.db.set_grant(1, "role", 44, "@R4", {"players": "view"})
        self.db.set_grant(1, "role", 44, "@R4", {"players": "edit"})        # update, not duplicate
        self.assertEqual([g["perms"] for g in self.db.list_grants(1)], [{"players": "edit"}])
        gid = self.db.list_grants(1)[0]["id"]
        self.assertFalse(self.db.remove_grant(gid, 2))                       # other server can't remove
        self.assertTrue(self.db.remove_grant(gid, 1))

    def test_plans_and_kvk_are_per_server(self):
        pid = self.db.save_plan(1, "Push", "kvk", {"squads": []}, "me")
        self.assertIsNone(self.db.get_plan(pid, 2))
        self.db.save_plan(1, "Push 2", "kvk", {"squads": []}, "me", pid)
        self.assertEqual(self.db.get_plan(pid, 1)["name"], "Push 2")
        kid = self.db.save_kvk(1, "S3", {"ours": [], "theirs": []}, "me")
        self.assertFalse(self.db.delete_kvk(kid, 2))
        self.assertEqual(len(self.db.list_kvk(1)), 1)

    def test_recent_claims_only_this_server(self):
        self.db.add_player("1", "A", 10, "t", guild_id=1)
        self.db.add_player("2", "B", 10, "t", guild_id=2)
        self.db.record("1", "C", "success", "SUCCESS")
        self.db.record("2", "C", "success", "SUCCESS")
        self.assertEqual([c["fid"] for c in self.db.recent_claims(1)], ["1"])


if __name__ == "__main__":
    unittest.main()
