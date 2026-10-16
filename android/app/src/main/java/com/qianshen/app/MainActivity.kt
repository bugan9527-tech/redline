package com.qianshen.app

import android.content.Intent
import android.content.pm.ApplicationInfo
import android.net.Uri
import android.os.Bundle
import android.provider.OpenableColumns
import android.util.Log
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.viewinterop.AndroidView
import com.chaquo.python.PyObject
import com.chaquo.python.Python
import java.io.File
import java.util.concurrent.atomic.AtomicBoolean
import org.json.JSONObject

/**
 * 壳层跟网页端同构的三页：首页 / 工作台 / 阅读器。
 *
 *   Home   讲清楚它干什么，两个入口。
 *   Bench  干活的地方：选书 → 抽查 → 出整本 →（可选）AI 复核 → 导出 EPUB。
 *   Reader 读书的地方：抽查页与阅读版都在这儿的 WebView 里，只留字号与换书。
 *
 * 判定这件事整个跑在手机本地（Chaquopy 把仓库里那份只读引擎塞进 APK），所以这里
 * 一条网都不用连。界面按网页端 app/ui.html 的分区排：一 · 选书、二 · AI 复核（可不接）、
 * 三 · 进度、四 · 拿走、五 · 当前这本；阅读器页对着 app/reader.html 做。
 *
 * AI 复核是可选的第二条路：引擎仍是纯本地规则，只有用户自己填了接口地址与密钥，
 * 才会把规则判不准的模糊带交给他的模型复核一遍，再按复核结果重渲。默认关闭。
 *
 * 产物一律落在 filesDir/book/ 下，不碰外部存储：SAF 给的是内容 URI，
 * 不用存储权限，取回来也只是我们自己看的副本。
 */
class MainActivity : ComponentActivity() {

    private var page: Page by mutableStateOf(Page.Home)

    // 工作台自己那台状态机；四个手工阶段各有各的界面，见 Stage。
    private var stage: Stage by mutableStateOf(Stage.Picker)

    // 导出这件事落在工作台的四号卡上，跑起来时压在任意页面上（对话框）。
    private var exportStage: EpubStage by mutableStateOf(EpubStage.Off)
    private var exportOptions: EpubOptions by mutableStateOf(EpubOptions())
    // 按下「导出」那一刻定下的选项；SAF 的选路径界面回来后用它，避免面板又被改一遍。
    private var pendingExport: EpubOptions? = null

    // AI 复核同样是一层可选的：Off 是常态（默认不跑，纯规则）。
    private var aiStage: AiStage by mutableStateOf(AiStage.Off)
    private var aiConfig: AiConfig by mutableStateOf(AiConfig())

    // 阅读器字号：直接喂给 WebView 的 textZoom，网页那边「字小/字大」也是这个意思。
    private var textZoom: Int by mutableStateOf(100)

    private val bookPicker =
        registerForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
            if (uri != null) acceptBook(uri)
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

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        aiConfig = loadAiConfig()
        // 回归入口：只有 debug 包、且 Intent 带了 regress 参数才走这条 headless 判定，
        // 不弹确认页、不等用户点任何东西。android/tools/regress.py 靠它跟桌面逐段对照。
        maybeRegress(intent)
        setContent {
            QsTheme {
                Surface(Modifier.fillMaxSize(), color = QsBg) {
                    // 返回键按页退：阅读器 -> 工作台 -> 首页 -> 退出，别一按就把 App 关了。
                    // 有对话框压着时由对话框自己的窗口先吃掉返回，到不了这里。
                    BackHandler {
                        when (page) {
                            Page.Reader -> goToPage(Page.Bench)
                            Page.Bench -> goToPage(Page.Home)
                            Page.Home -> finish()
                        }
                    }
                    Column(Modifier.fillMaxSize()) {
                        AppHeader(page) { goToPage(it) }
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
                            Page.Reader -> ReaderScreen(
                                stage = stage,
                                title = bookName(),
                                textZoom = textZoom,
                                onZoom = { textZoom = it.coerceIn(ZOOM_MIN, ZOOM_MAX) },
                                onBuild = { startBuild() },
                                onPick = { bookPicker.launch(PICK_MIME) },
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
        val dir = bookDir()
        val out = File(dir, "out")
        goTo(Stage.Working("回归：判定整本…", emptyList()))
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
                goTo(Stage.Reading(reader, "回归 · ${src.name}", 0, metaOf(src.name, doc), tiersOf(doc)))
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
                val dir = bookDir().apply { deleteRecursively(); mkdirs() }
                File(dir, "name.txt").writeText(name)
                val src = File(dir, "src.txt")
                contentResolver.openInputStream(uri).use { input ->
                    requireNotNull(input) { "读不到所选文件" }
                    src.outputStream().use { input.copyTo(it) }
                }
                runDoctor(src)
            } catch (e: Throwable) {
                fail("取书失败", e)
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
        // 抽查页直接落在阅读器页里看，那儿的 WebView 是整屏的、能滚。
        goTo(Stage.Confirming(confirm, summarize(doc)))
        goToPage(Page.Reader)
    }

    // ---------- 整本：后台跑 build，进度回调到界面 ----------

    private fun startBuild() {
        val dir = bookDir()
        val src = File(dir, "src.txt")
        val out = File(dir, "out")
        goToPage(Page.Bench)
        goTo(Stage.Working("开始判定…", emptyList()))
        Thread {
            try {
                val runner = Python.getInstance().getModule("runner")
                // 回调对象交给 Python 调 onStep(text)；它也跑在这条后台线程上，
                // 所以只往里塞数据，回主线程的事交给 appendLog。
                val bridge = ProgressBridge { appendLog(it) }
                val doc = asStringMap(
                    runner.callAttr(
                        "build", src.absolutePath, out.absolutePath,
                        null, 1, false, "auto", true, bridge,
                    ),
                )
                val reader = readerHtml(out)
                requireNotNull(reader) { "阅读版没生成" }
                goTo(
                    Stage.Reading(
                        reader, bookName(), 0,
                        metaOf(bookName(), doc), tiersOf(doc),
                    ),
                )
            } catch (e: Throwable) {
                fail("出阅读版失败", e)
            }
        }.start()
    }

    // ---------- 导出 EPUB：面板 -> SAF 建文件 -> 写字节 ----------

    private fun openExport() = goToExport(EpubStage.Panel)

    private fun closeExport() = goToExport(EpubStage.Off)

    /** 记下这一刻的选项再弹系统文件选择器；落到哪由用户挑。 */
    private fun startExport() {
        pendingExport = exportOptions
        epubSaver.launch(epubFileName())
    }

    private fun writeEpub(uri: Uri, opts: EpubOptions) {
        goToExport(EpubStage.Running("打包 EPUB…"))
        Thread {
            try {
                val out = File(bookDir(), "out")
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
        val base = bookName().removeSuffix(".txt").trim().ifBlank { "book" }
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
     * 复核只碰模糊带：改的是 out/analyzed.json，再按 build 的同一套模式重渲阅读版。
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
                    ),
                )
                stop.set(true)
                poller.join(1000)
                refreshReader(out)
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
        runOnUiThread {
            val cur = stage
            if (cur is Stage.Reading) {
                stage = cur.copy(html = reader, version = cur.version + 1)
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
        val failed = info["failed"]?.toString() ?: "0"
        val note = info["note"]?.toString().orEmpty()
        return buildString {
            if (asked == "0") {
                append("模糊带里没有段落，规则判定原样通过。")
            } else {
                append("模糊带挑出 ").append(asked).append(" 段，改用模型判定 ")
                append(reviewed).append(" 段")
                if (failed != "0") append("（").append(failed).append(" 批失败，保留规则判定）")
            }
            if (note.isNotBlank()) append("\n").append(note)
            append("\n阅读版与导出都会用复核后的判定。")
        }
    }

    // ---------- 杂项 ----------

    private fun bookDir() = File(filesDir, "book")

    private fun bookName(): String = File(bookDir(), "name.txt")
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

    private fun goToPage(p: Page) = runOnUiThread { page = p }

    private fun goToExport(s: EpubStage) = runOnUiThread { exportStage = s }

    private fun goToAi(s: AiStage) = runOnUiThread { aiStage = s }

    private fun fail(prefix: String, e: Throwable) {
        Log.e(TAG, prefix, e)
        goTo(Stage.Failed("$prefix：\n${e.message ?: e.toString()}"))
    }

    private companion object {
        const val TAG = "qianshen"
        // .txt 的 MIME 各家报得不一致，octet-stream 也得放进来，否则选不到书。
        val PICK_MIME = arrayOf("text/plain", "application/octet-stream")

        // AI 接入点存这儿；密钥只在这份 App 私有存储里，不进产物、不进日志。
        const val PREFS = "ai"

        // 模糊带与批量：与桌面版 ai_layer 的默认值一致，改判定口径只在引擎/那一层改。
        const val AI_LO = 0.30
        const val AI_HI = 0.70
        const val AI_MAX = 400
        const val AI_BATCH = 16

        // 回归入口的 Intent extra 名：见 startRegression / android/tools/regress.py。
        const val EXTRA_REGRESS = "regress"
    }
}

// 阅读器字号范围，对应 WebView textZoom 的百分比；两步一档，界面上的「字小/字大」都走它。
private const val ZOOM_MIN = 70
private const val ZOOM_MAX = 200
private const val ZOOM_STEP = 10

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
    data class Reading(
        val html: File,
        val title: String,
        val version: Int = 0,
        val meta: String = "",
        val tiers: String = "",
    ) : Stage
    data class Failed(val message: String) : Stage
}

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
 * AI 复核在界面上的几个态。Off 是常态——默认纯规则出书，一条网都不连；
 * 只有用户自己配了地址/模型/密钥，才会有 Running 这一段。问句（done/total）来自
 * Python 侧写的 progress.json，界面每 400ms 读一次。
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
            "放一本中文网文 TXT 进去。它逐段读完，把情节压深、把注水退浅，深度由你调。" +
                "原文一个字不改，整条流水线都在这台手机本地跑。",
            fontSize = 16.sp, lineHeight = 26.sp, color = QsMuted,
        )
        VGap(28)
        QsButton("进入工作台", onBench, Modifier.fillMaxWidth())
        VGap(12)
        QsButton("打开阅读器", onReader, Modifier.fillMaxWidth(), quiet = true)
        VGap(36)
        SectionCard("它怎么工作") {
            StepRow("一", "选书、体检", "挑一本 TXT，先体检源文件，再抽查前 3 章带标记渲染给你看。")
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
 * 五号卡对着网页端 app/ui.html 排：一 · 选书（含抽查确认）、二 · AI 复核（可不接）、
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
        Text("放一本 TXT 进来，读成深浅。", style = MaterialTheme.typography.headlineMedium, color = QsInk)
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
                    QsButton("点这里选一本 TXT", onPick, Modifier.fillMaxWidth())
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
                    StatusLine("当前这本：$title 已经判读完。")
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
            "二 · AI 复核（可不接）",
            "可选的一层，默认关闭：判定仍全在手机本地跑。填上你自己模型的地址与密钥后，才会把" +
                "规则判不准的模糊带交给它复核一遍。密钥只存在本机 App 私有目录。",
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
            StatusLine("模型：${aiConfig.model} · 密钥：${if (aiConfig.ready) "已填" else "未填（不会跑复核）"}")
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
            QsButton("复核这本的模糊带", onAiReview, Modifier.fillMaxWidth(), enabled = hasOut)
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
            StatusLine("口味决定整本压多深、退多浅；不确定就先平衡。")
            ChoiceRows(PRESET_CHOICES, options.preset) { onOptions(options.copy(preset = it)) }
            VGap(14)
            Row {
                QsButton("导出 EPUB", onExport, Modifier.weight(1f), enabled = hasOut)
                HGap(10)
                QsButton("更多选项", onMoreExport, quiet = true, enabled = hasOut)
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

// ---------- 阅读器 ----------

/**
 * 对着网页端 app/reader.html 做：有书时顶上一行 meta（类型 · 标题 · N 章 · N 字）+ 字小 / 字大 /
 * 换个文件，下面整屏 WebView；抽查阶段这里显示的是带标记的抽查页，等用户点头。
 */
@Composable
private fun ReaderScreen(
    stage: Stage,
    title: String,
    textZoom: Int,
    onZoom: (Int) -> Unit,
    onBuild: () -> Unit,
    onPick: () -> Unit,
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
                        QsButton("重选一本", onPick, quiet = true)
                    }
                }
                HtmlPane(stage.html, 0, 100, Modifier.fillMaxWidth().weight(1f))
            }
            is Stage.Reading -> {
                Row(
                    Modifier.fillMaxWidth().padding(horizontal = 20.dp, vertical = 10.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Text(
                        stage.meta.ifBlank { title },
                        fontSize = 13.sp, color = QsMuted, maxLines = 1,
                        modifier = Modifier.weight(1f),
                    )
                    QsButton("字小", { onZoom(textZoom - ZOOM_STEP) }, quiet = true, compact = true)
                    HGap(8)
                    QsButton("字大", { onZoom(textZoom + ZOOM_STEP) }, quiet = true, compact = true)
                    HGap(8)
                    QsButton("换个文件", onPick, quiet = true, compact = true)
                }
                HtmlPane(stage.html, stage.version, textZoom, Modifier.fillMaxWidth().weight(1f))
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
                        "TXT 放进来，自动分好章节，就在这一页读完。全部在你自己的手机本地跑，" +
                            "不上传、不改写。",
                        fontSize = 16.sp, lineHeight = 26.sp, color = QsMuted,
                    )
                    VGap(26)
                    QsButton("去工作台选一本", onBench, Modifier.fillMaxWidth())
                    VGap(12)
                    QsButton("换个文件", onPick, Modifier.fillMaxWidth(), quiet = true)
                }
            }
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
                    "可选的一层，默认关闭：判定仍全在手机本地跑。填上你自己模型的地址与密钥后，" +
                        "才会把规则判不准的模糊带交给它复核一遍。密钥只存在本机 App 私有目录，" +
                        "不进产物、不进日志。",
                    fontSize = 12.sp, lineHeight = 18.sp, color = QsMuted,
                )
                VGap(12)
                QsField("接口地址（填到 /v1 这一层）", endpoint, { endpoint = it })
                VGap(12)
                QsField("模型名", model, { model = it })
                VGap(12)
                QsField("密钥（留空=不跑复核）", key, { key = it }, placeholder = "sk-…", password = true)
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

@Composable
private fun GroupLabel(text: String) {
    Text(text, fontSize = 13.sp, color = QsInk)
}

// ---------- 阅读 WebView ----------

/**
 * 引擎渲染出的自包含 HTML 直接喂给 WebView。分章版的章间跳转是普通文件链接，不用放行跨源。
 * textZoom 就是阅读器页那两个字号的落点；重渲换了 version 才重载，翻到第几章不会丢。
 */
@Composable
private fun HtmlPane(file: File, version: Int, textZoom: Int, modifier: Modifier) {
    val url = "file://" + file.absolutePath
    // 同一个文件重渲后 tag 也得变，不然这里认不出是新版、WebView 还停在旧判定上。
    val stamp = "$url#$version"
    AndroidView(
        modifier = modifier,
        factory = { ctx ->
            WebView(ctx).apply {
                settings.javaScriptEnabled = true
                settings.allowFileAccess = true
                settings.domStorageEnabled = true
                settings.textZoom = textZoom
                webViewClient = WebViewClient()
                tag = stamp
                loadUrl(url)
            }
        },
        // 重组成对同一个文件不该重载——阅读时翻章是 WebView 自己的事，一重载就弹回目录。
        update = { wv ->
            wv.settings.textZoom = textZoom
            if (wv.tag != stamp) {
                wv.tag = stamp
                wv.loadUrl(url)
            }
        },
    )
}
