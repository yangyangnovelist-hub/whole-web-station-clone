"""Do Polymarket's BTC Up/Down markets of different lengths that end at the same moment price
the same final price consistently?

A 5-minute and a 15-minute market ending at the same time E both pay on the same settlement
price at E, against different reference prices (each market's own opening price). If the 15m
market's reference K15 is at or above the 5m market's K5, "15m Up" can only win when "5m Up"
also wins, so buying 5m Up and 15m Down pays at least 1 whatever happens (2 when the final
price lands between the two references). When the two asks plus fees cost less than 1, that is
an arbitrage; when they cost a little more, it can still be worth it for the chance of 2.

Source: the Hugging Face dataset whodisidk/polymarket-btc-updown-exchange-data (BTC 5m / 15m /
1h, 100 ms book snapshots, 2026-05-25 .. 08-29, daily tar archives). Runs where Hugging Face
is reachable (the polymarket-kacho workflow, "mode: cross").

    python cross.py probe --workdir /tmp/x --out real/cross-probe.md    # tables, schemas, samples
"""
from __future__ import annotations

import argparse
import io
import re
import tarfile
import urllib.request
from pathlib import Path

DS = "whodisidk/polymarket-btc-updown-exchange-data"
BASE = f"https://huggingface.co/datasets/{DS}/resolve/main/"


def fetch(name, dest=None):
    req = urllib.request.Request(BASE + name, headers={"User-Agent": "research"})
    with urllib.request.urlopen(req, timeout=600) as r:
        if dest is None:
            return r.read()
        with open(dest, "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
    return dest


def archives(manifest):
    """(name, bytes) of the daily archives listed in MANIFEST.txt."""
    out = []
    for line in manifest.splitlines():
        m = re.search(r"(market_parquet_\d{4}-\d{2}-\d{2}\.tar\.gz)", line)
        size = re.findall(r"\b(\d{6,})\b", line)
        if m:
            out.append((m.group(1), int(size[-1]) if size else 0))
    return out


def describe_parquet(raw, rows=8):
    import pyarrow.parquet as pq
    pf = pq.ParquetFile(io.BytesIO(raw))
    t = pf.read_row_group(0) if pf.metadata.num_row_groups else pf.read()
    head = t.slice(0, rows).to_pandas()
    lines = [f"行数 {pf.metadata.num_rows:,}", "", "```", str(pf.schema_arrow)[:6000], "```", "", "```",
             head.to_string(max_colwidth=60)[:6000], "```"]
    df = t.to_pandas()
    for c in df.columns:  # distinct values of the small categorical columns
        if df[c].dtype == object and df[c].nunique() <= 12:
            lines.append(f"- `{c}`: {sorted(map(str, df[c].dropna().unique()))}")
    return lines


def probe(workdir, out):
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    L = [f"# 跨周期一致性：数据探查（{DS}）", ""]
    for name in ("README.md", "SCHEMA.md", "COVERAGE.md"):
        try:
            text = fetch(name).decode("utf-8", "replace")
        except Exception as e:  # keep going: the archive itself matters most
            text = f"({name} unavailable: {e!r})"
        L += [f"<details><summary>{name}</summary>", "", "```", text[:20000].replace("```", "'''"), "```", "",
              "</details>", ""]
    manifest = fetch("MANIFEST.txt").decode()
    arcs = archives(manifest)
    L += [f"{len(arcs)} 个日档：{arcs[0][0] if arcs else '-'} … {arcs[-1][0] if arcs else '-'}", ""]
    # the smallest archive from the last month keeps the download short
    last = [a for a in arcs if a[0] >= "market_parquet_2026-08"] or arcs
    name = min(last, key=lambda a: a[1] or 1 << 62)[0]
    local = fetch(name, workdir / name)
    L += [f"## {name}", ""]
    seen = set()
    with tarfile.open(local, "r:gz") as tar:
        members = [m for m in tar.getmembers() if m.isfile()]
        by_table = {}
        for m in members:
            t = re.search(r"dataset=([^/]+)", m.name)
            by_table.setdefault(t.group(1) if t else m.name.split("/")[0], []).append(m)
        for table, ms in sorted(by_table.items()):
            size = sum(m.size for m in ms)
            L += [f"### {table}：{len(ms)} 个文件，{size / 1e6:.1f} MB", ""]
            L += [f"- `{m.name}` {m.size / 1e6:.1f} MB" for m in ms[:6]]
            first = min(ms, key=lambda m: m.size if m.size > 0 else 1 << 62)
            raw = tar.extractfile(first).read()
            if first.name.endswith(".parquet") and table not in seen:
                seen.add(table)
                try:
                    L += [""] + describe_parquet(raw)
                except Exception as e:
                    L.append(f"(unreadable: {e!r})")
            elif first.name.endswith((".jsonl", ".jsonl.gz", ".json")):
                import gzip
                text = (gzip.decompress(raw) if first.name.endswith(".gz") else raw).decode("utf-8", "replace")
                L += ["", "```", "\n".join(text.splitlines()[:5])[:4000], "```"]
            L.append("")
    Path(local).unlink()
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L)[:30000])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("probe")
    p.add_argument("--workdir", default="/tmp/cross")
    p.add_argument("--out", default="real/cross-probe.md")
    a = ap.parse_args(argv)
    if a.cmd == "probe":
        probe(a.workdir, a.out)


if __name__ == "__main__":
    main()
