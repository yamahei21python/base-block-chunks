# 名簿 DB（R1）構築キット

このフォルダの資料だけで、名簿 DB `registry.db` をローカルに再構築できます。
設計の正本は `DESIGN.md`、スキーマは `schema.sql` です。DB が作業マスターで、
4帳 JSON は DB からの輸出形態という位置づけです。

## 手順

### 1. 名簿4帳の用意

`import_registries.py` 冒頭のパス定数（TOPICS / CONTRACTS / POOLS）を、
手元のファイルに合わせて書き換えてください。使う版は次の通りです。

| 帳 | 版 | 取得元 |
|---|---|---|
| topics | v2026-11-05.29（7,559件） | 本リポジトリ `scripts/known_topics.json` |
| contracts | v2026-11-05.24（293件） | 本リポジトリ `scripts/known_contracts.json` |
| pools | v2026-11-08.2（6,328プール / decimals 5,565） | defi-wallet-analyzer リポジトリ `adapter/materials/known_pools.json` |

注意: 本リポジトリの `scripts/known_pools.json` は BBC 側の版で、DB 移入に使った
defi 側 v2026-11-08.2 とは別物です。プールだけ defi 側を使ってください。

署名の keccak 照合には node + ethers を使います（ETHERS 定数も書き換え）。
ethers が無い環境では照合だけが動きません。

### 2. DB 構築と名簿移入

```bash
python3 import_registries.py
```

`registry.db` が生成され、topics 7,559 / pools 6,328（主張は priority=30）/
tokens 5,565＋ネイティブ番兵 / contracts 293 が入ります。

### 3. 事実層（4表）の投入

形状・発行元・証拠・資金流の4表は、生チャンクからの抽出 partials（JSON）を
投入して作ります。partials の取得元は2つです。

- リリース `facts-extract` の資産 `facts-partials.tar.gz`（101チャンク分）。
  `.github/workflows/facts-extract.yml` が `data-*` リリースの生チャンクを
  Actions 上で抽出して作ったものです。再生成は workflow_dispatch でできます
  （スキップ指定は `scripts/facts_skip.txt`）。
- ブランチ `facts-extra` の `registry-db/facts-extra/` にある20ファイル
  （凍結範囲 53525000–53526999 の partials。この範囲の生チャンクは
  GitHub 上に無いため、partials 自体を置いています）。

両方を `facts-out/` フォルダに集めてから:

```bash
python3 load_facts.py
```

検収ゲート（形状と発行元の合計照合ほか）を通過した分だけが投入されます。
投入済み partial は `facts-out/.loaded.json` に記録され、再実行しても
二重計上しません。全121チャンク・10,981,526ログ分を投入した状態で
4表のカバー率は 7,425 / 7,559 トピック（98.2%）です。

### 4. v4 Initialize 正本（保留10件の決着用）

`load_initialize_targeted.py` が、対象 poolId の Initialize ログ
（HyperSync で取得した targeted.jsonl）から priority=100 の主張を投入し、
名簿との照合レポートを出します。取得自体は Muse 側の環境で実行中です。

## 再現性についての注意

- 資金流（topic_flow）の amount_match は 'unmeasured' です。構造的な
  事実のみで、金額照合は形式ごとのデコーダの仕事として分離しています。
- 未登録のまま観測されたトピック（1,355セル分）は投入されず、件数だけが
  報告されます。名簿への追加判断は別途行ってください。
