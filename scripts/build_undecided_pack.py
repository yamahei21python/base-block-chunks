#!/usr/bin/env python3
"""Build the undecided-events pack for one chunk.

Reads a chunk DB (fetch_chunk.py schema) and a census report
(census.py --out), and for every topic the census found UNKNOWN to the
registry, emits: counts, emitter count, a per-topic value-flow summary
(Transfer netting over the transactions that contain the topic), and up
to 3 sample transactions with their full log lists. This pack is the
only input a classification round needs; the raw chunk is discarded.

Flow classes (per transaction, holders = Transfer from/to):
  exchange_like: some holder both sends token A and receives token B (A != B)
  one_way:       value moves but no holder completes an exchange
  no_value:      no ERC-20 Transfer logs at all
Native-token (ETH) moves leave no Transfer log; that is a known limit
of the flow signal and why classification also uses samples + chain data.

Usage:
  python3 build_undecided_pack.py chunk.db census.json undecided_pack.json
"""
import json, sqlite3, sys
from collections import defaultdict

TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def flow_of(logs):
    net = defaultdict(lambda: defaultdict(int))
    for lg in logs:
        t = [x.lower() for x in lg.get("topics", [])]
        if len(t) == 3 and t[0] == TRANSFER:
            try:
                v = int(lg.get("data", "0x0"), 16)
            except ValueError:
                continue
            tok = lg["address"].lower()
            net["0x" + t[1][-40:]][tok] -= v
            net["0x" + t[2][-40:]][tok] += v
    for h, toks in net.items():
        neg = [t for t, v in toks.items() if v < 0]
        pos = [t for t, v in toks.items() if v > 0]
        if any(a != b for a in neg for b in pos):
            return "exchange_like"
    return "one_way" if net else "no_value"


def main():
    chunk_path, census_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
    census = json.load(open(census_path))
    unknown = {u["topic"]: u for u in census.get("unknown_topics", [])}
    pack = {
        "registry_version": census.get("registry_version"),
        "total_tx": census.get("total_tx"),
        "unknown_tx_total": 0,
        "unknown": [],
    }
    if not unknown:
        json.dump(pack, open(out_path, "w"), indent=1)
        print("no unknown topics")
        return
    flows = {t: defaultdict(int) for t in unknown}
    logs_of = {}
    con = sqlite3.connect(chunk_path)
    seen_tx = set()
    for tx_hash, receipt_json in con.execute("SELECT tx_hash, receipt_json FROM receipts"):
        r = json.loads(receipt_json)
        logs = r.get("logs", [])
        hit = {lg["topics"][0].lower() for lg in logs if lg.get("topics")} & set(unknown)
        if not hit:
            continue
        seen_tx.add(tx_hash)
        logs_of[tx_hash] = logs
        f = flow_of(logs)
        for t in hit:
            flows[t][f] += 1
    pack["unknown_tx_total"] = len(seen_tx)
    for topic, u in sorted(unknown.items(), key=lambda kv: -kv[1]["txs"]):
        samples = [
            {"tx": h, "logs": logs_of[h]}
            for h in u.get("sample_txs", [])[:3]
            if h in logs_of
        ]
        pack["unknown"].append({
            "topic": topic,
            "events": u["events"],
            "txs": u["txs"],
            "emitters": u["emitters"],
            "flow": dict(flows[topic]),
            "samples": samples,
        })
    json.dump(pack, open(out_path, "w"), indent=1)
    print(f"unknown topics={len(unknown)} unknown txs={len(seen_tx)} -> {out_path}")


if __name__ == "__main__":
    main()
