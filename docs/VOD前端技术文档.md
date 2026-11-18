# VOD 换源系统 · 前端技术文档

> 对应文件：`~/vod-research/frontend/index.html`（单文件，813 行 / 37 KB，无构建、无依赖）
> 访问地址：`http://127.0.0.1:18080/index.html`（后端 18080 静态托管，`Cache-Control: no-cache`）
> 配套后端文档：`VOD后端技术文档.md`
> 文档基准：2026-11-12 首帧提速 + 埋点后（见第 13 节变更记录）

---

## 0. 设计约束（先看这段，能省很多返工）

| 约束 | 原因 | 后果 |
|---|---|---|
| **单 HTML 文件，CSS/JS 全内联** | 通过 SSH 隧道远程维护，无法跑 npm/vite | 改完刷新即生效；不能用模块化、不能用 TS |
| **不依赖科达 kmedia 播放器** | kmedia 对「页面源 ≠ 媒体源」的跨源场景会拿到 URL 后卡死不请求 | 只用原生 `<video>` + 渐进式 MP4 |
| **不写 `<video controls>`** | 原生控件会浮在画面上、各浏览器 UI 不一致、双画面无法联动 | 自绘一条平铺控制条（`.bar`） |
| **浏览器自动播放策略** | 无用户手势时 `play()` 会被拒绝，表现为「一个画面在动、一个不动」 | `userActed` 标志位；首次进入只 seek 不 autoplay |
| **只播 mp4（渐进式）** | 媒体是本地文件直出，不是 HLS | `.m3u8` 不可用；seek 依赖服务端 Range |

---

## 1. 页面结构

```
body
├─ header.top                  品牌 / 课程下拉 / 主·小通道下拉 / 当前节次 / 连接状态
└─ main.layout  (grid: 1fr | 272px, height=100vh-48px)
   ├─ section.stage
   │  ├─ div#screen             ← 全屏作用域（播放器 + 控制条）
   │  │  ├─ div#player
   │  │  │  ├─ video#vMain     主画面（slot 0）
   │  │  │  ├─ div#subs        字幕叠加层（z-index:4）
   │  │  │  ├─ div#hint        空态提示
   │  │  │  └─ div#pip         悬浮小窗（z-index:3）
   │  │  │     ├─ div#pipBar   拖拽条 + 通道名 + 🔊 声音跟随
   │  │  │     └─ video#vPip   小画面（slot 1，默认 muted）
   │  │  └─ div.bar            播放/时间/进度/倍速/互换/字幕/全屏
   │  └─ section.panel.ppt     PPT 跳转切片条（.chips）
   └─ aside.side               (flex column)
      ├─ .side-pane  → 节次列表 #slist
      └─ .side-pane  → 语音逐句 #vlist
```

CSS 变量集中在 `:root`：`--ink #1f2a32`、`--muted #7c8a92`、`--line #e5ebee`、`--bg #f4f6f7`、`--accent #2f6b7f`、`--soft #e8f2f4`。改主题只改这一处。

**布局让位逻辑**：header 48px、padding 10px、右栏 272px、间距 8px；播放器 `flex:1` 吃满剩余高度，`min-height:180px` 兜底。因此窗口变矮时先压 PPT 条和语音面板，不压视频。

---

## 2. 数据模型

### 2.1 全局状态

```js
const slots = [
  {v:$('vMain'), sel:$('mainSel'), ch:null},   // 0 = 主画面
  {v:$('vPip'),  sel:$('pipSel'),  ch:null}    // 1 = 悬浮小画面
];

let courses=[];      // GET /api/courses → d.courses[]
let course=null;     // 当前课程 {teclId,name,dir,...}
let manifest=null;   // GET /api/course/<teclId> → {course,teclId,sessions[]}
let session=null;    // 当前节次 {id,time,week,label,channels[]}

let leader=0;        // 双画面同步的"基准画面"槽位
let guard=false;     // 同步豁免窗口（防止 seek 触发对方反向 seek）
let dragging=false;  // 进度条拖动中，暂停回写
let userActed=false; // 是否发生过用户手势（决定能否 autoplay）

let nodes=[];        // PPT 节点 [{t, el}]
let cues=[];         // 字幕/语音 [{s, e, text, el}]
let curNode=-1, curCue=-1, curSub=-1;
let userScrolledAt=0;// 用户手动滚动的时刻，用于 2.5s 内不抢滚动
let subsOn=true, audioSlot=0;
```

`slots[].ch` 的元素来自 `session.channels`：`{channel:'teacher1', viewNum:1, file:'/abs/path.mp4', duration_s, size_bytes}`。

### 2.2 通道命名

```js
const NAMES = {teacher1:'教师画面', teacher2:'教师画面 2', student1:'学生画面 1',
               student2:'学生画面 2', ppt:'课件画面', whiteboard:'板书画面', composite:'综合画面'};
```

`viewNum 1..7` ↔ 上面 7 个 channel（后端下载阶段同一套映射）。下拉框 `value` 用 **`viewNum-channel` 复合键**，因为同一节里 channel 名理论上可能重名，而 viewNum 一定唯一。

### 2.3 媒体地址

manifest 里 `channels[].file` 是 **<node> 上的绝对路径**，前端只取 basename 再拼接：

```js
const base  = p => String(p||'').split(/[\\/]/).pop();
const media = (id,kind,f) => API+'/media/'+encodeURIComponent(id)+'/'+kind+'/'+encodeURIComponent(f);
// 例：/media/<teclId>/videos/wk02_20260915_id<sid>_1.mp4
```

`API` 的推导：只有当页面本身跑在 `127.0.0.1:18080/18081` 时才用 `location.origin`，否则硬编码 `http://127.0.0.1:18080`（兼容从 18766 或 `file://` 打开的情况）。

---

## 3. 双画面播放器

### 3.1 为什么需要 `guard`

两路视频是**独立的两条 HTTP 流**。任何一侧的 seek 都会触发对方的 `seeking` 事件，如果不加抑制，就会互相拉扯成死循环。做法：

- `seekAll(t)` 主动 seek 时 `guard=true`，160ms 后释放；
- `setChannel()` 换源时 `guard=true`，`loadedmetadata` 里补 seek 后释放（4s 超时兜底）；
- 所有同步逻辑（`leader` 认领、漂移校正）在 `guard` 期间直接 return。

### 3.2 播放/暂停联动

```js
slot.v.addEventListener('play', () => {
  if(!guard) leader = i;                  // 谁被用户点了谁是基准
  setPlayIcon(true);
  slots.forEach((o,j)=>{ if(j!==i && alive(o) && o.v.paused) o.v.play().catch(()=>{}) });
});
slot.v.addEventListener('pause', () => {
  slots.forEach((o,j)=>{ if(j!==i && alive(o) && !o.v.paused) o.v.pause() });
  if(!playing()) setPlayIcon(false);
  saveState();
});
```

- `playing()` = 任一路在播；`togglePlay()` 在「全停 / 全播」之间切，而不是逐路取反，避免两路状态发散。
- `play()` 返回 Promise，自动播放被拦会 reject，所以一律 `.catch(()=>{})`，不弹错。

### 3.3 漂移校正（900ms 一次）

```js
setInterval(()=>{
  if(!alive(a)||!alive(b)||guard) return;
  if(a.v.paused||b.v.paused||a.v.readyState<1||b.v.readyState<1) return;
  const ref = leader===0? a.v : b.v, fol = leader===0? b.v : a.v;
  if(!fol.seeking && Math.abs(fol.currentTime-ref.currentTime) > 0.45) {
    try{ fol.currentTime = ref.currentTime }catch(_){}
  }
},900);
```

阈值 **0.45s**：小于一帧的音频相位差听不出来，但超过半秒的口型/翻页不同步很明显。周期性硬 seek 会打断播放器缓冲，所以不能每帧做。

### 3.4 主/小画面互换（`swap()`）

三处入口，行为等价：

1. 点小窗画面（`#vPip` 的 click）；
2. 控制条「⇄ 互换」；
3. 快捷键 `S`。

```js
function swap(){
  const a=slots[0].ch, b=slots[1].ch;
  if(!a||!b) return;                          // 只有一个通道时无可换
  const t = leaderVideo() ? leaderVideo().currentTime : 0;
  audioSlot = audioSlot===0 ? 1 : 0;          // 声音跟着"原画面"走
  setChannel(0,b,t);                          // 两路都从同一时间点重建
  setChannel(1,a,t);
  leader = 0;
  saveState();
}
```

注意：**不是交换两个 `<video>` 元素的位置，而是交换通道后重新 load**。这样 DOM 结构稳定（小窗永远在 `#pip` 里），代价是互换瞬间有一次 `loadedmetadata`（约 100–300ms 黑帧），换来的是不会出现「同一个 video 元素被 CSS 换位置导致全屏/拖拽坐标错乱」。

### 3.5 小窗拖拽

用 Pointer Events（鼠标/触摸/笔统一），不用 `dragstart`：

```js
pipBar.pointerdown → 记录起点 + setPointerCapture
pipBar.pointermove → 位移>3px 视为拖拽(pipMoved=true)，clamp 在 #player 内
pipBar.pointerup   → 若 !pipMoved 则视为点击 → swap()
```

- **3px 阈值**是把「点一下互换」和「按住拖走」区分开的关键，否则手抖就换画面。
- 位置用 `left/top` 绝对定位，首次拖拽时把默认的 `right/bottom` 置为 `auto`。
- `resetPipPos()` 在切节、互换、只剩一路画面时调用，回到右上角默认位。

### 3.6 声音归属（`audioSlot`）

两路视频只有一路 `muted=false`。`applyAudio()` 把 `audioSlot` 指向的那路开声，另一路静音；小窗标题条的 🔊 切换，并在顶栏 flash 一句提示。互换时 `audioSlot` 一起取反，保证「声音还是从原来那个通道出来」。

---

## 4. 字幕（叠在画面上）

### 4.1 渲染

```css
.subs{position:absolute;left:50%;transform:translateX(-50%);bottom:14px;
      width:max-content;max-width:78%;font-size:18px;line-height:1.55;
      color:#fff;background:rgba(0,0,0,.62);border-radius:7px;
      text-shadow:0 1px 3px rgba(0,0,0,.95),0 0 10px rgba(0,0,0,.55);
      z-index:4;white-space:pre-line;pointer-events:none}
```

`pointer-events:none` 必须保留——否则字幕条会挡住「点画面暂停」。

```js
function cueIndexAt(t){
  for(let i=0;i<cues.length;i++){
    const c=cues[i];
    if(c.s<=t && t<=c.e) return i;
    if(c.s>t) break;              // 时间升序，超了就一定没有
  }
  return -1;
}
```

`renderSubs(t)` 只在 `curSub` 变化时改 DOM；`subsOn=false` 时清空并隐藏（对应控制条「字幕：开/关」，快捷键 `C`）。

### 4.2 SRT 解析

`parseSRT()` 的处理点：

| 步骤 | 说明 |
|---|---|
| 去 BOM | `replace(/^\uFEFF/,'')`，否则首块序号正则不匹配 |
| 统一换行 | `\r\n?` → `\n` |
| 分块 | 以空行 `\n{2,}` 切 |
| 序号行 | 首行纯数字则跳过，不依赖序号连续 |
| 时间行 | `HH:MM:SS[,.]mmm --> HH:MM:SS[,.]mmm`，逗号/点都吃 |
| 正文 | 多行合并为空格分隔，去 HTML 标签（`<[^>]*>`） |

时间转秒后按 `s` 升序（源文件本身有序，这里不再排序，避免误伤）。

### 4.3 双来源兜底

```js
async function fetchCueList(s){
  // 1) 本地 SRT
  fetch(media(course.teclId,'subtitles', s.label+'.srt'))
  // 2) 本地没有 → 科达适配接口（与 SPA 同一份数据）
  fetch(API + '/jy-application-resourcemanage/v1/course_vod_subtitle_with_keyword'
        + '?courseId='+s.id+'&teclId='+course.teclId)
  //    bg/ed 是毫秒 → /1000；字段 res 或 zh
  return [];
}
```

两路都空时，右栏语音面板显示「本节无语音文本（字幕文件缺失）」，不再静默空白。

> 历史坑：早期版本 `cues.map()` 返回的是 `{s,e,el}`，丢掉了 `text`，于是叠加层 `cues[ci].text` 永远是 `undefined`——列表能显示、画面没字幕。现在 cue 对象固定为 `{s,e,text,el}`。

### 4.4 滚动不抢焦点

`highlight(t)` 每帧更新高亮，但字幕列表/PPT 条的 `scrollIntoView` 需要满足：

```js
performance.now() - userScrolledAt > 2500
```

`userScrolledAt` 由 `#vlist`/`#chips` 的 `wheel` 与 `touchstart` 更新。用户往回翻记录时，2.5 秒内不被自动滚动拽回去。

---

## 5. PPT 跳转

数据源：`/media/<teclId>/subtitles/<label>.nodes.json`（后端 `fetch_nodes.py` 生成）。

```js
const list = (d.slides||[])
  .filter(x => x.showFlag!==false && Number.isFinite(Number(x.imageSeekTime)))
  .map(x => ({t:Number(x.imageSeekTime),
              text:(x.textKeywordList||[]).join(' · ') || x.ocrText || 'PPT 画面'}))
  .sort((a,b)=>a.t-b.t);
```

- 每个节点渲染成 `.chip`：上行时间戳（`clock()`），下行关键词（两行截断）。
- 点击 → `seekAll(n.t)`，未播放则 `playAll()`；两路同时跳。
- 播放时当前片高亮，并 `scrollIntoView({inline:'center'})`（横向居中），同样受 2.5s 抑制。
- 当前节点判定：`nodes[i].t <= t+0.15` 的最后一个（0.15s 容差，避免翻页瞬间闪烁）。

---

## 6. 节次与语音面板

### 6.1 节次列表

- 序号 `01,02,...`（不足两位补零）+ 标题（`time` 前 16 字符，即 `YYYY-MM-DD HH:MM`）+ 副行（`第N周 · 通道名/通道名`）。
- 选中项 `.on`：浅底 + 左侧 3px 内阴影标记。
- `renderSessions()` 末尾对 `.sess.on` 执行 `scrollIntoView({block:'nearest'})` —— 这就是「切到某节时右栏自动聚焦」。

### 6.2 语音逐句

同一份 `cues`，渲染成 `time + 文本` 的按钮；点击 `seekAll(c.s+0.05)`（+50ms 防止落在边界上被判为上一句）。高亮/滚动与字幕共用 `highlight()`，两边永远同步。

### 6.3 选节时序（`selectSession`）

```
session = s
→ 清空 curNode/curCue/curSub，resetPipPos()
→ 重渲染节次列表（自动聚焦）
→ 顶栏标题 = 课程名 · 节次
→ 选通道：默认 teacher1 + ppt
   ├ 读 localStorage 偏好，findCh() 命中且两者不同 → 用偏好
   ├ 只命中主画面 → 小画面取第一个不同的通道
   └ 都不命中（本节没这个通道）→ 回退默认
→ setChannel(0, a, t) / setChannel(1, b, t)   （t = 续播点）
→ resetTiming() + 显示「缓冲中…」徽标
→ warmChannel(a)               （先让后端把主画面读进 page cache，不等首字节）
→ setChannel(0,a,t)            主画面立即发流
→ setChannel(1,b,t,{defer:true}) 小画面只登记，不发流
→ 恢复 audioSlot（比对 prev.audio 是否落在 slot1）
→ loadNodes(s) / loadCues(s)   （并行，互不 await）
→ 主画面 loadedmetadata 时 releasePending() → 小画面才开始发流
→ 若 userActed 则 playAll()
→ saveState()
```

`loadNodes`/`loadCues` 故意不 `await`：字幕和 PPT 各自独立加载，谁慢都不阻塞另一块。

---

## 7. 续播与偏好记忆

### 7.1 本地（第一顺位）

```js
const KEY = 'vod-mini-v2';
{teclId, sid, t, ts,
 main:'1-teacher1', pip:'5-ppt', audio:'1-teacher1',
 rate:1.5, subs:true}
```

通道存 `viewNum-channel` 复合键，恢复时用 `findCh(session, key)` 反查对象。

`saveState()` 的写入点（共 9 处）：

| 时机 | 说明 |
|---|---|
| `pause` 事件 | 暂停即存 |
| 每 5s 心跳 | `setInterval(2s)` 里判断 `playing() && now-lastSave>5000` |
| `selectSession` 末尾 | 切节落一次 |
| `loadCourse` 开头 | **先按旧课保存再切**，否则切课瞬间丢最后几秒 |
| 通道下拉 `change` | 换通道即存 |
| `swap()` | 互换即存 |
| 倍速 `change` | 即存 |
| 字幕开关 | 即存 |
| `pagehide` | 关标签/切走，同时 `flushState()` |

**防冲 0**：视频未就绪（`readyState<1`）时，如果 `prev.sid` 与当前一致，就沿用旧 `t`，不写 0。这条修的是「切节瞬间把进度覆盖成 0」的经典 bug。

### 7.2 后端（跨设备）

```js
queueState(st) → 距上次 POST ≥8s 立即发，否则 setTimeout 补齐
flushState()   → fetch(POST /api/progress, {keepalive:true})
```

- `keepalive:true` 让请求能在 `pagehide` 后继续发出（普通 fetch 会随页面销毁被取消）。
- `hydrate()` 在 `boot()` 第一步执行：只在**后端 `ts` 更新**时覆盖本地，否则保留本地（本地更快、离线可用）。
- 后端 404 / 隧道断开时静默降级，纯 localStorage 也能正常续播。

### 7.3 启动时序（`boot()`）

```
页面解析完成
→ 并行：GET /api/courses 与 hydrate()（互不依赖，省一次串行往返）
→ applyPrefs(readState())        先恢复倍速/字幕（早于任何 setChannel）
→ 等 courses                     过滤 status==='ready'
→ 选课程：上次的 teclId 命中则用，否则第一门
→ loadCourse(teclId)
   → GET /api/course/<id> 拿 manifest
   → 选节：上次的 sid 命中则用它 + saved.t 作起播点，否则第一节
   → selectSession(...)
→ 不自动播放（等用户手势）
```

顺序上有个细节：`applyPrefs` 在**文件末尾也执行了一次**（第 754 行），因为 `boot()` 是 async，而首屏渲染前需要把「字幕：关」这类 UI 状态先落定，避免闪一下。

---

## 8. 全屏

作用域是 `#screen`（播放器 + 控制条），不是 `#player`。原因：视频没有原生 controls，如果只全屏 `#player`，进全屏后就没有任何可点的控件，暂停/拖动/切字幕全部失效——这是「还不如原版」的主要体感来源之一。

`:fullscreen` 下配套 CSS：黑底、去边框圆角、控制条转深色（`#0a0e11` / `#16232b`）、时间数字提亮。

兼容性写法：`requestFullscreen || webkitRequestFullscreen`，并用 `Promise.resolve(...)` 包住，避免老浏览器返回 `undefined` 时 `.catch` 报错。

---

## 9. 交互事件总表

### 9.1 鼠标/触摸

| 元素 | 行为 |
|---|---|
| 主画面单击 | 播放/暂停 |
| 任一画面双击 | 全屏 |
| 小窗画面单击 | 互换主/小（拖动超过 3px 不算） |
| 小窗标题条拖动 | 移动小窗（限制在播放器内） |
| 小窗 🔊 | 切换声音跟随 |
| 进度条 `input` | `seekAll`，`pointerdown` 期间 `dragging=true` 抑制回写 |
| PPT 切片 / 语音句 | 双通道跳转 + 未播放则播放 |

### 9.2 键盘

| 键 | 行为 |
|---|---|
| `Space` | 播放/暂停 |
| `←` / `→` | −5s / +5s |
| `S` | 互换主/小 |
| `C` | 字幕开关 |
| `F` | 全屏 |

焦点在 `input/select/textarea` 时全部让行（`/input|select|textarea/i.test(e.target.tagName)` 直接 return），否则在课程下拉里按空格会误触播放。

---

## 10. 与后端的接口契约

| 方法 | 路径 | 用途 | 前端处理 |
|---|---|---|---|
| GET | `/api/courses` | 课程目录 | 过滤 `status==='ready'`，填课程下拉 |
| GET | `/api/course/<teclId>` | 单课 manifest | `manifest.sessions[]` |
| GET | `/media/<teclId>/videos/<file>` | 视频流 | 作为 `<video src>`，Range 由浏览器自动发 |
| GET | `/media/<teclId>/subtitles/<label>.srt` | 字幕 | 取文本 → `parseSRT` |
| GET | `/media/<teclId>/subtitles/<label>.nodes.json` | PPT 节点 | 取 `slides[]` |
| GET | `/api/progress` | 跨设备续播 | `hydrate()`，比 `ts` |
| POST | `/api/progress` | 上报进度 + 偏好 | 8s 节流 + `keepalive` |
| GET | `/jy-application-resourcemanage/v1/course_vod_subtitle_with_keyword` | 字幕兜底 | `bg/ed` 毫秒 → 秒 |
| POST | `/api/telemetry` | 播放诊断 | 由 `playback-telemetry.js` 调用；本文件另用 `event:'timing'` 上报首帧耗时 |
| GET | `/api/warm/<teclId>/<file>` | 选节即预热 | `warmChannel()`，fire-and-forget，后端按文件去重 |

**首帧埋点**（`event:'timing'`，每次选节一条）：

```json
{"teclId":"8537","file":"sid:<sid>","event":"timing",
 "meta_ms":161,"canplay_ms":228,"defer_ms":161,"warmed":1,
 "main_ch":"5-ppt","pip_ch":"1-teacher1"}
```

- `meta_ms` = 选节 → 主画面 `loadedmetadata`；`canplay_ms` = 选节 → 可播；`defer_ms` = 小画面被放开（发流）的时刻；
- 后端只把它写进 `playback.jsonl`（`record:"player"`），**不进 `/api/stutter`**，避免每条 session 冒充一条流；
- 15 秒还没 `canplay` 也会补发一条（`canplay_ms:null`），用来抓「彻底没起来」的情况。

页面末尾还引入了 `<script src="./original-vod-prototype/static/playback-telemetry.js">`：每 15 秒上报倍速、媒体时间推进率、缓冲深度、帧统计（`possible_frame_freeze` 等），供后端 `/api/stutter` 与 `backend/playback.jsonl` 对照。**它是独立文件，不随 index.html 内联，改动需同步到 Windows 侧副本**。

---

## 11. 已知边界与坑

1. **只有 mp4/H.264 渐进式能播**。换源素材若为 HLS/fMP4 分片，需先转码或改后端。
2. **两路视频 = 双倍带宽**。隧道带宽不足时，任何一路都可能频繁 buffer；`/api/stats` 的 `speed_human` 压不住码率就是带宽问题，不是前端 bug。
3. **刷新后不自动播放**（浏览器策略）。这不是缺陷；点一下画面或空格即可，位置已经 seek 好。
4. **`duration` 可能是 `Infinity`**（未拿到 `Content-Length`/未 seek 时的流式响应）。所有用到时长的地方都做了 `Number.isFinite` 保护，seek 目标统一 clamp 到 `duration-0.3`。
5. **`pointercapture` 丢失**（触控板三指手势、系统弹窗）会导致拖拽卡住；`pointercancel` 已做清理，但极端情况下松手后小窗停在半路，点一下再拖即可。
6. **跨源**：`index.html` 从 18080 打开时同源；若从 18766 打开，媒体仍走 18080，依赖后端全局 `Access-Control-Allow-Origin: *` 与 `Expose-Headers: Content-Range`。
7. **localStorage 绑定源**。换端口（18080↔18081）、换浏览器、清站点数据都会丢本地记忆；此时靠后端 `/api/progress` 兜底。
8. **通道组合按课程而非按节记录**。若某节缺上次选的通道，会回退默认组合并覆盖偏好（同课程各节通道集基本一致，影响很小；要按节记需改成 `sid → {main,pip}` 字典）。
9. **PPT 条/语音面板的滚动竞争**：只有 `wheel` 和 `touchstart` 会刷新 `userScrolledAt`，用滚动条拖拽不会，此时可能被自动滚动拽走。

---

## 12. 排障速查

| 现象 | 排查顺序 |
|---|---|
| 顶栏红点「连接失败（检查隧道）」 | `curl -s 127.0.0.1:18080/api/courses`；隧道是否断 |
| 课程能列出但无节次 | `/api/course/<id>` 是否 404（manifest 未下完） |
| 画面黑、状态「视频加载失败」 | F12 Network 看 `.mp4` 是否 200/206；后端 `/tmp/huanyuan.log` |
| 有语音列表但画面无字幕 | 看语音面板计数：`N 句` = 取数正常，渲染层问题；空 = 字幕文件缺失 |
| 一个画面动一个不动 | 自动播放被拦 → 点一下画面；或该通道文件损坏 |
| 全屏后无法暂停 | 确认全屏的是 `#screen` 而非 `#player`（F12 `document.fullscreenElement.id`） |
| 进度不跨设备 | `POST /api/progress` 是否 404（后端未重启到 v1.7） |

**改完怎么生效**：静态文件带 `Cache-Control: no-cache`，直接刷新浏览器即可；后端不需要重启（除了改 `server.py` 本身）。

---

## 13. 变更记录

### 2026-11-12 首帧提速（配合后端 v1.8）

| 改动 | 说明 | 效果 |
|---|---|---|
| 小画面延后发流 | `setChannel(1,ch,t,{defer:true})` 只登记，等主画面 `loadedmetadata` 才 `releasePending()`（2.5s 兜底） | 起播瞬间只有一条流抢隧道。服务端日志里单条流首波就要吞 5–13 MB，隧道实测只有 ~0.84 MB/s —— 两路同抢等于把主画面首帧往后推了一次「对方缓冲区灌满」的时间 |
| 选节即预热 | `warmChannel()` 打 `/api/warm/...` | 把「整文件读进 page cache」提前到用户看列表的几秒；拖进度前的尾部请求（moov 在文件末尾）直接命中内存 |
| `preload` 调整 | 主画面 `metadata`→`auto`，小画面保持 `metadata` 到发流为止 | 主画面更早进入真实缓冲 |
| boot 并行 | `/api/courses` 与 `hydrate()` 同时发 | 少一次串行往返（隧道上每跳几十毫秒） |
| 缓冲中徽标 | `#loadBadge`，`canplay` 即隐藏 | 感知提速：起播那几百毫秒不再是「黑屏没反应」 |
| 首帧埋点 | 见第 10 节 | 以后不用猜，`playback.jsonl` 里直接有 `meta_ms/canplay_ms/defer_ms` |

本机（loopback）实测：旧版两路同时拿到 src、都在 338 ms 就绪；新版主画面 317 ms，小画面 398 ms —— 错峰按设计生效，总时长基本不变（loopback 不是瓶颈），收益体现在隧道带宽紧张时。

**注意**：本文件仍是单文件手写 JS，没有构建；改完带 `Cache-Control: no-cache` 直接刷新即可，后端不用重启。

### 2026-11-18 课件通道改走图片流

| 改动 | 细节 |
|---|---|
| 新增路由 | `GET /api/slides/<teclId>/<label>` 返回页索引 `{count,pages:[{t,file}]}`；`GET /media/<teclId>/slides/<label>/NNN.jpg` 返回单页图片（`CTYPE['slides']='image/jpeg'`，`MEDIA_RE` 的 kind 增加 `slides`） |
| 数据来源 | `scripts/ppt_to_pdf.py --emit-images`：有 `nodes.json` 时用平台 `imageSeekTime`（3–14 s/节），没有则缩略图差分切页（~85 s/节）；产物 `<课程>/slides/<label>/{index.json,NNN.jpg}`，平均 **150 KB/页** |
| 前端 | 选节时并行取索引，命中就把小画面换成 `<img>` 并按主画面 `currentTime` 二分查找切图（预取下一张），**这一路视频流不再发送**；PPT 若被选成主画面会与视频换位；⤢ 放大；互换/换通道时退回视频流 |
| 实测 | 一次播放只剩 `teacher1.mp4` 一路传输；课件侧从 1 Mbps 连续流（398 MB/节）降到 2.9–3.2 MB/节 |

> 注意：这是「预生成图片」而不是实时转码，所以新增/重下一节课后要跑一次
> `python3 scripts/emit_all_slides.py --course <dir>`（幂等，已导出的跳过）。
