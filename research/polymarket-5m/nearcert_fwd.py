"""NEARCERT.md test 2 (forward): is the side a driftless model puts at 95-99 % underpriced? On the
polymarket-ladder recordings (paper research on market data only: never places orders, uses no key).

    python nearcert_fwd.py DIR [DIR ...] [--out real/nearcert-forward.md] [--cache DIR] [--expect FILE]

DIR is a built ladder directory (ladder.py build: books.csv.gz, markets.json, binance_trades.jsonl.gz,
clob_closes.csv.gz) or any directory above some (CI: <rec>/<run id>/built). Each recording is read on
its own (its own Binance trades, sigma and books). The rules are NEARCERT.md's, fixed 2026-10-04
before running; the points it leaves open are marked (*) and were settled conservatively here.

Markets and times
- above, range, updown_day and hit markets whose window is a day, a week or a month, that END strictly
  after 2026-10-05 00:00 UTC (--since). The hit period is read from the market question the way
  resolved.hit_window reads an event title ("on October 6", "September 28-October 4", "in October");
  any other hit window (the yearly "What price will Bitcoin hit in 2026?", "before ...") is kind
  hit_other, counted as rule_other and never judged (test 1 excluded it too). updown_4h (Chainlink 60 s
  TWAP) goes through the same steps into its own descriptive table, never into the verdict.
- Checkpoints t = end - 60, 30, 10, 5, 1 min, end = the recorded Gamma endDate (whole seconds).
- (*) A (market, checkpoint) seen by several recordings is taken from the one with >= 1 h of history
  and the earliest first Binance trade, whatever happens next in either.

Model at t (resolved.p_yes, as resolved.py): S = the last Binance trade received before t; sigma =
std (ddof 1) of the 1 s log returns of local receipt seconds t-3600 .. t-1 (last received price of
each second, carried forward through seconds without a trade); sd = sigma sqrt(tau). Exchange-stamped
trades that had not reached the recorder by t are never used.
- (*) History: skipped and counted as short_history unless the recording's Binance trades start at
  least 3601 s before t (the first return needs second t-3601); sigma_na if fewer than 1800 of those
  3600 s have a trade; spot_gap if the last trade before t is more than 5 s old (feed down).

Settlement rule, read from each market's own Gamma description (fetched by condition id; gamma_na,
undetermined and retried, while Gamma gives none):
- above / range / updown_day: which "12:00 ET" 1m candle settles is taken from the wording, not
  assumed. Every mention "closing at 12:00" (the daily up/down wording since 2026-10-02) is close12, the
  candle opening 11:59 ET: tau = noon - t. "12:00 in the ET timezone (noon)" with no "closing at" (above
  and range; resolved.py validated it as open12) is open12, the candle opening at noon: tau = noon + 60
  - t. Anything else (mixed wording, another source, pair or candle; an up/down whose two dates are not
  the previous and the same ET day) is rule_other. (*) A market whose end is not noon ET of its day is
  skipped (end_mismatch; test 1 does the same).
- updown_day reference: the close of the previous ET day's candle under the same convention, from
  Binance's own 1m klines (data.binance.vision daily file; ref_pending while not published). Under
  close12 that is the candle the recorder's ref_price holds.
- hit: window [start, end) = resolved.hit_window from the question and the description (ET midnight of
  the first date; createdAt rounded up to the minute when the description says "from the creation of
  this market"); rule_other when the description does not read as the question's period, end_mismatch
  when the window's end is not the market's end. Reflection principle 2 Phi(-|ln(K/S)| / sd), tau =
  end - t; skipped as decided when the window's high (low) so far reached the level, from (a) the
  official 1m candles from the window start up to the recorder's start_ts, when start_ts (Gamma's event
  startDate: seconds after the window start for daily and weekly events, about an hour for monthly
  ones) is later (data.binance.vision; those candles closed hours before t; kline_pending while the
  day's file is not published), (b) the recording's pre_high / pre_low (Binance 1m klines from start_ts
  to pre_at, ladder.discover) and (c) every recorded trade received from then to t.
  (*) Coverage, so nothing is missed and nothing is seen early: pre_* are used only if pre_at <= t and
  the recording's first trade is at most 30 s after pre_at; a window that began before the recording's
  trades without pre_* (recordings made before ladder.py stored them), or any Binance silence > 30 s in
  the stretch covered by trades, is skipped as hit_unknown unless a touch was seen. When pre_* begin
  before the window (start_ts earlier than the window start, as for a market "from the creation of this
  market"), pre_* that say "reached" are hit_unknown.
- updown_4h (descriptive): reference = the last recorded trade before its start (<= 5 s old), else the
  recorder's ref_price (Binance 1m OHLC mean); tau = end - t.

Side and entry
- The token whose model probability is in [0.95, 0.99); none if the favourite is below 0.95 or at or
  above 0.99.
- Bought at te = t + 0.5 s (exchange time) at the best ask of the token's book row as of te (the last
  row stamped <= te; no later row is read), if the ask is in [0.02, 0.98] with >= 5 shares there;
  taker fee uses that market's recorded fee rate (0.07 fallback) times p (1 - p); one share per entry
  (equal weight).
- (*) "Book state at most 5 s old and feed alive": the recorder writes a row only when a token's top
  of book changes, so a token's own last row may be minutes old and still the exchange's state; what
  must be fresh is the feed: from the time the recorder last had that state confirmed to te the feed
  never went more than 5 s without a book row of any token, the last at most 5 s before te, and no
  CLOB disconnect (recorder clock) in [te - 10 s, te + 1 s]; else stale. Confirmed = the row's own time,
  or its receipt time when it arrived more than 5 s after its exchange stamp (the snapshot sent on
  (re)subscribing carries the time of the book's last change, up to half an hour before the
  connection), raised to the first book row after the latest CLOB disconnect before te (the
  resubscription snapshot: a book that did not change produces no new row, its old row is then
  confirmed). A state received after te is stale. Every row's time in the feed-silence check is that
  same confirmed time. The entries whose own row was <= 5 s old are reported apart.
- Held to the official result: Gamma /markets?closed=true&condition_ids=... once the market has
  ended; a closed market's outcomePrices (umaResolutionStatus "resolved" when given; 50-50 pays 0.5).
  Not yet closed or resolved: pending, counted. Per-share pnl = result - ask - fee; means with
  standard errors clustered by market.

Ledger (the recordings are artifacts kept 30 days): each recording's checkpoint rows are kept in
<out>.ledger/<run>.csv.gz, with runs.csv (first / last Binance trade, rows, frozen) and markets.csv.gz
(the markets in scope), and committed with the report. A recording whose rows are all final is frozen:
read back, never recomputed. One with rows still undetermined (gamma_na, ref_pending, kline_pending)
is recomputed while it can be read; once its artifact has expired (it is not in --expect) those rows
become expired_pending, an explicit, counted exclusion. Development runs use no ledger.

Verdict: ordered by first entry time, stop once at least 300 Binance-settled markets with entries span
at least 7 UTC days, and judge every entry of those markets. Pass only when mean P&L/share > 0, the
one-sided 99% UTC-day-cluster lower bound > 0, and a one-observation-per-market exact fair-price
Poisson-binomial p < 0.01. (*) Judged only when all selected markets are resolved, a known recording
reaches past the last of their ends, no checkpoint up to the stopping market's first entry nor any of
the selected markets' checkpoints is undetermined (whatever its age), this run had no Gamma or kline
fetch error, and every recording in --expect (the workflow's list of ladder-<id> artifacts still
downloadable) was read now or is in the ledger; without --expect it never judges. Written to
<out>.verdict.md (with the judged entries in <out>.trades.csv) and afterwards only read back. Runs with
parameters other than the design's (--since, --checks, --sigma-window: for development) never write a
verdict.

Network: Gamma and data.binance.vision only, at most 4 requests a second, through the environment's
proxy with TLS verified; resolved Gamma records and the kline files are cached under --cache.
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import t as student_t

import ladder as ld
import ladder_check as lc
import resolved as rs

SINCE = "2026-10-05T00:00:00Z"
CHECKS = (60, 30, 10, 5, 1)
BAND = (0.95, 0.99)
ENTRY_LAG = 0.5
MIN_SIZE = 5.0
ASK_BAND = (0.02, 0.98)
FEE = 0.07                  # fallback only; each market's recorded rate takes precedence
SIGMA_S = 3600
SPOT_ALIVE = 5.0          # last Binance trade before t at most this old
FEED_AGE = 5.0            # CLOB feed: no silence longer than this from the token's row to te
SNAP_LAG = 5.0            # a book row received this long after its exchange stamp counts from its receipt
CLOSE_WIN = (10.0, 1.0)   # no CLOB disconnect in [te - 10, te + 1]
HIT_GAP = 30.0            # hit coverage: longest allowed hole (pre_at -> first trade, between trades)
N_VERDICT = 300
MIN_DAYS = 7
ALPHA = 0.01
RATE = 4.0                # requests per second, all hosts
SETTLED_AFTER = 60        # ask Gamma for a result only this long after the end
BATCH = 20                # condition ids per Gamma request
HIT_TYPES = ("hit_up", "hit_down")
NOON_TYPES = ("above", "range", "updown_day")
HIT_KINDS = {"daily": "hit_daily", "weekly": "hit_weekly", "monthly": "hit_monthly"}
OTHER = "hit_other"       # a hit window that is not a day, a week or a month: excluded, counted
BINANCE_KINDS = ("above", "range", "updown_day", "hit_daily", "hit_weekly", "hit_monthly")
CONTROL = "updown_4h"
TYPES = ("above", "range", "updown_day", "hit_up", "hit_down", "updown_4h")
CACHE = Path("/tmp/claude-0/-home-user-whole-web-station-clone/d12980d9-5808-53a6-9da0-aeb3bb8bdfb4/scratchpad/nearcert/fwd")
GAMMA_MARKETS = "https://gamma-api.polymarket.com/markets?"
VISION_1M = "https://data.binance.vision/data/spot/daily/klines/BTCUSDT/1m/BTCUSDT-1m-{d}.zip"
NAN = float("nan")

STATUS = {  # every (market, checkpoint) ends in exactly one; the report counts them in this order
    "not_yet": "时点在最后一段录制之后",
    "not_recorded": "没有录制覆盖这个时点",
    "rule_other": "结算规则或窗口不是设计里的（剔除）",
    "short_history": "录制里时点前的币安成交不足 1 小时（跳过）",
    "spot_gap": "时点前 5 秒内没有币安成交",
    "sigma_na": "过去 1 小时有成交的秒数不足一半",
    "end_mismatch": "结束时间不是结算时刻（中午类：当天 12:00 ET；触及类：窗口结束）",
    "gamma_na": "取不到 Gamma 市场说明（待定）",
    "ref_pending": "前一天 12:00 ET 的 K 线还取不到（待定）",
    "kline_pending": "触及类：窗口开头的币安 1 分钟 K 线还取不到（待定）",
    "expired_pending": "录制过期时仍待定（剔除）",
    "no_ref": "没有参考价",
    "hit_unknown": "触及类：时点前的最高/最低价覆盖不全",
    "decided": "触及类：时点前已触及（结果已定）",
    "below": "模型：较可能的一边 < 0.95",
    "above": "模型：较可能的一边 ≥ 0.99",
    "no_book": "下单时没有这个代币的盘口",
    "stale": "盘口数据不新鲜（静默 > 5 秒或断线）",
    "ask_band": "没有卖一或卖一不在 0.02–0.98",
    "ask_size": "卖一不足 5 份",
    "trade": "买入",
}
UNDETERMINED = ("ref_pending", "gamma_na", "kline_pending")
SIGNAL_STATUS = ("no_book", "stale", "ask_band", "ask_size", "trade")
KIND_NAMES = {"above": "above（高于）", "range": "range（区间）", "updown_day": "updown_day（日涨跌）",
              "hit_daily": "触及·日", "hit_weekly": "触及·周", "hit_monthly": "触及·月",
              CONTROL: "updown_4h（4 小时，Chainlink）", OTHER: "触及·其他窗口（剔除）"}
ROW_COLS = ["run", "cid", "kind", "type", "check", "t", "end", "rec_first", "status", "conv", "S", "sigma", "tau",
            "p_yes", "p_fav", "side", "token", "ask", "size", "age", "fee_rate"]
MARKET_COLS = ["condition_id", "type", "end_ts", "question", "event_slug", "day", "slug"]
STR_COLS = ("run", "cid", "kind", "type", "status", "conv", "side", "token", "condition_id", "question",
            "event_slug", "day", "slug")
_MON3 = {m[:3]: i + 1 for i, m in enumerate(ld.MONTHS)}


def ts_of(s):
    """Unix seconds of an ISO time ('2026-10-05T00:00:00Z') or a number."""
    try:
        return float(s)
    except (TypeError, ValueError):
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()


def _fin(x):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return NAN
    return x


def fee_rate(x):
    x = _fin(x)
    return x if np.isfinite(x) and x >= 0 else FEE


def _iso(ts):
    ts = _fin(ts)
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if np.isfinite(ts) else None


def _text(x):
    return x if isinstance(x, str) else ""


def hit_rule(question, end_ts, g=None):
    """resolved.hit_window of a hit market: (period, from_creation, start, end, note). The period and its
    dates come from the market question (the year from its end); the description and createdAt from its
    Gamma record g (without g the note only says the description was not checked)."""
    g = g or {}
    return rs.hit_window({"title": _text(question)}, {"description": _text(g.get("description")),
                                                     "endDate": _iso(end_ts), "createdAt": g.get("createdAt")})


def kind_of(typ, question=None, end_ts=NAN):
    """The market's kind: its type, or for hit markets hit_daily / hit_weekly / hit_monthly from the window
    the question names, hit_other for any other (yearly, 'before ...', unreadable)."""
    if typ in HIT_TYPES:
        return HIT_KINDS.get(hit_rule(question, end_ts)[0], OTHER)
    return typ


def market_kind(m):
    return kind_of(m.type, getattr(m, "question", None), m.end_ts)


def _norm(desc):
    d = str(desc or "").lower()
    for a, b in (("“", '"'), ("”", '"'), ("‘", "'"), ("’", "'"), ("–", "-"), ("—", "-")):
        d = d.replace(a, b)
    d = re.sub(r"(\d)-(minute|hour|day)", r"\1 \2", d)
    return re.sub(r"\s+", " ", d).strip()


def noon_rule(typ, desc, d):
    """(convention, '') of a noon market's settlement candle read from its Gamma description: 'close12'
    when every '12:00' is 'closing at 12:00', 'open12' for '12:00 in the ET timezone (noon)' with no
    'closing at'; (None, why) for anything else, or a daily up/down whose dates are not d - 1 and d."""
    s = _norm(desc)
    if not s:
        return None, "no description"
    if not ("binance" in s and re.search(r"btc[/_ -]?usdt\b", s) and ("1 minute candle" in s or '"1m"' in s)):
        return None, "source"
    noon, closing = len(re.findall(r"\b12:00\b", s)), len(re.findall(r"\bclosing at 12:00\b", s))
    if noon and closing == noon:
        conv = "close12"
    elif not closing and "12:00 in the et timezone (noon)" in s:
        conv = "open12"
    else:
        return None, "candle"
    if typ == "updown_day":
        days = []
        for m1, d1, d2, m2, y in re.findall(r"\b(?:([a-z]{3})[a-z]* (\d{1,2})|(\d{1,2}) ([a-z]{3})[a-z]*),? '(\d\d)\b", s):
            mon = _MON3.get((m1 or m2)[:3])
            try:
                days.append(date(2000 + int(y), mon, int(d1 or d2)) if mon else None)
            except ValueError:
                days.append(None)
        if len(days) < 2 or days[0] != d - timedelta(days=1) or days[-1] != d:
            return None, "dates"
    return conv, ""


def _utc(t, fmt="%m-%d %H:%M"):
    return datetime.fromtimestamp(float(t), timezone.utc).strftime(fmt) if np.isfinite(t) else "–"


# ===================================================================== network (rate limited)

class NotFound(Exception):
    pass


def _open(url, binary=False, timeout=60):
    """HTTPS GET through the environment's proxy, TLS verified (certifi's store, as ladder.fetch_json)."""
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:  # pragma: no cover
        ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": "research-nearcert/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise NotFound(url) from e
        raise
    return raw if binary else json.loads(raw)


class Net:
    """GET with a global request rate and retries. `opener(url, binary)` is replaceable for tests."""

    def __init__(self, rate=RATE, opener=_open, sleep=time.sleep, clock=time.monotonic, tries=5):
        self.rate, self.opener, self.sleep, self.clock, self.tries = rate, opener, sleep, clock, tries
        self.next_t = 0.0
        self.calls = 0

    def get(self, url, binary=False):
        err = None
        for k in range(self.tries):
            now = self.clock()
            if self.next_t > now:
                self.sleep(self.next_t - now)
            self.next_t = max(now, self.next_t) + 1.0 / self.rate
            try:
                self.calls += 1
                return self.opener(url, binary)
            except NotFound:
                raise
            except urllib.error.HTTPError as e:
                if e.code in (400, 401, 403, 422):
                    raise
                err = e
            except Exception as e:  # network, proxy, truncated transfer
                err = e
            self.sleep(min(30.0, 1.5 * 2 ** k))
        raise RuntimeError(f"giving up on {url}: {err!r}")


SLIM = ("conditionId", "closed", "outcomes", "outcomePrices", "umaResolutionStatus", "closedTime", "createdAt",
        "endDate", "description")


def official(rec):
    """Official Yes (Up) result of a Gamma record: 1 / 0 / 0.5 once closed and resolved, else NaN."""
    if not rec or rec.get("closed") is not True:
        return NAN
    uma = rec.get("umaResolutionStatus")
    if uma not in (None, "", "resolved"):
        return NAN
    try:
        outs = rs._list(rec.get("outcomes"))
    except (TypeError, ValueError):
        return NAN
    yes = next((i for i, o in enumerate(outs) if str(o).lower() in ("yes", "up")), None)
    return NAN if yes is None else rs.official_yes(rec, yes)


class Gamma:
    """Gamma market records by condition id; a closed, resolved one is cached for good under
    <cache>/gamma.json, anything else is asked again on the next run. Failed requests go to `errors`."""

    def __init__(self, net, cache, notes=None, errors=None):
        self.net, self.path = net, Path(cache) / "gamma.json"
        self.notes = [] if notes is None else notes
        self.errors = [] if errors is None else errors
        try:
            self.final = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.final = {}
        self.live = {}

    def _ask(self, cids, closed):
        for i in range(0, len(cids), BATCH):
            batch = cids[i:i + BATCH]
            q = ([("closed", "true")] if closed else []) + [("condition_ids", c) for c in batch]
            try:
                data = self.net.get(GAMMA_MARKETS + urllib.parse.urlencode(q))
            except Exception as e:
                self.errors.append(f"Gamma（{len(batch)} 个市场）：{type(e).__name__}: {e}")
                continue
            for m in data if isinstance(data, list) else []:
                cid = m.get("conditionId")
                if cid not in batch:
                    continue
                rec = {k: m.get(k) for k in SLIM}
                if np.isfinite(official(rec)):
                    self.final[cid] = rec
                else:
                    self.live[cid] = rec

    def fetch(self, cids, open_too=False):
        """cid -> record (None if Gamma gave none): closed markets first, then (open_too) open ones."""
        cids = list(dict.fromkeys(cids))
        need = [c for c in cids if c not in self.final and c not in self.live]
        self._ask(need, True)
        if open_too:
            self._ask([c for c in need if c not in self.final and c not in self.live], False)
        if need:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.final))
            tmp.replace(self.path)
        return {c: self.final.get(c) or self.live.get(c) for c in cids}


class Klines:
    """Binance BTCUSDT 1m candles (open time -> high, low, close in USD) from data.binance.vision's daily
    files (cached). A day whose file is not published yet (404) gives None (pending, noted); any other
    failure gives None too and is recorded in `errors`."""

    def __init__(self, net, cache, notes=None, errors=None):
        self.net, self.dir = net, Path(cache) / "klines1m"
        self.notes = [] if notes is None else notes
        self.errors = [] if errors is None else errors
        self.days = {}

    def _day(self, d):
        if d not in self.days:
            self.days[d] = None
            f = self.dir / f"BTCUSDT-1m-{d.isoformat()}.zip"
            if not (f.exists() and f.stat().st_size > 0):
                try:
                    raw = self.net.get(VISION_1M.format(d=d.isoformat()), binary=True)
                except NotFound:
                    self.notes.append(f"币安 1m K 线 {d}：还没发布")
                    return None
                except Exception as e:
                    self.errors.append(f"币安 1m K 线 {d}：{type(e).__name__}")
                    return None
                f.parent.mkdir(parents=True, exist_ok=True)
                tmp = f.with_suffix(".tmp")
                tmp.write_bytes(raw)
                tmp.replace(f)
            try:
                t, h, lo, c = rs.read_kline_zip(f)
            except Exception as e:  # a damaged file: fetched again next run
                f.unlink(missing_ok=True)
                self.errors.append(f"币安 1m K 线 {d}：{type(e).__name__}")
                return None
            self.days[d] = {int(a): (x / 100.0, y / 100.0, z / 100.0)
                            for a, x, y, z in zip(t.tolist(), h.tolist(), lo.tolist(), c.tolist())}
        return self.days[d]

    def close(self, open_ts):
        """Close of the candle opening at open_ts; None while its day is pending; NaN if missing."""
        day = self._day(datetime.fromtimestamp(int(open_ts), timezone.utc).date())
        if day is None:
            return None
        k = day.get(int(open_ts))
        return float(k[2]) if k else NAN

    def extremes(self, a, b):
        """(high, low) of the candles opening at a, a + 60, ..., b (whole minutes); None while a day is
        pending; (NaN, NaN) if a candle is missing."""
        hi, lo = -math.inf, math.inf
        for o in range(int(a), int(b) + 1, 60):
            day = self._day(datetime.fromtimestamp(o, timezone.utc).date())
            if day is None:
                return None
            k = day.get(o)
            if k is None:
                return NAN, NAN
            hi, lo = max(hi, k[0]), min(lo, k[1])
        return (hi, lo) if hi >= lo else (NAN, NAN)


# ===================================================================== one recording

class Rec:
    """One built recording: receipt-time Binance trades, exchange-time token books, and feed health."""

    def __init__(self, name, books, markets, spot, closes=None):
        self.name, self.markets = name, markets
        available = pd.to_numeric(spot.get("receive_ts", spot["trade_ts"]), errors="coerce")
        spot = spot.assign(_available=available).dropna(subset=["_available", "price"]) \
            .sort_values(["_available", "trade_ts"], kind="stable")
        self.st = spot["_available"].to_numpy(float)
        self.source_ts = spot["trade_ts"].to_numpy(float)
        self.sp = spot["price"].to_numpy(float)
        self.first, self.last = (float(self.st[0]), float(self.st[-1])) if len(self.st) else (NAN, NAN)
        self.first_source = float(self.source_ts.min()) if len(self.source_ts) else NAN
        if len(self.st):
            sec = np.floor(self.st).astype(np.int64)
            self.sec0 = int(sec[0])
            n = int(sec[-1]) - self.sec0 + 1
            close = np.full(n, np.nan)
            last = np.flatnonzero(np.r_[sec[1:] != sec[:-1], True])  # last trade of each second
            close[sec[last] - self.sec0] = self.sp[last]
            self.have = np.isfinite(close)
            idx = np.where(self.have, np.arange(n), -1)
            np.maximum.accumulate(idx, out=idx)
            lr = np.zeros(n)
            lr[1:] = np.diff(np.log(close[idx]))  # idx >= 0 everywhere: second 0 has a trade
            self.c1 = np.r_[0.0, np.cumsum(lr)]
            self.c2 = np.r_[0.0, np.cumsum(lr * lr)]
            self.ch = np.r_[0, np.cumsum(self.have)]
            self.n = n
        # when the recorder had each book row's state: its exchange stamp, or its receipt when it came more
        # than SNAP_LAG later (the (re)subscription snapshot carries the time of the book's last change)
        ts_all = books["ts"].to_numpy(float) if len(books) else np.array([])
        recv = pd.to_numeric(books["recv"], errors="coerce").to_numpy(float) if "recv" in books else ts_all
        eff = np.where(np.isfinite(recv) & (recv - ts_all > SNAP_LAG), recv, ts_all)
        self.toks = {}
        for tok, g in books.assign(_eff=eff).groupby("market_id", sort=False):
            self.toks[str(tok)] = (g["ts"].to_numpy(float), g["ask"].to_numpy(float), g["ask_size"].to_numpy(float),
                                   g["_eff"].to_numpy(float))
        self.all_ts = np.sort(eff)
        # time of the row ending the latest feed silence > FEED_AGE, up to each row
        gap_end = np.where(np.r_[False, np.diff(self.all_ts) > FEED_AGE], self.all_ts, -np.inf)
        self.silence = np.maximum.accumulate(gap_end) if len(gap_end) else gap_end
        self.closes = np.sort(np.asarray(closes, float)) if closes is not None and len(closes) else np.array([])
        # the first book row after each CLOB disconnect: the resubscription snapshot confirms every book
        k = np.searchsorted(self.all_ts, self.closes, "right")
        self.resync = np.unique(self.all_ts[k[k < len(self.all_ts)]]) if len(self.closes) else np.array([])

    @classmethod
    def load(cls, d):
        books, markets, spot, _, closes = ld.load(d)
        return cls(lc._name(d), books, markets, spot, closes)

    # ---- Binance
    def price_before(self, t):
        """(price, local receipt time) of the last Binance trade available before t."""
        i = np.searchsorted(self.st, t, "left") - 1
        return (float(self.sp[i]), float(self.st[i])) if i >= 0 else (NAN, NAN)

    def history_ok(self, t, w):
        return int(t) - w - 1 >= self.sec0

    def sigma(self, t, w=SIGMA_S):
        """Std (ddof 1) of the 1 s log returns of seconds t-w .. t-1 (resolved.Spot.sigma); NaN if the
        recording does not reach back to second t-w-1 or fewer than w/2 of those seconds traded."""
        a, b = int(t) - w - self.sec0, int(t) - self.sec0
        if a < 1 or b > self.n:
            return NAN
        if self.ch[b] - self.ch[a] < w / 2:  # seconds t-w .. t-1 with a trade
            return NAN
        s1, s2 = self.c1[b] - self.c1[a], self.c2[b] - self.c2[a]
        return math.sqrt(max((s2 - s1 * s1 / w) / (w - 1), 0.0))

    def hit_state(self, up, level, t, start, pre_at, pre_ext, pre_outside):
        """'decided' (the window's high / low reached the level before t), 'open', or 'unknown' (see
        the module notes). start = the window start; pre_ext = pre_high (up) or pre_low (down), already
        joined with the official candles between the window start and pre_*'s own start; pre_outside =
        pre_* began before the window (so a level they reached may lie outside it)."""
        b = np.searchsorted(self.st, t, "left")
        available_source = self.source_ts[:b]
        first_source = self.first_source
        if start < first_source:  # the window began before the recording's trades: pre_* needed
            if not (np.isfinite(pre_at) and np.isfinite(pre_ext)) or pre_at > t:
                return "unknown"
            if (pre_ext >= level) if up else (pre_ext <= level):
                return "unknown" if pre_outside else "decided"
            s0, hole = first_source, first_source - pre_at > HIT_GAP
        else:
            s0, hole = start, False
        use = (available_source >= start) & (available_source < t)
        source = available_source[use]
        px = self.sp[:b][use]
        if len(px) and ((px.max() >= level) if up else (px.min() <= level)):
            return "decided"
        source = np.sort(source)
        if hole or not len(px) or np.diff(np.r_[s0, source]).max() > HIT_GAP:
            return "unknown"
        return "open"

    # ---- CLOB
    def entry(self, tok, te):
        """(status, ask, ask size, age of the token's row) for a buy at the ask as of te."""
        if tok not in self.toks:
            return "no_book", NAN, NAN, NAN
        ts, ask, size, eff = self.toks[tok]
        i = np.searchsorted(ts, te, "right") - 1
        if i < 0:
            return "no_book", NAN, NAN, NAN
        p, q, age = float(ask[i]), float(size[i]), float(te - ts[i])
        seen = float(eff[i])                          # when the recorder had this state
        stale = seen > te                             # a snapshot received only after te
        k = np.searchsorted(self.resync, te, "right") - 1
        if k >= 0 and self.resync[k] > seen:          # reconfirmed by the latest resubscription before te
            seen = float(self.resync[k])
        b = np.searchsorted(self.all_ts, te, "right")
        stale = stale or b == 0 or te - self.all_ts[b - 1] > FEED_AGE or self.silence[b - 1] > seen
        if len(self.closes):
            stale |= np.searchsorted(self.closes, te - CLOSE_WIN[0], "left") != \
                np.searchsorted(self.closes, te + CLOSE_WIN[1], "right")
        if stale:
            return "stale", p, q, age
        if not (np.isfinite(p) and ASK_BAND[0] - 1e-9 <= p <= ASK_BAND[1] + 1e-9):
            return "ask_band", p, q, age
        if not (np.isfinite(q) and q >= MIN_SIZE - 1e-9):
            return "ask_size", p, q, age
        return "trade", p, q, age


# ===================================================================== checkpoints

def noon_day(m):
    day = getattr(m, "day", None)
    return date.fromisoformat(day) if isinstance(day, str) and day else ld.et_date(int(m.end_ts))


def checkpoint(rec, m, c, t, sigma_s=SIGMA_S, ref_close=None, gamma=None, extremes=None):
    """One (market, checkpoint) row of a recording that covers t. ref_close(open_ts) / extremes(a, b):
    Klines.close / Klines.extremes; gamma: condition id -> Gamma record (description, createdAt)."""
    kind = market_kind(m)
    row = {"run": rec.name, "cid": m.condition_id, "kind": kind, "type": m.type, "check": c, "t": float(t),
           "end": float(m.end_ts), "rec_first": rec.first, "status": "", "conv": "", "S": NAN, "sigma": NAN,
           "tau": NAN, "p_yes": NAN, "p_fav": NAN, "side": "", "token": "", "ask": NAN, "size": NAN, "age": NAN,
           "fee_rate": fee_rate(getattr(m, "fee_rate", NAN))}

    def done(status):
        row["status"] = status
        return row

    if kind == OTHER:
        return done("rule_other")
    if not rec.history_ok(t, sigma_s):
        return done("short_history")
    S, s_ts = rec.price_before(t)
    if not (np.isfinite(S) and t - s_ts <= SPOT_ALIVE):
        return done("spot_gap")
    sig = rec.sigma(t, sigma_s)
    if not np.isfinite(sig):
        return done("sigma_na")
    row["S"], row["sigma"] = S, sig
    lo, hi = _fin(m.lo), _fin(m.hi)
    g = (gamma or {}).get(m.condition_id)
    if m.type in NOON_TYPES:
        d = noon_day(m)
        if int(m.end_ts) != ld.noon_et(d):
            return done("end_mismatch")
        if g is None:
            return done("gamma_na")
        conv, _ = noon_rule(m.type, g.get("description"), d)
        if conv is None:
            return done("rule_other")
        row["conv"] = conv
        tau = rs.noon_open(d, conv) + 60 - t
        ref = NAN
        if m.type == "updown_day":
            ref = ref_close(rs.noon_open(d - timedelta(days=1), conv)) if ref_close is not None else None
            if ref is None:
                return done("ref_pending")
            if not np.isfinite(ref):
                return done("no_ref")
        p = rs.p_yes(m.type, S, lo, hi, sig * math.sqrt(tau), ref=ref)
    elif m.type in HIT_TYPES:
        up = m.type == "hit_up"
        level = lo if up else hi
        if g is None:
            return done("gamma_na")
        period, _, ws, we, note = hit_rule(getattr(m, "question", None), m.end_ts, g)
        if period is None or note:
            return done("rule_other")
        if not (np.isfinite(we) and int(we) == int(m.end_ts)):
            return done("end_mismatch")
        if not (np.isfinite(ws) and np.isfinite(level)) or t <= ws:
            return done("hit_unknown")
        pre_start = _fin(getattr(m, "start_ts", NAN))
        pre_at, pre_ext = _fin(getattr(m, "pre_at", NAN)), _fin(getattr(m, "pre_high" if up else "pre_low", NAN))
        if np.isfinite(pre_start) and ws < pre_start and ws < rec.first_source and np.isfinite(pre_at) \
                and np.isfinite(pre_ext):
            # pre_* start at start_ts (the event's startDate), after the window start: the candles between
            gap = extremes(ws, pre_start // 60 * 60) if extremes is not None else None
            if gap is None:
                return done("kline_pending")
            if not (np.isfinite(gap[0]) and np.isfinite(gap[1])):
                return done("hit_unknown")
            pre_ext = max(pre_ext, gap[0]) if up else min(pre_ext, gap[1])
        state = rec.hit_state(up, level, t, ws, pre_at, pre_ext, bool(np.isfinite(pre_start) and pre_start < ws))
        if state != "open":
            return done("decided" if state == "decided" else "hit_unknown")
        tau = float(m.end_ts) - t
        p = rs.p_yes(m.type, S, level, NAN, sig * math.sqrt(tau))
    elif m.type == CONTROL:
        ref_ts = _fin(getattr(m, "ref_ts", NAN))
        r, r_ts = rec.price_before(ref_ts) if np.isfinite(ref_ts) else (NAN, NAN)
        ref = r if np.isfinite(r) and ref_ts - r_ts <= SPOT_ALIVE else _fin(getattr(m, "ref_price", NAN))
        if not (np.isfinite(ref) and np.isfinite(ref_ts) and t >= ref_ts):
            return done("no_ref")
        tau = float(m.end_ts) - t
        p = rs.p_yes(CONTROL, S, NAN, NAN, sig * math.sqrt(tau), ref=ref)
    else:
        raise ValueError(m.type)
    row["tau"], row["p_yes"] = tau, p
    if not (np.isfinite(p) and tau > 0):
        return done("no_ref")
    yes = p >= 0.5
    pf = p if yes else 1.0 - p
    row.update(p_fav=pf, side="Yes" if yes else "No", token=str(m.yes_token if yes else m.no_token))
    if pf < BAND[0] - 1e-12:
        return done("below")
    if pf >= BAND[1] - 1e-12:
        return done("above")
    status, row["ask"], row["size"], row["age"] = rec.entry(row["token"], t + ENTRY_LAG)
    return done(status)


def rec_points(rec, scope, checks=CHECKS):
    """(market, check, t) of every market in scope whose checkpoint this recording's Binance trades span."""
    out = []
    if not len(rec.st) or rec.markets.empty:
        return out
    for m in rec.markets[rec.markets["condition_id"].isin(scope)].itertuples(index=False):
        for c in checks:
            t = int(m.end_ts) - 60 * c
            if rec.first < t <= rec.last:
                out.append((m, c, t))
    return out


def rec_rows(rec, scope, checks=CHECKS, sigma_s=SIGMA_S, ref_close=None, gamma=None, extremes=None):
    """Rows of every (market in scope, checkpoint) whose time this recording's Binance trades span."""
    return [checkpoint(rec, m, c, t, sigma_s, ref_close, gamma, extremes) for m, c, t in rec_points(rec, scope, checks)]


def combine(rows, scope_mk, checks=CHECKS, data_end=NAN):
    """One row per (market in scope, checkpoint): the recording with >= 1 h of history and the
    earliest first trade; not_recorded / not_yet where no recording spans the time."""
    r = pd.DataFrame(rows)
    if len(r):
        r = r.assign(_short=r["status"] == "short_history") \
            .sort_values(["cid", "check", "_short", "rec_first"], kind="stable") \
            .drop_duplicates(["cid", "check"]).drop(columns="_short")
    have = set(zip(r["cid"], r["check"])) if len(r) else set()
    miss = []
    for m in scope_mk.itertuples(index=False):
        for c in checks:
            if (m.condition_id, c) in have:
                continue
            t = float(int(m.end_ts) - 60 * c)
            miss.append({"run": "", "cid": m.condition_id, "kind": market_kind(m),
                         "type": m.type, "check": c, "t": t, "end": float(m.end_ts),
                         "status": "not_yet" if not np.isfinite(data_end) or t > data_end else "not_recorded"})
    out = pd.concat([r, pd.DataFrame(miss)], ignore_index=True) if miss else r
    if not len(out):
        return pd.DataFrame(columns=["run", "cid", "kind", "type", "check", "t", "end", "status"])
    return out.sort_values(["t", "cid"], kind="stable").reset_index(drop=True)


def settle(cp, gamma_fetch, now):
    """Entries (status trade) with the official result, won, fee and pnl; NaN result = pending."""
    e = cp[cp["status"] == "trade"].copy()
    if not len(e):
        return e.assign(official=pd.Series(dtype=float), won=pd.Series(dtype=float), fee=pd.Series(dtype=float),
                        pnl=pd.Series(dtype=float))
    ended = sorted(e.loc[e["end"] + SETTLED_AFTER <= now, "cid"].unique())
    recs = gamma_fetch(ended) if ended else {}
    e["official"] = [official(recs.get(c)) for c in e["cid"]]
    e["won"] = np.where(e["side"] == "Yes", e["official"], 1.0 - e["official"])
    rates = pd.to_numeric(e["fee_rate"], errors="coerce").fillna(FEE) if "fee_rate" in e else FEE
    e["fee"] = rates * e["ask"] * (1.0 - e["ask"])
    e["pnl"] = e["won"] - e["ask"] - e["fee"]
    return e


# ===================================================================== statistics and verdict

def clustered(pnl, groups):
    """Equal-weight mean, its standard error clustered by group, t and the number of groups."""
    x = np.asarray(pnl, float)
    n = len(x)
    if n == 0:
        return NAN, NAN, NAN, 0
    mu = float(x.mean())
    s = pd.Series(x - mu).groupby(np.asarray(groups)).sum().to_numpy()
    G = len(s)
    if G < 2:
        return mu, NAN, NAN, G
    se = math.sqrt(float((s * s).sum()) * G / (G - 1)) / n
    return mu, se, (mu / se if se > 0 else NAN), G


def daily_lower_99(entries, alpha=ALPHA):
    """One-sided cluster lower bound for equal-size entries grouped by UTC decision day."""
    if not len(entries):
        return NAN
    e = entries.copy()
    e["day"] = [_utc(t, "%Y-%m-%d") for t in e["t"]]
    groups = e.groupby("day")["pnl"].agg(["sum", "size"])
    if len(groups) < 2:
        return NAN
    estimate = float(groups["sum"].sum() / groups["size"].sum())
    influence = groups["sum"] - estimate * groups["size"]
    se = math.sqrt(len(groups) / (len(groups) - 1) * float((influence * influence).sum())) / groups["size"].sum()
    return estimate - float(student_t.ppf(1.0 - alpha, len(groups) - 1)) * se


def market_exact_pvalue(entries):
    """Poisson-binomial fair-price test with one Bernoulli observation per market.

    Several checkpoints share one outcome and the favoured side can even flip, so only the first
    filled checkpoint of each market enters this test. Later entries still enter P&L and the daily
    cluster bound, but are never mislabelled as independent Bernoulli observations.
    """
    if not len(entries):
        return 1.0
    e = entries.copy()
    e["cost"] = e["ask"] + e["fee"]
    markets = e.sort_values(["t", "cid"], kind="stable").drop_duplicates("cid")[["cost", "won"]]
    markets = markets[markets["won"].isin((0.0, 1.0))]
    if not len(markets):
        return 1.0
    probabilities = markets["cost"].clip(0.0, 1.0).to_numpy(float)
    observed = int(markets["won"].sum())
    pmf = np.zeros(len(probabilities) + 1)
    pmf[0] = 1.0
    for index, probability in enumerate(probabilities):
        for wins in range(index + 1, 0, -1):
            pmf[wins] = pmf[wins] * (1.0 - probability) + pmf[wins - 1] * probability
        pmf[0] *= 1.0 - probability
    return float(np.clip(pmf[observed:].sum(), 0.0, 1.0))


def judge(out, entries, cp, data_end, n=N_VERDICT, min_days=MIN_DAYS, design=True, blockers=()):
    """The once-only verdict (latency._judge): pinned in <out>.verdict.md with the judged entries in
    <out>.trades.csv, then only read back. See the module notes for when it is written; `blockers` are
    this run's reasons not to judge (missing recordings, fetch errors, no --expect list)."""
    pinned = Path(out).with_suffix(".verdict.md")
    if pinned.exists():
        return pinned.read_text(encoding="utf-8").strip() + "（已判定，不再重算）"
    if not design:
        return "开发运行（参数不同于 NEARCERT.md），不判定。"
    e = entries[entries["kind"].isin(BINANCE_KINDS)] if len(entries) else entries
    order = e.sort_values(["t", "cid"], kind="stable").drop_duplicates("cid") if len(e) else e
    if len(order) < n:
        return f"目前 {len(order):,} 个市场有买入，不到 {n:,} 个，不判定。"
    chosen, days = [], set()
    for row in order.itertuples(index=False):
        chosen.append(row)
        days.add(_utc(row.t, "%Y-%m-%d"))
        if len(chosen) >= n and len(days) >= min_days:
            break
    if len(days) < min_days:
        return f"目前 {len(order):,} 个市场有买入，但只覆盖 {len(days)} 个 UTC 日，不到 {min_days} 日，不判定。"
    first = pd.DataFrame(chosen, columns=order.columns)
    cut = float(first["t"].max())
    sel = e[e["cid"].isin(set(first["cid"]))].sort_values(["t", "cid"], kind="stable")
    wait = []
    k = sel.loc[sel["official"].isna(), "cid"].nunique()
    if k:
        wait.append(f"{k} 个还没结算")
    if not (np.isfinite(data_end) and data_end >= first["end"].max()):
        wait.append("录制还没覆盖到它们全部结束")
    # an undetermined checkpoint could still add an earlier market or another entry of a chosen one
    u = int((cp["status"].isin(UNDETERMINED) & ((cp["t"] <= cut) | cp["cid"].isin(set(first["cid"])))).sum()) \
        if len(cp) else 0
    if u:
        wait.append(f"{u} 个时点还待定")
    wait += list(blockers)
    if wait:
        return f"已有 {len(order):,} 个市场有买入；前 {n:,} 个里" + "，".join(wait) + "，暂不判定。"
    mu, se, t, G = clustered(sel["pnl"], sel["cid"])
    lower = daily_lower_99(sel)
    exact_p = market_exact_pvalue(sel)
    ok = mu > 0 and np.isfinite(lower) and lower > 0 and exact_p < ALPHA
    runs = sorted({str(x) for x in sel["run"]})
    v = (f"前向检验（NEARCERT.md 检验 2，前 {len(first):,} 个市场，{len(sel):,} 笔，{len(days)} 个 UTC 日）："
         f"每份 {100 * mu:+.2f}¢（按市场聚类标准误 {100 * se:.2f}¢，t = {t:.2f}，{G} 个市场；"
         f"单侧 99% 日聚类下界 {100 * lower:+.2f}¢；市场级精确公平价格 p = {exact_p:.4g}）"
         f"→ {'通过' if ok else '没通过'}"
         f"（录制段 {len(runs)} 个：{', '.join(runs)}；第 {n:,} 个市场的首次买入时点 {_utc(cut, '%Y-%m-%d %H:%M')} UTC）")
    pinned.parent.mkdir(parents=True, exist_ok=True)
    pinned.write_text(v + "\n", encoding="utf-8")
    sel.to_csv(Path(out).with_suffix(".trades.csv"), index=False)
    return v


# ===================================================================== report

def _row(name, e):
    """Table cells of a group of entries: all of them, then the resolved ones' statistics."""
    r = e[e["official"].notna()]
    mu, se, t, G = clustered(r["pnl"], r["cid"])
    cell = lambda x, f: format(x, f) if np.isfinite(x) else "–"
    return (f"| {name} | {len(e):,} | {e['cid'].nunique():,} | {len(r):,} | "
            f"{cell(r['ask'].mean() if len(r) else NAN, '.3f')} | {cell(r['p_fav'].mean() if len(r) else NAN, '.3f')} | "
            f"{cell(r['won'].mean() if len(r) else NAN, '.1%')} | {cell(100 * mu, '+.2f')} | {cell(100 * se, '.2f')} | "
            f"{cell(t, '.2f')} |")


HEAD = ["| 分组 | 买入 | 市场 | 已结算 | 平均买价 | 模型概率 | 胜率 | 每份 ¢ | 聚类 SE ¢ | t |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]


def _projection(e_main, cp, n=N_VERDICT):
    """'按目前速度 ...' from the markets with a first entry per day of the checkpoints covered so far."""
    if not len(e_main):
        return ""
    covered = cp[~cp["status"].isin(("not_yet", "not_recorded"))] if len(cp) else cp
    days = covered["t"].map(lambda t: _utc(t, "%Y-%m-%d")).nunique() if len(covered) else 0
    k = e_main["cid"].nunique()
    if not days:
        return ""
    rate = k / days
    left = max(n - k, 0)
    plan = ""
    try:
        until = json.loads((Path(__file__).resolve().parent / "ci" / "ladder.json").read_text()).get("until")
        plan = f"；录制计划（ci/ladder.json）到 {until}" if until else ""
    except (OSError, ValueError):
        pass
    return (f"按目前速度（{days} 个有录制的 UTC 日里 {k} 个市场有买入，约每天 {rate:.1f} 个），攒满 {n} 个还要约 "
            f"{left / rate:.0f} 天{plan}。")


def report(cp, entries, verdict, recs_meta, since, notes=(), dev=None, checks=CHECKS, info=None):
    info = info or {}
    when = time.strftime("%Y-%m-%d %H:%M", time.gmtime())
    spans = [(a, b) for _, a, b in recs_meta if np.isfinite(a)]
    hours = sum(b - a for a, b in spans) / 3600
    span = f"{_utc(min(a for a, _ in spans))} → {_utc(max(b for _, b in spans))} UTC" if spans else "–"
    main = cp[cp["kind"].isin(BINANCE_KINDS)] if len(cp) else cp
    ctrl = cp[cp["kind"] == CONTROL] if len(cp) else cp
    other = cp[cp["kind"] == OTHER] if len(cp) else cp
    e_main = entries[entries["kind"].isin(BINANCE_KINDS)] if len(entries) else entries
    e_ctrl = entries[entries["kind"] == CONTROL] if len(entries) else entries
    nm = main["cid"].nunique() if len(main) else 0
    L = [f"# “九成五以上”的一边：前向检验（NEARCERT.md 检验 2，{when} UTC）", ""]
    if dev:
        L += [f"**开发运行，不是检验**：{dev}。", ""]
    led = info.get("ledger")
    L += [f"只用市场数据，不下单。录制段 {len(recs_meta)} 个，共 {hours:.1f} 小时（{span}）"
          + (f"：本次读入 {led['loaded']} 段，账本里已冻结 {led['frozen']} 段"
             + (f"，录制过期时仍有待定时点而剔除的 {led['expired']} 段" if led["expired"] else "") if led else "")
          + f"；{_utc(ts_of(since), '%Y-%m-%d %H:%M')} UTC 之后结束的币安结算市场 {nm:,} 个，每个看 {len(checks)} 个时点"
          f"（结束前 {'/'.join(map(str, checks))} 分钟）。", "",
          "## 判定", "", verdict, ""]
    if info.get("blockers"):
        L += ["本次不会判定的原因：" + "；".join(info["blockers"]) + "。", ""]
    L += [f"规则：按首次买入时点取到至少 {N_VERDICT} 个有买入市场且覆盖至少 {MIN_DAYS} 个 UTC 日后，对这些市场的全部买入判定一次："
          f"每份 > 0、单侧 99% 日聚类下界 > 0、市场级精确公平价格 p < {ALPHA:g} 才通过。", "",
          "## 计数", "",
          "| 时点状态 | 币安结算 | 4 小时（对照） |", "|---|---:|---:|"]
    for s, name in STATUS.items():
        a = int((main["status"] == s).sum()) if len(main) else 0
        b = int((ctrl["status"] == s).sum()) if len(ctrl) else 0
        if a or b:
            L.append(f"| {name} | {a:,} | {b:,} |")
    extra = []
    if len(other):
        extra.append(f"触及类其他窗口（年度、“before …”等，剔除，不在上表）：{other['cid'].nunique():,} 个市场")
    if len(main) and "conv" in main:
        conv = main[main["type"].isin(NOON_TYPES)].groupby("cid")["conv"].agg(
            lambda x: next((v for v in x if isinstance(v, str) and v), ""))
        vc = conv[conv != ""].value_counts()
        if len(vc):
            extra.append("中午类结算 K 线（按 Gamma 描述）：" + "，".join(f"{k} {v:,} 个市场" for k, v in vc.items()))
    pend = e_main[e_main["official"].isna()] if len(e_main) else e_main
    signals = main[main["status"].isin(SIGNAL_STATUS)] if len(main) else main
    fill_rate = len(e_main) / len(signals) if len(signals) else NAN
    fill_text = f"{fill_rate:.1%}" if np.isfinite(fill_rate) else "–"
    L += [""] + [f"{x}。" for x in extra] + \
         [f"模型触发 {len(signals):,} 次，模拟成交 {len(e_main):,} 次，模拟成交率 {fill_text}。"
          "这是显示盘口在 0.5 秒时仍有至少 5 份的上限，不含队列竞争。",
          f"买入 {len(e_main):,} 笔（{e_main['cid'].nunique() if len(e_main) else 0:,} 个市场），"
          f"其中待结算 {len(pend):,} 笔（{pend['cid'].nunique() if len(pend) else 0:,} 个市场）。"
          + _projection(e_main, main), "",
          "## 每份盈亏（已结算的买入，¢/份，扣吃单费）", ""] + HEAD
    if len(e_main):
        L.append(_row("全部", e_main))
        for c in sorted(set(e_main["check"]), reverse=True):
            L.append(_row(f"结束前 {c} 分钟", e_main[e_main["check"] == c]))
        for k in BINANCE_KINDS:
            q = e_main[e_main["kind"] == k]
            if len(q):
                L.append(_row(KIND_NAMES[k], q))
        fresh = e_main[e_main["age"] <= FEED_AGE]
        L.append(_row(f"其中代币盘口 ≤ {FEED_AGE:g} 秒内变过（仅参考）", fresh) if len(fresh) else
                 f"| 其中代币盘口 ≤ {FEED_AGE:g} 秒内变过（仅参考） | 0 |" + " – |" * 8)
    else:
        L.append("| （还没有） | 0 |" + " – |" * 8)
    L += ["", "## 4 小时涨跌（Chainlink 结算，只描述，不进判定）", ""] + HEAD
    if len(e_ctrl):
        L.append(_row("全部", e_ctrl))
        for c in sorted(set(e_ctrl["check"]), reverse=True):
            L.append(_row(f"结束前 {c} 分钟", e_ctrl[e_ctrl["check"] == c]))
    else:
        L.append("| （还没有） | 0 |" + " – |" * 8)
    L += ["", "## 做法（细节见 nearcert_fwd.py 开头）", "",
          "- 模型：无漂移，只用时点前记录机已收到的币安成交；σ = 过去 1 小时本地接收秒的 1 秒对数收益标准差"
          "（逐秒最后收到价，无成交的秒沿用），按到结算的剩余时间放大。",
          "- 结算规则按每个市场自己的 Gamma 描述：中午类写“closing at 12:00”的按 11:59 ET 开盘、12:00 收盘的那根 1 分钟 K 线"
          "（close12，日涨跌 2026-10-02 起的写法），写“12:00 in the ET timezone (noon)”的按 12:00 ET 开盘的那根（open12，"
          "above / range）；日涨跌的参考价是前一天同一根 K 线（data.binance.vision）；其他写法剔除。触及类的窗口按问题里的"
          "日/周/月和描述（ET 零点起，或市场创建起），年度等其他窗口剔除；用反射原理，窗口里时点前的最高/最低价 = 窗口开头到事件"
          "startDate 的官方 1 分钟 K 线 + 录制开始时取的 1 分钟 K 线 + 录制的逐笔成交，覆盖不全的跳过。"
          "录制里时点前不足 1 小时币安数据的跳过并计数。",
          f"- 买入：模型概率在 [{BAND[0]:.2f}, {BAND[1]:.2f}) 的一边，时点后 {ENTRY_LAG:g} 秒的卖一（只看那一刻及之前的盘口），"
          f"≥ {MIN_SIZE:g} 份、{ASK_BAND[0]:.2f}–{ASK_BAND[1]:.2f}，按市场记录的吃单费率（缺失才用 {FEE:g}）付费，每次 1 份；"
          f"盘口要“新鲜”：从记录机最后一次确认该代币盘口（它的变化，或重新订阅时的快照）到下单，录制的盘口流从没静默超过 "
          f"{FEED_AGE:g} 秒，下单前后没有断线。",
          "- 结果：市场结束后从 Gamma 按 condition id 取官方结算；还没结算的算待结算。标准误按市场聚类。",
          "- 账本：每段录制的时点结果存在 `nearcert-forward.ledger/`（随报告提交）；全部定下来的录制冻结、不再重算，"
          "所以录制 artifact 30 天过期后结果仍在。"]
    if notes:
        L += ["", "备注：", ""] + [f"- {x}" for x in notes]
    return "\n".join(L) + "\n"


# ===================================================================== ledger

def _gz_csv(df, path):
    """A deterministic .csv.gz (no file name, mtime 0: unchanged rows give unchanged bytes), atomically."""
    data = gzip.compress(df.to_csv(index=False, float_format="%.15g").encode(), compresslevel=9, mtime=0)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def _read_csv(path):
    df = pd.read_csv(path, dtype={c: str for c in STR_COLS})
    for c in STR_COLS:
        if c in df:
            df[c] = df[c].fillna("")
    return df


def ledger_dir(out):
    out = Path(out)
    return out.with_name(out.stem + ".ledger")


class Ledger:
    """Checkpoint rows per recording, kept with the report (see the module notes): <dir>/<run>.csv.gz,
    <dir>/runs.csv (run, first, last, rows, frozen) and <dir>/markets.csv.gz (the markets in scope)."""

    def __init__(self, d):
        self.dir = Path(d)
        self.meta = {}
        f = self.dir / "runs.csv"
        if f.exists():
            for r in pd.read_csv(f, dtype={"run": str}).itertuples(index=False):
                self.meta[r.run] = {"first": float(r.first), "last": float(r.last), "rows": int(r.rows),
                                    "frozen": bool(int(r.frozen))}
        f = self.dir / "markets.csv.gz"
        self.markets = _read_csv(f) if f.exists() else pd.DataFrame(columns=MARKET_COLS)
        self._new_markets = False

    def rows(self, run):
        f = self.dir / f"{run}.csv.gz"
        return _read_csv(f).to_dict("records") if f.exists() else []

    def put(self, run, first, last, rows, frozen):
        _gz_csv(pd.DataFrame(rows, columns=ROW_COLS), self.dir / f"{run}.csv.gz")
        self.meta[run] = {"first": float(first), "last": float(last), "rows": len(rows), "frozen": bool(frozen)}

    def add_markets(self, mk):
        if not len(mk):
            return
        new = mk[~mk["condition_id"].isin(set(self.markets["condition_id"]))]
        if len(new):
            add = new.reindex(columns=MARKET_COLS)
            self.markets = pd.concat([self.markets, add], ignore_index=True) if len(self.markets) else add
            self._new_markets = True

    def save(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        runs = pd.DataFrame([{"run": k, **v, "frozen": int(v["frozen"])} for k, v in self.meta.items()],
                            columns=["run", "first", "last", "rows", "frozen"]).sort_values(["first", "run"])
        tmp = self.dir / "runs.csv.tmp"
        runs.to_csv(tmp, index=False, float_format="%.15g")
        tmp.replace(self.dir / "runs.csv")
        if self._new_markets:
            _gz_csv(self.markets.sort_values(["end_ts", "condition_id"], kind="stable"), self.dir / "markets.csv.gz")
            self._new_markets = False


# ===================================================================== run

def scope_markets(dirs, since, known=None):
    """Markets of every recording (static fields of the first one seen; then those `known` from the
    ledger) of the studied types that end after `since`."""
    rows = {}
    for d in dirs:
        try:
            ms = json.loads((Path(d) / "markets.json").read_text())
        except (OSError, ValueError):
            continue
        for m in ms:
            if isinstance(m, dict) and m.get("condition_id") and m["condition_id"] not in rows:
                rows[m["condition_id"]] = m
    if known is not None and len(known):
        for m in known.to_dict("records"):
            rows.setdefault(m["condition_id"], m)
    mk = pd.DataFrame(list(rows.values()))
    if mk.empty:
        return pd.DataFrame(columns=MARKET_COLS)
    for c in MARKET_COLS:
        if c not in mk:
            mk[c] = None
    mk["end_ts"] = pd.to_numeric(mk["end_ts"], errors="coerce")
    return mk[mk["type"].isin(TYPES) & (mk["end_ts"] > ts_of(since))].reset_index(drop=True)


def run(roots, out, cache=CACHE, since=SINCE, now=None, checks=CHECKS, sigma_s=SIGMA_S, net=None, n=N_VERDICT,
        min_days=MIN_DAYS, log=print, expect=None):
    """Everything; writes `out`, the ledger (design runs) and the pinned verdict when due. `expect`: the
    run names whose ladder-<id> artifact can still be downloaded (None: unknown, never judged). Returns
    the report text, or None when there is no recording to read (nothing is written then)."""
    now = time.time() if now is None else now
    net = Net() if net is None else net
    notes, errors, blockers = [], [], []
    dirs = lc.built_dirs(roots)
    if not dirs:
        log(f"no built recording under {', '.join(map(str, roots))}")
        return None
    design = ts_of(since) == ts_of(SINCE) and tuple(checks) == CHECKS and sigma_s == SIGMA_S
    dev = None if design else f"--since {since}，--checks {','.join(map(str, checks))}，--sigma-window {sigma_s}"
    led = Ledger(ledger_dir(out)) if design else None
    scope = scope_markets(dirs, since, led.markets if led is not None else None)
    cids = set(scope["condition_id"]) if len(scope) else set()
    gamma = Gamma(net, cache, notes, errors)
    klines = Klines(net, cache, notes, errors)
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
        pts = rec_points(rec, cids, checks)
        need = sorted({m.condition_id for m, _, _ in pts if m.type in NOON_TYPES + HIT_TYPES} - set(ginfo))
        if need:
            ginfo.update({c: r for c, r in gamma.fetch(need, open_too=True).items() if r is not None})
        rr = [checkpoint(rec, m, c, t, sigma_s, klines.close, ginfo, klines.extremes) for m, c, t in pts]
        if led is not None:
            led.put(name, rec.first, rec.last, rr, frozen=not any(r["status"] in UNDETERMINED for r in rr))
        else:
            rows += rr
            meta[name] = (rec.first, rec.last)
        log(f"{name}: {len(rec.st):,} Binance trades, {len(rec.all_ts):,} book rows, {len(rr):,} checkpoint rows")
    expired = 0
    if led is not None:
        exp = None if expect is None else {str(x) for x in expect}
        for name, m in list(led.meta.items()):
            if name in loaded or m["frozen"]:
                continue
            if exp is not None and name not in exp:  # its artifact has expired: what is undetermined stays out
                rr = [{**r, "status": "expired_pending"} if r["status"] in UNDETERMINED else r for r in led.rows(name)]
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
    cp = combine(rows, scope, checks, data_end)
    entries = settle(cp, lambda c: gamma.fetch(c), now)
    if errors:
        blockers.append(f"本次抓取出错 {len(errors)} 次")
    verdict = judge(out, entries, cp, data_end, n=n, min_days=min_days, design=design, blockers=blockers)
    info = {"blockers": blockers if design else [],
            "ledger": {"loaded": len(loaded), "expired": expired,
                       "frozen": sum(m["frozen"] for m in led.meta.values())} if led is not None else None}
    text = report(cp, entries, verdict, [(k, a, b) for k, (a, b) in meta.items()], since, notes + errors, dev,
                  checks, info)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(text, encoding="utf-8")
    return text


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dirs", nargs="+")
    ap.add_argument("--out", default="real/nearcert-forward.md")
    ap.add_argument("--cache", default=str(CACHE), help="Gamma 结果和币安 K 线的缓存目录")
    ap.add_argument("--expect", default=None,
                    help="文件：artifact 还能下载的录制 run id，一行一个（CI 生成；没有就不判定）")
    ap.add_argument("--since", default=SINCE, help="只用此后结束的市场（开发用；改了就不判定）")
    ap.add_argument("--checks", default=",".join(map(str, CHECKS)), help="结束前几分钟（开发用；改了就不判定）")
    ap.add_argument("--sigma-window", type=int, default=SIGMA_S, help="σ 的回看秒数（开发用；改了就不判定）")
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
