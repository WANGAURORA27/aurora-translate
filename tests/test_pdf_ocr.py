# -*- coding: utf-8 -*-
"""离线单元测试：``pipelines/pdf_ocr.py``（扫描版 PDF · OCR 原位翻译）。

原则：

* **翻译一律用本地假翻译函数**（绝不联网、不碰 API）；
* **OCR 要真跑 tesseract**——样例 PDF 用 PyMuPDF 现场画好再整页栅格化成
  「只有图片、没有文本层」的扫描件（写进 ``tests/_tmp/pdf_ocr/``）；
* tesseract 不可用（没装 / 缺语言包）时，所有依赖 OCR 的用例打印 ``SKIP``
  并通过，**绝不 fail**。

运行方式（两种都行）::

    cd <仓库目录>/docbridge
    python3 -m pytest tests/test_pdf_ocr.py -q
    python3 tests/test_pdf_ocr.py
"""

from __future__ import annotations

import importlib.util
import os
import sys
import traceback

import fitz

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                       # .../docbridge
MODULE_PATH = os.path.join(ROOT, "pipelines", "pdf_ocr.py")
TMP = os.path.join(HERE, "_tmp", "pdf_ocr")        # 自己的子目录，别踩别人的产物

OCR_LANG = "eng"                                   # 本机/服务器都装了 eng
SCAN_DPI = 200                                     # 栅格化成"扫描件"用的精度


# ---------------------------------------------------------------- 载入被测模块

def _load_pipeline():
    if not os.path.isfile(MODULE_PATH):
        raise RuntimeError(f"被测模块不存在：{MODULE_PATH}")
    spec = importlib.util.spec_from_file_location("pdf_ocr_under_test", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


po = _load_pipeline()
os.makedirs(TMP, exist_ok=True)

# OCR 可用性：模块导入时就探一次（真跑一次 tesseract），供各用例决定 SKIP
OCR_OK, OCR_WHY = po.ocr_probe(OCR_LANG, 120)
_SKIPPED = []


def _need_ocr():
    """tesseract 不可用 → 打印 SKIP 并返回 False（用例直接 return 即算通过）。"""
    if OCR_OK:
        return True
    _SKIPPED.append(OCR_WHY or "tesseract 不可用")
    print(f"  SKIP：tesseract 不可用（{OCR_WHY}），跳过依赖 OCR 的断言")
    return False


# ---------------------------------------------------------------- 样例生成

def _rm(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _blob_pixmap(w=96, h=68):
    """造一张彩色小位图：当作扫描页里的小插图（栅格化后会成为扫描图的一部分）。"""
    d = fitz.open()
    p = d.new_page(width=w, height=h)
    p.draw_rect(fitz.Rect(0, 0, w, h), color=None, fill=(0.86, 0.33, 0.18))
    p.draw_circle(fitz.Point(w * 0.33, h * 0.5), min(w, h) * 0.22,
                  color=None, fill=(0.10, 0.38, 0.85))
    p.draw_rect(fitz.Rect(w * 0.6, h * 0.2, w * 0.9, h * 0.8),
                color=None, fill=(0.16, 0.60, 0.30))
    pix = p.get_pixmap(dpi=72)
    d.close()
    return pix


def _scan_page(doc, lines, blob=False):
    """先把内容画在一张普通页上，再整页栅格化贴到新页 → **没有文本层**的扫描件。

    ``lines`` 是 ``[(文本, 字号, 与上一行的额外间距), ...]``。
    """
    tmp = fitz.open()
    src = tmp.new_page(width=595, height=842)
    y = 96.0
    for text, size, extra in lines:
        y += extra
        src.insert_text((64, y), text, fontsize=size)
        y += size + 11
    if blob:
        src.insert_image(fitz.Rect(400, 600, 496, 668), pixmap=_blob_pixmap())
    pix = src.get_pixmap(dpi=SCAN_DPI)
    tmp.close()

    page = doc.new_page(width=595, height=842)
    page.insert_image(page.rect, pixmap=pix)
    return page


PAGE1 = [
    ("Chapter 3  Supply and Demand", 17, 0),
    ("The market equilibrium is the point where the quantity", 15, 8),
    ("supplied equals the quantity demanded. When the price of", 15, 0),
    ("a good rises, the quantity demanded falls while the quantity", 15, 0),
    ("supplied rises, other things being equal.", 15, 0),
    ("12345", 15, 26),                       # 纯数字行：必须跳过，不送翻译
    ("Figure 3.1 shows the equilibrium price and quantity.", 15, 30),
]

PAGE2 = [
    ("Elasticity measures how much buyers and sellers respond", 15, 0),
    ("to changes in market conditions. A market is competitive", 15, 8),
    ("when many buyers and sellers trade identical goods.", 15, 0),
]


def build_scan_sample(path):
    """3 页扫描件：第 1 页（带小插图的正文）、第 2 页（正文）、第 3 页（纯图无文字）。"""
    doc = fitz.open()
    _scan_page(doc, PAGE1, blob=True)
    _scan_page(doc, PAGE2)
    _scan_page(doc, [], blob=True)           # 纯图片页：OCR 什么都认不出来
    doc.save(path)
    doc.close()
    return path


def build_blank_scan(path):
    """整页纯白（只有一张白图、没有任何文字）→ 整份文档一个字都 OCR 不出来。"""
    doc = fitz.open()
    _scan_page(doc, [])
    doc.save(path)
    doc.close()
    return path


# ---------------------------------------------------------------- 假翻译

def make_fake(record=None):
    """契约里的假翻译：等长同序返回，可选记录每批的 (texts, context)。"""
    def fake(texts, ctx=None):
        assert isinstance(texts, list), "translate 必须先收到 list"
        if record is not None:
            record.append((list(texts), dict(ctx or {})))
        return [f"【译{i}】{t}" for i, t in enumerate(texts)]
    return fake


def _progress_recorder():
    calls = []

    def progress(done, total, note=""):
        calls.append((done, total, note))
    return calls, progress


# ================================================================ 用例

def test_end_to_end_scan_pdf():
    """主流程：扫描件 → 中文译文画在页面上；页数/图片不变，进度单调到 total。"""
    if not _need_ocr():
        return
    src = build_scan_sample(os.path.join(TMP, "scan_sample.pdf"))
    out = os.path.join(TMP, "scan_sample_zh.pdf")
    _rm(out)

    calls = []
    prog, progress = _progress_recorder()
    stats = po.run(src, out, translate=make_fake(calls), progress=progress,
                   options={"ocr_lang": OCR_LANG, "ocr_dpi": SCAN_DPI})

    # --- 产物
    assert os.path.isfile(out), "没有生成输出文件"
    assert not os.path.exists(out + ".part"), "成功后不该残留 .part"

    with fitz.open(src) as sdoc, fitz.open(out) as odoc:
        assert odoc.page_count == sdoc.page_count == 3, \
            f"页数必须不变：输入 {sdoc.page_count}，输出 {odoc.page_count}"
        # 样例本身确实是"没有文本层的扫描件"
        assert sdoc[0].get_text().strip() == "", "样例第 1 页不该有文本层"
        # 原扫描图仍在：每页图片数量一致（且扫描页就是那 1 张整页图）
        for i in range(sdoc.page_count):
            n_in = len(sdoc[i].get_images(full=True))
            n_out = len(odoc[i].get_images(full=True))
            assert n_in == n_out == 1, f"第 {i+1} 页图片数量变了：{n_in} -> {n_out}"
        # 中文确实画上了文本层（不然复制/搜索都拿不到译文）
        text0 = odoc[0].get_text()
        assert "【译0】" in text0, f"输出第 1 页文本层里没有中文特征串：{text0!r}"
        assert len(text0.strip()) > 5
        assert "【译0】" in odoc[1].get_text(), "第 2 页也该有译文"

    # --- 统计
    assert stats["pages"] == 3
    assert stats["units"] >= 2, stats
    assert stats["chars_in"] > 0 and stats["chars_out"] > 0, stats
    assert stats["ocr_words"] > 0, stats
    assert stats["drawn_lines"] >= 1, stats
    assert stats["drawn_units"] >= 1, stats
    assert stats["empty_pages"] >= 1, f"第 3 页无文字，应计入 empty_pages：{stats}"
    assert "OCR" in stats["detail"] and "3 页" in stats["detail"], stats["detail"]
    assert "无可识别文本" in stats["detail"], stats["detail"]

    # --- 翻译回调：等长同序、（OCR 模式下）上下文正确、批大小合规
    assert calls, "translate 从未被调用"
    for texts, ctx in calls:
        assert ctx.get("kind") == "pdf" and ctx.get("ocr") is True, ctx
        assert 1 <= ctx.get("page", 0) <= 3, ctx
        assert 1 <= len(texts) <= 100, f"单批条数越界：{len(texts)}"
    sent = [t for texts, _ctx in calls for t in texts]
    assert not any(t.strip().isdigit() for t in sent), \
        f"纯数字行不该送翻译：{sent}"
    assert stats["skipped"] >= 1, f"纯数字行应计入 skipped：{stats}"

    # --- 进度：被调用过、单调递增、末次 done == total
    assert prog, "progress 回调没被调用"
    dones = [d for d, _t, _n in prog]
    assert dones == sorted(dones), f"进度不单调：{dones}"
    assert all(t == 3 for _d, t, _n in prog), prog
    assert prog[-1][0] == prog[-1][1] == 3, f"末次进度必须是 3/3：{prog[-1]}"
    assert prog[0][0] == 0, f"首次数应从 0 开始：{prog[0]}"


def test_progress_monotonic_and_final_total():
    """进度契约单独再断言一次（只在回调上做文章，方便定位问题）。"""
    if not _need_ocr():
        return
    src = os.path.join(TMP, "scan_sample.pdf")
    if not os.path.isfile(src):
        build_scan_sample(src)
    out = os.path.join(TMP, "progress_out.pdf")
    _rm(out)
    prog, progress = _progress_recorder()
    po.run(src, out, translate=make_fake(), progress=progress,
           options={"ocr_lang": OCR_LANG, "ocr_dpi": SCAN_DPI})
    assert prog, "progress 未被调用"
    dones = [d for d, _t, _n in prog]
    assert dones == sorted(dones), dones
    assert dones[0] == 0 and dones[-1] == 3, dones
    assert {t for _d, t, _n in prog} == {3}, prog


def test_pure_image_page_does_not_fail_document():
    """纯图片页（OCR 一个字都没有）不该让整份文档失败，只计入 detail。"""
    if not _need_ocr():
        return
    doc = fitz.open()
    _scan_page(doc, [], blob=True)                      # 纯图页
    _scan_page(doc, PAGE2)                              # 有文字的页
    src = os.path.join(TMP, "mixed_blank_page.pdf")
    doc.save(src)
    doc.close()

    out = os.path.join(TMP, "mixed_blank_page_zh.pdf")
    _rm(out)
    stats = po.run(src, out, translate=make_fake(),
                   options={"ocr_lang": OCR_LANG, "ocr_dpi": SCAN_DPI})
    assert os.path.isfile(out)
    assert stats["pages"] == 2
    assert stats["empty_pages"] >= 1, stats
    assert stats["units"] >= 1, stats
    assert "无可识别文本" in stats["detail"], stats["detail"]


def test_blank_document_raises():
    """整份文档一个字都没 OCR 出来 → 必须抛中文 RuntimeError，且不留半成品。"""
    if not _need_ocr():
        return
    src = build_blank_scan(os.path.join(TMP, "blank_scan.pdf"))
    out = os.path.join(TMP, "blank_scan_zh.pdf")
    _rm(out)
    try:
        po.run(src, out, translate=make_fake(),
               options={"ocr_lang": OCR_LANG, "ocr_dpi": SCAN_DPI})
    except RuntimeError as exc:
        msg = str(exc)
        assert "OCR" in msg, msg
        assert "没有 OCR 出任何文字" in msg, msg
    else:
        raise AssertionError("空扫描件必须抛 RuntimeError，不能假装成功")
    assert not os.path.exists(out), "失败时不该产出文件"
    assert not os.path.exists(out + ".part"), "失败后不该残留 .part"


def test_bad_ocr_language_raises_chinese_error():
    """ocr_lang 不存在（语言包没装/写错）→ 抛中文 RuntimeError，并给出 apt 安装提示。"""
    src = os.path.join(TMP, "scan_sample.pdf")
    if not os.path.isfile(src):
        build_scan_sample(src)
    out = os.path.join(TMP, "bad_lang_zh.pdf")
    _rm(out)
    try:
        po.run(src, out, translate=make_fake(),
               options={"ocr_lang": "zz_no_such_lang", "ocr_dpi": SCAN_DPI})
    except RuntimeError as exc:
        msg = str(exc)
        assert "OCR" in msg, msg
        assert "zz_no_such_lang" in msg, msg
        assert "apt install tesseract-ocr" in msg, msg
    else:
        raise AssertionError("语言不存在时必须抛 RuntimeError")
    assert not os.path.exists(out)
    assert not os.path.exists(out + ".part")


def test_original_line_boxes_are_covered():
    """盖原文是「真的盖上」：每个 OCR 行框都被一个填充矩形覆盖（原图之上再画底色块）。"""
    if not _need_ocr():
        return
    src = os.path.join(TMP, "scan_sample.pdf")
    if not os.path.isfile(src):
        build_scan_sample(src)
    out = os.path.join(TMP, "covered_zh.pdf")
    _rm(out)
    po.run(src, out, translate=make_fake(),
           options={"ocr_lang": OCR_LANG, "ocr_dpi": SCAN_DPI})

    with fitz.open(src) as sdoc:
        page0 = sdoc[0]
        words, err = po._ocr_words(page0, OCR_LANG, SCAN_DPI)
        assert not err, err
        lines = po._group_lines(words)
        assert lines, "样例页 OCR 不出行，用例无意义"
        # 只检查"确实要翻译"的行：纯数字/中文行本来就该保留原文（不盖）
        boxes = [ln["rect"] for ln in lines
                 if po._needs_translation(ln["text"], "zh-Hans")]
        assert boxes, "样例页没有待译行"

    with fitz.open(out) as odoc:
        fills = [fitz.Rect(d["rect"]) for d in odoc[0].get_drawings()
                 if d.get("fill") is not None]
    assert fills, "输出页上没有任何填充矩形（原文没被盖住）"
    for box in boxes:
        area = box.get_area()
        covered = max(((f & box).get_area() for f in fills), default=0.0)
        assert covered >= area - 1.0, f"行框没被底色盖住：{box} 覆盖率 {covered}/{area}"

    # 反过来：被判定为"无需翻译"的行（纯数字）必须原样保留、不被盖住
    kept = [ln["rect"] for ln in lines
            if not po._needs_translation(ln["text"], "zh-Hans")]
    assert kept, "样例里的纯数字行没被识别出来"
    for box in kept:
        covered = max(((f & box).get_area() for f in fills), default=0.0)
        assert covered <= 0.25 * box.get_area(), \
            f"无需翻译的行不该被覆盖：{box} 覆盖率 {covered}"


def test_translate_count_mismatch_raises():
    """翻译返回数量不符 → RuntimeError("翻译返回数量不符：期望 N，实际 M")，不留半成品。"""
    if not _need_ocr():
        return
    src = os.path.join(TMP, "scan_sample.pdf")
    if not os.path.isfile(src):
        build_scan_sample(src)
    out = os.path.join(TMP, "mismatch_zh.pdf")
    _rm(out)

    def fake_short(texts, ctx=None):
        return ["【译】%s" % t for t in texts[:-1]]     # 故意少一条

    try:
        po.run(src, out, translate=fake_short,
               options={"ocr_lang": OCR_LANG, "ocr_dpi": SCAN_DPI})
    except RuntimeError as exc:
        msg = str(exc)
        assert "翻译返回数量不符" in msg, msg
        assert "期望" in msg and "实际" in msg, msg
    else:
        raise AssertionError("数量不符必须抛 RuntimeError")
    assert not os.path.exists(out), "失败时不该产出文件"
    assert not os.path.exists(out + ".part"), "失败后不该残留 .part"


def test_chinese_only_translation_is_drawn():
    """真实场景的短中文译文：每一段按行框绘制，译文行数少于原文行时多余的原文要被盖住。"""
    if not _need_ocr():
        return
    src = os.path.join(TMP, "scan_sample.pdf")
    if not os.path.isfile(src):
        build_scan_sample(src)
    out = os.path.join(TMP, "chinese_only_zh.pdf")
    _rm(out)

    def fake_zh(texts, ctx=None):
        return [f"【中文译文{i}】" for i in range(len(texts))]

    stats = po.run(src, out, translate=fake_zh,
                   options={"ocr_lang": OCR_LANG, "ocr_dpi": SCAN_DPI})
    with fitz.open(out) as odoc:
        text = odoc[0].get_text()
    assert "【中文译文0】" in text, f"中文译文没画到页面上：{text!r}"
    # 译文保持中文、没有把 OCR 出来的英文再写进文本层
    assert "equilibrium" not in text.lower(), \
        f"输出文本层不该有 OCR 出来的英文原文（应已被中文替换）：{text!r}"
    assert stats["units"] >= 1 and stats["drawn_units"] >= 1, stats


def test_bad_inputs_raise_chinese_errors():
    """坏输入：文件不存在 / 不是 PDF / 加密 PDF → 中文 RuntimeError。"""
    out = os.path.join(TMP, "bad_input_zh.pdf")
    _rm(out)

    # 1) 文件不存在
    try:
        po.run(os.path.join(TMP, "nope_missing.pdf"), out, translate=make_fake())
    except RuntimeError as exc:
        assert "找不到输入文件" in str(exc), exc
    else:
        raise AssertionError("文件不存在必须抛 RuntimeError")

    # 2) 不是 PDF
    notpdf = os.path.join(TMP, "not_a_pdf.pdf")
    with open(notpdf, "w", encoding="utf-8") as fh:
        fh.write("这不是 PDF\n")
    try:
        po.run(notpdf, out, translate=make_fake())
    except RuntimeError as exc:
        assert "无法打开输入 PDF" in str(exc) or "不是有效的 PDF" in str(exc), exc
    else:
        raise AssertionError("非 PDF 必须抛 RuntimeError")

    # 3) 加密 PDF
    enc = os.path.join(TMP, "encrypted.pdf")
    d = fitz.open()
    d.new_page().insert_text((72, 100), "secret", fontsize=14)
    d.save(enc, encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="user")
    d.close()
    try:
        po.run(enc, out, translate=make_fake())
    except RuntimeError as exc:
        assert "加密" in str(exc), exc
    else:
        raise AssertionError("加密 PDF 必须抛 RuntimeError")

    # 4) 缺少 translate 回调
    src = os.path.join(TMP, "scan_sample.pdf")
    if not os.path.isfile(src):
        build_scan_sample(src)
    try:
        po.run(src, out, translate=None)
    except RuntimeError as exc:
        assert "translate" in str(exc), exc
    else:
        raise AssertionError("缺 translate 回调必须抛 RuntimeError")

    assert not os.path.exists(out + ".part"), "失败后不该残留 .part"


def test_contract_metadata():
    """契约元数据：FORMAT/MODE/LABEL/NOTE/OPTIONS/run 都要在。"""
    assert po.FORMAT == "pdf"
    assert po.MODE == "ocr"
    assert isinstance(po.LABEL, str) and po.LABEL
    assert isinstance(po.NOTE, str) and po.NOTE
    opts = list(po.OPTIONS)
    for key in ("ocr_lang", "ocr_dpi", "font", "font_scale",
                "source_lang", "target_lang"):
        assert key in opts, f"OPTIONS 缺少 {key}：{opts}"
    assert callable(po.run)


def test_two_column_scan_keeps_columns():
    """双栏扫描件：左右两栏不能被连成一行，中文不能横跨整页盖掉版式。"""
    if not _need_ocr():
        return
    tmp = fitz.open()
    src_page = tmp.new_page(width=595, height=842)
    for i in range(4):
        src_page.insert_text((56, 120 + i * 26),
                             "Left column line %d about supply." % i, fontsize=13)
        src_page.insert_text((330, 120 + i * 26),
                             "Right column line %d about demand." % i, fontsize=13)
    pix = src_page.get_pixmap(dpi=SCAN_DPI)
    tmp.close()
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_image(page.rect, pixmap=pix)
    src = os.path.join(TMP, "two_column.pdf")
    doc.save(src)
    doc.close()

    out = os.path.join(TMP, "two_column_zh.pdf")
    _rm(out)

    calls = []
    stats = po.run(src, out, translate=make_fake(calls),
                   options={"ocr_lang": OCR_LANG, "ocr_dpi": SCAN_DPI})
    assert stats["units"] >= 6, f"两栏各有 4 行，段落数不该这么少：{stats}"
    sent = [t for texts, _c in calls for t in texts]
    # 任何一条送翻的文本都不该同时含左右两栏的关键词（说明没被连成一行）
    assert not any("Left" in t and "Right" in t for t in sent), sent

    with fitz.open(out) as odoc:
        spans = [(s["bbox"], s["text"]) for b in odoc[0].get_text("dict")["blocks"]
                 for l in b.get("lines", []) for s in l["spans"]]
    assert spans, "输出页没有画上任何文本"
    mid = 595 / 2.0
    # 中文不许横跨两栏（画到中缝上）
    for bbox, text in spans:
        assert bbox[0] < mid or bbox[2] > mid, f"文本整段落在中缝右侧：{text[:20]!r}"
        assert not (bbox[0] < mid - 20 and bbox[2] > mid + 20), \
            f"这段文本横跨了中缝（两栏被连成一行）：{text[:30]!r} {bbox}"


def test_rotated_page_is_skipped_not_garbled():
    """/Rotate 90°/270° 的页：跳过不译（原文原样保留），其余页照常翻，绝不画歪。"""
    if not _need_ocr():
        return
    doc = fitz.open()
    rotated = _scan_page(doc, PAGE2)               # 栅格化扫描页，再打上 /Rotate
    rotated.set_rotation(90)
    _scan_page(doc, PAGE1[:4])                     # 正常页（无旋转）
    src = os.path.join(TMP, "rotated_mixed.pdf")
    doc.save(src)
    doc.close()

    out = os.path.join(TMP, "rotated_mixed_zh.pdf")
    _rm(out)
    calls = []
    stats = po.run(src, out, translate=make_fake(calls),
                   options={"ocr_lang": OCR_LANG, "ocr_dpi": SCAN_DPI})
    assert stats["pages"] == 2
    assert stats["rotated_pages"] == 1, stats
    assert "旋转" in stats["detail"], stats["detail"]
    with fitz.open(out) as odoc:
        assert odoc.page_count == 2
        assert odoc[0].rotation == 90, "旋转属性必须原样保留"
        assert odoc[0].get_text().strip() == "", "跳过页不该被写入任何文本"
        assert "【译0】" in odoc[1].get_text(), "没旋转的那页要正常翻译"


def test_all_rotated_document_raises():
    """整份文档都是旋转页 → 明确报「本模块暂不处理」，而不是含糊的 OCR 失败。"""
    if not _need_ocr():
        return
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((64, 120), "Rotated page with English text.", fontsize=14)
    page.set_rotation(90)
    src = os.path.join(TMP, "all_rotated.pdf")
    doc.save(src)
    doc.close()

    out = os.path.join(TMP, "all_rotated_zh.pdf")
    _rm(out)
    try:
        po.run(src, out, translate=make_fake(),
               options={"ocr_lang": OCR_LANG, "ocr_dpi": SCAN_DPI})
    except RuntimeError as exc:
        assert "旋转" in str(exc), exc
    else:
        raise AssertionError("全旋转页必须抛 RuntimeError（否则等于假装成功）")
    assert not os.path.exists(out)
    assert not os.path.exists(out + ".part")


def test_line_and_paragraph_grouping():
    """不依赖 OCR 的纯逻辑用例：聚行 / 聚段 / 跳过判定。"""
    words = [
        (60, 100, 110, 114, "The", 0, 0, 0),
        (114, 100, 150, 114, "market", 0, 0, 1),
        (60, 126, 96, 140, "clears", 0, 0, 0),
        (60, 190, 90, 204, "12345", 0, 0, 0),       # 隔得远 + 纯数字
    ]
    lines = po._group_lines(words)
    assert len(lines) == 3, lines
    assert lines[0]["text"] == "The market", lines[0]
    assert lines[0]["rect"].x0 == 60 and lines[0]["rect"].x1 == 150
    assert lines[1]["text"] == "clears"

    units = po._page_units(lines, "zh-Hans")
    assert len(units) == 1, units                      # "12345" 是断点，不参与
    assert units[0][1] == "The market clears", units
    assert lines[2]["tr"] is False, "纯数字行不该送翻译"

    # 连字符续行拼接
    assert po._join_lines(["exam-", "ple text"]) == "example text"
    # 批大小：105 条 → 53 + 52（均衡、都不超过 100）；超长文本按字符数封顶
    chunks = po._chunk_units(["x"] * 105)
    sizes = [b - a for a, b in chunks]
    assert sum(sizes) == 105 and max(sizes) <= po.BATCH_MAX, sizes
    assert min(sizes) >= 30, sizes
    long_chunks = po._chunk_units(["y" * 1000] * 5)
    assert all(sum(len("y" * 1000) for _ in range(b - a)) <= po.BATCH_MAX_CHARS
               for a, b in long_chunks), long_chunks
    assert sum(b - a for a, b in long_chunks) == 5


# ---------------------------------------------------------------- 独立运行

def _all_tests():
    return [(n, f) for n, f in sorted(globals().items())
            if n.startswith("test_") and callable(f)]


def main():
    print(f"OCR 探测（tesseract 语言 {OCR_LANG}）："
          + ("可用 ✅" if OCR_OK else f"不可用 ❌ {OCR_WHY}"))
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
    print(f"\n{ok}/{total} 通过" + ("（部分用例 SKIP：tesseract 不可用）"
                                    if _SKIPPED else ""))
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
