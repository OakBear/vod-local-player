#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给一门课（或全库）的每一节导出课件页图片，供播放器走图片流。

策略：有 <label>.nodes.json 就用平台时间点（快，~9 s/节）；没有就让算法自己切页
（~85 s/节，含一次视频解码，缓存后可复跑）。已导出过的默认跳过。

用法
----
    python3 emit_all_slides.py --course blockchain-apps            # 一门课
    python3 emit_all_slides.py --all --limit 5                     # 先试 5 节
    python3 emit_all_slides.py --all --dry-run                     # 只看计划
    python3 emit_all_slides.py --all --jobs 8                      # 8 路并行（建议在 Slurm 里跑）

产物：<课程目录>/slides/<label>/{index.json, 001.jpg, ...}
"""
import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, 'scripts', 'ppt_to_pdf.py')


def plan(course_dir):
    """返回 [(label, ppt_video, nodes_json, outdir)]"""
    man = os.path.join(course_dir, 'manifest.json')
    if not os.path.isfile(man):
        return []
    with open(man, encoding='utf-8') as f:
        d = json.load(f)
    out = []
    for s in d.get('sessions') or []:
        label = s.get('label')
        ppt = [c for c in (s.get('channels') or []) if 'ppt' in (c.get('channel') or '')]
        if not label or not ppt:
            continue
        vid = os.path.join(course_dir, 'videos', os.path.basename(ppt[0]['file']))
        if not os.path.isfile(vid):
            continue
        nodes = os.path.join(course_dir, 'subtitles', label + '.nodes.json')
        out.append((label, vid, nodes if os.path.isfile(nodes) else None,
                    os.path.join(course_dir, 'slides', label)))
    return sorted(out)


def run_one(item, width, timeout=600):
    label, vid, nodes, outdir = item
    if os.path.isfile(os.path.join(outdir, 'index.json')):
        return label, 'skip', 0.0
    cmd = [sys.executable, TOOL, os.path.dirname(os.path.dirname(vid)), os.path.basename(vid),
           '--emit-images', outdir, '--width', str(width)]
    if nodes:
        cmd += ['--from-nodes', nodes]
    t0 = time.time()
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        ok = 'ok' if p.returncode == 0 else 'fail(%d)' % p.returncode
    except subprocess.TimeoutExpired:
        ok = 'timeout'
    return label, ok, time.time() - t0


def main():
    ap = argparse.ArgumentParser(description='批量导出课件页图片')
    ap.add_argument('--course', help='课程目录（相对 ~/vod-research）')
    ap.add_argument('--all', action='store_true', help='全库所有课程')
    ap.add_argument('--root', default=ROOT)
    ap.add_argument('--width', type=int, default=1600, help='图片宽度，1600 够 1080p 屏')
    ap.add_argument('--jobs', type=int, default=2, help='并行度（登录节点别开太大）')
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()

    dirs = []
    if a.course:
        dirs = [os.path.join(a.root, a.course)]
    elif a.all:
        dirs = [os.path.join(a.root, n) for n in sorted(os.listdir(a.root))
                if os.path.isfile(os.path.join(a.root, n, 'manifest.json'))]
    else:
        ap.error('需要 --course 或 --all')

    items = []
    for d in dirs:
        items += plan(d)
    if a.limit:
        items = items[:a.limit]

    todo = [i for i in items if not os.path.isfile(os.path.join(i[3], 'index.json'))]
    print('共 %d 节，待导出 %d 节（已有 %d 节）' % (len(items), len(todo), len(items) - len(todo)))
    if a.dry_run:
        for label, vid, nodes, outdir in todo[:20]:
            print('  %-32s %s' % (label, 'nodes' if nodes else '算法切页'))
        return

    ok = fail = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max(1, a.jobs)) as ex:
        for label, status, secs in ex.map(lambda i: run_one(i, a.width), todo):
            print('  %-32s %-10s %5.1fs' % (label, status, secs), flush=True)
            ok += status in ('ok', 'skip')
            fail += status not in ('ok', 'skip')
    print('完成 %d 成功 / %d 失败，用时 %.1f min' % (ok, fail, (time.time() - t0) / 60))


if __name__ == '__main__':
    main()
