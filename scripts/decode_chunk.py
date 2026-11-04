#!/usr/bin/env python3
"""Decode a raw chunk DB (fetch_chunk.py output) into a ledger DB.

Mechanical step only: receipt logs -> swap legs across all verified
formats, with native token amounts, plus per-tx gas (used x price,
both native). Token/pool metadata comes from public RPC eth_call,
prefetched concurrently and cached in the ledger; singleton formats
(Uniswap V4, PancakeSwap Infinity) and LunarBase resolve their token
pair from the same receipt's Transfer flows instead (Initialize-event
lookup does not work within public-endpoint log-range limits).

No pricing happens here by design: the ledger stores native amounts as
the source of truth, and USD conversion is a separate downstream step
on the consumer side, where the price source is fixed and controlled.

Format notes (all specs verified against same-receipt Transfers):
- v2 / aerodrome: unsigned In/Out words; amounts are swapper-side
  gross (Aerodrome pools additionally siphon a fee to a varying fee
  recipient; the leg amounts are the event's own, not the pool net).
- v3 / pancake-v3 / algebra: signed amount0/amount1, pool perspective
  (positive = pool received). Algebra pools occasionally flush a
  community fee in the same tx; event amounts are unaffected.
- metric: data word1 packs two int128 deltas (upper amount0, lower
  amount1), pool perspective; pair via getImmutables() words 1,2.
- forte: explicit tokenIn (word0), amountIn (word1), amountOut
  (word2); pair via xToken()/yToken().
- maverick: tokenAIn is data word3 (bool), amountIn word6, amountOut
  word7; pair via tokenA()/tokenB(). (Indices commissioner-corrected.)
- v4 / pancake-infinity: singleton; signed int128 amount0/amount1 in
  SWAPPER perspective (negative = paid in). Currencies resolved by
  matching the custodian's (PoolManager / Vault) same-receipt token
  flows; an unmatched side is native ETH.
- lunarbase: xToY word1, dx word2, dy word3 on the PropAMM proxy;
  the ERC-20 side is resolved from same-receipt flows, the other
  side is native ETH.

Usage: decode_chunk.py <chunk.db> <ledger.db>
Writes <ledger.db>.manifest.json next to the ledger.
"""
import json
import os
import sqlite3
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

RPC_URLS = ["https://base-rpc.publicnode.com", "https://mainnet.base.org"]
UA = {"User-Agent": "base-block-chunks/0.4", "Content-Type": "application/json"}

ETH0 = "0x0000000000000000000000000000000000000000"
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
PM_V4 = "0x498581ff718922c3f8e6a244956af099b2652b2b"
VAULT_INFINITY = "0x238a358808379702088667322f80ac48bad5e6c4"

# topic -> (kind, family)
FORMATS = {
    "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822": ("v2", "v2style"),
    "0xb3e2773606abfd36b5bd91394b3a54d1398336c65005baf7bf7a05efeffaf75b": ("aerodrome", "v2style"),
    "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67": ("v3", "v3style"),
    "0x19b47279256b2a23a1665c810c8d55a1758940ee09377d4f8d26497a3577dc83": ("pancake-v3", "v3style"),
    "0x121cb44ee54098b1a04743c487e7460d8dd429b27f88b1f4d4767396e1a59f79": ("algebra", "v3style"),
    "0x9734819749a91fc3be03ea83205f924ee08479bd3f0da48efc91d94d050cac1e": ("metric", "metric"),
    "0xfcfa612587d1091cc6d8ce39878a7e6e8742c856865fc9d64db3e1a2f1ab52ef": ("forte", "forte"),
    "0x103ed084e94a44c8f5f6ba8e3011507c41063177e29949083c439777d8d63f60": ("maverick", "maverick"),
    "0x1b43ddf90e971181a7faf41549e512675072e84befadbba7873086509dec1fdc": ("lunarbase", "lunarbase"),
    "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f": ("v4", "singleton"),
    "0x04206ad2b7c0f463bff3dd4f33c5735b0f2957a351e4f79763a4fa9e775dd237": ("pancake-infinity", "singleton"),
    "0x858ca532ad00ebb4d1c8c4fa77d87a5a8f4955e607f96bce1b600eed67d4c2b0": ("pmm", "pmm"),
}


def eth_call(to, data):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                       "params": [{"to": to, "data": data}, "latest"]}).encode()
    last = None
    start = hash(to) % len(RPC_URLS)
    for k in range(6):
        try:
            req = urllib.request.Request(RPC_URLS[(start + k) % len(RPC_URLS)],
                                         data=body, headers=UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                out = json.load(r)
            if out.get("result") not in (None, "0x"):
                return out["result"]
            last = out
        except Exception as e:  # noqa: BLE001 - rotate endpoint and retry
            last = e
        time.sleep(0.5)
    raise RuntimeError(f"eth_call failed for {to}: {last}")


def call_address(to, selector):
    return "0x" + eth_call(to, selector)[-40:]


def signed(v, bits):
    return v - (1 << bits) if v >= (1 << (bits - 1)) else v


def hexint(v):
    return int(v, 16) if isinstance(v, str) else v


def words_of(log):
    data = log["data"][2:]
    return [int(data[i:i + 64], 16) for i in range(0, len(data) - 63, 64)]


def receipt_flows(rec):
    """net token flow per (holder, token) from Transfer logs."""
    net = defaultdict(int)
    for log in rec.get("logs", []):
        ts = log.get("topics") or []
        if ts and ts[0].lower() == TRANSFER and len(ts) >= 3 \
                and len(log.get("data") or "") > 2:
            amt = int(log["data"], 16)
            tok = log["address"].lower()
            net[("0x" + ts[1][-40:], tok)] -= amt
            net[("0x" + ts[2][-40:], tok)] += amt
    return net


def main():
    chunk_path, ledger_path = sys.argv[1], sys.argv[2]
    src = sqlite3.connect(f"file:{chunk_path}?mode=ro", uri=True)
    blocks = {n: json.loads(j) for n, j in
              src.execute("SELECT block_number, raw_json FROM blocks")}
    receipts = {h: json.loads(j) for h, j in
                src.execute("SELECT tx_hash, receipt_json FROM receipts")}
    first, last = min(blocks), max(blocks)

    out = sqlite3.connect(ledger_path)
    out.executescript("""
    CREATE TABLE IF NOT EXISTS ledger_tx(
      hash TEXT PRIMARY KEY, block_number INTEGER, tx_index INTEGER,
      from_addr TEXT, to_addr TEXT, status INTEGER, gas_used INTEGER,
      gas_price INTEGER, n_legs INTEGER);
    CREATE TABLE IF NOT EXISTS legs(
      tx_hash TEXT, leg_index INTEGER, pool TEXT, kind TEXT,
      token_in TEXT, amount_in REAL, token_out TEXT, amount_out REAL,
      PRIMARY KEY(tx_hash, leg_index));
    CREATE TABLE IF NOT EXISTS token_meta(address TEXT PRIMARY KEY, decimals INTEGER);
    CREATE TABLE IF NOT EXISTS pool_meta(address TEXT PRIMARY KEY, token0 TEXT, token1 TEXT);
    CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
    """)

    # --- pass 1: collect swap events per tx (no RPC yet) ---
    raw_events = {}          # tx hash -> list of event dicts
    pair_pools = defaultdict(set)   # resolver -> pools
    swap_events = 0
    for b in sorted(blocks):
        for tx in blocks[b]["transactions"]:
            h = tx["hash"].lower()
            rec = receipts.get(h)
            evs = []
            if rec:
                for log in rec.get("logs", []):
                    topics = log.get("topics") or []
                    if not topics:
                        continue
                    t0 = topics[0].lower()
                    if t0 not in FORMATS:
                        continue
                    kind, family = FORMATS[t0]
                    swap_events += 1
                    pool = log["address"].lower()
                    ev = {"kind": kind, "family": family, "pool": pool,
                          "topics": topics, "words": words_of(log),
                          "log": log}
                    if family in ("v2style", "v3style"):
                        pair_pools["pair01"].add(pool)
                    elif family == "metric":
                        pair_pools["immutables"].add(pool)
                    elif family == "maverick":
                        pair_pools["tokenab"].add(pool)
                    elif family == "forte":
                        pair_pools["xy"].add(pool)
                    evs.append(ev)
            raw_events[h] = evs

    # --- prefetch pool token pairs per resolver + decimals ---
    cached_pools = {a: (t0, t1) for a, t0, t1 in
                    out.execute("SELECT address, token0, token1 FROM pool_meta")}

    def fetch_pair01(pool):
        return pool, (call_address(pool, "0x0dfe1681").lower(),
                      call_address(pool, "0xd21220a7").lower())

    def fetch_immutables(pool):
        try:
            r = eth_call(pool, "0xbcdb4dad")
            ws = [r[2 + i * 64:2 + (i + 1) * 64]
                  for i in range((len(r) - 2) // 64)]
            return pool, ("0x" + ws[1][-40:], "0x" + ws[2][-40:])
        except Exception:  # noqa: BLE001 - some emitters lack getImmutables
            return fetch_pair01(pool)

    def fetch_tokenab(pool):
        return pool, (call_address(pool, "0x0fc63d10").lower(),
                      call_address(pool, "0x5f64b55b").lower())

    def fetch_xy(pool):
        return pool, (call_address(pool, "0x088b699e").lower(),
                      call_address(pool, "0x49bfcca1").lower())

    fetchers = {"pair01": fetch_pair01, "immutables": fetch_immutables,
                "tokenab": fetch_tokenab, "xy": fetch_xy}
    failed_pools = set()
    for resolver, pools in pair_pools.items():
        todo = [p for p in pools if p not in cached_pools]
        if not todo:
            continue
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(fetchers[resolver], p): p for p in todo}
            for fut, pool in futs.items():
                try:
                    _, pair = fut.result()
                except Exception:  # noqa: BLE001 - one bad pool must not
                    failed_pools.add(pool)  # kill the whole chunk
                    continue
                cached_pools[pool] = pair
                out.execute("INSERT OR REPLACE INTO pool_meta VALUES (?,?,?)",
                            (pool, pair[0], pair[1]))
    out.commit()

    tokens = {ETH0}
    for t0, t1 in cached_pools.values():
        tokens.update((t0, t1))
    # receipt-resolved tokens (singletons, lunarbase) join after pass 2a;
    # collect them now from flows of txs that carry such events
    for h, evs in raw_events.items():
        if any(e["family"] in ("singleton", "lunarbase") for e in evs):
            for (holder, tok) in receipt_flows(receipts[h]):
                tokens.add(tok)
    cached_dec = {a: d for a, d in
                  out.execute("SELECT address, decimals FROM token_meta")}
    cached_dec[ETH0] = 18

    def fetch_dec(addr):
        try:
            return addr, int(eth_call(addr, "0x313ce567"), 16)
        except Exception:  # noqa: BLE001
            return addr, None

    with ThreadPoolExecutor(max_workers=8) as ex:
        for addr, dec in ex.map(fetch_dec,
                                [t for t in tokens if t not in cached_dec]):
            cached_dec[addr] = dec
            out.execute("INSERT OR REPLACE INTO token_meta VALUES (?,?)",
                        (addr, dec))
    out.commit()

    def human(raw, token):
        dec = cached_dec.get(token)
        return None if dec is None else raw / (10 ** dec)

    def resolve_singleton(ev, rec):
        """Return (pool_key, currency0, currency1) or None."""
        custodian = PM_V4 if ev["kind"] == "v4" else VAULT_INFINITY
        a0 = signed(ev["words"][0] & ((1 << 128) - 1), 128)
        a1 = signed(ev["words"][1] & ((1 << 128) - 1), 128)
        net = receipt_flows(rec)
        cand = {}
        for (holder, tok), v in net.items():
            if holder != custodian or v == 0:
                continue
            if v == -a0:
                cand.setdefault(0, set()).add(tok)
            if v == -a1:
                cand.setdefault(1, set()).add(tok)
        cur = {}
        for slot in (0, 1):
            cs = cand.get(slot, set())
            if len(cs) == 1:
                cur[slot] = next(iter(cs))
        if len(cur) == 2:
            if cur[0] == cur[1]:
                return None
            return ev["topics"][1].lower(), cur[0], cur[1]
        if len(cur) == 1:
            # the unmatched side is native ETH (no Transfer flow)
            slot = next(iter(cur))
            pair = [ETH0, ETH0]
            pair[slot] = cur[slot]
            return ev["topics"][1].lower(), pair[0], pair[1]
        return None

    # --- pass 2: build legs ---
    n_tx = n_legs = skipped = 0
    legs_by_kind = Counter()
    for b in sorted(blocks):
        for tx in blocks[b]["transactions"]:
            h = tx["hash"].lower()
            rec = receipts.get(h) or {}
            legs = []
            for ev in raw_events.get(h, []):
                kind, family, pool, w = ev["kind"], ev["family"], ev["pool"], ev["words"]
                if family not in ("singleton", "lunarbase", "pmm") \
                        and pool not in cached_pools:
                    skipped += 1
                    continue
                leg = None
                if family == "v2style":
                    t0, t1 = cached_pools[pool]
                    a0i, a1i, a0o, a1o = w[0], w[1], w[2], w[3]
                    if a0i > 0 and a1o > 0:
                        leg = (pool, kind, t0, a0i, t1, a1o)
                    elif a1i > 0 and a0o > 0:
                        leg = (pool, kind, t1, a1i, t0, a0o)
                elif family in ("v3style", "metric"):
                    if family == "metric" and len(ev["topics"]) != 3:
                        # Same signature string, non-indexed variant
                        # (e.g. logger 0x71c2ed90...): not a Metric
                        # pool swap. Genuine Metric Swap has exactly
                        # 3 topics (2 indexed addresses).
                        continue
                    t0, t1 = cached_pools[pool]
                    if family == "v3style":
                        a0, a1 = signed(w[0], 256), signed(w[1], 256)
                    else:
                        packed = w[1]
                        a0 = signed(packed >> 128, 128)
                        a1 = signed(packed & ((1 << 128) - 1), 128)
                    if a0 > 0 and a1 < 0:
                        leg = (pool, kind, t0, a0, t1, -a1)
                    elif a1 > 0 and a0 < 0:
                        leg = (pool, kind, t1, a1, t0, -a0)
                elif family == "pmm":
                    # topics[1]=tokenIn, topics[2]=tokenOut,
                    # w0=amountIn, w1=amountOut (vault-settled;
                    # verified 66/69 events exact vs the PMM vault,
                    # remaining 3 in one multi-leg tx).
                    ts = ev["log"]["topics"]
                    if len(ts) >= 3 and len(w) >= 2 and w[0] > 0 and w[1] > 0:
                        leg = (pool, kind, "0x" + ts[1][-40:], w[0],
                               "0x" + ts[2][-40:], w[1])
                elif family == "forte":
                    xt, yt = cached_pools[pool]
                    tin = "0x" + ev["log"]["data"][2:66][-40:]
                    tout = yt if tin == xt else xt
                    leg = (pool, kind, tin, w[1], tout, w[2])
                elif family == "maverick":
                    ta, tb = cached_pools[pool]
                    if w[3] != 0:
                        leg = (pool, kind, ta, w[6], tb, w[7])
                    else:
                        leg = (pool, kind, tb, w[6], ta, w[7])
                elif family == "singleton":
                    r = resolve_singleton(ev, rec)
                    if r is None:
                        skipped += 1
                        continue
                    pkey, c0, c1 = r
                    cached_pools.setdefault(pkey, (c0, c1))
                    out.execute("INSERT OR REPLACE INTO pool_meta VALUES (?,?,?)",
                                (pkey, c0, c1))
                    a0 = signed(w[0] & ((1 << 128) - 1), 128)
                    a1 = signed(w[1] & ((1 << 128) - 1), 128)
                    # swapper perspective: negative = paid in
                    if a0 < 0 and a1 > 0:
                        leg = (pkey, kind, c0, -a0, c1, a1)
                    elif a1 < 0 and a0 > 0:
                        leg = (pkey, kind, c1, -a1, c0, a0)
                elif family == "lunarbase":
                    xtoy = w[1] != 0
                    dx, dy = w[2], w[3]
                    amt_in_exp = dx if xtoy else dy
                    amt_out_exp = dy if xtoy else dx
                    net = receipt_flows(rec)
                    tin = tout = None
                    for (holder, tok), v in net.items():
                        if holder != pool:
                            continue
                        if v == amt_in_exp and amt_in_exp > 0:
                            tin = tok
                        if v == -amt_out_exp and amt_out_exp > 0:
                            tout = tok
                    if tin is None and tout is None:
                        skipped += 1
                        continue
                    leg = (pool, kind, tin or ETH0, amt_in_exp,
                           tout or ETH0, amt_out_exp)
                if leg is not None:
                    legs.append(leg)
                    legs_by_kind[kind] += 1
            out.execute(
                "INSERT OR REPLACE INTO ledger_tx VALUES (?,?,?,?,?,?,?,?,?)",
                (h, b, tx.get("transactionIndex"),
                 (tx.get("from") or "").lower(), (tx.get("to") or "").lower(),
                 hexint(rec.get("status")), hexint(rec.get("gasUsed")),
                 hexint(rec.get("effectiveGasPrice")), len(legs)))
            for i, (pool, kind, tin, ain, tout, aout) in enumerate(legs):
                out.execute(
                    "INSERT OR REPLACE INTO legs VALUES (?,?,?,?,?,?,?,?)",
                    (h, i, pool, kind, tin, human(ain, tin),
                     tout, human(aout, tout)))
                n_legs += 1
            n_tx += 1
    out.executemany("INSERT OR REPLACE INTO meta VALUES (?,?)", [
        ("first_block", str(first)), ("last_block", str(last)),
        ("tx_count", str(n_tx)), ("leg_count", str(n_legs)),
        ("swap_events", str(swap_events)),
    ])
    out.commit()
    out.close()

    # --- self-check: independent recount of format topics in receipts ---
    recount = sum(
        1 for rec in receipts.values() for log in rec.get("logs", [])
        if (log.get("topics") or [""])[0].lower() in FORMATS)

    # --- unknown-topic residue (registry: known_topics.json, if present) ---
    unknown_topics = []
    tx_with_unknown = 0
    reg_path = Path(__file__).with_name("known_topics.json")
    if reg_path.exists():
        known = set(json.loads(reg_path.read_text())["topics"])
        ev_c = Counter()
        txs_of = defaultdict(set)
        for h, rec in receipts.items():
            here = set()
            for log in rec.get("logs", []):
                ts = log.get("topics") or []
                if not ts:
                    continue
                t0 = ts[0].lower()
                if t0 not in known:
                    ev_c[t0] += 1
                    here.add(t0)
            for t0 in here:
                txs_of[t0].add(h)
            if here:
                tx_with_unknown += 1
        unknown_topics = [
            {"topic": t0, "events": ev_c[t0], "txs": len(txs_of[t0])}
            for t0 in ev_c if ev_c[t0] >= 5]
        unknown_topics.sort(key=lambda r: -r["txs"])
        unknown_topics = unknown_topics[:20]

    manifest = {
        "first_block": first, "last_block": last, "tx": n_tx,
        "legs": n_legs, "swap_events": swap_events,
        "swap_events_recount": recount,
        "legs_by_kind": dict(legs_by_kind),
        "skipped_unresolved": skipped,
        "chunk_bytes": os.path.getsize(chunk_path),
        "ledger_bytes": os.path.getsize(ledger_path),
        "tx_with_unknown_topic": tx_with_unknown,
        "unknown_topics": unknown_topics,
    }
    with open(ledger_path + ".manifest.json", "w") as f:
        json.dump(manifest, f, indent=1)
    print(json.dumps(manifest, indent=1))
    if recount != swap_events:
        print("SELF-CHECK FAILED: swap event recount mismatch", file=sys.stderr)
        sys.exit(1)
    if n_tx != len(receipts):
        print("SELF-CHECK FAILED: tx/receipt count mismatch", file=sys.stderr)
        sys.exit(1)
    print("SELF-CHECK OK")


if __name__ == "__main__":
    main()
