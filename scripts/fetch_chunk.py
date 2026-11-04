#!/usr/bin/env python3
"""Fetch a range of Base mainnet blocks into a portable SQLite chunk.

Output schema (kept identical to the long-running local dataset so chunks
merge with a plain ATTACH + INSERT):
  blocks(block_number INTEGER PRIMARY KEY, raw_json TEXT)
  transactions(hash TEXT PRIMARY KEY, block_number INTEGER, tx_index INTEGER, raw_tx TEXT)
  receipts(tx_hash TEXT PRIMARY KEY, receipt_json TEXT)

Public RPC only, no API keys. Sources, in order:
  https://base-rpc.publicnode.com , https://mainnet.base.org
Raw transactions are fetched in JSON-RPC batches of 25. Type 0x7e (L2
deposit) transactions have no signed raw form and are stored with raw_tx
NULL, matching how the chain itself represents them.

Resumable: per-block commit; blocks already present in the output DB are
skipped, so re-running the same command continues where it stopped.

Usage:
  python3 fetch_chunk.py --first 53440500 --last 53440599 --out chunk_53440500_53440599.db
Writes <out>.manifest.json next to the DB with counts and the DB sha256.
"""
import argparse, hashlib, json, sqlite3, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor

PUBLIC = ["https://base-rpc.publicnode.com", "https://mainnet.base.org"]
BATCH = 25


def _curl(url, payload, timeout=90):
    body = json.dumps(payload)
    p = subprocess.run(
        ["curl", "-sS", "-m", str(timeout), "-H", "Content-Type: application/json",
         "-A", "Mozilla/5.0", "--data", body, url],
        capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"curl rc={p.returncode}: {p.stderr[:120]}")
    return json.loads(p.stdout)


def rpc(url, method, params, tries=5):
    last = None
    for a in range(tries):
        try:
            d = _curl(url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            if d.get("result") is not None:
                return d["result"]
            last = d.get("error")
            if last and "rate" in str(last).lower():
                time.sleep(10 + a * 10)
                continue
        except Exception as e:
            last = str(e)
        time.sleep(2 + a * 2)
    raise RuntimeError(f"{method} failed at {url}: {last}")


def rpc_any(method, params):
    for u in PUBLIC:
        try:
            return rpc(u, method, params)
        except Exception:
            continue
    raise RuntimeError(f"{method} failed at all endpoints")


def rpc_batch_raw(hashes):
    """Return {hash: raw_tx or None}. Batch first; singles as fallback."""
    out, pending = {}, list(hashes)
    for u in PUBLIC:
        if not pending:
            break
        still = []
        for i in range(0, len(pending), BATCH):
            chunk = pending[i:i + BATCH]
            try:
                payload = [{"jsonrpc": "2.0", "id": j, "method": "eth_getRawTransactionByHash",
                            "params": [h]} for j, h in enumerate(chunk)]
                res = _curl(u, payload)
                if not isinstance(res, list):
                    raise RuntimeError("batch not supported")
                by_id = {r.get("id"): r.get("result") for r in res}
                for j, h in enumerate(chunk):
                    if by_id.get(j):
                        out[h] = by_id[j]
                    else:
                        still.append(h)
            except Exception:
                still.extend(chunk)
            time.sleep(0.2)
        pending = still
    for h in pending:
        try:
            out[h] = rpc_any("eth_getRawTransactionByHash", [h])
        except Exception:
            out[h] = None
    return out


def fetch_block(bn):
    blk = rpc_any("eth_getBlockByNumber", [hex(bn), True])
    txs = blk["transactions"]
    receipts = None
    for u in PUBLIC:
        try:
            receipts = rpc(u, "eth_getBlockReceipts", [hex(bn)])
            break
        except Exception:
            receipts = None
    by_hash = {r["transactionHash"]: r for r in receipts} if receipts else {}
    need_raw = [t["hash"] for t in txs if t.get("type") != "0x7e"]
    raws = rpc_batch_raw(need_raw) if need_raw else {}
    rows_t, rows_r = [], []
    for i, t in enumerate(txs):
        rows_t.append((t["hash"], bn, i, raws.get(t["hash"])))
        r = by_hash.get(t["hash"])
        if r is None:
            r = rpc_any("eth_getTransactionReceipt", [t["hash"]])
        rows_r.append((t["hash"], json.dumps(r)))
    return (bn, json.dumps(blk)), rows_t, rows_r


DDL = """
CREATE TABLE IF NOT EXISTS blocks(block_number INTEGER PRIMARY KEY, raw_json TEXT);
CREATE TABLE IF NOT EXISTS transactions(hash TEXT PRIMARY KEY, block_number INTEGER,
  tx_index INTEGER, raw_tx TEXT);
CREATE TABLE IF NOT EXISTS receipts(tx_hash TEXT PRIMARY KEY, receipt_json TEXT);
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--first", type=int, required=True)
    ap.add_argument("--last", type=int, required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()
    if args.last < args.first:
        sys.exit("--last must be >= --first")

    con = sqlite3.connect(args.out, timeout=120)
    con.executescript(DDL)
    want = list(range(args.first, args.last + 1))
    have = {r[0] for r in con.execute("SELECT block_number FROM blocks")}
    todo = [b for b in want if b not in have]
    print(f"out={args.out} want={len(want)} have={len(want) - len(todo)} todo={len(todo)}", flush=True)

    def safe_fetch(bn):
        try:
            return fetch_block(bn)
        except Exception as e:
            print(f"FAILED block {bn}: {e} (re-run to retry)", flush=True)
            return None

    done, t0 = 0, time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for got in ex.map(safe_fetch, todo):
            if got is None:
                continue
            brow, rows_t, rows_r = got
            con.execute("INSERT OR REPLACE INTO blocks VALUES (?,?)", brow)
            con.executemany("INSERT OR REPLACE INTO transactions VALUES (?,?,?,?)", rows_t)
            con.executemany("INSERT OR REPLACE INTO receipts VALUES (?,?)", rows_r)
            con.commit()
            done += 1
            if done % 10 == 0 or done == len(todo):
                rate = done / max(time.time() - t0, 1)
                print(f"{done}/{len(todo)} blocks (last {brow[0]}), {rate:.2f} blk/s", flush=True)

    # Self-checks on the finished chunk.
    blocks = [r[0] for r in con.execute(
        "SELECT block_number FROM blocks WHERE block_number BETWEEN ? AND ? ORDER BY block_number",
        (args.first, args.last))]
    missing = [b for b in want if b not in set(blocks)]
    n_tx = con.execute("SELECT COUNT(*) FROM transactions WHERE block_number BETWEEN ? AND ?",
                       (args.first, args.last)).fetchone()[0]
    n_rc = con.execute(
        "SELECT COUNT(*) FROM receipts WHERE tx_hash IN "
        "(SELECT hash FROM transactions WHERE block_number BETWEEN ? AND ?)",
        (args.first, args.last)).fetchone()[0]
    linkage_ok, prev_hash = True, None
    for bn in blocks:
        raw = json.loads(con.execute("SELECT raw_json FROM blocks WHERE block_number=?",
                                     (bn,)).fetchone()[0])
        if prev_hash is not None and raw["parentHash"] != prev_hash:
            linkage_ok = False
        prev_hash = raw["hash"]
    con.execute("VACUUM")
    con.commit(), con.close()

    import os
    sha = hashlib.sha256(open(args.out, "rb").read()).hexdigest()
    manifest = {"first": args.first, "last": args.last, "blocks": len(blocks),
                "missing": missing, "transactions": n_tx, "receipts": n_rc,
                "parent_linkage_ok": linkage_ok, "db_file": os.path.basename(args.out),
                "db_bytes": os.path.getsize(args.out), "db_sha256": sha}
    with open(args.out + ".manifest.json", "w") as f:
        json.dump(manifest, f, indent=1)
    print("manifest:", json.dumps(manifest))
    if missing or n_tx != n_rc or not linkage_ok:
        sys.exit("SELF-CHECK FAILED")
    print("DONE")


if __name__ == "__main__":
    main()
