# 夜間作業指示書 B: 収益化前段パイロット（価格・勝ちパターン道具・検証ハーネス）

## 0. 基本原則・制約（厳守）

- 作業ブランチ: main 最新から `night/20261106-pilots-b` を切る。main への直接 push は
  しない。完了したら PR を作る（マージはしない）
- **分類器の不可侵**: `src/mev/classifier/` は変更しない。分類器を「実行して出力を使う」
  ことは可（ボットリストの生成など）
- 数値規律: 報告する全ての数値に導出スクリプトを添付すること（再現できない数値は
  書かない）。価格が取れないものは推測で埋めず NULL / unverified とする
- スコープ境界: リアルタイム実行系（Step 5/6: WebSocket 常時接続、実行コントラクト）
  には着手しない。`src/strategy/`・`src/config/`・`src/tracker/` と BBC のスクリプトには
  触らない（リファクタは別タスク A の担当）
- テスト強制: `poetry run pytest -q` を常に全件パスさせること
- コミットは作業項目ごとに分け、進捗ファイル `NIGHT_B_PROGRESS.md` で再開可能にすること
- 台帳データ: GitHub リポジトリ yamahei21python/base-block-chunks のリリース資産
  （ledger）を使う

## B-1. 価格カバー率パイロット（Step 3 のフィージビリティ）

目的: 「全面展開の前に、価格がどの程度付くか」を定量把握する。

- 対象: 範囲 **53524000–53524999** の台帳（1,000ブロック）
- **重要な前提**: 既存の `src/pricing/resolver.py` は現行価格の解決チェーンであり、
  ここで測るのは「この範囲のトークン群が今の価格系で解決できるか」という代理指標
  であって、ブロック時点の履歴カバー率ではない。レポートにはこの区別を明記すること
- 手順: まず対象範囲のユニークトークン数を数え、価格照会は**トークン単位に集約**
  すること（レグ単位で API を叩かない）。API のレート制限に当たったら無理に
  続けず、到達範囲を明記する
- 成果物:
  - `scripts/pilot_price_coverage.py`
  - `reports/pricing_pilot_report.md`: レグ全体の価格解決率（%）、トークン別の
    解決/未解決内訳、解決できた範囲での暫定の金額差分分布（**ガス控除前の gross**
    であることを明記。net_profit と呼ばない）

## B-2. 勝ちパターン分析ツールの骨格（Step 2 の道具作り）

目的: 確定ボットの取引パターンを4軸で集計するツールを準備する。今夜は道具と
暫定実行までで、パターンの優劣に関する**結論は書かない**（本分析は分類器の
ゲート通過後の確定名簿で行う）。

- `scripts/analyze_winners.py` を新設。入力は「台帳 DB + ボットリストファイル（引数）」
  とし、将来の全量実行にそのまま使える形にする
- 暫定実行の範囲: ホールドアウト2範囲（53523000–53523999 / 53524000–53524999）の
  台帳と、各範囲で分類器を実行して得たフラグ主体リスト
- 集計4軸:
  1. 経路トポロジー: 2-hop 循環 / 3-hop 三角 / 4+ hop / 分割経路の件数・比率
  2. 分割（split-route）の利用頻度・分岐数の分布
  3. 速度・位置: ブロック内 tx インデックス（ledger_tx.tx_index）の分布
  4. ガス軸: **台帳にはガス情報が無い**。対象範囲から上限300件のサンプル tx に
     ついてレシート（eth_getTransactionReceipt）を取得して実測するか、未実施と
     明記するかのどちらかを必須とする。台帳から計算したように見せないこと
- 成果物: ツール本体 + `reports/provisional_winner_patterns.md`（暫定値と明記）

## B-3. 検証ハーネスの一元化（Step 4 の下ごしらえ）

- `src/backtest/` の walk_forward / regime / benchmark / gate / amm_impact /
  latency_model を束ねる統合エントリ `run_validation(trades, config) -> ValidationReport`
  を実装する（AUDIT_REPORT §5.9）
- ハーネス側で一元的に強制すること: 合成データの禁止、NaN は fail-closed、
  必須フィールドの検証、最小サンプル数
- 既存 gate の基準値（PF > 1.3、MDD < 15%、Sharpe > 1.0 等）は config の値のまま
  移すこと。基準の意味や値を変えない
- 成果物: 実装 + 単体テスト `tests/unit/test_validation_harness.py`

## 提出物

- PR（ブランチ `night/20261106-pilots-b`）
- `reports/pricing_pilot_report.md`、`reports/provisional_winner_patterns.md`
- 作業サマリ（新規スクリプトの実行方法を含む）
- 数値は全て暫定値として扱う（最終計測と検収は Muse が自分の環境で行う）
