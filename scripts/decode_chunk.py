#!/usr/bin/env python3
"""Decode a raw chunk DB (fetch_chunk.py output) into a ledger DB.

Mechanical step only: receipt logs -> swap legs (Uniswap V2/V3 style)
with native token amounts, plus per-tx gas (used x price, both native).
Token/pool metadata comes from public RPC eth_call, prefetched
concurrently and cached in the ledger.

No pricing happens here by design: the ledger stores native amounts as
the source of truth, and USD conversion is a separate downstream step
on the consumer side, where the price source is fixed and controlled.

The raw chunk is an input, never modified; in the Actions pipeline the
raw DB is simply not uploaded once the ledger passes its self-checks,
which is what keeps storage and downstream downloads small.

Usage: decode_chunk.py <chunk.db> <ledger.db>
Writes <ledger.db>.manifest.json next to the ledger.
"""
import json
import os
import sqlite3
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

RPC_URLS = ["https://base-rpc.publicnode.com", "https://mainnet.base.org"]
UA = {"User-Agent": "base-block-chunks/0.3", "Content-Type": "application/json"}

TOPIC_V3_SWAP = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
TOPIC_V2_SWAP = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"


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


def signed256(word_hex):
    v = int(word_hex, 16)
    return v - (1 << 256) if v >= (1 << 255) else v


def hexint(v):
    return int(v, 16) if isinstance(v, str) else v


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

    # --- pass 1: scan swap events (no RPC yet) ---
    raw_swaps = {}
    pools = set()
    swap_topics = 0
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
                    if t0 not in (TOPIC_V2_SWAP, TOPIC_V3_SWAP):
                        continue
                    swap_topics += 1
                    pool = log["address"].lower()
                    pools.add(pool)
                    data = log["data"][2:]
                    words = [data[i:i + 64] for i in range(0, len(data), 64)]
                    if t0 == TOPIC_V3_SWAP:
                        evs.append((pool, "v3", signed256(words[0]),
                                    signed256(words[1])))
                    else:
                        evs.append((pool, "v2") + tuple(
                            int(w, 16) for w in words[:4]))
            raw_swaps[h] = evs

    # --- prefetch pool token pairs + token decimals concurrently ---
    cached_pools = {a: (t0, t1) for a, t0, t1 in
                    out.execute("SELECT address, token0, token1 FROM pool_meta")}

    def fetch_pair(pool):
        return pool, (call_address(pool, "0x0dfe1681").lower(),
                      call_address(pool, "0xd21220a7").lower())

    with ThreadPoolExecutor(max_workers=8) as ex:
        for pool, pair in ex.map(fetch_pair,
                                 [p for p in pools if p not in cached_pools]):
            cached_pools[pool] = pair
            out.execute("INSERT OR REPLACE INTO pool_meta VALUES (?,?,?)",
                        (pool, pair[0], pair[1]))
    out.commit()

    tokens = set()
    for t0, t1 in cached_pools.values():
        tokens.update((t0, t1))
    cached_dec = {a: d for a, d in
                  out.execute("SELECT address, decimals FROM token_meta")}

    def fetch_dec(addr):
        try:
            return addr, int(eth_call(addr, "0x313ce567"), 16)
        except Exception:  # noqa: BLE001 - non-ERC20 emitter etc.
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

    # --- resolve legs and write the ledger ---
    n_tx = n_legs = 0
    for b in sorted(blocks):
        for tx in blocks[b]["transactions"]:
            h = tx["hash"].lower()
            rec = receipts.get(h) or {}
            legs = []
            for ev in raw_swaps.get(h, []):
                pool, kind = ev[0], ev[1]
                token0, token1 = cached_pools[pool]
                if kind == "v3":
                    a0, a1 = ev[2], ev[3]
                    if a0 > 0 and a1 < 0:
                        legs.append((pool, kind, token0, a0, token1, -a1))
                    elif a1 > 0 and a0 < 0:
                        legs.append((pool, kind, token1, a1, token0, -a0))
                else:
                    a0i, a1i, a0o, a1o = ev[2], ev[3], ev[4], ev[5]
                    if a0i > 0 and a1o > 0:
                        legs.append((pool, kind, token0, a0i, token1, a1o))
                    elif a1i > 0 and a0o > 0:
                        legs.append((pool, kind, token1, a1i, token0, a0o))
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
        ("swap_events", str(swap_topics)),
    ])
    out.commit()
    out.close()

    # --- self-check: independent recount of swap topics in raw receipts ---
    recount = sum(
        1 for rec in receipts.values() for log in rec.get("logs", [])
        if (log.get("topics") or [""])[0].lower() in (TOPIC_V2_SWAP, TOPIC_V3_SWAP))
    manifest = {
        "first_block": first, "last_block": last, "tx": n_tx,
        "legs": n_legs, "swap_events": swap_topics,
        "swap_events_recount": recount,
        "chunk_bytes": os.path.getsize(chunk_path),
        "ledger_bytes": os.path.getsize(ledger_path),
    }
    with open(ledger_path + ".manifest.json", "w") as f:
        json.dump(manifest, f, indent=1)
    print(json.dumps(manifest, indent=1))
    if recount != swap_topics:
        print("SELF-CHECK FAILED: swap event recount mismatch", file=sys.stderr)
        sys.exit(1)
    if n_tx != len(receipts):
        print("SELF-CHECK FAILED: tx/receipt count mismatch", file=sys.stderr)
        sys.exit(1)
    print("SELF-CHECK OK")


if __name__ == "__main__":
    main()
