"""100 rules on the whodisidk 100 ms BTC 5m books (cross.py zoo).

Every 5m market is looked at once a second from 295 s to 10 s before the end (the recorder's
clock). At each second the state is what the recorder had received by then: both books (best
prices and sizes), the Up mid 2/10/30 s earlier, Binance returns over 1/5/30/60 s in units of
the 10 min per-second sigma, the realized volatility of the last 60 s against that sigma, the
Polymarket taker flow of the last 10/30 s, the book's top-of-book imbalance, a TWAP model price
(Binance with the market's own basis against the reference, binary.prob_up), the 5m price the
15m market ending at the same moment implies (both settle on the same TWAP), the hour, and the
previous market's result. Each rule says when to buy which side; its first decision-time signal
freezes that side's current ask as a FAK limit. Five hundred milliseconds later it fills only when
the book is healthy, the ask has not crossed that limit, and at least five shares remain. A killed
first order is not replaced with a hindsight-selected later signal. Filled shares are held to
settlement and pay the fee rate active at the simulated fill time.

Rules are compared on May 25 - Jul 15 (A) only. A rule is a candidate when it made money there
with an exact p < 0.05 on at least 50 trades; a candidate passes when it also made money on
Jul 16 - Aug 16 (B) and on Aug 17 - 29 (C), and B and C together give p < 0.05 / (number of
candidates). Passing here only earns a preregistered live test."""
from __future__ import annotations

import hashlib
from pathlib import Path

GRID_TAUS = tuple(range(295, 9, -1))  # seconds before the end, once a second
FILL_MS = 500
MIN_FILL_SHARES = 5.0
DATE_START = "2026-05-25"
DATE_A_END = "2026-07-15"
DATE_B_END = "2026-08-16"
DATE_END = "2026-08-29"
DATE_SEGMENTS = (("A", DATE_START, DATE_A_END), ("B", "2026-07-16", DATE_B_END),
                 ("C", "2026-08-17", DATE_END))
ZOO_MANIFEST_SHA256 = "dc5223a424bf59bfb45a636a8271727a7dbc798007a28617d4a58aea945e2491"
EXPECTED_ARCHIVES = tuple(f"market_parquet_{day}.tar.gz" for day in (
    "2026-05-25", "2026-05-26", "2026-05-27", "2026-05-28", "2026-05-29",
    "2026-05-30", "2026-05-31", "2026-06-01", "2026-06-02", "2026-06-03",
    "2026-06-04", "2026-06-05", "2026-06-06", "2026-06-07", "2026-06-08",
    "2026-06-09", "2026-06-10", "2026-06-11", "2026-06-12", "2026-06-13",
    "2026-06-14", "2026-06-15", "2026-06-16", "2026-06-17", "2026-06-18",
    "2026-06-19", "2026-06-20", "2026-06-21", "2026-06-23", "2026-06-24",
    "2026-06-25", "2026-06-26", "2026-06-27", "2026-06-28", "2026-06-29",
    "2026-06-30", "2026-07-01", "2026-07-02", "2026-07-03", "2026-07-04",
    "2026-07-05", "2026-07-06", "2026-07-11", "2026-07-13", "2026-07-14",
    "2026-07-15", "2026-07-17", "2026-07-18", "2026-08-06", "2026-08-12",
    "2026-08-13", "2026-08-14", "2026-08-15", "2026-08-16", "2026-08-17",
    "2026-08-18", "2026-08-19", "2026-08-20", "2026-08-21", "2026-08-22",
    "2026-08-23", "2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27",
    "2026-08-28", "2026-08-29",
))
REQUIRED_DAYS = tuple(name[15:25] for name in EXPECTED_ARCHIVES)


def _coverage_summary(covered):
    covered = set(covered)
    return "; ".join(
        f"{label} {sum(start <= day <= end for day in covered)}/"
        f"{sum(start <= day <= end for day in REQUIRED_DAYS)}"
        for label, start, end in DATE_SEGMENTS
    )


def _snap(fts, xs, max_age=1000):
    import numpy as np
    k = np.searchsorted(fts, xs, "right") - 1
    ok = (k >= 0) & (xs - fts[np.maximum(k, 0)] <= max_age)
    return np.where(ok, k, -1)


def build_state(feat, mkts, binance, trades=None, fill_ms=FILL_MS):
    """One row per 5m market and grid second (see the module notes)."""
    import warnings
    warnings.filterwarnings("ignore", category=RuntimeWarning)  # empty windows give NaN, as intended
    import numpy as np
    import pandas as pd
    import binary as bo
    import cross
    if binance.empty or feat.empty:
        return pd.DataFrame()
    received = binance[["recv_ts_ms", "price"]].apply(pd.to_numeric, errors="coerce")
    received = received.dropna().query("price > 0").sort_values("recv_ts_ms", kind="stable")
    if received.empty:
        return pd.DataFrame()
    rt = received["recv_ts_ms"].to_numpy(np.int64)
    lp = np.log(received["price"].to_numpy(float))
    available_sec = (rt + 999) // 1000
    last = pd.Series(lp, index=available_sec).groupby(level=0).last()
    g0 = int(last.index[0])
    grid = last.reindex(range(g0, int(last.index[-1]) + 1)).ffill(limit=10).to_numpy()
    gret = np.r_[np.nan, np.diff(grid)]
    sig = pd.Series(gret).rolling(600, min_periods=300).std().to_numpy()
    csum = np.nancumsum(np.where(np.isfinite(grid), grid, 0.0))
    ccnt = np.cumsum(np.isfinite(grid))

    def gidx(sec):
        return np.clip(np.asarray(sec, dtype=np.int64) - g0, 0, len(grid) - 1)

    def lp_recv(x):
        j = np.searchsorted(rt, x, "right") - 1
        return np.where(j >= 0, lp[np.maximum(j, 0)], np.nan)

    flows = {}
    if trades is not None and len(trades):
        t = trades.sort_values("recv_ts_ms", kind="stable")
        for (tok, side), g in t.groupby(["instrument", "taker_side"]):
            flows[(str(tok), side)] = (g["recv_ts_ms"].to_numpy(), np.cumsum(np.r_[0.0, g["size"].to_numpy(float)]))

    def flow(tok, side, lo, hi):
        if (tok, side) not in flows:
            return np.zeros(len(lo))
        r, cs = flows[(tok, side)]
        return cs[np.searchsorted(r, hi, "right")] - cs[np.searchsorted(r, lo, "right")]

    m = mkts[mkts["up_won"].notna()].copy()
    by = {mid: g.drop_duplicates("timestamp_ms").sort_values("timestamp_ms")
          for mid, g in feat[feat["market_id"].isin(set(m["market_id"]))].groupby("market_id")}
    m15 = m[m["horizon"] == 15].set_index("end")
    m5 = m[m["horizon"] == 5].sort_values("end")
    taus = np.asarray(GRID_TAUS)
    out = []
    for mk in m5.itertuples():
        if mk.market_id not in by:
            continue
        f = by[mk.market_id]
        fts = f["timestamp_ms"].to_numpy()
        col = {c: f[c].to_numpy(float) if c in f else np.full(len(f), np.nan)
               for c in ("up_best_bid", "up_best_ask", "down_best_bid", "down_best_ask",
                         "up_bid_size", "up_ask_size", "down_bid_size", "down_ask_size")}
        ub, ua, db, da = col["up_best_bid"], col["up_best_ask"], col["down_best_bid"], col["down_best_ask"]
        mids = (ub + ua) / 2
        sane, changes = cross.book_health(f)
        xs = mk.end - taus * 1000
        k0 = _snap(fts, xs)
        fill_at = xs + fill_ms
        kf = _snap(fts, fill_at)
        decision_present = k0 >= 0
        if not decision_present.any():
            continue
        i_b = np.searchsorted(changes, xs, "right")
        i_f = np.searchsorted(changes, fill_at, "right")
        pre_alive = (i_b > 0) & (xs - changes[np.maximum(i_b - 1, 0)] <= cross.HEALTH_ALIVE[0])
        fill_alive = (i_f > 0) & (fill_at - changes[np.maximum(i_f - 1, 0)] <= cross.HEALTH_ALIVE[0])
        k0c, kfc = np.maximum(k0, 0), np.maximum(kf, 0)
        decision_ok = (decision_present & sane[k0c] & pre_alive &
                       np.isfinite(ub[k0c]) & np.isfinite(ua[k0c]))
        fill_ok = (kf >= 0) & sane[kfc] & fill_alive
        ok = decision_ok & fill_ok

        def mid_at(off):
            k = _snap(fts, xs + off)
            return np.where(k >= 0, mids[np.maximum(k, 0)], np.nan)

        lpx = lp_recv(xs)
        market_start_ms = mk.end - 300_000
        previous_move = float(lp_recv(market_start_ms) - lp_recv(market_start_ms - 300_000))
        previous_binance_up = (
            float(previous_move > 0) if np.isfinite(previous_move) and previous_move != 0 else np.nan
        )
        sec = xs // 1000
        sg = sig[gidx(sec - 1)]
        rets = {L: (lpx - lp_recv(xs - L * 1000)) / (sg * np.sqrt(L)) for L in (1, 5, 30, 60)}
        win = np.lib.stride_tricks.sliding_window_view(np.r_[np.full(60, np.nan), gret], 60)
        rv60 = np.nanstd(win[np.clip(gidx(sec - 1) + 1, 0, len(win) - 1)], axis=1) / sg
        # TWAP model price: Binance less the market's basis against the reference
        t_mkt = (300 - taus).astype(float)
        s0 = mk.end // 1000 - 300
        lo_b, hi_b = gidx(s0 - 59), gidx(s0)
        nb = ccnt[hi_b] - (ccnt[lo_b - 1] if lo_b > 0 else 0)
        basis = ((csum[hi_b] - (csum[lo_b - 1] if lo_b > 0 else 0.0)) / nb - np.log(mk.k)) \
            if nb >= 30 and np.isfinite(mk.k) and mk.k > 0 else np.nan
        ks = gidx(s0 + 240)
        kn = gidx(s0 + t_mkt)
        known = np.where(t_mkt > 240, csum[kn] - csum[ks], 0.0) - np.maximum(t_mkt - 240, 0) * basis
        with np.errstate(invalid="ignore", divide="ignore"):
            fair_m = bo.prob_up(lpx - basis, np.log(mk.k) if mk.k > 0 else np.nan, sg, t_mkt, known_sum=known)
            # the 5m price implied by the 15m market that ends at the same moment (same TWAP)
            p15 = np.full(len(taus), np.nan)
            if mk.end in m15.index and m15.loc[[mk.end]].iloc[0]["market_id"] in by:
                q = m15.loc[[mk.end]].iloc[0]
                f15 = by[q["market_id"]]
                k15 = _snap(f15["timestamp_ms"].to_numpy(), xs)
                m15mid = (f15["up_best_bid"].to_numpy(float) + f15["up_best_ask"].to_numpy(float)) / 2
                mid15 = np.where(k15 >= 0, m15mid[np.maximum(k15, 0)], np.nan)
                std = sg * bo.twap_std_factor(t_mkt)
                from scipy.stats import norm
                p15 = norm.cdf(norm.ppf(np.clip(mid15, 0.005, 0.995)) + (np.log(q["k"]) - np.log(mk.k)) / std)
        up_t, dn_t = str(getattr(mk, "up_token", "")), str(getattr(mk, "down_token", ""))
        net = {}
        for L in (10, 30):
            lo_t = xs - L * 1000
            net[L] = (flow(up_t, "buy", lo_t, xs) + flow(dn_t, "sell", lo_t, xs)
                      - flow(dn_t, "buy", lo_t, xs) - flow(up_t, "sell", lo_t, xs))
        ubs = np.where(decision_present, col["up_bid_size"][k0c], np.nan)
        uas = np.where(decision_present, col["up_ask_size"][k0c], np.nan)
        mid0 = np.where(decision_present, mids[k0c], np.nan)
        ub0 = np.where(decision_present, ub[k0c], np.nan)
        ua0 = np.where(decision_present, ua[k0c], np.nan)
        da0 = np.where(decision_present, da[k0c], np.nan)
        uaf = np.where(kf >= 0, ua[kfc], np.nan)
        daf = np.where(kf >= 0, da[kfc], np.nan)
        uasf = np.where(kf >= 0, col["up_ask_size"][kfc], np.nan)
        dasf = np.where(kf >= 0, col["down_ask_size"][kfc], np.nan)
        ci_lo, ci_hi = np.searchsorted(changes, xs - 30000, "left"), np.searchsorted(changes, xs, "right")
        st = pd.DataFrame({
            "market_id": mk.market_id, "end": mk.end, "tau": taus, "ok": ok,
            "decision_present": decision_present, "decision_ok": decision_ok, "fill_ok": fill_ok,
            "mid": mid0, "spread": ua0 - ub0, "mid_m2": mid_at(-2000), "mid_m10": mid_at(-10000),
            "mid_m30": mid_at(-30000), "imb": (ubs - uas) / (ubs + uas),
            "r1": rets[1], "r5": rets[5], "r30": rets[30], "r60": rets[60], "rv60": rv60,
            "fair_m": fair_m, "p15": p15, "flow10": net[10], "flow30": net[30], "chg30": ci_hi - ci_lo,
            "hour": int((mk.end // 1000 - 300) % 86400 // 3600),
            "prev_binance_up": previous_binance_up,
            "fee_rate": cross.taker_rate(xs + fill_ms),
            "ua_0": ua0, "da_0": da0,
            "ua_f": uaf, "da_f": daf, "uas_f": uasf, "das_f": dasf,
            "up_won": float(mk.up_won)})
        out.append(st)
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def _band(x, lo, hi):
    return (x >= lo) & (x < hi)


def _fee_cost(S, price):
    rate = S["fee_rate"] if "fee_rate" in S else 0.07
    return rate * price * (1 - price)


def make_rules():
    """[(id, family, description, fn(S) -> (mask, buy_up))] with exactly 100 rules."""
    import numpy as np
    R = []

    def add(fam, desc, fn):
        R.append((len(R) + 1, fam, desc, fn))

    # 1. favourite / underdog at a fixed time left (25)
    for T in (180, 120, 60, 30, 15):
        for lo, hi, kind in ((0.60, 0.75, "强势"), (0.75, 0.90, "强势"), (0.90, 0.97, "强势"),
                             (0.03, 0.10, "弱势"), (0.10, 0.25, "弱势")):
            def fn(S, T=T, lo=lo, hi=hi):
                up_in, dn_in = _band(S["mid"], lo, hi), _band(1 - S["mid"], lo, hi)
                return (S["tau"] == T) & (up_in | dn_in), up_in
            add("强弱方", f"剩 {T}s 买{kind}方 [{lo:.2f},{hi:.2f})", fn)
    # 2. Binance momentum (6), with Polymarket not yet moved (3), reversal (3)
    for L, z in ((5, 1.5), (5, 2.5), (30, 1.5), (30, 2.5), (60, 1.5), (60, 2.5)):
        add("币安动量", f"币安 {L}s 涨跌 > {z}σ 顺势（剩 240–30s）",
            lambda S, L=L, z=z: ((S["tau"] <= 240) & (S["tau"] >= 30) & (S[f"r{L}"].abs() > z), S[f"r{L}"] > 0))
    for L, back in ((5, "mid_m2"), (30, "mid_m30"), (60, "mid_m30")):
        def fn(S, L=L, back=back):
            up = S[f"r{L}"] > 0
            moved = np.where(up, S["mid"] - S[back], S[back] - S["mid"])
            return (S["tau"] <= 240) & (S["tau"] >= 30) & (S[f"r{L}"].abs() > 1.5) & (moved < 0.02), up
        add("币安动量", f"币安 {L}s 涨跌 > 1.5σ 且 Polymarket 还没跟（< 2¢）顺势", fn)
    for L in (5, 30, 60):
        add("币安反转", f"币安 {L}s 涨跌 > 2.5σ 反向（剩 240–30s）",
            lambda S, L=L: ((S["tau"] <= 240) & (S["tau"] >= 30) & (S[f"r{L}"].abs() > 2.5), S[f"r{L}"] < 0))
    # 3. Polymarket mid momentum and reversal (8)
    for back, d in (("mid_m10", 0.05), ("mid_m10", 0.10), ("mid_m30", 0.05), ("mid_m30", 0.10)):
        for follow in (True, False):
            def fn(S, back=back, d=d, follow=follow):
                ch = S["mid"] - S[back]
                return (S["tau"] <= 240) & (S["tau"] >= 20) & (ch.abs() >= d), (ch > 0) if follow else (ch < 0)
            add("盘口动量" if follow else "盘口反转",
                f"Up 中间价 {back[5:]}s 内变动 ≥ {100 * d:.0f}¢ {'跟随' if follow else '反向'}", fn)
    # 4. TWAP model price against the ask (12)
    for lo_t, hi_t in ((240, 121), (120, 31), (30, 10)):
        for th in (0.03, 0.06, 0.10, 0.15):
            def fn(S, lo_t=lo_t, hi_t=hi_t, th=th):
                fee_u, fee_d = _fee_cost(S, S["ua_0"]), _fee_cost(S, S["da_0"])
                eu, ed = S["fair_m"] - S["ua_0"] - fee_u, (1 - S["fair_m"]) - S["da_0"] - fee_d
                return (S["tau"] <= lo_t) & (S["tau"] >= hi_t) & ((eu >= th) | (ed >= th)), eu >= ed
            add("模型定价", f"TWAP 模型价 − 卖一 − 费 ≥ {100 * th:.0f}¢（剩 {lo_t}–{hi_t}s）", fn)
    # 5. the 15m market's implied price (3)
    for th in (0.03, 0.06, 0.10):
        def fn(S, th=th):
            eu = S["p15"] - S["ua_0"] - _fee_cost(S, S["ua_0"])
            ed = (1 - S["p15"]) - S["da_0"] - _fee_cost(S, S["da_0"])
            return (S["tau"] <= 240) & ((eu >= th) | (ed >= th)), eu >= ed
        add("跨周期", f"同时结束的 15m 市场隐含价 − 卖一 − 费 ≥ {100 * th:.0f}¢", fn)
    # 6. book imbalance (6)
    for lo_t, hi_t in ((240, 61), (60, 10)):
        for q in (0.5, 0.8):
            add("盘口失衡", f"Up 买一/卖一量失衡 ≥ {q}（剩 {lo_t}–{hi_t}s）顺着",
                lambda S, lo_t=lo_t, hi_t=hi_t, q=q: ((S["tau"] <= lo_t) & (S["tau"] >= hi_t) & (S["imb"].abs() >= q), S["imb"] > 0))
        add("盘口失衡", f"Up 买一/卖一量失衡 ≥ 0.8（剩 {lo_t}–{hi_t}s）反着",
            lambda S, lo_t=lo_t, hi_t=hi_t: ((S["tau"] <= lo_t) & (S["tau"] >= hi_t) & (S["imb"].abs() >= 0.8), S["imb"] < 0))
    # 7. taker flow (6)
    for L, q in ((10, 50), (10, 200), (30, 100), (30, 400)):
        add("主动成交", f"{L}s 净主动买入 ≥ {q} 份 跟随",
            lambda S, L=L, q=q: ((S["tau"] <= 240) & (S["tau"] >= 20) & (S[f"flow{L}"].abs() >= q), S[f"flow{L}"] > 0))
    for L, q in ((10, 200), (30, 400)):
        add("主动成交", f"{L}s 净主动买入 ≥ {q} 份 反向",
            lambda S, L=L, q=q: ((S["tau"] <= 240) & (S["tau"] >= 20) & (S[f"flow{L}"].abs() >= q), S[f"flow{L}"] < 0))
    # 8. volatility regime (4)
    for T in (120, 60):
        def fn(S, T=T):
            up_in, dn_in = _band(S["mid"], 0.10, 0.30), _band(1 - S["mid"], 0.10, 0.30)
            return (S["tau"] == T) & (S["rv60"] >= 1.5) & (up_in | dn_in), up_in
        add("波动率", f"剩 {T}s、近 60s 波动 ≥ 1.5 倍平时，买弱势方 [0.10,0.30)", fn)
    for T in (60, 30):
        def fn(S, T=T):
            up_in, dn_in = _band(S["mid"], 0.70, 0.90), _band(1 - S["mid"], 0.70, 0.90)
            return (S["tau"] == T) & (S["rv60"] <= 0.7) & (up_in | dn_in), up_in
        add("波动率", f"剩 {T}s、近 60s 波动 ≤ 0.7 倍平时，买强势方 [0.70,0.90)", fn)
    # 9. spread (2)
    def fn_wide(S):
        eu = S["fair_m"] - S["ua_0"] - _fee_cost(S, S["ua_0"])
        ed = (1 - S["fair_m"]) - S["da_0"] - _fee_cost(S, S["da_0"])
        return (S["tau"] <= 240) & (S["tau"] >= 20) & (S["spread"] >= 0.04) & ((eu >= 0.05) | (ed >= 0.05)), eu >= ed
    add("价差", "价差 ≥ 4¢ 时模型价 − 卖一 − 费 ≥ 5¢", fn_wide)

    def fn_tight(S):
        up_in, dn_in = _band(S["mid"], 0.70, 0.90), _band(1 - S["mid"], 0.70, 0.90)
        return (S["tau"] == 60) & (S["spread"] <= 0.01) & (up_in | dn_in), up_in
    add("价差", "剩 60s、价差 ≤ 1¢，买强势方 [0.70,0.90)", fn_tight)
    # 10. time of day (3)
    for h0, h1, name in ((0, 7, "亚洲"), (7, 13, "欧洲"), (13, 21, "美国")):
        def fn(S, h0=h0, h1=h1):
            up_in, dn_in = _band(S["mid"], 0.70, 0.90), _band(1 - S["mid"], 0.70, 0.90)
            return (S["tau"] == 60) & (S["hour"] >= h0) & (S["hour"] < h1) & (up_in | dn_in), up_in
        add("时段", f"{name}时段（UTC {h0}–{h1} 点）剩 60s 买强势方 [0.70,0.90)", fn)
    # 11. previous five-minute Binance window (2).  This is observable by the
    # current market open; never use the previous market's eventual outcome.
    add("上一窗", "开盘 5s 后跟上一 5m 币安窗口方向（价格 0.30–0.70）",
        lambda S: ((S["tau"] == 295) & S["prev_binance_up"].notna()
                   & _band(S["mid"], 0.30, 0.70), S["prev_binance_up"] == 1))
    add("上一窗", "开盘 5s 后反上一 5m 币安窗口方向（价格 0.30–0.70）",
        lambda S: ((S["tau"] == 295) & S["prev_binance_up"].notna()
                   & _band(S["mid"], 0.30, 0.70), S["prev_binance_up"] == 0))
    # 12. late lock under the TWAP (2)
    for T in (20, 40):
        def fn(S, T=T):
            up = S["fair_m"] >= 0.5
            sure = np.where(up, S["fair_m"], 1 - S["fair_m"]) >= 0.97
            ask = np.where(up, S["ua_0"], S["da_0"])
            return (S["tau"] <= T) & sure & (ask <= 0.95), up
        add("TWAP 锁定", f"剩 ≤ {T}s、模型 ≥ 97% 而卖一 ≤ 0.95", fn)
    # 13. one-second jumps on the second grid (2, late by up to a second: reference only)
    for z in (3.0, 4.0):
        add("急动", f"币安 1s 涨跌 > {z:g}σ 顺势（按秒检查，最多晚一秒，仅作参照）",
            lambda S, z=z: ((S["tau"] <= 240) & (S["tau"] >= 15) & (S["r1"].abs() > z), S["r1"] > 0))
    # 14. Polymarket jumps without Binance (2)
    def jump(S, follow):
        ch = S["mid"] - S["mid_m30"]
        quiet = S["r30"].abs() < 0.5
        return (S["tau"] <= 240) & (S["tau"] >= 20) & (ch.abs() >= 0.15) & (quiet if not follow else ~quiet), \
            (ch > 0) if follow else (ch < 0)
    add("抖动", "30s 内中间价动 ≥ 15¢ 而币安没动（< 0.5σ）反向", lambda S: jump(S, False))
    add("抖动", "30s 内中间价动 ≥ 15¢ 且币安同期也动了 跟随", lambda S: jump(S, True))
    # 15. late momentum near 0.5 (2)
    for T in (30, 60):
        add("尾盘动量", f"剩 {T}s、价格 0.40–0.60，按币安 60s 方向买",
            lambda S, T=T: ((S["tau"] == T) & _band(S["mid"], 0.40, 0.60) & (S["r60"].abs() > 0.5), S["r60"] > 0))
    # 16. opening mispricing against the model (3)
    for th in (0.03, 0.06, 0.10):
        def fn(S, th=th):
            eu = S["fair_m"] - S["ua_0"] - _fee_cost(S, S["ua_0"])
            ed = (1 - S["fair_m"]) - S["da_0"] - _fee_cost(S, S["da_0"])
            return (S["tau"] >= 241) & ((eu >= th) | (ed >= th)), eu >= ed
        add("开盘", f"开盘第一分钟 模型价 − 卖一 − 费 ≥ {100 * th:.0f}¢", fn)
    # 17. combinations (6)
    def model_edge(S, th):
        eu = S["fair_m"] - S["ua_0"] - _fee_cost(S, S["ua_0"])
        ed = (1 - S["fair_m"]) - S["da_0"] - _fee_cost(S, S["da_0"])
        return ((eu >= th) | (ed >= th)) & (S["tau"] <= 240) & (S["tau"] >= 15), eu >= ed
    add("组合", "模型价差 ≥ 6¢ 且盘口失衡同向（≥ 0.3）",
        lambda S: (lambda m, u: (m & (np.where(u, S["imb"], -S["imb"]) >= 0.3), u))(*model_edge(S, 0.06)))
    add("组合", "模型价差 ≥ 6¢ 且 30s 主动成交同向（≥ 50 份）",
        lambda S: (lambda m, u: (m & (np.where(u, S["flow30"], -S["flow30"]) >= 50), u))(*model_edge(S, 0.06)))
    add("组合", "模型价差 ≥ 6¢ 且近 60s 波动 ≤ 平时",
        lambda S: (lambda m, u: (m & (S["rv60"] <= 1.0), u))(*model_edge(S, 0.06)))
    add("组合", "模型价差 ≥ 6¢ 且价差 ≤ 2¢",
        lambda S: (lambda m, u: (m & (S["spread"] <= 0.02), u))(*model_edge(S, 0.06)))

    def fav_mom(S, lo, hi):
        up_in, dn_in = _band(S["mid"], lo, hi), _band(1 - S["mid"], lo, hi)
        agree = np.where(up_in, S["r30"] > 0.5, S["r30"] < -0.5)
        return (S["tau"] == 60) & (up_in | dn_in) & agree, up_in
    add("组合", "剩 60s 强势方 [0.75,0.90) 且币安 30s 同向", lambda S: fav_mom(S, 0.75, 0.90))
    add("组合", "剩 60s 弱势方 [0.10,0.25) 且币安 30s 同向", lambda S: fav_mom(S, 0.10, 0.25))
    assert len(R) == 100, len(R)
    return R


def evaluate(S, rules, min_fill_shares=MIN_FILL_SHARES):
    """First decision-time signal per market and rule under a frozen FAK limit."""
    import numpy as np
    import pandas as pd
    if S.empty:
        return pd.DataFrame()
    parts = []
    for rid, fam, desc, fn in rules:
        mask, up = fn(S)
        decision_ok = S["decision_ok"] if "decision_ok" in S else S["ok"]
        fill_ok = S["fill_ok"] if "fill_ok" in S else S["ok"]
        mask = np.asarray(mask, bool)
        up = np.asarray(up, bool)
        limit = np.where(up, S["ua_0"], S["da_0"])
        px = np.where(up, S["ua_f"], S["da_f"])
        size = np.where(up, S["uas_f"], S["das_f"])
        if not mask.any():
            continue
        d = S.loc[mask, ["market_id", "end", "tau", "up_won"]].copy()
        d["up"], d["limit"] = up[mask], limit[mask]
        d["observed_ask"], d["size"] = px[mask], size[mask]
        fee_rate = S["fee_rate"].to_numpy(float) if "fee_rate" in S else np.full(len(S), 0.07)
        d["fee_rate"] = fee_rate[mask]
        d["decision_ok"] = decision_ok.to_numpy(bool)[mask]
        d["fill_ok"] = fill_ok.to_numpy(bool)[mask]
        d = d.sort_values(["market_id", "tau"], ascending=[True, False]).drop_duplicates("market_id")
        valid_limit = np.isfinite(d["limit"]) & (d["limit"] >= 0.02) & (d["limit"] <= 0.98)
        d["sent"] = d["decision_ok"] & valid_limit
        d["no_send_reason"] = np.select(
            [~d["decision_ok"], ~valid_limit],
            ["book_unavailable", "invalid_limit"],
            default="sent",
        )
        executable = (d["sent"] & d["fill_ok"] & np.isfinite(d["observed_ask"]) & np.isfinite(d["size"]) &
                      (d["observed_ask"] <= d["limit"] + 1e-12) & (d["size"] >= min_fill_shares))
        d["filled"] = executable
        d["price"] = np.where(executable, d["observed_ask"], np.nan)
        d["no_fill_reason"] = np.select(
            [~d["sent"], ~d["fill_ok"], ~np.isfinite(d["observed_ask"]),
             d["observed_ask"] > d["limit"] + 1e-12,
             ~np.isfinite(d["size"]) | (d["size"] < min_fill_shares)],
            ["not_sent", "book_unavailable", "no_ask", "above_limit", "insufficient_depth"],
            default="filled",
        )
        d["rule"] = rid
        parts.append(d)
    if not parts:
        return pd.DataFrame()
    t = pd.concat(parts, ignore_index=True)
    t["fee"] = np.where(t["filled"], t["fee_rate"] * t["price"] * (1 - t["price"]), 0.0)
    t["won"] = np.where(t["up"], t["up_won"], 1 - t["up_won"])
    t["pnl"] = np.where(t["filled"], t["won"] - t["price"] - t["fee"], 0.0)
    return t.drop(columns=["up_won"])


def report(t, rules, reps=5000):
    """The selection on A and the check on B and C, as markdown lines."""
    import numpy as np
    from momentum5s import exact_fair_price_pvalue
    t = t.copy()
    day = t["day"].astype(str)
    outside = (day < DATE_START) | (day > DATE_END)
    if outside.any():
        bad = ", ".join(sorted(day[outside].unique()))
        raise ValueError(f"zoo100 accepts only {DATE_START}..{DATE_END}; got {bad}")
    t["period"] = np.select([day <= DATE_A_END, day <= DATE_B_END], ["A", "B"], "C")
    if "sent" not in t:
        t["sent"] = True
    if "filled" not in t:
        t["filled"] = True
    fills = t[t["filled"]].copy()
    info = {rid: (fam, desc) for rid, fam, desc, _ in rules}

    def pv(g):
        if len(g) < 10 or g["pnl"].mean() <= 0:
            return 1.0
        return exact_fair_price_pvalue(g)

    rows = []
    for rid in sorted(info):
        g = fills[fills["rule"] == rid]
        sent = t[(t["rule"] == rid) & t["sent"]]
        r = {"rule": rid}
        for per in ("A", "B", "C"):
            h = g[g["period"] == per]
            r[f"s{per}"] = int((sent["period"] == per).sum())
            r[f"n{per}"], r[f"ev{per}"] = len(h), (h["pnl"].mean() if len(h) else np.nan)
            r[f"px{per}"] = h["price"].mean() if len(h) else np.nan
        r["pA"] = pv(g[g["period"] == "A"])
        rows.append(r)
    cand = [r for r in rows if r["nA"] >= 50 and r["evA"] > 0 and r["pA"] < 0.05]
    for r in cand:
        bc = fills[(fills["rule"] == r["rule"]) & (fills["period"] != "A")]
        r["pBC"] = pv(bc)
        r["pass"] = (r["evB"] > 0) and (r["evC"] > 0) and r["pBC"] < 0.05 / max(len(cand), 1)
    L = [f"100 条规则，A 段（5/25–7/15）实际成交 ≥ 50、赚钱且 p < 0.05 的候选 {len(cand)} 条；"
         f"B（7/16–8/16）和 C（8/17–8/29）都赚钱、且 B+C 合起来 p < 0.05/{max(len(cand), 1)} 的："
         f"{sum(r.get('pass', False) for r in cand)} 条。", ""]
    if cand:
        L += ["## 候选（A 段选出）", "", "| # | 类别 | 规则 | A 成交/发送 | A 每份 | A p | B 成交/发送 | B 每份 | C 成交/发送 | C 每份 | B+C p | 通过 |",
              "|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|:-:|"]
        for r in sorted(cand, key=lambda r: r["pBC"]):
            fam, desc = info[r["rule"]]
            L.append(f"| {r['rule']} | {fam} | {desc} | {r['nA']:,}/{r['sA']:,} | {100 * r['evA']:+.2f}¢ | {r['pA']:.4f} | "
                     f"{r['nB']:,}/{r['sB']:,} | {100 * r['evB']:+.2f}¢ | {r['nC']:,}/{r['sC']:,} | "
                     f"{100 * r['evC']:+.2f}¢ | {r['pBC']:.4f} | {'✓' if r['pass'] else ''} |")
    L += ["", "## 全部 100 条", "", "| # | 类别 | 规则 | A 成交/发送 | A 均价 | A 每份 | A p | B 成交/发送 | B 每份 | C 成交/发送 | C 每份 |",
          "|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    fmt = lambda v: "–" if not np.isfinite(v) else f"{100 * v:+.2f}¢"
    for r in rows:
        fam, desc = info[r["rule"]]
        px = "–" if not np.isfinite(r["pxA"]) else f"{r['pxA']:.3f}"
        L.append(f"| {r['rule']} | {fam} | {desc} | {r['nA']:,}/{r['sA']:,} | {px} | {fmt(r['evA'])} | {r['pA']:.4f} | "
                 f"{r['nB']:,}/{r['sB']:,} | {fmt(r['evB'])} | {r['nC']:,}/{r['sC']:,} | {fmt(r['evC'])} |")
    return L


def run(workdir, out, days=None, reps=5000, dataset=None):
    import pandas as pd
    import cross
    if dataset:
        cross.set_dataset(dataset)
    if days is not None:
        raise ValueError(f"zoo100 requires every day in {DATE_START}..{DATE_END}; --days is not supported")
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    rules = make_rules()
    manifest_raw = cross.fetch("MANIFEST.txt")
    manifest_sha256 = hashlib.sha256(manifest_raw).hexdigest()
    if manifest_sha256 != ZOO_MANIFEST_SHA256:
        raise RuntimeError(
            f"zoo100 manifest changed: {manifest_sha256}; expected {ZOO_MANIFEST_SHA256}"
        )
    listed = cross.archives(manifest_raw.decode())
    required = set(REQUIRED_DAYS)
    by_day, duplicates = {}, []
    for archive in listed:
        day = archive[0][15:25]
        if day not in required:
            continue
        if day in by_day:
            duplicates.append(day)
        else:
            by_day[day] = archive
    missing = sorted(required - set(by_day))
    if missing or duplicates:
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if duplicates:
            details.append("duplicate " + ", ".join(sorted(set(duplicates))))
        raise RuntimeError(f"zoo100 input coverage failed closed ({_coverage_summary(by_day)}): {'; '.join(details)}")
    arcs = [by_day[day] for day in REQUIRED_DAYS]
    parts = []
    processed, failures = set(), []
    for name, _ in arcs:
        day = name[15:25]
        local = workdir / name
        try:
            local = Path(cross.fetch(name, local))
            expected_bytes = int(by_day[day][1])
            if expected_bytes and local.stat().st_size != expected_bytes:
                raise ValueError(
                    f"archive size mismatch for {name}: {local.stat().st_size} != {expected_bytes}"
                )
            feat, mk, rs = cross.read_day(local)
            binance = cross.read_binance(local)
            prints = cross.read_poly_trades(local)
            S = build_state(feat, cross.market_table(mk, rs), binance, prints)
            t = evaluate(S, rules)
            t["day"] = day
            parts.append(t)
            processed.add(day)
            sends = int(t["sent"].sum()) if len(t) and "sent" in t else len(t)
            print(f"{name}: {len(S):,} market-seconds, {len(t):,} signals, {sends:,} sends over "
                  f"{t['rule'].nunique() if len(t) else 0} rules",
                  flush=True)
        except Exception as error:
            import traceback
            failures.append((day, repr(error)))
            print(f"{name}: failed\n{traceback.format_exc()}", flush=True)
        finally:
            if local.exists():
                local.unlink()
    if failures:
        detail = "; ".join(f"{day}: {error}" for day, error in failures)
        raise RuntimeError(f"zoo100 input processing failed closed ({_coverage_summary(processed)}): {detail}")
    t = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if len(t):  # a market is its end time; won and the fee follow from pnl and price
        t[["rule", "day", "end", "tau", "up", "limit", "sent", "no_send_reason", "observed_ask",
           "filled", "no_fill_reason", "price", "size", "fee_rate", "fee", "won", "pnl"]].round(6).to_csv(
            Path(out).with_suffix(".csv.gz"), index=False)
    L = [f"# 100 条规则（{cross.DS}，5m 市场，每秒看一次，500ms 固定限价 FAK）", "",
         f"数据覆盖：{_coverage_summary(processed)}。", "",
         "每个 5m 市场从剩 295 秒到剩 10 秒每秒看一次记录机已经收到的状态：两边盘口（最优价和数量）、Up 中间价 2/10/30 秒前的值、"
         "币安 1/5/30/60 秒涨跌（以 10 分钟的每秒 σ 为单位）、近 60 秒波动相对平时、Polymarket 近 10/30 秒的净主动买入、"
         "买一卖一量失衡、TWAP 模型价（币安价格减去本市场相对参考价的基差，binary.prob_up）、同时结束的 15m 市场隐含的 5m 价格"
         "（两者按同一个 TWAP 结算）、时段、上一局结果。每条规则锁定每个市场第一次满足的那一秒，以决策时卖一作为不可放宽的 "
         "FAK 限价。500ms 后只有盘口仍健康、卖一不高于该限价且至少有 5 份时才成交；首单被 kill 后不以后见方式换成后续信号。"
         "成交后按模拟成交时有效的 taker 费率付费，持有到结算。", "",
         "只在 A 段（5/25–7/15）比较：A 段至少 50 笔、赚钱且精确 p < 0.05 的算候选；候选在 B 段（7/16–8/16）和 C 段（8/17–8/29）"
         "都赚钱，并且 B+C 合起来 p < 0.05 /（候选数）才算通过。通过也只是有资格开一个事先写死规则的实盘检验。", ""]
    L += report(t, rules, reps) if len(t) else ["没有成交。"]
    Path(out).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))
