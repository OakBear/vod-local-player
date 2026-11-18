#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""学校原版播放中心 SPA 的本地宿主（127.0.0.1:18766）。

v2：不再自己实现科达接口——全部反代到 18080（backend/server.py 的 _keda_api 是唯一实现）。
这样只有一处适配逻辑要维护；SPA 的 localStorage/书签仍留在 18766 这个源上。

  GET  /                                   → 302 到 SPA 路由
  GET  /jy-application-resourcemanage-ui/* → 本地静态（ROOT 内，防目录穿越）
  其余（含 /jy-application-resourcemanage*）→ 反代 BACKEND，响应流式转发
"""
import http.server, socketserver, mimetypes, pathlib, urllib.request, urllib.error

ROOT    = pathlib.Path(__file__).parent
BACKEND = 'http://127.0.0.1:18080'
PREFIX  = '/jy-application-resourcemanage-ui/'
CSP = ("default-src 'self' data: blob: http://127.0.0.1:18080; "
       "connect-src 'self' http://127.0.0.1:18080; media-src 'self' blob: http://127.0.0.1:18080; "
       "img-src 'self' data: blob: http://127.0.0.1:18080; script-src 'self' 'unsafe-inline' 'unsafe-eval' blob:; "
       "style-src 'self' 'unsafe-inline'; worker-src 'self' blob:")

class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

class H(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Headers', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET,POST,OPTIONS')

    def _send(self, data, ctype='application/json; charset=utf-8', status=200):
        if isinstance(data, str): data = data.encode()
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Content-Security-Policy', CSP)
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(204); self._cors()
        self.send_header('Content-Length', '0'); self.end_headers()

    def do_GET(self):
        path = self.path.split('?')[0]
        if path == '/':
            self.send_response(302)
            self.send_header('Location', PREFIX + '#/play-center?teclId=10730&target=video')
            self.send_header('Content-Length', '0'); self.end_headers(); return
        if path.startswith(PREFIX):
            self._static(path[len(PREFIX):] or 'index.html'); return
        self._proxy('GET')

    def do_POST(self):
        self._proxy('POST')

    def do_HEAD(self): self.do_GET()

    # ---------- 静态 ----------
    def _static(self, rel):
        base = ROOT.resolve()
        f = (base / rel).resolve()
        if f != base and base not in f.parents:
            self._send('forbidden', 'text/plain', 403); return
        if f.is_dir(): f = f / 'index.html'
        if not f.is_file():
            self._send('not found', 'text/plain', 404); return
        ctype = mimetypes.guess_type(f.name)[0] or 'application/octet-stream'
        self._send(f.read_bytes(), ctype)

    # ---------- 反代到 18080 ----------
    def _proxy(self, method):
        print(method, self.path, flush=True)
        body = b''
        if method in ('POST', 'PUT', 'PATCH'):
            try: n = int(self.headers.get('Content-Length') or 0)
            except ValueError: n = 0
            if n: body = self.rfile.read(n)
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in ('host', 'connection', 'content-length', 'accept-encoding')}
        req = urllib.request.Request(BACKEND + self.path, data=(body or None), headers=headers, method=method)
        try:
            resp = urllib.request.urlopen(req, timeout=30)
            status, rheaders, stream = resp.status, resp.headers, resp
        except urllib.error.HTTPError as e:
            status, rheaders, stream = e.code, e.headers, e
        except Exception as e:
            self._send('{"error":"backend unreachable: %s"}' % type(e).__name__, status=502); return
        try:
            self.send_response(status)
            for k in ('Content-Type', 'Content-Length', 'Accept-Ranges', 'Content-Range'):
                v = rheaders.get(k)
                if v: self.send_header(k, v)
            self._cors()
            self.end_headers()
            while True:                       # 流式转发，不把大响应读进内存
                chunk = stream.read(64 * 1024)
                if not chunk: break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        finally:
            try: stream.close()
            except Exception: pass

    def log_message(self, fmt, *args): pass

if __name__ == '__main__':
    print('Original VOD SPA host (proxy -> %s) on http://127.0.0.1:18766/' % BACKEND, flush=True)
    Server(('127.0.0.1', 18766), H).serve_forever()
