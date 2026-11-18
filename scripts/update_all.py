#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""增量更新本地 VOD 库：只补「本地没有的节次 / 频道 / 字幕」，已有文件一律跳过。

用法:
  python3 ~/vod-research/scripts/update_all.py              # 全部课程，缺什么补什么
  python3 ~/vod-research/scripts/update_all.py --dry-run    # 只列差异，不下载
  python3 ~/vod-research/scripts/update_all.py --tecl 10730 8537
  python3 ~/vod-research/scripts/update_all.py --refresh    # 强制先铸新 token
  python3 ~/vod-research/scripts/update_all.py --no-nodes   # 不补 PPT 节点

判定规则（全部基于「本地已有什么」）:
  - 新节次  : 远端 subject_vod_list_new 的 id 不在本地 manifest.json 里
  - 缺频道  : manifest 里该频道 file 不存在或大小为 0
  - 缺字幕  : subtitles/<label>.srt 不存在或为空
  - 缺节点  : subtitles/<label>.nodes.json 不存在 → 交给 backend/fetch_nodes.py
  - 已下完的 mp4（存在且无 .part）不会重下；未下完的 .part 继续 Range 断点续传
"""
import argparse, base64, json, os, re, subprocess, sys, time, urllib.error, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = 'https://v.sjtu.edu.cn/jy-application-resourcemanage'
CATALOG = os.path.join(ROOT, 'backend', 'catalog.json')
JWT = os.path.join(ROOT, 'blockchain-apps', 'scripts', 'jwt.txt')
MINT = os.path.join(ROOT, 'scripts', 'mint_vod_token.py')
FETCH_NODES = os.path.join(ROOT, 'backend', 'fetch_nodes.py')
DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))

VIEW_NAMES = {1: 'teacher1', 2: 'teacher2', 3: 'student1', 4: 'student2',
              5: 'ppt', 6: 'whiteboard', 7: 'composite'}
AUTH_HINT = '认证失败：token 过期或 normandy 会话失效。先跑 python3 %s' % MINT


def die(msg):
    print('\n!! ' + msg, flush=True)
    sys.exit(1)


def read_json(path, default=None):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def write_json_atomic(path, obj):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def write_text_atomic(path, text):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(text)
    os.replace(tmp, path)


def b64d(x):
    return base64.urlsafe_b64decode(x + '=' * (-len(x) % 4))


def token_info():
    """返回 (accessToken, exp)。jwt.txt 里可能存整段 cookie，兜一下。"""
    raw = open(JWT, encoding='utf-8').read().strip()
    m = re.search(r'jy-application-resourcemanage-ui_STORAGE_KEY_JWT_TOKEN=([^\s;]+)', raw)
    if m:
        raw = m.group(1)
    pay = json.loads(b64d(raw.split('.')[1]).decode('utf-8', 'replace'))
    return pay['accessToken'], int(pay.get('exp') or 0)


def ensure_token(force=False):
    acc, exp = token_info()
    left = exp - time.time()
    if force or left < 600:
        why = '强制刷新' if force else ('已过期 %+.1fh' % (left / 3600.0))
        print('token %s，重新铸造 ...' % why, flush=True)
        rc = subprocess.call([sys.executable, MINT])
        if rc != 0:
            die('铸造失败（rc=%d）。normandy 会话可能已失效，需在浏览器重新登录后更新 credentials/normandy.txt' % rc)
        acc, exp = token_info()
        left = exp - time.time()
    print('token OK，剩余 %.1f 小时 (exp=%s UTC)' %
          (left / 3600.0, time.strftime('%Y-%m-%d %H:%M', time.gmtime(exp))), flush=True)
    return acc


def api(path, acc, params=None, tries=3):
    qs = '&'.join('%s=%s' % (k, v) for k, v in (params or {}).items())
    url = BASE + path + ('?' + qs if qs else '')
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + acc,
                                                       'User-Agent': 'Mozilla/5.0'})
            with DIRECT.open(req, timeout=45) as r:
                j = json.loads(r.read().decode('utf-8'))
            if j.get('ok') is False:
                raise RuntimeError('API error %s: %s' % (path, j.get('message')))
            return j
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                die(AUTH_HINT)
            last = 'HTTP %d' % e.code
        except Exception as e:
            last = '%s: %s' % (type(e).__name__, e)
        time.sleep(min(30, 2 ** i))
    raise RuntimeError('%s 请求失败（%s）' % (path, last))


def ms_to_ts(ms):
    ms = int(ms or 0)
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, mm = divmod(rem, 1000)
    return '%02d:%02d:%02d,%03d' % (h, m, s, mm)


def human(n):
    n = float(n or 0)
    for u in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or u == 'TB':
            return '%.1f %s' % (n, u)
        n /= 1024.0


def download_file(url, dest, retries=10):
    """已存在且无 .part → 跳过；有 .part → Range 续传。"""
    tmp = dest + '.part'
    if os.path.isfile(dest) and not os.path.isfile(tmp) and os.path.getsize(dest) > 0:
        return os.path.getsize(dest)
    started = time.time()
    for attempt in range(retries):
        have = os.path.getsize(tmp) if os.path.isfile(tmp) else 0
        headers = {'User-Agent': 'Mozilla/5.0'}
        if have:
            headers['Range'] = 'bytes=%d-' % have
        try:
            req = urllib.request.Request(url, headers=headers)
            with DIRECT.open(req, timeout=120) as r:
                status = r.status
                total = r.headers.get('Content-Length')
                total = int(total) + have if total else None
                mode = 'ab' if (status == 206 and have) else 'wb'
                if status == 200 and have:
                    have = 0
                with open(tmp, mode) as f:
                    while True:
                        chunk = r.read(1024 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                        have += len(chunk)
                        if total:
                            print('\r      %s %s/%s (%d%%)' % (os.path.basename(dest), human(have),
                                                               human(total), have * 100 // total),
                                  end='', flush=True)
            print('', flush=True)
            if os.path.isfile(tmp):
                os.replace(tmp, dest)
            return os.path.getsize(dest)
        except Exception as e:
            wait = min(120, 2 ** attempt)
            print('\n      retry %d/%d after %ds: %s' % (attempt + 1, retries, wait, e), flush=True)
            time.sleep(wait)
    raise RuntimeError('下载失败: %s' % dest)


def make_label(week, when, sid):
    try:
        w = int(week)
    except Exception:
        w = 0
    if not w and when:
        try:
            w = time.strptime(when[:10], '%Y-%m-%d').tm_yday // 7 + 1
        except Exception:
            w = 0
    day = (when or '')[:10].replace('-', '')
    return 'wk%02d_%s_id%s' % (w, day, sid)


def channel_path(d, label, vn):
    return os.path.join(d, 'videos', '%s_%s.mp4' % (label, vn))


def scan_missing(d, entry):
    """本地已有 entry 里缺文件的频道。"""
    out = []
    for ch in entry.get('channels') or []:
        p = ch.get('file') or channel_path(d, entry.get('label', ''), ch.get('channel', ''))
        if not os.path.isfile(p) or os.path.getsize(p) == 0 or os.path.isfile(p + '.part'):
            out.append(ch)
    return out


def update_course(c, acc, dry_run):
    d = os.path.join(ROOT, c['dir'])
    mpath = os.path.join(d, 'manifest.json')
    man = read_json(mpath) or {'course': c.get('name'), 'teclId': c['teclId'],
                               'auth': 'Bearer accessToken', 'sessions': []}
    by_id = {}
    for s in man.get('sessions') or []:
        by_id[str(s.get('id'))] = s

    remote, page = [], 1
    while True:
        j = api('/v1/subject_vod_list_new', acc,
                {'page.pageIndex': page, 'page.pageSize': 100, 'teclIds': c['teclId'],
                 'page.orders[0].asc': 'true', 'page.orders[0].field': 'courBeginTime'})
        data = j.get('data') or {}
        remote += data.get('records') or []
        if page >= (data.get('pageCount') or 1):
            break
        page += 1

    new_recs = [r for r in remote if str(r.get('id')) not in by_id]
    broken = []
    missing_subs = []
    for sid, e in by_id.items():
        if scan_missing(d, e):
            broken.append(sid)
        srt = os.path.join(d, 'subtitles', (e.get('label') or '') + '.srt')
        if not os.path.isfile(srt) or os.path.getsize(srt) == 0:
            missing_subs.append(sid)

    print('\n== %s (teclId=%s) ==' % (c.get('name'), c['teclId']), flush=True)
    print('   本地 %d 节 / 远端 %d 节 → 新增 %d 节，缺文件节次 %d 个'
          % (len(by_id), len(remote), len(new_recs), len(broken)), flush=True)
    print('   空或缺字幕 %d 节' % len(missing_subs), flush=True)
    if dry_run:
        for r in new_recs:
            print('   + 新节 %s %s (id=%s)' % (r.get('courBeginTime'), r.get('week'), r.get('id')), flush=True)
        for sid in broken:
            e = by_id[sid]
            print('   ~ 补文件 %s: %s' % (e.get('label'),
                  ','.join(ch.get('channel', '?') for ch in scan_missing(d, e))), flush=True)
        return {'course': c.get('name'), 'new': len(new_recs), 'files': 0, 'bytes': 0, 'errors': 0, 'dry': True}

    files = 0
    nbytes = 0
    errors = 0

    work = [(str(r.get('id')), r) for r in new_recs]
    retry_ids = set(broken) | set(missing_subs) | {sid for sid, e in by_id.items() if e.get('error')}
    work += [(sid, {'id': sid}) for sid in by_id if sid in retry_ids]

    for sid_s, rec in work:
        entry = by_id.get(sid_s)
        if entry is None:
            when = rec.get('courBeginTime') or ''
            entry = {'id': rec.get('id'), 'time': when, 'week': rec.get('week'),
                     'label': make_label(rec.get('week'), when, rec.get('id')), 'channels': []}
            by_id[sid_s] = entry
        label = entry.get('label') or make_label(entry.get('week'), entry.get('time'), entry.get('id'))
        entry['label'] = label
        print('   · %s' % label, flush=True)

        try:
            os.makedirs(os.path.join(d, 'subtitles'), exist_ok=True)
            os.makedirs(os.path.join(d, 'videos'), exist_ok=True)

            srt = os.path.join(d, 'subtitles', label + '.srt')
            if not os.path.isfile(srt) or os.path.getsize(srt) == 0:
                subs = (api('/v1/course_vod_subtitle_with_keyword', acc,
                            {'courseId': entry['id']}).get('data') or {})
                entries = subs.get('courseTransferEsDtoList') or []
                buf = []
                for i, e in enumerate(sorted(entries, key=lambda x: x.get('bg') or 0), 1):
                    buf.append('%d\n%s --> %s\n%s\n' % (i, ms_to_ts(e.get('bg')), ms_to_ts(e.get('ed')),
                                                        (e.get('res') or e.get('zh') or '').strip()))
                write_text_atomic(srt, '\n'.join(buf))
                entry['subtitle_lines'] = len(entries)

            views = (api('/v1/course_vod_urls_new', acc,
                         {'courseId': entry['id']}).get('data') or {}).get('courseVodViewList') or []
            have = {}
            for ch in entry.get('channels') or []:
                have[ch.get('channel')] = ch
            for v in views:
                vn = VIEW_NAMES.get(v.get('viewNum'), 'ch%s' % v.get('viewNum'))
                dest = channel_path(d, label, vn)
                ch = have.get(vn)
                if ch is None:
                    ch = {'channel': vn, 'viewNum': v.get('viewNum'), 'file': dest}
                    entry.setdefault('channels', []).append(ch)
                    have[vn] = ch
                ch['file'] = dest
                ch['duration_s'] = v.get('vodTime') or ch.get('duration_s')
                if not os.path.isfile(dest) or os.path.getsize(dest) == 0 or os.path.isfile(dest + '.part'):
                    size = download_file(v['url'], dest)
                    files += 1
                    nbytes += size
                ch['size_bytes'] = os.path.getsize(dest) if os.path.isfile(dest) else ch.get('size_bytes')
            entry.pop('error', None)
        except Exception as e:
            errors += 1
            entry['error'] = str(e)
            print('     !! %s' % e, flush=True)

        man['sessions'] = sorted(by_id.values(), key=lambda s: s.get('time') or '')
        write_json_atomic(mpath, man)

    return {'course': c.get('name'), 'new': len(new_recs), 'files': files,
            'bytes': nbytes, 'errors': errors, 'dry': False}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tecl', nargs='*', type=int, help='只处理指定 teclId')
    ap.add_argument('--dry-run', action='store_true', help='只列差异，不下载')
    ap.add_argument('--refresh', action='store_true', help='强制重新铸造 token')
    ap.add_argument('--no-nodes', action='store_true', help='不补 PPT 节点')
    args = ap.parse_args()

    catalog = read_json(CATALOG) or []
    todo = [c for c in catalog if not args.tecl or int(c['teclId']) in set(args.tecl)]
    if not todo:
        die('catalog.json 里没有匹配的课程')

    acc = ensure_token(force=args.refresh)
    rows = []
    t0 = time.time()
    for c in todo:
        try:
            rows.append(update_course(c, acc, args.dry_run))
        except Exception as e:
            print('\n!! %s 更新中断: %s' % (c.get('name'), e), flush=True)
            rows.append({'course': c.get('name'), 'new': 0, 'files': 0, 'bytes': 0,
                         'errors': 1, 'dry': args.dry_run})

    print('\n===== 汇总 (%.1f 分钟) =====' % ((time.time() - t0) / 60.0), flush=True)
    for r in rows:
        print('  %-24s 新增 %2d 节  下载 %2d 个文件  %8s  错误 %d'
              % (r['course'], r['new'], r['files'], human(r['bytes']), r['errors']), flush=True)

    if not args.dry_run and not args.no_nodes and os.path.isfile(FETCH_NODES):
        print('\n== 补 PPT 节点（已存在的自动跳过）==', flush=True)
        subprocess.call([sys.executable, FETCH_NODES])

    print('\n提示：后端每个请求都实时读 manifest.json，新节次刷新前端即可出现，无需重启。', flush=True)


if __name__ == '__main__':
    main()
