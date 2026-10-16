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
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
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
 * M2 主流程：选书 → 抽查判定 → 出整本 → 阅读。
 *
 * 判定这件事整个跑在手机本地（Chaquopy 把仓库里那份只读引擎塞进 APK），所以这里
 * 一条网都不用连。界面是个单 Activity 的状态机，四段手工阶段各有自己的界面：
 *
 *   Picker    还没选书
 *   Working  后台在跑（取书 / 体检 / 整本判定），带进度日志
 *   Confirming 抽查页出来等用户点头（引擎强制的步骤，确认了才出整本）
 *   Reading   阅读版 WebView
 *
 * 产物一律落在 filesDir/book/ 下，不碰外部存储：SAF 给的是内容 URI，
 * 不用存储权限，取回来也只是我们自己看的副本。
 */
class MainActivity : ComponentActivity() {

    private var stage: Stage by mutableStateOf(Stage.Picker)

    private val bookPicker =
        registerForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
            if (uri != null) acceptBook(uri)
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
                        is Stage.Reading -> ReaderScreen(s, onBack = { reset() })
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

    private fun reset() = goTo(Stage.Picker)

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
private fun ReaderScreen(s: Stage.Reading, onBack: () -> Unit) {
    BackHandler { onBack() }
    Column(Modifier.fillMaxSize()) {
        Row(
            modifier = Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 6.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(
                s.title, fontSize = 15.sp, maxLines = 1,
                modifier = Modifier.weight(1f),
            )
            OutlinedButton(onClick = onBack) { Text("选书") }
        }
        HtmlPane(s.html, Modifier.fillMaxWidth().weight(1f))
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
