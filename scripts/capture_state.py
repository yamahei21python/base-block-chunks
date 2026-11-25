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


def snapshot(pools, block):
    hb = hex_block(block)
    calls = []
    for pool in pools:
        calls.append(("eth_getStorageAt", [pool, "0x0", hb]))
        calls.append(("eth_call", [{"to": pool, "data": LIQ_SELECTOR}, hb]))
        for slot in range(1, 7):
            calls.append(("eth_getStorageAt", [pool, hex(slot), hb]))
    responses = rpc_batch(calls, block)
    out = []
    for i, pool in enumerate(pools):
        base = i * 8
        sqrt, tick = decode_slot0(result_of(responses[base]))
        liq_word = result_of(responses[base + 1])
        want = None
        if isinstance(liq_word, str) and liq_word.startswith("0x"):
            try:
                want = int(liq_word, 16) & MASK128
            except ValueError:
                want = None
        liquidity = None
        if want is not None:
            for slot in range(1, 7):
                word = result_of(responses[base + 1 + slot])
                if isinstance(word, str) and word.startswith("0x"):
                    try:
                        val = int(word, 16) & MASK128
                    except ValueError:
                        continue
                    if val == want:
                        liquidity = str(val)
                        break
        if sqrt is None and liquidity is None:
            out.append(None)  # unreadable pool: explicit null, never zero
        else:
            out.append({"pool_id": pool, "sqrt_price_x96": sqrt,
                        "tick": tick, "liquidity": liquidity})
    return out


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
