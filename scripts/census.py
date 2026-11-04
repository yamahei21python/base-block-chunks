#!/usr/bin/env python3
"""Event-topic census over raw chunk DBs: what is known, what is not.

Reads receipts from one or more SQLite DBs (chunk schema or the richer
replay_verify schema; only receipts.receipt_json is used), classifies
every log topic against the versioned registry in known_topics.json,
and reports the UNKNOWN residue: topics nobody has classified yet.

The point is operational, not analytical: the residue is a number that
should trend to zero as formats are identified (swap -> decoder gains a
format; non-swap -> registry gains an entry with a reason). Run it per
chunk for a light check, or over accumulated DBs for a deep sweep.

Usage: census.py DB [DB ...] [--registry PATH] [--min-events N]
                 [--min-txs N] [--out report.json]
A topic is flagged into the residue list when events >= N or txs >= M
(defaults: 100 / 50). All unknown topics are kept in the JSON report;
the printed summary shows the flagged ones.
"""
import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def load_registry(path):
    reg = json.loads(Path(path).read_text())
    return reg["topics"], reg.get("version", "?")


def scan(db_paths, topics_reg):
    events = Counter()
    txs_of = defaultdict(set)
    emitters = defaultdict(set)
    tx_classes = Counter()   # per-tx rollup
    total_tx = 0
    swap_topics = {t for t, v in topics_reg.items() if v.get("class") == "swap"}
    for path in db_paths:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        for (rj,) in con.execute("SELECT receipt_json FROM receipts"):
            rec = json.loads(rj)
            h = rec["transactionHash"].lower()
            total_tx += 1
            here = set()
            for log in rec.get("logs", []):
                ts = log.get("topics") or []
                if not ts:
                    continue
                t0 = ts[0].lower()
                events[t0] += 1
                emitters[t0].add(log["address"].lower())
                here.add(t0)
            for t0 in here:
                txs_of[t0].add(h)
            if here & swap_topics:
                tx_classes["tx_with_known_swap"] += 1
            if TRANSFER in here:
                tx_classes["tx_with_transfer"] += 1
                if not (here & swap_topics):
                    tx_classes["tx_transfer_no_known_swap"] += 1
            unknown_here = {t for t in here if t not in topics_reg}
            if unknown_here:
                tx_classes["tx_with_unknown_topic"] += 1
        con.close()
    return events, txs_of, emitters, tx_classes, total_tx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dbs", nargs="+")
    ap.add_argument("--registry", default=str(Path(__file__).with_name("known_topics.json")))
    ap.add_argument("--min-events", type=int, default=100)
    ap.add_argument("--min-txs", type=int, default=50)
    ap.add_argument("--out")
    args = ap.parse_args()

    topics_reg, version = load_registry(args.registry)
    events, txs_of, emitters, tx_classes, total_tx = scan(args.dbs, topics_reg)

    unknown = [
        {"topic": t0, "events": events[t0], "txs": len(txs_of[t0]),
         "emitters": len(emitters[t0]),
         "flagged": events[t0] >= args.min_events or len(txs_of[t0]) >= args.min_txs}
        for t0 in events if t0 not in topics_reg
    ]
    unknown.sort(key=lambda r: (-r["txs"], -r["events"]))
    known = [
        {"topic": t0, "class": topics_reg[t0].get("class"),
         "name": topics_reg[t0].get("name"), "events": events[t0],
         "txs": len(txs_of[t0])}
        for t0 in events if t0 in topics_reg
    ]
    known.sort(key=lambda r: -r["txs"])

    report = {
        "registry_version": version,
        "dbs": args.dbs,
        "total_tx": total_tx,
        "tx_classes": dict(tx_classes),
        "known_topics_seen": known,
        "unknown_topics": unknown,
        "unknown_flagged": [u for u in unknown if u["flagged"]],
    }
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=1))

    sw = tx_classes.get("tx_with_known_swap", 0)
    print(f"registry v{version} | tx total {total_tx}")
    print(f"txs with a known swap format: {sw} "
          f"({100.0 * sw / total_tx:.1f}%)")
    print(f"txs with transfers but no known swap: "
          f"{tx_classes.get('tx_transfer_no_known_swap', 0)}")
    print(f"unknown topics: {len(unknown)} "
          f"(flagged >= {args.min_events} events or >= {args.min_txs} txs: "
          f"{len(report['unknown_flagged'])})")
    for u in report["unknown_flagged"][:25]:
        print(f"  UNKNOWN {u['topic'][:18]}.. events={u['events']} "
              f"txs={u['txs']} emitters={u['emitters']}")
    if args.out:
        print(f"report written: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
