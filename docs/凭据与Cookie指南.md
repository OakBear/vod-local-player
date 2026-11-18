# 凭据与 Cookie 获取指南（新手向）

这套工具不破解任何东西：它用**你自己的账号**登录后留下的 Cookie / Token，去调平台自己的接口取地址。
所以第一件事永远是——把两层凭据放进正确的位置。

## 0. 一句话说清两层

| 层 | 名字 | 从哪来 | 存到哪 | 有效期 | 用来干什么 |
|---|---|---|---|---|---|
| **L1 OC 层** | `_normandy_session` | 开放课程平台（OC）域名的 Cookie | `credentials/normandy.txt` | 几天到几周 | 走 LTI 1.3 换来播放器 token（**换票**） |
| **L2 播放器层** | `jy-application-resourcemanage-ui_STORAGE_KEY_JWT_TOKEN`（内含 `accessToken`） | 播放器前端 Local Storage / 请求头 | `<课程>/scripts/jwt.txt` | 约 **2 小时** | 调 `/v1/course_vod_*` 接口取播放地址、字幕、课件节点（**用票**） |

为什么要两层：平台用 LTI 1.3（教育行业的标准单点登录协议）把「课程平台账号」换成「播放器临时票」。
L1 是会话语义（长期），L2 是业务票（短期且频繁过期）。**L1 失效就没法自动续票，L2 过期则随时可续。**

先体检：

```bash
python3 scripts/token_status.py
# [OC 层] ✓ _normandy_session  634 字符  长度正常（是否真有效要看铸造结果）
# [播放器层] ✗ 已过期 exp=11-12 12:31 UTC  剩余 -145.2 小时
```

---

## 1. 取 L1：`_normandy_session`

1. Chrome / Edge 登录课程平台（就是平时看回放那个站），确认**能正常打开一节课的播放页**；
2. `F12` → **Application（应用）** 面板 → 左侧 **Storage → Cookies** → 点平台域名；
3. 找到 `_normandy_session`，双击 `Value` 全选复制（**只要值，不要 `_normandy_session=` 前缀，不要引号**）；
4. 存成一行：

```bash
mkdir -p ~/vod-research/credentials
printf '%s' '<粘贴到这里>' > ~/vod-research/credentials/normandy.txt
chmod 600 ~/vod-research/credentials/normandy.txt
```

5. 验证：`python3 scripts/mint_vod_token.py`
   - 成功 → 会打印 `铸成功: sub=... exp=... 剩余 xx.xh`，并写好 L2 的两个文件；
   - 打印 `launch page HTTP 302 (normandy 会话可能已失效)` → **这个 Cookie 无效了**，回第 1 步重新取（通常是浏览器里那个标签页已经登出/换了账号）。

> 手机端也能取：用 Chrome 远程调试，或直接在电脑上登录。别用「复制请求头 Cookie:」整段，那样会把 `=`、`;`
> 一起带进去，脚本只认纯值。

## 2. 取 L2：播放器 JWT

三种方式，任选一种（**推荐第 3 种，最不容易错**）。

**方式 A：Local Storage（界面点）**

1. 打开任意一节课的播放页（`.../play-center?...`）；
2. `F12` → **Application** → **Local Storage** → 平台域名；
3. 找到 key 为 `jy-application-resourcemanage-ui_STORAGE_KEY_JWT_TOKEN` 的那一项，复制整个 Value
   （形如 `eyJhbGciOi....eyJ...xxxx`，三段用 `.` 分隔）。

**方式 B：Network（从请求头抄）**

1. `F12` → **Network** → 过滤 `jy-application-resourcemanage`；
2. 随便点一个返回 200 的请求 → **Headers → Request Headers**；
3. 复制 `Authorization: Bearer ` **后面**那一段（就是 JWT 本身）。

**方式 C：Console 一行（推荐）**

1. 播放页 `F12` → **Console**；
2. 粘贴：

```js
copy(localStorage.getItem('jy-application-resourcemanage-ui_STORAGE_KEY_JWT_TOKEN'))
```

3. 回车上写进文件（此时系统剪贴板里已经是那段 JWT）：

```bash
mkdir -p ~/vod-research/blockchain-apps/scripts      # 换成你自己的课程目录
cat > ~/vod-research/blockchain-apps/scripts/jwt.txt # 回车后粘贴，再按 Ctrl-D 结束
chmod 600 ~/vod-research/blockchain-apps/scripts/jwt.txt
```

> 如果终端里 `Ctrl+Shift+V` 粘不进去，说明你用的是 SSH 精简终端，改用方式 B 复制，或用 `scp` 传文件。

脚本对两种写法都兼容：既能读裸 JWT，也能读 `jy-application-resourcemanage-ui_STORAGE_KEY_JWT_TOKEN=eyJ...`
这种整段 Cookie 串（`update_all.py` 里会正则抠出来）。

## 3. 三个容易混淆的文件

| 文件 | 格式 | 谁在用 | 干什么 |
|---|---|---|---|
| `credentials/normandy.txt` | 纯值，一行 | `mint_vod_token.py` | 换票 |
| `<课程>/scripts/jwt.txt` | JWT 或整段 Cookie | `update_all.py`、`fetch_nodes.py` | 用票 |
| `<课程>/scripts/bearer.txt` | 纯 `accessToken` | 老脚本 | 同上，可忽略 |
| `cookies.txt`（如果你有） | Netscape 格式（7 列、Tab 分隔） | 下载器 | **另一套东西**，不是这里的两层凭据，别混 |

## 4. 过期了怎么办

| 症状 | 缺哪层 | 解决 |
|---|---|---|
| `launch page HTTP 302` | L1 | 重取 `_normandy_session` |
| `未拿到 jwt_token (launch -> HTTP 403/500)` | L1 有效但没选这门课 | 确认账号确实选了这门课 |
| 接口 401 / `token 已过期 -x.xh` | L2 | `python3 scripts/mint_vod_token.py`（L1 还在的话一条命令搞定） |
| `jwt.txt 解析失败` | 贴错内容 | 重新按上面方式 C 取 |

`update_all.py` 会在剩余不足 10 分钟时自动调 `mint_vod_token.py` 续票；续不了就停下并提示你去补 L1。

## 5. 安全

- 这三个文件等价于**你的账号密码**：能看你的课、以你的名义调接口。`chmod 600`，别贴群、别提交 git（`.gitignore` 已经挡了 `credentials/`、`*jwt.txt`、`*bearer.txt`、`cookies.txt`）。
- 只用于**你自己已选课程**的个人离线观看；不要转发采集到的视频、字幕、课件。
- 浏览器里「退出登录」会让 L1 立刻失效；想主动作废就退出登录或改密码。
