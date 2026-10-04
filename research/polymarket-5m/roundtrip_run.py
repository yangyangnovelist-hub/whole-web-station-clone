"""ROUNDTRIP.md, the kacho stage in one run: every variant of every round-trip family (1-7) on
kacho.io's per-second BTC 5m books, one Bonferroni selection over all of them, the Chinese report
real/roundtrip-kacho.md and the frozen list real/roundtrip-frozen.json. Paper research on market
data only, no orders.

    python roundtrip_run.py [--workers 4] [--no-verify]

What it does
- Builds the variant lists of the seven family modules exactly as they define them (nothing here
  changes a grid, a signal, an exit or the engine):
    f1 roundtrip.family1_variants      Binance momentum scalps
    f2 rt_fair.variants                fair-value gap convergence
    f3 rt_pm.family3_variants          Polymarket's own reversion
    f4 rt_pm.family4_variants          Polymarket's own momentum
    f5 rt_book.family5_variants        book imbalance
    f6 rt_factors.variants             FACTORS.md model, sold before the end
    f7 rt_book.family7_variants        time x price buckets
  Family 8 (merge / split arbitrage scan) has no variants; its kacho report is arb_scan.py's
  real/arb-scan-kacho.md, which the report links.
- Runs all of them on one roundtrip.Engine (fill_delay = 1, the selected timing) over the panel
  roundtrip.load_panel() builds from kacho + Binance 1 s klines. Family 6's predictions are attached
  first and, unless --no-verify, checked to be the walk-forward out-of-sample ones
  (rt_factors.verify_oos; the run stops if they are not).
- N = the number of variants of all seven families together (hold-to-settlement controls
  included, as every family counts them). roundtrip.select with that single N: A passes with
  mean > 0 and one-sided p < 0.05 / N; of those k, B passes with mean > 0 and p < 0.05 / k; a
  split needs >= 30 markets (roundtrip.MIN_MARKETS). Survivors of A and B are the frozen list.
- Writes the stats of every variant to SCRATCH/rt/run_all_stats.parquet (not stats_*.parquet, so
  `roundtrip.py select` does not count them twice) and compares them with the per-family stats files
  the family runs wrote (a reproducibility check; printed and reported).
- real/roundtrip-frozen.json: the survivors with full parameters (signal + exit), their A / B
  numbers and the A / B numbers of the same signal held to settlement, the selection numbers, and
  `frozen_at` (UTC time of writing). It also carries `describe`: each family's 10 best variants by A
  t (full parameters), which roundtrip_hf.py evaluates as a description only. Nothing in
  `describe` is part of any verdict.
- real/roundtrip-kacho.md (Chinese, short): N and A / B passes per family; each family's top 10 by
  A t with trades, markets, mean +- market-clustered SE, t, holding time and the unsellable share for
  A and B, beside the same signal held to settlement; a fill-delay sensitivity of each family's best
  sold-on-the-book variant (descriptive, fill_delay 0..3, never selected); the frozen list; the link
  to the arbitrage scan.

Choices where ROUNDTRIP.md is silent (conservative, written before running)
- "The same signal held to settlement" is the family's own settle control with the same signal key
  (Variant.key); for a control itself the column says so.
- The fill-delay table is a description of how the selected numbers depend on the extra row of
  delay; it uses the same variants and engine with fill_delay 0, 2, 3 and plays no part in the
  selection. Its variant per family is the best non-control variant by A t.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

import roundtrip as rt

HERE = Path(__file__).resolve().parent
REAL = HERE / "real"
OUT_MD = REAL / "roundtrip-kacho.md"
OUT_JSON = REAL / "roundtrip-frozen.json"
ARB_MD = REAL / "arb-scan-kacho.md"
STATS_ALL = rt.RT / "run_all_stats.parquet"
TOP = 10
DELAYS = (0, 1, 2, 3)

FAMILIES = (  # (code, module, function, Chinese name)
    ("f1", "roundtrip", "family1_variants", "币安动量短打"),
    ("f2", "rt_fair", "variants", "公允价缺口收敛"),
    ("f3", "rt_pm", "family3_variants", "Polymarket 自身回归"),
    ("f4", "rt_pm", "family4_variants", "Polymarket 自身动量"),
    ("f5", "rt_book", "family5_variants", "盘口失衡"),
    ("f6", "rt_factors", "variants", "多因子模型（样本外预测）"),
    ("f7", "rt_book", "family7_variants", "时段 × 价格档"),
)
STAT_COLS = ("n", "mk", "mean", "se", "sed", "t", "p", "hold", "fb")


# ----------------------------------------------------------------------------------------------
# Variants


def all_variants(panel=None, families=FAMILIES):
    """{family name: [Variant]} in FAMILIES order, plus {family name: (code, Chinese name)}."""
    import importlib
    out, names = {}, {}
    for code, mod, fn, zh in families:
        vs = getattr(importlib.import_module(mod), fn)(panel)
        if not vs:
            raise ValueError(f"{mod}.{fn} returned no variants")
        fam = {v.family for v in vs}
        if len(fam) != 1:
            raise ValueError(f"{mod}.{fn}: several family names {fam}")
        out[vs[0].family] = vs
        names[vs[0].family] = (code, zh)
    ids = [v.id for vs in out.values() for v in vs]
    if len(ids) != len(set(ids)):
        raise ValueError("variant ids are not unique across families")
    return out, names


def key_str(v):
    return json.dumps([v.family, *map(str, v.key)] if isinstance(v.key, tuple) else [v.family, str(v.key)])


def control_map(variants):
    """{variant id: id of the settle control with the same signal key} (None if the key has none)."""
    ctrl = {}
    for v in variants:
        if v.exit.kind == "settle":
            ctrl.setdefault(key_str(v), v.id)
    return {v.id: ctrl.get(key_str(v)) for v in variants}


# ----------------------------------------------------------------------------------------------
# Numbers and formatting


def _num(v):
    return rt._num(v)


def split_numbers(r, sp):
    return {c: _num(r[f"{c}_{sp}"]) for c in STAT_COLS}


def _c(v, nd=2):
    return "–" if v is None or not np.isfinite(v) else f"{100 * v:+.{nd}f}¢"


def cell(r, sp):
    """'+1.23¢ ±0.45（t +2.7）' or '–'."""
    if not r.get(f"n_{sp}"):
        return "–"
    t = r[f"t_{sp}"]
    tt = f"{t:+.1f}" if np.isfinite(t) else "–"
    return f"{100 * r[f'mean_{sp}']:+.2f}¢ ±{100 * r[f'se_{sp}']:.2f}（t {tt}）"


def nm(r, sp):
    n = r.get(f"n_{sp}") or 0
    return f"{int(n):,} / {int(r.get(f'mk_{sp}') or 0):,}"


def short_cell(r, sp):
    if r is None or not r.get(f"n_{sp}"):
        return "–"
    t = r[f"t_{sp}"]
    return f"{100 * r[f'mean_{sp}']:+.2f}¢（t {t:+.1f}）" if np.isfinite(t) else f"{100 * r[f'mean_{sp}']:+.2f}¢"


def _hold(r, sp):
    v = r.get(f"hold_{sp}")
    return "–" if v is None or not np.isfinite(v) else f"{v:.0f}"


def _pct(v):
    return "–" if v is None or not np.isfinite(v) else f"{100 * v:.0f}%"


# ----------------------------------------------------------------------------------------------
# Fill-delay sensitivity (descriptive)


def delay_table(panel, variants, sel, delays=DELAYS):
    """{family: {"id": id, delay: {"A": mean, "B": mean, "nA": n, "nB": n}}} for each family's best
    non-control variant by A t, simulated with Engine(fill_delay=delay)."""
    best = {}
    by_id = {v.id: v for vs in variants.values() for v in vs}
    for fam, g in sel[~sel["control"]].groupby("family", sort=False):
        g = g.sort_values("t_A", ascending=False, na_position="last")
        best[fam] = g.iloc[0]["id"]
    out = {fam: {"id": vid} for fam, vid in best.items()}
    for D in delays:
        eng = rt.Engine(panel, fill_delay=D)
        for fam, vid in best.items():
            v = by_id[vid]
            tr = eng.simulate(v.signal(panel), v.exit)
            st = rt.split_stats(panel, tr)
            out[fam][D] = {"A": st["mean_A"], "B": st["mean_B"], "nA": st["n_A"], "nB": st["n_B"],
                           "tA": st["t_A"], "tB": st["t_B"]}
        del eng
    return out


# ----------------------------------------------------------------------------------------------
# Outputs


def frozen_doc(sel, info, variants, names, ctrl, by_id_stats, top=TOP, extra=None):
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()

    def entry(r):
        e = {"id": r["id"], "family": r["family"], "name": r["name"], "params": json.loads(r["params"]),
             "control": bool(r["control"]), "A": split_numbers(r, "A"), "B": split_numbers(r, "B")}
        c = ctrl.get(r["id"])
        if c and c != r["id"] and c in by_id_stats:
            cr = by_id_stats[c]
            e["settle_control"] = {"id": c, "A": split_numbers(cr, "A"), "B": split_numbers(cr, "B")}
        return e

    surv = [entry(r) for r in sel[sel["pass_B"]].to_dict("records")]
    describe = {}
    for fam, g in sel.groupby("family", sort=False):
        g = g.sort_values("t_A", ascending=False, na_position="last").head(top)
        describe[fam] = [entry(r) for r in g.to_dict("records")]
    fam_rows = {fam: {"code": names[fam][0], "name_zh": names[fam][1], "variants": len(vs),
                      "controls": int(sum(v.control for v in vs)),
                      "pass_A": int(sel.loc[sel["family"] == fam, "pass_A"].sum()),
                      "pass_B": int(sel.loc[sel["family"] == fam, "pass_B"].sum())}
                for fam, vs in variants.items()}
    doc = {"frozen_at": now,
           "design": "research/polymarket-5m/ROUNDTRIP.md",
           "runner": "research/polymarket-5m/roundtrip_run.py",
           "engine": "research/polymarket-5m/roundtrip.py (Engine fill_delay=1)",
           "data": "kacho.io BTC 5m per-second books 2026-03-24..05-18 + Binance BTCUSDT 1 s klines; "
                   "A = market starts 03-24..04-25 UTC, B = 04-26..05-18",
           "timing": "decision d uses rows/klines stamped <= d-1; entry at the ask of the row stamped d+1, "
                     "exit decided at e at the bid of the row stamped e+1; both legs pay 0.07 p (1-p); >= 5 shares, "
                     "0.02..0.98; one position per variant per market; forced exit at row 285; unsellable -> settled",
           "selection": "A: mean > 0 and one-sided p < 0.05 / N (N = all variants of families 1-7); "
                        "B: of the k passing A, mean > 0 and p < 0.05 / k; >= 30 markets per split",
           **{k: _num(v) for k, v in info.items()},
           "families": fam_rows,
           "survivors": surv,
           "describe": describe,
           "describe_note": "each family's top 10 by A t (whether or not they pass), for a descriptive "
                            "evaluation only; not part of any verdict"}
    if extra:
        doc.update(extra)
    return doc


def arb_summary(path=ARB_MD):
    """(title, first paragraph under '## 结果') of the arbitrage scan's kacho report, or None."""
    p = Path(path)
    if not p.exists():
        return None
    text = p.read_text(encoding="utf-8")
    title = next((ln[2:].strip() for ln in text.splitlines() if ln.startswith("# ")), p.name)
    m = re.search(r"^## 结果\s*\n+(.+?)(?:\n\n|\Z)", text, re.M | re.S)
    return title, (m.group(1).strip() if m else None)


def report_md(sel, info, variants, names, ctrl, by_id_stats, delays, check, runtime, frozen_at, top=TOP,
              json_name=OUT_JSON.name, arb=None):
    L = ["# 不持有到结算：七个策略族在 kacho 每秒盘口上的一次性挑选（BTC 5m，3/24–5/18）", "",
         "> 设计见 [ROUNDTRIP.md](../ROUNDTRIP.md)（跑之前提交）；引擎 roundtrip.py，各族信号在 rt_fair.py、rt_pm.py、"
         "rt_book.py、rt_factors.py，本报告由 roundtrip_run.py 一次跑完全部变体后写出。只用行情数据，纸面研究，不下单。",
         "> 时序：在 d 秒决定，只用标为 ≤ d − 1 的盘口行和币安 K 线；按标为 d + 1 的行的卖一买入；在 e 秒决定卖出，"
         "按标为 e + 1 的行的买一卖出（比设计原文再晚一行）。两腿各付 0.07·p(1 − p)；两腿都要该价位 ≥ 5 份、价格 0.02–0.98；"
         "同一变体同一市场最多一笔；最晚标为 285 的行（剩 15 秒）卖出，卖不掉就按官方结算算（“卖不掉”占比）。"
         "每份盈亏 ± 按市场聚类的标准误。", "",
         f"- 分段：A = 3/24–4/25 开盘的市场（挑选），B = 4/26–5/18（确认）。",
         f"- **变体总数 N = {info['N']:,}**（七族合计，含持有到结算对照）。A 段门槛 p < 0.05 / N = {info['thr_A']:.2e} 且每份 > 0："
         f"过 A 的 k = {info['k']}；B 段门槛 p < 0.05 / k = {info['thr_B']:.2e} 且每份 > 0：**冻结 {info['k_B']} 个**"
         f"（其中持有到结算对照 {int((sel['pass_B'] & sel['control']).sum())} 个）。每段至少 {info['min_markets']} 个市场才可能通过。",
         f"- 冻结名单（完整参数）：[{json_name}]({json_name})，冻结时间 {frozen_at}。只有 Hugging Face 100 毫秒盘口上的"
         "一次复核（roundtrip_hf.py）能决定它们是否算数。",
         f"- 运行：{runtime}。"]
    if check:
        L.append(f"- 复现核对：与各族单独跑出的统计文件逐个比对 {check}。")
    L += ["", "## 各族汇总", "",
          "| 族 | 内容 | 变体数 | 其中持有到结算对照 | 过 A | 过 B（冻结） | 冻结中的对照 |", "|---|---|---:|---:|---:|---:|---:|"]
    for fam, vs in variants.items():
        g = sel[sel["family"] == fam]
        L.append(f"| {names[fam][0]} `{fam}` | {names[fam][1]} | {len(vs):,} | {int(sum(v.control for v in vs)):,} | "
                 f"{int(g['pass_A'].sum())} | {int(g['pass_B'].sum())} | {int((g['pass_B'] & g['control']).sum())} |")
    L.append(f"| 合计 | | {info['N']:,} | {int(sel['control'].sum()):,} | {info['k']} | {info['k_B']} | "
             f"{int((sel['pass_B'] & sel['control']).sum())} |")
    if arb:
        title, para = arb
        L += ["", "第 8 族（合并 / 拆分无风险套利扫描）没有变体，不计入 N；kacho 上的结果见 "
              f"[arb-scan-kacho.md](arb-scan-kacho.md)（{title}）。"]
        if para:
            L += ["", "> " + para.replace("\n", " ")]
    L += ["", "## 各族 A 段 t 值最高的 10 个（只作描述，不论是否通过）", "",
          "笔数 / 市场；每份 ± 标准误（t）；持有 = 平均持有秒数；卖不掉 = 到剩 15 秒都卖不出、按结算算的占比；"
          "“同信号持有到结算” = 同一组买入信号改为持有到官方结算（该族的对照变体）。"]
    for fam, g in sel.groupby("family", sort=False):
        g = g.sort_values("t_A", ascending=False, na_position="last").head(top)
        L += ["", f"### {names[fam][0]} {names[fam][1]}（`{fam}`，{len(variants[fam]):,} 个变体）", "",
              "| 变体 | A 笔数 / 市场 | A 每份 | A 持有 | A 卖不掉 | B 笔数 / 市场 | B 每份 | B 持有 | 同信号持有到结算 A / B | 过 A / B |",
              "|---|---:|---|---:|---:|---:|---|---:|---|:-:|"]
        for r in g.to_dict("records"):
            c = ctrl.get(r["id"])
            if r["control"]:
                cmp_ = "（本身即持有到结算）"
            elif c and c in by_id_stats:
                cr = by_id_stats[c]
                cmp_ = f"{short_cell(cr, 'A')} / {short_cell(cr, 'B')}"
            else:
                cmp_ = "–"
            flag = ("✓" if r["pass_A"] else "✗") + " / " + ("✓" if r["pass_B"] else ("✗" if r["pass_A"] else "–"))
            L.append(f"| `{r['name']}` | {nm(r, 'A')} | {cell(r, 'A')} | {_hold(r, 'A')} | {_pct(r['fb_A'])} | "
                     f"{nm(r, 'B')} | {cell(r, 'B')} | {_hold(r, 'B')} | {cmp_} | {flag} |")
    if delays:
        L += ["", "## 多等一行的代价（只作描述，不参与挑选）", "",
              "每族 A 段 t 最高的、在盘口上卖出的变体（非对照），同一信号和出场，只改成交行（买卖两腿一起）："
              "Δ = −1 即成交行 d（设计原文的时序），Δ = 0 即成交行 d + 1（本次挑选所用），Δ = +1、+2 再多等一、两行。每份 A / B：", "",
              "| 族 | 变体 | " + " | ".join(f"Δ = {D - 1:+d}（成交行 d + {D}" + ("，挑选）" if D == 1 else "）")
                                             for D in DELAYS) + " |",
              "|---|---|" + "---|" * len(DELAYS)]
        for fam, row in delays.items():
            cells = []
            for D in DELAYS:
                x = row.get(D)
                cells.append("–" if x is None else f"{_c(x['A'])} / {_c(x['B'])}")
            name = row["id"].split(":", 1)[1]
            L.append(f"| {names[fam][0]} | `{name}` | " + " | ".join(cells) + " |")
    fz = sel[sel["pass_B"]]
    L += ["", f"## 冻结名单（{len(fz)} 个）", ""]
    if not len(fz):
        L.append("没有变体同时通过 A 和 B。Hugging Face 复核脚本仍会运行，但只把各族 A 段最好的 10 个作为描述报告，不下结论。")
    else:
        L.append("每份 ± 标准误（t），笔数；完整参数见 JSON。")
        for fam, g in fz.groupby("family", sort=False):
            g = g.sort_values("t_A", ascending=False)
            L += ["", f"<details><summary>{names[fam][0]} {names[fam][1]}：{len(g)} 个"
                      f"（其中持有到结算对照 {int(g['control'].sum())} 个）</summary>", "",
                  "| 变体 | A 段 | B 段 | 同信号持有到结算 A / B |", "|---|---|---|---|"]
            for r in g.to_dict("records"):
                c = ctrl.get(r["id"])
                cmp_ = ("（对照）" if r["control"] else
                        f"{short_cell(by_id_stats[c], 'A')} / {short_cell(by_id_stats[c], 'B')}" if c in by_id_stats else "–")
                L.append(f"| `{r['name']}` | {cell(r, 'A')}，{int(r['n_A']):,} 笔 | {cell(r, 'B')}，{int(r['n_B']):,} 笔 | {cmp_} |")
            L += ["", "</details>"]
    return "\n".join(L) + "\n"


# ----------------------------------------------------------------------------------------------
# Reproducibility check against the family runs


def compare_family_files(stats, rt_dir=rt.RT):
    """Max |diff| of the A / B means and SEs against the stats_<family>.parquet files the family
    runs wrote, on the ids both have. Returns (text, details)."""
    files = sorted(Path(rt_dir).glob("stats_*.parquet"))
    det = {}
    for f in files:
        try:
            o = pd.read_parquet(f)
        except Exception as e:  # a file being written by another run
            det[f.name] = f"unreadable: {e!r}"
            continue
        m = stats.merge(o, on="id", suffixes=("", "_o"))
        if not len(m):
            continue
        d = 0.0
        for c in ("mean_A", "se_A", "mean_B", "se_B", "n_A", "n_B"):
            a, b = m[c].to_numpy(float), m[f"{c}_o"].to_numpy(float)
            both = np.isfinite(a) & np.isfinite(b)
            if (np.isfinite(a) != np.isfinite(b)).any():
                d = np.inf
            elif both.any():
                d = max(d, float(np.max(np.abs(a[both] - b[both]))))
        det[f.name] = {"ids": len(m), "of": len(o), "max_abs_diff": d}
    ok = [v for v in det.values() if isinstance(v, dict)]
    if not ok:
        return None, det
    same = all(v["max_abs_diff"] <= 1e-12 for v in ok)
    n, n_of = sum(v["ids"] for v in ok), sum(v["of"] for v in ok)
    txt = (f"{len(ok)} 个文件、{n:,} 个变体（文件里共 {n_of:,} 个），" + ("每份均值、标准误和笔数完全一致" if same else
           "有差异：" + "；".join(f"{k} 最大差 {v['max_abs_diff']:.3g}（{v['ids']}/{v['of']}）" for k, v in det.items()
                                  if isinstance(v, dict))))
    return txt, det


# ----------------------------------------------------------------------------------------------
# CLI


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--panel", default=str(rt.PANEL))
    ap.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1))
    ap.add_argument("--md", default=str(OUT_MD))
    ap.add_argument("--json", default=str(OUT_JSON))
    ap.add_argument("--stats", default=str(STATS_ALL))
    ap.add_argument("--no-verify", action="store_true", help="skip the family-6 out-of-sample check")
    ap.add_argument("--no-delays", action="store_true", help="skip the descriptive fill-delay table")
    a = ap.parse_args(argv)
    t_all = time.time()
    cpu0 = time.process_time()
    panel = rt.load_panel(a.panel)
    import rt_factors as rf
    preds = rf.load_preds()
    rf.attach_preds(panel, preds)
    if not a.no_verify:
        ver = rf.verify_oos()
        if not all(v["reproduced"] for v in ver.values()):
            raise SystemExit("family 6: stored predictions are not the walk-forward ones; refusing to run")
    variants, names = all_variants(panel)
    flat = [v for vs in variants.values() for v in vs]
    N = len(flat)
    print("variants: " + ", ".join(f"{names[f][0]} {len(vs):,}" for f, vs in variants.items()) + f"; N = {N:,}",
          flush=True)
    eng = rt.Engine(panel)
    t0 = time.time()
    stats, _ = rt.run_variants(eng, flat, workers=a.workers)
    t_run = time.time() - t0
    if len(stats) != N or stats["id"].duplicated().any():
        raise RuntimeError("stats do not have one row per variant")
    Path(a.stats).parent.mkdir(parents=True, exist_ok=True)
    stats.to_parquet(a.stats)
    print(f"ran {N:,} variants in {t_run:.0f} s with {a.workers} workers -> {a.stats}", flush=True)
    check, det = compare_family_files(stats)
    print("family files:", json.dumps(det, default=str), flush=True)
    sel, info = rt.select(stats, n_total=N)
    ctrl = control_map(flat)
    by_id_stats = {r["id"]: r for r in sel.to_dict("records")}
    delays = None
    if not a.no_delays:
        t1 = time.time()
        delays = delay_table(panel, variants, sel)
        print(f"fill-delay table in {time.time() - t1:.0f} s", flush=True)
    doc = frozen_doc(sel, info, variants, names, ctrl, by_id_stats,
                     extra={"fill_delay_descriptive": {f: {str(k): v for k, v in row.items()} for f, row in delays.items()}
                            if delays else None})
    Path(a.json).parent.mkdir(parents=True, exist_ok=True)
    Path(a.json).write_text(json.dumps(doc, ensure_ascii=False, indent=1, default=_num), encoding="utf-8")
    wall = time.time() - t_all
    cpu = time.process_time() - cpu0
    runtime = (f"{N:,} 个变体 {t_run:.0f} 秒（{a.workers} 个进程）；全程 {wall:.0f} 秒"
               f"（含读盘口面板、第 6 族样本外核对和多等一行的描述表）")
    md = report_md(sel, info, variants, names, ctrl, by_id_stats, delays, check, runtime, doc["frozen_at"],
                   json_name=Path(a.json).name, arb=arb_summary())
    Path(a.md).write_text(md, encoding="utf-8")
    print(f"N = {info['N']:,}; thr_A = {info['thr_A']:.3g}; pass A = {info['k']}; thr_B = {info['thr_B']:.3g}; "
          f"pass B = {info['k_B']} -> {a.json}, {a.md}; wall {wall:.0f} s, main-process CPU {cpu:.0f} s")
    for fam, g in sel.groupby("family", sort=False):
        print(f"  {fam}: {len(g):,} variants, pass A {int(g['pass_A'].sum())}, pass B {int(g['pass_B'].sum())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
