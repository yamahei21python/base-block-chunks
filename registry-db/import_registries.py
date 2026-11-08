#!/usr/bin/env python3
"""Build registry.db (R1 schema) and import the current 4 registries.

Sources (the newest verified state of each book):
  topics    BBC known_topics v2026-11-05.29 (7,559)
  pools     defi known_pools v2026-11-08.2 (6,328 pools / 5,565 decimals,
            the PR #62 output verified at acceptance)
  contracts BBC known_contracts v2026-11-05.24 (293)

Import semantics (DESIGN.md section 6):
  - topics become verdicts by rule:registry_import + an import review,
    so the authoritative view reproduces the current accepted state.
  - signature-like names are keccak-checked at import (ethers via node);
    the result lands in topic_signature.keccak_matches (NULL = not a
    signature-shaped name, 0 = mismatch, 1 = match).
  - pool pairs become assertions with source_kind='registry_import',
    priority=30. Nothing is silently promoted above that.
"""
import json, re, sqlite3, subprocess, sys, os

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "registry.db")
SCHEMA = os.path.join(HERE, "schema.sql")
TOPICS = os.path.expanduser("~/workspace/loop-scratch/integration/registries/known_topics.json")
CONTRACTS = os.path.expanduser("~/workspace/loop-scratch/integration/registries/known_contracts.json")
POOLS = os.path.expanduser("~/workspace/loop-scratch/pr62-accept/pools_fix.json")
ETHERS = os.path.expanduser("~/workspace/wallets/test-wallet-01/node_modules/ethers")
CHAIN = 8453
ZERO = "0x" + "00" * 20

def b(hexstr):
    return bytes.fromhex(hexstr[2:])

def keccak_map(signatures):
    """Return {signature: topic0_hex} using ethers keccak256 (batch via node)."""
    script = (
        "const {keccak256, toUtf8Bytes} = require(process.argv[1]);"
        "const sigs = JSON.parse(require('fs').readFileSync(process.argv[2], 'utf8'));"
        "const out = {};"
        "for (const s of sigs) { try { out[s] = keccak256(toUtf8Bytes(s)); } catch (e) { out[s] = null; } }"
        "console.log(JSON.stringify(out));"
    )
    sigfile = os.path.join(HERE, "_sigs.json")
    json.dump(sorted(signatures), open(sigfile, "w"))
    r = subprocess.run(["node", "-e", script, ETHERS, sigfile],
                       capture_output=True, text=True)
    os.remove(sigfile)
    if r.returncode != 0:
        raise RuntimeError("node keccak failed: " + r.stderr[:500])
    return json.loads(r.stdout)

def main():
    if os.path.exists(DB):
        os.remove(DB)
    con = sqlite3.connect(DB)
    con.executescript(open(SCHEMA).read())
    con.execute("PRAGMA foreign_keys = ON")
    cur = con.cursor()
    cur.execute(
        "INSERT INTO run(kind, source, started_at, status, meta_json) VALUES"
        " ('ingest','registry-import:2026-11-08', datetime('now'), 'running', ?)",
        (json.dumps({"topics": "v2026-11-05.29", "pools": "v2026-11-08.2",
                     "contracts": "v2026-11-05.24"}),))
    run_id = cur.lastrowid
    cur.execute(
        "INSERT INTO registry_release(label, created_at, run_id, note) VALUES"
        " ('2026-11-08.3', datetime('now'), ?, 'initial import of the 4 registries')",
        (run_id,))
    release_id = cur.lastrowid

    # ---------------- topics ----------------
    tdoc = json.load(open(TOPICS))
    topics = tdoc["topics"]
    sig_re = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\(.*\)$")
    sig_names = {v.get("name") for v in topics.values()
                 if v.get("name") and sig_re.match(v["name"])}
    sig_names |= {v["signature"] for v in topics.values() if v.get("signature")}
    kmap = keccak_map(sig_names)
    n_sig_match = n_sig_mismatch = 0
    for t0, info in topics.items():
        cur.execute(
            "INSERT INTO topic(topic0) VALUES (?)", (b(t0),))
        cur.execute(
            "INSERT INTO topic_verdict(topic0, class, basis, decided_by, note,"
            " basis_kind) VALUES (?, ?, ?, 'rule:registry_import', ?, 'imported')",
            (b(t0), info["class"],
             "registry import from known_topics v2026-11-05.29 (state as "
             "integrated and accepted in the loop cycles)",
             info.get("note")))
        vid = cur.lastrowid
        cur.execute(
            "INSERT INTO topic_review(topic0, verdict_id, status, reviewer,"
            " reviewed_at, basis, release_id) VALUES"
            " (?, ?, 'approved', 'registry-import', datetime('now'), ?, ?)",
            (b(t0), vid,
             "imported as previously accepted state; not a fresh review",
             release_id))
        fsig = info.get("signature")
        if fsig:
            fmatch = None
            fc = kmap.get(fsig)
            if fc is not None:
                fmatch = 1 if fc.lower() == t0.lower() else 0
            cur.execute(
                "INSERT INTO topic_signature(topic0, signature, source,"
                " keccak_matches, run_id) VALUES (?, ?, 'manual', ?, ?)",
                (b(t0), fsig, fmatch, run_id))
        name = info.get("name")
        if name and sig_re.match(name):
            computed = kmap.get(name)
            match = None
            if computed is not None:
                match = 1 if computed.lower() == t0.lower() else 0
                if match:
                    n_sig_match += 1
                else:
                    n_sig_mismatch += 1
            cur.execute(
                "INSERT INTO topic_signature(topic0, signature, source,"
                " keccak_matches, run_id) VALUES (?, ?, 'manual', ?, ?)",
                (b(t0), name, match, run_id))
    print(f"topics: {len(topics)} imported; signatures checked: "
          f"{n_sig_match} match, {n_sig_mismatch} mismatch")

    # ---------------- pools + tokens ----------------
    pdoc = json.load(open(POOLS))
    pools, decimals = pdoc["pools"], pdoc.get("decimals", {})
    seen_tokens = {}
    n_pools = 0
    for key, pair in pools.items():
        idpart = key.split(":", 1)[1] if ":" in key else key
        if len(idpart) == 66:
            cur.execute(
                "INSERT INTO pool(chain_id, pool_id, kind, status) VALUES"
                " (?, ?, 'v4', 'verified')", (CHAIN, b(idpart)))
        else:
            cur.execute(
                "INSERT INTO pool(chain_id, address, kind, status) VALUES"
                " (?, ?, 'other', 'verified')", (CHAIN, b(idpart)))
        pid = cur.lastrowid
        t0, t1 = pair[0].lower(), pair[1].lower()
        cur.execute(
            "INSERT INTO pool_pair_assertion(pool_id, token0, token1,"
            " source_kind, priority, source_ref, run_id) VALUES"
            " (?, ?, ?, 'registry_import', 30, ?, ?)",
            (pid, b(t0), b(t1), f"known_pools v2026-11-08.2 key={key}", run_id))
        seen_tokens.setdefault(t0); seen_tokens.setdefault(t1)
        n_pools += 1
    for addr, dec in decimals.items():
        seen_tokens[addr.lower()] = dec
    n_dec = 0
    for addr, dec in seen_tokens.items():
        if addr == ZERO:
            cur.execute(
                "INSERT INTO token(chain_id, address, is_native, decimals,"
                " decimals_source, symbol, symbol_source) VALUES"
                " (?, ?, 1, 18, 'manual', 'ETH', 'manual')",
                (CHAIN, b(addr)))
            continue
        if dec is not None:
            n_dec += 1
        cur.execute(
            "INSERT INTO token(chain_id, address, decimals, decimals_source)"
            " VALUES (?, ?, ?, ?)",
            (CHAIN, b(addr), dec, "registry" if dec is not None else None))
    print(f"pools: {n_pools} imported; tokens: {len(seen_tokens)} "
          f"({n_dec} with decimals)")

    # ---------------- contracts ----------------
    cdoc = json.load(open(CONTRACTS))
    contracts = cdoc["contracts"]
    role_map = {"emitter": "emitter", "router": "router",
                "aggregator": "aggregator", "pool_manager": "pool_manager",
                "vault": "vault", "token": "token"}
    for addr, info in contracts.items():
        note = (f"protocol: {info.get('protocol')}; "
                f"role: {info.get('role')}; source: {info.get('source')}")
        cur.execute(
            "INSERT INTO contract(chain_id, address, name, note) VALUES"
            " (?, ?, ?, ?)", (CHAIN, b(addr), info.get("name"), note))
        cur.execute(
            "INSERT INTO contract_role(chain_id, address, role) VALUES"
            " (?, ?, ?)",
            (CHAIN, b(addr), role_map.get(info.get("role"), "other")))
    print(f"contracts: {len(contracts)} imported")

    cur.execute("UPDATE run SET status='ok', finished_at=datetime('now'),"
                " rows_written=? WHERE id=?",
                (len(topics) + n_pools + len(contracts), run_id))
    con.commit()

    # ---------------- verification ----------------
    fk = cur.execute("PRAGMA foreign_key_check").fetchall()
    print("foreign_key_check violations:", len(fk))
    for label, q in [
        ("topics", "SELECT count(*) FROM topic"),
        ("verdicts", "SELECT count(*) FROM topic_verdict"),
        ("authoritative", "SELECT count(*) FROM v_authoritative_topic"),
        ("pools", "SELECT count(*) FROM pool"),
        ("assertions", "SELECT count(*) FROM pool_pair_assertion"),
        ("canonical_conflicted",
         "SELECT count(*) FROM v_pool_canonical WHERE conflicted=1"),
        ("tokens", "SELECT count(*) FROM token"),
        ("contracts", "SELECT count(*) FROM contract"),
    ]:
        print(f"  {label}: {cur.execute(q).fetchone()[0]}")
    usdc = cur.execute("SELECT decimals FROM token WHERE address=?",
                       (b("0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"),)).fetchone()
    weth = cur.execute("SELECT decimals FROM token WHERE address=?",
                       (b("0x4200000000000000000000000000000000000006"),)).fetchone()
    print("  USDC decimals:", usdc, "| WETH decimals:", weth)
    con.close()
    print("DB built:", DB, f"({os.path.getsize(DB)/1e6:.1f} MB)")

if __name__ == "__main__":
    main()
