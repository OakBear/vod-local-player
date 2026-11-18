# VOD 换源系统 · 后端技术文档

> 对应文件：`~/vod-research/backend/server.py`（672 行 / 34 KB，Python 3.6 兼容）
> 监听：`127.0.0.1:18080`（仅回环，不对外），`Server: HuanYuanBackend/1.8`
> 配套前端文档：`VOD前端技术文档.md`
> 文档基准：2026-11-12 维护后（v1.8 已真实上线，见第 13 节变更记录）

---

## 1. 系统定位

把学校科达 VOD（`v.sjtu.edu.cn`）的视频/字幕**提前下载到 <node> 本地**，再由本地 HTTP 服务供源。播放阶段完全不碰 JWT / AES / 签名 URL —— 那些只用于下载阶段（见第 7 节）。

```
[Windows 浏览器]
   │  SSH 隧道  -L 18080:127.0.0.1:18080  -L 18766:127.0.0.1:18766
   ▼
<node>
├─ 18080  backend/server.py（本文档）
│    /                  极简播放页（frontend/index.html）
│    /api/*             目录 / 统计 / 进度 / 卡顿 / 版本
│    /media/*           视频流（Range/206 + fadvise 预读）
│    /jy-application-resourcemanage*  科达 SPA 适配层（全系统唯一实现）
└─ 18766  frontend/original-vod-prototype/server.py（薄宿主，114 行）
      静态托管科达原版 SPA；其余请求全部反代回 18080
   │ 本地磁盘
   ▼
~/vod-research/<课目录>/{videos,subtitles,audio,manifest.json}
~/vod-research/backend/{catalog.json,server.py,vodctl.py,run.sh,progress.json,playback.jsonl}
```

---

## 2. 运行环境与进程模型

| 项 | 值 | 说明 |
|---|---|---|
| Python | 3.6.8 | 全部用 `%` 格式化；不用 f-string、不用 dataclass |
| 并发 | `ThreadingMixIn + HTTPServer`，`daemon_threads=True` | 每请求一线程；`allow_reuse_address=True` 支持快速重启 |
| 协议 | `protocol_version = 'HTTP/1.1'` | 有 `Content-Length` 才能 keep-alive |
| 绑定 | `HOST='127.0.0.1'`，`PORT=os.environ.get('VOD_PORT','18080')` | 端口可用环境变量覆盖 |
| 根路径 | `ROOT = dirname(dirname(__file__))` | 即 `~/vod-research` |
| CORS | 全局 `*`，并 `Expose-Headers: Content-Range, Accept-Ranges` | 允许 18766 跨源取媒体 |
| 日志 | `backend/playback.jsonl`，10 MiB × 5 轮转，`delay=True` | JSONL，一行一事件 |

> 版本号已统一（2026-11-12）：`server_version`、启动横幅、`/api/version` 都是 1.8。
> 以前「Header 1.6 / 横幅 v1.4 / 实际 v1.7」的三套号是历史遗留，别再照旧号判断。

---

## 3. 路由表（`_do_get`，自上而下，命中即 return）

| # | 匹配 | 处理 | 说明 |
|---|---|---|---|
| 1 | `/` 或空串 | `_static('/index.html')` | 极简播放页 |
| 1b | `/api/version` | 内联 JSON | `{version,pid,started,write_timeout,lookahead_mb,root}`，判断「跑的是不是磁盘上的代码」用 |
| 2 | 前缀 `/jy-application-resourcemanage-ui/` | `_keda_static` | 科达 SPA 静态（来自 `frontend/original-vod-prototype/`） |
| 3 | 前缀 `/jy-application-resourcemanage` | `_keda_api(...,'GET')` | 科达接口适配（第 6 节） |
| 4 | `/index.json`、`/api/courses` | `_courses_info()` | 课程目录 |
| 5 | `/api/stats` | `_stats()` | 速率/流量/seek/abort/warmed |
| 6 | `/api/history` | `_history()` | **deprecated**：Range 反推的「位置」不是真实播放点，只看 `/api/progress` |
| 7 | `/api/progress` | `_prog_view()` | 前端上报的权威进度（落盘） |
| 8 | `/api/stutter` | `_stutter_view()` | 卡顿汇总 |
| 9 | `^/api/warm/(\d+)/(.+)$` | `ensure_warm()` | 手动整文件预热 |
| 10 | `^/api/course/(\d+)$` | 读 `manifest.json` | 单课 manifest |
| 11 | `^/media/(\d+)/(videos\|subtitles\|audio)/(.+)$` | `_file()` | 媒体流 |
| 12 | 其余 | `_static(path)` | 前端静态；无扩展名回退 index.html |

`do_POST`：

| 路径 | 行为 |
|---|---|
| `/api/progress` | 解析 JSON → `_merge_prog()`，返回 `{ok,saved}` |
| `/api/telemetry` | 校验 `teclId` 为数字且 `file` 为字符串，否则 400；`event=timing`（首帧埋点）只落日志、不进 `_stutter`；其余走 `_telemetry()` + 写 `player` 日志 |
| 前缀 `/jy-application-resourcemanage` | 走 `_keda_api(...,'POST')` |
| 其余 | 404 |

`do_OPTIONS` 返回 204 + CORS 头（浏览器预检）。

**路由顺序有语义**：`/api/course/<id>` 只匹配纯数字（`COURSE_RE`），所以 `/api/course/abc` 会掉到第 12 条当静态资源处理，返回 404 JSON 而不是报错。

---

## 4. 媒体服务（`_file`，本项目的性能核心）

### 4.1 Range 解析

```python
partial = bool(rng) and kind == 'videos'      # 只有视频支持分段
start, end = 0, size-1
m = re.match(r'bytes=(\d*)-(\d*)', rng)
```

- 只支持 `bytes=start-` / `bytes=start-end` / `bytes=-end` 的常见形态；`bytes=` 后缀空值按 0 处理。
- `start/end` 都 clamp 到 `[0, size-1]`，越界不报 416 而是截断（浏览器的 Range 请求常常带超出末尾的 end）。
- 有 Range 且是视频 → **206 + `Content-Range: bytes s-e/size`**；否则 200 + 全长。
- 任何响应都带 `Accept-Ranges: bytes`，这是播放器敢拖动的前提。

### 4.2 读盘与写出

```python
while remaining > 0:
    chunk = f.read(min(1024*1024, remaining))   # 1 MiB/块
    if not chunk: outcome='short_read'; break
    self.wfile.write(chunk)
    _record_served(len(chunk))
```

1 MiB 是块大小与 `wfile` 缓冲的折中：太小 syscall 过多，太大则「客户端断开」的响应延迟变高（用户快速拖动时更明显）。

### 4.3 快进 seek：`posix_fadvise(WILLNEED)`

```python
if partial:
    f.seek(start)
    os.posix_fadvise(f.fileno(), start, min(LOOKAHEAD, size-start), FADV_WILLNEED)
else:
    os.posix_fadvise(f.fileno(), 0, length, FADV_SEQ)
```

- `LOOKAHEAD = 64 MiB`：把 seek 点往后 64MB 一次性提示内核预读进 page cache。跳播首字节从「等磁盘」变成「等内存」。
- 全量请求用 `FADV_SEQ`（顺序访问提示），让内核预读窗口从默认 128KB 提到更大。
- `FADV_WILLNEED` 只是**提示**，不阻塞；真正读不到的页仍会触发磁盘 IO，但不会再走一遍探测。

### 4.4 整文件预热（`ensure_warm`）

```python
if kind == 'videos': ensure_warm(path)     # 首次播放自动触发
```

- 守护线程 8 MiB/块读完整个文件，只为了把内容塞进 page cache，读完即丢。
- `_warmed` 集合去重：同一文件只会起一次预热线程。
- 也可手动 `GET /api/warm/<teclId>/<file>` 触发（选课时提前预热，首帧更快）。
- 代价：**首次播放会额外读一遍全文件**。对于 1–2GB 的课，等于多一次顺序读 IO；换来的是之后任意 seek 全是内存命中。磁盘是 SSD/内存充裕时非常划算。

### 4.5 果断放弃（`WRITE_TIMEOUT = 60s`）

```python
self.connection.settimeout(WRITE_TIMEOUT)
...
except (BrokenPipeError, ConnectionResetError, socket.timeout):
    _bump_aborted(); outcome='interrupted'; self.close_connection=True
```

- 用户拖动进度条时，浏览器会 `abort` 旧请求。服务端 write 立刻 `BrokenPipe`，线程干净退出，不占着 worker 写没人要的字节。
- 超时从早期 5s 调到 **60s**：浏览器缓冲写满后会合法地暂停读取几十秒（尤其高倍速 + 大缓冲），5s 会把正常播放误杀成 `interrupted`。60s 仍不是零卡顿的保证，只是降低误杀率。
- `finally` 里恢复 `settimeout(None)`，避免影响 keep-alive 的后续请求。

### 4.6 播放日志（`playback.jsonl`）

```json
{"record":"transfer_window","teclId":10730,"file":"wk02_..._1.mp4","seconds":15.0,"bytes":...,"bytes_per_second":...,"ts":...}
{"record":"transfer_end","teclId":10730,"file":"...","range_start":0,"range_end":...,"bytes":...,"seconds":...,"bytes_per_second":...,"outcome":"complete|interrupted|short_read","ts":...}
{"record":"player","teclId":...,"event":"waiting","rate":1.5,"buffered_depth_s":2.1,...}
```

- `transfer_window` 每 15 秒一条，`bytes_per_second` 是**服务端向套接字写入的速率**，不等于浏览器实际收到/解码的速率（TCP 发送缓冲会吸收差值）。
- 轮转：10 MiB × 5 备份，`delay=True` 表示首次写入才建文件。
- 写日志失败（磁盘满、序列化失败）全部静默吞掉，不能因为诊断拖垮播放。

---

## 5. 统计、历史、进度、卡顿

### 5.1 流量统计（`/api/stats`）

5 秒滑动窗口（`deque` + `_WINDOW=5.0`），速率 = 窗口首尾样本差 / 时间差：

```json
{"now":1762..., "active_connections":2,
 "total_bytes_served":..., "total_bytes_human":"12.3 GB",
 "total_requests":..., "seek_requests":..., "aborted_requests":..., "warmed_files":...,
 "speed_bps":..., "speed_human":"38.4 MB/s", "window_s":5.0}
```

| 计数器 | 语义 |
|---|---|
| `seek_requests` | 处理过的 partial（Range）请求数 |
| `aborted_requests` | 被客户端掐断的流数（快速拖动/关页面） |
| `warmed_files` | 已触发预热的文件数（集合大小，不清零） |
| `active_connections` | 当前在处理的请求数（`do_GET` 进出 `±1`） |

> 注意：`speed_human` 是**全局**速率，包含所有设备的所有流。多端同看时它会偏高，不能直接当「我这个页面的网速」用。

### 5.2 观看历史（`/api/history`，内存）

`_record_watch()` 在每个视频响应头发出后调用：用 `Range` 的 `end` 字节数按比例折算播放位置：

```python
pos = int(end * duration / size)   # duration 来自 manifest 的 channels[].duration_s
```

- `prev['end'] > end` 时保留较大值（拖动回退不会让记录倒退）。
- 只存内存，**重启即清空**；`_duration_for()` 的 manifest 缓存现在按 `(mtime, size)` 失效（2026-11-12 修），增量下载改了 manifest 不必重启。
- 这是「猜出来的」观看记录，与 5.3 的权威进度是两套东西，别混用。`/api/history` 已在响应里标 `deprecated:true`：浏览器一开口就要到文件尾，折算出来的 `position_s` 天然偏大。

### 5.3 播放进度（`/api/progress`，落盘）

文件：`backend/progress.json`

```json
{"version":1,
 "last":{"teclId":10730,"sid":136658,"t":1832,"ts":1762700000000,
         "main":"1-teacher1","pip":"5-ppt","audio":"1-teacher1","rate":1,"subs":true},
 "sessions":{"10730:136658":{"t":1832,"ts":1762700000000}},
 "updated":1762700000000}
```

**写入（`_merge_prog`）规则**：

1. `teclId`/`sid` 任一为空 → 拒绝（返回 `ok:false`）。
2. `t` 取整并 clamp ≥0；`ts` 缺省用**当前毫秒**（以前缺省传秒，会永远被判成旧上报而丢弃）。
3. **`ts` 先过 `_sanitize_ts()`**：秒级时间戳自动 ×1000；比 now 超前 >5s 的夹到 now。客户端时钟超前（实测出现过 +9 天）不能把这一节的进度永久锁死。
4. 该节已有记录的 `ts` **大于**新 `ts` → 丢弃（防乱序上报把进度改回去）。
5. 更新 `sessions["<teclId>:<sid>"]`（只存 `t` 和 `ts`）；若 `last.ts <= 新 ts` 则整体替换 `last`。
6. `sessions` 超 800 条按 `ts` 倒序截断。
7. `tmp + os.replace` 原子落盘（kill -9 也不会写坏文件），整段在 `_lock` 内完成。

> 单位统一：`last.ts` / `sessions[].ts` / `updated` 都是**毫秒**（2026-11-12 前 `updated` 是秒，混用容易看错）。

**读取（`_prog_view`）**：返回 `{version, last, sessions, updated}`，供新浏览器/新设备冷启动 hydrate。

前端策略是「本地 localStorage 优先，后端只在新于本地时才覆盖」，所以后端挂了不影响续播，只是不能跨设备。

### 5.4 卡顿感知（A 客户端 + B 服务端）

**A：客户端上报**（`POST /api/telemetry`）

```json
{"teclId":10730,"file":"wk02_..._1.mp4","event":"waiting|stalled",
 "buffered_depth_s":2.1,"readystate":3,"rate":1.5,"position_s":1830,
 "total_frames":...,"dropped_frames":...,"visible":true}
```

- `waiting` → `client_wait_count++`；`stalled` → `client_stall_count++`。
- 每条记录保留最近 50 个 `episodes`，供 `/api/stutter` 看时间线。
- 白名单字段才写 `player` 日志，防止前端塞任意字段把 JSONL 撑大。

**B：服务端推断**（`_infer_rebuffer`）

```python
if rec['max_end'] > 0 and (rec['max_end'] - start) > REBUF_GAP and end >= start:
    rec['rebuffer_count'] += 1        # REBUF_GAP = 5 MiB
```

含义：某个文件的读取位置已经推进到 `max_end`，新请求却从 `max_end - 5MB` 之前重新要数据 → 说明客户端把已缓冲的数据用完了、回头重取，即一次 rebuffer。

`/api/stutter` 汇总：

```json
{"aware_of_stutter":true,
 "total_rebuffer_inferred":3, "total_client_waiting":7, "total_client_stalled":0,
 "streams":[{... per file: rebuffer_count, client_wait_count, last_buffered_depth_s, episodes[]}]}
```

A 和 B 互补：B 抓不到「本地缓冲瞬间耗尽但服务端字节还没发」的短卡顿，A 抓不到「浏览器根本没上报」的情况（比如原生播放器、别的客户端）。

---

## 6. 科达原版 SPA 适配层（`_keda_api`）

目的：让学校原版播放中心（Vue SPA，`frontend/original-vod-prototype/`）在本地数据上跑起来。所有响应统一包成科达信封：

```python
def keda_wrap(data):
    return {'code':None,'data':data,'dataOk':True,'message':None,'ok':True,'status':200}
```

| 科达接口（后缀匹配） | 本地来源 |
|---|---|
| `/v1/config/vodNmediaConfigInfo` | 媒体源指向 `http://127.0.0.1:18080` |
| `/v1/teachingClass/<id>/t-1` | catalog 里的课名 |
| `/source/vod/lastPlayInfoByTeclId` | `vod_progress` cookie，命不中给第一节 |
| `/subject_vod_list_new` | manifest 节次，按 `time` **倒序**，`week` 由日期换算 |
| `/v1/course_vod_urls_new` | 视频 URL `http://127.0.0.1:18080/media/<teclId>/videos/<basename>`（`quote` 编码），带 `lastPlayTime` |
| `/v1/course_vod_subtitle_with_keyword` | 解析 SRT → `courseTransferEsDtoList`（`bg/ed` 毫秒） |
| `/v1/course/ai/ppt` | `nodes.json` 的 `slides` → `docList` |
| `/course/ai/textSummary/<id>` | `nodes.json` 的 `summary` / `mindmap` |
| `/course/ai/translate/<id>` | 前后都用同一份字幕（无翻译） |
| 权限/字典/样式/热力图/未读/收藏/提问/笔记等 | 返回空数组、`{}`、`false` 或默认值，让 SPA 正常走通 |

- `progress` 从 `Cookie: vod_progress`（URL 编码的 JSON）读，字段 `teclId/courseId/position_s`。
- 任何异常 → 打印 `ADAPTER ERROR <path> <Type> <msg>`，返回 `data:null`，**不抛到 HTTP 层**。
- 未命中 → 空对象，而不是 404：SPA 对 404 会弹全局错误。

`_keda_static` 出 SPA 静态：绝对路径 normpath 后必须仍在 `UI_DIR` 内（防目录穿越），无扩展名回退 `index.html`（hash 路由）。

---

## 7. 下载链路（增量更新的来源）

播放阶段不需要鉴权，但**下载阶段要**：JWT Bearer + AES 解密播放 URL。

### 7.1 铸 token（`scripts/mint_vod_token.py`）

- 从保存的 normandy cookie（`credentials/normandy.txt`）出发，重放 LTI 1.3 六跳链（oc.sjtu.edu.cn 外部工具 → authorize → launch）。
- 得到 `jwt_token`，解出 `accessToken` 与 `exp`，写入 `blockchain-apps/scripts/{jwt.txt,bearer.txt}`。
- **有效期约 24h**；过期重跑。若 normandy 会话也失效，需在浏览器重新登录并更新 cookie 文件。

### 7.2 增量更新（`scripts/update_all.py`，15 KB）

```bash
python3 ~/vod-research/scripts/update_all.py            # 全部课程，缺什么补什么
python3 ~/vod-research/scripts/update_all.py --dry-run  # 只列差异
python3 ~/vod-research/scripts/update_all.py --tecl 10730 8537
python3 ~/vod-research/scripts/update_all.py --refresh  # 强制铸新 token
python3 ~/vod-research/scripts/update_all.py --no-nodes # 不补 PPT 节点
```

**判定规则（全部基于本地已有文件）**：

| 缺口 | 判据 |
|---|---|
| 新节次 | 远端 `subject_vod_list_new` 的 `id` 不在本地 `manifest.json` |
| 缺频道 | manifest 里该频道 `file` 不存在 / 大小为 0 / 有残留 `.part` |
| 缺字幕 | `subtitles/<label>.srt` 不存在或为空 |
| 缺节点 | `<label>.nodes.json` 不存在 → 交给 `backend/fetch_nodes.py` |

**断点续传（`download_file`）**：

```python
tmp = dest + '.part'
if os.path.isfile(dest) and not os.path.isfile(tmp) and os.path.getsize(dest) > 0:
    return os.path.getsize(dest)          # 已完成，直接跳过
have = os.path.getsize(tmp)               # 有 .part → 续传
headers['Range'] = 'bytes=%d-' % have
# 206 → 'ab' 追加；200 → 服务端不支持 Range，'wb' 重下
os.replace(tmp, dest)                     # 原子落地
```

- 重试 10 次，退避 `min(120, 2**attempt)` 秒。
- token 在 `ensure_token()` 里检查：剩余 < 600 秒自动重铸；`--refresh` 强制。
- `401/403` 直接 `die(AUTH_HINT)`，不浪费重试次数。
- AES-128-CBC：key `jy-resource-qry1`，IV `jy-query-iv-0001`（解播放中心 URL 的加密参数）。
- 频道映射：`viewNum 1..7 → teacher1/teacher2/student1/student2/ppt/whiteboard/composite`。
- manifest 每完成一节就 `write_json_atomic` 一次 → 中断不丢已完成进度，重启后自动只补剩余。
- 单文件下载用 `ProxyHandler({})` 直连（绕过集群代理，否则大文件会被代理截断/超时）。

### 7.3 PPT 节点（`backend/fetch_nodes.py`）

- 遍历 catalog 全部课全部节，调 `/v1/course/ai/ppt`（`docList`: `imageId/imageSeekTime/ocrText/textKeywordList/showFlag`）与 `/v1/course/ai/textSummary/<id>`（`summary`/`mindmap`）。
- 写入 `<课>/subtitles/<label>.nodes.json`；**已存在则跳过**（增量），3 线程并发。

---

## 8. 数据契约

### 8.1 `backend/catalog.json`

8 门课，字段 `{teclId, name, dir, canvasId}`。后端据此定位课程目录、判断 ready/downloading。

```json
[{"teclId":10730,"name":"区块链技术及应用","dir":"blockchain-apps","canvasId":94157}, ...]
```

当前含：工程材料 / 区块链技术及应用 / 测试原理与技术 / 大数据算法与分析 / 工程流体力学（A类）/ 毛泽东思想和中国特色社会主义理论体系概论 / 设计与制造Ⅱ / 西方音乐文化史。

### 8.2 `<课>/manifest.json`

```json
{"course":"区块链技术及应用","teclId":10730,"auth":"Bearer accessToken",
 "sessions":[{"id":136658,"time":"2026-09-15 10:00:00","week":2,
   "label":"wk02_20260915_id136658","subtitle_lines":412,
   "channels":[{"channel":"teacher1","viewNum":1,"duration_s":5700,
                "file":"~/vod-research/<课程>/videos/wk02_20260915_id136658_1.mp4",
                "size_bytes":1234567890}]}]}
```

- `file` 是**绝对路径**，前端只取 basename。
- `/api/courses` 的 `status`：有 `manifest.json` → `ready`，否则 `downloading`（前端只显示 ready）。
- `sessions` 由 `update_all.py` 每节结束后按 `time` 升序重写。

### 8.3 目录布局

```
~/vod-research/<课目录>/
├─ videos/     wkXX_YYYYMMDD_id<sid>_<viewNum>.mp4
├─ subtitles/  <label>.srt  <label>.nodes.json
├─ audio/      （预留，当前未下载）
├─ logs/
└─ manifest.json
```

`label = wk%02d_%s_id%s`（周次、日期、节次 id）；周次缺失时按年内第几天 `//7+1` 估算。

---

## 9. 运维

### 9.1 启动 / 重启

```bash
ssh <host> ~/vod-research/backend/run.sh          # 两个都重启
ssh <host> ~/vod-research/backend/run.sh 18080    # 只重启主后端（不动 SPA 宿主）
python3 ~/vod-research/backend/vodctl.py status   # 看端口/pid/在监听的谁
```

`run.sh` / `start-all.sh` 现在都只是 `backend/vodctl.py` 的薄包装：

- **按进程 cwd 认领**（18080 的 cwd 是 `backend/`，18766 的 cwd 是 `frontend/original-vod-prototype/`），不再用 `pkill -f server.py` 一把梭——两个主程序同名，以前只能一起杀。
- pidfile 在 `backend/run/<port>.pid`；没写 pidfile 的老实例也能按 cwd 认出来。
- 启动方式仍是 `setsid` 脱离会话，进程挂到 init 下，DSH/SSH 重启不影响。
- 日志：`/tmp/huanyuan.log`（18080）、`/tmp/keda18766.log`（18766）。
- **cron 保活**：`*/5 * * * * .../backend/start-all.sh`，只在端口不通时拉起（`flock` 防并发），挂掉最多 5 分钟自愈。

### 9.2 验证

```bash
curl -s  127.0.0.1:18080/api/version       # 先确认跑的是哪一版（v1.8 / pid / 超时值）
curl -s  127.0.0.1:18080/api/courses | head -c 400
curl -sI -H 'Range: bytes=0-1' \
     127.0.0.1:18080/media/10730/videos/wk02_20260915_id136658_1.mp4   # 期望 206 + Content-Range
curl -s  127.0.0.1:18080/api/stats
curl -s  127.0.0.1:18080/api/progress
```

### 9.3 排障

| 现象 | 排查 |
|---|---|
| 18080 不通 | 先 `run.sh`；再看 `/tmp/huanyuan.log` 有无 traceback（无 traceback 多半被外部 SIGKILL） |
| 视频 404 | `course_dir(teclId)` 是否命中 catalog；`videos/<basename>` 是否真的存在 |
| 拖动很慢 | `/api/stats.speed_human` 是否压得住码率；压不住 = 隧道带宽，不是后端 |
| 首帧慢 | 是否触发了预热（`warmed_files` 增长）；首次播放必然要读全文件 |
| 频繁 `aborted_requests` | 正常（拖动导致）；若伴随卡顿则为 60s 超时仍在误杀，或前端在反复重载 |
| 新节次页面不显示 | 后端每次请求实时读 manifest，**不需要重启**；若显示旧数据，是浏览器缓存或前端未刷新 |
| 进度不跨设备 | `progress.json` 是否可写（权限/磁盘）；`GET /api/progress` 的 `last.ts` 是否在增长 |
| SPA 黑屏转圈 | F12 Console；`ADAPTER ERROR` 在 `/tmp/huanyuan.log`；接口是否 404（未适配的新端点） |

**pgrep 陷阱**：用 `pgrep -f 'python3 -u server\.py'`，别用 `pgrep -f server.py`（会匹配到编辑器/命令行自身）。

---

## 10. 关键参数表（调优入口）

| 常量 | 值 | 位置 | 影响 |
|---|---|---|---|
| `LOOKAHEAD` | 64 MiB | 第 32 行 | seek 预取窗口；越大跳播越顺，page cache 压力越大 |
| `WRITE_TIMEOUT` | 60.0 s | 第 33 行 | 写超时；太小误杀正常缓冲，太大卡住线程 |
| `_WARM_CHUNK` | 8 MiB | 第 129 行 | 整文件预热的读块 |
| `REBUF_GAP` | 5 MiB | 第 127 行 | 服务端判定 rebuffer 的回退阈值 |
| `_WINDOW` | 5.0 s | 第 55 行 | 速率滑动窗口 |
| 分块 | 1 MiB | `_file` | 读写粒度 |
| `sessions` 上限 | 800 | `_merge_prog` | progress.json 不会无限膨胀 |
| `episodes` 上限 | 50/文件 | `_stutter` | 卡顿时间线长度 |
| 日志轮转 | 10 MiB × 5 | 第 37 行 | 最多占 60 MiB |

---

## 11. 设计取舍与已知限制

1. **只绑 127.0.0.1，无鉴权**。安全边界完全由 SSH 隧道提供；不要把 `HOST` 改成 `0.0.0.0` 暴露到校园网。
2. ~~`_manifest_cache` 不失效~~ **已修（2026-11-12）**：`_duration_for()` 的缓存按 `(mtime, size)` 失效，增量下载改了 manifest 之后不必重启。
3. **`/api/history` 与 `/api/progress` 语义不同**，别把前者当续播依据：history 是字节反推、内存、会清空；progress 是前端主动上报、落盘、带 `ts` 防乱序。
4. **`transfer_window.bytes_per_second` ≠ 用户网速**。它是写入套接字的速率，TCP 发送缓冲会吃掉差值。真实体感要看前端 `playback-telemetry` 的 `observed_rate` / `buffered_depth_s`。
5. **`possible_frame_freeze` 只是线索**。页面可见 + 媒体时间在推进 + 帧计数不变 → 可能是画面真的静止（比如 PPT 一页讲十分钟），也可能只是渲染问题，不能自动判定故障。
6. **`/` 指向极简页后，科达 SPA 仍可访问**：`/jy-application-resourcemanage-ui/`（18080 同源），或 18766 的薄宿主（静态托管 + 反代）。两条入口的接口实现已经统一，不再有两份适配代码。
7. **Python 3.6 限制**：不能用 f-string、`dataclasses`、`asyncio.run`、`dict` 有序保证（3.7+ 才正式）；本文件都避开了。若要升级到 3.10+，主要改动点是用 `ThreadingHTTPServer` 替换手写 `ThreadingMixIn`。
8. **两路视频 = 两条独立流**。服务端不做任何合并/复用时序，同一节课看两个通道就是两份 IO + 两份带宽。这是「主屏+小屏」的固有成本。
9. **没有断点续播的服务端 session 概念**。进度是「最后位置」而非「多端各自位置」，两台设备同时看会互相覆盖 `last`（`sessions` 里按节保留，但仍只存一个 `t`）。

---

## 12. 版本演进

| 版本 | 内容 |
|---|---|
| v1.1 | `/api/stats` + `/api/history` |
| v1.2 | 卡顿感知：`/api/stutter` + `/api/telemetry`（A 客户端 + B 服务端推断） |
| v1.3 | 快进 seek（WILLNEED 64MB）+ 多倍速友好（TCP 流控 + 激进预热）+ 5s 果断放弃 |
| v1.4 | 整文件预热 `/api/warm` |
| v1.5 | 前端静态托管（`_static` + `EXT_CT`） |
| v1.6 | 合并科达 SPA 适配层（`_keda_static` + `_keda_api` + 全部端点） |
| v1.7 | 播放进度持久化 `/api/progress` + `backend/progress.json`；`/` 改指极简播放页 |
| v1.8（2026-11-12 上线） | 写超时 5s→60s；`playback.jsonl` 传输/播放诊断；`ts` 钳制、`/api/history` deprecated、manifest 缓存按 mtime、`/api/version`；18766 改为纯反代 |

> 教训：v1.8 的代码 11-11 就写好了，但只在一个 18081 的临时实例上验证过，**18080 一直跑着 11-10 的旧进程**（横幅写着 5s 超时）。判断「跑的到底是不是磁盘上的代码」现在有 `/api/version` 一句话，别再靠猜。

---

## 13. 变更记录

### 2026-11-12 维护（v1.8 上线）

| 类别 | 改动 | 原因 |
|---|---|---|
| P0 | 真正重启到 60s 写超时 | 旧 5s 超时下，浏览器缓冲写满 5 秒就被当「用户放弃」掐断。实测：64 MiB Range、客户端静默 8s，旧版只发 4 MiB 就断且 `aborted+1`；新版 67108864 字节全给完、`aborted` 不动 |
| P0 | `_sanitize_ts()` + `fix_progress.py` | `progress.json` 里有 7 条记录的时间戳在未来（最远 +9 天，客户端时钟超前），`_merge_prog` 只收「更新」的 ts，于是那几节的进度**永久写不进去**，`last` 被钉死，前端 `hydrate()` 每次把本地覆盖回旧位置。规则改为：秒级时间戳自动 ×1000，超前 >5s 夹到 now |
| P0 | `/api/history` 标 `deprecated` | 位置是 `range_end × 时长 / 大小` 反推的，浏览器一开口就是「到文件尾」，数值天然偏大 |
| P1 | 18766 去重 | 原文件 109 行把科达适配整段复制了一遍（与 18080 的 `_keda_api` 逐分支重复且已漂移）。现在只做「静态托管 + 反代」，适配逻辑全系统一份 |
| P1 | `vodctl.py` + cron 保活 | 两个服务同名 `server.py`，`pkill -f` 只能一起杀；改为按 cwd 认领，可单端口重启；cron 每 5 分钟探活 |
| P1 | 版本号统一、`/api/version` | 以前 Header 1.6 / 横幅 v1.4 / 实际 v1.7 |
| P2 | rebuffer 推断排除 `start==0` | 用户点回片头会被算成一次 rebuffer（实测两次从 0 起的请求让计数 +2），数字虚高 |
| 工程 | `~/vod-research` 纳入 git | 之前没有版本控制，「跑的是旧代码」只能靠日志横幅推断 |

### 2026-11-18 课件通道改走图片流

| 改动 | 细节 |
|---|---|
| 新增路由 | `GET /api/slides/<teclId>/<label>` 返回页索引 `{count,pages:[{t,file}]}`；`GET /media/<teclId>/slides/<label>/NNN.jpg` 返回单页图片（`CTYPE['slides']='image/jpeg'`，`MEDIA_RE` 的 kind 增加 `slides`） |
| 数据来源 | `scripts/ppt_to_pdf.py --emit-images`：有 `nodes.json` 时用平台 `imageSeekTime`（3–14 s/节），没有则缩略图差分切页（~85 s/节）；产物 `<课程>/slides/<label>/{index.json,NNN.jpg}`，平均 **150 KB/页** |
| 前端 | 选节时并行取索引，命中就把小画面换成 `<img>` 并按主画面 `currentTime` 二分查找切图（预取下一张），**这一路视频流不再发送**；PPT 若被选成主画面会与视频换位；⤢ 放大；互换/换通道时退回视频流 |
| 实测 | 一次播放只剩 `teacher1.mp4` 一路传输；课件侧从 1 Mbps 连续流（398 MB/节）降到 2.9–3.2 MB/节 |

> 注意：这是「预生成图片」而不是实时转码，所以新增/重下一节课后要跑一次
> `python3 scripts/emit_all_slides.py --course <dir>`（幂等，已导出的跳过）。
