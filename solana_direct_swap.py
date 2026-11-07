"""Direct Orca Whirlpool swap for one SOL to USDC readiness amount.

This module is not mounted. The production route is still the guarded Phantom
CLI, and the Jupiter route_v2 blockers stay in place. The layout is the
published 11-account `swap` instruction, not `swap_v2` and not Jupiter.

Sources used for the bytes:
- programs/whirlpool/src/instructions/swap.rs (account order and the
  exact-in comparison other_amount_threshold > output)
- ts-sdk generated swap.ts at f6d9b9e1 (discriminator and field order)
- programs/whirlpool/src/math/tick_math.rs (sqrt-price bounds)
- programs/whirlpool/src/lib.rs (parameter meanings)
- legacy swap-utils calculateSwapAmountsFromQuote: exact-in sets
  other_amount_threshold from the estimated output reduced by slippage.
  The unprotected SDK default for that field is 0.

The quoted output is computed from the pool fee, liquidity, sqrt price, and
the tick arrays' liquidity_net fields. It is not a caller-supplied number.
A passing byte check is still not a live quote of the mainnet pool: its tick
arrays were not read. Wrapping SOL is the create, transfer, sync, and close
sequence around that swap. The route stays disabled.
"""

from __future__ import annotations

import base64
import hashlib
import time

import phantom_transport
import solana_tx
import solana_whirlpool
import solana_whirlpool_quote

# docs.orca.so names this tick-spacing-8 SOL/USDC pool. The same address is the
# PDA ["whirlpool", mainnet config, wSOL, USDC, spacing 8] under the Whirlpool
# program. One confirmed getAccountInfo was read after that match.
SOL_USDC_POOL = "7qbRF6YsyGuLUVs6Y1q64bdVrfe4ZcUUz1JRdoVNUJnm"
SOL_USDC_CONFIG = "2LecshUwdy9xi7meFgHtFJQNSKk4KdTrcpvaB56dP2NQ"
MIN_CONTEXT_SLOT = 466540548
RENT_EXEMPTION_165 = 1488440
SOL_USDC_SNAPSHOT = {
    "method": "getMultipleAccounts",
    "commitment": "confirmed",
    "min_context_slot": MIN_CONTEXT_SLOT,
    "context_slot": 466547240,
    "min_context_slot_used": 466545251,
    "tick_current_index": -24091,
    "sqrt_price": 5531210526019943285,
    "liquidity": 576950119138,
    "fee_rate": 500,
    "protocol_fee_rate": 1300,
    "tick_spacing": 8,
    "fee_tier_seed": 8,
    "adaptive_fee": False,
    "discriminator_ok": True,
    "vault_a_amount": 1098283677722,
    "vault_b_amount": 103331159477,
    "wallet_wsol_ata_exists": False,
    "wallet_usdc_ata_amount": 188718141,
    "wallet_usdc_ata_lamports": 1488440,
    "wsol_decimals": 9,
    "usdc_decimals": 6,
    "tick_array_starts": (-26048, -25344, -24640, -23936, -23232),
    "tick_array_addresses": (
        "62AC6bi6g4DSUdKcXQYEM4EzWm53zgZg66CWHJrbxeqy",
        "4nyKDVYimL5pFkjZg5TtuxYjecVqaCEBHnqHVJk7bRWs",
        "7nDppuGeG1KBH6hY6Tctg4G2aVoaQYka4A7cFiY3RcNF",
        "6U6pVLPnquaocYi3rLBwMxHegVtZDoCUF2TttX3UzjkF",
        "Hty4rPfcu3LGjmLKRJymy4XGUS6i8n6eXesUs7H7PrDp",
    ),
    "tick_arrays_present": True,
    "tick_arrays_match_current_tick": True,
    "tick_array_layout": "fixed",
    "tick_array_discriminator": "4561bdbe6e0742bb",
    "i32_seed_accounts_present": False,
    "quote": "computed",
    "token_est_out": 898631,
    "token_min_out": 889644,
    "trade_fee": 5000,
    "entry_lamports": 88979314,
    "entry_token_est_out": 7995630,
    "entry_token_min_out": 7915673,
    "entry_trade_fee_lamports": 44490,
    "exit_token_est_out": 88882136,
    "exit_token_min_out": 87993314,
    "exit_trade_fee_usdc_raw": 3998,
    "marginal_micro_usdc_per_sol": 89908537,
    "future_exit_guaranteed": False,
    "quote_reason": None,
    "specified_slippage_bps": 100,
    "rent_exemption_165": RENT_EXEMPTION_165,
    "sdk_package": "@orca-so/whirlpools-core@3.1.1",
    "sdk_token_est_out": 898405,
    "sdk_token_min_out": 889420,
    "sdk_trade_fee": 5000,
    "wallet_observed_slot": 466547240,
}
SOL_USDC_OBSERVATION = {
    "context_slot": 466540548,
    "tick_spacing": 8,
    "fee_tier_seed": 8,
    "adaptive_fee": False,
    "fee_rate": 500,
    "protocol_fee_rate": 1300,
    "mint_a": solana_tx.NATIVE_MINT,
    "mint_b": solana_tx.USDC_MINT,
    "vault_a": "9RfZwn2Prux6QesG1Noo4HzMEBv3rPndJ2bN2Wwd6a7p",
    "vault_b": "BVNo8ftg2LkkssnWT4ZWdtoFaevnfD6ExYeramwM27pe",
    "liquidity": 576950119138,
    "sqrt_price": 5530728732878665336,
    "tick_current_index": -24093,
    "data_len": 653,
    "discriminator_ok": True,
    "tick_arrays_read": False,
}

ENABLED = False
INPUT_LAMPORTS = 10_000_000
MARKET = "SOL->USDC"
DIAGNOSTIC = True
SOL_DECIMALS = 9
USDC_DECIMALS = 6
TICKET_USD_MICRO = 8_000_000
ALLOCATION_USD_MICRO = 100_000_000
MAX_FEE_USD_MICRO = 50_000
BASE_FEE_LAMPORTS = 5_000
KNOWN_PREEXISTING_USDC = 188_718_141
DISC = hashlib.sha256(b"global:swap").digest()[:8]
PUBLISHED_DISC = bytes([248, 198, 158, 145, 225, 117, 135, 200])
MIN_SQRT_PRICE = 4295048016
MAX_SQRT_PRICE = 79226673515401279992447579055
_DATA_LEN = 42
_TICK_ZERO_SQRT = 18446744073709551616
PLACEHOLDER_BLOCKHASH = solana_tx.b58encode(b"\x22" * 32)


def status() -> dict:
    return {
        "route": "direct_whirlpool_swap",
        "mounted": False,
        "readonly_prepare_mounted": True,
        "enabled": ENABLED,
        "strategies_enabled": [],
        "diagnostic_market": MARKET,
        "production_path": "phantom_cli_unchanged",
        "market": MARKET,
        "input_lamports": INPUT_LAMPORTS,
        "fee_policy": "not_approved",
        "aggregate_loss_policy": False,
        "live_ready": False,
        "output_computed_from_pool": False,
        "adaptive_fee_extra_account": "classic_no_tail" if SOL_USDC_OBSERVATION["adaptive_fee"] is False else "oracle_only",
        "wrapped_sol_setup": "create_transfer_sync_close",
        "live_quote": "computed",
        "specified_slippage_bps": 100,
        "chat_conditional_live_approval": "not_a_control",
        "recorded_live_applied": False,
        "sol_usdc_pool": SOL_USDC_POOL,
        "observed_context_slot": SOL_USDC_OBSERVATION["context_slot"],
    }


def derived_sol_usdc_pool(tick_spacing: int = 8) -> tuple[str, int]:
    """PDA from the published config, mints, and tick-spacing seed. Not a guessed address."""
    derived, bump = solana_tx.find_program_address(
        [
            b"whirlpool",
            solana_tx.b58decode(SOL_USDC_CONFIG),
            solana_tx.b58decode(solana_tx.NATIVE_MINT),
            solana_tx.b58decode(solana_tx.USDC_MINT),
            int(tick_spacing).to_bytes(2, "little"),
        ],
        solana_tx.b58decode(solana_whirlpool.PROGRAM_ID),
    )
    return solana_tx.b58encode(derived), bump


def context_binds(slot: int, snapshot_slot: int | None = None) -> bool:
    """A quote slot has to clear the floor and, when a snapshot exists, equal it."""
    if isinstance(slot, bool) or not isinstance(slot, int) or slot < MIN_CONTEXT_SLOT:
        return False
    if snapshot_slot is not None and slot != snapshot_slot:
        return False
    return True


def explicit_rent_allowed(lamports: int) -> bool:
    """An explicit rent figure cannot exceed the rent-exempt read. It is not spent."""
    return isinstance(lamports, int) and not isinstance(lamports, bool) and 0 <= lamports <= RENT_EXEMPTION_165


def close_allowed(account_exists: bool) -> bool:
    """CloseAccount refunds the whole lamport balance. An existing ATA is not closed."""
    return account_exists is False


def snapshot_status() -> dict:
    """String-seed tick arrays were present. The route is still not mounted."""
    snap = SOL_USDC_SNAPSHOT
    fresh = context_binds(snap["context_slot"], snap["context_slot"]) and snap["context_slot"] >= snap["min_context_slot"]
    return {
        "quote": snap["quote"],
        "reason": snap["quote_reason"],
        "context_slot": snap["context_slot"],
        "context_fresh": fresh,
        "adaptive_fee": snap["adaptive_fee"],
        "tick_arrays_present": snap["tick_arrays_present"],
        "tick_arrays_match_current_tick": snap["tick_arrays_match_current_tick"],
        "wsol_ata_exists": snap["wallet_wsol_ata_exists"],
        "close_wsol": close_allowed(snap["wallet_wsol_ata_exists"]),
        "close_usdc": False,
        "usdc_ata_amount": snap["wallet_usdc_ata_amount"],
        "rent_exemption_165": snap["rent_exemption_165"],
        "enabled": False,
        "mounted": False,
        "live_ready": False,
        "five_agents": False,
    }


def marginal_micro_usdc_per_sol(sqrt_price: int) -> int:
    """Floor of the pool's marginal USDC price. A caller-supplied spot is not used."""
    if isinstance(sqrt_price, bool) or not isinstance(sqrt_price, int) or sqrt_price <= 0:
        raise ValueError("sqrt price is not usable")
    return (sqrt_price * sqrt_price * 10**SOL_DECIMALS) // (1 << 128)


def lamports_within_usd_cap(micro_usdc_cap: int, micro_per_sol: int) -> int:
    """Round down so the marginal value stays inside the cap. Residual is unspent."""
    if isinstance(micro_usdc_cap, bool) or isinstance(micro_per_sol, bool):
        return 0
    if not isinstance(micro_usdc_cap, int) or not isinstance(micro_per_sol, int):
        return 0
    if micro_usdc_cap <= 0 or micro_per_sol <= 0:
        return 0
    if micro_usdc_cap >= 10**18 or micro_per_sol >= 10**18:
        return 0
    return (micro_usdc_cap * 10**SOL_DECIMALS) // micro_per_sol


def sizing() -> dict:
    """$8 is a cap. The probe is not that ticket, and the fill is requoted."""
    snap = SOL_USDC_SNAPSHOT
    micro = marginal_micro_usdc_per_sol(snap["sqrt_price"])
    lamports = lamports_within_usd_cap(TICKET_USD_MICRO, micro)
    marginal_value = (lamports * micro) // 10**SOL_DECIMALS
    rent_value = (RENT_EXEMPTION_165 * micro) // 10**SOL_DECIMALS
    return {
        "diagnostic_probe_lamports": INPUT_LAMPORTS,
        "probe_usdc_raw": snap["token_est_out"],
        "probe_usdc": f"{snap['token_est_out'] / 10 ** USDC_DECIMALS:.6f}",
        "marginal_micro_usdc_per_sol": micro,
        "fx_slot": snap["context_slot"],
        "entry_lamports": lamports,
        "marginal_value_micro_usdc": marginal_value,
        "unspent_micro_usdc": TICKET_USD_MICRO - marginal_value,
        "quoted_out_micro_usdc": snap["entry_token_est_out"],
        "quoted_under_cap": snap["entry_token_est_out"] <= TICKET_USD_MICRO,
        "dex_fee_lamports": snap["entry_trade_fee_lamports"],
        "rent_deposit_lamports": RENT_EXEMPTION_165,
        "rent_deposit_micro_usdc": rent_value,
        "rent_refundable": snap["wallet_wsol_ata_exists"] is False,
        "probe_is_ticket": False,
        "usd_spend_is_exact": False,
        "ticket_usd": "8.00",
        "bankroll_usd": "100.00",
    }


def route_guard() -> dict:
    """Snapshot quotes are checked. They are not a future liquidity guarantee."""
    size = sizing()
    verified = (
        size["entry_lamports"] == SOL_USDC_SNAPSHOT["entry_lamports"]
        and size["quoted_under_cap"]
        and size["unspent_micro_usdc"] >= 0
        and SOL_USDC_SNAPSHOT["exit_token_est_out"] > 0
    )
    return {
        "swap_path_verified": False,
        "snapshot_matches_recorded_quote": verified,
        "future_exit_guaranteed": False,
        "diagnostic": True,
        "probe_is_ticket": False,
        "exit_quote": "quoted_on_snapshot",
        "exit_starts_missing": (),
        "sign": False,
        "blockers": (
            "policy rows stay unapproved until the protected route records them",
            "a placeholder blockhash is not broadcastable",
            "this snapshot does not guarantee later exit liquidity",
            "the signer stays off",
        ),
    }


def quote_exit_exact_in(pool: dict, arrays: list[dict], usdc_raw: int, slippage_bps: int, context_slot: int) -> dict:
    """USDC to SOL using the supplied tick arrays. This is not a later-liquidity promise."""
    quoted = solana_whirlpool_quote.quote_exact_in(pool, arrays, usdc_raw, False, slippage_bps, 0, context_slot)
    quoted["direction"] = "USDC->SOL"
    quoted["usdc_decimals"] = USDC_DECIMALS
    quoted["sol_decimals"] = SOL_DECIMALS
    quoted["future_exit_guaranteed"] = False
    return quoted


def batch_addresses() -> tuple[str, ...]:
    """One getMultipleAccounts list. A moved tick is a miss, not a second call."""
    pool = SOL_USDC_POOL
    wsol = solana_tx.associated_token_address(phantom_transport.REQUIRED_WALLET, solana_tx.NATIVE_MINT, solana_tx.TOKEN_PROGRAM)
    usdc = solana_tx.associated_token_address(phantom_transport.REQUIRED_WALLET, solana_tx.USDC_MINT, solana_tx.TOKEN_PROGRAM)
    return (
        pool,
        *SOL_USDC_SNAPSHOT["tick_array_addresses"],
        SOL_USDC_OBSERVATION["vault_a"],
        SOL_USDC_OBSERVATION["vault_b"],
        solana_tx.NATIVE_MINT,
        solana_tx.USDC_MINT,
        wsol,
        usdc,
    )


def fetch_state(rpc) -> dict:
    """Parse one public getMultipleAccounts result. This does not open a socket itself."""
    failed = {"ok": False, "fetched": False, "retry": False, "sign": False, "broadcastable": False, "runtime_ready": False}
    if rpc is None or not hasattr(rpc, "call"):
        return failed | {"reason": "rpc_not_configured"}
    addresses = list(batch_addresses())
    try:
        result = rpc.call("getMultipleAccounts", [addresses, {"encoding": "base64", "commitment": "confirmed"}])
    except Exception:
        return failed | {"reason": "rpc_failed"}
    if not isinstance(result, dict):
        return failed | {"reason": "rpc_shape"}
    context = result.get("context")
    values = result.get("value")
    slot = context.get("slot") if isinstance(context, dict) else None
    if isinstance(slot, bool) or not isinstance(slot, int) or slot < MIN_CONTEXT_SLOT:
        return failed | {"reason": "slot_unusable"}
    if not isinstance(values, list) or len(values) != len(addresses):
        return failed | {"reason": "batch_shape"}
    decoded: list[tuple[str, bytes] | None] = []
    for item in values:
        if item is None:
            decoded.append(None)
            continue
        if not isinstance(item, dict) or not isinstance(item.get("owner"), str):
            return failed | {"reason": "account_shape"}
        data = item.get("data")
        if not isinstance(data, list) or not data or not isinstance(data[0], str):
            return failed | {"reason": "account_shape"}
        try:
            raw = base64.b64decode(data[0], validate=True)
        except Exception:
            return failed | {"reason": "account_shape"}
        decoded.append((item["owner"], raw))
    by_addr = dict(zip(addresses, decoded))
    pool_rec = by_addr.get(SOL_USDC_POOL)
    if pool_rec is None or pool_rec[0] != solana_whirlpool.PROGRAM_ID:
        return failed | {"reason": "pool_unread"}
    state = solana_whirlpool.pool_state(pool_rec[1])
    if state is None:
        return failed | {"reason": "pool_unreadable"}
    if state["adaptive_fee"] or state["tick_spacing"] != 8:
        return failed | {"reason": "pool_unsupported"}
    for start in _covered_starts(state):
        address = solana_whirlpool.tick_array_address(SOL_USDC_POOL, start)
        record = by_addr.get(address)
        parsed = None if record is None else solana_whirlpool.tick_array_state(record[1])
        if record is None or record[0] != solana_whirlpool.PROGRAM_ID or parsed is None:
            return failed | {"reason": "tick_array_missing"}
        if parsed["pool"] != SOL_USDC_POOL or parsed["start_tick_index"] != start:
            return failed | {"reason": "tick_array_unbound"}
    wsol_mint = by_addr.get(solana_tx.NATIVE_MINT)
    usdc_mint = by_addr.get(solana_tx.USDC_MINT)
    if wsol_mint is None or usdc_mint is None or len(wsol_mint[1]) < 45 or len(usdc_mint[1]) < 45:
        return failed | {"reason": "mint_unread"}
    if wsol_mint[1][44] != SOL_DECIMALS or usdc_mint[1][44] != USDC_DECIMALS:
        return failed | {"reason": "decimals"}
    wsol_ata = solana_tx.associated_token_address(phantom_transport.REQUIRED_WALLET, solana_tx.NATIVE_MINT, solana_tx.TOKEN_PROGRAM)
    usdc_ata = solana_tx.associated_token_address(phantom_transport.REQUIRED_WALLET, solana_tx.USDC_MINT, solana_tx.TOKEN_PROGRAM)
    usdc_rec = by_addr.get(usdc_ata)
    if usdc_rec is None or usdc_rec[0] != solana_tx.TOKEN_PROGRAM or len(usdc_rec[1]) < 72:
        return failed | {"reason": "usdc_ata_unread"}
    wsol_exists = by_addr.get(wsol_ata) is not None
    accounts: dict[str, tuple[str, bytes]] = {}
    for address, record in by_addr.items():
        if record is not None and address not in {solana_tx.NATIVE_MINT, solana_tx.USDC_MINT}:
            accounts[address] = record
    if not wsol_exists:
        accounts[wsol_ata] = (
            solana_tx.TOKEN_PROGRAM,
            solana_whirlpool.reference_token_account(solana_tx.NATIVE_MINT, phantom_transport.REQUIRED_WALLET),
        )
    return {
        "ok": True,
        "fetched": True,
        "retry": False,
        "reason": None,
        "context_slot": slot,
        "accounts": accounts,
        "pool": SOL_USDC_POOL,
        "pool_data": pool_rec[1],
        "state": state,
        "wsol_exists": wsol_exists,
        "usdc_raw": int.from_bytes(usdc_rec[1][64:72], "little"),
        "wsol_decimals": SOL_DECIMALS,
        "usdc_decimals": USDC_DECIMALS,
        "sqrt_price": state["sqrt_price"],
        "sign": False,
        "broadcastable": False,
        "runtime_ready": False,
    }


def prepare_from_state(fetched: dict) -> dict:
    """Quote and hash one fetched state. The unsigned transaction is not returned."""
    empty = {
        "ok": False,
        "fetched": bool(fetched.get("fetched")) if isinstance(fetched, dict) else False,
        "stale": True,
        "sign": False,
        "cli": False,
        "diagnostic": True,
        "broadcastable": False,
        "runtime_ready": False,
        "unsigned_tx_sha256": None,
        "strategies_enabled": [],
    }
    if not isinstance(fetched, dict) or fetched.get("ok") is not True:
        empty["reason"] = None if not isinstance(fetched, dict) else fetched.get("reason")
        return empty
    try:
        fx = fx_from_pool(fetched["sqrt_price"], fetched["wsol_decimals"], fetched["usdc_decimals"], fetched["context_slot"])
    except (KeyError, TypeError, ValueError):
        empty["reason"] = "fx"
        return empty
    if fx is None:
        empty["reason"] = "fx"
        return empty
    micro = fx["micro_usdc_per_sol"]
    slot = fetched["context_slot"]
    pool = fetched["pool"]
    data = fetched["pool_data"]
    accounts = fetched["accounts"]
    spend = lamports_within_usd_cap(TICKET_USD_MICRO, micro)
    blockhash = fetched.get("blockhash")
    fresh = quote_is_fresh(fetched.get("fetched_at"), time.time(), slot)
    last_valid = fetched.get("last_valid_block_height")
    if blockhash in (None, PLACEHOLDER_BLOCKHASH) or not fresh or not isinstance(last_valid, int) or isinstance(last_valid, bool):
        empty["reason"] = "stale" if not fresh else "blockhash"
        empty["stale"] = not fresh or blockhash in (None, PLACEHOLDER_BLOCKHASH)
        empty["slot_is_ttl"] = False
        return empty
    try:
        tx = build(
            pool, data, accounts, SOL_USDC_SNAPSHOT["specified_slippage_bps"],
            MIN_SQRT_PRICE + 1, blockhash, slot, amount=spend,
        )
        checked = inspect(
            tx, accounts, SOL_USDC_SNAPSHOT["specified_slippage_bps"], slot,
            wsol_existed=bool(fetched["wsol_exists"]),
        )
        state = solana_whirlpool.pool_state(data)
        arrays = _quote_arrays(pool, data, accounts, True)
        quote = solana_whirlpool_quote.quote_exact_in(
            state, arrays, spend, True, SOL_USDC_SNAPSHOT["specified_slippage_bps"], MIN_SQRT_PRICE + 1, slot,
        )
    except (ValueError, solana_whirlpool.WhirlpoolError, solana_whirlpool_quote.QuoteError):
        empty["reason"] = "quote"
        empty["stale"] = False
        return empty
    fees = fee_report(
        quote["trade_fee"], micro, bool(fetched["wsol_exists"]), spend,
        network_fee_lamports=fetched.get("network_fee_lamports"),
        rent_lamports=fetched.get("rent_lamports"),
    )
    entry_raw = base64.b64decode(tx)
    entry_message = solana_tx.message_bytes(tx)
    public = {
        "ok": checked["byte_checks_passed"] is True and fees["fee_cap_ok"] and fees["exposure_cap_ok"],
        "fetched": True,
        "stale": False,
        "sign": False,
        "cli": False,
        "diagnostic": True,
        "probe_is_ticket": False,
        "broadcastable": False,
        "runtime_ready": False,
        "placeholder": False,
        "slot_is_ttl": False,
        "context_slot": slot,
        "network": "solana:mainnet",
        "wallet": phantom_transport.REQUIRED_WALLET,
        "entry_lamports": spend,
        "micro_usdc_per_sol": micro,
        "wsol_exists": bool(fetched["wsol_exists"]),
        "quote_token_est_out": quote["token_est_out"],
        "quote_token_min_out": quote["token_min_out"],
        "quote_trade_fee": quote["trade_fee"],
        "usd_spend_is_exact": False,
        "unsigned_tx_sha256": hashlib.sha256(entry_raw).hexdigest(),
        "message_sha256": hashlib.sha256(entry_message).hexdigest(),
        "exit_tx_sha256": None,
        "exit_executable": False,
        "exit_preview_only": True,
        "byte_checks_passed": checked["byte_checks_passed"],
        "min_out": checked["min_out"],
        "fx_slot": slot,
        "fx_bound_to_policy": False,
        "fetched_at": fetched.get("fetched_at"),
        "state_age_s": round(time.time() - float(fetched["fetched_at"]), 6),
        "wallet_usdc_raw": fetched["usdc_raw"],
        "exit_plan_raw": None,
        "exit_quote_preview_out": None,
        "exit_close_raw": None,
        "exit_allowed": False,
        "exit_uses_wallet_balance": False,
        "exit_closes_usdc": False,
        "strategies_enabled": [],
        "blockhash": blockhash,
        "last_valid_block_height": fetched.get("last_valid_block_height"),
        "block_height": fetched.get("block_height"),
        **fees,
    }
    # Kept for the in-process simulate, then discarded by the caller. Not a response field.
    public["rent_lamports"] = fetched.get("rent_lamports")
    public["_entry_tx"] = tx
    public["_exit_tx"] = None
    return public


def prepare_diagnostic(slot: int, rpc=None) -> dict:
    """Fetch through the adapter when an RPC is injected. No fixture and no socket here."""
    fetched = fetch_state(rpc)
    if not fetched.get("ok"):
        return {
            "stale": True,
            "fetched": False,
            "sign": False,
            "cli": False,
            "diagnostic": True,
            "broadcastable": False,
            "runtime_ready": False,
            "unsigned_tx_sha256": None,
            "context_slot": slot,
            "reason": fetched.get("reason"),
        }
    if slot != fetched["context_slot"]:
        return {
            "stale": True,
            "fetched": True,
            "sign": False,
            "cli": False,
            "diagnostic": True,
            "broadcastable": False,
            "runtime_ready": False,
            "unsigned_tx_sha256": None,
            "context_slot": fetched["context_slot"],
            "reason": "slot_mismatch",
        }
    return prepare_from_state(fetched)


def fx_from_pool(sqrt_price: int, wsol_decimals: int, usdc_decimals: int, slot: int) -> dict | None:
    """Recompute the pool price. The policy row does not carry this slot."""
    if wsol_decimals != SOL_DECIMALS or usdc_decimals != USDC_DECIMALS:
        return None
    if isinstance(slot, bool) or not isinstance(slot, int) or slot < MIN_CONTEXT_SLOT:
        return None
    return {
        "micro_usdc_per_sol": marginal_micro_usdc_per_sol(sqrt_price),
        "slot": slot,
        "wsol_decimals": SOL_DECIMALS,
        "usdc_decimals": USDC_DECIMALS,
        "bound_to_policy": False,
    }


def priority_lamports(price: int | None, limit: int | None) -> int:
    """Lamports from a compute-unit price. A missing price is zero, not an omission."""
    if price is None or isinstance(price, bool) or not isinstance(price, int) or price <= 0:
        return 0
    used = 200_000 if limit is None else limit
    if isinstance(used, bool) or not isinstance(used, int) or used < 0:
        return 0
    return (price * used + 999_999) // 1_000_000


def network_fee_lamports(raw: bytes) -> tuple[int, int]:
    """Base signature fee plus priority fee. Priority alone is not the network fee."""
    decoded = solana_tx.decode_transaction(raw)
    base = BASE_FEE_LAMPORTS * max(1, len(decoded.signers))
    price = None
    limit = None
    for instruction in decoded.instructions:
        program = decoded.static_keys[instruction.program_index] if instruction.program_index < len(decoded.static_keys) else ""
        if program != solana_tx.COMPUTE_BUDGET:
            continue
        parsed = solana_tx.compute_budget_payload(instruction.data)
        if not parsed:
            continue
        tag, payload, _length = parsed
        if tag == 3:
            price = int.from_bytes(payload, "little")
        elif tag == 2:
            limit = int.from_bytes(payload, "little")
    return base, priority_lamports(price, limit)


def quote_is_fresh(fetched_at, now: float, slot: int | None = None) -> bool:
    """Wall-clock age of the fetch. The slot integer is not a TTL."""
    del slot
    if isinstance(fetched_at, bool) or not isinstance(fetched_at, (int, float)):
        return False
    if isinstance(now, bool) or not isinstance(now, (int, float)):
        return False
    age = float(now) - float(fetched_at)
    return 0 <= age <= 30


def _ceil_micro(lamports: int, micro_per_sol: int) -> int:
    """Ceiling conversion so a cap comparison does not understate the charge."""
    if lamports <= 0:
        return 0
    scale = 10**SOL_DECIMALS
    return (int(lamports) * int(micro_per_sol) + scale - 1) // scale


def fee_report(dex_lamports: int, micro: int, wsol_exists: bool, spend: int, *, network_fee_lamports: int | None, rent_lamports: int | None) -> dict:
    """Pool-micro figures use ceilings. They are not an approved USD quote."""
    priced = (
        isinstance(network_fee_lamports, int) and not isinstance(network_fee_lamports, bool) and network_fee_lamports >= 0
        and isinstance(rent_lamports, int) and not isinstance(rent_lamports, bool) and rent_lamports > 0
    )
    network = 0 if not priced else network_fee_lamports
    rent = 0 if wsol_exists or not priced else rent_lamports
    dex_micro = _ceil_micro(int(dex_lamports), micro)
    network_micro = _ceil_micro(int(network), micro)
    fee_micro = dex_micro + network_micro
    marginal = _ceil_micro(int(spend), micro)
    rent_micro = _ceil_micro(int(rent), micro)
    exposure = marginal + network_micro + rent_micro
    pool_fee_ok = priced and fee_micro <= MAX_FEE_USD_MICRO and dex_micro >= 0
    pool_ticket_ok = 0 < marginal <= TICKET_USD_MICRO
    pool_exposure_ok = priced and exposure <= ALLOCATION_USD_MICRO and pool_ticket_ok
    return {
        "dex_fee_lamports": int(dex_lamports),
        "network_fee_lamports": None if not priced else int(network_fee_lamports),
        "network_fee_from_chain": priced,
        "assumed_signature_fee": False,
        "dex_fee_micro_usdc": dex_micro,
        "network_fee_micro_usdc": network_micro,
        "fee_micro_usdc": fee_micro,
        "fee_unit": "pool_micro_usdc",
        "fee_cap_micro": MAX_FEE_USD_MICRO,
        "pool_fee_within_micro_cap": pool_fee_ok,
        "fee_cap_ok": False,
        "usd_cap_ok": False,
        "usd_source": "unapproved",
        "priority_only": False,
        "marginal_micro_usdc": marginal,
        "rent_lamports": None if not priced else int(rent_lamports),
        "rent_micro_usdc": rent_micro,
        "rent_is_fee": False,
        "exposure_micro_usdc": exposure,
        "pool_exposure_within_micro_cap": pool_exposure_ok,
        "exposure_cap_ok": False,
        "ticket_cap_ok": pool_ticket_ok,
    }


def apply_read_tail(public: dict, tail: dict) -> dict:
    """Attach the chain fee and block height. A missing fee does not fall back to 5,000."""
    if not tail.get("ok"):
        public["ok"] = False
        public["reason"] = tail.get("reason")
        return public
    fees = fee_report(
        public["quote_trade_fee"], public["micro_usdc_per_sol"], public["wsol_exists"], public["entry_lamports"],
        network_fee_lamports=tail.get("network_fee_lamports"), rent_lamports=public.get("rent_lamports"),
    )
    public.update(fees)
    public["block_height"] = tail.get("block_height")
    public["blockhash_valid"] = True
    height_ok = (
        isinstance(public["block_height"], int)
        and isinstance(public.get("last_valid_block_height"), int)
        and public["block_height"] <= public["last_valid_block_height"]
    )
    pool_ok = bool(public.get("byte_checks_passed") and fees["pool_fee_within_micro_cap"] and fees["pool_exposure_within_micro_cap"] and height_ok)
    public["technical_ok"] = pool_ok
    import spend_policy
    import usdc_valuation
    source = spend_policy.usd_source()
    limits = usdc_valuation.within_limits(
        fees["fee_micro_usdc"], fees["marginal_micro_usdc"], fees["exposure_micro_usdc"], source,
    ) if source.get("approved") else {"ok": False, "reason": source.get("reason")}
    public["usd_source"] = source.get("source")
    public["usd_reason"] = source.get("reason")
    public["accepted_band"] = None
    public["usd_cap_ok"] = limits.get("ok") is True
    public["fee_cap_ok"] = limits.get("fee_ok") is True
    public["exposure_cap_ok"] = limits.get("exposure_ok") is True and limits.get("ticket_ok") is True
    public["fee_usd"] = limits.get("fee_usd")
    public["ticket_usd_ceiling"] = limits.get("ticket_usd")
    public["exposure_usd_ceiling"] = limits.get("exposure_usd")
    public["ok"] = pool_ok and public["usd_cap_ok"]
    if not height_ok:
        public["reason"] = "blockhash_expired"
    elif not fees["pool_fee_within_micro_cap"] or not fees["pool_exposure_within_micro_cap"]:
        public["reason"] = "cap"
    elif not source.get("approved"):
        public["reason"] = source.get("reason") or "usd_source_unapproved"
    elif not public["usd_cap_ok"]:
        public["reason"] = "cap"
    else:
        public["reason"] = None
    return public


def exit_amount(acquired: int | None, wallet: int, *, confirmed: bool, minimum: int | None = None, estimated: int | None = None, ata_balance: int | None = None) -> dict:
    """Only a confirmed fill inside min and est, and not the preexisting balance."""
    refused = {"exit_raw": None, "allowed": False, "executable": False}
    if confirmed is not True or isinstance(acquired, bool) or not isinstance(acquired, int) or acquired <= 0:
        return refused | {"reason": "unconfirmed"}
    if isinstance(minimum, bool) or isinstance(estimated, bool) or not isinstance(minimum, int) or not isinstance(estimated, int):
        return refused | {"reason": "bounds_missing"}
    if not minimum <= acquired <= estimated:
        return refused | {"reason": "outside_fill"}
    if isinstance(wallet, int) and not isinstance(wallet, bool) and wallet >= 0 and acquired > wallet:
        return refused | {"reason": "above_balance"}
    if isinstance(ata_balance, int) and not isinstance(ata_balance, bool) and acquired > ata_balance:
        return refused | {"reason": "above_balance"}
    if acquired == wallet or acquired == KNOWN_PREEXISTING_USDC or acquired > KNOWN_PREEXISTING_USDC:
        return refused | {"reason": "wallet_balance"}
    return {"exit_raw": acquired, "allowed": True, "executable": True, "reason": None}


def revalidate_blockhash(tx_blockhash, source, slot: int) -> dict:
    """Read an injected blockhash source. Nothing here rewrites a transaction."""
    report = {
        "applied": False,
        "broadcastable": False,
        "runtime_ready": False,
        "sign": False,
        "checked": False,
        "simulated": False,
        "source_ok": False,
        "placeholder": tx_blockhash == PLACEHOLDER_BLOCKHASH or source is None,
        "reason": "not_fetched",
    }
    if source is None:
        return report
    try:
        got = source()
    except Exception:
        report["reason"] = "source_failed"
        return report
    if not isinstance(got, dict):
        report["reason"] = "source_shape"
        return report
    fresh = got.get("blockhash")
    simulation = got.get("simulation") if isinstance(got.get("simulation"), dict) else None
    simulated = simulation is not None and simulation.get("err") is None and got.get("simulated") is True
    slot_ok = got.get("slot") == slot and isinstance(got.get("last_valid_block_height"), int) and not isinstance(got.get("last_valid_block_height"), bool) and got["last_valid_block_height"] > 0
    fresh_ok = isinstance(fresh, str) and fresh != PLACEHOLDER_BLOCKHASH and got.get("placeholder") is not True
    report["simulated"] = simulated
    report["source_ok"] = bool(fresh_ok and simulated and slot_ok)
    report["placeholder"] = tx_blockhash == PLACEHOLDER_BLOCKHASH or not fresh_ok
    report["checked"] = bool(report["source_ok"] and tx_blockhash == fresh and tx_blockhash != PLACEHOLDER_BLOCKHASH)
    report["reason"] = None if report["checked"] else "not_applied"
    return report


def prove(tx_b64: str, accounts: dict, slot: int, wsol_exists: bool, *, network_fee_lamports: int | None = None, rent_lamports: int | None = None) -> dict:
    """Byte proof for one recognized swap. Caller flags are not read."""
    checked = inspect(tx_b64, accounts, SOL_USDC_SNAPSHOT["specified_slippage_bps"], slot, wsol_existed=wsol_exists)
    body = {
        "slippage_verified": False,
        "spend_verified": False,
        "byte_checks_passed": False,
        "fee_cap_ok": False,
        "exposure_cap_ok": False,
        "wallet_verified": False,
        "blockhash": checked.get("blockhash"),
        "error_category": checked.get("error_category"),
    }
    if checked.get("byte_checks_passed") is not True:
        return body
    pool = None
    try:
        raw = base64.b64decode(tx_b64, validate=True)
        decoded = solana_tx.decode_transaction(raw)
        swap = _wrap_plan(decoded)["swap"]
        pool = decoded.static_keys[swap.account_indexes[2]]
        record = accounts.get(pool)
        state = None if record is None else solana_whirlpool.pool_state(record[1])
        if state is None:
            return body
        parsed = decode_swap_data(swap.data)
        if pool != SOL_USDC_POOL or parsed is None or parsed["amount"] == INPUT_LAMPORTS:
            body["error_category"] = "pool" if pool != SOL_USDC_POOL else "probe"
            return body
        arrays = _quote_arrays(pool, record[1], accounts, True)
        quote = solana_whirlpool_quote.quote_exact_in(
            state, arrays, parsed["amount"], True, SOL_USDC_SNAPSHOT["specified_slippage_bps"],
            parsed["sqrt_price_limit"], slot,
        )
        fx = fx_from_pool(state["sqrt_price"], SOL_DECIMALS, USDC_DECIMALS, slot)
    except (ValueError, KeyError, TypeError, solana_whirlpool_quote.QuoteError, solana_whirlpool.WhirlpoolError):
        return body
    if fx is None or quote["token_min_out"] != parsed["other_amount_threshold"]:
        return body
    fees = fee_report(
        quote["trade_fee"], fx["micro_usdc_per_sol"], wsol_exists, parsed["amount"],
        network_fee_lamports=network_fee_lamports, rent_lamports=rent_lamports,
    )
    body.update(fees)
    # Caller-supplied account bytes are not a signing proof.
    body["pool_math_matches"] = True
    body["slippage_verified"] = False
    body["spend_verified"] = False
    body["byte_checks_passed"] = False
    body["fee_cap_ok"] = False
    body["usd_cap_ok"] = False
    body["wallet_verified"] = False
    body["error_category"] = "caller_state"
    return body


def _covered_starts(state: dict) -> list[int]:
    base = solana_whirlpool.start_tick_index(state["tick_current_index"], state["tick_spacing"])
    step = solana_whirlpool.TICK_ARRAY_SIZE * state["tick_spacing"]
    starts = []
    for index in range(-2, 3):
        start = base + index * step
        if start not in starts:
            starts.append(start)
    return starts


def _allowed_amounts(state: dict, *, allow_probe: bool = False) -> set[int]:
    """The round-down under the ticket. The 0.01 SOL probe is diagnostic only."""
    amounts = {INPUT_LAMPORTS} if allow_probe else set()
    micro = marginal_micro_usdc_per_sol(state["sqrt_price"])
    capped = lamports_within_usd_cap(TICKET_USD_MICRO, micro)
    if capped > 0:
        amounts.add(capped)
    return amounts


def paper_lifecycle() -> dict:
    """Diagnostic probe only. No position is opened and no exit is armed."""
    guard = route_guard()
    return {
        "entry_opened": False,
        "exit_armed": False,
        "position_written": False,
        "entry": "blocked_probe_is_not_ticket",
        "exit": guard["exit_quote"],
        "reason": guard["blockers"][0],
        "chat_conditional_live_approval": "not_a_control",
        "recorded_live_applied": False,
        "five_agents": False,
        "sign": False,
    }


def exact_in_min_out(amount_out: int, slippage_bps: int) -> int:
    """SDK floor: amount * (10_000 - bps) / 10_000."""
    try:
        return solana_whirlpool_quote.min_amount(amount_out, slippage_bps)
    except solana_whirlpool_quote.QuoteError as error:
        raise ValueError("quote is not usable") from error


def encode_swap_data(amount: int, threshold: int, sqrt_price_limit: int, specified_input: bool, a_to_b: bool) -> bytes:
    """42-byte swap args: discriminator, u64, u64, u128, two booleans."""
    if DISC != PUBLISHED_DISC:
        raise RuntimeError("swap discriminator drifted")
    if not 0 < amount < 2**64 or not 0 <= threshold < 2**64:
        raise ValueError("swap amount is not usable")
    if not 0 <= sqrt_price_limit < 2**128:
        raise ValueError("sqrt price limit is not usable")
    return (
        DISC
        + int(amount).to_bytes(8, "little")
        + int(threshold).to_bytes(8, "little")
        + int(sqrt_price_limit).to_bytes(16, "little")
        + bytes([1 if specified_input else 0, 1 if a_to_b else 0])
    )


def decode_swap_data(data: bytes) -> dict | None:
    if not isinstance(data, bytes) or len(data) != _DATA_LEN or data[:8] != DISC:
        return None
    specified = data[40]
    direction = data[41]
    if specified not in (0, 1) or direction not in (0, 1):
        return None
    return {
        "amount": int.from_bytes(data[8:16], "little"),
        "other_amount_threshold": int.from_bytes(data[16:24], "little"),
        "sqrt_price_limit": int.from_bytes(data[24:40], "little"),
        "amount_specified_is_input": specified == 1,
        "a_to_b": direction == 1,
    }


def build(pool: str, pool_data: bytes, accounts: dict[str, tuple[str, bytes]], slippage_bps: int, sqrt_price_limit: int, blockhash: str, context_slot: int, amount: int | None = None, *, allow_probe: bool = False) -> str:
    """Unsigned wrapped swap. The threshold is the quote of these account bytes."""
    direction = _direction(pool_data)
    state = solana_whirlpool.pool_state(pool_data)
    if state is None:
        raise ValueError("pool is not usable")
    spend = INPUT_LAMPORTS if amount is None else amount
    if spend not in _allowed_amounts(state, allow_probe=allow_probe):
        raise ValueError("spend is outside the probe and the $8 cap")
    _require_limit(direction, sqrt_price_limit, state["sqrt_price"])
    source = solana_tx.associated_token_address(phantom_transport.REQUIRED_WALLET, solana_tx.NATIVE_MINT, solana_tx.TOKEN_PROGRAM)
    dest = solana_tx.associated_token_address(phantom_transport.REQUIRED_WALLET, solana_tx.USDC_MINT, solana_tx.TOKEN_PROGRAM)
    arrays = _quote_arrays(pool, pool_data, accounts, direction)
    quote = solana_whirlpool_quote.quote_exact_in(
        state, arrays, spend, direction, slippage_bps, sqrt_price_limit, context_slot,
    )
    if quote["token_in"] != spend or quote["token_est_out"] > TICKET_USD_MICRO and spend != INPUT_LAMPORTS:
        raise ValueError("quoted output is outside the cap")
    return transaction_from_fields(
        pool, pool_data, source, dest, spend, quote["token_min_out"], sqrt_price_limit,
        True, direction, blockhash, wrap=True, transfer_lamports=spend,
    )


def build_exit(pool: str, pool_data: bytes, accounts: dict[str, tuple[str, bytes]], slippage_bps: int, blockhash: str, context_slot: int, usdc_raw: int, wallet_usdc_raw: int, *, minimum: int | None = None, estimated: int | None = None, ata_balance: int | None = None) -> tuple[str, dict]:
    """Unsigned USDC to SOL close of one acquired fill. The USDC account is not closed."""
    choice = exit_amount(usdc_raw, wallet_usdc_raw, confirmed=True, minimum=minimum, estimated=estimated, ata_balance=ata_balance)
    if choice["allowed"] is not True:
        raise ValueError(choice["reason"])
    state = solana_whirlpool.pool_state(pool_data)
    if state is None or state["adaptive_fee"]:
        raise ValueError("pool is not usable")
    limit = MAX_SQRT_PRICE - 1
    arrays = _quote_arrays(pool, pool_data, accounts, False)
    quote = solana_whirlpool_quote.quote_exact_in(state, arrays, usdc_raw, False, slippage_bps, limit, context_slot)
    if quote["token_in"] != usdc_raw or quote["token_est_out"] <= 0:
        raise ValueError("exit quote is not usable")
    wallet = phantom_transport.REQUIRED_WALLET
    wsol = solana_tx.associated_token_address(wallet, solana_tx.NATIVE_MINT, solana_tx.TOKEN_PROGRAM)
    usdc = solana_tx.associated_token_address(wallet, solana_tx.USDC_MINT, solana_tx.TOKEN_PROGRAM)
    fields = solana_whirlpool.pool_fields(pool_data)
    if fields is None:
        raise ValueError("pool is not usable")
    _mint_a, vault_a, _mint_b, vault_b = fields
    base = solana_whirlpool.start_tick_index(state["tick_current_index"], state["tick_spacing"])
    step = solana_whirlpool.TICK_ARRAY_SIZE * state["tick_spacing"]
    ticks = [solana_whirlpool.tick_array_address(pool, base + i * step) for i in range(3)]
    oracle = solana_whirlpool.oracle_address(pool)
    data = encode_swap_data(usdc_raw, quote["token_min_out"], limit, True, False)
    readonly = [
        solana_tx.TOKEN_PROGRAM, solana_tx.SYSTEM_PROGRAM, solana_tx.ASSOCIATED_TOKEN,
        solana_tx.NATIVE_MINT, solana_whirlpool.PROGRAM_ID, oracle,
    ]
    keys = [wallet, wsol, usdc, pool, vault_a, vault_b, *ticks, *readonly]

    def index(key: str) -> int:
        return keys.index(key)

    create = [index(wallet), index(wsol), index(wallet), index(solana_tx.NATIVE_MINT), index(solana_tx.SYSTEM_PROGRAM), index(solana_tx.TOKEN_PROGRAM)]
    swap_accounts = [
        index(solana_tx.TOKEN_PROGRAM), index(wallet), index(pool), index(wsol), index(vault_a),
        index(usdc), index(vault_b), *[index(tick) for tick in ticks], index(oracle),
    ]
    instructions = [
        (index(solana_tx.ASSOCIATED_TOKEN), create, bytes([1])),
        (index(solana_whirlpool.PROGRAM_ID), swap_accounts, data),
        (index(solana_tx.TOKEN_PROGRAM), [index(wsol), index(wallet), index(wallet)], bytes([9])),
    ]
    tx = solana_tx.encode_legacy(wallet, keys, len(readonly), instructions, blockhash, required=1)
    quote["closes_usdc"] = False
    quote["closes_wsol_only"] = True
    return tx, quote


def exit_closes_acquired(tx_b64: str, acquired: int, wallet_usdc: int) -> bool:
    """The swap input is the acquired fill, and the USDC account is not closed."""
    try:
        decoded = solana_tx.decode_transaction(base64.b64decode(tx_b64, validate=True))
    except ValueError:
        return False
    usdc = solana_tx.associated_token_address(phantom_transport.REQUIRED_WALLET, solana_tx.USDC_MINT, solana_tx.TOKEN_PROGRAM)
    amount = None
    for instruction in decoded.instructions:
        program = decoded.static_keys[instruction.program_index]
        keys = [decoded.static_keys[item] for item in instruction.account_indexes]
        if program == solana_tx.TOKEN_PROGRAM and instruction.data == bytes([9]) and usdc in keys:
            return False
        if program == solana_whirlpool.PROGRAM_ID:
            parsed = decode_swap_data(instruction.data)
            if parsed is None or parsed["a_to_b"] or not parsed["amount_specified_is_input"]:
                return False
            amount = parsed["amount"]
    return amount == acquired and amount != wallet_usdc and amount != KNOWN_PREEXISTING_USDC


def transaction_from_fields(pool: str, pool_data: bytes, source: str, dest: str, amount: int, threshold: int, sqrt_price_limit: int, specified_input: bool, a_to_b: bool, blockhash: str, authority: str = phantom_transport.REQUIRED_WALLET, token_authority: str | None = None, copies: int = 1, extra_account: bool = False, wrap: bool = False, transfer_lamports: int | None = None) -> str:
    """Byte constructor used to test a transaction that does not match the rule."""
    fields = solana_whirlpool.pool_fields(pool_data)
    if fields is None:
        raise ValueError("pool is not usable")
    _mint_a, vault_a, _mint_b, vault_b = fields
    owner_a, owner_b = (source, dest) if a_to_b else (dest, source)
    spacing = int.from_bytes(pool_data[solana_whirlpool._TICK_SPACING:solana_whirlpool._TICK_SPACING + 2], "little")
    current = int.from_bytes(pool_data[solana_whirlpool._TICK_CURRENT:solana_whirlpool._TICK_CURRENT + 4], "little", signed=True)
    base = solana_whirlpool.start_tick_index(current, spacing)
    step = solana_whirlpool.TICK_ARRAY_SIZE * spacing
    starts = [base - i * step for i in range(3)] if a_to_b else [base + i * step for i in range(3)]
    ticks = [solana_whirlpool.tick_array_address(pool, start) for start in starts]
    oracle = solana_whirlpool.oracle_address(pool)
    writable = [pool, owner_a, vault_a, owner_b, vault_b, *ticks]
    readonly = [solana_tx.TOKEN_PROGRAM, oracle, solana_whirlpool.PROGRAM_ID]
    signer = authority if token_authority is None else token_authority
    signers = [authority] if signer == authority else [authority, signer]
    keys = [*signers, *writable, *readonly]
    accounts = [
        keys.index(solana_tx.TOKEN_PROGRAM),
        keys.index(signer),
        keys.index(pool),
        keys.index(owner_a),
        keys.index(vault_a),
        keys.index(owner_b),
        keys.index(vault_b),
        *[keys.index(tick) for tick in ticks],
        keys.index(oracle),
    ]
    if extra_account:
        accounts.append(accounts[-1])
    program = keys.index(solana_whirlpool.PROGRAM_ID)
    data = encode_swap_data(amount, threshold, sqrt_price_limit, specified_input, a_to_b)
    if not 1 <= copies <= 2:
        raise ValueError("transaction is not usable")
    swap = [(program, accounts, data)] * copies
    if not wrap:
        return solana_tx.encode_legacy(authority, keys, len(readonly), swap, blockhash, required=len(signers))
    wsol = solana_tx.associated_token_address(authority, solana_tx.NATIVE_MINT, solana_tx.TOKEN_PROGRAM)
    usdc = solana_tx.associated_token_address(authority, solana_tx.USDC_MINT, solana_tx.TOKEN_PROGRAM)
    if source != wsol or dest != usdc:
        raise ValueError("wrapped accounts are not the wallet ATAs")
    extra_keys = [solana_tx.SYSTEM_PROGRAM, solana_tx.ASSOCIATED_TOKEN, solana_tx.NATIVE_MINT, solana_tx.USDC_MINT]
    for key in extra_keys:
        if key not in keys:
            keys.append(key)
    readonly_ids = {solana_tx.TOKEN_PROGRAM, solana_tx.SYSTEM_PROGRAM, solana_tx.ASSOCIATED_TOKEN, solana_whirlpool.PROGRAM_ID, solana_tx.NATIVE_MINT, solana_tx.USDC_MINT}
    if not extra_account:
        readonly_ids.add(oracle)
    readonly = [key for key in keys if key in readonly_ids]
    # Rebuild with the wallet and token accounts writable and programs readonly.
    signers = [authority] if signer == authority else [authority, signer]
    writable = [key for key in keys if key not in readonly and key not in signers]
    keys = [*signers, *writable, *readonly]
    def index(key: str) -> int:
        return keys.index(key)
    create = [index(authority), index(wsol), index(authority), index(solana_tx.NATIVE_MINT), index(solana_tx.SYSTEM_PROGRAM), index(solana_tx.TOKEN_PROGRAM)]
    usdc_create = [index(authority), index(usdc), index(authority), index(solana_tx.USDC_MINT), index(solana_tx.SYSTEM_PROGRAM), index(solana_tx.TOKEN_PROGRAM)]
    lamports = INPUT_LAMPORTS if transfer_lamports is None else transfer_lamports
    transfer = (2).to_bytes(4, "little") + int(lamports).to_bytes(8, "little")
    swap_accounts = [
        index(solana_tx.TOKEN_PROGRAM), index(signer), index(pool), index(owner_a), index(vault_a),
        index(owner_b), index(vault_b), *[index(tick) for tick in ticks], index(oracle),
    ]
    if extra_account:
        swap_accounts.append(index(oracle))
    instructions = [
        (index(solana_tx.ASSOCIATED_TOKEN), create, bytes([1])),
        (index(solana_tx.ASSOCIATED_TOKEN), usdc_create, bytes([1])),
        (index(solana_tx.SYSTEM_PROGRAM), [index(authority), index(wsol)], transfer),
        (index(solana_tx.TOKEN_PROGRAM), [index(wsol)], bytes([17])),
        *[(index(solana_whirlpool.PROGRAM_ID), swap_accounts, data)] * copies,
        (index(solana_tx.TOKEN_PROGRAM), [index(wsol), index(authority), index(authority)], bytes([9])),
    ]
    return solana_tx.encode_legacy(authority, keys, len(readonly), instructions, blockhash, required=len(signers))


def inspect(tx_b64: str, accounts: dict[str, tuple[str, bytes]], slippage_bps: int, context_slot: int, *, wsol_existed: bool = False, allow_probe: bool = False) -> dict:
    """Read the wrapped swap. The minimum output is recomputed from the accounts."""
    body = {
        "enabled": False,
        "mounted": False,
        "market": MARKET,
        "input_lamports": INPUT_LAMPORTS,
        "fee_policy": "not_approved",
        "aggregate_loss_policy": False,
        "live_ready": False,
        "output_computed_from_pool": False,
        "spend_verified": False,
        "slippage_verified": False,
        "wallet_verified": False,
        "wrap_verified": False,
        "min_out": None,
        "context_slot": None,
        "blockhash": None,
        "error_category": None,
        "byte_checks_passed": False,
    }
    if isinstance(context_slot, bool) or not isinstance(context_slot, int) or context_slot <= 0:
        body["error_category"] = "quote_context_missing"
        return body
    try:
        raw = base64.b64decode(tx_b64, validate=True)
        decoded = solana_tx.decode_transaction(raw)
    except ValueError:
        body["error_category"] = "transaction_unusable"
        return body
    body["blockhash"] = decoded.blockhash
    body["context_slot"] = context_slot
    if wsol_existed or not close_allowed(wsol_existed):
        body["error_category"] = "preexisting_wsol"
        return body
    if decoded.fee_payer != phantom_transport.REQUIRED_WALLET:
        body["error_category"] = "transaction_shape"
        return body
    plan = _wrap_plan(decoded)
    if plan.get("error"):
        body["error_category"] = plan["error"]
        return body
    instruction = plan["swap"]
    parsed = decode_swap_data(instruction.data)
    pool = decoded.static_keys[instruction.account_indexes[2]]
    record = accounts.get(pool)
    if parsed is None or record is None:
        body["error_category"] = "swap_data_unbound" if parsed is None else "account_unread"
        return body
    pool_data = record[1]
    state = solana_whirlpool.pool_state(pool_data)
    if state is None:
        body["error_category"] = "whirlpool_pool_unbound"
        return body
    tail = _tail_ok(decoded.static_keys, instruction.account_indexes, state["adaptive_fee"])
    if tail is not None:
        body["error_category"] = tail
        return body
    body["min_out"] = parsed["other_amount_threshold"]
    allowed = _allowed_amounts(state, allow_probe=allow_probe)
    if not parsed["amount_specified_is_input"] or parsed["amount"] not in allowed or plan["lamports"] != parsed["amount"]:
        body["error_category"] = "spend_unbound"
        return body
    try:
        direction = _direction(pool_data)
    except ValueError:
        body["error_category"] = "whirlpool_mint_unbound"
        return body
    if parsed["a_to_b"] != direction:
        body["error_category"] = "direction_unbound"
        return body
    if not _limit_ok(direction, parsed["sqrt_price_limit"], state["sqrt_price"]):
        body["error_category"] = "sqrt_price_limit"
        return body
    source, dest = _owner_accounts(decoded.static_keys, instruction.account_indexes, direction)
    if source != plan["wsol"] or dest != plan["usdc"]:
        body["error_category"] = "wrap_unbound"
        return body
    try:
        solana_whirlpool.check_swap_accounts(
            decoded.static_keys, instruction.account_indexes[:11], phantom_transport.REQUIRED_WALLET,
            source, dest, solana_tx.NATIVE_MINT, solana_tx.USDC_MINT, direction, accounts,
        )
        arrays = _quote_arrays(pool, pool_data, accounts, direction)
        quote = solana_whirlpool_quote.quote_exact_in(
            state, arrays, parsed["amount"], direction, slippage_bps, parsed["sqrt_price_limit"], context_slot,
        )
    except solana_whirlpool.WhirlpoolError as error:
        body["error_category"] = error.category
        return body
    except solana_whirlpool_quote.QuoteError as error:
        body["error_category"] = error.category
        return body
    if quote.get("zero_liquidity") or quote["token_est_out"] <= 0 or quote["token_min_out"] <= 0:
        body["error_category"] = "zero_output"
        return body
    if quote["token_in"] != parsed["amount"] or quote["token_min_out"] != parsed["other_amount_threshold"]:
        body["error_category"] = "slippage_unbound"
        body["min_out"] = parsed["other_amount_threshold"]
        return body
    body["spend_verified"] = True
    body["slippage_verified"] = True
    body["wallet_verified"] = True
    body["wrap_verified"] = True
    body["output_computed_from_pool"] = True
    body["byte_checks_passed"] = True
    body["sqrt_price_before"] = quote["sqrt_price_before"]
    body["sqrt_price_after"] = quote["sqrt_price_after"]
    return body


def _quote_arrays(pool: str, pool_data: bytes, accounts: dict[str, tuple[str, bytes]], a_to_b: bool) -> list[dict]:
    state = solana_whirlpool.pool_state(pool_data)
    if state is None:
        raise solana_whirlpool_quote.QuoteError("whirlpool_pool_unbound")
    base = solana_whirlpool.start_tick_index(state["tick_current_index"], state["tick_spacing"])
    step = solana_whirlpool.TICK_ARRAY_SIZE * state["tick_spacing"]
    starts = [base - i * step for i in range(3)] if a_to_b else [base + i * step for i in range(3)]
    arrays = []
    for start in starts:
        address = solana_whirlpool.tick_array_address(pool, start)
        record = accounts.get(address)
        parsed = None if record is None else solana_whirlpool.tick_array_state(record[1])
        if parsed is None or parsed["pool"] != pool or parsed["start_tick_index"] != start:
            raise solana_whirlpool_quote.QuoteError("whirlpool_tick_array_unbound")
        arrays.append(parsed)
    return arrays


def _tail_ok(keys: list[str], indexes: list[int], adaptive: bool) -> str | None:
    """Classic pools have the 11-account swap. Adaptive pools add only their oracle."""
    if not adaptive:
        return None if len(indexes) == 11 else "adaptive_tail"
    if len(indexes) != 12 or keys[indexes[11]] != keys[indexes[10]]:
        return "adaptive_tail"
    if keys[indexes[10]] != solana_whirlpool.oracle_address(keys[indexes[2]]):
        return "whirlpool_oracle_unbound"
    return None


def _wrap_plan(decoded: solana_tx.Decoded) -> dict:
    wallet = phantom_transport.REQUIRED_WALLET
    wsol = solana_tx.associated_token_address(wallet, solana_tx.NATIVE_MINT, solana_tx.TOKEN_PROGRAM)
    usdc = solana_tx.associated_token_address(wallet, solana_tx.USDC_MINT, solana_tx.TOKEN_PROGRAM)
    creates = []
    transfer = None
    sync = False
    close = False
    swap = None
    for instruction in decoded.instructions:
        program = decoded.static_keys[instruction.program_index]
        keys = [decoded.static_keys[item] for item in instruction.account_indexes]
        if program == solana_tx.ASSOCIATED_TOKEN and instruction.data == bytes([1]) and len(keys) == 6:
            payer, ata, owner, mint, system, token_program = keys
            if payer != wallet or owner != wallet or system != solana_tx.SYSTEM_PROGRAM or token_program != solana_tx.TOKEN_PROGRAM:
                return {"error": "wrap_unbound"}
            if ata != solana_tx.associated_token_address(owner, mint, token_program):
                return {"error": "wrap_unbound"}
            if mint not in {solana_tx.NATIVE_MINT, solana_tx.USDC_MINT}:
                return {"error": "token_mint_unbound"}
            creates.append(mint)
            continue
        if program == solana_tx.SYSTEM_PROGRAM and solana_tx.system_transfer(instruction.data) is not None and len(keys) == 2:
            if transfer is not None or keys[0] != wallet or keys[1] != wsol:
                return {"error": "spend_unbound"}
            transfer = solana_tx.system_transfer(instruction.data)
            continue
        if program == solana_tx.TOKEN_PROGRAM and instruction.data == bytes([17]) and keys == [wsol]:
            sync = True
            continue
        if program == solana_tx.TOKEN_PROGRAM and instruction.data == bytes([9]) and keys == [wsol, wallet, wallet]:
            close = True
            continue
        if program == solana_whirlpool.PROGRAM_ID and swap is None:
            swap = instruction
            continue
        return {"error": "transaction_shape"}
    if creates.count(solana_tx.NATIVE_MINT) != 1 or creates.count(solana_tx.USDC_MINT) != 1:
        return {"error": "wrap_unbound"}
    if transfer is None or not sync or swap is None or not close:
        return {"error": "wrap_unbound"}
    kinds = [_kind(decoded, item) for item in decoded.instructions]
    if kinds != ["create", "create", "transfer", "sync", "swap", "close"]:
        return {"error": "wrap_unbound"}
    return {"swap": swap, "lamports": transfer, "wsol": wsol, "usdc": usdc}


def _kind(decoded: solana_tx.Decoded, instruction: solana_tx.Instruction) -> str:
    program = decoded.static_keys[instruction.program_index]
    if program == solana_tx.ASSOCIATED_TOKEN:
        return "create"
    if program == solana_tx.SYSTEM_PROGRAM:
        return "transfer"
    if program == solana_whirlpool.PROGRAM_ID:
        return "swap"
    if instruction.data == bytes([17]):
        return "sync"
    return "close"


def _direction(pool_data: bytes) -> bool:
    fields = solana_whirlpool.pool_fields(pool_data or b"")
    if fields is None:
        raise ValueError("pool")
    mint_a, _vault_a, mint_b, _vault_b = fields
    if mint_a == solana_tx.NATIVE_MINT and mint_b == solana_tx.USDC_MINT:
        return True
    if mint_a == solana_tx.USDC_MINT and mint_b == solana_tx.NATIVE_MINT:
        return False
    raise ValueError("mints")


def _require_limit(a_to_b: bool, limit: int, current: int | None) -> None:
    if not _limit_ok(a_to_b, limit, current):
        raise ValueError("sqrt price limit is not usable")


def _limit_ok(a_to_b: bool, limit: int, current: int | None) -> bool:
    if current is None or not MIN_SQRT_PRICE < current < MAX_SQRT_PRICE:
        return False
    if a_to_b:
        return MIN_SQRT_PRICE < limit < current
    return current < limit < MAX_SQRT_PRICE


def _owner_accounts(keys: list[str], indexes: list[int], a_to_b: bool) -> tuple[str, str]:
    owner_a = keys[indexes[3]]
    owner_b = keys[indexes[5]]
    if a_to_b:
        return owner_a, owner_b
    return owner_b, owner_a
