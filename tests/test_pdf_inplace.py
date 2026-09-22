# -*- coding: utf-8 -*-
"""docbridge · PDF 原位版式保留管线 离线单元测试。

* 全程使用**假翻译函数**，绝不联网、绝不调用真实 API。
* 测试样例用 PyMuPDF 现场生成到 ``tests/_tmp/``（不依赖任何既有大 PDF）。
* 两种跑法都行：

      cd <仓库目录>/docbridge
      python3 -m pytest tests/test_pdf_inplace.py -q
      python3 tests/test_pdf_inplace.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import traceback

import fitz

HERE = os.path.dirname(os.path.abspath(__file__))
DOCBRIDGE = os.path.dirname(HERE)
TMP_ROOT = os.path.join(HERE, "_tmp")


# ------------------------------------------------------------------ 加载被测模块

def _load_module():
    if DOCBRIDGE not in sys.path:
        sys.path.insert(0, DOCBRIDGE)
    try:
        import pipelines.pdf_inplace as mod  # noqa: PLC0415
        return mod
    except Exception:  # noqa: BLE001  （pipelines/__init__.py 由别人维护，可能暂时不可导入）
        path = os.path.join(DOCBRIDGE, "pipelines", "pdf_inplace.py")
        spec = importlib.util.spec_from_file_location("dsh_pdf_inplace", path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return mod


mod = _load_module()


# ------------------------------------------------------------------ 断言小工具

class _AssertRaises:
    """同时兼容 pytest 与直接运行的 assertRaises。"""

    def __init__(self, exc):
        self.exc = exc
        self.value = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            raise AssertionError("期望抛出 %s，但没有抛" % self.exc.__name__)
        if not issubclass(exc_type, self.exc):
            return False
        self.value = exc
        return True


def _fail(msg):
    raise AssertionError(msg)


def _case_dir(name):
    path = os.path.join(TMP_ROOT, name)
    shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path, exist_ok=True)
    # 跨任务共享缓存默认开启，所以测试必须**按用例隔离**：否则前一个用例翻过的
    # 样例会被后一个用例直接命中，看起来像「没调用翻译」，掩盖真实行为。
    os.environ["DOCBRIDGE_SHARED_CACHE"] = os.path.join(path, "_shared_cache")
    return path


# ------------------------------------------------------------------ 样例生成

def _solid_pixmap(w, h, rgb):
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, w, h), False)
    pix.set_rect(pix.irect, tuple(rgb))
    return pix


def _build_sample(path):
    """3 页样例：标题 + 正文段落 + 表格（draw_rect + insert_text）+ 图片。

    第 3 页**没有任何文本**（只有图片）——上游会跳过无文本页、不打印完成行，
    正好用来验证本模块最后补报 progress(total, total)。
    """
    doc = fitz.open()

    # ---- 第 1 页：标题 + 正文段落
    p1 = doc.new_page(width=595, height=842)
    p1.insert_text((72, 96), "Introduction to Economics",
                   fontname="hebo", fontsize=18)
    y = 140
    for line in (
        "Microeconomics studies how societies allocate scarce resources.",
        "Scarcity means that human wants exceed the available supply.",
        "Economists build models to explain observed market behaviour.",
        "Prices coordinate the decisions of buyers and sellers.",
    ):
        p1.insert_text((72, y), line, fontname="tiro", fontsize=11)
        y += 16

    # ---- 第 2 页：表格 + 图片 + 图注
    p2 = doc.new_page(width=595, height=842)
    p2.insert_text((72, 96), "Table 1.1 Regional Growth Summary",
                   fontname="hebo", fontsize=14)
    rows = [["Region", "Growth", "Notes"],
            ["Asia", "Rapid", "High"],
            ["Europe", "Steady", "Moderate"]]
    x0, y0, cw, ch = 72, 130, 150, 22
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            rect = fitz.Rect(x0 + c * cw, y0 + r * ch,
                             x0 + (c + 1) * cw, y0 + (r + 1) * ch)
            p2.draw_rect(rect, color=(0, 0, 0), width=0.7)
            p2.insert_text((rect.x0 + 5, rect.y0 + 15), cell,
                           fontname="helv", fontsize=10)
    p2.insert_text((72, 300), "Figure 1.1 Demand curve shifts right.",
                   fontname="tiro", fontsize=11)
    p2.insert_image(fitz.Rect(340, 300, 480, 380),
                    pixmap=_solid_pixmap(80, 60, (200, 60, 60)))

    # ---- 第 3 页：只有图片，没有任何文本
    p3 = doc.new_page(width=595, height=842)
    p3.insert_image(fitz.Rect(120, 200, 420, 380),
                    pixmap=_solid_pixmap(120, 80, (40, 90, 200)))

    doc.save(path, garbage=4, deflate=True)
    doc.close()
    return path


_SAMPLE_CACHE = {}


def _sample_pdf():
    """整个测试进程内共用一个样例文件（只读，不让上游写它）。"""
    if "path" not in _SAMPLE_CACHE:
        os.makedirs(TMP_ROOT, exist_ok=True)
        _SAMPLE_CACHE["path"] = _build_sample(os.path.join(TMP_ROOT, "sample.pdf"))
    return _SAMPLE_CACHE["path"]


# ------------------------------------------------------------------ 假翻译函数

def _fake(calls=None, short=False, boom=False):
    """契约里的假翻译函数：等长同序、带特征串。【绝不联网】"""
    def fake(texts, ctx=None):
        if not isinstance(texts, list):
            raise AssertionError("translate 收到非 list：%r" % (type(texts),))
        if calls is not None:
            calls.append(list(texts))
        if boom:
            raise RuntimeError("模拟翻译服务不可用")
        out = ["【译%d】%s" % (i, t) for i, t in enumerate(texts)]
        return out[:-1] if short else out
    return fake


# ------------------------------------------------------------------ 图片指纹

def _image_signature(path):
    """(页号, 宽, 高, 放置矩形) 的排序列表 —— 用于逐张比对图片零改动。"""
    sig = []
    with fitz.open(path) as doc:
        for pno, page in enumerate(doc):
            for info in page.get_images(full=True):
                xref, _smask, w, h = info[0], info[1], info[2], info[3]
                rects = page.get_image_rects(xref)
                if not rects:
                    rects = [None]
                for r in rects:
                    box = None if r is None else (round(r.x0, 2), round(r.y0, 2),
                                                  round(r.x1, 2), round(r.y1, 2))
                    sig.append((pno, w, h, box))
    return sorted(sig)


def _image_count(path):
    with fitz.open(path) as doc:
        return sum(len(p.get_images(full=True)) for p in doc)


def _image_pixels(path, pno, rect, dpi=150):
    """把某页某个矩形区域渲染出来，返回原始像素 —— 用于逐像素比对图片。"""
    with fitz.open(path) as doc:
        pix = doc[pno].get_pixmap(dpi=dpi, clip=rect, colorspace=fitz.csRGB)
        return (pix.width, pix.height, bytes(pix.samples))


def _page_count(path):
    with fitz.open(path) as doc:
        return doc.page_count


# ------------------------------------------------------------------ 测试用例

def test_module_metadata_matches_contract():
    assert mod.FORMAT == "pdf", mod.FORMAT
    assert mod.MODE == "inplace", mod.MODE
    assert isinstance(mod.LABEL, str) and mod.LABEL, "LABEL 不能为空"
    assert isinstance(mod.NOTE, str) and mod.NOTE, "NOTE 不能为空"
    assert "sim_bold" in mod.OPTIONS and "font_scale" in mod.OPTIONS, mod.OPTIONS
    assert callable(mod.run)


def test_inplace_translation_keeps_pages_images_and_rects():
    """核心用例：原位替换成功 + 页数一致 + 图片数量/放置矩形完全一致。"""
    d = _case_dir("basic")
    src = _sample_pdf()
    out = os.path.join(d, "out.pdf")
    calls = []

    res = mod.run(src, out, translate=_fake(calls), progress=None,
                  options={"sim_bold": True, "font_scale": 0.9})

    # 输出存在且能被 fitz 打开
    assert os.path.isfile(out), "输出文件不存在"
    assert not os.path.exists(out + ".part"), "残留了 .part 文件"

    # 页数一致
    n_src, n_out = _page_count(src), _page_count(out)
    assert n_out == n_src == 3, (n_src, n_out)

    # 译文真的写进去了。
    # 注意：空格可能被写成 NBSP（\xa0）——不同中文字体的 ToUnicode 映射不一样
    # （内置 china-s 给普通空格，Noto Serif CJK 给 NBSP），所以比较前先归一化，
    # 否则这条断言会变成「字体相关」的脆弱测试。
    with fitz.open(out) as doc:
        p1_text = doc[0].get_text().replace("\xa0", " ")
        all_text = "".join(p.get_text() for p in doc).replace("\xa0", " ")
    assert "【译0】" in p1_text, "第 1 页没有找到译文特征串：%r" % p1_text[:200]
    assert "【译0】Introduction to Economics" in p1_text, p1_text[:200]
    assert "【译" in all_text, "输出里没有任何译文"

    # ★ 最重要的断言：图片数量 + 放置矩形零改动
    sig_in, sig_out = _image_signature(src), _image_signature(out)
    assert _image_count(out) == _image_count(src) == 2, \
        (_image_count(src), _image_count(out))
    assert sig_out == sig_in, (
        "图片放置矩形发生变化：\n输入 %r\n输出 %r" % (sig_in, sig_out))

    # ★★ 最强形式：图片区域的**像素**逐字节一致（150dpi 渲染比对）
    with fitz.open(src) as doc:
        placements = [(pno, r) for pno, page in enumerate(doc)
                      for info in page.get_images(full=True)
                      for r in page.get_image_rects(info[0])]
    assert placements, "样例里没有图片，测试本身有问题"
    for pno, rect in placements:
        pix_in = _image_pixels(src, pno, rect)
        pix_out = _image_pixels(out, pno, rect)
        assert pix_in == pix_out, "第 %d 页图片区域像素被改动了：%r" % (pno + 1, rect)
    print("    [断言] %d 处图片区域像素逐字节一致（输入 == 输出）"
          % len(placements))

    # 返回值契约
    for key in ("units", "chars_in", "chars_out", "skipped", "pages", "detail"):
        assert key in res, "返回值缺少 %s" % key
    assert res["pages"] == 3, res
    assert res["units"] > 0, res
    assert res["chars_in"] > 0 and res["chars_out"] > 0, res
    assert res["skipped"] >= 0, res
    assert "图片" in res["detail"] and "一致" in res["detail"], res["detail"]
    print("    [断言] 图片校验：输入 %d 张 -> 输出 %d 张，矩形完全一致"
          % (_image_count(src), _image_count(out)))
    print("    [detail] %s" % res["detail"])

    # 假翻译被批量调用（不是一行一次）
    assert calls, "translate 从未被调用"
    assert all(isinstance(c, list) and c for c in calls)

    # 缓存目录：默认走跨任务共享缓存（内容哈希 + 模型 + 语言对）；
    # 关掉 shared_cache 时退回「输出文件同级的 _translate_cache」。
    assert "缓存目录" in res["detail"], res["detail"]
    shared_root = os.environ.get("DOCBRIDGE_SHARED_CACHE", "")
    assert shared_root and os.path.isdir(shared_root), \
        "默认应启用共享缓存，实际未建目录：%r" % shared_root

    d2 = _case_dir("basic_nonshared")
    out2 = os.path.join(d2, "out.pdf")
    mod.run(src, out2, translate=_fake(),
            options={"shared_cache": False})
    assert os.path.isdir(os.path.join(d2, "_translate_cache")), \
        "关掉共享缓存后，缓存目录应建在输出同级"


def test_short_translation_return_raises_and_leaves_no_part():
    """translate 少返回一条 → RuntimeError，且不留 .part（options 缺省也要能用）。"""
    d = _case_dir("short")
    src = _sample_pdf()
    out = os.path.join(d, "out.pdf")
    orig = mod.pt.translate_lines

    with _AssertRaises(RuntimeError) as ctx:
        mod.run(src, out, translate=_fake(short=True))   # 不传 options / progress
    assert "翻译返回数量不符" in str(ctx.value), str(ctx.value)
    assert "期望" in str(ctx.value) and "实际" in str(ctx.value), str(ctx.value)

    assert not os.path.exists(out + ".part"), "失败后残留了 .part 文件"
    assert not os.path.exists(out), "失败却产生了输出文件"
    assert mod.pt.translate_lines is orig, "translate_lines 没有被还原！"


def test_translate_exception_propagates_and_state_restored():
    """翻译回调抛异常：异常冒泡、.part 被清理、pt.translate_lines 还原成原函数。"""
    d = _case_dir("boom")
    src = _sample_pdf()
    out = os.path.join(d, "out.pdf")
    orig = mod.pt.translate_lines
    orig_cache = mod.pt.cache_dir_for

    with _AssertRaises(RuntimeError) as ctx:
        mod.run(src, out, translate=_fake(boom=True), options={})
    assert "模拟翻译服务不可用" in str(ctx.value), str(ctx.value)

    assert not os.path.exists(out + ".part"), "失败后残留了 .part 文件"
    assert not os.path.exists(out), "失败却产生了输出文件"
    assert mod.pt.translate_lines is orig, "translate_lines 没有被还原！"
    assert mod.pt.cache_dir_for is orig_cache, "cache_dir_for 没有被还原！"


def test_progress_is_monotonic_and_ends_at_total():
    """进度回调：被调用过、单调不减、最后一次 done == total（第 3 页无文本也能收尾）。"""
    d = _case_dir("progress")
    src = _sample_pdf()
    out = os.path.join(d, "out.pdf")
    seen = []

    def progress(done, total, note=""):
        seen.append((done, total, note))

    res = mod.run(src, out, translate=_fake(), progress=progress, options={})

    assert seen, "progress 回调从未被调用"
    assert all(t == 3 for _, t, _ in seen), seen
    dones = [d_ for d_, _, _ in seen]
    assert dones == sorted(dones), "进度不是单调递增的：%r" % (dones,)
    assert seen[-1][0] == 3, "最后一次 done 不是 total：%r" % (seen[-1],)
    assert seen[-1][0] == seen[-1][1] == res["pages"], (seen[-1], res["pages"])
    assert max(dones) == 3, dones
    print("    [progress] %r" % (seen,))


def test_two_sequential_runs_both_work():
    """连续两次 run（模拟两个任务）：第二次仍然正常 —— 验证锁与状态还原没坏。"""
    src = _sample_pdf()
    orig = mod.pt.translate_lines
    results = []
    for i in (1, 2):
        d = _case_dir("seq%d" % i)
        out = os.path.join(d, "out.pdf")
        calls = []
        res = mod.run(src, out, translate=_fake(calls),
                      options={"font_scale": 1.0 if i == 2 else 0.92})
        with fitz.open(out) as doc:
            text = doc[0].get_text()
            assert doc.page_count == 3, doc.page_count
        assert "【译0】" in text, "第 %d 次 run 没有写入译文" % i
        assert calls, "第 %d 次 run 没有调用 translate" % i
        assert mod.pt.translate_lines is orig, "第 %d 次 run 后状态没还原" % i
        results.append(res)
    assert results[0]["units"] > 0 and results[1]["units"] > 0, results


def test_concurrent_runs_do_not_cross_contaminate():
    """并发（模拟 Flask 多线程）时，模块级替换必须被锁串行化：两路译文不许串线。

    小样例在无锁实现下才会偶发串线，因此本用例是那把 Lock 的直接回归测试。
    """
    import threading

    src = _sample_pdf()
    # 本用例考的是「模块级替换被锁串行化」，必须关掉共享缓存：
    # 三路输入内容相同，开着共享缓存会直接复用译文，反而与断言语义冲突。
    os.environ["DOCBRIDGE_SHARED_CACHE"] = "0"
    tags = ["A", "B", "C"]
    outs = {}
    errors = []
    barrier = threading.Barrier(len(tags))

    def worker(tag):
        d = _case_dir("conc_%s" % tag)
        os.environ["DOCBRIDGE_SHARED_CACHE"] = "0"   # _case_dir 会设路径，这里再关掉
        out = os.path.join(d, "out.pdf")
        outs[tag] = out

        def tr(texts, ctx=None):
            return ["【%s%d】%s" % (tag, i, t) for i, t in enumerate(texts)]

        try:
            barrier.wait(timeout=30)
            mod.run(src, out, translate=tr, options={})
        except Exception as exc:  # noqa: BLE001
            errors.append("%s: %r" % (tag, exc))

    threads = [threading.Thread(target=worker, args=(t,)) for t in tags]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=180)

    assert not errors, "并发 run 报错：%r" % (errors,)
    assert not any(t.is_alive() for t in threads), "并发 run 卡死（可能死锁）"
    for tag in tags:
        with fitz.open(outs[tag]) as doc:
            text = "".join(p.get_text() for p in doc)
        assert "【%s0】" % tag in text, "%s 自己的译文没写进输出" % tag
        for other in tags:
            if other != tag:
                assert "【%s" % other not in text, \
                    "串线了！%s 的输出里出现了 %s 的译文" % (tag, other)
    print("    [并发] 3 路任务互不串线，状态已复原")


def test_cache_hit_on_second_run():
    """同一输入 + 同一缓存目录跑两次：第二次应命中缓存（translate 调用次数更少）。"""
    d = _case_dir("cache")
    src = _sample_pdf()
    shared_cache = os.path.join(d, "cache")
    out_a, out_b = os.path.join(d, "a.pdf"), os.path.join(d, "b.pdf")
    calls_a, calls_b = [], []

    res_a = mod.run(src, out_a, translate=_fake(calls_a),
                    options={"cache_dir": shared_cache, "target_lang": "zh-Hans"})
    res_b = mod.run(src, out_b, translate=_fake(calls_b),
                    options={"cache_dir": shared_cache, "target_lang": "zh-Hans"})

    assert calls_a, "第一次没有调用 translate"
    assert len(calls_b) < len(calls_a), (
        "第二次没有命中缓存：第一次 %d 批，第二次 %d 批"
        % (len(calls_a), len(calls_b)))
    assert len(calls_b) == 0, "第二次仍调用了 %d 批（应全部命中缓存）" % len(calls_b)
    assert res_a["units"] == res_b["units"], (res_a["units"], res_b["units"])
    assert "缓存复用" in res_b["detail"], res_b["detail"]
    assert _image_signature(out_b) == _image_signature(src)
    print("    [cache] 第一批 %d 次调用 / 第二批 %d 次调用；%s"
          % (len(calls_a), len(calls_b), res_b["detail"]))

    # 不同语言对 → 不同缓存目录（互不污染）
    calls_c = []
    out_c = os.path.join(d, "c.pdf")
    mod.run(src, out_c, translate=_fake(calls_c),
            options={"cache_dir": shared_cache, "target_lang": "ja"})
    assert calls_c, "换了目标语言却仍命中缓存 —— 语言对隔离失效"


def test_image_mismatch_guard_deletes_part_and_raises():
    """图片数量不一致必须崩，并删掉 .part（用伪造的上游自检行触发）。"""
    d = _case_dir("guard")
    src = _sample_pdf()
    part = os.path.join(d, "out.pdf.part")
    shutil.copyfile(src, part)

    fake_stdout = "  p1 完成\n图片: 输入 3 张 -> 输出 2 张  ⚠️ 不一致！\n"
    with _AssertRaises(RuntimeError) as ctx:
        mod._verify_images(fake_stdout, src, part, part)
    assert "图片数量发生变化：输入 3 张、输出 2 张，结果不可信" == str(ctx.value), \
        str(ctx.value)
    assert not os.path.exists(part), "图片校验失败后 .part 没被删除"

    # 正常情况返回 (输入, 输出)
    ok_stdout = "图片: 输入 2 张 -> 输出 2 张  ✅ 数量一致\n"
    good = os.path.join(d, "good.pdf")
    shutil.copyfile(src, good)
    assert mod._verify_images(ok_stdout, src, good, good) == (2, 2)


def test_parse_helpers_are_forgiving():
    """解析失败不许崩：给合理值。"""
    assert mod._parse_stats("") is None
    assert mod._parse_image_counts("随便什么输出") is None
    parsed = mod._parse_stats(
        "统计: 待译行 12，已译 9，跳过 3，缓存复用 5 行")
    assert parsed == (12, 9, 3, 5), parsed
    assert mod._parse_image_counts("图片: 输入 12 张 -> 输出 12 张  ✅") == (12, 12)
    # 未知 options 键不该报错
    d = _case_dir("unknownopt")
    src = _sample_pdf()
    out = os.path.join(d, "out.pdf")
    res = mod.run(src, out, translate=_fake(),
                  options={"完全不认识的键": 1, "font": "china-s",
                           "source_lang": "en", "target_lang": "zh-Hans"})
    assert res["pages"] == 3 and res["units"] > 0, res


# ------------------------------------------------------------------ 直接运行

def main():
    os.makedirs(TMP_ROOT, exist_ok=True)
    tests = [(name, obj) for name, obj in sorted(globals().items())
             if name.startswith("test_") and callable(obj)]
    failures = []
    for name, fn in tests:
        print("== %s" % name)
        try:
            fn()
        except Exception:  # noqa: BLE001
            failures.append(name)
            print("   FAIL")
            traceback.print_exc()
        else:
            print("   ok")
    print("\n%d passed, %d failed (共 %d 个用例，样例目录 %s)"
          % (len(tests) - len(failures), len(failures), len(tests), TMP_ROOT))
    if failures:
        print("失败：%s" % ", ".join(failures))
        return 1
    shutil.rmtree(TMP_ROOT, ignore_errors=True)   # 全绿才清理现场，失败留证据
    return 0


def test_prefetch_batches_across_pages_and_reuses_cache():
    """批量预翻译：跨页攒批、请求数远少于页数、写出的缓存能被上游直接命中。

    这是「更便宜/更快/更准」三件事的实现所在，所以单独钉住：
      * 便宜：请求数从「页数」降到「约 行数/批大小」；
      * 快：批次并发（这里用并发 1 的确定性来断言批次划分）；
      * 准：每批都拿到文档背景与术语表（断言 context 真的传下去了）。
    """
    d = _case_dir("prefetch")
    src = os.path.join(d, "many.pdf")
    doc = fitz.open()
    for page_no in range(5):                       # 5 页 × 15 行 = 75 行待译
        page = doc.new_page(width=595, height=842)
        for i in range(15):
            page.insert_text((60, 80 + i * 26),
                             "Page %d line %d of the financial management chapter."
                             % (page_no + 1, i + 1), fontsize=9)
    doc.set_metadata({"title": "Financial Management Lecture 2"})
    doc.save(src)
    doc.close()

    calls, contexts = [], []

    def fake(texts, ctx=None):
        calls.append(list(texts))
        contexts.append(ctx or {})
        return ["【译%d】%s" % (i, t) for i, t in enumerate(texts)]

    out = os.path.join(d, "many_out.pdf")
    orig_batch = mod.PREFETCH_LINES
    mod.PREFETCH_LINES = 20                        # 强制跨页攒批（75 行 → 4 批）
    try:
        res = mod.run(src, out, translate=fake,
                      options={"cache_dir": os.path.join(d, "cache")})
    finally:
        mod.PREFETCH_LINES = orig_batch

    assert len(calls) >= 2, "应该攒成多批，实际 %d 次" % len(calls)
    assert len(calls) < 5, (
        "请求数没有少于页数（预翻译没生效？）：%d 次 / 5 页" % len(calls))
    assert max(len(c) for c in calls) <= 20, [len(c) for c in calls]
    assert sum(len(c) for c in calls) == 75, sum(len(c) for c in calls)

    # 跨页攒批：至少有一批包含来自不同页的行
    assert any(len({c.split()[1] for c in batch}) > 1 for batch in calls), \
        "没有任何一批跨页，攒批没生效"

    # 上下文确实传给了翻译层（文档背景 + 术语表字段）
    assert all(isinstance(c, dict) for c in contexts)
    assert any((c.get("doc_context") or "").strip() for c in contexts), \
        "没有把文档背景传给翻译层"
    assert any("glossary" in c for c in contexts)

    # 写出的缓存必须能被上游直接命中：detail 里应显示「缓存复用 75 行」
    assert "缓存复用 75 行" in res["detail"], res["detail"]
    assert "预翻译" in res["detail"], res["detail"]

    # 再跑一次：应当全部命中缓存，一次请求都不发
    calls2 = []
    res2 = mod.run(src, os.path.join(d, "many_out2.pdf"),
                   translate=_fake(calls2),
                   options={"cache_dir": os.path.join(d, "cache")})
    assert not calls2, "第二次仍发了 %d 次请求（缓存没被复用）" % len(calls2)
    assert "命中缓存 75 行" in res2["detail"], res2["detail"]
    print("    [prefetch] 第一次 %d 批（75 行 / 5 页），第二次 %d 批；%s"
          % (len(calls), len(calls2), res2["detail"][-60:]))


def test_shared_cache_reuses_across_different_task_paths():
    """跨任务共享缓存：**同一份文件在不同路径下**也要命中同一份译文。

    每个任务的上传目录都不同，所以逐任务缓存（按路径哈希）永远命不中；
    共享缓存改用**文件内容哈希**，同一本书换参数重跑、或多人传同一份文件时
    已经翻过的行零成本复用——这是最省钱的一条。
    """
    d = _case_dir("shared")
    a_dir, b_dir = os.path.join(d, "task_a"), os.path.join(d, "task_b")
    os.makedirs(a_dir); os.makedirs(b_dir)
    src_a = os.path.join(a_dir, "book.pdf")
    src_b = os.path.join(b_dir, "book.pdf")      # 同名不同路径：路径哈希必然不同
    _build_sample(src_a)
    import shutil
    shutil.copyfile(src_a, src_b)                # 内容完全一致

    shared = os.path.join(d, "shared_cache")
    os.environ["DOCBRIDGE_SHARED_CACHE"] = shared
    try:
        calls_a, calls_b = [], []
        res_a = mod.run(src_a, os.path.join(a_dir, "out.pdf"), translate=_fake(calls_a),
                        options={"target_lang": "zh-Hans"})
        res_b = mod.run(src_b, os.path.join(b_dir, "out.pdf"), translate=_fake(calls_b),
                        options={"target_lang": "zh-Hans"})
    finally:
        os.environ.pop("DOCBRIDGE_SHARED_CACHE", None)

    assert calls_a, "第一次没有调用 translate"
    assert not calls_b, "第二次仍调用 %d 批（共享缓存没生效）" % len(calls_b)
    assert "命中缓存" in res_b["detail"], res_b["detail"]

    # 换模型标识则不应命中旧译文（否则会把 A 模型的译文当成 B 的）
    calls_c = []
    res_c = mod.run(src_a, os.path.join(a_dir, "out2.pdf"), translate=_fake(calls_c),
                    options={"target_lang": "zh-Hans", "model_tag": "another-model"})
    os.environ["DOCBRIDGE_SHARED_CACHE"] = shared
    try:
        calls_c = []
        res_c = mod.run(src_a, os.path.join(a_dir, "out2.pdf"), translate=_fake(calls_c),
                        options={"target_lang": "zh-Hans", "model_tag": "another-model"})
    finally:
        os.environ.pop("DOCBRIDGE_SHARED_CACHE", None)
    assert calls_c, "换了模型标识却命中了旧缓存（会串模型）"
    print("    [shared] 独立路径复用：第一次 %d 批 / 第二次 %d 批；换模型后重新翻译 %d 批"
          % (len(calls_a), len(calls_b), len(calls_c)))


def test_glossary_change_invalidates_shared_cache():
    """改了术语表，共享缓存必须失效（否则术语表静默不生效）。

    这是"看起来成功、其实是旧译文"的隐蔽错误，所以单独钉住：
      * 同样输入 + 同样模型 + 同样语言对，仅术语表不同 → 必须重新翻译。
    """
    d = _case_dir("fingerprint")
    src = os.path.join(d, "book.pdf")
    _build_sample(src)
    shared = os.path.join(d, "shared")
    gl1 = os.path.join(d, "gloss1.json")
    gl2 = os.path.join(d, "gloss2.json")
    json.dump({"firm": "企业"}, open(gl1, "w", encoding="utf-8"))
    json.dump({"firm": "厂商"}, open(gl2, "w", encoding="utf-8"))

    os.environ["DOCBRIDGE_SHARED_CACHE"] = shared
    try:
        calls1, calls2, calls3 = [], [], []
        mod.run(src, os.path.join(d, "o1.pdf"), translate=_fake(calls1),
                options={"glossary_path": gl1})
        # 同一术语表再跑：应全部命中缓存
        mod.run(src, os.path.join(d, "o2.pdf"), translate=_fake(calls2),
                options={"glossary_path": gl1})
        # 换了术语表：必须重新翻译
        mod.run(src, os.path.join(d, "o3.pdf"), translate=_fake(calls3),
                options={"glossary_path": gl2})
    finally:
        os.environ.pop("DOCBRIDGE_SHARED_CACHE", None)

    assert calls1, "第一次没有调用 translate"
    assert not calls2, "同一术语表重跑仍调用 %d 批（缓存没命中）" % len(calls2)
    assert calls3, "换了术语表却命中旧缓存 —— 术语表会静默失效！"
    print("    [fingerprint] 首次 %d 批 / 同表重跑 %d 批 / 换表 %d 批"
          % (len(calls1), len(calls2), len(calls3)))


def test_scanned_pdf_points_to_ocr_mode():
    """把扫描件丢给「原位」模式时，必须给出明确指引，而不是"成功但没翻"。

    扫描件整页是图片、没有文本层。旧行为：输出一份没翻过的文件、状态显示完成 ——
    用户看到"成功"却内容没变，比报错更让人困惑。
    """
    d = _case_dir("scanned_guard")
    src = os.path.join(d, "scanned.pdf")
    # 造一份真扫描件：把有文字的页栅格化成纯图片 PDF
    tmp = fitz.open()
    p = tmp.new_page(width=595, height=842)
    p.insert_text((60, 100), "This page has text on it originally.", fontsize=14)
    pix = p.get_pixmap(dpi=150)
    img = pix.tobytes("png")
    tmp.close()
    out_doc = fitz.open()
    op = out_doc.new_page(width=595, height=842)
    op.insert_image(op.rect, stream=img)
    out_doc.save(src)
    out_doc.close()
    with fitz.open(src) as chk:
        assert not chk[0].get_text().strip(), "样例没造成扫描件"

    calls = []
    err = None
    try:
        mod.run(src, os.path.join(d, "o.pdf"), translate=_fake(calls))
    except RuntimeError as exc:
        err = str(exc)
    assert err, "扫描件没有报错（会静默输出未翻译的文件）"
    assert "OCR" in err and "扫描" in err, err
    assert not calls, "扫描件不该调用翻译"
    print("    [scanned] 已正确引导到 OCR 模式：%s" % err[:60])


def test_progress_reaches_total_despite_upstream_page_numbering():
    """进度必须能走到 total/total，且在收尾阶段有提示。

    回归用例：上游用 0 基索引做 doc[pno-1]，**最后一页是以 pno=0 处理的**；
    照页号上报的话进度条会永远停在 (总页数-1)/总页数（线上真实反馈：125 页卡在 124/125）。
    另外字体子集化对大文件很慢，那段时间必须给提示，不能一声不吭。
    """
    d = _case_dir("progress_total")
    src = _sample_pdf()
    out = os.path.join(d, "out.pdf")
    seen = []
    mod.run(src, out, translate=_fake(), progress=lambda dn, tt, note="": seen.append((dn, tt, note)))

    totals = [t for _dn, t, _n in seen]
    final_done = max(dn for dn, _t, _n in seen)
    assert final_done == max(totals), (final_done, totals)
    assert seen[-1][0] == seen[-1][1], "最后一次进度没有到 total/total：%r" % (seen[-1],)
    notes = " ".join(n for _d, _t, n in seen)
    assert "字体" in notes, "收尾阶段（字体子集化）没有任何进度提示：%r" % notes[-200:]
    print("    [progress] 末次 %r；收尾提示已出现" % (seen[-1],))


if __name__ == "__main__":
    sys.exit(main())
