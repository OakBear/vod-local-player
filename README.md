# 本地课堂播放器（Lecture VOD 换源）

把自己有权访问的大学在线课堂点播（SJTU「好大学在线 / 科达 Keda」平台）镜像到一台自己的服务器上，
用一个**极简播放器**来看：拖动几乎即时、教师画面与课件画面同屏、进度跨设备、全程可自诊断。

> 只含代码，不含任何视频、字幕、课件或平台前端资源。凭据必须自己提供，登录与下载是你自己的账号行为。
> 仅供个人学习使用，请遵守所在学校的使用条款。

## 为什么需要它

官方播放器的问题不是「服务器慢」，而是链路上每一跳都在限制你：

| 环节 | 官方表现 | 本地镜像后 |
|---|---|---|
| 拖动进度 | 重新协商、等 CDN 回源，常见 10–30 s | Range/206 + `posix_fadvise` 预读，通常 <200 ms |
| 双画面 | 强制合成或不可选 | 教师 / 课件两路独立，可自由切换、小窗拖动 |
| 倍速 | 受限于单条流 | 想开几倍开几倍（带宽允许的话） |
| 进度 | 绑在浏览器 local storage | 服务端落盘，换设备接着看 |
| 排障 | 只能猜 | 每次传输、每次卡顿、首帧耗时都有日志 |

## 结构

```
[浏览器]
   │  SSH 隧道：-L 18080:127.0.0.1:18080   -L 18766:127.0.0.1:18766
   ▼
服务器
├─ 18080  backend/server.py            主服务
│    /                   极简播放器（frontend/index.html，单文件无构建）
│    /api/*              目录 / 统计 / 进度 / 卡顿 / 版本
│    /media/*            视频流（Range/206 + fadvise 预读 64 MB）
│    /jy-application-resourcemanage*   平台 SPA 的适配层（把平台接口映射到本地文件）
└─ 18766  frontend/original-vod-prototype/server.py   薄宿主
     静态托管平台原版 SPA + 其余请求反代回 18080
```

- 两服务都只用标准库：`http.server` + `ThreadingMixIn`，Python 3.6+ 即可跑，无第三方依赖。
- 只监听 `127.0.0.1`：**安全边界是 SSH 隧道本身**，不要把 `HOST` 改成 `0.0.0.0`。

## 目录

```
backend/    server.py（主服务）、vodctl.py（启停）、fix_progress.py（进度修复）、
            run.sh / start-all.sh（薄包装）、fetch_nodes.py（抓 PPT 节点）
frontend/   index.html（播放器单文件）、original-vod-prototype/（SPA 薄宿主）
scripts/    update_all.py（增量下载）、mint_vod_token.py（换 token）、ppt_to_pdf.py（课件→PDF）
docs/       VOD后端技术文档.md、VOD前端技术文档.md、architecture-legacy.md（早期设计笔记）
examples/   manifest.example.json（数据契约示例）
```

## 快速开始

1. **拿到凭据**（你自己的账号）：平台 token 与会话 cookie 放在本机，路径见脚本头部说明；
   `scripts/mint_vod_token.py` 用于 token 过期后重铸。这些文件都在 `.gitignore` 里。
2. **下载媒体**：`python3 scripts/update_all.py`（增量、断点续传；按课程目录组织）。
3. **配目录**：把 `backend/catalog.example.json` 复制成 `backend/catalog.json`，
   填上 `teclId / name / dir`。目录结构：
   ```
   ~/vod-research/<dir>/videos/<label>_<channel>.mp4
   ~/vod-research/<dir>/subtitles/<label>.srt|.nodes.json
   ~/vod-research/<dir>/manifest.json
   ```
4. **跑起来**：
   ```bash
   ./backend/run.sh              # 18080 + 18766 一起
   ./backend/run.sh 18080        # 只重启主服务
   python3 backend/vodctl.py status
   ```
   本机开 `http://127.0.0.1:18080/`；远程用 SSH 隧道。

## 接口一览

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/version` | 版本 / pid / 写超时 / 预读窗口（判断跑的是不是磁盘上的代码） |
| GET | `/api/courses` | 课程目录 |
| GET | `/api/course/<teclId>` | 单课 manifest（节次、通道、码率） |
| GET | `/media/<teclId>/<kind>/<file>` | 媒体流，支持 `Range` |
| GET | `/api/warm/<teclId>/<file>` | 选节即把整文件读进 page cache |
| GET | `/api/stats` | 速率 / 流量 / seek / abort / 预热数 |
| GET | `/api/progress` | 跨设备进度（落盘，带 `ts` 防乱序） |
| POST | `/api/progress` | 前端上报位置（毫秒时间戳，异常时钟会被钳制） |
| POST | `/api/telemetry` | 播放器诊断：`waiting/stalled`、首帧 `timing` |
| GET | `/api/stutter` | 卡顿汇总（客户端 A + 服务端 B 推断） |

`/api/history` 仍返回，但**已废弃**：它是「Range 字节数 ÷ 文件大小 × 时长」猜出来的位置，
浏览器习惯性先取文件尾，所以数值天然偏大，不要拿来做续播。

## 运维

```bash
python3 backend/vodctl.py status|start|stop|restart|ensure [18080|18766|all]
```

- 按进程 cwd 认领实例，两个服务同名 `server.py` 也能分开重启；
- `ensure` 幂等，适合挂 cron 自愈：`*/5 * * * * .../backend/start-all.sh`；
- 日志：`/tmp/huanyuan.log`（18080）、`/tmp/keda18766.log`（18766）、
  `backend/playback.jsonl`（结构化诊断，10 MB × 5 轮转）。

## 课件通道：图片代替视频（省一半带宽）

真 PPT 是静态的。课件通道（1 Mbps 的屏幕录制）完全可以换成「按时间轴换一张 JPG」：

```bash
# 每节导出课件页图片（有平台时间点时 9 s/节，没有时约 85 s/节）
python3 scripts/ppt_to_pdf.py <课程目录> <课件视频> --emit-images <课程目录>/slides/<label>
```

后端多两条路由：`GET /api/slides/<teclId>/<label>`（页索引）与
`GET /media/<teclId>/slides/<label>/NNN.jpg`（单页图片）。
播放器选节时先取索引：命中就把小画面换成 `<img>`，**这一路视频流直接不发**，
滚到哪张图就取哪张（提前预取下一张）；没命中就照旧走视频。

实测（一节 60 min 的课）：课件图片 25–28 页共 **2.9–3.2 MB**，而课件视频是 **398 MB / 1 Mbps 持续**。
同一次播放服务端日志里只剩 `teacher1.mp4` 一路传输。

## 课件视频 → PDF

`scripts/ppt_to_pdf.py` 把一堂课的课件通道压成一份 PDF（一页一张幻灯片）：

```bash
# ① 有平台页数据（nodes.json）时：直接用它的时间点，不解码视频，一节课十几秒
python3 scripts/ppt_to_pdf.py <课程目录> <课件视频文件> \
        --from-nodes <课程目录>/subtitles/<label>.nodes.json -o 第06周.pdf

# ② 没有平台数据时：靠缩略图差分自己切页（一支 60 min 课件约 85 s）
python3 scripts/ppt_to_pdf.py <课程目录> <课件视频文件> -o 第03周.pdf --dry-run
python3 scripts/ppt_to_pdf.py <课程目录> <课件视频文件> -o 第03周.pdf
```

实测（同一门课）：
- `--from-nodes`：43 页，**14 s**；
- 自研切页：默认阈值 25 页 / 85 s；`--dedup 0` 保留每一次变化（≈49 页），接近平台给的 43 页；
- 平台自己的 43 个时间点里有 13 处间隔 < 8 s（幻灯片动画被当成新页），所以「一页一张」和「一次变化一张」是两种需求，用 `--dedup` 调。

做法：先把视频缩成 64×48 灰度缩略图取帧间差 → 差异超阈值就切页 → 取「动画播完」的稳定帧 →
跨页去重（翻回上一页的不重复出图）→ 按时间点从 1080p 原片提一张 JPEG → 用纯标准库写 PDF
（JPEG 直接以 `DCTDecode` 嵌入，不二次编码）。

实测：47 分钟 / 398 MB 的课件视频，1 fps 采样，解码约 2 分钟，出 PDF 与平台 AI 抽取的 35 页基本对齐。
依赖：`ffmpeg`（可用静态版）与 `numpy`；不需要 PIL / reportlab。

已知边界：视频里叠了教师画面的通道不适合（小图差异被人物动作放大，切页虚高）；
鼠标划动、滚动动画可能切出多余页，去重能吃掉大部分。

## 实测数据（8 门课 / 205 节 / 410 个通道文件 / 263 GB）

| 指标 | 数值 |
|---|---|
| 本机回环吞吐 | 283 MB/s |
| 教师通道码率 | 中位 ≈ 2.0 Mbps（841 MB / 3306 s） |
| 课件通道码率 | 中位 ≈ 1.0 Mbps（433 MB / 3306 s） |
| SSH 隧道有效吞吐（41 个传输窗口样本） | p10 147 KB/s ・ p50 731 KB/s ・ p90 2.0 MB/s |
| 首帧（选节 → 可播，25 次真实使用） | p50 989 ms ・ p90 5.3 s ・ max 14.1 s |

**结论**：瓶颈只在隧道，不在服务端。双通道 1× 播放就需要 ≈ 385 KB/s，而隧道中位只有 731 KB/s；
开到 2× 以上就必然追不上——日志里出现的 2×–12× 倍速，是「还会卡」的第一原因。

## 已知限制

1. 平台 SPA 的前端资源不随仓库分发（版权与体积），需要你自己从浏览器里取，放到
   `frontend/original-vod-prototype/static/`；不放也能用极简播放器。
2. MP4 的 `moov` 在文件尾，起播要多一次尾部 Range 请求（预热能抵消大部分）。
3. 进度是「最后位置」单值，两台设备同时看会互相覆盖。
4. 适配层只覆盖自己用到的平台接口，收藏 / 笔记 / 提问等未适配。

## 文档

- [后端技术文档](docs/VOD后端技术文档.md)：路由、媒体服务、进度、日志、参数表、排障
- [前端技术文档](docs/VOD前端技术文档.md)：双播放器模型、错峰与预热、埋点、交互事件表
- [早期架构笔记](docs/architecture-legacy.md)

## License

MIT。不含任何第三方视频内容与平台资源。
