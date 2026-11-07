"""API path for one direct SOL/USDC prepare. No socket, no signer, no broadcast."""
import json
import subprocess
import unittest
from decimal import Decimal
from pathlib import Path

import db
import execution_transport
import phantom_auth
import phantom_runner
import phantom_transport
import solana_direct_swap
import solana_read_rpc
import spend_policy
from fastapi.testclient import TestClient
from tests.support import temp_desk_env
from tests.whirlpool_fixture import FakeTransport, FixtureRpc

TOKEN = "dummy-app-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SIGNATURE = "1" * 64


class DirectApiTests(unittest.TestCase):
    def setUp(self):
        _tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        previous = __import__("os").environ.get("APP_TOKEN")
        __import__("os").environ["APP_TOKEN"] = TOKEN
        self.addCleanup(lambda: __import__("os").environ.pop("APP_TOKEN") if previous is None else __import__("os").environ.__setitem__("APP_TOKEN", previous))
        db.init_db()
        phantom_auth.reset_for_tests()
        self.addCleanup(phantom_auth.reset_for_tests)
        execution_transport.STATE_RPC = None
        solana_read_rpc.TRANSPORT = None
        phantom_runner.CHAIN_STATUS = None
        self.addCleanup(lambda: setattr(execution_transport, "STATE_RPC", None))
        self.addCleanup(lambda: setattr(solana_read_rpc, "TRANSPORT", None))
        self.addCleanup(lambda: setattr(phantom_runner, "CHAIN_STATUS", None))
        self.transport = FakeTransport()
        import server
        self.client = TestClient(server.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.rpc = FixtureRpc()

    def test_policy_prepare_reserve_and_chain_hold(self):
        self.assertEqual(db.get_setting("mode"), "PAPER")
        self.assertEqual(db.get_setting("agents_enabled"), "0")
        self.assertTrue(phantom_runner.halted())
        self.assertNotIn("fixtures", Path(solana_direct_swap.__file__).read_text(encoding="utf-8"))
        injected = self.client.post(
            "/api/phantom/financial-policy",
            headers=AUTH,
            json={"approved": True, "owner_approved": True, "max_fee_usd": "0.01", "fx_slot": 1},
        )
        self.assertEqual(injected.status_code, 400)
        self.assertFalse(spend_policy.fee_view()["approved"])
        started = []
        original = subprocess.run

        def boom(*_args, **_kwargs):
            started.append(True)
            raise AssertionError("cli")

        subprocess.run = boom
        try:
            raw = self.client.post("/api/phantom/direct-prepare", headers=AUTH, json={"transaction": "aaaa", "sign": True})
            self.assertEqual(raw.status_code, 400)
            self.assertEqual(started, [])
            self.assertEqual(self.rpc.calls, 0)
            cold = self.client.post("/api/phantom/direct-prepare", headers=AUTH, json={})
        finally:
            subprocess.run = original
        self.assertEqual(cold.status_code, 200)
        self.assertEqual(cold.json()["reason"], "reads_disabled")
        self.assertFalse(cold.json()["fetched"])
        self.assertEqual(self.transport.payloads, [])
        self.assertFalse(cold.json()["sign"])
        self.assertFalse(cold.json()["broadcastable"])
        self.assertFalse(cold.json()["runtime_ready"])
        approved = self.client.post("/api/phantom/financial-policy", headers=AUTH, json={"approved": True})
        self.assertEqual(approved.status_code, 200)
        self.assertTrue(approved.json()["fee_approved"])
        self.assertTrue(approved.json()["aggregate_approved"])
        self.assertFalse(approved.json()["spec"]["recorded"])
        self.assertFalse(approved.json()["live_ready"])
        self.assertNotIn("fx_slot", spend_policy._read(spend_policy._FEE))
        solana_read_rpc.TRANSPORT = self.transport
        solana_read_rpc.enable_reads()
        subprocess.run = boom
        try:
            prepared = self.client.post("/api/phantom/direct-prepare", headers=AUTH, json={})
        finally:
            subprocess.run = original
        self.assertEqual(prepared.status_code, 200, prepared.text)
        body = prepared.json()
        self.assertEqual(body["rpc_methods"], [
            "getGenesisHash", "getMultipleAccounts", "getMinimumBalanceForRentExemption",
            "getLatestBlockhash", "getFeeForMessage", "isBlockhashValid", "getBlockHeight",
        ])
        self.assertEqual(started, [])
        self.assertEqual(body["entry_lamports"], 88979314)
        self.assertEqual(body["quote_token_est_out"], 7995630)
        self.assertEqual(body["min_out"], 7915673)
        self.assertEqual(len(body["unsigned_tx_sha256"]), 64)
        self.assertNotIn("unsigned_tx", body)
        self.assertFalse(body["sign"])
        self.assertFalse(body["cli"])
        self.assertFalse(body["broadcastable"])
        self.assertFalse(body["runtime_ready"])
        self.assertFalse(body["fx_bound_to_policy"])
        self.assertFalse(body["placeholder"])
        self.assertFalse(body["slot_is_ttl"])
        self.assertNotEqual(body["blockhash"], solana_direct_swap.PLACEHOLDER_BLOCKHASH)
        self.assertEqual(body["strategies_enabled"], [])
        self.assertEqual(body["network_fee_lamports"], 6000)
        self.assertFalse(body["assumed_signature_fee"])
        self.assertGreater(body["dex_fee_micro_usdc"], 0)
        self.assertEqual(body["fee_micro_usdc"], body["dex_fee_micro_usdc"] + body["network_fee_micro_usdc"])
        self.assertEqual(
            body["exposure_micro_usdc"],
            body["marginal_micro_usdc"] + body["network_fee_micro_usdc"] + body["rent_micro_usdc"],
        )
        self.assertEqual(body["exit_plan_raw"], 7995630)
        self.assertEqual(body["exit_quote_out"], 88882136)
        self.assertFalse(body["exit_closes_usdc"])
        self.assertEqual(len(body["exit_tx_sha256"]), 64)
        self.assertNotIn("_entry_tx", body)
        self.assertNotIn("_exit_tx", body)
        self.assertTrue(body["fee_cap_ok"])
        self.assertFalse(body["priority_only"])
        self.assertFalse(body["rent_is_fee"])
        self.assertLessEqual(body["marginal_micro_usdc"], 8_000_000)
        self.assertGreater(body["exposure_micro_usdc"], body["marginal_micro_usdc"])
        self.assertLessEqual(body["exposure_micro_usdc"], 100_000_000)
        self.assertIsNone(body["exit_close_raw"])
        self.assertNotEqual(body["quote_token_est_out"], body["wallet_usdc_raw"])
        self.assertEqual(body["wallet_usdc_raw"], 188718141)
        self.assertFalse(solana_direct_swap.exit_amount(188718141, 188718141, confirmed=True)["allowed"])
        self.assertEqual(
            solana_direct_swap.exit_amount(body["quote_token_est_out"], body["wallet_usdc_raw"], confirmed=True)["exit_raw"],
            body["quote_token_est_out"],
        )
        exit_state = solana_direct_swap.fetch_state(self.rpc)
        exit_arrays = solana_direct_swap._quote_arrays(exit_state["pool"], exit_state["pool_data"], exit_state["accounts"], False)
        exit_quote = solana_direct_swap.quote_exit_exact_in(
            exit_state["state"], exit_arrays, body["quote_token_est_out"], 100, exit_state["context_slot"],
        )
        self.assertEqual(exit_quote["token_in"], 7995630)
        self.assertNotEqual(exit_quote["token_in"], 188718141)
        self.assertEqual(exit_quote["token_est_out"], 88882136)
        later = FakeTransport(slot=466547241)
        solana_read_rpc.TRANSPORT = later
        again = self.client.post("/api/phantom/direct-prepare", headers=AUTH, json={})
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()["context_slot"], 466547241)
        self.assertTrue(spend_policy.fee_view()["approved"])
        self.assertNotIn("fx_slot", spend_policy._read(spend_policy._FEE))
        solana_read_rpc.TRANSPORT = self.transport
        held = self.client.post("/api/phantom/direct-reserve", headers=AUTH, json={"intent_id": "entry-1"})
        self.assertEqual(held.status_code, 409)
        self.assertEqual(held.json()["reason"], "kill_switch")
        self.assertEqual(db.query("SELECT intent_id FROM phantom_reservations"), [])
        phantom_runner.set_halt({"halted": False})
        reserved = self.client.post("/api/phantom/direct-reserve", headers=AUTH, json={"intent_id": "entry-1"})
        self.assertEqual(reserved.status_code, 200, reserved.text)
        self.assertEqual(reserved.json()["status"], "sim_open")
        self.assertEqual(reserved.json()["network_fee_lamports"], 6000)
        self.assertEqual(reserved.json()["exit_plan_raw"], 7995630)
        self.assertFalse(reserved.json()["exit_closes_usdc"])
        self.assertEqual(len(reserved.json()["message_sha256"]), 64)
        self.assertEqual(reserved.json()["rpc_methods"].count("simulateTransaction"), 1)
        self.assertEqual(reserved.json()["simulation"]["simulated"], True)
        self.assertFalse(reserved.json()["simulation"]["broadcastable"])
        sent = [item for item in self.transport.payloads if item["method"] == "simulateTransaction"]
        self.assertEqual(sent[-1]["params"][1]["sigVerify"], False)
        self.assertEqual(sent[-1]["params"][1]["replaceRecentBlockhash"], False)
        self.assertNotIn("sendTransaction", reserved.json()["rpc_methods"])
        self.assertEqual(reserved.json()["entry_lamports"], 88979314)
        self.assertLessEqual(reserved.json()["marginal_micro_usdc"], 8_000_000)
        self.assertIsNone(reserved.json()["exit_close_raw"])
        self.assertFalse(reserved.json()["sign"])
        repeat = self.client.post("/api/phantom/direct-reserve", headers=AUTH, json={"intent_id": "entry-1"})
        self.assertFalse(repeat.json()["created"])
        self.assertEqual(db.query("SELECT COUNT(*) AS n FROM phantom_reservations WHERE intent_id = 'entry-1'")[0]["n"], 1)
        fetched = solana_direct_swap.fetch_state(self.rpc)
        unsigned = solana_direct_swap.build(
            fetched["pool"], fetched["pool_data"], fetched["accounts"], 100,
            solana_direct_swap.MIN_SQRT_PRICE + 1, solana_direct_swap.PLACEHOLDER_BLOCKHASH,
            fetched["context_slot"], amount=reserved.json()["entry_lamports"],
        )
        proved = execution_transport.decide("entry-1", unsigned, direct_state=fetched)
        self.assertTrue(proved["checks"]["slippage_verified"])
        self.assertTrue(proved["checks"]["spend_verified"])
        self.assertFalse(proved["checks"]["fee_unbound_clear"])
        self.assertFalse(proved["checks"]["blockhash_checked"])
        fetched["network_fee_lamports"] = 6000
        fetched["rent_lamports"] = 1488440
        priced = solana_direct_swap.build(
            fetched["pool"], fetched["pool_data"], fetched["accounts"], 100,
            solana_direct_swap.MIN_SQRT_PRICE + 1, self.transport.blockhash,
            fetched["context_slot"], amount=reserved.json()["entry_lamports"],
        )
        checked = execution_transport.decide("entry-1", priced, direct_state=fetched, blockhash_source=lambda: {
            "blockhash": self.transport.blockhash, "slot": fetched["context_slot"],
            "last_valid_block_height": 5000, "simulated": True, "simulation": {"err": None},
        })
        self.assertTrue(checked["checks"]["fee_unbound_clear"])
        self.assertTrue(checked["checks"]["blockhash_checked"])
        self.assertIsNot(checked.get("broadcastable"), True)
        flagged = execution_transport.decide("entry-1", unsigned, direct_state={"slippage_verified": True, "fee_cap_ok": True, "exposure_cap_ok": True})
        self.assertFalse(flagged["checks"]["slippage_verified"])
        self.assertFalse(flagged["checks"]["fee_unbound_clear"])
        self._closed("loss-19", "-19.00", "5.00")
        connection = db.connect()
        try:
            self.assertEqual(phantom_runner.cumulative_realized_loss(connection), Decimal("19.00"))
        finally:
            connection.close()
        second = self.client.post("/api/phantom/direct-reserve", headers=AUTH, json={"intent_id": "entry-2"})
        self.assertEqual(second.status_code, 200, second.text)
        self._closed("loss-1", "-1.00", "0.01")
        blocked = self.client.post("/api/phantom/direct-reserve", headers=AUTH, json={"intent_id": "entry-3"})
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.json()["reason"], "stop_new_entries")
        self.assertEqual(db.query("SELECT reserved_usd FROM phantom_reservations WHERE intent_id = 'entry-3'")[0]["reserved_usd"], "0.00")
        runner_calls = []

        def runner(argv, _env, _timeout):
            runner_calls.append(argv)
            raise AssertionError("signer")

        submitted = execution_transport.submit("entry-1", "AAAA", runner=runner, direct_state={"slippage_verified": True})
        self.assertEqual(runner_calls, [])
        self.assertFalse(submitted["signer_called"])
        self.assertFalse(submitted["checks"]["slippage_verified"])
        self.assertFalse(submitted["broadcastable"])
        self.assertTrue(execution_transport._claim("entry-1", "abc"))
        row = db.query("SELECT status, reserved_usd, result_json FROM phantom_reservations WHERE intent_id = 'entry-1'")[0]
        self.assertEqual(row["status"], "submit_unknown")
        self.assertNotEqual(row["reserved_usd"], "0.00")
        stored = json.loads(row["result_json"])
        self.assertTrue(stored["chain_submit"])
        self.assertFalse(stored["confirmed"])
        held_usd = row["reserved_usd"]
        plain = self.client.post("/api/phantom/reconcile", headers=AUTH, json={"intent_id": "entry-1"})
        self.assertEqual(plain.status_code, 200)
        self.assertEqual(plain.json()["reason"], "held_pending_proof")
        self.assertFalse(plain.json()["resubmit"])
        self.assertEqual(db.query("SELECT status, reserved_usd FROM phantom_reservations WHERE intent_id = 'entry-1'")[0]["reserved_usd"], held_usd)
        unproven = self.client.post("/api/phantom/direct-confirm", headers=AUTH, json={
            "intent_id": "entry-1", "signature": SIGNATURE, "block_height": 400,
        })
        self.assertFalse(unproven.json()["confirmed"])
        self.assertEqual(db.query("SELECT status FROM phantom_reservations WHERE intent_id = 'entry-1'")[0]["status"], "submit_unknown")
        stored = json.loads(db.query("SELECT result_json FROM phantom_reservations WHERE intent_id = 'entry-1'")[0]["result_json"])
        phantom_runner.CHAIN_STATUS = lambda _signature: {
            "err": None, "confirmation_status": "finalized", "block_height": 400, "slot": 466547240,
            "network": "solana:mainnet", "signature": SIGNATURE, "wallet": phantom_transport.REQUIRED_WALLET,
            "message_sha256": stored["message_sha256"], "network_fee_lamports": stored["network_fee_lamports"],
            "acquired_raw": 188718141,
        }
        rejected = self.client.post("/api/phantom/reconcile", headers=AUTH, json={
            "intent_id": "entry-1", "proof": {"signature": SIGNATURE, "block_height": 400},
        })
        self.assertEqual(rejected.json()["reason"], "delta_mismatch")
        self.assertEqual(db.query("SELECT status FROM phantom_reservations WHERE intent_id = 'entry-1'")[0]["status"], "submit_unknown")
        phantom_runner.CHAIN_STATUS = lambda _signature: {
            "err": None, "confirmation_status": "finalized", "block_height": 400, "slot": 466547240,
            "network": "solana:mainnet", "signature": SIGNATURE, "wallet": phantom_transport.REQUIRED_WALLET,
            "message_sha256": stored["message_sha256"], "network_fee_lamports": stored["network_fee_lamports"],
            "acquired_raw": stored["entry_token_est_out"],
        }
        confirmed = self.client.post("/api/phantom/direct-confirm", headers=AUTH, json={
            "intent_id": "entry-1", "signature": SIGNATURE, "block_height": 400,
        })
        self.assertTrue(confirmed.json()["confirmed"])
        self.assertEqual(confirmed.json()["acquired_raw"], 7995630)
        self.assertEqual(confirmed.json()["exit_raw"], 7995630)
        self.assertNotEqual(confirmed.json()["exit_raw"], 188718141)
        self.assertIsNone(confirmed.json()["realized_pnl"])
        self.assertEqual(db.query("SELECT reserved_usd FROM phantom_reservations WHERE intent_id = 'entry-1'")[0]["reserved_usd"], held_usd)
        replay = execution_transport.send_after_identity("entry-1", "AAAA", runner, binary="phantom")
        self.assertEqual(replay["subprocess"], [])
        self.assertFalse(replay["resend"])
        self.assertEqual(runner_calls, [])
        status = self.client.get("/api/phantom/direct-status", headers=AUTH, params={"intent_id": "entry-1"})
        view = status.json()
        self.assertEqual(view["network"], "solana:mainnet")
        self.assertEqual(view["wallet"], phantom_transport.REQUIRED_WALLET)
        self.assertEqual(view["intent"]["context_slot"], 466547240)
        self.assertEqual(view["intent"]["exit_close_raw"], 7995630)
        self.assertFalse(view["intent"]["exit_uses_wallet_balance"])
        self.assertFalse(view["agents_enabled"])
        self.assertEqual(view["mode"], "PAPER")
        self.assertEqual(view["strategies_enabled"], [])
        self.assertFalse(view["live_ready"])
        self.assertFalse(view["kill_switch"])
        hook = solana_direct_swap.revalidate_blockhash(solana_direct_swap.PLACEHOLDER_BLOCKHASH, lambda: {
            "blockhash": "9" * 32, "slot": 466547240, "last_valid_block_height": 500,
            "simulated": True, "simulation": {"err": None}, "placeholder": False,
        }, 466547240)
        self.assertTrue(hook["source_ok"])
        self.assertFalse(hook["checked"])
        self.assertFalse(hook["applied"])
        self.assertFalse(hook["broadcastable"])
        self.assertFalse(hook["runtime_ready"])
        self.assertEqual(db.get_setting("agents_enabled"), "0")
        self.assertEqual(db.get_setting("mode"), "PAPER")

    def test_a_missing_tick_array_is_not_retried(self):
        address = solana_direct_swap.SOL_USDC_SNAPSHOT["tick_array_addresses"][0]
        rpc = FixtureRpc(drop=address)
        fetched = solana_direct_swap.fetch_state(rpc)
        self.assertFalse(fetched["ok"])
        self.assertEqual(fetched["reason"], "tick_array_missing")
        self.assertFalse(fetched["retry"])
        self.assertEqual(rpc.calls, 1)

    def test_allocation_cap_counts_open_holds(self):
        spend_policy.persist_owner_approval()
        solana_read_rpc.enable_reads()
        solana_read_rpc.TRANSPORT = self.transport
        phantom_runner.set_halt({"halted": False})
        now = __import__("time").time()
        db.execute(
            """
            INSERT INTO phantom_reservations (
                intent_id, status, wallet_id, wallet, chain, pair_json, amount_usd, fee_usd,
                reserved_usd, slippage, entry_quote_id, exit_quote_id, fx_sol_usd, fx_at,
                result_json, created_at, updated_at
            ) VALUES ('open-hold', 'sim_open', ?, ?, 'solana:mainnet', '{}', '8.00', '0.01',
                      '95.00', 1, NULL, NULL, '89.00', ?, NULL, ?, ?)
            """,
            (phantom_transport.REQUIRED_WALLET_ID, phantom_transport.REQUIRED_WALLET, now, now, now),
        )
        blocked = self.client.post("/api/phantom/direct-reserve", headers=AUTH, json={"intent_id": "too-much"})
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.json()["reason"], "allocation")

    def test_synthetic_paper_unknown_still_releases(self):
        now = __import__("time").time()
        wallet = phantom_transport.REQUIRED_WALLET
        wallet_id = phantom_transport.REQUIRED_WALLET_ID
        db.execute(
            """
            INSERT INTO phantom_reservations (
                intent_id, status, wallet_id, wallet, chain, pair_json, amount_usd, fee_usd,
                reserved_usd, slippage, entry_quote_id, exit_quote_id, fx_sol_usd, fx_at,
                result_json, created_at, updated_at
            ) VALUES ('paper-unknown', 'submit_unknown', ?, ?, 'solana:mainnet', '{}', '8.00', '0.20',
                      '8.20', 1, NULL, NULL, '150.00', ?, NULL, ?, ?)
            """,
            (wallet_id, wallet, now, now, now),
        )
        released = phantom_runner.reconcile_unknown({"intent_id": "paper-unknown"})
        self.assertEqual(released["reason"], "reconciled_without_resubmit")
        self.assertEqual(db.query("SELECT status FROM phantom_reservations WHERE intent_id = 'paper-unknown'")[0]["status"], "released")
        db.execute(
            """
            INSERT INTO phantom_reservations (
                intent_id, status, wallet_id, wallet, chain, pair_json, amount_usd, fee_usd,
                reserved_usd, slippage, entry_quote_id, exit_quote_id, fx_sol_usd, fx_at,
                result_json, created_at, updated_at
            ) VALUES ('chain-manual', 'submit_unknown', ?, ?, 'solana:mainnet', '{}', '8.00', '0.01',
                      '8.14', 1, NULL, NULL, '89.00', ?, ?, ?, ?)
            """,
            (wallet_id, wallet, json.dumps({"chain_submit": True, "origin": "direct_whirlpool"}), now, now, now),
        )
        still = phantom_runner.reconcile_unknown({"intent_id": "chain-manual"})
        self.assertEqual(still["reason"], "held_pending_proof")
        refused = phantom_runner.reconcile_unknown({
            "intent_id": "chain-manual",
            "manual": True,
            "statement": "Manually reconcile this unknown chain submit without resubmitting.",
        })
        self.assertEqual(refused["reason"], "held_pending_proof")
        self.assertEqual(db.query("SELECT status FROM phantom_reservations WHERE intent_id = 'chain-manual'")[0]["status"], "submit_unknown")
        manual = self.client.post("/api/phantom/owner-release", headers=AUTH, json={"intent_id": "chain-manual", "release": True})
        self.assertEqual(manual.status_code, 200, manual.text)
        self.assertEqual(manual.json()["reason"], "owner_release")
        self.assertFalse(manual.json()["ledger_proven"])
        self.assertFalse(manual.json()["resubmit"])
        self.assertEqual(db.query("SELECT status, reserved_usd FROM phantom_reservations WHERE intent_id = 'chain-manual'")[0]["reserved_usd"], "0.00")

    def _closed(self, intent_id: str, realized: str, fee: str) -> None:
        now = __import__("time").time()
        db.execute(
            """
            INSERT INTO phantom_reservations (
                intent_id, status, wallet_id, wallet, chain, pair_json, amount_usd, fee_usd,
                reserved_usd, slippage, entry_quote_id, exit_quote_id, fx_sol_usd, fx_at,
                result_json, created_at, updated_at, realized_pnl
            ) VALUES (?, 'sim_closed', ?, ?, 'solana:mainnet', '{}', '8.00', ?,
                      '0.00', 1, NULL, NULL, '89.00', ?, NULL, ?, ?, ?)
            """,
            (
                intent_id, phantom_transport.REQUIRED_WALLET_ID,
                phantom_transport.REQUIRED_WALLET, fee, now, now, now, realized,
            ),
        )

