"""Six-stage memecoin desk. Fixtures are not a live provider and nothing is sent."""
import json
import os
import threading
import urllib.request
from pathlib import Path
import time
import unittest
from decimal import Decimal

import cross_source
import db
import memecoin_desk
import phantom_transport
import raydium_cpmm
import solana_tx
import solana_whirlpool
import token_execution
from fastapi.testclient import TestClient
from tests.support import arm_clean_creator, arm_holder_chart, clear_filter_fixtures, temp_desk_env

TOKEN = "dummy-app-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
NOW = 1_800_000_000.0


def _mint(number: int) -> str:
    return solana_tx.b58encode(bytes([number]) * 32)


def _amm_route(mint: str, amount: int, side: str):
    """Build a route whose minimum is the pool estimate, inside the 50 bps ceiling."""
    import jupiter_route_v2
    from tests.test_jupiter_route_v2 import WHEN, _metas, _pool, _prices, _route, _snap, _tx
    if side == "buy":
        values, accounts = _pool(solana_tx.NATIVE_MINT, mint)
        metas = _metas(solana_tx.NATIVE_MINT, mint, values)
    else:
        values, accounts = _pool(mint, solana_tx.NATIVE_MINT)
        metas = _metas(mint, solana_tx.NATIVE_MINT, values)
    probe = jupiter_route_v2.decode_message(
        _tx(metas, _route(1, 0, True, amount=amount), side=side),
        _snap(accounts, WHEN), side=side, mint=mint, now=WHEN, price_transport=_prices(),
    )
    est = probe.get("amm_est_out")
    if not isinstance(est, int):
        raise AssertionError(probe.get("reason"))
    return _route(est, 0, True, amount=amount), est


def _pair(mint: str, **over):
    body = {
        "mint": mint,
        "source": "dexscreener",
        "fetched_at": NOW,
        "pair_created_at": int((NOW - 3600) * 1000),
        "market_cap_usd": "1000000",
        "market_cap_basis": "marketCap",
        "pair_address": cross_source.pair_id_for(mint),
        "liquidity_usd": "250000",
        "price_usd": "1",
        "price_change_h1": "10",
    }
    body.update(over)
    if "pair_address" not in over:
        body["pair_address"] = cross_source.pair_id_for(mint)
    cross_source.note_cap(mint, body.get("market_cap_usd"))
    return body


def _bind_exit(case_id: str) -> dict:
    import base64
    import hashlib
    from tests.test_jupiter_route_v2 import _metas, _pool, _prices, _route, _tx, WHEN
    import phantom_transport
    import solana_inspect
    mint = memecoin_desk.view(case_id)["mint"]
    values, accounts = _pool(mint, solana_tx.NATIVE_MINT)
    tx = _tx(_metas(mint, solana_tx.NATIVE_MINT, values), _route(1_000, 0, True, amount=50), side="exit")
    decoded = solana_tx.decode_transaction(base64.b64decode(tx))
    width = len(decoded.static_keys)
    pre = [0] * width
    post = [0] * width
    pre[0] = 5_000_000
    post[0] = 5_100_000
    source = values["source"]
    owner, raw = accounts[values["pool"]]
    wallet = phantom_transport.REQUIRED_WALLET
    signature = solana_tx.b58encode(hashlib.sha256(mint.encode()).digest() + hashlib.sha256(b"exit" + mint.encode()).digest())
    body = {
        "signature": signature,
        "slot": 466835746,
        "genesis": solana_inspect.MAINNET_GENESIS,
        "transaction": tx,
        "meta": {
            "err": None,
            "fee": 5000,
            "preBalances": pre,
            "postBalances": post,
            "preTokenBalances": [{"accountIndex": decoded.static_keys.index(source), "mint": mint, "owner": wallet, "uiTokenAmount": {"amount": "50"}}],
            "postTokenBalances": [{"accountIndex": decoded.static_keys.index(source), "mint": mint, "owner": wallet, "uiTokenAmount": {"amount": "0"}}],
        },
        "accounts": {values["pool"]: {"owner": owner, "data": base64.b64encode(raw).decode()}},
    }
    return memecoin_desk.record_exit_receipt(case_id, body, _prices(), WHEN)


def _passing_cross(mint: str, quote="300000", dex_cap="1000000", tracker_cap="1000000"):
    """Quote-side liquidity and two confirmed caps. DexScreener liquidity.usd is noise."""
    pool = cross_source.pair_id_for(mint)
    return {
        "mint": mint,
        "pair_id": pool,
        "dex": {"pairs": [{
            "pairAddress": pool,
            "baseToken": {"address": mint},
            "quoteToken": {"address": solana_tx.NATIVE_MINT, "symbol": "LOOKALIKE"},
            "marketCap": dex_cap,
            "liquidity": {"usd": "999999999"},
        }]},
        "rugcheck": {
            "creator": "SameCreator",
            "creatorBalance": 0,
            "token": {"supply": 1000},
            "mintAuthority": None,
            "freezeAuthority": None,
            "risks": [],
            "creatorTokens": [{"mint": mint}],
            "markets": [{"pubkey": pool, "lp": {"quoteUSD": quote, "lpLockedPct": 100}}],
        },
        "tracker": {
            "holders": 10,
            "token": {"creation": {"creator": "SameCreator"}},
            "risk": {"dev": {"percentage": 0}, "risks": []},
            "pools": [{
                "poolId": pool,
                "lpBurn": 100,
                "security": {"mintAuthority": None, "freezeAuthority": None},
                "liquidity": {"quote": quote, "usd": 1},
                "marketCap": {"quote": tracker_cap, "usd": tracker_cap},
            }],
        },
        "chart": {"holders": [{"holders": 2, "time": 1}, {"holders": 4, "time": 2}]},
        "das": {"result": {"total": 1, "items": [{"id": mint}]}},
        "deployer": {"total": 1, "data": [{"mint": mint}]},
    }


def _onchain(**over):
    body = {
        "source": "stalkchain_token_onchain",
        "fetched_at": NOW,
        "mint_authority_revoked": True,
        "freeze_authority_revoked": True,
        "lp_burned_or_locked": True,
        "lpLockedPct": 100,
        "dev_holding_pct": "3",
        "dev_rugs": {"status": "checked", "source": "stalkchain_token_deployer", "rugs": 0, "tokens_seen": 2},
        "bundled_launch_pct": "10",
    }
    body.update(over)
    return body


def _components(value="70", **over):
    body = {
        name: {"value": value, "source": "holder_reader", "fetched_at": NOW}
        for name in memecoin_desk.COMPONENTS
    }
    body["holder_growth"] = {"source": "solana_tracker_holders_chart", "fetched_at": NOW}
    body.update(over)
    return body


def _arm_recorded_exit(case_id: str) -> None:
    """Give a confirmed fill the intent row reserve_exit now requires. Snapshot flags are not enough."""
    rows = db.query(
        "SELECT signature, qty, cost_basis_lamports, pool FROM memecoin_entry_receipts WHERE case_id = ? AND failed = 0 AND ledger = 'live'",
        (case_id,),
    )
    if not rows or not isinstance(rows[0]["pool"], str) or db.query("SELECT id FROM memecoin_risk_intents WHERE case_id = ?", (case_id,)):
        return
    snap = memecoin_desk.view(case_id)["snapshot"]
    db.execute(
        """
        INSERT INTO memecoin_risk_intents (
            id, case_id, ledger, ledger_provenance, entry_signature, action, pool, slot, mark_price_usd,
            entry_price_usd, stop_price_usd, qty, basis_cost_lamports, basis_decimals,
            evidence_json, created_at
        ) VALUES (?, ?, 'live', 'server', ?, 'stop', ?, 1, '1', ?, '0.94', ?, ?, 6, '{}', ?)
        """,
        (
            "intent-" + case_id, case_id, rows[0]["signature"], rows[0]["pool"],
            snap.get("entry_price_usd") or "1", rows[0]["qty"], rows[0]["cost_basis_lamports"], NOW,
        ),
    )


def _protect(case_id: str, ledger: str | None = None):
    pool = solana_whirlpool.reference_pool(
        solana_tx.NATIVE_MINT, solana_tx.b58encode(b"\x11" * 32),
        solana_tx.USDC_MINT, solana_tx.b58encode(b"\x12" * 32),
        sqrt_price=5531210526019943285,
    )
    quote = {"usable": True, "upper": "1", "lower": "1", "origin": "test_harness"}
    return memecoin_desk.protect(case_id, pool, quote, ledger=ledger)


def _buy(mint: str, amount: int = 1000, minimum: int = 50) -> str:
    keys = [
        phantom_transport.REQUIRED_WALLET,
        solana_tx.b58encode(b"\x11" * 32),
        solana_tx.b58encode(b"\x12" * 32),
        solana_tx.b58encode(b"\x13" * 32),
        solana_tx.b58encode(b"\x14" * 32),
        solana_tx.b58encode(b"\x15" * 32),
        solana_tx.b58encode(b"\x16" * 32),
        solana_tx.b58encode(b"\x21" * 32),
        solana_tx.b58encode(b"\x22" * 32),
        solana_tx.b58encode(b"\x23" * 32),
        solana_tx.b58encode(b"\x24" * 32),
        raydium_cpmm.WSOL,
        mint,
        raydium_cpmm.PROGRAM_ID,
    ]
    accounts = [0, 7, 8, 1, 2, 3, 4, 5, 9, 10, 11, 12, 6]
    data = raydium_cpmm.DISC + amount.to_bytes(8, "little") + minimum.to_bytes(8, "little")
    return solana_tx.encode_legacy(keys[0], keys, 7, [(13, accounts, data)], solana_tx.b58encode(b"\x44" * 32))


class MemecoinDeskTests(unittest.TestCase):
    def setUp(self):
        _tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        os.environ["APP_TOKEN"] = TOKEN
        self.addCleanup(lambda: os.environ.pop("APP_TOKEN", None))
        db.init_db()
        db.execute("UPDATE phantom_control SET value = '0' WHERE key = 'halt'")
        cross_source.arm_test_fetcher()
        self.addCleanup(cross_source.clear_fetcher)
        arm_holder_chart()
        self.addCleanup(clear_filter_fixtures)
        db.set_setting("agents_enabled", "0", "test")
        original = memecoin_desk.reserve_exit

        def reserve_exit(case_id, *args, **kwargs):
            _arm_recorded_exit(case_id)
            return original(case_id, *args, **kwargs)

        memecoin_desk.reserve_exit = reserve_exit
        self.addCleanup(lambda: setattr(memecoin_desk, "reserve_exit", original))

    def _open(self, number: int, **pair):
        found = memecoin_desk.scout(_pair(_mint(number), **pair), now=NOW)
        self.assertTrue(found["ok"], found)
        return found["case_id"]

    def _filtered(self, number: int, **pair):
        case_id = self._open(number, **pair)
        arm_clean_creator(_mint(number))
        result = memecoin_desk.filter_case(case_id, _onchain(cross_sources=_passing_cross(_mint(number))), now=NOW)
        self.assertTrue(result["ok"], result)
        return case_id

    def _analyzed(self, number: int, **pair):
        case_id = self._filtered(number, **pair)
        snap = json.loads(db.query("SELECT snapshot_json FROM memecoin_cases WHERE id = ?", (case_id,))[0]["snapshot_json"])
        snap["fixture"] = True
        snap["analyst_scored"] = False
        snap["smart_wallets_scored"] = False
        db.execute(
            "UPDATE memecoin_cases SET stage = 'analyst_passed', snapshot_json = ? WHERE id = ? AND stage = 'filter_passed'",
            (json.dumps(snap, sort_keys=True), case_id),
        )
        return case_id

    def _filled(self, number: int):
        case_id = self._analyzed(number)
        protected = _protect(case_id, "paper")
        self.assertEqual(protected["stage"], "protected", protected)
        tx = _buy(_mint(number))
        offered = memecoin_desk.offer_fixture_route(case_id, tx, amount_in=1000, minimum_out=50, fee_usd="0.05")
        self.assertTrue(offered["ok"], offered)
        digest = solana_tx.message_bytes(tx)
        import hashlib
        filled = memecoin_desk.harness_fill(case_id, {"message_sha256": hashlib.sha256(digest).hexdigest(), "tokens_out": 50})
        self.assertEqual(filled["stage"], "filled", filled)
        return case_id

    def test_age_and_market_cap_bounds_use_market_cap_not_fdv(self):
        self.assertTrue(self._open(2, pair_created_at=int((NOW - 30 * 60) * 1000)))
        self.assertEqual(memecoin_desk.scout(_pair(_mint(3), pair_created_at=int((NOW - 30 * 60 + 1) * 1000)), now=NOW)["reason"], "age")
        self.assertTrue(self._open(4, pair_created_at=int((NOW - 72 * 3600) * 1000)))
        self.assertEqual(memecoin_desk.scout(_pair(_mint(5), pair_created_at=int((NOW - 72 * 3600 - 1) * 1000)), now=NOW)["reason"], "age")
        self.assertEqual(memecoin_desk.scout(_pair(_mint(6), pair_created_at=int(NOW - 3600)), now=NOW)["reason"], "pair_created_at")
        self.assertTrue(self._open(7, market_cap_usd="300000", liquidity_usd="75000"))
        self.assertEqual(memecoin_desk.scout(_pair(_mint(8), market_cap_usd="299999"), now=NOW)["reason"], "market_cap")
        self.assertTrue(self._open(9, market_cap_usd="20000000", liquidity_usd="5000000"))
        self.assertEqual(memecoin_desk.scout(_pair(_mint(10), market_cap_usd="20000001"), now=NOW)["reason"], "market_cap")
        missing = _pair(_mint(11))
        missing["market_cap_usd"] = None
        missing["fdv"] = "5000000"
        self.assertEqual(memecoin_desk.scout(missing, now=NOW)["reason"], "market_cap_missing")
        basis = _pair(_mint(12), market_cap_basis="fdv")
        self.assertEqual(memecoin_desk.scout(basis, now=NOW)["reason"], "market_cap_basis")
        for bad in ("0", "NaN", "Infinity", "-1"):
            self.assertEqual(memecoin_desk.scout(_pair(_mint(13), market_cap_usd=bad), now=NOW)["reason"], "market_cap")
            self.assertEqual(memecoin_desk.scout(_pair(_mint(13), liquidity_usd=bad), now=NOW)["reason"], "liquidity")
        stale = memecoin_desk.scout(_pair(_mint(14), fetched_at=NOW - 31), now=NOW)
        self.assertEqual(stale["reason"], "snapshot_stale")
        unconfirmed = _pair(_mint(47))
        unconfirmed["confirmed_market_cap_usd"] = unconfirmed["market_cap_usd"]
        cross_source.clear_fetcher()
        self.assertEqual(memecoin_desk.scout(unconfirmed, now=NOW)["reason"], "market_cap_unconfirmed")
        cross_source.arm_test_fetcher()
        self.assertEqual(memecoin_desk.scout(_pair(_mint(48), market_cap_basis=None), now=NOW)["reason"], "market_cap_basis")
        self.assertTrue(any(row["reason"] == "market_cap_missing" for row in db.query("SELECT reason FROM memecoin_log")))

    def test_liquidity_ratio_uses_quote_side_not_dexscreener(self):
        exact = self._open(15, market_cap_usd="1000000", liquidity_usd="1")
        arm_clean_creator(_mint(15))
        self.assertTrue(memecoin_desk.filter_case(exact, _onchain(cross_sources=_passing_cross(_mint(15))), now=NOW)["ok"])
        missing = self._open(16, market_cap_usd="1000000", liquidity_usd="250000")
        arm_clean_creator(_mint(16))
        cross_source.clear_fetcher()
        self.assertEqual(
            memecoin_desk.filter_case(missing, _onchain(cross_sources=_passing_cross(_mint(16))), now=NOW)["reason"],
            "liquidity_source_missing",
        )
        cross_source.arm_test_fetcher()
        short = self._open(46, market_cap_usd="1000000", liquidity_usd="999999")
        arm_clean_creator(_mint(46))
        cross_source.note_bundle(_mint(46), _passing_cross(_mint(46), quote="26", dex_cap="100", tracker_cap="300"))
        refused = memecoin_desk.filter_case(short, _onchain(cross_sources=_passing_cross(_mint(46))), now=NOW)
        self.assertEqual(refused["reason"], "liquidity_below_25pct_mcap")
        self.assertEqual(memecoin_desk.view(short)["stage"], "rejected")
        dexscreener = self._open(17)
        self.assertEqual(memecoin_desk.filter_case(dexscreener, _onchain(source="dexscreener"), now=NOW)["reason"], "onchain_source")

    def test_filter_rejects_missing_or_failed_onchain_facts(self):
        self.assertEqual(memecoin_desk.filter_case(self._open(18), _onchain(mint_authority_revoked=False), now=NOW)["reason"], "mint_authority")
        self.assertEqual(memecoin_desk.filter_case(self._open(19), _onchain(freeze_authority_revoked=False), now=NOW)["reason"], "freeze_authority")
        self.assertEqual(memecoin_desk.filter_case(self._open(20), _onchain(lpLockedPct=99), now=NOW)["reason"], "lp")
        self.assertEqual(memecoin_desk.filter_case(self._open(40), _onchain(lpLockedPct=True), now=NOW)["reason"], "lp")
        missing_lp = _onchain()
        missing_lp.pop("lpLockedPct")
        self.assertEqual(memecoin_desk.filter_case(self._open(41), missing_lp, now=NOW)["reason"], "lp")
        self.assertEqual(memecoin_desk.filter_case(self._open(21), _onchain(dev_holding_pct="3.01"), now=NOW)["reason"], "dev_holding")
        arm_clean_creator(_mint(22))
        self.assertTrue(memecoin_desk.filter_case(self._open(22), _onchain(dev_holding_pct="3", cross_sources=_passing_cross(_mint(22))), now=NOW)["ok"])
        supplied = _onchain(dev_rugs={"status": "checked", "source": "stalkchain_token_deployer", "rugs": 0, "tokens_seen": 2})
        self.assertEqual(memecoin_desk.filter_case(self._open(23), supplied, now=NOW)["reason"], "dev_rug_history_unverified")
        self.assertEqual(memecoin_desk.filter_case(self._open(24), _onchain(dev_rugs={"status": "checked", "source": "stalkchain_token_deployer", "rugs": 1, "tokens_seen": 4}), now=NOW)["reason"], "dev_rug_history_unverified")
        arm_clean_creator(_mint(25))
        self.assertEqual(memecoin_desk.filter_case(self._open(25), _onchain(bundled_launch_pct="10.01"), now=NOW)["reason"], "bundle")
        arm_clean_creator(_mint(26))
        self.assertTrue(memecoin_desk.filter_case(self._open(26), _onchain(bundled_launch_pct="10", cross_sources=_passing_cross(_mint(26))), now=NOW)["ok"])
        pumped = self._open(27, price_change_h1="80")
        arm_clean_creator(_mint(27))
        self.assertEqual(memecoin_desk.filter_case(pumped, _onchain(), now=NOW)["reason"], "already_up_1h")
        arm_clean_creator(_mint(28))
        self.assertTrue(memecoin_desk.filter_case(self._open(28, price_change_h1="79"), _onchain(cross_sources=_passing_cross(_mint(28))), now=NOW)["ok"])

    def test_rug_reader_and_holder_chart_fail_closed(self):
        import dev_rug_history
        import public_filter_docs
        self.addCleanup(dev_rug_history.reset_source)
        dev_rug_history.reset_source()
        self.assertEqual(
            memecoin_desk.filter_case(self._open(42), _onchain(dev_rugs=None), now=NOW)["reason"],
            "dev_rug_history_unverified",
        )

        other = _mint(49)
        dev_rug_history.remember(_mint(43), {
            "id": _mint(43),
            "creators": [{"address": "CreatorForNestedRug", "verified": True, "share": 100}],
            "authorities": [],
        }, {
            "result": {"total": 2, "page": 1, "limit": 20, "items": [
                {"id": _mint(43), "interface": "FungibleToken"},
                {"id": other, "interface": "FungibleToken", "rugcheck": {"rugged": False}},
            ]},
        })
        dev_rug_history.remember_report(other, {"rugged": True, "creator": "CreatorForNestedRug"})
        rugged = memecoin_desk.filter_case(self._open(43), _onchain(dev_rugs={"status": "checked", "source": "stalkchain_token_deployer", "rugs": 0, "tokens_seen": 1}), now=NOW)
        self.assertEqual(rugged["reason"], "dev_rug_history")

        def broken(_mint):
            raise RuntimeError("source down")

        dev_rug_history.set_source(broken)
        self.assertEqual(
            memecoin_desk.filter_case(self._open(44), _onchain(dev_rugs={"status": "checked", "source": "test_reader", "rugs": 0, "tokens_seen": 1}), now=NOW)["reason"],
            "dev_rug_history_unverified",
        )
        dev_rug_history.reset_source()

        os.environ.pop("SOLANA_TRACKER_API_KEY", None)
        calls = []

        def refuse_network(*_args, **_kwargs):
            calls.append("network")
            raise AssertionError("holder chart must not open a socket")

        original = urllib.request.urlopen
        urllib.request.urlopen = refuse_network
        self.addCleanup(lambda: setattr(urllib.request, "urlopen", original))
        unknown = public_filter_docs.read_holder_chart(_mint(45), allow_network=True)
        self.assertEqual(unknown["reason"], "holder_growth_unknown")
        self.assertFalse(unknown["fetched"])
        self.assertEqual(calls, [])
        case_id = self._filtered(45)
        missing = _components()
        self.assertEqual(memecoin_desk.analyze(case_id, missing, now=NOW)["reason"], "holder_growth_unknown")
        bad = self._filtered(46)
        refused = _components()
        refused["holder_growth"] = {"source": "solana_tracker_holders_chart", "fetched_at": NOW, "chart": {"holders": [{"holders": 10, "time": 1}, {"holders": 12, "time": 2}]}, "value": "99"}
        self.assertEqual(memecoin_desk.analyze(bad, refused, now=NOW)["reason"], "holder_growth_unknown")
        os.environ[public_filter_docs.KEY_ENV] = "test-tracker-key"
        good = self._filtered(47)
        measured = _components()
        measured["holder_growth"]["value"] = "1"
        scored = memecoin_desk.analyze(good, measured, now=NOW)
        self.assertEqual(scored["reason"], "score", scored)
        self.assertEqual(calls, [])

    def test_score_boundaries_and_missing_provenance(self):
        low = self._filtered(29)
        scored = memecoin_desk.analyze(low, _components(momentum={"value": "65", "source": "holder_reader", "fetched_at": NOW}), now=NOW)
        self.assertEqual(scored["reason"], "score")
        passed = memecoin_desk.analyze(self._filtered(30), _components(), now=NOW)
        self.assertEqual(passed["reason"], "score", passed)
        for offset, name in enumerate(name for name in memecoin_desk.COMPONENTS if name != "smart_wallets"):
            incomplete = _components()
            del incomplete[name]
            self.assertEqual(memecoin_desk.analyze(self._filtered(80 + offset), incomplete, now=NOW)["reason"], name + "_missing")
        stale = _components()
        stale["holder_growth"] = {"value": "70", "source": "holder_reader", "fetched_at": NOW - 31}
        self.assertEqual(memecoin_desk.analyze(self._filtered(32), stale, now=NOW)["reason"], "holder_growth_unknown")
        counts = _components()
        counts["unique_buyers"] = {"value": "70", "source": "dexscreener_txns", "fetched_at": NOW}
        self.assertEqual(memecoin_desk.analyze(self._filtered(33), counts, now=NOW)["reason"], "unique_buyers_unproven")
        boost = _components()
        boost["smart_wallets"] = {"value": "100", "source": "dexscreener_boost", "fetched_at": NOW}
        ignored = self._filtered(34)
        self.assertEqual(memecoin_desk.analyze(ignored, boost, now=NOW)["reason"], "score")
        stored = json.loads(db.query(
            "SELECT evidence_json FROM memecoin_log WHERE case_id = ? AND reason = 'score' ORDER BY id DESC LIMIT 1",
            (ignored,),
        )[0]["evidence_json"])
        self.assertEqual(stored["smart_wallets"]["value"], "0")
        self.assertEqual(stored["smart_wallets_source"], "none_scored_zero")
        skipped = self._open(35)
        self.assertEqual(memecoin_desk.analyze(skipped, _components(), now=NOW)["reason"], "stage")
        self.assertEqual(memecoin_desk.view(skipped)["stage"], "scout_passed")

    def test_stop_and_trail_are_persisted_before_entry_and_are_not_a_profit_lock(self):
        case_id = self._analyzed(36)
        early = memecoin_desk.offer_fixture_route(case_id, _buy(_mint(36)), amount_in=1000, minimum_out=50, fee_usd="0.05")
        self.assertEqual(early["reason"], "stage")
        protected = _protect(case_id)
        self.assertEqual(protected["stage"], "protected")
        snap = memecoin_desk.view(case_id)["snapshot"]
        self.assertEqual(Decimal(snap["stop_price_usd"]), Decimal("0.94"))
        self.assertEqual(Decimal(snap["trail_arm_price_usd"]), Decimal("1.12"))
        self.assertEqual(Decimal(snap["trail_floor_if_armed_at_threshold"]), Decimal("0.9856"))
        self.assertIs(snap["trail_breakeven_guaranteed"], False)
        self.assertFalse(snap["send"])
        self.assertFalse(snap["live_ready"])
        priced = memecoin_desk.offer_fixture_route(case_id, _buy(_mint(36)), amount_in=1000, minimum_out=50, fee_usd="0.0500001")
        self.assertEqual(priced["reason"], "fee_cap")
        case_id = self._analyzed(37)
        _protect(case_id)
        missing = memecoin_desk.offer_fixture_route(case_id, _buy(_mint(37)), amount_in=1000, minimum_out=50, fee_usd="NaN")
        self.assertEqual(missing["reason"], "fee_unpriced")
        case_id = self._filled(38)
        held = memecoin_desk.mark(case_id, "1.119", source="fixture_price", fetched_at=NOW, now=NOW)
        self.assertEqual(held["action"], "hold")
        self.assertFalse(memecoin_desk.view(case_id)["snapshot"]["trail_armed"])
        armed = memecoin_desk.mark(case_id, "1.12", source="fixture_price", fetched_at=NOW, now=NOW)
        self.assertEqual(armed["action"], "hold")
        self.assertTrue(memecoin_desk.view(case_id)["snapshot"]["trail_armed"])
        exited = memecoin_desk.mark(case_id, "0.9856", source="fixture_price", fetched_at=NOW, now=NOW)
        self.assertEqual(exited["action"], "trail")
        closed = memecoin_desk.view(case_id)
        self.assertEqual(closed["stage"], "paper_exited")
        self.assertFalse(closed["snapshot"]["live_ledger"])
        self.assertIsNone(closed["snapshot"]["paper_exit"]["receipt_proceeds"])
        self.assertNotIn("realized_pnl", closed["snapshot"])
        self.assertFalse(closed["snapshot"]["trail_breakeven_guaranteed"])
        self.assertEqual(db.query("SELECT intent_id FROM phantom_reservations"), [])

    def test_stop_exit_still_runs_when_loss_is_large_and_halt_blocks(self):
        live = self._filled(44)
        db.execute("UPDATE phantom_control SET value = '1' WHERE key = 'halt'")
        halted = memecoin_desk.mark(live, "0.50", source="fixture_price", fetched_at=NOW, now=NOW)
        self.assertEqual(halted["reason"], "kill_switch")
        self.assertEqual(memecoin_desk.view(live)["stage"], "filled")
        db.execute("UPDATE phantom_control SET value = '0' WHERE key = 'halt'")
        for number in (40, 41, 42):
            case_id = self._filled(number)
            stopped = memecoin_desk.mark(case_id, "0.01", source="fixture_price", fetched_at=NOW, now=NOW)
            self.assertEqual(stopped["action"], "stop")
            self.assertEqual(memecoin_desk.view(case_id)["stage"], "paper_exited")
            self.assertNotIn("realized_pnl", memecoin_desk.view(case_id)["snapshot"])
        blocked = self._analyzed(43)
        self.assertNotEqual(_protect(blocked)["reason"], "stop_new_entries")
        self.assertEqual(memecoin_desk.loss_totals()["live_loss_usd"], "0.00")
        self.assertGreater(Decimal(memecoin_desk.loss_totals()["paper_loss_usd"]), Decimal("0"))
        self.assertFalse(memecoin_desk.loss_totals()["live_includes_paper"])
        stopped = memecoin_desk.mark(live, "0.94", source="fixture_price", fetched_at=NOW, now=NOW)
        self.assertEqual(stopped["action"], "stop")
        self.assertFalse(memecoin_desk.view(live)["snapshot"]["live_ledger"])
        self.assertLess(Decimal(memecoin_desk.view(live)["snapshot"]["paper_exit"]["paper_realized_pnl"]), Decimal("0"))
        self.assertFalse(memecoin_desk.view(live)["snapshot"]["paper_exit"]["counts_toward_live_loss"])
        self.assertEqual(memecoin_desk.loss_totals()["live_loss_usd"], "0.00")

    def test_allocation_restart_and_one_winner(self):
        for number in range(50, 61):
            self.assertEqual(_protect(self._analyzed(number))["stage"], "protected")
        left = self._analyzed(61)
        right = self._analyzed(62)
        results = []

        def race(case_id):
            results.append(_protect(case_id))

        threads = [threading.Thread(target=race, args=(case_id,)) for case_id in (left, right)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        reasons = [result["reason"] for result in results]
        self.assertCountEqual(reasons, [None, "allocation"])
        raced = []

        def scout_once():
            raced.append(memecoin_desk.scout(_pair(_mint(70)), now=NOW))

        threads = [threading.Thread(target=scout_once) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertCountEqual([item["reason"] for item in raced], [None, "already_open"])

    def test_a_failed_log_does_not_leave_a_pass(self):
        original = memecoin_desk._log
        def fail(conn, case_id, mint, stage, verdict, reason, source, evidence, moment):
            raise RuntimeError("log_failed")
        memecoin_desk._log = fail
        try:
            with self.assertRaises(RuntimeError):
                memecoin_desk.scout(_pair(_mint(90)), now=NOW)
        finally:
            memecoin_desk._log = original
        self.assertIsNone(memecoin_desk.view(_mint(90)))

    def test_exit_pnl_counts_the_recorded_entry_fee_once(self):
        case_id = self._filled(91)
        self.assertEqual(memecoin_desk.record_exit_receipt(case_id, {"fee_usd": "0.04"})["reason"], "receipt_unbound")
        closed = memecoin_desk.mark(case_id, "0.94", source="fixture_price", fetched_at=NOW, now=NOW)
        self.assertEqual(closed["action"], "stop")
        body = memecoin_desk.view(case_id)["snapshot"]
        self.assertEqual(memecoin_desk.view(case_id)["stage"], "paper_exited")
        self.assertFalse(body["live_ledger"])
        self.assertIsNone(body["paper_exit"]["receipt_proceeds"])
        self.assertNotIn("realized_pnl", body)

    def test_a_restart_keeps_the_fill_and_does_not_send_again(self):
        kept = self._filled(63)
        db.init_db()
        self.assertEqual(memecoin_desk.view(kept)["stage"], "filled")
        self.assertTrue(memecoin_desk.log_for(kept))
        self.assertFalse(memecoin_desk.view(kept)["snapshot"]["send"])
        again = memecoin_desk.harness_fill(kept, {"message_sha256": "nope", "tokens_out": 50})
        self.assertEqual(again["reason"], "stage")
        live = self._analyzed(64)
        _protect(live)
        held = memecoin_desk.view(live)["snapshot"]["reserved_usd"]
        for stage in ("protected", "entry_ready", "entry_inflight", "filled", "partial", "exit_intent"):
            db.execute("UPDATE memecoin_cases SET stage = ? WHERE id = ?", (stage, live))
            refused = memecoin_desk.harness_fill(live, {"message_sha256": "nope", "tokens_out": 50})
            self.assertEqual(refused["reason"], "paper_not_live", stage)
            self.assertEqual(memecoin_desk.view(live)["stage"], stage)
            self.assertEqual(db.query("SELECT ledger, ledger_provenance FROM memecoin_cases WHERE id = ?", (live,))[0]["ledger"], "live")
            self.assertEqual(memecoin_desk.view(live)["snapshot"]["reserved_usd"], held)
        db.execute("UPDATE memecoin_cases SET stage = 'entry_ready' WHERE id = ?", (live,))
        snap = memecoin_desk.view(live)["snapshot"]
        snap["entry_inflight"] = {"message_sha256": "held"}
        db.execute("UPDATE memecoin_cases SET snapshot_json = ? WHERE id = ?", (json.dumps(snap), live))
        refused = memecoin_desk.harness_fill(live, {"message_sha256": "held", "tokens_out": 50})
        self.assertEqual(refused["reason"], "paper_not_live")
        self.assertEqual(memecoin_desk.view(live)["snapshot"]["entry_inflight"]["message_sha256"], "held")
        import threading
        reasons = []

        def hit():
            reasons.append(memecoin_desk.harness_fill(live, {"message_sha256": "held", "tokens_out": 50})["reason"])

        threads = [threading.Thread(target=hit) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(reasons, ["paper_not_live", "paper_not_live"])
        self.assertEqual(memecoin_desk.view(live)["snapshot"]["reserved_usd"], held)
        self.assertEqual(db.query("SELECT ledger, ledger_provenance FROM memecoin_cases WHERE id = ?", (live,))[0]["ledger_provenance"], "server")
        self.assertEqual(db.query("SELECT intent_id FROM phantom_reservations"), [])

    def test_decoder_rejects_a_different_program_and_http_cannot_skip(self):
        checked = raydium_cpmm.inspect_buy(_buy(_mint(80)), payer=phantom_transport.REQUIRED_WALLET, output_mint=_mint(80), amount_in=1000, minimum_out=50)
        self.assertTrue(checked["ok"], checked)
        self.assertFalse(checked["send"])
        self.assertFalse(checked["live_ready"])
        wrong = raydium_cpmm.inspect_buy(_buy(_mint(80)), payer=phantom_transport.REQUIRED_WALLET, output_mint=_mint(80), amount_in=1001, minimum_out=50)
        self.assertEqual(wrong["reason"], "amount")
        self.assertEqual(token_execution.supported_buy(_mint(80))["reason"], "message_required")
        self.assertEqual(token_execution.supported_buy(_mint(80))["decoder"], "jupiter_route_v2_whirlpool")
        self.assertEqual(token_execution.supported_buy(_mint(80))["jupiter_route_v2"], "message_required")
        import server
        client = TestClient(server.app)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        forged = client.post("/api/memecoin/scout", headers=AUTH, json=_pair(_mint(81)) | {"passed": True})
        self.assertEqual(forged.status_code, 400)
        routed = client.post("/api/memecoin/protect", headers=AUTH, json={"case_id": "nope", "transaction": _buy(_mint(81))})
        self.assertEqual(routed.status_code, 400)
        moment = time.time()
        posted = _pair(_mint(81), fetched_at=moment, pair_created_at=int((moment - 3600) * 1000))
        self.assertEqual(client.post("/api/memecoin/scout", headers=AUTH, json=posted).status_code, 400)
        for key in ("confirmed_market_cap_usd", "market_cap_basis", "pair_address"):
            posted.pop(key, None)
        opened = client.post("/api/memecoin/scout", headers=AUTH, json=posted)
        self.assertEqual(opened.status_code, 409, opened.text)
        self.assertEqual(opened.json()["reason"], "caller_snapshot_not_the_handoff")
        nested = client.post("/api/memecoin/filter", headers=AUTH, json={
            "case_id": "nope",
            "onchain": {"cross_sources": {"pair_id": "caller"}, "confirmed_market_cap_usd": "1"},
        })
        self.assertEqual(nested.status_code, 400, nested.text)
        ran = client.post("/api/memecoin/run", headers=AUTH, json={"mint": _mint(81), "slippage_allowance_bps": 50, "price_transport": "injected"})
        self.assertEqual(ran.status_code, 409, ran.text)
        self.assertEqual(ran.json()["reason"], "scheduler_unarmed")
        self.assertFalse(ran.json()["request_body_used"])
        self.assertFalse(ran.json()["armed"])
        self.assertFalse(ran.json()["started"])
        self.assertFalse(ran.json()["scheduled"])
        self.assertEqual(db.get_setting("agents_enabled"), "0")
        from pathlib import Path
        route = Path("api.py").read_text(encoding="utf-8").split("def memecoin_run")[1].split("def memecoin_scout")[0]
        self.assertNotIn("slippage_allowance_bps", route)
        self.assertNotIn("price_transport", route)
        import pyth_sponsored
        fetched = []

        def prices(now):
            fetched.append(now)
            return {"ok": False, "fresh": False, "reason": "valuation_stale", "slot": 5, "publish_time": 1, "observed_at": now, "hermes_called": False}

        previous_fetch = pyth_sponsored.public_fetch
        pyth_sponsored.public_fetch = prices
        self.addCleanup(lambda: setattr(pyth_sponsored, "public_fetch", previous_fetch))
        forged = client.post("/api/memecoin/sol-usd", headers=AUTH, json={"owner": "rec5EKMGg6MxZYaMdyBfgwp4d5rB9T1VQH5pJv5LtFJ", "data": "AAAA", "upper": "0.01", "now": 1})
        self.assertEqual(forged.status_code, 200, forged.text)
        self.assertEqual(len(fetched), 1)
        self.assertFalse(forged.json()["request_body_used"])
        self.assertFalse(forged.json()["live"])
        self.assertEqual(forged.json()["reason"], "valuation_stale")
        self.assertNotIn("0.01", forged.text)

    def test_a_confirmed_slice_books_chain_proceeds_and_a_second_signature_does_not(self):
        import base64
        from tests.test_jupiter_route_v2 import WHEN, _metas, _pool, _prices, _route, _snap, _tx
        case_id = self._filled(91)
        live = self._analyzed(92)
        _protect(live)
        live_snap = memecoin_desk.view(live)["snapshot"]
        live_snap["fill"] = {"fixture": True}
        live_snap["entry_funding"] = {"source": "paper_harness"}
        live_snap["remaining_raw"] = 50
        db.execute(
            "UPDATE memecoin_cases SET stage = 'filled', snapshot_json = ? WHERE id = ?",
            (json.dumps(live_snap), live),
        )
        calls = []

        def prices(now):
            calls.append(now)
            return {"fresh": False}

        blocked = memecoin_desk.reserve_exit(live, "not-a-message", 50, None, prices, NOW)
        self.assertEqual(blocked["reason"], "exit_unproven")
        self.assertEqual(calls, [])
        self.assertEqual(memecoin_desk.view(live)["snapshot"]["remaining_raw"], 50)
        self.assertEqual(db.query("SELECT ledger, ledger_provenance FROM memecoin_cases WHERE id = ?", (live,))[0]["ledger"], "live")
        snap = memecoin_desk.view(case_id)["snapshot"]
        snap["fill"]["fixture"] = True
        snap["entry_funding"]["source"] = "paper_harness"
        snap["remaining_raw"] = 50
        db.execute(
            "UPDATE memecoin_cases SET snapshot_json = ? WHERE id = ?",
            (json.dumps(snap), case_id),
        )
        mint = memecoin_desk.view(case_id)["mint"]
        values, accounts = _pool(mint, solana_tx.NATIVE_MINT)
        unsigned = _tx(_metas(mint, solana_tx.NATIVE_MINT, values), _route(1_000, 0, True, amount=50), side="exit")
        raw = bytearray(base64.b64decode(unsigned))
        raw[1:65] = b"\x11" * 64
        signed = base64.b64encode(bytes(raw)).decode()
        signature = solana_tx.first_signature(signed)
        reserved = memecoin_desk.reserve_exit(case_id, signed, 50, _snap(accounts, WHEN), _prices(), WHEN)
        self.assertTrue(reserved["ok"], reserved)
        other = bytearray(raw)
        other[1:65] = b"\x22" * 64
        other_sig = solana_tx.first_signature(base64.b64encode(bytes(other)).decode())
        blocked = memecoin_desk.reserve_exit(case_id, base64.b64encode(bytes(other)).decode(), 50, _snap(accounts, WHEN), _prices(), WHEN)
        self.assertEqual(blocked["reason"], "exit_inflight")
        decoded = solana_tx.decode_transaction(bytes(raw))
        width = len(decoded.static_keys)
        pre = [0] * width
        post = [0] * width
        pre[0] = 5_000_000
        post[0] = 5_100_000
        source = values["source"]
        wallet = phantom_transport.REQUIRED_WALLET

        import json as _json
        import urllib.request
        calls = []

        def urlopen(req, timeout=8):
            payload = _json.loads(req.data.decode())
            calls.append(payload["method"])
            if payload["method"] == "getGenesisHash":
                body = {"jsonrpc": "2.0", "result": __import__("solana_inspect").MAINNET_GENESIS}
            elif payload["method"] == "getTransaction" and len(calls) < 4:
                body = {"jsonrpc": "2.0", "result": None}
            else:
                body = {"jsonrpc": "2.0", "result": {
                    "slot": 466835746,
                    "meta": {
                        "err": None,
                        "fee": 5000,
                        "preBalances": pre,
                        "postBalances": post,
                        "preTokenBalances": [{"mint": mint, "owner": wallet, "uiTokenAmount": {"amount": "50"}}],
                        "postTokenBalances": [{"mint": mint, "owner": wallet, "uiTokenAmount": {"amount": "0"}}],
                    },
                    "transaction": [signed, "base64"],
                }}

            class _Response:
                status = 200

                def read(self):
                    return _json.dumps(body).encode()

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

            return _Response()

        original = urllib.request.urlopen
        urllib.request.urlopen = urlopen
        self.addCleanup(lambda: setattr(urllib.request, "urlopen", original))
        unknown = memecoin_desk.confirm_exit(case_id, signature, WHEN)
        self.assertEqual(unknown["reason"], "receipt_unconfirmed")
        self.assertTrue(unknown["holds"])
        self.assertFalse(unknown["resend"])
        self.assertEqual(memecoin_desk.view(case_id)["snapshot"]["reserved_usd"], "8.00")
        booked = memecoin_desk.confirm_exit(case_id, signature, WHEN)
        self.assertTrue(booked["ok"], booked)
        self.assertEqual(booked["sol_proceeds"], 105_000)
        self.assertEqual(booked["remaining_raw"], 0)
        self.assertEqual(booked["stage"], "exited")
        self.assertTrue(booked["exit_chain_proven"])
        self.assertFalse(booked["entry_funding_proven"])
        self.assertFalse(booked["live_ledger"])
        self.assertFalse(booked["pnl_live"])
        self.assertTrue(booked["holds"])
        self.assertFalse(booked["dex_fee_remeasured"])
        self.assertEqual(booked["dex_fee_source"], "reserved_pool_state")
        self.assertEqual(booked["snapshot_slot"], 466835746)
        self.assertLessEqual(booked["snapshot_slot"], booked["slot"])
        self.assertFalse(booked["blockhash_rechecked"])
        self.assertTrue(booked["wsol_rent_net_proven"])
        self.assertEqual(booked["wsol_rent_net_lamports"], 0)
        self.assertFalse(booked["mint_refund_proven"])
        self.assertIsNone(booked["position_rent_lamports"])
        closed = memecoin_desk.view(case_id)["snapshot"]
        self.assertEqual(closed["reserved_usd"], "8.00")
        self.assertNotIn("realized_pnl", closed)
        self.assertEqual(closed["deposit_hold_reason"], "mint_ata_unobserved")
        self.assertNotEqual(booked["sol_proceeds"], 8)
        self.assertEqual(booked["fee_usd"], db.query("SELECT fee_usd FROM memecoin_exit_receipts")[0]["fee_usd"])
        self.assertFalse(str(booked["fee_usd"]).startswith("lamports:"))
        self.assertLessEqual(Decimal(booked["fee_usd"]), Decimal("0.05"))
        self.assertIn("getTransaction", calls)
        self.assertIn("getGenesisHash", calls)
        same = memecoin_desk.confirm_exit(case_id, signature, WHEN)
        self.assertEqual(same["reason"], "exit_duplicate")
        self.assertEqual(same["status"], "duplicate")
        self.assertTrue(same["holds"])
        self.assertFalse(same["recorded"])
        self.assertEqual(same["remaining_raw"], 0)
        again = memecoin_desk.confirm_exit(case_id, other_sig, WHEN)
        self.assertNotEqual(again.get("reason"), None)
        self.assertEqual(len(db.query("SELECT signature FROM memecoin_exit_receipts")), 1)
        import memecoin_exit
        zeros = bytearray(raw)
        zeros[1:65] = b"\x00" * 64
        unsigned = memecoin_exit.match_landed(
            {"message_sha256": reserved["message_sha256"], "mint": mint, "qty": 50, "pool": values["pool"]},
            signature,
            {"ok": True, "transaction": base64.b64encode(bytes(zeros)).decode(), "meta": {"fee": 5000, "err": None, "preBalances": pre, "postBalances": post, "preTokenBalances": [], "postTokenBalances": []}, "slot": 1},
        )
        self.assertEqual(unsigned["reason"], "receipt_unsigned")
        self.assertTrue(unsigned["holds"])
        self.assertFalse(unsigned["resend"])

    def test_open_positions_keep_the_ticket_and_a_landed_over_fee_is_a_breach(self):
        import base64
        import json as _json
        import urllib.request
        from tests.test_jupiter_route_v2 import WHEN, _metas, _pool, _prices, _route, _snap, _tx
        partial = self._filled(12)
        body = memecoin_desk.view(partial)["snapshot"]
        body["remaining_raw"] = 200
        db.execute("UPDATE memecoin_cases SET stage = 'partial', snapshot_json = ? WHERE id = ?", (_json.dumps(body), partial))
        self.assertEqual(db.exclusive(memecoin_desk._held_conn), Decimal("8.00"))
        paper = self._filled(13)
        self.assertEqual(memecoin_desk.mark(paper, "0.94", source="fixture_price", fetched_at=NOW, now=NOW)["action"], "stop")
        self.assertEqual(memecoin_desk.view(paper)["stage"], "paper_exited")
        self.assertEqual(db.exclusive(memecoin_desk._held_conn), Decimal("16.00"))
        opened = []
        for number in range(20, 32):
            opened.append(_protect(self._analyzed(number)))
        self.assertEqual(sum(1 for item in opened if item.get("ok")), 10)
        self.assertEqual(opened[10]["reason"], "allocation")
        mint = memecoin_desk.view(partial)["mint"]
        values, accounts = _pool(mint, solana_tx.NATIVE_MINT)
        unsigned = _tx(_metas(mint, solana_tx.NATIVE_MINT, values), _route(1_000, 0, True, amount=50), side="exit")
        raw = bytearray(base64.b64decode(unsigned))
        raw[1:65] = b"\x31" * 64
        signed = base64.b64encode(bytes(raw)).decode()
        signature = solana_tx.first_signature(signed)
        reserved = memecoin_desk.reserve_exit(partial, signed, 50, _snap(accounts, WHEN), _prices(), WHEN)
        self.assertTrue(reserved["ok"], reserved)
        decoded = solana_tx.decode_transaction(bytes(raw))
        width = len(decoded.static_keys)
        pre = [0] * width
        post = [0] * width
        pre[0] = 5_000_000
        post[0] = 5_100_000
        wallet = phantom_transport.REQUIRED_WALLET

        def urlopen(req, timeout=8):
            payload = _json.loads(req.data.decode())
            if payload["method"] == "getGenesisHash":
                rpc = {"jsonrpc": "2.0", "result": __import__("solana_inspect").MAINNET_GENESIS}
            else:
                rpc = {"jsonrpc": "2.0", "result": {"slot": 466835746, "transaction": [signed, "base64"], "meta": {
                    "err": None, "fee": 2_000_000, "preBalances": pre, "postBalances": post,
                    "preTokenBalances": [{"mint": mint, "owner": wallet, "uiTokenAmount": {"amount": "200"}}],
                    "postTokenBalances": [{"mint": mint, "owner": wallet, "uiTokenAmount": {"amount": "150"}}],
                }}}

            class _Response:
                status = 200
                def read(self):
                    return _json.dumps(rpc).encode()
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    return False
            return _Response()

        original = urllib.request.urlopen
        urllib.request.urlopen = urlopen
        self.addCleanup(lambda: setattr(urllib.request, "urlopen", original))
        breach = memecoin_desk.confirm_exit(partial, signature)
        self.assertEqual(breach["reason"], "fee_policy_breach")
        self.assertTrue(breach["recorded"])
        self.assertEqual(breach["remaining_raw"], 150)
        self.assertGreater(Decimal(breach["fee_usd"]), Decimal("0.05"))
        self.assertNotEqual(breach["fee_usd"], str(breach["sol_proceeds"]))
        self.assertEqual(db.get_setting("memecoin_entry_halt"), "1")
        self.assertEqual(db.exclusive(memecoin_desk._held_conn), Decimal("96.00"))
        self.assertFalse(breach["dex_fee_remeasured"])
        self.assertEqual(breach["status"], "breach")
        self.assertTrue(breach["holds"])
        self.assertNotIn("realized_pnl", memecoin_desk.view(partial)["snapshot"])

    def test_slot_window_and_mint_rent_stay_inside_the_ticket_until_refund(self):
        import base64
        import json as _json
        import urllib.request
        from tests.test_jupiter_route_v2 import PRICE_SLOT, WHEN, _metas, _pool, _prices, _route, _snap, _tx
        wallet = phantom_transport.REQUIRED_WALLET
        rent = solana_tx.TOKEN_ACCOUNT_RENT
        queue = []

        def urlopen(req, timeout=8):
            payload = _json.loads(req.data.decode())
            if payload["method"] == "getGenesisHash":
                body = {"jsonrpc": "2.0", "result": __import__("solana_inspect").MAINNET_GENESIS}
            else:
                body = {"jsonrpc": "2.0", "result": queue.pop(0)}

            class _Response:
                status = 200
                def read(self):
                    return _json.dumps(body).encode()
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    return False
            return _Response()

        original = urllib.request.urlopen
        urllib.request.urlopen = urlopen
        self.addCleanup(lambda: setattr(urllib.request, "urlopen", original))

        nonce = {"n": 1}

        def signed_exit(case_id, amount, slot_accounts=None, fund=False, ata_pre=0, ata_post=0):
            mint = memecoin_desk.view(case_id)["mint"]
            values, accounts = _pool(mint, solana_tx.NATIVE_MINT)
            ata = solana_tx.associated_token_address(wallet, mint, solana_tx.TOKEN_PROGRAM)
            owned = accounts.pop(values["source"])
            values["source"] = ata
            accounts[ata] = owned
            unsigned = _tx(_metas(mint, solana_tx.NATIVE_MINT, values), _route(1_000, 0, True, amount=amount), side="exit", fund_mint=mint if fund else None)
            raw = bytearray(base64.b64decode(unsigned))
            raw[1:65] = bytes([nonce["n"]]) + b"\x41" * 63
            nonce["n"] += 1
            signed = base64.b64encode(bytes(raw)).decode()
            signature = solana_tx.first_signature(signed)
            reserved = memecoin_desk.reserve_exit(case_id, signed, amount, _snap(accounts, WHEN), _prices(), WHEN)
            self.assertTrue(reserved["ok"], reserved)
            decoded = solana_tx.decode_transaction(bytes(raw))
            pre = [0] * len(decoded.static_keys)
            post = [0] * len(decoded.static_keys)
            pre[0] = 5_000_000
            post[0] = 5_100_000
            ata_index = decoded.static_keys.index(ata)
            pre[ata_index] = ata_pre
            post[ata_index] = ata_post
            result = {
                "slot": PRICE_SLOT,
                "transaction": [signed, "base64"],
                "meta": {
                    "err": None, "fee": 5000, "preBalances": pre, "postBalances": post,
                    "preTokenBalances": [{"mint": mint, "owner": wallet, "uiTokenAmount": {"amount": str(amount)}}],
                    "postTokenBalances": [{"mint": mint, "owner": wallet, "uiTokenAmount": {"amount": "0"}}],
                },
            }
            return signature, signed, raw, reserved, result

        partial = self._filled(31)
        body = memecoin_desk.view(partial)["snapshot"]
        body["remaining_raw"] = 200
        db.execute("UPDATE memecoin_cases SET stage = 'partial', snapshot_json = ? WHERE id = ?", (_json.dumps(body), partial))
        signature, signed, raw, reserved, landed = signed_exit(partial, 50, ata_pre=0, ata_post=0)
        flight = memecoin_desk.view(partial)["snapshot"]["inflight"]
        self.assertEqual(flight["snapshot_slot"], PRICE_SLOT)
        self.assertFalse(flight["dex_fee_remeasured"])
        self.assertEqual(flight["dex_fee_source"], "reserved_pool_state")
        flipped = bytearray(raw)
        flipped[-1] ^= 1
        mismatch = dict(landed)
        mismatch["transaction"] = [base64.b64encode(bytes(flipped)).decode(), "base64"]
        zeros = bytearray(raw)
        zeros[1:65] = b"\x00" * 64
        unsigned_result = dict(landed)
        unsigned_result["transaction"] = [base64.b64encode(bytes(zeros)).decode(), "base64"]
        queue.extend([mismatch, unsigned_result, landed])
        refused = memecoin_desk.confirm_exit(partial, signature, WHEN)
        self.assertEqual(refused["reason"], "message_mismatch")
        self.assertEqual(refused["status"], "held")
        self.assertTrue(refused["holds"])
        self.assertEqual(refused["remaining_raw"], 200)
        self.assertFalse(refused["recorded"])
        blank = memecoin_desk.confirm_exit(partial, signature, WHEN)
        self.assertEqual(blank["reason"], "receipt_unsigned")
        self.assertTrue(blank["holds"])
        self.assertEqual(blank["remaining_raw"], 200)
        self.assertIsNotNone(memecoin_desk.view(partial)["snapshot"].get("inflight"))
        booked = memecoin_desk.confirm_exit(partial, signature, WHEN)
        self.assertTrue(booked["ok"], booked)
        self.assertEqual(booked["remaining_raw"], 150)
        self.assertEqual(booked["dex_fee_lamports"], flight["dex_fee_lamports"])
        self.assertFalse(booked["dex_fee_remeasured"])
        self.assertEqual(booked["reserved_usd"], "8.00")
        self.assertTrue(booked["holds"])
        again = memecoin_desk.confirm_exit(partial, signature, WHEN)
        self.assertEqual(again["reason"], "exit_duplicate")
        self.assertEqual(again["status"], "duplicate")
        self.assertTrue(again["holds"])
        self.assertEqual(again["remaining_raw"], 150)
        self.assertEqual(len(db.query("SELECT signature FROM memecoin_exit_receipts WHERE case_id = ?", (partial,))), 1)

        future = self._filled(32)
        future_sig, _, _, _, future_landed = signed_exit(future, 50)
        future_landed = dict(future_landed)
        future_landed["slot"] = PRICE_SLOT - 1
        queue.append(future_landed)
        ahead = memecoin_desk.confirm_exit(future, future_sig, WHEN)
        self.assertEqual(ahead["reason"], "snapshot_after_landed")
        self.assertTrue(ahead["holds"])
        self.assertEqual(ahead["reserved_usd"], "8.00")
        self.assertEqual(db.query("SELECT signature FROM memecoin_exit_receipts WHERE case_id = ?", (future,)), [])

        window = self._filled(33)
        window_sig, _, _, _, window_landed = signed_exit(window, 50)
        window_landed = dict(window_landed)
        window_landed["slot"] = PRICE_SLOT + 151
        queue.append(window_landed)
        late = memecoin_desk.confirm_exit(window, window_sig, WHEN)
        self.assertEqual(late["reason"], "slot_distance_guard")
        self.assertTrue(late["holds"])
        self.assertEqual(late["slot_distance_cap"], 150)
        self.assertEqual(late["slot_distance"], 151)
        self.assertFalse(late["slot_distance_is_blockhash_proof"])
        self.assertTrue(late["blockhash_expiry_needs_block_height"])
        self.assertFalse(late["quote_clock_proven"])
        self.assertFalse(late["quote_expiry_proven"])
        self.assertFalse(late["blockhash_rechecked"])
        self.assertEqual(db.query("SELECT signature FROM memecoin_exit_receipts WHERE case_id = ?", (window,)), [])

        preexisting = self._filled(34)
        old_sig, _, _, _, old_landed = signed_exit(preexisting, 50, ata_pre=rent, ata_post=rent)
        queue.append(old_landed)
        kept = memecoin_desk.confirm_exit(preexisting, old_sig, WHEN)
        self.assertTrue(kept["ok"], kept)
        self.assertEqual(kept["remaining_raw"], 0)
        self.assertEqual(memecoin_desk.view(preexisting)["snapshot"]["reserved_usd"], "8.00")
        self.assertEqual(memecoin_desk.view(preexisting)["snapshot"]["deposit_hold_reason"], "preexisting_mint_account")
        self.assertEqual(memecoin_desk.confirm_deposit_refund(preexisting, "refund-old")["reason"], "preexisting_account")
        self.assertEqual(memecoin_desk.view(preexisting)["snapshot"]["reserved_usd"], "8.00")

        paper = self._filled(35)
        self.assertEqual(memecoin_desk.mark(paper, "0.94", source="fixture_price", fetched_at=NOW, now=NOW)["action"], "stop")
        self.assertEqual(memecoin_desk.confirm_deposit_refund(paper, "refund-paper")["reason"], "paper_not_live")
        self.assertEqual(memecoin_desk.view(paper)["snapshot"]["reserved_usd"], "8.00")

        funded = self._filled(36)
        fund_sig, _, _, _, fund_landed = signed_exit(funded, 50, fund=True, ata_pre=0, ata_post=rent)
        stored_leg = memecoin_desk.view(funded)["snapshot"]["inflight"]
        leg = {"used": True, "fresh": True, "price": stored_leg["sol_price"], "conf": stored_leg["sol_conf"], "expo": stored_leg["sol_expo"]}
        import pyth_sponsored
        deposit = pyth_sponsored.lamports_usd(rent, leg)
        queue.append(fund_landed)
        opened = memecoin_desk.confirm_exit(funded, fund_sig, WHEN)
        self.assertTrue(opened["ok"], opened)
        self.assertEqual(opened["remaining_raw"], 0)
        self.assertEqual(opened["position_rent_lamports"], rent)
        self.assertTrue(opened["wsol_rent_net_proven"])
        self.assertEqual(opened["wsol_rent_net_lamports"], 0)
        self.assertFalse(opened["mint_refund_proven"])
        self.assertFalse(opened["entry_funding_proven"])
        self.assertNotIn("realized_pnl", memecoin_desk.view(funded)["snapshot"])
        self.assertEqual(memecoin_desk.view(funded)["snapshot"]["reserved_usd"], format(deposit, "f"))
        self.assertLess(deposit, Decimal("8.00"))
        self.assertGreater(deposit, Decimal("0"))
        reloaded = _json.loads(db.query("SELECT snapshot_json FROM memecoin_cases WHERE id = ?", (funded,))[0]["snapshot_json"])
        self.assertEqual(reloaded["deposit_hold_lamports"], rent)
        self.assertEqual(reloaded["deposits"]["mint_ata"]["funder"], wallet)
        self.assertEqual(reloaded["deposits"]["mint_ata"]["provenance"], "bot_created_this_transaction")
        ledger = db.query("SELECT bot_funded, refunded, rent_lamports, funder FROM memecoin_deposits WHERE case_id = ? AND kind = 'mint_ata'", (funded,))[0]
        self.assertEqual(ledger["bot_funded"], 1)
        self.assertEqual(ledger["refunded"], 0)
        self.assertEqual(ledger["rent_lamports"], rent)
        held_with_deposit = db.exclusive(memecoin_desk._held_conn)
        self.assertEqual(held_with_deposit, Decimal("40.00") + deposit)

        ata = reloaded["deposits"]["mint_ata"]["account"]
        close = solana_tx.encode_legacy(wallet, [wallet, ata, solana_tx.TOKEN_PROGRAM], 1, [(2, [1, 0, 0], bytes([9]))], solana_tx.b58encode(b"\x44" * 32))
        close_raw = bytearray(base64.b64decode(close))
        close_raw[1:65] = b"\x52" * 64
        close_signed = base64.b64encode(bytes(close_raw)).decode()
        close_sig = solana_tx.first_signature(close_signed)
        queue.append({
            "slot": PRICE_SLOT + 2,
            "transaction": [close_signed, "base64"],
            "meta": {"err": None, "fee": 5000, "preBalances": [1_000_000_000, rent, 1], "postBalances": [1_000_000_000 + rent - 5000, 0, 1]},
        })
        refunded = memecoin_desk.confirm_deposit_refund(funded, close_sig)
        self.assertTrue(refunded["ok"], refunded)
        self.assertFalse(refunded["holds"])
        self.assertEqual(refunded["reserved_usd"], "0.00")
        self.assertEqual(refunded["rent_lamports"], rent)
        self.assertEqual(refunded["wallet"], wallet)
        self.assertNotIn("realized_pnl", memecoin_desk.view(funded)["snapshot"])
        self.assertEqual(db.query("SELECT refunded FROM memecoin_deposits WHERE case_id = ? AND kind = 'mint_ata'", (funded,))[0]["refunded"], 1)
        self.assertEqual(db.exclusive(memecoin_desk._held_conn), Decimal("40.00"))
        repeat = memecoin_desk.confirm_deposit_refund(funded, close_sig)
        self.assertEqual(repeat["reason"], "deposit_duplicate")
        self.assertFalse(repeat["holds"])
        self.assertEqual(db.exclusive(memecoin_desk._held_conn), Decimal("40.00"))
        self.assertEqual(memecoin_desk.confirm_deposit_refund(partial, "too-soon")["reason"], "tokens_unsold")

    def test_entry_receipt_books_chain_qty_and_paper_loss_stays_off_the_live_halt(self):
        import base64
        import json as _json
        import urllib.request
        import desk_loop
        from tests.test_jupiter_route_v2 import PRICE_SLOT, WHEN, _decode, _metas, _pool, _prices, _route, _snap, _tx
        wallet = phantom_transport.REQUIRED_WALLET
        rent = solana_tx.TOKEN_ACCOUNT_RENT
        queue = []

        def urlopen(req, timeout=8):
            payload = _json.loads(req.data.decode())
            if payload["method"] == "getGenesisHash":
                body = {"jsonrpc": "2.0", "result": __import__("solana_inspect").MAINNET_GENESIS}
            else:
                body = {"jsonrpc": "2.0", "result": queue.pop(0)}

            class _Response:
                status = 200
                def read(self):
                    return _json.dumps(body).encode()
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    return False
            return _Response()

        original = urllib.request.urlopen
        urllib.request.urlopen = urlopen
        self.addCleanup(lambda: setattr(urllib.request, "urlopen", original))
        for number in range(70, 74):
            case_id = self._filled(number)
            self.assertEqual(memecoin_desk.mark(case_id, "0.94", source="fixture_price", fetched_at=NOW, now=NOW)["action"], "stop")
        planted = memecoin_desk.view(self._filled(74))
        forged = planted["snapshot"]
        forged["realized_pnl"] = "-5.00"
        forged["pnl_live"] = True
        forged["entry_funding"]["source"] = "entry_receipt"
        forged["entry_funding"]["proven"] = True
        forged["entry_funding"]["signature"] = "forged-without-a-receipt"
        db.execute("UPDATE memecoin_cases SET snapshot_json = ? WHERE id = ?", (_json.dumps(forged), planted["case_id"]))
        self.assertEqual(memecoin_desk.loss_totals()["live_loss_usd"], "0.00")
        self.assertGreater(Decimal(memecoin_desk.loss_totals()["paper_loss_usd"]), Decimal("0"))
        self.assertFalse(memecoin_desk.loss_totals()["live_includes_paper"])
        self.assertFalse(desk_loop.scheduler_status()["live_ready"])
        self.assertFalse(desk_loop.scheduler_status()["send"])

        nonce = {"n": 4}

        def buy(case_id, amount, tokens, ata_pre=0, ata_post=None, fund=True, pre_token=None, fee=5000):
            mint = memecoin_desk.view(case_id)["mint"]
            values, accounts = _pool(solana_tx.NATIVE_MINT, mint)
            route, est = _amm_route(mint, amount, "buy")
            floor = est * 9950 // 10_000
            tokens = tokens if isinstance(tokens, int) and tokens >= floor else est
            ata = solana_tx.associated_token_address(wallet, mint, solana_tx.TOKEN_PROGRAM)
            owned = accounts.pop(values["dest"])
            values["dest"] = ata
            accounts[ata] = owned
            unsigned = _tx(_metas(solana_tx.NATIVE_MINT, mint, values), route, side="buy", fund_mint=mint if fund else None)
            raw = bytearray(base64.b64decode(unsigned))
            raw[1:65] = bytes([nonce["n"]]) + b"\x61" * 63
            nonce["n"] += 1
            signed = base64.b64encode(bytes(raw)).decode()
            signature = solana_tx.first_signature(signed)
            reserved = memecoin_desk.reserve_entry(case_id, signed, _snap(accounts, WHEN), _prices(), WHEN)
            decoded = solana_tx.decode_transaction(bytes(raw))
            pre = [0] * len(decoded.static_keys)
            post = [0] * len(decoded.static_keys)
            deposit = rent if fund else 0
            pre[0] = 200_000_000
            post[0] = 200_000_000 - (amount + fee + (rent if ata_post is None and fund else (ata_post or 0) - ata_pre))
            ata_index = decoded.static_keys.index(ata)
            pre[ata_index] = ata_pre
            post[ata_index] = rent if ata_post is None and fund else (ata_pre if ata_post is None else ata_post)
            pre_tokens = [] if pre_token is None else [{"mint": mint, "owner": wallet, "uiTokenAmount": {"amount": str(pre_token)}}]
            landed = {
                "slot": PRICE_SLOT,
                "blockTime": int(WHEN),
                "transaction": [signed, "base64"],
                "meta": {
                    "err": None, "fee": fee, "preBalances": pre, "postBalances": post,
                    "preTokenBalances": pre_tokens,
                    "postTokenBalances": [{"mint": mint, "owner": wallet, "uiTokenAmount": {"amount": str(tokens)}}],
                },
            }
            return signature, reserved, landed, ata

        case_id = _protect(self._analyzed(80))["case_id"] if False else self._analyzed(80)
        _protect(case_id)
        signature, reserved, landed, ata = buy(case_id, 1_000_000, 1_000)
        self.assertTrue(reserved["ok"], reserved)
        self.assertFalse(reserved["effectual_entry"])
        self.assertTrue(reserved["owner_slippage_approved"])
        self.assertFalse(reserved["test_consent_is_owner_consent"])
        self.assertNotIn("owner_slippage_unapproved", reserved["effectual_blocked_by"])
        self.assertIn("lp_fields_unpublished", reserved["effectual_blocked_by"])
        self.assertIn("quote_expiry_unproven", reserved["effectual_blocked_by"])
        self.assertIn("quote_expiry_unproven", reserved["effectual_blocked_by"])
        self.assertFalse(reserved["quote_clock_proven"])
        self.assertFalse(reserved["send"])
        self.assertFalse(reserved["live_ready"])
        again = memecoin_desk.reserve_entry(case_id, base64.b64encode(base64.b64decode(landed["transaction"][0])).decode(), _snap(_pool(solana_tx.NATIVE_MINT, memecoin_desk.view(case_id)["mint"])[1], WHEN), _prices(), WHEN)
        self.assertTrue(again["idempotent"], again)
        queue.append(None)
        unknown = memecoin_desk.confirm_entry(case_id, signature, WHEN)
        self.assertEqual(unknown["reason"], "receipt_unconfirmed")
        self.assertTrue(unknown["holds"])
        self.assertFalse(unknown["resend"])
        queue.append(landed)
        booked = memecoin_desk.confirm_entry(case_id, signature, WHEN)
        self.assertTrue(booked["ok"], booked)
        self.assertEqual(booked["qty"], 999_899)
        self.assertEqual(booked["qty_source"], "entry_receipt")
        self.assertEqual(booked["token_pre_source"], "account_absent")
        self.assertEqual(booked["cost_basis_lamports"], 1_000_000 + 5000)
        self.assertNotEqual(booked["cost_basis_lamports"], 1_000_000 + 5000 + booked["dex_fee_lamports"])
        self.assertTrue(booked["dex_fee_counted_in_input"])
        self.assertTrue(booked["network_fee_added_once"])
        self.assertIsNone(booked["proceeds"])
        self.assertFalse(booked["quote_clock_proven"])
        self.assertFalse(booked["quote_expiry_proven"])
        self.assertEqual(booked["quote_clock_reason"], "block_height_missing")
        self.assertFalse(booked["effectual_entry"])
        self.assertNotEqual(memecoin_desk.view(case_id)["snapshot"]["entry_funding"]["cost_basis_usd"], memecoin_desk.view(case_id)["snapshot"]["entry_price_usd"])
        duplicate = memecoin_desk.confirm_entry(case_id, signature, WHEN)
        self.assertEqual(duplicate["reason"], "entry_duplicate")
        self.assertEqual(len(db.query("SELECT signature FROM memecoin_entry_receipts WHERE case_id = ?", (case_id,))), 1)
        reloaded = _json.loads(db.query("SELECT snapshot_json FROM memecoin_cases WHERE id = ?", (case_id,))[0]["snapshot_json"])
        self.assertEqual(reloaded["remaining_raw"], 999_899)
        self.assertEqual(reloaded["entry_funding"]["qty_source"], "entry_receipt")
        self.assertEqual(memecoin_desk.loss_totals()["live_loss_usd"], "0.00")

        mint = memecoin_desk.view(case_id)["mint"]
        values, accounts = _pool(mint, solana_tx.NATIVE_MINT)
        owned = accounts.pop(values["source"])
        values["source"] = ata
        accounts[ata] = owned
        exit_unsigned = _tx(_metas(mint, solana_tx.NATIVE_MINT, values), _route(1_000, 0, True, amount=1_000), side="exit")
        exit_raw = bytearray(base64.b64decode(exit_unsigned))
        exit_raw[1:65] = b"\x71" * 64
        exit_signed = base64.b64encode(bytes(exit_raw)).decode()
        exit_sig = solana_tx.first_signature(exit_signed)
        exit_reserved = memecoin_desk.reserve_exit(case_id, exit_signed, 1_000, _snap(accounts, WHEN), _prices(), WHEN)
        self.assertTrue(exit_reserved["ok"], exit_reserved)
        decoded = solana_tx.decode_transaction(bytes(exit_raw))
        pre = [0] * len(decoded.static_keys)
        post = [0] * len(decoded.static_keys)
        pre[0] = 5_000_000
        post[0] = 5_100_000
        pre[decoded.static_keys.index(ata)] = rent
        post[decoded.static_keys.index(ata)] = rent
        queue.append({
            "slot": PRICE_SLOT, "transaction": [exit_signed, "base64"],
            "meta": {"err": None, "fee": 5000, "preBalances": pre, "postBalances": post,
                     "preTokenBalances": [{"mint": mint, "owner": wallet, "uiTokenAmount": {"amount": "1000"}}],
                     "postTokenBalances": [{"mint": mint, "owner": wallet, "uiTokenAmount": {"amount": "0"}}]},
        })
        closed = memecoin_desk.confirm_exit(case_id, exit_sig, WHEN)
        self.assertTrue(closed["ok"], closed)
        self.assertTrue(closed["pnl_live"])
        self.assertEqual(closed["pnl_source"], "entry_receipt_and_exit_receipt")
        self.assertNotEqual(memecoin_desk.view(case_id)["snapshot"]["entry_price_usd"], memecoin_desk.view(case_id)["snapshot"]["realized_pnl"])
        self.assertFalse(memecoin_desk.view(case_id)["snapshot"]["pnl_uses_case_price"])
        self.assertLess(Decimal(memecoin_desk.view(case_id)["snapshot"]["realized_pnl"]), Decimal("0"))
        live_after_exit = memecoin_desk.loss_totals()["live_loss_usd"]
        self.assertGreater(Decimal(live_after_exit), Decimal("0"))
        paper_case = self._filled(81)
        self.assertEqual(memecoin_desk.mark(paper_case, "0.94", source="fixture_price", fetched_at=NOW, now=NOW)["action"], "stop")
        self.assertEqual(memecoin_desk.loss_totals()["live_loss_usd"], live_after_exit)

        missing = self._analyzed(82)
        _protect(missing)
        missing_sig, missing_reserved, missing_landed, _missing_ata = buy(missing, 1_000_000, 1_000, fund=False, ata_pre=rent, ata_post=rent, pre_token=None)
        self.assertTrue(missing_reserved["ok"], missing_reserved)
        queue.append(missing_landed)
        refused = memecoin_desk.confirm_entry(missing, missing_sig, WHEN)
        self.assertEqual(refused["reason"], "token_prebalance_unknown")
        self.assertTrue(refused["holds"])
        self.assertEqual(db.query("SELECT signature FROM memecoin_entry_receipts WHERE case_id = ?", (missing,)), [])

        old = self._analyzed(83)
        _protect(old)
        old_sig, old_reserved, old_landed, _old_ata = buy(old, 1_000_000, 1_000, fund=False, ata_pre=rent, ata_post=rent, pre_token=0)
        self.assertTrue(old_reserved["ok"], old_reserved)
        queue.append(old_landed)
        preexisting = memecoin_desk.confirm_entry(old, old_sig, WHEN)
        self.assertTrue(preexisting["ok"], preexisting)
        self.assertEqual(preexisting["deposit_lamports"], 0)
        self.assertEqual(memecoin_desk.view(old)["snapshot"]["deposits"]["mint_ata"]["preexisting"], True)
        self.assertEqual(memecoin_desk.confirm_deposit_refund(old, "close-user-ata")["reason"], "tokens_unsold")
        sold_out = memecoin_desk.view(old)["snapshot"]
        sold_out["remaining_raw"] = 0
        db.execute("UPDATE memecoin_cases SET stage = 'exited', snapshot_json = ? WHERE id = ?", (_json.dumps(sold_out), old))
        self.assertEqual(memecoin_desk.confirm_deposit_refund(old, "close-user-ata")["reason"], "preexisting_account")
        self.assertEqual(memecoin_desk.view(old)["snapshot"]["reserved_usd"], "8.00")

        breach_case = self._analyzed(86)
        _protect(breach_case)
        breach_sig, breach_reserved, breach_landed, _breach_ata = buy(breach_case, 1_000_000, 1_000, fee=2_000_000)
        self.assertTrue(breach_reserved["ok"], breach_reserved)
        failed_case = self._analyzed(84)
        _protect(failed_case)
        failed_sig, failed_reserved, failed_landed, _failed_ata = buy(failed_case, 1_000_000, 1_000)
        self.assertTrue(failed_reserved["ok"], failed_reserved)
        queue.append(breach_landed)
        breach = memecoin_desk.confirm_entry(breach_case, breach_sig, WHEN)
        self.assertEqual(breach["reason"], "fee_policy_breach")
        self.assertTrue(breach["recorded"])
        self.assertEqual(breach["qty"], 999_899)
        self.assertIsNone(breach["proceeds"])
        self.assertGreater(Decimal(breach["fee_usd"]), Decimal("0.05"))
        self.assertEqual(db.query("SELECT failed, qty, fee_usd FROM memecoin_entry_receipts WHERE case_id = ?", (breach_case,))[0]["failed"], 0)
        failed_landed = dict(failed_landed)
        failed_landed["meta"] = dict(failed_landed["meta"])
        failed_landed["meta"]["err"] = {"InstructionError": [0, "Custom"]}
        failed_landed["meta"]["preTokenBalances"] = []
        failed_landed["meta"]["postTokenBalances"] = []
        failed_landed["meta"]["postBalances"] = list(failed_landed["meta"]["preBalances"])
        failed_landed["meta"]["postBalances"][0] = failed_landed["meta"]["preBalances"][0] - 5000
        queue.append(failed_landed)
        failed = memecoin_desk.confirm_entry(failed_case, failed_sig, WHEN)
        self.assertEqual(failed["reason"], "receipt_failed")
        self.assertEqual(failed["qty"], 0)
        self.assertIsNone(failed["proceeds"])
        self.assertTrue(failed["recorded"])
        self.assertTrue(failed["ordinary_revert"])
        self.assertFalse(failed["entry_halt"])
        self.assertFalse(failed["holds"])
        self.assertEqual(db.get_setting("memecoin_entry_halt"), "1")
        self.assertGreater(Decimal(memecoin_desk.loss_totals()["live_loss_usd"]), Decimal(live_after_exit))
        self.assertEqual(_protect(self._analyzed(85))["reason"], "entry_halt")

    def test_three_live_round_trips_halt_new_entries_and_do_not_block_the_open_exit(self):
        import base64
        import json as _json
        import urllib.request
        from tests.test_jupiter_route_v2 import PRICE_SLOT, WHEN, _metas, _pool, _prices, _route, _snap, _tx
        wallet = phantom_transport.REQUIRED_WALLET
        rent = solana_tx.TOKEN_ACCOUNT_RENT
        queue = []
        nonce = {"n": 9}

        def urlopen(req, timeout=8):
            payload = _json.loads(req.data.decode())
            if payload["method"] == "getGenesisHash":
                body = {"jsonrpc": "2.0", "result": __import__("solana_inspect").MAINNET_GENESIS}
            else:
                body = {"jsonrpc": "2.0", "result": queue.pop(0)}

            class _Response:
                status = 200
                def read(self):
                    return _json.dumps(body).encode()
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    return False
            return _Response()

        original = urllib.request.urlopen
        urllib.request.urlopen = urlopen
        self.addCleanup(lambda: setattr(urllib.request, "urlopen", original))

        def sign(raw):
            raw[1:65] = bytes([nonce["n"]]) + b"\x72" * 63
            nonce["n"] += 1
            signed = base64.b64encode(bytes(raw)).decode()
            return signed, solana_tx.first_signature(signed)

        def enter(number, amount, tokens):
            case_id = self._analyzed(number)
            _protect(case_id)
            mint = memecoin_desk.view(case_id)["mint"]
            values, accounts = _pool(solana_tx.NATIVE_MINT, mint)
            ata = solana_tx.associated_token_address(wallet, mint, solana_tx.TOKEN_PROGRAM)
            owned = accounts.pop(values["dest"])
            values["dest"] = ata
            accounts[ata] = owned
            route, est = _amm_route(mint, amount, "buy")
            raw = bytearray(base64.b64decode(_tx(_metas(solana_tx.NATIVE_MINT, mint, values), route, side="buy", fund_mint=mint)))
            signed, signature = sign(raw)
            reserved = memecoin_desk.reserve_entry(case_id, signed, _snap(accounts, WHEN), _prices(), WHEN)
            self.assertTrue(reserved["ok"], reserved)
            self.assertFalse(reserved["effectual_entry"])
            decoded = solana_tx.decode_transaction(bytes(raw))
            pre = [0] * len(decoded.static_keys)
            post = [0] * len(decoded.static_keys)
            pre[0] = 300_000_000
            post[0] = 300_000_000 - (amount + 5000 + rent)
            pre[decoded.static_keys.index(ata)] = 0
            post[decoded.static_keys.index(ata)] = rent
            queue.append({"slot": PRICE_SLOT, "transaction": [signed, "base64"], "meta": {
                "err": None, "fee": 5000, "preBalances": pre, "postBalances": post,
                "preTokenBalances": [],
                "postTokenBalances": [{"mint": mint, "owner": wallet, "uiTokenAmount": {"amount": str(est)}}],
            }})
            booked = memecoin_desk.confirm_entry(case_id, signature, WHEN)
            self.assertTrue(booked["ok"], booked)
            return case_id, mint, ata, est

        def leave(case_id, mint, ata, tokens):
            values, accounts = _pool(mint, solana_tx.NATIVE_MINT)
            owned = accounts.pop(values["source"])
            values["source"] = ata
            accounts[ata] = owned
            route, est = _amm_route(mint, tokens, "exit")
            raw = bytearray(base64.b64decode(_tx(_metas(mint, solana_tx.NATIVE_MINT, values), route, side="exit")))
            signed, signature = sign(raw)
            reserved = memecoin_desk.reserve_exit(case_id, signed, tokens, _snap(accounts, WHEN), _prices(), WHEN)
            self.assertTrue(reserved["ok"], reserved)
            decoded = solana_tx.decode_transaction(bytes(raw))
            pre = [0] * len(decoded.static_keys)
            post = [0] * len(decoded.static_keys)
            pre[0] = 5_000_000
            post[0] = 5_000_000 + est
            pre[decoded.static_keys.index(ata)] = rent
            post[decoded.static_keys.index(ata)] = rent
            queue.append({"slot": PRICE_SLOT, "transaction": [signed, "base64"], "meta": {
                "err": None, "fee": 5000, "preBalances": pre, "postBalances": post,
                "preTokenBalances": [{"mint": mint, "owner": wallet, "uiTokenAmount": {"amount": str(tokens)}}],
                "postTokenBalances": [{"mint": mint, "owner": wallet, "uiTokenAmount": {"amount": "0"}}],
            }})
            closed = memecoin_desk.confirm_exit(case_id, signature, WHEN)
            self.assertTrue(closed["pnl_live"], closed)
            self.assertLess(Decimal(closed["realized_pnl"]), Decimal("0"))
            return closed

        paper = self._filled(95)
        open_case, open_mint, open_ata, open_qty = enter(90, 1_000_000, 1_000)
        closed_positions = []
        for number in (91, 92, 93):
            case_id, mint, ata, qty = enter(number, 80_000_000, 1_000_000)
            leave(case_id, mint, ata, qty)
            closed_positions.append((case_id, ata, memecoin_desk.view(case_id)["snapshot"]["realized_pnl"]))
        live = Decimal(memecoin_desk.loss_totals()["live_loss_usd"])
        self.assertGreater(live, Decimal("0"))
        self.assertEqual(memecoin_desk.loss_totals()["paper_loss_usd"], "0.00")
        self.assertNotEqual(_protect(self._analyzed(94)).get("reason"), "stop_new_entries")
        for case_id, ata, trading_pnl in closed_positions:
            raw = bytearray(base64.b64decode(solana_tx.encode_legacy(wallet, [wallet, ata, solana_tx.TOKEN_PROGRAM], 1, [(2, [1, 0, 0], bytes([9]))], solana_tx.b58encode(b"\x44" * 32))))
            signed, signature = sign(raw)
            queue.append({"slot": PRICE_SLOT, "transaction": [signed, "base64"], "meta": {
                "err": None, "fee": 5000,
                "preBalances": [1_000_000_000, rent, 1],
                "postBalances": [1_000_000_000 + rent - 5000, 0, 1],
            }})
            refunded = memecoin_desk.confirm_deposit_refund(case_id, signature)
            self.assertTrue(refunded["ok"], refunded)
            self.assertEqual(refunded["reserved_usd"], "0.00")
            self.assertEqual(memecoin_desk.view(case_id)["snapshot"]["realized_pnl"], trading_pnl)
            self.assertTrue(memecoin_desk.view(case_id)["snapshot"]["pnl_live"])
        self.assertGreaterEqual(Decimal(memecoin_desk.loss_totals()["live_loss_usd"]), live)
        still = leave(open_case, open_mint, open_ata, open_qty)
        self.assertTrue(still["ok"], still)
        before = memecoin_desk.loss_totals()["live_loss_usd"]
        self.assertEqual(memecoin_desk.mark(paper, "0.94", source="fixture_price", fetched_at=NOW, now=NOW)["action"], "stop")
        self.assertEqual(memecoin_desk.loss_totals()["live_loss_usd"], before)
        self.assertGreater(Decimal(memecoin_desk.loss_totals()["paper_loss_usd"]), Decimal("0"))
        self.assertEqual(_json.loads(db.query("SELECT snapshot_json FROM memecoin_cases WHERE id = ?", (open_case,))[0]["snapshot_json"])["pnl_live"], True)

    def test_rent_refund_keeps_the_trading_loss_and_adds_its_fee_once(self):
        import base64
        import json as _json
        import urllib.request
        from tests.test_jupiter_route_v2 import PRICE_SLOT, WHEN, _metas, _pool, _prices, _route, _snap, _tx
        wallet = phantom_transport.REQUIRED_WALLET
        rent = solana_tx.TOKEN_ACCOUNT_RENT
        queue = []

        def urlopen(req, timeout=8):
            payload = _json.loads(req.data.decode())
            body = {"jsonrpc": "2.0", "result": __import__("solana_inspect").MAINNET_GENESIS if payload["method"] == "getGenesisHash" else queue.pop(0)}

            class _Response:
                status = 200
                def read(self):
                    return _json.dumps(body).encode()
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    return False
            return _Response()

        original = urllib.request.urlopen
        urllib.request.urlopen = urlopen
        self.addCleanup(lambda: setattr(urllib.request, "urlopen", original))
        case_id = self._analyzed(110)
        _protect(case_id)
        mint = memecoin_desk.view(case_id)["mint"]
        values, accounts = _pool(solana_tx.NATIVE_MINT, mint)
        ata = solana_tx.associated_token_address(wallet, mint, solana_tx.TOKEN_PROGRAM)
        owned = accounts.pop(values["dest"])
        values["dest"] = ata
        accounts[ata] = owned
        route, est = _amm_route(mint, 1_000_000, "buy")
        raw = bytearray(base64.b64decode(_tx(_metas(solana_tx.NATIVE_MINT, mint, values), route, side="buy", fund_mint=mint)))
        raw[1:65] = b"\x21" * 64
        signed = base64.b64encode(bytes(raw)).decode()
        signature = solana_tx.first_signature(signed)
        self.assertTrue(memecoin_desk.reserve_entry(case_id, signed, _snap(accounts, WHEN), _prices(), WHEN)["ok"])
        decoded = solana_tx.decode_transaction(bytes(raw))
        pre = [0] * len(decoded.static_keys)
        post = list(pre)
        pre[0] = 20_000_000
        post[0] = 20_000_000 - (1_000_000 + 5000 + rent)
        pre[decoded.static_keys.index(ata)] = 0
        post[decoded.static_keys.index(ata)] = rent
        queue.append({"slot": PRICE_SLOT, "transaction": [signed, "base64"], "meta": {
            "err": None, "fee": 5000, "preBalances": pre, "postBalances": post, "preTokenBalances": [],
            "postTokenBalances": [{"mint": mint, "owner": wallet, "uiTokenAmount": {"amount": str(est)}}],
        }})
        booked = memecoin_desk.confirm_entry(case_id, signature, WHEN)
        self.assertTrue(booked["ok"], booked)
        self.assertEqual(booked["qty"], est)
        self.assertEqual(booked["cost_basis_lamports"], 1_005_000)

        def sell(amount, before, after, mark):
            values, accounts = _pool(mint, solana_tx.NATIVE_MINT)
            owned = accounts.pop(values["source"])
            values["source"] = ata
            accounts[ata] = owned
            route, out = _amm_route(mint, amount, "exit")
            raw = bytearray(base64.b64decode(_tx(_metas(mint, solana_tx.NATIVE_MINT, values), route, side="exit")))
            raw[1:65] = bytes([mark]) * 64
            signed = base64.b64encode(bytes(raw)).decode()
            signature = solana_tx.first_signature(signed)
            self.assertTrue(memecoin_desk.reserve_exit(case_id, signed, amount, _snap(accounts, WHEN), _prices(), WHEN)["ok"])
            decoded = solana_tx.decode_transaction(bytes(raw))
            pre = [0] * len(decoded.static_keys)
            post = [0] * len(decoded.static_keys)
            pre[0] = 5_000_000
            post[0] = 5_000_000 + out
            pre[decoded.static_keys.index(ata)] = rent
            post[decoded.static_keys.index(ata)] = rent
            queue.append({"slot": PRICE_SLOT, "transaction": [signed, "base64"], "meta": {
                "err": None, "fee": 5000, "preBalances": pre, "postBalances": post,
                "preTokenBalances": [{"mint": mint, "owner": wallet, "uiTokenAmount": {"amount": str(before)}}],
                "postTokenBalances": [{"mint": mint, "owner": wallet, "uiTokenAmount": {"amount": str(after)}}],
            }})
            return memecoin_desk.confirm_exit(case_id, signature, WHEN)

        partial = sell(400, est, est - 400, 22)
        self.assertTrue(partial["ok"], partial)
        self.assertEqual(partial["remaining_raw"], est - 400)
        self.assertTrue(partial["pnl_live"])
        self.assertLess(Decimal(partial["realized_pnl"]), Decimal("0"))
        self.assertEqual(memecoin_desk.view(case_id)["snapshot"]["reserved_usd"], "8.00")
        self.assertEqual(memecoin_desk.confirm_deposit_refund(case_id, "too-soon")["reason"], "tokens_unsold")
        self.assertEqual(memecoin_desk.view(case_id)["snapshot"]["realized_pnl"], partial["realized_pnl"])
        finished = sell(est - 400, est - 400, 0, 23)
        self.assertEqual(finished["remaining_raw"], 0)
        trading = memecoin_desk.view(case_id)["snapshot"]["realized_pnl"]
        cost_basis = memecoin_desk.view(case_id)["snapshot"]["entry_funding"]["cost_basis_lamports"]
        entry_sig = memecoin_desk.view(case_id)["snapshot"]["entry_funding"]["signature"]
        held_rent = memecoin_desk.view(case_id)["snapshot"]["reserved_usd"]
        self.assertEqual(held_rent, "0.14")
        paper_before = memecoin_desk.loss_totals()["paper_loss_usd"]
        live_before = Decimal(memecoin_desk.loss_totals()["live_loss_usd"])
        close = bytearray(base64.b64decode(solana_tx.encode_legacy(wallet, [wallet, ata, solana_tx.TOKEN_PROGRAM], 1, [(2, [1, 0, 0], bytes([9]))], solana_tx.b58encode(b"\x44" * 32))))
        close[1:65] = b"\x24" * 64
        close_signed = base64.b64encode(bytes(close)).decode()
        close_sig = solana_tx.first_signature(close_signed)
        queue.append({"slot": PRICE_SLOT, "transaction": [close_signed, "base64"], "meta": {
            "err": None, "fee": 5000, "preBalances": [1_000_000_000, rent, 1], "postBalances": [1_000_000_000 + rent - 5000, 0, 1],
        }})
        refunded = memecoin_desk.confirm_deposit_refund(case_id, close_sig)
        self.assertTrue(refunded["ok"], refunded)
        self.assertEqual(refunded["reserved_usd"], "0.00")
        self.assertEqual(refunded["realized_pnl"], trading)
        self.assertTrue(refunded["pnl_live"])
        self.assertEqual(refunded["refund_fee_usd"], "0.01")
        kept = memecoin_desk.view(case_id)["snapshot"]
        self.assertEqual(kept["realized_pnl"], trading)
        self.assertEqual(kept["entry_funding"]["signature"], entry_sig)
        self.assertEqual(kept["entry_funding"]["cost_basis_lamports"], cost_basis)
        self.assertEqual(kept["pnl_source"], "entry_receipt_and_exit_receipt")
        live_after = Decimal(memecoin_desk.loss_totals()["live_loss_usd"])
        self.assertEqual(live_after, live_before + Decimal("0.01"))
        self.assertEqual(memecoin_desk.loss_totals()["paper_loss_usd"], paper_before)
        again = memecoin_desk.confirm_deposit_refund(case_id, close_sig)
        self.assertEqual(again["reason"], "deposit_duplicate")
        self.assertEqual(memecoin_desk.loss_totals()["live_loss_usd"], format(live_after, "f"))
        reloaded = _json.loads(db.query("SELECT snapshot_json FROM memecoin_cases WHERE id = ?", (case_id,))[0]["snapshot_json"])
        self.assertEqual(reloaded["realized_pnl"], trading)
        self.assertEqual(len(reloaded["live_events"]), 1)
        self.assertEqual(db.query("SELECT fee_usd FROM memecoin_refunds WHERE signature = ?", (close_sig,))[0]["fee_usd"], "0.01")

    def test_failed_and_over_cap_closes_record_the_fee_without_erasing_the_trade(self):
        import base64
        import json as _json
        import urllib.request
        from tests.test_jupiter_route_v2 import PRICE_SLOT, WHEN, _metas, _pool, _prices, _snap, _tx
        wallet = phantom_transport.REQUIRED_WALLET
        rent = solana_tx.TOKEN_ACCOUNT_RENT
        queue = []

        def urlopen(req, timeout=8):
            payload = _json.loads(req.data.decode())
            body = {"jsonrpc": "2.0", "result": __import__("solana_inspect").MAINNET_GENESIS if payload["method"] == "getGenesisHash" else queue.pop(0)}

            class _Response:
                status = 200
                def read(self):
                    return _json.dumps(body).encode()
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    return False
            return _Response()

        original = urllib.request.urlopen
        urllib.request.urlopen = urlopen
        self.addCleanup(lambda: setattr(urllib.request, "urlopen", original))

        def position(number):
            case_id = self._analyzed(number)
            _protect(case_id)
            mint = memecoin_desk.view(case_id)["mint"]
            values, accounts = _pool(solana_tx.NATIVE_MINT, mint)
            ata = solana_tx.associated_token_address(wallet, mint, solana_tx.TOKEN_PROGRAM)
            route, est = _amm_route(mint, 1_000_000, "buy")
            owned = accounts.pop(values["dest"])
            values["dest"] = ata
            accounts[ata] = owned
            raw = bytearray(base64.b64decode(_tx(_metas(solana_tx.NATIVE_MINT, mint, values), route, side="buy", fund_mint=mint)))
            raw[1:65] = bytes([number]) * 64
            signed = base64.b64encode(bytes(raw)).decode()
            signature = solana_tx.first_signature(signed)
            self.assertTrue(memecoin_desk.reserve_entry(case_id, signed, _snap(accounts, WHEN), _prices(), WHEN)["ok"])
            decoded = solana_tx.decode_transaction(bytes(raw))
            pre = [0] * len(decoded.static_keys)
            post = [0] * len(decoded.static_keys)
            pre[0] = 20_000_000
            post[0] = 20_000_000 - (1_000_000 + 5000 + rent)
            pre[decoded.static_keys.index(ata)] = 0
            post[decoded.static_keys.index(ata)] = rent
            queue.append({"slot": PRICE_SLOT, "transaction": [signed, "base64"], "meta": {
                "err": None, "fee": 5000, "preBalances": pre, "postBalances": post, "preTokenBalances": [],
                "postTokenBalances": [{"mint": mint, "owner": wallet, "uiTokenAmount": {"amount": str(est)}}],
            }})
            booked = memecoin_desk.confirm_entry(case_id, signature, WHEN)
            self.assertTrue(booked["ok"], booked)
            exit_route, out = _amm_route(mint, est, "exit")
            values, accounts = _pool(mint, solana_tx.NATIVE_MINT)
            owned = accounts.pop(values["source"])
            values["source"] = ata
            accounts[ata] = owned
            raw = bytearray(base64.b64decode(_tx(_metas(mint, solana_tx.NATIVE_MINT, values), exit_route, side="exit")))
            raw[1:65] = bytes([number + 1]) * 64
            signed = base64.b64encode(bytes(raw)).decode()
            signature = solana_tx.first_signature(signed)
            self.assertTrue(memecoin_desk.reserve_exit(case_id, signed, est, _snap(accounts, WHEN), _prices(), WHEN)["ok"])
            decoded = solana_tx.decode_transaction(bytes(raw))
            pre = [0] * len(decoded.static_keys)
            post = [0] * len(decoded.static_keys)
            pre[0] = 5_000_000
            post[0] = 5_000_000 + out
            pre[decoded.static_keys.index(ata)] = rent
            post[decoded.static_keys.index(ata)] = rent
            queue.append({"slot": PRICE_SLOT, "transaction": [signed, "base64"], "meta": {
                "err": None, "fee": 5000, "preBalances": pre, "postBalances": post,
                "preTokenBalances": [{"mint": mint, "owner": wallet, "uiTokenAmount": {"amount": str(est)}}],
                "postTokenBalances": [{"mint": mint, "owner": wallet, "uiTokenAmount": {"amount": "0"}}],
            }})
            closed = memecoin_desk.confirm_exit(case_id, signature, WHEN)
            self.assertEqual(closed["remaining_raw"], 0, closed)
            return case_id, ata, memecoin_desk.view(case_id)["snapshot"]["realized_pnl"]

        def close_tx(ata, mark):
            raw = bytearray(base64.b64decode(solana_tx.encode_legacy(
                wallet, [wallet, ata, solana_tx.TOKEN_PROGRAM], 1, [(2, [1, 0, 0], bytes([9]))], solana_tx.b58encode(b"\x44" * 32),
            )))
            raw[1:65] = bytes([mark]) * 64
            signed = base64.b64encode(bytes(raw)).decode()
            return signed, solana_tx.first_signature(signed)

        first, first_ata, first_pnl = position(150)
        second, second_ata, second_pnl = position(152)
        live_before = Decimal(memecoin_desk.loss_totals()["live_loss_usd"])
        self.assertEqual(db.get_setting("memecoin_entry_halt", ""), "")

        signed, signature = close_tx(first_ata, 30)
        queue.append({"slot": PRICE_SLOT, "transaction": [signed, "base64"], "meta": {
            "err": {"InstructionError": [0, "Custom"]}, "fee": 5000,
            "preBalances": [2_000_000, rent, 1], "postBalances": [2_000_000 - 5000, rent, 1],
        }})
        failed = memecoin_desk.confirm_deposit_refund(first, signature)
        self.assertEqual(failed["reason"], "receipt_failed")
        self.assertTrue(failed["ordinary_revert"])
        self.assertFalse(failed["entry_halt"])
        self.assertTrue(failed["holds"])
        self.assertEqual(failed["realized_pnl"], first_pnl)
        self.assertEqual(memecoin_desk.view(first)["snapshot"]["reserved_usd"], "0.14")
        self.assertEqual(memecoin_desk.loss_totals()["live_loss_usd"], format(live_before + Decimal("0.01"), "f"))
        self.assertEqual(db.get_setting("memecoin_entry_halt", ""), "")
        again = memecoin_desk.confirm_deposit_refund(first, signature)
        self.assertEqual(again["reason"], "deposit_duplicate")
        self.assertEqual(memecoin_desk.loss_totals()["live_loss_usd"], format(live_before + Decimal("0.01"), "f"))
        queue.append(None)
        unknown = memecoin_desk.confirm_deposit_refund(first, "unknown-close")
        self.assertEqual(unknown["reason"], "receipt_unconfirmed")
        self.assertEqual(len(db.query("SELECT signature FROM memecoin_refunds WHERE case_id = ?", (first,))), 1)
        other, _other_sig = close_tx(solana_tx.associated_token_address(wallet, solana_tx.NATIVE_MINT, solana_tx.TOKEN_PROGRAM), 31)
        queue.append({"slot": PRICE_SLOT, "transaction": [other, "base64"], "meta": {
            "err": {"InstructionError": [0, "Custom"]}, "fee": 5000,
            "preBalances": [2_000_000, rent, 1], "postBalances": [2_000_000 - 5000, rent, 1],
        }})
        mismatched = memecoin_desk.confirm_deposit_refund(first, solana_tx.first_signature(other))
        self.assertEqual(mismatched["reason"], "refund_not_to_wallet")
        self.assertEqual(len(db.query("SELECT signature FROM memecoin_refunds")), 1)
        signed, signature = close_tx(first_ata, 35)
        queue.append({"slot": PRICE_SLOT, "transaction": [signed, "base64"], "meta": {
            "err": {"InstructionError": [0, "Custom"]}, "fee": 5000,
            "preBalances": [2_000_000, rent, 1], "postBalances": [2_000_000 - 5000, rent, 6],
        }})
        anomaly = memecoin_desk.confirm_deposit_refund(first, signature)
        self.assertFalse(anomaly["ordinary_revert"])
        self.assertTrue(anomaly["entry_halt"])
        self.assertTrue(anomaly["holds"])
        self.assertEqual(anomaly["realized_pnl"], first_pnl)
        self.assertEqual(memecoin_desk.view(first)["snapshot"]["reserved_usd"], "0.14")
        self.assertEqual(db.get_setting("memecoin_entry_halt"), "1")

        signed, signature = close_tx(second_ata, 32)
        queue.append({"slot": PRICE_SLOT, "transaction": [signed, "base64"], "meta": {
            "err": None, "fee": 2_000_000,
            "preBalances": [5_000_000, rent, 1], "postBalances": [5_000_000 + rent - 2_000_000, 0, 1],
        }})
        opened = memecoin_desk.confirm_deposit_refund(second, signature)
        self.assertEqual(opened["reason"], "fee_policy_breach")
        self.assertTrue(opened["refunded"])
        self.assertEqual(opened["reserved_usd"], "0.00")
        self.assertEqual(opened["realized_pnl"], second_pnl)
        self.assertGreater(Decimal(opened["refund_fee_usd"]), Decimal("0.05"))
        self.assertEqual(db.get_setting("memecoin_entry_halt"), "1")
        after_success = memecoin_desk.loss_totals()["live_loss_usd"]

        signed, signature = close_tx(first_ata, 33)
        queue.append({"slot": PRICE_SLOT, "transaction": [signed, "base64"], "meta": {
            "err": {"InstructionError": [0, "Custom"]}, "fee": 2_000_000,
            "preBalances": [9_000_000, rent, 1], "postBalances": [9_000_000 - 2_000_000, rent, 1],
        }})
        over = memecoin_desk.confirm_deposit_refund(first, signature)
        self.assertTrue(over["fee_policy_breach"])
        self.assertFalse(over["refunded"])
        self.assertEqual(over["realized_pnl"], first_pnl)
        self.assertEqual(memecoin_desk.view(first)["snapshot"]["reserved_usd"], "0.14")
        self.assertEqual(db.get_setting("memecoin_entry_halt"), "1")
        signed, signature = close_tx(first_ata, 34)
        queue.append({"slot": PRICE_SLOT, "transaction": [signed, "base64"], "meta": {
            "err": None, "fee": 5000,
            "preBalances": [4_000_000, rent, 1], "postBalances": [4_000_000 + rent - 5000, 0, 1],
        }})
        released = memecoin_desk.confirm_deposit_refund(first, signature)
        self.assertTrue(released["refunded"])
        self.assertFalse(released["entry_halt"])
        self.assertEqual(released["realized_pnl"], first_pnl)
        self.assertEqual(db.get_setting("memecoin_entry_halt"), "1")
        self.assertEqual(memecoin_desk.view(first)["snapshot"]["reserved_usd"], "0.00")
        fees = db.query("SELECT fee_usd, released FROM memecoin_refunds WHERE case_id = ? ORDER BY rowid", (first,))
        self.assertEqual([item["fee_usd"] for item in fees], ["0.01", "0.01", fees[2]["fee_usd"], "0.01"])
        self.assertEqual([item["released"] for item in fees], [0, 0, 0, 1])
        self.assertGreater(Decimal(fees[2]["fee_usd"]), Decimal("0.05"))
        self.assertGreater(Decimal(memecoin_desk.loss_totals()["live_loss_usd"]), Decimal(after_success))
        reloaded = _json.loads(db.query("SELECT snapshot_json FROM memecoin_cases WHERE id = ?", (first,))[0]["snapshot_json"])
        self.assertEqual(reloaded["realized_pnl"], first_pnl)
        self.assertTrue(reloaded["pnl_live"])

    def test_changed_preexisting_and_extra_accounts_do_not_book_a_clean_fill(self):
        import base64
        import json as _json
        import urllib.request
        from tests.test_jupiter_route_v2 import PRICE_SLOT, WHEN, _metas, _pool, _prices, _route, _snap, _tx
        wallet = phantom_transport.REQUIRED_WALLET
        rent = solana_tx.TOKEN_ACCOUNT_RENT
        queue = []

        def urlopen(req, timeout=8):
            payload = _json.loads(req.data.decode())
            body = {"jsonrpc": "2.0", "result": __import__("solana_inspect").MAINNET_GENESIS if payload["method"] == "getGenesisHash" else queue.pop(0)}

            class _Response:
                status = 200
                def read(self):
                    return _json.dumps(body).encode()
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    return False
            return _Response()

        original = urllib.request.urlopen
        urllib.request.urlopen = urlopen
        self.addCleanup(lambda: setattr(urllib.request, "urlopen", original))

        prepared = []

        def land(number, adjust):
            case_id = self._analyzed(number)
            _protect(case_id)
            mint = memecoin_desk.view(case_id)["mint"]
            values, accounts = _pool(solana_tx.NATIVE_MINT, mint)
            ata = solana_tx.associated_token_address(wallet, mint, solana_tx.TOKEN_PROGRAM)
            owned = accounts.pop(values["dest"])
            values["dest"] = ata
            accounts[ata] = owned
            route, est = _amm_route(mint, 1_000_000, "buy")
            raw = bytearray(base64.b64decode(_tx(_metas(solana_tx.NATIVE_MINT, mint, values), route, side="buy")))
            raw[1:65] = bytes([number]) + b"\x31" * 63
            signed = base64.b64encode(bytes(raw)).decode()
            signature = solana_tx.first_signature(signed)
            self.assertTrue(memecoin_desk.reserve_entry(case_id, signed, _snap(accounts, WHEN), _prices(), WHEN)["ok"])
            decoded = solana_tx.decode_transaction(bytes(raw))
            pre = [0] * len(decoded.static_keys)
            post = [0] * len(decoded.static_keys)
            pre[0] = 20_000_000
            post[0] = 20_000_000 - (1_000_000 + 5000)
            ata_index = decoded.static_keys.index(ata)
            pre[ata_index] = rent
            post[ata_index] = rent
            wsol = solana_tx.associated_token_address(wallet, solana_tx.NATIVE_MINT, solana_tx.TOKEN_PROGRAM)
            adjust(decoded.static_keys, pre, post, ata_index, decoded.static_keys.index(wsol))
            landed = {"slot": PRICE_SLOT, "transaction": [signed, "base64"], "meta": {
                "err": None, "fee": 5000, "preBalances": pre, "postBalances": post,
                "preTokenBalances": [{"mint": mint, "owner": wallet, "uiTokenAmount": {"amount": "0"}}],
                "postTokenBalances": [{"mint": mint, "owner": wallet, "uiTokenAmount": {"amount": str(est)}}],
            }}
            prepared.append((case_id, signature, landed))
            return case_id

        clean_id = land(120, lambda keys, pre, post, ata_index, wsol_index: None)
        land(121, lambda keys, pre, post, ata_index, wsol_index: post.__setitem__(ata_index, pre[ata_index] + 10))
        land(122, lambda keys, pre, post, ata_index, wsol_index: post.__setitem__(ata_index, pre[ata_index] - 20))
        def bump(keys, pre, post, ata_index, wsol_index):
            for index in range(len(keys)):
                if index not in {0, ata_index, wsol_index}:
                    post[index] = pre[index] + 15
                    return
        land(123, bump)
        def dip(keys, pre, post, ata_index, wsol_index):
            for index in range(len(keys) - 1, -1, -1):
                if index not in {0, ata_index, wsol_index}:
                    post[index] = 4
                    pre[index] = 11
                    return
        land(124, dip)
        results = []
        for case_id, signature, landed in prepared:
            queue.append(landed)
            results.append(memecoin_desk.confirm_entry(case_id, signature, WHEN))
        clean = results[0]
        self.assertTrue(clean["ok"], clean)
        self.assertEqual(clean["deposit_lamports"], 0)
        self.assertTrue(memecoin_desk.view(clean_id)["snapshot"]["deposits"]["mint_ata"]["preexisting"])
        self.assertFalse(memecoin_desk.view(clean_id)["snapshot"]["deposits"]["mint_ata"]["bot_funded"])

        def expect_held(result):
            self.assertEqual(result["reason"], "unexplained_lamports", result)
            self.assertTrue(result["holds"])
            self.assertTrue(result["entry_halt"])
            self.assertIsNone(result["proceeds"])
            self.assertIsNone(result["qty"])
            self.assertLess(Decimal(result["realized_pnl"]), Decimal("0"))
            self.assertTrue(result["accounts"])

        for result in results[1:]:
            expect_held(result)
        refused = results[-1]
        self.assertNotEqual(refused["accounts"][0]["index"], 0)
        self.assertEqual(db.get_setting("memecoin_entry_halt"), "1")

    def test_an_ordinary_revert_counts_its_fee_without_a_permanent_halt(self):
        import base64
        import json as _json
        import urllib.request
        from tests.test_jupiter_route_v2 import PRICE_SLOT, WHEN, _metas, _pool, _prices, _route, _snap, _tx
        wallet = phantom_transport.REQUIRED_WALLET
        queue = []

        def urlopen(req, timeout=8):
            payload = _json.loads(req.data.decode())
            body = {"jsonrpc": "2.0", "result": __import__("solana_inspect").MAINNET_GENESIS if payload["method"] == "getGenesisHash" else queue.pop(0)}

            class _Response:
                status = 200
                def read(self):
                    return _json.dumps(body).encode()
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    return False
            return _Response()

        original = urllib.request.urlopen
        urllib.request.urlopen = urlopen
        self.addCleanup(lambda: setattr(urllib.request, "urlopen", original))

        def case(number):
            mint = solana_tx.b58encode(number.to_bytes(4, "little") + b"\xab" * 28)
            opened = memecoin_desk.scout(_pair(mint), now=NOW)
            self.assertTrue(opened["ok"], opened)
            arm_clean_creator(mint)
            self.assertTrue(memecoin_desk.filter_case(opened["case_id"], _onchain(cross_sources=_passing_cross(mint)), now=NOW)["ok"])
            snap = json.loads(db.query("SELECT snapshot_json FROM memecoin_cases WHERE id = ?", (opened["case_id"],))[0]["snapshot_json"])
            snap["fixture"] = True
            snap["analyst_scored"] = False
            snap["smart_wallets_scored"] = False
            db.execute(
                "UPDATE memecoin_cases SET stage = 'analyst_passed', snapshot_json = ? WHERE id = ? AND stage = 'filter_passed'",
                (json.dumps(snap, sort_keys=True), opened["case_id"]),
            )
            return opened["case_id"]

        def revert(number, fee, alter=None):
            case_id = case(number)
            protected = _protect(case_id)
            if protected.get("ok") is not True:
                return protected
            mint = memecoin_desk.view(case_id)["mint"]
            values, accounts = _pool(solana_tx.NATIVE_MINT, mint)
            route, est = _amm_route(mint, 1_000_000, "buy")
            raw = bytearray(base64.b64decode(_tx(_metas(solana_tx.NATIVE_MINT, mint, values), route, side="buy")))
            raw[1:65] = bytes([number % 250]) + bytes([number // 250]) + b"\x41" * 62
            signed = base64.b64encode(bytes(raw)).decode()
            signature = solana_tx.first_signature(signed)
            reserved = memecoin_desk.reserve_entry(case_id, signed, _snap(accounts, WHEN), _prices(), WHEN)
            if reserved.get("ok") is not True:
                return reserved
            decoded = solana_tx.decode_transaction(bytes(raw))
            pre = [1] * len(decoded.static_keys)
            post = list(pre)
            pre[0] = 5_000_000
            post[0] = 5_000_000 - fee
            if alter is not None:
                alter(decoded, pre, post)
            queue.append({"slot": PRICE_SLOT, "transaction": [signed, "base64"], "meta": {
                "err": {"InstructionError": [0, "Custom"]}, "fee": fee, "preBalances": pre, "postBalances": post,
                "preTokenBalances": [], "postTokenBalances": [],
            }})
            return memecoin_desk.confirm_entry(case_id, signature, WHEN)

        held_for_later = []

        def reserve_only(number, fee, alter=None):
            case_id = case(number)
            self.assertTrue(_protect(case_id)["ok"])
            mint = memecoin_desk.view(case_id)["mint"]
            values, accounts = _pool(solana_tx.NATIVE_MINT, mint)
            route, est = _amm_route(mint, 1_000_000, "buy")
            raw = bytearray(base64.b64decode(_tx(_metas(solana_tx.NATIVE_MINT, mint, values), route, side="buy")))
            raw[1:65] = bytes([number % 250]) + bytes([7]) + bytes([number // 250]) + b"\x41" * 61
            signed = base64.b64encode(bytes(raw)).decode()
            signature = solana_tx.first_signature(signed)
            self.assertTrue(memecoin_desk.reserve_entry(case_id, signed, _snap(accounts, WHEN), _prices(), WHEN)["ok"])
            decoded = solana_tx.decode_transaction(bytes(raw))
            pre = [1] * len(decoded.static_keys)
            post = list(pre)
            pre[0] = 5_000_000
            post[0] = 5_000_000 - fee
            if alter is not None:
                alter(decoded, pre, post)
            landed = {"slot": PRICE_SLOT, "transaction": [signed, "base64"], "meta": {
                "err": {"InstructionError": [0, "Custom"]}, "fee": fee, "preBalances": pre, "postBalances": post,
                "preTokenBalances": [], "postTokenBalances": [],
            }}
            held_for_later.append((case_id, signature, landed))

        first = revert(130, 5000)
        self.assertTrue(first["ordinary_revert"], first)
        self.assertFalse(first["entry_halt"])
        self.assertFalse(first["holds"])
        self.assertEqual(first["fee_usd"], "0.01")
        self.assertEqual(db.get_setting("memecoin_entry_halt", ""), "")
        self.assertTrue(_protect(case(131))["ok"])
        reserve_only(542, 5000, alter=lambda decoded, pre, post: post.__setitem__(1, pre[1] + 10))
        reserve_only(543, 5000)
        self.assertEqual(memecoin_desk.loss_totals()["live_halts_new_entries"], False)
        for number in range(140, 540):
            result = revert(number, 500_000)
            self.assertTrue(result.get("ordinary_revert"), result)
        self.assertEqual(db.get_setting("memecoin_entry_halt", ""), "")
        self.assertGreaterEqual(Decimal(memecoin_desk.loss_totals()["live_loss_usd"]), Decimal("20.00"))
        self.assertEqual(_protect(case(541))["reason"], "stop_new_entries")
        anomaly_case, anomaly_sig, anomaly_landed = held_for_later[0]
        queue.append(anomaly_landed)
        anomaly = memecoin_desk.confirm_entry(anomaly_case, anomaly_sig, WHEN)
        self.assertEqual(anomaly["reason"], "failed_balance_unproven")
        self.assertTrue(anomaly["holds"])
        self.assertTrue(anomaly["entry_halt"])
        self.assertEqual(db.get_setting("memecoin_entry_halt"), "1")
        later_case, later_sig, later_landed = held_for_later[1]
        queue.append(later_landed)
        later = memecoin_desk.confirm_entry(later_case, later_sig, WHEN)
        self.assertTrue(later.get("ordinary_revert"), later)
        self.assertFalse(later["entry_halt"])
        self.assertEqual(db.get_setting("memecoin_entry_halt"), "1")
        self.assertEqual(_protect(case(544))["reason"], "entry_halt")

    def test_a_tighter_route_clears_a_nondivisible_pool_estimate(self):
        import base64
        import json as _json
        import urllib.request
        from tests.test_jupiter_route_v2 import PRICE_SLOT, WHEN, _metas, _pool, _prices, _route, _snap, _tx
        import desk_loop
        import jupiter_route_v2
        import jupiter_swap_v2
        import solana_jupiter
        wallet = phantom_transport.REQUIRED_WALLET
        rent = solana_tx.TOKEN_ACCOUNT_RENT
        queue = []

        def urlopen(req, timeout=8):
            payload = _json.loads(req.data.decode())
            if payload["method"] == "getGenesisHash":
                body = {"jsonrpc": "2.0", "result": __import__("solana_inspect").MAINNET_GENESIS}
            else:
                body = {"jsonrpc": "2.0", "result": queue.pop(0)}

            class _Response:
                status = 200
                def read(self):
                    return _json.dumps(body).encode()
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    return False
            return _Response()

        original = urllib.request.urlopen
        urllib.request.urlopen = urlopen
        self.addCleanup(lambda: setattr(urllib.request, "urlopen", original))
        case_id = self._analyzed(91)
        _protect(case_id)
        mint = memecoin_desk.view(case_id)["mint"]
        raw_idl = _json.loads(Path("tests/fixtures/jupiter_program_idl.json").read_text(encoding="utf-8"))
        route_args = [arg["name"] for arg in next(item for item in raw_idl["instructions"] if item["name"] == "route_v2")["args"]]
        self.assertNotIn("min_out", route_args)
        self.assertNotIn("other_amount_threshold", route_args)
        values, accounts = _pool(solana_tx.NATIVE_MINT, mint)
        probe = jupiter_route_v2.decode_message(
            _tx(_metas(solana_tx.NATIVE_MINT, mint, values), _route(1, 0, True, amount=1_000_000), side="buy"),
            _snap(accounts, WHEN), side="buy", mint=mint, now=WHEN, slippage_allowance_bps=0, price_transport=_prices(),
        )
        est = probe["amm_est_out"]
        self.assertEqual(est, 999_899)
        request = jupiter_route_v2.quote_request(est)
        self.assertEqual(request["slippage_bps"], 49)
        self.assertEqual(request["quoted_out_amount"], est)
        self.assertGreaterEqual(request["min_out"], request["owner_floor"])
        url = jupiter_swap_v2.build_url(solana_tx.NATIVE_MINT, mint, "1000000", slippage_bps=request["slippage_bps"])
        self.assertIn("slippageBps=49", url)
        self.assertNotIn("slippageBps=50", url)
        ata = solana_tx.associated_token_address(wallet, mint, solana_tx.TOKEN_PROGRAM)
        owned = accounts.pop(values["dest"])
        values["dest"] = ata
        accounts[ata] = owned

        def sign(route, nonce):
            unsigned = _tx(_metas(solana_tx.NATIVE_MINT, mint, values), route, side="buy", fund_mint=mint)
            raw = bytearray(base64.b64decode(unsigned))
            raw[1:65] = bytes([nonce]) + b"\x61" * 63
            return base64.b64encode(bytes(raw)).decode()

        loose = sign(_route(est, 50, True, amount=1_000_000), 11)
        refused = memecoin_desk.reserve_entry(case_id, loose, _snap(accounts, WHEN), _prices(), WHEN)
        self.assertEqual(refused["reason"], "min_below_amm", refused)
        raised = sign(_route(est + 1, 49, True, amount=1_000_000), 12)
        self.assertEqual(memecoin_desk.reserve_entry(case_id, raised, _snap(accounts, WHEN), _prices(), WHEN)["reason"], "quoted_above_amm")
        wider = sign(_route(est, 51, True, amount=1_000_000), 13)
        self.assertEqual(memecoin_desk.reserve_entry(case_id, wider, _snap(accounts, WHEN), _prices(), WHEN)["reason"], "slippage_exceeds")
        signed = sign(_route(request["quoted_out_amount"], request["slippage_bps"], True, amount=1_000_000), 14)
        decoded_route = solana_tx.decode_transaction(base64.b64decode(signed))
        swap = next(item for item in decoded_route.instructions if decoded_route.static_keys[item.program_index] == solana_jupiter.PROGRAM_ID)
        parsed = jupiter_route_v2.parse(bytes(swap.data))
        self.assertEqual(parsed["slippage_bps"], 49)
        self.assertEqual(parsed["quoted_out_amount"], est)
        self.assertEqual(parsed["min_out"], request["min_out"])
        reserved = memecoin_desk.reserve_entry(case_id, signed, _snap(accounts, WHEN), _prices(), WHEN)
        self.assertTrue(reserved["ok"], reserved)
        self.assertEqual(reserved["instruction_slippage_bps"], 49)
        self.assertEqual(reserved["quoted_out_amount"], est)
        self.assertEqual(reserved["amm_est_out"], est)
        self.assertGreaterEqual(reserved["min_out"], reserved["owner_min_out"])
        self.assertEqual(reserved["max_slippage_bps"], 50)
        self.assertFalse(reserved["effectual_entry"])
        self.assertIn("lp_fields_unpublished", reserved["effectual_blocked_by"])
        self.assertIn("quote_expiry_unproven", reserved["effectual_blocked_by"])
        self.assertFalse(reserved["send"])
        self.assertFalse(reserved["live_ready"])
        decoded = solana_tx.decode_transaction(base64.b64decode(signed))
        pre = [0] * len(decoded.static_keys)
        post = [0] * len(decoded.static_keys)
        pre[0] = 200_000_000
        post[0] = 200_000_000 - (1_000_000 + 5000 + rent)
        ata_index = decoded.static_keys.index(ata)
        post[ata_index] = rent
        signature = solana_tx.first_signature(signed)
        queue.append({
            "slot": PRICE_SLOT, "blockTime": int(WHEN), "transaction": [signed, "base64"],
            "meta": {
                "err": None, "fee": 5000, "preBalances": pre, "postBalances": post,
                "preTokenBalances": [],
                "postTokenBalances": [{"mint": mint, "owner": wallet, "uiTokenAmount": {"amount": str(est)}}],
            },
        })
        booked = memecoin_desk.confirm_entry(case_id, signature, WHEN)
        self.assertTrue(booked["ok"], booked)
        self.assertEqual(booked["qty"], est)
        self.assertFalse(booked["effectual_entry"])
        self.assertFalse(booked["quote_expiry_proven"])
        out_values, out_accounts = _pool(mint, solana_tx.NATIVE_MINT)
        out_owned = out_accounts.pop(out_values["source"])
        out_values["source"] = ata
        out_accounts[ata] = out_owned
        exit_probe = jupiter_route_v2.decode_message(
            _tx(_metas(mint, solana_tx.NATIVE_MINT, out_values), _route(1, 0, True, amount=est), side="exit"),
            _snap(out_accounts, WHEN), side="exit", mint=mint, now=WHEN, slippage_allowance_bps=0, price_transport=_prices(),
        )
        exit_est = exit_probe["amm_est_out"]
        self.assertIsInstance(exit_est, int)
        exit_request = jupiter_route_v2.quote_request(exit_est)
        self.assertLessEqual(exit_request["slippage_bps"], 50)
        self.assertGreaterEqual(exit_request["min_out"], exit_request["owner_floor"])
        loose_exit = _tx(_metas(mint, solana_tx.NATIVE_MINT, out_values), _route(exit_est, 50, True, amount=est), side="exit")
        if jupiter_route_v2.minimum_out(exit_est, 50) < exit_request["owner_floor"]:
            loose_raw = bytearray(base64.b64decode(loose_exit))
            loose_raw[1:65] = b"\x72" * 64
            loose_signed = base64.b64encode(bytes(loose_raw)).decode()
            self.assertEqual(
                memecoin_desk.reserve_exit(case_id, loose_signed, est, _snap(out_accounts, WHEN), _prices(), WHEN)["reason"],
                "min_below_amm",
            )
        exit_unsigned = _tx(
            _metas(mint, solana_tx.NATIVE_MINT, out_values),
            _route(exit_request["quoted_out_amount"], exit_request["slippage_bps"], True, amount=est),
            side="exit",
        )
        exit_raw = bytearray(base64.b64decode(exit_unsigned))
        exit_raw[1:65] = b"\x73" * 64
        exit_signed = base64.b64encode(bytes(exit_raw)).decode()
        exit_reserved = memecoin_desk.reserve_exit(case_id, exit_signed, est, _snap(out_accounts, WHEN), _prices(), WHEN)
        self.assertTrue(exit_reserved["ok"], exit_reserved)
        self.assertEqual(exit_reserved["instruction_slippage_bps"], exit_request["slippage_bps"])
        self.assertEqual(exit_reserved["quoted_out_amount"], exit_est)
        self.assertGreaterEqual(exit_reserved["min_out"], exit_request["owner_floor"])
        self.assertFalse(exit_reserved["send"])
        exit_decoded = solana_tx.decode_transaction(bytes(exit_raw))
        pre = [0] * len(exit_decoded.static_keys)
        post = [0] * len(exit_decoded.static_keys)
        pre[0] = 5_000_000
        post[0] = 5_000_000 + exit_est
        pre[exit_decoded.static_keys.index(ata)] = rent
        post[exit_decoded.static_keys.index(ata)] = rent
        queue.append({
            "slot": PRICE_SLOT, "transaction": [exit_signed, "base64"],
            "meta": {
                "err": None, "fee": 5000, "preBalances": pre, "postBalances": post,
                "preTokenBalances": [{"mint": mint, "owner": wallet, "uiTokenAmount": {"amount": str(est)}}],
                "postTokenBalances": [{"mint": mint, "owner": wallet, "uiTokenAmount": {"amount": "0"}}],
            },
        })
        closed = memecoin_desk.confirm_exit(case_id, solana_tx.first_signature(exit_signed), WHEN)
        self.assertTrue(closed["ok"], closed)
        self.assertFalse(closed.get("live_ready", False))
        blocked = desk_loop.readiness()["blocked_because"]
        self.assertIn("stalkchain_response_fields_for_lp_rug_growth_are_unpublished", blocked)
        self.assertNotIn("owner_slippage_unapproved", blocked)
        self.assertFalse(desk_loop.readiness()["live_ready"])
        self.assertFalse(desk_loop.readiness()["send"])
        self.assertFalse(desk_loop.readiness()["sign"])
        reasons = desk_loop.scheduler_status()["ready_because"]
        self.assertIn("lp_fields_unpublished", reasons)
        self.assertIn("rug_history_unpublished", reasons)
        self.assertIn("holder_growth_unproven", reasons)
        self.assertNotIn("owner_slippage_unapproved", reasons)
        self.assertFalse(desk_loop.scheduler_status()["armed"])
