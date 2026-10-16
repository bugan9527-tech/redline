plugins {
    id("com.android.application")
    id("com.chaquo.python")
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
        }
    }
}
