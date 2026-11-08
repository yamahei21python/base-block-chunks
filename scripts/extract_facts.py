#!/usr/bin/env python3
"""Extract the 4 fact layers (shape / emitter / evidence / flow inputs)
from raw chunk DBs (blocks/transactions/receipts as raw JSON).

Usage: extract_facts.py <input_dir> <output_dir>

Per chunk DB in <input_dir>, writes one partial JSON to <output_dir>
(checkpointed: existing partials are skipped). The partials are the
GENERATION artifact; a separate loader merges them into the registry
DB after review. Run in GitHub Actions over the `data-*` release
assets so the multi-GB chunk DBs never leave GitHub infrastructure.

Per topic0 aggregates:
  shapes   : {(topics_count, data_words): occurrences}
  emitters : {address: [log_count, first_block, last_block]}
  samples  : up to 8 txs (tx_hash, block) — evidence candidates
  flow     : for each sample tx, the structural flow facts vs the
             emitter (transfers in/out, native in, WETH wrap/unwrap)
             computed from the same receipt + the tx's value/to.
"""
import glob, json, os, sqlite3, sys

TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
WETH = "0x4200000000000000000000000000000000000006"
# keccak-verified topic0s (WETH9 Deposit / Withdrawal)
WETH_DEPOSIT = "0xe1fffcc4923d04b559f4d29a8bfc6cda04eb5b0d3c460751c2402c5c5cc9109c"
WETH_WITHDRAWAL = "0x7fcf532c15f0a6db0bd6d0e038bea71d30d808c7d98cb3bf7268a95bf5081b65"
MAX_SAMPLES = 8

def find_raw_chunks(input_dir):
    out = []
    for path in sorted(glob.glob(os.path.join(input_dir, "chunk_*.db"))):
        if path.endswith("_ledger.db"):
            continue
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            tabs = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            con.close()
        except Exception:
            continue
        if "receipts" in tabs and "transactions" in tabs:
            out.append(path)
    return out

def hx(v):
    return int(v, 16) if isinstance(v, str) and v.startswith("0x") else int(v)

def process(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    txmeta = {}
    for h, raw in con.execute("SELECT hash, raw_tx FROM transactions"):
        try:
            t = json.loads(raw)
            txmeta[h.lower()] = (t.get("to"), hx(t.get("value", "0x0")))
        except Exception:
            pass
    shapes, emitters, samples, flows = {}, {}, {}, {}
    seen_sample_tx = {}
    n_logs = 0
    for tx_hash, rjson in con.execute("SELECT tx_hash, receipt_json FROM receipts"):
        try:
            receipt = json.loads(rjson)
        except Exception:
            continue
        logs = receipt.get("logs", [])
        txl = tx_hash.lower()
        by_topic = {}
        for log in logs:
            topics = log.get("topics") or []
            if not topics:
                continue
            t0 = topics[0].lower()
            by_topic.setdefault(t0, []).append(log)
        for t0, tlogs in by_topic.items():
            n_logs += len(tlogs)
            sh = shapes.setdefault(t0, {})
            em = emitters.setdefault(t0, {})
            for log in tlogs:
                topics = log.get("topics") or []
                data = log.get("data") or "0x"
                key = f"{len(topics)}:{(len(data) - 2) // 64}"
                sh[key] = sh.get(key, 0) + 1
                addr = (log.get("address") or "").lower()
                blk = hx(log.get("blockNumber", "0x0"))
                e = em.get(addr)
                if e is None:
                    em[addr] = [1, blk, blk]
                else:
                    e[0] += 1
                    e[1] = min(e[1], blk)
                    e[2] = max(e[2], blk)
            ss = seen_sample_tx.setdefault(t0, set())
            if txl not in ss and len(ss) < MAX_SAMPLES:
                ss.add(txl)
                blk = hx(tlogs[0].get("blockNumber", "0x0"))
                samples.setdefault(t0, []).append([txl, blk])
                emitter = (tlogs[0].get("address") or "").lower()
                tin = tout = 0
                for log in logs:
                    lt = log.get("topics") or []
                    if lt and lt[0].lower() == TRANSFER and len(lt) >= 3:
                        frm = "0x" + lt[1][-40:]
                        to = "0x" + lt[2][-40:]
                        if to == emitter:
                            tin += 1
                        if frm == emitter:
                            tout += 1
                to_addr, value = txmeta.get(txl, (None, 0))
                native_in = 1 if (to_addr and to_addr.lower() == emitter
                                   and value > 0) else 0
                wrap = 1 if any(
                    (l.get("topics") or [None])[0] and
                    l["topics"][0].lower() in (WETH_DEPOSIT, WETH_WITHDRAWAL)
                    and (l.get("address") or "").lower() == WETH
                    for l in logs) else 0
                direction = ("both" if tin and tout else
                             "in" if tin else "out" if tout else "none")
                flows.setdefault(t0, []).append({
                    "tx": txl, "direction": direction,
                    "transfers_in": tin, "transfers_out": tout,
                    "has_native_in": native_in, "has_weth_wrap": wrap})
    con.close()
    return {"chunk": os.path.basename(path), "logs": n_logs,
            "shapes": shapes, "emitters": emitters,
            "samples": samples, "flows": flows}

def main():
    input_dir, out_dir = sys.argv[1], sys.argv[2]
    os.makedirs(out_dir, exist_ok=True)
    chunks = find_raw_chunks(input_dir)
    print(f"raw chunk DBs in {input_dir}: {len(chunks)}", flush=True)
    for path in chunks:
        name = os.path.basename(path).replace(".db", ".json")
        out = os.path.join(out_dir, name)
        if os.path.exists(out):
            print("skip", name, flush=True)
            continue
        part = process(path)
        tmp = out + ".tmp"
        json.dump(part, open(tmp, "w"))
        os.replace(tmp, out)
        print(f"done {name}: {part['logs']} logs, "
              f"{len(part['shapes'])} topics", flush=True)
    print("CHUNKS EXTRACTED")

if __name__ == "__main__":
    main()
