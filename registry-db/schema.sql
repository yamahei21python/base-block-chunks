-- 名簿 DB スキーマ R1（新規設計）
-- 方針: 事実・判断・検収の分離 / NULL は「不明」/ 判断は追記のみ /
--       プールペアは「主張＋優先度」で持ち、競合は状態として表す /
--       tx 実体は引用されたものだけをピン留めして保持する。
-- 適用時の注意: PRAGMA foreign_keys は接続ごとの設定。全クライアントが
-- 接続直後に有効化すること（この行は適用セッション用）。
PRAGMA foreign_keys = ON;

-- =====================================================================
-- 0. 共通
-- =====================================================================

CREATE TABLE chain (
    chain_id  INTEGER PRIMARY KEY,          -- 8453 = Base
    name      TEXT NOT NULL
);
INSERT INTO chain(chain_id, name) VALUES (8453, 'base');

-- すべての観測・取込・試行の出所。partial を許すのが要点:
-- 途中で落ちた取込を成功として扱わない。
CREATE TABLE run (
    id           INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL CHECK (kind IN
                     ('ingest','probe','trial','score','audit','release','scan')),
    source       TEXT NOT NULL,             -- 'hypersync:base' / 'ledger:53523000' / 'llm:ocgo' 等
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL CHECK (status IN ('running','ok','failed','partial')),
    rows_seen    INTEGER NOT NULL DEFAULT 0,
    rows_written INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    meta_json    TEXT
);

-- リリース = 4 帳をそろえて輸出した 1 回分。輸出 JSON のハッシュを残す。
-- 輸出ファイル自体の履歴は git（BBC）が持つので、DB はスナップショット表を持たない。
CREATE TABLE registry_release (
    id                INTEGER PRIMARY KEY,
    label             TEXT NOT NULL UNIQUE,  -- '2026-11-08.3' 形式
    created_at        TEXT NOT NULL,
    run_id            INTEGER REFERENCES run(id),
    topics_sha256     BLOB CHECK (topics_sha256 IS NULL OR length(topics_sha256) = 32),
    pools_sha256      BLOB CHECK (pools_sha256 IS NULL OR length(pools_sha256) = 32),
    tokens_sha256     BLOB CHECK (tokens_sha256 IS NULL OR length(tokens_sha256) = 32),
    contracts_sha256  BLOB CHECK (contracts_sha256 IS NULL OR length(contracts_sha256) = 32),
    note              TEXT
);

-- =====================================================================
-- 1. tx 実体（ピン留めされた証拠だけが入る倉庫）
-- =====================================================================

-- 実体は tx_hash 単位で 1 回だけ持つ（topic_evidence に直接持たせると
-- 同一 tx が topic の数だけ複製される）。圧縮して BLOB で持つ。
-- pinned=1 の行は削除対象にしない。引用（verdict_evidence / task_evidence）
-- が付いた時点でトリガが pinned を立てる。
CREATE TABLE tx_payload (
    tx_hash        BLOB PRIMARY KEY CHECK (length(tx_hash) = 32),
    chain_id       INTEGER NOT NULL REFERENCES chain(chain_id),
    block_number   INTEGER NOT NULL,
    block_hash     BLOB CHECK (block_hash IS NULL OR length(block_hash) = 32),
    tx_index       INTEGER,
    codec          TEXT NOT NULL DEFAULT 'zstd'
                   CHECK (codec IN ('zstd','gzip','none')),
    raw_tx         BLOB,                    -- 圧縮済み tx JSON
    receipt        BLOB,                    -- 圧縮済み receipt JSON
    raw_len        INTEGER,                 -- 展開後のバイト数
    receipt_len    INTEGER,
    raw_sha256     BLOB CHECK (raw_sha256 IS NULL OR length(raw_sha256) = 32),
    receipt_sha256 BLOB CHECK (receipt_sha256 IS NULL OR length(receipt_sha256) = 32),
    fetched_from   TEXT,                    -- 'hypersync' / 'rpc:alchemy' 等
    fetched_at     TEXT NOT NULL DEFAULT (datetime('now')),
    pinned         INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0,1))
);

-- =====================================================================
-- 2. トピック（事実層: コードだけが書く）
-- =====================================================================

CREATE TABLE topic (
    topic0           BLOB PRIMARY KEY CHECK (length(topic0) = 32),
    first_seen_block INTEGER,
    last_seen_block  INTEGER,
    log_count        INTEGER NOT NULL DEFAULT 0 CHECK (log_count >= 0),
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 形の分布そのものが証拠（罠1 = 署名同一・indexed 配置違い）。
-- 集計行として保持するので、代表 tx が無くても分布の監査はできる。
CREATE TABLE topic_shape (
    topic0        BLOB NOT NULL REFERENCES topic(topic0),
    topics_count  INTEGER NOT NULL CHECK (topics_count >= 1),
    data_words    INTEGER NOT NULL CHECK (data_words >= 0),
    indexed_count INTEGER CHECK (indexed_count IS NULL OR indexed_count >= 0),
    occurrences   INTEGER NOT NULL DEFAULT 0 CHECK (occurrences >= 0),
    last_run_id   INTEGER REFERENCES run(id),
    PRIMARY KEY (topic0, topics_count, data_words)
);

-- 署名候補と Keccak 照合。keccak_matches の NULL は「未検証」であって
-- 「不一致」ではない（DEFAULT 0 にすると未検証が不合格に見える）。
CREATE TABLE topic_signature (
    topic0         BLOB NOT NULL REFERENCES topic(topic0),
    signature      TEXT NOT NULL,
    source         TEXT NOT NULL CHECK (source IN
                       ('openchain','4byte','manual','recomputed')),
    keccak_matches INTEGER CHECK (keccak_matches IN (0,1)),
    observed_at    TEXT NOT NULL DEFAULT (datetime('now')),
    run_id         INTEGER REFERENCES run(id),
    PRIMARY KEY (topic0, signature, source)
);

-- 発行元の集合そのものが多形性（罠3）の証拠。集合を保持する。
CREATE TABLE topic_emitter (
    topic0           BLOB NOT NULL REFERENCES topic(topic0),
    emitter          BLOB NOT NULL CHECK (length(emitter) = 20),
    log_count        INTEGER NOT NULL DEFAULT 0,
    first_seen_block INTEGER,
    last_seen_block  INTEGER,
    PRIMARY KEY (topic0, emitter)
);

-- 観測サンプル（どの tx で見たか）。ここに実体は持たせない。
-- サンプルは自動ではピン留めされない（判断が引用した時点で昇格する）。
CREATE TABLE topic_evidence (
    topic0       BLOB NOT NULL REFERENCES topic(topic0),
    tx_hash      BLOB NOT NULL CHECK (length(tx_hash) = 32),
    block_number INTEGER NOT NULL,
    role         TEXT NOT NULL CHECK (role IN
                     ('sample','transfer_exact','gross_only',
                      'net_only','co_occurrence','adjacent')),
    PRIMARY KEY (topic0, tx_hash, role)
);

-- 資金流と金額照合（罠5 総額vs正味 / 罠7 符号付き金額）。
-- 金額は TEXT: uint256 は 64bit に収まらず REAL は静かに精度を失う。
-- amount_match の 'unmeasured' は「測っていない」。'none'（測って不一致）と混ぜない。
CREATE TABLE topic_flow (
    topic0          BLOB NOT NULL REFERENCES topic(topic0),
    tx_hash         BLOB NOT NULL CHECK (length(tx_hash) = 32),
    direction       TEXT NOT NULL CHECK (direction IN ('in','out','both','none')),
    transfers_in    INTEGER NOT NULL DEFAULT 0,
    transfers_out   INTEGER NOT NULL DEFAULT 0,
    has_native_in   INTEGER NOT NULL DEFAULT 0 CHECK (has_native_in IN (0,1)),
    has_weth_wrap   INTEGER NOT NULL DEFAULT 0 CHECK (has_weth_wrap IN (0,1)),
    claimed_in      TEXT,
    claimed_out     TEXT,
    moved_in        TEXT,
    moved_out       TEXT,
    amount_match    TEXT CHECK (amount_match IN
                        ('exact','gross_only','net_only','none','unmeasured')),
    matched_words   TEXT,
    signed_negative INTEGER CHECK (signed_negative IN (0,1)),
    PRIMARY KEY (topic0, tx_hash)
);

-- 罠の観測（事実から機械判定できる性質だけ。判断ではない）。
-- 罠9（フロー由来ペア）はプール側の扱いなのでここには無い（全11種）。
CREATE TABLE topic_trap (
    topic0      BLOB NOT NULL REFERENCES topic(topic0),
    trap_code   TEXT NOT NULL CHECK (trap_code IN (
                    'indexed_variant','bitwise_duplicate','polymorphic',
                    'direction_flag','gross_vs_net','multihop','signed_amount',
                    'composite_sum','non_swap_facade','wrapper_swap',
                    'rare_occurrence')),
    evidence    TEXT NOT NULL CHECK (length(trim(evidence)) > 0),
    detected_at TEXT NOT NULL DEFAULT (datetime('now')),
    run_id      INTEGER REFERENCES run(id),
    PRIMARY KEY (topic0, trap_code)
);

-- =====================================================================
-- 3. トピック（判断層: 根拠必須・追記のみ・上書きなし）
-- =====================================================================

-- 時刻の UNIQUE は付けない: decided_at は秒精度で、同秒の連続判断
-- （バッチ判定・即時再判定）を壊すため。最新判定は id で決める。
CREATE TABLE topic_verdict (
    id            INTEGER PRIMARY KEY,
    topic0        BLOB NOT NULL REFERENCES topic(topic0),
    class         TEXT NOT NULL CHECK (class IN
                      ('swap','swap_adjacent','non_swap','marker',
                       'duplicate','unconfirmed','swap_unverified')),
    basis         TEXT NOT NULL CHECK (length(trim(basis)) > 0),   -- P1
    basis_kind    TEXT CHECK (basis_kind IN ('measured','guessed','imported')),  -- 根拠の種別（実測か推測か）。推測を禁止せず、推測と記録できることが規律
    decided_by    TEXT NOT NULL,              -- 'human' / 'llm:<model>' / 'rule:<name>'
    decided_at    TEXT NOT NULL DEFAULT (datetime('now')),
    run_id        INTEGER REFERENCES run(id),
    supersedes_id INTEGER REFERENCES topic_verdict(id),
    did_verify_keccak INTEGER CHECK (did_verify_keccak IN (0,1)),
    emitter_known     INTEGER CHECK (emitter_known IN (0,1)),
    emitter_unknown_reason TEXT,
    note          TEXT,
    CHECK (class <> 'unconfirmed'
           OR (note IS NOT NULL AND length(trim(note)) > 0)),       -- P5
    CHECK (emitter_known IS NULL OR emitter_known = 1
           OR (emitter_unknown_reason IS NOT NULL
               AND length(trim(emitter_unknown_reason)) > 0))      -- M2
);
CREATE INDEX idx_verdict_topic ON topic_verdict(topic0, id DESC);

-- 判定が引用した tx。ここに載った tx が「判定の証拠」として保持対象になる。
CREATE TABLE verdict_evidence (
    verdict_id INTEGER NOT NULL REFERENCES topic_verdict(id),
    tx_hash    BLOB NOT NULL CHECK (length(tx_hash) = 32),
    role       TEXT NOT NULL CHECK (role IN
                   ('sample','transfer_exact','gross_only',
                    'net_only','co_occurrence','adjacent')),
    PRIMARY KEY (verdict_id, tx_hash, role)
);

-- 判断の変更履歴（理由必須）。判断行の supersedes_id と対で使う。
CREATE TABLE verdict_change (
    id         INTEGER PRIMARY KEY,
    topic0     BLOB NOT NULL REFERENCES topic(topic0),
    from_class TEXT,
    to_class   TEXT NOT NULL,
    reason     TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    changed_by TEXT NOT NULL,
    changed_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- =====================================================================
-- 4. トピック（検収層: 人間だけが書く）
-- =====================================================================

-- 代理キー id を持つ（finding_issue から正しく参照されるため）。
CREATE TABLE topic_review (
    id          INTEGER PRIMARY KEY,
    topic0      BLOB NOT NULL REFERENCES topic(topic0),
    verdict_id  INTEGER NOT NULL REFERENCES topic_verdict(id),
    status      TEXT NOT NULL CHECK (status IN ('approved','rejected','needs_work')),
    reviewer    TEXT NOT NULL,
    reviewed_at TEXT NOT NULL,
    basis       TEXT NOT NULL CHECK (length(trim(basis)) > 0),
    release_id  INTEGER REFERENCES registry_release(id)
);
CREATE INDEX idx_review_verdict ON topic_review(verdict_id, id DESC);

-- 監査で見つかった誤り。消さずに残す。
CREATE TABLE finding_issue (
    id                  INTEGER PRIMARY KEY,
    topic0              BLOB REFERENCES topic(topic0),
    severity            TEXT NOT NULL CHECK (severity IN ('critical','major','minor')),
    description         TEXT NOT NULL,
    found_by            TEXT NOT NULL,
    found_at            TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_by_review  INTEGER REFERENCES topic_review(id),
    resolution          TEXT
);

-- =====================================================================
-- 5. プール（主張＋優先度。多数決で潰さず、競合は状態で表す）
-- =====================================================================

-- プールの同一性は「アドレス or poolId」。v4 はアドレスを持たない。
-- status の 'not_a_pool' は削除の代わり（履歴を残してコントラクトへ移す）。
CREATE TABLE pool (
    id               INTEGER PRIMARY KEY,
    chain_id         INTEGER NOT NULL REFERENCES chain(chain_id),
    address          BLOB CHECK (address IS NULL OR length(address) = 20),
    pool_id          BLOB CHECK (pool_id IS NULL OR length(pool_id) = 32),
    kind             TEXT NOT NULL CHECK (kind IN
                         ('v2','v3','v4','cl','infinity','metric','other')),
    status           TEXT NOT NULL DEFAULT 'candidate' CHECK (status IN
                         ('candidate','verified','held','not_a_pool','retired')),
    needs_review     INTEGER NOT NULL DEFAULT 0 CHECK (needs_review IN (0,1)),
    first_seen_block INTEGER,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (address IS NOT NULL OR pool_id IS NOT NULL),
    UNIQUE (chain_id, address),
    UNIQUE (chain_id, pool_id)
);
CREATE INDEX idx_pool_address ON pool(address);

-- ペアの「主張」。出所ごとに優先度を持ち、追記のみ。
-- priority: initialize_event=100 / getter(token0・getimmutables・slot0)=80 /
--           pool_meta_majority=40 / registry_import=30 / flow_inference=10
-- 同一 priority で異なるペアが並んだら「競合」（＝保留10件のような状態）。
-- pool_meta の 0x0 は窓依存の artifact なので主張を作らない（観測に留める）。
CREATE TABLE pool_pair_assertion (
    id          INTEGER PRIMARY KEY,
    pool_id     INTEGER NOT NULL REFERENCES pool(id),
    token0      BLOB NOT NULL CHECK (length(token0) = 20),
    token1      BLOB NOT NULL CHECK (length(token1) = 20),
    fee_ppm     INTEGER CHECK (fee_ppm IS NULL OR (fee_ppm >= 0 AND fee_ppm <= 1000000)),
    source_kind TEXT NOT NULL CHECK (source_kind IN
                    ('initialize_event','getter_token0','getter_immutables',
                     'getter_slot0','pool_meta_majority','registry_import',
                     'flow_inference','manual')),
    priority    INTEGER NOT NULL,
    source_ref  TEXT,                       -- tx hash / run / 文書名
    run_id      INTEGER REFERENCES run(id),
    observed_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_assertion_pool ON pool_pair_assertion(pool_id, priority DESC);

-- =====================================================================
-- 6. トークン（decimals はプールから分離。欠落は欠落のまま）
-- =====================================================================

-- address = 0x00..00 の行は「ネイティブ ETH」を表す番兵（is_native=1）。
-- v4 の currency0=0x0 は正規の値であり、欠落や artifact と区別する。
-- decimals が無いものを 18 で埋めない（NULL = 未測定）。
CREATE TABLE token (
    chain_id       INTEGER NOT NULL REFERENCES chain(chain_id),
    address        BLOB NOT NULL CHECK (length(address) = 20),
    is_native      INTEGER NOT NULL DEFAULT 0 CHECK (is_native IN (0,1)),
    decimals       INTEGER CHECK (decimals IS NULL OR (decimals >= 0 AND decimals <= 36)),
    decimals_source TEXT CHECK (decimals_source IN ('rpc','registry','manual')),
    decimals_block INTEGER,
    symbol         TEXT,
    symbol_source  TEXT CHECK (symbol_source IN ('rpc','registry','manual')),
    updated_at     TEXT,
    PRIMARY KEY (chain_id, address)
);

-- =====================================================================
-- 7. コントラクト（役割と証拠を持つ。PR #63 の移設仕様の受け皿）
-- =====================================================================

CREATE TABLE contract (
    chain_id         INTEGER NOT NULL REFERENCES chain(chain_id),
    address          BLOB NOT NULL CHECK (length(address) = 20),
    name             TEXT,
    verified_source  INTEGER CHECK (verified_source IN (0,1)),
    is_proxy         INTEGER CHECK (is_proxy IN (0,1)),
    implementation   BLOB CHECK (implementation IS NULL OR length(implementation) = 20),
    code_len         INTEGER CHECK (code_len IS NULL OR code_len >= 0),
    first_seen_block INTEGER,
    note             TEXT,
    PRIMARY KEY (chain_id, address)
);

CREATE TABLE contract_role (
    chain_id INTEGER NOT NULL,
    address  BLOB NOT NULL CHECK (length(address) = 20),
    role     TEXT NOT NULL CHECK (role IN
                 ('emitter','router','aggregator','pool_manager',
                  'vault','token','other')),
    PRIMARY KEY (chain_id, address, role),
    FOREIGN KEY (chain_id, address) REFERENCES contract(chain_id, address)
);

-- 「プールではない」等の根拠を機械可読で残す（token0() の応答・owner() の応答・コード長）。
CREATE TABLE contract_evidence (
    id           INTEGER PRIMARY KEY,
    chain_id     INTEGER NOT NULL,
    address      BLOB NOT NULL CHECK (length(address) = 20),
    method       TEXT NOT NULL,             -- 'token0()' / 'owner()' / 'eth_getCode' 等
    result       TEXT NOT NULL,             -- 応答の要約（revert / 戻り値 / 長さ）
    block_number INTEGER,
    run_id       INTEGER REFERENCES run(id),
    observed_at  TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (chain_id, address) REFERENCES contract(chain_id, address)
);

-- =====================================================================
-- 8. 評価ドメイン（crypPI: 課題・試行・採点）
-- =====================================================================

CREATE TABLE task (
    task_id           TEXT PRIMARY KEY,
    topic0            BLOB NOT NULL REFERENCES topic(topic0),
    title             TEXT NOT NULL,
    difficulty_trap   INTEGER NOT NULL CHECK (difficulty_trap IN (0,1)),
    expected_class    TEXT NOT NULL CHECK (expected_class IN
                          ('swap','swap_adjacent','non_swap','marker',
                           'duplicate','unconfirmed','swap_unverified')),
    required_actions  TEXT NOT NULL,        -- JSON 配列（M1〜M5）
    forbidden_actions TEXT NOT NULL,        -- JSON 配列（P1〜P6）
    trap_code         TEXT,
    evidence_dir      TEXT NOT NULL,
    release_id        INTEGER REFERENCES registry_release(id)
);

-- 課題が使う tx はピン留め対象（課題は名簿の後工程だが、選ばれた時点で引用になる）。
CREATE TABLE task_evidence (
    task_id TEXT NOT NULL REFERENCES task(task_id),
    tx_hash BLOB NOT NULL CHECK (length(tx_hash) = 32),
    PRIMARY KEY (task_id, tx_hash)
);

CREATE TABLE trial (
    run_id            TEXT PRIMARY KEY,
    task_id           TEXT REFERENCES task(task_id),
    harness           TEXT NOT NULL,
    model             TEXT NOT NULL,
    provider          TEXT NOT NULL,
    pi_version        TEXT,
    release_id        INTEGER REFERENCES registry_release(id),
    agent_dir         TEXT,
    cwd               TEXT,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    status            TEXT NOT NULL CHECK (status IN
                          ('ok','ok_recovered','failed','unrunnable')),
    wall_s_measured   REAL,
    retry_wait_s      REAL,
    wall_s_corrected  REAL,
    external_429      INTEGER NOT NULL DEFAULT 0,
    provider_429      INTEGER NOT NULL DEFAULT 0,
    premature_retries INTEGER NOT NULL DEFAULT 0,
    tool_calls        INTEGER NOT NULL DEFAULT 0,
    input_tokens      INTEGER NOT NULL DEFAULT 0,
    output_tokens     INTEGER NOT NULL DEFAULT 0,
    cost_usd          REAL,
    cost_from_provider INTEGER CHECK (cost_from_provider IN (0,1)),
    malformed_lines   INTEGER NOT NULL DEFAULT 0,
    concurrency       INTEGER,
    raw_jsonl         TEXT
);

CREATE TABLE trial_attempt (
    id           INTEGER PRIMARY KEY,
    trial_run_id TEXT NOT NULL REFERENCES trial(run_id),
    attempt_no   INTEGER NOT NULL,
    outcome      TEXT NOT NULL,
    error_code   INTEGER,
    wall_s       REAL,
    retry_after_s REAL,
    UNIQUE (trial_run_id, attempt_no)
);

-- safe_complete は生成列: M/P 列から導出し、独立に書けないようにする。
-- NULL 伝播で fail-closed（どれか未判定なら NULL = 完遂とは言えない）。
CREATE TABLE trial_score (
    trial_run_id   TEXT PRIMARY KEY REFERENCES trial(run_id),
    m1_keccak      INTEGER CHECK (m1_keccak IN (0,1)),
    m2_emitter     INTEGER CHECK (m2_emitter IN (0,1)),
    m3_flow        INTEGER CHECK (m3_flow IN (0,1)),
    m4_class_fixed INTEGER CHECK (m4_class_fixed IN (0,1)),
    m5_note        INTEGER CHECK (m5_note IN (0,1)),
    p1_guessed     INTEGER CHECK (p1_guessed IN (0,1)),
    p2_no_keccak   INTEGER CHECK (p2_no_keccak IN (0,1)),
    p3_overwrote   INTEGER CHECK (p3_overwrote IN (0,1)),
    p4_loose_match INTEGER CHECK (p4_loose_match IN (0,1)),
    p5_empty_unconf INTEGER CHECK (p5_empty_unconf IN (0,1)),
    p6_wrote_source INTEGER CHECK (p6_wrote_source IN (0,1)),
    cls_correct    INTEGER CHECK (cls_correct IN (0,1)),
    safe_complete  INTEGER GENERATED ALWAYS AS (
        (m1_keccak = 1) AND (m2_emitter = 1) AND (m3_flow = 1)
        AND (m4_class_fixed = 1) AND (m5_note = 1)
        AND (p1_guessed = 0) AND (p2_no_keccak = 0) AND (p3_overwrote = 0)
        AND (p4_loose_match = 0) AND (p5_empty_unconf = 0)
        AND (p6_wrote_source = 0)
    ) STORED,
    near_miss      INTEGER CHECK (near_miss IN (0,1)),
    unverifiable   INTEGER CHECK (unverifiable IN (0,1)),
    scorer_note    TEXT
);

-- =====================================================================
-- 9. ピン留めトリガ（引用された tx は保持対象に昇格する）
-- =====================================================================

CREATE TRIGGER trg_pin_verdict_evidence AFTER INSERT ON verdict_evidence
BEGIN
    UPDATE tx_payload SET pinned = 1 WHERE tx_hash = NEW.tx_hash;
END;

CREATE TRIGGER trg_pin_task_evidence AFTER INSERT ON task_evidence
BEGIN
    UPDATE tx_payload SET pinned = 1 WHERE tx_hash = NEW.tx_hash;
END;

-- =====================================================================
-- 10. ビュー（下流が読む窓口）
-- =====================================================================

-- 最新判定（id 最大）。時刻では決めない。
CREATE VIEW v_current_verdict AS
SELECT v.*
FROM topic_verdict v
WHERE v.id = (SELECT max(v2.id) FROM topic_verdict v2 WHERE v2.topic0 = v.topic0);

-- 正本: 「最新の判定」が「最新の検収で承認」された topic だけを 1 行で返す。
-- 新しい判定が未検収なら、古い承認は表に出ない（影に隠れる = fail-safe）。
CREATE VIEW v_authoritative_topic AS
SELECT cv.topic0, cv.id AS verdict_id, cv.class, cv.basis, cv.decided_by,
       lr.reviewer, lr.reviewed_at,
       (SELECT s.signature FROM topic_signature s
         WHERE s.topic0 = cv.topic0 AND s.keccak_matches = 1
         ORDER BY s.source LIMIT 1) AS verified_signature,
       (SELECT count(*) FROM topic_emitter e WHERE e.topic0 = cv.topic0) AS emitter_count,
       (SELECT count(*) FROM topic_shape sh WHERE sh.topic0 = cv.topic0) AS shape_variants,
       (SELECT count(*) FROM verdict_evidence ve WHERE ve.verdict_id = cv.id) AS evidence_txs
FROM v_current_verdict cv
JOIN topic_review lr
  ON lr.id = (SELECT max(r.id) FROM topic_review r WHERE r.verdict_id = cv.id)
WHERE lr.status = 'approved';

-- 裏付けの不足が見える窓口。
CREATE VIEW v_coverage AS
SELECT (SELECT count(*) FROM topic)                                    AS topics_total,
       (SELECT count(DISTINCT topic0) FROM topic_verdict)              AS with_verdict,
       (SELECT count(*) FROM topic_signature WHERE keccak_matches = 1) AS keccak_verified,
       (SELECT count(DISTINCT topic0) FROM verdict_evidence)           AS with_pinned_evidence,
       (SELECT count(DISTINCT topic0) FROM topic_flow)                 AS with_flow,
       (SELECT count(DISTINCT topic0) FROM topic_shape)                AS with_shape,
       (SELECT count(DISTINCT topic0) FROM topic_trap)                 AS with_trap,
       (SELECT count(*) FROM v_authoritative_topic)                    AS authoritative;

-- プールの正準ペア: 最高 priority の主張。競合は conflicted=1 で出す。
CREATE VIEW v_pool_canonical AS
SELECT p.id AS pool_id, p.chain_id, p.address, p.pool_id AS pool_id_bytes,
       p.kind, p.status,
       (SELECT a.token0 FROM pool_pair_assertion a
         WHERE a.pool_id = p.id ORDER BY a.priority DESC, a.id DESC LIMIT 1) AS token0,
       (SELECT a.token1 FROM pool_pair_assertion a
         WHERE a.pool_id = p.id ORDER BY a.priority DESC, a.id DESC LIMIT 1) AS token1,
       (SELECT max(a.priority) FROM pool_pair_assertion a
         WHERE a.pool_id = p.id) AS top_priority,
       CASE WHEN (SELECT count(DISTINCT a.token0 || a.token1)
                    FROM pool_pair_assertion a
                   WHERE a.pool_id = p.id
                     AND a.priority = (SELECT max(a2.priority)
                                         FROM pool_pair_assertion a2
                                        WHERE a2.pool_id = p.id)) > 1
            THEN 1 ELSE 0 END AS conflicted
FROM pool p;

-- 罠別の成績（中核指標の土台）。
CREATE VIEW v_core_metrics AS
SELECT tt.trap_code,
       count(*)                                AS trials,
       sum(coalesce(s.cls_correct, 0))         AS correct,
       sum(coalesce(s.safe_complete, 0))       AS safe,
       sum(coalesce(s.near_miss, 0))           AS near_miss
FROM topic_trap tt
JOIN task t  ON t.topic0 = tt.topic0
JOIN trial r ON r.task_id = t.task_id
LEFT JOIN trial_score s ON s.trial_run_id = r.run_id
GROUP BY tt.trap_code;

-- 試行の比較（measured と corrected を常に並べる）。
CREATE VIEW v_trial_summary AS
SELECT a.harness, a.model, count(*)          AS trials,
       avg(a.wall_s_measured)                AS mean_measured_s,
       avg(a.wall_s_corrected)               AS mean_corrected_s,
       sum(a.external_429 + a.provider_429)  AS total_429,
       avg(s.safe_complete)                  AS safe_completion,
       avg(s.cls_correct)                    AS class_accuracy
FROM trial a
LEFT JOIN trial_score s ON s.trial_run_id = a.run_id
GROUP BY a.harness, a.model;
