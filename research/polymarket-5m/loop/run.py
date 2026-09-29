"""python -m loop.run discovery H1_fav_band [...]   |   python -m loop.run holdout H1_fav_band '<params json>'"""
from __future__ import annotations

import json
import sys
import time

from . import engine, obd
from .hypotheses import FAMILIES, MIN_TRADES, PROMOTE_P


def iteration():
    import csv
    if not obd.LEDGER.exists():
        return 1
    with open(obd.LEDGER, newline="", encoding="utf-8") as f:
        its = [int(r["iteration"]) for r in csv.DictReader(f) if r["iteration"]]
    return max(its, default=0) + (0 if "--same-iteration" in sys.argv else 1)


def row(it, name, params, split, fill, res, threshold="", verdict="", note=""):
    s = res[fill]
    return dict(iteration=it, hypothesis=name, params=json.dumps(params, sort_keys=True), split=split, fill=fill,
                events=res["events"], trades=s.get("trades", 0),
                win_rate=f"{s['win_rate']:.4f}" if s.get("trades") else "",
                avg_cost=f"{s['avg_cost']:.4f}" if s.get("trades") else "",
                pnl_per_share=f"{s['pnl_per_share']:.5f}" if s.get("trades") else "",
                roi=f"{s['roi']:.5f}" if s.get("trades") else "",
                p=f"{s['p']:.5f}" if s.get("trades") else "", threshold=threshold, verdict=verdict, note=note)


def discovery(names, it):
    con = obd.discovery()
    promoted = []
    for name in names:
        fam = FAMILIES[name]
        best = None
        for params in fam["grid"]:
            t0 = time.time()
            res = engine.evaluate(con, fam["sql"](params))
            nx = res["next"]
            obd.log(row(it, name, params, "discovery", "next", res))
            print(f"{name} {params} events={res['events']} next: n={nx.get('trades', 0)} "
                  f"pnl={nx.get('pnl_per_share', float('nan')) * 100:+.2f}c p={nx.get('p', float('nan')):.4f} "
                  f"fill={nx.get('fill_rate', float('nan')):.2f} | same: pnl={res['same'].get('pnl_per_share', float('nan')) * 100:+.2f}c "
                  f"p={res['same'].get('p', float('nan')):.4f}  ({time.time() - t0:.0f}s)", flush=True)
            if nx.get("trades", 0) >= MIN_TRADES and (best is None or nx["p"] < best[1]["next"]["p"]):
                best = (params, res)
        if best and best[1]["next"]["p"] < PROMOTE_P and best[1]["next"]["pnl_per_share"] > 0:
            promoted.append((name, best[0]))
            obd.log(row(it, name, best[0], "discovery", "next", best[1], threshold=PROMOTE_P, verdict="promoted"))
        else:
            obd.log(dict(iteration=it, hypothesis=name, split="discovery", verdict="not promoted",
                         note="best p=%s" % (f"{best[1]['next']['p']:.4f}" if best else "n/a")))
    return promoted


def holdout(name, params, it):
    con = obd.holdout(name, json.dumps(params, sort_keys=True), it)
    res = engine.evaluate(con, FAMILIES[name]["sql"](params), sims=200_000)
    thr = obd.ALPHA / obd.HOLDOUT_BUDGET
    nx = res["next"]
    passed = nx.get("trades", 0) >= MIN_TRADES and nx["p"] < thr and nx["pnl_per_share"] > 0
    obd.log(row(it, name, params, "holdout", "next", res, threshold=thr, verdict="PASS" if passed else "fail",
                note=f"weeks positive {nx.get('weeks_positive', float('nan')):.2f}; same-fill p={res['same'].get('p', float('nan')):.4f}"))
    print(json.dumps(dict(hypothesis=name, params=params, result=res, passed=passed), default=str, indent=1))
    return passed


if __name__ == "__main__":
    it = iteration()
    if sys.argv[1] == "discovery":
        for n, p in discovery([a for a in sys.argv[2:] if not a.startswith("--")], it):
            print("PROMOTED", n, p)
    elif sys.argv[1] == "holdout":
        holdout(sys.argv[2], json.loads(sys.argv[3]), it)
