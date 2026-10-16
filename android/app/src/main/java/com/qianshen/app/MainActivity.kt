package com.qianshen.app

import android.app.Activity
import android.os.Bundle
import android.util.Log
import android.widget.ScrollView
import android.widget.TextView
import com.chaquo.python.Python
import java.io.File

/**
 * B 方案 spike：把仓库里那本只读引擎整个搬进 APK，进程内跑一次完整流水线，
 * 产物落 filesDir/spike/out，再由 adb pull 回桌面和桌面版逐段对分层。
 *
 * 界面只是把结果摊在屏幕上，正式应用层不这么做（换个 TextView 就行）。
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
        Thread { runSpike() }.start()
    }

    private fun runSpike() {
        val text = try {
            val src = File(filesDir, "spike/book.txt")
            src.parentFile?.mkdirs()
            assets.open("spike_book.txt").use { input ->
                src.outputStream().use { input.copyTo(it) }
            }
            val out = File(filesDir, "spike/out")
            out.mkdirs()
            val res = Python.getInstance().getModule("runner")
                .callAttr("smoke", src.absolutePath, out.absolutePath)
            "OK\n$res\n\n产出目录：${out.absolutePath}"
        } catch (e: Throwable) {
            "FAIL\n" + Log.getStackTraceString(e)
        }
        Log.i(TAG, "spike result: $text")
        runOnUiThread { output.text = text }
    }

    private companion object {
        const val TAG = "qianshen-spike"
    }
}
