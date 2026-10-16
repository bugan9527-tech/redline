# 深浅

把一本中文网文（TXT / EPUB）读成**深浅**。

逐段判读，关键情节加重、注水淡化、零信息判死，产出一份强度可调的 HTML 阅读版和一份
EPUB。引擎本体是纯本地规则；规则判不准的那一段（默认 0.30~0.70 的模糊带）**必须**接上
你自己的模型复核一遍——端点、模型、Key 都由你填，喂给它的判定标准只读自引擎 skill 的
`references/`，Key 不落盘、不进日志。这一层是必经的，配不齐接口就出不了书。

引擎只有一份：桌面上跑成本地网页，手机上打包进 APK 在本地跑。

## 要什么

- 一台能跑 Python 的机器（Windows / macOS / Linux 都行）
- **Python 3.10 或更高，不需要 `pip install` 任何东西**——后端只用标准库
- 引擎副本要在 `engine/webnovel-focus/`（仓库里已经带了）
- 一个 OpenAI 兼容的接口地址、模型名和 Key——AI 复核是必经的一层，配不齐就出不了书

## 跑起来

Windows 上双击 **`启动.bat`**，浏览器会自己打开 <http://127.0.0.1:8765>。
关掉用 **`停止服务.bat`**。

`启动.bat` 会自己找 Python：先问 `py` 启动器，再退回 `where python`（顺手排掉微软商店那个
只会弹应用商店的假 `python.exe`），版本低于 3.10 就提示你装、并帮你打开下载页。

手动起也行：

```bash
python app/server.py                    # 默认 127.0.0.1:8765，起来后自动开浏览器
python app/server.py --port 9000        # 换个端口
python app/server.py --no-open          # 别开浏览器
python app/server.py --log work/server.log   # 无控制台时把日志落到文件
python app/server.py --host 0.0.0.0     # 对局域网开放（默认只开本机）
```

已经在跑的时候再启动一次不会报错：它先探一下 `/api/health`，发现是自己就把页面打开、安静退出。

三个页面：

| 地址 | 是什么 |
|---|---|
| `/` | 首页，介绍 |
| `/app` | 工作台：选书 → 体检抽查 → 出整本（含必经的 AI 复核）→ 导出 EPUB |
| `/read` | 阅读器，不落盘，直接把一本 TXT / EPUB 摊开读 |

## 目录

```
app/            自有应用层：后端 + 三个页面
  server.py     本地后端，一个文件，只用标准库
  engine.py     包住引擎的壳（只通过命令行调它，从不写进引擎目录）
  bookread.py   阅读器的解析层（TXT 编码回退 / EPUB 按书脊取正文）
  ai_layer.py   AI 复核层（只把模糊带的段落发给你自己的模型）
  ui.html       工作台
  reader.html   阅读器
engine/         webnovel-focus 引擎的只读副本
android/        Android 端：把 engine/ 那份引擎打进 APK，判定整个跑在手机里
  app/          APK 模块（Chaquopy 打包 Python 3.10，壳层是 Compose 三页）
  tools/        真机回归：headless 跑一遍判定，跟桌面逐段对照
assets/         落地页图片
tests/          测试（标准库 unittest，零依赖）
work/           运行时产物，随时可以整个删掉（已 gitignore）
```

## 怎么跑测试

```bash
python -m unittest discover -s tests            # 快套，秒级
```

引擎那层的「完整链路」默认跳过（会真的跑一次引擎）。要连它一起跑：

```powershell
$env:WNF_SLOW="1"; python -m unittest discover -s tests      # PowerShell
WNF_SLOW=1 python -m unittest discover -s tests              # bash
```

测试全程把工作目录指到临时目录，**不会碰你真实的 `work/`**。

## 提交与推送

`origin` 走 SSH（`git@github.com:bugan9527-tech/redline.git`），推送就一条命令：

```bash
git commit -am "说明"
git push
```

提交身份设成了**仓库级**（写在 `.git/config`，只对这份检出生效，不进仓库也不提交），
所以 `git commit` 不用再传 `-c user.name=… -c user.email=…`。

本机 **`github.com:443`（HTTPS）连不通**，别把 remote 换回 `https://` 地址。

一条踩过的坑：**不要**用 GitHub REST 的 git-data 接口（`POST /git/commits`）造提交来推送。
GitHub 会把提交时间转成 UTC、偏移量强制写成 `+0000`，而本机提交是 `+0800`——同一时刻两种
写法，SHA 必定对不上，本地和远端就再也合不拢了。走 SSH 才是字节保真。

## HTTP 接口

后端是网页端和 Android 端共用的契约。所有响应都是 JSON，出错统一是
`{"error": "说明"}` 加上对应的状态码。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 探活：`{"ok":true,"engine":"<路径>","readonly":true}` |
| GET | `/api/state?job=` | 进度与产物，见下 |
| GET | `/api/jobs` | 做过的书，新的在前 |
| GET | `/api/skill-rules` | AI 复核实际使用的判定标准（纯文本） |
| POST | `/api/upload` | body = 文件字节，header `x-filename`；返回 job + 抽查页 |
| POST | `/api/read` | body = 文件字节，header `x-filename`；返回分好章的正文，不落盘 |
| POST | `/api/build` | `{job, ai:{endpoint,model,key,lo,hi,max}}`（三样缺一条回 400），后台跑 |
| POST | `/api/cancel` | `{job}` 取消正在跑的任务 |
| POST | `/api/export` | `{job, preset, lever}`，后台打包 EPUB |
| POST | `/api/job-delete` | `{job}` 删掉这本书的整个工作目录 |
| POST | `/api/ai-test` | `{endpoint, model, key}` 测一次连接 |
| GET | `/work/<job>/...` | 抽查页 / 阅读版 / EPUB 下载（`?dl=1` 走下载） |
| GET | `/assets/...` | 落地页图片 |

`/api/upload` 和 `/api/read` 靠 `x-filename` 认类型，**中文文件名要先
percent-encode**（网页端用的就是 `encodeURIComponent(file.name)`）。

## 给 Android 端

接口是给两个客户端共用的，改动要当破坏性变更来对待。

**任务句柄**：一切后续操作都靠 `/api/upload` 返回的 `job`。浏览器端把
`job` 存在 `localStorage`，Android 端建议同样持久化，进程被杀之后还能接回去。

**进度靠轮询** `/api/state?job=`，没有推送。字段：

```json
{
  "job": "20261016-132723-9bb97",
  "phase": "build",
  "log": ["..."],
  "done": false,
  "error": null,
  "result": { "...": "见下" },
  "progress": { "total": 24, "done": 7, "reviewed": 112, "failed": 0 }
}
```

跑起来的时候还会多一个 `started`（unix 秒），用来显示已用时间。

`phase` 的全部取值：

| phase | 意思 | 算不算「在跑」 |
|---|---|---|
| `idle` | 只有源文件 | 否 |
| `doctor` | 正在体检 | 是 |
| `checked` | 等你确认抽查页 | 否 |
| `build` | 正在判读整本 | 是 |
| `ai` | 正在做 AI 复核 | 是 |
| `render` | 正在重渲 | 是 |
| `export` | 正在打包 | 是 |
| `ready` | 好了 | 否 |
| `error` | 出错，看 `error` | 否 |
| `cancelled` | 已取消 | 否 |

客户端**不要**把 phase 写死成枚举去 `switch`——遇到不认识的阶段当作「不在跑」处理，
以后加阶段才不会被带崩。

**`result` 结构**（`/api/state` 和上传返回里都有）：

```json
{
  "job": "...", "name": "书名",
  "tiers": { "plot": 120, "weak": 40, "fade": 88, "zero": 12 },
  "confirm": "/work/<job>/confirm/book.html",
  "reader":  "/work/<job>/out/book.html",
  "files":   [{ "name": "书名.epub", "url": "/work/<job>/out/书名.epub", "kb": 6400 }],
  "prescan": "/work/<job>/prescan.txt",
  "report":  "/work/<job>/out/report.txt"
}
```

`files` 只出搬得走的成品（EPUB）。`reader` 在出整本之前是空字符串。
`tiers` 的键：`plot` 情节 / `mid` 普通 / `weak` 偏水 / `fade` 注水 / `zero` 零信息 / `sep` 分隔。

**`/api/jobs`** 每项固定是 `{"job","name","created","phase"}`。

**取消**：`POST /api/cancel` 成功返回 `{"ok":true,"job":...,"phase":"cancelled","killed":true}`
（`killed` 表示真的掐到了引擎子进程）。任务没在跑会回 **409**；任务不存在回 **404**。
取消之后这本书可以重新 `build`，不会被卡住。

**产物下载**：`result` 里的 URL 是**没有编码过的原始路径**（文件名可能是中文）。
浏览器会自动编码，Android 端得自己 percent-encode 再用。

**状态会落盘**：每台任务的状态写在 `work/<job>/state.json`，日志写在 `work/<job>/run.log`。
服务重启后 `/api/state` 会从磁盘把失败原因捡回来；如果重启那一刻它还在跑，
会明确告诉你「被中断了」，而不是假装还在跑。这一点对手机端尤其有用——
断线重连之后接着轮询就行，不用自己记状态。

## 隐私

- 判读全程在本地，书不上传
- AI 复核只把规则判不准的那一段（默认 0.30~0.70 的模糊带）发给你自己填的接口，
  其余部分一步不落地留在本机
- API Key 只存在浏览器 `localStorage` 里，不写进任何文件、不进日志
- AI 复核用的判定标准来自引擎 skill 自己的 `references/`（只读），不是另编一套

## 已知限制

- 只吃 TXT 和 EPUB。PDF 要先抽成文本，这层没做
- **AI 复核必须有可用的模型接口**：这一层是必经的，接口不通或额度用尽就不出书，
  不会静默退回纯规则
- **PDF 导出已经拿掉**：那要靠本机浏览器的无头打印，几十万字的大书会把进程拖死
- 默认只听 `127.0.0.1`。要给别人用就 `--host 0.0.0.0`，别开到公网
- 单机单进程：并发没有管制，`work/` 也不会自动回收，用「删掉」自己清
- **分发仍依赖本机有 Python**：Python 不打包进仓库。`启动.bat` 会先认 `py` 启动器、
  再用 `where python` 兜底（并排掉微软商店那个假 `python.exe`），版本低于 3.10 就直接提示并
  打开下载页；但换一台没装 Python 的机器，仍要用户先自己装一次
