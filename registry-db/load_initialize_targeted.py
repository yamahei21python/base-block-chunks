#!/usr/bin/env python3
"""Load the TARGETED Initialize fetch into registry.db.

Truth source: ~/workspace/chain-archive/initialize/targeted.jsonl
(708 poolIds: 704 registry v4 + 4 held, queried by topic1 filter).
Inserts pool_pair_assertion rows at priority=100, creates pool rows
for the 4 held v4 ids, then cross-checks the 704 registry v4 pools and
resolves all 10 held poolIds. Report: initialize_crosscheck.json.
"""
import json, os, sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "registry.db")
SRC = os.path.expanduser("~/workspace/chain-archive/initialize/targeted.jsonl")
CHAIN = 8453
HELD = [
    "0x12d76c5c8ec8edffd3c143995b0aa43fe44a6d71eb9113796272909e54b8e078",
    "0x3b92f4962d799934d24d114f3d7569f836d5d9af01bd92ecee613d5ba89f17a2",
    "0x36d7043ec113e9926b8f98392544a1b00b027556269bd35e2945403c69806f97",
    "0xc02bef5e619aba0caee987c68620fd7a436c37c83b8269d825c2ac96b8dbe70f",
    "0x322d9aef3cf895da9e35b95b6c6e8f3c5e2a90b6d447a8bbb1ff2616b608a4d0",
    "0xc37144e227dde7cf27f42d81ffa7099c8b4c71fbeb1cc28da8370bfd59e82cb4",
    "0x7f68ded7abb35ab67997c0604bc24a984d007ad33e6b793562853cbd59efd735",
    "0x9a4ff629a58a25e52fc63b0179f825cf4984e14663a17c89e1a96734db630448",
    "0xc6e6728c2e10e90b1ea3d1571eaaa6c4613d1583eba55f3fe2a37b972d26e555",
    "0xa975393c7d58e873eb6e23617e6e24b441d156063e4befa4e2042bf019a9ede1",
]

def b(h):
    return bytes.fromhex(h[2:])

def addr(topic):
    return "0x" + topic[-40:]

def words(data):
    h = data[2:]
    return [h[i:i + 64] for i in range(0, len(h), 64)]

def main():
    truth = {}
    for line in open(SRC):
        log = json.loads(line)
        w = words(log["data"])
        truth[log["topic1"].lower()] = {
            "currency0": addr(log["topic2"]),
            "currency1": addr(log["topic3"]),
            # v4 DYNAMIC_FEE_FLAG = 0x800000: no fixed fee -> NULL fee_ppm
            "fee": None if int(w[0], 16) == 0x800000 else int(w[0], 16),
            "dynamic_fee": int(w[0], 16) == 0x800000,
            "tick_spacing": int(w[1], 16),
            "hooks": "0x" + w[2][-40:],
            "block": log["block_number"],
            "tx": log["transaction_hash"],
        }
    print(f"truth entries: {len(truth)}")

    con = sqlite3.connect(DB)
    con.execute("PRAGMA foreign_keys = ON")
    cur = con.cursor()
    cur.execute(
        "INSERT INTO run(kind, source, started_at, status, meta_json) VALUES"
        " ('ingest','hypersync:initialize-targeted', datetime('now'),"
        " 'running', ?)", (json.dumps({"pools": len(truth)}),))
    run_id = cur.lastrowid

    existing = {bytes(r[0]).hex(): r[1] for r in cur.execute(
        "SELECT pool_id, id FROM pool WHERE pool_id IS NOT NULL")}
    n_new = 0
    for pid, e in truth.items():
        dbid = existing.get(pid[2:])
        if dbid is None:
            cur.execute(
                "INSERT INTO pool(chain_id, pool_id, kind, status,"
                " first_seen_block) VALUES (?, ?, 'v4', 'verified', ?)",
                (CHAIN, b(pid), e["block"]))
            dbid = cur.lastrowid
            n_new += 1
        # 冪等ガード: 同じ Initialize 主張が既にあれば再投入しない
        # （再実行で主張が二重化した実績があるため）
        already = cur.execute(
            "SELECT 1 FROM pool_pair_assertion WHERE pool_id=?"
            " AND source_kind='initialize_event' AND priority=100",
            (dbid,)).fetchone()
        if already:
            continue
        cur.execute(
            "INSERT INTO pool_pair_assertion(pool_id, token0, token1, fee_ppm,"
            " source_kind, priority, source_ref, run_id) VALUES"
            " (?, ?, ?, ?, 'initialize_event', 100, ?, ?)",
            (dbid, b(e["currency0"]), b(e["currency1"]), e["fee"],
             f"initialize tx:{e['tx']} block:{e['block']}"
             + (" dynamic_fee" if e["dynamic_fee"] else ""), run_id))
    print(f"assertions inserted: {len(truth)} ({n_new} pools newly created)")

    imported = {}
    for pid_bytes, t0, t1 in cur.execute(
            "SELECT p.pool_id, a.token0, a.token1 FROM pool p"
            " JOIN pool_pair_assertion a ON a.pool_id = p.id"
            " WHERE p.kind='v4' AND a.source_kind='registry_import'"):
        imported["0x" + bytes(pid_bytes).hex()] = (
            "0x" + bytes(t0).hex(), "0x" + bytes(t1).hex())
    match, mismatch, missing = [], [], []
    for pid, pair in imported.items():
        t = truth.get(pid)
        if t is None:
            missing.append(pid)
        elif (t["currency0"], t["currency1"]) == pair:
            match.append(pid)
        else:
            mismatch.append({"poolId": pid, "registry": list(pair),
                             "truth": [t["currency0"], t["currency1"]]})
    held = {}
    for pid in HELD:
        t = truth.get(pid)
        reg = imported.get(pid)
        held[pid] = {
            "in_registry": reg is not None,
            "registry_pair": list(reg) if reg else None,
            "truth": ([t["currency0"], t["currency1"]] if t else None),
            "fee": t["fee"] if t else None,
            "dynamic_fee": t["dynamic_fee"] if t else None,
            "block": t["block"] if t else None,
            "tx": t["tx"] if t else None,
            "verdict": ("not_in_truth" if t is None else
                        "registry_correct" if reg and
                        (t["currency0"], t["currency1"]) == reg else
                        "registry_wrong" if reg else "new_pool_confirmed"),
        }
    report = {"truth_pools": len(truth), "registry_v4_checked": len(imported),
              "match": len(match), "mismatch": mismatch,
              "not_in_truth": missing, "held10": held}
    out = os.path.join(HERE, "initialize_crosscheck.json")
    json.dump(report, open(out, "w"), indent=1)
    cur.execute("UPDATE run SET status='ok', finished_at=datetime('now'),"
                " rows_written=? WHERE id=?", (len(truth), run_id))
    con.commit()
    print(f"cross-check: {len(imported)} registry v4 -> {len(match)} match,"
          f" {len(mismatch)} mismatch, {len(missing)} not in truth")
    for pid, h in held.items():
        print("held:", pid[:14], h["verdict"],
              "truth:", h["truth"], "registry:", h["registry_pair"])
    print("report:", out)
    con.close()

if __name__ == "__main__":
    main()
