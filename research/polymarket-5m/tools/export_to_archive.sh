#!/usr/bin/env bash
# Export the market data of a shadow runtime (SQLite), an engine log or a research data folder into
# the private sgk90-shadow-archive repo, on its own branch `export/<label>`, where a research
# session can read it. export_all.sh runs this for every known source on a machine.
#
#   bash export_to_archive.sh <label> <runtime.sqlite | data folder> [since YYYY-MM-DD] [until YYYY-MM-DD]
#
# Examples (Dublin server; the service may keep running, the database is only read):
#   bash export_to_archive.sh dublin-0926-0929 sgk90_regime_reverse_filter_shadow/state/runtime_1000.sqlite 2026-09-26 2026-09-30
#   bash export_to_archive.sh dublin-log-0926-0929 <the shadow's current .jsonl log> 2026-09-26 2026-09-30
#   bash export_to_archive.sh payoff-0901-0911 ~/Downloads/sgk90_regime_r553_v3_live/docs/btcpoly_payoff_research_20260911/data
#
# What leaves the machine:
# - from every SQLite file: only the market-data tables below (Polymarket books, market registry and
#   outcomes, Chainlink TWAP, Coinbase and Binance samples), optionally cut to [since, until);
#   never orders, fills, positions, proposals, audit logs or settings;
# - from an engine log (.jsonl / .jsonl.gz): only market-data events (Binance trades, Coinbase and
#   Chainlink prices, market registrations and outcomes), cut to [since, until); never the strategy's
#   decisions, orders or settlements;
# - from a folder: the above for every SQLite / JSONL inside, plus its *.csv / *.csv.gz / *.parquet
#   files, except any whose name looks like a key, secret, wallet, password or env file.
# Everything is gzip-compressed, split into 90 MB parts and pushed with this machine's own git
# credentials to the private repo; nothing is published elsewhere, nothing trades. Re-running a
# label replaces its branch.
set -euo pipefail

label=${1:?label, e.g. dublin-0926-0929}
src=${2:?runtime.sqlite or a data folder}
since=${3:-}
until=${4:-}
repo=${ARCHIVE_URL:-https://github.com/yangyangnovelist-hub/sgk90-shadow-archive.git}
git ls-remote "$repo" >/dev/null 2>&1 || {
  echo "cannot reach $repo with this machine's git credentials; run: gh auth login && gh auth setup-git" >&2
  exit 2
}
work=$(mktemp -d "${TMPDIR:-/tmp}/export.XXXXXX")
trap 'rm -rf "$work"' EXIT
out="$work/out/$label"
mkdir -p "$out"

python3 - "$src" "$out" "$since" "$until" <<'PY'
import gzip, os, re, shutil, sqlite3, sys, csv
from datetime import datetime, timezone
from pathlib import Path

src, out, since, until = Path(sys.argv[1]).expanduser(), Path(sys.argv[2]), sys.argv[3], sys.argv[4]
to_ts = lambda d: datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() if d else None
lo, hi = to_ts(since) or 0.0, to_ts(until) or 9e18
TABLES = {  # known market-data tables: time column used for since/until (None: whole table)
    "poly_probability_observations_v1": "source_ts", "market_registry": None, "market_outcomes": None,
    "chainlink_twap60_samples": "source_ts", "coinbase_perp_mid_samples_v1": "event_ts",
    "binance_agg_trades_v1": "trade_ts", "binance_trades": "trade_ts",
}
# Older engine versions named their tables differently: any other table whose name says it holds
# market data is exported too, and nothing that holds the strategy's own state.
MARKET = re.compile(r"poly|book|quote|tick|price|chainlink|coinbase|binance|twap|spot|feed|observation", re.I)
PRIVATE = re.compile(r"order|fill|position|proposal|forensic|audit|equity|settle|calib|meta|revalid|redeem|"
                     r"receivable|caps|trace|blocker|decision|signal|strategy|wallet|key|secret|config|sqlite_", re.I)
TIME_COLS = ("source_ts", "event_ts", "trade_ts", "ts", "timestamp", "recv_ts", "receive_ts")
secret = re.compile(r"key|secret|wallet|passw|mnemonic|\.env|\.pem", re.I)
kept = []
inventory = []  # every table of every database, names and row counts only

def export_db(db, name):
    """Market-data tables of one SQLite file; `name` keeps files from equally named databases apart."""
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        have = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
    except sqlite3.DatabaseError as e:
        kept.append(f"{name}: unreadable ({e})")
        return
    counts = {}
    for t in sorted(have):
        try:
            counts[t] = con.execute(f'select count(*) from "{t}"').fetchone()[0]
        except sqlite3.DatabaseError as e:
            counts[t] = f"unreadable ({e})"
    inventory.append(f"{name}: " + ", ".join(f"{t} ({c})" for t, c in counts.items()))
    chosen = {t: tc for t, tc in TABLES.items() if t in have}
    for t in sorted(have - set(chosen)):
        if MARKET.search(t) and not PRIVATE.search(t):
            cols = [r[1] for r in con.execute(f'pragma table_info("{t}")')]
            chosen[t] = next((c for c in TIME_COLS if c in cols), None)
    for table, tcol in chosen.items():
        path = out / f"{name}.{table}.csv.gz"
        n = 0
        try:
            where = f' where "{tcol}" >= ? and "{tcol}" < ?' if tcol else ""
            cur = con.execute(f'select * from "{table}"{where}', (lo, hi) if tcol else ())
            with gzip.open(path, "wt", newline="", compresslevel=6) as fh:
                w = csv.writer(fh)
                w.writerow([d[0] for d in cur.description])
                for row in cur:
                    w.writerow(row)
                    n += 1
            kept.append(f"{path.name}: {n:,} rows")
        except sqlite3.DatabaseError as e:  # a damaged file: keep what was read
            kept.append(f"{path.name}: {n:,} rows, then stopped ({e})")
    con.close()


EVENTS = ("BINANCE_AGG_TRADE", "COINBASE_PERP_L2", "CHAINLINK_TWAP", "MARKET_REGISTERED", "MARKET_OUTCOME_RECORDED")
event_re = re.compile(r'"event"\s*:\s*"([A-Z0-9_]+)"')
ts_re = re.compile(r'"ts"\s*:\s*([0-9.]+)')

def export_log(f, rel):
    """Market-data events of an engine log; lines without an event type (plain tapes) are kept."""
    opener = gzip.open if f.name.endswith(".gz") else open
    dst = out / (rel.removesuffix(".gz") + ".gz")
    n = dropped = 0
    with opener(f, "rt", errors="replace") as a, gzip.open(dst, "wt", compresslevel=6) as b:
        for line in a:
            m = event_re.search(line[:200])
            if m and m.group(1) not in EVENTS:
                dropped += 1
                continue
            t = ts_re.search(line[:200])
            if t and not (lo <= float(t.group(1)) < hi):
                continue
            b.write(line)
            n += 1
    kept.append(f"{dst.name}: {n:,} lines kept, {dropped:,} strategy/engine events left out")

def is_db(f):
    return f.suffix in (".sqlite", ".db") or f.name.endswith(".sqlite3")

if src.is_file():
    export_db(src, src.stem) if is_db(src) else export_log(src, src.name)
else:
    for f in sorted(src.rglob("*")):
        if not f.is_file() or secret.search(f.name):
            continue
        rel = "__".join(f.relative_to(src).parts)
        if is_db(f):
            export_db(f, rel.rsplit(".", 1)[0])
        elif f.name.endswith((".jsonl", ".jsonl.gz")):
            export_log(f, rel)
        elif f.name.endswith((".csv", ".csv.gz", ".parquet")):
            dst = out / (rel if f.name.endswith((".gz", ".parquet")) else rel + ".gz")
            if dst.suffix == ".gz" and not f.name.endswith(".gz"):
                with open(f, "rb") as a, gzip.open(dst, "wb", compresslevel=6) as b:
                    shutil.copyfileobj(a, b)
            else:
                shutil.copy2(f, dst)
            kept.append(f"{dst.name}: {f.stat().st_size / 1e6:.0f} MB raw")
(out / "EXPORT.md").write_text(f"# {out.name}\n\nsource: {src.name}\nsince: {since or '-'}\nuntil: {until or '-'}\n\n"
                               + "\n".join(f"- {k}" for k in kept) + "\n\n## All tables (names and row counts only)\n\n"
                               + "\n".join(f"- {k}" for k in inventory) + "\n")
print("\n".join(kept) or "nothing to export")
PY

for f in "$out"/*; do
  [ "$(basename "$f")" = EXPORT.md ] && continue
  size=$(wc -c < "$f")
  if [ "$size" -gt 94371840 ]; then split -b 90m "$f" "$f.part-" && rm "$f"; fi
done
du -sh "$out"

branch="export/$label"
git init -q "$work/repo"
mkdir -p "$work/repo/$label"
cd "$work/repo"
git remote add origin "$repo"
commit_push() {
  git add "$label"
  git diff --cached --quiet && return 0
  git -c user.name="${GIT_AUTHOR_NAME:-export}" -c user.email="${GIT_AUTHOR_EMAIL:-export@localhost}" \
    commit -q -m "research export: $label ($1)"
  git push -q -f origin "HEAD:refs/heads/$branch"
}
# Push in batches of about 1.2 GB: GitHub refuses a single push over 2 GB.
batch=0; n=0
for f in "$out"/*; do
  cp "$f" "$label/"
  batch=$((batch + $(wc -c < "$f"))); n=$((n + 1))
  if [ "$batch" -gt 1288490188 ]; then commit_push "part $n"; batch=0; fi
done
commit_push "done"
echo "pushed $label to branch $branch"
