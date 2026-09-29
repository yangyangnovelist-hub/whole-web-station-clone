"""Data access for the DineshKumar8399 Polymarket order-book dataset, with a sealed holdout.

    curl -L -o obd.tar.zst https://github.com/DineshKumar8399/polymarket-orderbook-dataset/releases/download/data-2026-09-13/polymarket-orderbook-2026-09-13.tar.zst
    tar --zstd -xf obd.tar.zst -C $OBD_ROOT      # or python -m loop.obd fetch

2-minute top-of-book snapshots (bid/ask of the YES token, no size) for ~314k
markets, 2026-07-10 .. 2026-09-13, and official outcomes (source='api') for
~169k of them, which run out after 2026-08-31. Convergence labels are
biased (README issue 6) and never used here.

Split, by time, fixed before any strategy was run:
- discovery: markets whose last quote is before SPLIT, quotes before SPLIT;
- holdout:   markets whose last quote is in [SPLIT, END), quotes from SPLIT on.
Only `holdout()` opens the holdout views, and every call is written to the
ledger first; at most HOLDOUT_BUDGET calls are allowed, each at
ALPHA / HOLDOUT_BUDGET, so the whole loop's false-positive rate stays <= ALPHA.
"""
from __future__ import annotations

import csv
import os
import sys
import time
from pathlib import Path

import duckdb

HERE = Path(__file__).resolve().parent
LEDGER = HERE / "ledger.csv"
RELEASE = "data-2026-09-13"
URL = (f"https://github.com/DineshKumar8399/polymarket-orderbook-dataset/releases/download/"
       f"{RELEASE}/polymarket-orderbook-2026-09-13.tar.zst")
START, SPLIT, END = "2026-07-10", "2026-08-06", "2026-09-01"
ALPHA, HOLDOUT_BUDGET = 0.05, 20
FEE_RATE = {"crypto": 0.07}          # sports and everything else: 0.05 (Polymarket, from July 2026)
DEFAULT_FEE = 0.05
LEDGER_FIELDS = ["time", "iteration", "hypothesis", "params", "split", "fill", "events", "trades",
                 "win_rate", "avg_cost", "pnl_per_share", "roi", "p", "threshold", "verdict", "note"]


def root():
    r = os.environ.get("OBD_ROOT") or str(Path(os.environ.get("SCRATCH", "/tmp")) / "obd")
    return Path(r)


def fetch(dest=None):
    import subprocess
    dest = Path(dest or root())
    if (dest / "labels.parquet").exists():
        return dest
    dest.mkdir(parents=True, exist_ok=True)
    tar = dest / "obd.tar.zst"
    subprocess.run(["curl", "-sSL", "-m", "900", "-o", str(tar), URL], check=True)
    subprocess.run(["tar", "--zstd", "-xf", str(tar), "-C", str(dest)], check=True)
    tar.unlink()
    return dest


EVENT_RE = r"^[a-z0-9]+-(.+?-\d{4}-\d{2}-\d{2})"


def connect(r=None):
    """Base views: `markets` (api-labelled only, with event key and fee rate) and `quotes_all`."""
    r = Path(r or root())
    con = duckdb.connect()
    con.execute("SET threads TO 4")
    con.execute("SET memory_limit = '10GB'")
    con.execute("SET enable_progress_bar = false")
    con.execute(f"""
        CREATE VIEW markets AS
        SELECT m.slug, m.question, m.category, m.first_ts, m.last_ts, l.y,
               coalesce(regexp_extract(m.slug, '{EVENT_RE}', 1), m.question) AS event,
               try_cast(regexp_extract(m.slug, '(\\d{{4}}-\\d{{2}}-\\d{{2}})', 1) AS DATE) AS sched,
               CASE m.category WHEN 'crypto' THEN {FEE_RATE['crypto']} ELSE {DEFAULT_FEE} END AS fee_rate
        FROM read_parquet('{r}/markets.parquet') m
        JOIN read_parquet('{r}/labels.parquet') l USING (slug)
        WHERE l.source = 'api'""")
    con.execute(f"""
        CREATE VIEW quotes_all AS
        SELECT ts, slug, bid, ask, segment
        FROM read_parquet('{r}/quotes/**/*.parquet', hive_partitioning=1)
        WHERE bid IS NOT NULL AND ask IS NOT NULL AND ask >= bid AND bid > 0 AND ask < 1""")
    return con


def discovery(r=None):
    con = connect(r)
    con.execute(f"CREATE VIEW m AS SELECT * FROM markets WHERE last_ts >= TIMESTAMP '{START}' AND last_ts < TIMESTAMP '{SPLIT}'")
    con.execute(f"CREATE VIEW q AS SELECT q.* FROM quotes_all q JOIN m USING (slug) WHERE q.ts < TIMESTAMP '{SPLIT}'")
    return con


def holdout_count():
    if not LEDGER.exists():
        return 0
    with open(LEDGER, newline="", encoding="utf-8") as f:
        return sum(1 for r in csv.DictReader(f) if r["split"] == "holdout" and r["verdict"] != "void")


def holdout(hypothesis, params, iteration, r=None):
    """Open the holdout for ONE pre-selected candidate. Logged before any data is read."""
    k = holdout_count()
    if k >= HOLDOUT_BUDGET:
        raise SystemExit(f"holdout budget exhausted ({k}/{HOLDOUT_BUDGET}); no more holdout tests")
    log(dict(iteration=iteration, hypothesis=hypothesis, params=params, split="holdout", verdict="opened",
             note=f"holdout test {k + 1}/{HOLDOUT_BUDGET}, threshold {ALPHA / HOLDOUT_BUDGET}"))
    con = connect(r)
    con.execute(f"CREATE VIEW m AS SELECT * FROM markets WHERE last_ts >= TIMESTAMP '{SPLIT}' AND last_ts < TIMESTAMP '{END}'")
    con.execute(f"CREATE VIEW q AS SELECT q.* FROM quotes_all q JOIN m USING (slug) WHERE q.ts >= TIMESTAMP '{SPLIT}'")
    return con


def log(row):
    new = not LEDGER.exists()
    with open(LEDGER, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LEDGER_FIELDS)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in LEDGER_FIELDS} | {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})


if __name__ == "__main__":
    if sys.argv[1:] == ["fetch"]:
        print(fetch())
