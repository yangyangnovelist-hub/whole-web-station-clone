"""List Hugging Face datasets that may hold Polymarket 5m data, with their files.

    python hf_survey.py --out real/hf-survey.md

Runs where Hugging Face is reachable (the polymarket-kacho workflow with
"mode: survey"). For every dataset matching the searches, plus the ones named
below, it records the last update, the file list with sizes, and the start
of the README, so the one holding September 2026 BTC 5m books can be picked.
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://huggingface.co/api/datasets"
SEARCHES = ("polymarket", "updown", "up-down", "btc-5m", "5m crypto")
NAMED = ("kachoio/polymarket-5-minute-crypto-up-down-markets", "BrockMisner/polymarket-crypto-5m-15m",
         "BrockMisner/polymarket-btc-updown", "Mindbyte-89/polymarket-btc-updown", "mingossx/polymarket-btc-updown",
         "obadiaha/polymarket-crypto-5m-15m", "SII-WANGZJ/Polymarket_data", "godss1985/Polymarket_data",
         "Ligengxin96/polymarket-predict-fun-tick-data", "AdamAtractor/btc-l2-orderbook")


def get(url, raw=False, tries=4):
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "polymarket-research/1.0"})
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
            return data.decode("utf-8", "replace") if raw else json.loads(data)
        except Exception as e:
            if k == tries - 1:
                return {"error": repr(e)} if not raw else f"(error: {e!r})"
            time.sleep(2 ** k)


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f}{unit}"
        n /= 1024
    return f"{n:.0f}PB"


def survey():
    ids = list(NAMED)
    for q in SEARCHES:
        found = get(f"{API}?search={urllib.parse.quote(q)}&limit=100&sort=lastModified&direction=-1")
        if isinstance(found, list):
            ids += [d["id"] for d in found if "id" in d]
    seen, rows = set(), []
    for ds in ids:
        if ds in seen:
            continue
        seen.add(ds)
        info = get(f"{API}/{ds}")
        tree = get(f"{API}/{ds}/tree/main?recursive=1")
        files = [f for f in tree if isinstance(f, dict) and f.get("type") == "file"] if isinstance(tree, list) else []
        readme = get(f"https://huggingface.co/datasets/{ds}/raw/main/README.md", raw=True)
        rows.append({"id": ds, "last": (info or {}).get("lastModified") if isinstance(info, dict) else None,
                     "error": info.get("error") if isinstance(info, dict) else None,
                     "files": [(f["path"], f.get("size", 0)) for f in files],
                     "readme": readme if isinstance(readme, str) else ""})
    return rows


def report(rows):
    lines = ["# Hugging Face 上的 Polymarket 数据集", "",
             "由 `hf_survey.py` 在 GitHub 的机器上生成（本环境连不上 Hugging Face）。按最后更新时间排序；"
             "找含 2026 年 9 月 BTC 5 分钟逐秒盘口的那一个。", "",
             "| 数据集 | 最后更新 | 文件数 | 总大小 |", "|---|---|---:|---:|"]
    rows = sorted(rows, key=lambda r: r["last"] or "", reverse=True)
    for r in rows:
        total = sum(s for _, s in r["files"])
        lines.append(f"| [{r['id']}](https://huggingface.co/datasets/{r['id']}) | {(r['last'] or r['error'] or '?')[:19]} | "
                     f"{len(r['files'])} | {human(total)} |")
    for r in rows:
        lines += ["", f"## {r['id']}", "", f"最后更新：{r['last']}", ""]
        for path, size in r["files"][:60]:
            lines.append(f"- `{path}` {human(size)}")
        if len(r["files"]) > 60:
            lines.append(f"- ……共 {len(r['files'])} 个文件")
        head = "\n".join(r["readme"].splitlines()[:60])
        if head:
            lines += ["", "<details><summary>README 开头</summary>", "", "```", head.replace("```", "'''"), "```", "",
                      "</details>"]
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="real/hf-survey.md")
    args = ap.parse_args(argv)
    text = report(survey())
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(text, encoding="utf-8")
    print(text[:20000])


if __name__ == "__main__":
    main()
