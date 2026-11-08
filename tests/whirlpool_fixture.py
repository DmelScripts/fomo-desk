"""Fake read RPC built from the saved tick fixture. Tests only."""
import base64
import json
from pathlib import Path

import phantom_transport
import solana_direct_swap
import solana_tx
import solana_whirlpool

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "sol_usdc_ts8_ticks.json"


def _b64(raw: bytes) -> dict:
    return {"owner": "", "lamports": 0, "data": [base64.b64encode(raw).decode("ascii"), "base64"]}


class FixtureRpc:
    def __init__(self, slot: int = 466547240, drop: str | None = None):
        self.slot = slot
        self.drop = drop
        self.calls = 0
        self.methods = []

    def call(self, method, params):
        self.calls += 1
        self.methods.append(method)
        if method != "getMultipleAccounts":
            raise AssertionError(method)
        addresses = params[0]
        encoded = _accounts()
        values = []
        for address in addresses:
            if address == self.drop:
                values.append(None)
                continue
            item = encoded.get(address)
            values.append(None if item is None else item)
        return {"context": {"slot": self.slot}, "value": values}


def _accounts(wsol_amount: int | None = None) -> dict:
    body = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    pool = solana_direct_swap.SOL_USDC_POOL
    vault_a = solana_direct_swap.SOL_USDC_OBSERVATION["vault_a"]
    vault_b = solana_direct_swap.SOL_USDC_OBSERVATION["vault_b"]
    data = solana_whirlpool.reference_pool(
        solana_tx.NATIVE_MINT, vault_a, solana_tx.USDC_MINT, vault_b,
        body["tickSpacing"], body["tickCurrentIndex"], int(body["sqrtPrice"]),
        fee_rate=body["feeRate"], liquidity=int(body["liquidity"]),
    )
    wallet = phantom_transport.REQUIRED_WALLET
    wsol = solana_tx.associated_token_address(wallet, solana_tx.NATIVE_MINT, solana_tx.TOKEN_PROGRAM)
    usdc = solana_tx.associated_token_address(wallet, solana_tx.USDC_MINT, solana_tx.TOKEN_PROGRAM)
    usdc_raw = bytearray(solana_whirlpool.reference_token_account(solana_tx.USDC_MINT, wallet))
    usdc_raw[64:72] = int(solana_direct_swap.KNOWN_PREEXISTING_USDC).to_bytes(8, "little")
    wsol_mint = bytearray(82)
    wsol_mint[44] = 9
    usdc_mint = bytearray(82)
    usdc_mint[44] = 6
    accounts = {
        pool: (solana_whirlpool.PROGRAM_ID, data),
        vault_a: (solana_tx.TOKEN_PROGRAM, solana_whirlpool.reference_token_account(solana_tx.NATIVE_MINT, pool)),
        vault_b: (solana_tx.TOKEN_PROGRAM, solana_whirlpool.reference_token_account(solana_tx.USDC_MINT, pool)),
        solana_tx.NATIVE_MINT: (solana_tx.TOKEN_PROGRAM, bytes(wsol_mint)),
        solana_tx.USDC_MINT: (solana_tx.TOKEN_PROGRAM, bytes(usdc_mint)),
        usdc: (solana_tx.TOKEN_PROGRAM, bytes(usdc_raw)),
    }
    for arr in body["arrays"]:
        raw = bytearray(solana_whirlpool.reference_tick_array(pool, arr["start"]))
        for tick, net in arr["initialized"]:
            offset = 12 + ((tick - arr["start"]) // body["tickSpacing"]) * 113
            raw[offset] = 1
            raw[offset + 1:offset + 17] = int(net).to_bytes(16, "little", signed=True)
        accounts[solana_whirlpool.tick_array_address(pool, arr["start"])] = (solana_whirlpool.PROGRAM_ID, bytes(raw))
    encoded = {}
    for address, (owner, raw) in accounts.items():
        item = _b64(raw)
        item["owner"] = owner
        encoded[address] = item
    if wsol_amount is None:
        encoded[wsol] = None
    else:
        raw = bytearray(solana_whirlpool.reference_token_account(solana_tx.NATIVE_MINT, wallet))
        raw[64:72] = int(wsol_amount).to_bytes(8, "little")
        present = _b64(bytes(raw))
        present["owner"] = solana_tx.TOKEN_PROGRAM
        present["lamports"] = solana_direct_swap.RENT_EXEMPTION_165
        encoded[wsol] = present
    return encoded


class FakeTransport:
    """JSON-RPC envelopes for the read methods. This does not open a socket."""

    def __init__(self, slot: int = 466547240, fee: int = 6000, height: int = 900, last_valid: int = 5000, valid: bool = True, sim_err=None, drop: str | None = None, tip: int | None = None, wsol_amount: int | None = None):
        import solana_tx
        self.slot = slot
        self.fee = fee
        self.height = height
        self.last_valid = last_valid
        self.valid = valid
        self.sim_err = sim_err
        self.drop = drop
        self.tip = slot + 1 if tip is None else tip
        self.wsol_amount = wsol_amount
        self.payloads = []
        self.statuses = None
        self.transaction = None
        self.blockhash = solana_tx.b58encode(b"\x44" * 32)

    def __call__(self, payload):
        self.payloads.append(payload)
        method = payload["method"]
        if method == "getGenesisHash":
            import solana_read_rpc
            return {"jsonrpc": "2.0", "id": 1, "result": solana_read_rpc.MAINNET_GENESIS}
        if method == "getMultipleAccounts":
            encoded = _accounts(self.wsol_amount)
            values = []
            for address in payload["params"][0]:
                if address == self.drop:
                    values.append(None)
                    continue
                values.append(encoded.get(address))
            return {"jsonrpc": "2.0", "id": 1, "result": {"context": {"slot": self.slot}, "value": values}}
        if method == "getMinimumBalanceForRentExemption":
            return {"jsonrpc": "2.0", "id": 1, "result": 1488440}
        if method == "getSlot":
            return {"jsonrpc": "2.0", "id": 1, "result": self.tip}
        if method == "getLatestBlockhash":
            return {"jsonrpc": "2.0", "id": 1, "result": {"context": {"slot": self.slot}, "value": {"blockhash": self.blockhash, "lastValidBlockHeight": self.last_valid}}}
        if method == "getFeeForMessage":
            return {"jsonrpc": "2.0", "id": 1, "result": {"context": {"slot": self.slot}, "value": self.fee}}
        if method == "isBlockhashValid":
            return {"jsonrpc": "2.0", "id": 1, "result": {"context": {"slot": self.slot}, "value": self.valid}}
        if method == "getBlockHeight":
            return {"jsonrpc": "2.0", "id": 1, "result": self.height}
        if method == "getSignatureStatuses":
            return {"jsonrpc": "2.0", "id": 1, "result": self.statuses}
        if method == "getTransaction":
            return {"jsonrpc": "2.0", "id": 1, "result": self.transaction}
        if method == "simulateTransaction":
            return {"jsonrpc": "2.0", "id": 1, "result": {"context": {"slot": self.slot}, "value": {"err": self.sim_err, "unitsConsumed": 12000}}}
        return {"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": method}}


def reader(slot: int = 466547240, **kwargs):
    import solana_read_rpc
    return solana_read_rpc.ReadRpc(FakeTransport(slot=slot, **kwargs))
