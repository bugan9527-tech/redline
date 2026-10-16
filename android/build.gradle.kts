plugins {
    id("com.android.application") version "9.3.0" apply false
    id("com.chaquo.python") version "17.1.0" apply false
    // AGP 9 内建 Kotlin 自己锁定 KGP 2.2.10（见 com.android.tools.build:gradle:9.3.0 的依赖），
    // 所以 Compose 编译器插件必须取同一版本号，不能各写各的。
    id("org.jetbrains.kotlin.plugin.compose") version "2.2.10" apply false
}
