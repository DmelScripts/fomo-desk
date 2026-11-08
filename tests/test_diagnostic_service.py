"""Authenticated diagnostic tick. Fake runner, fake reads, harness price. No live signer."""
import base64
import json
import os
import time
import unittest

import db
import diagnostic_lifecycle
import exit_policy
import phantom_auth
import phantom_runner
import phantom_transport
import solana_read_rpc
import solana_tx
import spend_policy
import usdc_valuation
from fastapi.testclient import TestClient
from tests.support import temp_desk_env
from tests.whirlpool_fixture import FakeTransport

TOKEN = "dummy-app-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
WALLET = phantom_transport.REQUIRED_WALLET
WALLET_ID = phantom_transport.REQUIRED_WALLET_ID


def _harness():
    return {
        "price": 99_990_000, "conf": 10_000, "expo": -8,
        "publish_time": int(time.time()), "feed_id": usdc_valuation.FEED_ID,
    }


def _identity():
    return json.dumps({
        "walletId": WALLET_ID,
        "organizationId": "org",
        "addresses": [{"addressType": "solana", "address": WALLET}],
    })


def _natives(decoded, wallet_pre, sol_delta, fee):
    pre = [0] * len(decoded.static_keys)
    post = [0] * len(decoded.static_keys)
    pre[0] = wallet_pre
    post[0] = wallet_pre - sol_delta - fee
    return pre, post


class DiagnosticServiceTests(unittest.TestCase):
    def setUp(self):
        _tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        previous = os.environ.get("APP_TOKEN")
        os.environ["APP_TOKEN"] = TOKEN
        self.addCleanup(lambda: os.environ.pop("APP_TOKEN") if previous is None else os.environ.__setitem__("APP_TOKEN", previous))
        db.init_db()
        db.set_setting("mode", "PAPER", "test")
        db.set_setting("agents_enabled", "0", "test")
        phantom_auth.reset_for_tests()
        self.addCleanup(phantom_auth.reset_for_tests)
        spend_policy.persist_owner_approval()
        phantom_runner.set_halt({"halted": False})
        exit_policy.set_approval(True)
        usdc_valuation.enable()
        usdc_valuation.set_quote(None)
        usdc_valuation.install_harness(None)
        self.addCleanup(lambda: usdc_valuation.install_harness(None))
        self.addCleanup(lambda: usdc_valuation.set_quote(None))
        solana_read_rpc.TRANSPORT = None
        self.addCleanup(lambda: setattr(solana_read_rpc, "TRANSPORT", None))
        diagnostic_lifecycle.RUNNER = None
        diagnostic_lifecycle.CLOCK = None
        self.addCleanup(lambda: setattr(diagnostic_lifecycle, "RUNNER", None))
        self.addCleanup(lambda: setattr(diagnostic_lifecycle, "CLOCK", None))
        solana_read_rpc.enable_reads()
        self.transport = FakeTransport()
        solana_read_rpc.TRANSPORT = self.transport
        import server
        self.client = TestClient(server.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.calls = []
        self.signed = {}

        def runner(argv, _env, _timeout):
            self.calls.append(argv[1:4])
            if argv[1:3] == ["wallet", "addresses"]:
                return 0, _identity()
            raw = base64.b64decode(argv[4])
            signed = bytes([raw[0]]) + (b"\x55" * 64) + raw[1 + 64:]
            encoded = base64.b64encode(signed).decode("ascii")
            signature = solana_tx.first_signature(encoded)
            self.signed[signature] = encoded
            return 0, json.dumps({"signature": signature, "networkId": "solana:mainnet", "account": WALLET})

        self.runner = runner

    def test_set_quote_is_not_a_live_source_and_a_missing_key_fails_closed(self):
        usdc_valuation.set_quote(_harness())
        refused = self.client.post("/api/phantom/diagnostic/tick", headers=AUTH, json={})
        self.assertEqual(refused.status_code, 409)
        self.assertEqual(refused.json()["reason"], "injected_quote_not_live")
        self.assertFalse(refused.json()["live_provider"])
        self.assertFalse(usdc_valuation.authenticated_read()["authenticated_live"])
        self.assertEqual(usdc_valuation.authenticated_read()["reason"], "credentials_missing")
        self.assertEqual(db.query("SELECT intent_id FROM phantom_reservations"), [])
        leaked = self.client.post("/api/phantom/diagnostic/tick", headers=AUTH, json={"transaction": "aaaa", "price": "1"})
        self.assertEqual(leaked.status_code, 400)

    def test_http_tick_runs_entry_risk_exit_and_does_not_resend(self):
        usdc_valuation.install_harness(_harness())
        diagnostic_lifecycle.RUNNER = self.runner
        reserved = self.client.post("/api/phantom/diagnostic/tick", headers=AUTH, json={})
        self.assertEqual(reserved.status_code, 200, reserved.text)
        self.assertEqual(reserved.json()["stage"], "sim_open")
        self.assertFalse(reserved.json()["scanner_qualified"])
        self.assertFalse(reserved.json()["auditor_qualified"])
        self.assertFalse(reserved.json()["analyst_qualified"])
        self.assertFalse(reserved.json()["user_token_buy"])
        self.assertIsNone(reserved.json()["strategy"])
        self.assertFalse(reserved.json()["signer_enabled"])
        self.assertFalse(reserved.json()["effectful"])
        self.assertEqual(db.get_setting("agents_enabled"), "0")
        self.assertEqual(self.calls, [])
        spawned = self.client.post("/api/phantom/diagnostic/tick", headers=AUTH, json={})
        self.assertEqual(spawned.status_code, 200, spawned.text)
        self.assertTrue(spawned.json()["sign_proof"]["recomputed"])
        self.assertTrue(spawned.json()["sign_proof"]["spend_verified"])
        self.assertFalse(spawned.json()["sign_proof"]["signer_enabled"])
        self.assertEqual(spawned.json()["subprocess"], ["addresses", "send"])
        sends = [call for call in self.calls if call[:2] == ["solana", "send"]]
        self.assertEqual(len(sends), 1)
        held = self.client.post("/api/phantom/diagnostic/tick", headers=AUTH, json={})
        self.assertEqual(len([call for call in self.calls if call[:2] == ["solana", "send"]]), 1)
        self.assertNotEqual(held.json().get("stage"), "sim_open")
        view = self.client.get("/api/phantom/diagnostic", headers=AUTH).json()["intent"]
        self._plant_entry(view)
        confirmed = self.client.post("/api/phantom/diagnostic/tick", headers=AUTH, json={})
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertEqual(confirmed.json()["stage"], "confirmed")
        self.assertIsNone(confirmed.json()["realized_pnl"])
        self.assertEqual(len([call for call in self.calls if call[:2] == ["solana", "send"]]), 1)
        opened = self.client.get("/api/phantom/diagnostic", headers=AUTH).json()["intent"]["opened_at"]
        diagnostic_lifecycle.CLOCK = lambda: opened + 3 * 3600 + 5
        db.execute(
            """
            INSERT INTO phantom_reservations (
                intent_id, status, wallet_id, wallet, chain, pair_json, amount_usd, fee_usd,
                reserved_usd, slippage, entry_quote_id, exit_quote_id, fx_sol_usd, fx_at,
                result_json, created_at, updated_at, realized_pnl
            ) VALUES ('other-loss', 'sim_closed', ?, ?, 'solana:mainnet', '{}', '8.00', '0.01',
                      '0.00', 1, NULL, NULL, '89', ?, NULL, ?, ?, '-20.00')
            """,
            (WALLET_ID, WALLET, time.time(), time.time(), time.time()),
        )
        exited = self.client.post("/api/phantom/diagnostic/tick", headers=AUTH, json={})
        self.assertEqual(exited.status_code, 200, exited.text)
        self.assertEqual(exited.json()["action"], "timeout")
        self.assertFalse(exited.json()["loss_stop_blocks_exit"])
        self.assertEqual(len([call for call in self.calls if call[:2] == ["solana", "send"]]), 2)
        state = self.client.get("/api/phantom/diagnostic", headers=AUTH).json()["intent"]
        self._plant_exit(state, flat=True)
        held_flat = self.client.post("/api/phantom/diagnostic/tick", headers=AUTH, json={})
        self.assertEqual(held_flat.status_code, 409, held_flat.text)
        self.assertEqual(held_flat.json()["reason"], "proceeds_missing")
        self.assertEqual(len([call for call in self.calls if call[:2] == ["solana", "send"]]), 2)
        still = self.client.get("/api/phantom/diagnostic", headers=AUTH).json()["intent"]
        self.assertEqual(still["status"], "exit_unknown")
        self.assertEqual(still["reserved_usd"], state["reserved_usd"])
        self.assertIsNone(still["realized_pnl"])
        self._plant_exit(still, flat=False)
        closed = self.client.post("/api/phantom/diagnostic/tick", headers=AUTH, json={})
        self.assertEqual(closed.status_code, 200, closed.text)
        self.assertEqual(closed.json()["stage"], "sim_closed")
        self.assertTrue(str(closed.json()["realized_pnl"]).startswith("-"))
        self.assertNotEqual(closed.json()["realized_pnl"], "-8.02")
        again = self.client.post("/api/phantom/diagnostic/tick", headers=AUTH, json={})
        self.assertEqual(again.json()["stage"], "closed")
        self.assertEqual(len([call for call in self.calls if call[:2] == ["solana", "send"]]), 2)
        self.assertEqual(db.query("SELECT COUNT(*) AS n FROM phantom_reservations WHERE intent_id = 'diagnostic-sol-usdc'")[0]["n"], 1)

    def test_exit_halt_flip_after_identity_does_not_send(self):
        usdc_valuation.install_harness(_harness())
        diagnostic_lifecycle.RUNNER = self.runner
        self.assertEqual(self.client.post("/api/phantom/diagnostic/tick", headers=AUTH, json={}).status_code, 200)
        spawned = self.client.post("/api/phantom/diagnostic/tick", headers=AUTH, json={})
        self.assertEqual(spawned.status_code, 200, spawned.text)
        view = self.client.get("/api/phantom/diagnostic", headers=AUTH).json()["intent"]
        self._plant_entry(view)
        confirmed = self.client.post("/api/phantom/diagnostic/tick", headers=AUTH, json={})
        self.assertEqual(confirmed.json()["stage"], "confirmed", confirmed.text)
        opened = self.client.get("/api/phantom/diagnostic", headers=AUTH).json()["intent"]["opened_at"]
        diagnostic_lifecycle.CLOCK = lambda: opened + 3 * 3600 + 5
        armed = {"n": 0}

        def runner(argv, _env, _timeout):
            self.calls.append(argv[1:4])
            if argv[1:3] == ["wallet", "addresses"]:
                armed["n"] += 1
                if armed["n"] >= 2:
                    phantom_runner.set_halt({"halted": True})
                return 0, _identity()
            raise AssertionError("exit send")

        diagnostic_lifecycle.RUNNER = runner
        refused = self.client.post("/api/phantom/diagnostic/tick", headers=AUTH, json={})
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertEqual(refused.json()["reason"], "kill_switch")
        self.assertFalse(refused.json()["send"])
        self.assertFalse(refused.json()["loss_stop_blocks_exit"])
        intent = self.client.get("/api/phantom/diagnostic", headers=AUTH).json()["intent"]
        self.assertEqual(intent["status"], "confirmed")
        self.assertEqual([call for call in self.calls if call[:2] == ["solana", "send"] and call != ["solana", "send", "--transaction"]], [])
        self.assertEqual(len([call for call in self.calls if call[:2] == ["solana", "send"]]), 1)

    def test_an_unknown_entry_is_not_resent_after_restart(self):
        usdc_valuation.install_harness(_harness())
        diagnostic_lifecycle.RUNNER = self.runner
        self.client.post("/api/phantom/diagnostic/tick", headers=AUTH, json={})
        spawned = self.client.post("/api/phantom/diagnostic/tick", headers=AUTH, json={})
        self.assertEqual(spawned.status_code, 200, spawned.text)
        sends = len([call for call in self.calls if call[:2] == ["solana", "send"]])
        db.init_db()
        again = self.client.post("/api/phantom/diagnostic/tick", headers=AUTH, json={})
        self.assertNotEqual(again.json().get("stage"), "sim_open")
        self.assertEqual(len([call for call in self.calls if call[:2] == ["solana", "send"]]), sends)
        self.assertEqual(db.query("SELECT status FROM phantom_reservations WHERE intent_id = 'diagnostic-sol-usdc'")[0]["status"], "submit_unknown")

    def _plant_entry(self, view):
        encoded = self.signed[view["signature"]]
        decoded = solana_tx.decode_transaction(base64.b64decode(encoded))
        ata = solana_tx.associated_token_address(WALLET, solana_tx.USDC_MINT, solana_tx.TOKEN_PROGRAM)
        index = decoded.static_keys.index(ata)
        delta = view["entry_token_min_out"] + 100
        pre_usdc = view["wallet_usdc_raw"]
        fee = view["network_fee_lamports"]
        native_pre, native_post = _natives(decoded, 2_000_000_000, view["entry_lamports"], fee)
        self.transport.statuses = {"context": {"slot": view["context_slot"]}, "value": [{
            "slot": view["context_slot"], "err": None, "confirmationStatus": "finalized", "blockHeight": 20,
        }]}
        self.transport.transaction = {
            "slot": view["context_slot"],
            "meta": {
                "err": None, "fee": fee,
                "preBalances": native_pre, "postBalances": native_post,
                "preTokenBalances": [{"accountIndex": index, "mint": solana_tx.USDC_MINT, "owner": WALLET, "uiTokenAmount": {"amount": str(pre_usdc)}}],
                "postTokenBalances": [{"accountIndex": index, "mint": solana_tx.USDC_MINT, "owner": WALLET, "uiTokenAmount": {"amount": str(pre_usdc + delta)}}],
            },
            "transaction": [encoded, "base64"],
        }

    def _plant_exit(self, view, flat: bool):
        encoded = self.signed[view["exit_signature"]]
        decoded = solana_tx.decode_transaction(base64.b64decode(encoded))
        ata = solana_tx.associated_token_address(WALLET, solana_tx.USDC_MINT, solana_tx.TOKEN_PROGRAM)
        index = decoded.static_keys.index(ata)
        acquired = view["acquired_raw"]
        pre_usdc = view["wallet_usdc_raw"] + acquired
        fee = view["network_fee_lamports"]
        if flat:
            native_pre = [0] * len(decoded.static_keys)
            native_post = [0] * len(decoded.static_keys)
            native_pre[0] = native_post[0] = 1_000_000_000
        else:
            native_pre, native_post = _natives(decoded, 1_000_000_000, -view["entry_lamports"], fee)
        self.transport.statuses = {"context": {"slot": view["context_slot"]}, "value": [{
            "slot": view["context_slot"], "err": None, "confirmationStatus": "finalized", "blockHeight": 22,
        }]}
        self.transport.transaction = {
            "slot": view["context_slot"],
            "meta": {
                "err": None, "fee": fee,
                "preBalances": native_pre, "postBalances": native_post,
                "preTokenBalances": [{"accountIndex": index, "mint": solana_tx.USDC_MINT, "owner": WALLET, "uiTokenAmount": {"amount": str(pre_usdc)}}],
                "postTokenBalances": [{"accountIndex": index, "mint": solana_tx.USDC_MINT, "owner": WALLET, "uiTokenAmount": {"amount": str(pre_usdc - acquired)}}],
            },
            "transaction": [encoded, "base64"],
        }
