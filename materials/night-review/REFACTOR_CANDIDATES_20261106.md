# 今夜の GLM リファクタ候補リスト（2026-11-06 Muse 棚卸し）

出どころ: (A) REFACTORING_PROPOSAL.md（ブランチ night/20261105-goal1、P1–P3 全11件）
(B) AUDIT_REPORT.md（2026-11-04 全監査）の §4 リファクタリング / §5 構造変更。
分類器の調整2件は日中に終わる前提なので、分類器本体に触る候補も対象に含めた。
状態の根拠: ARCHITECTURE.md の判断メモ・モジュール表、main のツリー、本日の実測。
「要確認」としたものは、着手前に実コードで現状確認が必要。

## 第1群: そのまま出せる（提案書 P1、検収済み成果物の整理）

1. ADAPTER_READERS を同梱 JSON に分離（decode_chunk.py 2,610行 → 約820行）+ manifest に readers sha256
2. manifest / 台帳 meta に版情報を書く（decoder_version・名簿の版・schema_version）
3. Transfer 走査の一本化（3実装 → 1ジェネレータ）
4. adapter-work のリポ加入整理（78.77 MB の肥大解消、名簿 JSON の単一ソース化）

## 第2群: 提案書 P2–P3（P1 の後、または独立に切れるもの）

5. family リーダーの統一インターフェイス化
6. skip 理由の構造化（4箇所に散在する skipped += 1 に理由を持たせる）
7. BBC への正式取り込み（文字列手術ビルドの廃止） ※BBC 側の変更を伴う
8. テストハーネスの CLI 化
9. WIN-13 採点スクリプトの入力契約（/tmp ハードコード解消）
10. GT 作成ツールをリポに保存（現在 /tmp のみで再現不可）
11. 材料ブランチ参照の安定化

## 第3群: 監査 §4 の残件（未実施が濃厚なもの）

12. **S1–S5 の重複統合**: ExitManager ×5 → BaseExitManager + ルール合成、
    スコア関数 ×5 → 共通骨格、backtest の gate 基準を evaluate_trades_gate に一本化、
    loader.py の S1–S5 設定ローダ ×5（約430行）→ 汎用ローダ
13. **コストモデル三重実装の統一**（backtest 独自 / paper/execution / mev/cost_calculator
    → cost_model + amm_impact + latency_model 経由に）
14. **デッドコードの処遇決定と実施**: tracker/quality_scorer.py（本番未接線）、
    mev/checkpoint.py（本流未使用）、tracker/ledger.py（孤島）、tracker/pnl.py（v1、
    降格か削除）、mev/clients/rpc.py（実使用ゼロ）など — 接線するか削除するかを
    1件ずつ決めて実施
15. **型・契約の整備**: DataAdapter から fetch を外し AddressSource/TxSource/
    BalanceSource に分割（ISP）、ClassificationResult の str 継承ハック解消、
    duck-typing（hasattr 三連）の Protocol 化
16. **細部の衛生**: マジックナンバーの外部化（s2: 0.80、s4: 0.30 等）、ロガー取得の
    統一、定数・静的マップの単一ソース化、流動性正規化の重複7行 ×4 の共通化

## 第4群: 監査 §4 で実施済み（候補から除外）

- db/repository.py の分割 → repository/ パッケージ化済み
- スワップデコードの一本化 → src/evm/topics.py + swap.py に統合済み（keccak 検証テスト付き）
- pnl_v2 のパッケージ分割（1157行 → パッケージ）済み。価格解決チェーンの
  PriceOracle/Resolver 分離も実施済み（src/pricing/ が正本）
- crawler/service.py の mixin 分割（882行 → dataclass + mixin）済み。
  ただし提案されていた完全な責務分離（SourceSelector / CsvWriter / Presenter 等）は
  未完の可能性があり **要確認**

## 第5群: 構造変更（リファクタより「新設」に近く、今夜の枠には大きめ）

監査 §5 の14件。比較的小さく切れるのは次の2件:

17. MEV パイプラインのステージ化（Discovery→Enrichment→Parse→Valuation→Cost→Persist
    を dataclass 受渡しの明示ステージに。水位は pipeline_state テーブルへ）
18. 検証ハーネスの一元化（walk_forward/regime/benchmark/gate を run_validation に）

残り（注文パイプライン新設、ポートフォリオ状態の永続化、EventBus 化、戦略レジストリ、
ExecutionSimulator 統合、WebSocket 基盤、収集パイプライン化、スキーマ設計、
設定の型付き一元化、HyperSync logs 直取得、価格系統合、UoW 導入、マイグレーション
一本化の残り）は、設計判断を伴うので今夜の無人ランには入れず、別途の設計対象とする
のがよい。

## 監査 §3 のバグ全リストについて

サブシステム別のバグ一覧（§3.1–3.6）はリファクタではなく修正作業。AIZ 系の修正
バッチ（CHANGES_AIZ_* 等）で相当数が消化されたはずだが、1件ごとの残存確認は
していない。今夜の対象にするなら別途棚卸しが要る。

## 今夜の推奨バンドル（Muse 案）

- 本命: 第1群（提案書 P1 の4件）+ 第3群の 14（デッドコード処遇）と 12（S1–S5 統合）
- 余力で: 第2群の 9・10（WIN-13/GT 道具のリポ保存 — 分類器調整の受け入れにも効く）
- 入れない: 第5群（設計判断が要る）、分類器の仕様に触る変更（調整2件の検収が先）
