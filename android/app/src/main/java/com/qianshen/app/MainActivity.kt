package com.qianshen.app

import android.content.Intent
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
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.viewinterop.AndroidView
import com.chaquo.python.PyObject
import com.chaquo.python.Python
import java.io.File

/**
 * 主流程：选书 → 抽查判定 → 出整本 → 阅读 →（可选）导出 EPUB。
 *
 * 判定这件事整个跑在手机本地（Chaquopy 把仓库里那份只读引擎塞进 APK），所以这里
 * 一条网都不用连。界面是个单 Activity 的状态机，四段手工阶段各有自己的界面：
 *
 *   Picker    还没选书
 *   Working  后台在跑（取书 / 体检 / 整本判定），带进度日志
 *   Confirming 抽查页出来等用户点头（引擎强制的步骤，确认了才出整本）
 *   Reading   阅读版 WebView（导出是在它上面压一层对话框，见 EpubStage）
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
        setContent {
            MaterialTheme {
                Surface(
                    modifier = Modifier.fillMaxSize(),
                    color = MaterialTheme.colorScheme.background,
                ) {
                    when (val s = stage) {
                        is Stage.Picker -> PickerScreen(onPick = { bookPicker.launch(PICK_MIME) })
                        is Stage.Working -> WorkingScreen(s)
                        is Stage.Confirming -> ConfirmScreen(
                            s, onRead = { startBuild() }, onBack = { reset() },
                        )
                        is Stage.Reading -> ReaderScreen(
                            s, exportStage, exportOptions,
                            onOptions = { exportOptions = it },
                            onOpenPanel = { openExport() },
                            onStart = { startExport() },
                            onCloseExport = { closeExport() },
                            onBack = { reset() },
                        )
                        is Stage.Failed -> FailScreen(s, onBack = { reset() })
                    }
                }
            }
        }
    }

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

    private fun reset() {
        goToExport(EpubStage.Off)
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
    data class Reading(val html: File, val title: String) : Stage
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
private fun PickerScreen(onPick: () -> Unit) {
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
        HtmlPane(s.html, Modifier.fillMaxWidth().weight(1f))
    }
}

@Composable
private fun ReaderScreen(
    s: Stage.Reading,
    export: EpubStage,
    options: EpubOptions,
    onOptions: (EpubOptions) -> Unit,
    onOpenPanel: () -> Unit,
    onStart: () -> Unit,
    onCloseExport: () -> Unit,
    onBack: () -> Unit,
) {
    // 导出对话框开着时，返回键先关对话框，别直接把书丢了。
    BackHandler { if (export == EpubStage.Off) onBack() else onCloseExport() }
    Column(Modifier.fillMaxSize()) {
        Row(
            modifier = Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 6.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(
                s.title, fontSize = 15.sp, maxLines = 1,
                modifier = Modifier.weight(1f),
            )
            OutlinedButton(onClick = onOpenPanel) { Text("导出 EPUB") }
            Spacer(Modifier.width(8.dp))
            OutlinedButton(onClick = onBack) { Text("选书") }
        }
        HtmlPane(s.html, Modifier.fillMaxWidth().weight(1f))
    }

    when (export) {
        is EpubStage.Panel -> ExportPanelDialog(options, onOptions, onStart, onCloseExport)
        is EpubStage.Running -> ExportProgressDialog(export.step)
        is EpubStage.Done -> ExportMessageDialog("导出完成", export.message, onCloseExport)
        is EpubStage.Fail -> ExportMessageDialog("导出失败", export.message, onCloseExport)
        EpubStage.Off -> Unit
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
private fun ExportProgressDialog(step: String) {
    AlertDialog(
        // 打包中不让点掉：这是本地几秒的事，中途撒手只会留下半截文件。
        onDismissRequest = {},
        title = { Text("导出 EPUB") },
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
private fun HtmlPane(file: File, modifier: Modifier) {
    val url = "file://" + file.absolutePath
    AndroidView(
        modifier = modifier,
        factory = { ctx ->
            WebView(ctx).apply {
                settings.javaScriptEnabled = true
                settings.allowFileAccess = true
                settings.domStorageEnabled = true
                webViewClient = WebViewClient()
                tag = url
                loadUrl(url)
            }
        },
        // 重组成对同一个文件不该重载——阅读时翻章是 WebView 自己的事，一重载就弹回目录。
        update = { wv ->
            if (wv.tag != url) {
                wv.tag = url
                wv.loadUrl(url)
            }
        },
    )
}
