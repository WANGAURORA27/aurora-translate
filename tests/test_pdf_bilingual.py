# -*- coding: utf-8 -*-
"""离线单元测试：``pipelines/pdf_bilingual.py``（PDF 双语对照）。

严禁联网：所有测试都用本地假翻译函数，样例 PDF 用 PyMuPDF 现场生成
（写进 ``tests/_tmp/``，不使用工作目录里的教材 PDF）。

运行方式（两种都行）::

    cd <仓库目录>/docbridge
    python3 -m pytest tests/test_pdf_bilingual.py -q
    python3 tests/test_pdf_bilingual.py
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
import traceback

import fitz

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                       # .../docbridge
MODULE_PATH = os.path.join(ROOT, "pipelines", "pdf_bilingual.py")
# 自己的子目录：同一 _tmp 下还有别人（docx / inplace）的测试产物，别互相踩
TMP = os.path.join(HERE, "_tmp", "pdf_bilingual")
SRC = os.path.join(TMP, "sample_src.pdf")

SAMPLE_PAGES = 3


# ---------------------------------------------------------------- 载入被测模块

def _load_pipeline():
    if not os.path.isfile(MODULE_PATH):
        raise RuntimeError(f"被测模块不存在：{MODULE_PATH}")
    spec = importlib.util.spec_from_file_location("pdf_bilingual_under_test",
                                                 MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pb = _load_pipeline()
os.makedirs(TMP, exist_ok=True)


# ---------------------------------------------------------------- 样例 PDF

def build_sample():
    """现场生成 3 页样例：标题/正文/表格/图片/纯图页。"""
    os.makedirs(TMP, exist_ok=True)
    doc = fitz.open()

    # ---- 第 1 页：粗体标题 + 正文 + 一张小图 + 一行中文（应计入 skipped）
    p1 = doc.new_page(width=595, height=842)
    p1.insert_text((60, 80), "Chapter 1 Introduction to Markets", fontsize=17,
                   fontname="hebo")
    body = [
        "This chapter covers the fundamentals of microeconomics.",
        "We begin with the concept of supply and demand, which",
        "explains how prices are determined in a competitive market.",
        "The equilibrium quantity is reached when the quantity",
        "demanded equals the quantity supplied at a given price.",
    ]
    for i, line in enumerate(body):
        p1.insert_text((60, 130 + i * 20), line, fontsize=11)
    box = fitz.Rect(60, 260, 250, 350)
    p1.draw_rect(box, color=(0.2, 0.3, 0.7), width=1.2)
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 64, 48))
    pix.clear_with(170)
    p1.insert_image(box + (5, 5, -5, -5), pixmap=pix)
    p1.insert_text((60, 380), "图 1 市场均衡", fontsize=11, fontname="china-s")
    p1.insert_text((60, 400), "12.5", fontsize=11)          # 纯数字 -> skipped

    # ---- 第 2 页：标题 + 正文 + 一个 3x3 的表格
    p2 = doc.new_page(width=595, height=842)
    p2.insert_text((60, 90), "Chapter 2 Elasticity", fontsize=15, fontname="hebo")
    for i, line in enumerate([
        "Price elasticity measures the responsiveness of demand.",
        "A one percent price change leads to a change in quantity.",
    ]):
        p2.insert_text((60, 130 + i * 20), line, fontsize=11)
    cell_w, cell_h, x0, y0 = 130.0, 26.0, 60.0, 200.0
    cells = [["Good", "Price", "Quantity"],
             ["Apples", "3.50", "120"],
             ["Bread", "2.00", "300"]]
    for r, row in enumerate(cells):
        for c, val in enumerate(row):
            rect = fitz.Rect(x0 + c * cell_w, y0 + r * cell_h,
                             x0 + (c + 1) * cell_w, y0 + (r + 1) * cell_h)
            p2.draw_rect(rect, color=(0.35, 0.35, 0.35), width=0.8)
            p2.insert_text((rect.x0 + 6, rect.y0 + 17), val, fontsize=10)

    # ---- 第 3 页：纯图页（没有任何文本）-> 译文区应给灰色提示
    p3 = doc.new_page(width=595, height=842)
    p3.draw_rect(fitz.Rect(50, 400, 545, 760), color=(0.4, 0.4, 0.4), width=1)
    pix2 = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 200, 200))
    pix2.clear_with(90)
    p3.insert_image(fitz.Rect(150, 240, 450, 640), pixmap=pix2)

    doc.save(SRC)
    doc.close()
    return SRC


def _norm(text):
    """去掉所有空白，避免排版换行把特征串切开。"""
    return re.sub(r"\s+", "", text or "")


def _dark_pixels(pix, x0, y0, x1, y1, step=2):
    """统计区域内非白像素数（证明那一半真的画了内容）。"""
    n = pix.n
    w = pix.width
    s = pix.samples
    cnt = 0
    for y in range(y0, min(y1, pix.height), step):
        row = y * w
        for x in range(x0, min(x1, pix.width), step):
            i = (row + x) * n
            if s[i] < 240 or s[i + 1] < 240 or s[i + 2] < 240:
                cnt += 1
    return cnt


class FakeTranslator:
    """假翻译：记录调用，返回带特征串的译文。"""

    def __init__(self, drop=0):
        self.calls = []
        self.drop = drop

    def __call__(self, texts, ctx=None):
        assert isinstance(texts, list), "translate 必须收到 list"
        assert all(isinstance(t, str) for t in texts)
        self.calls.append((list(texts), ctx))
        out = [f"【译{i}】{t}" for i, t in enumerate(texts)]
        if self.drop:
            return out[:len(out) - self.drop]
        return out

    @property
    def batch_sizes(self):
        return [len(t) for t, _c in self.calls]


class Recorder:
    """进度回调记录器。"""

    def __init__(self):
        self.events = []

    def __call__(self, done, total, note=""):
        self.events.append((done, total, note))

    @property
    def last(self):
        return self.events[-1] if self.events else None


def _run(layout, translate=None, out_name=None, out_path=None):
    src = build_sample()
    out = out_path or os.path.join(TMP, out_name or f"out_{layout}.pdf")
    trans = translate or FakeTranslator()
    prog = Recorder()
    stats = pb.run(src, out, translate=trans, progress=prog,
                   options={"bilingual_layout": layout})
    return out, trans, prog, stats


# ================================================================ 测试

def test_metadata_contract():
    """契约元数据 + 可选参数。"""
    assert pb.FORMAT == "pdf"
    assert pb.MODE == "bilingual"
    assert isinstance(pb.LABEL, str) and pb.LABEL
    assert isinstance(pb.NOTE, str) and pb.NOTE
    assert "bilingual_layout" in pb.OPTIONS
    assert callable(pb.run)


def test_side_layout():
    """左右并排：1:1 页数、横向尺寸、译文上纸、原文矢量嵌入、进度到位。"""
    out, trans, prog, stats = _run("side")
    assert os.path.isfile(out), "输出文件不存在"
    assert not os.path.exists(out + ".part"), "残留 .part 文件"

    doc = fitz.open(out)
    try:
        assert len(doc) == SAMPLE_PAGES, f"页数应 1:1，实际 {len(doc)}"
        for page in doc:
            assert page.rect.width > page.rect.height, "side 应为横向"
            assert abs(page.rect.width - 842) < 1, page.rect
            assert abs(page.rect.height - 595) < 1, page.rect

        # 原文确实被矢量嵌入（Form XObject，不是截图）
        xobjs = sum(len(p.get_xobjects()) for p in doc)
        assert xobjs > 0, "输出页里没有 XObject，原文没被嵌入"
        assert os.path.getsize(out) > 8000, "输出体积过小，原文可能没嵌进去"

        # 左半页真的画了原文（渲染后非白像素）
        pix = doc[0].get_pixmap(dpi=60)
        ink_left = _dark_pixels(pix, 0, 0, pix.width // 2, pix.height)
        assert ink_left > 200, f"左半页几乎空白（非白像素 {ink_left}）"

        # 译文真的画上去了（特征串来自假翻译函数）
        page1 = _norm(doc[0].get_text())
        assert "【译0】" in page1, f"第 1 页找不到译文特征串：{page1[:200]}"
        whole = _norm("".join(p.get_text() for p in doc))
        assert "【译" in whole
        assert "Chapter1Introduction" in whole, "原文文本应可从 XObject 读回"

        # 纯图页的灰色提示
        page3 = _norm(doc[2].get_text())
        assert "（本页无可翻译文本）" in page3, f"纯图页缺提示：{page3[:120]}"
    finally:
        doc.close()

    # 统计字段
    for key in ("units", "chars_in", "chars_out", "skipped", "pages", "detail"):
        assert key in stats, f"返回 dict 缺 {key}"
    assert stats["pages"] == SAMPLE_PAGES
    assert stats["units"] >= 5, stats
    assert stats["chars_in"] > 0 and stats["chars_out"] > 0
    assert stats["skipped"] >= 2, f"中文行与纯数字行应计入 skipped：{stats}"
    assert isinstance(stats["detail"], str) and stats["detail"]

    # 攒批：不能一条一次调用
    assert trans.calls, "translate 没被调用"
    assert all(len(t) <= pb.BATCH_MAX for t, _c in trans.calls)
    assert max(trans.batch_sizes) > 1, f"疑似逐条调用：{trans.batch_sizes}"
    ctx = trans.calls[0][1]
    assert isinstance(ctx, dict) and ctx.get("kind") == "pdf", ctx
    assert "page" in ctx or "pages" in ctx, ctx

    # 进度：被调用过、单调不减、最后 done == total
    assert prog.events, "进度回调没被调用"
    total = prog.events[0][1]
    assert total == SAMPLE_PAGES
    dones = [e[0] for e in prog.events]
    assert dones == sorted(dones), f"进度非单调：{dones}"
    assert prog.last[0] == prog.last[1], f"最后一次未完成：{prog.last}"


def test_stack_layout():
    """上下堆叠：页面更高、原文在上半页、译文在下面。"""
    out, trans, prog, stats = _run("stack")
    assert os.path.isfile(out)
    doc = fitz.open(out)
    try:
        assert len(doc) == SAMPLE_PAGES
        for page in doc:
            assert page.rect.height > page.rect.width, "stack 应为纵向"
            assert page.rect.height > 842, "stack 页高应高于原页（更高）"
            assert abs(page.rect.width - 595) < 1, page.rect
        pix = doc[0].get_pixmap(dpi=60)
        top_ink = _dark_pixels(pix, 0, 0, pix.width, pix.height // 3)
        assert top_ink > 200, f"上半页几乎空白（非白像素 {top_ink}）"
        assert "【译0】" in _norm(doc[0].get_text())
        assert "（本页无可翻译文本）" in _norm(doc[2].get_text())
    finally:
        doc.close()
    assert stats["layout"] == "stack"
    assert stats["pages"] == SAMPLE_PAGES
    assert prog.last[0] == prog.last[1] == SAMPLE_PAGES


def test_count_mismatch_raises():
    """translate 少返回一条 -> RuntimeError；且不留下半成品。"""
    out = os.path.join(TMP, "out_mismatch.pdf")
    with open(out, "wb") as f:
        f.write(b"OLD-CONTENT")           # 原子落盘：失败不得顶掉同名文件
    trans = FakeTranslator(drop=1)
    prog = Recorder()
    try:
        pb.run(build_sample(), out, translate=trans, progress=prog,
               options={"bilingual_layout": "side"})
    except RuntimeError as exc:
        assert "翻译返回数量不符" in str(exc), str(exc)
    else:
        raise AssertionError("少返回一条时必须抛 RuntimeError")
    assert open(out, "rb").read() == b"OLD-CONTENT", "失败时不该动同名旧文件"
    assert not os.path.exists(out + ".part"), "失败后残留 .part"


def test_bad_input_raises_runtimeerror():
    """失败要说话：源文件不存在时抛中文 RuntimeError。"""
    try:
        pb.run(os.path.join(TMP, "no_such_file.pdf"),
               os.path.join(TMP, "x.pdf"), translate=FakeTranslator())
    except RuntimeError as exc:
        assert "不存在" in str(exc), str(exc)
    else:
        raise AssertionError("源文件不存在时应抛 RuntimeError")


def test_options_are_optional_and_unknown_keys_ignored():
    """options=None / 未知键 / 非法 layout 都要有合理默认，不报错。"""
    out = os.path.join(TMP, "out_defaults.pdf")
    trans = FakeTranslator()
    stats = pb.run(build_sample(), out, translate=trans, progress=None,
                   options={"unknown_key": 1, "bilingual_layout": "nonsense"})
    doc = fitz.open(out)
    try:
        assert stats["layout"] == "side", "非法 layout 应回落到 side"
        assert len(doc) == SAMPLE_PAGES
        assert doc[0].rect.width > doc[0].rect.height
    finally:
        doc.close()


def test_batching_accumulates_across_pages():
    """攒批：跨页凑满一批（30~100 条），绝不一条一次调用。"""
    os.makedirs(TMP, exist_ok=True)
    src = os.path.join(TMP, "many_lite.pdf")
    doc = fitz.open()
    for p in range(2):
        page = doc.new_page(width=595, height=842)
        page.insert_text((60, 70), f"Section {p + 1} Overview", fontsize=16,
                         fontname="hebo")
        for i in range(17):
            page.insert_text((60, 110 + i * 24),
                             f"Line number {i} of page {p + 1} in English.",
                             fontsize=11)
    doc.save(src)
    doc.close()

    trans = FakeTranslator()
    stats = pb.run(src, os.path.join(TMP, "many_lite_out.pdf"), translate=trans,
                   options={"bilingual_layout": "side"})
    assert stats["units"] >= 34, stats
    assert len(trans.calls) == 1, f"应该跨页攒成一批：{trans.batch_sizes}"
    size = trans.batch_sizes[0]
    assert size == stats["units"], (size, stats)
    assert 30 <= size <= pb.BATCH_MAX, f"批大小应落在 30~100：{size}"
    ctx = trans.calls[0][1]
    assert ctx["kind"] == "pdf" and ctx["count"] == size
    assert ctx["page"] == 1 and ctx["pages"] == [1, 2], ctx


def test_callback_exception_bubbles():
    """契约第 4 条：translate 抛的异常原样冒泡，不被包装。"""
    class Boom(Exception):
        pass

    def boom(texts, ctx=None):
        raise Boom("配额用完")

    out = os.path.join(TMP, "out_boom.pdf")
    try:
        pb.run(build_sample(), out, translate=boom,
               options={"bilingual_layout": "side"})
    except Boom as exc:
        assert "配额用完" in str(exc)
    else:
        raise AssertionError("回调异常必须冒泡")
    assert not os.path.exists(out), "失败时不该产出文件"
    assert not os.path.exists(out + ".part"), "失败后残留 .part"


def test_rotated_page_keeps_content_inside_original_half():
    """源页带 /Rotate（show_pdf_page 不跟随 /Rotate）：不崩、不裁、计数上报。"""
    os.makedirs(TMP, exist_ok=True)
    src = os.path.join(TMP, "rotated.pdf")
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    for i in range(5):
        page.insert_text((60, 100 + i * 30),
                         f"Rotated line {i} with some English text.", fontsize=13)
    page.set_rotation(90)
    doc.save(src)
    doc.close()

    out = os.path.join(TMP, "rotated_out.pdf")
    stats = pb.run(src, out, translate=FakeTranslator(),
                   options={"bilingual_layout": "side"})
    assert stats["pages"] == 1
    assert stats["rotated_pages"] == 1, stats
    assert "旋转" in stats["detail"], stats["detail"]
    doc = fitz.open(out)
    try:
        assert len(doc) == 1 and doc[0].rect.width > doc[0].rect.height
        pix = doc[0].get_pixmap(dpi=72)
        ink = _dark_pixels(pix, 30, 45, 411, 565)
        assert ink > 100, f"旋转页的原文没嵌进原文那一半（非白像素 {ink}）"
        assert "【译0】" in _norm(doc[0].get_text())
    finally:
        doc.close()


# ---------------------------------------------------------------- 独立运行

def _all_tests():
    return [(n, f) for n, f in sorted(globals().items())
            if n.startswith("test_") and callable(f)]


def main():
    ok = 0
    for name, fn in _all_tests():
        try:
            fn()
        except Exception:
            print(f"FAIL {name}")
            traceback.print_exc()
        else:
            ok += 1
            print(f"PASS {name}")
    total = len(_all_tests())
    print(f"\n{ok}/{total} 通过")
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
