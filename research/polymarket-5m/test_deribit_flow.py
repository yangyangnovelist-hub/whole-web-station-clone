import json

import numpy as np
import pandas as pd
import pytest

import deribit_flow as dfw

MIN = 60_000
H = dfw.MS_H
DAY = dfw.MS_DAY
T0 = int(pd.Timestamp("2026-05-01", tz="UTC").value // 10**6)  # a Friday, 00:00 UTC
EXP_15MAY = int(pd.Timestamp("2026-05-15 08:00", tz="UTC").value // 10**6)
EXP_2MAY = int(pd.Timestamp("2026-05-02 08:00", tz="UTC").value // 10**6)


def _trade(ts, name, direction="buy", amount=1.0, iv=50.0, S=60000.0, block=None, tid=None):
    return {"trade_id": tid, "timestamp": int(ts), "instrument_name": name, "direction": direction,
            "amount": amount, "iv": iv, "index_price": S, "price": 0.01, "mark_price": 0.01,
            "block_trade_id": block, "combo_id": None, "liquidation": None}


def _prepare(rows):
    for i, r in enumerate(rows):
        if r["trade_id"] is None:
            r["trade_id"] = str(1000 + i)
    return dfw.prepare(dfw.trades_frame(rows))


def _gamma(S, K, exp, t, iv, call):
    _, g = dfw.bs_greeks(S, K, (exp - t) / dfw.MS_YEAR, iv, call)
    return float(g) * S ** 2 * 0.01


# ------------------------------------------------------------- download / paging

def _server(trades, page):
    calls = []

    def fetch(a, b):
        calls.append(a)
        sel = [t for t in trades if a <= t["timestamp"] <= b]
        return sel[:page], len(sel) > page
    return fetch, calls


def test_paging_keeps_trades_that_share_the_boundary_millisecond_and_dedupes():
    trades = [{"trade_id": str(i), "timestamp": 1000 + i // 3} for i in range(50)]  # 3 per ms
    fetch, calls = _server(trades, 7)
    got, pages = dfw.fetch_range(fetch, 0, 10**6, sleep=0)
    assert [t["trade_id"] for t in got] == [t["trade_id"] for t in trades]
    assert pages == len(calls) and pages > 50 // 7


def test_paging_always_moves_forward_even_if_a_page_is_one_millisecond():
    trades = [{"trade_id": str(i), "timestamp": 5000} for i in range(10)] + [{"trade_id": "99", "timestamp": 5001}]
    fetch, calls = _server(trades, 4)
    got, _ = dfw.fetch_range(fetch, 5000, 6000, sleep=0)
    assert "99" in [t["trade_id"] for t in got]
    assert len(calls) == 2  # the second page starts 1 ms later instead of looping


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class _Session:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def test_fetch_page_retries_rate_limits_errors_and_connection_failures():
    ok = _Resp(200, {"result": {"trades": [{"trade_id": "1", "timestamp": 5}], "has_more": False}})
    s = _Session([_Resp(429, {}), _Resp(400, {"error": {"code": 10028, "message": "too_many_requests"}}),
                  ConnectionError("reset"), ok])
    trades, more = dfw.fetch_page(s, 0, 10, base_sleep=0)
    assert s.calls == 4 and trades[0]["trade_id"] == "1" and more is False
    with pytest.raises(RuntimeError):
        dfw.fetch_page(_Session([_Resp(503, {})] * 3), 0, 10, retries=3, base_sleep=0)


def test_download_day_writes_atomically_and_resumes(tmp_path):
    day = "2026-05-01"
    rows = [_trade(T0 + 10 * MIN, "BTC-15MAY26-60000-C", tid="1"), _trade(T0 + 50 * MIN, "BTC-15MAY26-60000-P", tid="2")]  # gaps 10, 40, 1390 min
    fetch, calls = _server(rows, 1)
    m = dfw.download_day(day, tmp_path, fetch=fetch, sleep=0)
    assert m["trades"] == 2 and m["gaps_over_30min"] == 2  # the 40 min gap and the rest of the day
    assert round(m["max_gap_min"]) == round((DAY - 50 * MIN) / MIN)
    d = pd.read_parquet(dfw.day_path(tmp_path, day))
    assert list(d["trade_id"]) == [1, 2]

    def boom(a, b):
        raise AssertionError("a finished day must not be downloaded again")
    assert dfw.download_day(day, tmp_path, fetch=boom) == json.loads(dfw.day_path(tmp_path, day).with_suffix(".json").read_text())


# ------------------------------------------------------------- parsing and greeks

def test_prepare_parses_names_drops_bad_rows_and_invalid_iv():
    rows = [_trade(T0 + 1, "BTC-15MAY26-60000-C", "buy", iv=55.0, tid="1"),
            _trade(T0 + 2, "BTC-15MAY26-60000-P", "sell", iv=0.0, tid="2"),
            _trade(T0 + 3, "BTC-2MAY26-61500-P", "sell", iv=999.0, tid="3"),
            _trade(T0 + 3, "BTC-2MAY26-61500-P", "sell", iv=999.0, tid="3"),  # duplicate
            _trade(T0 + 4, "BTC-PERPETUAL", "buy", tid="4")]
    t = _prepare(rows)
    assert list(t["trade_id"]) == [1, 2, 3]
    assert list(t["side"]) == [1.0, -1.0, -1.0]
    assert t["iv"].iloc[0] == pytest.approx(0.55) and t["iv"].iloc[1:].isna().all()
    assert list(t["expiry"]) == [EXP_15MAY, EXP_15MAY, EXP_2MAY]
    assert list(t["strike"]) == [60000.0, 60000.0, 61500.0] and list(t["is_call"]) == [True, False, False]


def _bs_price(S, K, tau, s, call):
    from scipy.stats import norm
    d1 = (np.log(S / K) + 0.5 * s * s * tau) / (s * np.sqrt(tau))
    c = S * norm.cdf(d1) - K * norm.cdf(d1 - s * np.sqrt(tau))
    return c if call else c - S + K


@pytest.mark.parametrize("K,call", [(55000, True), (60000, True), (66000, False), (60000, False)])
def test_greeks_match_finite_differences_of_the_price(K, call):
    S, tau, s, h = 60000.0, 10 / 365, 0.5, 1.0
    delta, gamma = dfw.bs_greeks(S, K, tau, s, call)
    p = [_bs_price(S + k * h, K, tau, s, call) for k in (-1, 0, 1)]
    assert delta == pytest.approx((p[2] - p[0]) / (2 * h), rel=1e-5)
    assert gamma == pytest.approx((p[2] - 2 * p[1] + p[0]) / h ** 2, rel=1e-3)
    assert np.isnan(dfw.bs_greeks(S, K, 0.0, s, call)[1]) and np.isnan(dfw.bs_greeks(S, K, tau, np.nan, call)[0])


def test_pin_distance_and_hours_to_friday_expiry():
    assert dfw.pin_distance(61234.0, 1000) == pytest.approx(100 * 234 / 61234)
    assert dfw.pin_distance(61234.0, 5000) == pytest.approx(100 * 1234 / 61234)
    assert dfw.pin_distance(62600.0, 1000) == pytest.approx(-100 * 400 / 62600)
    fri8 = T0 + 8 * H
    assert list(dfw.hours_to_friday(np.array([T0, fri8 - 5 * MIN, fri8, T0 + 2 * DAY]))) == \
        pytest.approx([8.0, 5 / 60, 168.0, 6 * 24 - 16.0])


# ------------------------------------------------------------- the grid

def test_dealer_gamma_sign_current_price_window_expiry_and_strictly_before_T():
    call, put, short = "BTC-15MAY26-60000-C", "BTC-15MAY26-58000-P", "BTC-2MAY26-60000-C"
    rows = [_trade(T0 + 1 * MIN, call, "buy", 10, 50.0, 60000.0),     # customer buys 10 calls
            _trade(T0 + 10 * MIN, put, "sell", 4, 60.0, 61000.0),    # customer sells 4 puts, stamped AT a grid time
            _trade(T0 + 20 * MIN, short, "buy", 2, 40.0, 61000.0, block="b1"),  # block, expires May 2 08:00
            _trade(T0 + 9 * DAY, call, "sell", 1, 45.0, 62000.0)]
    t = _prepare(rows)
    T = np.array([T0, T0 + 5 * MIN, T0 + 10 * MIN, T0 + 15 * MIN, T0 + 25 * MIN,
                  EXP_2MAY - 5 * MIN, EXP_2MAY, T0 + 7 * DAY + 5 * MIN, T0 + 9 * DAY + 5 * MIN])
    f = dfw.build(t, T)
    g = f["dealer_gamma_usd_1pct"].to_numpy()
    assert g[0] == 0 and np.isnan(f["S"].iloc[0])
    # only the call is known: dealer short 10 calls -> negative, at S of the last trade
    assert g[1] == pytest.approx(-10 * _gamma(60000, 60000, EXP_15MAY, T[1], 0.5, True))
    assert g[1] < 0
    # the put stamped exactly at T[2] is not known at T[2], only at T[3]
    assert g[2] == pytest.approx(-10 * _gamma(60000, 60000, EXP_15MAY, T[2], 0.5, True))
    exp3 = -10 * _gamma(61000, 60000, EXP_15MAY, T[3], 0.5, True) + 4 * _gamma(61000, 58000, EXP_15MAY, T[3], 0.6, False)
    assert g[3] == pytest.approx(exp3)  # re-evaluated at the CURRENT index (61,000), dealer long the puts
    near = f["dealer_gamma_7d_usd_1pct"].to_numpy()
    nb = f["dealer_gamma_noblock_usd_1pct"].to_numpy()
    sh = -2 * _gamma(61000, 60000, EXP_2MAY, T[4], 0.4, True)
    assert g[4] == pytest.approx(-10 * _gamma(61000, 60000, EXP_15MAY, T[4], 0.5, True)
                                 + 4 * _gamma(61000, 58000, EXP_15MAY, T[4], 0.6, False) + sh)
    assert near[4] == pytest.approx(sh)  # the 15 May expiry is more than 7 days away
    assert nb[4] == pytest.approx(g[4] - sh)  # the block trade is left out of the no-block variant
    # the short-dated call is live five minutes before its expiry and gone at it
    assert near[5] == pytest.approx(-2 * _gamma(61000, 60000, EXP_2MAY, T[5], 0.4, True))
    assert near[6] == 0
    ex0 = f["dealer_gamma_ex0dte_usd_1pct"].to_numpy()
    assert ex0[4] == pytest.approx(g[4])  # at T[4] the 2 May expiry is still 31.6 h away
    assert ex0[5] == pytest.approx(g[5] - near[5])  # 5 min left: dropped from the ex-0DTE variant
    # seven days after the first call trade it is forgotten; the put (T0+10m) still counts
    assert g[7] == pytest.approx(4 * _gamma(61000, 58000, EXP_15MAY, T[7], 0.6, False))
    # nine days later only the new trade (customer sells 1 call) is in the window, at its IV and S
    assert g[8] == pytest.approx(+1 * _gamma(62000, 60000, EXP_15MAY, T[8], 0.45, True))
    assert list(f["n_open_instruments"]) == [0, 1, 1, 2, 3, 3, 2, 1, 1]


def test_same_day_expiry_counts_in_all_and_7d_but_not_ex0dte():
    t = _prepare([_trade(T0, "BTC-2MAY26-60000-P", "buy", 3, 50.0, 60000.0)])
    T = np.array([EXP_2MAY - 6 * H])
    f = dfw.build(t, T)
    want = -3 * _gamma(60000, 60000, EXP_2MAY, T[0], 0.5, False)
    assert f["dealer_gamma_usd_1pct"].iloc[0] == pytest.approx(want) and want < -1000
    assert f["dealer_gamma_7d_usd_1pct"].iloc[0] == pytest.approx(want)
    assert f["dealer_gamma_ex0dte_usd_1pct"].iloc[0] == 0


def test_delta_flow_put_call_ratio_atm_iv_and_windows():
    T = T0 + 2 * DAY
    c, p, far = "BTC-15MAY26-60000-C", "BTC-15MAY26-61000-P", "BTC-15MAY26-80000-C"
    rows = [_trade(T - 30 * H, c, "buy", 5, 70.0, 60000.0),   # outside 24 h
            _trade(T - 90 * MIN, p, "sell", 3, 55.0, 60500.0),  # in 24 h, not 1 h
            _trade(T - 30 * MIN, c, "buy", 2, 50.0, 60000.0),
            _trade(T - 20 * MIN, far, "sell", 1, 80.0, 60000.0),  # far OTM: not ATM
            _trade(T - 10 * MIN, p, "buy", 1, 60.0, 60000.0),   # 61000 / 60000 - 1 = 1.7%: ATM
            _trade(T, c, "buy", 100, 99.0, 60000.0)]             # at T: not known yet
    t = _prepare(rows)
    f = dfw.build(t, np.array([T]))
    r = f.iloc[0]

    def d(ts, K, iv, S, call):
        return float(dfw.bs_greeks(S, K, (EXP_15MAY - ts) / dfw.MS_YEAR, iv, call)[0])
    f1 = 2 * d(T - 30 * MIN, 60000, .5, 60000, True) - 1 * d(T - 20 * MIN, 80000, .8, 60000, True) \
        + 1 * d(T - 10 * MIN, 61000, .6, 60000, False)
    f24 = f1 - 3 * d(T - 90 * MIN, 61000, .55, 60500, False)
    assert r["cust_delta_flow_1h"] == pytest.approx(f1)
    assert r["cust_delta_flow_24h"] == pytest.approx(f24)
    assert r["put_contracts_24h"] == 4 and r["call_contracts_24h"] == 3 and r["pc_ratio_24h"] == pytest.approx(4 / 3)
    assert r["atm_iv_1h"] == pytest.approx(np.median([0.5, 0.6])) and r["n_atm_1h"] == 2
    assert r["n_trades_1h"] == 3 and r["n_trades_24h"] == 4
    assert r["S"] == 60000.0 and r["S_age_s"] == 600
    assert r["hours_to_fri_expiry"] == pytest.approx(6 * 24 - 16.0)
    assert f.index[0] == pd.Timestamp(T, unit="ms", tz="UTC") and f.index.name == "known_at"


def test_a_row_only_depends_on_trades_before_its_time():
    rng = np.random.default_rng(1)
    names = [f"BTC-{d}-{k}-{cp}" for d in ("2MAY26", "8MAY26", "15MAY26", "29MAY26")
             for k in (56000, 60000, 64000) for cp in "CP"]
    n = 3000
    ts = np.sort(rng.integers(T0 - 8 * DAY, T0 + 1 * DAY, n))
    S = 60000 * np.exp(np.cumsum(rng.normal(0, 0.002, n)))
    rows = [_trade(ts[i], names[rng.integers(len(names))], "buy" if rng.random() < .5 else "sell",
                   float(rng.integers(1, 20)) / 10, float(rng.choice([0.0, 45.0, 50.0, 62.0, 999.0])), S[i],
                   block="b" if rng.random() < .05 else None, tid=str(i)) for i in range(n)]
    t = _prepare(rows)
    T = T0 + np.arange(0, DAY, 5 * MIN)
    full = dfw.build(t, T)
    for k in (0, 7, 100, 250):
        cut = dfw.build(t[t["ts"] < T[k]].reset_index(drop=True), T[k:k + 1])
        pd.testing.assert_series_equal(full.iloc[k], cut.iloc[0], check_names=False)
    assert full["dealer_gamma_usd_1pct"].abs().gt(0).all() and full["n_iv_fallback"].max() >= 0
