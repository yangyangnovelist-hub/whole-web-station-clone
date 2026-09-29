"""python -m bot {replay,evaluate,record,paper,live} ..."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


def cmd_replay(a):
    from . import replay
    from .paper import JsonlSink, PaperBroker
    from .strategy import Engine

    Path(a.log).parent.mkdir(parents=True, exist_ok=True)
    if not a.append:
        Path(a.log).write_text("")
    sink = JsonlSink(a.log)
    engine = Engine(PaperBroker(sink, latency_ms=a.latency_ms))
    kinds = replay.CLOB_KINDS + ("spot", "twap")
    if a.no_levels:
        kinds = tuple(k for k in kinds if k != "price_change")
    t0 = time.time()
    markets, n = replay.run(a.root, engine, kinds=kinds, days=set(a.day) if a.day else None,
                            progress=lambda n, ms: print(f"  {n:,} events  {time.strftime('%H:%M:%S', time.gmtime(ms / 1000))}",
                                                         file=sys.stderr))
    sink.close()
    print(f"replayed {n:,} events, {len(markets)} markets in {time.time() - t0:.0f}s -> {a.log}", file=sys.stderr)


def cmd_evaluate(a):
    from . import evaluate

    res = evaluate.evaluate(a.logs, include_in_sample=a.include_in_sample)
    text = evaluate.markdown(res)
    print(text)
    if a.out:
        Path(a.out).write_text(text + "\n", encoding="utf-8")
    if a.gate:
        Path(a.gate).write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


def cmd_live(a, mode):
    import asyncio

    from .live import main as live_main

    try:
        asyncio.run(live_main(a, mode))
    except KeyboardInterrupt:
        print("stopped", file=sys.stderr)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m bot", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("replay", help="run the variants over recorded days")
    p.add_argument("root", help="data root in the outcometick layout")
    p.add_argument("--log", default="logs/replay.jsonl")
    p.add_argument("--day", action="append", help="only these UTC days (repeatable)")
    p.add_argument("--latency-ms", type=int, default=250)
    p.add_argument("--no-levels", action="store_true", help="skip price_change (faster, coarser book)")
    p.add_argument("--append", action="store_true")
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("evaluate", help="GO / NO-GO per variant from trade logs")
    p.add_argument("logs", nargs="+")
    p.add_argument("--gate", help="write the machine-readable verdict here (live mode reads it)")
    p.add_argument("--out", help="write the markdown table here")
    p.add_argument("--include-in-sample", action="store_true",
                   help="also count the day the variants were chosen on (not evidence!)")
    p.set_defaults(func=cmd_evaluate)

    for name, help_ in (("record", "record the raw feeds only"),
                        ("paper", "record + paper-trade every variant"),
                        ("live", "record + paper-trade + REAL orders for gated variants")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--data", default="data", help="where recordings go (outcometick layout)")
        p.add_argument("--log", default="logs/paper.jsonl")
        p.add_argument("--latency-ms", type=int, default=250)
        if name == "live":
            p.add_argument("--variant", action="append", required=True, help="variant(s) to trade for real")
            p.add_argument("--gate", required=True, help="gate.json from `evaluate` with verdict GO")
            p.add_argument("--max-shares", type=float, default=20, help="per order")
            p.add_argument("--max-daily-loss", type=float, default=50, help="USDC; stop trading for the UTC day")
            p.add_argument("--max-orders-per-day", type=int, default=150)
            p.add_argument("--kill-file", default="STOP", help="if this file exists, place no new orders")
        p.set_defaults(func=lambda a, n=name: cmd_live(a, n))

    a = ap.parse_args(argv)
    a.func(a)


if __name__ == "__main__":
    main()
