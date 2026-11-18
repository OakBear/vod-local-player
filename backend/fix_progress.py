#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性/幂等修复 backend/progress.json 的未来时间戳。

背景：客户端时钟超前时会上报 ts 在未来（实测出现过 +9 天），而 _merge_prog 只接受
「比库里更新」的 ts —— 于是那几节的进度再也写不进去，`last` 也被钉死，前端 hydrate()
每次启动都拿这条过期记录覆盖 localStorage。

规则：
  1. 任何 ts > now+60s（或看起来是秒级）→ 夹到 now，并记为「被污染」；
  2. `last` 只从**没被污染**的 session 里选最近的（时间不可信的不该当续播锚点）；
     偏好字段（main/pip/audio/rate/subs）沿用旧 last；
  3. 原子写回，原文件先备份。

用法：python3 ~/vod-research/backend/fix_progress.py [--dry-run]
"""
import json, os, shutil, sys, time

PROG = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'progress.json')


def sanitize(ts, now_ms):
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return 0, True
    if ts <= 0:
        return 0, False
    if ts < 100000000000:
        return now_ms, True
    if ts > now_ms + 5000:
        return now_ms, True
    return ts, False


def main():
    dry = '--dry-run' in sys.argv
    with open(PROG, encoding='utf-8') as f:
        d = json.load(f)
    now_ms = int(time.time() * 1000)
    sessions = d.get('sessions') or {}
    clamped = []
    for key, v in sessions.items():
        if not isinstance(v, dict):
            continue
        new, bad = sanitize(v.get('ts'), now_ms)
        if bad or new != v.get('ts'):
            if bad:
                clamped.append((key, v.get('ts'), new))
            v['ts'] = new

    old_last = d.get('last') if isinstance(d.get('last'), dict) else {}
    tainted = {k for k, _, _ in clamped}
    clean = [(k, v) for k, v in sessions.items() if k not in tainted and isinstance(v, dict)]
    if clean:
        key, v = max(clean, key=lambda kv: kv[1].get('ts', 0))
        tecl, sid = key.split(':', 1)
        last = {'teclId': int(tecl) if tecl.isdigit() else tecl,
                'sid': int(sid) if sid.isdigit() else sid,
                't': v.get('t', 0), 'ts': v.get('ts', now_ms),
                'rate': old_last.get('rate'), 'subs': old_last.get('subs'),
                'main': old_last.get('main'), 'pip': old_last.get('pip'), 'audio': old_last.get('audio')}
    else:
        last = None

    print('被污染（未来 ts）的 session:')
    for k, old, new in clamped:
        print('  %-14s %s -> %s' % (k, time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(old / 1000)) if isinstance(old, int) and old > 1e11 else old,
                                    time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(new / 1000))))
    print('旧 last:', json.dumps(old_last, ensure_ascii=False))
    print('新 last:', json.dumps(last, ensure_ascii=False))

    d['sessions'] = sessions
    d['last'] = last
    d['updated'] = now_ms
    if dry:
        print('\n--dry-run，未写入')
        return
    bak = PROG + '.bak-' + time.strftime('%Y%m%d-%H%M%S')
    shutil.copy2(PROG, bak)
    tmp = PROG + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, PROG)
    print('\n已写回 %s（备份 %s）' % (PROG, os.path.basename(bak)))


if __name__ == '__main__':
    main()
