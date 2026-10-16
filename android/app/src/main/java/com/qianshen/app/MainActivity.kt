package com.qianshen.app

import android.app.Activity
import android.os.Bundle
import android.util.Log
import android.widget.ScrollView
import android.widget.TextView
import com.chaquo.python.PyObject
import com.chaquo.python.Python
import java.io.File

/**
 * M1 的验证台，不是正式界面：把仓库里那份只读引擎整个搬进 APK，
 * 进程内跑一次完整流水线，产物落到外部私有目录，再由 adb 取回桌面逐段对判定。
 *
 * 试验用书不进 APK——放 android/testdata/，验证前 adb push 到
 * Android/data/com.qianshen.app/files/book.txt。选书 / 阅读那套正式流程在 M2。
 */
class MainActivity : Activity() {

    private lateinit var output: TextView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        output = TextView(this).apply {
            textSize = 12f
            setPadding(32, 32, 32, 32)
        }
        setContentView(ScrollView(this).apply { addView(output) })
        output.text = "准备中…"
        Thread {
            val text = try {
                runPipeline()
            } catch (e: Throwable) {
                "FAIL\n" + Log.getStackTraceString(e)
            }
            Log.i(TAG, text)
            runOnUiThread { output.text = text }
        }.start()
    }

    private fun runPipeline(): String {
        val root = getExternalFilesDir(null) ?: return "FAIL：拿不到外部私有目录"
        val src = File(root, "book.txt")
        if (!src.isFile) {
            val adbPath = src.absolutePath.replace('\\', '/')
            return "缺书。先推一本过去：\n adb push <书> $adbPath"
        }
        val out = File(root, "out").apply { mkdirs() }
        val runner = Python.getInstance().getModule("runner")
        // 不把 PyObject 转成 Java 集合：Chaquopy 只支持有限的几类转换，dict→Map 会抛异常。
        // 屏幕上摊成 Python repr 看就够，真正要逐段对的是落盘的 analyzed.json。
        val probe = flatten(runner.callAttr("probe"))
        val res = flatten(runner.callAttr("build", src.absolutePath, out.absolutePath))
        return "probe\n$probe\n\nbuild\n$res\n\n产物目录\n${out.absolutePath}"
    }

    private fun flatten(d: PyObject): String =
        d.asMap().entries.joinToString("\n") { "  ${it.key} = ${it.value}" }

    private companion object {
        const val TAG = "qianshen-m1"
    }
}
