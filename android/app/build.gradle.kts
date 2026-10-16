import org.gradle.api.DefaultTask
import org.gradle.api.file.DirectoryProperty
import org.gradle.api.tasks.InputDirectory
import org.gradle.api.tasks.OutputDirectory
import org.gradle.api.tasks.PathSensitive
import org.gradle.api.tasks.PathSensitivity
import org.gradle.api.tasks.TaskAction
import java.io.File

plugins {
    id("com.android.application")
    id("com.chaquo.python")
    id("org.jetbrains.kotlin.plugin.compose")
}

android {
    namespace = "com.qianshen.app"
    compileSdk = 37

    defaultConfig {
        applicationId = "com.qianshen.app"
        minSdk = 26
        targetSdk = 37
        versionCode = 1
        versionName = "0.2"

        // 引擎是全原生组件，必须显式声明 ABI：arm64-v8a 给真机，x86_64 给模拟器。
        ndk {
            abiFilters += listOf("arm64-v8a", "x86_64")
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }

    buildFeatures {
        compose = true
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

// AGP 9 内建 Kotlin，不再需要 org.jetbrains.kotlin.android 插件；
// kotlin 扩展由 AGP 自己注册，直接配编译器选项即可。
kotlin {
    compilerOptions {
        jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17)
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.16.0")
    // 版本全部与本地 gradle 缓存对齐：ui/foundation/runtime 同属 1.10.4 一档，
    // material3 只有 1.4.0，activity-compose 只有 1.8.2。混版本要靠缓存里现成的那些，
    // 所以 material-ripple / animation 这些被 material3 以低版本声明、缓存里只有 1.10.4 的，
    // 显式提到 1.10.4，免得 Gradle 去解析一个没缓存、又没网可拉的版本。
    implementation("androidx.activity:activity-compose:1.8.2")
    implementation("androidx.compose.runtime:runtime:1.10.4")
    implementation("androidx.compose.ui:ui:1.10.4")
    implementation("androidx.compose.ui:ui-graphics:1.10.4")
    implementation("androidx.compose.foundation:foundation:1.10.4")
    implementation("androidx.compose.animation:animation:1.10.4")
    implementation("androidx.compose.material:material-ripple:1.10.4")
    implementation("androidx.compose.material3:material3:1.4.0")
}

// 引擎 requirement 的自带原生库（libomp / libopenblas / libc++_shared）不归 Chaquopy 管：
// generate<Variant>PythonJniLibs 只往 lib/<abi>/ 放 Python 运行时本身（libpython3.10.so 等），
// pip 产物里那几个 .so 得由壳侧自己搬。漏一个的后果不是报错而是静默降级——
// sklearn 载入 libomp.so 失败后 detect_rules 照跑，但 fade 层整层消失，与桌面版判定对不上。
//
// 所以构建时直接从 pip 产物同步到生成的 jniLibs 目录，仓库里不再手放二进制。
abstract class SyncPyNativeLibs : DefaultTask() {

    @get:InputDirectory
    @get:PathSensitive(PathSensitivity.RELATIVE)
    abstract val pipDir: DirectoryProperty

    @get:OutputDirectory
    abstract val outputDir: DirectoryProperty

    @TaskAction
    fun sync() {
        val out = outputDir.get().asFile
        out.deleteRecursively()
        // pip 产物按 ABI 分层：<variant>/<abi>/chaquopy/lib/*.so。
        // 这里摊平成 jniLibs 认的 <abi>/<name>.so。
        pipDir.get().asFile.listFiles { f: File -> f.isDirectory }.orEmpty().forEach { abiDir ->
            val libs = abiDir.resolve("chaquopy").resolve("lib")
            if (!libs.isDirectory) return@forEach
            val dest = out.resolve(abiDir.name).apply { mkdirs() }
            libs.listFiles { f: File -> f.isFile && f.name.endsWith(".so") }.orEmpty().forEach {
                it.copyTo(dest.resolve(it.name), overwrite = true)
            }
        }
    }
}

androidComponents {
    onVariants { variant ->
        val cap = variant.name.replaceFirstChar { it.uppercase() }
        val syncLibs = tasks.register<SyncPyNativeLibs>("sync${cap}PyNativeLibs") {
            pipDir.set(layout.buildDirectory.dir("python/pip/${variant.name}"))
            outputDir.set(layout.buildDirectory.dir("generated/pyNativeLibs/${variant.name}"))
            // pip 产物得先落盘；显式挂上，不指望 @InputDirectory 的推断。
            dependsOn(tasks.named("install${cap}PythonRequirements"))
        }
        // 用 addGeneratedSourceDirectory 而不是写死 srcDir，让 AGP 自己把
        // 「合并 native 库」这一步排在这个任务之后。
        variant.sources.jniLibs?.addGeneratedSourceDirectory(syncLibs) { it.outputDir }
    }
}

chaquopy {
    defaultConfig {
        version = "3.10"
        // 构建机的 Python 必须与 App 同一次版本，否则 pip 装不了。
        buildPython("C:/Users/PC/AppData/Local/Programs/Python/Python310/python.exe")
        pip {
            // render.py 顶层 from jinja2 import Template —— 运行期硬依赖。
            install("jinja2")
            // detect_rules 的重复段检测靠它；不装会让 fade 层整层消失，与桌面版判定不一致。
            install("scikit-learn==1.3.2")
        }
        // 保留 .py 源码，出问题时堆栈里能看到行号。
        pyc {
            src = false
        }
    }
    sourceSets {
        getByName("main") {
            // 直接引用仓库里那份只读引擎，不做任何拷贝——
            // 这样 scripts/ 与 assets/ 的相对层级原样保留，
            // render.py 里的 __file__.parent.parent/"assets" 才找得到模板。
            srcDir("../../engine/webnovel-focus")
            // AI 复核层（桌面版 app/ai_layer.py）也照原样引用，不拷贝：它是独立一层，
            // 不碰引擎，判定标准仍只读自 references/。桌面 app/ 里其余那些（服务端、
            // 引擎包装、网页）Android 一条都用不上，逐个剔掉，别把它们拖进 APK。
            // 落点与引擎同层（源根），runner 里会把 ai_layer.ENGINE 指到源根。
            srcDir("../../app")
            exclude("__pycache__/**", "data/**", "bookread.py", "engine.py",
                "server.py", "reader.html", "ui.html")
        }
    }
}
