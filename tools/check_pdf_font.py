"""docbridge · PDF 中文字体环境自检

部署后跑一次，回答三个问题：

  1. 流水线**实际**会选用哪个中文字体（系统字体还是内嵌兜底 china-s）？
  2. 译文是否**真的画上去了**（在译文应有的区域里数非白像素）？
  3. 译文能否被**复制/搜索**（文本层能否提取出中文）？

第 3 点容易被忽略：PyMuPDF 用内置 `china-s` 字体绘制时，字形画得出来，
但 PDF 里可能没有 ToUnicode 映射，于是**页面上看得见中文、却复制不出来**。
对教材/论文场景这很影响体验，所以单独检出来。

用法：
    cd /opt/docbridge
    .venv/bin/python tools/check_pdf_font.py                    # 用默认字体
    .venv/bin/python tools/check_pdf_font.py /usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fitz  # noqa: E402

from pipelines import pdf_bilingual, pdf_inplace  # noqa: E402

SAMPLE_LINES = [
    "Introduction to Machine Learning",
    "Machine learning is a branch of artificial intelligence that enables "
    "computers to learn from data without being explicitly programmed.",
    "The standard error of the estimate measures the accuracy of predictions.",
]


def make_sample(path: str) -> None:
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    y = 90
    for line in SAMPLE_LINES:
        page.insert_text((60, y), line, fontsize=12)
        y += 40
    doc.save(path)
    doc.close()


def fake_translate(texts, context=None):
    """标记式假翻译：译文字符比较容易辨认。"""
    return ["【译】" + t[:12] for t in texts]


def non_white(pix: fitz.Pixmap, x_from: int, x_to: int) -> int:
    """数一块区域里的非白像素（用灰度采样，避免逐字节比较的噪声）。"""
    count = 0
    for y in range(0, pix.height, 2):
        for x in range(x_from, x_to, 2):
            try:
                if pix.pixel(x, y)[0] < 235:
                    count += 1
            except (IndexError, ValueError):
                pass
    return count


def check(mode: str, font_spec: str | None) -> dict:
    tmp = tempfile.mkdtemp(prefix="fontcheck_")
    src = os.path.join(tmp, "sample.pdf")
    out = os.path.join(tmp, f"out_{mode}.pdf")
    make_sample(src)

    module = pdf_bilingual if mode == "bilingual" else pdf_inplace
    options = {"bilingual_layout": "side"}
    if font_spec:
        options["font"] = font_spec
    result = module.run(src, out, translate=fake_translate, options=options)

    doc = fitz.open(out)
    page = doc[0]
    text = page.get_text()
    # 译文区域：双语版式在右半页
    pix = page.get_pixmap(dpi=110)
    mid = pix.width // 2
    right_ink = non_white(pix, mid, pix.width)
    left_ink = non_white(pix, 0, mid)
    fonts = [f[3] for f in page.get_fonts(full=True)]
    doc.close()

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    return {
        "mode": mode,
        "font": font_spec or "（默认 auto）",
        "extractable": "译" in text,          # 译文能不能被复制出来
        "right_ink": right_ink,               # 译文栏有没有画东西
        "left_ink": left_ink,                 # 原文栏（对照）
        "fonts": fonts,
        "detail": result.get("detail", ""),
    }


def main() -> int:
    font_spec = sys.argv[1] if len(sys.argv) > 1 else None
    print("=" * 70)
    print("PDF 中文字体环境自检")
    print("=" * 70)

    rows = []
    for mode in ("inplace", "bilingual"):
        try:
            rows.append(check(mode, font_spec))
        except Exception as exc:                     # noqa: BLE001
            print(f"  [{mode}] 运行失败：{type(exc).__name__}: {exc}")

    ok = True
    for r in rows:
        print(f"\n── {r['mode']}（字体：{r['font']}）")
        print(f"   译文可提取（能复制/搜索）: {'✅ 是' if r['extractable'] else '❌ 否'}")
        print(f"   译文栏墨迹像素: {r['right_ink']}   原文栏墨迹像素: {r['left_ink']}")
        print(f"   页面字体: {r['fonts']}")
        if r["detail"]:
            print(f"   管线说明: {r['detail'][:120]}")
        if not r["extractable"]:
            ok = False
            print("   ⚠ 译文无法提取：中文画得出来但复制不出来。")
            print("     解决：装中文字体并指定 font 参数，例如")
            print("       apt install fonts-noto-cjk")
            print("       DOCBRIDGE_PDF_FONT=/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc")
        if r["right_ink"] < 30 and r["mode"] == "bilingual":
            ok = False
            print("   ⚠ 译文栏几乎没有墨迹：中文可能根本没画出来！")

    print("\n" + "=" * 70)
    print("结论：" + ("✅ 环境可用（译文可见且可复制）" if ok else "⚠ 见上面提示"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
