# base-block-chunks

Fetch Base mainnet blocks into portable SQLite chunks, using only public
RPC endpoints (no API keys). Data is public chain data.

## Usage

Run the **fetch-blocks** workflow (Actions → fetch-blocks → Run workflow)
with a first and last block number. The range is split into chunks of at
most 100 blocks. Each chunk is published as a release asset:

- `chunk_<first>_<last>.db.gz` — the chunk database, gzipped
- `chunk_<first>_<last>.db.manifest.json` — block/tx/receipt counts,
  parent-hash linkage result, and the sha256 of the uncompressed DB

Releases are tagged `data-<first>-<last>`.

Or run the fetcher directly:

```
python3 scripts/fetch_chunk.py --first 53440500 --last 53440599 --out chunk.db
```

It is resumable: blocks already in the DB are skipped, and every block is
committed individually.

## Chunk schema

```sql
blocks(block_number INTEGER PRIMARY KEY, raw_json TEXT)       -- full block JSON
transactions(hash TEXT PRIMARY KEY, block_number INTEGER,
             tx_index INTEGER, raw_tx TEXT)                   -- raw_tx is NULL for 0x7e deposit txs
receipts(tx_hash TEXT PRIMARY KEY, receipt_json TEXT)         -- full receipt JSON
```

## Merging chunks

Chunks merge into one database with plain SQLite:

```sql
ATTACH 'chunk_53440500_53440599.db' AS c;
INSERT OR REPLACE INTO blocks SELECT * FROM c.blocks;
INSERT OR REPLACE INTO transactions SELECT * FROM c.transactions;
INSERT OR REPLACE INTO receipts SELECT * FROM c.receipts;
DETACH c;
```
