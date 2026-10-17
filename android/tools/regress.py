"""真机 × 桌面逐段对照：验证 Android 壳层没把判定跑漂。

判定这件事整个跑在手机本地，规则永远只有引擎那一份（engine/ 只读）。可壳层每动一次
（换 ABI、改 requirement、加一层 UI 逻辑），都可能无意间让设备端判出跟桌面不一样的结果。
最典型的是那几个原生库漏打包：不报错，只是 sklearn 静默降级、fade 层整层消失，
判定跟桌面悄悄对不上——所以这个脚本把装机前校验也纳了进来。

一条命令走完：

    python android/tools/regress.py                 # 构建 APK + 装机 + 跑 + 逐段对照
    python android/tools/regress.py --skip-build    # 复用已有 APK，只跑对照

步骤：
  1. （可跳过）gradle assembleDebug；随后校验 APK 里 arm64-v8a 三个引擎原生库齐全，
     缺一个就报错退出，不往设备上装。
  2. 装包 -> 把试验书塞进 App 私有目录 -> `am start --es regress` 触发 headless build；
     设备侧判定结果与摘要落在 files/books/regress/out/，脚本轮询 regress_done.json 判跑完。
  3. 桌面侧用同一个 run.py build 出 parsed.json / analyzed.json。
  4. 逐段对照：段落数 / 拼接文本 sha1 / 每段 kind·tier·score / 人名集合。
     一致打对照表退出 0；有漂移打前 20 条差异 + 分层计数对照，退出 1。

产物落在 work/android-regress/（device/ 与 desktop/ 两份，已 gitignore）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "engine" / "webnovel-focus"

# tier 只跟引擎那一份对齐；不在 Android 侧另存一套映射。
sys.path.insert(0, str(ENGINE / "scripts"))
from tiers import tier_of  # noqa: E402

PKG = "com.qianshen.app"
COMPONENT = f"{PKG}/.MainActivity"
APK = ROOT / "android" / "app" / "build" / "outputs" / "apk" / "debug" / "app-debug.apk"
DEFAULT_BOOK = ROOT / "android" / "testdata" / "regression_book.txt"
DEFAULT_SERIAL = "10.228.26.157:41041"
DEFAULT_OUT = ROOT / "work" / "android-regress"

# 装机前的硬门槛：arm64-v8a 三个引擎原生库一个都不能少。
NATIVE_LIBS = ("libomp.so", "libopenblas.so", "libc++_shared.so")

# 设备侧落点。试验书塞进 App 私有目录——/sdcard 在 targetSdk 37 的沙箱里读不到。
DEVICE_TMP = "/data/local/tmp/wnf_regress_book.txt"
PRIVATE_BOOK = "files/regress_book.txt"
DEVICE_BOOK = f"/data/data/{PKG}/{PRIVATE_BOOK}"
PRIVATE_OUT = "files/books/regress/out"

# arm64 与 x86 的浮点尾差容忍；超过这个差才算真漂移。
SCORE_TOL = 0.01
TIERS = ("plot", "mid", "weak", "fade", "zero", "sep")
TIER_LABEL = {"plot": "情节", "mid": "普通", "weak": "偏水",
              "fade": "注水", "zero": "零信息", "sep": "分隔"}


# ---------- 命令与路径 ----------

def run(cmd, *, cwd=None, timeout=180, quiet=False):
    """跑一条外部命令，失败不抛、交给调用方看 returncode。quiet 用于轮询，免得刷屏。"""
    if not quiet:
        print("$ " + " ".join(str(c) for c in cmd), flush=True)
    return subprocess.run(
        [str(c) for c in cmd], cwd=cwd, timeout=timeout,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def _first_file(cands, what: str) -> str:
    for c in cands:
        if c and Path(c).is_file():
            return str(Path(c))
    raise SystemExit(f"找不到 {what}：设对应环境变量，或让它在 PATH 里")


def find_adb() -> str:
    cands = [os.environ.get("ADB"), shutil.which("adb")]
    for key in ("ANDROID_HOME", "ANDROID_SDK_ROOT", "ANDROID_SDK"):
        sdk = os.environ.get(key)
        if sdk:
            cands.append(str(Path(sdk) / "platform-tools" / "adb.exe"))
    cands.append("D:/AndroidStudioSDK/platform-tools/adb.exe")
    return _first_file(cands, "adb")


def find_gradle() -> str:
    cands = [os.environ.get("GRADLE")]
    dists = Path.home() / ".gradle" / "wrapper" / "dists"
    if dists.is_dir():
        # 版本号降序：本机装了多个 gradle 时优先用最新的那个。
        cands += [str(p) for p in
                  sorted(dists.glob("gradle-*/**/bin/gradle.bat"), reverse=True)]
    return _first_file(cands, "gradle")


def find_python() -> str:
    cands = [os.environ.get("WNF_PYTHON"), os.environ.get("PYTHON"),
             "D:/python/python.exe", sys.executable]
    return _first_file(cands, "python（桌面侧跑 run.py 用）")


# ---------- 设备 ----------

class Device:
    """adb 的一层薄封装。每进入一个阶段先 connect：daemon 不跨命令存活。"""

    def __init__(self, adb: str, serial: str) -> None:
        self.adb, self.serial = adb, serial

    def connect(self) -> None:
        r = run([self.adb, "connect", self.serial], timeout=30)
        print("   " + ((r.stdout or r.stderr).strip().replace("\n", " ") or "connected"))

    def must(self, cmd: str, *, timeout=180) -> str:
        r = run([self.adb, "-s", self.serial, "shell", cmd], timeout=timeout)
        if r.returncode != 0:
            raise SystemExit(f"设备命令失败（{cmd}）：\n{(r.stderr or r.stdout).strip()}")
        return r.stdout

    def push_book(self, local: Path) -> None:
        """试验书先丢 /data/local/tmp，再让 run-as 拷进 App 私有目录。"""
        r = run([self.adb, "-s", self.serial, "push", str(local), DEVICE_TMP], timeout=120)
        if r.returncode != 0:
            raise SystemExit(f"push 试验书失败：\n{(r.stderr or r.stdout).strip()}")
        self.must(f"run-as {PKG} mkdir -p files")
        self.must(f"run-as {PKG} cp {DEVICE_TMP} {PRIVATE_BOOK}")

    def fetch(self, rel: str, dest: Path) -> bool:
        """取 App 私有文件：run-as cat 到 /data/local/tmp（重定向由外层 shell 执行），再 pull。"""
        tmp = "/data/local/tmp/wnf_regress_pull"
        run([self.adb, "-s", self.serial, "shell", f"rm -f {tmp}"], timeout=30, quiet=True)
        r = run([self.adb, "-s", self.serial, "shell",
                 f"run-as {PKG} cat {rel} > {tmp}"], timeout=180, quiet=True)
        if r.returncode != 0:
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        r2 = run([self.adb, "-s", self.serial, "pull", tmp, str(dest)],
                 timeout=180, quiet=True)
        return r2.returncode == 0 and dest.is_file() and dest.stat().st_size > 0


def check_apk(apk: Path) -> list[str]:
    """返回 APK 里缺的 arm64-v8a 原生库；空列表=齐全。"""
    if not apk.is_file():
        raise SystemExit(f"没有找到 APK：{apk}\n先构建（去掉 --skip-build），或检查 gradle 输出路径")
    with zipfile.ZipFile(apk) as z:
        names = set(z.namelist())
    return [lib for lib in NATIVE_LIBS if f"lib/arm64-v8a/{lib}" not in names]


# ---------- 对照 ----------

def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def flat(doc: dict) -> list[dict]:
    return [p for ch in doc["chapters"] for p in ch["paragraphs"]]


def joined(doc: dict) -> str:
    return "\n".join(p["text"] for p in flat(doc))


def width(s: str) -> int:
    return sum(2 if ord(c) > 0x2E7F else 1 for c in s)


def pad(s: str, w: int) -> str:
    return s + " " * max(0, w - width(s))


def pid_key(pid: str) -> tuple[int, int]:
    m = re.match(r"c(\d+)p(\d+)$", pid)
    return (int(m.group(1)), int(m.group(2))) if m else (10 ** 9, 0)


def tally(doc: dict) -> dict[str, int]:
    t: dict[str, int] = {}
    for p in flat(doc):
        k = tier_of(p["kind"], p["score"] or 0.0)
        t[k] = t.get(k, 0) + 1
    return t


def compare(dev_parsed: dict, dev_an: dict, desk_parsed: dict, desk_an: dict,
            show: int = 20) -> tuple[bool, list[str]]:
    """逐段对照，返回 (是否一致, 报告行)。引擎只读，这里不引入任何第二套规则。"""
    lines: list[str] = []
    diffs: list[str] = []

    dch, vch = len(desk_parsed["chapters"]), len(dev_parsed["chapters"])
    dp, vp = flat(desk_parsed), flat(dev_parsed)
    dsha, vsha = sha1(joined(desk_parsed)), sha1(joined(dev_parsed))
    dn, vn = set(desk_an.get("names", [])), set(dev_an.get("names", []))

    def mark(ok: bool) -> str:
        return "✓" if ok else "✗"

    lines.append(f"章数          {pad('桌面 ' + str(dch), 18)}"
                 f"{pad('设备 ' + str(vch), 18)}{mark(dch == vch)}")
    lines.append(f"段落数        {pad('桌面 ' + str(len(dp)), 18)}"
                 f"{pad('设备 ' + str(len(vp)), 18)}{mark(len(dp) == len(vp))}")
    lines.append(f"拼接文本 sha1 {pad('桌面 ' + dsha[:10], 18)}"
                 f"{pad('设备 ' + vsha[:10], 18)}{mark(dsha == vsha)}")
    lines.append(f"人名集合      {pad('桌面 ' + str(len(dn)), 18)}"
                 f"{pad('设备 ' + str(len(vn)), 18)}{mark(dn == vn)}")

    if dch != vch:
        diffs.append(f"章数不一致：桌面 {dch} / 设备 {vch}")
    if dsha != vsha:
        diffs.append("拼接文本 sha1 不一致——解析出的正文本身就不一样，先查 parse 那一层")
    if dn != vn:
        diffs.append(f"人名集合不一致：桌面独有 {sorted(dn - vn)} / 设备独有 {sorted(vn - dn)}")

    dmap = {p["pid"]: p for p in flat(desk_an)}
    vmap = {p["pid"]: p for p in flat(dev_an)}
    for pid in sorted(dmap.keys() | vmap.keys(), key=pid_key):
        a, b = dmap.get(pid), vmap.get(pid)
        if a is None or b is None:
            diffs.append(f"{pid}  只有{'设备' if a is None else '桌面'}侧有这一段")
            continue
        if a["kind"] != b["kind"]:
            diffs.append(f"{pid}  kind   桌面 {a['kind']}  设备 {b['kind']}")
        ta, tb = tier_of(a["kind"], a["score"] or 0.0), tier_of(b["kind"], b["score"] or 0.0)
        if ta != tb:
            diffs.append(f"{pid}  tier   桌面 {ta}  设备 {tb}")
        if a["score"] is not None and b["score"] is not None:
            delta = abs(a["score"] - b["score"])
            if delta > SCORE_TOL:
                diffs.append(f"{pid}  score  桌面 {a['score']:.3f}  设备 {b['score']:.3f}"
                             f"  Δ{delta:.3f}")

    dt, vt = tally(desk_an), tally(dev_an)
    lines.append("")
    lines.append("分层计数      " + pad("桌面", 10) + pad("设备", 10) + "差")
    for t in TIERS:
        x, y = dt.get(t, 0), vt.get(t, 0)
        if x == 0 and y == 0:
            continue
        tail = "0 ✓" if x == y else f"{y - x:+d} ✗"
        lines.append(f"  {pad(TIER_LABEL[t], 8)}{pad(str(x), 10)}{pad(str(y), 10)}{tail}")

    ok = not diffs
    if diffs:
        lines.append("")
        lines.append(f"逐段差异 {len(diffs)} 条（显示前 {min(show, len(diffs))}）：")
        lines += ["  " + d for d in diffs[:show]]

    return ok, lines


# ---------- 主流程 ----------

def main() -> int:
    ap = argparse.ArgumentParser(prog="regress.py", description="真机 × 桌面逐段判定对照")
    ap.add_argument("--serial", default=DEFAULT_SERIAL, help="adb 设备号（host:port 也行）")
    ap.add_argument("--book", default=str(DEFAULT_BOOK), help="桌面侧试验书，也会 push 到设备")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="对照产物目录（默认 work/android-regress）")
    ap.add_argument("--skip-build", action="store_true", help="复用已有 APK，不重新构建")
    ap.add_argument("--adb", default=None, help="adb 路径（默认按 ADB/PATH/ANDROID_HOME 找）")
    ap.add_argument("--gradle", default=None, help="gradle 路径（默认按 GRADLE/本机 dists 找）")
    ap.add_argument("--desktop-python", default=None, help="桌面侧跑 run.py 的 python")
    ap.add_argument("--timeout", type=int, default=300, help="等设备侧跑完的秒数上限")
    args = ap.parse_args()

    book = Path(args.book).resolve()
    if not book.is_file():
        print(f"找不到试验书：{book}", file=sys.stderr)
        return 2
    out = Path(args.out).resolve()

    adb = args.adb or find_adb()
    dev = Device(adb, args.serial)

    if not args.skip_build:
        gradle = args.gradle or find_gradle()
        r = run([gradle, "-p", str(ROOT / "android"), "assembleDebug", "--offline"],
                timeout=600)
        if r.returncode != 0:
            print((r.stdout or "")[-3000:])
            print((r.stderr or "")[-3000:], file=sys.stderr)
            print("构建 APK 失败", file=sys.stderr)
            return 2

    missing = check_apk(APK)
    if missing:
        print(f"APK 缺 arm64-v8a 原生库 {missing}——sklearn 会静默降级、fade 层整层消失，"
              f"判定必然对不上。先修 build.gradle.kts 里的 SyncPyNativeLibs 再回来。",
              file=sys.stderr)
        return 2
    print(f"APK 原生库齐全：{'、'.join(NATIVE_LIBS)}")

    dev.connect()
    r = run([adb, "-s", args.serial, "install", "-r", str(APK)], timeout=300)
    if r.returncode != 0:
        print((r.stderr or r.stdout).strip(), file=sys.stderr)
        print("装包失败", file=sys.stderr)
        return 2

    dev.push_book(book)
    # 先清掉上一轮的完成标记，否则轮询会立刻拿到旧结果，等于没跑。
    dev.must(f"run-as {PKG} rm -f {PRIVATE_OUT}/regress_done.json")
    # -S 先强杀、--activity-clear-task 清掉壳里可能残留的界面（比如上一次没关的选书器），
    # 否则再造一个 Activity 才会走 onCreate，regress 参数会被投递给旧实例。
    dev.must(f"am start -S --activity-clear-task -n {COMPONENT} --es regress {DEVICE_BOOK}")
    print(f"已触发 headless 判定（{DEVICE_BOOK}）")

    dev_out = out / "device"
    done_rel = f"{PRIVATE_OUT}/regress_done.json"
    deadline = time.time() + args.timeout
    print("等设备侧跑完", end="", flush=True)
    while time.time() < deadline:
        if dev.fetch(done_rel, dev_out / "regress_done.json"):
            print(" 好了")
            break
        print(".", end="", flush=True)
        time.sleep(2)
    else:
        print()
        print(f"等设备侧跑完超时（{args.timeout}s）。检查设备上 App 是否崩在 Chaquopy 初始化，"
              f"或把 --timeout 调大。", file=sys.stderr)
        return 3

    for name in ("parsed.json", "analyzed.json"):
        if not dev.fetch(f"{PRIVATE_OUT}/{name}", dev_out / name):
            print(f"设备侧 {name} 没取到", file=sys.stderr)
            return 3

    desk_out = out / "desktop"
    desk_out.mkdir(parents=True, exist_ok=True)
    py = args.desktop_python or find_python()
    r = run([py, str(ENGINE / "scripts" / "run.py"), "build", str(book),
             "--skip-check", "-o", str(desk_out)], timeout=600)
    if r.returncode != 0:
        print((r.stdout or "")[-3000:])
        print((r.stderr or "")[-3000:], file=sys.stderr)
        print("桌面侧 build 失败", file=sys.stderr)
        return 2

    summary = load_json(dev_out / "regress_done.json")
    dev_parsed = load_json(dev_out / "parsed.json")
    dev_an = load_json(dev_out / "analyzed.json")
    desk_parsed = load_json(desk_out / "parsed.json")
    desk_an = load_json(desk_out / "analyzed.json")

    print(f"\n设备侧摘要：{summary.get('chapters')} 章 / {summary.get('chars')} 字 / "
          f"人名 {summary.get('names')} · 分层 {summary.get('tiers')}")
    print(f"\n=== 设备 × 桌面 逐段对照（{book.name}）===")
    ok, lines = compare(dev_parsed, dev_an, desk_parsed, desk_an)
    print("\n".join(lines))
    print(f"\n产物：{dev_out}  |  {desk_out}")
    if ok:
        print("✓ 设备端判定与桌面逐段一致，壳层没把判定跑漂")
        return 0
    print("✗ 检测到判定漂移，见上方差异")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
