import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import notify


def write(p, text):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_message_counts_and_verdicts(tmp_path):
    write(tmp_path / "real/latency-test-c.md", "# 检验 C\n\n| L | 笔数 |\n|---:|---|\n"
          "| 0.3 秒 | 123 | 60.0% | 0.580 | +2.10¢ | 0.1000 | 90 |\n\n检验 C（前 1,500 笔）：目前 123 笔，不到 1,500 笔，不判定。\n")
    write(tmp_path / "real/latency-test-d.md", "# 检验 D\n\n录制段 1 个，成交 40 笔。\n")
    write(tmp_path / "real/latency-test-d.verdict.md", "检验 D（前 1,200 笔）：…：1200 笔，EV +3.00¢，p = 0.0010 → 通过\n")
    write(tmp_path / "forward/confirm.md", "# 预注册候选的前向检验：x\n\n数据：纸面交易录制，1197 个 5 分钟市场。\n"
          "- Bonferroni（整体误报率 5%，即原始 p < 0.0071）通过：0 个。\n"
          "- 精确 p 值最小的是 #80「盘口失衡」：116 笔，每份 +1.83¢，原始 p = 0.335，Bonferroni 校正后 p = 1.000。\n"
          "# #109 的前向检验（…）\n\n数据：10 个市场。\n\n成交 12 笔，胜率 8.3%，平均价 0.050，每份 +3.33¢。不到 2,000 笔，不判定。\n")
    text = notify.build(tmp_path, now=1_790_784_000)
    assert "09-30 16:00 UTC（北京 00:00）" in text
    assert "检验 C（Coinbase 3σ，0.3 秒）：目前 123 笔，不到 1,500 笔，不判定。" in text
    assert "目前（不作判定）：123 笔，胜率 60.0%，平均价 0.580，每份 +2.10¢" in text
    assert '<font color="info">检验 D（公平价筛选 θ=12¢） 已判定</font>' in text and "→ 通过" in text
    assert "7 个候选：1197 个市场，通过 0 个；最好的 #80「盘口失衡」：116 笔，每份 +1.83¢，原始 p = 0.335" in text
    assert "#109：成交 12 笔" in text and len(text.encode()) <= notify.LIMIT


def test_send_posts_markdown(monkeypatch):
    import urllib.request
    for k in ("no_proxy", "NO_PROXY"):  # the local test server must not go through a proxy
        monkeypatch.setenv(k, "127.0.0.1,localhost")
    monkeypatch.setattr(urllib.request, "_opener", None)
    got = {}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            got["path"] = self.path
            got["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"errcode":0,"errmsg":"ok"}')

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    code = notify.send("hello", f"http://127.0.0.1:{srv.server_port}/cgi-bin/webhook/send?key=abc")
    assert code == 0 and got["path"].endswith("key=abc")
    assert got["body"] == {"msgtype": "markdown", "markdown": {"content": "hello"}}
