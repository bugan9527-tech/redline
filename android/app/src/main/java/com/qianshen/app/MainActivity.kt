package com.qianshen.app

import android.content.Intent
import android.content.pm.ApplicationInfo
import android.net.Uri
import android.os.Bundle
import android.provider.OpenableColumns
import android.util.Log
import android.webkit.JavascriptInterface
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Slider
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.viewinterop.AndroidView
import com.chaquo.python.PyObject
import com.chaquo.python.Python
import java.io.File
import java.security.MessageDigest
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.UUID
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.math.roundToInt
import org.json.JSONObject

/**
 * 壳层跟网页端同构的四页：首页 / 工作台 / 书架 / 阅读器。
 *
 *   Home   讲清楚它干什么，两个入口。
 *   Bench  干活的地方：选书 → 抽查 → 出整本（含必经的 AI 复核）→ 导出 EPUB。
 *   Shelf  书架：放在这台机器上的书，未判定的接着读原文，判定过的读深浅版。对着网页
 *          端 app/shelf.html 做：每本一条，状态（未判定 / 已判定 / 已导出）与续读位置从
 *          产物派生，动作有续读、导出 EPUB、改名、删除（整组一起）。
 *   Reader 读书的地方：抽查页与阅读版都在这儿的 WebView 里，只留字号与换书。
 *
 * 判定这件事跑在手机本地（Chaquopy 把仓库里那份只读引擎塞进 APK），只有 AI 复核那一步
 * 会连网。界面按网页端 app/ui.html 的分区排：一 · 选书、二 · AI 复核（必须）、
 * 三 · 进度、四 · 拿走、五 · 当前这本；阅读器页对着 app/reader.html 做，书架对着 app/shelf.html。
 *
 * AI 复核是必经的一层：引擎本体是纯本地规则，规则判不准的那一段模糊带一定要交给用户
 * 自己的模型复核一遍，再按复核结果重渲。没配好接口就不出书，接口不通也不静默退回纯规则。
 *
 * 产物一律落在 filesDir/books/ 下，不碰外部存储：SAF 给的是内容 URI，
 * 不用存储权限，取回来也只是我们自己看的副本。每本书一个目录（books/<job>/），
 * 目录里 name.txt 是显示名、src.txt 是引擎输入、out/ 是判定与导出产物——照网页端
 * work/<job>/ 的形状。meta.json 记内容指纹（去重用）、类型、创建时间与续读位置。
 */
class MainActivity : ComponentActivity() {

    private var page: Page by mutableStateOf(Page.Home)

    // 工作台自己那台状态机；四个手工阶段各有各的界面，见 Stage。
    private var stage: Stage by mutableStateOf(Stage.Picker)

    // 阅读器自己的状态与当前书号，跟工作台分开存。阅读器里直接添加的书只服务阅读，
    // 不把自己塞进工作台的「当前这本」，也不触发工作台的体检 / 抽查 / 判定状态。
    private var readerStage: Stage by mutableStateOf(Stage.Picker)
    private var readerJob: String by mutableStateOf("")
    // 从哪一页进的阅读器：返回键与沉浸式顶部那一行都按它退，别一律回工作台。
    private var readerReturn: Page = Page.Home

    // 导出这件事落在工作台的四号卡上，跑起来时压在任意页面上（对话框）。
    private var exportStage: EpubStage by mutableStateOf(EpubStage.Off)
    private var exportOptions: EpubOptions by mutableStateOf(EpubOptions())
    // 按下「导出」那一刻定下的选项；SAF 的选路径界面回来后用它，避免面板又被改一遍。
    private var pendingExport: EpubOptions? = null

    // AI 复核是必经的一层，不是可选开关：配好接口才让出书，复核结果直接改 analyzed.json。
    private var aiStage: AiStage by mutableStateOf(AiStage.Off)
    private var aiConfig: AiConfig by mutableStateOf(AiConfig())
    // AI 成功标记变化时 +1，让工作台只在必要时重算 analyzed.json 的校验哈希。
    private var aiRevision: Int by mutableStateOf(0)

    // 阅读器字号：直接喂给 WebView 的 textZoom，网页那边「字小/字大」也是这个意思。
    private var textZoom: Int by mutableStateOf(100)

    // 阅读器外观与版式：主题四档、亮度、字体、行距、段距、页宽、对齐，取值与语义都照搬
    // 网页端 reader.html 的「阅读设置」。落 App 私有 SharedPreferences，进来还是上次那套；
    // 改设置只重刷样式，不重载 WebView。
    private var readPrefs: ReadPrefs by mutableStateOf(ReadPrefs())

    // 「删掉这本」的确认框开没开。删的是 App 私有目录里那份副本（正文、判定结果、导出产物），
    // 用户自己选的原文件不动——跟桌面端 /api/job-delete 删整份工作目录是同一件事。
    private var deleteOpen: Boolean by mutableStateOf(false)

    // 读到哪儿：当前那一页（分章版是 ch####.html，单文件版是 book.html）、页内比例，
    // 以及章内第几页 / 本章共几页（底部那行进度要用）。落盘另按 800ms 节流。
    private var curPage: String by mutableStateOf("")
    private var curRatio: Double by mutableStateOf(0.0)
    private var curPageNo: Int by mutableStateOf(0)
    private var curPageCount: Int by mutableStateOf(0)
    private var spotAt: Long = 0L

    // 阅读页的壳（App 头部 + meta / 那排按钮）显不显示。**默认收起来**：进阅读页就是沉浸式，
    // 只留顶上一行「← 书名」和底下一行进度；点中间叫出壳，再点中间收回。
    private var chromeOn: Boolean by mutableStateOf(false)

    // 工作台当前这本（books/<job>/ 的目录名）。阅读器另用 readerJob，互不覆盖；
    // 冷启动时只有阅读器从 spot 偏好恢复上次读到那一屏。
    private var currentJob: String by mutableStateOf("")

    // 书架那份清单，进书架页时读一次文件系统算出来（跟网页端 /api/library 一个意思）。
    private var shelf: List<ShelfBook> by mutableStateOf(emptyList())
    private var shelfMsg: String by mutableStateOf("")
    private var shelfLoading: Boolean by mutableStateOf(false)
    private var shelfError: String by mutableStateOf("")
    // 连续进书架 / 重试时只认最后一次扫描，慢的旧线程回来不能覆盖新结果。
    private var shelfLoadSeq = 0

    // 改名对话框、「删掉这本」确认框、以及「导出后写去哪儿」那一步的落点。
    private var renameTarget: ShelfBook? by mutableStateOf(null)
    private var pendingDelete: ShelfBook? by mutableStateOf(null)
    private var saveTarget: File? = null

    private val bookPicker =
        registerForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
            if (uri != null) acceptBook(uri)
        }

    // 阅读器里的「添加文件」走这条：选完直接渲染出来读，不进体检 / 抽查 / 判定那条流水线。
    private val readPicker =
        registerForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
            if (uri != null) acceptRead(uri)
        }

    // SAF 建文件：Android 10+ 拿不到也没必要拿外部存储路径，直接给内容 URI 写字节。
    private val epubSaver = registerForActivityResult(
        ActivityResultContracts.CreateDocument("application/epub+zip"),
    ) { uri ->
        val opts = pendingExport
        pendingExport = null
        // 用户在系统文件选择器里点了返回：退回选项面板，别把整个导出流程丢掉。
        if (uri == null || opts == null) goToExport(EpubStage.Panel) else writeEpub(uri, opts)
    }

    // 书架「导出」：成品 EPUB 已经在 out/ 里躺着了，挑个落点把它拷出去就是（对着网页端那条
    // 「下载 <名字>.epub」）。走 SAF 建文件，跟工作台那条同一种落法。
    private val shelfSaver = registerForActivityResult(
        ActivityResultContracts.CreateDocument("application/epub+zip"),
    ) { uri ->
        val src = saveTarget
        saveTarget = null
        if (uri == null || src == null || !src.isFile) return@registerForActivityResult
        Thread {
            try {
                contentResolver.openOutputStream(uri).use { sink ->
                    requireNotNull(sink) { "打不开你选的落点" }
                    src.inputStream().use { it.copyTo(sink) }
                }
                runOnUiThread { shelfMsg = "已导出 ${src.name}" }
            } catch (e: Throwable) {
                Log.e(TAG, "书架导出失败", e)
                runOnUiThread { shelfMsg = "导出失败：${e.message ?: e}" }
            }
        }.start()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // 老版本只有一本（filesDir/book/），先把它搬进 books/ 并补上 meta，再谈别的。
        ensureMigrated()
        aiConfig = loadAiConfig()
        loadAppearance()
        // 续读要排在回归入口之前：回归是调试入口，真跑起来时该由它定界面。
        restoreSpot()
        // 回归入口：只有 debug 包、且 Intent 带了 regress 参数才走这条 headless 判定，
        // 不弹确认页、不等用户点任何东西。android/tools/regress.py 靠它跟桌面逐段对照。
        maybeRegress(intent)
        setContent {
            QsTheme {
                val hasAi = remember(currentJob, aiRevision) { aiReady(bookDir()) }
                Surface(Modifier.fillMaxSize(), color = QsBg) {
                    // 返回键按页退：阅读器 -> 工作台 -> 首页 -> 退出，别一按就把 App 关了。
                    // 有对话框压着时由对话框自己的窗口先吃掉返回，到不了这里。
                    BackHandler {
                        when (page) {
                            Page.Reader -> backFromReader()
                            Page.Shelf -> goToPage(Page.Home)
                            Page.Bench -> goToPage(Page.Home)
                            Page.Home -> finish()
                        }
                    }
                    // safeDrawingPadding：targetSdk 37 是强制 edge-to-edge，不加这层的话顶栏会画到
                    // 状态栏底下（「深浅」压在时间上、三个页名压在电量图标上，那一块点不动）。
                    // 底部手势条同理，正文别伸进去。对话框自己开窗口，各自处理 insets，不受这层影响。
                    Column(Modifier.fillMaxSize().safeDrawingPadding()) {
                        // 沉浸式阅读：在阅读器里点正文把壳收起来时，连 App 头部一起让位。
                        // 只在「阅读器页 + 已判读完/直接读」这一种状态下才收，别的页面永远显示，
                        // 所以不会出现"壳藏起来了、又没有任何地方能点回来"的死状态。
                        val immersive = !chromeOn && page == Page.Reader && readerStage is Stage.Reading
                        if (!immersive) AppHeader(page) { goToPage(it) }
                        when (page) {
                            Page.Home -> HomeScreen(
                                onBench = { goToPage(Page.Bench) },
                                onReader = { goToPage(Page.Reader) },
                            )
                            Page.Bench -> BenchScreen(
                                stage = stage,
                                title = bookName(),
                                hasSrc = File(bookDir(), "src.txt").isFile,
                                hasOut = File(bookDir(), "out/analyzed.json").isFile,
                                hasAi = hasAi,
                                export = exportStage,
                                options = exportOptions,
                                aiConfig = aiConfig,
                                onPick = { bookPicker.launch(PICK_MIME) },
                                onBuild = { startBuild() },
                                onReader = { goToPage(Page.Reader) },
                                onOptions = { exportOptions = it },
                                onExport = { startExport() },
                                onMoreExport = { openExport() },
                                onProvider = { applyAiConfig(aiConfig.withProvider(it)) },
                                onAiTest = { report -> probeAi(aiConfig, report) },
                                onAiReview = { startAiReview() },
                                onAiSettings = { openAiSettings() },
                            )
                            Page.Shelf -> ShelfScreen(
                                books = shelf,
                                message = shelfMsg,
                                loading = shelfLoading,
                                error = shelfError,
                                onReload = { loadShelf() },
                                onOpen = { openShelfBook(it) },
                                onExport = { book, fileName -> exportFromShelf(book, fileName) },
                                onRename = { renameTarget = it },
                                onDelete = { pendingDelete = it },
                                onBench = { goToPage(Page.Bench) },
                            )
                            Page.Reader -> ReaderScreen(
                                stage = readerStage,
                                title = bookName(readerJob),
                                textZoom = textZoom,
                                onZoom = { textZoom = it.coerceIn(ZOOM_MIN, ZOOM_MAX) },
                                prefs = readPrefs,
                                onPrefs = { readPrefs = it; saveAppearance() },
                                curPage = curPage,
                                curRatio = curRatio,
                                curPageNo = curPageNo,
                                curPageCount = curPageCount,
                                onPos = { f, r, n, t -> runOnUiThread { notePos(f, r, n, t) } },
                                chromeOn = chromeOn,
                                onTapBody = { runOnUiThread { chromeOn = !chromeOn } },
                                onDelete = { deleteOpen = true },
                                onAddFile = { readPicker.launch(PICK_MIME) },
                                onBuild = { startBuild() },
                                onPick = { bookPicker.launch(PICK_MIME) },
                                onToc = { openReaderToc() },
                                onBack = { backFromReader() },
                                onBench = { goToPage(Page.Bench) },
                            )
                        }
                    }
                    // 对话框都挂在最外层：不论当前在哪一页，导出与复核都压在整个界面上。
                    when (val e = exportStage) {
                        is EpubStage.Panel -> ExportPanelDialog(
                            exportOptions,
                            onOptions = { exportOptions = it },
                            onConfirm = { startExport() },
                            onDismiss = { closeExport() },
                        )
                        is EpubStage.Running -> BusyDialog("导出 EPUB", e.step)
                        is EpubStage.Done -> ExportMessageDialog("导出完成", e.message) { closeExport() }
                        is EpubStage.Fail -> ExportMessageDialog("导出失败", e.message) { closeExport() }
                        EpubStage.Off -> Unit
                    }
                    when (val a = aiStage) {
                        is AiStage.Settings -> AiSettingsDialog(
                            aiConfig,
                            onSave = { applyAiConfig(it); closeAi() },
                            onTest = { cfg, report -> probeAi(cfg, report) },
                            onDismiss = { closeAi() },
                        )
                        is AiStage.Running -> AiProgressDialog(a)
                        is AiStage.Done -> ExportMessageDialog("AI 复核完成", a.message) { closeAi() }
                        is AiStage.Fail -> ExportMessageDialog("AI 复核失败", a.message) { closeAi() }
                        AiStage.Off -> Unit
                    }
                    if (deleteOpen) {
                        DeleteBookDialog(
                            title = bookName(readerJob),
                            onConfirm = { deleteBook() },
                            onDismiss = { deleteOpen = false },
                        )
                    }
                    // 书架那条「改名」：改的是显示名，不动目录名（产物路径全靠目录名）。
                    renameTarget?.let { b ->
                        RenameDialog(
                            title = b.name,
                            onConfirm = { newName -> renameBook(b, newName); renameTarget = null },
                            onDismiss = { renameTarget = null },
                        )
                    }
                    // 书架那条「删除」：删的是这本在 App 私有目录里的整份副本。
                    pendingDelete?.let { b ->
                        DeleteBookDialog(
                            title = b.name,
                            onConfirm = { deleteShelfBook(b); pendingDelete = null },
                            onDismiss = { pendingDelete = null },
                        )
                    }
                }
            }
        }
    }

    // 壳已经在跑时再 am start 一次不会走 onCreate，regress 参数会走 onNewIntent —— 补上。
    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        maybeRegress(intent)
    }

    // 切走/熄屏时把位置补写一次：节流省掉的那一段，这里补回来。
    override fun onStop() {
        super.onStop()
        saveSpot()
    }

    // ---------- 回归：headless 判定入口（只有 debug 包会走） ----------

    /** 认 regress 参数并起后台判定；发布包（非 debuggable）里这个参数一律不认。 */
    private fun maybeRegress(intent: Intent?) {
        if (!isDebuggable()) return
        intent?.getStringExtra(EXTRA_REGRESS)?.let { startRegression(it) }
    }

    /**
     * `adb shell am start -n com.qianshen.app/.MainActivity --es regress <设备上 txt 路径>`
     * 触发的后台判定：跳过抽查确认，直接在 out/ 落 parsed.json / analyzed.json，跑完写
     * out/regress_done.json 供脚本轮询，界面停在阅读器页。判定本身仍是引擎那一份，
     * 这里一条网都不连——它只负责把引擎跑起来，好让桌面侧逐段比对。
     */
    private fun startRegression(devicePath: String) {
        val src = File(devicePath)
        // 回归固定落 books/regress/，好让 android/tools/regress.py 按固定路径取回产物。
        bindJob("regress")
        bindReaderJob("regress")
        val dir = bookDir()
        val out = File(dir, "out")
        val working = Stage.Working("回归：判定整本…", emptyList())
        goTo(working)
        goToReader(working)
        Thread {
            try {
                require(src.isFile) { "设备上找不到试验书：$devicePath" }
                dir.mkdirs()
                out.mkdirs()
                File(dir, "name.txt").writeText(src.name)
                val runner = Python.getInstance().getModule("runner")
                val bridge = ProgressBridge { appendLog(it) }
                val doc = asStringMap(
                    runner.callAttr(
                        "build", src.absolutePath, out.absolutePath,
                        null, 1, false, "auto", true, bridge,
                    ),
                )
                writeRegressDone(src, out, doc)
                val reader = readerHtml(out)
                requireNotNull(reader) { "阅读版没生成" }
                resetSpot()
                val reading = Stage.Reading(
                    reader, "回归 · ${src.name}", 0, metaOf(src.name, doc), tiersOf(doc),
                )
                goTo(reading)
                goToReader(reading)
                goToPage(Page.Reader)
            } catch (e: Throwable) {
                fail("回归失败", e)
            }
        }.start()
    }

    /** 摘要落盘：脚本轮询这个文件判断设备侧跑完没有，所以它是「跑完」的唯一信号。 */
    private fun writeRegressDone(src: File, out: File, doc: Map<String, PyObject>) {
        val tiers = doc["tiers"]?.let { asStringMap(it) }?.mapValues { it.value.toString() }
            ?: emptyMap()
        val o = JSONObject()
            .put("source", src.name)
            .put("chapters", doc["chapters"]?.toString()?.toIntOrNull() ?: 0)
            .put("chars", doc["chars"]?.toString()?.toIntOrNull() ?: 0)
            .put("names", doc["names"]?.toString()?.toIntOrNull() ?: 0)
            .put("tiers", JSONObject(tiers))
            .put("target", doc["target"]?.toString().orEmpty())
        File(out, "regress_done.json").writeText(o.toString())
    }

    /** debug 构建才认这个入口：发布包里 Intent 带了 regress 也不动。 */
    private fun isDebuggable(): Boolean =
        (applicationInfo.flags and ApplicationInfo.FLAG_DEBUGGABLE) != 0

    // ---------- 选书：内容 URI -> 私有目录的副本 ----------

    private fun acceptBook(uri: Uri) {
        val name = displayName(uri)
        // 选完就回工作台：后面体检、抽查都在那几号卡上看着。
        goToPage(Page.Bench)
        goTo(Stage.Working("取文书…", emptyList()))
        Thread {
            try {
                // 选中一次就尽量长期可读；有些 provider 不支持，失败了也不影响这次拷。
                try {
                    contentResolver.takePersistableUriPermission(
                        uri, Intent.FLAG_GRANT_READ_URI_PERMISSION,
                    )
                } catch (_: Throwable) {
                }
                // 先按新的一本落盘：目录照网页端 work/<job>/ 的形状，名字 + 源文件 + 产物。
                val job = newJobId()
                val dir = File(booksRoot(), job).apply { mkdirs() }
                File(dir, "name.txt").writeText(name)
                // 原始字节先落到私有目录：Chaquopy 只认普通路径，内容 URI 当不成文件喂进去。
                val raw = File(dir, "raw.bin")
                contentResolver.openInputStream(uri).use { input ->
                    requireNotNull(input) { "读不到所选文件" }
                    raw.outputStream().use { input.copyTo(it) }
                }
                // EPUB 是个 zip，引擎只吃 TXT：这一步把它摊平成章节分明的 src.txt，
                // 用的就是网页端那条路上同一个 bookread。TXT 只是改个名。
                val info = asStringMap(
                    Python.getInstance().getModule("runner").callAttr(
                        "prepare_source", raw.absolutePath, dir.absolutePath, name,
                    ),
                )
                if (info["kind"]?.toString() == "epub") {
                    appendLog("EPUB 已解成 ${info["chapters"]} 章正文")
                }
                // 同一本书再选一次不再入架：按内容指纹复用已有那份（对着网页端 save_book_from_raw）。
                val digest = info["source_hash"]?.toString().orEmpty()
                val existing = findBookByHash(digest, except = job)
                if (existing != null) {
                    dir.deleteRecursively()
                    bindJob(existing.name)
                    appendLog("书架里已经有这本书，接着用原来那份记录。")
                } else {
                    bindJob(job)
                    ensureMeta(dir, hash = digest, kind = info["kind"]?.toString().orEmpty())
                }
                val target = bookDir()
                if (aiReady(target)) {
                    // 只有这一版判定确实完整跑过 AI，才允许直接续读。
                    // 光有 analyzed.json 可能只是规则判定，不能绕过必经的 AI 一层。
                    showReading(target, name, syncBench = true)
                } else {
                    runDoctor(File(target, "src.txt"))
                }
            } catch (e: Throwable) {
                fail("取书失败", e)
            }
        }.start()
    }

    // ---------- 阅读器里直接读一本：选完就渲染，不进工作台 ----------

    /**
     * 阅读器里「添加文件」那一步。跟桌面端 /read 一样：文件丢进来直接解章渲染，翻到就能看，
     * 不做体检、抽查、判定，也不碰 AI 复核那一条。
     *
     * 差别只有一处：这本要留在书架上（对着网页端 /api/read?shelf=1），所以源文件与渲染结果
     * 落在 books/<job>/ 里，下次从书架点进来还是它。EPUB 里本来就带着的深浅标记由 bookread
     * 反解出来，随渲染一起落在 out/ 里，重开也还在。
     */
    private fun acceptRead(uri: Uri) {
        val name = displayName(uri)
        goToPage(Page.Reader)
        goToReader(Stage.Working("正在打开这本…", emptyList()))
        Thread {
            var newDir: File? = null
            try {
                try {
                    contentResolver.takePersistableUriPermission(
                        uri, Intent.FLAG_GRANT_READ_URI_PERMISSION,
                    )
                } catch (_: Throwable) {
                }
                val job = newJobId()
                val dir = File(booksRoot(), job).apply { mkdirs() }
                newDir = dir
                File(dir, "name.txt").writeText(name)
                val raw = File(dir, "raw.bin")
                contentResolver.openInputStream(uri).use { input ->
                    requireNotNull(input) { "读不到所选文件" }
                    raw.outputStream().use { input.copyTo(it) }
                }
                // 解章 + 渲染 + 落 source（open_direct 把网页端 /read 那条路挪进本机，并留档）。
                val info = asStringMap(
                    Python.getInstance().getModule("runner").callAttr(
                        "open_direct", raw.absolutePath, dir.absolutePath, name,
                    ),
                )
                val digest = info["source_hash"]?.toString().orEmpty()
                val existing = findBookByHash(digest, except = job)
                if (existing != null) {
                    dir.deleteRecursively()
                    bindReaderJob(existing.name)
                    showReading(existing, name)
                } else {
                    bindReaderJob(job)
                    ensureMeta(dir, hash = digest, kind = info["kind"]?.toString().orEmpty())
                    showReading(dir, name)
                }
            } catch (e: Throwable) {
                // 失败就撤掉这次刚建的半成品，别让书架上留下一条打不开的空记录。
                newDir?.takeIf { !metaFile(it).isFile }?.deleteRecursively()
                failReader("打不开这本", e)
            }
        }.start()
    }

    // ---------- 抽查：体检 + 前 3 章带标记渲染 ----------

    private fun runDoctor(src: File) {
        goTo(Stage.Working("源文件体检 + 抽查前 3 章…", emptyList()))
        val pre = File(bookDir(), "pre")
        val runner = Python.getInstance().getModule("runner")
        val doc = runner.callAttr("doctor", src.absolutePath, pre.absolutePath, 3, 1)
        val confirm = File(pre, "confirm/book.html")
        require(confirm.isFile) { "抽查页没生成：${confirm.path}" }
        // 体检完留工作台：先让用户看清抽查摘要再决定。想翻抽查正文，点「打开抽查页」。
        goTo(Stage.Confirming(confirm, summarize(doc)))
    }

    // ---------- 整本：后台跑 build，进度回调到界面 ----------

    private fun startBuild() {
        // AI 复核是必须的一层：接口还没配好就先别开工，把设置弹出来（那上面写着「必须」）。
        if (!aiConfig.ready) {
            goToPage(Page.Bench)
            openAiSettings()
            return
        }
        val cfg = aiConfig
        val dir = bookDir()
        val src = File(dir, "src.txt")
        val out = File(dir, "out")
        // 新的一次判定开始，旧的成功标记立即作废；中途失败不能沿用上一版结果。
        clearAiReady(dir)
        goToPage(Page.Bench)
        goTo(Stage.Working("开始判定…", emptyList()))
        Thread {
            try {
                val runner = Python.getInstance().getModule("runner")
                // 回调对象交给 Python 调 onStep(text)；它也跑在这条后台线程上，
                // 所以只往里塞数据，回主线程的事交给 appendLog。
                val bridge = ProgressBridge { appendLog(it) }
                runner.callAttr(
                    "build", src.absolutePath, out.absolutePath,
                    null, 1, false, "auto", true, bridge,
                )
                // 出整本之后紧接着复核模糊带：这一步不给跳过，改判结果直接落在 analyzed.json，
                // 阅读版和导出都按复核后的判定来。它跑完会自己重渲，所以这里不用再渲一次。
                val prog = File(out, "progress.json").apply { delete() }
                runner.callAttr(
                    "ai_review", out.absolutePath, cfg.endpoint, cfg.model, cfg.key,
                    AI_LO, AI_HI, AI_MAX, AI_BATCH, prog.absolutePath, bridge,
                    AI_CONCURRENCY,
                )
                val reader = readerHtml(out)
                requireNotNull(reader) { "阅读版没生成" }
                // 分层得读复核后的那份，不能拿 build 时那份旧的。
                val doc = asStringMap(runner.callAttr("read_analyzed", out.absolutePath))
                val job = currentJob
                val reading = Stage.Reading(
                    reader, bookName(job), 0,
                    metaOf(bookName(job), doc), tiersOf(doc),
                )
                runOnUiThread {
                    resetSpot()
                    readerJob = job
                    readerStage = reading
                    stage = reading
                    aiRevision++
                }
            } catch (e: Throwable) {
                fail("这一本没出成", e)
            }
        }.start()
    }

    // ---------- 导出 EPUB：面板 -> SAF 建文件 -> 写字节 ----------

    private fun openExport() = goToExport(EpubStage.Panel)

    private fun closeExport() = goToExport(EpubStage.Off)

    /** 记下这一刻的选项再弹系统文件选择器；落到哪由用户挑。 */
    private fun startExport() {
        if (!aiReady(bookDir())) {
            goToExport(EpubStage.Fail("AI 复核还没完整跑完，这次不能导出。"))
            return
        }
        pendingExport = exportOptions
        epubSaver.launch(epubFileName())
    }

    private fun writeEpub(uri: Uri, opts: EpubOptions) {
        goToExport(EpubStage.Running("打包 EPUB…"))
        Thread {
            try {
                val dir = bookDir()
                require(aiReady(dir)) { "AI 复核还没完整跑完，这次不能导出。" }
                val out = File(dir, "out")
                require(File(out, "analyzed.json").isFile) { "判定结果不在了，先读一遍整本" }
                // 先在私有目录落一份，再流拷贝到 SAF 的 URI：Chaquopy 只认普通路径，
                // 内容 URI 喂给 pack_epub 没法当文件写。
                val epub = File(out, epubFileName())
                val info = asStringMap(
                    Python.getInstance().getModule("runner").callAttr(
                        "export_epub", out.absolutePath, epub.absolutePath,
                        opts.preset, opts.lever, null, opts.keeps.toTypedArray(), opts.panel,
                    ),
                )
                contentResolver.openOutputStream(uri).use { sink ->
                    requireNotNull(sink) { "打不开你选的导出位置" }
                    epub.inputStream().use { it.copyTo(sink) }
                }
                goToExport(EpubStage.Done(describe(info, opts, epub.name)))
            } catch (e: Throwable) {
                Log.e(TAG, "导出 EPUB 失败", e)
                goToExport(EpubStage.Fail("导出失败：\n${e.message ?: e.toString()}"))
            }
        }.start()
    }

    private fun describe(info: Map<String, PyObject>, opts: EpubOptions, name: String): String {
        val kb = info["bytes"]?.toString()?.toLongOrNull()?.div(1024)
        return buildString {
            append("已导出 ").append(name)
            if (kb != null) append("（").append(kb).append(" KB）")
            append("\n").append(info["chapters"]).append(" 章 · ")
            append(info["graded"]).append(" 段带分层样式")
            append("\n强度 ").append(info["strength"])
            append(" · 淡化杠杆 ").append(opts.lever)
            append(" · 面板 ").append(opts.panel)
            append(" · 保留原文 ").append(opts.keeps.joinToString("、").ifBlank { "无" })
        }
    }

    /** 导出文件名：拿显示名去扩展名，再剔掉 SAF 不认的字符。 */
    private fun epubFileName(): String {
        val base = bookName()
            .replace(Regex("""\.(txt|epub)$""", RegexOption.IGNORE_CASE), "")
            .trim()
            .ifBlank { "book" }
        return base.replace(Regex("""[\\/:*?"<>|]"""), "_") + ".epub"
    }

    // ---------- AI 复核：设置 -> 复核模糊带 -> 重渲 -> 阅读页刷新 ----------

    private fun openAiSettings() = goToAi(AiStage.Settings)

    private fun closeAi() = goToAi(AiStage.Off)

    /** 设置改动落 SharedPreferences：密钥只进 App 私有目录，不写产物、不进日志。 */
    private fun applyAiConfig(cfg: AiConfig) {
        aiConfig = cfg
        getSharedPreferences(PREFS, MODE_PRIVATE).edit()
            .putString("endpoint", cfg.endpoint)
            .putString("model", cfg.model)
            .putString("key", cfg.key)
            .apply()
    }

    private fun loadAiConfig(): AiConfig {
        val p = getSharedPreferences(PREFS, MODE_PRIVATE)
        val d = AiConfig()
        return AiConfig(
            endpoint = p.getString("endpoint", null) ?: d.endpoint,
            model = p.getString("model", null) ?: d.model,
            key = p.getString("key", "").orEmpty(),
        )
    }

    // ---------- 阅读器外观与版式：主题 / 亮度 / 字体 / 行距 / 段距 / 页宽 / 对齐 ----------
    // 挑值也夹范围：脏值别把正文版式带跑（本地存储是外部输入）。

    private fun loadAppearance() {
        val p = getSharedPreferences(LOOK_PREFS, MODE_PRIVATE)
        val d = ReadPrefs()
        readPrefs = ReadPrefs(
            theme = p.getString("theme", d.theme)?.takeIf { it in READER_LOOKS } ?: d.theme,
            bright = p.getInt("bright", d.bright).coerceIn(BRIGHT_MIN, BRIGHT_MAX),
            font = p.getString("font", d.font)?.takeIf { it in FONT_STACKS } ?: d.font,
            lh = p.getFloat("lh", d.lh).coerceIn(LH_MIN, LH_MAX),
            gap = p.getFloat("gap", d.gap).coerceIn(GAP_MIN, GAP_MAX),
            width = p.getInt("width", d.width).coerceIn(WIDTH_MIN, WIDTH_MAX),
            align = p.getString("align", d.align)?.takeIf { it == "left" || it == JUSTIFY } ?: d.align,
        )
    }

    private fun saveAppearance() {
        val v = readPrefs
        getSharedPreferences(LOOK_PREFS, MODE_PRIVATE).edit()
            .putString("theme", v.theme)
            .putInt("bright", v.bright)
            .putString("font", v.font)
            .putFloat("lh", v.lh)
            .putFloat("gap", v.gap)
            .putInt("width", v.width)
            .putString("align", v.align)
            .apply()
    }

    // ---------- 阅读位置：读到哪一页、页内到哪儿 ----------
    // 桌面端 reader.html 把「书 + 章号 + 章内比例」存 IndexedDB，进来接着读；这里同理，
    // 只是书不是那份 JSON 而是渲染好的页面，所以记的是「哪一页 + 页内比例」。

    /** 这本的入口页：分章版是 index.html（目录），单文件版是 book.html。 */
    private fun entryName(out: File): String =
        listOf("index.html", "book.html").firstOrNull { File(out, it).isFile }
            ?: out.listFiles()?.firstOrNull { it.isFile }?.name.orEmpty()

    /** 换了一本（或重渲）：位置从入口页起，别把上一本读到的位置续到新书上。 */
    private fun resetSpot() {
        curPage = ""
        curRatio = 0.0
        curPageNo = 0
        curPageCount = 0
        spotAt = 0L
    }

    /** WebView 报上来的位置。落盘按 800ms 节流——读数时别一直往 SharedPreferences 写。 */
    private fun notePos(page: String, ratio: Double, no: Int, total: Int) {
        if (page.isBlank()) return
        curPage = page
        curRatio = ratio.coerceIn(0.0, 1.0)
        curPageNo = no
        curPageCount = total
        val now = System.currentTimeMillis()
        if (now - spotAt >= 800) {
            spotAt = now
            saveSpot()
        }
    }

    private fun saveSpot() {
        val s = readerStage as? Stage.Reading ?: return
        val out = s.html.parentFile ?: return
        if (!out.isDirectory) return
        val page = curPage.ifBlank { s.html.name }
        // 目录页 / 入口页不是正文章节，不能把“目录”误记成第 1 章，
        // 否则从目录退出后书架会把续读位置倒退。
        val chapterFiles = chapterPages(out)
        if (chapterFiles.isNotEmpty() && page !in chapterFiles) return
        val o = JSONObject()
            .put("job", readerJob)
            .put("name", s.title)
            .put("entry", entryName(out))
            .put("page", page)
            .put("ratio", curRatio)
            .put("judged", s.judged)
            .put("meta", s.meta)
            .put("tiers", s.tiers)
        getSharedPreferences(LOOK_PREFS, MODE_PRIVATE).edit()
            .putString(SPOT_KEY, o.toString()).apply()
        // 续读位置另写进这一本的 meta.json：书架那条「读到 第N章 · NN%」就是从这儿来的，
        // 跟网页端 /api/library/progress 写 meta 是同一件事（状态与位置都随书留着）。
        val dir = out.parentFile
        if (dir != null && dir.isDirectory && readerJob.isNotBlank()) {
            val m = readMeta(dir)
            m.put(
                "progress",
                JSONObject()
                    .put("chapter", chapterIndexOf(out, page))
                    .put("ratio", curRatio)
                    .put("page", page),
            )
            writeMeta(dir, m)
        }
    }

    private fun clearSpot() {
        resetSpot()
        getSharedPreferences(LOOK_PREFS, MODE_PRIVATE).edit().remove(SPOT_KEY).apply()
    }

    /**
     * 进来先翻翻上次读到哪：页面还在就接着读，书被删了（或页面没了）就当没记过。
     * 只恢复状态，不切页面——用户点「阅读器」那一下才落在上次那一屏。
     */
    private fun restoreSpot() {
        val raw = getSharedPreferences(LOOK_PREFS, MODE_PRIVATE).getString(SPOT_KEY, null)
            ?: return
        val o = try {
            JSONObject(raw)
        } catch (_: Throwable) {
            return
        }
        // 上次读到的是哪一本：位置记录带着 job，认出来就把它接回阅读器。
        // 工作台仍从空手开始，不能被阅读器直接打开的书带进判定流程。
        val job = o.optString("job")
        if (job.isNotBlank() && File(booksRoot(), job).isDirectory) readerJob = job
        if (readerJob.isBlank()) return
        val out = File(File(booksRoot(), readerJob), "out")
        val entry = o.optString("entry")
        val page = File(out, o.optString("page")).takeIf { it.isFile }
            ?: File(out, entry).takeIf { it.isFile }
            ?: return
        val name = o.optString("name")
        if (name.isBlank()) return
        readerStage = Stage.Reading(
            page, name, 0,
            o.optString("meta"), o.optString("tiers"), o.optBoolean("judged", true),
        )
        curPage = page.name
        curRatio = o.optDouble("ratio", 0.0).coerceIn(0.0, 1.0)
    }

    private fun probeAi(cfg: AiConfig, report: (String) -> Unit) {
        Thread {
            val msg = try {
                val info = asStringMap(
                    Python.getInstance().getModule("runner")
                        .callAttr("ai_probe", cfg.endpoint, cfg.model, cfg.key),
                )
                info["message"]?.toString().orEmpty()
            } catch (e: Throwable) {
                "测试失败：${e.message ?: e.toString()}"
            }
            runOnUiThread { report(msg) }
        }.start()
    }

    /**
     * 手动再复核一遍：出整本时已经自动跑过（那是必经的一步），这里留给改了接口、
     * 或者想重跑一次的场合。改的是 out/analyzed.json，再按同一套模式重渲阅读版；
     * 重渲写的是同一个文件名，所以读完要让 WebView 重载（版本号 +1，见 HtmlPane）。
     */
    private fun startAiReview() {
        val out = File(bookDir(), "out")
        if (!aiConfig.ready) { openAiSettings(); return }
        if (!File(out, "analyzed.json").isFile) {
            goToAi(AiStage.Fail("判定结果不在了，先读一遍整本再复核。"))
            return
        }
        val cfg = aiConfig
        clearAiReady(bookDir())
        val prog = File(out, "progress.json").apply { delete() }
        goToAi(AiStage.Running("复核模糊带…", 0, 0, 0, 0))
        Thread {
            // 复核跑在这条线程上，进度只能靠轮询 progress.json —— Python 那一层边跑边写它。
            val stop = AtomicBoolean(false)
            val poller = Thread {
                while (!stop.get()) {
                    readAiProgress(prog)?.let { p -> runOnUiThread { mergeAiProgress(p) } }
                    try {
                        Thread.sleep(400)
                    } catch (_: InterruptedException) {
                        stop.set(true)
                    }
                }
            }
            poller.start()
            try {
                val runner = Python.getInstance().getModule("runner")
                val bridge = ProgressBridge { msg -> runOnUiThread { mergeAiStep(msg) } }
                val info = asStringMap(
                    runner.callAttr(
                        "ai_review", out.absolutePath, cfg.endpoint, cfg.model, cfg.key,
                        AI_LO, AI_HI, AI_MAX, AI_BATCH, prog.absolutePath, bridge,
                        AI_CONCURRENCY,
                    ),
                )
                stop.set(true)
                poller.join(1000)
                refreshReader(out)
                runOnUiThread { aiRevision++ }
                goToAi(AiStage.Done(describeAi(info)))
            } catch (e: Throwable) {
                stop.set(true)
                Log.e(TAG, "AI 复核失败", e)
                goToAi(AiStage.Fail("AI 复核失败：\n${e.message ?: e.toString()}"))
            }
        }.start()
    }

    /** 复核改了 analyzed.json，重渲出来的是同一个文件，给阅读页换个版本号逼 WebView 重载。 */
    private fun refreshReader(out: File) {
        val reader = readerHtml(out) ?: return
        val job = out.parentFile?.name.orEmpty()
        runOnUiThread {
            if (currentJob == job) {
                val bench = stage
                if (bench is Stage.Reading) {
                    stage = bench.copy(html = reader, version = bench.version + 1)
                }
            }
            if (readerJob == job) {
                val reading = readerStage
                if (reading is Stage.Reading) {
                    readerStage = reading.copy(html = reader, version = reading.version + 1)
                }
            }
        }
    }

    private fun readAiProgress(f: File): AiStage.Running? = try {
        val o = JSONObject(f.readText())
        AiStage.Running("", o.optInt("done"), o.optInt("total"),
            o.optInt("reviewed"), o.optInt("failed"))
    } catch (_: Throwable) {
        null
    }

    /** 进度文件只有计数，界面上的那句话由 Python 侧的进度回调给，别互相盖掉。 */
    private fun mergeAiProgress(p: AiStage.Running) {
        val cur = aiStage
        if (cur is AiStage.Running) aiStage = p.copy(step = cur.step)
    }

    private fun mergeAiStep(msg: String) {
        val cur = aiStage
        if (cur is AiStage.Running) aiStage = cur.copy(step = msg)
    }

    private fun describeAi(info: Map<String, PyObject>): String {
        val asked = info["asked"]?.toString() ?: "0"
        val reviewed = info["reviewed"]?.toString() ?: "0"
        return buildString {
            if (asked == "0") {
                append("模糊带里没有段落，规则判定原样通过。")
            } else {
                append("模糊带挑出 ").append(asked).append(" 段，改用模型判定 ")
                append(reviewed).append(" 段")
            }
            append("\n阅读版与导出都会用复核后的判定。")
        }
    }

    // ---------- 书架：存储位置与一览 ----------
    // 一本一个目录（books/<job>/），跟网页端 work/<job>/ 一个形状：
    //   name.txt  显示名
    //   src.txt   引擎输入（EPUB 已摊平）
    //   meta.json 内容指纹（去重用）、类型、创建时间、续读位置
    //   pre/ out/ 抽查页与判定 / 导出产物
    // 目录名就是 job 号，产物路径全靠它，所以改名只动 name.txt 与 meta，不动目录。

    private fun booksRoot(): File = File(filesDir, "books").apply { mkdirs() }

    private fun bookDir() = File(booksRoot(), currentJob)

    private fun metaFile(dir: File) = File(dir, "meta.json")

    private fun readMeta(dir: File): JSONObject = try {
        JSONObject(metaFile(dir).readText())
    } catch (_: Throwable) {
        JSONObject()
    }

    private fun writeMeta(dir: File, o: JSONObject) {
        try {
            metaFile(dir).writeText(o.toString())
        } catch (_: Throwable) {
        }
    }

    private fun sha256(f: File): String =
        MessageDigest.getInstance("SHA-256").digest(f.readBytes())
            .joinToString("") { "%02x".format(it) }

    private fun aiOkFile(dir: File) = File(File(dir, "out"), "ai_ok.json")

    /**
     * AI 是否完整跑完这一版 analyzed.json。
     *
     * analyzed.json 只能说明规则判定跑过；calibration.json 在部分批次失败时也可能已写出。
     * ai_ok.json 由 runner 在“AI 无失败 + 标定已应用 + 阅读版已重渲”之后才写，并用
     * analyzed.json 的哈希绑定当前版本，防止拿旧书的成功标记给新结果背书。
     */
    private fun aiReady(dir: File): Boolean {
        val analyzed = File(File(dir, "out"), "analyzed.json")
        val marker = aiOkFile(dir)
        if (!analyzed.isFile || !marker.isFile) return false
        return try {
            val o = JSONObject(marker.readText())
            o.optInt("version", 0) == 1 &&
                o.optInt("failed", 1) == 0 &&
                o.optString("analyzed_sha256") == sha256(analyzed)
        } catch (_: Throwable) {
            false
        }
    }

    private fun clearAiReady(dir: File) {
        aiOkFile(dir).delete()
        runOnUiThread { aiRevision++ }
    }

    /** 起一个 job 号，形状跟桌面 engine.new_job 一致：时间戳-五位随机。 */
    private fun newJobId(): String {
        val ts = SimpleDateFormat("yyyyMMdd-HHmmss", Locale.US).format(Date())
        return "$ts-" + UUID.randomUUID().toString().replace("-", "").take(5)
    }

    /** 把当前这本切过去，并记进偏好——冷启动读回来还是它。 */
    private fun bindJob(job: String) {
        currentJob = job
        getSharedPreferences(LOOK_PREFS, MODE_PRIVATE).edit().putString("job", job).apply()
    }

    /** 阅读器当前这本，跟工作台当前这本分开记；两者都进同一份书架，但状态互不覆盖。 */
    private fun bindReaderJob(job: String) {
        readerJob = job
        getSharedPreferences(LOOK_PREFS, MODE_PRIVATE).edit().putString("job", job).apply()
    }

    /** 新建一本的 meta。hash 给内容指纹（去重），缺了就从 src.txt 现算。 */
    private fun ensureMeta(dir: File, hash: String = "", kind: String = "") {
        val name = File(dir, "name.txt").takeIf { it.isFile }?.readText()?.trim().orEmpty()
        val k = kind.ifBlank { if (name.endsWith(".epub", true)) "epub" else "txt" }
        val h = hash.ifBlank { File(dir, "src.txt").takeIf { it.isFile }?.let { sha256(it) }.orEmpty() }
        writeMeta(
            dir,
            JSONObject()
                .put("job", dir.name)
                .put("name", name)
                .put("kind", k)
                .put("created", dir.lastModified())
                .put("source_hash", h)
                .put("progress", JSONObject()),
        )
    }

    /** 按内容指纹找已入架的同一本：命中就复用它，不再入架（except 排除这次刚建的新目录）。 */
    private fun findBookByHash(hash: String, except: String = ""): File? {
        if (hash.isBlank()) return null
        return booksRoot().listFiles().orEmpty().firstOrNull { d ->
            d.isDirectory && d.name != except && metaFile(d).isFile &&
                readMeta(d).optString("source_hash") == hash
        }
    }

    /** 同一本书的一组目录（按内容指纹归并）——改名、删除都整组一起。 */
    private fun groupDirs(book: ShelfBook): List<File> {
        val self = File(booksRoot(), book.id)
        val hash = readMeta(self).optString("source_hash")
        if (hash.isBlank()) return listOf(self)
        return booksRoot().listFiles().orEmpty()
            .filter {
                it.isDirectory && metaFile(it).isFile &&
                    readMeta(it).optString("source_hash") == hash
            }
            .ifEmpty { listOf(self) }
    }

    /**
     * 老版本只有一本：filesDir/book/。第一次跑新版时把它搬进 books/ 并补上 meta，
     * 老的单点续读记录也并进这一本的 progress。搬过一次之后就不再动。
     */
    private fun ensureMigrated() {
        val legacy = File(filesDir, "book")
        val root = booksRoot()
        if (root.listFiles().orEmpty().any { it.isDirectory }) return
        if (!legacy.isDirectory) return
        val hasBook = File(legacy, "name.txt").isFile || File(legacy, "src.txt").isFile ||
            File(legacy, "out").isDirectory
        if (!hasBook) return
        val job = newJobId()
        if (!legacy.renameTo(File(root, job))) return
        val dir = File(root, job)
        ensureMeta(dir)
        // 老的单点位置记录没有 job 号，认不回是哪一本；把当前这本直接钉在这本上，
        // restoreSpot 就能按它把上次读到的那一屏接回来。
        bindJob(job)
        // 老位置里那点续读进度也带过来。
        val spot = getSharedPreferences(LOOK_PREFS, MODE_PRIVATE).getString(SPOT_KEY, null) ?: return
        try {
            val o = JSONObject(spot)
            val m = readMeta(dir)
            m.put(
                "progress",
                JSONObject().put("chapter", 0)
                    .put("ratio", o.optDouble("ratio", 0.0))
                    .put("page", o.optString("page")),
            )
            writeMeta(dir, m)
        } catch (_: Throwable) {
        }
    }

    /** 最近改过的一本排在前面——跟网页端 library() 一个口径。 */
    private fun library(): List<ShelfBook> {
        val groups = LinkedHashMap<String, MutableList<File>>()
        for (d in booksRoot().listFiles().orEmpty()) {
            if (!d.isDirectory || !metaFile(d).isFile) continue
            val hash = readMeta(d).optString("source_hash")
            groups.getOrPut(hash.ifBlank { "job:${d.name}" }) { mutableListOf() }.add(d)
        }
        return groups.values
            .mapNotNull { ds ->
                val canonical = chooseCanonical(ds)
                adoptBestProgress(canonical, ds)
                entryOf(canonical, ds.size)
            }
            .sortedByDescending { it.added }
    }

    /** 一条书目从哪一本当门面：优先已导出 / 已判定，再看读得最远、建得最晚。 */
    private fun chooseCanonical(ds: List<File>): File {
        val rank = mapOf("source" to 1, "judged" to 2, "exported" to 3)

        fun stateOf(d: File): String {
            val out = File(d, "out")
            return when {
                !File(out, "analyzed.json").isFile -> "source"
                out.listFiles().orEmpty().any { it.name.endsWith(".epub", true) } -> "exported"
                else -> "judged"
            }
        }

        // compareBy + thenBy：三级排序写成一条比较器，避免 Triple 在泛型推断上卡住。
        val by = compareBy<File> { rank[stateOf(it)] ?: 0 }
            .thenBy { readMeta(it).optJSONObject("progress")?.optInt("chapter", -1) ?: -1 }
            .thenBy { readMeta(it).optLong("created", it.lastModified()) }
        return ds.maxWithOrNull(by) ?: ds.first()
    }

    /** 同组副本里读得最远的位置；只认真正写过 chapter 的 progress。 */
    private fun bestShelfProgress(ds: List<File>): JSONObject? {
        var best: JSONObject? = null
        var bestChapter = -1
        var bestRatio = -1.0
        for (dir in ds) {
            val p = readMeta(dir).optJSONObject("progress") ?: continue
            val chapter = p.optInt("chapter", -1)
            if (chapter < 0) continue
            val ratio = p.optDouble("ratio", 0.0).coerceIn(0.0, 1.0)
            if (best == null || chapter > bestChapter ||
                (chapter == bestChapter && ratio > bestRatio)
            ) {
                best = JSONObject(p.toString())
                bestChapter = chapter
                bestRatio = ratio
            }
        }
        return best
    }

    /**
     * 门面书自己还没记过位置时，把旧副本里读得最远的位置迁进来。
     *
     * 只补缺、不改已有值：读者若在门面书里往回重读，下一次书架加载不能被另一份旧副本
     * 的更大章号又拽回去。写回 meta 还有一层必要——书架点开的是门面书，阅读器续读也从
     * 它的 meta 取 page / ratio，不能只改界面显示。
     */
    private fun adoptBestProgress(canonical: File, ds: List<File>) {
        val meta = readMeta(canonical)
        val own = meta.optJSONObject("progress")
        if (own != null && own.optInt("chapter", -1) >= 0) return
        val best = bestShelfProgress(ds) ?: return
        if (best.optString("page").isBlank()) {
            val out = File(canonical, "out")
            val chapter = best.optInt("chapter", 0)
            val page = chapterPages(out).getOrNull(chapter) ?: readerHtml(out)?.name
            if (!page.isNullOrBlank()) best.put("page", page)
        }
        meta.put("progress", best)
        writeMeta(canonical, meta)
    }

    /**
     * 把一本算成书架条目。状态从产物派生，不另存一份，免得两处对不上：
     * 没判定结果（out/analyzed.json）= 未判定；有判定但没成品 = 已判定；out/ 里有 .epub = 已导出。
     */
    private fun entryOf(dir: File, copies: Int): ShelfBook {
        val out = File(dir, "out")
        val m = readMeta(dir)
        val name = File(dir, "name.txt").takeIf { it.isFile }?.readText()?.trim().orEmpty()
            .ifBlank { m.optString("name").ifBlank { dir.name } }
        val kind = m.optString("kind").ifBlank { if (name.endsWith(".epub", true)) "epub" else "txt" }
        val epubs = out.listFiles().orEmpty().filter { it.isFile && it.name.endsWith(".epub", true) }
        val state = when {
            !File(out, "analyzed.json").isFile -> "source"
            epubs.isNotEmpty() -> "exported"
            else -> "judged"
        }
        val prog = m.optJSONObject("progress")
        return ShelfBook(
            id = dir.name,
            name = name,
            kind = kind,
            state = state,
            chapter = prog?.optInt("chapter", -1) ?: -1,
            ratio = prog?.optDouble("ratio", 0.0) ?: 0.0,
            seen = prog != null && prog.has("chapter"),
            files = epubs.map { it.name }.sorted(),
            copies = copies,
            added = m.optLong("created", dir.lastModified()),
        )
    }

    /** 进书架页读一次文件系统，把清单算出来（对着网页端 /api/library）。 */
    private fun loadShelf() {
        val seq = ++shelfLoadSeq
        shelfLoading = true
        shelfError = ""
        shelfMsg = "正在翻书架…"
        Thread {
            val result = runCatching { library() }
            result.exceptionOrNull()?.let { Log.e(TAG, "读书架失败", it) }
            runOnUiThread {
                if (seq != shelfLoadSeq) return@runOnUiThread
                shelfLoading = false
                result.onSuccess { books ->
                    shelf = books
                    shelfError = ""
                    shelfMsg = if (books.isEmpty()) "" else "共 ${books.size} 本。"
                }.onFailure { e ->
                    // 读失败时保留上一次成功的书目，错误单独显示；整页不能伪装成「空书架」。
                    shelfError = "读不到书架：${e.message ?: "本地存储读取失败"}"
                    shelfMsg = ""
                }
            }
        }.start()
    }

    /** 这一页在正文第几章（0 起）：分章版按 ch####.html 排位，单文件版算第 0 章。 */
    private fun chapterIndexOf(out: File, page: String): Int =
        chapterPages(out).indexOf(page).coerceAtLeast(0)

    /**
     * 打开一本书进阅读器：有判定结果读深浅版，没有就读原文（没有现成正文页就先渲一次）。
     * 位置从 meta.progress 接上——续读这一屏就是上次离开的那一屏。
     */
    private fun showReading(dir: File, fallbackName: String, syncBench: Boolean = false) {
        val out = File(dir, "out")
        val src = File(dir, "src.txt")
        val runner = Python.getInstance().getModule("runner")
        val name = File(dir, "name.txt").takeIf { it.isFile }?.readText()?.trim().orEmpty()
            .ifBlank { fallbackName }
        val html = readerHtml(out)
        val judged = File(out, "analyzed.json").isFile
        val doc: Map<String, PyObject> = when {
            judged -> asStringMap(runner.callAttr("read_analyzed", out.absolutePath))
            html != null && src.isFile ->
                // 正文页已经渲好了（阅读器里加进来的那本），只读概要，不重渲。
                asStringMap(runner.callAttr("source_meta", src.absolutePath, src.name))
            html != null ->
                // 老版本在阅读器里加进来的那本：只有正文页、没留 src.txt，就按现成页面读，
                // 概要留空。
                emptyMap()
            else -> {
                // 工作台里选进来还没判定的：现渲一次，读的就是原文。
                require(src.isFile) { "这本没有源文件了，去工作台重选一本。" }
                asStringMap(runner.callAttr("read_direct", src.absolutePath, out.absolutePath, name))
            }
        }
        val target = html ?: error("阅读版没生成")
        runOnUiThread {
            resetSpot()
            val prog = readMeta(dir).optJSONObject("progress")
            curPage = prog?.optString("page").orEmpty()
            curRatio = prog?.optDouble("ratio", 0.0) ?: 0.0
            val reading = Stage.Reading(
                target, name, 0, metaOf(name, doc), tiersOf(doc), judged = judged,
            )
            readerJob = dir.name
            readerStage = reading
            if (syncBench) {
                currentJob = dir.name
                stage = reading
            }
            goToPage(Page.Reader)
        }
    }

    /** 阅读器顶栏的「目录」：回到引擎生成的可点击章节目录页。 */
    private fun openReaderToc() {
        val s = readerStage as? Stage.Reading ?: return
        val out = s.html.parentFile ?: return
        val toc = File(out, "index.html")
        if (!toc.isFile) return
        saveSpot()                 // 先把当前章节位置留住
        curPage = toc.name
        curRatio = 0.0
        curPageNo = 0
        curPageCount = 0
        // version 变化让 HtmlPane 重新加载入口目录页；点章节后仍是 WebView 内部跳转。
        readerStage = s.copy(html = toc, version = s.version + 1)
        goToPage(Page.Reader)
    }

    /** 书架点开一本：切到它、进阅读器（后台把正文与位置准备好）。 */
    private fun openShelfBook(book: ShelfBook) {
        val dir = File(booksRoot(), book.id)
        if (!dir.isDirectory) {
            shelfMsg = "这本不在了。"
            loadShelf()
            return
        }
        bindReaderJob(book.id)
        goToPage(Page.Reader)
        goToReader(Stage.Working("正在打开这本…", emptyList()))
        Thread {
            try {
                showReading(dir, book.name)
            } catch (e: Throwable) {
                failReader("打不开这本", e)
            }
        }.start()
    }

    /** 书架「导出」：只导出按钮上点中的那一份成品，不能拿列表第一份顶替。 */
    private fun exportFromShelf(book: ShelfBook, fileName: String) {
        val name = book.files.firstOrNull { it == fileName }
        val file = name?.let { File(File(File(booksRoot(), book.id), "out"), it) }
        if (file == null || !file.isFile) {
            shelfMsg = "这本还没有可导出的成品。"
            return
        }
        saveTarget = file
        shelfSaver.launch(file.name)
    }

    /** 书架「改名」：只动显示名（name.txt 与 meta），不动目录与产物路径。 */
    private fun renameBook(book: ShelfBook, newName: String) {
        val clean = newName.trim().take(80)
        if (clean.isBlank()) return
        val dirs = groupDirs(book)
        Thread {
            try {
                for (dir in dirs) {
                    File(dir, "name.txt").writeText(clean)
                    val m = readMeta(dir)
                    m.put("name", clean)
                    writeMeta(dir, m)
                }
                val hitBench = dirs.any { it.name == currentJob }
                val hitReader = dirs.any { it.name == readerJob }
                runOnUiThread {
                    if (hitBench) {
                        val s = stage
                        if (s is Stage.Reading) stage = s.copy(title = clean)
                    }
                    if (hitReader) {
                        val s = readerStage
                        if (s is Stage.Reading) readerStage = s.copy(title = clean)
                    }
                    loadShelf()
                }
            } catch (e: Throwable) {
                Log.e(TAG, "改名失败", e)
                runOnUiThread { shelfMsg = "改名失败：${e.message ?: e}" }
            }
        }.start()
    }

    /** 书架「删除」：把这一本（整组）在 App 私有目录里的副本整个清掉。 */
    private fun deleteShelfBook(book: ShelfBook) {
        val dirs = groupDirs(book)
        val hitBench = dirs.any { it.name == currentJob }
        val hitReader = dirs.any { it.name == readerJob }
        runOnUiThread {
            if (hitReader) {
                clearSpot()
                readerStage = Stage.Picker
                bindReaderJob("")
            }
            if (hitBench) {
                stage = Stage.Picker
                bindJob("")
            }
        }
        Thread {
            try {
                dirs.forEach { it.deleteRecursively() }
            } catch (e: Throwable) {
                Log.e(TAG, "删书失败", e)
                runOnUiThread { shelfMsg = "删不掉：${e.message ?: e}" }
            }
            runOnUiThread { loadShelf() }
        }.start()
    }

    // ---------- 杂项 ----------

    /**
     * 删掉当前这本：把 App 私有目录里那份副本整个清掉（raw.bin / src.txt / pre / out 全在里面），
     * 跟桌面端删整份工作目录是同一件事。用户自己选的源文件只是读进来一份副本，这里动不到它。
     * 界面先空手（回到选书状态），删目录放后台，别为几 MB 的文件 IO 卡住主线程。
     */
    private fun deleteBook() {
        deleteOpen = false
        val job = readerJob
        if (job.isBlank()) {
            readerStage = Stage.Picker
            return
        }
        val dir = File(booksRoot(), job)
        val hitBench = currentJob == job
        runOnUiThread {
            clearSpot()   // 位置记录跟着这本一起作废，别拿去续一本已经没了的书
            readerStage = Stage.Picker
            bindReaderJob("")
            if (hitBench) {
                stage = Stage.Picker
                bindJob("")
            }
        }
        Thread {
            try {
                dir.deleteRecursively()
                check(!dir.exists()) { "目录还有残留：${dir.path}" }
            } catch (e: Throwable) {
                Log.e(TAG, "删书失败", e)
                goToReader(Stage.Failed("删不掉：\n${e.message ?: e.toString()}"))
            }
        }.start()
    }

    private fun bookName(job: String = currentJob): String = File(File(booksRoot(), job), "name.txt")
        .takeIf { it.isFile }?.readText()?.trim().orEmpty().ifBlank { "未命名" }

    /** 阅读版落哪个文件：分章版是 index.html，整本版是 book.html。 */
    private fun readerHtml(out: File): File? =
        listOf(File(out, "index.html"), File(out, "book.html")).firstOrNull { it.isFile }

    /** 阅读器页顶上那行：类型 · 标题 · N 章 · N 字。 */
    private fun metaOf(name: String, doc: Map<String, PyObject>): String {
        val kind = name.substringAfterLast('.', "txt").uppercase()
        val chapters = doc["chapters"]?.toString() ?: "?"
        val chars = doc["chars"]?.toString() ?: "?"
        return "$kind · $name · $chapters 章 · $chars 字"
    }

    /** 分层计数那行，跟桌面 TALLY_LABEL 一套口径。 */
    private fun tiersOf(doc: Map<String, PyObject>): String {
        val t = doc["tiers"]?.let { asStringMap(it) }.orEmpty()
        if (t.isEmpty()) return ""
        return t.entries.joinToString(" · ") { "${TIER_LABEL[it.key] ?: it.key} ${it.value}" }
    }

    private fun displayName(uri: Uri): String {
        contentResolver.query(uri, null, null, null, null)?.use { c ->
            val i = c.getColumnIndex(OpenableColumns.DISPLAY_NAME)
            if (i >= 0 && c.moveToFirst()) return c.getString(i) ?: "book.txt"
        }
        return uri.lastPathSegment ?: "book.txt"
    }

    private fun summarize(doc: PyObject): String {
        val top = asStringMap(doc)
        val tier = top["tiers"]?.let { asStringMap(it) }.orEmpty()
        val tiers = tier.entries.joinToString(" · ") { "${TIER_LABEL[it.key] ?: it.key} ${it.value}" }
        val notes = top["verdict"]?.let { asStringMap(it)["notes"] }
            ?.asList()?.joinToString("；") { it.toString() }.orEmpty()
        return buildString {
            append("抽查 ").append(top["chapters"]).append(" 章 / ").append(top["chars"])
            append(" 字 · 人名候选 ").append(top["names"])
            if (tiers.isNotEmpty()) append("\n分层：").append(tiers)
            append("\n源文件体检：").append(notes.ifBlank { "通过" })
        }
    }

    private fun appendLog(msg: String) = runOnUiThread {
        val cur = stage
        if (cur is Stage.Working) stage = cur.copy(step = msg, logs = cur.logs + msg)
    }

    // 不能叫 setStage：stage 是 by mutableStateOf 的属性，编译器生成的 setter 正好同名。
    private fun goTo(s: Stage) = runOnUiThread { stage = s }
    private fun goToReader(s: Stage) = runOnUiThread { readerStage = s }

    private fun goToPage(p: Page) = runOnUiThread {
        val from = page
        if (p == Page.Reader && from != Page.Reader) {
            readerReturn = from
            // 从工作台点进阅读器时，把工作台当前这本（抽查页 / 已判读）带过去。
            // 阅读器直接添加的书不反向写回工作台。
            if (from == Page.Bench && stage !is Stage.Picker) {
                readerJob = currentJob
                readerStage = stage
            }
        }
        page = p
        // 落到阅读页就沉浸；落到别的页面一定把壳放回来（那几页没有"点中间叫出"这条路）
        chromeOn = p != Page.Reader
        // 进书架页就读一次文件系统，把清单算出来（对着网页端 /api/library）。
        if (p == Page.Shelf) loadShelf()
    }

    /** 阅读器的返回目标跟着入口走：书架进来的回书架，工作台进来的回工作台。 */
    private fun backFromReader() {
        when (readerReturn) {
            Page.Shelf -> goToPage(Page.Shelf)
            Page.Bench -> goToPage(Page.Bench)
            else -> goToPage(Page.Home)
        }
    }

    private fun goToExport(s: EpubStage) = runOnUiThread { exportStage = s }

    private fun goToAi(s: AiStage) = runOnUiThread { aiStage = s }

    private fun fail(prefix: String, e: Throwable) {
        Log.e(TAG, prefix, e)
        goTo(Stage.Failed("$prefix：\n${e.message ?: e.toString()}"))
    }

    private fun failReader(prefix: String, e: Throwable) {
        Log.e(TAG, prefix, e)
        goToReader(Stage.Failed("$prefix：\n${e.message ?: e.toString()}"))
    }

    private companion object {
        const val TAG = "qianshen"
        // .txt 各家报的 MIME 不一致，octet-stream 也得放进来；epub 自带一个 MIME，
        // 漏了它文件在选择器里是灰的、点不动。
        val PICK_MIME = arrayOf("text/plain", "application/octet-stream", "application/epub+zip")

        // AI 接入点存这儿；密钥只在这份 App 私有存储里，不进产物、不进日志。
        const val PREFS = "ai"

        // 阅读器外观（主题 / 亮度）另存一份，跟 AI 那套分开，互不干扰。
        const val LOOK_PREFS = "read"

        // 阅读位置那一条就存在同一份 reader 偏好里，单开一个键。
        const val SPOT_KEY = "spot"

        // 复核预算、批大小与并发：与桌面版 ai_layer 的口径保持一致。
        const val AI_LO = 0.30
        const val AI_HI = 0.70
        // 桌面与手机共用同一口径：800 是抽样 + 定向升级的总预算。
        const val AI_MAX = 800
        const val AI_BATCH = 16
        const val AI_CONCURRENCY = 3

        // 回归入口的 Intent extra 名：见 startRegression / android/tools/regress.py。
        const val EXTRA_REGRESS = "regress"
    }
}

// 阅读器字号范围，对应 WebView textZoom 的百分比；两步一档，界面上的「字小/字大」都走它。
private const val ZOOM_MIN = 70
private const val ZOOM_MAX = 200
private const val ZOOM_STEP = 10

// 亮度范围与步长对齐网页端 reader.html 那条滑杆：40–100%，每档 5%。
private const val BRIGHT_MIN = 40
private const val BRIGHT_MAX = 100
private const val BRIGHT_STEPS = 11
private const val DEFAULT_THEME = "light"
private const val DEFAULT_BRIGHT = 100

// 版式范围与步长同样照搬网页端「阅读设置」：行距 1.5–2.6、段距 0–2、页宽 32–56em。
private const val LH_MIN = 1.5f
private const val LH_MAX = 2.6f
private const val LH_STEPS = 21
private const val GAP_MIN = 0f
private const val GAP_MAX = 2f
private const val GAP_STEPS = 39
private const val WIDTH_MIN = 32
private const val WIDTH_MAX = 56
private const val WIDTH_STEPS = 11
private const val DEFAULT_FONT = "song"
private const val DEFAULT_LH = 1.95f
private const val DEFAULT_GAP = 1.05f
private const val DEFAULT_WIDTH = 44
private const val DEFAULT_ALIGN = "left"
private const val JUSTIFY = "justify"

/**
 * Chaquopy 回调桥。Python 侧调的是 progress.onStep(text)，名字必须对上。
 * 类与方法都得是 public，不然 Python 反射不到。
 */
class ProgressBridge(private val sink: (String) -> Unit) {
    fun onStep(message: String) = sink(message)
}

/** 工作台那台状态机。Working 同时承担「取书 / 体检 / 整本判定」，差别只在日志内容。 */
sealed interface Stage {
    data object Picker : Stage
    data class Working(val step: String, val logs: List<String>) : Stage
    data class Confirming(val html: File, val summary: String) : Stage
    // version 只在 AI 复核重渲后 +1：重渲写的是同一个文件，靠它把 WebView 逼着重载一次。
    // meta 是「类型 · 标题 · N 章 · N 字」，tiers 是分层计数，都只在阅读器页上显示。
    // judged=false 是阅读器里直接读进来的：没过体检 / 抽查 / 判定，工作台上别把它说成"判读完"。
    data class Reading(
        val html: File,
        val title: String,
        val version: Int = 0,
        val meta: String = "",
        val tiers: String = "",
        val judged: Boolean = true,
    ) : Stage
    data class Failed(val message: String) : Stage
}

/**
 * 书架的一条书目。状态（未判定 / 已判定 / 已导出）与续读位置都从产物派生，不另存一份——
 * 跟网页端 library() 给的那几条一个意思。files 是 out/ 里现成的 EPUB 成品，供导出。
 */
private data class ShelfBook(
    val id: String,
    val name: String,
    val kind: String,
    val state: String,
    val chapter: Int,
    val ratio: Double,
    val seen: Boolean,
    val files: List<String>,
    val copies: Int,
    val added: Long,
)

private val TIER_LABEL = mapOf(
    "plot" to "情节", "mid" to "普通", "weak" to "偏水",
    "fade" to "注水", "zero" to "零信息", "sep" to "分隔",
)

/**
 * 导出选项。默认全部取中间值：平衡 / 灰字 / 面板全留 / 不保留原文。
 *
 * 这几个名字是直接透传给引擎的（preset 就是强强度 0.0/0.55/1.0，keeps 就是 pack_epub
 * 认的那几个分组），Android 侧不留任何第二套语义。
 */
data class EpubOptions(
    val preset: String = "balance",
    val lever: String = "color",
    val panel: String = "full",
    val keeps: Set<String> = emptySet(),
)

/** 导出在界面上的几个态。除 Off 外都靠对话框压在最上面，WebView 不重建。 */
sealed interface EpubStage {
    data object Off : EpubStage
    data object Panel : EpubStage
    data class Running(val step: String) : EpubStage
    data class Done(val message: String) : EpubStage
    data class Fail(val message: String) : EpubStage
}

/**
 * AI 复核在界面上的几个态。Off 是常态——对话框没弹着；Running 这一段是出整本时自动跑的
 * 那一遍（必经），也可以从工作台手动再跑一遍。进度（done/total）来自 Python 侧写的
 * progress.json，界面每 400ms 读一次。
 */
sealed interface AiStage {
    data object Off : AiStage
    data object Settings : AiStage
    data class Running(val step: String, val done: Int, val total: Int,
                       val reviewed: Int, val failed: Int) : AiStage
    data class Done(val message: String) : AiStage
    data class Fail(val message: String) : AiStage
}

/**
 * 用户自己那套模型接入点。端点按 OpenAI 兼容的 /chat/completions 约定，默认取官方地址；
 * 密钥只存 App 私有 SharedPreferences，不进产物、不进日志。
 */
data class AiConfig(
    val endpoint: String = "https://api.openai.com/v1",
    val model: String = "gpt-4o-mini",
    val key: String = "",
) {
    val ready: Boolean get() = key.isNotBlank()

    /** 换一家：地址与模型一起换，密钥保留（各家密钥不通用，用户自己会重填）。 */
    fun withProvider(p: AiProvider) = copy(endpoint = p.endpoint, model = p.model)
}

/** 一键填的那几家，取值与网页端 ui.html 的 PRESET 完全一致。 */
data class AiProvider(val label: String, val endpoint: String, val model: String)

private val AI_PROVIDERS = listOf(
    AiProvider("DeepSeek", "https://api.deepseek.com/v1", "deepseek-chat"),
    AiProvider("Kimi", "https://api.moonshot.cn/v1", "moonshot-v1-8k"),
    AiProvider("通义千问", "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
    AiProvider("智谱 GLM", "https://open.bigmodel.cn/api/paas/v4", "glm-4-flash"),
    AiProvider("OpenAI", "https://api.openai.com/v1", "gpt-4o-mini"),
)

private val PRESET_CHOICES = listOf(
    "balance" to "平衡", "original" to "原汁原味", "plotonly" to "只看剧情",
)
private val LEVER_CHOICES = listOf("color" to "灰字", "opacity" to "透明")
private val PANEL_CHOICES = listOf("full" to "全部保留", "collapse" to "折叠")
private val KEEP_CHOICES = listOf(
    "sys_panel" to "系统面板", "scenery" to "景物", "short_burst" to "短句暴走",
    "recap" to "回顾", "dialogue" to "对话", "junk" to "零信息段",
)

/** PyObject dict -> Map<String, PyObject>。Chaquopy 的 asMap 键是 PyObject，得转成字符串才好取。 */
private fun asStringMap(o: PyObject): Map<String, PyObject> =
    o.asMap().entries.associate { it.key.toString() to it.value }

// ---------- 首页 ----------

/** 对着网页端 index.html 的 hero：两行大字 + 一段说明 + 两个入口，再补一段三步说明。 */
@Composable
private fun HomeScreen(onBench: () -> Unit, onReader: () -> Unit) {
    Column(
        Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(horizontal = 20.dp),
    ) {
        Spacer(Modifier.height(40.dp))
        Text("把重要的情节，", style = MaterialTheme.typography.displaySmall, color = QsInk)
        Text("读得更深。", style = MaterialTheme.typography.displaySmall, color = QsInk)
        VGap(18)
        Text(
            "放一本中文网文 TXT 或 EPUB 进去。它逐段读完，把情节压深、把注水退浅，深度由你调。" +
                "原文一个字不改：判定在你手机本地跑，判不准的那一段交给你自己的模型复核。",
            fontSize = 16.sp, lineHeight = 26.sp, color = QsMuted,
        )
        VGap(28)
        QsButton("进入工作台", onBench, Modifier.fillMaxWidth())
        VGap(12)
        QsButton("打开阅读器", onReader, Modifier.fillMaxWidth(), quiet = true)
        VGap(36)
        SectionCard("它怎么工作") {
            StepRow("一", "选书、体检", "挑一本 TXT 或 EPUB，先体检源文件，再抽查前 3 章带标记渲染给你看。")
            StepRow("二", "判定整本", "确认抽查页没问题，整本逐段判读，分层从「情节」到「零信息」。")
            StepRow("三", "读或拿走", "阅读器里按字号读，或者导出成 EPUB 带去看书软件里读。")
        }
        VGap(40)
    }
}

@Composable
private fun StepRow(ord: String, title: String, body: String) {
    Row(Modifier.padding(bottom = 14.dp)) {
        Text(
            ord, fontFamily = FontFamily.Serif, fontSize = 18.sp, color = QsMuted,
            modifier = Modifier.width(30.dp),
        )
        Column {
            Text(title, fontSize = 15.sp, color = QsInk)
            VGap(2)
            Text(body, fontSize = 13.sp, lineHeight = 20.sp, color = QsMuted)
        }
    }
}

// ---------- 工作台 ----------

/**
 * 五号卡对着网页端 app/ui.html 排：一 · 选书（含抽查确认）、二 · AI 复核（必须）、
 * 三 · 进度（生成阅读版 + 日志）、四 · 拿走（口味 + 导出 EPUB）、五 · 当前这本。
 * 网页那边还能在卡片五列历史任务，Android 这边只留当前这一本：产物就在 App 私有目录里，
 * 没有多任务库，硬造一个清单反而名不副实。
 */
@Composable
private fun BenchScreen(
    stage: Stage,
    title: String,
    hasSrc: Boolean,
    hasOut: Boolean,
    hasAi: Boolean,
    export: EpubStage,
    options: EpubOptions,
    aiConfig: AiConfig,
    onPick: () -> Unit,
    onBuild: () -> Unit,
    onReader: () -> Unit,
    onOptions: (EpubOptions) -> Unit,
    onExport: () -> Unit,
    onMoreExport: () -> Unit,
    onProvider: (AiProvider) -> Unit,
    onAiTest: ((String) -> Unit) -> Unit,
    onAiReview: () -> Unit,
    onAiSettings: () -> Unit,
) {
    Column(
        Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(horizontal = 20.dp),
    ) {
        Spacer(Modifier.height(10.dp))
        Text("放一本 TXT 或 EPUB 进来，读成深浅。", style = MaterialTheme.typography.headlineMedium, color = QsInk)
        VGap(10)
        Text(
            "第一步先体检 + 抽查前几章，判定合理再出整本。产物落在 App 私有目录，不上传。",
            fontSize = 15.sp, lineHeight = 24.sp, color = QsMuted,
        )
        VGap(20)

        SectionCard("一 · 选书") {
            when (val s = stage) {
                Stage.Picker -> {
                    StatusLine("还没有选文件。")
                    VGap(12)
                    QsButton("点这里选一本 TXT 或 EPUB", onPick, Modifier.fillMaxWidth())
                }
                is Stage.Working -> StatusLine("正在处理：${s.step}")
                is Stage.Confirming -> {
                    StatusLine("当前这本：$title")
                    VGap(8)
                    Text(s.summary, fontSize = 13.sp, lineHeight = 20.sp, color = QsMuted)
                    VGap(14)
                    Row {
                        QsButton("打开抽查页", onReader, Modifier.weight(1f))
                        HGap(10)
                        QsButton("重选一本", onPick, quiet = true)
                    }
                    VGap(10)
                    QsButton("判定没问题，出整本", onBuild, Modifier.fillMaxWidth())
                }
                is Stage.Reading -> {
                    StatusLine(
                        if (s.judged) "当前这本：$title 已经判读完。"
                        else "当前这本：$title 是在阅读器里直接打开的，没走过判定。",
                    )
                    VGap(12)
                    Row {
                        QsButton("去阅读器", onReader, Modifier.weight(1f))
                        HGap(10)
                        QsButton("换一本", onPick, quiet = true)
                    }
                }
                is Stage.Failed -> {
                    StatusLine("出错：", error = true)
                    VGap(6)
                    Text(s.message, fontSize = 13.sp, lineHeight = 20.sp, color = QsErr)
                    VGap(14)
                    QsButton("换一本重来", onPick, Modifier.fillMaxWidth())
                }
            }
        }
        VGap(14)

        SectionCard(
            "二 · AI 复核（必须）",
            "引擎本体是纯本地规则，判不准的那一段模糊带必须交给你的模型复核一遍——出整本时" +
                "会自动跑这一步，三样没配好就不出书。喂给它的判定标准只读自引擎的 references/。" +
                "密钥只存在本机 App 私有目录。",
        ) {
            Text("一键填地址与模型：", fontSize = 13.sp, color = QsMuted)
            AI_PROVIDERS.chunked(2).forEach { row ->
                Row(Modifier.padding(top = 8.dp)) {
                    row.forEach { p ->
                        ChoiceChip(p.label, aiConfig.endpoint == p.endpoint) { onProvider(p) }
                        HGap(8)
                    }
                }
            }
            VGap(14)
            StatusLine("接口：${aiConfig.endpoint}")
            StatusLine("模型：${aiConfig.model} · 密钥：${if (aiConfig.ready) "已填" else "未填（出不了书）"}")
            VGap(12)
            var testing by remember { mutableStateOf(false) }
            var testMsg by remember { mutableStateOf("") }
            Row {
                QsButton(
                    "测试连接",
                    onClick = { testing = true; testMsg = "正在测试…"; onAiTest { testing = false; testMsg = it } },
                    enabled = !testing && aiConfig.ready,
                    quiet = true,
                )
                HGap(10)
                QsButton("设置", onAiSettings, quiet = true)
            }
            VGap(10)
            QsButton("重新复核这本", onAiReview, Modifier.fillMaxWidth(), enabled = hasOut)
            if (testMsg.isNotBlank()) {
                VGap(10)
                StatusLine(testMsg)
            }
        }
        VGap(14)

        SectionCard("三 · 进度") {
            Row(verticalAlignment = Alignment.CenterVertically) {
                QsButton(
                    "生成阅读版",
                    onBuild,
                    enabled = hasSrc && stage !is Stage.Working,
                )
                HGap(12)
                StatusLine(phaseOf(stage))
            }
            VGap(14)
            ProgressLine(
                when {
                    stage is Stage.Working -> null
                    hasOut -> 1f
                    else -> 0f
                },
            )
            VGap(12)
            LogBox(
                (stage as? Stage.Working)?.logs?.joinToString("\n").orEmpty(),
                Modifier.height(180.dp),
            )
        }
        VGap(14)

        SectionCard("四 · 拿走") {
            if (hasOut && !hasAi) {
                StatusLine("判定结果已生成，但 AI 复核还没完整通过，暂不能导出。", error = true)
                VGap(10)
            }
            StatusLine("口味决定整本压多深、退多浅；不确定就先平衡。")
            ChoiceRows(PRESET_CHOICES, options.preset) { onOptions(options.copy(preset = it)) }
            VGap(14)
            Row {
                QsButton("导出 EPUB", onExport, Modifier.weight(1f), enabled = hasOut && hasAi)
                HGap(10)
                QsButton("更多选项", onMoreExport, quiet = true, enabled = hasOut && hasAi)
            }
            if (export is EpubStage.Done) {
                VGap(12)
                StatusLine(export.message)
            }
            if (export is EpubStage.Fail) {
                VGap(12)
                StatusLine(export.message, error = true)
            }
        }
        VGap(14)

        SectionCard("五 · 当前这本") {
            val s = stage
            if (s is Stage.Reading) {
                StatusLine(s.meta)
                if (s.tiers.isNotBlank()) {
                    VGap(6)
                    StatusLine(s.tiers)
                }
                VGap(14)
                QsButton("去阅读器", onReader, Modifier.fillMaxWidth())
            } else {
                StatusLine("还没有判读完的书。")
            }
        }
        VGap(40)
    }
}

/** 进度卡上那句状态，口径对着网页端 ui.html 的 LABEL。 */
private fun phaseOf(s: Stage): String = when (s) {
    Stage.Picker -> "待命中"
    is Stage.Working -> s.step
    is Stage.Confirming -> "等你确认抽查页"
    is Stage.Reading -> "好了"
    is Stage.Failed -> "出错"
}

// ---------- 书架 ----------

/**
 * 对着网页端 app/shelf.html 做：一页列出这台机器上的书，每条给状态与续读位置，
 * 动作有续读（带位置进阅读器）、导出成品 EPUB、改名、删除。清单由 loadShelf 读文件系统算出来。
 */
@Composable
private fun ShelfScreen(
    books: List<ShelfBook>,
    message: String,
    loading: Boolean,
    error: String,
    onReload: () -> Unit,
    onOpen: (ShelfBook) -> Unit,
    onExport: (ShelfBook, String) -> Unit,
    onRename: (ShelfBook) -> Unit,
    onDelete: (ShelfBook) -> Unit,
    onBench: () -> Unit,
) {
    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(horizontal = 20.dp),
    ) {
        Spacer(Modifier.height(10.dp))
        Text("书架", style = MaterialTheme.typography.headlineMedium, color = QsInk)
        VGap(10)
        Text(
            "放在这台机器上的书。未判定的接着读原文，判定过的直接读深浅版。",
            fontSize = 15.sp, lineHeight = 24.sp, color = QsMuted,
        )
        VGap(8)
        StatusLine(message)
        VGap(12)
        if (error.isNotBlank()) {
            Column(Modifier.fillMaxWidth().background(QsSurface).padding(24.dp)) {
                Text(
                    error,
                    fontSize = 14.sp, lineHeight = 22.sp, color = QsErr,
                )
                VGap(14)
                QsButton("重试", onReload, quiet = true)
            }
            VGap(12)
        }
        if (loading && books.isEmpty()) {
            Column(Modifier.fillMaxWidth().background(QsSurface).padding(24.dp)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    CircularProgressIndicator(
                        Modifier.size(18.dp), strokeWidth = 2.dp, color = QsInk,
                    )
                    HGap(12)
                    Text("正在翻书架…", fontSize = 14.sp, color = QsMuted)
                }
            }
            VGap(40)
        } else if (books.isEmpty() && error.isBlank()) {
            Column(Modifier.fillMaxWidth().background(QsSurface).padding(24.dp)) {
                Text(
                    "书架还是空的。去「工作台」放一本进来，或者在「阅读器」里打开一本——" +
                        "打开过的书都会留在这儿。",
                    fontSize = 14.sp, lineHeight = 22.sp, color = QsMuted,
                )
                VGap(14)
                QsButton("去工作台选一本", onBench, Modifier.fillMaxWidth())
            }
            VGap(40)
        } else {
            books.forEach { b ->
                ShelfRow(b, onOpen, onExport, onRename, onDelete)
                VGap(12)
            }
            VGap(28)
        }
    }
}

/** 一条书目：书名（点开就读）、一行元信息、一排动作。对着网页端那条 .row。 */
@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun ShelfRow(
    b: ShelfBook,
    onOpen: (ShelfBook) -> Unit,
    onExport: (ShelfBook, String) -> Unit,
    onRename: (ShelfBook) -> Unit,
    onDelete: (ShelfBook) -> Unit,
) {
    Column(Modifier.fillMaxWidth().background(QsSurface).padding(20.dp)) {
        Text(
            b.name,
            style = MaterialTheme.typography.titleLarge, color = QsInk,
            modifier = Modifier.clickable { onOpen(b) },
        )
        VGap(6)
        Text(shelfMetaLine(b), fontSize = 14.sp, color = QsMuted)
        VGap(14)
        FlowRow(
            horizontalArrangement = Arrangement.spacedBy(8.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            QsButton(
                if (b.state == "source") "继续读" else if (b.seen) "继续读深浅版" else "读深浅版",
                { onOpen(b) }, compact = true,
            )
            b.files.forEach { f ->
                QsButton("导出 $f", { onExport(b, f) }, quiet = true, compact = true)
            }
            QsButton("改名", { onRename(b) }, quiet = true, compact = true)
            QsButton("删除", { onDelete(b) }, quiet = true, compact = true)
        }
    }
}

/** 一条书目的元信息行：类型 · 状态 · 读到 第N章 · NN%（对着网页端 shelf.html 那条）。 */
private fun shelfMetaLine(b: ShelfBook): String {
    val kind = if (b.kind == "epub") "EPUB" else "TXT"
    val state = when (b.state) {
        "source" -> "未判定"
        "judged" -> "已判定"
        else -> "已导出"
    }
    val parts = mutableListOf(kind, state)
    if (b.seen && b.chapter >= 0) {
        val pct = (b.ratio * 100).roundToInt()
        parts.add("读到 第${b.chapter + 1}章" + if (pct > 0) " · ${pct}%" else "")
    }
    if (b.copies > 1) parts.add("已合并 ${b.copies} 份")
    return parts.joinToString(" · ")
}

// ---------- 阅读器 ----------

/**
 * 对着网页端 app/reader.html 做：有书时顶上一行 meta（类型 · 标题 · N 章 · N 字）+ 阅读设置 / 字小 /
 * 字大 / 换个文件，下面整屏 WebView；抽查阶段这里显示的是带标记的抽查页，等用户点头。
 *
 * 「阅读设置」面板就是网页端那块 opts：版式（字体 / 对齐 / 行距 / 段距 / 页宽）加外观（主题 / 亮度），
 * 取值范围与默认值逐项照搬 reader.html。全部由 HtmlPane 刷进 WebView 的样式里，不重载页面。
 */
@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun ReaderScreen(
    stage: Stage,
    title: String,
    textZoom: Int,
    onZoom: (Int) -> Unit,
    prefs: ReadPrefs,
    onPrefs: (ReadPrefs) -> Unit,
    curPage: String,
    curRatio: Double,
    curPageNo: Int,
    curPageCount: Int,
    onPos: (String, Double, Int, Int) -> Unit,
    chromeOn: Boolean,
    onTapBody: () -> Unit,
    onDelete: () -> Unit,
    onAddFile: () -> Unit,
    onBuild: () -> Unit,
    onPick: () -> Unit,
    onToc: () -> Unit,
    onBack: () -> Unit,
    onBench: () -> Unit,
) {
    Column(Modifier.fillMaxSize()) {
        when (stage) {
            is Stage.Confirming -> {
                Column(Modifier.fillMaxWidth().padding(horizontal = 20.dp, vertical = 10.dp)) {
                    Text(
                        "抽查判定 · $title",
                        style = MaterialTheme.typography.titleLarge, color = QsInk,
                    )
                    VGap(6)
                    Text(stage.summary, fontSize = 13.sp, lineHeight = 20.sp, color = QsMuted)
                    VGap(12)
                    Row {
                        QsButton("判定没问题，出整本", onBuild, Modifier.weight(1f))
                        HGap(10)
                        QsButton("重选一本", onPick, quiet = true, compact = true)
                        HGap(8)
                        QsButton("删掉这本", onDelete, quiet = true, compact = true)
                    }
                }
                HtmlPane(
                    entry = stage.html, start = stage.html, startRatio = 0.0,
                    version = 0, textZoom = 100, prefs = prefs, paged = false,
                    onPos = { _, _, _, _ -> },
                    onTap = null,   // 抽查页上一排按钮得一直看得见，不收壳
                    modifier = Modifier.fillMaxWidth().weight(1f),
                )
            }
            is Stage.Reading -> {
                // 章内第几页 / 本章共几页 由 WebView 报上来；全书% 按「本章序 + 章内页比」折算。
                val chapterCount = remember(stage.html.parentFile, stage.version) {
                    chapterPages(stage.html.parentFile).size
                }
                val idx = remember(stage.html.parentFile, stage.version, curPage) {
                    chapterPages(stage.html.parentFile).indexOf(curPage)
                }
                val bookPct = if (chapterCount <= 0 || idx < 0) 0
                else (((idx + curRatio.coerceIn(0.0, 1.0)) / chapterCount) * 100).roundToInt()
                val pageText = if (curPageCount <= 0) "目录" else "$curPageNo/$curPageCount 页"
                // 这一屏落在哪一页：续读就是上次离开的那一章，没记过就是这本的入口页。
                val start = File(stage.html.parentFile, curPage)
                    .takeIf { curPage.isNotBlank() && it.isFile } ?: stage.html
                // 面板默认收起，跟网页端一样：顶栏只挂一行按钮，要用时点开。
                var showLook by remember { mutableStateOf(false) }
                if (chromeOn) {
                    Column(Modifier.fillMaxWidth().padding(horizontal = 20.dp, vertical = 10.dp)) {
                        // meta 单独一行：按钮和它挤一行时，meta 会被压到只剩「TXT ·」。
                        Text(
                            stage.meta.ifBlank { title },
                            fontSize = 13.sp, color = QsMuted, maxLines = 1,
                        )
                        VGap(8)
                        // 五个按钮在窄屏上一行放不下，交给 FlowRow 自己换行。
                        FlowRow(
                            horizontalArrangement = Arrangement.spacedBy(8.dp),
                            verticalArrangement = Arrangement.spacedBy(8.dp),
                        ) {
                            QsButton("目录", onToc, quiet = true, compact = true)
                            QsButton("阅读设置", { showLook = !showLook }, quiet = true, compact = true)
                            QsButton("字小", { onZoom(textZoom - ZOOM_STEP) }, quiet = true, compact = true)
                            QsButton("字大", { onZoom(textZoom + ZOOM_STEP) }, quiet = true, compact = true)
                            QsButton("换个文件", onAddFile, quiet = true, compact = true)
                            QsButton("删掉这本", onDelete, quiet = true, compact = true)
                        }
                        if (showLook) {
                            // 七项设置比一屏还长，限高 + 可滚：别把下面那屏正文挤没。
                            Column(
                                Modifier
                                    .fillMaxWidth()
                                    .heightIn(max = 300.dp)
                                    .verticalScroll(rememberScrollState())
                                    .padding(top = 4.dp),
                            ) {
                                LookChips("主题", READER_THEMES, prefs.theme) { onPrefs(prefs.copy(theme = it)) }
                                LookChips("字体", FONT_CHOICES, prefs.font) { onPrefs(prefs.copy(font = it)) }
                                LookChips("对齐", ALIGN_CHOICES, prefs.align) { onPrefs(prefs.copy(align = it)) }
                                LookSlider("行距 ${fmt2(prefs.lh)}", prefs.lh, LH_MIN..LH_MAX, LH_STEPS) {
                                    onPrefs(prefs.copy(lh = it))
                                }
                                LookSlider("段距 ${fmt2(prefs.gap)}", prefs.gap, GAP_MIN..GAP_MAX, GAP_STEPS) {
                                    onPrefs(prefs.copy(gap = it))
                                }
                                LookSlider(
                                    "页宽 ${prefs.width}em", prefs.width.toFloat(),
                                    WIDTH_MIN.toFloat()..WIDTH_MAX.toFloat(), WIDTH_STEPS,
                                ) { onPrefs(prefs.copy(width = it.roundToInt())) }
                                LookSlider(
                                    "亮度 ${prefs.bright}%", prefs.bright.toFloat(),
                                    BRIGHT_MIN.toFloat()..BRIGHT_MAX.toFloat(), BRIGHT_STEPS,
                                ) { onPrefs(prefs.copy(bright = it.roundToInt())) }
                            }
                        }
                    }
                } else {
                    // 沉浸式：顶上一行「← 书名」，点它回到进阅读器之前那一页；
                    // 其余全靠点中间叫出来。
                    // 这条和底下那条都吃阅读主题的底色，别让沙色把黑页面框起来。
                    Row(
                        Modifier
                            .fillMaxWidth()
                            .background(stripBg(prefs.theme))
                            .clickable { onBack() }
                            .padding(horizontal = 20.dp, vertical = 6.dp),
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        Text("←", fontSize = 15.sp, color = stripFg(prefs.theme))
                        HGap(6)
                        Text(
                            title, fontSize = 13.sp, color = stripFg(prefs.theme),
                            maxLines = 1, modifier = Modifier.weight(1f),
                        )
                        Text(
                            "目录",
                            fontSize = 13.sp, color = stripFg(prefs.theme),
                            modifier = Modifier
                                .clickable { onToc() }
                                .padding(start = 12.dp, top = 4.dp, bottom = 4.dp),
                        )
                    }
                }
                HtmlPane(
                    entry = stage.html, start = start, startRatio = curRatio,
                    version = stage.version, textZoom = textZoom, prefs = prefs, paged = true,
                    onPos = onPos, onTap = onTapBody,
                    modifier = Modifier.fillMaxWidth().weight(1f),
                )
                // 底部那一行进度：左边「章内第几页/共几页」，右边「全书%」。
                Row(
                    Modifier
                        .fillMaxWidth()
                        .background(stripBg(prefs.theme))
                        .padding(horizontal = 20.dp, vertical = 6.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Text(pageText, fontSize = 12.sp, color = stripFg(prefs.theme))
                    Spacer(Modifier.weight(1f))
                    Text("$bookPct%", fontSize = 12.sp, color = stripFg(prefs.theme))
                }
            }
            else -> {
                Column(
                    Modifier
                        .fillMaxSize()
                        .verticalScroll(rememberScrollState())
                        .padding(horizontal = 20.dp),
                ) {
                    Spacer(Modifier.height(40.dp))
                    Text(
                        "手里的书，拿来就读。",
                        style = MaterialTheme.typography.headlineMedium, color = QsInk,
                    )
                    VGap(14)
                    Text(
                        "TXT 或 EPUB 放进来，自动分好章节，就在这一页读完。判定在你自己的手机本地跑，" +
                            "不上传、不改写。",
                        fontSize = 16.sp, lineHeight = 26.sp, color = QsMuted,
                    )
                    VGap(26)
                    // 打开的空档给句话：几 MB 的 EPUB 解章要一两秒，界面不给动静像没反应。
                    when (stage) {
                        is Stage.Working -> {
                            StatusLine(stage.step)
                            VGap(10)
                            ProgressLine(null)
                            VGap(18)
                        }
                        is Stage.Failed -> {
                            Text(stage.message, fontSize = 13.sp, lineHeight = 20.sp, color = QsErr)
                            VGap(18)
                        }
                        else -> Unit
                    }
                    // 就地加一本：这一页选完直接渲染出来读（acceptRead），不经体检 / 抽查 / 判定。
                    QsButton("添加文件", onAddFile, Modifier.fillMaxWidth())
                    VGap(10)
                    Text(
                        "支持 TXT · EPUB。选完直接就能读；要逐段判深浅、出成品，再去工作台那份。",
                        fontSize = 13.sp, color = QsMuted,
                    )
                    VGap(18)
                    QsButton("去工作台选一本", onBench, Modifier.fillMaxWidth(), quiet = true)
                }
            }
        }
    }
}

// ---------- 阅读设置面板里的小件 ----------

/** 一行滑杆设置：左边标签（带当前值），右边滑杆，跟网页端 opts 那一排一个意思。 */
@Composable
private fun LookSlider(
    label: String,
    value: Float,
    range: ClosedFloatingPointRange<Float>,
    steps: Int,
    onChange: (Float) -> Unit,
) {
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = Modifier.fillMaxWidth().padding(top = 10.dp),
    ) {
        Text(label, fontSize = 13.sp, color = QsMuted, modifier = Modifier.width(88.dp))
        Slider(
            value = value, onValueChange = onChange, valueRange = range, steps = steps,
            modifier = Modifier.weight(1f),
        )
    }
}

/** 一行分段设置：左边标签，右边一排胶囊，窄屏放不下就换行。 */
@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun LookChips(
    label: String,
    items: List<Pair<String, String>>,
    selected: String,
    onPick: (String) -> Unit,
) {
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = Modifier.fillMaxWidth().padding(top = 10.dp),
    ) {
        Text(label, fontSize = 13.sp, color = QsMuted, modifier = Modifier.width(64.dp))
        FlowRow(
            horizontalArrangement = Arrangement.spacedBy(8.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
            modifier = Modifier.weight(1f),
        ) {
            items.forEach { (key, text) -> ChoiceChip(text, key == selected) { onPick(key) } }
        }
    }
}

// ---------- 对话框 ----------

/** 口味 / 杠杆 / 面板 / 保留原文。选项直接透传引擎，这里只负责挑。 */
@Composable
private fun ExportPanelDialog(
    options: EpubOptions,
    onOptions: (EpubOptions) -> Unit,
    onConfirm: () -> Unit,
    onDismiss: () -> Unit,
) {
    AlertDialog(
        onDismissRequest = onDismiss,
        containerColor = QsSurface,
        title = { Text("导出 EPUB", style = MaterialTheme.typography.titleLarge) },
        text = {
            Column(Modifier.verticalScroll(rememberScrollState())) {
                GroupLabel("口味")
                ChoiceRows(PRESET_CHOICES, options.preset) { onOptions(options.copy(preset = it)) }
                VGap(10)
                GroupLabel("淡化杠杆")
                ChoiceRows(LEVER_CHOICES, options.lever, perRow = 2) { onOptions(options.copy(lever = it)) }
                VGap(10)
                GroupLabel("系统面板")
                ChoiceRows(PANEL_CHOICES, options.panel, perRow = 2) { onOptions(options.copy(panel = it)) }
                VGap(10)
                GroupLabel("保留原文（这几类不淡化）")
                KEEP_CHOICES.chunked(2).forEach { row ->
                    Row(Modifier.padding(top = 8.dp)) {
                        row.forEach { (key, label) ->
                            ChoiceChip(label, key in options.keeps) {
                                val keeps = options.keeps
                                onOptions(
                                    options.copy(
                                        keeps = if (key in keeps) keeps - key else keeps + key,
                                    ),
                                )
                            }
                            HGap(8)
                        }
                    }
                }
            }
        },
        confirmButton = { QsButton("导出", onConfirm) },
        dismissButton = { QsButton("取消", onDismiss, quiet = true) },
    )
}

@Composable
private fun BusyDialog(title: String, step: String) {
    AlertDialog(
        // 跑着的时候不让点掉：这几步中途撒手只会留下半截结果。
        onDismissRequest = {},
        containerColor = QsSurface,
        title = { Text(title, style = MaterialTheme.typography.titleLarge) },
        text = {
            Row(verticalAlignment = Alignment.CenterVertically) {
                CircularProgressIndicator(Modifier.size(18.dp), strokeWidth = 2.dp, color = QsInk)
                HGap(12)
                Text(step, fontSize = 14.sp, color = QsMuted)
            }
        },
        confirmButton = {},
    )
}

/** 复核进度：总批数来自 Python 侧写的 progress.json，没写出来（还没开始）就先只显示那句话。 */
@Composable
private fun AiProgressDialog(ai: AiStage.Running) {
    AlertDialog(
        onDismissRequest = {},
        containerColor = QsSurface,
        title = { Text("AI 复核", style = MaterialTheme.typography.titleLarge) },
        text = {
            Column {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    CircularProgressIndicator(Modifier.size(18.dp), strokeWidth = 2.dp, color = QsInk)
                    HGap(12)
                    Text(ai.step.ifBlank { "复核中…" }, fontSize = 14.sp, color = QsMuted)
                }
                if (ai.total > 0) {
                    VGap(10)
                    StatusLine("批次 ${ai.done}/${ai.total}")
                }
                VGap(6)
                StatusLine(
                    "已改判 ${ai.reviewed} 段" +
                        if (ai.failed > 0) " · 失败 ${ai.failed} 批（保留规则判定）" else "",
                )
            }
        },
        confirmButton = {},
    )
}

/**
 * AI 接入点设置。默认值就是"不跑"：密钥留空则一切都还在本地。
 * 地址按 OpenAI 兼容约定（服务端会拼 /chat/completions），测试连接不碰任何文件。
 */
@Composable
private fun AiSettingsDialog(
    config: AiConfig,
    onSave: (AiConfig) -> Unit,
    onTest: (AiConfig, (String) -> Unit) -> Unit,
    onDismiss: () -> Unit,
) {
    var endpoint by remember { mutableStateOf(config.endpoint) }
    var model by remember { mutableStateOf(config.model) }
    var key by remember { mutableStateOf(config.key) }
    var testing by remember { mutableStateOf(false) }
    var testMsg by remember { mutableStateOf("") }
    val edited = AiConfig(endpoint.trim(), model.trim(), key.trim())

    AlertDialog(
        onDismissRequest = onDismiss,
        containerColor = QsSurface,
        title = { Text("AI 复核设置", style = MaterialTheme.typography.titleLarge) },
        text = {
            Column(Modifier.verticalScroll(rememberScrollState())) {
                Text(
                    "AI 复核是必须的一层：引擎本体是纯本地规则，判不准的那一段模糊带一定要交给" +
                        "你自己填的模型复核一遍，出整本时会自动跑这一步。三样没配好就不出书。" +
                        "密钥只存在本机 App 私有目录，不进产物、不进日志。",
                    fontSize = 12.sp, lineHeight = 18.sp, color = QsMuted,
                )
                VGap(12)
                QsField("接口地址（填到 /v1 这一层）", endpoint, { endpoint = it })
                VGap(12)
                QsField("模型名", model, { model = it })
                VGap(12)
                QsField("密钥（必填，留空出不了书）", key, { key = it }, placeholder = "sk-…", password = true)
                VGap(14)
                Row(verticalAlignment = Alignment.CenterVertically) {
                    QsButton(
                        "测试连接",
                        onClick = { testing = true; testMsg = "正在测试…"; onTest(edited) { testing = false; testMsg = it } },
                        enabled = !testing && edited.key.isNotBlank(),
                        quiet = true,
                    )
                    if (testing) {
                        HGap(10)
                        CircularProgressIndicator(Modifier.size(16.dp), strokeWidth = 2.dp, color = QsInk)
                    }
                }
                if (testMsg.isNotBlank()) {
                    VGap(10)
                    StatusLine(testMsg)
                }
            }
        },
        confirmButton = { QsButton("保存", { onSave(edited) }) },
        dismissButton = { QsButton("取消", onDismiss, quiet = true) },
    )
}

@Composable
private fun ExportMessageDialog(title: String, message: String, onClose: () -> Unit) {
    AlertDialog(
        onDismissRequest = onClose,
        containerColor = QsSurface,
        title = { Text(title, style = MaterialTheme.typography.titleLarge) },
        text = {
            Text(
                message, fontSize = 13.sp, lineHeight = 20.sp, color = QsMuted,
                modifier = Modifier.verticalScroll(rememberScrollState()),
            )
        },
        confirmButton = { QsButton("好", onClose) },
    )
}

/**
 * 「删掉这本」的确认框。删的是 App 私有目录里那份副本，删了拿不回来，所以先问一句；
 * 用户自己选的原文件不受影响（App 只读过它一份副本）。
 */
@Composable
private fun DeleteBookDialog(title: String, onConfirm: () -> Unit, onDismiss: () -> Unit) {
    AlertDialog(
        onDismissRequest = onDismiss,
        containerColor = QsSurface,
        title = { Text("删掉这本？", style = MaterialTheme.typography.titleLarge) },
        text = {
            Text(
                "删掉《$title》的整份副本？正文、判定结果和导出产物会一起没，删了拿不回来。" +
                    "你选的原文件不受影响。",
                fontSize = 13.sp, lineHeight = 20.sp, color = QsMuted,
            )
        },
        confirmButton = { QsButton("删掉", onConfirm) },
        dismissButton = { QsButton("取消", onDismiss, quiet = true) },
    )
}

/**
 * 书架那条「改名」。只改显示名（name.txt 与 meta），不动目录与产物路径——目录名是 job，
 * 产物地址全靠它，改了会把整本的产物路径全弄断。
 */
@Composable
private fun RenameDialog(
    title: String,
    onConfirm: (String) -> Unit,
    onDismiss: () -> Unit,
) {
    var text by remember { mutableStateOf(title) }
    AlertDialog(
        onDismissRequest = onDismiss,
        containerColor = QsSurface,
        title = { Text("改名", style = MaterialTheme.typography.titleLarge) },
        text = {
            Column {
                Text(
                    "只改显示名，不动文件与目录。",
                    fontSize = 12.sp, lineHeight = 18.sp, color = QsMuted,
                )
                VGap(12)
                QsField("书名", text, { text = it })
            }
        },
        confirmButton = { QsButton("保存", { onConfirm(text) }) },
        dismissButton = { QsButton("取消", onDismiss, quiet = true) },
    )
}

@Composable
private fun GroupLabel(text: String) {
    Text(text, fontSize = 13.sp, color = QsInk)
}

// ---------- 阅读 WebView ----------

/**
 * 引擎渲染出的自包含 HTML 直接喂给 WebView。
 *
 * entry 是这本的入口页（决定 tag，也就是"换书 / 重渲才重载"的判断）；start 是这一屏真正
 * 要落的页——续读时就是上次离开的那一章，没记过就是入口页；startRatio 是章内落点。
 * 这两个只在新建 WebView 那一次用：换页是 WebView 自己的事，Kotlin 侧不跟着重载。
 *
 * tag 编成「url#version | 外观 | 字号」：url#version 变了才 loadUrl；只是外观变了就重刷样式；
 * 字号变了除了重刷还得重量栏宽页数（textZoom 改的是 CSS 视口，栏宽跟着变）。
 * paged=true 的页面走分页阅读（见 pagerJs），false 的（抽查页）就照常滚动。
 */
@Composable
private fun HtmlPane(
    entry: File,
    start: File,
    startRatio: Double,
    version: Int,
    textZoom: Int,
    prefs: ReadPrefs,
    paged: Boolean,
    onPos: (String, Double, Int, Int) -> Unit,
    onTap: (() -> Unit)?,
    modifier: Modifier,
) {
    val url = "file://" + entry.absolutePath
    val look = prefs.key()
    // 同一个文件重渲后 tag 也得变，不然这里认不出是新版、WebView 还停在旧判定上。
    val stamp = "$url#$version|$look|$textZoom"
    AndroidView(
        modifier = modifier,
        factory = { ctx ->
            var restored = false
            // 「上一章退回来、要落在它最后一页」这个意图：分页脚本把它挂在 #qs:last 上，
            // 引擎自己的脚本一 load 就用 replaceState 把 URL 冲干净了，所以只能在
            // onPageStarted（脚本还没跑）这一刻接住。
            var backCross = false
            WebView(ctx).apply {
                settings.javaScriptEnabled = true
                settings.allowFileAccess = true
                settings.domStorageEnabled = true
                settings.textZoom = textZoom
                // 位置上报 + 点正文收放壳都走这个桥（onTap 为 null 就不装点击那一段）。
                addJavascriptInterface(
                    PosBridge(onPos, onTap ?: {}), "QsPos",
                )
                webViewClient = object : WebViewClient() {
                    override fun onPageStarted(view: WebView, pageUrl: String, favicon: android.graphics.Bitmap?) {
                        backCross = pageUrl.contains("qs=last")
                        super.onPageStarted(view, pageUrl, favicon)
                    }

                    override fun onPageFinished(view: WebView, pageUrl: String) {
                        val parts = (view.tag as? String)?.split('|').orEmpty()
                        parts.getOrNull(1)?.let {
                            view.evaluateJavascript(prefsJs(it), null)
                        }
                        // 只有第一屏落回上次读到的位置；后面翻到哪一章都从头。
                        val r = if (restored) 0.0 else startRatio
                        restored = true
                        if (paged) view.evaluateJavascript(pagerJs(r), null)
                        if (backCross) {
                            backCross = false
                            // 上一章：落到它的最后一页
                            view.evaluateJavascript("window.__qsGo && window.__qsGo(1)", null)
                        }
                    }
                }
                tag = stamp
                loadUrl("file://" + start.absolutePath)
            }
        },
        // 重组成对同一个文件不该重载——阅读时翻章是 WebView 自己的事，一重载就弹回目录。
        update = { wv ->
            val old = wv.tag as? String
            if (old == stamp) {
                wv.settings.textZoom = textZoom
            } else {
                val parts = old?.split('|').orEmpty()
                wv.tag = stamp
                wv.settings.textZoom = textZoom
                if (parts.getOrNull(0) != stamp.substringBefore('|')) {
                    // 换书 / 重渲：从入口页重来
                    wv.loadUrl(url)
                } else {
                    // 只是设置变了：重刷样式；字号另要重量栏宽页数
                    wv.evaluateJavascript(prefsJs(look), null)
                    if (parts.getOrNull(2) != textZoom.toString()) {
                        wv.evaluateJavascript("window.__qsRelayout && window.__qsRelayout()", null)
                    }
                }
            }
        },
    )
}

/**
 * 阅读页的桥。脚本侧调 pos.onPos(页名, 页内比例, 章内第几页, 本章共几页) 报位置、
 * pos.onTap() 报「点了中间」。类与方法都得是 public，不然 JS 反射不到。
 * 回调跑在 WebView 的 JS 线程上，不是主线程。
 */
class PosBridge(
    private val sinkPos: (String, Double, Int, Int) -> Unit,
    private val sinkTap: () -> Unit,
) {
    @JavascriptInterface
    fun onPos(page: String, ratio: Double, no: Int, total: Int) = sinkPos(page, ratio, no, total)

    @JavascriptInterface
    fun onTap() = sinkTap()
}

/**
 * 移动端分页阅读：正文不再纵向滚动，改成横向多栏，一栏就是一页。
 *
 * 跟网页端 reader.html 的分页模式是同一套做法：多栏容器高度钉死、栏宽等于一屏、栏数就是页数，
 * 翻页就是把容器横向平移一栏。裁剪必须由 .qs-clip 这一层来做——它正好等于正文栏的盒子；
 * 若把 overflow 直接放在 main 上，裁剪边界是 main 的内边距盒，下一栏的开头会从页边距里漏出来。
 *
 * 一章另起一页：引擎本来就一章一个文件；单文件版（整本一个 html）靠 h1 上的
 * break-before:column 另起一栏。章节名落在它那一页的页首。
 *
 * 手势：单击左 30% 上一页、右 30% 下一页、中间 40% 收放顶上的壳；左滑下一页、右滑上一页。
 * 翻页都是立刻执行，不等待。到了本章头尾就跨章：下一章从它第一页起，上一章落到它的最后一页
 * （这个意图靠查询串 qs=last 带过去，由壳侧在 onPageStarted 抢在引擎改 URL 之前接住）。
 */
private fun pagerJs(restore: Double): String {
    val r = if (restore.isFinite() && restore > 0.0) restore.coerceIn(0.0, 1.0) else 0.0
    return """
(function(){
  var d = document.documentElement, b = document.body;
  var main = b.querySelector('main');
  if (!main) return;

  if (!d.querySelector('.qs-clip')) {
    var st = document.createElement('style');
    st.textContent = '.bar,.stats,.nav,.toolbar{display:none!important}'
      + 'html{overflow:hidden}'
      + 'body{margin:0}'
      + 'main{max-width:none;margin:0;box-sizing:border-box;padding:14px 20px 16px}'
      + '.qs-clip{overflow:hidden}'
      + '.qs-flow{column-gap:0}'
      // 栏距必须是 0，每栏推进才正好是一页。那截"行末多出几像素"的尾巴靠元素自己右侧内缩
      // 兜住：块的可用宽度小了，行提前折，就不会溢进下一栏、在左页边距里露出来。
      + '.qs-flow>*{margin-right:10px}'
      + '.qs-flow>h1{font-size:1.3em;line-height:1.45;font-weight:700;'
      + 'color:var(--t-plot);margin:0 10px .8em 0}'
      + '.qs-flow>h1{break-before:column}'
      + '.qs-flow>h1:first-child{break-before:auto}';
    (document.head || d).appendChild(st);

    var clip = document.createElement('div'); clip.className = 'qs-clip';
    var flow = document.createElement('div'); flow.className = 'qs-flow';
    while (main.firstChild) flow.appendChild(main.firstChild);
    clip.appendChild(flow);
    main.appendChild(clip);
  }

  var clip = d.querySelector('.qs-clip'), flow = d.querySelector('.qs-flow');
  var pageW = 0, pages = 1, page = 0;

  function ratio(){ return pages > 1 ? page / (pages - 1) : 0; }
  function report(){
    try {
      QsPos.onPos(location.pathname.split('/').pop() || '', ratio(), page + 1, pages);
    } catch (e) {}
  }
  function setPage(i){
    page = Math.min(Math.max(0, i), Math.max(0, pages - 1));
    // 用 scrollLeft 定位，让浏览器自己裁：早先试过给 flow 加 translateX，那属于"自己画到框外"，
    // 上一栏会从页边距里漏出来（overflow:hidden 没兜住）。
    clip.scrollLeft = page * pageW;
    report();
  }
  function layout(keep){
    var r = (keep === null || keep === undefined) ? ratio() : keep;
    clip.scrollLeft = 0;
    flow.style.columnWidth = '';
    // 正文栏高度按视口实算，不走百分比链——引擎那份 HTML 上 body{height:100%} 实测会算成 0，
    // 一塌成 0 多栏就全被裁掉，整页空白。
    var cs = getComputedStyle(main);
    var padY = (parseFloat(cs.paddingTop) || 0) + (parseFloat(cs.paddingBottom) || 0);
    var vh = window.innerHeight || d.clientHeight || 600;
    var h = Math.max(120, Math.round(vh - padY));
    clip.style.height = h + 'px';
    flow.style.height = h + 'px';
    pageW = Math.max(160, Math.floor(clip.clientWidth));
    flow.style.columnWidth = pageW + 'px';
    // 页数问裁剪层要：它是那个 scroll 容器，scrollWidth 就是所有栏加起来的总宽。
    pages = Math.max(1, Math.round(clip.scrollWidth / pageW));
    setPage(Math.round(r * (pages - 1)));
  }
  window.__qsRelayout = function(){ layout(); };
  window.__qsGo = function(r){ layout(r); };

  function navTo(dir){
    var l = document.querySelectorAll('.nav .navlink');
    for (var i = 0; i < l.length; i++) {
      var t = l[i].textContent || '';
      if (dir > 0 && t.indexOf('下一章') >= 0) return l[i].getAttribute('href');
      if (dir < 0 && t.indexOf('上一章') >= 0) return l[i].getAttribute('href');
    }
    // 兜底：分章版每章就是一个 ch0001.html 这样的文件，按序号直接推相邻章。
    var m = /^ch(\d{4})\.html$/.exec(location.pathname.split('/').pop() || '');
    if (!m) return null;
    var n = parseInt(m[1], 10) + dir;
    return n >= 1 ? 'ch' + ('0000' + n).slice(-4) + '.html' : null;
  }
  function turn(dir){
    if (dir > 0) {
      if (page + 1 < pages) { setPage(page + 1); return; }
      var nxt = navTo(1); if (nxt) location.href = nxt;
    } else {
      if (page > 0) { setPage(page - 1); return; }
      var prv = navTo(-1);
      // 上一章要落在它的最后一页：意图挂在查询串 qs=last 上带过去，由壳侧在 onPageStarted
      // 抓住（引擎的脚本会用 replaceState 把 URL 冲掉，所以只能抢在它前面接）。
      if (prv) location.href = prv + (prv.indexOf('?') >= 0 ? '&' : '?') + 'qs=last';
    }
  }

  // 手势：单击左 30% 上一页、右 30% 下一页、中间 40% 收放顶上的壳；
  // 左滑下一页、右滑上一页（都是立刻就翻，不再等"是不是双击"）。
  var swipedAt = 0, sx = 0, sy = 0, st = 0, skipSwipe = false;
  var BLOCKED = 'a,button,input,select,textarea,label,.toggle,.panel';
  function blocked(el){ return !!el && !!el.closest && !!el.closest(BLOCKED); }

  // 注册只做一次：同一个文档里 pagerJs 万一被灌第二遍，点一下会翻两页。
  if (!window.__qsTap) {
    window.__qsTap = 1;
    document.addEventListener('click', function(e){
      if (blocked(e.target)) return;
      e.preventDefault();
      e.stopPropagation();
      if (Date.now() - swipedAt < 500) return;   // 刚滑过，这一下不算点击
      var f = e.clientX / (window.innerWidth || 1);
      if (f < 0.3) turn(-1);
      else if (f > 0.7) turn(1);
      else { try { QsPos.onTap(); } catch (err) {} }   // 中间：收放壳
    }, true);

    document.addEventListener('touchstart', function(e){
      var t = e.touches && e.touches[0];
      if (!t) return;
      sx = t.clientX; sy = t.clientY; st = Date.now();
      skipSwipe = blocked(e.target);
    }, {passive: true});

    document.addEventListener('touchend', function(e){
      var t = e.changedTouches && e.changedTouches[0];
      var skip = skipSwipe;
      skipSwipe = false;
      if (!t || skip) return;
      var dx = t.clientX - sx, dy = t.clientY - sy;
      if (Date.now() - st > 700) return;                       // 慢拖不算滑
      if (Math.abs(dx) < 40 || Math.abs(dx) < Math.abs(dy)) return;   // 得是明显横向
      swipedAt = Date.now();
      turn(dx < 0 ? 1 : -1);
    }, {passive: true});

    window.addEventListener('resize', function(){ layout(); });
    if (window.ResizeObserver) new ResizeObserver(function(){ layout(); }).observe(clip);
  }

  // 第一屏落到续读的位置（0 就是从头）；"上一章退回来、落末页"另由壳侧 __qsGo 通知。
  layout($r);
})();
""".trim()
}

/** 分章版那几页：ch0001.html… 按章号排。单文件版（book.html）返回空。 */
private fun chapterPages(dir: File?): List<String> =
    dir?.listFiles().orEmpty()
        .filter { CH_PAGE.matches(it.name) }
        .map { it.name }
        .sorted()

private val CH_PAGE = Regex("""ch\d{4}\.html""")

// ---------- 阅读器外观与版式：配色与样式 ----------

/**
 * 阅读器一整套设置：外观（主题 / 亮度）加版式（字体 / 行距 / 段距 / 页宽 / 对齐）。
 * 默认值与取值范围逐项照搬网页端 reader.html 的「阅读设置」，Android 侧不另立一套。
 * key() 把这些折成一个字符串塞进 WebView 的 tag —— 值一变就重刷样式，不重载页面。
 */
data class ReadPrefs(
    val theme: String = DEFAULT_THEME,
    val bright: Int = DEFAULT_BRIGHT,
    val font: String = DEFAULT_FONT,
    val lh: Float = DEFAULT_LH,
    val gap: Float = DEFAULT_GAP,
    val width: Int = DEFAULT_WIDTH,
    val align: String = DEFAULT_ALIGN,
) {
    fun key() = "$theme:$bright:$font:$lh:$gap:$width:$align"
}

/** 一套阅读配色。取值照搬网页端 reader.html 的主题变量，别在 Android 侧另造一套。 */
private class ReaderLook(
    val surface: String,    // 正文底；网页 --surface，正文就铺在这上面
    val bg: String,         // 页面底；网页 --bg，控制栏那一条用它
    val ink: String,        // 标题与控件文字；网页 --ink
    val muted: String,      // 次要文字；网页 --muted
    val accentInk: String,  // 压在 ink 上的字色；网页 --accent-ink
    val plot: String,       // 情节档墨色；网页 --t-plot
    val mid: String,        // 普通档墨色；网页 --t-mid（正文基准色）
    val weak: String,       // 水档墨色；网页 --t-weak（偏水/注水/零信息合并成这一档）
)

private val READER_THEMES = listOf(
    "light" to "浅色", "dark" to "深色", "green" to "护眼", "invert" to "反色",
)

private val FONT_CHOICES = listOf("song" to "宋体", "hei" to "黑体")
private val ALIGN_CHOICES = listOf("left" to "左对齐", JUSTIFY to "两端")

/** 字体栈对到网页端：--font-serif（宋）与 --font-sans（黑），顺序也照抄。 */
private val FONT_STACKS = mapOf(
    "song" to "\"Noto Serif SC\",\"Songti SC\",SimSun,serif",
    "hei" to "\"Noto Sans SC\",\"PingFang SC\",\"Microsoft YaHei\",sans-serif",
)

/**
 * 四套主题。浅色/护眼/反色逐项对齐 reader.html 的主题变量；**深色改成中性灰阶**
 * （白标题 / 浅灰正文 / 暗灰次要），不再走网页端那套暖炭+奶油——主流阅读器的深色页都是中性灰，
 * 三级灰阶也比暖米色更容易拉开差距。
 */
private val READER_LOOKS = mapOf(
    "light" to ReaderLook("#ddd0b9", "#e6dbc7", "#062b8a", "#31509b", "#e6dbc7", "#111111", "#333333", "#999999"),
    "dark" to ReaderLook("#141516", "#0e0f10", "#f2f2f2", "#9aa0a6", "#141516", "#ffffff", "#cfd2d4", "#6f7479"),
    "green" to ReaderLook("#dbe6d2", "#cfdcc4", "#1e3a26", "#4a6b50", "#eef4e8", "#12200f", "#2c3a28", "#7d8b76"),
    "invert" to ReaderLook("#1f2c42", "#192438", "#f9d475", "#c8ad60", "#192438", "#ffffff", "#d7dce6", "#7e879a"),
)

/** 行距/段距留两位小数，且用点作小数点——本地化若把小数点写成逗号，CSS 会直接崩。 */
private fun fmt2(v: Float) = "%.2f".format(Locale.US, v)

/**
 * 沉浸时顶上/底下那两条的底色与字色，跟着阅读主题走——不然沙色条会把一个黑页面框起来，
 * 比不沉浸还难看。颜色直接取自上面那份配色表，不另存一套。
 */
private fun stripBg(theme: String): Color =
    Color(android.graphics.Color.parseColor(READER_LOOKS[theme]?.bg ?: "#e6dbc7"))

private fun stripFg(theme: String): Color =
    Color(android.graphics.Color.parseColor(READER_LOOKS[theme]?.muted ?: "#31509b"))

/** 把 tag 里那串 "theme:bright:font:lh:gap:width:align" 还原成一套设置。 */
private fun parsePrefs(key: String): ReadPrefs {
    val a = key.split(':')
    val d = ReadPrefs()
    if (a.size < 7) return d
    return ReadPrefs(
        theme = a[0].takeIf { it in READER_LOOKS } ?: d.theme,
        bright = a[1].toIntOrNull()?.coerceIn(BRIGHT_MIN, BRIGHT_MAX) ?: d.bright,
        font = a[2].takeIf { it in FONT_STACKS } ?: d.font,
        lh = a[3].toFloatOrNull()?.coerceIn(LH_MIN, LH_MAX) ?: d.lh,
        gap = a[4].toFloatOrNull()?.coerceIn(GAP_MIN, GAP_MAX) ?: d.gap,
        width = a[5].toIntOrNull()?.coerceIn(WIDTH_MIN, WIDTH_MAX) ?: d.width,
        align = a[6].takeIf { it == "left" || it == JUSTIFY } ?: d.align,
    )
}

/**
 * 把设置编成一段 JS：往文档里塞/更新一条 <style id="qs-look">。
 * 只改样式，不动引擎固有的结构与档位（强度滑杆照旧）。
 *
 * 主题：引擎那份 HTML 的正文底是 --paper、文字色继承 --ink、档位靠 opacity 淡化——但透明度在
 * 小差距上肉眼分不出来，弱档看上去和正文没区别。这里跟网页端 reader.html 对齐成三档实色：
 * 情节档 t-plot、普通档 t-mid、水档（偏水/注水/零信息合并）t-weak，引擎的 opacity 淡化直接归零。
 * 深色主题下这三档反过来是"亮→暗"，重点不会沉底。
 * 版式：字体/行距/段距/页宽/对齐逐项对到网页端阅读页那几条变量（--font-read / --read-lh /
 * --read-gap / --read-width / --read-align），正文首行缩进 2em 也照网页端那一档。
 * 亮度按网页端 .veil 的做法：盖一层纯黑、只调透明度，绝不改字色（改字色就等于偷偷换了主题）。
 */
private fun prefsJs(key: String): String {
    val p = parsePrefs(key)
    val l = READER_LOOKS.getValue(p.theme)
    val font = FONT_STACKS.getValue(p.font)
    val veil = (100 - p.bright.coerceIn(BRIGHT_MIN, BRIGHT_MAX)) / 100.0
    val lh = fmt2(p.lh.coerceIn(LH_MIN, LH_MAX))
    val gap = fmt2(p.gap.coerceIn(GAP_MIN, GAP_MAX))
    val width = p.width.coerceIn(WIDTH_MIN, WIDTH_MAX)
    val align = if (p.align == JUSTIFY) "justify" else "left"
    val line = rgba(l.muted, 0.35)
    val css = """
:root{--paper:${l.surface};--ink:${l.ink};--rule:$line;--accent:${l.ink};
--t-plot:${l.plot};--t-mid:${l.mid};--t-weak:${l.weak};}
body{background:${l.surface};color:${l.ink};}
.bar{background:${l.bg};border-bottom:1px solid $line;}
.bar label,.bar .hint,.stats,.toc .meta,.nav .mid,.toggle{color:${l.muted};}
.stats b{color:${l.ink};}
button{background:${l.bg};border-color:$line;color:${l.ink};}
button[aria-pressed="true"]{background:${l.ink};border-color:${l.ink};color:${l.accentInk};}
input[type=range]{accent-color:${l.ink};}
main{max-width:${width}em;}
/* 三档实色，不用引擎的 opacity 淡化：透明度在小差距上肉眼分不出来（与网页端 reader.html 同一口径）。
   先把引擎按档位调的透明度归零，再按 plot / mid / weak（偏水·注水·零信息合并）三档给实色。 */
.p{opacity:1 !important;font-family:$font;line-height:$lh;text-align:$align;text-indent:2em;margin-bottom:${gap}em;color:${l.mid};}
body[data-emphasis="1"] .p[data-tier="plot"]{color:${l.plot};}
.p[data-tier="weak"],.p[data-tier="fade"],.p[data-tier="zero"]{color:${l.weak};}
.p:hover::after{background:${l.bg};border-color:$line;color:${l.muted};}
.toc a{color:${l.ink};}.toc a:hover{color:${l.ink};}
.nav a{color:${l.ink};}
.panel{border-left-color:$line;}
hr.rule{border-top-color:$line;}
.p.keep[data-tier="zero"]{text-decoration-color:${l.muted};}
body::after{content:"";position:fixed;top:0;right:0;bottom:0;left:0;background:#000;opacity:$veil;pointer-events:none;z-index:9999;}
""".trim()
    return "(function(){var d=document,s=d.getElementById('qs-look');" +
        "if(!s){s=d.createElement('style');s.id='qs-look';(d.head||d.documentElement).appendChild(s);}" +
        "s.textContent=${jsString(css)};})();"
}

/** #rrggbb -> rgba(r,g,b,a)。控制栏、分隔线要半透明，颜色表只存实色。 */
private fun rgba(hex: String, alpha: Double): String {
    val h = hex.removePrefix("#")
    val r = h.substring(0, 2).toInt(16)
    val g = h.substring(2, 4).toInt(16)
    val b = h.substring(4, 6).toInt(16)
    return "rgba($r,$g,$b,$alpha)"
}

/** 把字符串包成一个 JS 字面量。样式里有换行和引号，直接拼会断。 */
private fun jsString(s: String): String = buildString {
    append('"')
    for (c in s) when (c) {
        '"' -> append("\\\"")
        '\\' -> append("\\\\")
        '\n' -> append("\\n")
        '\r' -> append("\\r")
        else -> append(c)
    }
    append('"')
}
