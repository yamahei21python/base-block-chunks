#!/usr/bin/env python3
"""Decode a raw chunk DB (fetch_chunk.py output) into a ledger DB.

Mechanical step only: receipt logs -> swap legs across all verified
formats, with native token amounts, plus per-tx gas (used x price,
both native). Token/pool metadata comes from public RPC eth_call,
prefetched concurrently and cached in the ledger; singleton formats
(Uniswap V4, PancakeSwap Infinity) and LunarBase resolve their token
pair from the same receipt's Transfer flows instead (Initialize-event
lookup does not work within public-endpoint log-range limits).

No pricing happens here by design: the ledger stores native amounts as
the source of truth, and USD conversion is a separate downstream step
on the consumer side, where the price source is fixed and controlled.

Format notes (all specs verified against same-receipt Transfers):
- v2 / aerodrome: unsigned In/Out words; amounts are swapper-side
  gross (Aerodrome pools additionally siphon a fee to a varying fee
  recipient; the leg amounts are the event's own, not the pool net).
- v3 / pancake-v3 / algebra: signed amount0/amount1, pool perspective
  (positive = pool received). Algebra pools occasionally flush a
  community fee in the same tx; event amounts are unaffected.
- metric: data word1 packs two int128 deltas (upper amount0, lower
  amount1), pool perspective; pair via getImmutables() words 1,2.
- forte: explicit tokenIn (word0), amountIn (word1), amountOut
  (word2); pair via xToken()/yToken().
- maverick: tokenAIn is data word3 (bool), amountIn word6, amountOut
  word7; pair via tokenA()/tokenB(). (Indices commissioner-corrected.)
- v4 / pancake-infinity: singleton; signed int128 amount0/amount1 in
  SWAPPER perspective (negative = paid in). Currencies resolved by
  matching the custodian's (PoolManager / Vault) same-receipt token
  flows; an unmatched side is native ETH.
- lunarbase: xToY word1, dx word2, dy word3 on the PropAMM proxy;
  the ERC-20 side is resolved from same-receipt flows, the other
  side is native ETH.

Usage: decode_chunk.py <chunk.db> <ledger.db>
Writes <ledger.db>.manifest.json next to the ledger.
"""
import json
import os
import sqlite3
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

RPC_URLS = ["https://base-rpc.publicnode.com", "https://mainnet.base.org"]
UA = {"User-Agent": "base-block-chunks/0.4", "Content-Type": "application/json"}

# Pool registry (known_pools.json, sibling of known_topics.json): pool
# token pairs and token decimals are immutable on-chain facts, so they
# are kept in a versioned registry instead of being re-asked from the
# RPC for every chunk. Pairs are keyed "resolver:pool" (a pair is only
# reused for the resolver that produced it). The registry is advisory —
# any failure to read or write it falls back to RPC.
SHARED_CACHE = os.environ.get(
    "DECODE_SHARED_CACHE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "known_pools.json"))


def load_shared_cache():
    try:
        with open(SHARED_CACHE) as f:
            d = json.load(f)
        return ({a: (t[0], t[1]) for a, t in d.get("pools", {}).items()},
                dict(d.get("decimals", {})))
    except Exception:  # noqa: BLE001 - cache is optional
        return {}, {}


def save_shared_cache(pools, decimals):
    try:
        version = "dev"
        try:
            with open(SHARED_CACHE) as f:
                version = json.load(f).get("version", "dev")
        except Exception:  # noqa: BLE001
            pass
        tmp = SHARED_CACHE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"version": version,
                       "pools": {a: list(t) for a, t in pools.items()},
                       "decimals": {a: d for a, d in decimals.items()
                                    if d is not None}}, f)
        os.replace(tmp, SHARED_CACHE)
    except Exception:  # noqa: BLE001 - registry is optional
        pass

ETH0 = "0x0000000000000000000000000000000000000000"
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
PM_V4 = "0x498581ff718922c3f8e6a244956af099b2652b2b"
VAULT_INFINITY = "0x238a358808379702088667322f80ac48bad5e6c4"

# topic -> (kind, family)
FORMATS = {
    "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822": ("v2", "v2style"),
    "0xb3e2773606abfd36b5bd91394b3a54d1398336c65005baf7bf7a05efeffaf75b": ("aerodrome", "v2style"),
    "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67": ("v3", "v3style"),
    "0x19b47279256b2a23a1665c810c8d55a1758940ee09377d4f8d26497a3577dc83": ("pancake-v3", "v3style"),
    "0x121cb44ee54098b1a04743c487e7460d8dd429b27f88b1f4d4767396e1a59f79": ("algebra", "v3style"),
    "0x9734819749a91fc3be03ea83205f924ee08479bd3f0da48efc91d94d050cac1e": ("metric", "metric"),
    "0xfcfa612587d1091cc6d8ce39878a7e6e8742c856865fc9d64db3e1a2f1ab52ef": ("forte", "forte"),
    "0x103ed084e94a44c8f5f6ba8e3011507c41063177e29949083c439777d8d63f60": ("maverick", "maverick"),
    "0x1b43ddf90e971181a7faf41549e512675072e84befadbba7873086509dec1fdc": ("lunarbase", "lunarbase"),
    "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f": ("v4", "singleton"),
    "0x04206ad2b7c0f463bff3dd4f33c5735b0f2957a351e4f79763a4fa9e775dd237": ("pancake-infinity", "singleton"),
    "0x858ca532ad00ebb4d1c8c4fa77d87a5a8f4955e607f96bce1b600eed67d4c2b0": ("pmm", "pmm"),
}


# --- 最小アダプター追加（87形式のうち71形式。ADAPTER_SPEC.md 実装規則1-5）---
# 68 形式はサンプル実測で確定した読み規則（ADAPTER_READERS）で、
# 3 形式は既存 family への読み位置一致で追加（規則1）。残り16形式は
# サンプルから意味を確定できず未実装（NOTES.md の「未実装一覧」）。

FORMATS.update({
    "0xe0c1b141a85ba7f8df7ded19a02a004b049d9e0a67f9dea87186c07bce3923b0": ('Trade', "adapter"),
    "0x853c8e677490312983a1757c837355487031d90920d260d439269c024de030c2": ('Swapped', "adapter"),
    "0x9ec2cbdc7c510df38f982d15dfab2db2ba557c382afbec908ed154dfb83b1fcb": ('Swapped', "adapter"),
    "0x5a417e3ceaff68360f7300df6686a9d417ae12328724de7bc2fd6c012a2ffa85": ('Swapped', "adapter"),
    "0xee4825a7988c5519a874be79f2a84d13d02cfe5f74cad97cda2d5b55d2b13bfa": ('SwapExecuted', "adapter"),
    "0x77f92a1b6a1a11de8ca49515ad4c1fad45632dd3442167d74b90b304a3c7a758": ('Swap', "adapter"),
    "0x4aa9c8b0c9a939db1d9261ba132e6ad5bc8390e89cb6ac192c10f4ca67f49e40": ('Swapped', "adapter"),
    "0xc9b356cd595a52e855855ac43eae98058d001ef4f0895fc098509ad2f2fe7cad": ('Traded', "adapter"),
    "0xee524511e0afdb415a78a457a9fb0be80c11f2d7d2bbec13af282afd933923f9": ('Bought', "adapter"),
    "0x6db63bebf1e6540277744df32846ebdb98385b1a73f2d5de49b28348add63f50": ('TokensSold', "adapter"),
    "0x2ad8739d64c070ab4ae9d9c0743d56550b22c3c8c96e7a6045fac37b5b8e89e3": ('Swap', "adapter"),
    "0x2afb56853a8b76a0868e64c0001eedd84d9a7f5bc7fb788aba6180e422ba6ec5": ('Swap', "adapter"),
    "0x0a17ff6a9995a1332a432b8bf852005142714ccfe1f1ccda72a3aba5e9ed408f": (None, "adapter"),
    "0x22aea6cd253f249ce9b7d0a1af21a0eca8bbe111c26bcc705eece987ad4d2105": (None, "adapter"),
    "0x4fd47c7108c4f49e2398c5159bbd183546a2fca795309705e629fed588852416": (None, "adapter"),
    "0xdde2f3711ab09cdddcfee16ca03e54d21fb8cf3fa647b9797913c950d38ad693": ('SwapExecuted', "adapter"),
    "0x0e8e403c2d36126272b08c75823e988381d9dc47f2f0a9a080d95f891d95c469": ('WooSwap', "adapter"),
    "0x8b3e96f2b889fa771c53c981b40daf005f63f637f1869f707052d15a3dd97140": ('TokenExchange', "adapter"),
    "0x3b841dc9ab51e3104bda4f61b41e4271192d22cd19da5ee6e292dc8e2744f713": ('Swap', "adapter"),
    "0x143f1f8e861fbdeddd5b46e844b7d3ac7b86a122f36e8c463859ee6811b1f29c": ('TokenExchange', "adapter"),
    "0x2170c741c41531aec20e7c107c24eecfdd15e69c9bb0a8dd37b1840b9e0b207b": ('Swap', "adapter"),
    "0x88b012650d7da64b7697c175b1dc42bd7dd09d8a2ef28c62cbb448086f548ce6": ('Swapped', "adapter"),
    "0x0874b2d545cb271cdbda4e093020c452328b24af12382ed62c4d00f5c26709db": ('Swap', "adapter"),
    "0xc2c0245e056d5fb095f04cd6373bc770802ebd1e6c918eb78fdef843cdb37b0f": ('DODOSwap', "adapter"),
    "0xe2cee3f6836059820b673943853afebd9b3026125dab0d774284e6f28a4855be": ('Swapped', "adapter"),
    "0xbfca96f8fea048c07457ab90159d04dfbef79bc43270596da55ae633213585e4": ('Swapped', "adapter"),
    "0xb159dc503a6f507638f48f62f81cab93f1e806b7476ac426ea5f5793ddbf9127": ('Swap', "adapter"),
    "0xb2e76ae99761dc136e598d4a629bb347eccb9532a5f8bbd72e18467c3c34cc98": ('TokenExchange', "adapter"),
    "0xc4cc441884c8b8ce500de14f1f490522149ac5ec673710ca5c792c38ec736847": ('Swap', "adapter"),
    "0xb39c9bc43f811e1a7ce159c5f147458fdb80266bf23c17322013316e27e086d0": ('Swap', "adapter"),
    "0xfe5d070a8c6c1bf4a4675905e56f14d922474c8fead10febb0d9c963a2c2d0e7": ('Unknown', "adapter"),
    "0xa54201e99d8e1488291e015a3af88c7a169bcf4932617ecd9536e1f1ad435110": ('Swapped', "adapter"),
    "0xca1985de4499b70bca89ad1f3711d7081a4bfefe76dfbdf83beecf1336fb85d8": ('Swapped', "adapter"),
    "0x086c7fcd1952b421cc49f207e360cef93dead3f9caa1d6eb4b0e51bb58367738": ('StableSwap', "adapter"),
    "0x546b1f988790578dc4043a3d0e3640574a568eb04e498b08ef98518eee574882": ('WEDXswapEvent', "adapter"),
    "0xb8a2a375cd0661da64ed8ab72ba981ae5e21e5d484fb6eaa8e5236ac2111d13c": ('Swapped', "adapter"),
    "0xac98876c63e9e95442ed72d8ca81d9a5c922f18616dc2b8b155416310b898dd9": ('Swap', "adapter"),
    "0x2fb0748023df976464b18b5e5d1c2f8d79da559ffeb202c8a2ce83737a946906": ('MultiSwap', "adapter"),
    "0xa62ddba0018cb74df710cd81eb65dbeb85eb16937bc4967591c36b169d8100c6": ('SwapDone', "adapter"),
    "0xdb587d878116df0bdd4fe154699aa2c5f439da001cc811dfd05d9f589fc5a8ee": ('Swapped', "adapter"),
    "0xc82975a4eae9f14416813a0bd7312edf547928cd25ae9b8597ee4c92fa6862d2": ('RFQTrade', "adapter"),
    "0x27c98e911efdd224f4002f6cd831c3ad0d2759ee176f9ee8466d95826af22a1c": ('WooRouterSwap', "adapter"),
    "0x2e65e08f215f8172b6bc6ae6c1191f00c014355c9d39c03627de921283bd3ba2": ('Unknown', "adapter"),
    "0x3993372c7f54dee1879dcc58987731c2857a033eccc34111f4d2efb1b045d947": ('Swap', "adapter"),
    "0xdd36740e2a012d93061a0d99eaa9107860955de4e90027d3cf465a055026c407": ('SwapExecuted', "adapter"),
    "0xa3f37f0446dcac60c3831cc0ebda32d57d7cddb7bef6d734cdd7b0c72c277922": ('SwapExecuted', "adapter"),
    "0xa078c4190abe07940190effc1846be0ccf03ad6007bc9e93f9697d0b460befbb": ('Swapped', "adapter"),
    "0x25f1d03755df23c30e25db2dbd3891e31ce084bdfbfc46f9fe5e446ee5f9b2d4": ('TokensSwapped', "adapter"),
    "0x6621486d9c28838df4a87d2cca5007bc2aaf6a5b5de083b1db8faf709302c473": ('OrderFilled', "adapter"),
    "0xbbc79dc7d11fb8ae8de963e009943172807faa4a90c3620ef772f4783e76355f": ('Sell', "adapter"),
    "0x5380cf97d8f645d3c4896da60c053458dca03a3a31cec642ac80e1ddf0d8d02a": ('Swap', "adapter"),
    "0xe4c8d224f59766afb8ace8df8ee71f9f0e26664f915e4045a3df5296a991ad50": ('TideFiSwap', "adapter"),
    "0x6b17016c67d28f4cc3a653d895e3b1db09320d6852a46f95aa225059190f7dcd": ('TokensSold', "adapter"),
    "0x673ff070bec3aea00f2fa45c0f7ed58c227af4a03a626e01fe29b34d3b9ecfdb": ('Swap', "adapter"),
    "0x1560b71ca6695825842f12e8721dc7f437286c08deb7c7ebc708bb3bdc3d01c5": ('StockBought', "adapter"),
    "0x7a7e1d68c6a73a4841f6e57bf2d968148c641201d165b651b1fd3a43f270991b": ('Swapped', "adapter"),
    "0x9c45db89e530035bcb2e1d6a8c5526097f83283b8c1938f69ee5ffff9ae133ca": (None, "adapter"),
    "0x0a3279173ae79e514355394a631c9ff226a83934a08c0703301aaccd3a8afa70": (None, "adapter"),
    "0x2251435bd151cd72851a82be055bf6d1c3d7f34d08d56493dddf874229b8e897": ('TransitSwapped', "adapter"),
    "0x6bef5eec9bc0ce6168c21a5cfb879d8b7dd72ef58d698042cd16112a52a7daa3": (None, "adapter"),
    "0xe1010d0ab476908054981d12fbb96915efcceb8d57a09ae8ca62f4bda7318373": ('Swap', "adapter"),
    "0x6d32c49713283fb5a97cce0721fc538d74432a839930d49ddb15a199f6cb44c3": (None, "adapter"),
    "0x298c349c742327269dc8de6ad66687767310c948ea309df826f5bd103e19d207": ('Swap', 'v2style'),
    "0xa4228e1eb11eb9b31069d9ed20e7af9a010ca1a02d4855cee54e08e188fcc32c": ('Swap', 'v3style'),
    "0x4e10ec2896c1df89b9597b5fdf91d1005fe1c886d92335c5e7f6584a5b8b906e": ('Swaped', 'v2shift'),
})


# 読み規則テーブル: st は戦略、tin/ain/tout/aout は欄参照 [place, index]。
# 各戦略の意味と確定根拠（サンプル実測との照合）は NOTES.md を参照。
ADAPTER_READERS = json.loads(r'''{
 "0xe0c1b141a85ba7f8df7ded19a02a004b049d9e0a67f9dea87186c07bce3923b0": {
  "kind": "Trade",
  "types": {
   "indexed": [
    "address",
    "address"
   ],
   "data": [
    "uint256",
    "uint256",
    "uint256"
   ]
  },
  "st": "flows",
  "ain": [
   "data",
   1
  ],
  "aout": [
   "data",
   0
  ]
 },
 "0x853c8e677490312983a1757c837355487031d90920d260d439269c024de030c2": {
  "kind": "Swapped",
  "types": {
   "indexed": [
    "address",
    "address",
    "address"
   ],
   "data": [
    "address",
    "uint256",
    "uint256",
    "uint256",
    "address"
   ]
  },
  "st": "event",
  "tin": [
   "indexed",
   2
  ],
  "ain": [
   "data",
   1
  ],
  "tout": [
   "data",
   0
  ],
  "aout": [
   "data",
   3
  ]
 },
 "0x9ec2cbdc7c510df38f982d15dfab2db2ba557c382afbec908ed154dfb83b1fcb": {
  "kind": "Swapped",
  "types": {
   "indexed": [
    "address"
   ],
   "data": [
    "address",
    "uint8",
    "address",
    "address",
    "uint256",
    "uint256"
   ]
  },
  "st": "event",
  "tin": [
   "data",
   3
  ],
  "ain": [
   "data",
   5
  ],
  "tout": [
   "data",
   2
  ],
  "aout": [
   "data",
   4
  ]
 },
 "0x5a417e3ceaff68360f7300df6686a9d417ae12328724de7bc2fd6c012a2ffa85": {
  "kind": "Swapped",
  "types": {
   "indexed": [
    "address",
    "address",
    "address"
   ],
   "data": [
    "uint256"
   ]
  },
  "st": "flows",
  "ain": [
   "data",
   0
  ],
  "aout": [
   "data",
   0
  ]
 },
 "0xee4825a7988c5519a874be79f2a84d13d02cfe5f74cad97cda2d5b55d2b13bfa": {
  "kind": "SwapExecuted",
  "types": {
   "indexed": [
    "address",
    "address"
   ],
   "data": [
    "uint256",
    "uint256",
    "address"
   ]
  },
  "st": "event",
  "tin": [
   "indexed",
   0
  ],
  "ain": [
   "data",
   0
  ],
  "tout": [
   "indexed",
   1
  ],
  "aout": [
   "data",
   1
  ]
 },
 "0x77f92a1b6a1a11de8ca49515ad4c1fad45632dd3442167d74b90b304a3c7a758": {
  "kind": "Swap",
  "types": {
   "indexed": [],
   "data": [
    "address",
    "uint256",
    "uint256"
   ]
  },
  "st": "flows",
  "ain": [
   "data",
   1
  ],
  "aout": [
   "data",
   2
  ]
 },
 "0x4aa9c8b0c9a939db1d9261ba132e6ad5bc8390e89cb6ac192c10f4ca67f49e40": {
  "kind": "Swapped",
  "types": {
   "indexed": [
    "address"
   ],
   "data": [
    "bool",
    "uint256",
    "uint256"
   ]
  },
  "st": "flows",
  "ain": [
   "data",
   2
  ],
  "aout": [
   "data",
   1
  ]
 },
 "0xc9b356cd595a52e855855ac43eae98058d001ef4f0895fc098509ad2f2fe7cad": {
  "kind": "Traded",
  "types": {
   "indexed": [
    "address",
    "address",
    "address"
   ],
   "data": [
    "uint256",
    "uint256"
   ]
  },
  "st": "event",
  "tin": [
   "indexed",
   1
  ],
  "ain": [
   "data",
   0
  ],
  "tout": [
   "indexed",
   2
  ],
  "aout": [
   "data",
   1
  ]
 },
 "0xee524511e0afdb415a78a457a9fb0be80c11f2d7d2bbec13af282afd933923f9": {
  "kind": "Bought",
  "types": {
   "indexed": [
    "address",
    "address"
   ],
   "data": [
    "uint256",
    "uint256",
    "uint256",
    "uint256",
    "uint256",
    "uint256"
   ]
  },
  "st": "flows",
  "ain": [
   "data",
   3
  ],
  "aout": [
   "data",
   0
  ]
 },
 "0x6db63bebf1e6540277744df32846ebdb98385b1a73f2d5de49b28348add63f50": {
  "kind": "TokensSold",
  "types": {
   "indexed": [
    "address"
   ],
   "data": [
    "uint256",
    "uint256",
    "uint256"
   ]
  },
  "st": "flows",
  "ain": [
   "data",
   0
  ],
  "aout": [
   "data",
   1
  ]
 },
 "0x2ad8739d64c070ab4ae9d9c0743d56550b22c3c8c96e7a6045fac37b5b8e89e3": {
  "kind": "Swap",
  "types": {
   "indexed": [
    "address",
    "address"
   ],
   "data": [
    "uint256",
    "uint256",
    "uint256",
    "uint256",
    "uint256",
    "uint256",
    "uint256"
   ]
  },
  "st": "flows",
  "ain": [
   "data",
   0
  ],
  "aout": [
   "data",
   3
  ]
 },
 "0x2afb56853a8b76a0868e64c0001eedd84d9a7f5bc7fb788aba6180e422ba6ec5": {
  "kind": "Swap",
  "types": {
   "indexed": [
    "uint256",
    "bytes32"
   ],
   "data": [
    "uint256",
    "uint256",
    "address"
   ]
  },
  "st": "flows",
  "ain": [
   "data",
   1
  ],
  "aout": [
   "data",
   1
  ]
 },
 "0x0a17ff6a9995a1332a432b8bf852005142714ccfe1f1ccda72a3aba5e9ed408f": {
  "kind": null,
  "types": {
   "indexed": [
    "address",
    "address"
   ],
   "data": [
    "uint256",
    "uint256"
   ]
  },
  "st": "event",
  "tin": [
   "indexed",
   1
  ],
  "ain": [
   "data",
   1
  ],
  "tout": [
   "indexed",
   0
  ],
  "aout": [
   "data",
   0
  ]
 },
 "0x22aea6cd253f249ce9b7d0a1af21a0eca8bbe111c26bcc705eece987ad4d2105": {
  "kind": null,
  "types": {
   "indexed": [
    "uint256",
    "address",
    "address"
   ],
   "data": [
    "uint256",
    "uint256"
   ]
  },
  "st": "event",
  "tin": [
   "indexed",
   2
  ],
  "ain": [
   "data",
   1
  ],
  "tout": [
   "indexed",
   1
  ],
  "aout": [
   "data",
   0
  ]
 },
 "0x4fd47c7108c4f49e2398c5159bbd183546a2fca795309705e629fed588852416": {
  "kind": null,
  "types": {
   "indexed": [
    "address",
    "address",
    "address"
   ],
   "data": [
    "uint256",
    "uint256",
    "uint256"
   ]
  },
  "st": "event",
  "tin": [
   "indexed",
   2
  ],
  "ain": [
   "data",
   2
  ],
  "tout": [
   "indexed",
   1
  ],
  "aout": [
   "data",
   1
  ]
 },
 "0xdde2f3711ab09cdddcfee16ca03e54d21fb8cf3fa647b9797913c950d38ad693": {
  "kind": "SwapExecuted",
  "types": {
   "indexed": [],
   "data": [
    "address",
    "address",
    "uint256",
    "address",
    "uint256"
   ]
  },
  "st": "event",
  "tin": [
   "data",
   1
  ],
  "ain": [
   "data",
   2
  ],
  "tout": [
   "data",
   3
  ],
  "aout": [
   "data",
   4
  ]
 },
 "0x0e8e403c2d36126272b08c75823e988381d9dc47f2f0a9a080d95f891d95c469": {
  "kind": "WooSwap",
  "types": {
   "indexed": [
    "address",
    "address",
    "address"
   ],
   "data": [
    "uint256",
    "uint256",
    "address",
    "address",
    "uint256",
    "uint256"
   ]
  },
  "st": "event",
  "tin": [
   "indexed",
   0
  ],
  "ain": [
   "data",
   0
  ],
  "tout": [
   "indexed",
   1
  ],
  "aout": [
   "data",
   1
  ]
 },
 "0x8b3e96f2b889fa771c53c981b40daf005f63f637f1869f707052d15a3dd97140": {
  "kind": "TokenExchange",
  "types": {
   "indexed": [
    "address"
   ],
   "data": [
    "int128",
    "uint256",
    "int128",
    "uint256"
   ]
  },
  "st": "flows",
  "ain": [
   "data",
   1
  ],
  "aout": [
   "data",
   3
  ]
 },
 "0x3b841dc9ab51e3104bda4f61b41e4271192d22cd19da5ee6e292dc8e2744f713": {
  "kind": "Swap",
  "types": {
   "indexed": [],
   "data": [
    "address",
    "address",
    "bool",
    "bool",
    "uint256",
    "uint256",
    "int32"
   ]
  },
  "st": "flows",
  "ain": [
   "data",
   4
  ],
  "aout": [
   "data",
   5
  ]
 },
 "0x143f1f8e861fbdeddd5b46e844b7d3ac7b86a122f36e8c463859ee6811b1f29c": {
  "kind": "TokenExchange",
  "types": {
   "indexed": [
    "address"
   ],
   "data": [
    "uint256",
    "uint256",
    "uint256",
    "uint256",
    "uint256",
    "uint256"
   ]
  },
  "st": "flows",
  "ain": [
   "data",
   1
  ],
  "aout": [
   "data",
   3
  ]
 },
 "0x2170c741c41531aec20e7c107c24eecfdd15e69c9bb0a8dd37b1840b9e0b207b": {
  "kind": "Swap",
  "types": {
   "indexed": [
    "bytes32",
    "address",
    "address"
   ],
   "data": [
    "uint256",
    "uint256"
   ]
  },
  "st": "event",
  "tin": [
   "indexed",
   1
  ],
  "ain": [
   "data",
   0
  ],
  "tout": [
   "indexed",
   2
  ],
  "aout": [
   "data",
   1
  ]
 },
 "0x88b012650d7da64b7697c175b1dc42bd7dd09d8a2ef28c62cbb448086f548ce6": {
  "kind": "Swapped",
  "types": {
   "indexed": [
    "uint256"
   ],
   "data": [
    "bool",
    "uint256",
    "uint256"
   ]
  },
  "st": "flows",
  "ain": [
   "data",
   2
  ],
  "aout": [
   "data",
   1
  ]
 },
 "0x0874b2d545cb271cdbda4e093020c452328b24af12382ed62c4d00f5c26709db": {
  "kind": "Swap",
  "types": {
   "indexed": [
    "address",
    "address",
    "address"
   ],
   "data": [
    "uint256",
    "uint256",
    "uint256",
    "uint256"
   ]
  },
  "st": "event",
  "tin": [
   "indexed",
   1
  ],
  "ain": [
   "data",
   0
  ],
  "tout": [
   "indexed",
   2
  ],
  "aout": [
   "data",
   1
  ]
 },
 "0xc2c0245e056d5fb095f04cd6373bc770802ebd1e6c918eb78fdef843cdb37b0f": {
  "kind": "DODOSwap",
  "types": {
   "indexed": [],
   "data": [
    "address",
    "address",
    "uint256",
    "uint256",
    "address",
    "address"
   ]
  },
  "st": "event",
  "tin": [
   "data",
   0
  ],
  "ain": [
   "data",
   2
  ],
  "tout": [
   "data",
   1
  ],
  "aout": [
   "data",
   3
  ]
 },
 "0xe2cee3f6836059820b673943853afebd9b3026125dab0d774284e6f28a4855be": {
  "kind": "Swapped",
  "types": {
   "indexed": [
    "address",
    "address",
    "address"
   ],
   "data": [
    "uint256",
    "uint256",
    "uint256",
    "uint256"
   ]
  },
  "st": "event",
  "tin": [
   "indexed",
   2
  ],
  "ain": [
   "data",
   1
  ],
  "tout": [
   "indexed",
   1
  ],
  "aout": [
   "data",
   0
  ]
 },
 "0xbfca96f8fea048c07457ab90159d04dfbef79bc43270596da55ae633213585e4": {
  "kind": "Swapped",
  "types": {
   "indexed": [
    "address",
    "address"
   ],
   "data": [
    "uint8",
    "uint256",
    "uint256",
    "uint256"
   ]
  },
  "st": "event",
  "tin": [
   "indexed",
   1
  ],
  "ain": [
   "data",
   2
  ],
  "tout": [
   "indexed",
   0
  ],
  "aout": [
   "data",
   1
  ]
 },
 "0xb159dc503a6f507638f48f62f81cab93f1e806b7476ac426ea5f5793ddbf9127": {
  "kind": "Swap",
  "types": {
   "indexed": [
    "address",
    "address"
   ],
   "data": [
    "uint256",
    "uint256",
    "uint24",
    "bool"
   ]
  },
  "st": "flows",
  "ain": [
   "data",
   0
  ],
  "aout": [
   "data",
   1
  ]
 },
 "0xb2e76ae99761dc136e598d4a629bb347eccb9532a5f8bbd72e18467c3c34cc98": {
  "kind": "TokenExchange",
  "types": {
   "indexed": [
    "address"
   ],
   "data": [
    "uint256",
    "uint256",
    "uint256",
    "uint256"
   ]
  },
  "st": "flows",
  "ain": [
   "data",
   1
  ],
  "aout": [
   "data",
   3
  ]
 },
 "0xc4cc441884c8b8ce500de14f1f490522149ac5ec673710ca5c792c38ec736847": {
  "kind": "Swap",
  "types": {
   "indexed": [
    "address",
    "address"
   ],
   "data": [
    "address",
    "address",
    "uint256",
    "uint256",
    "bytes32"
   ]
  },
  "st": "event",
  "tin": [
   "data",
   0
  ],
  "ain": [
   "data",
   2
  ],
  "tout": [
   "data",
   1
  ],
  "aout": [
   "data",
   3
  ]
 },
 "0xb39c9bc43f811e1a7ce159c5f147458fdb80266bf23c17322013316e27e086d0": {
  "kind": "Swap",
  "types": {
   "indexed": [],
   "data": [
    "address",
    "address",
    "uint256",
    "uint256",
    "address"
   ]
  },
  "st": "event",
  "tin": [
   "data",
   0
  ],
  "ain": [
   "data",
   2
  ],
  "tout": [
   "data",
   1
  ],
  "aout": [
   "data",
   3
  ]
 },
 "0xfe5d070a8c6c1bf4a4675905e56f14d922474c8fead10febb0d9c963a2c2d0e7": {
  "kind": "Unknown",
  "types": {
   "indexed": [
    "address",
    "address"
   ],
   "data": [
    "uint256",
    "uint256",
    "uint256",
    "uint256"
   ]
  },
  "st": "flows",
  "ain": [
   "data",
   0
  ],
  "aout": [
   "data",
   3
  ]
 },
 "0xa54201e99d8e1488291e015a3af88c7a169bcf4932617ecd9536e1f1ad435110": {
  "kind": "Swapped",
  "types": {
   "indexed": [
    "address",
    "address"
   ],
   "data": [
    "uint256",
    "uint256",
    "uint256"
   ]
  },
  "st": "event",
  "tin": [
   "indexed",
   1
  ],
  "ain": [
   "data",
   1
  ],
  "tout": [
   "indexed",
   0
  ],
  "aout": [
   "data",
   0
  ]
 },
 "0xca1985de4499b70bca89ad1f3711d7081a4bfefe76dfbdf83beecf1336fb85d8": {
  "kind": "Swapped",
  "types": {
   "indexed": [
    "bytes32"
   ],
   "data": [
    "address",
    "address",
    "uint256",
    "uint256",
    "uint256",
    "uint256",
    "uint8"
   ]
  },
  "st": "event",
  "tin": [
   "data",
   1
  ],
  "ain": [
   "data",
   3
  ],
  "tout": [
   "data",
   0
  ],
  "aout": [
   "data",
   2
  ]
 },
 "0x086c7fcd1952b421cc49f207e360cef93dead3f9caa1d6eb4b0e51bb58367738": {
  "kind": "StableSwap",
  "types": {
   "indexed": [
    "address",
    "address",
    "address"
   ],
   "data": [
    "uint256",
    "uint256",
    "uint256",
    "uint256",
    "uint256"
   ]
  },
  "st": "event",
  "tin": [
   "indexed",
   1
  ],
  "ain": [
   "data",
   0
  ],
  "tout": [
   "indexed",
   2
  ],
  "aout": [
   "data",
   1
  ]
 },
 "0x546b1f988790578dc4043a3d0e3640574a568eb04e498b08ef98518eee574882": {
  "kind": "WEDXswapEvent",
  "types": {
   "indexed": [
    "address",
    "address",
    "address"
   ],
   "data": [
    "uint256",
    "uint256",
    "address",
    "uint256"
   ]
  },
  "st": "event",
  "tin": [
   "indexed",
   1
  ],
  "ain": [
   "data",
   0
  ],
  "tout": [
   "indexed",
   2
  ],
  "aout": [
   "data",
   1
  ]
 },
 "0xb8a2a375cd0661da64ed8ab72ba981ae5e21e5d484fb6eaa8e5236ac2111d13c": {
  "kind": "Swapped",
  "types": {
   "indexed": [
    "address"
   ],
   "data": [
    "address",
    "uint256",
    "uint24",
    "uint256",
    "uint256"
   ]
  },
  "st": "event",
  "tin": [
   "indexed",
   0
  ],
  "ain": [
   "data",
   1
  ],
  "tout": [
   "data",
   0
  ],
  "aout": [
   "data",
   4
  ]
 },
 "0xac98876c63e9e95442ed72d8ca81d9a5c922f18616dc2b8b155416310b898dd9": {
  "kind": "Swap",
  "types": {
   "indexed": [],
   "data": [
    "address",
    "address",
    "address",
    "address",
    "uint256",
    "uint256",
    "uint256",
    "uint256",
    "uint256",
    "bytes32",
    "uint256",
    "uint256",
    "uint256",
    "uint256",
    "uint256",
    "uint256"
   ]
  },
  "st": "event",
  "tin": [
   "data",
   2
  ],
  "ain": [
   "data",
   4
  ],
  "tout": [
   "data",
   3
  ],
  "aout": [
   "data",
   5
  ],
  "sentinel": true
 },
 "0x2fb0748023df976464b18b5e5d1c2f8d79da559ffeb202c8a2ce83737a946906": {
  "kind": "MultiSwap",
  "types": {
   "indexed": [
    "address",
    "address",
    "address"
   ],
   "data": [
    "uint256",
    "uint256",
    "uint256",
    "uint256"
   ]
  },
  "st": "event",
  "tin": [
   "indexed",
   1
  ],
  "ain": [
   "data",
   0
  ],
  "tout": [
   "indexed",
   2
  ],
  "aout": [
   "data",
   2
  ]
 },
 "0xa62ddba0018cb74df710cd81eb65dbeb85eb16937bc4967591c36b169d8100c6": {
  "kind": "SwapDone",
  "types": {
   "indexed": [
    "address"
   ],
   "data": [
    "address",
    "address",
    "uint256",
    "uint256"
   ]
  },
  "st": "event",
  "tin": [
   "data",
   0
  ],
  "ain": [
   "data",
   2
  ],
  "tout": [
   "data",
   1
  ],
  "aout": [
   "data",
   3
  ]
 },
 "0xdb587d878116df0bdd4fe154699aa2c5f439da001cc811dfd05d9f589fc5a8ee": {
  "kind": "Swapped",
  "types": {
   "indexed": [],
   "data": [
    "address",
    "uint256",
    "address",
    "uint256"
   ]
  },
  "st": "event",
  "tin": [
   "data",
   0
  ],
  "ain": [
   "data",
   1
  ],
  "tout": [
   "data",
   2
  ],
  "aout": [
   "data",
   3
  ]
 },
 "0xc82975a4eae9f14416813a0bd7312edf547928cd25ae9b8597ee4c92fa6862d2": {
  "kind": "RFQTrade",
  "types": {
   "indexed": [],
   "data": [
    "address",
    "address",
    "address",
    "uint256",
    "uint256",
    "bytes16",
    "address"
   ]
  },
  "st": "event",
  "tin": [
   "data",
   1
  ],
  "ain": [
   "data",
   3
  ],
  "tout": [
   "data",
   2
  ],
  "aout": [
   "data",
   4
  ]
 },
 "0x27c98e911efdd224f4002f6cd831c3ad0d2759ee176f9ee8466d95826af22a1c": {
  "kind": "WooRouterSwap",
  "types": {
   "indexed": [
    "address",
    "address",
    "address"
   ],
   "data": [
    "uint8",
    "uint256",
    "uint256",
    "address",
    "address"
   ]
  },
  "st": "event",
  "tin": [
   "indexed",
   0
  ],
  "ain": [
   "data",
   1
  ],
  "tout": [
   "indexed",
   1
  ],
  "aout": [
   "data",
   2
  ]
 },
 "0x2e65e08f215f8172b6bc6ae6c1191f00c014355c9d39c03627de921283bd3ba2": {
  "kind": "Unknown",
  "types": {
   "indexed": [
    "uint256"
   ],
   "data": [
    "address",
    "address",
    "uint256",
    "uint256"
   ]
  },
  "st": "event",
  "tin": [
   "data",
   0
  ],
  "ain": [
   "data",
   2
  ],
  "tout": [
   "data",
   1
  ],
  "aout": [
   "data",
   3
  ]
 },
 "0x3993372c7f54dee1879dcc58987731c2857a033eccc34111f4d2efb1b045d947": {
  "kind": "Swap",
  "types": {
   "indexed": [
    "address",
    "address",
    "address"
   ],
   "data": [
    "address",
    "address",
    "uint256",
    "uint256",
    "uint112",
    "uint256",
    "uint256"
   ]
  },
  "st": "event",
  "tin": [
   "data",
   0
  ],
  "ain": [
   "data",
   2
  ],
  "tout": [
   "data",
   1
  ],
  "aout": [
   "data",
   3
  ],
  "pool_field": [
   "indexed",
   2
  ]
 },
 "0xdd36740e2a012d93061a0d99eaa9107860955de4e90027d3cf465a055026c407": {
  "kind": "SwapExecuted",
  "types": {
   "indexed": [
    "address",
    "address"
   ],
   "data": [
    "uint256",
    "uint256"
   ]
  },
  "st": "event",
  "tin": [
   "indexed",
   0
  ],
  "ain": [
   "data",
   0
  ],
  "tout": [
   "indexed",
   1
  ],
  "aout": [
   "data",
   1
  ]
 },
 "0xa3f37f0446dcac60c3831cc0ebda32d57d7cddb7bef6d734cdd7b0c72c277922": {
  "kind": "SwapExecuted",
  "types": {
   "indexed": [
    "address",
    "address",
    "address"
   ],
   "data": [
    "uint256",
    "uint256",
    "uint8"
   ]
  },
  "st": "event",
  "tin": [
   "indexed",
   1
  ],
  "ain": [
   "data",
   0
  ],
  "tout": [
   "indexed",
   2
  ],
  "aout": [
   "data",
   1
  ]
 },
 "0xa078c4190abe07940190effc1846be0ccf03ad6007bc9e93f9697d0b460befbb": {
  "kind": "Swapped",
  "types": {
   "indexed": [
    "address",
    "address"
   ],
   "data": [
    "uint256",
    "uint256"
   ]
  },
  "st": "event",
  "tin": [
   "indexed",
   0
  ],
  "ain": [
   "data",
   0
  ],
  "tout": [
   "indexed",
   1
  ],
  "aout": [
   "data",
   1
  ]
 },
 "0x25f1d03755df23c30e25db2dbd3891e31ce084bdfbfc46f9fe5e446ee5f9b2d4": {
  "kind": "TokensSwapped",
  "types": {
   "indexed": [
    "address"
   ],
   "data": [
    "address",
    "uint256",
    "uint256"
   ]
  },
  "st": "event",
  "tin": [
   "indexed",
   0
  ],
  "ain": [
   "data",
   1
  ],
  "tout": [
   "data",
   0
  ],
  "aout": [
   "data",
   2
  ]
 },
 "0x6621486d9c28838df4a87d2cca5007bc2aaf6a5b5de083b1db8faf709302c473": {
  "kind": "OrderFilled",
  "types": {
   "indexed": [
    "bytes32",
    "address",
    "address"
   ],
   "data": [
    "address",
    "uint256",
    "address",
    "uint256"
   ]
  },
  "st": "event",
  "tin": [
   "data",
   2
  ],
  "ain": [
   "data",
   3
  ],
  "tout": [
   "data",
   0
  ],
  "aout": [
   "data",
   1
  ]
 },
 "0xbbc79dc7d11fb8ae8de963e009943172807faa4a90c3620ef772f4783e76355f": {
  "kind": "Sell",
  "types": {
   "indexed": [
    "address",
    "address",
    "address"
   ],
   "data": [
    "uint256",
    "uint256"
   ]
  },
  "st": "event",
  "tin": [
   "indexed",
   1
  ],
  "ain": [
   "data",
   0
  ],
  "tout": [
   "indexed",
   2
  ],
  "aout": [
   "data",
   1
  ]
 },
 "0x5380cf97d8f645d3c4896da60c053458dca03a3a31cec642ac80e1ddf0d8d02a": {
  "kind": "Swap",
  "types": {
   "indexed": [
    "address",
    "address",
    "address"
   ],
   "data": [
    "uint256",
    "uint256"
   ]
  },
  "st": "event",
  "tin": [
   "indexed",
   2
  ],
  "ain": [
   "data",
   1
  ],
  "tout": [
   "indexed",
   1
  ],
  "aout": [
   "data",
   0
  ]
 },
 "0xe4c8d224f59766afb8ace8df8ee71f9f0e26664f915e4045a3df5296a991ad50": {
  "kind": "TideFiSwap",
  "types": {
   "indexed": [
    "address",
    "address"
   ],
   "data": [
    "uint256",
    "uint256"
   ]
  },
  "st": "flows",
  "ain": [
   "data",
   0
  ],
  "aout": [
   "data",
   1
  ]
 },
 "0x6b17016c67d28f4cc3a653d895e3b1db09320d6852a46f95aa225059190f7dcd": {
  "kind": "TokensSold",
  "types": {
   "indexed": [
    "address"
   ],
   "data": [
    "uint256",
    "uint256",
    "uint256",
    "uint256"
   ]
  },
  "st": "flows",
  "ain": [
   "data",
   0
  ],
  "aout": [
   "data",
   1
  ]
 },
 "0x673ff070bec3aea00f2fa45c0f7ed58c227af4a03a626e01fe29b34d3b9ecfdb": {
  "kind": "Swap",
  "types": {
   "indexed": [
    "address",
    "address"
   ],
   "data": [
    "bool",
    "uint256",
    "uint256",
    "uint256",
    "uint256",
    "uint256"
   ]
  },
  "st": "gross",
  "ain": [
   "data",
   1
  ],
  "aout": [
   "data",
   2
  ]
 },
 "0x1560b71ca6695825842f12e8721dc7f437286c08deb7c7ebc708bb3bdc3d01c5": {
  "kind": "StockBought",
  "types": {
   "indexed": [
    "address"
   ],
   "data": [
    "uint256",
    "uint256"
   ]
  },
  "st": "gross",
  "ain": [
   "data",
   1
  ],
  "aout": [
   "data",
   0
  ]
 },
 "0x7a7e1d68c6a73a4841f6e57bf2d968148c641201d165b651b1fd3a43f270991b": {
  "kind": "Swapped",
  "types": {
   "indexed": [],
   "data": [
    "bool",
    "uint256",
    "uint256"
   ]
  },
  "st": "gross_rev",
  "ain": [
   "data",
   1
  ],
  "aout": [
   "data",
   2
  ]
 },
 "0x9c45db89e530035bcb2e1d6a8c5526097f83283b8c1938f69ee5ffff9ae133ca": {
  "kind": null,
  "types": {
   "indexed": [
    "address",
    "address"
   ],
   "data": [
    "uint256",
    "uint256",
    "uint256"
   ]
  },
  "st": "flows",
  "ain": [
   "data",
   0
  ],
  "aout": [
   "data",
   1
  ]
 },
 "0x0a3279173ae79e514355394a631c9ff226a83934a08c0703301aaccd3a8afa70": {
  "kind": null,
  "types": {
   "indexed": [
    "address"
   ],
   "data": [
    "address",
    "address",
    "uint256",
    "uint256",
    "uint256"
   ]
  },
  "st": "event_flow_out",
  "tin": [
   "data",
   0
  ],
  "ain": [
   "data",
   2
  ],
  "tout": [
   "data",
   1
  ]
 },
 "0x2251435bd151cd72851a82be055bf6d1c3d7f34d08d56493dddf874229b8e897": {
  "kind": "TransitSwapped",
  "types": {
   "indexed": [
    "address",
    "address",
    "address"
   ],
   "data": [
    "uint256",
    "uint256",
    "uint256",
    "uint256",
    "uint256",
    "bytes32"
   ]
  },
  "st": "event",
  "tin": [
   "indexed",
   0
  ],
  "ain": [
   "data",
   0
  ],
  "tout": [
   "indexed",
   1
  ],
  "aout": [
   "data",
   1
  ],
  "sentinel": true
 },
 "0x6bef5eec9bc0ce6168c21a5cfb879d8b7dd72ef58d698042cd16112a52a7daa3": {
  "kind": null,
  "types": {
   "indexed": [
    "address"
   ],
   "data": [
    "address",
    "uint256",
    "address",
    "address",
    "uint256",
    "uint256",
    "uint256"
   ]
  },
  "st": "event_auto_dir",
  "cand": [
   [
    [
     "data",
     2
    ],
    [
     "data",
     4
    ]
   ],
   [
    [
     "data",
     3
    ],
    [
     "data",
     5
    ]
   ]
  ],
  "trader": [
   "indexed",
   0
  ]
 },
 "0xe1010d0ab476908054981d12fbb96915efcceb8d57a09ae8ca62f4bda7318373": {
  "kind": "Swap",
  "types": {
   "indexed": [],
   "data": [
    "address[]",
    "uint256[]",
    "address"
   ]
  },
  "st": "array"
 },
 "0x6d32c49713283fb5a97cce0721fc538d74432a839930d49ddb15a199f6cb44c3": {
  "kind": null,
  "types": {
   "indexed": [
    "address"
   ],
   "data": [
    "uint256",
    "uint256"
   ]
  },
  "st": "flows",
  "ain": [
   "data",
   0
  ],
  "aout": [
   "data",
   1
  ]
 }
}''')


def eth_call(to, data):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                       "params": [{"to": to, "data": data}, "latest"]}).encode()
    last = None
    start = hash(to) % len(RPC_URLS)
    for k in range(6):
        try:
            req = urllib.request.Request(RPC_URLS[(start + k) % len(RPC_URLS)],
                                         data=body, headers=UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                out = json.load(r)
            if out.get("result") not in (None, "0x"):
                return out["result"]
            last = out
        except Exception as e:  # noqa: BLE001 - rotate endpoint and retry
            last = e
        time.sleep(0.5)
    raise RuntimeError(f"eth_call failed for {to}: {last}")


def call_address(to, selector):
    return "0x" + eth_call(to, selector)[-40:]


def signed(v, bits):
    return v - (1 << bits) if v >= (1 << (bits - 1)) else v


def hexint(v):
    return int(v, 16) if isinstance(v, str) else v


def words_of(log):
    data = log["data"][2:]
    return [int(data[i:i + 64], 16) for i in range(0, len(data) - 63, 64)]


def receipt_flows(rec):
    """net token flow per (holder, token) from Transfer logs."""
    net = defaultdict(int)
    for log in rec.get("logs", []):
        ts = log.get("topics") or []
        if ts and ts[0].lower() == TRANSFER and len(ts) >= 3 \
                and len(log.get("data") or "") > 2:
            amt = int(log["data"], 16)
            tok = log["address"].lower()
            net[("0x" + ts[1][-40:], tok)] -= amt
            net[("0x" + ts[2][-40:], tok)] += amt
    return net

# --- 最小アダプター: 読み規則に従って 1 leg を組み立てる -------------------

_SENTINELS = {"0x" + "0" * 40, "0x" + "e" * 40}


def _addr_of(v):
    return "0x" + format(v & ((1 << 160) - 1), "040x")


def _amt_of(v, ty):
    if ty.startswith("int"):
        return signed(v, 256)
    if ty == "int128":
        return signed(v & ((1 << 128) - 1), 128)
    return v


def _fval(spec, ev, ref):
    place, i = ref
    if place == "indexed":
        ts = ev["topics"]
        return int(ts[i + 1], 16) if i + 1 < len(ts) else 0
    w = ev["words"]
    return w[i] if i < len(w) else 0


def _ftype(spec, ref):
    return spec["types"][ref[0]][ref[1]]


def _gross_flows(rec, pool):
    """pool の粗 Transfer（受入 / 送出の個々のTransfer）"""
    g = {"in": [], "out": []}
    for log in rec.get("logs", []):
        ts = log.get("topics") or []
        if ts and ts[0].lower() == TRANSFER and len(ts) >= 3 \
                and len(log.get("data") or "") > 2:
            amt = int(log["data"], 16)
            if not amt:
                continue
            tok = log["address"].lower()
            frm = "0x" + ts[1][-40:]
            to = "0x" + ts[2][-40:]
            if to == pool and frm != pool:
                g["in"].append((tok, amt))
            if frm == pool and to != pool:
                g["out"].append((tok, amt))
    return g


def _agg(entries):
    d = {}
    for tok, amt in entries:
        d[tok] = d.get(tok, 0) + amt
    return d


def _match_list(entries, val):
    """個々のTransfer額と照合（同額の複数Transferは dict 折り畳みで消えない）"""
    hits = [tok for tok, amt in entries if amt == val or amt == -val]
    return hits[0] if hits else None


def _match_tok(table, val):
    """量 val（±両符号許容）に一致するトークン。一意なら確定、
    複数は先頭（曖昧 — NOTES の「曖昧一致」参照）、無ければ None。"""
    hits = [tok for tok, amt in table.items() if amt == val or amt == -val]
    return hits[0] if hits else None


def _pool_net(net, pool):
    rin, rout = {}, {}
    for (holder, tok), v in net.items():
        if holder != pool or v == 0:
            continue
        (rin if v > 0 else rout)[tok] = v if v > 0 else -v
    return rin, rout


def adapter_leg(ev, rec):
    spec = ADAPTER_READERS[ev["topics"][0].lower()]
    st = spec["st"]
    pool = ev["pool"]
    w = ev["words"]
    net = gross = None

    def flows():
        nonlocal net, gross
        if net is None:
            net = receipt_flows(rec)
            gross = _gross_flows(rec, pool)
        return net, gross

    tin = tout = None
    ain = aout = None
    if st == "event":
        ain = _amt_of(_fval(spec, ev, spec["ain"]), _ftype(spec, spec["ain"]))
        aout = _amt_of(_fval(spec, ev, spec["aout"]), _ftype(spec, spec["aout"]))
        tin = _addr_of(_fval(spec, ev, spec["tin"]))
        tout = _addr_of(_fval(spec, ev, spec["tout"]))
        if spec.get("sentinel"):
            tin = ETH0 if tin in _SENTINELS else tin
            tout = ETH0 if tout in _SENTINELS else tout
        if spec.get("pool_field"):
            pool = _addr_of(_fval(spec, ev, spec["pool_field"]))
    elif st == "flows":
        _, gross = flows()
        rin, rout = _agg(gross["in"]), _agg(gross["out"])
        ain = _amt_of(_fval(spec, ev, spec["ain"]), _ftype(spec, spec["ain"]))
        aout = _amt_of(_fval(spec, ev, spec["aout"]), _ftype(spec, spec["aout"]))
        if spec["ain"] == spec["aout"]:
            # 1:1 交換（共有量欄）: 受入 / 送出が各 1 トークンであることを要求
            # （同一トークンの往復=バーン&ミント型もそのまま出す。NOTES 参照）
            if len(rin) == 1 and len(rout) == 1:
                tin, tout = next(iter(rin)), next(iter(rout))
                ain = aout = abs(ain)
        else:
            tin = (_match_list(gross["in"], ain) or _match_tok(rin, ain))
            tout = (_match_list(gross["out"], aout) or _match_tok(rout, aout))
    elif st in ("gross", "gross_rev"):
        _, gross = flows()
        ain = _amt_of(_fval(spec, ev, spec["ain"]), _ftype(spec, spec["ain"]))
        aout = _amt_of(_fval(spec, ev, spec["aout"]), _ftype(spec, spec["aout"]))
        side_in, side_out = (("out", "in") if st == "gross_rev" else ("in", "out"))
        tin = _match_list(gross[side_in], ain)
        tout = _match_list(gross[side_out], aout)
    elif st == "event_flow_out":
        # in 側は宣言欄、out 側は宣言トークンの pool 送出実額（0a327917 型）
        ain = _amt_of(_fval(spec, ev, spec["ain"]), _ftype(spec, spec["ain"]))
        tin = _addr_of(_fval(spec, ev, spec["tin"]))
        tout = _addr_of(_fval(spec, ev, spec["tout"]))
        _, gross = flows()
        aout = sum(a for t, a in gross["out"] if t == tout) or None
    elif st == "event_auto_dir":
        # trader アドレスの送出方向で 2 候補の向きを確定する（6bef5eec 型）
        trader = _addr_of(_fval(spec, ev, spec["trader"]))
        _, gross = flows()
        c0, c1 = spec["cand"]
        d0 = (_addr_of(_fval(spec, ev, c0[0])), _amt_of(_fval(spec, ev, c0[1]), _ftype(spec, c0[1])))
        d1 = (_addr_of(_fval(spec, ev, c1[0])), _amt_of(_fval(spec, ev, c1[1]), _ftype(spec, c1[1])))
        sent_by_trader = [(t, a) for (t, a) in
                          [(tok, amt) for tok, amt, frm, to in
                           [(lg_tok, lg_amt, lg_frm, lg_to) for lg_tok, lg_amt, lg_frm, lg_to in
                            _trader_flows(rec, trader)]] if a]
        tin, ain, tout, aout = d0[0], d0[1], d1[0], d1[1]
        if any(t == d1[0] and a == d1[1] for t, a in sent_by_trader):
            tin, ain, tout, aout = d1[0], d1[1], d0[0], d0[1]
    elif st == "array":
        # 動的配列形式（SPEC 規則4）: path[0]/amounts[0] -> path[-1]/amounts[-1]
        data = ev["log"]["data"][2:]
        hx = spec  # 型は使わない
        try:
            p_off = w[0] * 2
            a_off = w[1] * 2
            pn = int(data[p_off:p_off + 64], 16)
            an = int(data[a_off:a_off + 64], 16)
            path = [_addr_of(int(data[p_off + 64 + i * 64:p_off + 128 + i * 64], 16))
                    for i in range(pn)]
            amounts = [int(data[a_off + 64 + i * 64:a_off + 128 + i * 64], 16)
                       for i in range(an)]
        except Exception:  # noqa: BLE001 - 配列構造が読めないものは落とす
            return None
        if pn >= 2 and an >= 2:
            tin, tout = path[0], path[-1]
            ain, aout = amounts[0], amounts[-1]
    elif st == "flag_2d72":
        # d5 フラグで入出欄が入れ替わる形式（2d720abb 型）
        tin = _addr_of(w[3])
        if w[5] == 1:
            ain, aout = w[1], w[0]
        else:
            ain, aout = w[0], w[1]
        _, gross = flows()
        tout = _match_list([(t, a) for t, a in gross["out"] if t != tin], aout)
    if tin and tout and ain and aout and ain > 0 and aout > 0:
        return (pool, spec["kind"], tin, ain, tout, aout)
    return None


def _trader_flows(rec, trader):
    out = []
    for log in rec.get("logs", []):
        ts = log.get("topics") or []
        if ts and ts[0].lower() == TRANSFER and len(ts) >= 3 \
                and len(log.get("data") or "") > 2:
            amt = int(log["data"], 16)
            frm = "0x" + ts[1][-40:]
            to = "0x" + ts[2][-40:]
            if frm == trader or to == trader:
                out.append((log["address"].lower(), amt, frm, to))
    return out


def main():
    chunk_path, ledger_path = sys.argv[1], sys.argv[2]
    src = sqlite3.connect(f"file:{chunk_path}?mode=ro", uri=True)
    blocks = {n: json.loads(j) for n, j in
              src.execute("SELECT block_number, raw_json FROM blocks")}
    receipts = {h: json.loads(j) for h, j in
                src.execute("SELECT tx_hash, receipt_json FROM receipts")}
    first, last = min(blocks), max(blocks)

    out = sqlite3.connect(ledger_path)
    out.executescript("""
    CREATE TABLE IF NOT EXISTS ledger_tx(
      hash TEXT PRIMARY KEY, block_number INTEGER, tx_index INTEGER,
      from_addr TEXT, to_addr TEXT, status INTEGER, gas_used INTEGER,
      gas_price INTEGER, n_legs INTEGER);
    CREATE TABLE IF NOT EXISTS legs(
      tx_hash TEXT, leg_index INTEGER, pool TEXT, kind TEXT,
      token_in TEXT, amount_in REAL, token_out TEXT, amount_out REAL,
      PRIMARY KEY(tx_hash, leg_index));
    CREATE TABLE IF NOT EXISTS token_meta(address TEXT PRIMARY KEY, decimals INTEGER);
    CREATE TABLE IF NOT EXISTS pool_meta(address TEXT PRIMARY KEY, token0 TEXT, token1 TEXT);
    CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
    """)

    # --- pass 1: collect swap events per tx (no RPC yet) ---
    raw_events = {}          # tx hash -> list of event dicts
    pair_pools = defaultdict(set)   # resolver -> pools
    swap_events = 0
    for b in sorted(blocks):
        for tx in blocks[b]["transactions"]:
            h = tx["hash"].lower()
            rec = receipts.get(h)
            evs = []
            if rec:
                for log in rec.get("logs", []):
                    topics = log.get("topics") or []
                    if not topics:
                        continue
                    t0 = topics[0].lower()
                    if t0 not in FORMATS:
                        continue
                    kind, family = FORMATS[t0]
                    swap_events += 1
                    pool = log["address"].lower()
                    ev = {"kind": kind, "family": family, "pool": pool,
                          "topics": topics, "words": words_of(log),
                          "log": log}
                    if family in ("v2style", "v3style"):
                        pair_pools["pair01"].add(pool)
                    elif family == "metric":
                        pair_pools["immutables"].add(pool)
                    elif family == "maverick":
                        pair_pools["tokenab"].add(pool)
                    elif family == "forte":
                        pair_pools["xy"].add(pool)
                    elif family == "v2shift":
                        pair_pools["pair01"].add(pool)
                    evs.append(ev)
            raw_events[h] = evs

    # --- prefetch pool token pairs per resolver + decimals ---
    cached_pools = {a: (t0, t1) for a, t0, t1 in
                    out.execute("SELECT address, token0, token1 FROM pool_meta")}
    # Shared pairs are keyed "resolver:pool": a pair is only ever reused
    # for the same resolver that produced it, because resolvers differ
    # in convention (token0/token1 vs tokenA/tokenB vs tokenX/tokenY).
    # Singleton poolIds are NOT shared: their pairs are flow-derived
    # per chunk and are not stable facts.
    _shared_pairs, _shared_dec = load_shared_cache()

    def fetch_pair01(pool):
        return pool, (call_address(pool, "0x0dfe1681").lower(),
                      call_address(pool, "0xd21220a7").lower())

    def fetch_immutables(pool):
        try:
            r = eth_call(pool, "0xbcdb4dad")
            ws = [r[2 + i * 64:2 + (i + 1) * 64]
                  for i in range((len(r) - 2) // 64)]
            return pool, ("0x" + ws[1][-40:], "0x" + ws[2][-40:])
        except Exception:  # noqa: BLE001 - some emitters lack getImmutables
            return fetch_pair01(pool)

    def fetch_tokenab(pool):
        return pool, (call_address(pool, "0x0fc63d10").lower(),
                      call_address(pool, "0x5f64b55b").lower())

    def fetch_xy(pool):
        return pool, (call_address(pool, "0x088b699e").lower(),
                      call_address(pool, "0x49bfcca1").lower())

    fetchers = {"pair01": fetch_pair01, "immutables": fetch_immutables,
                "tokenab": fetch_tokenab, "xy": fetch_xy}
    failed_pools = set()
    for resolver, pools in pair_pools.items():
        for p in pools:
            key = f"{resolver}:{p}"
            if p not in cached_pools and key in _shared_pairs:
                pair = tuple(_shared_pairs[key])
                cached_pools[p] = pair
                out.execute("INSERT OR REPLACE INTO pool_meta VALUES (?,?,?)",
                            (p, pair[0], pair[1]))
        todo = [p for p in pools if p not in cached_pools]
        if not todo:
            continue
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(fetchers[resolver], p): p for p in todo}
            for fut, pool in futs.items():
                try:
                    _, pair = fut.result()
                except Exception:  # noqa: BLE001 - one bad pool must not
                    failed_pools.add(pool)  # kill the whole chunk
                    continue
                cached_pools[pool] = pair
                _shared_pairs[f"{resolver}:{pool}"] = list(pair)
                out.execute("INSERT OR REPLACE INTO pool_meta VALUES (?,?,?)",
                            (pool, pair[0], pair[1]))
    out.commit()

    tokens = {ETH0}
    for t0, t1 in cached_pools.values():
        tokens.update((t0, t1))
    # receipt-resolved tokens (singletons, lunarbase) join after pass 2a;
    # collect them now from flows of txs that carry such events
    for h, evs in raw_events.items():
        if any(e["family"] in ("singleton", "lunarbase") for e in evs):
            for (holder, tok) in receipt_flows(receipts[h]):
                tokens.add(tok)
    cached_dec = {a: d for a, d in
                  out.execute("SELECT address, decimals FROM token_meta")}
    cached_dec[ETH0] = 18
    for _a, _d in _shared_dec.items():
        if _d is not None:
            cached_dec.setdefault(_a, _d)

    def fetch_dec(addr):
        try:
            return addr, int(eth_call(addr, "0x313ce567"), 16)
        except Exception:  # noqa: BLE001
            return addr, None

    with ThreadPoolExecutor(max_workers=8) as ex:
        for addr, dec in ex.map(fetch_dec,
                                [t for t in tokens if t not in cached_dec]):
            cached_dec[addr] = dec
            out.execute("INSERT OR REPLACE INTO token_meta VALUES (?,?)",
                        (addr, dec))
    out.commit()

    def human(raw, token):
        dec = cached_dec.get(token)
        return None if dec is None else raw / (10 ** dec)

    def resolve_singleton(ev, rec):
        """Return (pool_key, currency0, currency1) or None."""
        custodian = PM_V4 if ev["kind"] == "v4" else VAULT_INFINITY
        a0 = signed(ev["words"][0] & ((1 << 128) - 1), 128)
        a1 = signed(ev["words"][1] & ((1 << 128) - 1), 128)
        net = receipt_flows(rec)
        cand = {}
        for (holder, tok), v in net.items():
            if holder != custodian or v == 0:
                continue
            if v == -a0:
                cand.setdefault(0, set()).add(tok)
            if v == -a1:
                cand.setdefault(1, set()).add(tok)
        cur = {}
        for slot in (0, 1):
            cs = cand.get(slot, set())
            if len(cs) == 1:
                cur[slot] = next(iter(cs))
        if len(cur) == 2:
            if cur[0] == cur[1]:
                return None
            return ev["topics"][1].lower(), cur[0], cur[1]
        if len(cur) == 1:
            # the unmatched side is native ETH (no Transfer flow)
            slot = next(iter(cur))
            pair = [ETH0, ETH0]
            pair[slot] = cur[slot]
            return ev["topics"][1].lower(), pair[0], pair[1]
        return None

    # --- pass 2: build legs ---
    n_tx = n_legs = skipped = 0
    legs_by_kind = Counter()
    for b in sorted(blocks):
        for tx in blocks[b]["transactions"]:
            h = tx["hash"].lower()
            rec = receipts.get(h) or {}
            legs = []
            for ev in raw_events.get(h, []):
                kind, family, pool, w = ev["kind"], ev["family"], ev["pool"], ev["words"]
                if family not in ("singleton", "lunarbase", "pmm",
                                  "adapter") \
                        and pool not in cached_pools:
                    skipped += 1
                    continue
                leg = None
                if family == "v2style":
                    t0, t1 = cached_pools[pool]
                    a0i, a1i, a0o, a1o = w[0], w[1], w[2], w[3]
                    if a0i > 0 and a1o > 0:
                        leg = (pool, kind, t0, a0i, t1, a1o)
                    elif a1i > 0 and a0o > 0:
                        leg = (pool, kind, t1, a1i, t0, a0o)
                elif family in ("v3style", "metric"):
                    if family == "metric" and len(ev["topics"]) != 3:
                        # Same signature string, non-indexed variant
                        # (e.g. logger 0x71c2ed90...): not a Metric
                        # pool swap. Genuine Metric Swap has exactly
                        # 3 topics (2 indexed addresses).
                        continue
                    t0, t1 = cached_pools[pool]
                    if family == "v3style":
                        a0, a1 = signed(w[0], 256), signed(w[1], 256)
                    else:
                        packed = w[1]
                        a0 = signed(packed >> 128, 128)
                        a1 = signed(packed & ((1 << 128) - 1), 128)
                    if a0 > 0 and a1 < 0:
                        leg = (pool, kind, t0, a0, t1, -a1)
                    elif a1 > 0 and a0 < 0:
                        leg = (pool, kind, t1, a1, t0, -a0)
                elif family == "pmm":
                    # topics[1]=tokenIn, topics[2]=tokenOut,
                    # w0=amountIn, w1=amountOut (vault-settled;
                    # verified 66/69 events exact vs the PMM vault,
                    # remaining 3 in one multi-leg tx).
                    ts = ev["log"]["topics"]
                    if len(ts) >= 3 and len(w) >= 2 and w[0] > 0 and w[1] > 0:
                        leg = (pool, kind, "0x" + ts[1][-40:], w[0],
                               "0x" + ts[2][-40:], w[1])
                elif family == "forte":
                    xt, yt = cached_pools[pool]
                    tin = "0x" + ev["log"]["data"][2:66][-40:]
                    tout = yt if tin == xt else xt
                    leg = (pool, kind, tin, w[1], tout, w[2])
                elif family == "maverick":
                    ta, tb = cached_pools[pool]
                    if w[3] != 0:
                        leg = (pool, kind, ta, w[6], tb, w[7])
                    else:
                        leg = (pool, kind, tb, w[6], ta, w[7])
                elif family == "singleton":
                    r = resolve_singleton(ev, rec)
                    if r is None:
                        skipped += 1
                        continue
                    pkey, c0, c1 = r
                    cached_pools.setdefault(pkey, (c0, c1))
                    out.execute("INSERT OR REPLACE INTO pool_meta VALUES (?,?,?)",
                                (pkey, c0, c1))
                    a0 = signed(w[0] & ((1 << 128) - 1), 128)
                    a1 = signed(w[1] & ((1 << 128) - 1), 128)
                    # swapper perspective: negative = paid in
                    if a0 < 0 and a1 > 0:
                        leg = (pkey, kind, c0, -a0, c1, a1)
                    elif a1 < 0 and a0 > 0:
                        leg = (pkey, kind, c1, -a1, c0, a0)
                elif family == "lunarbase":
                    xtoy = w[1] != 0
                    dx, dy = w[2], w[3]
                    amt_in_exp = dx if xtoy else dy
                    amt_out_exp = dy if xtoy else dx
                    net = receipt_flows(rec)
                    tin = tout = None
                    for (holder, tok), v in net.items():
                        if holder != pool:
                            continue
                        if v == amt_in_exp and amt_in_exp > 0:
                            tin = tok
                        if v == -amt_out_exp and amt_out_exp > 0:
                            tout = tok
                    if tin is None and tout is None:
                        skipped += 1
                        continue
                    leg = (pool, kind, tin or ETH0, amt_in_exp,
                           tout or ETH0, amt_out_exp)
                elif family == "v2shift":
                    # v2style の word 位置が1つずれた形式（txOrigin 先頭）
                    t0, t1 = cached_pools[pool]
                    a0i, a1i, a0o, a1o = w[1], w[2], w[3], w[4]
                    if a0i > 0 and a1o > 0:
                        leg = (pool, kind, t0, a0i, t1, a1o)
                    elif a1i > 0 and a0o > 0:
                        leg = (pool, kind, t1, a1i, t0, a0o)
                elif family == "adapter":
                    leg = adapter_leg(ev, rec)
                    if leg is None:
                        skipped += 1
                if leg is not None:
                    legs.append(leg)
                    legs_by_kind[kind] += 1
            out.execute(
                "INSERT OR REPLACE INTO ledger_tx VALUES (?,?,?,?,?,?,?,?,?)",
                (h, b, tx.get("transactionIndex"),
                 (tx.get("from") or "").lower(), (tx.get("to") or "").lower(),
                 hexint(rec.get("status")), hexint(rec.get("gasUsed")),
                 hexint(rec.get("effectiveGasPrice")), len(legs)))
            for i, (pool, kind, tin, ain, tout, aout) in enumerate(legs):
                out.execute(
                    "INSERT OR REPLACE INTO legs VALUES (?,?,?,?,?,?,?,?)",
                    (h, i, pool, kind, tin, human(ain, tin),
                     tout, human(aout, tout)))
                n_legs += 1
            n_tx += 1
    out.executemany("INSERT OR REPLACE INTO meta VALUES (?,?)", [
        ("first_block", str(first)), ("last_block", str(last)),
        ("tx_count", str(n_tx)), ("leg_count", str(n_legs)),
        ("swap_events", str(swap_events)),
    ])
    out.commit()
    # Keep the ledger self-contained: decimals served from the shared
    # cache were never inserted by the fetch loop, so write back this
    # chunk's token set (the same set a cold-cache run would have
    # stored; ETH0 is excluded, as a cold run never inserts it).
    for _a in tokens:
        if _a != ETH0 and cached_dec.get(_a) is not None:
            out.execute("INSERT OR REPLACE INTO token_meta VALUES (?,?)",
                        (_a, cached_dec[_a]))
    out.commit()
    out.close()

    # --- self-check: independent recount of format topics in receipts ---
    recount = sum(
        1 for rec in receipts.values() for log in rec.get("logs", [])
        if (log.get("topics") or [""])[0].lower() in FORMATS)

    # --- unknown-topic residue (registry: known_topics.json, if present) ---
    unknown_topics = []
    tx_with_unknown = 0
    reg_path = Path(__file__).with_name("known_topics.json")
    if reg_path.exists():
        known = set(json.loads(reg_path.read_text())["topics"])
        ev_c = Counter()
        txs_of = defaultdict(set)
        for h, rec in receipts.items():
            here = set()
            for log in rec.get("logs", []):
                ts = log.get("topics") or []
                if not ts:
                    continue
                t0 = ts[0].lower()
                if t0 not in known:
                    ev_c[t0] += 1
                    here.add(t0)
            for t0 in here:
                txs_of[t0].add(h)
            if here:
                tx_with_unknown += 1
        unknown_topics = [
            {"topic": t0, "events": ev_c[t0], "txs": len(txs_of[t0])}
            for t0 in ev_c if ev_c[t0] >= 5]
        unknown_topics.sort(key=lambda r: -r["txs"])
        unknown_topics = unknown_topics[:20]

    manifest = {
        "first_block": first, "last_block": last, "tx": n_tx,
        "legs": n_legs, "swap_events": swap_events,
        "swap_events_recount": recount,
        "legs_by_kind": dict(legs_by_kind),
        "skipped_unresolved": skipped,
        "chunk_bytes": os.path.getsize(chunk_path),
        "ledger_bytes": os.path.getsize(ledger_path),
        "tx_with_unknown_topic": tx_with_unknown,
        "unknown_topics": unknown_topics,
    }
    with open(ledger_path + ".manifest.json", "w") as f:
        json.dump(manifest, f, indent=1)
    save_shared_cache(_shared_pairs, cached_dec)
    print(json.dumps(manifest, indent=1))
    if recount != swap_events:
        print("SELF-CHECK FAILED: swap event recount mismatch", file=sys.stderr)
        sys.exit(1)
    if n_tx != len(receipts):
        print("SELF-CHECK FAILED: tx/receipt count mismatch", file=sys.stderr)
        sys.exit(1)
    print("SELF-CHECK OK")


if __name__ == "__main__":
    main()
