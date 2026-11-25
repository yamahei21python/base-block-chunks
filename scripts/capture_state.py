#!/usr/bin/env python3
"""Capture slot0/liquidity for every pool seen in a ledger chunk.

Usage: capture_state.py <ledger.db> <out.json>
Reads pool ids from legs; reads state at the chunk's first and last block via
eth_getStorageAt. Uniswap-V3-style pools expose slot0 in storage slot 0; the
liquidity slot is located by matching the value returned by liquidity() against
slots 1..6, so unrelated fee-growth words are never mistaken for liquidity.
Failures stay null: no zero filling, no invented state.
"""
import json
import sqlite3
import sys

from rpc_util import result_of, rpc_batch

MASK160 = (1 << 160) - 1
MASK128 = (1 << 128) - 1
MASK24 = (1 << 24) - 1
MIN_TICK, MAX_TICK = -887272, 887272
LIQ_SELECTOR = "0x1a686502"


def hex_block(n):
    return hex(int(n))


def decode_slot0(word):
    if not isinstance(word, str) or not word.startswith("0x"):
        return None, None
    v = int(word, 16)
    sqrt = v & MASK160
    tick = (v >> 160) & MASK24
    if tick & (1 << 23):
        tick -= 1 << 24
    if sqrt == 0 or not MIN_TICK <= tick <= MAX_TICK:
        return None, None
    return str(sqrt), tick


def _uint(word, mask):
    if not isinstance(word, str) or not word.startswith("0x"):
        return None
    try:
        return int(word, 16) & mask
    except ValueError:
        return None


def snapshot(pools, block):
    hb = hex_block(block)
    calls = []
    for pool in pools:
        calls.append(("eth_getStorageAt", [pool, "0x0", hb]))
        calls.append(("eth_call", [{"to": pool, "data": LIQ_SELECTOR}, hb]))
        calls.append(("eth_getStorageAt", [pool, "0x4", hb]))
    responses = rpc_batch(calls, block)
    out, fallback = [], []
    for i, pool in enumerate(pools):
        base = i * 3
        word = result_of(responses[base])
        sqrt, tick = decode_slot0(word)
        want = _uint(result_of(responses[base + 1]), MASK128)
        val4 = _uint(result_of(responses[base + 2]), MASK128)
        liquidity = str(want) if want is not None and val4 == want else None
        out.append({"pool_id": pool, "sqrt_price_x96": sqrt,
                    "tick": tick, "liquidity": liquidity})
        if want is not None and liquidity is None:
            fallback.append((i, pool, want))
    if fallback:
        extra = []
        for _i, pool, _want in fallback:
            for slot in (1, 2, 3, 5, 6):
                extra.append(("eth_getStorageAt", [pool, hex(slot), hb]))
        extra_res = rpc_batch(extra, block)
        for j, (idx, _pool, want) in enumerate(fallback):
            base = j * 5
            for slot in range(5):
                if _uint(result_of(extra_res[base + slot]), MASK128) == want:
                    out[idx]["liquidity"] = str(want)
                    break
    return [None if e["sqrt_price_x96"] is None and e["liquidity"] is None else e
            for e in out]


def main():
    if len(sys.argv) != 3:
        sys.exit("usage: capture_state.py <ledger.db> <out.json>")
    con = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
    meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
    first, last = int(meta["first_block"]), int(meta["last_block"])
    pools = sorted({r[0].lower() for r in con.execute(
        "SELECT DISTINCT pool FROM legs WHERE pool IS NOT NULL AND pool != ''")})
    con.close()
    head = snapshot(pools, first)
    tail = snapshot(pools, last)
    doc = {"chain_id": 8453, "source": "eth_getStorageAt",
           "block": first, "pools": head,
           "snapshots": [{"block": first, "pools": head},
                         {"block": last, "pools": tail}]}
    with open(sys.argv[2], "w") as fh:
        json.dump(doc, fh, separators=(",", ":"))
    print(f"state blocks={first},{last} pools={len(pools)} "
          f"readable={sum(x is not None for x in head)}/"
          f"{sum(x is not None for x in tail)}")


if __name__ == "__main__":
    main()
