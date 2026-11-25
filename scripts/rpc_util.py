"""Shared public-RPC helpers for the state/price capture scripts.

Block states older than the last ~128 blocks are archive requests: publicnode
rejects them without a token, so fall back to the public BlastAPI/Base
endpoints and remember the working URL for the rest of the process.
No credentials are read or emitted.
"""
import json
import time
import urllib.request

PUBLICNODE = "https://base-rpc.publicnode.com"
BLASTAPI = "https://base-mainnet.public.blastapi.io"
BASE_ORG = "https://mainnet.base.org"
ARCHIVE_CUTOFF = 51_000_000
BATCH = 25
_UA = {"User-Agent": "base-block-chunks/0.5", "Content-Type": "application/json"}
_ACTIVE = {"url": None}


def urls_for(block):
    # publicnode serves recent state only; archive reads need a personal token.
    # Chunk captures are historical, so BlastAPI/Base.org come first.
    if block >= ARCHIVE_CUTOFF:
        return [BLASTAPI, BASE_ORG, PUBLICNODE]
    return [BLASTAPI, BASE_ORG, PUBLICNODE]


def _is_archive_error(text):
    low = str(text).lower()
    return "archive" in low and ("token" in low or "request" in low)


def _is_permanent_error(err):
    """Reverts and bad params (v4 pool ids are 32-byte, not addresses)."""
    if isinstance(err, dict):
        if err.get("code") in (3, -32602):
            return True
        low = str(err.get("message", "")).lower()
    else:
        low = str(err).lower()
    return "revert" in low or "invalid params" in low


def _post(url, payload):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=_UA, method="POST")
    with urllib.request.urlopen(req, timeout=45) as fh:
        return json.load(fh)


def rpc_batch(calls, block):
    """calls: list of (method, params). Returns list of response dicts by index."""
    urls = urls_for(block)
    if _ACTIVE["url"]:
        urls = [_ACTIVE["url"]] + [u for u in urls if u != _ACTIVE["url"]]
    out = [None] * len(calls)
    last = None
    for attempt in range(5):
        for url in urls:
            for start in range(0, len(calls), BATCH):
                chunk = calls[start:start + BATCH]
                payload = [{"jsonrpc": "2.0", "id": start + i, "method": m, "params": p}
                           for i, (m, p) in enumerate(chunk)]
                try:
                    res = _post(url, payload)
                except Exception as exc:  # endpoint down / HTTP error
                    last = exc
                    break
                by_id = {r.get("id"): r for r in res if isinstance(r, dict)}
                errs = [r["error"] for r in by_id.values() if "error" in r]
                if any(_is_archive_error(e) for e in errs):
                    last = RuntimeError("archive state unavailable at " + url)
                    if _ACTIVE["url"] == url:
                        _ACTIVE["url"] = None
                    break
                # eth_call reverts / v4 32-byte pool ids are expected, not retryable.
                if errs and not all(_is_permanent_error(e) for e in errs):
                    last = RuntimeError(f"RPC error at {url}: {errs[0]}")
                    _ACTIVE["url"] = None  # do not reuse an endpoint that is failing
                    time.sleep(3 + attempt * 5)
                    break
                for i in range(len(chunk)):
                    out[start + i] = by_id.get(start + i, {"error": "missing response"})
                _ACTIVE["url"] = url
                time.sleep(0.4)  # gentle pacing between batches (4 jobs in parallel)
                continue
            else:
                if all(r is not None for r in out):
                    return out
                continue
            continue  # try the next endpoint after a failed batch
        time.sleep(2 + attempt * 4)
    raise RuntimeError(f"RPC batch failed for block {block}: {last}")


def result_of(resp):
    if not isinstance(resp, dict) or "error" in resp:
        return None
    return resp.get("result")
