"""生成一个 CSS 样式探针 EPUB，用于实测各家阅读器保留哪些样式手段。

打包 EPUB 时"淡化用灰色还是透明度"（`pack_epub.py --lever`）不该靠猜：
先把这个探针导进目标阅读器，看哪些手段活着，再定杠杆。

用法:
    python make_css_probe.py [输出路径]
默认输出当前目录下的 css-probe.epub

把它导入要用的阅读器（微信读书 / 起点 / 静读天下 / Apple Books / Kindle…），
逐行对照，看哪些行的样式活了下来。
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

MIMETYPE = "application/epub+zip"

CONTAINER = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

OPF = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="BookId">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>CSS 样式探针</dc:title>
    <dc:language>zh-CN</dc:language>
    <dc:identifier id="BookId">urn:uuid:css-probe-0001</dc:identifier>
  </metadata>
  <manifest>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
    <item id="probe" href="probe.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine toc="ncx">
    <itemref idref="probe"/>
  </spine>
</package>
"""

NCX = """<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head/>
  <docTitle><text>CSS 样式探针</text></docTitle>
  <navMap>
    <navPoint id="n1" playOrder="1">
      <navLabel><text>样式探针</text></navLabel>
      <content src="probe.xhtml"/>
    </navPoint>
  </navMap>
</ncx>
"""

PAGE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
  <title>样式探针</title>
  <style type="text/css">
    .c-color { color: #999999; }
    .c-opacity { opacity: 0.35; }
    .c-bold { font-weight: 700; }
  </style>
</head>
<body>

<h2>样式探针</h2>
<p>把这份文件导入你要用的阅读器，逐行往下看。每一行开头的【】里写了它用的是什么手段，只要该行确实变灰 / 变淡 / 变粗 / 变小 / 反白，就说明这个手段在该阅读器里可用。</p>
<p>把生效和失效的行号记下来即可，正文内容本身就是测试文案，不需要理解。</p>

<h3>一、淡化方向</h3>

<p>【01 对照】这一行是基准，不带任何样式。后面每一行都跟它比。</p>
<p class="c-color">【02 class 灰色】color 写在 style 块里，靠 class 选择器生效。</p>
<p style="color:#999999">【03 行内 灰色】color 直接写在行内 style 属性上。</p>
<p class="c-opacity">【04 class 透明】opacity 写在 style 块里，靠 class 选择器生效。</p>
<p style="opacity:0.35">【05 行内 透明】opacity 直接写在行内 style 属性上。</p>
<p><small>【06 small 标签】用 small 标签包起来，靠标签默认样式生效。</small></p>
<p style="font-size:0.75em">【07 行内 小字号】font-size 写在行内 style 属性上。</p>
<p><font color="#999999">【08 font 标签】老式 font color 标签，部分老阅读器仍认。</font></p>

<h3>二、加深方向</h3>

<p>【09 对照】同样是基准行，不带任何样式。</p>
<p><b>【10 b 标签】用 b 标签包起来，靠标签默认样式生效。</b></p>
<p style="font-weight:700">【11 行内 加粗】font-weight 写在行内 style 属性上。</p>
<p class="c-bold">【12 class 加粗】font-weight 写在 style 块里，靠 class 选择器生效。</p>
<p style="background-color:#333333; color:#ffffff">【13 行内 反白】深底白字，靠行内 background-color 生效。</p>

<h3>三、收尾</h3>

<p>如果 02/04 生效而 03/05 失效，说明该阅读器保留了 style 块；反过来则说明它只认行内样式。若整页只有 01 和 09 是正常的、其余全被抹平，说明它做了彻底的样式清洗，那就只能改走内容压缩路线。</p>

</body>
</html>
"""


def build(out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w") as zf:
        # mimetype 必须是第一个条目且不压缩
        info = zipfile.ZipInfo("mimetype")
        info.compress_type = zipfile.ZIP_STORED
        zf.writestr(info, MIMETYPE)

        entries = {
            "META-INF/container.xml": CONTAINER,
            "OEBPS/content.opf": OPF,
            "OEBPS/toc.ncx": NCX,
            "OEBPS/probe.xhtml": PAGE,
        }
        for name, text in entries.items():
            zf.writestr(name, text, compress_type=zipfile.ZIP_DEFLATED)
    return out_path


def main() -> None:
    default = Path.cwd() / "css-probe.epub"
    out = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else default
    build(out)
    print(f"已生成: {out}")


if __name__ == "__main__":
    main()
