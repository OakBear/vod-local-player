#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""体检两层凭据：OC 层的 normandy 会话 / 播放器层的 JWT。

    python3 scripts/token_status.py                # 只看状态
    python3 scripts/token_status.py --normandy     # 只查 normandy
"""
import argparse
import base64
import datetime
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NORMANDY = os.path.join(ROOT, 'credentials', 'normandy.txt')
JWT_CANDIDATES = [
    os.path.join(ROOT, 'blockchain-apps', 'scripts', 'jwt.txt'),   # 老路径，兼容
]


def b64d(s):
    s += '=' * (-len(s) % 4)
    return base64.urlsafe_b64decode(s)


def find_jwt():
    for p in JWT_CANDIDATES:
        if os.path.isfile(p):
            return p
    # 新布局：任意 <课程>/scripts/jwt.txt
    for name in sorted(os.listdir(ROOT)):
        p = os.path.join(ROOT, name, 'scripts', 'jwt.txt')
        if os.path.isfile(p):
            return p
    return None


def check_normandy(quiet=False):
    if not os.path.isfile(NORMANDY):
        print('[OC 层] ✗ 缺 %s' % os.path.relpath(NORMANDY, ROOT))
        print('        去浏览器 Application → Cookies → <平台域名> 复制 _normandy_session 的值，整行存进去')
        return False
    raw = open(NORMANDY, encoding='utf-8').read().strip()
    ok = raw.startswith('eyJ') or len(raw) > 40
    if not quiet:
        print('[OC 层] %s _normandy_session  %d 字符  %s'
              % ('✓' if ok else '?', len(raw), '长度正常（是否真有效要看铸造结果）' if ok
                 else '长度可疑，检查是否复制完整/带引号'))
    return ok


def check_jwt(quiet=False):
    p = find_jwt()
    if not p:
        print('[播放器层] ✗ 没找到 jwt.txt（<课程>/scripts/jwt.txt）')
        print('          浏览器 Local Storage 里找 jy-application-resourcemanage-ui_STORAGE_KEY_JWT_TOKEN，整段存进去')
        return False
    raw = open(p, encoding='utf-8').read().strip()
    m = re.search(r'jy-application-resourcemanage-ui_STORAGE_KEY_JWT_TOKEN=([^\s;]+)', raw)
    if m:
        raw = m.group(1)
    try:
        pay = json.loads(b64d(raw.split('.')[1]).decode('utf-8', 'replace'))
    except Exception as e:
        print('[播放器层] ✗ jwt.txt 解析失败: %s（应当是三段式 xxx.yyy.zzz）' % e)
        return False
    exp = int(pay.get('exp') or 0)
    left = (exp - time.time()) / 3600.0
    when = datetime.datetime.utcfromtimestamp(exp).strftime('%m-%d %H:%M') if exp else '?'
    if not quiet:
        print('[播放器层] %s exp=%s UTC  剩余 %.1f 小时  sub=%s'
              % ('✓' if left > 0 else '✗ 已过期', when, left, str(pay.get('sub'))[:12]))
        if not pay.get('accessToken'):
            print('          注意：payload 里没有 accessToken 字段，可能贴的是别的 token')
    return left > 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--normandy', action='store_true', help='只检查 OC 层会话')
    a = ap.parse_args()
    ok1 = check_normandy()
    ok2 = True if a.normandy else check_jwt()
    if a.normandy:
        sys.exit(0 if ok1 else 1)
    if ok1 and ok2:
        print('\n两层都在有效期内，可以直接跑: python3 scripts/update_all.py --dry-run')
    elif ok2 and not ok1:
        print('\n播放器 token 还有效，但 OC 层会话失效 → 到期后就换不出新 token 了，趁早补 _normandy_session')
    elif ok1 and not ok2:
        print('\nOC 层会话在（有效性要靠铸造验证）→ python3 scripts/mint_vod_token.py；报 HTTP 302 就是失效了')
    else:
        print('\n两层都要重新取，按 docs/凭据与Cookie指南.md 做一遍')
    sys.exit(0 if (ok1 and ok2) else 1)


if __name__ == '__main__':
    main()
