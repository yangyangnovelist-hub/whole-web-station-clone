"""Isolated three-route recorder for the frozen MIX Binance aggregate-trade tape."""
from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path
from typing import Any

import raw_rec


AGG_SOURCES = ("bn_spot_agg", "bn_spot_agg_2", "bn_spot_agg_3")
AGG_URL = "wss://data-stream.binance.vision/ws/btcusdt@aggTrade"


def source_specs() -> dict[str, dict[str, Any]]:
    return {
        source: {"g": "mix-agg", "url": AGG_URL, "idle": 60}
        for source in AGG_SOURCES
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("hours", type=float)
    args = parser.parse_args(argv)
    if args.hours <= 0:
        parser.error("hours must be positive")
    args.output.mkdir(parents=True, exist_ok=True)
    raw_rec.OUT = str(args.output)
    raw_rec.END = time.time() + args.hours * 3_600.0
    raw_rec.NEW = source_specs()
    raw_rec.SINKS.clear()
    asyncio.run(raw_rec.child_main("mix-agg", list(AGG_SOURCES)))


if __name__ == "__main__":
    main()
