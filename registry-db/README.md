# 名簿 DB（R1）構築キット

名簿 DB `registry.db` をローカルに再構築するためのキットです。
設計の正本は `DESIGN.md`、スキーマは `schema.sql` です。DB が作業マスターで、
4帳 JSON は DB からの輸出形態という位置づけです。

## 前提

- `base-block-chunks` と `defi-wallet-analyzer` の**両方**の読み取り権限。
  4帳のうち pools だけが defi 側（PRIVATE リポジトリ）にあります
- python3（標準ライブラリのみで動きます）
- node + ethers（署名の keccak 照合用。**無くても構築はできます**が、
  その場合は警告が出て `topic_signature.keccak_matches` が NULL（未照合）
  になります。下記の版検証と件数確認は ethers の有無と無関係に効きます）

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

### 2. DB 構築と名簿移入

```bash
python3 import_registries.py
```

`registry.db` が生成されます。スクリプトは各帳の**版を検証**し、期待と
違えば WARNING を出します（版違いはエラーにならず件数だけが変わるので、
必ず確認してください）。完了時に次の件数が出ることが正しい状態です:

```
topics: 7559
pools: 6328
tokens: 5565
contracts: 293
```

pools の主張は priority=30 で入ります。tokens の 5,565 は decimals 付きで、
ほかにネイティブ番兵（ETH）が1件あります。

### 3. 事実層（4表）の投入

形状・発行元・証拠・資金流の4表は、生チャンクからの抽出 partials（JSON）を
投入して作ります。partials の取得元は2つです。

- リリース `facts-extract` の資産 `facts-partials.tar.gz`（101チャンク分）。
  `.github/workflows/facts-extract.yml` が `data-*` リリースの生チャンクを
  Actions 上で抽出して作ったものです。再生成は workflow_dispatch でできます
  （スキップ指定は `scripts/facts_skip.txt`）。

  **展開後に必ず件数を確認してください。** `tar` がエラーを出さずに
  途中で止まることがあり、欠けたまま進むとカバー率だけが静かに下がります。

  ```bash
  tar xzf facts-partials.tar.gz -C facts-out/
  ls facts-out/*.json | wc -l     # この時点で 101 であること
  ```

- ブランチ `facts-extra` の `registry-db/facts-extra/` にある20ファイル
  （凍結範囲 53525000–53526999 の partials。この範囲の生チャンクは
  GitHub 上に無いため、partials 自体を置いています）。取得は**生データ**
  で行ってください（API の `.content` は改行入り base64 で、そのまま
  デコードすると壊れます）:

  ```bash
  for f in $(gh api "repos/yamahei21python/base-block-chunks/contents/registry-db/facts-extra?ref=facts-extra" --jq '.[].name'); do
    gh api -H "Accept: application/vnd.github.raw" \
      "repos/yamahei21python/base-block-chunks/contents/registry-db/facts-extra/$f?ref=facts-extra" \
      > "facts-out/$f"
  done
  ```

  あるいは sparse-checkout でも取得できます
  （`git clone --filter=blob:none --sparse` 後に
  `git sparse-checkout set registry-db/facts-extra`、
  `git checkout facts-extra`）。

両方合わせて **121 件**になるのが正しい状態です。集めてから:

```bash
python3 load_facts.py
```

検収ゲート（形状と発行元の合計照合ほか）を通過した分だけが投入されます。
投入済み partial は `facts-out/.loaded.json` に記録され、再実行しても
二重計上しません。全121チャンク・10,981,526ログ分を投入した状態で
4表のカバー率は 7,425 / 7,559 トピック（98.2%）です。投入の最後に
カバー率が表示されるので、この値と見比べてください（低い場合は
partials の不足です）。

### 4. v4 Initialize 正本（保留10件の決着用）

`load_initialize_targeted.py` が、対象 poolId の Initialize ログ
（HyperSync で取得した targeted.jsonl）から priority=100 の主張を投入し、
名簿との照合レポートを出します。取得自体は Muse 側の環境で実行中です。

## topic_trap について

`topic_trap` は構築時点では **0 件** です。これは未投入ではなく、
同定作業の中で「罠だと検出されたもの」を証拠テキスト付きで記録する
表だからです（証拠なしの行は制約で入りません）。facts 層からは
rare_occurrence や indexed_variant のような構造系の**候補**は機械的に
出せますが、罠として確定させるにはデコードが実際に誤読を招くことの
検証が要るため、候補を自動投入する作りにはしていません。

## 再現性についての注意

- 資金流（topic_flow）の amount_match は 'unmeasured' です。構造的な
  事実のみで、金額照合は形式ごとのデコーダの仕事として分離しています。
- 未登録のまま観測されたトピック（1,355セル分）は投入されず、件数だけが
  報告されます。名簿への追加判断は別途行ってください。
