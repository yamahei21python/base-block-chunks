#!/usr/bin/env python3
"""Capture block-timestamped USD prices for every token seen in a ledger chunk.

Usage: capture_prices.py <ledger.db> <out.json>
Uses DefiLlama's historical endpoint at the chunk's first block timestamp.
A price is never back-filled with a later spot price: observed_at is the
earlier of the API snapshot time and the block time; the raw API timestamp is
kept in source_ts when it differs. Missing prices stay null.
"""
import datetime as dt
import json
import math
import sqlite3
import sys
import urllib.parse
import urllib.request

from rpc_util import result_of, rpc_batch

ETH0 = "0x0000000000000000000000000000000000000000"
ETH_KEY = "coingecko:ethereum"
LLAMA = "https://coins.llama.fi/prices/historical/"
MAX_KEYS = 100


def iso(ts):
    return dt.datetime.fromtimestamp(int(ts), dt.timezone.utc).isoformat().replace("+00:00", "Z")


def coin_key(token):
    return ETH_KEY if token.lower() == ETH0 else "base:" + token.lower()


def fetch_prices(ts, keys):
    prices = {}
    for start in range(0, len(keys), MAX_KEYS):
        part = keys[start:start + MAX_KEYS]
        url = LLAMA + str(int(ts)) + "/" + urllib.parse.quote(",".join(part), safe=",:")
        last = None
        for attempt in range(4):
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": "base-block-chunks/0.5"})
                with urllib.request.urlopen(req, timeout=45) as fh:
                    data = json.load(fh)
                prices.update(data.get("coins", {}))
                break
            except Exception as exc:
                last = exc
        else:
            print(f"WARN: DefiLlama batch {start} failed: {last}", file=sys.stderr)
    return prices


def main():
    if len(sys.argv) != 3:
        sys.exit("usage: capture_prices.py <ledger.db> <out.json>")
    con = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
    meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
    block = int(meta["first_block"])
    tokens = sorted({r[0].lower() for r in con.execute(
        "SELECT token_in FROM legs UNION SELECT token_out FROM legs") if r[0]})
    con.close()

    resp = rpc_batch([("eth_getBlockByNumber", [hex(block), False])], block)[0]
    head = result_of(resp)
    if not isinstance(head, dict) or head.get("timestamp") is None:
        sys.exit(f"block timestamp unavailable for {block}")
    block_ts = int(head["timestamp"], 16)
    coins = fetch_prices(block_ts, [coin_key(t) for t in tokens])
    prices = {}
    for token in tokens:
        entry = coins.get(coin_key(token))
        if not entry or not isinstance(entry.get("price"), (int, float)):
            prices[token] = None
            continue
        price = float(entry["price"])
        if not math.isfinite(price):
            prices[token] = None
            continue
        source_ts = int(entry.get("timestamp") or block_ts)
        observed_ts = min(source_ts, block_ts)
        record = {"usd": price, "source": "defillama", "observed_at": iso(observed_ts)}
        if source_ts != observed_ts:
            record["source_ts"] = iso(source_ts)
        prices[token] = record
    doc = {"chain_id": 8453, "block": block, "block_ts": iso(block_ts),
           "prices": prices}
    with open(sys.argv[2], "w") as fh:
        json.dump(doc, fh, separators=(",", ":"))
    print(f"prices block={block} tokens={len(tokens)} "
          f"found={sum(v is not None for v in prices.values())}")


if __name__ == "__main__":
    main()
