"""Reminder timing + multi-server database tests. Run: python -m unittest discover tests"""
import sqlite3
import tempfile
import unittest

from bot.db import DB
from bot.reminders import due_alerts, format_alert, next_occurrence, parse_before

H = 3600


def rem(**kw):
    base = dict(id=1, kind="bear_trap", name="Bear Trap", start_at=100 * H, repeat_minutes=48 * 60,
                before_minutes="30,10,0", message=None, last_fire_ts=0, role_id=None)
    base.update(kw)
    return base


class TestReminderTiming(unittest.TestCase):
    def test_parse_before(self):
        self.assertEqual(parse_before("0, 30,10,10,x"), [30, 10, 0])
        self.assertEqual(parse_before(""), [0])

    def test_alerts_fire_at_the_right_times(self):
        r = rem()
        self.assertEqual(due_alerts(r, 100 * H - 31 * 60), [])                     # too early
        self.assertEqual([b for _, _, b in due_alerts(r, 100 * H - 30 * 60)], [30])  # 30 min before
        self.assertEqual([b for _, _, b in due_alerts(r, 100 * H - 10 * 60)], [10])  # 30-min one is >5 min late
        self.assertEqual([b for _, _, b in due_alerts(r, 100 * H + 60)], [0])        # 1 min late is fine

    def test_no_repeats_after_sending(self):
        r = rem()
        fire, _, _ = due_alerts(r, 100 * H - 30 * 60)[0]
        r["last_fire_ts"] = fire
        self.assertEqual(due_alerts(r, 100 * H - 29 * 60), [])

    def test_repeats_every_48h(self):
        r = rem(last_fire_ts=100 * H)                       # first occurrence done
        nxt = 148 * H
        self.assertEqual(next_occurrence(r, 101 * H), nxt)
        self.assertEqual([o for _, o, _ in due_alerts(r, nxt)], [nxt])

    def test_long_offline_does_not_spam_old_alerts(self):
        r = rem()
        self.assertEqual(due_alerts(r, 100 * H + 3 * H), [])  # 3h late -> skipped

    def test_one_off(self):
        r = rem(repeat_minutes=0, before_minutes="0")
        self.assertEqual(len(due_alerts(r, 100 * H)), 1)
        self.assertIsNone(next_occurrence(r, 101 * H))

    def test_message_text(self):
        r = rem(message="Rally up!")
        self.assertIn("starts in **30 minutes**", format_alert(r, 100 * H, 30))
        self.assertIn("starts in **2 hours**", format_alert(r, 100 * H, 120))
        self.assertIn("starting **now**", format_alert(r, 100 * H, 0))
        self.assertIn("Rally up!", format_alert(r, 100 * H, 0))


class TestRichAlerts(unittest.TestCase):
    def test_group_2_alert(self):
        from bot.reminders import build_alert
        r = rem(kind="bear_trap_2", name="Bear Trap — Group 2", before_minutes="30,15,0")
        a = build_alert(r, 100 * H, 15)
        self.assertEqual(a["title"], "🐻 Bear Trap — Group 2")
        self.assertIn("starts in **15 minutes**", a["description"])
        self.assertIn(f"<t:{100 * H}:R>", a["description"])       # live countdown in Discord
        self.assertEqual(a["color"], 0xBF5AF2)
        self.assertEqual(a["card"]["tag"], "GROUP 2 · EVERY 48 H")
        self.assertEqual(a["card"]["title"], "Bear Trap")

    def test_card_renders(self):
        from bot.alert_card import render_card
        from bot.reminders import build_alert
        png = render_card(build_alert(rem(kind="arena", name="Arena reset"), 100 * H, 60)["card"], "example.com")
        self.assertTrue(png.startswith(b"\x89PNG"))


class TestMultiServerDB(unittest.TestCase):
    def setUp(self):
        self.db = DB(":memory:")

    def test_players_per_server(self):
        self.db.add_player("1", "A", 10, "t", guild_id=111, discord_user_id=9)
        self.db.add_player("2", "B", 10, "t", guild_id=222)
        self.assertEqual([p["fid"] for p in self.db.list_players(111)], ["1"])
        self.assertEqual([p["fid"] for p in self.db.list_players(discord_user_id=9)], ["1"])
        self.assertEqual(len(self.db.list_players()), 2)

    def test_guild_results(self):
        self.db.add_player("1", "A", 10, "t", guild_id=111)
        self.db.add_player("2", "B", 10, "t", guild_id=222)
        self.db.record("1", "CODE", "success", "SUCCESS")
        self.db.record("2", "CODE", "already", "RECEIVED.")
        self.assertEqual(self.db.guild_results_since(111, 0), {"CODE": {"success": 1}})
        self.assertEqual(self.db.code_stats("CODE", 222), {"already": 1})

    def test_settings_and_reminders_are_per_server(self):
        self.db.set_guild_setting(111, "report_channel_id", 555)
        self.assertEqual(self.db.guild_settings(111)["report_channel_id"], "555")
        self.assertIsNone(self.db.guild_settings(222)["report_channel_id"])
        rid = self.db.add_reminder(111, "arena", "Arena", 555, 1000, 1440, "60,15")
        self.assertFalse(self.db.remove_reminder(rid, 222))   # other server can't delete it
        self.assertTrue(self.db.remove_reminder(rid, 111))

    def test_sessions(self):
        tok = self.db.create_session(9, "Darren", None, {"manage": ["111"], "member": ["111"]})
        self.assertEqual(self.db.get_session(tok)["guilds"]["manage"], ["111"])
        self.db.delete_session(tok)
        self.assertIsNone(self.db.get_session(tok))

    def test_upgrades_old_database(self):
        path = tempfile.mktemp(suffix=".db")
        old = sqlite3.connect(path)
        old.executescript("CREATE TABLE players (fid TEXT PRIMARY KEY, nickname TEXT, kid INTEGER, "
                          "enabled INTEGER NOT NULL DEFAULT 1, added_by TEXT, added_at INTEGER);"
                          "INSERT INTO players VALUES ('1','A',10,1,'web',0);"
                          "CREATE TABLE log (id INTEGER PRIMARY KEY AUTOINCREMENT, at INTEGER, text TEXT);")
        old.commit(); old.close()
        db = DB(path)
        self.assertIsNone(db.get_player("1")["guild_id"])     # kept, just no server yet
        db.log("hello", 111)


if __name__ == "__main__":
    unittest.main()
