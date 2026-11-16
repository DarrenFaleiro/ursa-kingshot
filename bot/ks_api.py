"""Client for the official Kingshot gift-code website (ks-giftcode.centurygame.com).

The website uses these endpoints (form-encoded POST):
  {base}/gift_code_config  time, sign              -> harmless, used to test the salt
  {base}/gift_code         fid, kid, cdk, time, sign -> redeems a code

Note: the old {base}/player "login" endpoint was removed by Century Games, so there is
no way to look up a player's name or kingdom any more. The kingdom (kid) must be supplied.

`sign` is md5( "k1=v1&k2=v2..." (keys sorted) + SALT ). You put the salt in .env (KS_SALT).
"""
import hashlib
import time
from dataclasses import dataclass

import aiohttp


def make_sign(params: dict, salt: str) -> str:
    query = "&".join(f"{k}={params[k]}" for k in sorted(params))
    return hashlib.md5((query + salt).encode()).hexdigest()


@dataclass
class Result:
    status: str      # see classify() below
    message: str
    data: dict | None = None


RATE_LIMIT_ERRS = (40002, 40019, 40101)


def classify(payload: dict) -> str:
    """Turn the website's reply into one of our simple statuses.

    success        - redeemed
    already        - player already has this code (counts as done)
    expired        - code is past its end date
    invalid        - code doesn't exist
    limit          - code has hit its claim limit
    wrong_kingdom  - player ID + kingdom don't match
    no_player      - player ID doesn't exist
    rate_limited   - too many requests, wait and retry
    retry          - temporary problem, try again later
    captcha        - the site now wants a captcha (can't be automated)
    sign_error     - wrong salt
    error          - anything else
    """
    msg = str(payload.get("msg", "")).upper()
    err = payload.get("err_code")

    if payload.get("code") == 0 and "SUCCESS" in msg:
        return "success"
    if "SIGN" in msg:
        return "sign_error"
    if "RECEIVED" in msg or "SAME TYPE EXCHANGE" in msg or err in (40008, 40011):
        return "already"
    if "TIME ERROR" in msg or "EXPIRED" in msg or err == 40007:
        return "expired"
    if "CDK NOT FOUND" in msg or err == 40014:
        return "invalid"
    if msg.startswith("USED") or err == 40005:
        return "limit"
    if err == 40020 or "USER INFO ERROR" in msg or "CHARACTER INFO" in msg:
        return "wrong_kingdom"
    if "ROLE NOT EXIST" in msg or err == 40001:
        return "no_player"
    if "TOO FREQUENT" in msg or err in RATE_LIMIT_ERRS:
        return "rate_limited"
    if "TIMEOUT" in msg or err == 40004:
        return "retry"
    if "CAPTCHA" in msg:
        return "captcha"
    return "error"


class KingshotAPI:
    def __init__(self, base_url: str, salt: str):
        self.base = base_url.rstrip("/")
        self.salt = salt
        self._session: aiohttp.ClientSession | None = None

    async def _session_get(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20),
                headers={
                    "Origin": "https://ks-giftcode.centurygame.com",
                    "Referer": "https://ks-giftcode.centurygame.com/",
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
                },
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _post(self, path: str, params: dict) -> Result:
        if not self.salt:
            return Result("sign_error", "KS_SALT is not set in .env")
        params = {**params, "time": int(time.time() * 1000)}
        params["sign"] = make_sign(params, self.salt)
        session = await self._session_get()
        try:
            async with session.post(f"{self.base}/{path}", data=params) as resp:
                if resp.status == 429:
                    return Result("rate_limited", "HTTP 429 - too many requests")
                if resp.status >= 400:
                    return Result("retry", f"HTTP {resp.status} on {self.base}/{path}")
                payload = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as e:
            return Result("retry", f"network error: {e}")
        except ValueError:
            return Result("error", "website did not return JSON")
        return Result(classify(payload), str(payload.get("msg", "")), payload.get("data"))

    async def check_salt(self) -> Result:
        """Safe test: touches no player. Returns status 'ok' if the salt signs correctly."""
        r = await self._post("gift_code_config", {})
        if r.status in ("sign_error", "retry", "rate_limited"):
            return r
        return Result("ok", r.message, r.data)

    async def redeem(self, fid: str, kid: int | str, code: str) -> Result:
        return await self._post("gift_code", {"fid": fid, "kid": kid, "cdk": code})
