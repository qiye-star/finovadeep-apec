"""本地预览：静态站 + /apec-api 反代到 127.0.0.1:8840，等价于容器里的 Nginx。

用法：
  1. 另开一个终端启动对话服务：apec-chat/.venv/Scripts/python.exe -m uvicorn server:app --port 8840
  2. python dev-server.py            # 打开 http://localhost:3100/apec/
"""
import http.server
import os
import sys
import urllib.error
import urllib.request
from socketserver import ThreadingMixIn

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "finova-website")
UPSTREAM = os.environ.get("APEC_API", "http://127.0.0.1:8840")
PREFIX = "/apec-api"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 3100


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def do_GET(self):
        if self.path.startswith(PREFIX + "/"):
            return self._proxy()
        return super().do_GET()

    def do_POST(self):
        if self.path.startswith(PREFIX + "/"):
            return self._proxy()
        self.send_error(404)

    def _proxy(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        req = urllib.request.Request(
            UPSTREAM + self.path[len(PREFIX):],
            data=body,
            method=self.command,
            headers={"Content-Type": self.headers.get("Content-Type", "application/json")},
        )
        try:
            resp = urllib.request.urlopen(req, timeout=60)
        except urllib.error.HTTPError as e:
            resp = e
        except Exception as e:
            self.send_error(502, str(e))
            return

        self.send_response(resp.status)
        self.send_header("Content-Type", resp.headers.get("Content-Type", "text/plain; charset=utf-8"))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            while chunk := resp.read1(1024):
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))


class Server(ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    print(f"http://localhost:{PORT}/apec/  (API -> {UPSTREAM})", flush=True)
    Server(("127.0.0.1", PORT), Handler).serve_forever()
