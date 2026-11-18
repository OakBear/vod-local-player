#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把一堂课的「课件画面」视频压成一份 PDF（一页 PPT 一张图）。

原理
----
课件通道（manifest 里 kind=ppt 的那路视频）是纯屏幕录制；同一张幻灯片在讲
解期间几乎不变，翻页/动画时才变。所以：

1. 采样：把视频按固定帧率缩成 64x36 灰度小图（ffmpeg rawvideo 管道），得到
   「每秒一张缩略图」的数组。相机/编码噪声在这么小的图上会被平均掉。
2. 分段：相邻缩略图的平均绝对差 > 阈值 → 认为发生了翻页/切换，切成一段。
   段内取最后一个采样点作为「稳定帧」（动画播完后的样子）。
3. 去重：不同段之间的稳定帧再比一次，把「翻回上一页」的去重掉。
4. 取原图：按段的时间戳在 1080p 原视频上 seek 提一张 JPEG（-ss 前置，快速）。
5. 组 PDF：纯标准库写 PDF（图片走 DCTDecode 直接塞 JPEG，不重新编码）。

用法
----
    python3 ppt_to_pdf.py <课程目录> <画质通道名> -o out.pdf
    python3 ppt_to_pdf.py testing-principles wk03_20261111_id232920_ppt.mp4 -o wk03.pdf

    --fps 1        采样帧率（越小越快，越可能漏掉短闪现的页）
    --thr 3.0      分段阈值（0-255 灰度平均差；PPT 场景 2~5 都稳）
    --dedup 2.5    去重阈值
    --min-dur 2    短于该秒数的段视为动画/误切，并入相邻段
    --nodes FILE   有 nodes.json 时用它给的时间点做「候选」，只在这些点上验证
    --max N        只处理前 N 页（调试用）
    --dry-run      只打印分段结果，不写 PDF

已知边界
--------
* 视频里嵌了教师镜头/画中画时，小图差异会被教师动作放大 → 段数虚高，先
  `--dry-run` 看一眼段数是否合理，必要时调大 --thr。
* 视频里出现「鼠标划过」「滚动动画」也会被切成新页，去重能吃掉大部分。
* 只对 kind=ppt 的通道有效；纯教师画面没有意义。
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
FFMPEG = os.path.join(os.path.dirname(HERE), 'ffmpeg-7.0.2-amd64-static', 'ffmpeg')
FFPROBE = os.path.join(os.path.dirname(HERE), 'ffmpeg-7.0.2-amd64-static', 'ffprobe')
THUMB_W, THUMB_H = 64, 36


def ffmpeg_bin(name='ffmpeg'):
    p = os.path.join(os.path.dirname(HERE), 'ffmpeg-7.0.2-amd64-static', name)
    return p if os.path.exists(p) else name


def probe_duration(path):
    out = subprocess.check_output([
        ffmpeg_bin('ffprobe'), '-v', 'error', '-select_streams', 'v:0',
        '-show_entries', 'format=duration', '-of', 'default=nw=1:nk=1', path])
    return float(out.strip())


def thumbnails(path, fps, workers=1):
    """返回 (N, THUMB_H*THUMB_W) uint8 数组，第 i 行对应第 i/fps 秒。"""
    cmd = [ffmpeg_bin('ffmpeg'), '-nostdin', '-loglevel', 'error', '-i', path,
           '-vf', 'fps=%g,scale=%d:%d' % (fps, THUMB_W, THUMB_H),
           '-pix_fmt', 'gray', '-f', 'rawvideo', '-']
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    frame_bytes = THUMB_W * THUMB_H
    chunks = []
    while True:
        buf = proc.stdout.read(frame_bytes * 512)
        if not buf:
            break
        chunks.append(buf)
    proc.stdout.close()
    proc.wait()
    data = b''.join(chunks)
    n = len(data) // frame_bytes
    arr = np.frombuffer(data[:n * frame_bytes], dtype=np.uint8).reshape(n, frame_bytes)
    return arr


def segment(thumbs, fps, thr, min_dur):
    """按相邻帧差异切段，返回 [(start_s, end_s, rep_index), ...]。"""
    n = len(thumbs)
    if n == 0:
        return []
    cut = [0]
    prev = thumbs[0].astype(np.int16)
    for i in range(1, n):
        cur = thumbs[i].astype(np.int16)
        d = float(np.abs(cur - prev).mean())
        if d > thr:
            cut.append(i)
        prev = cur
    cut.append(n)
    segs = []
    for k in range(len(cut) - 1):
        a, b = cut[k], cut[k + 1]
        if b - a < min_dur * fps:          # 太短：动画抖动，并进上一段
            if segs:
                segs[-1] = (segs[-1][0], b, b - 1)
                continue
        segs.append((a, b, b - 1))
    return [(a / float(fps), b / float(fps), rep) for a, b, rep in segs]


def dedup(thumbs, segs, fps, thr):
    """段间去重：与已保留的段代表帧比较，近似则合并。"""
    kept = []
    for start, end, rep in segs:
        t = thumbs[rep].astype(np.int16)
        dup = False
        for (_s, _e, krep, _kstart) in kept:
            if float(np.abs(t - thumbs[krep].astype(np.int16)).mean()) <= thr:
                dup = True
                break
        if not dup:
            kept.append((start, end, rep, start))
    return kept


def grab_jpeg(path, t, out, quality=2, width=1600):
    subprocess.check_call([
        ffmpeg_bin('ffmpeg'), '-nostdin', '-loglevel', 'error',
        '-ss', '%.3f' % t, '-i', path, '-frames:v', '1',
        '-vf', 'scale=%d:-2' % width, '-q:v', str(quality), '-y', out])


# --------------------------------------------------------------------------
# 最小 PDF 写入器：每页一张 JPEG（DCTDecode），无字体依赖，纯标准库
# --------------------------------------------------------------------------
def write_pdf(pages, out_path, page_w=960.0):
    """pages: [(jpeg_path, pixel_w, pixel_h, caption_ts), ...]"""
    objs = []          # 每个元素是 bytes（对象体），索引 i 对应对象号 i+1

    def add(body):
        objs.append(body)
        return len(objs)

    catalog = add(b'')          # 占位，最后填
    pages_obj = add(b'')
    kids = []
    for jpeg, pw, ph, _ts in pages:
        page_h = page_w * ph / float(pw)
        with open(jpeg, 'rb') as f:
            data = f.read()
        img = add(('<</Type/XObject/Subtype/Image/Width %d/Height %d/ColorSpace/DeviceRGB'
                   '/BitsPerComponent 8/Filter/DCTDecode/Length %d>>stream\n' % (pw, ph, len(data))).encode()
                  + data + b'\nendstream')
        content = ('q %.2f 0 0 %.2f 0 0 cm /Im0 Do Q' % (page_w, page_h)).encode()
        cs = add(('<</Length %d>>stream\n' % len(content)).encode() + content + b'\nendstream')
        kids.append(add(('<</Type/Page/Parent %d 0 R/MediaBox[0 0 %.2f %.2f]'
                         '/Resources<</XObject<</Im0 %d 0 R>>>>/Contents %d 0 R>>'
                         % (pages_obj, page_w, page_h, img, cs)).encode()))

    objs[catalog - 1] = ('<</Type/Catalog/Pages %d 0 R>>' % pages_obj).encode()
    objs[pages_obj - 1] = ('<</Type/Pages/Count %d/Kids[%s]>>'
                           % (len(kids), ' '.join('%d 0 R' % k for k in kids))).encode()

    with open(out_path, 'wb') as f:
        f.write(b'%PDF-1.4\n%\xe2\xe3\xcf\xd3\n')
        offsets = []
        for i, body in enumerate(objs):
            offsets.append(f.tell())
            f.write(b'%d 0 obj\n' % (i + 1))
            f.write(body)
            f.write(b'\nendobj\n')
        xref = f.tell()
        f.write(b'xref\n0 %d\n' % (len(objs) + 1))
        f.write(b'0000000000 65535 f \n')
        for off in offsets:
            f.write(b'%010d 00000 n \n' % off)
        f.write(b'trailer\n<</Size %d/Root %d 0 R>>\nstartxref\n%d\n%%%%EOF\n'
                % (len(objs) + 1, catalog, xref))


def load_nodes(path):
    with open(path, encoding='utf-8') as f:
        d = json.load(f)
    out = []
    for s in d.get('slides') or []:
        try:
            out.append((float(s['imageSeekTime']), s.get('ocrText') or '', s.get('imageId')))
        except (KeyError, TypeError, ValueError):
            continue
    out.sort()
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description='课件视频 → PDF（一页一张幻灯）')
    ap.add_argument('course_dir')
    ap.add_argument('video')
    ap.add_argument('-o', '--out', required=False)
    ap.add_argument('--fps', type=float, default=1.0)
    ap.add_argument('--thr', type=float, default=3.0)
    ap.add_argument('--dedup', type=float, default=2.5)
    ap.add_argument('--min-dur', type=float, default=2.0)
    ap.add_argument('--nodes', default=None)
    ap.add_argument('--max', type=int, default=0)
    ap.add_argument('--width', type=int, default=1600)
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args(argv)

    video = a.video if os.path.isabs(a.video) else os.path.join(a.course_dir, 'videos', a.video)
    if not os.path.exists(video):
        sys.exit('找不到视频: %s' % video)
    t0 = time.time()
    dur = probe_duration(video)
    print('视频 %.1f min / %.0f MB  采样 %g fps' % (dur / 60, os.path.getsize(video) / 1e6, a.fps))
    thumbs = thumbnails(video, a.fps)
    print('  缩略图 %d 张（%.1fs）' % (len(thumbs), time.time() - t0))

    segs = segment(thumbs, a.fps, a.thr, a.min_dur)
    kept = dedup(thumbs, segs, a.fps, a.dedup)
    print('  分段 %d → 去重后 %d 页（阈值 thr=%g dedup=%g）' % (len(segs), len(kept), a.thr, a.dedup))
    if a.max:
        kept = kept[:a.max]

    if a.dry_run:
        for i, (start, end, rep, _k) in enumerate(kept):
            print('  %3d  %6.1f–%6.1f s' % (i + 1, start, end))
        return

    out = a.out or os.path.join(a.course_dir, 'slides-%s.pdf' % os.path.basename(video).replace('.mp4', ''))
    tmpdir = tempfile.mkdtemp(prefix='slides-')
    pages = []
    try:
        for i, (start, end, rep, _k) in enumerate(kept):
            t = start            # 取段首：翻页那一刻起就是新页
            jpg = os.path.join(tmpdir, '%04d.jpg' % i)
            grab_jpeg(video, t, jpg, width=a.width)
            pw, ph = a.width, int(round(a.width * 9 / 16.0))
            pages.append((jpg, pw, ph, t))
            if (i + 1) % 10 == 0:
                print('  提取 %d/%d' % (i + 1, len(kept)))
        write_pdf(pages, out)
    finally:
        for p in pages:
            try:
                os.unlink(p[0])
            except OSError:
                pass
        try:
            os.rmdir(tmpdir)
        except OSError:
            pass

    size = os.path.getsize(out)
    print('完成: %s  %d 页  %.1f MB  用时 %.0fs' % (out, len(pages), size / 1e6, time.time() - t0))
    # 附带一份文字索引（有 nodes.json 时用它的 OCR 文本，能全文搜）
    if a.nodes:
        idx = out.replace('.pdf', '.md')
        with open(idx, 'w', encoding='utf-8') as f:
            f.write('# %s 幻灯片索引\n\n' % os.path.basename(video))
            nodes = load_nodes(a.nodes)
            for i, (start, _e, _r, _k) in enumerate(kept):
                near = [o for (t, o, _id) in nodes if abs(t - start) <= 3 and o]
                f.write('- **P%d** `%02d:%02d` %s\n' % (i + 1, int(start) // 60, int(start) % 60,
                                                       (near[0] if near else '').strip()[:160]))
        print('  文字索引: %s' % idx)


if __name__ == '__main__':
    main()
