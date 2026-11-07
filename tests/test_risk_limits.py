"""Explicit PAPER limits retain losses and atomically bound new reservations."""

import importlib
import json
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

import db
import phantom_transport
import risk_limits
from tests.support import temp_desk_env


class TestRiskLimits(unittest.TestCase):
    def setUp(self):
        self.tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        db.init_db()

    def approve(self, loss="20.00", fee="0.20"):
        return risk_limits.approve({
            "max_total_loss_usd": loss,
            "max_round_trip_fee_usd": fee,
        })

    def insert(self, intent_id, status="sim_open", pnl=None, reserved="8.20", fee="0.20", conn=None):
        sql = (
            "INSERT INTO phantom_reservations (intent_id, status, wallet_id, wallet, chain, "
            "pair_json, amount_usd, fee_usd, reserved_usd, realized_pnl, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'solana:mainnet', '{}', '8.00', ?, ?, ?, ?, ?)"
        )
        values = (
            intent_id, status, phantom_transport.REQUIRED_WALLET_ID,
            phantom_transport.REQUIRED_WALLET, fee, reserved, pnl, time.time(), time.time(),
        )
        if conn is None:
            db.execute(sql, values)
        else:
            conn.execute(sql, values)

    def test_defaults_unapproved_and_never_claim_live_or_verified_fees(self):
        shown = risk_limits.view()
        self.assertFalse(shown["approved"])
        self.assertEqual(shown["approval_state"], "not_approved")
        self.assertEqual(risk_limits.assess("0.20")["reason"], "not_approved")
        approved = self.approve()
        allowed = risk_limits.assess("0.20")
        self.assertTrue(allowed["allowed"])
        for result in (shown, approved, allowed):
            self.assertEqual(result["evidence"], "paper_ledger_only")
            self.assertEqual(result["scope"]["authority"], "PAPER")
            for key in ("live_ready", "execution_allowed", "send", "executable", "fee_source_verified"):
                self.assertFalse(result[key])
        self.assertEqual(db.get_setting("mode"), "PAPER")
        self.assertEqual(db.query("SELECT value FROM phantom_control WHERE key='halt'")[0]["value"], "1")
        self.assertEqual(db.query("SELECT intent_id FROM phantom_reservations"), [])

    def test_invalid_limits_are_rejected_without_replacing_existing_approval(self):
        self.approve()
        invalid = [
            None, {}, {"max_total_loss_usd": "20.00"},
            {"max_total_loss_usd": "20.00", "max_round_trip_fee_usd": "0.20", "execute": True},
        ]
        for value in [None, True, 10, 10.0, "NaN", "Infinity", "-1.00", "0.00", "100.01", "10", "10.0", "10.001", "1e1", " 10.00", "010.00"]:
            invalid.append({"max_total_loss_usd": value, "max_round_trip_fee_usd": "0.20"})
        for value in [True, 0.2, "NaN", "Infinity", "-0.01", "8.01", "0.200"]:
            invalid.append({"max_total_loss_usd": "20.00", "max_round_trip_fee_usd": value})
        for body in invalid:
            with self.subTest(body=body):
                with self.assertRaises(ValueError):
                    risk_limits.approve(body)
                self.assertEqual(risk_limits.view()["limits"], {
                    "max_total_loss_usd": "20.00", "max_round_trip_fee_usd": "0.20",
                })

    def test_zero_fee_and_exact_boundaries_are_allowed(self):
        self.approve("8.00", "0.00")
        self.assertTrue(risk_limits.assess("0.00")["allowed"])
        self.assertEqual(risk_limits.assess("0.01")["reason"], "round_trip_fee_limit")
        self.approve("8.20", "0.20")
        self.assertTrue(risk_limits.assess("0.20")["allowed"])
        self.approve("8.19", "0.20")
        self.assertEqual(risk_limits.assess("0.20")["reason"], "total_loss_limit")
        self.approve("100.00", "8.00")
        self.assertTrue(risk_limits.assess("8.00")["allowed"])

    def test_fee_assessment_accepts_exact_cents_and_rejects_bad_quotes(self):
        self.approve()
        self.assertTrue(risk_limits.assess("0.2")["allowed"])
        for value in (None, True, 0.2, "NaN", "Infinity", "-0.01", "0.201", "1e999999999", "1e-999999999"):
            with self.subTest(value=value):
                result = risk_limits.assess(value)
                self.assertFalse(result["allowed"])
                self.assertEqual(result["reason"], "fee_quote_invalid")
        self.assertEqual(risk_limits.assess("1000.00")["reason"], "round_trip_fee_limit")

    def test_restart_and_reapproval_do_not_reset_losses(self):
        self.approve("10.00")
        self.insert("loss", "sim_closed", pnl="-2.00", reserved="0.00")
        db.init_db()
        importlib.reload(risk_limits)
        self.assertTrue(risk_limits.view()["approved"])
        self.assertEqual(risk_limits.assess("0.20")["reason"], "total_loss_limit")
        risk_limits.clear()
        self.assertFalse(risk_limits.view()["approved"])
        self.assertEqual(risk_limits.assess("0.20")["reason"], "approval_revoked")
        db.init_db()
        self.assertEqual(risk_limits.view()["approval_state"], "approval_revoked")
        self.approve("10.00")
        self.assertEqual(risk_limits.view()["cumulative_realized_losses_usd"], "2.00")
        self.assertFalse(risk_limits.assess("0.20")["allowed"])

    def test_profit_does_not_refund_loss_capacity(self):
        self.approve("10.00")
        self.insert("loss", "sim_closed", pnl="-2.00", reserved="0.00")
        self.insert("profit", "sim_closed", pnl="50.00", reserved="0.00")
        self.assertEqual(risk_limits.view()["risk_used_usd"], "2.00")
        self.assertEqual(risk_limits.assess("0.20")["reason"], "total_loss_limit")

    def test_every_pending_status_including_unknown_holds_full_principal_and_fees(self):
        self.approve("40.00")
        for status in ("reserved", "quoted", "sim_open", "submit_unknown"):
            self.insert(status, status)
        self.assertEqual(risk_limits.view()["open_committed_usd"], "32.80")
        self.assertEqual(risk_limits.assess("0.20")["reason"], "total_loss_limit")
        self.assertEqual(db.query("SELECT status FROM phantom_reservations WHERE intent_id='submit_unknown'")[0]["status"], "submit_unknown")

    def test_understated_reserve_does_not_hide_principal_or_fee(self):
        self.approve("16.00")
        self.insert("short-reserve", reserved="0.00")
        self.assertEqual(risk_limits.view()["open_committed_usd"], "8.20")
        self.assertFalse(risk_limits.assess("0.20")["allowed"])

    def test_allocation_is_not_increased_by_realized_profit(self):
        self.approve("100.00")
        self.insert("loss", "sim_closed", pnl="-92.00", reserved="0.00")
        self.insert("profit", "sim_closed", pnl="200.00", reserved="0.00")
        self.assertEqual(risk_limits.assess("0.20")["reason"], "allocation_limit")

    def test_missing_corrupt_or_mismatched_ledger_data_fails_closed(self):
        self.approve()
        self.insert("bad", "sim_closed", pnl=None)
        self.assertEqual(risk_limits.assess("0.20")["reason"], "ledger_invalid")
        for pnl in ("NaN", "Infinity", "nope", "-0.001", "1e999999999"):
            db.execute("UPDATE phantom_reservations SET realized_pnl=?", (pnl,))
            self.assertFalse(risk_limits.view()["ledger_valid"])
        db.execute("UPDATE phantom_reservations SET status='submit_unknown', wallet='different'")
        self.assertEqual(risk_limits.assess("0.20")["reason"], "ledger_invalid")

    def test_terminal_label_cannot_hide_unreleased_money_or_realized_loss(self):
        self.approve()
        self.insert("inconsistent", "released")
        self.assertEqual(risk_limits.assess("0.20")["reason"], "ledger_invalid")
        db.execute("UPDATE phantom_reservations SET reserved_usd='0.00', realized_pnl='-1.00'")
        self.assertEqual(risk_limits.assess("0.20")["reason"], "ledger_invalid")
        db.execute("UPDATE phantom_reservations SET realized_pnl=NULL")
        self.assertTrue(risk_limits.assess("0.20")["allowed"])

    def test_stored_policy_cannot_be_reused_for_a_changed_scope(self):
        self.approve()
        row = db.query("SELECT value FROM phantom_control WHERE key=?", (risk_limits._KEY,))[0]
        payload = json.loads(row["value"])
        payload["scope"]["chain"] = "solana:devnet"
        db.execute("UPDATE phantom_control SET value=? WHERE key=?", (json.dumps(payload), risk_limits._KEY))
        self.assertEqual(risk_limits.assess("0.20")["reason"], "approval_invalid")

    def test_concurrent_assessment_and_reservation_share_one_transaction(self):
        self.approve("8.20")
        barrier = threading.Barrier(2)

        def try_entry(intent_id):
            barrier.wait(timeout=5)

            def work(conn):
                result = risk_limits.assess("0.20", conn=conn)
                if result["allowed"]:
                    self.insert(intent_id, status="reserved", conn=conn)
                return result["allowed"]

            return db.exclusive(work)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(try_entry, ("first", "second")))
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(len(db.query("SELECT intent_id FROM phantom_reservations")), 1)
        self.assertEqual(risk_limits.view()["open_committed_usd"], "8.20")

    def test_pending_entry_recheck_does_not_double_reserve(self):
        self.approve("8.20")
        self.insert("pending", "reserved")
        self.assertFalse(risk_limits.assess("0.20")["allowed"])
        result = db.exclusive(lambda conn: risk_limits.assess_existing("pending", conn=conn))
        self.assertTrue(result["allowed"])
        self.assertEqual(result["projected_risk_usd"], "8.20")
        self.assertEqual(result["new_entry_reservation_usd"], "0.00")
        self.assertEqual(risk_limits.view()["open_committed_usd"], "8.20")

    def test_pending_entry_honors_revocation_lowered_limits_and_other_losses(self):
        self.approve("10.00")
        self.insert("pending", "reserved")
        risk_limits.clear()
        self.assertEqual(risk_limits.assess_existing("pending")["reason"], "approval_revoked")
        self.approve("8.19")
        self.assertEqual(risk_limits.assess_existing("pending")["reason"], "total_loss_limit")
        self.approve("10.00", "0.19")
        self.assertEqual(risk_limits.assess_existing("pending")["reason"], "round_trip_fee_limit")
        self.approve("10.00")
        self.insert("prior-loss", "sim_closed", pnl="-2.00", reserved="0.00")
        self.assertEqual(risk_limits.assess_existing("pending")["reason"], "total_loss_limit")

    def test_pending_entry_requires_exact_reserved_row_and_scope(self):
        self.approve()
        self.assertEqual(risk_limits.assess_existing("absent")["reason"], "reservation_missing")
        self.insert("pending", "submit_unknown")
        self.assertEqual(risk_limits.assess_existing("pending")["reason"], "reservation_not_pending_entry")
        db.execute("UPDATE phantom_reservations SET status='sim_open'")
        self.assertEqual(risk_limits.assess_existing("pending")["reason"], "reservation_not_pending_entry")
        db.execute("UPDATE phantom_reservations SET status='reserved', reserved_usd='8.00'")
        self.assertEqual(risk_limits.assess_existing("pending")["reason"], "reservation_invalid")
        db.execute("UPDATE phantom_reservations SET reserved_usd='8.20', amount_usd='7.00'")
        self.assertEqual(risk_limits.assess_existing("pending")["reason"], "reservation_invalid")
        db.execute("UPDATE phantom_reservations SET amount_usd='8.00', wallet='other'")
        self.assertEqual(risk_limits.assess_existing("pending")["reason"], "ledger_invalid")


if __name__ == "__main__":
    unittest.main()
