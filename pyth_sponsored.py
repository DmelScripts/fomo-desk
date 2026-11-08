"""Read sponsored Solana Pyth Core price accounts. This module does not push a price.

The account address is the shard-0 PDA from the official receiver SDK:
seeds are the shard as a little-endian u16 and the 32-byte feed id, under the
Pyth Core push-oracle program. The sponsored-feed page publishes those
shard-0 addresses, and the derived addresses match them.

The account is 134 bytes, owned by the Pyth Core receiver, with discriminator
sha256("account:PriceUpdateV2")[:8]. VerificationLevel::Full is one byte, so
the price message starts at byte 41 and the last byte is the unused allocation
the SDK sizes for the two-byte Partial variant. Partial is recognized and
refused: get_price_no_older_than requires Full.

A usable quote also requires the feed id inside the account, a positive
price minus confidence, and publish_time within 30 seconds. The sponsored
heartbeat on these two feeds is 60 seconds. That window is not widened.
Hermes is not called.

https://docs.pyth.network/pro/use-prices-on-blockchain/solana
https://docs.pyth.network/pro/sponsored-feeds
https://docs.pyth.network/pro/contract-addresses
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from decimal import Decimal, ROUND_CEILING

import solana_tx
import usdc_valuation

DISC = hashlib.sha256(b"account:PriceUpdateV2").digest()[:8]
RECEIVER_PROGRAM_ID = "rec5EKMGg6MxZYaMdyBfgwp4d5rB9T1VQH5pJv5LtFJ"
PUSH_ORACLE_PROGRAM_ID = "pythWSnswVUd12oZpeFP8e9CVaEqJg25g1Vtc2biRsT"
ACCOUNT_LEN = 134
SOL_FEED = "ef0d8b6fda2ceba41da15d4095d1da392a0d2f8ed0c6c7bc0f4cfac8c280b56d"
USDC_FEED = usdc_valuation.FEED_ID
SOL_ACCOUNT = "7UVimffxr9ow1uXYxsr4LHAcV58mLzhmwaeKvJ1pjLiE"
USDC_ACCOUNT = "Dpw1EAVrSB1ibxiDQyTAW6Zip3J4Btk2x4SgApQCeFbX"
RPC = "https://api.mainnet-beta.solana.com"
_LAST = None


def account_for(feed_id: str) -> str:
    """Shard 0 of the Pyth Core push oracle. The same derivation the SDK uses."""
    derived, _bump = solana_tx.find_program_address(
        [(0).to_bytes(2, "little"), bytes.fromhex(feed_id)],
        solana_tx.b58decode(PUSH_ORACLE_PROGRAM_ID),
    )
    return solana_tx.b58encode(derived)


def _refused(reason: str, **extra) -> dict:
    body = {
        "ok": False,
        "usable": False,
        "configured": True,
        "valid": False,
        "fresh": False,
        "live": False,
        "fetched": False,
        "hermes_called": False,
        "pusher": False,
        "origin": "pyth_sponsored",
        "reason": reason,
        "authenticated_live": False,
    }
    body.update(extra)
    return body


def _other_feed(price: int, conf: int, expo: int, published: int, now: float) -> dict:
    """Same age and confidence rules as the USDC valuation, for a different feed id."""
    if conf < 0 or expo < -12 or expo > 0 or isinstance(now, bool) or not isinstance(now, (int, float)):
        return {"usable": False, "reason": "valuation_scale"}
    age = float(now) - published
    if age < -5 or age > usdc_valuation.MAX_AGE_S:
        return {"usable": False, "reason": "valuation_stale"}
    scale = Decimal(10) ** expo
    upper = (Decimal(price) + Decimal(conf)) * scale
    lower = (Decimal(price) - Decimal(conf)) * scale
    if lower <= 0 or upper <= 0:
        return {"usable": False, "reason": "valuation_not_positive"}
    return {"usable": True, "reason": None, "upper": format(upper, "f"), "lower": format(lower, "f")}


def parse_account(data: bytes, *, owner: str, address: str, feed_id: str, now: float) -> dict:
    """Parse one PriceUpdateV2. Stale, partial, and unbound accounts are not a price."""
    expected = account_for(feed_id)
    if address != expected:
        return _refused("account_binding", address=address, expected=expected)
    if owner != RECEIVER_PROGRAM_ID:
        return _refused("account_owner", owner=owner, program_id=RECEIVER_PROGRAM_ID)
    if not isinstance(data, (bytes, bytearray)) or len(data) != ACCOUNT_LEN or bytes(data[:8]) != DISC:
        return _refused("price_update_v2_layout")
    kind = data[40]
    if kind == 0:
        return _refused("verification_not_full", valid=False, verification="partial", num_signatures=data[41])
    if kind != 1 or data[133] != 0:
        return _refused("price_update_v2_layout")
    message = data[41:133]
    found = message[:32].hex()
    if found != feed_id:
        return _refused("feed_id", feed_id=found)
    price = int.from_bytes(message[32:40], "little", signed=True)
    conf = int.from_bytes(message[40:48], "little")
    expo = int.from_bytes(message[48:52], "little", signed=True)
    published = int.from_bytes(message[52:60], "little", signed=True)
    posted_slot = int.from_bytes(message[84:92], "little")
    quote = {
        "feed_id": feed_id,
        "price": price,
        "conf": conf,
        "expo": expo,
        "publish_time": published,
        "origin": "pyth_sponsored",
        "authenticated_live": False,
    }
    if feed_id == usdc_valuation.FEED_ID:
        assessed = usdc_valuation.assess(quote, now)
    else:
        assessed = _other_feed(price, conf, expo, published, now)
    fresh = assessed.get("usable") is True
    valid = fresh or assessed.get("reason") == "valuation_stale"
    upper = assessed.get("upper")
    return {
        "ok": fresh,
        "usable": fresh,
        "configured": True,
        "valid": valid,
        "fresh": fresh,
        "live": False,
        "fetched": True,
        "hermes_called": False,
        "pusher": False,
        "origin": "pyth_sponsored",
        "reason": None if fresh else assessed.get("reason"),
        "authenticated_live": False,
        "verification": "full",
        "feed_id": feed_id,
        "price": price,
        "conf": conf,
        "expo": expo,
        "publish_time": published,
        "posted_slot": posted_slot,
        "address": address,
        "owner": owner,
        "program_id": RECEIVER_PROGRAM_ID,
        "upper": upper,
        "lower": assessed.get("lower"),
        "age_s": None if not isinstance(now, (int, float)) else float(now) - published,
    }


def lamports_usd(lamports: int, leg: dict) -> Decimal | None:
    """SOL/USD ceiling for a lamport amount. The pool price is not this rate."""
    if isinstance(lamports, bool) or not isinstance(lamports, int) or lamports < 0 or not isinstance(leg, dict):
        return None
    if leg.get("fresh") is not True or leg.get("used") is not True:
        return None
    price = leg.get("price")
    conf = leg.get("conf")
    expo = leg.get("expo")
    if isinstance(price, bool) or isinstance(conf, bool) or isinstance(expo, bool):
        return None
    if not isinstance(price, int) or not isinstance(conf, int) or not isinstance(expo, int):
        return None
    if conf < 0 or expo < -12 or expo > 0:
        return None
    ceiling = (Decimal(price) + Decimal(conf)) * (Decimal(10) ** expo)
    if ceiling <= 0:
        return None
    return (Decimal(lamports) / Decimal(1_000_000_000) * ceiling).quantize(Decimal("0.01"), rounding=ROUND_CEILING)


def status(now: float | None = None) -> dict:
    """Recompute age from the observation clock. A stored fresh flag does not stay fresh."""
    moment = time.time() if now is None else now
    last = _LAST if isinstance(_LAST, dict) else {}
    fetched = last.get("fetched") is True
    observed = last.get("observed_at")
    published = last.get("publish_time")
    observation_age = None if not isinstance(observed, (int, float)) else float(moment) - float(observed)
    price_age = None if not isinstance(published, int) else float(moment) - published
    fresh = (
        fetched
        and last.get("valid") is True
        and last.get("hard_reason") is None
        and isinstance(price_age, float)
        and -5 <= price_age <= usdc_valuation.MAX_AGE_S
    )
    usable = fresh and isinstance(observation_age, float) and 0 <= observation_age <= usdc_valuation.MAX_AGE_S
    stale_observation = isinstance(observation_age, float) and observation_age > usdc_valuation.MAX_AGE_S
    if not fetched:
        reason = "not_read"
    elif last.get("hard_reason"):
        reason = last["hard_reason"]
    elif observed is None:
        reason = last.get("reason") or "rpc_status"
    elif not fresh:
        reason = "valuation_stale"
    else:
        reason = None
    return {
        "available": usable,
        "configured": True,
        "valid": last.get("valid") is True,
        "fresh": fresh,
        "usable": usable,
        "stale_observation": stale_observation,
        "live": False,
        "reason": reason,
        "live_provider": False,
        "fetched": fetched,
        "valuation": "sol_usd",
        "usdc_used": False,
        "account": SOL_ACCOUNT,
        "sol_account": SOL_ACCOUNT,
        "usdc_account": USDC_ACCOUNT,
        "program_id": RECEIVER_PROGRAM_ID,
        "push_oracle_program_id": PUSH_ORACLE_PROGRAM_ID,
        "feed_id": SOL_FEED,
        "sol_feed_id": SOL_FEED,
        "usdc_feed_id": USDC_FEED,
        "publish_time": published,
        "observed_at": observed,
        "age_s": price_age,
        "observation_age_s": observation_age,
        "slot": last.get("slot"),
        "freshness_window_s": usdc_valuation.MAX_AGE_S,
        "sponsored_heartbeat": "60 seconds / 0.5 percent on SOL/USD; not widened",
        "hermes_called": False,
        "pusher": False,
        "rpc": RPC,
    }


def read_accounts(transport, now: float) -> dict:
    """Read the two sponsored accounts through the given transport. No Hermes call."""
    global _LAST
    request = {
        "kind": "rpc",
        "method": "getMultipleAccounts",
        "url": RPC,
        "accounts": [SOL_ACCOUNT, USDC_ACCOUNT],
        "commitment": "confirmed",
    }
    try:
        response = transport(request) if transport is not None else None
    except Exception:
        result = _refused("rpc_transport", fetched=False)
        _LAST = result
        return result
    if not isinstance(response, dict) or response.get("status") != 200 or not isinstance(response.get("body"), dict):
        result = _refused("rpc_status", fetched=True)
        _LAST = result
        return result
    body = response["body"]
    value = (body.get("result") or {}).get("value") if isinstance(body.get("result"), dict) else None
    context = (body.get("result") or {}).get("context") if isinstance(body.get("result"), dict) else None
    if not isinstance(value, list) or len(value) != 2:
        result = _refused("rpc_shape", fetched=True)
        _LAST = result
        return result
    import base64
    parsed = []
    for address, feed, item in ((SOL_ACCOUNT, SOL_FEED, value[0]), (USDC_ACCOUNT, USDC_FEED, value[1])):
        if not isinstance(item, dict) or not isinstance(item.get("data"), list):
            parsed.append(_refused("account_missing", address=address))
            continue
        try:
            raw = base64.b64decode(item["data"][0], validate=True)
        except Exception:
            parsed.append(_refused("account_encoding", address=address))
            continue
        parsed.append(parse_account(raw, owner=item.get("owner"), address=address, feed_id=feed, now=now))
    slot = context.get("slot") if isinstance(context, dict) else None
    sol = _bind_posted_slot(parsed[0], slot)
    usdc = _bind_posted_slot(parsed[1], slot)
    sol["used"] = True
    usdc["used"] = False
    fresh = sol.get("fresh") is True and sol.get("reason") is None
    result = {
        "ok": fresh,
        "configured": True,
        "valid": sol.get("valid") is True,
        "fresh": fresh,
        "live": False,
        "fetched": True,
        "hermes_called": False,
        "pusher": False,
        "sol_used": True,
        "usdc_used": False,
        "reason": None if fresh else sol.get("reason"),
        "slot": slot if isinstance(slot, int) and not isinstance(slot, bool) else None,
        "publish_time": sol.get("publish_time"),
        "observed_at": float(now),
        "age_s": None if sol.get("publish_time") is None else float(now) - sol["publish_time"],
        "usdc": {key: usdc.get(key) for key in ("valid", "fresh", "used", "reason", "price", "conf", "expo", "publish_time", "posted_slot", "age_s", "upper")},
        "sol": {key: sol.get(key) for key in ("valid", "fresh", "used", "reason", "price", "conf", "expo", "publish_time", "posted_slot", "age_s", "upper")},
        "rpc": RPC,
    }
    _LAST = {
        "fetched": True,
        "observed_at": float(now),
        "publish_time": sol.get("publish_time") if fresh or sol.get("valid") else None,
        "valid": sol.get("valid") is True and sol.get("reason") != "posted_slot_future",
        "hard_reason": sol.get("reason") if sol.get("reason") in {"posted_slot_future", "slot_unproven"} else None,
        "slot": result["slot"],
    }
    return result


def _bind_posted_slot(leg: dict, slot) -> dict:
    """A posted slot ahead of the confirmed context is not that read. A slot behind it can be."""
    if isinstance(slot, bool) or not isinstance(slot, int):
        return leg | {"fresh": False, "usable": False, "ok": False, "reason": "slot_unproven"}
    posted = leg.get("posted_slot")
    if isinstance(posted, int) and not isinstance(posted, bool) and posted > slot:
        return leg | {"fresh": False, "usable": False, "ok": False, "reason": "posted_slot_future"}
    return leg


def public_fetch(now: float) -> dict:
    """One confirmed getMultipleAccounts of the two pinned accounts. No redirect."""

    def transport(request: dict) -> dict:
        if request.get("url") != RPC or request.get("method") != "getMultipleAccounts":
            raise RuntimeError("rpc_refused")
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getMultipleAccounts",
            "params": [request["accounts"], {"encoding": "base64", "commitment": "confirmed"}],
        }

        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(urllib.request.Request(RPC, data=json.dumps(payload).encode(), headers={"content-type": "application/json"}), timeout=8) as response:
                return {"status": response.status, "body": json.loads(response.read().decode())}
        except urllib.error.HTTPError as exc:
            return {"status": exc.code, "body": None}
    return read_accounts(transport, now)
