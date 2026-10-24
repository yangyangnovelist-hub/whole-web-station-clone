"""MIX.md on the Hugging Face books: rescue signals that carry information but lose it to costs, by
cheaper exits (resting sells, settlement) and by stacking weak signals into one score, trading only
its top few percent. Paper research on market data only (no orders, no keys).

Data: whodisidk/polymarket-btc-updown-exchange-data (BTC 5m, 2026-05-25 .. 08-29: 100 ms books of
both tokens, every Polymarket print with its taker side, every Binance BTCUSDT print with trade and
recorder times). Runs on GitHub as `python cross.py mix --workdir DIR --out real/cross-mix-hf.md`
(ci/cross.request "cmd: mix --out real/cross-mix-hf.md", the polymarket-cross lane, which commits only
real/cross-*: the report, real/cross-mix-frozen.json, real/cross-mix-lockbox.json and
real/cross-mix-rows.csv.gz). The factor-model predictions and the hourly DVOL it needs are shipped in
real/mix-inputs/ (built locally by `build_inputs`, see below). Only a run that reads every A archive
of MANIFEST.txt freezes rules into the committed file; `mix N` (the last N archives) or
run(names=...) is a trial (see the lockbox below).

Pipeline (MIX.md; the constants were fixed before the full run. Review fixes made after dev smoke runs on
a few archives, before any full run: taker exits wait for a usable bid, the jump reference and klines use
only received prints (60 s kline guard), verified downloads, trial runs, the lockbox record):
1. Per daily archive (downloaded, checked against MANIFEST.txt's size and sha256, read and deleted one
   at a time; per-day rows written to <workdir>/mix-shards/<day>.parquet, then concatenated): BTC 5m
   markets with a final official outcome (jump2s_hf.final_resolution: provisional resolution rows
   dropped; shrink_markets first). Only archives dated 05-25 .. 08-29 are read.
2. Decision points (times are the recorder's receipt clock, as cross.stale_trades / jump2s_hf):
   - "jump": a Binance print whose log price moved >= 2 bp from its reference, the last print by
     trade time at least 1 s and at most 5 s earlier AMONG THE PRINTS RECEIVED BY THE TRIGGER'S OWN
     RECEIPT TIME (prints arrive out of trade order by up to 9 s: 07-17; a reference received later
     is skipped for the last earlier one that had arrived); decision time t = its receipt time;
     285 >= end - t >= 20 s (inclusive); per market a point is kept only >= 5 s after the last kept
     one (greedy, in t order).
   - "fixed": t = end - 240, 180, 120, 60 s in every market.
   - "random" (cost-structure control only; never modelled or traded by a rule): one per jump point
     of the same market, t uniform in [end - 285, end - 20], side +-1 at random, seeded by
     zlib.crc32(market id) (so a market's draws do not depend on the other markets).
   Every jump / fixed point gives two rows, one per side (Up = +1, Down = -1).
3. Features at t, only data received at or before t (side-relative: "s" is the row's side):
   jump_bp = s x d (bp), jump_abs_bp = |d|, jump_z = s x d / sigma, dir = sign(s x d), is_jump,
   where d = log move of the last Binance print received by t against its 1-5 s reference (as in 2:
   among the prints received by that print's receipt time; for a jump row the trigger itself) and
   sigma the per-second vol of the 600 s before (jump2s.Seconds on the recorder's receipt clock, so
   only prints received before t; gaps up to 20 s forward filled, as jump2s_hf); h_edge = H fair
   value - the side's ask at t, where H fair (fill_rate.sends) = Phi(Phi^-1(Up mid 2 s earlier,
   clipped 0.005..0.995) + Binance log move over those 2 s / (sigma x f)) for Up, 1 - that for Down,
   f = sqrt(seconds left) for point-price markets, binary.twap_std_factor for the TWAP ones (rule
   window by pm_outcomes.rule_window: point before 08-07, 30 s 08-07 .. 08-13, 60 s from 08-14); NaN
   without a vol estimate; dmid2, dmid10 = the side's own mid now minus 2 s / 10 s earlier; imb =
   (bid size - ask size) / (sum) at the side's best; spread = ask - bid; price = the side's ask; fee
   = 0.07 p (1 - p); tau = seconds left; trend_agree = sign(d) x sign(ret4h) (is the move with the
   4 h trend), trend_side = sign(s x ret4h); vr60 (jump2s.trend_features on Binance 1 m klines from
   data.binance.vision; a minute is used only when it closed at least 60 s before the latest exchange
   time among the prints received by t, a function of what had arrived; 60 s is above the receipt
   disorder seen (8.9 s 07-17, 9.6 s 08-28, 38 s on the damaged 08-19 archive) and costs at most one
   minute of a 4 h / 60 min feature; the report counts per archive the points whose last minute still
   had a print in flight anyway); f_ridge5, f_hgb5, f_ridge15, f_hgb15 = s x (P(up) - 0.5)
   of the factor study's walk-forward out-of-sample predictions for the window starting at the
   market's open (FACTORS.md; known at the open); dvol_rv = DVOL / 100 (last hourly candle closed by
   t, known at its hour's end) - rv30 (annualised std of the 1 s log returns of the 1,800 s ending
   with the second before t on the receipt-clock grid, at least 900 present), as
   factors.dvol_minus_rv30.
4. Execution labels, per share, for every row:
   - Entry: decided at t, filled at te = t + 0.5 s at the side's best ask in the last snapshot at
     or before te (<= 1 s old), which must be active, not halted, not crossed on that token
     (bid < ask), fresh (the market's top of book changed <= 1 s before te, as jump2s_hf), ask in
     0.02..0.98 with >= 5 shares at it. No such snapshot -> no row. Taker fee 0.07 p (1 - p).
   - "tk{h}" (h = 5, 10, 30, 60 s): sell decided at te + h, sent at tx = te + h + 0.5 s, sold at the
     side's best bid as a taker (fee on the bid). A usable bid: active, not crossed, bid > 0 with
     >= 5 shares, the book fresh (top of book changed <= 1 s before). The snapshot at or before tx
     (<= 1 s old) is used when its bid is usable at tx; otherwise the sale waits for the first later
     snapshot with a usable bid, before the market's end (the exit is never swapped for a different
     one while a usable bid appears). Held to settlement only when tx is not before the end or no
     usable bid appears before it.
   - "mk{x}_{h}" (x = 1, 2, 3, 5 c; h as above): a resting sell at L = min(entry price + x, 0.99),
     no fee. It counts as filled at the first moment in (tl, min(te + h, end)] when a Polymarket
     print on that token is a taker BUY at a price >= L, or a snapshot (active, not crossed, fresh)
     shows the token's best bid >= L; tl = te + 0.5 s is when the order is live (see below). Not
     filled by te + h: cancelled, and sold as in tk{h} (else settlement).
   - "settle": held to the official result.
   how (per policy): 0 held to settlement, 1 taker sale, 2 maker fill at L, 3 the resting sell
   crossed on arrival (taker sale at the bid at tl, see below).
   pnl = proceeds - entry price - every taker fee. Each row also keeps, per policy, the holding time
   (entry fill to exit fill, or to the market's end when held) and the 20-share size (see units).
5. After all days, segments by market start (UTC): A = 05-25 .. 07-15, B = 07-16 .. 08-15, C = 08-16
   .. 08-29. Models per policy (21 policies; rows = jump and fixed rows): ridge (alpha 10 on
   features standardised on the training rows, clipped at +-5 sd, NaN -> 0) and
   HistGradientBoostingRegressor (depth 3, 8 leaves, >= 400 per leaf, lr 0.05, 200 iterations,
   l2 1, no early stopping, random_state 0), both predicting the policy's per-share pnl. A
   out-of-sample: 7-day blocks from 05-25; each block scored by models fit on every A row of
   markets that ended before the block starts; the first 14 days only train (out-of-sample A =
   06-08 .. 07-15). Rules = (policy, model, top q in 20, 10, 5, 2 %), q cut = the score at the
   top q of A's out-of-sample scores. The best 3 by A out-of-sample per-share pnl (>= 300 A trades)
   are frozen to <prefix>frozen.json BEFORE anything of B or C is read (freeze() is given the A rows
   only and the file is read back from disk for the evaluation). Then models fit on all of A score
   B and C. B: mean > 0 and one-sided p < 0.05 / 3 (SE clustered by market). C, only for the rules
   that pass B, once: mean > 0 and p < 0.05.
   Single-signal controls: for each frozen rule, the same policy and q with the single feature
   (either direction) whose top q on the same A out-of-sample rows had the best per-share pnl
   (>= 300 trades), frozen with the rules and evaluated beside them (its A number is therefore
   in-sample for the control; only B / C compare rule and control fairly).
   Sensitivities, reported beside the verdict and never used by it:
   - Queue: fs{x} = seconds from the entry fill to the first hit of the +x resting sell when a print
     must be ABOVE L (a print at exactly L may only have filled orders queued ahead at L; bids >= L
     count as before); strict_pnl re-prices the maker policies with it.
   - Effective bid: pnl_eff_{tk*, mk*} = the same policies when a sale may also match the other
     token's asks (Polymarket matches a sell of one token against sell orders of the other): the
     bid is max(own best bid, 1 - the other token's best ask), each candidate with >= 5 shares and
     its own token's book uncrossed, 1 - other ask below the token's own ask; maker fills also count
     1 - other ask >= L in fresh snapshots and taker SELL prints of the other token at <= 1 - L (= a
     buy of ours at >= L). MIX.md and the task price each token on its own book, so the verdict
     does; real taker sells print at 1 - the other ask much more often than at a dropped own bid
     (07-14), so own-book taker exits are an upper bound on the cost.
6. Report (Chinese, with units) and the traded rows of the frozen rules and their controls (with
   their strict-fill and effective-bid pnl).

Where MIX.md and the task are silent, the conservative choice made here:
- Resting sell latency: MIX.md says the sell is posted immediately after the buy fills; it is
  counted live 0.5 s after the entry fill (the same decision-to-match latency as every order
  here), not at the fill itself (prints and bids in (te, tl] do not count). If the book at tl
  already shows the token's best bid >= L (active, not crossed, fresh, >= 5 shares, tl before the
  end), the order would cross: it is sold at that bid as a taker (fee paid, min(20, bid size)
  shares), not as a maker. A bid >= L that is stale or thinner at tl does not cross; the order rests
  and the next qualifying event fills it at L.
- A bid fills a resting sell only in a fresh snapshot (the market's top of book changed <= 1 s
  before): a bid first reaching L is a change, so this only drops repeats of an old state.
- A print counts for a maker fill only when its taker side is "buy" on the bought token (a sell of
  the other token, a merge, is not counted in the verdict; the effective-bid sensitivity counts it),
  price >= L, receipt time after the order is live.
- Crossed snapshots (bid >= ask on the token) are never used for an entry, an exit or a maker
  fill; at an exit they are not a usable bid (the sale waits for the next usable one).
- A taker exit whose snapshot at tx is stale, thin (< 5 shares at the bid), crossed or shows no bid
  waits for the next usable bid (07-14: median 1.6 s later) instead of being held to settlement:
  settlement is a different exit and paid much better on exactly those rows (07-14: -1.3 c a share
  held against -6.9 c sold at the shown bid), so swapping it in let the models pick rows by their
  chance of a bad book at the exit.
- Book features at t use the last active snapshot at or before t (<= 1 s old) whatever its
  freshness (what a trader sees), NaN when crossed or missing; every price paid or received needs a
  fresh one.
- Each selected row is one trade: positions are not limited to one per market, a market can be
  traded at several decision points and both sides of one point can be selected; n counts these
  stacked entries and statistics are clustered by market (k = markets). The report gives trades per
  market and the number of both-side pairs.
- Units: 5 shares a trade (the ask shows >= 5), and min(20, ask size at entry, bid size at a taker
  exit) shares: the 20-share figures use the exit bid's size, an exit-time quantity, and leave the
  unsold remainder out; a maker fill is assumed to fill the whole order at L. Only the $ columns
  depend on this, not the per-share verdict. Capital is the entry cost (price + fee) x shares, tied
  from the entry fill to the exit fill, or to the market's end when held to settlement (the payout
  comes later; ignored). Days = UTC days with rows in the segment (for A out-of-sample: days in
  06-08 .. 07-15); a partial archive day counts as a full day.
- Single-signal controls: features whose top-q selection takes more than 1.5 q of the rows
  (ties: dir, is_jump, trend_agree, trend_side) are not candidates.
- Rows of markets that start outside 05-25 .. 08-29 are dropped; duplicate rows (a market in two
  archives) are dropped.
- Lockbox and frozen file:
  - Rules are frozen into <prefix>frozen.json only by a run that read every A archive of
    MANIFEST.txt. Any other run (`mix N`, names=..., an A archive that failed or was skipped) is a
    trial: it freezes into <workdir>/mix-trial-frozen.json (rewritten each time, never committed),
    never opens C, and its report says it is not a verdict.
  - A frozen file that already exists is not overwritten: it is used as is. It counts for a verdict
    only when it came from a full run, its A had rows, the current A rows have the same fingerprint,
    and its segments, alphas, policies, features, q's, models and minimum A trades equal this code's;
    otherwise the report says it is not a verdict and C stays closed (delete it to re-freeze).
  - C is opened only when every A, B and C archive of MANIFEST.txt was read (none failed after
    FETCH_TRIES verified downloads, none skipped for the time budget BUDGET_S). The first opening is
    recorded in <prefix>lockbox.json (frozen file sha256, C rows fingerprint, the C results); a later
    run opens C again only on the same frozen file and the same C rows (a repeat of the same
    computation), otherwise it shows the recorded first result and stays closed.
  - A download is checked against MANIFEST.txt's byte count and sha256 (urllib returns a short body
    silently when the server closes early); any error or mismatch is retried, FETCH_TRIES in all.
- Capital: per day, the time-weighted mean of the entry cost held, and the median and the maximum
  over the segment's days of the day's peak of concurrent entry cost (a day without a trade = 0).
- Factor predictions: the factor study's own walk-forward out-of-sample predictions (A + B of
  FACTORS.md, up to 08-15); from 08-16 the same walk-forward continued (each 7-day block of the
  factor study's C fit on rows whose target ended before the block, its ridge alpha and HGB
  settings), as factors.lockbox does for its frozen model. Missing -> NaN. The factor study chose
  its ridge alpha inside its own A (03-24 .. 06-30), which overlaps this A; B and C are after it.
- Settlement rule: A (05-25 .. 07-15) is all point-price settlement; B holds point days (07-17,
  07-18, 08-06), 30 s TWAP (08-12, 08-13) and 60 s TWAP (08-14, 08-15); C is all 60 s TWAP. The
  report splits each frozen rule's B and C results by settlement rule (the verdict does not).
- Binance prints arrive out of trade order (0.25% of the 06-02 prints by at most 0.64 s, 3.2% of
  the 07-17 prints by up to 8.9 s): sigma and rv30 use a receipt-clock grid, the jump reference and
  d only prints received by then, and the klines a 60 s guard on the latest exchange time received
  (see 2, 3). The report gives per archive the largest disorder, the jump points whose trade-time
  reference had not arrived (skipped here) and the points whose last minute still had a print in
  flight (diagnostics computed on the whole day, never features).
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import json
import math
import os
import platform
import re
import time
import zlib
from pathlib import Path

import numpy as np
import pandas as pd

import jump2s as j2
import jump2s_hf as hf

HERE = Path(__file__).resolve().parent
INPUTS = HERE / "real" / "mix-inputs"
MIGRATION_FACTOR_FILE = "factor_preds_5m_A.csv.gz"
MIGRATION_DVOL_FILE = "dvol_1h_A.csv.gz"
CANONICAL_STUDY_FROZEN = HERE / "real" / "cross-mix-frozen.json"

# --------------------------------------------------------------------------- MIX.md constants
JUMP_BP = 2.0
REF_MIN_S, REF_MAX_S = 1.0, 5.0
SPACING_S = 5.0
TAU_HI, TAU_LO = 285.0, 20.0
FIXED_TAUS = (240, 180, 120, 60)
LAT_S = 0.5                 # decision -> fill (entry and taker exit)
MAKER_LIVE_S = 0.5          # entry fill -> resting sell live
HOLDS = (5, 10, 30, 60)
MAKER_X = (1, 2, 3, 5)      # cents
MAKER_CAP = 0.99
FEE_RATE = 0.07
MIN_SHARES = 5.0
BAND = (0.02, 0.98)
BOOK_MAX_AGE_S = 1.0
PRICE_MAX_AGE_S = 5.0
H_ANCHOR_S = 2.0
RV_N, RV_MIN = 1800, 900
KLINE_GUARD_S = 60.0        # a 1 m kline counts once it closed >= 60 s before the latest exchange time received (disorder: 9.6 s 08-28, 38 s 08-19)
GAP_FILL_S = hf.GAP_FILL_S
CARRY_S = 1800
MIGRATION_DVOL_PRE_ROLL_S = 3600
# Closed-minute lookup plus the 60 s receipt guard need two extra minutes beyond the 240-minute return.
MIGRATION_KLINE_PRE_ROLL_S = (max(j2.RET4H_MIN, j2.VR_N + j2.VR_Q) + 2) * 60
SEC_YEAR = 365 * 86400
T_EPS = 1e-6
DAY = 86400

SEGMENTS = (("A", "2026-05-25", "2026-07-16"), ("B", "2026-07-16", "2026-08-16"), ("C", "2026-08-16", "2026-08-30"))
BLOCK_DAYS, MIN_TRAIN_DAYS = 7, 14
QS = (0.20, 0.10, 0.05, 0.02)
N_PICK, MIN_A_TRADES = 3, 300
RIDGE_ALPHA = 10.0
HGB_PARAMS = dict(max_depth=3, max_leaf_nodes=8, min_samples_leaf=400, learning_rate=0.05, max_iter=200,
                  l2_regularization=1.0, early_stopping=False, random_state=0)
MODELS = ("ridge", "hgb")
B_ALPHA, C_ALPHA = 0.05 / 3, 0.05
TIE_MAX = 1.5               # a control's top-q may take at most 1.5 q of the rows
BUDGET_S = 285 * 60         # stop reading archives after this (the lane has 330 min)
FETCH_TRIES = 3
FETCH_WAIT_S = 15           # x attempt number, between download attempts
ARCHIVE_DAYS = ("2026-05-25", "2026-08-30")  # archives read: dated in [from, to)
STRICT_TICK = 1e-6          # strict sensitivity: a print counts only at >= L + 1e-6 (ticks are >= 0.001)
TRIAL_FROZEN = "mix-trial-frozen.json"  # in the workdir: the freeze of a trial run (never committed)
HOW = {0: "持有到结算", 1: "吃单卖", 2: "挂单成交", 3: "挂单到达时直接吃买一"}
UNITS = (5, 20)

POLICIES = (["settle"] + [f"tk{h}" for h in HOLDS] + [f"mk{x}_{h}" for x in MAKER_X for h in HOLDS])
POLICY_NAMES = {"settle": "持有到结算", **{f"tk{h}": f"{h} 秒后吃单卖" for h in HOLDS},
                **{f"mk{x}_{h}": f"挂 +{x}¢ 卖、{h} 秒未成交吃单卖" for x in MAKER_X for h in HOLDS}}
FEATURES = ["jump_bp", "jump_abs_bp", "jump_z", "dir", "is_jump", "h_edge", "dmid2", "dmid10", "imb", "spread",
            "ask", "ask_fee", "tau", "trend_agree", "trend_side", "vr60", "f_ridge5", "f_hgb5", "f_ridge15",
            "f_hgb15", "dvol_rv"]
FEATURE_NAMES = {"jump_bp": "急动（这一方向，bp）", "jump_abs_bp": "急动大小（bp）", "jump_z": "急动 σ 倍数（这一方向）",
                 "dir": "急动方向是否这一方", "is_jump": "急动决策点", "h_edge": "H 公允价 − 卖一",
                 "dmid2": "中间价 2 秒变动", "dmid10": "中间价 10 秒变动", "imb": "买一卖一挂单量失衡",
                 "spread": "价差", "ask": "卖一价", "ask_fee": "卖一价的手续费", "tau": "剩余时间",
                 "trend_agree": "急动与 4 小时趋势同向", "trend_side": "这一方与 4 小时趋势同向", "vr60": "60 分钟方差比",
                 "f_ridge5": "多因子岭回归 5m", "f_hgb5": "多因子梯度提升 5m", "f_ridge15": "多因子岭回归 15m",
                 "f_hgb15": "多因子梯度提升 15m", "dvol_rv": "DVOL − 已实现波动"}
KINDS = ("jump", "fixed", "random")
META = ["day", "market", "start", "t", "kind", "side", "jdir", "price", "ask_size", "won"]


def fee(p):
    """Taker fee per share, 0.07 p (1 - p) (MIX.md)."""
    p = np.asarray(p, float)
    return FEE_RATE * p * (1.0 - p)


def ts(day):
    return int(pd.Timestamp(day, tz="UTC").timestamp())


def segment_of(start_s):
    """'A' / 'B' / 'C' by market start (s, UTC), '' outside."""
    s = np.asarray(start_s, float)
    out = np.full(s.shape, "", dtype=object)
    for name, a, b in SEGMENTS:
        out[(s >= ts(a)) & (s < ts(b))] = name
    return out


# --------------------------------------------------------------------------- inputs (factor preds, DVOL)
def build_inputs(cache=None, out_dir=INPUTS, start="2026-05-20", end="2026-09-01"):
    """real/mix-inputs/factor_preds_5m.csv.gz and dvol_1h.csv.gz from the factor study's cache (local only;
    the CI lane reads the files). Predictions: factors_study.pkl's walk-forward (out of sample, to
    08-15) and, from 08-16, the same walk-forward continued through the factor study's C blocks with
    its ridge alpha and HGB settings (factors.lockbox's procedure, for both models)."""
    import pickle

    import factors as F
    cache = Path(cache or F.CACHE)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(cache / "factors_study.pkl", "rb") as fh:
        R = pickle.load(fh)
    spec = json.loads((cache / "frozen.json").read_text())
    assert spec["features"] == F.FACTORS
    df = F.load_frame(cache)
    T = df.index.to_numpy()
    X = df[spec["features"]].to_numpy(float)

    def factory(name, alpha):
        return F.make_model(name, alpha, spec["hgb_params"])

    cols = {}
    c0 = F.ts(F.PERIODS["C"][0])
    for h in ("5m", "15m"):
        up = F.up_of(df[f"r_{h}"].to_numpy())
        for m in MODELS:
            ab = R["preds"][f"{m}_{h}"].reindex(df.index).to_numpy(float)
            blk = [(b0, b1) for b0, b1 in F.blocks(["C"]) if b0 < ts(end)]
            pc, _ = F.walk_forward(X, up, T, F.HMIN[h], m, blk, alpha=R["alpha"][h], factory=factory)
            cols[f"{m}_{h}"] = np.where(T < c0, ab, pc)
    p = pd.DataFrame(cols, index=pd.Index(T, name="T"))
    p = p[(p.index >= ts(start)) & (p.index < ts(end))].dropna(how="all").sort_index()
    if not p.index.is_unique or not p.index.is_monotonic_increasing:
        raise ValueError("factor input timestamps are not unique and sorted")
    p.round(5).to_csv(out_dir / "factor_preds_5m.csv.gz")
    d = pd.read_csv(cache / "dvol_1h.csv")
    d = pd.DataFrame({"hour": d["t"].to_numpy(np.int64) // 1000, "dvol": d["close"].to_numpy(float)})
    d = d[(d["hour"] >= ts(start) - 7 * DAY) & (d["hour"] < ts(end))].sort_values("hour")
    if d["hour"].duplicated().any() or not d["hour"].is_monotonic_increasing:
        raise ValueError("DVOL input timestamps are not unique and sorted")
    d.to_csv(out_dir / "dvol_1h.csv.gz", index=False)
    return p, d


def load_inputs(inputs=INPUTS):
    """(factor predictions indexed by window start s, DVOL by hour start s); empty frames when missing."""
    inputs = Path(inputs)
    fp, dv = pd.DataFrame(), pd.DataFrame(columns=["hour", "dvol"])
    if (inputs / "factor_preds_5m.csv.gz").exists():
        fp = pd.read_csv(inputs / "factor_preds_5m.csv.gz", index_col=0)
        fp.index = fp.index.astype(np.int64)
    if (inputs / "dvol_1h.csv.gz").exists():
        dv = pd.read_csv(inputs / "dvol_1h.csv.gz").sort_values("hour")
    return fp, dv


def _a_only_numeric_csv(path, expected_columns, start_s, end_s):
    """Read a physically A-only numeric CSV and reject any schema, order or boundary drift."""
    path = Path(path)
    if not path.exists():
        raise ValueError(f"missing migration input: {path}")
    opener = gzip.open if path.suffix == ".gz" else open
    rows = []
    with opener(path, "rt", encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        try:
            columns = next(reader)
        except StopIteration as error:
            raise ValueError(f"empty migration input: {path}") from error
        if columns != list(expected_columns):
            raise ValueError(f"migration input schema differs: {path}")
        last = -np.inf
        for raw in reader:
            if not raw:
                continue
            if len(raw) != len(columns):
                raise ValueError(f"malformed migration input row: {path}")
            try:
                stamp = int(raw[0])
            except (TypeError, ValueError) as error:
                raise ValueError(f"invalid migration timestamp in {path}") from error
            if stamp <= last:
                raise ValueError(f"migration input is not strictly time sorted: {path}")
            last = stamp
            if not start_s <= stamp < end_s:
                raise ValueError(f"migration input contains a non-A row: {path}")
            try:
                rows.append([stamp, *(float(value) for value in raw[1:])])
            except (TypeError, ValueError) as error:
                raise ValueError(f"invalid migration value in {path}") from error
    return pd.DataFrame(rows, columns=columns)


def load_migration_inputs(inputs, a_bounds):
    """Load dedicated frozen-A files; no opened file contains B/C values."""
    inputs = Path(inputs)
    a0, a1 = (ts(value) for value in a_bounds)
    raw_factor = _a_only_numeric_csv(
        inputs / MIGRATION_FACTOR_FILE,
        ("T", "ridge_5m", "hgb_5m", "ridge_15m", "hgb_15m"), a0, a1,
    )
    raw_dvol = _a_only_numeric_csv(
        inputs / MIGRATION_DVOL_FILE, ("hour", "dvol"), a0 - MIGRATION_DVOL_PRE_ROLL_S, a1,
    )
    factor = raw_factor.set_index("T")
    factor.index = factor.index.astype(np.int64)
    factor.index.name = "T"
    dvol = raw_dvol.assign(hour=raw_dvol["hour"].astype(np.int64)).sort_values("hour").reset_index(drop=True)
    want_factor = np.arange(a0, a1, 300, dtype=np.int64)
    want_dvol = np.arange(a0 - MIGRATION_DVOL_PRE_ROLL_S, a1, 3600, dtype=np.int64)
    if not np.array_equal(factor.index.to_numpy(np.int64), want_factor):
        raise ValueError("A-only factor input is incomplete")
    if not np.array_equal(dvol["hour"].to_numpy(np.int64), want_dvol):
        raise ValueError("A-only DVOL input is incomplete")
    return factor, dvol


def migration_kline_plan(a_bounds):
    """Use complete pre-A/A months, then daily files in the partial boundary month (never B)."""
    start = pd.Timestamp(a_bounds[0], tz="UTC") - pd.Timedelta(seconds=MIGRATION_KLINE_PRE_ROLL_S)
    end = pd.Timestamp(a_bounds[1], tz="UTC")
    months, days = [], []
    cursor = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    while cursor < end:
        next_month = cursor + pd.offsets.MonthBegin(1)
        if next_month <= end:
            months.append(f"{cursor.year:04d}-{cursor.month:02d}")
        else:
            day = max(cursor, start.normalize())
            while day < end:
                days.append(day.strftime("%Y-%m-%d"))
                day += pd.Timedelta(days=1)
        cursor = next_month
    return {"months": tuple(months), "days": tuple(days)}


def migration_input_sources(inputs):
    inputs = Path(inputs)
    return {
        "factor": {"name": MIGRATION_FACTOR_FILE, "sha256": _sha256(inputs / MIGRATION_FACTOR_FILE)},
        "dvol": {"name": MIGRATION_DVOL_FILE, "sha256": _sha256(inputs / MIGRATION_DVOL_FILE)},
    }


def migration_kline_sources(workdir, plan):
    workdir = Path(workdir)
    out = []
    for kind in ("months", "days"):
        source_kind = "monthly" if kind == "months" else "daily"
        for value in plan[kind]:
            path = workdir / f"BTCUSDT-1m-{value}.zip"
            out.append({"kind": source_kind, "name": path.name, "sha256": _sha256(path)})
    return out


def dvol_at(dv, t):
    """DVOL (percent) of the last hourly candle closed by t (known at its hour's end), NaN when none."""
    t = np.asarray(t, float)
    if dv is None or not len(dv):
        return np.full(t.shape, np.nan)
    known = dv["hour"].to_numpy(float) + 3600.0
    k = np.searchsorted(known, t + T_EPS, "right") - 1
    v = dv["dvol"].to_numpy(float)
    return np.where(k >= 0, v[np.maximum(k, 0)], np.nan)


# --------------------------------------------------------------------------- Binance
class Spot:
    """A day's Binance prints (with the carried tail of the previous archive): trade-time order (ts,
    lp, rv, d = move against the 1-5 s reference received by the print's own receipt time), receipt
    order, the per-second grid (sigma) and rv30."""

    def __init__(self, binance):
        b = binance[["trade_ts_ms", "recv_ts_ms", "price"]].apply(pd.to_numeric, errors="coerce")
        v = b.to_numpy(float)
        b = b[np.isfinite(v).all(axis=1) & (v[:, 2] > 0)].drop_duplicates()
        b = b.sort_values(["trade_ts_ms", "recv_ts_ms"], kind="stable")
        self.ts = b["trade_ts_ms"].to_numpy(float) / 1000.0
        self.rv = b["recv_ts_ms"].to_numpy(float) / 1000.0
        px = b["price"].to_numpy(float)
        self.lp = np.log(px)
        j = received_ref(self.ts, self.rv)
        jj = np.maximum(j, 0)
        ok = (j >= 0) & (self.ts - self.ts[jj] <= REF_MAX_S + T_EPS) if len(self.ts) else np.zeros(0, bool)
        self.d = np.where(ok, self.lp - self.lp[jj], np.nan) if len(self.ts) else np.zeros(0)
        self.order = np.argsort(self.rv, kind="stable")  # receipt order -> trade-sorted index
        self.rv_o = self.rv[self.order]
        # latest trade time among the prints received so far, in receipt order
        self.tmax_o = np.maximum.accumulate(self.ts[self.order]) if len(self.ts) else np.zeros(0)
        # diagnostics only (they use the whole day): cmax[k] = latest receipt among the first k + 1 prints by
        # trade time; disorder = how much later than a print an earlier-received print had traded (max, s)
        self.cmax = np.maximum.accumulate(self.rv) if len(self.rv) else np.zeros(0)
        self.disorder = float(np.max(np.r_[0.0, self.tmax_o[:-1] - self.ts[self.order][1:]])) if len(self.ts) > 1 else 0.0
        # per-second grid on the RECEIPT clock: second s holds the last print received in [s, s + 1), so
        # sigma / rv30 at floor(t) - 1 only use prints received before t (0.25% of the 06-02 prints
        # arrived after a print traded up to 0.64 s later; a trade-time grid could use one of them)
        self.sp = j2.Seconds(self.rv, px, ffill=GAP_FILL_S)
        if len(self.sp.lp):
            r = pd.Series(self.sp.lp).diff()
            self.rv30 = (r.rolling(RV_N, min_periods=RV_MIN).std() * math.sqrt(SEC_YEAR)).to_numpy(float)
        else:
            self.rv30 = np.array([])

    def last(self, t, max_age=np.inf):
        """Trade-sorted index of the last print received at or before t (-1 when none or older than max_age)."""
        t = np.asarray(t, float)
        if not len(self.rv_o):
            return np.full(t.shape, -1)
        k = np.searchsorted(self.rv_o, t + T_EPS, "right") - 1
        kk = np.maximum(k, 0)
        ok = (k >= 0) & (t - self.rv_o[kk] <= max_age + T_EPS)
        return np.where(ok, self.order[kk], -1)

    def at(self, arr, i):
        """arr[i] with NaN where i < 0."""
        return np.where(i >= 0, arr[np.maximum(i, 0)], np.nan) if len(arr) else np.full(np.shape(i), np.nan)

    def seen_through(self, t):
        """Latest trade (exchange) time among the prints received by t (NaN when none): a function of the
        prints received by t only."""
        t = np.asarray(t, float)
        if not len(self.ts):
            return np.full(t.shape, np.nan)
        k = np.searchsorted(self.rv_o, t + T_EPS, "right") - 1
        return np.where(k >= 0, self.tmax_o[np.maximum(k, 0)], np.nan)

    def in_flight(self, t, upto):
        """Diagnostic (uses the whole day): was a print traded before `upto` still unreceived at t?"""
        t, upto = np.asarray(t, float), np.asarray(upto, float)
        k = np.searchsorted(self.ts, upto, "left") - 1
        ok = np.isfinite(upto) & (k >= 0)
        return ok & (self.cmax[np.clip(k, 0, max(len(self.cmax) - 1, 0))] > t + T_EPS) if len(self.ts) else np.zeros(t.shape, bool)


def received_ref(ts, rv):
    """Per print i (trade-time order): the index of the last print by trade time at least REF_MIN_S
    earlier that had been received by rv[i] (a reference received after the trigger is skipped for the
    last earlier one received in time), -1 when none; the caller applies the REF_MAX_S age limit."""
    ts, rv = np.asarray(ts, float), np.asarray(rv, float)
    if not len(ts):
        return np.zeros(0, np.int64)
    j = np.searchsorted(ts, ts - REF_MIN_S + T_EPS, "right") - 1
    bad = np.flatnonzero((j >= 0) & (rv[np.maximum(j, 0)] > rv + T_EPS))
    if len(bad):
        lo = np.searchsorted(ts, ts[bad] - REF_MAX_S - T_EPS, "left")
        for i, a in zip(bad, lo):
            k = np.flatnonzero(rv[a:j[i]] <= rv[i] + T_EPS)
            j[i] = a + int(k[-1]) if len(k) else -1
    return j


def jump_points(spot, win, own_from=-np.inf):
    """MIX.md jump decision points: prints (trade-time reference, received at t) with |d| >= 2 bp, 285 >=
    end - t >= 20 s, per market >= 5 s after the last kept point. Only prints with trade time >=
    own_from (the archive's own prints, not the carried tail) can be jumps.
    -> DataFrame market, t, i (trade-sorted index)."""
    if not len(spot.ts) or not len(win):
        return pd.DataFrame(columns=["market", "t", "i"])
    with np.errstate(invalid="ignore"):
        cand = np.flatnonzero((np.abs(spot.d) * 1e4 >= JUMP_BP - 1e-9) & (spot.ts >= own_from))
    ci = cand[np.argsort(spot.rv[cand], kind="stable")]
    co = spot.rv[ci]
    out = []
    for m, end in zip(win["market"], win["end"]):
        a = np.searchsorted(co, end - TAU_HI - T_EPS, "left")
        b = np.searchsorted(co, end - TAU_LO + T_EPS, "right")
        k, tt = 0, co[a:b]
        while k < len(tt):
            out.append((m, tt[k], ci[a + k]))
            k = max(k + 1, int(np.searchsorted(tt, tt[k] + SPACING_S - T_EPS, "left")))
    return pd.DataFrame(out, columns=["market", "t", "i"])


def plan(spot, win, own_from=-np.inf):
    """Decision points of a day: jump, fixed and random (one per jump point, seeded per market);
    columns market, start, end, t, kind, i (the print giving d: the trigger for jumps, the last received
    by t otherwise), side (random only; 0 = both sides)."""
    jp = jump_points(spot, win, own_from)
    st = dict(zip(win["market"], win["start"]))
    en = dict(zip(win["market"], win["end"]))
    parts = []
    if len(jp):
        parts.append(jp.assign(kind="jump", side=0))
    fx = [(m, e - tau) for m, e in zip(win["market"], win["end"]) for tau in FIXED_TAUS]
    if fx:
        f = pd.DataFrame(fx, columns=["market", "t"])
        parts.append(f.assign(kind="fixed", side=0, i=spot.last(f["t"].to_numpy(float), PRICE_MAX_AGE_S)))
    rnd = []
    for m, g in jp.groupby("market", sort=False) if len(jp) else []:
        rng = np.random.default_rng(zlib.crc32(str(m).encode()))
        t = rng.uniform(en[m] - TAU_HI, en[m] - TAU_LO, len(g))
        sd = rng.choice(np.array([-1, 1]), len(g))
        rnd.append(pd.DataFrame({"market": m, "t": t, "side": sd}))
    if rnd:
        r = pd.concat(rnd, ignore_index=True)
        parts.append(r.assign(kind="random", i=spot.last(r["t"].to_numpy(float), PRICE_MAX_AGE_S)))
    if not parts:
        return pd.DataFrame(columns=["market", "start", "end", "t", "kind", "i", "side"])
    p = pd.concat(parts, ignore_index=True)
    p["start"] = p["market"].map(st).astype(float)
    p["end"] = p["market"].map(en).astype(float)
    return p[["market", "start", "end", "t", "kind", "i", "side"]]


# --------------------------------------------------------------------------- books
class Books:
    """The day's 5m snapshots grouped by market (sorted, one row per timestamp), with the time of each
    market's last top-of-book change (jump2s_hf / cross.book_health)."""

    COLS = ("up_best_bid", "up_best_ask", "down_best_bid", "down_best_ask", "up_bid_size", "up_ask_size",
            "down_bid_size", "down_ask_size")

    def __init__(self, feat, markets):
        f = feat[feat["market_id"].astype(str).isin(set(markets))] if len(feat) else feat
        self.seg = {}
        if not len(f):
            return
        f = f.assign(market_id=f["market_id"].astype(str)).sort_values(["market_id", "timestamp_ms"], kind="stable")
        f = f.drop_duplicates(["market_id", "timestamp_ms"], keep="last")
        mk = f["market_id"].to_numpy()
        self.T = f["timestamp_ms"].to_numpy(float) / 1000.0
        self.c = {c: (f[c].to_numpy(float) if c in f else np.full(len(f), np.nan)) for c in self.COLS}
        state = f["lifecycle_state"].astype(str).str.lower().to_numpy() if "lifecycle_state" in f else \
            np.full(len(f), "active")
        halt = f["observed_halt_flag"].eq(True).to_numpy(bool) if "observed_halt_flag" in f else np.zeros(len(f), bool)
        self.live = (state == "active") & ~halt
        stv = f[[c for c in hf.STATE_COLS if c in f]].to_numpy(float)
        same = (stv[1:] == stv[:-1]) | (np.isnan(stv[1:]) & np.isnan(stv[:-1]))
        moved = np.r_[True, ~same.all(axis=1) | (mk[1:] != mk[:-1])]
        self.last_chg = pd.Series(np.where(moved, self.T, np.nan)).ffill().to_numpy(float)
        self.seg = hf._segments(mk)

    def side(self, g, s, ok):
        """(bid, ask, bid size, ask size) of side s at global rows g, NaN where not ok."""
        up = s > 0
        out = []
        for u, d in (("up_best_bid", "down_best_bid"), ("up_best_ask", "down_best_ask"),
                     ("up_bid_size", "down_bid_size"), ("up_ask_size", "down_ask_size")):
            out.append(np.where(ok, np.where(up, self.c[u][g], self.c[d][g]), np.nan))
        return out


def _asof(T, a, b, t, max_age=BOOK_MAX_AGE_S):
    """Global index of the last snapshot of segment [a, b) at or before t (<= max_age old), -1 when none."""
    k = j2.asof_idx(T[a:b], t, max_age)
    return np.where(k >= 0, k + a, -1)


def _quote(bk, g, s):
    """Side quote at global rows g (-1: none): (bid, ask, bid size, ask size, usable) where usable =
    active and not crossed (bid < ask when both are shown)."""
    has = g >= 0
    gg = np.maximum(g, 0)
    ok = has & bk.live[gg]
    bid, ask, bsz, asz = bk.side(gg, s, ok)
    with np.errstate(invalid="ignore"):
        crossed = np.isfinite(bid) & np.isfinite(ask) & (bid >= ask)
    ok &= ~crossed
    return np.where(ok, bid, np.nan), np.where(ok, ask, np.nan), np.where(ok, bsz, np.nan), \
        np.where(ok, asz, np.nan), ok


def _fresh(bk, g, t):
    gg = np.maximum(g, 0)
    with np.errstate(invalid="ignore"):
        return (g >= 0) & (t - bk.last_chg[gg] <= BOOK_MAX_AGE_S + T_EPS)


# --------------------------------------------------------------------------- one archive
def label_cols():
    cols = []
    for p in POLICIES:
        cols += [f"pnl_{p}", f"hold_{p}", f"how_{p}", f"sh20_{p}"]
    return cols


# strict-fill sensitivity (not MIX.md's rule): seconds from the entry fill to the first hit of the resting
# sell at +x when a print must be ABOVE L (a print at L may have filled only orders ahead in the queue)
STRICT_COLS = [f"fs{x}" for x in MAKER_X]
# effective-bid sensitivity (not MIX.md's rule): the taker and maker policies when a sale may also match the
# other token's asks, bid = max(own best bid, 1 - other best ask)
EFF_POLICIES = [p for p in POLICIES if p != "settle"]
EFF_COLS = [f"pnl_eff_{p}" for p in EFF_POLICIES]
ROW_COLS = META + FEATURES + label_cols() + STRICT_COLS + EFF_COLS


def _empty_rows():
    return pd.DataFrame({c: pd.Series(dtype="float32") for c in ROW_COLS})


def day_rows(feat, mkts, binance, prints, carry=None, factor=None, dvol=None, closes=None, day=""):
    """Rows (ROW_COLS) of one archive and a dict of counts.

    feat / mkts / binance / prints as from cross.read_day + cross.market_table, cross.read_binance and
    cross.read_poly_trades; carry: the previous archive's last Binance prints (lookups only);
    factor: load_inputs()[0]; dvol: load_inputs()[1]; closes: Binance minute closes (trend)."""
    info = dict(day=day, markets=0, jump=0, fixed=0, random=0, rows=0, no_entry=0, prints=len(binance))
    if mkts is None or mkts.empty or binance is None or binance.empty:
        return _empty_rows(), info
    m5 = mkts[(mkts["horizon"] == 5) & mkts["up_won"].notna()].copy()
    m5["market_id"] = m5["market_id"].astype(str)
    m5 = m5.drop_duplicates("market_id", keep="last")
    info["markets"] = len(m5)
    if m5.empty:
        return _empty_rows(), info
    win = pd.DataFrame({"market": m5["market_id"].to_numpy(), "start": m5["start"].to_numpy(float) / 1000.0,
                        "end": m5["end"].to_numpy(float) / 1000.0})
    own = pd.to_numeric(binance["trade_ts_ms"], errors="coerce")
    own_from = float(own.min()) / 1000.0 if own.notna().any() else -np.inf
    both = binance if carry is None or carry.empty else pd.concat([carry, binance], ignore_index=True)
    spot = Spot(both)
    p = plan(spot, win, own_from)
    for k in KINDS:
        info[k] = int((p["kind"] == k).sum())
    if p.empty:
        return _empty_rows(), info

    # ---- point-level Binance features (before the sides are split)
    i = p["i"].to_numpy(np.int64)
    t = p["t"].to_numpy(float)
    d = spot.at(spot.d, i)
    t_known = spot.seen_through(t)  # latest exchange time received by t
    sigma = spot.sp.sigma(t)  # receipt-clock grid: the seconds before floor(t)
    sec = np.floor(t).astype(np.int64) - 1
    rv30 = spot.sp._at(spot.rv30, sec) if len(spot.rv30) else np.full(len(p), np.nan)
    lp_now = spot.at(spot.lp, spot.last(t, PRICE_MAX_AGE_S))
    lp_ago = spot.at(spot.lp, spot.last(t - H_ANCHOR_S, PRICE_MAX_AGE_S))
    info["disorder_s"] = spot.disorder
    # diagnostics (whole day): jump points whose trade-time reference had not arrived (now skipped), and
    # points whose last kline minute still had a print in flight at t despite the guard
    jt = np.searchsorted(spot.ts, spot.ts - REF_MIN_S + T_EPS, "right") - 1
    ij = i[(p["kind"] == "jump").to_numpy() & (i >= 0)]
    info["ref_late"] = int(np.sum((jt[ij] >= 0) & (spot.rv[np.maximum(jt[ij], 0)] > spot.rv[ij] + T_EPS))) if len(ij) else 0
    m_end = np.floor((t_known - KLINE_GUARD_S) / 60.0) * 60.0
    info["kline_late"] = int(spot.in_flight(t, m_end).sum())
    if closes is not None and len(closes):
        ret4h, vr60 = j2.trend_features(closes, t_known - KLINE_GUARD_S)
    else:
        ret4h, vr60 = np.full(len(p), np.nan), np.full(len(p), np.nan)
    dv = dvol_at(dvol, t) / 100.0
    start = p["start"].to_numpy(float)
    fpred = {}
    for c, col in (("f_ridge5", "ridge_5m"), ("f_hgb5", "hgb_5m"), ("f_ridge15", "ridge_15m"), ("f_hgb15", "hgb_15m")):
        if factor is not None and len(factor) and col in factor:
            fpred[c] = factor[col].reindex(np.round(start).astype(np.int64)).to_numpy(float)
        else:
            fpred[c] = np.full(len(p), np.nan)
    w = j2.settle_twap(start)
    t_el = t - start
    import binary as bo
    fac = np.where(w > 0, bo.twap_std_factor(t_el, bo.WINDOW_S, np.maximum(w, 1)),
                   np.sqrt(np.maximum(bo.WINDOW_S - t_el, 1.0)))

    # ---- one row per side (random rows: their own side)
    kind = p["kind"].to_numpy()
    two = kind != "random"
    rep = np.where(two, 2, 1)
    idx = np.repeat(np.arange(len(p)), rep)
    side = np.empty(len(idx), np.int64)
    pos = np.cumsum(rep) - rep
    side[pos] = np.where(two, 1, p["side"].to_numpy(np.int64))
    side[pos[two] + 1] = -1
    n = len(idx)
    R = {"market": p["market"].to_numpy()[idx], "start": start[idx], "end": p["end"].to_numpy(float)[idx],
         "t": t[idx], "kind": kind[idx], "side": side}
    s = side.astype(float)
    dd = d[idx]
    with np.errstate(invalid="ignore", divide="ignore"):
        R["jump_bp"] = s * dd * 1e4
        R["jump_abs_bp"] = np.abs(dd) * 1e4
        R["jump_z"] = np.where(sigma[idx] > 0, s * dd / sigma[idx], np.nan)
        R["dir"] = np.sign(s * dd)
        R["jdir"] = np.nan_to_num(np.sign(dd)).astype(np.int8)
        R["is_jump"] = (R["kind"] == "jump").astype(float)
        R["tau"] = R["end"] - R["t"]
        r4 = ret4h[idx]
        R["trend_agree"] = np.where(np.isfinite(r4) & np.isfinite(dd), np.sign(dd) * np.sign(r4), np.nan)
        R["trend_side"] = np.where(np.isfinite(r4), np.sign(s * r4), np.nan)
        R["vr60"] = vr60[idx]
        for c, v in fpred.items():
            R[c] = s * (v[idx] - 0.5)
        R["dvol_rv"] = dv[idx] - rv30[idx]
    move = (lp_now - lp_ago)[idx]
    scale = (fac * sigma)[idx]
    with np.errstate(invalid="ignore"):
        scale = np.where(scale > 0, scale, np.nan)  # no vol estimate -> no H fair

    # ---- book features, entry and exits, per market
    bk = Books(feat, win["market"])
    tok = {}
    for m, u, dn in zip(m5["market_id"], m5.get("up_token", [None] * len(m5)), m5.get("down_token", [None] * len(m5))):
        for tk, sd in ((u, 1), (dn, -1)):
            if tk is not None and str(tk) not in ("", "None", "nan"):
                tok[str(tk)] = (m, sd)
    pr_by, pr_eff = _print_events(prints, tok)
    F = {c: np.full(n, np.nan) for c in ("h_edge", "dmid2", "dmid10", "imb", "spread", "price_t")}
    price, asz_e = np.full(n, np.nan), np.full(n, np.nan)
    L = {f"{k}_{pol}": np.full(n, np.nan) for pol in POLICIES for k in ("pnl", "hold", "how", "sh20")}
    S = {c: np.full(n, np.nan) for c in STRICT_COLS}
    E = {c: np.full(n, np.nan) for c in EFF_COLS}
    won_up = dict(zip(m5["market_id"], m5["up_won"].astype(float)))
    won = np.where(s > 0, np.array([won_up[m] for m in R["market"]]), 1 - np.array([won_up[m] for m in R["market"]]))
    order = pd.Series(np.arange(n)).groupby(R["market"], sort=False).indices
    for m, ix in order.items():
        if m not in bk.seg:
            continue
        a, b = bk.seg[m]
        tt, ss, end = R["t"][ix], s[ix], R["end"][ix]
        # features at t (any freshness), t - 2, t - 10
        g = _asof(bk.T, a, b, tt)
        bid, ask, bsz, asz, ok = _quote(bk, g, ss)
        mid = (bid + ask) / 2
        with np.errstate(invalid="ignore", divide="ignore"):
            F["spread"][ix] = ask - bid
            F["imb"][ix] = (bsz - asz) / (bsz + asz)
            F["price_t"][ix] = ask
            for lag, c in ((2.0, "dmid2"), (10.0, "dmid10")):
                b0, a0, _, _, _ = _quote(bk, _asof(bk.T, a, b, tt - lag), ss)
                F[c][ix] = mid - (b0 + a0) / 2
            ub, ua, _, _, _ = _quote(bk, _asof(bk.T, a, b, tt - H_ANCHOR_S), np.ones(len(ix)))
            prior = np.clip((ub + ua) / 2, 0.005, 0.995)
            from scipy.stats import norm
            p1 = norm.cdf(norm.ppf(prior) + move[ix] / scale[ix])
            fair = np.where(ss > 0, p1, 1 - p1)
            F["h_edge"][ix] = fair - ask
        # entry at t + 0.5
        te = tt + LAT_S
        ge = _asof(bk.T, a, b, te)
        _, pe, _, se, oke = _quote(bk, ge, ss)
        with np.errstate(invalid="ignore"):
            enter = oke & _fresh(bk, ge, te) & j2.price_ok(pe, se) & (te < end - T_EPS)
        if not enter.any():
            continue
        ix, te, ss, end, pe, se = ix[enter], te[enter], ss[enter], end[enter], pe[enter], se[enter]
        price[ix], asz_e[ix] = pe, se
        for sd in (1, -1):
            k = ss == sd
            if k.any():
                _label_side(SideBook(bk, a, b, sd), ix[k], te[k], end[k], pe[k], se[k], won[ix[k]],
                            pr_by.get((m, sd)), pr_eff.get((m, sd)), L, S, E)
    keep = np.isfinite(price)
    info["no_entry"] = int((~keep & (R["kind"] != "random")).sum())
    out = {"day": day, "market": R["market"], "start": np.round(R["start"]).astype(np.int64), "t": R["t"],
           "kind": R["kind"], "side": R["side"].astype(np.int8), "jdir": R["jdir"], "price": price,
           "ask_size": asz_e, "won": won}
    for c in FEATURES:
        if c == "ask":
            out[c] = F["price_t"]
        elif c == "ask_fee":
            out[c] = fee(F["price_t"])
        elif c in F:
            out[c] = F[c]
        else:
            out[c] = R[c]
    out.update(L)
    out.update({c: np.where(np.isfinite(v), v, np.nan) for c, v in S.items()})
    out.update(E)
    rows = pd.DataFrame(out)[keep].reset_index(drop=True)
    info["rows"] = len(rows)
    return compact(rows), info


class SideBook:
    """One market's snapshots seen from side sd. A sellable bid ("own": the verdict) is the token's own
    best bid; "eff" (sensitivity) is max(own best bid, 1 - the other token's best ask). Each candidate
    needs an active snapshot, its token's book not crossed, a price > 0 and >= 5 shares at it; 1 - other
    ask also has to stay below this token's own ask (otherwise the snapshot pairs a stale book with a
    fresh one). Maker bid events: fresh snapshots, any size (own bid; eff adds 1 - other ask)."""

    def __init__(self, bk, a, b, sd):
        self.T, self.lc = bk.T[a:b], bk.last_chg[a:b]
        live = bk.live[a:b]
        me, other = ("up", "down") if sd > 0 else ("down", "up")
        ob, oa, obs = (bk.c[f"{me}_best_bid"][a:b], bk.c[f"{me}_best_ask"][a:b], bk.c[f"{me}_bid_size"][a:b])
        xb, xa, xas = (bk.c[f"{other}_best_bid"][a:b], bk.c[f"{other}_best_ask"][a:b], bk.c[f"{other}_ask_size"][a:b])
        n = len(self.T)
        with np.errstate(invalid="ignore"):
            own_ok = live & ~(np.isfinite(ob) & np.isfinite(oa) & (ob >= oa))
            oth_ok = live & ~(np.isfinite(xb) & np.isfinite(xa) & (xb >= xa))
            self.fresh = self.T - self.lc <= BOOK_MAX_AGE_S + T_EPS
            own = own_ok & np.isfinite(ob) & (ob > 0) & np.isfinite(obs) & (obs >= MIN_SHARES - 1e-12)
            # 1 - other ask counts only while the two books are consistent: below this token's own ask
            # (else one book is stale and the two asks would already have been merged)
            consistent = oth_ok & np.isfinite(xa) & (xa < 1) & ~(np.isfinite(oa) & (1.0 - xa >= oa - 1e-9))
            comp = consistent & np.isfinite(xas) & (xas >= MIN_SHARES - 1e-12)
            use_c = comp & (~own | (1.0 - xa > ob))
            ev_own = np.where(own_ok & self.fresh & np.isfinite(ob), ob, -np.inf)
            ev_cmp = np.where(consistent & self.fresh, 1.0 - xa, -np.inf)
        self.sell = {"own": (np.where(own, ob, np.nan), np.where(own, obs, np.nan)),
                     "eff": (np.where(use_c, 1.0 - xa, np.where(own, ob, np.nan)),
                             np.where(use_c, xas, np.where(own, obs, np.nan)))}
        self.nxt = {}
        for kind, (px, _) in self.sell.items():  # first index >= k with a usable bid in a fresh snapshot (n: none)
            idx = np.where(np.isfinite(px) & self.fresh, np.arange(n), n)
            self.nxt[kind] = np.minimum.accumulate(idx[::-1])[::-1]
        self.bid_ev = {"own": ev_own, "eff": np.maximum(ev_own, ev_cmp)}

    def at(self, kind, t, end):
        """Usable bid at t: the snapshot at or before t (<= 1 s old) with a usable bid, the book changed
        <= 1 s before t, t before end -> (ok, price, size)."""
        px, sz = self.sell[kind]
        g = j2.asof_idx(self.T, t, BOOK_MAX_AGE_S)
        gg = np.maximum(g, 0)
        if not len(self.T):
            z = np.full(np.shape(t), np.nan)
            return np.zeros(np.shape(t), bool), z, z
        with np.errstate(invalid="ignore"):
            ok = (g >= 0) & np.isfinite(px[gg]) & (t - self.lc[gg] <= BOOK_MAX_AGE_S + T_EPS) & (t < end - T_EPS)
        return ok, np.where(ok, px[gg], np.nan), np.where(ok, sz[gg], np.nan)

    def exit(self, kind, tx, end):
        """Taker sale sent at tx: the usable bid at tx, else the first later fresh snapshot with a usable
        bid before end -> (sold, price, size, fill time)."""
        ok, p, s = self.at(kind, tx, end)
        n = len(self.T)
        if not n:
            return ok, p, s, np.full(np.shape(tx), np.nan)
        px, sz = self.sell[kind]
        k0 = np.searchsorted(self.T, tx + T_EPS, "right")
        k = np.where(k0 < n, self.nxt[kind][np.minimum(k0, n - 1)], n)
        kk = np.minimum(k, n - 1)
        later = ~ok & (k < n) & (self.T[kk] < end - T_EPS) & (tx < end - T_EPS)
        return (ok | later, np.where(ok, p, np.where(later, px[kk], np.nan)),
                np.where(ok, s, np.where(later, sz[kk], np.nan)), np.where(ok, tx, np.where(later, self.T[kk], np.nan)))

    def events(self, kind, pr):
        """Time-sorted (times, values, is-print) at which a resting sell could be hit: the fresh bid
        events and the prints pr = (receipt s, price) (taker buys of the token; eff: also taker sells of
        the other token at 1 - price)."""
        T, v = self.T, self.bid_ev[kind]
        isp = np.zeros(len(T), bool)
        if pr is not None and len(pr[0]):
            T = np.concatenate([T, pr[0]])
            v = np.concatenate([v, pr[1]])
            isp = np.concatenate([isp, np.ones(len(pr[0]), bool)])
            o = np.argsort(T, kind="stable")
            T, v, isp = T[o], v[o], isp[o]
        return T, v, isp


def _print_events(prints, tok):
    """Per (market, side): (receipt s, price) of the taker BUY prints of the side's token (the verdict's
    maker fills) and, for the effective-bid sensitivity, those plus the taker SELL prints of the other
    token at 1 - price (a sell of the other token at q can match a resting sell of ours at <= 1 - q)."""
    buys, eff = {}, {}
    if prints is None or not len(prints) or not tok:
        return buys, eff
    q = prints[prints["instrument"].astype(str).isin(tok)]
    if not len(q):
        return buys, eff
    key = [tok[x] for x in q["instrument"].astype(str).to_numpy()]
    mk = np.array([k[0] for k in key], dtype=object)
    sd = np.array([k[1] for k in key], np.int64)
    side = q["taker_side"].astype(str).str.lower().to_numpy()
    t = q["recv_ts_ms"].to_numpy(float) / 1000.0
    p = q["price"].to_numpy(float)
    b_, s_ = side == "buy", side == "sell"
    df = pd.DataFrame({"m": np.concatenate([mk[b_], mk[s_]]), "sd": np.concatenate([sd[b_], -sd[s_]]),
                       "t": np.concatenate([t[b_], t[s_]]), "p": np.concatenate([p[b_], 1.0 - p[s_]]),
                       "buy": np.concatenate([np.ones(int(b_.sum()), bool), np.zeros(int(s_.sum()), bool)])})
    for (m, d), g in df.groupby(["m", "sd"], sort=False):
        g = g.sort_values("t", kind="stable")
        eff[(m, int(d))] = (g["t"].to_numpy(float), g["p"].to_numpy(float))
        gb = g[g["buy"].to_numpy(bool)]
        if len(gb):
            buys[(m, int(d))] = (gb["t"].to_numpy(float), gb["p"].to_numpy(float))
    return buys, eff


def _label_side(sb, ix, te, end, pe, se, w, pr, pr_e, L, S, E):
    """Execution labels of the entered rows ix of one side of one market (SideBook sb) into L (verdict),
    S (queue sensitivity) and E (effective-bid sensitivity)."""
    cost = pe + fee(pe)
    settle = w - cost
    hold_end = end - te
    sh_e = np.minimum(20.0, se)
    L["pnl_settle"][ix], L["hold_settle"][ix], L["how_settle"][ix], L["sh20_settle"][ix] = settle, hold_end, 0, sh_e
    tk, tk_e = {}, {}
    for h in HOLDS:
        tx = te + h + LAT_S
        sold, bx, bsx, tf = sb.exit("own", tx, end)
        pnl = np.where(sold, bx - fee(bx) - cost, settle)
        hold = np.where(sold, tf - te, hold_end)
        sh = np.where(sold, np.minimum(sh_e, bsx), sh_e)
        tk[h] = (pnl, hold, sold.astype(float), sh)
        L[f"pnl_tk{h}"][ix], L[f"hold_tk{h}"][ix], L[f"how_tk{h}"][ix], L[f"sh20_tk{h}"][ix] = tk[h]
        sold_e, be, _, _ = sb.exit("eff", tx, end)
        tk_e[h] = np.where(sold_e, be - fee(be) - cost, settle)
        E[f"pnl_eff_tk{h}"][ix] = tk_e[h]
    # resting sells: live at tl; marketable on arrival -> taker at that bid (a usable bid at tl, as any
    # taker sale); never after the market's end
    tl = te + MAKER_LIVE_S
    cross_ok, bl, bsl = sb.at("own", tl, end)
    cross_e, ble, _ = sb.at("eff", tl, end)
    ev_t, ev_v, ev_p = sb.events("own", pr)
    ev_s = np.where(ev_p, ev_v - STRICT_TICK, ev_v)  # strict: a print must be above L
    ee_t, ee_v, _ = sb.events("eff", pr_e)
    t_to = np.minimum(te + max(HOLDS), end)
    for x in MAKER_X:
        lim = np.minimum(np.round(pe + x / 100.0, 6), MAKER_CAP)
        first = _first_hit(ev_t, ev_v, tl, t_to, lim)
        S[f"fs{x}"][ix] = _first_hit(ev_t, ev_s, tl, t_to, lim) - te
        first_e = _first_hit(ee_t, ee_v, tl, t_to, lim)
        with np.errstate(invalid="ignore"):
            mkt = cross_ok & (bl >= lim - 1e-9)
            mkt_e = cross_e & (ble >= lim - 1e-9)
        for h in HOLDS:
            pol = f"mk{x}_{h}"
            filled = ~mkt & (first <= np.minimum(te + h, end) + T_EPS)
            tp, th, tw, tsh = tk[h]
            L[f"pnl_{pol}"][ix] = np.where(mkt, bl - fee(bl) - cost, np.where(filled, lim - cost, tp))
            L[f"hold_{pol}"][ix] = np.where(mkt, MAKER_LIVE_S, np.where(filled, first - te, th))
            L[f"how_{pol}"][ix] = np.where(mkt, 3.0, np.where(filled, 2.0, tw))
            L[f"sh20_{pol}"][ix] = np.where(mkt, np.minimum(sh_e, bsl), np.where(filled, sh_e, tsh))
            filled_e = ~mkt_e & (first_e <= np.minimum(te + h, end) + T_EPS)
            E[f"pnl_eff_{pol}"][ix] = np.where(mkt_e, ble - fee(ble) - cost, np.where(filled_e, lim - cost, tk_e[h]))


def _first_hit(ev_t, ev_v, t_from, t_to, lim):
    """First event time in (t_from, t_to] with value >= lim (inf when none), per row."""
    out = np.full(len(t_from), np.inf)
    lo = np.searchsorted(ev_t, t_from + T_EPS, "left")
    hi = np.searchsorted(ev_t, t_to + T_EPS, "right")
    for r in range(len(t_from)):
        if hi[r] > lo[r]:
            hit = ev_v[lo[r]:hi[r]] >= lim[r] - 1e-9
            if hit.any():
                out[r] = ev_t[lo[r] + int(np.argmax(hit))]
    return out


def compact(rows):
    """float32 / int8 / categorical columns (t stays float64: millisecond times)."""
    r = rows.copy()
    for c in r.columns:
        if c in ("day", "market", "kind"):
            r[c] = r[c].astype("category")
        elif c in ("start",):
            r[c] = r[c].astype(np.int64)
        elif c == "t":
            r[c] = r[c].astype(np.float64)
        elif c in ("side", "jdir", "won") or c.startswith("how_"):
            r[c] = pd.to_numeric(r[c]).fillna(-1).astype(np.int8)
        else:
            r[c] = pd.to_numeric(r[c]).astype(np.float32)
    return r


# --------------------------------------------------------------------------- models
class Ridge:
    """Ridge regression on features standardised on the training rows (clipped at +-5 sd, NaN -> 0)."""

    def __init__(self, alpha=RIDGE_ALPHA):
        self.alpha = alpha

    def _z(self, X):
        Z = (X - self.mu) / self.sd
        return np.clip(np.where(np.isfinite(Z), Z, 0.0), -5, 5)

    def fit(self, X, y):
        X = np.asarray(X, float)
        with np.errstate(invalid="ignore"):
            mu, sd = np.nanmean(X, axis=0), np.nanstd(X, axis=0)
        self.mu = np.where(np.isfinite(mu), mu, 0.0)
        self.sd = np.where(np.isfinite(sd) & (sd > 0), sd, 1.0)
        Z = self._z(X)
        self.zm = Z.mean(axis=0)
        Z = Z - self.zm
        self.ym = float(np.mean(y))
        self.coef = np.linalg.solve(Z.T @ Z + self.alpha * np.eye(Z.shape[1]), Z.T @ (np.asarray(y, float) - self.ym))
        return self

    def predict(self, X):
        return self.ym + (self._z(np.asarray(X, float)) - self.zm) @ self.coef


def make_model(name):
    if name == "ridge":
        return Ridge()
    from sklearn.ensemble import HistGradientBoostingRegressor
    return HistGradientBoostingRegressor(**HGB_PARAMS)


A_FINGERPRINT_V1_ROUNDED = "mix-a-v1-rounded-all-rows"
A_FINGERPRINT_V2_EXACT = "mix-a-v2-exact-model-rows"
MODEL_BUNDLE_FORMAT = "polymarket-mix-models-v2"
MODEL_ANCHOR_FORMAT = "polymarket-mix-model-anchor-v1"
MODEL_PROPOSAL_FORMAT = "polymarket-mix-model-proposal-v2"
MODEL_APPROVAL_FORMAT = "polymarket-mix-model-approval-v2"
MIGRATION_PROVENANCE_FORMAT = "polymarket-mix-migration-provenance-v1"


def model_bundle_paths(frozen_path):
    """Trusted fitted-model sidecars paired with an immutable ``*-frozen.json`` file."""
    frozen_path = Path(frozen_path)
    suffix = "frozen.json"
    prefix = frozen_path.name[:-len(suffix)] if frozen_path.name.endswith(suffix) else frozen_path.stem + "-"
    return frozen_path.with_name(prefix + "models.joblib"), frozen_path.with_name(prefix + "models.json")


def model_anchor_path(frozen_path):
    """Small version-controlled trust anchor for the fitted model and its manifest."""
    frozen_path = Path(frozen_path)
    suffix = "frozen.json"
    prefix = frozen_path.name[:-len(suffix)] if frozen_path.name.endswith(suffix) else frozen_path.stem + "-"
    return frozen_path.with_name(prefix + "model-anchor.json")


def model_proposal_path(frozen_path):
    """Machine-produced exact-A identity for review; it never authorizes fitting by itself."""
    frozen_path = Path(frozen_path)
    suffix = "frozen.json"
    prefix = frozen_path.name[:-len(suffix)] if frozen_path.name.endswith(suffix) else frozen_path.stem + "-"
    return frozen_path.with_name(prefix + "model-proposal.json")


def model_approval_path(frozen_path):
    """Separately committed authorization that pins the exact A identity for a legacy migration."""
    frozen_path = Path(frozen_path)
    suffix = "frozen.json"
    prefix = frozen_path.name[:-len(suffix)] if frozen_path.name.endswith(suffix) else frozen_path.stem + "-"
    return frozen_path.with_name(prefix + "model-approval.json")


def _atomic_json(path, value):
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, indent=1, ensure_ascii=False, default=float), encoding="utf-8")
    os.replace(tmp, path)


def _canonical_json_sha256(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _is_sha256(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _validated_migration_provenance(provenance, spec):
    if not isinstance(provenance, dict) or provenance.get("format") != MIGRATION_PROVENANCE_FORMAT:
        raise ValueError("complete A migration provenance is required")
    expected_top = {"format", "a_start", "a_end", "manifest_sha256", "archive_coverage", "archives",
                    "archives_sha256", "inputs", "runner"}
    if set(provenance) != expected_top:
        raise ValueError("migration provenance schema differs")
    a_bounds = tuple((spec.get("segments") or [[None, None, None]])[0][1:3])
    if (provenance.get("a_start"), provenance.get("a_end")) != a_bounds:
        raise ValueError("migration provenance A bounds differ from the freeze")
    if not _is_sha256(provenance.get("manifest_sha256")):
        raise ValueError("migration manifest provenance is invalid")
    a0, a1 = (ts(value) for value in a_bounds)
    coverage = provenance.get("archive_coverage") or {}
    archives = provenance.get("archives") or []
    if set(coverage) != {"required", "read"}:
        raise ValueError("migration archive coverage schema differs")
    if coverage.get("required") != coverage.get("read") or coverage.get("required") != len(archives):
        raise ValueError("migration provenance lacks the complete A archive set")
    names = []
    for archive in archives:
        if set(archive) != {"name", "size", "sha256"}:
            raise ValueError("migration archive provenance schema differs")
        match = re.fullmatch(r"market_parquet_(\d{4}-\d{2}-\d{2})\.tar\.gz", str(archive.get("name")))
        if (not match or not a_bounds[0] <= match.group(1) < a_bounds[1]
                or not isinstance(archive.get("size"), int) or archive["size"] <= 0
                or not _is_sha256(archive.get("sha256"))):
            raise ValueError("migration archive provenance is invalid")
        names.append(archive["name"])
    if names != sorted(names) or len(names) != len(set(names)):
        raise ValueError("migration archive provenance is not unique and sorted")
    if provenance.get("archives_sha256") != _canonical_json_sha256(archives):
        raise ValueError("migration archive provenance hash mismatch")
    inputs = provenance.get("inputs") or {}
    if set(inputs) != {"factor", "dvol", "klines"}:
        raise ValueError("migration auxiliary-input provenance is incomplete")
    expected_inputs = {
        "factor": {"source": MIGRATION_FACTOR_FILE,
                   "columns": ["ridge_5m", "hgb_5m", "ridge_15m", "hgb_15m"],
                   "bound_start": a0, "bound_end": a1, "step_s": 300,
                   "rows": (a1 - a0) // 300, "first": a0, "last": a1 - 300,
                   "physical": ("file", {"name": MIGRATION_FACTOR_FILE})},
        "dvol": {"source": MIGRATION_DVOL_FILE, "columns": ["dvol"],
                  "bound_start": a0 - MIGRATION_DVOL_PRE_ROLL_S, "bound_end": a1, "step_s": 3600,
                  "rows": (a1 - a0 + MIGRATION_DVOL_PRE_ROLL_S) // 3600,
                  "first": a0 - MIGRATION_DVOL_PRE_ROLL_S, "last": a1 - 3600,
                  "physical": ("file", {"name": MIGRATION_DVOL_FILE})},
        "klines": {"source": "BTCUSDT-1m A-only source plan", "columns": ["close"],
                    "bound_start": a0 - MIGRATION_KLINE_PRE_ROLL_S, "bound_end": a1, "step_s": 60,
                    "rows": (a1 - a0 + MIGRATION_KLINE_PRE_ROLL_S) // 60,
                    "first": a0 - MIGRATION_KLINE_PRE_ROLL_S, "last": a1 - 60,
                    "physical": ("files", None)},
    }
    plan = migration_kline_plan(a_bounds)
    expected_kline_files = ([{"kind": "monthly", "name": f"BTCUSDT-1m-{value}.zip"}
                             for value in plan["months"]]
                            + [{"kind": "daily", "name": f"BTCUSDT-1m-{value}.zip"}
                               for value in plan["days"]])
    expected_inputs["klines"]["physical"] = ("files", expected_kline_files)
    for name, expected in expected_inputs.items():
        value = inputs[name]
        physical_key, physical_expected = expected.pop("physical")
        required_keys = set(expected) | {"sha256", physical_key}
        if set(value) != required_keys or any(value.get(key) != item for key, item in expected.items()):
            raise ValueError(f"migration {name} provenance schema differs")
        if not _is_sha256(value.get("sha256")):
            raise ValueError(f"migration {name} logical hash is invalid")
        physical = value[physical_key]
        if physical_key == "file":
            if (set(physical or {}) != {"name", "sha256"}
                    or physical.get("name") != physical_expected["name"]
                    or not _is_sha256(physical.get("sha256"))):
                raise ValueError(f"migration {name} source file is invalid")
        else:
            stripped = [{k: item.get(k) for k in ("kind", "name")} for item in physical or []]
            if (stripped != physical_expected or any(set(item) != {"kind", "name", "sha256"}
                                                     or not _is_sha256(item.get("sha256"))
                                                     for item in physical or [])):
                raise ValueError("migration kline source plan differs")
    runner = provenance.get("runner") or {}
    required_files = {"binary.py", "cross.py", "jump2s.py", "jump2s_hf.py", "mix_hf.py", "pm_outcomes.py"}
    required_runtime = {"python", "numpy", "pandas", "scipy", "pyarrow", "sklearn", "joblib"}
    if (set(runner) != {"files", "runtime", "sha256"}
            or set(runner.get("files") or {}) != required_files
            or set(runner.get("runtime") or {}) != required_runtime
            or any(not _is_sha256(value) for value in runner["files"].values())
            or runner.get("sha256") != _canonical_json_sha256(
                {"files": runner["files"], "runtime": runner["runtime"]})
            or runner != migration_runner_identity()):
        raise ValueError("migration runner provenance is incomplete")
    return json.loads(json.dumps(provenance, ensure_ascii=False, sort_keys=True))


def _bundle_a_hashes(models, rules, train, X):
    prediction_sha, selected_keys_sha = {}, {}
    for rule in rules:
        rule_id = str(rule["id"])
        prediction = _canonical_float64(models[rule_id].predict(X))
        prediction_sha[rule_id] = hashlib.sha256(prediction.tobytes()).hexdigest()
        selected = np.isfinite(prediction) & (prediction >= float(rule["cut"]))
        selected_keys_sha[rule_id] = hashlib.sha256(_row_key_bytes(train.loc[selected])).hexdigest()
    return prediction_sha, selected_keys_sha


def _a_identity_fields(identity):
    return {
        "a_fingerprint": identity["frozen_a_fingerprint"],
        "a_fingerprint_version": identity["frozen_a_fingerprint_version"],
        "frozen_a_fingerprint": identity["frozen_a_fingerprint"],
        "frozen_a_fingerprint_version": identity["frozen_a_fingerprint_version"],
        "canonical_a_fingerprint": identity["canonical_a_fingerprint"],
        "canonical_a_fingerprint_version": A_FINGERPRINT_V2_EXACT,
        "a_identity_match": identity["match_kind"],
    }


def model_bundle_proposal(A, spec, frozen_path, provenance):
    """Deterministic exact-A record for human/code review before a legacy fitted-model migration."""
    frozen_path = Path(frozen_path)
    segments = set(segment_of(A["start"].to_numpy(float))) if len(A) else set()
    if segments - {"A"}:
        raise ValueError("model proposal accepts only A rows")
    identity = a_identity(spec, A)
    if not identity["matches"]:
        raise ValueError("A fingerprint differs from the frozen study")
    train, training_matrix = _canonical_training_data(A)
    provenance = _validated_migration_provenance(provenance, spec)
    return {
        "format": MODEL_PROPOSAL_FORMAT,
        "frozen_file": frozen_path.name,
        "frozen_sha256": _sha256(frozen_path),
        **_a_identity_fields(identity),
        "training_rows": int(len(train)),
        "training_matrix_sha256": hashlib.sha256(training_matrix.tobytes()).hexdigest(),
        "provenance": provenance,
        "provenance_sha256": _canonical_json_sha256(provenance),
    }


def validate_model_approval(A, spec, frozen_path, provenance):
    """Require a separately committed exact-A approval; proposals never authorize themselves."""
    proposal = model_bundle_proposal(A, spec, frozen_path, provenance)
    path = model_approval_path(frozen_path)
    if not path.exists():
        raise ValueError(f"MIX legacy model approval is missing: {path}")
    approval = json.loads(path.read_text(encoding="utf-8"))
    expected = {**proposal, "format": MODEL_APPROVAL_FORMAT}
    bad = [key for key, value in expected.items() if approval.get(key) != value]
    if bad:
        raise ValueError("MIX model approval does not match the approved exact A: " + ", ".join(bad))
    return approval


def export_model_bundle(A, spec, frozen_path, model_path=None, manifest_path=None, anchor_path=None, *,
                        allow_legacy_migration=False, migration_provenance=None):
    """Fit the frozen rules on A only and write an immutable, hash-verified inference bundle."""
    import joblib
    import sklearn

    frozen_path = Path(frozen_path)
    default_model, default_manifest = model_bundle_paths(frozen_path)
    model_path = Path(model_path or default_model)
    manifest_path = Path(manifest_path or default_manifest)
    default_anchor = model_anchor_path(frozen_path)
    anchor_path = Path(anchor_path or (default_anchor if (model_path == default_model and manifest_path == default_manifest)
                                      else manifest_path.with_name(manifest_path.stem + "-anchor.json")))
    segments = set(segment_of(A["start"].to_numpy(float))) if len(A) else set()
    if segments - {"A"}:
        raise ValueError("model export accepts only A rows")
    identity = a_identity(spec, A)
    if not identity["matches"]:
        raise ValueError("A fingerprint differs from the frozen study")
    identity_fields = _a_identity_fields(identity)
    approval_binding, migration_binding = {}, {}
    if (identity["frozen_a_fingerprint_version"] == A_FINGERPRINT_V1_ROUNDED
            and not allow_legacy_migration):
        raise ValueError("legacy A identity requires an explicit one-time migration")
    if identity["frozen_a_fingerprint_version"] == A_FINGERPRINT_V1_ROUNDED:
        approval = validate_model_approval(A, spec, frozen_path, migration_provenance)
        approval_path = model_approval_path(frozen_path)
        approval_binding = {"approval_file": approval_path.name, "approval_sha256": _sha256(approval_path)}
        migration_binding = {"migration_provenance_sha256": approval["provenance_sha256"]}
    frozen_sha = _sha256(frozen_path)
    train, training_matrix = _canonical_training_data(A)
    X = training_matrix[:, :len(FEATURES)]
    training_matrix_sha = hashlib.sha256(training_matrix.tobytes()).hexdigest()
    frozen_rules = [{key: rule[key] for key in ("id", "policy", "model", "q", "cut")}
                    for rule in spec.get("rules", [])]

    artifacts = (model_path, manifest_path, anchor_path)
    if any(path.exists() for path in artifacts):
        if not all(path.exists() for path in artifacts):
            raise ValueError("partial MIX model bundle")
        anchor = json.loads(anchor_path.read_text(encoding="utf-8"))
        anchor_expected = {
            "format": MODEL_ANCHOR_FORMAT,
            "frozen_file": frozen_path.name,
            "frozen_sha256": frozen_sha,
            **identity_fields,
            "model_file": model_path.name,
            "model_sha256": _sha256(model_path),
            "manifest_file": manifest_path.name,
            "manifest_sha256": _sha256(manifest_path),
            **approval_binding,
            **migration_binding,
            "training_rows": int(len(train)),
            "training_matrix_sha256": training_matrix_sha,
        }
        bad_anchor = [key for key, value in anchor_expected.items() if anchor.get(key) != value]
        if bad_anchor:
            raise ValueError("existing MIX model anchor does not match canonical A: " + ", ".join(bad_anchor))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected = {
            "format": MODEL_BUNDLE_FORMAT,
            "frozen_sha256": frozen_sha,
            **identity_fields,
            "features": list(FEATURES),
            "model_file": model_path.name,
            "rules": frozen_rules,
            "training_rows": int(len(train)),
            "training_matrix_sha256": training_matrix_sha,
            **migration_binding,
        }
        bad = [key for key, value in expected.items() if manifest.get(key) != value]
        if bad or manifest.get("model_sha256") != _sha256(model_path):
            raise ValueError("existing MIX model bundle does not match the frozen study: " + ", ".join(bad or ["sha256"]))
        payload = joblib.load(model_path)
        try:
            prediction_sha, selected_keys_sha = _bundle_a_hashes(payload["models"], frozen_rules, train, X)
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("existing MIX model bundle cannot validate its A predictions") from error
        audit = {"a_prediction_sha256": prediction_sha, "selected_row_keys_sha256": selected_keys_sha}
        payload_expected = {
            "format": MODEL_BUNDLE_FORMAT,
            "frozen_sha256": frozen_sha,
            **identity_fields,
            "features": list(FEATURES),
            "rules": frozen_rules,
            "training_matrix_sha256": training_matrix_sha,
            **migration_binding,
            **audit,
        }
        bad = [key for key, value in audit.items() if manifest.get(key) != value]
        bad += [key for key, value in payload_expected.items() if payload.get(key) != value]
        if bad:
            raise ValueError("existing MIX model bundle does not match canonical A: " + ", ".join(bad))
        return manifest

    label_index = {policy: len(FEATURES) + i for i, policy in enumerate(POLICIES)}
    models = {}
    for rule in spec.get("rules", []):
        policy = str(rule["policy"])
        model_name = str(rule["model"])
        models[str(rule["id"])] = make_model(model_name).fit(X, training_matrix[:, label_index[policy]])
    prediction_sha, selected_keys_sha = _bundle_a_hashes(models, frozen_rules, train, X)
    versions = {"python": platform.python_version(), "numpy": np.__version__, "sklearn": sklearn.__version__,
                "joblib": joblib.__version__}
    payload = {
        "format": MODEL_BUNDLE_FORMAT,
        "frozen_sha256": frozen_sha,
        **identity_fields,
        "features": list(FEATURES),
        "rules": frozen_rules,
        "versions": versions,
        "training_matrix_sha256": training_matrix_sha,
        **migration_binding,
        "a_prediction_sha256": prediction_sha,
        "selected_row_keys_sha256": selected_keys_sha,
        "models": models,
    }
    model_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_model = model_path.with_name(model_path.name + ".tmp")
    joblib.dump(payload, temporary_model, compress=3)
    os.replace(temporary_model, model_path)
    manifest = {
        "format": MODEL_BUNDLE_FORMAT,
        "made": pd.Timestamp.now(tz="UTC").isoformat(),
        "model_file": model_path.name,
        "model_sha256": _sha256(model_path),
        "frozen_file": frozen_path.name,
        "frozen_sha256": frozen_sha,
        **identity_fields,
        "features": list(FEATURES),
        "rules": frozen_rules,
        "training_rows": int(len(train)),
        "training_matrix_sha256": training_matrix_sha,
        **migration_binding,
        "a_prediction_sha256": prediction_sha,
        "selected_row_keys_sha256": selected_keys_sha,
        "versions": versions,
    }
    _atomic_json(manifest_path, manifest)
    anchor = {
        "format": MODEL_ANCHOR_FORMAT,
        "made": pd.Timestamp.now(tz="UTC").isoformat(),
        "frozen_file": frozen_path.name,
        "frozen_sha256": frozen_sha,
        **identity_fields,
        "model_file": model_path.name,
        "model_sha256": _sha256(model_path),
        "manifest_file": manifest_path.name,
        "manifest_sha256": _sha256(manifest_path),
        **approval_binding,
        **migration_binding,
        "training_rows": int(len(train)),
        "training_matrix_sha256": training_matrix_sha,
    }
    _atomic_json(anchor_path, anchor)
    return manifest


def a_blocks():
    """7-day blocks of A from its first day: (start, end, out-of-sample?)."""
    a0, a1 = ts(SEGMENTS[0][1]), ts(SEGMENTS[0][2])
    out, b = [], a0
    while b < a1:
        out.append((b, min(b + BLOCK_DAYS * DAY, a1), b - a0 >= MIN_TRAIN_DAYS * DAY))
        b += BLOCK_DAYS * DAY
    return out


def walk_forward(X, y, start, model):
    """A out-of-sample scores: each out-of-sample block scored by a model fit on the rows of markets
    that ended (start + 300 s) by the block's start; NaN elsewhere."""
    out = np.full(len(y), np.nan)
    end = start + 300
    for b0, b1, oos in a_blocks():
        if not oos:
            continue
        te = (start >= b0) & (start < b1)
        tr = end <= b0
        if not te.any() or tr.sum() < 2 * HGB_PARAMS["min_samples_leaf"]:
            continue
        out[te] = make_model(model).fit(X[tr], y[tr]).predict(X[te])
    return out


def top_cut(v, q):
    """Score at the top q of v (NaN ranks last): rows with v >= cut are the top q (ties included)."""
    v = np.asarray(v, float)
    f = np.sort(v[np.isfinite(v)])[::-1]
    k = max(1, int(math.ceil(q * len(v))))
    return float(f[k - 1]) if k <= len(f) else (float(f[-1]) if len(f) else np.nan)


# --------------------------------------------------------------------------- statistics
def cl_stat(pnl, cluster):
    """dict(mean, se, t, p (one-sided, mean > 0), n, k) with the SE clustered by `cluster`
    (jump2s.cl's formula)."""
    from scipy.stats import norm
    x = np.asarray(pnl, float)
    ok = np.isfinite(x)
    x, c = x[ok], np.asarray(cluster)[ok]
    if not len(x):
        return dict(mean=np.nan, se=np.nan, t=np.nan, p=np.nan, n=0, k=0)
    g = pd.DataFrame({"c": c, "x": x}).groupby("c", observed=True)["x"].agg(["sum", "count"])
    m = g["sum"].sum() / g["count"].sum()
    r = g["sum"] - m * g["count"]
    k = len(g)
    se = float(np.sqrt((r ** 2).sum() * k / max(k - 1, 1)) / g["count"].sum())
    t = m / se if se > 0 else np.nan
    return dict(mean=float(m), se=se, t=float(t), p=float(norm.sf(t)) if np.isfinite(t) else np.nan, n=int(len(x)),
                k=int(k))


def units(tr, pol, n_days):
    """Per-day units of a set of trades of policy `pol`: trades, shares (5 a trade and min(20, sizes)),
    entry cost $, profit $, capital tied up $ (time-weighted mean over the day; median and max of the
    daily peaks of concurrent positions, a day without a trade counting 0), all per day over n_days."""
    out = dict(days=n_days, trades=np.nan, sh5=np.nan, sh20=np.nan, cost5=np.nan, cost20=np.nan, profit5=np.nan,
               profit20=np.nan, cap5=np.nan, cap20=np.nan, peak5=np.nan, peak20=np.nan, pmax5=np.nan, pmax20=np.nan)
    if not n_days:
        return out
    if not len(tr):
        return {**out, **{k: 0.0 for k in out if k != "days"}}
    p = tr["price"].to_numpy(float)
    unit = p + fee(p)
    pnl = tr[f"pnl_{pol}"].to_numpy(float)
    hold = tr[f"hold_{pol}"].to_numpy(float)
    te = tr["t"].to_numpy(float) + LAT_S
    sh = {5: np.minimum(5.0, tr["ask_size"].to_numpy(float)), 20: tr[f"sh20_{pol}"].to_numpy(float)}
    out["trades"] = len(tr) / n_days
    day = np.floor(te / DAY)
    for u in UNITS:
        out[f"sh{u}"] = sh[u].sum() / n_days
        out[f"cost{u}"] = (sh[u] * unit).sum() / n_days
        out[f"profit{u}"] = (sh[u] * pnl).sum() / n_days
        out[f"cap{u}"] = (sh[u] * unit * hold).sum() / (n_days * DAY)
        peaks = []
        for dd in np.unique(day):
            k = day == dd
            ev = np.concatenate([te[k], te[k] + hold[k]])
            amt = np.concatenate([(sh[u] * unit)[k], -(sh[u] * unit)[k]])
            o = np.lexsort((amt, ev))  # at equal times an exit is counted before an entry
            peaks.append(float(np.max(np.cumsum(amt[o]))))
        peaks += [0.0] * max(n_days - len(peaks), 0)  # days of the segment without a trade
        out[f"peak{u}"] = float(np.median(peaks)) if peaks else 0.0
        out[f"pmax{u}"] = float(np.max(peaks)) if peaks else 0.0
    return out


def strict_pnl(rows, pol):
    """Per-share pnl of maker policy `pol` (mk{x}_{h}) when a print must be ABOVE the resting price
    (fs{x}); the crossing-on-arrival sale (how 3) and the fallback taker exit are unchanged. None for
    other policies or without the fs columns. A sensitivity beside MIX.md's rule, not the rule."""
    if not str(pol).startswith("mk") or f"fs{pol[2:].split('_')[0]}" not in rows:
        return None
    x, h = (int(v) for v in pol[2:].split("_"))
    price = rows["price"].to_numpy(float)
    lim = np.minimum(np.round(price + x / 100.0, 6), MAKER_CAP)
    te = rows["t"].to_numpy(float) + LAT_S
    left = rows["start"].to_numpy(float) + 300.0 - te
    fs = rows[f"fs{x}"].to_numpy(float)
    how = rows[f"how_{pol}"].to_numpy(float)
    with np.errstate(invalid="ignore"):
        filled = (how != 3) & np.isfinite(fs) & (fs <= np.minimum(h, left) + T_EPS)
    return np.where(how == 3, rows[f"pnl_{pol}"].to_numpy(float),
                    np.where(filled, lim - price - fee(price), rows[f"pnl_tk{h}"].to_numpy(float)))


def n_days(rows):
    """UTC days (of market start) present in rows."""
    return int(pd.Series(np.floor(rows["start"].to_numpy(float) / DAY)).nunique()) if len(rows) else 0


# --------------------------------------------------------------------------- selection, freeze, evaluation
A_ROW_KEY = ("market", "start", "t", "side", "kind")
CANONICAL_FLOAT_DTYPE = np.dtype("<f8")
CANONICAL_NAN_BITS = np.uint64(0x7FF8000000000000)


def _canonical_float64(values):
    """C-contiguous little-endian float64 with every NaN using one quiet-NaN payload."""
    out = np.array(values, dtype=CANONICAL_FLOAT_DTYPE, order="C", copy=True)
    out.view(np.dtype("<u8"))[np.isnan(out)] = CANONICAL_NAN_BITS
    return out


def a_oos_mask(start):
    """Rows of A's out-of-sample blocks (by market start)."""
    s = np.asarray(start, float)
    return (s >= min(b0 for b0, _, oos in a_blocks() if oos)) & (s < ts(SEGMENTS[0][2]))


def model_rows(rows):
    return rows[rows["kind"].astype(str) != "random"]


def canonical_a_rows(rowsA):
    """Model rows in a deterministic order defined only by the fixed row key."""
    r = model_rows(rowsA).reset_index(drop=True)
    if not len(r):
        return r
    if r[["market", "start", "t", "side", "kind"]].isna().any().any():
        raise ValueError("A row key contains a missing value")
    start = pd.to_numeric(r["start"]).to_numpy(float)
    t = pd.to_numeric(r["t"]).to_numpy(float)
    side = pd.to_numeric(r["side"]).to_numpy(float)
    if not (np.isfinite(start).all() and np.isfinite(t).all() and np.isfinite(side).all()):
        raise ValueError("A row key contains a non-finite number")
    if not (np.equal(start, np.floor(start)).all() and np.equal(side, np.floor(side)).all()):
        raise ValueError("A row key contains a non-integral start or side")
    keys = pd.DataFrame({
        "market": r["market"].astype(str),
        "start": start.astype(np.int64),
        "t": t,
        "side": side.astype(np.int64),
        "kind": r["kind"].astype(str),
    })
    if keys.duplicated(list(A_ROW_KEY)).any():
        raise ValueError("duplicate A row key")
    order = keys.sort_values(list(A_ROW_KEY), kind="stable").index.to_numpy()
    return r.iloc[order].reset_index(drop=True)


def _canonical_training_data(rowsA):
    rows = canonical_a_rows(rowsA)
    columns = FEATURES + [f"pnl_{p}" for p in POLICIES]
    return rows, _canonical_float64(rows[columns].to_numpy())


def _row_key_bytes(rows):
    return b"".join(
        (json.dumps([str(row.market), int(row.start), float(row.t).hex(), int(row.side), str(row.kind)],
                    ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        for row in rows.itertuples(index=False)
    )


def fingerprint(rowsA):
    """sha256 of the A rows that the freeze depends on (keys, features, labels)."""
    r, matrix = _canonical_training_data(rowsA)
    if not len(r):
        return "empty"
    h = hashlib.sha256()
    h.update(_row_key_bytes(r))
    h.update(matrix.tobytes())
    return h.hexdigest()


def legacy_a_fingerprint(rowsA):
    """Exact v1 freeze identity: all A rows, rounded keys to 3 and values to 5 decimals.

    The first committed MIX freeze predates the exact canonical identity above.  Keeping this function
    byte-for-byte equivalent to that historical algorithm lets the old immutable freeze be verified once;
    the exported bundle then records and enforces the exact v2 identity.
    """
    if not len(rowsA):
        return "empty"
    r = rowsA.sort_values(["market", "t", "side", "kind"], kind="stable")
    h = hashlib.sha256()
    h.update("\n".join(r["market"].astype(str) + "|" + r["kind"].astype(str)).encode())
    h.update(np.round(r[["start", "t", "side"]].to_numpy(float), 3).tobytes())
    h.update(np.round(r[FEATURES + [f"pnl_{policy}" for policy in POLICIES]].to_numpy(float), 5).tobytes())
    return h.hexdigest()


def a_identity(spec, rowsA):
    """Verify A under the fingerprint contract stored by the immutable freeze and expose exact v2 identity."""
    version = spec.get("a_fingerprint_version", A_FINGERPRINT_V1_ROUNDED)
    frozen = spec.get("a_fingerprint")
    canonical = fingerprint(rowsA)
    if version == A_FINGERPRINT_V1_ROUNDED:
        observed = legacy_a_fingerprint(rowsA)
        match_kind = "legacy_v1_rounded"
    elif version == A_FINGERPRINT_V2_EXACT:
        observed = canonical
        match_kind = "canonical_v2_exact"
    else:
        raise ValueError(f"unknown A fingerprint version: {version!r}")
    row_count_matches = int(spec.get("a_rows") or 0) == int(len(canonical_a_rows(rowsA)))
    matches = bool(frozen and observed == frozen and row_count_matches)
    return {
        "matches": matches,
        "match_kind": match_kind if matches else "mismatch",
        "frozen_a_fingerprint": frozen,
        "frozen_a_fingerprint_version": version,
        "observed_a_fingerprint": observed,
        "canonical_a_fingerprint": canonical,
        "training_rows": int(len(canonical_a_rows(rowsA))),
    }


def candidates(A, log=print):
    """Every (policy, model, q) on A out of sample: cut, n, mean, se; plus the A oos scores."""
    A, training_matrix = _canonical_training_data(A)
    X = training_matrix[:, :len(FEATURES)]
    label_index = {policy: len(FEATURES) + i for i, policy in enumerate(POLICIES)}
    st = A["start"].to_numpy(float)
    oos = a_oos_mask(st)
    mk = A["market"].astype(str).to_numpy()
    cand, scores = [], {}
    t0 = time.time()
    for pol in POLICIES:
        y = training_matrix[:, label_index[pol]]
        for model in MODELS:
            sc = walk_forward(X, y, st, model)
            scores[(pol, model)] = sc
            v = np.where(oos, sc, np.nan)
            for q in QS:
                cut = top_cut(v[oos], q)
                sel = oos & np.isfinite(v) & (v >= cut)
                cand.append(dict(policy=pol, model=model, q=q, cut=cut, **cl_stat(y[sel], mk[sel])))
        log(f"  walk-forward {pol}: {time.time() - t0:.0f} s")
    return pd.DataFrame(cand), scores


def single_control(A, pol, q):
    """Best single feature (either direction) at top q on the A out-of-sample rows: dict or None."""
    oos = a_oos_mask(A["start"].to_numpy(float))
    S = A[oos]
    y = S[f"pnl_{pol}"].to_numpy(float)
    mk = S["market"].astype(str).to_numpy()
    best = None
    for f in FEATURES:
        x = S[f].to_numpy(float)
        for sg in (1, -1):
            v = sg * x
            cut = top_cut(v, q)
            if not np.isfinite(cut):
                continue
            sel = np.isfinite(v) & (v >= cut)
            if sel.sum() > TIE_MAX * q * len(v) or sel.sum() < MIN_A_TRADES:
                continue
            st = cl_stat(y[sel], mk[sel])
            if best is None or st["mean"] > best["A"]["mean"]:
                best = dict(feature=f, sign=sg, cut=cut, A=st)
    return best


def freeze(A, path, log=print, full=True):
    """Pick and freeze the rules on A alone (A rows only are passed in). Writes `path` unless it
    exists; returns the spec read back from disk and whether it was written now. `full`: the run read
    every A archive of MANIFEST.txt (only such a freeze can give a verdict; spec_problems)."""
    path = Path(path)
    fp = fingerprint(A)
    if path.exists():
        spec = json.loads(path.read_text())
        log(f"{path} exists (made {spec.get('made')}); not re-frozen")
        return spec, False, None
    A = canonical_a_rows(A)
    cand, scores = candidates(A, log)
    ok = cand[cand["n"] >= MIN_A_TRADES].sort_values(["mean", "policy", "model", "q"], ascending=[False, True, True, False],
                                                     kind="stable")
    rules = []
    for k, r in enumerate(ok.head(N_PICK).itertuples(index=False)):
        ctl = single_control(A, r.policy, r.q)
        rules.append(dict(id=f"R{k + 1}", policy=r.policy, model=r.model, q=r.q, cut=r.cut,
                          A=dict(mean=r.mean, se=r.se, t=r.t, p=r.p, n=r.n, k=r.k), control=ctl))
    spec = dict(made=pd.Timestamp.now(tz="UTC").isoformat(), design="MIX.md", full=bool(full), segments=SEGMENTS,
                a_oos_from=pd.Timestamp(min(b0 for b0, _, o in a_blocks() if o), unit="s").strftime("%Y-%m-%d"),
                features=FEATURES, policies=POLICIES, models={"ridge": {"alpha": RIDGE_ALPHA}, "hgb": HGB_PARAMS},
                qs=QS, min_a_trades=MIN_A_TRADES, b_alpha=B_ALPHA, c_alpha=C_ALPHA, a_rows=int(len(A)),
                a_fingerprint=fp, a_fingerprint_version=A_FINGERPRINT_V2_EXACT,
                n_candidates=int(len(cand)), n_eligible=int(len(ok)), rules=rules,
                candidates=json.loads(cand.to_json(orient="records")))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(spec, indent=1, ensure_ascii=False, default=float), encoding="utf-8")
    log(f"frozen {len(rules)} rules -> {path}")
    return json.loads(path.read_text()), True, scores


def _norm(x):
    return json.loads(json.dumps(x, default=float))


def spec_setting_problems(spec):
    """Frozen-study problems independent of the current A rows."""
    out = []
    if not spec.get("full", False):
        out.append("冻结文件来自试跑（那次没读全 A 段的日档）")
    if not spec.get("a_rows") or spec.get("a_fingerprint") == "empty":
        out.append("冻结时 A 段没有行")
    want = dict(segments=SEGMENTS, b_alpha=B_ALPHA, c_alpha=C_ALPHA, policies=POLICIES, features=FEATURES, qs=QS,
                min_a_trades=MIN_A_TRADES, models={"ridge": {"alpha": RIDGE_ALPHA}, "hgb": HGB_PARAMS})
    diff = [k for k, v in want.items() if _norm(spec.get(k)) != _norm(v)]
    if diff:
        out.append("冻结文件的设定与现在的代码不同（" + "、".join(diff) + "）")
    return out


def spec_problems(spec, A):
    """Why a frozen spec cannot give a verdict on current A; legacy migration never unlocks B/C."""
    out = spec_setting_problems(spec)
    exact = spec.get("a_fingerprint_version") == A_FINGERPRINT_V2_EXACT
    if not exact or spec.get("a_fingerprint") != fingerprint(A):
        out.append("A 段数据与冻结时不同（指纹不符）")
    return out


def evaluate(rows, spec, open_c=True, log=print):
    """B (and C for the rules that pass B, when open_c) of the frozen rules and their controls, with A out
    of sample beside them. Returns (per rule dict, traded rows, meta)."""
    M = model_rows(rows)
    mseg = segment_of(M["start"].to_numpy(float))
    A, training_matrix = _canonical_training_data(M[mseg == "A"])
    Xa = training_matrix[:, :len(FEATURES)]
    label_index = {policy: len(FEATURES) + i for i, policy in enumerate(POLICIES)}
    oos = a_oos_mask(A["start"].to_numpy(float))
    days = {"A": n_days(A[oos]), "B": n_days(M[mseg == "B"]), "C": n_days(M[mseg == "C"])}
    res, traded = [], []
    for r in spec.get("rules", []):
        pol, model, cut = r["policy"], r["model"], r["cut"]
        y = training_matrix[:, label_index[pol]]
        sc_a = walk_forward(Xa, y, A["start"].to_numpy(float), model)
        final = make_model(model).fit(Xa, y)
        out = dict(rule=r, stats={}, units={}, ctl_stats={}, ctl_units={}, strict={}, eff={}, extra={}, by_w={},
                   ctl_by_w={}, b_pass=False, c_pass=None, c_open=False)
        segs = {"A": (A[oos], sc_a[oos])}
        B = M[mseg == "B"]
        segs["B"] = (B, final.predict(B[FEATURES].to_numpy(float)) if len(B) else np.array([]))
        for name in ("A", "B", "C"):
            if name == "C":
                b = out["stats"].get("B", {})
                out["b_pass"] = bool(b.get("n", 0) and b["mean"] > 0 and b["p"] < B_ALPHA)
                if not (out["b_pass"] and open_c):
                    break
                C = M[mseg == "C"]
                segs["C"] = (C, final.predict(C[FEATURES].to_numpy(float)) if len(C) else np.array([]))
                out["c_open"] = True
            S, sc = segs[name]
            sel = np.isfinite(sc) & (sc >= cut) if len(S) else np.zeros(0, bool)
            tr = S[sel]
            out["stats"][name] = cl_stat(tr[f"pnl_{pol}"].to_numpy(float), tr["market"].astype(str).to_numpy())
            out["units"][name] = units(tr, pol, days[name])
            mk_ = tr["market"].astype(str).to_numpy()
            sp = strict_pnl(tr, pol)
            if sp is not None:  # sensitivities only: the verdict uses MIX.md's fill rule and the own book
                out["strict"][name] = cl_stat(sp, mk_)
            ep = eff_pnl(tr, pol)
            if ep is not None:
                out["eff"][name] = cl_stat(ep, mk_)
            out["extra"][name] = trade_extras(tr, pol)
            if name != "A":
                out["by_w"][name] = by_window(tr, pol)
            traded.append(tr.assign(rule=r["id"], segment=name, score=sc[sel], policy=pol))
            ctl = r.get("control")
            if ctl:
                v = ctl["sign"] * S[ctl["feature"]].to_numpy(float)
                cs = np.isfinite(v) & (v >= ctl["cut"])
                ct = S[cs]
                out["ctl_stats"][name] = cl_stat(ct[f"pnl_{pol}"].to_numpy(float), ct["market"].astype(str).to_numpy())
                out["ctl_units"][name] = units(ct, pol, days[name])
                if name != "A":
                    out["ctl_by_w"][name] = by_window(ct, pol)
                traded.append(ct.assign(rule=r["id"] + "-ctl", segment=name, score=v[cs], policy=pol))
        if out["c_open"]:
            c = out["stats"]["C"]
            out["c_pass"] = bool(c["n"] and c["mean"] > 0 and c["p"] < C_ALPHA)
        res.append(out)
        log(f"{r['id']} {pol} {model} q={r['q']}: " + ", ".join(f"{k} {v['mean']:+.4f} ({v['n']})"
                                                                  for k, v in out["stats"].items()))
    tr = pd.concat(traded, ignore_index=True) if traded else pd.DataFrame()
    return res, tr, dict(days=days)


def eff_pnl(rows, pol):
    """Per-share pnl of `pol` under the effective-bid sensitivity (settle: unchanged); None without it."""
    if pol == "settle":
        return rows["pnl_settle"].to_numpy(float) if "pnl_settle" in rows else None
    c = f"pnl_eff_{pol}"
    return rows[c].to_numpy(float) if c in rows else None


def _hold_s(pol):
    return int(pol[2:]) if pol.startswith("tk") else (int(pol.split("_")[1]) if pol.startswith("mk") else None)


def trade_extras(tr, pol):
    """Trades per market, both-side pairs (one decision point, both sides selected), and for taker / maker
    policies: the share held to settlement although the planned exit was before the end (no usable bid
    came) and the mean wait of the taker sales beyond te + h + 0.5 s."""
    out = dict(n=len(tr), markets=int(tr["market"].nunique()) if len(tr) else 0, pairs=0, held_early=np.nan,
               held_won=np.nan, wait=np.nan)
    if not len(tr):
        return out
    g = tr.groupby([tr["market"].astype(str), tr["kind"].astype(str), tr["t"].round(3)], observed=True)["side"].nunique()
    out["pairs"] = int((g > 1).sum())
    h = _hold_s(pol)
    if h is not None:
        te = tr["t"].to_numpy(float) + LAT_S
        early = te + h + LAT_S < tr["start"].to_numpy(float) + 300.0 - T_EPS
        how = tr[f"how_{pol}"].to_numpy(float)
        out["held_early"] = float(np.mean(how[early] == 0)) if early.any() else np.nan
        held = early & (how == 0)
        out["held_won"] = float(np.mean(tr["won"].to_numpy(float)[held] == 1)) if held.any() else np.nan
        sold = how == 1
        if sold.any():
            out["wait"] = float(np.mean(tr[f"hold_{pol}"].to_numpy(float)[sold] - (h + LAT_S)))
    return out


def by_window(tr, pol):
    """Per-share stats of `pol` by settlement rule (0 = point price, 30 / 60 = TWAP seconds)."""
    if not len(tr):
        return {}
    w = j2.settle_twap(tr["start"].to_numpy(float))
    y = tr[f"pnl_{pol}"].to_numpy(float)
    mk = tr["market"].astype(str).to_numpy()
    return {int(v): cl_stat(y[w == v], mk[w == v]) for v in np.unique(w)}


# --------------------------------------------------------------------------- the run
def out_paths(out):
    """(report, frozen json, rows csv.gz) from --out: real/mix-hf.md -> real/mix-frozen.json, real/mix-rows.csv.gz."""
    out = Path(out)
    stem = out.stem
    prefix = stem[:-2] if stem.endswith("-hf") else stem + "-"
    return out, out.with_name(f"{prefix}frozen.json"), out.with_name(f"{prefix}rows.csv.gz")


def migration_preflight(out):
    """Validate the immutable study and return its frozen A date bounds before any history is read."""
    frozen_path = out_paths(out)[1]
    if not frozen_path.exists():
        raise ValueError("bundle migration requires the existing immutable freeze")
    spec = json.loads(frozen_path.read_text(encoding="utf-8"))
    problems = spec_setting_problems(spec)
    if problems:
        raise ValueError("frozen study cannot migrate: " + "; ".join(problems))
    segments = spec.get("segments") or []
    if len(segments) != 3 or len(segments[0]) != 3 or segments[0][0] != "A":
        raise ValueError("frozen study has invalid A segment bounds")
    return str(segments[0][1]), str(segments[0][2])


def select_archives(every, days=None, names=None, bundle_only=False, bundle_proposal=False,
                    migration_a_bounds=None):
    """Choose source archives; one-time bundle migration is structurally unable to read B or C."""
    if bundle_only and bundle_proposal:
        raise ValueError("bundle-only and bundle-proposal are mutually exclusive")
    migration = bundle_only or bundle_proposal
    if migration and (days or names):
        raise ValueError("bundle-only cannot be combined with days or names")
    arcs = list(every)
    if migration:
        a0, a1 = migration_a_bounds or (SEGMENTS[0][1], SEGMENTS[0][2])
        return [item for item in arcs if a0 <= item[0][15:25] < a1]
    if names:
        return [item for item in arcs if item[0] in set(names)]
    if days:
        return arcs[-int(days):]
    return arcs


def export_bundle_only(rows, out, full_a=False, provenance=None, log=print):
    """One-time legacy migration using complete A only; never score B or C and never rewrite the freeze."""
    if not full_a:
        raise ValueError("bundle migration requires complete A coverage")
    if len(rows):
        rows = rows.drop_duplicates(["market", "kind", "t", "side"]).reset_index(drop=True)
    segments = set(segment_of(rows["start"].to_numpy(float))) if len(rows) else set()
    if not rows.size or segments != {"A"}:
        raise ValueError("bundle migration accepts only A rows")
    frozen_path = out_paths(out)[1]
    if not frozen_path.exists():
        raise ValueError("bundle migration requires the existing immutable freeze")
    spec = json.loads(frozen_path.read_text(encoding="utf-8"))
    problems = spec_setting_problems(spec)
    if problems:
        raise ValueError("frozen study cannot migrate: " + "; ".join(problems))
    identity = a_identity(spec, rows)
    if not identity["matches"]:
        raise ValueError("A fingerprint differs from the frozen study")
    manifest = export_model_bundle(rows, spec, frozen_path, allow_legacy_migration=True,
                                   migration_provenance=provenance)
    log(f"A-only model bundle anchored at {model_anchor_path(frozen_path)}")
    return manifest


def write_bundle_proposal(rows, out, full_a=False, provenance=None, log=print):
    """Write the deterministic exact-A proposal only; this file cannot authorize model fitting."""
    if not full_a:
        raise ValueError("bundle proposal requires complete A coverage")
    if len(rows):
        rows = rows.drop_duplicates(["market", "kind", "t", "side"]).reset_index(drop=True)
    segments = set(segment_of(rows["start"].to_numpy(float))) if len(rows) else set()
    if not rows.size or segments != {"A"}:
        raise ValueError("bundle proposal accepts only A rows")
    frozen_path = out_paths(out)[1]
    if not frozen_path.exists():
        raise ValueError("bundle proposal requires the existing immutable freeze")
    spec = json.loads(frozen_path.read_text(encoding="utf-8"))
    problems = spec_setting_problems(spec)
    if problems:
        raise ValueError("frozen study cannot propose a migration: " + "; ".join(problems))
    proposal = model_bundle_proposal(rows, spec, frozen_path, provenance)
    path = model_proposal_path(frozen_path)
    if path.exists() and json.loads(path.read_text(encoding="utf-8")) != proposal:
        raise ValueError("existing MIX model proposal differs from canonical A")
    _atomic_json(path, proposal)
    log(f"A-only exact identity proposed at {path}; no model was fitted")
    return proposal


def run(workdir, out, days=None, dataset=None, names=None, inputs=INPUTS, budget_s=BUDGET_S, log=None,
        bundle_only=False, bundle_proposal=False):
    """Every daily archive (the last `days`; or the archives in `names`), per-day shards in the workdir,
    then the freeze on A, B / C, the report and the traded rows. ``bundle_only`` reads every A archive and
    exits after anchoring the fitted A-only model; ``bundle_proposal`` only writes the exact-A identity;
    neither migration mode selects B or C archives."""
    import traceback

    import cross
    log = log or (lambda s: print(s, flush=True))
    if bundle_only and bundle_proposal:
        raise ValueError("bundle-only and bundle-proposal are mutually exclusive")
    committed_frozen = out_paths(out)[1]
    historical_freezes = {committed_frozen.resolve(), Path(CANONICAL_STUDY_FROZEN).resolve()}
    if not (bundle_only or bundle_proposal) and any(path.exists() for path in historical_freezes):
        raise ValueError("MIX historical study is frozen; use forward replay or the A-only migration workflow")
    migration_a_bounds = migration_preflight(out) if (bundle_only or bundle_proposal) else None
    if dataset and dataset != cross.DS:
        cross.set_dataset(dataset)
    workdir = Path(workdir)
    shards = workdir / "mix-shards"
    shards.mkdir(parents=True, exist_ok=True)
    t_start = time.time()
    migration = bundle_only or bundle_proposal
    if migration:
        a0_s, a1_s = (ts(value) for value in migration_a_bounds)
        kline_plan = migration_kline_plan(migration_a_bounds)
        kl, missing = hf.klines(workdir, months=kline_plan["months"], days=kline_plan["days"],
                                start_s=a0_s - MIGRATION_KLINE_PRE_ROLL_S, end_s=a1_s)
        if missing:
            raise ValueError("A-only migration requires complete bounded klines")
        want_klines = np.arange(a0_s - MIGRATION_KLINE_PRE_ROLL_S, a1_s, 60, dtype=np.int64)
        if not np.array_equal(np.asarray(kl.index, dtype=np.int64), want_klines):
            raise ValueError("A-only kline input is incomplete")
        fp, dv = load_migration_inputs(inputs, migration_a_bounds)
        migration_sources = migration_input_sources(inputs)
        migration_sources["klines"] = migration_kline_sources(workdir, kline_plan)
    else:
        kl, missing = hf.klines(workdir)
        fp, dv = load_inputs(inputs)
        migration_sources = None
    log(f"klines: {len(kl):,} minutes, missing months {missing or 'none'}")
    log(f"inputs: {len(fp):,} factor windows, {len(dv):,} DVOL hours")
    manifest_raw = cross.fetch("MANIFEST.txt")
    manifest = manifest_raw.decode()
    sha = manifest_sha(manifest)
    every = [a for a in cross.archives(manifest) if ARCHIVE_DAYS[0] <= a[0][15:25] < ARCHIVE_DAYS[1]]
    arcs = select_archives(every, days=days, names=names, bundle_only=bundle_only,
                           bundle_proposal=bundle_proposal, migration_a_bounds=migration_a_bounds)
    infos, failed, skipped, carry, own, read_archives = [], [], [], None, [], []
    files = []
    for name, size in arcs:
        day = name[15:25]
        if time.time() - t_start > budget_s:
            skipped.append(day)
            continue
        try:
            local = _fetch(cross, name, workdir / name, log, size=size, sha256=sha.get(name, ""))
            feat, mk, rs = cross.read_day(local)
            hf._release()
            rs, provisional = hf.final_resolution(rs)
            mt = cross.market_table(hf.shrink_markets(mk), rs)
            del mk, rs
            hf._release()
            binance = cross.read_binance(local)
            prints = cross.read_poly_trades(local)
            Path(local).unlink()
            if len(binance):
                tt = pd.to_numeric(binance["trade_ts_ms"], errors="coerce")
                own.append(j2.minute_closes(tt.to_numpy(float) / 1000.0, binance["price"].to_numpy(float)))
            own_c = pd.concat(own).groupby(level=0).last() if own else pd.Series(dtype=float)
            closes = hf.closes_for(kl, own_c, missing)
            rows, info = day_rows(feat, mt, binance, prints, carry=carry, factor=fp, dvol=dv, closes=closes, day=day)
            info["provisional"] = provisional
            del feat, prints, mt
            if len(binance):
                carry = binance[tt >= tt.max() - CARRY_S * 1000][["trade_ts_ms", "recv_ts_ms", "price"]].copy()
            else:
                carry = None
            del binance
            hf._release()
            f = shards / f"{day}.parquet"
            rows.to_parquet(f, index=False)
            files.append(f)
            infos.append(info)
            read_archives.append(name)
            log(f"{name}: {info['markets']} markets, {info['jump']:,} jump / {info['fixed']:,} fixed / "
                f"{info['random']:,} random points, {info['rows']:,} rows ({info['no_entry']:,} without entry) "
                f"({time.time() - t_start:.0f} s)")
        except Exception:
            log(f"{name}: failed\n{traceback.format_exc()}")
            failed.append(day)
            carry = None
    rows = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True) if files else _empty_rows()
    cov = coverage([a[0][15:25] for a in every], [i["day"] for i in infos])
    log("coverage: " + ", ".join(f"{k} {v[0]}/{v[1]}" for k, v in cov.items()))
    full_a = cov["A"][0] == cov["A"][1] > 0
    provenance = (build_migration_provenance(manifest_raw, arcs, read_archives, fp, dv, kl,
                                              migration_a_bounds, migration_sources)
                  if migration and full_a else None)
    if bundle_proposal:
        return write_bundle_proposal(rows, out, full_a=full_a, provenance=provenance, log=log)
    if bundle_only:
        return export_bundle_only(rows, out, full_a=full_a, provenance=provenance, log=log)
    complete = all(v[0] == v[1] for v in cov.values())
    return analyze(rows, out, infos, failed, skipped, len(arcs), missing, ds=cross.DS, log=log, full_a=full_a,
                   complete=complete, trial_frozen=workdir / TRIAL_FROZEN, coverage=cov)


def manifest_sha(manifest):
    """{archive name: sha256} from MANIFEST.txt (lines without a 64-hex digest are left out)."""
    out = {}
    for line in manifest.splitlines():
        m = re.search(r"(market_parquet_\d{4}-\d{2}-\d{2}\.tar\.gz)", line)
        h = re.findall(r"\b([0-9a-fA-F]{64})\b", line)
        if m and h:
            out[m.group(1)] = h[-1].lower()
    return out


def archive_segment(day):
    """'A' / 'B' / 'C' of an archive by its date (YYYY-MM-DD), '' outside."""
    for name, a, b in SEGMENTS:
        if a <= day < b:
            return name
    return ""


def coverage(manifest_days, read_days):
    """{segment: (archives read, archives in MANIFEST.txt)} by archive date."""
    read = set(read_days)
    out = {}
    for s, _, _ in SEGMENTS:
        need = [d for d in manifest_days if archive_segment(d) == s]
        out[s] = (sum(d in read for d in need), len(need))
    return out


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 22):
            h.update(chunk)
    return h.hexdigest()


def _bounded_input_identity(keys, values, columns, source, bound_start, bound_end, **extra):
    keys = np.asarray(keys, dtype=np.dtype("<i8"))
    values = _canonical_float64(values)
    if not len(keys) or values.shape[0] != len(keys):
        raise ValueError(f"empty or malformed migration input: {source}")
    schema = {"source": source, "columns": list(columns), "bound_start": int(bound_start),
              "bound_end": int(bound_end), **extra}
    h = hashlib.sha256()
    h.update(json.dumps(schema, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    h.update(keys.tobytes())
    h.update(values.tobytes())
    return {**schema, "rows": int(len(keys)), "first": int(keys[0]), "last": int(keys[-1]),
            "sha256": h.hexdigest()}


def migration_runner_identity():
    import joblib
    import pyarrow
    import scipy
    import sklearn

    names = ("binary.py", "cross.py", "jump2s.py", "jump2s_hf.py", "mix_hf.py", "pm_outcomes.py")
    files = {name: _sha256(HERE / name) for name in names}
    runtime = {"python": platform.python_version(), "numpy": np.__version__, "pandas": pd.__version__,
               "scipy": scipy.__version__, "pyarrow": pyarrow.__version__, "sklearn": sklearn.__version__,
               "joblib": joblib.__version__}
    return {"files": files, "runtime": runtime,
            "sha256": _canonical_json_sha256({"files": files, "runtime": runtime})}


def build_migration_provenance(manifest_raw, selected_archives, read_archives, factor, dvol, klines,
                               a_bounds, sources):
    """Bind a proposal to complete A archives, bounded auxiliary values and the exact runner."""
    raw = manifest_raw.encode("utf-8") if isinstance(manifest_raw, str) else bytes(manifest_raw)
    manifest = raw.decode("utf-8")
    digests = manifest_sha(manifest)
    archives = []
    for name, size in selected_archives:
        digest = digests.get(name, "")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError(f"A archive lacks a manifest sha256: {name}")
        archives.append({"name": str(name), "size": int(size), "sha256": digest})
    required_names = [item["name"] for item in archives]
    if list(read_archives) != required_names or len(set(required_names)) != len(required_names):
        raise ValueError("migration provenance lacks the complete A archive set")
    if set(sources or {}) != {"factor", "dvol", "klines"}:
        raise ValueError("migration source-file provenance is incomplete")
    a0, a1 = (ts(value) for value in a_bounds)
    factor_columns = [str(c) for c in factor.columns]
    factor_id = _bounded_input_identity(
        factor.index.to_numpy(np.int64), factor.to_numpy(float), factor_columns,
        MIGRATION_FACTOR_FILE, a0, a1, file=sources["factor"], step_s=300,
    )
    dvol_id = _bounded_input_identity(
        dvol["hour"].to_numpy(np.int64), dvol[["dvol"]].to_numpy(float), ["dvol"],
        MIGRATION_DVOL_FILE, a0 - MIGRATION_DVOL_PRE_ROLL_S, a1,
        file=sources["dvol"], step_s=3600,
    )
    kline_id = _bounded_input_identity(
        np.asarray(klines.index, dtype=np.int64), np.asarray(klines, dtype=float).reshape(-1, 1), ["close"],
        "BTCUSDT-1m A-only source plan", a0 - MIGRATION_KLINE_PRE_ROLL_S, a1,
        files=list(sources["klines"]), step_s=60,
    )
    return {
        "format": MIGRATION_PROVENANCE_FORMAT,
        "a_start": str(a_bounds[0]),
        "a_end": str(a_bounds[1]),
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "archive_coverage": {"required": len(archives), "read": len(read_archives)},
        "archives": archives,
        "archives_sha256": _canonical_json_sha256(archives),
        "inputs": {"factor": factor_id, "dvol": dvol_id, "klines": kline_id},
        "runner": migration_runner_identity(),
    }


def _fetch(cross, name, dest, log=print, tries=FETCH_TRIES, size=0, sha256=""):
    """cross.fetch, then the file checked against MANIFEST.txt's byte count and sha256 (urllib returns a
    short body silently when the server closes early); any error or mismatch is retried, `tries` in all
    (an archive that still fails is "failed": a C one keeps the lockbox closed)."""
    for k in range(tries):
        try:
            cross.fetch(name, dest)
            got = Path(dest).stat().st_size
            if size and got != size:
                raise OSError(f"{got:,} bytes, MANIFEST.txt says {size:,}")
            if sha256 and _sha256(dest) != sha256:
                raise OSError("sha256 differs from MANIFEST.txt")
            return dest
        except Exception as e:
            Path(dest).unlink(missing_ok=True)
            if k + 1 == tries:
                raise
            log(f"{name}: download failed ({e!r}), retry {k + 1}")
            time.sleep(FETCH_WAIT_S * (k + 1))


def lockbox_path(frozen_path):
    """<prefix>lockbox.json beside <prefix>frozen.json: the record of C's first opening."""
    p = Path(frozen_path)
    return p.with_name(p.name[:-len("frozen.json")] + "lockbox.json" if p.name.endswith("frozen.json")
                       else p.stem + "-lockbox.json")


def analyze(rows, out, infos=(), failed=(), skipped=(), n_arcs=0, missing=(), ds=None, log=print, full_a=True,
            complete=True, trial_frozen=None, coverage=None):
    """Segments, freeze on A, B / C, report and traded rows (separate from run() for the tests).

    full_a: every A archive of MANIFEST.txt was read (else a trial: frozen into trial_frozen, rewritten,
    C closed); complete: every A, B and C archive was read (C may open)."""
    out, frozen_path, rows_path = out_paths(out)
    lock_path = lockbox_path(frozen_path)
    trial = not full_a
    historical_freezes = {frozen_path.resolve(), Path(CANONICAL_STUDY_FROZEN).resolve()}
    if any(path.exists() for path in historical_freezes):
        raise ValueError("MIX historical study is frozen; B/C cannot be read or recomputed")
    out.parent.mkdir(parents=True, exist_ok=True)
    if len(rows):
        rows = rows.drop_duplicates(["market", "kind", "t", "side"]).reset_index(drop=True)
        seg = segment_of(rows["start"].to_numpy(float))
        rows = rows[seg != ""].reset_index(drop=True)
    seg = segment_of(rows["start"].to_numpy(float)) if len(rows) else np.array([], dtype=object)
    # C is opened only when every archive was read (none skipped for time, none failed)
    complete_c = bool(complete) and not trial and \
        not any(SEGMENTS[2][1] <= d < SEGMENTS[2][2] for d in list(skipped) + list(failed))
    if trial:  # never the committed file: a trial freezes afresh in the workdir and never opens C
        frozen_path = Path(trial_frozen) if trial_frozen else out.with_name(TRIAL_FROZEN)
        frozen_path.unlink(missing_ok=True)
        log(f"trial run (not every A archive read): frozen into {frozen_path}, C stays closed")
    A = rows[seg == "A"]
    spec, fresh, _ = freeze(A, frozen_path, log, full=not trial)  # A only: nothing of B or C reaches the freeze
    problems = [] if trial else spec_problems(spec, A)
    model_manifest = export_model_bundle(A, spec, frozen_path) if not trial and not problems else None
    a_now = int(len(model_rows(A)))
    del A
    digest = hashlib.sha256(Path(frozen_path).read_bytes()).hexdigest()
    log(f"frozen file sha256 {digest} (before B / C are scored)" + (f"; not a verdict: {problems}" if problems else ""))
    c_fp = fingerprint(rows[seg == "C"])
    lock = None
    if not trial and lock_path.exists():
        lock = json.loads(lock_path.read_text())
    lock_ok = lock is None or (lock.get("frozen_sha256") == digest and lock.get("c_fingerprint") == c_fp)
    open_c = complete_c and not problems and lock_ok
    res, traded, meta = evaluate(rows, spec, open_c, log=log)
    meta.update(frozen_sha256=digest, problems=problems, trial=trial, complete_c=complete_c, lock=lock,
                lock_ok=lock_ok, a_rows_now=a_now, coverage=coverage, lock_path=lock_path, lock_written=False,
                model_manifest=model_manifest)
    if lock is None and not trial and any(o["c_open"] for o in res):
        rec = dict(opened=pd.Timestamp.now(tz="UTC").isoformat(), frozen_sha256=digest, c_fingerprint=c_fp,
                   c_days=meta["days"]["C"], results=[dict(rule=o["rule"]["id"], policy=o["rule"]["policy"],
                                                           model=o["rule"]["model"], q=o["rule"]["q"],
                                                           C=o["stats"].get("C"), c_pass=o["c_pass"])
                                                      for o in res if o["c_open"]])
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(json.dumps(rec, indent=1, ensure_ascii=False, default=float), encoding="utf-8")
        meta["lock"], meta["lock_written"] = rec, True
        log(f"lockbox opened for the first time; recorded in {lock_path}")
    meta["rows_note"] = write_traded(traded, rows_path)
    L = report(rows, spec, fresh, res, meta, infos, failed, skipped, n_arcs, missing, ds, frozen_path, rows_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    log("\n".join(L))
    return rows, spec, res


TRADED_COLS = ["rule", "segment", "policy", "market", "start", "t", "kind", "side", "jdir", "price", "ask_size", "won",
               "score", "pnl", "hold", "how", "sh20", "pnl_strict", "pnl_eff"]
ROWS_MAX_BYTES = 19_500_000


def write_traded(traded, path):
    """The rows the frozen rules and their controls traded (their own policy's pnl, holding time, exit kind
    and 20-share size) to path; under ROWS_MAX_BYTES, dropping the A rows of the controls and then all A
    rows when needed. Returns a note for the report."""
    path = Path(path)
    if not len(traded):
        pd.DataFrame(columns=TRADED_COLS).to_csv(path, index=False)
        return "没有交易行"
    tr = traded.reset_index(drop=True)
    pol = tr["policy"].astype(str).to_numpy()
    for k in ("pnl", "hold", "how", "sh20"):
        tr[k] = np.nan
        for p_ in np.unique(pol):
            m = pol == p_
            tr.loc[m, k] = tr.loc[m, f"{k}_{p_}"].to_numpy(float)
    tr["pnl_strict"], tr["pnl_eff"] = np.nan, np.nan
    for p_ in np.unique(pol):
        m = pol == p_
        sp = strict_pnl(tr[m], p_)
        if sp is not None:
            tr.loc[m, "pnl_strict"] = sp
        ep = eff_pnl(tr[m], p_)
        if ep is not None:
            tr.loc[m, "pnl_eff"] = ep
    tr = tr[TRADED_COLS].copy()
    tr["t"] = tr["t"].round(3)
    for c in ("price", "ask_size", "score", "pnl", "hold", "sh20", "pnl_strict", "pnl_eff"):
        tr[c] = tr[c].astype(float).round(5)
    seg, ctl = tr["segment"].astype(str), tr["rule"].astype(str).str.endswith("-ctl")
    for note, keep in (("全部", np.ones(len(tr), bool)), ("去掉对照的 A 行", ~((seg == "A") & ctl)), ("去掉 A 行", seg != "A")):
        tr[keep].to_csv(path, index=False)
        if path.stat().st_size <= ROWS_MAX_BYTES:
            return f"{note}（{int(keep.sum()):,} 行）"
    return f"去掉 A 行后仍有 {path.stat().st_size / 1e6:.1f} MB"


# --------------------------------------------------------------------------- report
def _c(x, nd=2):
    return "–" if x is None or not np.isfinite(x) else f"{100 * x:+.{nd}f}¢"


def _st(s):
    if not s or not s.get("n"):
        return "–（0 笔）"
    return f"{_c(s['mean'])} ±{100 * s['se']:.2f}（t {s['t']:.1f}，{s['n']:,} 笔，{s['k']:,} 个市场）"


def _p(s):
    return "–" if not s or not np.isfinite(s.get("p", np.nan)) else f"{s['p']:.4f}"


def _usd(x):
    return "–" if x is None or not np.isfinite(x) else f"${x:,.2f}"


def _units_line(name, u, s):
    if not u or not u.get("days"):
        return f"| {name} | – | – | – | – | – | – | – | – |"
    return (f"| {name} | {u['days']} | {u['trades']:.1f} | {u['sh5']:,.0f} / {u['sh20']:,.0f} | "
            f"{_usd(u['cost5'])} / {_usd(u['cost20'])} | {_usd(u['profit5'])} / {_usd(u['profit20'])} | "
            f"{_usd(u['cap5'])} / {_usd(u['cap20'])} | {_usd(u['peak5'])}（{_usd(u['pmax5'])}） / "
            f"{_usd(u['peak20'])}（{_usd(u['pmax20'])}） | {_st(s)} |")


UNITS_HEAD = ["| 段 | 天数 | 笔/天 | 份/天（每笔 5 份 / 每笔 ≤ 20 份） | 买入花费 $/天（5 / 20） | 利润 $/天（5 / 20） | "
              "平均占用资金 $（5 / 20） | 当天峰值占用 $：中位（最大）（5 / 20） | 每份盈亏 ± 标准误 |",
              "|---|---:|---:|---:|---:|---:|---:|---:|---|"]


def _pairs(rows):
    """Row positions (jump side, opposite side) of the jump points where both sides were entered."""
    kind = rows["kind"].astype(str).to_numpy()
    side, jdir = rows["side"].to_numpy(int), rows["jdir"].to_numpy(int)
    key = pd.DataFrame({"m": rows["market"].astype(str).to_numpy(), "t": np.round(rows["t"].to_numpy(float), 3),
                        "pos": np.arange(len(rows))})
    js = key[(kind == "jump") & (side == jdir)]
    jo = key[(kind == "jump") & (side == -jdir)]
    m = js.merge(jo, on=["m", "t"], suffixes=("_s", "_o"))
    return m["pos_s"].to_numpy(np.int64), m["pos_o"].to_numpy(np.int64)


def cost_table(rows, segs=("A", "B")):
    """Raw per-share pnl of every policy by row group (no model, no filter), A and B only, with the
    matched contrast (jump side - opposite side) / 2 at the same point and the effective-bid rows."""
    seg = segment_of(rows["start"].to_numpy(float))
    kind = rows["kind"].astype(str).to_numpy()
    side, jdir = rows["side"].to_numpy(int), rows["jdir"].to_numpy(int)
    groups = (("急动这一方", (kind == "jump") & (side == jdir)), ("急动反方向", (kind == "jump") & (side == -jdir)),
              ("固定时点（两方）", kind == "fixed"), ("随机时点、随机方向", kind == "random"))
    mk = rows["market"].astype(str).to_numpy()
    ps, po = _pairs(rows)
    lines = [(POLICY_NAMES[p], f"pnl_{p}") for p in POLICIES]
    lines += [(POLICY_NAMES[p] + "（有效买一）", f"pnl_eff_{p}") for p in EFF_POLICIES if f"pnl_eff_{p}" in rows]
    L = []
    for sg in segs:
        L += ["", f"**{sg} 段**", "", "| 执行方式 | " + " | ".join(g for g, _ in groups) + " | 同一急动点：（这一方 − 反方向）÷ 2 |",
              "|---|" + "---:|" * (len(groups) + 1)]
        pk = seg[ps] == sg
        for name, col in lines:
            y = rows[col].to_numpy(float)
            cells = []
            for _, m in groups:
                k = m & (seg == sg)
                st = cl_stat(y[k], mk[k])
                cells.append("–" if not st["n"] else f"{_c(st['mean'])} ±{100 * st['se']:.2f}（{st['n']:,}）")
            st = cl_stat((y[ps[pk]] - y[po[pk]]) / 2.0, mk[ps[pk]])
            cells.append("–" if not st["n"] else f"{_c(st['mean'])} ±{100 * st['se']:.2f}（{st['n']:,} 个点）")
            L.append(f"| {name} | " + " | ".join(cells) + " |")
        cells = []
        for _, m in groups:
            k = m & (seg == sg)
            if not k.any():
                cells.append("–")
                continue
            p = rows["price"].to_numpy(float)[k]
            sp = rows["spread"].to_numpy(float)[k]
            cells.append(f"{np.nanmean(p):.3f} / {100 * np.nanmean(sp):.2f}¢（中位 {100 * np.nanmedian(sp):.0f}¢） / "
                         f"{100 * np.nanmean(fee(p)):.2f}¢")
        L.append("| 买入价 / 决策时价差 / 买入手续费（平均） | " + " | ".join(cells) + " | |")
        if all(f"how_mk{x}_10" in rows for x in MAKER_X):
            cells = []
            for _, m in groups:
                k = m & (seg == sg)
                cells.append("–" if not k.any() else
                             " / ".join(f"{np.mean(rows[f'how_mk{x}_10'].to_numpy()[k] == 2):.0%}" for x in MAKER_X))
            L.append("| 挂单 10 秒内成交率（+1 / +2 / +3 / +5¢） | " + " | ".join(cells) + " | |")
        if all(f"fs{x}" in rows and f"how_mk{x}_10" in rows for x in MAKER_X):
            left = rows["start"].to_numpy(float) + 300.0 - rows["t"].to_numpy(float) - LAT_S
            cells = []
            for _, m in groups:
                k = m & (seg == sg)
                if not k.any():
                    cells.append("–")
                    continue
                r = []
                for x in MAKER_X:
                    fs = rows[f"fs{x}"].to_numpy(float)[k]
                    with np.errstate(invalid="ignore"):
                        hit = (rows[f"how_mk{x}_10"].to_numpy()[k] != 3) & np.isfinite(fs) & \
                            (fs <= np.minimum(10.0, left[k]) + T_EPS)
                    r.append(f"{np.mean(hit):.0%}")
                cells.append(" / ".join(r))
            L.append("| 同上，成交价须高于挂单价（排队敏感性） | " + " | ".join(cells) + " | |")
        if "how_tk5" in rows:
            cells = []
            for _, m in groups:
                k = m & (seg == sg)
                if not k.any():
                    cells.append("–")
                    continue
                te = rows["t"].to_numpy(float)[k] + LAT_S
                early = te + 5 + LAT_S < rows["start"].to_numpy(float)[k] + 300.0 - T_EPS
                how = rows["how_tk5"].to_numpy()[k]
                wait = rows["hold_tk5"].to_numpy(float)[k][how == 1] - 5 - LAT_S
                late = wait > T_EPS
                cells.append((f"{np.mean(how[early] == 0):.1%}" if early.any() else "–") + " / "
                             + (f"{np.mean(late):.0%} / {np.median(wait[late]) if late.any() else 0:.1f} 秒" if len(wait) else "– / –"))
            L.append("| 5 秒吃单卖：没有可用买一、持有到结算 / 要等下一个可用买一 / 等的中位秒数 | " + " | ".join(cells) + " | |")
    return L


def _close_reason(o, meta):
    if not o["b_pass"]:
        return "未开锁箱（B 未通过）"
    if meta.get("trial"):
        return "未开锁箱（试跑）"
    if meta.get("problems"):
        return "未开锁箱（冻结文件不能用作判定）"
    if not meta.get("complete_c", True):
        return "未开锁箱（有日档没读到）"
    if not meta.get("lock_ok", True):
        return "未开锁箱（锁箱已开过一次，见下）"
    return "未开锁箱"


def _passes(s, alpha):
    return bool(s and s.get("n") and s["mean"] > 0 and np.isfinite(s.get("p", np.nan)) and s["p"] < alpha)


def _cov_text(cov):
    return "，".join(f"{k} 段 {v[0]}/{v[1]} 个" for k, v in cov.items()) if cov else ""


W_NAMES = {0: "点价结算", 30: "30 秒均价结算", 60: "60 秒均价结算"}


def report(rows, spec, fresh, res, meta, infos, failed, skipped, n_arcs, missing, ds, frozen_path, rows_path):
    days = [i["day"] for i in infos]
    seg = segment_of(rows["start"].to_numpy(float)) if len(rows) else np.array([], dtype=object)
    cov = meta.get("coverage")
    L = [f"# 成本侧改造 + 多信号叠加（MIX.md）：Hugging Face 5–8 月 BTC 5m 毫秒盘口（{ds or 'whodisidk'}）", "",
         "设计在 [`MIX.md`](../MIX.md)（跑之前写死并提交），代码 `mix_hf.py`。纸面研究，只用行情数据，不下单。"
         "所有价格是每份（1 份赢了付 $1），¢ = 美分；手续费 taker 0.07·p(1 − p)，挂单方不付。", ""]
    if meta.get("trial"):
        L += ["**试跑，不是判定。** 这次没有读全 A 段的日档" + (f"（按日档日期：{_cov_text(cov)}）" if cov else "")
              + "，规则冻结在工作目录的 `" + Path(frozen_path).name + "`（每次重写、不提交），锁箱 C 不开。"
              "正式判定要一次读全 5/25–8/29 的日档（`cross.py mix`，不带天数）。", ""]
    L += [f"数据：{n_arcs} 个日档里读到 {len(days)} 个（{days[0] if days else '–'} 至 {days[-1] if days else '–'}）"
          + (f"，读失败 {len(failed)} 个：{'、'.join(failed)}" if failed else "")
          + (f"，因时间预算没读 {len(skipped)} 个：{'、'.join(skipped)}" if skipped else "") + "。"
          + (f"按日档日期，MANIFEST.txt 里 {_cov_text(cov)}读到。" if cov else "")
          + f"只用官方最终结果已知的 BTC 5m 市场（{sum(i['markets'] for i in infos):,} 个，按日档计）。"
          "下载的日档都对过 MANIFEST.txt 的字节数和 sha256。", ""]
    if len(rows):
        kind = rows["kind"].astype(str).to_numpy()
        w = j2.settle_twap(rows["start"].to_numpy(float))
        L += ["| 段 | 市场开盘日期 | 天数 | 市场 | 急动决策点行 | 固定时点行 | 随机对照行 | 结算方式（市场数：点价 / 30 秒均价 / 60 秒均价） |",
              "|---|---|---:|---:|---:|---:|---:|---|"]
        for name, a, b in SEGMENTS:
            k = seg == name
            mw = pd.Series(w[k], index=rows.loc[k, "market"].astype(str).to_numpy()).groupby(level=0).first()
            L.append(f"| {name} | {a} – {(pd.Timestamp(b) - pd.Timedelta(days=1)).strftime('%Y-%m-%d')} | "
                     f"{n_days(rows[k])} | {rows.loc[k, 'market'].nunique():,} | {int((k & (kind == 'jump')).sum()):,} | "
                     f"{int((k & (kind == 'fixed')).sum()):,} | {int((k & (kind == 'random')).sum()):,} | "
                     + " / ".join(f"{int((mw == v).sum()):,}" for v in (0, 30, 60)) + " |")
        L += ["", "每个急动 / 固定决策点算两行（买 Up、买 Down 各一行）；只留下 0.5 秒后能按卖一买进的行"
              "（盘口 1 秒内变过、在交易状态、没交叉、0.02–0.98、卖一 ≥ 5 份）。最后一列是各段市场的结算方式"
              "（8/07 前点价、8/07–8/13 30 秒均价、8/14 起 60 秒均价）；模型只在 A 段的行上训练。", ""]
    L += ["## 规则（照 MIX.md）", "",
          f"- **决策点**（记录机收到时刻）：币安逐笔成交相对 1–5 秒前的最后一笔 ≥ {JUMP_BP:g} bp 的急动（参照的那一笔也必须在触发那一笔收到时已经收到；"
          "剩 285–20 秒，同一市场间隔 ≥ 5 秒），加每个市场剩 240、180、120、60 秒的固定时点。",
          "- **特征**（只用决策时刻及之前收到的数据，按买的这一方取向）：" + "、".join(FEATURE_NAMES[f] for f in FEATURES) + "。",
          f"- **买入**：决策后 0.5 秒按这一方的卖一吃单（≥ 5 份）。**平仓**：① 买后 h = {'、'.join(map(str, HOLDS))} 秒决定按买一吃单卖"
          "（再晚 0.5 秒；那一刻的买一要可用：盘口 1 秒内变过、没交叉、买一 > 0 且 ≥ 5 份；不可用就等到下一个可用的买一再卖，"
          "到市场结束都没有才持有到结算）；② 买后在 买入价 + x（x = "
          f"{'、'.join(map(str, MAKER_X))}¢，最高 0.99）挂卖单，0.5 秒后生效；之后这一方代币有吃单买入价 ≥ 挂单价的成交、"
          "或买一 ≥ 挂单价才算成交，到 h 秒还没成交就撤单按 ① 吃单卖；③ 持有到官方结算。共 21 种执行方式。"
          "买一只看这个代币自己的盘口（MIX.md；对方代币的卖单能撮合的价格见下面的“有效买一”敏感性）。",
          f"- **模型**：每种执行方式各拟合岭回归（α = {RIDGE_ALPHA:g}，特征标准化）和梯度提升树（深 3、8 叶、每叶 ≥ 400、"
          "学习率 0.05、200 轮），预测每份盈亏。A 内按周滚动（每周用开始前已结束的市场训练，前 14 天只训练），得到 A 的样本外分数。",
          f"- **挑选**：规则 = （执行方式，模型，样本外分数前 {'、'.join(f'{100 * q:g}%' for q in QS)}），门槛取 A 样本外分数的分位数；"
          f"只按 A 样本外每份盈亏挑最好的 {N_PICK} 个（至少 {MIN_A_TRADES} 笔），冻结在 `{Path(frozen_path).name}`，然后才读 B、C。"
          f"B 和 C 用全部 A 训练的模型打分。B：每份 > 0 且单边 p < 0.05/3（按市场聚类）才进锁箱；C 只算一次，每份 > 0 且 p < 0.05 才通过。",
          "- **单信号对照**：同一执行方式、同一比例，只用 A 样本外最好的单个特征（正反两个方向都试）排名。",
          "- **笔数怎么数**：一条规则不限制每个市场只持一笔仓：同一个市场的几个决策点都可以买，同一个决策点的两方也可以都买；"
          "n 把这些叠在一起的买入都算一笔，标准误按市场聚类（k = 市场数），所以有效样本更接近市场数。", ""]
    rules = spec.get("rules", [])
    problems = meta.get("problems") or []
    L += ["## 判定", ""]
    note = ("本次运行冻结" if fresh else "冻结文件已存在，沿用（未重新冻结）") + f"：{spec.get('made', '–')}"
    if meta.get("frozen_sha256"):
        note += f"，文件 sha256 `{meta['frozen_sha256'][:16]}…`（在给 B、C 打分之前记进日志）"
    note += f"；冻结时 A 段 {spec.get('a_rows', 0):,} 行（模型行），这次 A 段 {meta.get('a_rows_now', 0):,} 行。"
    L.append(note)
    if problems:
        L += ["", "**这不是判定：** " + "；".join(problems) + f"。C 不开。要重新冻结，先删掉 `{Path(frozen_path).name}`"
              + ("（锁箱已经开过，第一次的结果见下）" if meta.get("lock") else "") + "。"]
    if not rules:
        if fresh:
            L += ["", f"A 段没有一个候选规则达到 {MIN_A_TRADES} 笔样本外交易，没有可冻结的规则。"]
        elif not problems:
            L += ["", f"沿用的冻结文件里没有规则：冻结时 A 段没有一个候选达到 {MIN_A_TRADES} 笔样本外交易。"]
    if rules:
        L += ["", "| 规则 | 执行方式 | 模型 | 前 q | A 样本外每份 | B 每份（p） | B 通过（p < 0.0167） | C 每份（p） | C 通过 |",
              "|---|---|---|---:|---|---|---|---|---|"]
        for o in res:
            r = o["rule"]
            b, c = o["stats"].get("B"), o["stats"].get("C")
            cells = [r["id"], POLICY_NAMES[r["policy"]], r["model"], f"{100 * r['q']:g}%", _st(o["stats"].get("A"))]
            cells += [f"{_st(b)}（p {_p(b)}）", "是" if o["b_pass"] else "否"]
            if o["c_open"]:
                cells += [f"{_st(c)}（p {_p(c)}）", "**通过，上前向**" if o["c_pass"] else "否"]
            else:
                cells += [_close_reason(o, meta), "–"]
            L.append("| " + " | ".join(cells) + " |")
        L += ["", "B、C 都用全部 A 训练出的模型打分，门槛是冻结时 A 样本外分数的分位数（所以 B、C 的成交比例不一定正好是 q）。"]
        L += ["", "**两个敏感性（不改判定）。** 排队：MIX.md 的口径是成交价 ≥ 挂单价就算挂单成交，等于假设我们排在同价位最前；"
              "成交价正好等于挂单价时，同价位排在前面的单可能先成交。“严格排队”只算成交价**高于**挂单价（或买一 ≥ 挂单价）才成交，"
              "没成交照旧到点吃单卖。有效买一：Polymarket 会把卖单和另一个代币的卖单撮合（合并），所以能卖到的价是 "
              "max(本代币买一, 1 − 另一代币卖一)；判定只用本代币买一（MIX.md），吃单卖的成本因此偏高（另一代币的卖单在本代币买一"
              "短暂断档时常常还在）。“有效买一”用这个价吃单卖、判断挂单是否一到就成交，挂单成交也算 1 − 另一代币卖一 ≥ 挂单价"
              "和另一代币的吃单卖价 ≤ 1 − 挂单价。", "",
              "| 规则 | 段 | MIX.md 口径 | 严格排队 | 有效买一 |", "|---|---|---|---|---|"]
        only = []
        for o in res:
            r = o["rule"]
            for k in ("A", "B", "C"):
                if k not in o["stats"]:
                    continue
                s_, e_ = o["strict"].get(k), o["eff"].get(k)
                L.append(f"| {r['id']} | {k} | {_st(o['stats'][k])}（p {_p(o['stats'][k])}） | "
                         + (f"{_st(s_)}（p {_p(s_)}）" if s_ else "不适用（不挂单）") + " | "
                         + (f"{_st(e_)}（p {_p(e_)}）" if e_ and r["policy"] != "settle" else "不适用（不卖）") + " |")
            if o["b_pass"] and o["strict"].get("B") is not None and not _passes(o["strict"].get("B"), B_ALPHA):
                only.append(r["id"])
        if only:
            L += ["", "只在“成交价 ≥ 挂单价”（排在队首）的口径下才通过 B 的挂单规则：" + "、".join(only)
                  + "。它们的 B（和 C）结果依赖排队位置的假设。"]
    lock = meta.get("lock")
    if lock:
        if meta.get("lock_written"):
            L += ["", f"锁箱第一次打开，记录在 `{Path(meta['lock_path']).name}`（冻结文件 sha256、C 行指纹、C 的结果）。"]
        elif meta.get("lock_ok", True):
            L += ["", f"锁箱第一次在 {lock.get('opened', '–')} 打开；这次是同一冻结文件、同一 C 数据的重复计算，结果相同。"]
        else:
            L += ["", f"**锁箱已在 {lock.get('opened', '–')} 开过一次**，这次的冻结文件或 C 数据与那次不同，C 不再计算。第一次的结果：", "",
                  "| 规则 | 执行方式 | 模型 | 前 q | C 每份（p） | C 通过 |", "|---|---|---|---:|---|---|"]
            for x in lock.get("results", []):
                L.append(f"| {x['rule']} | {POLICY_NAMES.get(x['policy'], x['policy'])} | {x['model']} | {100 * x['q']:g}% | "
                         f"{_st(x.get('C'))}（p {_p(x.get('C'))}） | {'通过' if x.get('c_pass') else '否'} |")
    L += ["", "## 每条规则每天的量（带单位）", "",
          "份数：每笔 5 份（卖一至少 5 份），或每笔 min(20, 买入时卖一数量, 吃单卖出时买一数量) 份（用了卖出那一刻的买一数量，"
          "是事后才知道的量，卖不掉的部分不计入花费和利润；只影响 20 份的几列，不影响每份盈亏和判定）；挂单成交按整单成交算。"
          "买入花费 = 份数 × (买入价 + 手续费)；利润 = 份数 × 每份盈亏；占用资金按买入成交到卖出成交（持有到结算的算到市场结束）"
          "的时间加权；峰值是同一天同时持仓花费之和的最大值，表里是各天峰值的中位数，括号里是峰值最大的一天"
          "（没有交易的天算 0）。天数 = 该段有数据的 UTC 天数（A 只算样本外 "
          f"{spec.get('a_oos_from', '–')} 起；B、C 里有几个日档只有几十个市场，也按一天算，每天的量因此偏低）。", ""]
    for o in res:
        r = o["rule"]
        L += [f"### {r['id']}：{POLICY_NAMES[r['policy']]}，{r['model']}，前 {100 * r['q']:g}%（分数门槛 {r['cut']:+.4f}）", ""]
        L += UNITS_HEAD
        for name in ("A", "B", "C"):
            if name in o["units"]:
                L.append(_units_line(f"{name}{'（样本外）' if name == 'A' else ''}", o["units"][name], o["stats"][name]))
            else:
                L.append(f"| {name} | 未开锁箱 | – | – | – | – | – | – | – |")
        ex = []
        for name in ("A", "B", "C"):
            e = o["extra"].get(name)
            if not e or not e["n"]:
                continue
            t = f"{name} 段 {e['n']:,} 笔落在 {e['markets']:,} 个市场（每个市场 {e['n'] / max(e['markets'], 1):.1f} 笔），同一时点两方都买 {e['pairs']:,} 次"
            if np.isfinite(e.get("held_early", np.nan)):
                t += f"；计划在结束前卖、却因到结束都没有可用买一而持有到结算的占 {e['held_early']:.1%}"
                if np.isfinite(e.get("held_won", np.nan)):
                    t += f"（其中最后赢的占 {e['held_won']:.0%}：多是没人买的输家一方）"
            if np.isfinite(e.get("wait", np.nan)):
                t += f"；吃单卖平均比计划晚 {e['wait']:.1f} 秒成交（等下一个可用买一）"
            ex.append(t + "。")
        if ex:
            L += [""] + ex
        ctl = r.get("control")
        if ctl:
            L += ["", f"单信号对照：{FEATURE_NAMES[ctl['feature']]}（{'越大越好' if ctl['sign'] > 0 else '越小越好'}，"
                  f"门槛 {ctl['cut']:+.4f}）。对照的特征、方向和门槛是在 A 样本外的行上挑出来的，所以它的 A 行是样本内的最大值，"
                  "和规则的 A 样本外数字不能直接比；规则和对照只在 B、C 上公平比较。", ""] + UNITS_HEAD
            for name in ("A", "B", "C"):
                if name in o["ctl_units"]:
                    L.append(_units_line(f"{name}{'（在这些行上挑的，样本内）' if name == 'A' else ''}", o["ctl_units"][name],
                                         o["ctl_stats"][name]))
                else:
                    L.append(f"| {name} | 未开锁箱 | – | – | – | – | – | – | – |")
        else:
            L += ["", "单信号对照：没有单个特征在 A 样本外达到 300 笔。"]
        if o["by_w"]:
            L += ["", "按结算方式拆开（不改判定；A 全是点价结算）：", "", "| 段 | 结算方式 | 规则每份 | 对照每份 |", "|---|---|---|---|"]
            for name in ("B", "C"):
                bw, cw = o["by_w"].get(name, {}), o["ctl_by_w"].get(name, {})
                for v in sorted(set(bw) | set(cw)):
                    L.append(f"| {name} | {W_NAMES.get(v, v)} | {_st(bw.get(v))} | {_st(cw.get(v))} |")
        L.append("")
    cand = pd.DataFrame(spec.get("candidates", []))
    if len(cand):
        top = cand[cand["n"] >= MIN_A_TRADES].sort_values("mean", ascending=False).head(15)
        L += ["## A 样本外候选（前 15，挑选只看这一列）", "",
              f"{spec.get('n_candidates', len(cand))} 个候选，{spec.get('n_eligible', len(top))} 个达到 {MIN_A_TRADES} 笔。"
              "最好的几个是从很多候选里挑出来的最大值，A 上的数字偏乐观，所以要看 B。"]
        if len(top):
            L += ["", "| 执行方式 | 模型 | 前 q | 每份 ± 标准误 |", "|---|---|---:|---|"]
        for r in top.itertuples(index=False):
            L.append(f"| {POLICY_NAMES[r.policy]} | {r.model} | {100 * r.q:g}% | {_st(r._asdict())} |")
        L.append("")
    if len(rows):
        L += ["## 成本结构：不经模型筛选的原始每份盈亏（A、B；C 是锁箱，不列）", "",
              "每格：每份 ± 按市场聚类的标准误（行数）。随机对照 = 同一市场 285–20 秒内随机时刻、随机方向（每个急动配一个）。"
              "“急动这一方”和随机行的差不只是信号：两者的时点（剩余时间、盘口状态、急动后价差更宽）和成本都不同，"
              "随机行也要过同样的入场条件（盘口 1 秒内变过），是被挑过的一部分时刻，不是无条件的基准。"
              "同一时点、同一盘口、同样成本的对照是最后一列：（急动这一方 − 反方向）÷ 2，只算两方都能买进的急动点。"
              "“有效买一”行是同一执行方式按 max(本代币买一, 1 − 另一代币卖一) 卖的敏感性（不改判定）。"]
        L += cost_table(rows)
        L.append("")
    L += ["## 数据覆盖", "", "<details><summary>每个日档</summary>", "",
          "| 日期 | 市场 | 只有临时结算（不用） | 急动点 | 固定点 | 随机点 | 行（能买进） | 决策点行买不进 | "
          "币安逐笔最大乱序（秒） | 按成交时间的参照晚到的急动点（已跳过） | 最后一根 K 线仍有成交在路上的点 |",
          "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for i in infos:
        L.append(f"| {i['day']} | {i['markets']} | {i.get('provisional', 0)} | {i['jump']:,} | {i['fixed']:,} | "
                 f"{i['random']:,} | {i['rows']:,} | {i['no_entry']:,} | {i.get('disorder_s', float('nan')):.2f} | "
                 f"{i.get('ref_late', 0):,} | {i.get('kline_late', 0):,} |")
    L += ["", "币安逐笔按记录机收到的时间会乱序：急动的参照只用触发那一笔收到时已收到的成交；K 线只用比已收到的最新成交时间早 "
          f"{KLINE_GUARD_S:g} 秒以上收盘的分钟。后两列是用全天数据做的检查（不是特征）。", "</details>", "",
          f"冻结文件 `{Path(frozen_path).name}`，冻结规则（和对照）交易过的逐行数据在 `{Path(rows_path).name}`"
          f"（{meta.get('rows_note', '')}；t = 决策时刻，记录机时钟，秒；pnl 是那条规则执行方式的每份盈亏，pnl_strict / pnl_eff 是"
          "两个敏感性下的每份盈亏，how：" + "、".join(f"{k} {v}" for k, v in HOW.items()) + "）。"
          + (f" K 线缺 {'、'.join(missing)}，这些月份的分钟收盘改用日档里的币安逐笔推出。" if missing else "")]
    return L
