"""Tests for the core logic. Run with:  python -m unittest discover tests
Uses a fake website so nothing real is called."""
import asyncio
import hashlib
import unittest

from bot.codes_feed import extract_codes
from bot.db import DB
from bot.ks_api import Result, classify, make_sign
from bot.redeemer import Redeemer


class FakeAPI:
    """Pretends to be the gift-code website."""
    def __init__(self):
        self.calls = []
        self.redeemed = set()
        self.players = {"111": {"nickname": "Alice", "kid": 12}, "222": {"nickname": "Bob", "kid": 12}}
        self.expired = {"OLDCODE"}

    async def check_salt(self):
        return Result("ok", "success")

    async def redeem(self, fid, kid, code):
        self.calls.append(("redeem", fid, code))
        if self.players.get(fid, {}).get("kid") != kid:
            return Result("wrong_kingdom", "USER INFO ERROR")
        if code in self.expired:
            return Result("expired", "TIME ERROR.")
        if (fid, code) in self.redeemed:
            return Result("already", "RECEIVED.")
        self.redeemed.add((fid, code))
        return Result("success", "SUCCESS")


def run(coro):
    return asyncio.run(coro)


class TestSign(unittest.TestCase):
    def test_sign_sorts_keys(self):
        s = make_sign({"time": 2, "fid": 1}, "SALT")
        self.assertEqual(s, hashlib.md5(b"fid=1&time=2SALT").hexdigest())


class TestClassify(unittest.TestCase):
    def test_messages(self):
        self.assertEqual(classify({"code": 0, "msg": "SUCCESS"}), "success")
        self.assertEqual(classify({"code": 1, "msg": "RECEIVED.", "err_code": 40008}), "already")
        self.assertEqual(classify({"code": 1, "msg": "TIME ERROR.", "err_code": 40007}), "expired")
        self.assertEqual(classify({"code": 1, "msg": "time Expired"}), "expired")
        self.assertEqual(classify({"code": 1, "msg": "CDK NOT FOUND.", "err_code": 40014}), "invalid")
        self.assertEqual(classify({"code": 1, "msg": "role not exist.", "err_code": 40001}), "no_player")
        self.assertEqual(classify({"code": 1, "msg": "Sign Error"}), "sign_error")
        self.assertEqual(classify({"code": 1, "msg": "USER INFO ERROR", "err_code": 40020}), "wrong_kingdom")
        self.assertEqual(classify({"code": 1, "msg": "TOO FREQUENT", "err_code": 40019}), "rate_limited")
        self.assertEqual(classify({"code": 1, "msg": "???"}), "error")


class TestFeed(unittest.TestCase):
    def test_shapes(self):
        self.assertEqual(extract_codes({"data": {"giftCodes": [{"code": "A1B2"}, {"code": "C3D4"}]}}),
                         ["A1B2", "C3D4"])
        self.assertEqual(extract_codes({"codes": ["KS2026", "VIP777"]}), ["KS2026", "VIP777"])
        self.assertEqual(extract_codes([{"code": "OLD1", "isExpired": True}, {"code": "NEW1"}]), ["NEW1"])
        self.assertEqual(extract_codes([{"code": "OLD2", "expiresAt": "2025-01-01T00:00:00Z"},
                                        {"code": "NEVER", "expiresAt": None},
                                        {"code": "LATER", "expiresAt": "2099-01-01T00:00:00.000Z"}]),
                         ["NEVER", "LATER"])


class TestRedeemer(unittest.TestCase):
    def setUp(self):
        self.db = DB(":memory:")
        self.api = FakeAPI()
        self.r = Redeemer(self.db, self.api, delay=0)

    def test_add_player_validates(self):
        ok, msg = run(self.r.add_player("111", "12", "test", "Alice"))
        self.assertTrue(ok)
        self.assertEqual(self.db.get_player("111")["nickname"], "Alice")
        self.assertFalse(run(self.r.add_player("111", "12", "test"))[0])  # duplicate
        self.assertFalse(run(self.r.add_player("abc", "12", "test"))[0])  # not a number
        self.assertFalse(run(self.r.add_player("333", "", "test"))[0])    # no kingdom

    def test_full_run_and_no_repeats(self):
        run(self.r.add_player("111", "12", "t"))
        run(self.r.add_player("222", "12", "t"))
        self.r.add_code("GOOD1", "web")
        self.r.add_code("OLDCODE", "web")

        summary = run(self.r.run())
        self.assertIn("success 2", summary)
        self.assertTrue(self.db.is_done("111", "GOOD1"))
        # expired code is marked dead after the first try, not tried for everyone
        self.assertEqual(self.db.list_codes(only_active=True)[0]["code"], "GOOD1")
        self.assertEqual(sum(1 for c in self.api.calls if c[:1] == ("redeem",) and c[2] == "OLDCODE"), 1)

        # second run should do nothing
        self.api.calls.clear()
        summary = run(self.r.run())
        self.assertEqual([c for c in self.api.calls if c[0] == "redeem"], [])
        self.assertIn("already has every", summary)

    def test_only_selected_players(self):
        run(self.r.add_player("111", "12", "t"))
        run(self.r.add_player("222", "12", "t"))
        self.r.add_code("GOOD1", "web")
        run(self.r.run(fids=["222"]))
        self.assertTrue(self.db.is_done("222", "GOOD1"))
        self.assertFalse(self.db.is_done("111", "GOOD1"))

    def test_paused_player_skipped(self):
        run(self.r.add_player("111", "12", "t"))
        self.db.set_player_enabled("111", False)
        self.r.add_code("GOOD1", "web")
        self.assertIn("Nothing to do", run(self.r.run()))

    def test_wrong_kingdom_pauses_player(self):
        run(self.r.add_player("111", "99", "t"))   # real kingdom is 12
        self.r.add_code("GOOD1", "web")
        run(self.r.run())
        self.assertFalse(self.db.get_player("111")["enabled"])
        self.assertEqual(self.db.list_codes(only_active=True)[0]["code"], "GOOD1")  # code still fine

    def test_salt_check(self):
        self.assertTrue(run(self.r.check_salt()))
        self.assertEqual(self.r.salt_status, "ok")


if __name__ == "__main__":
    unittest.main()
