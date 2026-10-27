"""HTTP wrapper do agente, para rodar dentro de um Cloudflare Container.

Endpoints:
  GET  /health  -> 200 quando pronto
  POST /run     -> executa nba-fantasy-agent.py e devolve o relatorio em JSON
  GET  /run     -> idem, para teste manual no navegador (?dry=1 nao notifica)
"""

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

APP = Path(os.getenv("AGENT_HOME") or "/app")
SCRIPT = APP / "nba-fantasy-agent.py"
REPORTS = APP / "reports"
TIMEOUT_S = 900
_lock = threading.Lock()


def latest_report():
    files = sorted(REPORTS.glob("relatorio_*.md"), key=lambda p: p.stat().st_mtime)
    return files[-1].read_text(encoding="utf-8") if files else ""


def roster_file():
    raw = os.getenv("ROSTER_CSV")
    path = APP / "roster.csv"
    if raw:
        path.write_text(raw.replace("\\n", "\n").strip() + "\n", encoding="utf-8")
    return str(path)


def run_agent(notify):
    args = [sys.executable, str(SCRIPT), "--espn", "--roster", roster_file()]
    adds = (os.getenv("ADDS_USED") or "").strip()
    if adds.isdigit():
        args += ["--adds-used", adds]
    if notify:
        args.append("--notify")
    proc = subprocess.run(args, cwd=APP, capture_output=True, text=True, timeout=TIMEOUT_S)
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "notified": notify,
        "report": latest_report(),
        "stdout_tail": proc.stdout[-4000:],
        "stderr_tail": proc.stderr[-4000:],
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/health":
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
            return
        if url.path == "/run":
            notify = parse_qs(url.query).get("dry", ["0"])[0] not in ("1", "true")
            return self._run(notify)
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if urlparse(self.path).path != "/run":
            return self._send(404, {"error": "not found"})
        self._run(True)

    def _run(self, notify):
        if not _lock.acquire(blocking=False):
            return self._send(409, {"error": "ja existe uma execucao em andamento"})
        try:
            self._send(200, run_agent(notify))
        except subprocess.TimeoutExpired:
            self._send(504, {"error": f"timeout apos {TIMEOUT_S}s"})
        except Exception as ex:
            self._send(500, {"error": str(ex)})
        finally:
            _lock.release()

    def log_message(self, fmt, *args):
        print(f"[http] {fmt % args}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    port = int(os.getenv("PORT") or 8080)
    print(f"agente ouvindo em 0.0.0.0:{port}", file=sys.stderr, flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
