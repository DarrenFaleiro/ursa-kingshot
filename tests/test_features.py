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
        prefs = self.db.guild_settings(1)["prefs"]
        self.assertEqual({k: prefs[k] for k in PRIVATE}, PRIVATE)
        self.assertIsNone(prefs["next_kvk"])
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


class TestUnlocks(unittest.TestCase):
    def setUp(self):
        from datetime import datetime, timezone
        self.utc = lambda y, m, d, h=0, mi=0: int(datetime(y, m, d, h, mi, tzinfo=timezone.utc).timestamp())
        # kingdom opened 2026-03-10 -> Gen 5 (day 270) starts 2026-12-05
        self.s = {"enabled": True, "channel_id": "1", "kingdom": "1844", "open_date": "2026-03-10",
                  "leads": [7, 1, 0], "categories": ["heroes"], "role_id": None}

    def test_open_date_from_current_day(self):
        from bot.unlocks import kingdom_day, open_date_from_day
        now = self.utc(2026, 11, 17, 9)
        od = open_date_from_day(252, now)
        self.assertEqual(od, "2026-03-10")
        self.assertEqual(kingdom_day(od, now), 252)

    def test_posts_at_noon_on_each_lead_day_once(self):
        from bot.unlocks import due_posts
        sent = set()
        self.assertEqual(due_posts(self.s, sent, self.utc(2026, 11, 28, 11, 59)), [])           # before 12:00
        due = due_posts(self.s, sent, self.utc(2026, 11, 28, 12, 0))                           # 7 days before
        self.assertEqual([(u["key"], lead) for u, lead, _ in due], [("gen5", 7)])
        sent.add(due[0][2])
        self.assertEqual(due_posts(self.s, sent, self.utc(2026, 11, 28, 15, 0)), [])           # not twice
        self.assertEqual([lead for _, lead, _ in due_posts(self.s, sent, self.utc(2026, 12, 4, 12, 1))], [1])
        self.assertEqual([lead for _, lead, _ in due_posts(self.s, sent, self.utc(2026, 12, 5, 13, 0))], [0])

    def test_late_bot_still_posts_same_day_but_not_next_day(self):
        from bot.unlocks import due_posts
        self.assertEqual(len(due_posts(self.s, set(), self.utc(2026, 12, 4, 20, 0))), 1)       # 8h late: ok
        self.assertEqual(due_posts(self.s, set(), self.utc(2026, 12, 6, 12, 1))[:0], [])
        late = [lead for _, lead, _ in due_posts(self.s, set(), self.utc(2026, 12, 6, 12, 1))]
        self.assertNotIn(0, late)                                                              # a day late: skipped

    def test_categories_and_off_switch(self):
        from bot.unlocks import due_posts
        pets_only = dict(self.s, categories=["pets"])
        self.assertEqual(due_posts(pets_only, set(), self.utc(2026, 11, 28, 12, 0)), [])
        self.assertEqual(due_posts(dict(self.s, enabled=False), set(), self.utc(2026, 11, 28, 12, 0)), [])

    def test_checker_sends_and_remembers(self):
        import asyncio
        from bot.reminders import run_due_unlocks
        db = DB(":memory:")
        db.save_unlock_settings(5, self.s)
        posted = []

        async def send(gid, settings, unlock, lead):
            posted.append((gid, unlock["key"], lead))

        now = self.utc(2026, 11, 28, 12, 0, )
        self.assertEqual(asyncio.run(run_due_unlocks(db, send, now)), 1)
        self.assertEqual(asyncio.run(run_due_unlocks(db, send, now + 60)), 0)
        self.assertEqual(posted, [("5", "gen5", 7)])

    def test_card(self):
        from bot.alert_card import render_card
        from bot.unlocks import card_for, unlock_list
        u = [x for x in unlock_list("2026-03-10", self.utc(2026, 11, 17)) if x["key"] == "gen5"][0]
        c = card_for(u, "1844", self.utc(2026, 11, 17))
        self.assertEqual((c["big"], c["unit"], c["tag"]), ("18", "DAYS", "KINGDOM 1844 · HEROES"))
        self.assertTrue(render_card(c).startswith(b"\x89PNG"))


class TestKvKPrep(unittest.TestCase):
    def test_orbio_example(self):
        from bot.kvk import parse_prep, prep_summary
        p = prep_summary(parse_prep([("1.37B", "771.2M"), ("1.23B", "541.6M"), ("850.9M", "356.7M"),
                                     ("1.35B", "454.0M"), ("1.17B", "478.7M")]))
        self.assertEqual((p["won"], p["entered"], p["result"]), (5, 5, "won"))
        self.assertAlmostEqual(p["lead"], 129.5, places=1)

    def test_partial_and_overrides(self):
        from bot.kvk import parse_prep, prep_summary
        prep = parse_prep([("1B", "2B"), ("", "")])
        self.assertEqual(prep_summary(prep)["result"], "pending")
        self.assertEqual(prep_summary(prep, "won")["result"], "won")                   # officer override
        three = parse_prep([("1B", "2B")] * 3)
        self.assertEqual(prep_summary(three)["result"], "lost")                        # 3 days lost = decided

    def test_record(self):
        from bot.kvk import kingdom_record, parse_prep
        win, loss = parse_prep([("2B", "1B")] * 5), parse_prep([("1B", "2B")] * 5)
        rec = kingdom_record([
            {"id": 1, "title": "a", "data": {"kvk_date": "2026-09-01", "prep": win, "castle_result": "won"}},
            {"id": 2, "title": "b", "data": {"kvk_date": "2026-10-01", "prep": loss, "castle_result": "lost"}},
            {"id": 3, "title": "c", "data": {"kvk_date": "2026-11-01", "prep": win, "castle_result": "pending"}}])
        self.assertEqual((rec["prep_w"], rec["prep_l"], rec["prep_rate"]), (2, 1, 67))
        self.assertEqual((rec["castle_w"], rec["castle_l"], rec["castle_rate"]), (1, 1, 50))
        self.assertEqual([r["id"] for r in rec["recent"]], [1, 2, 3])

    def test_image(self):
        from bot.kvk import kingdom_record, parse_prep, prep_summary
        from bot.kvk_card import render_kvk
        d = {"our_kingdom": "1", "their_kingdom": "2", "prep": parse_prep([("1B", "2B")])}
        self.assertTrue(render_kvk(d, prep_summary(d["prep"]), kingdom_record([])).startswith(b"\x89PNG"))
