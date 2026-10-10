"""Send the forward tests' status to a WeChat Work (企业微信) group robot.

    python notify.py --dry-run          # print the message
    WECOM_WEBHOOK=... python notify.py  # send it (the robot's webhook URL, or just its key)

Run by .github/workflows/polymarket-shadow.yml after every forward recording run, with the
webhook kept as the repository secret WECOM_WEBHOOK (never in the code or the logs). The message
repeats what real/latency-test-{c,d,g,i}.md and forward/confirm.md already say:
running counts are marked as not judged; a pinned verdict (real/latency-test-*.verdict.md) is
shown as such.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={key}"
LIMIT = 4000  # the robot accepts markdown up to 4096 bytes


def _lines(path):
    p = Path(path)
    return p.read_text(encoding="utf-8").splitlines() if p.exists() else []


def _first(lines, pattern):
    return next((ln.strip() for ln in lines if re.search(pattern, ln)), None)


def _section(lines, heading):
    """Lines of the '# heading...' section of a report, up to the next '# ' heading."""
    out, on = [], False
    for ln in lines:
        if ln.startswith("# "):
            on = ln.startswith(heading)
            continue
        if on:
            out.append(ln)
    return out


def _row(lines, lag="0.3 秒"):
    """'n 笔，胜率 …，每份 …' from the table row of one lag in a latency report."""
    ln = _first(lines, rf"^\| {re.escape(lag)} \|") or _first(lines, r"^\| [0-9,]+ \| [0-9.]+% \|")
    if not ln:
        return None
    cells = [c.strip() for c in ln.strip("|").split("|")]
    if cells and cells[0] == lag:
        cells = cells[1:]
    if len(cells) < 5 or cells[0] in ("0", ""):
        return None
    return f"{cells[0]} 笔，胜率 {cells[1]}，平均价 {cells[2]}，每份 {cells[3]}"


def test_line(name, path):
    """One line per preregistered test: the pinned verdict if there is one, else the count."""
    lines = _lines(path)
    if not lines:
        return f"{name}：还没有数据"
    pinned = Path(path).with_suffix(".verdict.md")
    if pinned.exists():
        v = pinned.read_text(encoding="utf-8").strip()
        colour = "info" if "→ 通过" in v else "warning"
        return f'<font color="{colour}">{name} 已判定</font>：{v}'
    count = _first(lines, r"^检验 [A-Z]（前") or ""
    count = re.sub(r"^检验 [A-Z]（前 [0-9,]+ (笔|个市场)）：", "", count)
    row = _row(lines) or _i_row(lines)
    return f"{name}：{count}" + (f"\n> 　目前（不作判定）：{row}" if row else "")


def _i_row(lines):
    """'m 个市场 n 笔，每份 …，p = …' from test I's row for its judged rule."""
    ln = _first(lines, r"^\| 反向 2 倍（检验的规则） \|")
    if not ln:
        return None
    cells = [c.strip() for c in ln.strip("|").split("|")]
    if len(cells) < 6 or cells[2] in ("0", ""):
        return None
    return f"{cells[2]} 个市场 {cells[3]} 笔，每份 {cells[4]}，p = {cells[5]}"


def forward_lines(path):
    lines = _lines(path)
    out = []
    for heading, name in (("# 预注册候选的前向检验", "7 个候选"), ("# 九月链上规律的前向检验", "#103–#108")):
        sec = _section(lines, heading)
        if not sec:
            continue
        n = _first(sec, r"^数据：")
        n = re.search(r"([0-9,]+) 个 5 分钟市场", n or "")
        ok = _first(sec, r"Bonferroni（整体误报率") or ""
        ok = re.search(r"通过：([0-9]+) 个", ok)
        best = _first(sec, r"精确 p 值最小的是") or ""
        best = re.sub(r"^- 精确 p 值最小的是 ", "最好的 ", best)
        best = re.sub(r"，Bonferroni 校正后 p = [0-9.]+。?$", "", best)
        out.append(f"{name}：{n.group(1) if n else '?'} 个市场，通过 {ok.group(1) if ok else '?'} 个；{best}")
    sec = _section(lines, "# #109 的前向检验")
    if sec:
        body = _first(sec, r"^(成交|还没有成交|前 [0-9,]+ 笔)") or "还没有数据"
        out.append(f"#109：{body}")
    return out


def build(root=HERE, now=None):
    ts = now if now is not None else time.time()
    now, bj = time.gmtime(ts), time.gmtime(ts + 8 * 3600)
    L = [f"## Polymarket 5m 前向检验 {time.strftime('%m-%d %H:%M', now)} UTC（北京 {time.strftime('%H:%M', bj)}）",
         "**预注册检验**（规则事先写死，只用新录的数据，笔数够了判定一次；纸面交易，不下单）",
         "> " + test_line("检验 C（Coinbase 3σ，0.3 秒）", Path(root) / "real/latency-test-c.md"),
         "> " + test_line("检验 D（公平价筛选 θ=12¢）", Path(root) / "real/latency-test-d.md"),
         "> " + test_line("检验 G（币安 2σ、Polymarket 还没动，0.3 秒）", Path(root) / "real/latency-test-g.md"),
         "> " + test_line("检验 I（H 每次都加、反向 2 倍，0.4 秒）", Path(root) / "real/latency-test-i.md"),
         "> " + test_line("检验 J（触发时发单、限价放宽，0.45 秒）", Path(root) / "real/latency-test-j.md"),
         "**其他前向检验**"]
    L += ["> " + ln for ln in forward_lines(Path(root) / "forward/confirm.md")]
    text = "\n".join(L)
    while len(text.encode("utf-8")) > LIMIT:
        text = text[: int(len(text) * 0.9)]
    return text


def send(text, webhook):
    """POST a markdown message to the robot; returns the robot's errcode (0 = sent)."""
    url = webhook if webhook.startswith("http") else WEBHOOK.format(key=webhook)
    body = json.dumps({"msgtype": "markdown", "markdown": {"content": text}}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    for i in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return int(json.loads(r.read().decode("utf-8")).get("errcode", -1))
        except Exception:
            time.sleep(2 ** i)
    return -1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--root", default=str(HERE))
    a = ap.parse_args(argv)
    text = build(a.root)
    print(text)
    hook = os.environ.get("WECOM_WEBHOOK", "").strip()
    if a.dry_run:
        return
    if not hook:
        print("WECOM_WEBHOOK is not set: nothing sent")
        return
    code = send(text, hook)
    print("sent" if code == 0 else f"robot returned errcode {code}")


if __name__ == "__main__":
    main()
