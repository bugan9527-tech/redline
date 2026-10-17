# 深浅

帮你把一本千万字级别的网文进行科学化的重要剧情标深，次要剧情标淡，大大提高你的阅读体验。

逐段判读，关键情节加重、注水淡化、零信息判死，产出一份强度可调的 HTML 阅读版和一份
EPUB。引擎本体是纯本地规则；AI 层按规则签名抽样标定，按预算先补稀有桶、再升级不稳定桶——
端点、模型、Key 都由你填，模型输出五类标签和原文引用，不直接给连续分。AI 标定是必经的
一层，配不齐接口就出不了书。

Key 由客户端保存在本机：网页端使用浏览器 `localStorage`，Android 使用 App 私有
`SharedPreferences`。服务端不会主动把它写进 `work/` 或日志，但它会随客户端存储保留下来，
不是“不落盘”，也不是加密保险箱。

引擎只有一份：桌面上跑成本地网页，手机上打包进 APK 在本地跑。

## 要什么

- 一台能跑 Python 的机器（Windows / macOS / Linux 都行）
- **Python 3.10 或更高**
  - `app/server.py` 本身只用标准库；
  - 引擎渲染需要 **Jinja2**，先执行 `python -m pip install -r requirements.txt`；
  - `scikit-learn` 可选，用于重复段检测；需要时装 `python -m pip install scikit-learn`。
    缺了不会阻断启动，但判定结果可能与完整环境不同
- 引擎副本要在 `engine/webnovel-focus/`（仓库里已经带了）
- 一个 OpenAI 兼容的接口地址、模型名和 Key——AI 标定是必经的一层，配不齐就出不了书

## 跑起来

第一次运行前先装引擎依赖（只需一次）：

```bash
python -m pip install -r requirements.txt
```

Windows 上双击 **`启动.bat`**，浏览器会自己打开 <http://127.0.0.1:8765>。
关掉用 **`停止服务.bat`**。

`启动.bat` 只负责找 Python 和启动服务，不负责安装依赖；第一次运行请先执行上面的安装命令。

`启动.bat` 会自己找 Python：先问 `py` 启动器，再退回 `where python`（顺手排掉微软商店那个
只会弹应用商店的假 `python.exe`），版本低于 3.10 就提示你装、并帮你打开下载页。

手动起也行：

```bash
python app/server.py                    # 默认 127.0.0.1:8765，起来后自动开浏览器
python app/server.py --port 9000        # 换个端口
python app/server.py --no-open          # 别开浏览器
python app/server.py --log work/server.log   # 无控制台时把日志落到文件
python app/server.py --host 0.0.0.0     # 对局域网开放（无鉴权，默认只开本机）
```

已经在跑的时候再启动一次不会报错：新启动的服务会先探一下 `/api/health`；端口上已有健康
响应时，它只把页面打开并退出，不会换掉旧进程。

**改过 `app/` 下的 `.py`（比如 `bookread.py`）之后要重启服务**：双击 `停止服务.bat`，再双击
`启动.bat`。8765 上已有服务在跑时，新进程只把页面打开、不会换进程——旧代码会一直留在
内存里，改了也白改。阅读器页进来自会探一次服务版本，遇到旧版会直接把这句话写在页面上。

四个页面：

| 地址 | 是什么 |
|---|---|
| `/` | 首页，介绍 |
| `/shelf` | 书架：这台机器上放过的书，接着读 / 读深浅版 / 导出 / 改名 / 删除 |
| `/app` | 工作台：选书 → 体检抽查 → 出整本（含必经的 AI 标定）→ 导出 EPUB |
| `/read` | 阅读器，把一本 TXT / EPUB 摊开读；从这里拖进来的书会落盘入架 |

## 目录

```
app/            自有应用层：后端 + 工作台/书架/阅读器三个页面
  server.py     本地后端，一个文件，只用标准库
  engine.py     包住引擎的壳（只通过命令行调它，从不写进引擎目录）
  bookread.py   阅读器的解析层（TXT 编码回退 / EPUB 按书脊取正文）
  ai_layer.py   AI 标定层（抽样、五分类、桶后验、定向升级）
  ui.html       工作台
  shelf.html    书架
  reader.html   阅读器
index.html      首页
启动.bat        Windows 启动脚本
停止服务.bat    Windows 停止脚本
requirements.txt 引擎依赖（Jinja2；scikit-learn 可选）
engine/         webnovel-focus 引擎的只读副本
android/        Android 端：把 engine/ 那份引擎打进 APK，判定整个跑在手机里
  app/          APK 模块（Chaquopy 打包 Python 3.10，壳层是 Compose 三页）
  tools/        真机回归：headless 跑一遍判定，跟桌面逐段对照
assets/         落地页图片
tests/          测试（标准库 unittest；快套零第三方依赖）
work/           运行时产物，随时可以整个删掉（已 gitignore）
```

## 怎么跑测试

```bash
python -m unittest discover -s tests            # 快套，秒级，不跑引擎
```

引擎那层的「完整链路」默认跳过（会真的跑一次引擎）。要连它一起跑：

```powershell
$env:WNF_SLOW="1"; python -m unittest discover -s tests      # PowerShell
WNF_SLOW=1 python -m unittest discover -s tests              # bash
```

完整链路需要先装好 `jinja2`；没有它，引擎会在导入 `render.py` 时直接失败。测试全程把
工作目录指到临时目录，**不会碰你真实的 `work/`**。

## 提交与推送

`origin` 走 SSH（`git@github.com:bugan9527-tech/redline.git`），提交流程：

```bash
git status
git add <你改的文件...>
git commit -m "说明"
git push
```

提交身份是本地 Git 配置，不会随仓库分发；新克隆需要自己设置 `user.name` 和
`user.email`。提交前先确认 `git status`，只暂存这次要提交的文件，避免把本地缓存和
未跟踪目录带进去。

## HTTP 接口

后端是网页端和外部客户端的 HTTP 契约。除 `/api/skill-rules`（纯文本）以及
`/work`、`/assets` 下的静态文件外，API 响应都是 JSON；出错统一是
`{"error": "说明"}` 加上对应的状态码。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 探活：`{"ok":true,"engine":"<路径>","readonly":true,"tiers":true}`（`tiers` = 这版服务能读出分层标记） |
| GET | `/api/state?job=` | 进度与产物，见下 |
| GET | `/api/calibration?job=` | 标定结果（模型、种子、样本 id、桶表和指标） |
| GET | `/api/jobs` | 做过的书，新的在前 |
| GET | `/api/library` | 书架：同一本书只出一条 `{id,name,kind,kinds,copies,added,state,progress,reader,files}`，新的在前 |
| GET | `/api/read-book?job=` | 按号打开书架上的书：有判定结果就返回带深浅档位的正文，否则返回原文（书架点击用） |
| GET | `/api/read-source?job=` | 只把源文件正文读回来（兼容旧客户端） |
| GET | `/api/skill-rules` | AI 标定实际使用的五分类提示词（纯文本） |
| POST | `/api/upload` | body = 文件字节，header `x-filename`；返回 `job`、`phase`、`confirm`、`reused`，体检完成时还含 `result` |
| POST | `/api/read` | body = 文件字节，header `x-filename`；返回分好章的正文。带 `?shelf=1` 时同时落盘入架，返回里多一个 `job` |
| POST | `/api/build` | `{job, ai:{endpoint,model,key,max}}`（三样缺一条回 400；`lo/hi` 仅兼容旧客户端，`max` 是抽样+升级总预算，默认 800） |
| POST | `/api/cancel` | `{job}` 取消正在跑的任务 |
| POST | `/api/export` | `{job, preset, lever}`，后台打包 EPUB |
| POST | `/api/job-delete` | `{job}` 删掉这本书及同组副本的整个工作目录 |
| POST | `/api/library/progress` | `{job, chapter, ratio}` 记续读位置（写进这本书的 meta.json） |
| POST | `/api/library/rename` | `{job, name}` 只改显示名，不动目录 |
| POST | `/api/ai-test` | `{endpoint, model, key}` 测一次连接 |
| GET | `/work/<job>/...` | 抽查页 / 阅读版 / EPUB 下载（`?dl=1` 走下载） |
| GET | `/assets/...` | 落地页图片 |

`/api/upload` 和 `/api/read` 靠 `x-filename` 认类型，**中文文件名要先
percent-encode**（网页端用的就是 `encodeURIComponent(file.name)`）。

书架上的每本书，`state` 从产物派生、不另存：只有源文件 = `source`（未判定）；
有阅读版 = `judged`（已判定）；有阅读版且 `out/` 里有 `.epub` = `exported`（已导出）。
这三个取值是网页端与 Android 端共用的书架语义。

网页端书架按正文身份归并：同一份内容重复导入会复用已有 `job`；换成 TXT / EPUB
另一种格式、或只改文件名/元数据标题，也仍按同一份正文合并成一条。正文不同时，即使
标题相同也不会合并。

Android 原生书架按摊平后的 `src.txt` 内容哈希去重：同一份内容重复导入会复用已有
记录，但不做 TXT / EPUB 的跨格式身份匹配。工作台只导出 EPUB，阅读器里直接打开的
TXT 是未经工作台处理的原文，所以移动端不依赖跨格式合并。

历史遗留的重复目录也会在读取书架时按各自的内容标识合并成一条：`copies` 是合并了
几份，网页端还额外返回 `kinds`（这些副本出现过的格式）。门面书自己还没有续读位置时，
`progress` 从旧副本里取读得最远的那个；门面书已经记过位置时以门面为准，允许读者
往回重读。删除和改名会作用到整组副本上，避免删掉门面后旧记录又冒出来。

## HTTP 客户端与 Android

下面这套 HTTP 契约主要供网页端和外部 HTTP 客户端使用。当前仓库里的 Android 原生端
不通过本地 HTTP 服务调用，而是用 Chaquopy 直接 import
`android/app/src/main/python/runner.py`；`build`、`ai_review`、`export_epub` 都是进程内调用，
`job` 也是本地生成的。改动共同引擎或 `runner.py` 仍要按兼容性对待；如果你在做一个走
HTTP 的 Android 客户端，再按下面的句柄和轮询语义接入。

Android 构建另外需要 JDK 17、Android SDK、Gradle、Chaquopy，以及构建机上的 Python 3.10。
这个 Python 只用于打包（安装依赖、编译字节码），APK 运行时会自带 Python，手机端不需要另装。
Chaquopy 会按 App 的 `version = "3.10"` 自动查找解释器：Windows 上依次尝试 `py -3.10`、
`py -3`，Linux/macOS 上尝试 `python3.10`、`python3`。如果 Python 3.10 装在非常规位置，
可以在 `android/local.properties` 写 `buildPython=C:/path/to/python.exe`，或构建时加
`-PbuildPython=...` / 设置 `BUILD_PYTHON` 环境变量；优先级为 Gradle 属性 > 环境变量 >
`local.properties`。Windows 上不要指向 Microsoft Store 的占位 `python.exe`。

**任务句柄（HTTP 客户端）**：一切后续操作都靠 `/api/upload` 返回的 `job`。
浏览器端把 `job` 存在 `localStorage`，HTTP 客户端也应持久化，进程被杀之后还能接回去。

**进度靠轮询** `/api/state?job=`，没有推送。字段：

```json
{
  "job": "20261016-132723-9bb97",
  "phase": "build",
  "log": ["..."],
  "done": false,
  "error": null,
  "result": { "...": "见下" },
  "progress": { "stage": "ask", "total": 24, "done": 7, "sampled": 50,
                "fitted": 0, "escalated": 0, "reviewed": 48, "failed": 0 }
}
```

跑起来的时候还会多一个 `started`（unix 秒），用来显示已用时间。
AI 标定期间 `stage` 依次为 `sample → ask → fit → escalate → apply`；顶层
`phase` 仍保持 `ai`，避免旧客户端把新阶段误判成“已经结束”。

`phase` 的全部取值：

| phase | 意思 | 算不算「在跑」 |
|---|---|---|
| `idle` | 只有源文件 | 否 |
| `doctor` | 正在体检 | 是 |
| `checked` | 等你确认抽查页 | 否 |
| `build` | 正在判读整本 | 是 |
| `ai` | 正在做 AI 标定/升级 | 是 |
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
  "tiers": { "plot": 120, "mid": 300, "weak": 40, "fade": 88, "zero": 12, "sep": 3 },
  "confirm": "/work/<job>/confirm/book.html",
  "reader":  "/work/<job>/out/index.html",
  "files":   [{ "name": "书名.epub", "url": "/work/<job>/out/书名.epub", "kb": 6400 }],
  "prescan": "/work/<job>/prescan.txt",
  "report":  "/work/<job>/out/report.txt"
}
```

`files` 只出搬得走的成品（EPUB）。`reader` 在出整本之前是空字符串；分章模式指向
`index.html`，单文件模式指向 `book.html`。
`tiers` 的键：`plot` 情节 / `mid` 普通 / `weak` 偏水 / `fade` 注水 / `zero` 零信息 / `sep` 分隔。

**`/api/jobs`** 每项固定是 `{"job","name","created","phase"}`。

**取消**：`POST /api/cancel` 成功返回 `{"ok":true,"job":...,"phase":"cancelled","killed":true}`
（`killed` 表示真的掐到了引擎子进程）。任务没在跑会回 **409**；任务不存在回 **404**。
取消之后这本书可以重新 `build`，不会被卡住。

**产物下载**：`result` 里的 URL 是**没有编码过的原始路径**（文件名可能是中文）。
浏览器会自动编码，HTTP 客户端得自己 percent-encode 再用。

**状态会落盘**：每个任务的状态写在 `work/<job>/state.json`，日志写在 `work/<job>/run.log`。
服务重启后 `/api/state` 会从磁盘把失败原因捡回来；如果重启那一刻它还在跑，
会明确告诉你「被中断了」，而不是假装还在跑。这一点对 HTTP 客户端尤其有用——
断线重连之后接着轮询就行，不用自己记状态。

## 隐私

- 整本书和规则判读都留在本机；只有 AI 标定需要联网
- AI 标定会把抽样段、预算内升级段，以及这些段落的 `prev` / `next` 上下文发给你填写的接口；
  其余正文不发送
- 网页端把 API Key 存在浏览器 `localStorage`，Android 存在 App 私有 `SharedPreferences`；
  服务端不主动写进 `work/` 或日志，但客户端存储会保留 Key，且不是加密存储
- AI 标定使用固定的五分类提示词和锚点；系统提示词哈希、模型、端点主机名、种子、样本 id 和桶表都会写进 `calibration.json`

## 已知限制

- 只吃 TXT 和 EPUB。PDF 要先抽成文本，这层没做
- **AI 标定必须有可用的模型接口**：这一层是必经的，接口不通或额度用尽就不出书，
  不会静默退回纯规则
- **桌面应用层不再提供 PDF 导出**：引擎命令行的 PDF 工具还在，但网页/Android 的导出只走
  EPUB；PDF 要靠本机浏览器无头打印，几十万字的大书会把进程拖死
- 默认只听 `127.0.0.1`。`--host 0.0.0.0` 会让同一局域网的人访问书架、阅读版和删除接口，
  服务没有鉴权；只在你信任的网络里使用，别开到公网
- 单机单进程：并发没有管制，`work/` 也不会自动回收，用「删掉」自己清
- **桌面分发仍依赖本机有 Python**：Python 不打包进仓库。`启动.bat` 会先认 `py` 启动器、
  再用 `where python` 兜底（并排掉微软商店那个假 `python.exe`），版本低于 3.10 就直接提示并
  打开下载页；但换一台没装 Python 的机器，仍要用户先自己装一次。Android APK 运行时由
  Chaquopy 自带 Python，不受这一条影响；但构建 Android APK 的机器仍需 Python 3.10
