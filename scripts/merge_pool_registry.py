#!/usr/bin/env python3
"""Fold pool/token metadata out of decoded ledgers into known_pools.json.

Ledgers produced anywhere (local runs, GitHub Actions decode runs)
carry pool_meta / token_meta tables. This tool merges them back into
the shared registry so resolutions compound instead of being stranded
per-ledger:

- Decimals merge directly (universal per-token facts).
- Pairs merge ONLY with a reconstructed resolver key. A pool's
  resolver follows from the kinds of legs it produced in that ledger:
    v2 / aerodrome / v3 / pancake-v3 / algebra -> pair01
    metric -> immutables, maverick -> tokenab, forte -> xy
  Pools whose kinds map to no getter resolver (singleton, pmm,
  lunarbase — flow/topic-derived pairs, registry triage class B) are
  NEVER merged from pool_meta, and neither are pools with no legs.
- Existing registry entries win; disagreements are reported as
  conflicts, never overwritten silently.
- Addresses listed in known_non_pools.json (audited as NOT pools)
  are never merged as pools, whatever resolver their legs imply;
  each skip is reported.

Usage:
  python3 merge_pool_registry.py LEDGER.db [LEDGER2.db ...] [--dry-run]
"""
import json, os, sqlite3, sys

HERE = os.path.dirname(os.path.abspath(__file__))
KIND_RESOLVER = {
    "v2": "pair01", "aerodrome": "pair01", "v3": "pair01",
    "pancake-v3": "pair01", "algebra": "pair01",
    "metric": "immutables", "maverick": "tokenab", "forte": "xy",
}
DENY_PATH = os.path.join(HERE, "known_non_pools.json")


def load_denylist():
    """Addresses audited as NOT pools (known_non_pools.json)."""
    if not os.path.exists(DENY_PATH):
        return set()
    doc = json.load(open(DENY_PATH))
    return {a.lower() for a in doc.get("non_pools", {})}


def expand(paths):
    out = []
    for p in paths:
        if os.path.isdir(p):
            out += [os.path.join(p, f) for f in sorted(os.listdir(p))
                    if f.endswith(".db")]
        else:
            out.append(p)
    return out


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry-run" in sys.argv
    reg_path = os.path.join(HERE, "known_pools.json")
    reg = json.load(open(reg_path))
    pools, dec = reg.setdefault("pools", {}), reg.setdefault("decimals", {})
    denylist = load_denylist()
    added_p = added_d = skipped = skipped_np = 0
    conflicts = []
    denied = []
    for path in expand(args):
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "pool_meta" not in tables:
            print(f"{path}: no pool_meta, skipped")
            continue
        kinds = {}
        for pool, kind in con.execute("SELECT DISTINCT pool, kind FROM legs"):
            kinds.setdefault(pool, set()).add(kind)
        for addr, t0, t1 in con.execute(
                "SELECT address, token0, token1 FROM pool_meta"):
            if addr.lower() in denylist:
                skipped_np += 1
                denied.append((addr, path))
                continue
            resolvers = {KIND_RESOLVER[k] for k in kinds.get(addr, set())
                         if k in KIND_RESOLVER}
            if len(resolvers) != 1:
                skipped += 1
                continue
            key = f"{next(iter(resolvers))}:{addr}"
            cur = pools.get(key)
            if cur is None:
                pools[key] = [t0, t1]
                added_p += 1
            elif list(cur) != [t0, t1]:
                conflicts.append((key, cur, [t0, t1], path))
        for addr, d in con.execute(
                "SELECT address, decimals FROM token_meta"):
            if d is None:
                continue
            if addr not in dec:
                dec[addr] = d
                added_d += 1
            elif dec[addr] != d:
                conflicts.append((f"decimals:{addr}", dec[addr], d, path))
        con.close()
    print(f"pairs +{added_p} (registry now {len(pools)}), "
          f"decimals +{added_d} (now {len(dec)}), "
          f"pool_meta rows skipped (no single getter resolver): {skipped}, "
          f"pool_meta rows skipped (known non-pool): {skipped_np}")
    for d in denied[:20]:
        print("NON-POOL SKIPPED:", d)
    for c in conflicts[:20]:
        print("CONFLICT:", c)
    if conflicts:
        print(f"conflicts total: {len(conflicts)} (registry kept, not overwritten)")
    if not dry:
        with open(reg_path, "w") as f:
            json.dump(reg, f, indent=1)
        print("registry written:", reg_path)
    else:
        print("dry run: registry not written")


if __name__ == "__main__":
    main()
