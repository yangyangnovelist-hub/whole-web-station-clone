import hashlib
import io
import zipfile

import numpy as np
import pandas as pd
import pytest

import factors_data as fd

DAY = "2026-03-14"
T0 = 1_773_446_400  # 2026-03-14 00:00 UTC, seconds
MS = 1000


def zbytes(name, text):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr(name, text)
    return b.getvalue()


def zwrite(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(zbytes(path.name.replace(".zip", ".csv"), text))


def kline_rows(minutes, close, unit=MS):
    for i in minutes:
        o = (T0 + 60 * i) * unit
        yield (f"{o},{close(i)},{close(i)},{close(i)},{close(i)},{1 + i},{o + 60 * unit - 1},0,{10 + i},"
               f"{0.5 + i},0,0")


def at(panel, minutes):
    """The row stamped T0 + minutes."""
    return panel.loc[pd.Timestamp((T0 + 60 * minutes) * 10**9, tz="UTC")]


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    root = tmp_path_factory.mktemp("factors")
    cache = root / "cache"
    # perpetual 1 m klines with a header, minute 100 missing
    mins = [i for i in range(1440) if i != 100]
    zwrite(fd.raw_path(cache, "klines", DAY),
           ",".join(fd.KLINE_HEADER) + "\n" + "\n".join(kline_rows(mins, lambda i: 70000 + i)))
    # premium index without a header
    zwrite(fd.raw_path(cache, "premium", DAY), "\n".join(kline_rows(range(1440), lambda i: i / 1e6)))
    # metrics: 5-minute stamps, out of order, with a 20-minute gap after 10:00 and a 15-minute gap after 12:00
    stamps = [m for m in range(0, 1440, 5) if m not in (605, 610, 615, 725, 730)]
    rng = np.random.default_rng(0)
    lines = []
    for m in rng.permutation(stamps):
        c = pd.Timestamp((T0 + 60 * int(m)) * 10**9, tz="UTC").strftime("%Y-%m-%d %H:%M:%S")
        lines.append(f"{c},BTCUSDT,{1000 + m}.5,{m * 1e6},1.1,1.2,1.3,{m / 1000}")
    zwrite(fd.raw_path(cache, "metrics", DAY),
           "create_time,symbol," + ",".join(fd.METRICS) + "\n" + "\n".join(lines))
    # funding: one settlement before the day, one 5 ms after midnight, one exactly at 08:00
    f = [((T0 - 8 * 3600) * MS, 0.0001), (T0 * MS + 5, 0.0002), ((T0 + 8 * 3600) * MS, 0.0003),
         ((T0 + 16 * 3600) * MS, 0.0004)]
    zwrite(fd.raw_path(cache, "funding", "2026-03"),
           "calc_time,funding_interval_hours,last_funding_rate\n" + "\n".join(f"{t},8,{r}" for t, r in f))
    # spot 1 s klines (open time in us, no header): minute 5 absent, second 59 of minute 0 absent
    secs = [s for s in range(1440 * 60) if s // 60 != 5 and s != 59]
    klines = root / "klines"
    zwrite(klines / f"BTCUSDT-1s-{DAY}.zip",
           "\n".join(f"{(T0 + s) * 10**6},0,0,0,{60000 + s},1.0,{(T0 + s + 1) * 10**6 - 1},0,2,0.25,0,0" for s in secs))
    # DVOL 1 m (open time ms) with a gap from 06:00 to 07:00; DVOL 1 h
    dmin = [m for m in range(1440) if not 360 <= m < 420]
    dvol = pd.DataFrame({"t": [(T0 + 60 * m) * MS for m in dmin], "close": [50 + m / 1000 for m in dmin]})
    d1h = pd.DataFrame({"t": [(T0 + 3600 * h) * MS for h in range(24)], "close": [40.0 + h for h in range(24)]})
    panel, info = fd.build_panel(cache, klines, dvol, d1h, DAY, DAY)
    return panel, info, cache


def test_to_ns_reads_ms_us_and_ns():
    ns = T0 * 10**9
    assert (fd.to_ns([T0 * 1000, T0 * 10**6, ns]) == ns).all()


def test_asof_pos_takes_the_latest_known_within_the_age_cap_and_the_last_of_ties():
    known = np.array([30, 10, 10, 50])
    pos, age = fd.asof_pos(np.array([5, 10, 29, 30, 45, 50]), known, max_age=10)
    assert pos.tolist() == [-1, 2, -1, 0, -1, 3]  # 29: the 10s are 19 old; 45: the 30 is 15 old
    assert np.isnan(age[0]) and age[3] == 0


def test_minute_grid_keeps_its_end_point_over_long_ranges():
    g = fd.minute_grid("2026-03-14", "2026-09-30")
    assert len(g) == 201 * 1440 + 1 and g[-1] == fd.ts_ns(["2026-10-01"])[0]
    assert (np.diff(g) == fd.MIN).all()


def test_index_is_every_minute_through_the_next_midnight(built):
    panel, _, _ = built
    assert len(panel) == 1441 and str(panel.index.tz) == "UTC"
    assert panel.index[0] == pd.Timestamp("2026-03-14", tz="UTC")
    assert panel.index[-1] == pd.Timestamp("2026-03-15", tz="UTC")
    assert (np.diff(panel.index.asi8) == 60 * 10**9).all()


def test_a_kline_is_known_one_minute_after_it_opens_and_gaps_stay_empty(built):
    panel, _, _ = built
    assert np.isnan(at(panel, 0)["fut_close"])  # the 23:59 kline of the day before was not loaded
    assert at(panel, 1)["fut_close"] == 70000 and at(panel, 1)["fut_buy"] == 0.5 and at(panel, 1)["fut_trades"] == 10
    assert at(panel, 1440)["fut_close"] == 70000 + 1439
    assert np.isnan(at(panel, 101)["fut_close"]) and at(panel, 102)["fut_close"] == 70101  # open 100 missing
    ok = panel["fut_close"].notna()
    opened = (panel.index[ok].asi8 // 10**9 - T0) // 60 - 1
    assert (panel.loc[ok, "fut_close"].to_numpy() == 70000 + opened).all()
    assert at(panel, 61)["premium"] == pytest.approx(60 / 1e6)


def test_metrics_delay_covers_binance_publication_delay():
    """A row stamped c covers [c, c + 5 min) and Binance published rows up to 166 s after that
    (live poll 2026-10-04), so the default must be at least c + 8 min."""
    assert fd.METRICS_DELAY >= 8 * fd.MIN


def test_metrics_are_known_eight_minutes_after_create_time_and_never_fill_across_long_gaps(built):
    panel, _, _ = built
    assert np.isnan(at(panel, 7)["sum_open_interest"]) and at(panel, 8)["sum_open_interest"] == 1000.5
    assert at(panel, 12)["sum_open_interest"] == 1000.5 and at(panel, 13)["sum_open_interest"] == 1005.5
    assert at(panel, 8)["metrics_time"] == pd.Timestamp(T0 * 10**9, tz="UTC") and at(panel, 10)["metrics_age_s"] == 120
    # on the 5-minute grid the latest row at T is stamped T - 10 min
    assert at(panel, 30)["metrics_time"] == pd.Timestamp((T0 + 20 * 60) * 10**9, tz="UTC")
    # the 10:00 row (600) is known at 10:08 and carried 15 min; the next row (620) is known at 10:28
    assert (panel["sum_open_interest"].iloc[608:624] == 1600.5).all()
    assert panel["sum_open_interest"].iloc[624:628].isna().all() and at(panel, 628)["sum_open_interest"] == 1620.5
    # a 15-minute gap (rows 720 and 735) is filled throughout
    assert (panel["sum_open_interest"].iloc[728:743] == 1720.5).all() and at(panel, 743)["sum_open_interest"] == 1735.5
    assert at(panel, 8)["sum_taker_long_short_vol_ratio"] == 0 and at(panel, 13)["count_long_short_ratio"] == 1.3


def test_the_five_minute_rule_of_factors_md_is_still_available(built):
    _, _, cache = built
    p5, _ = fd.build_panel(cache, start=DAY, end=DAY, metrics_delay=5 * fd.MIN)
    assert np.isnan(at(p5, 4)["sum_open_interest"]) and at(p5, 5)["sum_open_interest"] == 1000.5


def test_a_longer_metrics_delay_shifts_every_metrics_row_later(built):
    _, _, cache = built
    late, _ = fd.build_panel(cache, start=DAY, end=DAY, metrics_delay=6 * fd.MIN)
    assert np.isnan(at(late, 5)["sum_open_interest"]) and at(late, 6)["sum_open_interest"] == 1000.5
    assert at(late, 10)["sum_open_interest"] == 1000.5 and at(late, 11)["sum_open_interest"] == 1005.5
    assert "spot_close" not in late and "dvol" not in late


def test_funding_is_known_from_its_calc_time_to_the_millisecond(built):
    panel, _, _ = built
    assert at(panel, 0)["funding_rate"] == 0.0001  # the 00:00:00.005 settlement is not known at 00:00:00
    assert at(panel, 1)["funding_rate"] == 0.0002 and at(panel, 479)["funding_rate"] == 0.0002
    assert at(panel, 480)["funding_rate"] == 0.0003 and at(panel, 1440)["funding_rate"] == 0.0004
    assert at(panel, 480)["funding_time"] == pd.Timestamp((T0 + 8 * 3600) * 10**9, tz="UTC")
    assert at(panel, 1)["funding_time"] == pd.Timestamp((T0 * 1000 + 5) * 10**6, tz="UTC")
    assert at(panel, 1)["funding_interval_hours"] == 8


def test_spot_minutes_from_one_second_klines(built):
    panel, _, _ = built
    first = at(panel, 1)  # the minute that opened at 00:00, second 59 absent
    assert first["spot_close"] == 60000 + 58 and first["spot_nsec"] == 59
    assert first["spot_vol"] == 59 and first["spot_buy"] == 59 * 0.25 and first["spot_trades"] == 118
    assert at(panel, 2)["spot_close"] == 60000 + 119 and at(panel, 2)["spot_nsec"] == 60
    assert np.isnan(at(panel, 6)["spot_close"]) and np.isnan(at(panel, 6)["spot_vol"])  # minute 5 absent
    assert np.isnan(at(panel, 0)["spot_close"])


def test_dvol_is_known_when_its_candle_closes_and_is_carried_at_most_fifteen_minutes(built):
    panel, _, _ = built
    assert np.isnan(at(panel, 0)["dvol"]) and at(panel, 1)["dvol"] == 50.0
    assert at(panel, 360)["dvol"] == pytest.approx(50.359) and at(panel, 375)["dvol"] == pytest.approx(50.359)
    assert at(panel, 375)["dvol_age_s"] == 900 and np.isnan(at(panel, 376)["dvol"])
    assert at(panel, 421)["dvol"] == pytest.approx(50.42)
    assert np.isnan(at(panel, 59)["dvol_1h"]) and at(panel, 60)["dvol_1h"] == 40 and at(panel, 119)["dvol_1h"] == 40
    assert at(panel, 120)["dvol_1h"] == 41


def test_coverage_report_names_the_gaps(built):
    panel, info, _ = built
    text = "\n".join(fd.coverage(panel, info))
    assert "perp 1 m klines" in text and "(1 min)" in text
    assert "15 min" in text or "20 min" in text
    assert fd.nan_runs(np.array([0, 1, 1, 0, 1], bool)) == [(1, 2), (4, 1)]


def test_readers_refuse_unexpected_layouts(tmp_path):
    p = tmp_path / "x.zip"
    zwrite(p, "time,price\n1,2\n")
    with pytest.raises(ValueError):
        fd.read_kline_zip(p)
    with pytest.raises(ValueError):
        fd.read_metrics_zip(p)
    with pytest.raises(ValueError):
        fd.read_funding_zip(p)


class FakeNet:
    def __init__(self, files, failures=0):
        self.files, self.failures, self.calls = files, failures, []

    def __call__(self, url):
        self.calls.append(url)
        if self.failures:
            self.failures -= 1
            raise ConnectionResetError("cut off")
        if url not in self.files:
            raise fd.NotFound(url)
        return self.files[url]


def test_fetch_retries_checks_the_checksum_writes_atomically_and_resumes(tmp_path):
    data = zbytes("a.csv", "1,2\n")
    url = "https://example/a.zip"
    net = FakeNet({url: data, url + ".CHECKSUM": f"{hashlib.sha256(data).hexdigest()}  a.zip\n".encode()}, failures=2)
    dest = tmp_path / "raw" / "a.zip"
    assert fd.fetch(url, dest, get=net, sleep=lambda s: None) == "downloaded"
    assert dest.read_bytes() == data and not (tmp_path / "raw" / "a.zip.part").exists()
    n = len(net.calls)
    assert fd.fetch(url, dest, get=net, sleep=lambda s: None) == "cached" and len(net.calls) == n


def test_fetch_rejects_a_bad_checksum_and_reports_404(tmp_path):
    data = zbytes("a.csv", "1,2\n")
    url = "https://example/a.zip"
    net = FakeNet({url: data, url + ".CHECKSUM": b"0" * 64 + b"  a.zip\n"})
    with pytest.raises(RuntimeError):
        fd.fetch(url, tmp_path / "a.zip", get=net, tries=3, sleep=lambda s: None)
    assert not (tmp_path / "a.zip").exists()
    assert fd.fetch("https://example/none.zip", tmp_path / "b.zip", get=FakeNet({}), sleep=lambda s: None) == "missing"
    # a truncated (invalid) cached file is fetched again
    (tmp_path / "c.zip").write_bytes(data[:10])
    ok = FakeNet({url: data})  # no CHECKSUM published: the zip test alone
    assert fd.fetch(url, tmp_path / "c.zip", get=ok, sleep=lambda s: None) == "downloaded"


def test_download_maps_every_day_and_month_to_its_url(tmp_path):
    files = {}
    for name, (period, url, _) in fd.DATASETS.items():
        for d in (["2026-03-31", "2026-04-01"] if period == "daily" else ["2026-03", "2026-04"]):
            files[url.format(d=d)] = zbytes("x.csv", "1\n")
    del files[fd.DATASETS["metrics"][1].format(d="2026-04-01")]
    st = fd.download(tmp_path, "2026-03-31", "2026-04-01", get=FakeNet(files), workers=2, log=lambda s: None)
    assert st["metrics"] == {"2026-03-31": "downloaded", "2026-04-01": "missing"}
    assert st["funding"] == {"2026-03": "downloaded", "2026-04": "downloaded"}
    assert fd.raw_path(tmp_path, "premium", "2026-04-01").exists()
