"""The signing adapter is a stub. Tests never sign or broadcast."""
import unittest

import execution_transport


def _ready():
    return {
        "mode": "PAPER",
        "live_consent": True,
        "halted": False,
        "loss_policy": "approved",
        "risk_allows_send": True,
        "intent_verified": True,
        "spend_verified": True,
        "slippage_verified": True,
        "blockhash_status": "checked",
        "blockhash_valid": True,
        "last_valid_block_height": 250_000_000,
        "freshness_guarantee": False,
        "provider_expiry": None,
        "intent_id": "intent-1",
        "used_intent_ids": [],
        "reservation": {"status": "reserved", "intent_id": "intent-1", "usd": "8.00"},
    }


class ExecutionTransportTest(unittest.TestCase):
    def test_default_snapshot_names_every_unmet_gate(self):
        report = execution_transport.prerequisites({})
        self.assertFalse(report["ready"])
        self.assertFalse(report["complete"])
        for name in (
            "mode_paper", "live_consent", "kill_switch_open", "loss_policy_approved",
            "risk_allows_send", "intent_verified", "spend_verified", "slippage_verified",
            "blockhash_checked", "last_valid_block_height", "reservation_held", "idempotent",
        ):
            self.assertIn(name, report["unmet"])
        self.assertEqual(execution_transport.status()["send"], "stub")
        self.assertFalse(execution_transport.status()["wired"])

    def test_satisfied_gates_still_do_not_sign_or_send(self):
        result = execution_transport.send(_ready())
        self.assertEqual(result["unmet"], [])
        self.assertEqual(result["status"], "stub")
        self.assertFalse(result["ready"])
        self.assertFalse(result["complete"])
        self.assertIsNone(result["signature"])
        self.assertFalse(result["send"])
        self.assertFalse(result["executable"])
        self.assertFalse(result["live_ready"])

    def test_reservation_idempotency_and_invented_freshness_fail_closed(self):
        over = _ready()
        over["reservation"] = {"status": "reserved", "intent_id": "intent-1", "usd": "100.01"}
        self.assertIn("reservation_held", execution_transport.prerequisites(over)["unmet"])
        cap = _ready()
        cap["reservation"] = {"status": "reserved", "intent_id": "intent-1", "usd": "100.00"}
        self.assertNotIn("reservation_held", execution_transport.prerequisites(cap)["unmet"])
        duplicate = _ready()
        duplicate["used_intent_ids"] = ["intent-1"]
        self.assertIn("idempotent", execution_transport.prerequisites(duplicate)["unmet"])
        invented = _ready()
        invented["freshness_guarantee"] = True
        self.assertIn("freshness_not_invented", execution_transport.prerequisites(invented)["unmet"])
        live = _ready()
        live["mode"] = "LIVE"
        self.assertIn("mode_paper", execution_transport.prerequisites(live)["unmet"])

    def test_fake_signature_status_is_not_a_confirmation(self):
        finalized = {"value": [{"confirmationStatus": "finalized", "err": None, "slot": 12}]}
        idle = execution_transport.reconcile({"intent_id": "intent-1"}, finalized)
        self.assertEqual(idle["status"], "not_run")
        self.assertIsNone(idle["confirmation"])
        self.assertFalse(idle["send"])
        claimed = execution_transport.reconcile({"sent": True, "signature": "sig"}, finalized)
        self.assertEqual(claimed["status"], "refused")
        self.assertIsNone(claimed["confirmation"])
        self.assertFalse(claimed["executable"])
