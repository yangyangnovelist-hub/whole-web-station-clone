#!/usr/bin/env bash
# Pool the forward/ results of one recording whose analyze job lost them to the stale-source_commit
# rebase conflict, with the analyze job's own commands ('Resolve, convert and test' and 'Frozen
# five-second momentum paper test' in polymarket-forward.yml) minus the network and conversion
# parts: the bundles in the converted archive ARE recording.py's output at the collector revision,
# with outcomes as fetched then. Run from research/polymarket-5m; commits nothing.
#
#   ci/forward_backfill.sh ID COLLECTOR_COMMIT CONVERTED_MANIFEST_SHA256 LEDGER_DIR WORK_DIR
#
# The converted archive's index and parts must already be in WORK_DIR/conv-ID/{index,parts}
# (the workflow downloads them). Exits non-zero, leaving forward/ for the caller to reset, when
# anything does not reproduce: code that differs from the collector revision, a hash mismatch,
# a judged row already pooled from another recording, or a change outside the pooled files.
set -euo pipefail
id=$1 commit=$2 conv=$3 ledgers=$4 T=$5
P="${RUNNER_TEMP:?}/paper"   # the analyze job's layout, so momentum5s rows keep run="_temp"
closure="forward.py strategy_zoo.py real_day.py binary.py paper_trader.py theta30.py asian180.py
  momentum5s.py latency.py recording.py backtest_book.py fill_rate.py artifact_parts.py
  restore_published_archive.py requirements.txt
  forward/theta30-freeze.json forward/momentum5s-freeze.json real/momentum5s-backfill.csv"
allowed='^forward/((trades|markets|theta30|momentum5s|paper_ledger)\.csv|(confirm|theta30|momentum5s|paper_report)\.md)$'

git diff --quiet "$commit" HEAD -- $closure \
  || { echo "::error::$id: analysis code or freeze differs from collector revision $commit"; exit 1; }
d="$T/conv-$id"
rm -rf "$P" "$d/raw.tgz" "$d/raw.tgz.sha256"; mkdir -p "$P"
python restore_published_archive.py --index-dir "$d/index" --parts-dir "$d/parts" --out "$d/raw.tgz" \
  --expected-manifest-sha256 "$conv" --expected-prefix "recording-$id" --expected-run-id "$id"
tar xzf "$d/raw.tgz" -C "$P"
rm -f "$d/raw.tgz" "$d/raw.tgz.sha256"
python - "$P/capture-manifest.json" "$commit" <<'PY'
import json, sys
got = json.load(open(sys.argv[1])).get("source_commit")
raise SystemExit(0 if got == sys.argv[2] else f"collector revision {got} != {sys.argv[2]}")
PY
before=$(wc -l < forward/trades.csv)

# the paper ledger rebuilt from the decisions and the outcomes the live recorder itself wrote
# (paper_trader settles an order only from a Gamma record seen before the recorder stopped)
python - "$P" "$T/rebuilt-$id.csv" <<'PY'
import csv, gzip, json, sys
from pathlib import Path
import paper_trader as pt
root, out = Path(sys.argv[1]), sys.argv[2]
def rows(name):
    for p in sorted(root.glob(f"raw/*/{name}.jsonl*")):
        with (gzip.open if p.suffix == ".gz" else open)(p, "rt", encoding="utf-8") as f:
            yield from (json.loads(line) for line in f if line.strip())
ended = json.loads((root / "capture-runtime.json").read_text())["ended_ms"]
won = {}
for m in rows("markets"):
    if isinstance(m, dict) and m.get("_recorded_at_ms", float("inf")) <= ended:
        try:
            up = pt.parse_gamma_market(m)["up_won"]
        except (KeyError, ValueError):
            continue
        if up is not None:
            won.setdefault(m["slug"], up)
cols = ["slug", "end", "tau", "side", "ask", "shares", "fee", "cost", "won", "pnl", "mid_up", "decided_ms"]
n = 0
with open(out, "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for d in rows("decisions"):
        if "skip" not in d and d.get("slug") in won:
            w.writerow(pt.settle(d, won[d["slug"]])); n += 1
print(f"{sys.argv[2]}: {n} rebuilt ledger rows")
PY
if [ -f "$ledgers/$id/ledger.csv" ]; then
  if python - "$ledgers/$id/ledger.csv" "$T/rebuilt-$id.csv" <<'PY'
import sys, pandas as pd
a, b = (pd.read_csv(p, dtype=str).sort_values(["end", "tau"]).reset_index(drop=True) for p in sys.argv[1:])
raise SystemExit(0 if a.equals(b) else 1)
PY
  then echo "$id" >> "$T/rebuild-validated"; else echo "::warning::$id: rebuilt ledger differs from the salvaged one"; fi
fi

collisions() {   # rows of this recording that another recording already pooled, on judged keys
  python - "$T" "$1" <<'PY'
import sys, pandas as pd
T, c = sys.argv[1], sys.argv[2]
def hits(new, old, keys):
    new, old = pd.read_csv(new), pd.read_csv(old)
    if new.empty or old.empty:
        return new.iloc[:0], old.iloc[:0]
    n = new[new.set_index(keys).index.isin(old.set_index(keys).index)]
    return n, old[old.set_index(keys).index.isin(n.set_index(keys).index)]
bad = []
n, _ = hits(f"{T}/new-{c}.csv", "forward/trades.csv", ["id", "slug"])
bad += [f"trade {r.id} {r.slug}" for r in n.itertuples()]
if c == "btc":
    n, o = hits(f"{T}/theta30-new.csv", "forward/theta30.csv", ["market_id"])
    if len(n):
        print("theta30 boundary rows (kept as pooled):\n" + pd.concat([n.assign(src="this"), o.assign(src="pooled")]).to_string())
    bad += [f"theta30 {m}" for m in set(n.loc[n["sent"].astype(str) == "True", "market_id"])
            | set(o.loc[o["sent"].astype(str) == "True", "market_id"])]
    n, _ = hits(f"{T}/momentum5s-new.csv", "forward/momentum5s.csv", ["market_id", "lag"])
    bad += [f"momentum5s {r.market_id} {r.lag}" for r in n.itertuples()]
raise SystemExit(f"judged rows already pooled from another recording: {bad}" if bad else 0)
PY
}

for c in btc eth sol xrp doge; do
  b="$P/bundle-$c"
  echo "== $c"
  [ -d "$b" ] || { echo "$b missing"; continue; }
  if python strategy_zoo.py "$b" --confirm --extra 101 103 104 105 106 107 108 109 --reps 2000 \
       --out "$T/run-$c.md" --trades-out "$T/new-$c.csv"; then
    if [ "$c" = btc ] && [ -d "$b/latency" ]; then
      python theta30.py --latency-dir "$b/latency" --out "$T/theta30-new.csv" > /dev/null
      python momentum5s.py --latency-dir "$b/latency" --manifest forward/momentum5s-freeze.json \
        --csv-out "$T/momentum5s-new.csv" --out "$T/momentum5s-dry.md" > /dev/null
    fi
    collisions "$c"
    python forward.py add "$T/new-$c.csv" --markets "$b"
  else
    echo "$b: strategy_zoo failed; independent frozen tests still run"
  fi
  if [ "$c" = btc ] && [ -d "$b/latency" ]; then
    python theta30.py --latency-dir "$b/latency" --out "$T/theta30-new.csv" \
      --manifest forward/theta30-freeze.json --append forward/theta30.csv --report forward/theta30.md > /dev/null
  fi
done
if [ -f "$ledgers/$id/ledger.csv" ]; then
  python forward.py add-ledger "$ledgers/$id/ledger.csv"
else
  echo "$id: no salvaged ledger (raw capture expired); the workflow adds the rebuilt one if it validates"
fi
python forward.py report > /dev/null
python momentum5s.py --latency-dir "$P/bundle-btc/latency" --seed real/momentum5s-backfill.csv \
  --manifest forward/momentum5s-freeze.json --append forward/momentum5s.csv \
  --verdict-out forward/momentum5s-verdict.json --out forward/momentum5s.md > /dev/null
bad=$(git status --porcelain=v1 --untracked-files=all -- forward | cut -c4- | sed 's|^research/polymarket-5m/||' \
      | grep -vE "$allowed" || true)
[ -z "$bad" ] || { echo "::error::$id: unexpected forward/ change (freeze, pinned verdict or new file): $bad"; exit 1; }
echo "$id: trades $before -> $(wc -l < forward/trades.csv) lines"
git diff --stat -- forward
