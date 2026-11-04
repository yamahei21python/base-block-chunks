#!/usr/bin/env python3
"""Resolve token identities (symbol / name) for every token the pool
registry knows, into known_tokens.json.

Mechanical getter facts only (symbol() 0x95d89b41, name() 0x06fdde03);
tokens whose calls fail or return nothing are left out and retried on
the next run. Some tokens return bytes32 instead of a string — both
forms are decoded. Usage: python3 resolve_tokens.py [known_pools.json]
"""
import json, os, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

RPC_URLS = ["https://base-rpc.publicnode.com", "https://mainnet.base.org"]
UA = {"User-Agent": "base-block-chunks/0.4", "Content-Type": "application/json"}
HERE = os.path.dirname(os.path.abspath(__file__))


def eth_call(to, data):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                       "params": [{"to": to, "data": data}, "latest"]}).encode()
    last = None
    start = hash(to) % len(RPC_URLS)
    for k in range(4):
        try:
            req = urllib.request.Request(RPC_URLS[(start + k) % len(RPC_URLS)],
                                         data=body, headers=UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                out = json.load(r)
            if out.get("result") not in (None, "0x"):
                return out["result"]
            last = out
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(0.5)
    raise RuntimeError(f"eth_call failed for {to}: {last}")


def decode_string(result):
    h = result[2:]
    if len(h) == 64:  # bytes32 form
        raw = bytes.fromhex(h).rstrip(b"\x00")
        return raw.decode("utf-8", "replace") or None
    try:
        off = int(h[:64], 16) * 2
        ln = int(h[off:off + 64], 16) * 2
        return bytes.fromhex(h[off + 64:off + 64 + ln]).decode("utf-8", "replace") or None
    except Exception:  # noqa: BLE001
        return None


def resolve(addr):
    out = {}
    for key, sel in (("symbol", "0x95d89b41"), ("name", "0x06fdde03")):
        try:
            v = decode_string(eth_call(addr, sel))
            if v:
                out[key] = v
        except Exception:  # noqa: BLE001 - leave for the next run
            pass
    return addr, out


def main():
    pools_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "known_pools.json")
    tokens_path = os.path.join(HERE, "known_tokens.json")
    pools = json.load(open(pools_path))
    wanted = set(pools.get("decimals", {}))
    for t0, t1 in pools.get("pools", {}).values():
        wanted.update((t0, t1))
    wanted.discard("0x0000000000000000000000000000000000000000")
    reg = {"version": "dev", "tokens": {}}
    if os.path.exists(tokens_path):
        reg = json.load(open(tokens_path))
    have = reg.setdefault("tokens", {})
    todo = sorted(a for a in wanted if a not in have)
    print(f"tokens wanted={len(wanted)} have={len(have)} todo={len(todo)}", flush=True)
    with ThreadPoolExecutor(max_workers=8) as ex:
        for addr, out in ex.map(resolve, todo):
            if out.get("symbol") or out.get("name"):
                have[addr] = out
    reg["tokens"] = have
    with open(tokens_path, "w") as f:
        json.dump(reg, f, indent=1, ensure_ascii=False)
    print(f"known_tokens: {len(have)} entries -> {tokens_path}")


if __name__ == "__main__":
    main()
