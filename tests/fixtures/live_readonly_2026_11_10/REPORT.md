# LIVE READ-ONLY checks — run 2026-11-10 22:57–23:07 (UTC+7)
Scope: read-only HTTP/RPC GETs/POSTs only. No simulateTransaction, signing, sending, wallet, Render or repo actions. Keys are read from env and redacted (`REDACTED`) in every saved file.

## Per-source results
| Source | Endpoint | HTTP | Latency (median/max ms) | Rate limits | Verdict |
|---|---|---|---|---|---|
| Pyth Hermes | GET hermes.pyth.network/v2/updates/price/latest?ids[]=ef0d8b…b56d&parsed=true | 401 ×5 (`unauthorized`) | 163/165 | none seen | **FAIL**: since 2026-08-26 Hermes requires a Pyth API key (`Authorization: Bearer`) per docs.pyth.network; no key on box |
| Pyth on-chain (fallback, via Helius getAccountInfo) | PriceUpdateV2 PDA `7UVimffxr9ow1uXYxsr4LHAcV58mLzhmwaeKvJ1pjLiE` (push oracle `pythWSnsw…`, shard 0, owner `rec5EKM…`) | 200 ×5 | 122/195 | none | **FAIL for 30 s freshness**: publish_time never moved over 40 s; age 13.6→54.2 s; 4/5 reads >30 s |
| Helius getBlockHeight (confirmed) | mainnet.helius-rpc.com | 200 | 128 | none | PASS: `result`=445796895 |
| Helius getLatestBlockhash (confirmed) | same | 200 | 169 | none | PASS: `result.value.blockhash`, `result.value.lastValidBlockHeight`=445797045 (= height+150), `result.context.slot` |
| Helius isBlockhashValid | same | 200 ×2 | 117/120 | none | PASS: real hash → `result.value`=true; control `111…1` → false; `result.context.apiVersion`=4.4.1 |
| Helius getMinimumBalanceForRentExemption | same | 200 ×2 | 114/117 | none | PASS: **165 bytes → 1488440** (matches fixture 1488440, so the 2,039,280 figure is outdated); 0 bytes → 650240 |
| DexScreener | token-profiles/boosts/community-takeovers, /latest/dex/search, /tokens/v1/solana/…, /latest/dex/pairs/solana/… | 200 ×26 | 103/210 | none hit, no headers | PASS for availability; **untrusted for price/liquidity** (see surprises) |
| RugCheck | api.rugcheck.xyz/v1/tokens/<mint>/report | 200 ×25 | 2974/4833 | `X-Rate-Limit-Limit: 15`; no 429 at ~1 req/4 s | PASS (slow) |
| Helius DAS | getAssetsByCreator / getAssetsByAuthority (limit 20) | 200 ×10 | 170 / 159 | none | PARTIAL: creator listing non-empty for 1/5 devs; authority always 0 (pump.fun authority is `TSLvdd1…`) |
| Solana Tracker | /tokens/<mint>, /tokens/<mint>/holders, /holders/chart/<mint>, /deployer/<wallet> (header x-api-key) | 200 ×20 | 192/500, 328/404, 265/642 | none hit, no headers | PASS for availability; holder chart is daily and sparse |

### Pyth on-chain reads (SOL/USD, feed ef0d8b6f…b56d, verification Full)
| # | publish_time | age s | price | conf | conf/price | expo | posted_slot |
|---|---|---|---|---|---|---|---|
| 0 | 1794326368 | 13.59 | 82.223430 | 0.029003 | 0.000353 | -8 | 467779724 |
| 1 | 1794326368 | 23.72 | 82.223430 | 0.029003 | 0.000353 | -8 | 467779724 |
| 2 | 1794326368 | 33.92 | 82.223430 | 0.029003 | 0.000353 | -8 | 467779724 |
| 3 | 1794326368 | 44.05 | 82.223430 | 0.029003 | 0.000353 | -8 | 467779724 |
| 4 | 1794326368 | 54.17 | 82.223430 | 0.029003 | 0.000353 | -8 | 467779724 |

Hermes shape (from docs, not observed due to 401): `parsed[0].price.{price,conf,expo,publish_time}` (strings for price/conf), `parsed[0].ema_price`, `parsed[0].metadata`, `binary.data[]`. On-chain PriceUpdateV2 layout decoded: disc 8 | write_authority 32 | verification_level (1 byte Full / 2 bytes Partial) | feed_id 32 | price i64 | conf u64 | exponent i32 | publish_time i64 | prev_publish_time i64 | ema_price i64 | ema_conf u64 | posted_slot u64.

## Token picks (DexScreener, window pairCreatedAt 30 min–72 h, marketCap $300k–$20M)
| Sym | Mint | Quote | Pair age h | DexScreener marketCap | liquidity.usd/marketCap | Overall |
|---|---|---|---|---|---|---|
| BIG | `34DXEU2ttEtBSb1TqGJm7izgvYTTkqKcCoiNWdPDpump` | TTWO | 47.84 | 880,038 | 0.0972 | **FAIL** |
| X | `9VZerdNL1x6EjetDqRnh5bYcdmk1y8sLAGs1ybzGpump` | SOL | 7.25 | 307,666 | 0.155 | **FAIL** |
| TRUMP | `3yjyHCC4EzLb7J7cx3uczgTrqhpXDCBkfShK8Rf3pump` | SOL | 4.26 | 1,220,598 | 0.8623 | **FAIL** |
| MYWAY | `tx41eAXzN6jHH22LSgF7eSxpTNvJXwcbrcGsbDrpump` | SOL | 49.31 | 628,629 | 0.1008 | **FAIL** |
| Nothing | `6GMjV5PHfeNMaXykCktvbdmXAx54TdKfRxSRaD5Spump` | SOL | 22.04 | 1,598,814 | 0.8341 | **FAIL-CLOSED** |

**No token passes.** Rules: mint+freeze revoked; LP 100% burned/locked; liq ≥25% mcap; dev ≤3%; no past dev rug (fail closed); holder growth available.

### BIG — FAIL
mint `34DXEU2ttEtBSb1TqGJm7izgvYTTkqKcCoiNWdPDpump`, pair `5TecgcpFLdBnJvBrpjvk32UAYABPRA1HgeFPEUjQQkYK`

- **mint_freeze_revoked**: PASS
- **lp_100**: PASS
- **liq_ge_25pct**: FAIL
- **dev_le_3pct**: FAIL-CLOSED: creator identity conflict (RugCheck 3gGYvn.. vs SolanaTracker 5YLSzD..)
- **no_past_rug**: FAIL-CLOSED: dev identity conflict; past tokens not attributable
- **holder_growth**: PASS
- evidence: `{"authorities": {"rc_mint": null, "rc_freeze": null, "st_security": {"freezeAuthority": null, "mintAuthority": null}}, "lp": {"rc_lpLockedPct": 100, "rc_lpCurrentSupply": 0, "st_lpBurn": 100, "rc_markets": [["pump_fun_amm", 100], ["meteoraDlmm", 0], ["meteoraDlmm", 0]]}, "dev": {"rc_creator": "3gGYvne8RDn7wXca7tuejFoh46hP9wnofQy649DFRbYR", "st_creator": "5YLSzDFSFTY2rD485ihPNzdY4GahEjsBeiBBKW3fqSkS", "rc_creatorBalance_pct": 0.0, "st_dev_pct": 0}, "holders": {"chart": [{"holders": 0, "time": 1794092400}, {"holders": 52, "time": 1794178800}, {"holders": 210, "time": 1794265200}], "st_holders": 611, "rc_totalHolders": 942}, "rc_risks": [], "st_risks": ["Top 10 Holders"]}`

### X — FAIL
mint `9VZerdNL1x6EjetDqRnh5bYcdmk1y8sLAGs1ybzGpump`, pair `6MdmwvCsrMAbTqJC7GBBu5FBUFLbC6dJXGxm5VtnrDzQ`

- **mint_freeze_revoked**: PASS
- **lp_100**: PASS
- **liq_ge_25pct**: FAIL
- **dev_le_3pct**: FAIL-CLOSED: creator identity conflict (RugCheck AiKvek.. vs SolanaTracker 9MsrGF..)
- **no_past_rug**: FAIL-CLOSED: dev identity conflict; past tokens not attributable
- **holder_growth**: FAIL-CLOSED: holders chart has 0 non-zero daily point(s); growth not computable
- evidence: `{"authorities": {"rc_mint": null, "rc_freeze": null, "st_security": {"freezeAuthority": null, "mintAuthority": null}}, "lp": {"rc_lpLockedPct": 100, "rc_lpCurrentSupply": 0, "st_lpBurn": 100, "rc_markets": [["pump_fun_amm", 100], ["meteoraDlmm", 0], ["meteoraDlmm", 0], ["meteoraDlmm", 0], ["meteoraDlmm", 0], ["meteora_damm_v2", 0], ["meteora_damm_v2", 0]]}, "dev": {"rc_creator": "AiKvekQa4Zp6ARDErEtD26DhtCGKc4HagGjEpYarzr6c", "st_creator": "9MsrGFJqAD1JmQCfTR86XfLMsLFKFGTsUQFCLRoykZZf", "rc_creatorBalance_pct": 0.0, "st_dev_pct": 0}, "holders": {"chart": [{"holders": 0, "time": 1794265200}], "st_holders": 3436, "rc_totalHolders": 8463}, "rc_risks": [], "st_risks": ["Top 10 Holders"]}`

### TRUMP — FAIL
mint `3yjyHCC4EzLb7J7cx3uczgTrqhpXDCBkfShK8Rf3pump`, pair `DmoLzHQEtUPeoeo1XMY6CqJodT2TCkFQHPV4Y4AhvYRH`

- **mint_freeze_revoked**: PASS
- **lp_100**: PASS
- **liq_ge_25pct**: FAIL-CLOSED: source conflict - DexScreener liquidity.usd/marketCap ~$1.0-1.3M each, but RugCheck markets[].lp and SolanaTracker pools[] show liquidity ~$2.3-4.1k and mcap ~$2.2-2.4k for the same pair (~500x apart)
- **dev_le_3pct**: PASS
- **no_past_rug**: FAIL: RugCheck risks[] contains "Creator history of rugged tokens" for this creator, contradicting the 20 capped past-token reports (all rugged=false); 20 checked of >=46 (RugCheck creatorTokens) / 71 pools (SolanaTracker /deployer)
- **holder_growth**: FAIL-CLOSED: holders chart has 0 non-zero daily point(s); growth not computable
- evidence: `{"authorities": {"rc_mint": null, "rc_freeze": null, "st_security": {"freezeAuthority": null, "mintAuthority": null}}, "lp": {"rc_lpLockedPct": 100, "rc_lpCurrentSupply": 0, "st_lpBurn": 100, "rc_markets": [["pump_fun_amm", 100], ["meteora_damm_v2", 0]]}, "dev": {"rc_creator": "HCBQjnixpBcqBz8jr3kJqgWPJA517w5gz6XxA53H13DY", "st_creator": "HCBQjnixpBcqBz8jr3kJqgWPJA517w5gz6XxA53H13DY", "rc_creatorBalance_pct": 0.0, "st_dev_pct": 0}, "holders": {"chart": [{"holders": 0, "time": 1794265200}], "st_holders": 150, "rc_totalHolders": 657}, "rc_risks": ["Creator history of rugged tokens", "Low Liquidity"], "st_risks": ["Price Decrease", "Top 10 Holders", "Bundlers Detected"]}`

### MYWAY — FAIL
mint `tx41eAXzN6jHH22LSgF7eSxpTNvJXwcbrcGsbDrpump`, pair `CV3MT2sZ4kfzc6GpBhw9esoz2dCfKkz5ZGS8uZP85p32`

- **mint_freeze_revoked**: PASS
- **lp_100**: PASS
- **liq_ge_25pct**: FAIL
- **dev_le_3pct**: PASS
- **no_past_rug**: FAIL-CLOSED: DAS creator+authority listings empty (DAS also does not index the current token under this creator), RugCheck creatorTokens null; only SolanaTracker /deployer lists the current mint itself - first-time dev not independently provable
- **holder_growth**: PASS
- evidence: `{"authorities": {"rc_mint": null, "rc_freeze": null, "st_security": {"freezeAuthority": null, "mintAuthority": null}}, "lp": {"rc_lpLockedPct": 100, "rc_lpCurrentSupply": 0, "st_lpBurn": 100, "rc_markets": [["pump_fun_amm", 100], ["meteora_damm_v2", 0], ["orca", 0]]}, "dev": {"rc_creator": "GPd7fqFxZQmGwZsGwQbFsW1zUvEPD2byUvMB63Cyp43J", "st_creator": "GPd7fqFxZQmGwZsGwQbFsW1zUvEPD2byUvMB63Cyp43J", "rc_creatorBalance_pct": 0.0, "st_dev_pct": 0}, "holders": {"chart": [{"holders": 0, "time": 1794092400}, {"holders": 4165, "time": 1794178800}, {"holders": 8007, "time": 1794265200}], "st_holders": 6444, "rc_totalHolders": 14348}, "rc_risks": [], "st_risks": ["No social media"]}`

### Nothing — FAIL-CLOSED
mint `6GMjV5PHfeNMaXykCktvbdmXAx54TdKfRxSRaD5Spump`, pair `7Uj81bsdR8DyzcvAJajs537gqpBWVwkXcjxStxW22qpB`

- **mint_freeze_revoked**: PASS
- **lp_100**: PASS
- **liq_ge_25pct**: FAIL-CLOSED: source conflict - DexScreener liquidity.usd/marketCap ~$1.0-1.3M each, but RugCheck markets[].lp and SolanaTracker pools[] show liquidity ~$2.3-4.1k and mcap ~$2.2-2.4k for the same pair (~500x apart)
- **dev_le_3pct**: FAIL-CLOSED: creator identity conflict (RugCheck AeVtfB.. vs SolanaTracker CAsJeU..)
- **no_past_rug**: FAIL-CLOSED: dev identity conflict; past tokens not attributable
- **holder_growth**: FAIL-CLOSED: holders chart has 1 non-zero daily point(s); growth not computable
- evidence: `{"authorities": {"rc_mint": null, "rc_freeze": null, "st_security": {"freezeAuthority": null, "mintAuthority": null}}, "lp": {"rc_lpLockedPct": 100, "rc_lpCurrentSupply": 0, "st_lpBurn": 100, "rc_markets": [["pump_fun_amm", 100], ["meteora_damm_v2", 0], ["meteora_damm_v2", 0]]}, "dev": {"rc_creator": "AeVtfBvPjCRJJ2KNwh2E6ygPf4sz6Pi4NXsM1vxyrmaT", "st_creator": "CAsJeUjgQohPYC4t5UWCELhfSjvMVrKo4itZ7DA74rTQ", "rc_creatorBalance_pct": 0.0, "st_dev_pct": 0}, "holders": {"chart": [{"holders": 0, "time": 1794178800}, {"holders": 231, "time": 1794265200}], "st_holders": 176, "rc_totalHolders": 1212}, "rc_risks": ["Low Liquidity"], "st_risks": ["Top 10 Holders", "Bundlers Detected"]}`

## Field paths used
- Pair age: DexScreener `pairs[0].pairCreatedAt` (ms). Market cap: `pairs[0].marketCap` (not `fdv`). Liquidity: `pairs[0].liquidity.usd` (+ `.base`, `.quote`).
- Mint/freeze: RugCheck `token.mintAuthority`, `token.freezeAuthority` (also top-level `mintAuthority`/`freezeAuthority`); Solana Tracker `pools[i].security.{mintAuthority,freezeAuthority}`.
- LP: RugCheck `markets[i].lp.lpLockedPct` (match `markets[i].pubkey` to pair), `markets[i].lp.lpCurrentSupply`; Solana Tracker `pools[i].lpBurn` (match `pools[i].poolId`).
- Liquidity cross-check: RugCheck `markets[i].lp.quoteUSD`/`baseUSD`, `totalMarketLiquidity`; Solana Tracker `pools[i].liquidity.{usd,quote}`, `pools[i].marketCap.usd`, `pools[i].price`.
- Dev: RugCheck `creator`, `creatorBalance` ÷ `token.supply`, `creatorTokens[]` (often null); Solana Tracker `token.creation.creator`, `risk.dev.percentage`, `pools[i].deployer`.
- Past tokens: Helius DAS `result.total`, `result.items[].id`, `items[].creators[].address`; Solana Tracker `/deployer` → `total`, `data[].mint`; RugCheck past report `rugged`, `risks[].name`.
- Holders: Solana Tracker `holders` (int), `/holders/chart` → `holders[].{holders,time}` (daily buckets); RugCheck `totalHolders`.
- Rug flags: RugCheck `rugged`, `risks[]`; Solana Tracker `risk.rugged`, `risk.risks[]`.

## Surprises
1. **Hermes now needs an API key** (401 for every unauthenticated request since 2026-08-26). The desk's Pyth freshness gate can't run without a Pyth key or another feed. The on-chain push feed (shard 0) didn't update for over 40 s, so it fails the 30 s freshness rule as a substitute.
2. **Rent 1,488,440 is correct on live mainnet today** (and 0 bytes = 650,240). The reviewer's 2,039,280 is the old pre-change value.
3. **DexScreener price and liquidity can be off by about 500x.** For TRUMP and Nothing, DexScreener reported ~$1.0–1.3M liquidity and market cap. RugCheck and Solana Tracker showed ~$2.3–4k for the same pair, and the pool held only ~7 SOL / ~5 SOL. DexScreener `liquidity.usd` also counts both sides at its own price. The liquidity rule needs a quote-side, cross-source check.
4. **The creator address disagrees between RugCheck and Solana Tracker for 3/5 tokens** (BIG, X, Nothing), so dev holdings and dev history fail closed.
5. **RugCheck flags TRUMP's creator with "Creator history of rugged tokens",** yet all 20 capped past-token reports have `rugged=false`. A per-token `rugged` scan alone would wrongly pass this serial launcher (≥46 tokens, RugCheck; 71 pools, Solana Tracker).
6. **DAS doesn't index pump.fun tokens reliably by creator** (0 results for 4/5 devs, including the current token itself), and by-authority is always 0.
7. **The Solana Tracker holder chart is daily and lags:** for BIG the chart ends at 210 while `holders`=611 and RugCheck says 942. Holder counts disagree across sources by 2–4x.
8. BIG's pair is quoted in TTWO (a tokenized stock), not SOL.

## Files
- `raw/*.json`: every response, wrapped as `{meta:{url(redacted),status,latency_ms,rate_headers,fetched_local},body}`
- `calls.jsonl`: call log. `pyth_summary.json`, `helius_summary.json`, `dex_candidates.json`, `picked.json`, `past_TRUMP.json`, `verdicts.json`
- `fixtures.zip`: everything above
