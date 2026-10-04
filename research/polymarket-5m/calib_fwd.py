"""CALIB.md "前向检验规则" (forward): the calibration cell that passed the independent half year - 'above'
markets, 30-120 minutes before the end, the side a driftless model puts at 85-95 % - on the
polymarket-ladder recordings (paper research on market data only: never places orders, uses no key).

    python calib_fwd.py DIR [DIR ...] [--out real/calib-forward.md] [--cache DIR] [--expect FILE]

CALIB.md says the model and the data hygiene are NEARCERT.md's forward test's, so everything that is the
same is nearcert_fwd.py's, imported and not edited: the recordings (DIR as there), the receipt-time-only
Binance model (S, sigma of local receipt seconds, tau), the settlement rule read from each market's Gamma
description (open12 / close12, end_mismatch, rule_other, gamma_na), the (*) choice of recording per
(market, checkpoint), the entry at the best ask 0.5 s after the checkpoint with its freshness rules
(nearcert_fwd.Rec.entry), the recorded fee rate, the official results from Gamma, the ledger, the
market-clustered statistics, the UTC-day-cluster lower bound and the exact fair-price test. What differs
(CALIB.md, written 2026-10-04 16:50 / 16:55 UTC before any forward data):

- Markets: 'above' only, ending strictly after 2026-10-05 00:00 UTC (--since).
- Checkpoints: t = end - 60 and end - 30 minutes.
- Side: the token whose model probability is in [0.85, 0.95). nearcert_fwd.checkpoint applies NEARCERT's
  band [0.95, 0.99) and prices only inside it, but its row already holds the favourite's probability,
  side and token: the band is applied again here and the entry priced with the same nearcert_fwd.Rec.entry
  (the ask of the token's last book row stamped <= t + 0.5 s, 0.02-0.98, >= 5 shares, fresh feed, no
  disconnect; taker fee = the recorded rate x p (1 - p), 0.07 only when missing; one share per buy).
- One buy per market and side: the earliest checkpoint at which the side is bought; a later buy of the
  same side of the same market is counted as 'repeat' and not bought. (*) "先到的时点" is read as the
  first checkpoint that buys: a 60-minute signal that cannot be bought (no book, stale feed, ask outside
  0.02-0.98 or under 5 shares) leaves the 30-minute checkpoint free to buy. Applied after the recordings
  are combined (one row per market and checkpoint), so a recording can never buy a side twice.
- Verdict (CALIB.md, sized by power): ordered by first buy time, judged once on the first 40 markets with
  a buy - no minimum number of UTC days - on every buy of those markets. Pass only when the mean P&L per
  share > 0, the one-sided 99 % UTC-day-cluster lower bound > 0 and the one-observation-per-market exact
  fair-price p < 0.01. (*) Held back exactly when nearcert_fwd holds its own (a selected market not
  resolved, no recording past their ends, an undetermined checkpoint up to the 40th market's first buy or
  of a selected market, a fetch error, a recording in --expect not read, no --expect list). A bound that
  cannot be computed (fewer than 2 UTC days) is not > 0. Pinned in <out>.verdict.md with the judged buys
  in <out>.trades.csv, afterwards only read back. Runs with other parameters (--since, --checks,
  --sigma-window: development) never write a ledger or a verdict.

Resting bid (CALIB.md "描述（不判定）"; never in the verdict):
- At each checkpoint whose side is in the band, a buy order at that token's best bid as of t + 0.5 s: the
  same book row and freshness rules as the taker entry, the bid in 0.02-0.98 (else bid_band).
- Filled only if a print on that token with taker side SELL, price STRICTLY below the bid and >= 5 shares
  is stamped in (t + 0.5 s, t + 0.5 s + 30 min], capped at the market's end; filled at the bid, no fee,
  one share, held to the official result. One fill per market and side (the earliest; a later live bid of
  a side already filled is bid_repeat).
- (*) A window the recording's book feed does not reach to its end, without a fill, is bid_open (counted,
  not valued). Prints lost during a CLOB disconnect are not seen (no fill).

Ledger: <out>.ledger/ (real/calib-forward.ledger/) in nearcert_fwd's format with the resting-bid columns
added; frozen recordings, expired_pending and --expect behave exactly as in nearcert_fwd.

Network: Gamma only (nearcert_fwd.Net: at most 4 requests a second, proxy, TLS verified); resolved Gamma
records cached under --cache.
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

import numpy as np
import pandas as pd

import ladder as ld
import ladder_check as lc
import nearcert_fwd as nf

SINCE = nf.SINCE
CHECKS = (60, 30)
BAND = (0.85, 0.95)
TYPE = "above"
N_VERDICT = 40
ALPHA = nf.ALPHA
BID_WINDOW = 1800.0             # a resting bid lives 30 minutes
BID_BAND = nf.ASK_BAND          # the bid must be in 0.02-0.98
MIN_PRINT = nf.MIN_SIZE         # a filling SELL print has >= 5 shares
CACHE = Path("/tmp/claude-0/-home-user-whole-web-station-clone/d12980d9-5808-53a6-9da0-aeb3bb8bdfb4/scratchpad/calib/fwd")
NAN = nf.NAN

STATUS = {**nf.STATUS, "below": f"模型：较可能的一边 < {BAND[0]:.2f}", "above": f"模型：较可能的一边 ≥ {BAND[1]:.2f}",
          "repeat": "同一市场同一边已在更早的时点买过（不再买）"}
MODELLED = ("below", "above") + nf.SIGNAL_STATUS       # nearcert_fwd statuses whose row holds p_fav, side, token
IN_BAND = nf.SIGNAL_STATUS + ("repeat",)
BID_STATUS = {
    "bid_no_book": "没有这个代币的盘口",
    "bid_stale": "盘口数据不新鲜（静默 > 5 秒或断线）",
    "bid_band": "没有买一或买一不在 0.02–0.98",
    "bid_unfilled": "30 分钟内没有低于买一、≥ 5 份的卖出成交",
    "bid_open": "录制没覆盖完 30 分钟，也还没成交（不计）",
    "bid_filled": "成交",
    "bid_repeat": "同一市场同一边已在更早的时点成交（不再挂）",
}
LIVE = ("bid_filled", "bid_unfilled", "bid_open")
ROW_COLS = nf.ROW_COLS + ["bid", "bid_size", "bid_status", "fill_t", "fill_px", "fill_size"]
STR_COLS = ("bid_status",)


# ===================================================================== one recording

class Rec(nf.Rec):
    """nearcert_fwd.Rec plus each token's bid side (aligned with its book rows) and its taker-SELL prints."""

    def __init__(self, name, books, markets, spot, closes=None, prints=None):
        super().__init__(name, books, markets, spot, closes)
        self.bids = {}
        if len(books):
            # the same grouping as nearcert_fwd.Rec, so row i of a token is the same row there
            for tok, g in books.groupby("market_id", sort=False):
                self.bids[str(tok)] = (pd.to_numeric(g["bid"], errors="coerce").to_numpy(float),
                                       pd.to_numeric(g["bid_size"], errors="coerce").to_numpy(float))
        self.book_end = float(pd.to_numeric(books["ts"], errors="coerce").max()) if len(books) else NAN
        self.sells = {}
        if prints is not None and len(prints):
            p = prints.assign(ts=pd.to_numeric(prints["ts"], errors="coerce"),
                              price=pd.to_numeric(prints["price"], errors="coerce"),
                              size=pd.to_numeric(prints["size"], errors="coerce"))
            p = p[p["side"].astype(str).str.upper().eq("SELL")].dropna(subset=["ts", "price", "size"])
            for tok, g in p.sort_values("ts", kind="stable").groupby("asset_id", sort=False):
                self.sells[str(tok)] = (g["ts"].to_numpy(float), g["price"].to_numpy(float), g["size"].to_numpy(float))

    @classmethod
    def load(cls, d):
        books, markets, spot, prints, closes = ld.load(d)
        return cls(lc._name(d), books, markets, spot, closes, prints)

    def bid_entry(self, tok, te):
        """(status, bid, bid size) of a buy order resting at the best bid as of te: the token's last book
        row stamped <= te (no later row is read), fresh by nearcert_fwd.Rec.entry's rules."""
        st = self.entry(tok, te)[0]
        if st in ("no_book", "stale"):
            return "bid_" + st, NAN, NAN
        ts = self.toks[tok][0]
        i = int(np.searchsorted(ts, te, "right")) - 1
        bid, size = self.bids[tok]
        b, q = float(bid[i]), float(size[i])
        if not (np.isfinite(b) and BID_BAND[0] - 1e-9 <= b <= BID_BAND[1] + 1e-9):
            return "bid_band", b, q
        return "bid_live", b, q

    def bid_fill(self, tok, te, bid, until):
        """(status, time, price, size): the first taker-SELL print on tok strictly below `bid` with >= 5
        shares stamped in (te, until] -> bid_filled; else bid_unfilled when the recording's book feed
        reaches `until`, bid_open when it does not."""
        if tok in self.sells:
            ts, px, sz = self.sells[tok]
            a, b = np.searchsorted(ts, te, "right"), np.searchsorted(ts, until, "right")
            hit = np.flatnonzero((px[a:b] < bid - 1e-9) & (sz[a:b] >= MIN_PRINT - 1e-9))
            if len(hit):
                k = a + int(hit[0])
                return "bid_filled", float(ts[k]), float(px[k]), float(sz[k])
        return ("bid_unfilled" if self.book_end >= until else "bid_open"), NAN, NAN, NAN


# ===================================================================== checkpoints

def checkpoint(rec, m, c, t, sigma_s=nf.SIGMA_S, gamma=None):
    """One (market, checkpoint) row: nearcert_fwd.checkpoint's (model, settlement rule, data hygiene), with
    CALIB.md's band [0.85, 0.95) applied to its favourite and the taker entry and the resting bid priced
    at t + 0.5 s."""
    row = nf.checkpoint(rec, m, c, t, sigma_s, gamma=gamma)
    row.update(bid=NAN, bid_size=NAN, bid_status="", fill_t=NAN, fill_px=NAN, fill_size=NAN)
    if row["status"] not in MODELLED:   # no model probability: nearcert_fwd's status stands
        return row
    pf = row["p_fav"]
    row.update(ask=NAN, size=NAN, age=NAN)   # nearcert_fwd priced only its own band
    if pf < BAND[0] - 1e-12:
        row["status"] = "below"
        return row
    if pf >= BAND[1] - 1e-12:
        row["status"] = "above"
        return row
    te = float(t) + nf.ENTRY_LAG
    row["status"], row["ask"], row["size"], row["age"] = rec.entry(row["token"], te)
    st, row["bid"], row["bid_size"] = rec.bid_entry(row["token"], te)
    if st == "bid_live":
        st, row["fill_t"], row["fill_px"], row["fill_size"] = rec.bid_fill(
            row["token"], te, row["bid"], min(te + BID_WINDOW, float(m.end_ts)))
    row["bid_status"] = st
    return row


def complete(cp):
    """Every ledger column present (rows nearcert_fwd.combine adds for unrecorded checkpoints lack them)."""
    cp = cp.copy()
    for c in ROW_COLS:
        if c not in cp:
            cp[c] = "" if c in nf.STR_COLS + STR_COLS else NAN
    for c in nf.STR_COLS + STR_COLS:
        if c in cp:
            cp[c] = cp[c].fillna("")
    return cp


def one_per_side(cp):
    """CALIB.md's one buy per market and side, on the combined rows (one per market and checkpoint): a
    'trade' of a (market, side) bought at an earlier checkpoint becomes 'repeat'; a live resting bid of a
    side filled at an earlier checkpoint becomes 'bid_repeat'."""
    if not len(cp):
        return cp
    cp = cp.sort_values(["t", "cid"], kind="stable").reset_index(drop=True)
    key = cp["cid"].astype(str) + "|" + cp["side"].astype(str)
    trade = cp["status"].eq("trade")
    cp.loc[trade & (trade.astype(int).groupby(key).cumsum() > 1), "status"] = "repeat"
    filled = cp["bid_status"].eq("bid_filled")
    before = filled.astype(int).groupby(key).cumsum() - filled.astype(int)
    cp.loc[cp["bid_status"].isin(LIVE) & (before > 0), "bid_status"] = "bid_repeat"
    return cp


def settle_bids(cp, gamma_fetch, now):
    """Resting-bid fills valued as nearcert_fwd.settle values a buy: at the bid, without fee."""
    f = cp[cp["bid_status"].eq("bid_filled")] if "bid_status" in cp else cp.iloc[0:0]
    f = f.assign(status="trade", ask=f["bid"] if "bid" in f else NAN, fee_rate=0.0)
    return nf.settle(f, gamma_fetch, now)


# ===================================================================== verdict

def judge(out, entries, cp, data_end, n=N_VERDICT, design=True, blockers=()):
    """The once-only verdict, pinned in <out>.verdict.md (judged buys in <out>.trades.csv), then only read
    back: the first n markets with a buy (by first buy time), every buy of those markets."""
    pinned = Path(out).with_suffix(".verdict.md")
    if pinned.exists():
        return pinned.read_text(encoding="utf-8").strip() + "（已判定，不再重算）"
    if not design:
        return "开发运行（参数不同于 CALIB.md 的前向检验规则），不判定。"
    e = entries[entries["kind"] == TYPE] if len(entries) else entries
    order = e.sort_values(["t", "cid"], kind="stable").drop_duplicates("cid") if len(e) else e
    if len(order) < n:
        return f"目前 {len(order):,} 个市场有买入，不到 {n:,} 个，不判定。"
    first = order.iloc[:n]
    cut = float(first["t"].max())
    chosen = set(first["cid"])
    sel = e[e["cid"].isin(chosen)].sort_values(["t", "cid"], kind="stable")
    wait = []
    k = sel.loc[sel["official"].isna(), "cid"].nunique()
    if k:
        wait.append(f"{k} 个还没结算")
    if not (np.isfinite(data_end) and data_end >= first["end"].max()):
        wait.append("录制还没覆盖到它们全部结束")
    # an undetermined checkpoint could still add an earlier market or move a chosen market's buy
    u = int((cp["status"].isin(nf.UNDETERMINED) & ((cp["t"] <= cut) | cp["cid"].isin(chosen))).sum()) if len(cp) else 0
    if u:
        wait.append(f"{u} 个时点还待定")
    wait += list(blockers)
    if wait:
        return f"已有 {len(order):,} 个市场有买入；前 {n:,} 个里" + "，".join(wait) + "，暂不判定。"
    mu, se, t, G = nf.clustered(sel["pnl"], sel["cid"])
    lower = nf.daily_lower_99(sel)
    exact_p = nf.market_exact_pvalue(sel)
    ok = mu > 0 and np.isfinite(lower) and lower > 0 and exact_p < ALPHA
    days = len({nf._utc(x, "%Y-%m-%d") for x in sel["t"]})
    runs = sorted({str(x) for x in sel["run"]})
    v = (f"前向检验（CALIB.md 前向检验规则：高于 · 结束前 60/30 分钟 · 模型 {BAND[0]:.2f}–{BAND[1]:.2f} 的一边；"
         f"前 {n:,} 个有买入市场，{len(sel):,} 笔、每笔 1 份，{days} 个 UTC 日）："
         f"每份 {100 * mu:+.2f}¢（按市场聚类标准误 {100 * se:.2f}¢，t = {t:.2f}，{G} 个市场；"
         f"单侧 99% 日聚类下界 {100 * lower:+.2f}¢；市场级精确公平价格 p = {exact_p:.4g}）"
         f"→ {'通过' if ok else '没通过'}"
         f"（录制段 {len(runs)} 个：{', '.join(runs)}；第 {n:,} 个市场的首次买入时点 {nf._utc(cut, '%Y-%m-%d %H:%M')} UTC）")
    pinned.parent.mkdir(parents=True, exist_ok=True)
    pinned.write_text(v + "\n", encoding="utf-8")
    sel.to_csv(Path(out).with_suffix(".trades.csv"), index=False)
    return v


# ===================================================================== report

def _head(what):
    return [f"| 分组 | 有{what}市场 | 已结算 | 待结算 | 份数 | 花费 $ | 已结算花费 $ | 已结算盈亏 $ | 每份 ¢ | 聚类 SE ¢ | 平均买价 | 胜率 |",
            "|---|" + "---:|" * 11]


def _units(name, e):
    """A table row in units, one share per buy: markets (settled / pending), shares, cost (price + fee) of
    all buys and of the settled ones, the settled P&L and its per-share mean (SE clustered by market)."""
    cell = lambda x, f: format(x, f) if np.isfinite(x) else "–"
    if not len(e):
        return f"| {name} | 0 | 0 | 0 | 0 | 0.00 | 0.00 | – | – | – | – | – |"
    r = e[e["official"].notna()]
    mu, se, _, _ = nf.clustered(r["pnl"], r["cid"])
    cost = e["ask"] + e["fee"]
    return (f"| {name} | {e['cid'].nunique():,} | {r['cid'].nunique():,} | {e.loc[e['official'].isna(), 'cid'].nunique():,} | "
            f"{len(e):,} | {cost.sum():.2f} | {cost[e['official'].notna()].sum():.2f} | "
            f"{cell(float(r['pnl'].sum()) if len(r) else NAN, '+.2f')} | {cell(100 * mu, '+.2f')} | {cell(100 * se, '.2f')} | "
            f"{cell(float(r['ask'].mean()) if len(r) else NAN, '.3f')} | {cell(float(r['won'].mean()) if len(r) else NAN, '.1%')} |")


def _table(e, checks, what="买入"):
    L = _head(what) + [_units("全部", e)]
    for c in checks:
        L.append(_units(f"结束前 {c} 分钟", e[e["check"] == c] if len(e) else e))
    return L


def report(cp, entries, fills, verdict, recs_meta, since, notes=(), dev=None, checks=CHECKS, info=None):
    info = info or {}
    when = time.strftime("%Y-%m-%d %H:%M", time.gmtime())
    spans = [(a, b) for _, a, b in recs_meta if np.isfinite(a)]
    hours = sum(b - a for a, b in spans) / 3600
    span = f"{nf._utc(min(a for a, _ in spans))} → {nf._utc(max(b for _, b in spans))} UTC" if spans else "–"
    nm = cp["cid"].nunique() if len(cp) else 0
    L = [f"# 校准格子：前向检验（CALIB.md：高于 · 结束前 60/30 分钟 · 模型 {BAND[0]:.2f}–{BAND[1]:.2f}，{when} UTC）", ""]
    if dev:
        L += [f"**开发运行，不是检验**：{dev}。", ""]
    led = info.get("ledger")
    L += [f"只用市场数据，不下单。录制段 {len(recs_meta)} 个，共 {hours:.1f} 小时（{span}）"
          + (f"：本次读入 {led['loaded']} 段，账本里已冻结 {led['frozen']} 段"
             + (f"，录制过期时仍有待定时点而剔除的 {led['expired']} 段" if led["expired"] else "") if led else "")
          + f"；{nf._utc(nf.ts_of(since), '%Y-%m-%d %H:%M')} UTC 之后结束的“高于”市场 {nm:,} 个，每个看 {len(checks)} 个时点"
          f"（结束前 {'/'.join(map(str, checks))} 分钟）。", "",
          "## 判定", "", verdict, ""]
    if info.get("blockers"):
        L += ["本次不会判定的原因：" + "；".join(info["blockers"]) + "。", ""]
    L += [f"规则：按首次买入时点取前 {N_VERDICT} 个有买入的市场（不要求天数），对它们的全部买入判定一次："
          f"每份 > 0、单侧 99% 日聚类下界 > 0、市场级精确公平价格 p < {ALPHA:g} 才通过。挂单版只描述，不进判定。", ""]
    sig = cp[cp["status"].isin(IN_BAND)] if len(cp) else cp
    rep = int((sig["status"] == "repeat").sum()) if len(sig) else 0
    tried = len(sig) - rep
    rate = f"{len(entries) / tried:.0%}" if tried else "–"
    depth = float(entries["size"].sum()) if len(entries) else 0.0
    L += [f"## 吃单买入（判定用；时点后 {nf.ENTRY_LAG:g} 秒的卖一，每笔 1 份，付录到的吃单费 rate·p·(1 − p)）", ""] \
        + _table(entries, checks) \
        + ["", f"模型落在 [{BAND[0]:.2f}, {BAND[1]:.2f}) 的时点 {len(sig):,} 个：买入 {len(entries):,} 笔（{rate}），"
               f"同一市场同一边已买过 {rep:,} 个。买入时卖一显示合计 {depth:,.0f} 份（容量上限，不含队列竞争）。"
           + nf._projection(entries, cp, N_VERDICT), ""]
    bs = sig["bid_status"].value_counts() if len(sig) else pd.Series(dtype=int)
    L += [f"## 挂单买入（只描述，不判定；时点后 {nf.ENTRY_LAG:g} 秒的买一挂 1 份，{BID_WINDOW / 60:g} 分钟内有低于买一、"
          f"≥ {MIN_PRINT:g} 份的卖出成交才算成交，无手续费）", ""] + _table(fills, checks, "成交") \
        + ["", "挂单状态：" + ("，".join(f"{name} {int(bs.get(s, 0)):,}" for s, name in BID_STATUS.items() if bs.get(s, 0))
                             or "还没有") + "。", ""]
    L += ["## 时点计数", "", "| 时点状态 | 个数 |", "|---|---:|"]
    for s, name in STATUS.items():
        k = int((cp["status"] == s).sum()) if len(cp) else 0
        if k:
            L.append(f"| {name} | {k:,} |")
    L += ["", "## 做法（细节见 calib_fwd.py 开头）", "",
          "- 模型、结算规则（按每个市场的 Gamma 描述）、录制选择、盘口新鲜度和官方结果都和 NEARCERT.md 的前向检验相同"
          "（直接调用 nearcert_fwd.py）：只用时点前记录机已收到的币安成交，σ = 过去 1 小时 1 秒对数收益标准差，按剩余时间放大。",
          f"- 吃单：模型概率在 [{BAND[0]:.2f}, {BAND[1]:.2f}) 的一边，时点后 {nf.ENTRY_LAG:g} 秒的卖一（只看那一刻及之前的盘口，"
          f"{nf.ASK_BAND[0]:.2f}–{nf.ASK_BAND[1]:.2f}、≥ {nf.MIN_SIZE:g} 份），同一市场同一边只买一次（先买到的时点），持有到官方结算。",
          "- 挂单：同一时点的买一，之后 30 分钟内（不过结束时间）有严格低于买一、≥ 5 份的卖出成交才算成交；同一市场同一边只算一次。",
          "- 账本：每段录制的时点结果存在 `calib-forward.ledger/`（随报告提交），全部定下来的录制冻结、不再重算。"]
    if notes:
        L += ["", "备注：", ""] + [f"- {x}" for x in notes]
    return "\n".join(L) + "\n"


# ===================================================================== ledger and run

class Ledger(nf.Ledger):
    """nearcert_fwd.Ledger with this module's row columns (the resting-bid fields added)."""

    def rows(self, run):
        rr = super().rows(run)
        for r in rr:
            for c in STR_COLS:
                r[c] = r[c] if isinstance(r.get(c), str) else ""
        return rr

    def put(self, run, first, last, rows, frozen):
        nf._gz_csv(pd.DataFrame(rows, columns=ROW_COLS), self.dir / f"{run}.csv.gz")
        self.meta[run] = {"first": float(first), "last": float(last), "rows": len(rows), "frozen": bool(frozen)}


def scope_markets(dirs, since, known=None):
    """nearcert_fwd.scope_markets restricted to 'above' markets."""
    mk = nf.scope_markets(dirs, since, known)
    return mk[mk["type"] == TYPE].reset_index(drop=True)


def run(roots, out, cache=CACHE, since=SINCE, now=None, checks=CHECKS, sigma_s=nf.SIGMA_S, net=None, n=N_VERDICT,
        log=print, expect=None):
    """Everything (nearcert_fwd.run's steps with this module's rows); writes `out`, the ledger (design runs)
    and the pinned verdict when due. Returns the report text, or None when there is no recording to read."""
    now = time.time() if now is None else now
    net = nf.Net() if net is None else net
    notes, errors, blockers = [], [], []
    dirs = lc.built_dirs(roots)
    if not dirs:
        log(f"no built recording under {', '.join(map(str, roots))}")
        return None
    design = nf.ts_of(since) == nf.ts_of(SINCE) and tuple(checks) == CHECKS and sigma_s == nf.SIGMA_S
    dev = None if design else f"--since {since}，--checks {','.join(map(str, checks))}，--sigma-window {sigma_s}"
    led = Ledger(nf.ledger_dir(out)) if design else None
    scope = scope_markets(dirs, since, led.markets if led is not None else None)
    cids = set(scope["condition_id"]) if len(scope) else set()
    gamma = nf.Gamma(net, cache, notes, errors)
    ginfo, rows, meta, loaded = {}, [], {}, set()
    for d in dirs:
        name = lc._name(d)
        if led is not None and led.meta.get(name, {}).get("frozen"):
            continue  # every row final: read back from the ledger, never recomputed
        try:
            rec = Rec.load(d)
        except Exception as e:
            notes.append(f"{name}：读不了（{type(e).__name__}: {e}）")
            continue
        loaded.add(name)
        pts = nf.rec_points(rec, cids, checks)
        need = sorted({m.condition_id for m, _, _ in pts} - set(ginfo))
        if need:
            ginfo.update({c: r for c, r in gamma.fetch(need, open_too=True).items() if r is not None})
        rr = [checkpoint(rec, m, c, t, sigma_s, ginfo) for m, c, t in pts]
        if led is not None:
            led.put(name, rec.first, rec.last, rr, frozen=not any(r["status"] in nf.UNDETERMINED for r in rr))
        else:
            rows += rr
            meta[name] = (rec.first, rec.last)
        log(f"{name}: {len(rec.st):,} Binance trades, {len(rec.all_ts):,} book rows, {len(rr):,} checkpoint rows")
        del rec
    expired = 0
    if led is not None:  # nearcert_fwd.run's ledger bookkeeping, unchanged
        exp = None if expect is None else {str(x) for x in expect}
        if exp is not None and loaded - exp:  # a recording read now is missing from the list: list unreliable
            notes.append(f"--expect 清单缺了这次读到的录制（{', '.join(sorted(loaded - exp)[:5])}），本次当作没有清单")
            exp = None
        for name, m in list(led.meta.items()):
            if name in loaded or m["frozen"]:
                continue
            if exp is not None and name not in exp:  # its artifact has expired: what is undetermined stays out
                rr = [{**r, "status": "expired_pending"} if r["status"] in nf.UNDETERMINED else r for r in led.rows(name)]
                led.put(name, m["first"], m["last"], rr, frozen=True)
                expired += 1
                notes.append(f"{name}：录制已过期，{sum(r['status'] == 'expired_pending' for r in rr)} 个仍待定的时点剔除")
            else:
                blockers.append(f"录制 {name} 还有待定时点，这次没读到")
        if exp is None:
            blockers.append("没有可下载录制的清单（--expect）")
        else:
            miss = sorted(exp - loaded - set(led.meta))
            if miss:
                blockers.append(f"{len(miss)} 段录制的 artifact 还在但这次没读到（{', '.join(miss[:5])}）")
        led.add_markets(scope)
        led.save()
        for name, m in led.meta.items():
            rows += led.rows(name)
            meta[name] = (m["first"], m["last"])
        log(f"ledger {led.dir}: {len(led.meta)} recordings ({sum(m['frozen'] for m in led.meta.values())} frozen), "
            f"{len(rows):,} checkpoint rows")
    if not meta:
        log("no recording could be read")
        return None
    data_end = max((b for a, b in meta.values() if np.isfinite(b)), default=NAN)
    cp = one_per_side(complete(nf.combine(rows, scope, checks, data_end)))
    entries = nf.settle(cp, lambda c: gamma.fetch(c), now)
    fills = settle_bids(cp, lambda c: gamma.fetch(c), now)
    if errors:
        blockers.append(f"本次抓取出错 {len(errors)} 次")
    verdict = judge(out, entries, cp, data_end, n=n, design=design, blockers=blockers)
    info = {"blockers": blockers if design else [],
            "ledger": {"loaded": len(loaded), "expired": expired,
                       "frozen": sum(m["frozen"] for m in led.meta.values())} if led is not None else None}
    text = report(cp, entries, fills, verdict, [(k, a, b) for k, (a, b) in meta.items()], since, notes + errors, dev,
                  checks, info)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(text, encoding="utf-8")
    return text


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dirs", nargs="+")
    ap.add_argument("--out", default="real/calib-forward.md")
    ap.add_argument("--cache", default=str(CACHE), help="Gamma 结果的缓存目录")
    ap.add_argument("--expect", default=None,
                    help="文件：artifact 还能下载的录制 run id，一行一个（CI 生成；没有就不判定）")
    ap.add_argument("--since", default=SINCE, help="只用此后结束的市场（开发用；改了就不判定）")
    ap.add_argument("--checks", default=",".join(map(str, CHECKS)), help="结束前几分钟（开发用；改了就不判定）")
    ap.add_argument("--sigma-window", type=int, default=nf.SIGMA_S, help="σ 的回看秒数（开发用；改了就不判定）")
    ap.add_argument("--now", type=float, default=None, help="当作现在的 unix 时间（测试用）")
    a = ap.parse_args(argv)
    checks = tuple(int(x) for x in a.checks.split(",") if x.strip())
    expect = None
    if a.expect:
        expect = [x.strip() for x in Path(a.expect).read_text().split() if x.strip()]
    text = run(a.dirs, a.out, a.cache, a.since, a.now, checks, a.sigma_window, expect=expect)
    if text is None:
        raise SystemExit(2)
    print(text)


if __name__ == "__main__":
    main()
