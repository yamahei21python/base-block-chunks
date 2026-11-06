# NOTES — 最小アダプター87形式の実装と検証（2026-11-06 夜間ラン goal① フェーズ2）

## サマリー

- **87形式のうち 65形式を実装**（テスト 113 サンプル全件 PASS）し、**22形式は未実装**
  （理由は下記「未実装一覧」）。仕様 ADAPTER_SPEC.md の実装規則1-5・納品条件
  （形式別テスト a/b/c + 既存形式の回帰）に従った。
- 回帰確認: ホールドアウトの生チャンク3本（53523000/53523100/53523700 の各100ブロック）を
  旧デコーダと新デコーダで解読し、**既存12形式の legs は多重集合として1件も差分なし**
  （leg_index の並びだけが同一tx内の新形式混入で変わる）。
- ドライラン3本合計: legs 14,940 → 15,283（**+343、+2.3%**）。新形式の内訳は
  「ドライラン結果」節。

## 成果物一式（このフォルダ）

| ファイル | 内容 |
|---|---|
| `work/decode_chunk.py` | **変更した decode_chunk.py**（533行 → 2,609行。差分は5点: FORMATS 追加 / ADAPTER_READERS / ヘルパー+adapter_leg / pass-1 v2shift 登録 / pass-2 ブランチ） |
| `baseline/decode_chunk.py` | 変更前（base-block-chunks main `eecd60f` 時点のコピー・比較用） |
| `run_format_tests.py` | 形式別テスト（SPEC 納品条件 a/b/c）。`python3 -I run_format_tests.py` |
| `test_results.json` | テスト実行結果（サンプル別の a/b/c 合否と注記） |
| `adapter_readers.json` / `adapter_families.json` / `adapter_skipped.json` | 読み規則テーブル・family 寄せ・未実装の機械可判リスト（decode_chunk.py には ADAPTER_READERS が埋め込み済み） |
| `infer_semantics*.py` / `out_infer*.json` | サンプル実測からの読み規則推定の作業記録 |
| `fetch_rpc_samples.py` | サンプル無し5形式の eth_getLogs 取得試行（3回失敗の記録） |
| `materials/` | ADAPTER_SPEC.md・formats_accepted.json・samples_adapter.json・samples_enriched.json（77サンプルに receipt 取得で transfers を補完したもの）・名簿コピー |

## 実装方法

各形式の読み規則（どの欄が入出トークン・入出量か）は、`samples_adapter.json` の
target_logs と同レシート Transfer 実測の値照合で確定した（77サンプルは tx ハッシュから
`eth_getTransactionReceipt` を取得して transfers を補完済み）。確定した規則を
戦略として `ADAPTER_READERS` に埋め込み、`adapter_leg()` が解読する。

| 戦略 | 意味 | 件数 |
|---|---|---|
| `event` | トークン・量ともイベント欄から（pmm 型。センチネルは ETH0 に正規化） | 33 |
| `flows` | 量はイベント欄、トークンは emitter の粗Transfer（受入/送出）との額照合で決定。共有量欄（同一欄が両側に一致=1:1交換）は受入/送出が各1トークンのときのみ | 26 |
| `gross` / `gross_rev` | トークンを粗Transferの個別額照合で決定（pool 受入側が入 / 逆方向が swapper 側） | 4 |
| `event_flow_out` | in は宣言欄、out 量は宣言トークンの粗送出合計 | 1 |
| `event_auto_dir` | 2候補の向きを trader アドレスの送出方向で確定 | 1 |
| `array` | 動的配列 `Swap(address[],uint256[],address)`: path[0]/amounts[0] → path[-1]/amounts[-1]（SPEC 規則4） | 1 |
| （family 寄せ） | 既存 family の読み位置と完全一致し `FORMATS` の1行追加で済むもの（SPEC 規則1） | 3 |

## 既存 family へ寄せた形式の一覧（SPEC 規則1）

| topic | 名 | 寄せ先 | 根拠 |
|---|---|---|---|
| 0x298c349c… | Swap (amount0In/1In/0Out/1Out) | `v2style` | 宣言欄が In/Out 4語で既存 v2style と同一。サンプルは逆方向（a1i>0）で規則どおり解釈されることを確認 |
| 0xa4228e1e… | Swap (amount0/1 int256) | `v3style` | 符号付き2語・pool 視点で既存 v3style と同一。2サンプルで両方向を確認 |
| 0x4e10ec28… | Swaped (txOrigin, amount0In…) | `v2shift`（専用分岐） | 欄名は v2style と同一だが word 位置が1つずれる（w1-w4）。pair01 レゾルバを使用 |

## 未実装一覧（22形式・SPEC 規則5）

**サンプル取得失敗（5）** — 公開 RPC の eth_getLogs がレート制限(HTTP 429)で
3回失敗し検証サンプルを得られず。SPEC「テスト用サンプルは eth_getLogs で自前取得」
を満たせないため見送り。0x30f37b38 / 0x38367098 / 0x4a6de6fb / 0x829000a5 / 0xd3c1d9b3
（0xd3c1d9b3 は同型の 0xa3a28465 が未実装化したため、サンプル取得後に双方の実装を推奨）

**サンプルとの整合が確定できず（17）** — 実装規則3「サンプルの Transfer で厳密に決める」
を満たせないもの。理由は形式ごとに次のとおり:

| topic | 名 | 理由 |
|---|---|---|
| 0x6e897e83 | SwapExecuted | amountOut が実流れ（minAmountOut 側）と一致せず、集約器の内部経路で in/out が一意に対応しない（1サンプル） |
| 0x8bec65d6 | Trade | emitter にフローが無く、宣言 'token' 欄が転送当事者アドレスと一致。OTC/記帳イベントの疑いで交換レグの型を確定できない |
| 0x02e8b414 | (無名) | 全欄が bytes32/数値でトークンを特定できず、emitter のフローも無し |
| 0x3b789221 | (無名) | 欄値がパススルー系の複数トークン流れに対応し、実交換相手（d4a0e0）がイベントから導出できない |
| 0x4f0bfa1a | (無名) | pool が受入なし・3トークン送出のみで、in 側が確定できない |
| 0xdb0b70fa | (無名) | out 側が欄にもフローにも一致しない（d1/d2 と USDC 送出が不整合） |
| 0xe5aa1cf5 | Swap(addr,uint,addr,uint) | 方向が確定できない（pool 受入からは USDC 入側、欄順からは ETH 側が先で WETH 送出量と約2倍の不整合） |
| 0xf067e8ca | Swapped (direction flag) | direction フラグで入出欄が入れ替わり、s1 の out 側が pool 受入として現れるなどサンプル間で向きが不整合 |
| 0x7199b4a4 | BebopPmmSwap | base/quote の向きが flags ビットで決まるが s1 は両方向フロー・s2 は base==quote の縮退サンプルで検証不能 |
| 0xdc004dbc | Swap(bool,uint,uint,addr) | サンプルにトークンフローが無く、pool は名簿にも無い。(b)(c) の検証条件を満たせない |
| 0x7097fd23 | SwapExecuted | 宣言 tokenIn が実流れに一切現れず、実流れ 36b712 が宣言 tokenOut(ETH0) とも不一致（1サンプル） |
| 0xa3a28465 | SwapYtAndToken (Pendle) | out 側（SY ミント量 96e15）が欄と対応せず、共有量欄の一致はパススルーの偶然 |
| 0xf76b0a9f | Swapped | パススルー連鎖で out 側が確定できない |
| 0xc3e76429 | SwappedExactAmountOut | in/out が同トークンの実質ラッパー流れで、別流れの WETH と整合しない |
| 0xc3faa507 | SwappedTokenToBase | out 側がイベントに無く、実流れもパススルーで out トークンを確定できない |
| 0x2d720abb | (無名) | out 側が emitter を流れず差額1%の fee 構造で、欄→実流れの対応が確定できない |
| 0x6abc2d66 | SwappedExactAmountOut | トークン欄が無く data 尾部の path 解釈がサンプル間で非一貫（s1 は3候補・s2 は1候補） |

## テスト結果（納品条件 a/b/c）

- 対象: 実装 65 形式・サンプル 113 件（2サンプルの形式が大半）
- (a) leg 生成 / (b) トークンペアが Transfer 由来 / (c) 量が Transfer 実数と一致
- **結果: 113/113 PASS**（`test_results.json` にサンプル別の詳細）
- 既存 family 寄せ3形式は family 読みをサンプルで再現する形で同条件を検証

### 検証例外（テスト内で明記のうえ合格扱い）

| 件 | 内容 |
|---|---|
| 0x5380cf97 s1 | amountOut=0 の縮退サンプル。leg を生成しないことが正（EXPECT_NO_LEG として記録） |
| 0x086c7fcd s2 | amountIn は手数料控除後の純額（32174826）で、実 Transfer は 32193387=純額+18561（fee）。fee 差し引き構造の算術確認で合格（SPEC 規則3 の「手数料差し引き等」型） |
| 0xc82975a4 s2 | maker 側（makerToken/makerAmount）の受領がこのレシート外で行われる RFQ のため、taker 側のみ実額照合 |

## 回帰確認（納品条件）

ホールドアウト生チャンク3本（data-53523000-53523999 リリース）を旧・新デコーダで解読:

| チャンク | legs 旧 → 新 | 増分 | 既存12形式の多重集合差分 |
|---|---|---|---|
| 53523000-53523099 | 4,627 → 4,698 | +71 | **0（完全一致）** |
| 53523100-53523199 | 3,216 → 3,276 | +60 | **0** |
| 53523700-53523799（最大） | 7,097 → 7,309 | +212 | **0** |
| **合計** | **14,940 → 15,283** | **+343（+2.3%）** | **0** |

- 比較は `(tx_hash, pool, kind, token_in, amount_in, token_out, amount_out)` の多重集合。
  同一txに新形式が混在した場合の `leg_index` 並び替えは差分として数えない（内容は不変）。
- `ledger_tx` の n_legs 以外の列も全行一致。
- `SELF-CHECK OK`（swap_events の再計数一致）は3本とも確認済み。

## ドライラン結果（2-3チャンクの再解読集計）

新形式 65 種のうち3チャンクで出現したのは 12 kind、計 **343 legs 追加**:

| kind | legs（c1/c2/c3） |
|---|---|
| Swap（0x298c349c v2style 寄せ） | 21 / 23 / 29 |
| SwapExecuted（0xdd36740e） | 15 / 9 / 146 |
| TideFiSwap（0xe4c8d224） | 13 / 9 / 8 |
| WooSwap（0x0e8e403c） | 10 / 5 / 3 |
| MultiSwap（0x2fb07480） | 4 / 3 / 6 |
| Swapped（0xa078c419 ほか flows 型） | 3 / 3 / 4 |
| TokenExchange（0x143f1f8e ほか） | 2 / 5 / 8 |
| DODOSwap（0xc2c0245e） | 2 / 0 / 0 |
| RFQTrade（0xc82975a4） | 1 / 0 / 0 |
| TransitSwapped（0x2251435b） | 0 / 2 / 0 |
| Trade（0x853c8e67） | 0 / 1 / 0 |
| SwapDone（0xa62ddba0） | 0 / 0 / 4 |

残り 53 形式はこの3チャンクでは出現しなかった（レア形式）。全量再解読は検収後に
Muse が GitHub 側で回す前提（本ランでは実施しない）。

## 注意事項

- **名簿は変更していない**: デコーダは共有キャッシュ（known_pools.json）へ解読時の
  交好后を書き戻すため、`DECODE_SHARED_CACHE` 環境変数で scratch コピー
  （`cache/base_known_pools.json` 等）を指し、`materials/` 以下の名簿実物は未変更。
- 会計: adapter 戦略で決定できなかったイベントは `skipped_unresolved` に加算される
  ようにした（旧デコーダの会計と整合）。swap_events - legs - skipped は 0 に整合。
- 一般化のリスク（NOTES として明記）: (a) 共有量欄（1:1）形式は「受入/送出が各
  1トークン」の前提で、将来の非1:1のtxでは量が不正確になる可能性、(b) 額照合が
  複数ヒットした場合は先頭を採用（曖昧一致）、(c) event 戦略の向きは宣言名を
  user 視点としてそのまま採用（emitter の帳簿方向と逆のサンプルがあった。
  0xdd36740e s2 等は分類器の経路復元にとって user 視点が正のため）。
- 依存の追加・更新なし（標準ライブラリのみ）。既存形式・分類規則の挙動変更なし
  （回帰 0 で確認）。
