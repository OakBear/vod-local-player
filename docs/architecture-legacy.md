# VOD 换源系统 · 架构与实现细节

> 服务器：**<node>**（<user>，集群登录节点，Python 3.6.8，24 核）。原始源：科达 VOD（v.sjtu.edu.cn）。
> 换源后：视频/字幕**已下载到 <node> 本地**，由 <node> 上的 HTTP 后端（**18080**，只绑 127.0.0.1）供源；前端经 **SSH 隧道**访问。
> 本文覆盖三块：**① 当前后端设计**（server.py v1.6）、**② 后端更新 / 增量下载**、**③ 前端网页细节**。

---

## 0. 系统总览

    [你的 Windows]
       |  SSH 隧道:  -L 18080:127.0.0.1:18080  -L 18766:127.0.0.1:18766
       v
    +---------------------------- <node> ----------------------------+
    | 18080  换源主后端 server.py (v1.6)                              |
    |    课程目录 / manifest / 视频流(Range) / 字幕 / 音频            |
    |    流量统计 / 观看历史 / 卡顿感知 / 整文件预热                  |
    |    科达原版 SPA 适配层 (_keda_api) + SPA 静态 (/jy-...-ui/)    |
    |    单文件播放器 course-player.html 静态                          |
    | 18766  科达适配服务 (original-vod-prototype/server.py)          |
    |    独立提供科达 SPA，接口代理到 18080（跨源，kmedia 可起播）     |
    +----------------------------------------------------------------+
       v  本地文件
    ~/vod-research/<课目录>/{videos,subtitles,audio,manifest.json}
    ~/vod-research/backend/{catalog.json,server.py,run.sh}

- **不再需要**：JWT / Bearer / AES 解密 / 签名 URL（那是下载阶段才用的，见第 2 节）。播放阶段全是本地明文 HTTP + CORS。
- **两个前端并存**：科达原版 SPA（`/` 或 18766）与单文件播放器（`/course-player.html`）。

---

## 1. 当前后端设计（server.py v1.6）

文件：`~/vod-research/backend/server.py`（约 517 行，Python 3.6）。响应头 `Server: HuanYuanBackend/1.6`。基于 `socketserver.ThreadingMixIn + http.server.HTTPServer`（每请求一线程），**只绑 127.0.0.1:18080**，HTTP/1.1 keep-alive，全局 CORS `Access-Control-Allow-Origin: *`。

### 1.1 路由分发顺序（`_do_get`，自上而下，命中即返回）

| # | 匹配 | 处理 | 说明 |
|---|------|------|------|
| 1 | `/` 或空 | `_static('/index.html')` | 根路径 = 极简播放页（双通道播放器） |
| 2 | 前缀 `/jy-application-resourcemanage-ui/` | `_keda_static` | 科达 SPA 静态；无扩展名回退 index.html；防目录穿越 |
| 3 | 前缀 `/jy-application-resourcemanage` | `_keda_api(path, q, 'GET')` | 科达接口适配（见 1.6） |
| 4 | `/index.json`、`/api/courses` | 课程总目录 | 8 门课 teclId/name/dir/status/sessions/videos |
| 5 | `/api/stats` | 流量统计 | 速率/总量/连接/seek/abort/warmed |
| 6 | `/api/history` | 观看历史 | 最近观看记录（内存，重启清空） |
| 7 | `/api/progress` | 播放进度（持久化） | 读写 `backend/progress.json`：`last` 全量状态 + `sessions[start:end]` 进度表 |
| 8 | `/api/stutter` | 卡顿感知视图 | A 客户端 + B 服务端推断汇总 |
| 9 | `/api/warm/<teclId>/<file>` | 整文件预热 | 手动触发 page-cache 预热 |
| 10 | `/api/course/<teclId>` | 单课 manifest | 节/频道/文件/时长/字幕 |
| 11 | `/media/<teclId>/{videos,subtitles,audio}/<file>` | `_file` | 媒体流（Range/206，见 1.3） |
| 12 | 其余 | `_static` | 单文件播放器 frontend/ 目录静态 |

`do_POST`：`/api/progress`（写进度，body 与前端 localStorage 同构）；`/api/telemetry`（客户端缓冲状态上报）；前缀 `/jy-application-resourcemanage` 走 `_keda_api(...,'POST')`；其余 404。

### 1.2 关键常量

| 常量 | 值 | 用途 |
|------|-----|------|
| `LOOKAHEAD` | 64 MB | seek 时向前预热的窗口 |
| `WRITE_TIMEOUT` | 5.0 s | 被切断请求（快速拖动）的放弃阈值 |
| `REBUF_GAP` | 5 MB | 服务端推断 rebuffer 的间隙阈值 |
| `_WARM_CHUNK` | 8 MB | 整文件预热的读块 |
| `FADV_SEQ`=2 / `FADV_WILLNEED`=3 | posix_fadvise | 顺序 / 急切预取进 page cache |

### 1.3 视频流与快进 seek（`_file`）

- 仅 `videos` 支持 partial；解析 `Range: bytes=start-end` 返回 **206** + `Content-Range`；无 Range 返回 200 全量。
- **1MB 分块**读盘写出，每块计入流量统计。
- **快进 seek**：partial 时先 seek 到 start，再 `posix_fadvise(start, 64MB, WILLNEED)` 把 seek 点 + 64MB 拽进 page cache，跳播首字节毫秒级；全量时用 `FADV_SEQ` 顺序提示。
- **整文件自动预热**：`if kind=='videos': ensure_warm(path)`，首次播放把整个文件后台读进 page cache，之后任意 seek 都是内存命中。
- **果断放弃**：`connection.settimeout(5s)`，客户端拖动时掐断旧请求，BrokenPipe/timeout 后干净收尾、不占线程。
- 播放同时记录观看历史 + 服务端卡顿推断。

### 1.4 卡顿感知（A 客户端 + B 服务端）

- **B 服务端推断**：记录每文件 max_end，若新请求 start 比 max_end 回退超过 REBUF_GAP(5MB) 且 end>=start，记一次 rebuffer（客户端缓冲耗尽回拉）。
- **A 客户端上报**：`POST /api/telemetry` 带 event(waiting/stalled)、buffered_depth_s、readystate，记 client_wait/stall 计数。
NaN

### 1.5 流量统计与历史

- 5 秒滑动窗口算实时速率；累计 total_bytes_served、total_requests、active_connections、seek_requests（partial 计数）、aborted_requests（切断计数）、warmed_files（已预热文件数）。

**播放进度持久化（`/api/progress`，v1.7）**：`backend/progress.json` 落盘，重启不丢。

- 结构：`{version,last:{teclId,sid,t,ts,main,pip,audio,rate,subs}, sessions:{"<teclId>:<sid>":{t,ts}}, updated}`。
- `POST /api/progress`：body 与前端 localStorage 同构；`ts` 比库里旧则丢弃（防乱序回退），否则写该节进度并推进 `last`，`sessions` 超过 800 条按 `ts` 截断；`tmp + os.replace` 原子写。
- `GET /api/progress`：返回 `last` + 全部 `sessions`，供新浏览器/新设备冷启动时 hydrate。
- 与 `/api/history` 的区别：history 是从 Range 请求猜的观看记录（内存、重启清空），progress 是前端主动上报的权威进度（落盘）。

### 1.6 科达原版 SPA 适配层（v1.6，`_keda_api`）

把原科达 API 一一映射到本地数据，统一用 `keda_wrap` 包成科达信封 `{code,data,dataOk,message,ok,status:200}`。续播进度从 `vod_progress` cookie 读。已适配端点：

| 科达接口 | 本地来源 |
|---|---|
| `/v1/config/vodNmediaConfigInfo` | 媒体源配置 uri 指向 18080 |
| `/v1/teachingClass/<id>/t-1` | 课程名/teclId（catalog） |
| `/source/vod/lastPlayInfoByTeclId` | 续播课程 id（cookie 或首节） |
| `/subject_vod_list_new` | 节次列表（records，按时间倒序） |
| `/v1/course_vod_urls_new` | **视频 URL**：`http://127.0.0.1:18080/media/<teclId>/videos/<basename>`（quote 编码），含 lastPlayTime |
| `/v1/course_vod_subtitle_with_keyword` | SRT 解析成 courseTransferEsDtoList（bg/ed/res 毫秒） |
| `/v1/course/ai/ppt` | PPT 节点（nodes.json slides） |
| `/course/ai/textSummary/<id>` | 课堂概要 + 思维导图（nodes.json） |
| `/course/ai/translate/<id>` | 字幕翻译（before/after 同 SRT） |
| 其余（heatmap/styles/keyValues/acknowledge/hasDomainName/tecl-ai-show-config/getAiFunctionShow/question/note/getCollection 等） | 返回空/默认，让 SPA 正常走通 |

未命中一律返回空对象；异常打印 `ADAPTER ERROR` 并返回 data:null（不崩）。

### 1.7 前端静态服务

- **科达 SPA**：`_keda_static` 从 `frontend/original-vod-prototype/` 出，无扩展名回退 index.html，防穿越。
- **单文件播放器**：`_static` 从 `frontend/` 出（course-player.html / index.html）。
- 静态带 `Cache-Control: no-cache`，每次取新。

### 1.8 版本演进

v1.1 加 `/api/stats`+`/api/history`；v1.2 卡顿感知（`/api/stutter`+`/api/telemetry`，A 客户端 + B 服务端推断）；v1.3 快进 seek（WILLNEED 64MB + 5s 果断放弃）+ 多倍速（TCP 流控 + 激进预热）；v1.4 整文件预热（`/api/warm`，选课时调用）；v1.5 前端静态托管；v1.6 合并科达原版 SPA 适配层（SPA 静态 + 全部科达接口 + `/` 302 播放中心）；**v1.7 播放进度持久化 `/api/progress` + `backend/progress.json`（`/` 改指极简播放页）。**

---

## 2. 后端更新 / 增量下载

下载阶段**才**用到科达的 JWT/Bearer/AES。三步：**铸 token → 下载视频/字幕 → 抓 PPT 节点**。全部脚本在 <node> 本地。

### 2.1 铸 token（`scripts/mint_vod_token.py`）

- 从保存的 **normandy cookie**（`credentials/normandy.txt`）出发，**重放 LTI 1.3 六跳链**（oc.sjtu.edu.cn 外部工具 → authorize → launch），拿到 jwt_token。
- 解出 accessToken 与 exp，写入 `blockchain-apps/scripts/{jwt.txt,bearer.txt}`，打印剩余小时数。
- **token 约 24h 失效**，过期重跑本脚本（前提是 normandy 会话还有效；失效需重新登录拿 cookie）。

### 2.2 下载脚本（`<课>/scripts/vod_download.py`，已参数化）

用法：`python3 vod_download.py --jwt-file .../jwt.txt --tecl-id <id> --out <课目录>`（`--only-subtitles` 只下字幕）。

- **鉴权**：`Authorization: Bearer <accessToken>`（从 JWT claim 解出）。
- **AES 解密**：科达播放中心 URL 带加密参数，用 AES-128-CBC（key `jy-resource-qry1`、IV `jy-query-iv-0001`）解出真实参数。
- **流程**：`teachingClass/<id>/t-1` 拿课名 → `subject_vod_list_new` **分页**拉全部节次 → 每节 `course_vod_subtitle_with_keyword`（写 SRT）+ `course_vod_urls_new`（各频道 MP4）。
- **频道命名**：viewNum 1-7 对应 teacher1/teacher2/student1/student2/ppt/whiteboard/composite。
- **断点续传（增量核心）**：每个文件先写 `dest.part`，已存在则带 `Range: bytes=<have>-` 续传；服务端 206 则追加，200 则重来；下完 `os.replace(.part, dest)` 原子落地。**已完成文件（dest 存在且无 .part）直接跳过**，重跑同一命令即从断点继续、不重复下。
- **重试**：最多 10 次，指数退避 `min(120, 2**attempt)` 秒。
- **manifest 增量**：每下完一节就把 manifest.json 落盘（节/频道/时长/文件/大小/字幕行数），中断不丢已下进度。

### 2.3 PPT 节点与 AI（`backend/fetch_nodes.py`）

- 遍历 catalog 所有课所有节，调 `/v1/course/ai/ppt`（docList：imageId/imageSeekTime/ocrText 等）+ `/v1/course/ai/textSummary/<id>`（summary/mindmap）。
- 存 `<课>/subtitles/<label>.nodes.json`；**已存在则跳过**（增量）。3 线程并发。
- 供科达适配层的 PPT 节点/课堂概要/AI 展示用。

### 2.4 数据结构

- **`backend/catalog.json`**：8 门课 `{teclId, name, dir, canvasId}`。后端据此解析课程目录、判断 ready/downloading。
- **`<课>/manifest.json`**：`{course, teclId, sessions:[{id,time,week,label,subtitle_lines,channels:[{channel,viewNum,duration_s,file,size_bytes}]}]}`。**`file` 是 <node> 绝对路径**，前端取 basename 再拼媒体地址。
- **目录布局**：`<课>/{videos/*.mp4, subtitles/*.srt, subtitles/*.nodes.json, logs/, manifest.json}`。
- **命名**：视频 `wkXX_日期_id<频道>.mp4`，字幕/节点 `<label>.srt/.nodes.json`（label=`wkXX_日期_id<sid>`）。
- 下载总日志：`~/vod-research/download-all.log`。

### 2.5 更新 / 新增课程

1. token 过期先跑 `python3 scripts/mint_vod_token.py`。
2. 新课：在 `catalog.json` 加 `{teclId,name,dir,canvasId}`，建目录，跑 `vod_download.py --tecl-id <id> --out <目录>`（增量续传）。
3. 下完跑 `python3 backend/fetch_nodes.py` 补 PPT 节点。
4. 后端 `/api/courses` 会自动显示该课 `status: ready`（有 manifest 即 ready）。
5. 已下完的课重跑下载命令 = 全跳过（幂等），只补缺失。

---

## 3. 前端网页细节

两套前端并存，都只认 `http://127.0.0.1:18080`（隧道后）。

### 3.1 科达原版 SPA（`frontend/original-vod-prototype/`）

- **形态**：学校原版播放中心的 HTML/JS/CSS/播放器资源（Vue hash-router，`#/play-center?teclId=...&target=video`）。入口 index.html 带全屏黑底 spinner（**任何 JS 失败/挂起 = 黑屏转圈**）。
- **加载**：`static/js/3415.*.js`(4MB)、`index.*.js`(479KB)、`local-courses.js`，懒加载 `9609.*.js`(5MB)、`kmedia-uni.js`(6MB 专有播放器)。
- **配置**：`static/config/global-production.json` 的 `BASE_URL:"/jy-application-resourcemanage"`、`PLAY_TYPE:"hls"`（但 .mp4 走渐进下载）。
- **local-courses.js**：`const API='http://127.0.0.1:18080'` 直连 `/api/courses`+`/api/stats`，课程切换下拉、网速角标、极简/原版控制切换、进度存 localStorage + `vod_progress` cookie。
- **kmedia 播放器**：靠 `vodNmediaConfigInfo` 拿媒体源、`course_vod_urls_new` 拿 URL 起播。**对「页面源 ≠ 媒体源」（跨源）敏感**——同源会拿到 URL 后卡死不请求视频（见 3.4）。
- **限制**：收藏/提问/笔记/下载/反馈等在线功能为 stub；PPT 缩略图可能为空；续播仅靠 cookie。

### 3.2 极简播放器（`frontend/index.html`，根路径 `/`）

- 标准 HTML5 `<video>`，**不依赖 kmedia**，同源/跨源无所谓，实测稳定能播。
- 版式对齐科达原版：**主屏幕 + 悬浮小屏幕 + 互换**。主画面默认 teacher1，小窗默认 ppt（26% 宽、16:9、**右上角**，避免压字幕；可拖拽改位置、拖拽范围限制在播放器内）；点小窗画面 / 顶栏「⇄ 互换」/ 按 `S` 互换主小；小窗标题条上的 🔊 决定声音跟随哪个画面。
- 双通道联动：播放/暂停/拖动/倍速同步，副画面按 leader 漂移 >0.45s 校正；`localStorage`（`vod-mini-v2`）记 `teclId/sid/t` **+ 主/小画面通道、声音跟随谁、倍速、字幕开关**，刷新或换节后沿用上次那套（通道在本节不存在时回退 teacher1+ppt）。
- 同一份状态每 ≥8 秒（外加暂停/切课/关页 `pagehide` 立即 flush）`POST /api/progress` 落盘；启动时 `GET /api/progress`，后端 `ts` 更新则覆盖本地，所以换浏览器/换设备也能续播。localStorage 仍是第一顺位（更快、离线可用）。
- **字幕叠在画面上**：解析 SRT（非 `<track>`），主画面底部居中半透明条，18px 白字、`max-width:78%`，随播放切换；cue 对象自带 `text`（早前版本漏保存该字段导致叠加层为空）。取字幕优先 `/media/<teclId>/subtitles/<label>.srt`，失败回退科达适配接口 `v1/course_vod_subtitle_with_keyword`。控制条「字幕：开/关」（快捷键 `C`）可隐藏。
- 全屏作用于 `#screen`（播放器 + 控制条一起进全屏），因此全屏下仍能暂停/拖动/切字幕。
- **PPT 跳转**：nodes.json 切片条，点击 seek 双通道 + 高亮当前片，播放时自动横向滚动（手滚后 2.5s 内不抢）。
- **节次**：右栏上半，当前节高亮 + 自动滚到可视区。
- **语音**：右栏下半（与节次上下分栏，`flex:1 1 50%`），同一份 SRT 逐句文本，随播放高亮/自动滚动/点击跳转。
- **版式给视频让位**：header 48px、左右 padding 10px、右栏 272px，视频区只剩「播放器 + 控制条 + 底部 PPT 切片条」；播放在 `flex:1` 下吃满剩余高度（`min-height:180px`），窗口变矮时不再挤压。
- 控制只有一条平铺控制条（播放、时间、进度、倍速、互换、字幕、全屏）；画面上除字幕与小窗外无任何浮层。
- 旧地址 `/course-player.html` 已改为跳转 `/index.html`；科达 SPA 仍在 `/jy-application-resourcemanage-ui/`（18766 亦可用）。

### 3.3 前端—后端接口契约

| 接口 | 用途 | 关键返回 |
|---|---|---|
| `GET /` 或 `/api/courses` | 课程目录 | courses[]（status/sessions/videos） |
| `GET /api/course/<teclId>` | 单课 manifest | sessions[].channels[].file（绝对路径） |
| `GET /media/<teclId>/videos/<basename>` | 视频流（Range 拖动） | 206 + Content-Range |
| `GET /media/<teclId>/subtitles/<label>.srt` | 字幕 | SRT 文本 |
| `GET /api/stats` | 速率/流量/seek | speed_human、total_bytes_human 等 |
| `GET /api/history` | 续播 | 最近观看（内存） |
| `GET /api/progress` | 播放进度（持久化） | last 全量状态 + sessions[] |
| `POST /api/progress` | 上报进度/偏好 | 旧 ts 丢弃，返回 {ok,saved} |
| `POST /api/telemetry` | 卡顿上报 | event/buffered_depth_s |
| 科达接口（`/jy-application-resourcemanage/v1/...`） | 科达 SPA 专用 | 见 1.6 |

**前端取视频**：manifest 的 `file` 是绝对路径，取 basename 再拼 `/media/<teclId>/videos/<basename>`。

### 3.4 跨源架构与访问方式（关键）

- **科达原版要跨源**：SPA 源（18766）≠ 媒体源（18080），kmedia 才起播。故 **18766 跑科达适配服务**（original-vod-prototype/server.py，接口代理到 18080，媒体仍走 18080 带 CORS）。
- **单文件播放器可同源**：直接 `18080/course-player.html`。
- **隧道**（Windows，一条连接两个映射）：

    ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
        -L 18080:127.0.0.1:18080 -L 18766:127.0.0.1:18766 <host>

- 访问：科达原版 `http://127.0.0.1:18766/`；单文件 `http://127.0.0.1:18080/course-player.html`。
- 18766 适配服务是为 Py3.6 打过兼容补丁（ThreadingMixIn 替代 ThreadingHTTPServer）。

---

## 4. 运维手册

### 4.1 一键启动（与 DSH 解耦）

    ssh <host> ~/vod-research/backend/run.sh

杀掉旧进程，用 `setsid nohup` 脱离拉起 18080+18766，打印状态。进程过继给 init，**DSH 重启/会话重建不影响**。无 systemd、无 cron 保活。

### 4.2 验证

- `curl -s http://127.0.0.1:18080/api/courses` 看课程目录。
- `curl -sI -H 'Range: bytes=0-1' http://127.0.0.1:18080/media/<teclId>/videos/<f>.mp4` 应返回 206 + Content-Range。
- `curl -s http://127.0.0.1:18080/api/stats` 看速率/流量/seek/warmed。

### 4.3 排障

- **端口不通**：先跑 run.sh；再看 `/tmp/huanyuan.log`、`/tmp/keda18766.log` 有无 traceback（无 traceback = 被外部 SIGKILL）。
- **科达黑屏转圈**：F12 看 Console 红色报错 + Network 里 .mp4 是否发出；多为 kmedia 跨源/隧道时序问题。
- **播放卡**：看 `/api/stats` 的 speed_human 是否压得住码率；压不住 = 隧道带宽不够，可转码降码率。
- **pgrep 陷阱**：用 `pgrep -f 'python3 -u server\.py'`，别用会自匹配的宽 pattern。
### 播放诊断（待更新后的后端进程及前端页面启用）

- `backend/server.py` 将视频请求的 `transfer_window`（约 15 秒）和 `transfer_end` 写入 `backend/playback.jsonl`，其中字节速率是后端向套接字写入的速率，不等于浏览器实际收到或解码的速率。日志按 10 MiB 轮转，最多保留 5 个备份。
- `frontend/original-vod-prototype/static/playback-telemetry.js` 为每个本地视频每 15 秒上报播放倍速、该区间媒体时间推进率、缓冲秒数和可用的帧统计，也上报等待、错误和倍速变化。`possible_frame_freeze` 只是页面可见且播放时间推进、视频帧计数不变时的线索，不能判断静态画面是否应当变化。浏览器不支持帧统计时这些字段为空。
- 区间推进率为媒体时间增量除以实际时间，暂停、拖动、切源等会重置采样；它不是网络速度。前端和后端日志都需各自运行新版本。若 Windows 运行独立副本，必须更新其 HTML 和脚本，服务端的本地副本不会自动覆盖 Windows 文件。
- 视频请求写入超时从 5 秒改为 60 秒，降低浏览器缓冲满时被服务端提前中断的可能，仍不能保证播放零卡顿。运行中的 Python 进程不会自动加载这些后端改动。
