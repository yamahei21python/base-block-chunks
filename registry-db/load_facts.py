#!/usr/bin/env python3
"""Merge facts-out/ partials and load the 4 fact tables into registry.db.

Review gates (abort before any insert if violated):
  - per topic: sum(shape occurrences) == sum(emitter log_counts)
  - Transfer topic's dominant shape is (3 topics, 1 data word)
  - UniV3 Swap topic's dominant shape is (3 topics, 5 data words)
Topics observed in chunks but absent from the registry are reported,
not inserted (the registry decides membership; observations wait).
"""
import glob, json, os, sqlite3, sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "registry.db")
PARTS = sorted(glob.glob(os.path.join(HERE, "facts-out", "*.json")))
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
UNIV3_SWAP = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"

def b(h):
    return bytes.fromhex(h[2:])

STATE = os.path.join(HERE, "facts-out", ".loaded.json")

def main():
    loaded_before = set()
    if os.path.exists(STATE):
        loaded_before = set(json.load(open(STATE)))
    new_parts = [p for p in PARTS
                 if os.path.basename(p) not in loaded_before]
    if not new_parts:
        print("no new partials to load")
        return
    print(f"loading {len(new_parts)} new partials "
          f"({len(loaded_before)} already loaded)")
    shapes = defaultdict(Counter)
    emitters = {}
    samples = {}
    flows = {}
    total_logs = 0
    for p in new_parts:
        part = json.load(open(p))
        total_logs += part["logs"]
        for t0, sh in part["shapes"].items():
            for key, n in sh.items():
                tc, dw = key.split(":")
                shapes[t0][(int(tc), int(dw))] += n
        for t0, em in part["emitters"].items():
            slot = emitters.setdefault(t0, {})
            for addr, (cnt, f, l) in em.items():
                e = slot.get(addr)
                if e is None:
                    slot[addr] = [cnt, f, l]
                else:
                    e[0] += cnt; e[1] = min(e[1], f); e[2] = max(e[2], l)
        for t0, ss in part["samples"].items():
            cur = samples.setdefault(t0, [])
            have = {tx for tx, _ in cur}
            for tx, blk in ss:
                if tx not in have and len(cur) < 8:
                    cur.append((tx, blk)); have.add(tx)
        for t0, fs in part["flows"].items():
            cur = flows.setdefault(t0, [])
            have = {f["tx"] for f in cur}
            for f in fs:
                if f["tx"] not in have and len(cur) < 8:
                    cur.append(f); have.add(f["tx"])
    print(f"partials: {len(new_parts)}, logs: {total_logs}, "
          f"topics observed: {len(shapes)}")

    # ---- review gates ----
    bad = 0
    for t0, sh in shapes.items():
        if sum(sh.values()) != sum(e[0] for e in emitters[t0].values()):
            bad += 1
    print("gate shape/emitter totals mismatch topics:", bad)
    if bad:
        sys.exit("review gate failed: shape/emitter totals")
    for t0, expect in [(TRANSFER, (3, 1)), (UNIV3_SWAP, (3, 5))]:
        if t0 in shapes:
            top = shapes[t0].most_common(1)[0]
            print(f"gate {t0[:12]}: dominant shape {top[0]} "
                  f"({top[1]} occ), expected {expect}")
            if top[0] != expect:
                sys.exit("review gate failed: dominant shape")

    con = sqlite3.connect(DB)
    con.execute("PRAGMA foreign_keys = ON")
    cur = con.cursor()
    known = {bytes(r[0]).hex() for r in cur.execute("SELECT topic0 FROM topic")}
    unknown = [t for t in shapes if t[2:] not in known and t not in known]
    unknown = [t for t in shapes if t[2:] not in known]
    print(f"observed topics not in registry: {len(unknown)} (skipped)")
    cur.execute(
        "INSERT INTO run(kind, source, started_at, status, meta_json) VALUES"
        " ('ingest','chunk-db-extract', datetime('now'), 'running', ?)",
        (json.dumps({"chunks": len(PARTS), "logs": total_logs}),))
    run_id = cur.lastrowid
    n_sh = n_em = n_ev = n_fl = 0
    for t0, sh in shapes.items():
        if t0[2:] not in known:
            continue
        tb = b(t0)
        for (tc, dw), n in sh.items():
            cur.execute(
                "INSERT INTO topic_shape(topic0, topics_count, data_words,"
                " indexed_count, occurrences, last_run_id) VALUES"
                " (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(topic0, topics_count, data_words) DO UPDATE"
                " SET occurrences = occurrences + excluded.occurrences,"
                " last_run_id = excluded.last_run_id",
                (tb, tc, dw, tc - 1, n, run_id))
            n_sh += 1
        for addr, (cnt, f, l) in emitters[t0].items():
            cur.execute(
                "INSERT INTO topic_emitter(topic0, emitter, log_count,"
                " first_seen_block, last_seen_block) VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(topic0, emitter) DO UPDATE SET"
                " log_count = log_count + excluded.log_count,"
                " first_seen_block = min(first_seen_block,"
                " excluded.first_seen_block),"
                " last_seen_block = max(last_seen_block,"
                " excluded.last_seen_block)",
                (tb, b(addr), cnt, f, l))
            n_em += 1
        for tx, blk in samples.get(t0, []):
            cur.execute(
                "INSERT OR IGNORE INTO topic_evidence(topic0, tx_hash,"
                " block_number, role) VALUES (?, ?, ?, 'sample')",
                (tb, b(tx), blk))
            n_ev += 1
        for f in flows.get(t0, []):
            cur.execute(
                "INSERT OR IGNORE INTO topic_flow(topic0, tx_hash, direction,"
                " transfers_in, transfers_out, has_native_in, has_weth_wrap,"
                " amount_match) VALUES (?, ?, ?, ?, ?, ?, ?, 'unmeasured')",
                (tb, b(f["tx"]), f["direction"], f["transfers_in"],
                 f["transfers_out"], f["has_native_in"], f["has_weth_wrap"]))
            n_fl += 1
    cur.execute("UPDATE run SET status='ok', finished_at=datetime('now'),"
                " rows_written=? WHERE id=?", (n_sh + n_em + n_ev + n_fl, run_id))
    con.commit()
    json.dump(sorted(loaded_before |
                     {os.path.basename(p) for p in new_parts}),
              open(STATE, "w"))
    print(f"loaded: shapes {n_sh}, emitters {n_em}, evidence {n_ev}, "
          f"flows {n_fl}")
    cov = cur.execute(
        "SELECT (SELECT count(DISTINCT topic0) FROM topic_shape),"
        " (SELECT count(DISTINCT topic0) FROM topic_emitter),"
        " (SELECT count(DISTINCT topic0) FROM topic_evidence),"
        " (SELECT count(DISTINCT topic0) FROM topic_flow),"
        " (SELECT count(*) FROM topic)").fetchone()
    print(f"topic coverage: shape {cov[0]}, emitter {cov[1]}, "
          f"evidence {cov[2]}, flow {cov[3]} of {cov[4]} registry topics")
    con.close()

if __name__ == "__main__":
    main()
