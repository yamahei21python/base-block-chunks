# 最小アダプター実装仕様（確定87形式 → decode_chunk.py）

## 目的

`scripts/decode_chunk.py`（base-block-chunks）の `FORMATS` に、同定済みの swap 形式87件を追加し、
これまで解読できなかった取引からも台帳の legs（トークンと量）を出せるようにする。
精密化はしない。分類器がトークンの流れを見られる精度（＝最小アダプター）が目標。

## 材料

- `formats_accepted.json` — 87形式の定義（topic → name / indexed / data。indexed・data は各グループ内の宣言順）
- `samples_adapter.json` — 形式ごとの検証サンプル（topic → [{tx, target_logs, transfers}]）。
  target_logs は対象イベントの生ログ、transfers は同一レシート内の全 Transfer（token/from/to/value）。

## 実装規則

1. **既存 family に寄せられるものは寄せる。** 形式の読み位置（indexed の割当てと data ワード順）が
   既存 family（v2style / v3style / metric / forte / maverick / singleton / pmm / lunarbase）の
   どれかと完全に一致する場合は、`FORMATS` への1行追加で済ませる。
   一致しない形式は形式専用の抽出分岐を足す。**無理に既存 family へ寄せて読み位置をごまかさない。**
2. **ペア解決は既存の流儀に従う。** プールから token0/token1 等が読める形式は既存 resolver
   （pair01 / immutables / tokenab / xy）を使い、共有キャッシュへは resolver キー付きで登録する。
   フローからしか決まらない形式は singleton / lunarbase の流儀（receipt_flows）を使う。
   singleton 由来のペアを共有キャッシュへ昇格させない（従来規則どおり）。
3. **量の向きと符号はサンプルの Transfer で厳密に決める。** イベント内の数値と Transfer 実数が
   一致するのが原則。一致しない形式（手数料差し引き等）は、その構造を NOTES に明記し、
   台帳には Transfer 実数と整合する向き・量を入れる。概算や推測値で埋めない。
4. **動的配列形式**（`0xe1010d0a…` Swap(address[],uint256[],address)）は、data のオフセットと
   長さを辿って配列要素から読む。サンプルで形が確認できている。
5. **実装できない形式は、理由を書いて未実装のまま報告する。** 黙って落とさない。
   形式ごとに「実装した／見送った（理由）」を NOTES.md に一覧で残す。

## テスト（納品条件）

- 形式ごとに、`samples_adapter.json` の全サンプルを使って次を assert するテストを書く：
  (a) 対象ログから leg が生成される、(b) leg の token_in / token_out が Transfer 由来のペアと一致する、
  (c) leg の量が Transfer 実数と一致する（符号規約込み）。
- 既存形式の回帰：既存の台帳チャンクを1つ再解読し、legs の差分が「新規87形式による追加」だけであること。
- 全テスト緑が納品条件。自分のテスト結果は報告書にそのまま書く（Muse が再実行して検収する）。

## 成果物

- 変更した `decode_chunk.py`（差分が分かる形）
- `NOTES.md`（形式ごとの実装可否と判断理由、既存 family へ寄せた形式の一覧、回帰確認の結果）
- テスト一式と実行結果

## 追記（材料の出所とサンプルの無い形式）

- 87形式の内訳: 27形式は深掘り便（deep5000＋fix5）由来、18形式は unresolved56 便（PR #29）由来、42形式は swap91 便（2026-11-05）由来で Muse の金額突合検証を通過したもの。swap91 で不一致だった20形式は含まない（形式の修正が先）。
- 5形式（0x30f37b38…, 0x38367098…, 0x4a6de6fb…, 0x829000a5…, 0xd3c1d9b3…）は手元サンプルが無い。
  これらは検証済みコントラクトの ABI から形式が確定している（Pendle / imToken 等）。
  テスト用サンプルは公開 RPC の eth_getLogs（topic 指定、直近ブロック範囲）で自前取得すること。
- 12形式はイベント名が未確定（署名未復元）だが、レイアウトは実サンプルの形と Transfer 実額で
  照合済み。実装はレイアウトのみに依存し、名前は使わないこと。
