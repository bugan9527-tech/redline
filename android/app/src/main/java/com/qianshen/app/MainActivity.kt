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
import androidx.compose.foundation.layout.Arrangement
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
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.FilterChip
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.viewinterop.AndroidView
import com.chaquo.python.PyObject
import com.chaquo.python.Python
import java.io.File
import java.util.concurrent.atomic.AtomicBoolean
import org.json.JSONObject

/**
 * 主流程：选书 → 抽查判定 → 出整本 → 阅读 →（可选）AI 复核 / 导出 EPUB。
 *
 * 判定这件事整个跑在手机本地（Chaquopy 把仓库里那份只读引擎塞进 APK），所以这里
 * 一条网都不用连。界面是个单 Activity 的状态机，四段手工阶段各有自己的界面：
 *
 *   Picker    还没选书
 *   Working  后台在跑（取书 / 体检 / 整本判定），带进度日志
 *   Confirming 抽查页出来等用户点头（引擎强制的步骤，确认了才出整本）
 *   Reading   阅读版 WebView（AI 复核与导出都压在它上面，见 AiStage / EpubStage）
 *
 * AI 复核是可选的第二条路：引擎仍是纯本地规则，只有用户自己填了接口地址与密钥，
 * 才会把规则判不准的模糊带交给他的模型复核一遍，再按复核结果重渲。默认关闭。
 *
 * 产物一律落在 filesDir/book/ 下，不碰外部存储：SAF 给的是内容 URI，
 * 不用存储权限，取回来也只是我们自己看的副本。
 */
class MainActivity : ComponentActivity() {

    private var stage: Stage by mutableStateOf(Stage.Picker)

    // 导出这件事压在阅读页上（对话框），WebView 不重建，翻到第几章就还在第几章。
    private var exportStage: EpubStage by mutableStateOf(EpubStage.Off)
    private var exportOptions: EpubOptions by mutableStateOf(EpubOptions())
    // 按下「导出」那一刻定下的选项；SAF 的选路径界面回来后用它，避免面板又被改一遍。
    private var pendingExport: EpubOptions? = null

    // AI 复核同样压在阅读页上；Off 是常态（默认不跑，纯规则）。设置是全局的，
    // 选书页也要能打开，所以它的对话框挂在状态机外面。
    private var aiStage: AiStage by mutableStateOf(AiStage.Off)
    private var aiConfig: AiConfig by mutableStateOf(AiConfig())

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
            MaterialTheme {
                Surface(
                    modifier = Modifier.fillMaxSize(),
                    color = MaterialTheme.colorScheme.background,
                ) {
                    when (val s = stage) {
                        is Stage.Picker -> PickerScreen(
                            onPick = { bookPicker.launch(PICK_MIME) },
                            onAiSettings = { openAiSettings() },
                        )
                        is Stage.Working -> WorkingScreen(s)
                        is Stage.Confirming -> ConfirmScreen(
                            s, onRead = { startBuild() }, onBack = { reset() },
                        )
                        is Stage.Reading -> ReaderScreen(
                            s, exportStage, exportOptions, aiStage, aiConfig.ready,
                            onOptions = { exportOptions = it },
                            onOpenPanel = { openExport() },
                            onStart = { startExport() },
                            onCloseExport = { closeExport() },
                            onAiReview = { startAiReview() },
                            onAiSettings = { openAiSettings() },
                            onCloseAi = { closeAi() },
                            onBack = { reset() },
                        )
                        is Stage.Failed -> FailScreen(s, onBack = { reset() })
                    }
                    // 设置在选书页和阅读页都要能开，所以挂在状态机外面。
                    if (aiStage == AiStage.Settings) {
                        AiSettingsDialog(
                            aiConfig,
                            onSave = { applyAiConfig(it); closeAi() },
                            onTest = { cfg, report -> probeAi(cfg, report) },
                            onDismiss = { closeAi() },
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

    // ---------- 回归：headless 判定入口（只有 debug 包会走） ----------

    /** 认 regress 参数并起后台判定；发布包（非 debuggable）里这个参数一律不认。 */
    private fun maybeRegress(intent: Intent?) {
        if (!isDebuggable()) return
        intent?.getStringExtra(EXTRA_REGRESS)?.let { startRegression(it) }
    }

    /**
     * `adb shell am start -n com.qianshen.app/.MainActivity --es regress <设备上 txt 路径>`
     * 触发的后台判定：跳过抽查确认，直接在 out/ 落 parsed.json / analyzed.json，跑完写
     * out/regress_done.json 供脚本轮询，界面停在阅读页。判定本身仍是引擎那一份，
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
                val reader = listOf(File(out, "index.html"), File(out, "book.html"))
                    .firstOrNull { it.isFile }
                requireNotNull(reader) { "阅读版没生成" }
                goTo(Stage.Reading(reader, "回归 · ${src.name}"))
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
        goTo(Stage.Confirming(confirm, summarize(doc)))
    }

    // ---------- 整本：后台跑 build，进度回调到界面 ----------

    private fun startBuild() {
        val dir = bookDir()
        val src = File(dir, "src.txt")
        val out = File(dir, "out")
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
                val reader = listOf(File(out, "index.html"), File(out, "book.html"))
                    .firstOrNull { it.isFile }
                requireNotNull(reader) { "阅读版没生成" }
                goTo(Stage.Reading(reader, bookName()))
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
        val reader = listOf(File(out, "index.html"), File(out, "book.html"))
            .firstOrNull { it.isFile } ?: return
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
            append("抽查 ${top["chapters"]} 章 / ${top["chars"]} 字 · 人名候选 ${top["names"]}")
            if (tiers.isNotEmpty()) append("\n分层：$tiers")
            append("\n源文件体检：").append(notes.ifBlank { "通过" })
        }
    }

    private fun appendLog(msg: String) = runOnUiThread {
        val cur = stage
        if (cur is Stage.Working) stage = cur.copy(step = msg, logs = cur.logs + msg)
    }

    // 不能叫 setStage：stage 是 by mutableStateOf 的属性，编译器生成的 setter 正好同名。
    private fun goTo(s: Stage) = runOnUiThread { stage = s }

    private fun goToExport(s: EpubStage) = runOnUiThread { exportStage = s }

    private fun goToAi(s: AiStage) = runOnUiThread { aiStage = s }

    private fun reset() {
        goToExport(EpubStage.Off)
        goToAi(AiStage.Off)
        goTo(Stage.Picker)
    }

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

/**
 * Chaquopy 回调桥。Python 侧调的是 progress.onStep(text)，名字必须对上。
 * 类与方法都得是 public，不然 Python 反射不到。
 */
class ProgressBridge(private val sink: (String) -> Unit) {
    fun onStep(message: String) = sink(message)
}

/** 界面阶段。Working 同时承担「取书 / 体检 / 整本判定」，差别只在日志内容。 */
sealed interface Stage {
    data object Picker : Stage
    data class Working(val step: String, val logs: List<String>) : Stage
    data class Confirming(val html: File, val summary: String) : Stage
    // version 只在 AI 复核重渲后 +1：重渲写的是同一个文件，靠它把 WebView 逼着重载一次。
    data class Reading(val html: File, val title: String, val version: Int = 0) : Stage
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

/** 导出在界面上的几个态。除 Off 外都靠对话框压在阅读页上，WebView 不重建。 */
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
}

private val PRESET_CHOICES = listOf(
    "original" to "原始", "balance" to "平衡", "plotonly" to "只看剧情",
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

// ---------- 界面 ----------

@Composable
private fun PickerScreen(onPick: () -> Unit, onAiSettings: () -> Unit) {
    Column(
        modifier = Modifier.fillMaxSize().padding(28.dp),
        verticalArrangement = Arrangement.Center,
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Text("深浅", fontSize = 34.sp)
        Spacer(Modifier.height(8.dp))
        Text("离线分级阅读 · 判定全在手机本地跑", fontSize = 14.sp)
        Spacer(Modifier.height(28.dp))
        Button(onClick = onPick, modifier = Modifier.fillMaxWidth()) { Text("选择 TXT 书") }
        Spacer(Modifier.height(12.dp))
        Text("选书后先抽查前 3 章给你确认判定，再出整本。", fontSize = 13.sp)
        Spacer(Modifier.height(20.dp))
        // 复核是可选的一层，默认不跑；入口放在这儿，也放在阅读页顶栏。
        TextButton(onClick = onAiSettings) { Text("AI 复核设置（可选）", fontSize = 13.sp) }
    }
}

@Composable
private fun WorkingScreen(s: Stage.Working) {
    Column(Modifier.fillMaxSize().padding(24.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            CircularProgressIndicator(Modifier.size(20.dp), strokeWidth = 2.dp)
            Spacer(Modifier.width(12.dp))
            Text(s.step.ifBlank { "处理中…" }, fontSize = 15.sp)
        }
        Spacer(Modifier.height(16.dp))
        Column(
            Modifier.fillMaxWidth().weight(1f).verticalScroll(rememberScrollState()),
        ) {
            s.logs.forEach {
                Text(it, fontSize = 13.sp, modifier = Modifier.padding(vertical = 3.dp))
            }
        }
    }
}

@Composable
private fun ConfirmScreen(s: Stage.Confirming, onRead: () -> Unit, onBack: () -> Unit) {
    Column(Modifier.fillMaxSize()) {
        Column(Modifier.fillMaxWidth().padding(16.dp)) {
            Text("抽查判定", fontSize = 18.sp)
            Spacer(Modifier.height(6.dp))
            Text(s.summary, fontSize = 13.sp)
            Spacer(Modifier.height(12.dp))
            Row(verticalAlignment = Alignment.CenterVertically) {
                Button(onClick = onRead, modifier = Modifier.weight(1f)) { Text("判定没问题，读整本") }
                Spacer(Modifier.width(12.dp))
                OutlinedButton(onClick = onBack) { Text("重选") }
            }
        }
        HtmlPane(s.html, 0, Modifier.fillMaxWidth().weight(1f))
    }
}

@Composable
private fun ReaderScreen(
    s: Stage.Reading,
    export: EpubStage,
    options: EpubOptions,
    ai: AiStage,
    aiReady: Boolean,
    onOptions: (EpubOptions) -> Unit,
    onOpenPanel: () -> Unit,
    onStart: () -> Unit,
    onCloseExport: () -> Unit,
    onAiReview: () -> Unit,
    onAiSettings: () -> Unit,
    onCloseAi: () -> Unit,
    onBack: () -> Unit,
) {
    // 有对话框压着时，返回键先关对话框，别直接把书丢了。
    BackHandler {
        when {
            export != EpubStage.Off -> onCloseExport()
            ai != AiStage.Off -> onCloseAi()
            else -> onBack()
        }
    }
    Column(Modifier.fillMaxSize()) {
        Column(Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 6.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    s.title, fontSize = 15.sp, maxLines = 1,
                    modifier = Modifier.weight(1f),
                )
                TextButton(onClick = onAiSettings) { Text("AI 设置", fontSize = 13.sp) }
                Spacer(Modifier.width(4.dp))
                OutlinedButton(onClick = onBack) { Text("选书") }
            }
            Spacer(Modifier.height(6.dp))
            Row {
                OutlinedButton(onClick = onAiReview, modifier = Modifier.weight(1f)) {
                    Text(if (aiReady) "AI 复核" else "AI 复核（未配置）", fontSize = 13.sp)
                }
                Spacer(Modifier.width(8.dp))
                OutlinedButton(onClick = onOpenPanel, modifier = Modifier.weight(1f)) {
                    Text("导出 EPUB", fontSize = 13.sp)
                }
            }
        }
        HtmlPane(s.html, s.version, Modifier.fillMaxWidth().weight(1f))
    }

    when (export) {
        is EpubStage.Panel -> ExportPanelDialog(options, onOptions, onStart, onCloseExport)
        is EpubStage.Running -> BusyDialog("导出 EPUB", export.step)
        is EpubStage.Done -> ExportMessageDialog("导出完成", export.message, onCloseExport)
        is EpubStage.Fail -> ExportMessageDialog("导出失败", export.message, onCloseExport)
        EpubStage.Off -> Unit
    }

    when (ai) {
        is AiStage.Running -> AiProgressDialog(ai)
        is AiStage.Done -> ExportMessageDialog("AI 复核完成", ai.message, onCloseAi)
        is AiStage.Fail -> ExportMessageDialog("AI 复核失败", ai.message, onCloseAi)
        AiStage.Off, AiStage.Settings -> Unit
    }
}

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
        title = { Text("导出 EPUB") },
        text = {
            Column(Modifier.verticalScroll(rememberScrollState())) {
                GroupLabel("口味")
                ChipRow(PRESET_CHOICES, options.preset) { onOptions(options.copy(preset = it)) }
                Spacer(Modifier.height(10.dp))
                GroupLabel("淡化杠杆")
                ChipRow(LEVER_CHOICES, options.lever) { onOptions(options.copy(lever = it)) }
                Spacer(Modifier.height(10.dp))
                GroupLabel("系统面板")
                ChipRow(PANEL_CHOICES, options.panel) { onOptions(options.copy(panel = it)) }
                Spacer(Modifier.height(10.dp))
                GroupLabel("保留原文（这几类不淡化）")
                KEEP_CHOICES.chunked(3).forEach { row ->
                    Row {
                        row.forEach { (key, label) ->
                            FilterChip(
                                selected = key in options.keeps,
                                onClick = {
                                    val keeps = options.keeps
                                    onOptions(
                                        options.copy(
                                            keeps = if (key in keeps) keeps - key else keeps + key,
                                        ),
                                    )
                                },
                                label = { Text(label, fontSize = 13.sp) },
                                modifier = Modifier.padding(end = 8.dp, top = 6.dp),
                            )
                        }
                    }
                }
            }
        },
        confirmButton = { Button(onClick = onConfirm) { Text("导出") } },
        dismissButton = { TextButton(onClick = onDismiss) { Text("取消") } },
    )
}

@Composable
private fun BusyDialog(title: String, step: String) {
    AlertDialog(
        // 跑着的时候不让点掉：这几步中途撒手只会留下半截结果。
        onDismissRequest = {},
        title = { Text(title) },
        text = {
            Row(verticalAlignment = Alignment.CenterVertically) {
                CircularProgressIndicator(Modifier.size(20.dp), strokeWidth = 2.dp)
                Spacer(Modifier.width(12.dp))
                Text(step, fontSize = 14.sp)
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
        title = { Text("AI 复核") },
        text = {
            Column {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    CircularProgressIndicator(Modifier.size(20.dp), strokeWidth = 2.dp)
                    Spacer(Modifier.width(12.dp))
                    Text(ai.step.ifBlank { "复核中…" }, fontSize = 14.sp)
                }
                if (ai.total > 0) {
                    Spacer(Modifier.height(10.dp))
                    Text("批次 ${ai.done}/${ai.total}", fontSize = 13.sp)
                }
                Spacer(Modifier.height(6.dp))
                Text(
                    "已改判 ${ai.reviewed} 段" +
                        if (ai.failed > 0) " · 失败 ${ai.failed} 批（保留规则判定）" else "",
                    fontSize = 13.sp,
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
        title = { Text("AI 复核设置") },
        text = {
            Column(Modifier.verticalScroll(rememberScrollState())) {
                Text(
                    "可选的一层，默认关闭：判定仍全在手机本地跑。填上你自己模型的地址与密钥后，" +
                        "才会把规则判不准的模糊带交给它复核一遍。密钥只存在本机 App 私有目录，" +
                        "不进产物、不进日志。",
                    fontSize = 12.sp,
                )
                Spacer(Modifier.height(10.dp))
                OutlinedTextField(
                    value = endpoint, onValueChange = { endpoint = it },
                    label = { Text("接口地址（填到 /v1 这一层）", fontSize = 12.sp) },
                    singleLine = true, modifier = Modifier.fillMaxWidth(),
                )
                Spacer(Modifier.height(8.dp))
                OutlinedTextField(
                    value = model, onValueChange = { model = it },
                    label = { Text("模型名", fontSize = 12.sp) },
                    singleLine = true, modifier = Modifier.fillMaxWidth(),
                )
                Spacer(Modifier.height(8.dp))
                OutlinedTextField(
                    value = key, onValueChange = { key = it },
                    label = { Text("密钥（留空=不跑复核）", fontSize = 12.sp) },
                    singleLine = true, modifier = Modifier.fillMaxWidth(),
                )
                Spacer(Modifier.height(12.dp))
                Row(verticalAlignment = Alignment.CenterVertically) {
                    OutlinedButton(
                        enabled = !testing && edited.key.isNotBlank(),
                        onClick = {
                            testing = true
                            testMsg = "正在测试…"
                            onTest(edited) { testing = false; testMsg = it }
                        },
                    ) { Text("测试连接") }
                    if (testing) {
                        Spacer(Modifier.width(10.dp))
                        CircularProgressIndicator(Modifier.size(16.dp), strokeWidth = 2.dp)
                    }
                }
                if (testMsg.isNotBlank()) {
                    Spacer(Modifier.height(8.dp))
                    Text(testMsg, fontSize = 12.sp)
                }
            }
        },
        confirmButton = { Button(onClick = { onSave(edited) }) { Text("保存") } },
        dismissButton = { TextButton(onClick = onDismiss) { Text("取消") } },
    )
}

@Composable
private fun ExportMessageDialog(title: String, message: String, onClose: () -> Unit) {
    AlertDialog(
        onDismissRequest = onClose,
        title = { Text(title) },
        text = {
            Text(
                message, fontSize = 13.sp,
                modifier = Modifier.verticalScroll(rememberScrollState()),
            )
        },
        confirmButton = { Button(onClick = onClose) { Text("好") } },
    )
}

@Composable
private fun GroupLabel(text: String) {
    Text(text, fontSize = 13.sp, color = MaterialTheme.colorScheme.primary)
}

@Composable
private fun ChipRow(choices: List<Pair<String, String>>, selected: String, onPick: (String) -> Unit) {
    Row {
        choices.forEach { (key, label) ->
            FilterChip(
                selected = key == selected,
                onClick = { onPick(key) },
                label = { Text(label, fontSize = 13.sp) },
                modifier = Modifier.padding(end = 8.dp, top = 6.dp),
            )
        }
    }
}

@Composable
private fun FailScreen(s: Stage.Failed, onBack: () -> Unit) {
    Column(
        modifier = Modifier.fillMaxSize().padding(24.dp),
        verticalArrangement = Arrangement.Center,
    ) {
        Text("出错了", fontSize = 20.sp)
        Spacer(Modifier.height(10.dp))
        Text(
            s.message, fontSize = 13.sp,
            modifier = Modifier.fillMaxWidth().weight(1f, fill = false)
                .verticalScroll(rememberScrollState()),
        )
        Spacer(Modifier.height(16.dp))
        Button(onClick = onBack) { Text("回到选书") }
    }
}

/** 引擎渲染出的自包含 HTML 直接喂给 WebView。分章版的章间跳转是普通文件链接，不用放行跨源。 */
@Composable
private fun HtmlPane(file: File, version: Int, modifier: Modifier) {
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
                webViewClient = WebViewClient()
                tag = stamp
                loadUrl(url)
            }
        },
        // 重组成对同一个文件不该重载——阅读时翻章是 WebView 自己的事，一重载就弹回目录。
        update = { wv ->
            if (wv.tag != stamp) {
                wv.tag = stamp
                wv.loadUrl(url)
            }
        },
    )
}
