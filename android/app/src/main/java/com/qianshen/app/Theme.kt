package com.qianshen.app

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.Typography
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.RectangleShape
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.text.style.TextDecoration
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.em
import androidx.compose.ui.unit.sp

/**
 * 壳层的设计系统：跟网页端 index.html / ui.html / reader.html 用同一套 token ——
 * 羊皮纸底（--bg 与 --surface）+ 单色钴蓝（--ink 兼作强调色，--muted 做次要文字），
 * 衬线做标题、无衬线做正文，按钮一律直角、不加圆角不加阴影。
 *
 * 网页那边 token 写在 :root 里，这里一一对应成常量与 Material3 主题，三页都由它取色。
 * 字体落到 FontFamily.Serif / SansSerif：手机上就是系统自带的中文衬线/无衬线，
 * 不去引 Noto Serif SC 那种网页字体，包体不会被字体拖大。
 */

/** 页面底；网页 --bg。 */
val QsBg = Color(0xFFE6DBC7)

/** 卡片底；网页 --surface。 */
val QsSurface = Color(0xFFDDD0B9)

/** 正文与强调色，同一支钴蓝；网页 --ink 与 --accent。 */
val QsInk = Color(0xFF062B8A)

/** 次要文字；网页 --muted。 */
val QsMuted = Color(0xFF31509B)

/** 压在钴蓝上的字色，就是羊皮纸色本身；网页 --accent-ink。 */
val QsAccentInk = Color(0xFFE6DBC7)

/** 出错文字；网页 .err。 */
val QsErr = Color(0xFF8A1C0F)

/** 文件名与显式二分的页名。三页共用一个顶栏，点它换页。 */
enum class Page(val label: String) {
    Home("首页"),
    Bench("工作台"),
    Reader("阅读器"),
}

private val QsScheme = lightColorScheme(
    primary = QsInk,
    onPrimary = QsAccentInk,
    secondary = QsMuted,
    onSecondary = QsAccentInk,
    background = QsBg,
    onBackground = QsInk,
    surface = QsSurface,
    onSurface = QsInk,
    surfaceVariant = QsBg,
    onSurfaceVariant = QsMuted,
    outline = QsMuted,
    error = QsErr,
    onError = QsAccentInk,
)

/** 字阶对齐网页 --t-lead / --t-head / --t-body / --t-small，标题一律轻字重衬线。 */
private val QsTypography = Typography(
    displaySmall = TextStyle(
        fontFamily = FontFamily.Serif, fontWeight = FontWeight.Light,
        fontSize = 40.sp, lineHeight = 42.sp, letterSpacing = (-0.03).em,
    ),
    headlineMedium = TextStyle(
        fontFamily = FontFamily.Serif, fontWeight = FontWeight.Light,
        fontSize = 28.sp, lineHeight = 30.sp, letterSpacing = (-0.02).em,
    ),
    titleLarge = TextStyle(
        fontFamily = FontFamily.Serif, fontWeight = FontWeight.Light,
        fontSize = 22.sp, lineHeight = 26.sp,
    ),
    titleMedium = TextStyle(
        fontFamily = FontFamily.Serif, fontWeight = FontWeight.Light, fontSize = 18.sp,
    ),
    bodyLarge = TextStyle(fontSize = 16.sp, lineHeight = 24.sp),
    bodyMedium = TextStyle(fontSize = 15.sp, lineHeight = 22.sp),
    bodySmall = TextStyle(fontSize = 13.sp, lineHeight = 20.sp),
    labelLarge = TextStyle(fontSize = 15.sp),
)

@Composable
fun QsTheme(content: @Composable () -> Unit) =
    MaterialTheme(colorScheme = QsScheme, typography = QsTypography, content = content)

// ---------- 三页共用的外壳 ----------

/** 页顶栏：左边字标「深浅」，右边三个页名，当前页带下划线。 */
@Composable
fun AppHeader(current: Page, onNav: (Page) -> Unit) {
    Row(
        modifier = Modifier.fillMaxWidth().padding(start = 20.dp, end = 20.dp, top = 14.dp, bottom = 10.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(
            "深浅",
            fontFamily = FontFamily.Serif, fontWeight = FontWeight.Light,
            fontSize = 20.sp, letterSpacing = 0.06.em, color = QsInk,
            modifier = Modifier.weight(1f),
        )
        PageBar.forEach { p ->
            NavLink(p.label, p == current) { onNav(p) }
        }
    }
}

private val PageBar = listOf(Page.Home, Page.Bench, Page.Reader)

@Composable
private fun NavLink(label: String, on: Boolean, onClick: () -> Unit) {
    Text(
        label,
        fontSize = 15.sp,
        color = if (on) QsInk else QsMuted,
        textDecoration = if (on) TextDecoration.Underline else TextDecoration.None,
        modifier = Modifier.padding(start = 18.dp).clickable(onClick = onClick),
    )
}

/** 一张卡：抬头 + 一句说明 + 内容，底用 --surface，直角、无阴影。 */
@Composable
fun SectionCard(
    title: String,
    note: String = "",
    content: @Composable ColumnScope.() -> Unit,
) {
    Column(Modifier.fillMaxWidth().background(QsSurface).padding(20.dp)) {
        Text(title, style = MaterialTheme.typography.titleLarge, color = QsInk)
        if (note.isNotBlank()) {
            Spacer(Modifier.height(6.dp))
            Text(note, fontSize = 13.sp, lineHeight = 20.sp, color = QsMuted)
        }
        Spacer(Modifier.height(14.dp))
        content()
    }
}

/**
 * 全站只有一个按钮：实心钴蓝 + 羊皮纸字；quiet 就是网页那个 .btn-quiet（底退回 --bg）。
 * 禁用时整体退到 --muted 的半透明，保持直角。
 */
@Composable
fun QsButton(
    text: String,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    enabled: Boolean = true,
    quiet: Boolean = false,
    compact: Boolean = false,
) {
    val bg = when {
        !enabled -> QsMuted.copy(alpha = 0.22f)
        quiet -> QsBg
        else -> QsInk
    }
    val fg = when {
        !enabled -> QsMuted.copy(alpha = 0.55f)
        quiet -> QsInk
        else -> QsAccentInk
    }
    Box(
        modifier
            .height(if (compact) 40.dp else 48.dp)
            .background(bg, RectangleShape)
            .clickable(enabled = enabled, onClick = onClick)
            .padding(horizontal = if (compact) 12.dp else 20.dp),
        contentAlignment = Alignment.Center,
    ) {
        Text(text, fontSize = if (compact) 13.sp else 15.sp, color = fg)
    }
}

/** 选项小块，选中的翻成实心钴蓝；网页那边的 select / .tier.is-on 就是这层意思。 */
@Composable
fun ChoiceChip(text: String, selected: Boolean, onClick: () -> Unit) {
    Text(
        text, fontSize = 13.sp,
        color = if (selected) QsAccentInk else QsInk,
        modifier = Modifier
            .background(if (selected) QsInk else QsBg, RectangleShape)
            .clickable(onClick = onClick)
            .padding(horizontal = 14.dp, vertical = 10.dp),
    )
}

/** 把一串选项按每行几个排开，选中的用 ChoiceChip 标出来。 */
@Composable
fun ChoiceRows(items: List<Pair<String, String>>, selected: String, perRow: Int = 3, onPick: (String) -> Unit) {
    items.chunked(perRow).forEach { row ->
        Row(Modifier.padding(top = 8.dp)) {
            row.forEach { (key, label) ->
                ChoiceChip(label, key == selected) { onPick(key) }
                HGap(8)
            }
        }
    }
}

/** 小标签，给分层计数与人名候选这类附带信息用；网页 .pill。 */
@Composable
fun Pill(text: String) {
    Text(
        text, fontSize = 13.sp, color = QsMuted,
        modifier = Modifier.background(QsBg).padding(horizontal = 12.dp, vertical = 6.dp),
    )
}

/** 一行灰色状态说明；网页 .status / .bar-text / .tiers-line。 */
@Composable
fun StatusLine(text: String, error: Boolean = false, modifier: Modifier = Modifier) {
    Text(
        text.ifBlank { " " }, fontSize = 13.sp, lineHeight = 20.sp,
        color = if (error) QsErr else QsMuted, modifier = modifier,
    )
}

/** 设置项：标签在上一行，输入框用 --bg 填底、没有边框，跟网页 <input> 一个样子。 */
@Composable
fun QsField(
    label: String,
    value: String,
    onValueChange: (String) -> Unit,
    placeholder: String = "",
    password: Boolean = false,
    modifier: Modifier = Modifier,
) {
    Column(modifier) {
        Text(label, fontSize = 13.sp, color = QsMuted)
        Spacer(Modifier.height(6.dp))
        Box(Modifier.fillMaxWidth().background(QsBg).padding(horizontal = 12.dp, vertical = 12.dp)) {
            if (value.isEmpty() && placeholder.isNotEmpty()) {
                Text(placeholder, fontSize = 15.sp, color = QsMuted.copy(alpha = 0.55f))
            }
            BasicTextField(
                value = value,
                onValueChange = onValueChange,
                singleLine = true,
                textStyle = TextStyle(fontSize = 15.sp, color = QsInk),
                cursorBrush = SolidColor(QsInk),
                visualTransformation =
                    if (password) PasswordVisualTransformation() else VisualTransformation.None,
                modifier = Modifier.fillMaxWidth(),
            )
        }
    }
}

/** 进度条：fraction 为空时就是网页 .pbar.wait 那条来回扫的不确定态。 */
@Composable
fun ProgressLine(fraction: Float?, modifier: Modifier = Modifier) {
    val m = modifier.fillMaxWidth().height(6.dp)
    if (fraction == null) {
        LinearProgressIndicator(modifier = m, color = QsInk, trackColor = QsBg)
    } else {
        LinearProgressIndicator(
            progress = { fraction }, modifier = m, color = QsInk, trackColor = QsBg,
        )
    }
}

/** 日志框：--bg 底、等宽留白，跟网页 .log 一样 pre-wrap。 */
@Composable
fun LogBox(text: String, modifier: Modifier = Modifier) {
    Text(
        text.ifBlank { "（还没有开始）" },
        fontSize = 13.sp, lineHeight = 20.sp, color = QsMuted,
        modifier = modifier
            .fillMaxWidth()
            .background(QsBg)
            .padding(14.dp)
            .verticalScroll(rememberScrollState()),
    )
}

/** 页面留白：网页 --margin 在窄屏是 20px，这里照搬。 */
@Composable
fun PageColumn(modifier: Modifier = Modifier, content: @Composable ColumnScope.() -> Unit) {
    Column(modifier.fillMaxWidth().padding(horizontal = 20.dp), content = content)
}

/** 竖直方向的间距快捷写法，省得每处都算 dp。 */
@Composable
fun VGap(h: Int) = Spacer(Modifier.height(h.dp))

/** 水平方向的间距快捷写法。 */
@Composable
fun HGap(w: Int) = Spacer(Modifier.width(w.dp))
