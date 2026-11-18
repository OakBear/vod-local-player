#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""huanYuan (source-replacement) backend for <user> @ <node>."""
"""v1.8: fast-seek (WILLNEED read-ahead + 60s write timeout), multi-speed friendly
     (TCP flow control + aggressive page-cache warming), stutter awareness
     (client telemetry A + server rebuffer-inference B), persistent progress,
     Keda SPA adapter, playback.jsonl diagnostics.
     v1.8 fixes: progress ts clamp (future client clocks), manifest cache keyed by
     mtime, /api/history marked deprecated, /api/version endpoint."""
import json, os, re, time, socket, socketserver, threading, datetime, mimetypes, logging
from logging.handlers import RotatingFileHandler
from collections import deque
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import unquote, parse_qs, quote

ROOT    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CATALOG = os.path.join(ROOT, 'backend', 'catalog.json')
PORT    = int(os.environ.get('VOD_PORT', '18080'))
HOST    = '127.0.0.1'
VERSION = '1.8'
STARTED_AT = int(time.time())

COURSE_RE = re.compile(r'^/api/course/(\d+)$')
MEDIA_RE  = re.compile(r'^/media/(\d+)/(videos|subtitles|audio|slides)/(.+)$')
SLIDES_RE = re.compile(r'^/api/slides/(\d+)/([A-Za-z0-9_.-]+)$')
WARM_RE   = re.compile(r'^/api/warm/(\d+)/(.+)$')
CTYPE = {'videos': 'video/mp4', 'subtitles': 'application/x-subrip; charset=utf-8', 'audio': 'audio/mp4',
         'slides': 'image/jpeg'}
FRONTEND = os.path.join(ROOT, 'frontend')   # 用户从 Windows 上传的前端静态文件
EXT_CT = {'.html':'text/html; charset=utf-8', '.htm':'text/html; charset=utf-8',
          '.js':'text/javascript; charset=utf-8', '.mjs':'text/javascript; charset=utf-8',
          '.css':'text/css; charset=utf-8', '.json':'application/json; charset=utf-8',
          '.png':'image/png', '.jpg':'image/jpeg', '.jpeg':'image/jpeg', '.gif':'image/gif',
          '.svg':'image/svg+xml', '.ico':'image/x-icon', '.webp':'image/webp',
          '.woff':'font/woff', '.woff2':'font/woff2', '.ttf':'font/ttf', '.eot':'application/octet-stream',
          '.mp4':'video/mp4', '.m4a':'audio/mp4', '.txt':'text/plain; charset=utf-8', '.map':'application/json'}
FADV_SEQ = 2          # POSIX_FADV_SEQUENTIAL
FADV_WILLNEED = 3     # POSIX_FADV_WILLNEED (eager prefetch into page cache)
LOOKAHEAD = 64 * 1024 * 1024   # warm 64MB around a seek point
WRITE_TIMEOUT = 60.0   # a full browser buffer can legitimately stop reading for a while

_diag = logging.getLogger('vod.playback')
_diag.setLevel(logging.INFO)
_handler = RotatingFileHandler(os.path.join(ROOT, 'backend', 'playback.jsonl'), maxBytes=10*1024*1024, backupCount=5, encoding='utf-8', delay=True)
_handler.setFormatter(logging.Formatter('%(message)s'))
_diag.addHandler(_handler)
def _log_event(kind, **values):
    try:
        values.update(record=kind, ts=time.time())
        _diag.info(json.dumps(values, ensure_ascii=False, allow_nan=False))
    except (ValueError, TypeError, OSError):
        pass

# ---------------- traffic / speed accounting ----------------
_lock = threading.Lock()
_total_bytes = 0
_total_reqs  = 0
_active      = 0
_seek_count  = 0      # partial-range (seek) requests served
_aborted     = 0      # requests cut off mid-stream (rapid drag)
_samples     = deque()
_WINDOW      = 5.0

# ---------------- watch history ----------------
_watch  = {}
_watch_order = []
_manifest_cache = {}

# ---------------- playback progress (persistent, survives restart) ----------------
PROG_FILE = os.path.join(ROOT, 'backend', 'progress.json')
_prog = {'version': 1, 'last': None, 'sessions': {}, 'updated': 0}
_prog_loaded = False

def _sanitize_ts(ts, now_ms=None):
    """客户端时钟可能超前（实测出现过 +9 天的 ts，会把 last 钉死、后续上报全被当成乱序丢弃）。
    合理窗口是 [0, now+60s]；超出的夹到 now，单位明显是秒的（<1e11）按秒处理。"""
    try: ts = int(ts)
    except (TypeError, ValueError): return 0
    if ts <= 0: return 0
    if ts < 100000000000: ts *= 1000          # 秒级时间戳
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    # 5s 内的轻微超前照收（正常时钟抖动）；明显超前的夹到 now —— 不能夹成 now+N，
    # 否则接下来 N 秒内的正常上报又会被当成「旧上报」丢掉
    return ts if ts <= now_ms + 5000 else now_ms

def _load_prog():
    global _prog, _prog_loaded
    if _prog_loaded: return _prog
    _prog_loaded = True
    try:
        with open(PROG_FILE, encoding='utf-8') as f: d = json.load(f)
        if isinstance(d, dict):
            now_ms = int(time.time() * 1000)
            last = d.get('last')
            if isinstance(last, dict): last['ts'] = _sanitize_ts(last.get('ts'), now_ms)
            _prog['last'] = last
            s = d.get('sessions') or {}
            s = s if isinstance(s, dict) else {}
            for v in s.values():
                if isinstance(v, dict): v['ts'] = _sanitize_ts(v.get('ts'), now_ms)
            _prog['sessions'] = s
            _prog['updated'] = d.get('updated') or 0
    except Exception:
        pass
    return _prog

def _save_prog():
    tmp = PROG_FILE + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(_prog, f, ensure_ascii=False, indent=1)
        os.replace(tmp, PROG_FILE)
        return True
    except Exception:
        return False

def _merge_prog(payload):
    """payload 与前端 localStorage 同构：{teclId,sid,t,ts,main,pip,audio,rate,subs}"""
    _load_prog()
    if not isinstance(payload, dict): return False
    try:
        tecl, sid = payload.get('teclId'), payload.get('sid')
        if tecl is None or sid is None: return False
        st = {'teclId': tecl, 'sid': sid,
              't': max(0, int(round(float(payload.get('t') or 0)))),
              'ts': _sanitize_ts(payload.get('ts') or int(time.time() * 1000)),
              'rate': payload.get('rate'), 'subs': payload.get('subs'),
              'main': payload.get('main'), 'pip': payload.get('pip'), 'audio': payload.get('audio')}
        key = '%s:%s' % (tecl, sid)
        with _lock:
            cur = _prog['sessions'].get(key)
            if cur and cur.get('ts', 0) > st['ts']: return False   # 旧上报，丢弃
            _prog['sessions'][key] = {'t': st['t'], 'ts': st['ts']}
            if (_prog['last'] or {}).get('ts', 0) <= st['ts']: _prog['last'] = st
            if len(_prog['sessions']) > 800:   # 只保留最近 800 节
                keep = sorted(_prog['sessions'].items(), key=lambda kv: kv[1].get('ts', 0), reverse=True)[:800]
                _prog['sessions'] = dict(keep)
            _prog['updated'] = int(time.time() * 1000)
            _save_prog()
        return True
    except Exception:
        return False

def _prog_view():
    _load_prog()
    with _lock:
        return {'version': 1, 'last': _prog['last'],
                'sessions': dict(_prog['sessions']), 'updated': _prog['updated']}

# ---------------- stutter awareness ----------------
_stutter = {}
REBUF_GAP = 5 * 1024 * 1024
_warmed = set()      # absolute paths with a warm in progress or done
_WARM_CHUNK = 8 * 1024 * 1024

def _warm(path):
    try:
        with open(path, 'rb') as f:
            while f.read(_WARM_CHUNK): pass   # read whole file into page cache
    except Exception: pass

def ensure_warm(path):
    with _lock:
        if path in _warmed: return False
        _warmed.add(path)
    threading.Thread(target=_warm, args=(path,), daemon=True).start()
    return True


def _human(n):
    for u in ('B','KB','MB','GB','TB'):
        if n < 1024 or u == 'TB': return '%.1f %s' % (n, u)
        n /= 1024.0

def _record_served(n):
    global _total_bytes
    now = time.monotonic()
    with _lock:
        _total_bytes += n
        _samples.append((now, _total_bytes))
        while _samples and now - _samples[0][0] > _WINDOW: _samples.popleft()

def _record_req():
    global _total_reqs
    with _lock: _total_reqs += 1

def _bump_active(d):
    global _active
    with _lock: _active = max(0, _active + d)

def _bump_seek():
    global _seek_count
    with _lock: _seek_count += 1

def _bump_aborted():
    global _aborted
    with _lock: _aborted += 1

def _speed():
    now = time.monotonic()
    with _lock:
        while _samples and now - _samples[0][0] > _WINDOW: _samples.popleft()
        if len(_samples) < 2: return 0.0
        t0, b0 = _samples[0]; t1, b1 = _samples[-1]
        dt = t1 - t0
        return (b1 - b0) / dt if dt > 0 else 0.0

def _stats():
    with _lock: tb, tr, ac, sk, ab, wf = _total_bytes, _total_reqs, _active, _seek_count, _aborted, len(_warmed)
    sp = _speed()
    return {'now': int(time.time()), 'active_connections': ac,
            'total_bytes_served': tb, 'total_bytes_human': _human(tb),
            'total_requests': tr, 'seek_requests': sk, 'aborted_requests': ab, 'warmed_files': wf,
            'speed_bps': int(sp), 'speed_human': _human(sp) + '/s', 'window_s': _WINDOW}

def _duration_for(tecl_id, fname):
    """manifest 缓存按 (mtime, size) 失效：增量下载改写 manifest 后不必重启进程。"""
    d = course_dir(tecl_id)
    mfp = os.path.join(d, 'manifest.json') if d else None
    stamp = None
    if mfp and os.path.isfile(mfp):
        try:
            s = os.stat(mfp); stamp = (s.st_mtime_ns, s.st_size)
        except OSError:
            stamp = None
    cached = _manifest_cache.get(tecl_id)
    if not cached or cached[0] != stamp:
        m = None
        if stamp:
            try:
                with open(mfp, encoding='utf-8') as f: m = json.load(f)
            except Exception:
                m = None
        cached = (stamp, m)
        _manifest_cache[tecl_id] = cached
    m = cached[1]
    if not m: return None
    for s in m.get('sessions', []):
        for c in s.get('channels', []):
            if os.path.basename(c.get('file', '')) == fname: return c.get('duration_s')
    return None

def _record_watch(tecl_id, fname, end, size):
    key = (str(tecl_id), fname)
    dur = _duration_for(tecl_id, fname)
    pos = int(end * dur / size) if (size and dur) else 0
    with _lock:
        if key not in _watch: _watch_order.append(key)
        prev = _watch.get(key)
        if prev and prev['end'] > end: end = prev['end']
        _watch[key] = {'teclId': int(tecl_id), 'file': fname, 'end': end, 'size': size,
                       'duration_s': dur, 'position_s': pos, 'ts': int(time.time())}

def _history(limit=20):
    with _lock:
        keys = list(_watch_order); recs = [dict(_watch[k]) for k in keys if k in _watch]
    recs.sort(key=lambda r: r['ts'], reverse=True)
    # deprecated: position 是按 Range 的 end 反推的，浏览器一开口就是「到文件尾」，所以数值偏大，
    # 不能当续播依据。续播请看 /api/progress。
    return {'deprecated': True,
            'note': 'byte-range inference, not a real playhead; use /api/progress instead',
            'last': (recs[0] if recs else None), 'recent': recs[:limit], 'total_unique': len(recs)}

def _infer_rebuffer(tecl_id, fname, start, end):
    key = (str(tecl_id), fname)
    with _lock:
        rec = _stutter.get(key)
        if rec is None:
            rec = {'teclId': int(tecl_id), 'file': fname, 'max_end': 0,
                  'rebuffer_count': 0, 'client_wait_count': 0, 'client_stall_count': 0,
                  'last_buffered_depth_s': None, 'last_readystate': None,
                  'last_event': None, 'ts': 0, 'episodes': []}
            _stutter[key] = rec
        # start>0：从头重看（用户点回片头）不是 rebuffer，否则每次重头看都会虚增计数
        if rec['max_end'] > 0 and start > 0 and (rec['max_end'] - start) > REBUF_GAP and end >= start:
            rec['rebuffer_count'] += 1
            rec['episodes'].append({'ts': int(time.time()), 'type': 'rebuffer', 'from': start, 'was': rec['max_end']})
            rec['episodes'] = rec['episodes'][-50:]
        if end > rec['max_end']: rec['max_end'] = end
        rec['ts'] = int(time.time())

def _telemetry(tecl_id, fname, payload):
    key = (str(tecl_id), fname)
    ev = str(payload.get('event', ''))
    with _lock:
        rec = _stutter.get(key)
        if rec is None:
            rec = {'teclId': int(tecl_id), 'file': fname, 'max_end': 0,
                  'rebuffer_count': 0, 'client_wait_count': 0, 'client_stall_count': 0,
                  'last_buffered_depth_s': None, 'last_readystate': None,
                  'last_event': None, 'ts': 0, 'episodes': []}
            _stutter[key] = rec
        if payload.get('buffered_depth_s') is not None: rec['last_buffered_depth_s'] = float(payload['buffered_depth_s'])
        if payload.get('readystate') is not None: rec['last_readystate'] = int(payload['readystate'])
        if ev: rec['last_event'] = ev
        if ev == 'waiting': rec['client_wait_count'] += 1
        elif ev == 'stalled': rec['client_stall_count'] += 1
        if ev in ('waiting', 'stalled'):
            rec['episodes'].append({'ts': int(time.time()), 'type': ev, 'buffered_depth_s': rec['last_buffered_depth_s']})
            rec['episodes'] = rec['episodes'][-50:]
        rec['ts'] = int(time.time())
        return rec

def _stutter_view(limit=30):
    with _lock: recs = [dict(r) for r in _stutter.values()]
    recs.sort(key=lambda r: r['ts'], reverse=True)
    for r in recs: r['episodes'] = r.get('episodes', [])[-limit:]
    tot_rebuf = sum(r['rebuffer_count'] for r in recs)
    tot_wait  = sum(r['client_wait_count'] for r in recs)
    tot_stall = sum(r['client_stall_count'] for r in recs)
    return {'aware_of_stutter': (tot_rebuf + tot_wait + tot_stall > 0),
            'total_rebuffer_inferred': tot_rebuf,
            'total_client_waiting': tot_wait,
            'total_client_stalled': tot_stall,
            'streams': recs[:limit]}

# ---------------- 科达原版 SPA 适配 (rigorous replication of original-vod-prototype/server.py) ----------------
UI_PREFIX  = '/jy-application-resourcemanage-ui/'
UI_DIR     = os.path.join(ROOT, 'frontend', 'original-vod-prototype')
API_PREFIX = '/jy-application-resourcemanage'   # SPA config BASE_URL
BACKEND    = 'http://127.0.0.1:%d' % PORT
def keda_wrap(data): return {'code':None,'data':data,'dataOk':True,'message':None,'ok':True,'status':200}

def _courses_info():
    out = []
    for c in load_catalog():
        d = os.path.join(ROOT, c['dir'])
        info = {'teclId': c['teclId'], 'name': c['name'], 'canvasId': c.get('canvasId'), 'dir': c['dir']}
        mfp = os.path.join(d, 'manifest.json')
        if os.path.isfile(mfp):
            m = json.load(open(mfp, encoding='utf-8'))
            info['course'] = m.get('course'); info['status'] = 'ready'
            info['sessions'] = len(m.get('sessions', []))
            info['videos'] = sum(len(s.get('channels', [])) for s in m.get('sessions', []))
        else:
            info['status'] = 'downloading'; info['sessions'] = 0; info['videos'] = 0
        out.append(info)
    return out

def keda_courses(): return _courses_info()
def keda_course(cid):
    for c in keda_courses():
        if str(c['teclId']) == str(cid): return c
    return None
def keda_manifest(cid):
    d = course_dir(str(cid))
    mfp = os.path.join(d, 'manifest.json') if d else None
    return json.load(open(mfp, encoding='utf-8')) if (mfp and os.path.isfile(mfp)) else {'sessions': []}
def keda_session_for(sid):
    for c in keda_courses():
        if c.get('status') != 'ready': continue
        try:
            for s in keda_manifest(c['teclId']).get('sessions', []):
                if str(s['id']) == str(sid): return c, s
        except Exception: pass
    return None, None
def keda_nodes(c, s):
    d = course_dir(str(c['teclId']))
    nj = os.path.join(d, 'subtitles', s['label'] + '.nodes.json') if d else None
    try: return json.load(open(nj, encoding='utf-8'))
    except Exception: return {}
def keda_timestamp(s):
    try: return int(datetime.datetime.strptime(s, '%Y-%m-%d %H:%M:%S').timestamp()*1000)
    except Exception: return 0
def keda_subtitle_entries(c, s):
    d = course_dir(str(c['teclId']))
    srt = os.path.join(d, 'subtitles', s['label'] + '.srt') if d else None
    if not (srt and os.path.isfile(srt)): return []
    try: text = open(srt, encoding='utf-8-sig').read()
    except Exception: return []
    pattern = re.compile(r'(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})[^\n]*\n([^\n]+)')
    result = []
    for n, m in enumerate(pattern.finditer(text.replace('\r\n', '\n'))):
        v = [int(x) for x in m.groups()[:8]]
        bg = ((v[0]*60+v[1])*60+v[2])*1000+v[3]; ed = ((v[4]*60+v[5])*60+v[6])*1000+v[7]
        result.append({'id': str(n), 'bg': bg, 'ed': ed, 'courId': s['id'], 'courseId': s['id'], 'res': m.group(9), 'zh': m.group(9), 'tenantOrgCode': 'RBAC', 'transferType': 1})
    return result

def load_catalog():
    with open(CATALOG, encoding='utf-8') as f: return json.load(f)

def course_dir(tecl_id):
    for c in load_catalog():
        if str(c['teclId']) == str(tecl_id): return os.path.join(ROOT, c['dir'])
    return None

class H(BaseHTTPRequestHandler):
    server_version = 'HuanYuanBackend/' + VERSION
    protocol_version = 'HTTP/1.1'
    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Headers', 'Range, Content-Type')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Expose-Headers', 'Content-Range, Accept-Ranges')
    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(b)))
        self._cors(); self.end_headers()
        self.wfile.write(b)
    def _file(self, path, kind, tecl_id=None, fname=None):
        if not path or not os.path.isfile(path):
            self._json({'error': 'not found', 'path': str(path)}, 404); return
        size = os.path.getsize(path)
        rng = self.headers.get('Range')
        partial = bool(rng) and kind == 'videos'
        start, end = 0, size - 1
        if partial:
            m = re.match(r'bytes=(\d*)-(\d*)', rng)
            if m:
                if m.group(1): start = int(m.group(1))
                if m.group(2): end = int(m.group(2))
                start = max(0, min(start, size - 1))
                end   = min(end, size - 1)
        length = end - start + 1 if partial else size
        self.send_response(206 if partial else 200)
        self.send_header('Content-Type', CTYPE[kind])
        self.send_header('Accept-Ranges', 'bytes')
        if partial: self.send_header('Content-Range', 'bytes %d-%d/%d' % (start, end, size))
        self.send_header('Content-Length', str(length))
        self._cors(); self.end_headers()
        if kind == 'videos' and tecl_id:
            _record_watch(tecl_id, fname, end, size)
            _infer_rebuffer(tecl_id, fname, start, end)
        if kind == 'videos': ensure_warm(path)   # first play auto-warms the whole file into page cache
        with open(path, 'rb') as f:
            if partial:
                f.seek(start)
                _bump_seek()
                # fast-seek: warm the seek point + 64MB ahead into page cache
                try: os.posix_fadvise(f.fileno(), start, min(LOOKAHEAD, size - start), FADV_WILLNEED)
                except Exception: pass
            else:
                try: os.posix_fadvise(f.fileno(), 0, length, FADV_SEQ)
                except Exception: pass
            # prompt abort: a cut-off request (rapid drag) stops after WRITE_TIMEOUT
            try: self.connection.settimeout(WRITE_TIMEOUT)
            except Exception: pass
            remaining = length
            sent = 0
            report_bytes = 0
            started = report_at = time.monotonic()
            outcome = 'complete'
            try:
                while remaining > 0:
                    chunk = f.read(min(1024*1024, remaining))
                    if not chunk:
                        outcome = 'short_read'
                        self.close_connection = True
                        break
                    self.wfile.write(chunk)
                    _record_served(len(chunk))
                    sent += len(chunk)
                    remaining -= len(chunk)
                    now = time.monotonic()
                    if kind == 'videos' and now - report_at >= 15:
                        _log_event('transfer_window', teclId=tecl_id, file=fname, seconds=round(now-report_at, 3), bytes=sent-report_bytes, bytes_per_second=round((sent-report_bytes)/(now-report_at)))
                        report_at, report_bytes = now, sent
            except (BrokenPipeError, ConnectionResetError, socket.timeout):
                _bump_aborted()   # client cut us off (seeked again) - clean shutdown
                outcome = 'interrupted'
                self.close_connection = True
            finally:
                if kind == 'videos':
                    elapsed = time.monotonic()-started
                    _log_event('transfer_end', teclId=tecl_id, file=fname, range_start=start, range_end=end, bytes=sent, seconds=round(elapsed, 3), bytes_per_second=round(sent/elapsed) if elapsed else 0, outcome=outcome if remaining else 'complete')
                try: self.connection.settimeout(None)
                except Exception: pass
    def _static(self, path):
        rel = path.lstrip('/')
        if rel == '': rel = 'index.html'
        base = os.path.normpath(FRONTEND)
        full = os.path.normpath(os.path.join(FRONTEND, rel))
        if not (full == base or full.startswith(base + os.sep)):
            self._json({'error': 'forbidden'}, 403); return
        if os.path.isdir(full): full = os.path.join(full, 'index.html')
        if os.path.isfile(full):
            ext = os.path.splitext(full)[1].lower()
            self._send_static(full, EXT_CT.get(ext, 'application/octet-stream')); return
        if '.' not in os.path.basename(rel):
            idx = os.path.join(FRONTEND, 'index.html')
            if os.path.isfile(idx):
                self._send_static(idx, 'text/html; charset=utf-8'); return
        self._json({'error': 'not found', 'path': path}, 404)
    def _send_static(self, full, ct):
        size = os.path.getsize(full)
        self.send_response(200)
        self.send_header('Content-Type', ct)
        self.send_header('Content-Length', str(size))
        self.send_header('Cache-Control', 'no-cache')
        self._cors(); self.end_headers()
        with open(full, 'rb') as f:
            self.wfile.write(f.read())
    def _keda_static(self, rel):
        if rel in ('', '/'): rel = 'index.html'
        f = os.path.normpath(os.path.join(UI_DIR, rel))
        base = os.path.normpath(UI_DIR)
        if not (f == base or f.startswith(base + os.sep)):
            self._json({'error': 'forbidden'}, 403); return
        if os.path.isdir(f): f = os.path.join(f, 'index.html')
        if os.path.isfile(f):
            ext = os.path.splitext(f)[1].lower()
            ct = EXT_CT.get(ext) or mimetypes.guess_type(f)[0] or 'application/octet-stream'
            self._send_static(f, ct); return
        idx = os.path.join(UI_DIR, 'index.html')
        if os.path.isfile(idx): self._send_static(idx, 'text/html; charset=utf-8'); return
        self._json({'error': 'not found', 'path': rel}, 404)
    def _keda_api(self, path, q, method='GET'):
        print(method, path, flush=True)
        try:
            progress = {}
            try:
                import http.cookies
                jar = http.cookies.SimpleCookie(); jar.load(self.headers.get('Cookie', ''))
                progress = json.loads(unquote(jar['vod_progress'].value)) if 'vod_progress' in jar else {}
            except Exception: pass
            cid = q.get('teclId', q.get('teclIds', ['10730']))[0]; sid = q.get('courseId', [''])[0]
            out = {}
            if path.endswith('/v1/config/vodNmediaConfigInfo'):
                out = {'enable': True, 'hostname': '127.0.0.1', 'path': '/', 'port': str(PORT), 'scheme': 'http', 'uri': BACKEND, 'serverName': '本地视频'}
            elif path.endswith('/v1/api/dicts/typeCode/subject_source'): out = []
            elif path.endswith('/v1/app/info'): out = {}
            elif '/course-role-permission/teaching-class/resources' in path:
                out = {'courseRoleCodeList': ['student'], 'effectiveRoleCodeList': ['student'], 'identityType': 'COURSE_ROLE', 'resourceList': [{'sign': s} for s in ['p2-video-page', 'p2-video-playback-view', 'p2-video-playback-sidebar']], 'systemRoleCodeList': []}
            elif re.search(r'/v1/teachingClass/\d+/t-1$', path):
                cid2 = path.split('/')[-2]; c = keda_course(cid2) or {}
                out = {'id': int(cid2), 'teclId': int(cid2), 'subjName': c.get('name', '本地课程'), 'teclName': c.get('name', '本地课程'), 'teclCode': str(cid2), 'subjId': int(cid2), 'vodEnable': 1, 'liveEnable': 0, 'teacherList': [], 'studentList': [], 'courVodOpen': 1}
            elif '/source/vod/lastPlayInfoByTeclId' in path:
                s = keda_manifest(cid).get('sessions', [])
                saved = next((x for x in s if str(x['id']) == str(progress.get('courseId'))), None) if str(progress.get('teclId')) == str(cid) else None
                out = {'courId': (saved or s[0])['id']} if s else None
            elif '/subject_vod_list_new' in path:
                c = keda_course(cid) or {}; ss = sorted(keda_manifest(cid).get('sessions', []), key=lambda x: x.get('time') or '', reverse=True); records = []
                for i, s in enumerate(ss):
                    records.append({'id': s['id'], 'courBeginTime': s.get('time'), 'teclId': int(cid), 'subjId': int(cid), 'teclCode': str(cid), 'subjName': c.get('name'), 'teclName': c.get('name'), 'week': datetime.datetime.fromtimestamp(keda_timestamp(s.get('time', ''))/1000).isoweekday() if keda_timestamp(s.get('time', '')) else None, 'letiNumber': None, 'vodEnable': 1, 'vodStatus': 5, 'vodClickEnable': 1, 'vodDisplayStatus': 5, 'courVodOpen': 1, 'vodTransferStatus': 1, 'courTransferFlag': True, 'coursePptSliceStatus': 5})
                out = {'records': records, 'rowCount': len(records), 'pageIndex': 1, 'pageSize': 1000, 'pageCount': 1}
            elif path.endswith('/v1/course_vod_urls_new'):
                c, s = keda_session_for(sid); c = c or {}; s = s or {}; cid3 = c.get('teclId', 0); channels = s.get('channels', []); views = []
                for ch in channels:
                    name = quote(ch['file'].replace('\\', '/').rsplit('/', 1)[-1], safe='')
                    views.append({'courId': int(sid), 'message': '', 'url': f'{BACKEND}/media/{cid3}/videos/{name}', 'viewNum': ch.get('viewNum', 1), 'vodTime': ch.get('duration_s', 0), 'vodId': None})
                name2 = c.get('name', '本地课程')
                out = {'id': int(sid or 0), 'teclId': cid3, 'subjId': cid3, 'courName': name2, 'subjName': name2, 'teachClassName': name2, 'teclName': name2, 'courBeginTime': keda_timestamp(s.get('time', '')), 'courEndTime': keda_timestamp(s.get('time', '')) + (channels[0].get('duration_s', 0)*1000 if channels else 0), 'courseVodViewList': views, 'vodStatus': 5, 'courVodOpen': 1, 'lastPlayTime': (int(progress.get('position_s', 0)) if str(progress.get('teclId')) == str(cid3) and str(progress.get('courseId')) == str(sid) else 0), 'tecName': '', 'tecList': [{'id': 0}], 'labelList': [], 'noteFlag': False}
            elif path.endswith('/v1/course/ai/ppt'):
                c, s = keda_session_for(sid)
                d = keda_nodes(c, s) if c else {}
                out = {'docList': d.get('slides', []), 'dupKeywordList': []}
            elif '/course/ai/textSummary/' in path:
                sid2 = path.rsplit('/', 1)[-1]; c, s = keda_session_for(sid2)
                d = keda_nodes(c, s) if c else {}
                out = {'summary': d.get('summary'), 'mindmap': d.get('mindmap')}
            elif path.endswith('/v1/data/getHeatmap'): out = []
            elif path.endswith('/v1/config/styles'): out = []
            elif path.endswith('/v1/config/style'): out = [{'sypaValue': '{"type":0}'}]
            elif path.endswith('/v1/config/keyValues'): out = {}
            elif path.endswith('/v1/api/dicts/typeCodes'): out = {'vod_display_status': []}
            elif path.endswith('/v1/message/stat/unRead'): out = 0
            elif path.endswith('/v1/web/login/acknowledge'): out = True
            elif path.endswith('/v1/campus/get/hasDomainName'): out = False
            elif '/v1/tecl-ai-show-config/' in path: out = {'configList': [{'functionCode': code, 'showFlag': True} for code in ['ppt', 'offline_voice_transfer', 'summary', 'knowledge_point_mind_map']]}
            elif path.endswith('/v1/course/ai/getAiFunctionShow'): out = {'courseSummaryShow': 1, 'pptShow': 1, 'transferShow': 1, 'translateShow': 0}
            elif path.endswith('/v1/course_vod_subtitle_with_keyword'):
                c, s = keda_session_for(sid); out = {'courseTransferEsDtoList': keda_subtitle_entries(c, s) if c else [], 'keywordDto': None}
            elif '/course/ai/translate/' in path:
                sid3 = path.split('/translate/')[-1]; c, s = keda_session_for(sid3); subs = keda_subtitle_entries(c, s) if c else []; out = {'beforeAssemblyList': subs, 'afterAssemblyList': subs}
            elif '/course/ai/transcript/optimize/tasks' in path: out = []
            elif '/getCollection' in path: out = False
            elif path.endswith('/v1/course/question') or path.endswith('/v1/course/note/getList'): out = {'list': [], 'records': [], 'total': 0}
            else: out = {}
            self._json(keda_wrap(out))
        except Exception as e:
            print('ADAPTER ERROR', path, type(e).__name__, str(e)[:150], flush=True)
            self._json(keda_wrap(None))
    def do_OPTIONS(self):
        self.send_response(204); self._cors()
        self.send_header('Content-Length', '0'); self.end_headers()
    def do_GET(self):
        _bump_active(1); _record_req()
        try: self._do_get()
        except (BrokenPipeError, ConnectionResetError): pass
        finally: _bump_active(-1)
    def _do_get(self):
        path = unquote(self.path.split('?')[0])
        qs = self.path.split('?')[1] if '?' in self.path else ''
        q = parse_qs(qs)
        if path in ('/', ''):
            self._static('/index.html'); return
        if path == '/api/version':
            self._json({'version': VERSION, 'pid': os.getpid(), 'started': STARTED_AT,
                        'write_timeout': WRITE_TIMEOUT, 'lookahead_mb': LOOKAHEAD // (1024*1024),
                        'root': ROOT}); return
        if path.startswith(UI_PREFIX):
            self._keda_static(path[len(UI_PREFIX):]); return
        if path.startswith(API_PREFIX):
            self._keda_api(path, q, 'GET'); return
        if path in ('/index.json', '/api/courses'):
            self._json({'source': 'head2 huanYuan backend', 'host': HOST, 'port': PORT, 'courses': _courses_info()}); return
        if path == '/api/stats': self._json(_stats()); return
        if path == '/api/history': self._json(_history()); return
        if path == '/api/progress': self._json(_prog_view()); return
        if path == '/api/stutter': self._json(_stutter_view()); return
        m = WARM_RE.match(path)
        if m:
            tecl, fname = m.group(1), m.group(2)
            d = course_dir(tecl)
            p = os.path.join(d, 'videos', fname) if d else None
            if p and os.path.isfile(p):
                started = ensure_warm(p)
                self._json({'warming': True, 'started': started, 'file': fname, 'warmed_files': len(_warmed)}); return
            self._json({'error': 'not found', 'file': fname}, 404); return

        m = COURSE_RE.match(path)
        if m:
            d = course_dir(m.group(1))
            mfp = os.path.join(d, 'manifest.json') if d else None
            if mfp and os.path.isfile(mfp):
                self._json(json.load(open(mfp, encoding='utf-8'))); return
            self._json({'error': 'no manifest yet (still downloading?)'}, 404); return
        m = SLIDES_RE.match(path)
        if m:
            tecl, label = m.group(1), m.group(2)
            d = course_dir(tecl)
            if not d: self._json({'error': 'unknown teclId'}, 404); return
            idx = os.path.join(d, 'slides', label, 'index.json')
            if not os.path.isfile(idx):
                self._json({'error': 'no slides', 'label': label}, 404); return
            self._file(idx, 'slides') if False else None
            try:
                with open(idx, 'rb') as f: body = f.read()
            except OSError as e:
                self._json({'error': str(e)}, 500); return
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-cache')
            self._cors(); self.end_headers(); self.wfile.write(body)
            return

        m = MEDIA_RE.match(path)
        if m:
            tecl, kind, fname = m.group(1), m.group(2), m.group(3)
            d = course_dir(tecl)
            if not d: self._json({'error': 'unknown teclId'}, 404); return
            self._file(os.path.join(d, kind, fname), kind, tecl, fname); return
        self._static(path)
    def do_POST(self):
        path = unquote(self.path.split('?')[0])
        if path == '/api/progress':
            n = int(self.headers.get('Content-Length', 0) or 0)
            body = self.rfile.read(n) if n else b''
            try: payload = json.loads(body.decode('utf-8'))
            except Exception: payload = None
            ok = _merge_prog(payload)
            self._json({'ok': ok, 'saved': ok}); return
        if path == '/api/telemetry':
            n = int(self.headers.get('Content-Length', 0) or 0)
            body = self.rfile.read(n) if n else b''
            try: payload = json.loads(body.decode('utf-8'))
            except Exception: payload = {}
            if not isinstance(payload, dict) or not str(payload.get('teclId', '')).isdigit() or not isinstance(payload.get('file'), str):
                self._json({'error': 'invalid telemetry'}, 400); return
            tecl = str(payload.get('teclId', '?')); fname = payload.get('file', '?')
            allowed = ('teclId','file','event','position_s','rate','buffered_depth_s','readystate','paused','seeking','visible','total_frames','dropped_frames','interval_s','media_advanced_s','observed_rate','frame_delta','dropped_delta','width','height','error_code',
                       # 首帧耗时埋点（frontend/index.html 的 timing 事件）
                       'select_ms','meta_ms','canplay_ms','defer_ms','warmed','main_ch','pip_ch')
            if str(payload.get('event', '')) == 'timing':
                # 首帧耗时只入日志，不进 _stutter（否则每条 session 都会在 /api/stutter 里伪装成一条流）
                _log_event('player', **{k: payload[k] for k in allowed if k in payload})
                self._json({'ok': True, 'recorded': True}); return
            try: rec = _telemetry(tecl, fname, payload)
            except (ValueError, TypeError):
                self._json({'error': 'invalid telemetry'}, 400); return
            _log_event('player', **{k: payload[k] for k in allowed if k in payload})
            self._json({'ok': True, 'recorded': True,
                       'buffered_depth_s': rec['last_buffered_depth_s'],
                       'rebuffer_count': rec['rebuffer_count'],
                       'client_wait_count': rec['client_wait_count'],
                       'client_stall_count': rec['client_stall_count']}); return
        if path.startswith(API_PREFIX):
            self._keda_api(path, parse_qs(self.path.split('?')[1] if '?' in self.path else ''), 'POST'); return
        self._json({'error': 'not found'}, 404)
    def log_message(self, fmt, *args): pass

class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

if __name__ == '__main__':
    print('huanYuan backend v%s on http://%s:%d  (root=%s)' % (VERSION, HOST, PORT, ROOT), flush=True)
    print('  /            player page (frontend/index.html)', flush=True)
    print('  /api/version   build/pid/timeout introspection', flush=True)
    print('  /api/stats   speed + traffic + seek/abort counters', flush=True)
    print('  /api/history DEPRECATED byte-range guess (use /api/progress)', flush=True)
    print('  /api/stutter stutter awareness (A client + B server)', flush=True)
    print('  /api/telemetry (POST) player buffer state + frontend timing', flush=True)
    print('  /api/warm      pre-warm a whole file into page cache (call on selection)', flush=True)
    print('  fast-seek: WILLNEED %dMB read-ahead + %gs write timeout' % (LOOKAHEAD // (1024*1024), WRITE_TIMEOUT), flush=True)
    print('  multi-speed: TCP flow control + aggressive page-cache warming', flush=True)
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
