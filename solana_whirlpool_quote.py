"""Exact-in Whirlpool quote from pool and tick state.

The arithmetic is the Rust SDK at commit 4c75c2f0:
rust-sdk/core/src/quote/swap.rs, math/token.rs, math/tick.rs, and math/tick_array.rs.
Slippage uses try_get_min_amount_with_slippage_tolerance, which floors
amount * (10_000 - bps) / 10_000. A caller-supplied output is not an input.
The SDK quote struct has no price-impact percentage; the result keeps the
sqrt price before and after the same loop. context_slot is not a blockhash.
"""

from __future__ import annotations

FEE_RATE_DENOMINATOR = 1_000_000
# programs/whirlpool/src/math/token_math.rs at 2509ad9. update_fee_rate rejects a larger static rate.
MAX_STATIC_FEE_RATE = 60_000
# fee_rate_manager.rs. Adaptive totals are capped here. This quote refuses adaptive pools.
FEE_RATE_HARD_LIMIT = 100_000
BPS_DENOMINATOR = 10_000
MIN_SQRT_PRICE = 4295048016
MAX_SQRT_PRICE = 79226673515401279992447579055
TICK_ARRAY_SIZE = 88
MIN_TICK_INDEX = -443636
MAX_TICK_INDEX = 443636
_U128 = 1 << 128
_LOG_B_2_X32 = 59543866431248
_BIT_PRECISION = 14
_LOG_LOWER = 184467440737095516
_LOG_UPPER = 15793534762490258745


class QuoteError(Exception):
    def __init__(self, category: str):
        self.category = category


def exact_in_input_ceiling(amount: int) -> int:
    """The fee of a successful exact-in cannot exceed the input.

    compute_swap subtracts amount_in + fee_amount from amount_remaining.
    A subtraction that does not fit is not a successful swap, so the fees
    add to at most the input. It does not use the current fee rate.

    Equality is not proven for a frozen route. A step that does not move
    the price can set amount_in to 0, the fee to the whole remainder, and
    the output to 0. programs/whirlpool/src/instructions/swap.rs at 2509ad9
    returns AmountOutBelowMinimum when other_amount_threshold is greater
    than that output. route_v2 does not encode other_amount_threshold or
    sqrt_price_limit, so that step is not a successful execution of a
    message with a positive minimum output, and it does not prove the fee
    can equal the input on such a message.
    """
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0 or amount >= 2**64:
        raise QuoteError("spend_unbound")
    return amount


def crossing_step_fee(amount_in: int, fee_rate: int) -> int:
    """ceil(amount_in * rate / (1_000_000 - rate)). The max-swap branch."""
    if amount_in < 0 or not 0 <= fee_rate < FEE_RATE_DENOMINATOR:
        raise QuoteError("fee_unbound")
    if amount_in == 0 or fee_rate == 0:
        return 0
    return (amount_in * fee_rate + (FEE_RATE_DENOMINATOR - fee_rate) - 1) // (FEE_RATE_DENOMINATOR - fee_rate)


def min_amount(amount: int, slippage_bps: int) -> int:
    """Floor of amount * (10_000 - bps) / 10_000."""
    if isinstance(amount, bool) or isinstance(slippage_bps, bool):
        raise QuoteError("slippage_unbound")
    if not isinstance(amount, int) or not isinstance(slippage_bps, int):
        raise QuoteError("slippage_unbound")
    if amount <= 0 or not 0 <= slippage_bps <= BPS_DENOMINATOR:
        raise QuoteError("slippage_unbound")
    product = BPS_DENOMINATOR - slippage_bps
    minimum = _mul_div(amount, product, BPS_DENOMINATOR, False)
    if minimum <= 0:
        raise QuoteError("slippage_unbound")
    return minimum


def quote_exact_in(pool: dict, tick_arrays: list[dict], amount: int, a_to_b: bool, slippage_bps: int, sqrt_price_limit: int, context_slot: int) -> dict:
    """Quote one exact-in swap. context_slot is required and is not a blockhash."""
    if isinstance(context_slot, bool) or not isinstance(context_slot, int) or context_slot <= 0:
        raise QuoteError("quote_context_missing")
    if isinstance(sqrt_price_limit, bool) or not isinstance(sqrt_price_limit, int) or sqrt_price_limit < 0:
        raise QuoteError("sqrt_price_limit")
    fee_rate = pool.get("fee_rate")
    if isinstance(fee_rate, bool) or not isinstance(fee_rate, int) or not 0 <= fee_rate <= FEE_RATE_DENOMINATOR:
        raise QuoteError("fee_unbound")
    if pool.get("liquidity") == 0:
        return {
            "token_in": amount,
            "token_est_out": 0,
            "token_min_out": 0,
            "trade_fee": amount,
            "sqrt_price_before": pool.get("sqrt_price"),
            "sqrt_price_after": pool.get("sqrt_price"),
            "context_slot": context_slot,
            "slippage_bps": slippage_bps,
            "output_computed_from_pool": True,
            "zero_liquidity": True,
        }
    result = _compute_swap(amount, sqrt_price_limit, pool, tick_arrays, a_to_b, True)
    token_est_out = result["token_b"] if a_to_b else result["token_a"]
    return {
        "token_in": amount if result["amount_remaining"] == 0 else amount - result["amount_remaining"],
        "token_est_out": token_est_out,
        "token_min_out": min_amount(token_est_out, slippage_bps),
        "trade_fee": result["trade_fee"],
        "sqrt_price_before": pool["sqrt_price"],
        "sqrt_price_after": result["sqrt_price"],
        "context_slot": context_slot,
        "slippage_bps": slippage_bps,
        "output_computed_from_pool": True,
    }


def sdk_fixture(amount: int, specified_token_a: bool, slippage_bps: int, sufficient_liquidity: bool, exact_in: bool) -> dict:
    """One published rust-sdk/core/src/quote/swap.rs test input."""
    sqrt_price = 1 << 64
    pool = {
        "sqrt_price": sqrt_price,
        "tick_current_index": sqrt_price_to_tick_index(sqrt_price),
        "fee_rate": 3000,
        "liquidity": 100_000_000 if sufficient_liquidity else 265_000,
        "tick_spacing": 2,
    }
    arrays = []
    for start in (0, 176, 352, -176, -352):
        liquidity_net = 1000 if start < 0 else -1000
        arrays.append({
            "start_tick_index": start,
            "ticks": [{"initialized": True, "liquidity_net": liquidity_net} for _ in range(TICK_ARRAY_SIZE)],
        })
    if exact_in:
        return quote_exact_in(pool, arrays, amount, specified_token_a, slippage_bps, 0, 1)
    return _quote_exact_out(pool, arrays, amount, specified_token_a, slippage_bps)


def sqrt_price_from_tick(tick: int) -> int:
    if tick >= 0:
        return _positive_tick(tick)
    return _negative_tick(tick)


def sqrt_price_to_tick_index(sqrt_price: int) -> int:
    msb = sqrt_price.bit_length() - 1
    log2p_integer_x32 = (msb - 64) << 32
    bit = 0x8000000000000000
    precision = 0
    fraction = 0
    if msb >= 64:
        ratio = sqrt_price >> (msb - 63)
    else:
        ratio = (sqrt_price << (63 - msb)) % _U128
    while bit > 0 and precision < _BIT_PRECISION:
        ratio = (ratio * ratio) % _U128
        flag = ratio >> 127
        ratio >>= 63 + flag
        fraction += bit * flag
        bit >>= 1
        precision += 1
    log2p_x32 = log2p_integer_x32 + (fraction >> 32)
    logbp = log2p_x32 * _LOG_B_2_X32
    tick_low = _i32((logbp - _LOG_LOWER) >> 64)
    tick_high = _i32((logbp + _LOG_UPPER) >> 64)
    if tick_low == tick_high:
        return tick_low
    if sqrt_price_from_tick(tick_high) <= sqrt_price:
        return tick_high
    return tick_low


def _quote_exact_out(pool: dict, arrays: list[dict], amount: int, specified_token_a: bool, slippage_bps: int) -> dict:
    result = _compute_swap(amount, 0, pool, arrays, not specified_token_a, False)
    token_est_in = result["token_b"] if specified_token_a else result["token_a"]
    return {
        "token_out": amount,
        "token_est_in": token_est_in,
        "token_max_in": _max_amount(token_est_in, slippage_bps),
        "trade_fee": result["trade_fee"],
    }


def _compute_swap(amount: int, sqrt_price_limit: int, pool: dict, arrays: list[dict], a_to_b: bool, specified_input: bool) -> dict:
    if amount == 0:
        raise QuoteError("spend_unbound")
    limit = MIN_SQRT_PRICE if a_to_b and sqrt_price_limit == 0 else MAX_SQRT_PRICE if sqrt_price_limit == 0 else sqrt_price_limit
    if not MIN_SQRT_PRICE <= limit <= MAX_SQRT_PRICE:
        raise QuoteError("sqrt_price_limit")
    if a_to_b and limit > pool["sqrt_price"] or not a_to_b and limit < pool["sqrt_price"]:
        raise QuoteError("sqrt_price_limit")
    sequence = _sequence(arrays, pool["tick_spacing"])
    remaining = amount
    calculated = 0
    price = pool["sqrt_price"]
    tick = pool["tick_current_index"]
    liquidity = pool["liquidity"]
    fee = 0
    steps = 0
    while remaining > 0 and limit != price:
        steps += 1
        if steps > 512:
            raise QuoteError("quote_unbounded")
        initialized, next_index = _prev_tick(sequence, tick) if a_to_b else _next_tick(sequence, tick)
        next_price = sqrt_price_from_tick(next_index)
        target = max(next_price, limit) if a_to_b else min(next_price, limit)
        step = _swap_step(remaining, pool["fee_rate"], liquidity, price, target, a_to_b, specified_input)
        fee += step["fee_amount"]
        if specified_input:
            remaining -= step["amount_in"] + step["fee_amount"]
            calculated += step["amount_out"]
        else:
            remaining -= step["amount_out"]
            calculated += step["amount_in"] + step["fee_amount"]
        if step["next_sqrt_price"] == next_price:
            liquidity = _next_liquidity(liquidity, initialized, a_to_b)
            tick = next_index - 1 if a_to_b else next_index
        elif step["next_sqrt_price"] != price:
            tick = sqrt_price_to_tick_index(step["next_sqrt_price"])
        price = step["next_sqrt_price"]
    swapped = amount - remaining
    if a_to_b == specified_input:
        token_a, token_b = swapped, calculated
    else:
        token_a, token_b = calculated, swapped
    return {
        "token_a": token_a,
        "token_b": token_b,
        "trade_fee": fee,
        "sqrt_price": price,
        "amount_remaining": remaining,
        "steps": steps,
    }


def _swap_step(remaining, fee_rate, liquidity, price, target, a_to_b, specified_input) -> dict:
    try:
        fixed = _fixed_delta(price, target, liquidity, a_to_b, specified_input)
        overflow = False
    except QuoteError as error:
        if error.category != "amount_exceeds_u64":
            raise
        fixed = None
        overflow = True
    calculated = _apply_fee(remaining, fee_rate) if specified_input else remaining
    if not overflow and fixed <= calculated:
        next_price = target
    else:
        next_price = _next_sqrt(price, liquidity, calculated, a_to_b, specified_input)
    maximum = next_price == target
    unfixed = _unfixed_delta(price, next_price, liquidity, a_to_b, specified_input)
    if not maximum or overflow:
        fixed = _fixed_delta(price, next_price, liquidity, a_to_b, specified_input)
    amount_in, amount_out = (fixed, unfixed) if specified_input else (unfixed, fixed)
    if not specified_input and amount_out > remaining:
        amount_out = remaining
    if specified_input and not maximum:
        fee_amount = remaining - amount_in
    else:
        fee_amount = _reverse_fee(amount_in, fee_rate) - amount_in
    return {"amount_in": amount_in, "amount_out": amount_out, "next_sqrt_price": next_price, "fee_amount": fee_amount}


def _fixed_delta(price, target, liquidity, a_to_b, specified_input) -> int:
    if a_to_b == specified_input:
        return _delta_a(price, target, liquidity, specified_input)
    return _delta_b(price, target, liquidity, specified_input)


def _unfixed_delta(price, target, liquidity, a_to_b, specified_input) -> int:
    if specified_input == a_to_b:
        return _delta_b(price, target, liquidity, not specified_input)
    return _delta_a(price, target, liquidity, not specified_input)


def _delta_a(first, second, liquidity, round_up) -> int:
    lower, upper = (first, second) if first < second else (second, first)
    numerator = liquidity * (upper - lower) << 64
    denominator = lower * upper
    if denominator == 0:
        raise QuoteError("quote_unusable")
    quotient, remainder = divmod(numerator, denominator)
    result = quotient + 1 if round_up and remainder else quotient
    if result >= 2**64:
        raise QuoteError("amount_exceeds_u64")
    return result


def _delta_b(first, second, liquidity, round_up) -> int:
    lower, upper = (first, second) if first < second else (second, first)
    product = liquidity * (upper - lower)
    quotient = product >> 64
    result = quotient + 1 if round_up and (product & ((1 << 64) - 1)) else quotient
    if result >= 2**64:
        raise QuoteError("amount_exceeds_u64")
    return result


def _next_sqrt(price, liquidity, amount, a_to_b, specified_input) -> int:
    if specified_input == a_to_b:
        return _next_from_a(price, liquidity, amount, specified_input)
    return _next_from_b(price, liquidity, amount, specified_input)


def _next_from_a(price, liquidity, amount, specified_input) -> int:
    if amount == 0:
        return price
    product = price * amount
    numerator = liquidity * price << 64
    shifted = liquidity << 64
    denominator = shifted + product if specified_input else shifted - product
    if denominator <= 0:
        raise QuoteError("sqrt_price_limit")
    quotient, remainder = divmod(numerator, denominator)
    result = quotient + 1 if remainder else quotient
    if not MIN_SQRT_PRICE <= result <= MAX_SQRT_PRICE:
        raise QuoteError("sqrt_price_limit")
    return result


def _next_from_b(price, liquidity, amount, specified_input) -> int:
    if amount == 0:
        return price
    shifted = amount << 64
    quotient, remainder = divmod(shifted, liquidity)
    delta = quotient + 1 if not specified_input and remainder else quotient
    result = price + delta if specified_input else price - delta
    if not MIN_SQRT_PRICE <= result <= MAX_SQRT_PRICE:
        raise QuoteError("sqrt_price_limit")
    return result


def _apply_fee(amount: int, fee_rate: int) -> int:
    return _mul_div(amount, FEE_RATE_DENOMINATOR - fee_rate, FEE_RATE_DENOMINATOR, False)


def _reverse_fee(amount: int, fee_rate: int) -> int:
    return _mul_div(amount, FEE_RATE_DENOMINATOR, FEE_RATE_DENOMINATOR - fee_rate, True)


def _max_amount(amount: int, slippage_bps: int) -> int:
    return _mul_div(amount, BPS_DENOMINATOR + slippage_bps, BPS_DENOMINATOR, True)


def _mul_div(amount: int, product: int, denominator: int, round_up: bool) -> int:
    if amount == 0 or product == 0:
        return 0
    numerator = amount * product
    quotient, remainder = divmod(numerator, denominator)
    result = quotient + 1 if round_up and remainder else quotient
    if result >= 2**64:
        raise QuoteError("amount_exceeds_u64")
    return result


def _next_liquidity(liquidity: int, tick: dict | None, a_to_b: bool) -> int:
    net = 0 if tick is None else tick["liquidity_net"]
    size = abs(net)
    if a_to_b:
        updated = liquidity + size if net < 0 else liquidity - size
    else:
        updated = liquidity - size if net < 0 else liquidity + size
    if updated < 0:
        raise QuoteError("liquidity_unbound")
    return updated


def _sequence(arrays: list[dict], spacing: int) -> list[dict]:
    ordered = sorted(arrays, key=lambda item: item["start_tick_index"])
    gap = TICK_ARRAY_SIZE * spacing
    for left, right in zip(ordered, ordered[1:]):
        if right["start_tick_index"] - left["start_tick_index"] != gap:
            raise QuoteError("tick_array_unbound")
    return ordered


def _tick(sequence: list[dict], index: int, spacing: int) -> dict:
    first = max(sequence[0]["start_tick_index"], MIN_TICK_INDEX)
    end = min(sequence[-1]["start_tick_index"] + TICK_ARRAY_SIZE * spacing - 1, MAX_TICK_INDEX)
    if index < first or index > end or index % spacing:
        raise QuoteError("tick_array_unbound")
    slot = (index - sequence[0]["start_tick_index"]) // (TICK_ARRAY_SIZE * spacing)
    array = sequence[slot]
    offset = (index - array["start_tick_index"]) // spacing
    return array["ticks"][offset]


def _prev_tick(sequence: list[dict], index: int) -> tuple[dict | None, int]:
    spacing = _spacing(sequence)
    start = max(sequence[0]["start_tick_index"], MIN_TICK_INDEX)
    previous = _initializable(index, spacing, False)
    while True:
        if previous < start:
            return None, start
        tick = _tick(sequence, previous, spacing)
        if tick["initialized"]:
            return tick, previous
        previous = _previous_initializable(previous, spacing)


def _next_tick(sequence: list[dict], index: int) -> tuple[dict | None, int]:
    spacing = _spacing(sequence)
    end = min(sequence[-1]["start_tick_index"] + TICK_ARRAY_SIZE * spacing - 1, MAX_TICK_INDEX)
    nxt = index
    while True:
        nxt = _following_initializable(nxt, spacing)
        if nxt > end:
            return None, end
        tick = _tick(sequence, nxt, spacing)
        if tick["initialized"]:
            return tick, nxt


def _spacing(sequence: list[dict]) -> int:
    return (sequence[1]["start_tick_index"] - sequence[0]["start_tick_index"]) // TICK_ARRAY_SIZE


def _initializable(index: int, spacing: int, round_up: bool) -> int:
    remainder = index % spacing
    result = (index // spacing) * spacing
    if round_up and remainder > 0:
        return result + spacing
    return result


def _previous_initializable(index: int, spacing: int) -> int:
    remainder = index % spacing
    if remainder == 0:
        return index - spacing
    return index - remainder


def _following_initializable(index: int, spacing: int) -> int:
    remainder = index % spacing
    return index - remainder + spacing


def _positive_tick(tick: int) -> int:
    ratio = 79232123823359799118286999567 if tick & 1 else 79228162514264337593543950336
    multiples = (
        (2, 79236085330515764027303304731),
        (4, 79244008939048815603706035061),
        (8, 79259858533276714757314932305),
        (16, 79291567232598584799939703904),
        (32, 79355022692464371645785046466),
        (64, 79482085999252804386437311141),
        (128, 79736823300114093921829183326),
        (256, 80248749790819932309965073892),
        (512, 81282483887344747381513967011),
        (1024, 83390072131320151908154831281),
        (2048, 87770609709833776024991924138),
        (4096, 97234110755111693312479820773),
        (8192, 119332217159966728226237229890),
        (16384, 179736315981702064433883588727),
        (32768, 407748233172238350107850275304),
        (65536, 2098478828474011932436660412517),
        (131072, 55581415166113811149459800483533),
        (262144, 38992368544603139932233054999993551),
    )
    for bit, factor in multiples:
        if tick & bit:
            ratio = ((ratio * factor) >> 96) % _U128
    return ratio >> 32


def _negative_tick(tick: int) -> int:
    absolute = abs(tick)
    ratio = 18445821805675392311 if absolute & 1 else 18446744073709551616
    multiples = (
        (2, 18444899583751176498),
        (4, 18443055278223354162),
        (8, 18439367220385604838),
        (16, 18431993317065449817),
        (32, 18417254355718160513),
        (64, 18387811781193591352),
        (128, 18329067761203520168),
        (256, 18212142134806087854),
        (512, 17980523815641551639),
        (1024, 17526086738831147013),
        (2048, 16651378430235024244),
        (4096, 15030750278693429944),
        (8192, 12247334978882834399),
        (16384, 8131365268884726200),
        (32768, 3584323654723342297),
        (65536, 696457651847595233),
        (131072, 26294789957452057),
        (262144, 37481735321082),
    )
    for bit, factor in multiples:
        if absolute & bit:
            ratio = (ratio * factor) >> 64
    return ratio


def _i32(value: int) -> int:
    value &= 0xFFFFFFFF
    if value >= 0x80000000:
        value -= 0x100000000
    return value
