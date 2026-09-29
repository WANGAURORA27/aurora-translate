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


# ---------------------------------------------- 换行合并 / 分配 / 去重（纯函数）

def _entry(i, text, x0=72.0, y0=100.0, x1=400.0, h=13.0, size=11.0):
    """构造一个"行 entry"（与 pdf_inplace._merge_units 的输入同构）。"""
    return {"i": i, "text": text, "size": size,
            "rect": fitz.Rect(x0, y0, x1, y0 + h)}


def test_merge_units_joins_only_obvious_continuations():
    """合并判据必须是"保守"的：漏合并只是退回旧行为，误合并会毁掉整段译文。

    合并需要同时满足：上一行不以句末标点结尾、下一行小写开头（或以续行标点开头）、
    不是列表项、同列、行距正常、字号接近。
    """
    # ① 连字符断词 + 小写续行 → 合并，并且还原连字符（pub- + lic → public）
    assert mod._merge_units([_entry(0, "The pub-", y0=100),
                             _entry(1, "lic policy is strict.", y0=116)]) == [[0, 1]]
    assert mod._unit_text([_entry(0, "The pub-"), _entry(1, "lic policy is strict.")]) \
        == "The public policy is strict."
    # 无连字符时用空格拼接
    assert mod._unit_text([_entry(0, "one sentence here"), _entry(1, "and its tail.")]) \
        == "one sentence here and its tail."

    # ② 上一行以句末标点结尾 → 不合并
    assert mod._merge_units([_entry(0, "Ends with a period.", y0=100),
                             _entry(1, "lower case but a new sentence", y0=116)]) \
        == [[0], [1]]
    # ③ 下一行大写开头 → 不合并（新句子 / 新标题）
    assert mod._merge_units([_entry(0, "continues without punctuation", y0=100),
                             _entry(1, "Upper case starts a new one", y0=116)]) \
        == [[0], [1]]
    # ④ 下一行是列表项/编号 → 不合并
    assert mod._merge_units([_entry(0, "no punctuation at the end", y0=100),
                             _entry(1, "1. numbered item", y0=116)]) == [[0], [1]]
    assert mod._merge_units([_entry(0, "no punctuation at the end", y0=100),
                             _entry(1, "\u2022 bullet item", y0=116)]) == [[0], [1]]
    # ⑤ 行距过大（>= 段间距）→ 不合并
    assert mod._merge_units([_entry(0, "wrapped line without punctuation", y0=100),
                             _entry(1, "next paragraph starts lower", y0=130)]) \
        == [[0], [1]]
    # ⑥ 字号突变（标题/图注）→ 不合并
    assert mod._merge_units([_entry(0, "wrapped line without punctuation", y0=100),
                             _entry(1, "caption in another size", y0=116, size=16.0)]) \
        == [[0], [1]]
    # ⑦ 不同列（左缘差 >15pt）→ 不合并
    assert mod._merge_units([_entry(0, "left column line without punct", x0=72, y0=100),
                             _entry(1, "right column continues here", x0=320, y0=116)]) \
        == [[0], [1]]
    # ⑧ 三行连续（都不以句末标点结尾）→ 合成一个单元
    assert mod._merge_units([_entry(0, "a line that does not end", y0=100),
                             _entry(1, "with punctuation and continues", y0=116),
                             _entry(2, "into a third line.", y0=132)]) == [[0, 1, 2]]
    # ⑨ 单元按最小行号排序：批内顺序与上游逐行口径一致
    assert mod._merge_units([_entry(0, "single one.", y0=100),
                             _entry(1, "single two.", y0=116),
                             _entry(2, "wrapped head", y0=132),
                             _entry(3, "and its tail.", y0=148)]) == [[0], [1], [2, 3]]
    print("    [合并] 9 组判据全部符合预期")


def test_split_translation_uses_width_ratio_and_never_duplicates():
    """译文按行盒宽度比例分配；分配不了时也**绝不整句重复**（用户最刺眼的问题）。"""
    zh = "这一章介绍了消费者行为的核心概念与边际效用的变化规律"
    parts = mod._split_translation(zh, [300.0, 150.0, 50.0])
    assert len(parts) == 3 and all(parts), parts
    assert "".join(parts) == zh, parts                 # 不丢字
    assert parts[0] not in (zh,) and parts[1] != zh, parts   # 不整句重复
    assert len(parts[0]) > len(parts[2]), parts        # 宽的行分到更多字

    # 译文比行数还短：整句只放最宽的那一行，其余留空（调用方只覆盖原文）
    assert mod._split_translation("短", [100.0, 300.0, 120.0]) == ["", "短", ""]
    # 单行单元原样返回
    assert mod._split_translation("只有一行", [200.0]) == ["只有一行"]

    # 切点不落在拉丁单词中间（机器学习 + Python语言，而不是 机器学习P + ython语言）
    assert mod._split_translation("机器学习Python语言", [160.0, 100.0]) \
        == ["机器学习", "Python语言"], mod._split_translation("机器学习Python语言", [160.0, 100.0])
    print("    [分配] 比例/保底/不重复/不断词 全部符合预期")


def test_dedupe_merges_similar_adjacent_translations_only():
    """去重只打"相邻且高度相似"的译文；过短的与不相邻的正常重复必须保留。"""
    a = "这一章介绍了消费者行为的核心概念与研究方法"
    b = "这一章介绍了消费者行为的核心概念与分析方法"       # 与 a 高度相似
    c = "价格机制协调买卖双方的决策"
    entries = [_entry(0, "x", y0=100), _entry(1, "y", y0=116),
               _entry(2, "z", y0=132)]
    trans = {0: a, 1: b, 2: c}
    assert mod._dedupe_page(mod._line_columns(entries), trans) == {1}
    assert trans[1] == mod.COVER_ONLY, trans

    # 相似的两句之间夹了一句不相干的（不相邻）→ 不去重
    trans2 = {0: a, 1: c, 2: b}
    assert mod._dedupe_page(mod._line_columns(entries), trans2) == set()

    # 过短的句子（<6 字）不去重：可能是正常的重复强调
    short_entries = [_entry(0, "x", y0=100), _entry(1, "y", y0=116)]
    trans3 = {0: "是", 1: "是"}
    assert mod._dedupe_page(mod._line_columns(short_entries), trans3) == set()

    # 相似度确实是字符二元组 Jaccard：完全不同 → 不删
    assert not mod._similar("消费者行为研究", "宏观经济政策分析")
    assert mod._similar(a, b)
    print("    [去重] 相邻相似才合并、短的/不相邻的不动")


# ---------------------------------------------- 版面几何（端到端 + 真实 bbox 断言）

CJK_SPAN_RE = __import__("re").compile(r"[\u4e00-\u9fff]")


def _build_wrapped_sample(path):
    """造一页"真实排版"的样例：跨行段落 + 不需要翻译的纯数字行 + 超长行。"""
    lines = [
        "This paragraph is wrapped across several physical lines because the",
        "typesetter broke it where the column ended, so each line is only a",
        "fragment of one sentence.",
        "The second paragraph also wraps here and continues on the next line",
        "without any terminal punctuation at the break point.",
        "12345",
        "A very long single line of English text that will need a much longer Chinese translation than usual.",
    ]
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    y = 90
    for text in lines:
        page.insert_text((72, y), text, fontname="tiro", fontsize=11)
        y += 17
    doc.save(path, garbage=4, deflate=True)
    doc.close()
    return path, lines


def _fake_long(calls=None, ratio=0.8):
    """假翻译器：产出**故意比英文更长**的中文（不联网、不花钱）。

    每个汉字由源文本字符确定性地映射而来：既能保证"逐行不同"（否则会被
    新加的碎片去重正确地删掉，掩盖版面问题），又保证长度 = 源长度 × ratio，
    于是中文宽度约为英文的 1.5 倍 —— 逼着排版走自适应缩字号。
    """
    def fake(texts, ctx=None):
        if calls is not None:
            calls.append(list(texts))
        out = []
        for i, t in enumerate(texts):
            n = max(3, int(round(len(t) * ratio)))
            mapped = "".join(chr(0x4e00 + (ord(c) * 7 + 13) % 1200) for c in t)
            out.append("第%d段" % (i + 1) + mapped[:n])
        return out
    return fake


def _cjk_visual_lines(path, page_no=0):
    """把译文（含 CJK 的 span）聚成视觉行，返回**墨迹盒**。

    PyMuPDF 给的 CJK span bbox 用的是字体 ascent/descent（实测 1.4em 高），
    比真实墨迹（约 1.04em）大得多，直接拿来判相交会把正常行距判成重叠。
    所以这里按"基线 ± 实测墨迹比例"重算：上方 0.88em、下方 0.16em。

    同一基线上的 span 可能是表格不同单元格（各自一条），所以在组内按 x 间距切开
    （间距 > 0.6 倍字号即不同单元格），不能整行合并。
    """
    spans = []
    with fitz.open(path) as doc:
        page = doc[page_no]
        for block in page.get_text("dict")["blocks"]:
            if block.get("type", 0) != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    if CJK_SPAN_RE.search(span["text"]):
                        spans.append((span["origin"][1], span["bbox"][0],
                                      span["bbox"][2], span["size"], span["text"]))
    spans.sort(key=lambda s: (round(s[0], 1), s[1]))
    rows = []
    for base, x0, x1, size, text in spans:
        if (rows and abs(rows[-1]["base"] - base) <= 0.6
                and x0 - rows[-1]["x1"] <= 0.6 * size):
            row = rows[-1]
            row["x1"] = max(row["x1"], x1)
            row["size"] = max(row["size"], size)
            row["text"] += text
            continue
        rows.append({"x0": x0, "x1": x1, "base": base, "size": size, "text": text})
    for row in rows:
        row["ink_top"] = row["base"] - 0.88 * row["size"]
        row["ink_bottom"] = row["base"] + 0.16 * row["size"]
    return rows


def test_merged_paragraph_is_translated_once_then_split_back():
    """端到端①：换行段落只翻一次（合并），译文按行分配，不出现整句重复。"""
    d = _case_dir("merge_e2e")
    src = os.path.join(d, "wrapped.pdf")
    _build_wrapped_sample(src)
    calls = []
    mod.run(src, os.path.join(d, "out.pdf"), translate=_fake_long(calls),
            options={"shared_cache": False})

    # 三行一段被合并成一个单元：一次调用里同时出现段首与段尾
    merged = [c for c in calls
              if any("This paragraph is wrapped" in t and "fragment of one sentence." in t
                     for t in c)]
    assert merged, "跨行段落没有被合并成一个翻译单元：%r" % (calls,)
    flat = [t for c in calls for t in c]
    assert not any("fragment of one sentence." in t and "This paragraph" not in t
                   for t in flat), "段尾行被单独翻译了（没有合并）"

    lines = _cjk_visual_lines(os.path.join(d, "out.pdf"))
    texts = [ln["text"] for ln in lines]
    assert len(texts) >= 4, texts
    # 没有任何两行拿到完全相同的译文（那就是用户看到的"同一句翻两遍"）
    assert len(set(texts)) == len(texts), texts
    print("    [合并] 一次调用覆盖整段；输出 %d 行译文，无整句重复" % len(texts))


def test_drawn_translations_fit_width_and_never_overlap():
    """端到端②（几何硬约束）：故意更长的中文译文

    ① 每行译文都在页面内、且不超出它所属原行的 x 范围（允许小容差）；
    ② 同页任意两行译文的墨迹盒互不相交；
    ③ 字号不低于设定下限（原行字号的 60%）；
    ④ 不需要翻译的原行（纯数字）没有被译文压住。
    """
    d = _case_dir("geometry")
    src = os.path.join(d, "wrapped.pdf")
    _lines = _build_wrapped_sample(src)
    out = os.path.join(d, "out.pdf")
    res = mod.run(src, out, translate=_fake_long(), options={"shared_cache": False})

    with fitz.open(src) as doc:
        page = doc[0]
        originals = []
        for block in page.get_text("dict")["blocks"]:
            if block.get("type", 0) != 0:
                continue
            for line in block.get("lines", []):
                text = "".join(s["text"] for s in line["spans"]).strip()
                if text:
                    originals.append({"rect": fitz.Rect(line["bbox"]), "text": text,
                                      "size": max(s["size"] for s in line["spans"]),
                                      "base": max(s["origin"][1] for s in line["spans"])})

    def match_original(box):
        """归行：返回可能归属的原行候选。

        译文折行时首行会被基线钳制整体上移，"基线最近"不可靠；判断"有没有超出原行
        x 范围"的可靠口径是：同栏、基线相近的候选里**最宽的那一行能不能装下**。
        """
        return [o for o in originals
                if abs(o["base"] - box["base"]) <= 1.6 * max(o["size"], box["size"])
                and o["rect"].x0 <= box["x0"] + 1.0]

    boxes = _cjk_visual_lines(out)
    assert boxes, "没有画出任何译文"
    with fitz.open(out) as doc:
        out_page = doc[0]
        page_rect = out_page.rect

    shrank = 0
    for box in boxes:
        # ① 页面内
        assert box["x0"] >= page_rect.x0 - 0.5, box
        assert box["x1"] <= page_rect.x1 + 0.5, box
        cands = match_original(box)
        assert cands, ("译文附近找不到任何原行", box)
        widest = max(cands, key=lambda o: o["rect"].x1)
        nearest = min(cands, key=lambda o: abs(o["base"] - box["base"]))
        # ① 不超原行 x 范围（容差 2pt：中文禁则允许行尾闭标点略微出界）
        assert box["x0"] >= widest["rect"].x0 - 1.0, (box, widest)
        assert box["x1"] <= widest["rect"].x1 + 2.0, (box, widest)
        # ③ 字号下限 60%（同段字号一致，按基线最近的原行比）
        assert box["size"] >= 0.6 * nearest["size"] - 0.51, (box, nearest)
        if box["size"] < nearest["size"] * 0.92 - 0.01:
            shrank += 1
    assert shrank > 0, "样例没有触发自适应缩字号，测试没覆盖到目标路径"

    # ② 两两不相交
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            a, b = boxes[i], boxes[j]
            assert (a["x1"] <= b["x0"] or b["x1"] <= a["x0"]
                    or a["ink_bottom"] <= b["ink_top"] or b["ink_bottom"] <= a["ink_top"]), \
                "两行译文墨迹相交：\n%r\n%r" % (a, b)

    # ④ 纯数字行没被覆盖、也没被压到
    digit = next(o for o in originals if o["text"] == "12345")
    with fitz.open(out) as doc:
        text = doc[0].get_text()
    assert "12345" in text, "纯数字行被覆盖了"
    for box in boxes:
        assert not (box["x0"] < digit["rect"].x1 and digit["rect"].x0 < box["x1"]
                    and box["ink_top"] < digit["rect"].y1
                    and digit["rect"].y0 < box["ink_bottom"]), \
            ("译文压住了不需要翻译的行：%r vs %r" % (box, digit))
    print("    [几何] %d 行译文：不超行宽、两两不相交、字号 >= 60%%、纯数字行未被压"
          % len(boxes))
    assert res["pages"] == 1


def test_wrapped_translation_never_presses_the_next_paragraph():
    """回归：**跨段落**的相邻行之间也必须留出空白。

    上游 group_paragraphs 会按行距把一段切成多个"段落"（dy>6pt 就切）。只跟
    "同段下一行"比较的话，跨段的下一行就管不住了 —— 实测在一份 PPT 版式的真实
    PDF 上，译文折行后整体上移 18pt 压住了上一行（几何脚本抓到的真问题）。
    """
    d = _case_dir("crosspara")
    src = os.path.join(d, "crosspara.pdf")
    lines = [
        "First paragraph first line that keeps going without any stop",
        "first paragraph second line ends here.",
        "Second paragraph first line also fairly long and keeps going",
        "second paragraph second line ends here.",
    ]
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    y = 100
    for text in lines:
        page.insert_text((72, y), text, fontname="tiro", fontsize=11)
        y += 22                       # 行距 22 → dy≈7.5 > 6，group_paragraphs 会切成两段
    doc.save(src, garbage=4, deflate=True)
    doc.close()

    out = os.path.join(d, "out.pdf")
    mod.run(src, out, translate=_fake_long(), options={"shared_cache": False})
    boxes = _cjk_visual_lines(out)

    with fitz.open(src) as doc:
        originals = [{"rect": fitz.Rect(l["bbox"]),
                      "text": "".join(s["text"] for s in l["spans"]).strip()}
                     for b in doc[0].get_text("dict")["blocks"] if b.get("type", 0) == 0
                     for l in b.get("lines", [])]
        originals = [o for o in originals if o["text"]]

    # ② 两两不相交（旧实现在这里会挂：折行后的译文压住上一段）
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            a, b = boxes[i], boxes[j]
            assert (a["x1"] <= b["x0"] or b["x1"] <= a["x0"]
                    or a["ink_bottom"] <= b["ink_top"]
                    or b["ink_bottom"] <= a["ink_top"]), \
                "两行译文墨迹相交：\n%r\n%r" % (a, b)
    # 每一条译文都不许压到**其它原行**的矩形
    for box in boxes:
        for orig in originals:
            if abs(box["base"] - (orig["rect"].y0 + orig["rect"].y1) / 2) < 20:
                continue              # 跳过自己的原行（基线附近）
            assert not (box["x0"] < orig["rect"].x1 and orig["rect"].x0 < box["x1"]
                        and box["ink_top"] < orig["rect"].y1
                        and orig["rect"].y0 < box["ink_bottom"]), \
                ("译文压到了别的原行：%r vs %r" % (box, orig))
    print("    [跨段] %d 行译文，两两不相交、未压到其它原行" % len(boxes))


# ---------------------------------------------- 字体容错（线上真实崩溃回归）

def _build_f1_collision_sample(path):
    """造一份「自带 /F1 且该字体没有内嵌文件」的 PDF —— PowerPoint 导出的真实特征。

    线上那份 Ch3&4 Cases 的每一页都长这样：Resources 里有 /F1 = Arial-BoldMT
    但没有 FontFile。旧代码把缺字回退字体注册成 fontname="F1"，PyMuPDF 见名复用
    源 PDF 的这个坏字体 → get_char_widths → 'NoneType' has no attribute 'm_internal'。
    """
    doc = fitz.open()
    page = doc.new_page(width=420, height=260)
    page.insert_text((30, 50), "Chapter 3 Case Study of Market Structure",
                     fontname="helv", fontsize=13)
    font_xref = doc.get_new_xref()
    doc.update_object(
        font_xref,
        "<< /Type /Font /Subtype /TrueType /BaseFont /Arial-BoldMT "
        "/Encoding /WinAnsiEncoding >>")
    res = doc.get_new_xref()
    doc.update_object(res, "<< >>")
    doc.xref_set_key(res, "Font", "<< >>")
    doc.xref_set_key(res, "Font/F1", "%d 0 R" % font_xref)
    doc.xref_set_key(page.xref, "Resources", "%d 0 R" % res)
    doc.save(path, garbage=4, deflate=True)
    doc.close()
    return path


def test_source_pdf_font_name_collision_does_not_crash():
    """回归：源 PDF 自带 /F1（无内嵌文件）时，整条原位翻译必须跑完而不是崩掉。"""
    d = _case_dir("fontcollision")
    src = os.path.join(d, "slide.pdf")
    _build_f1_collision_sample(src)
    with fitz.open(src) as doc:
        names = {f[4] for f in doc[0].get_fonts(full=True)}
    assert "F1" in names, "样例没造出 /F1：%r" % names

    # 主字体缺字形的字符（∂ ∆ ⊂ 之类）才会走"缺字回退字体"分支 —— 正是崩的那条
    probe = next((ch for ch in "\u2202\u2206\u2282\u2209\u21d2"
                  if mod.pt._needs_fallback(ch)), None)
    assert probe, "没有找到主字体缺失的字符，无法覆盖回退字体分支"

    def fake(texts, ctx=None):
        return ["【译%s】%s" % (probe, t) for t in texts]

    out = os.path.join(d, "out.pdf")
    mod.pt.reset_draw_failures()
    res = mod.run(src, out, translate=fake, options={"shared_cache": False})
    assert os.path.isfile(out) and res["pages"] == 1
    assert mod.pt.draw_failures()["chars"] == 0, mod.pt.draw_failures()

    with fitz.open(out) as doc:
        names = {f[4] for f in doc[0].get_fonts(full=True)}
    # 我们注册的字体必须是**自己的名字**（DSH*），而不是复用源 PDF 的 /F1
    assert any(n.startswith("DSH") for n in names), names
    print("    [字体] 源 PDF 的 /F1 未被复用；输出字体资源 %r" % sorted(names))


def test_unusable_font_candidates_degrade_gracefully():
    """候选字体全部不可用（不存在 / 取不到字宽）→ 退回内置字体，不崩、可上报。"""
    d = _case_dir("fontdegrade")
    src = _sample_pdf()
    out = os.path.join(d, "out.pdf")
    pt = mod.pt
    saved = (pt.FONT_CANDIDATES, pt.FALLBACK_CANDIDATES, pt.font_drawable,
             os.environ.pop("DOCBRIDGE_PDF_FONT", None))
    try:
        pt.FONT_CANDIDATES = ["/nonexistent/a.ttf", "/nonexistent/b.otf"]
        pt.FALLBACK_CANDIDATES = ["/nonexistent/math.ttf"]
        pt.font_drawable = lambda _p: False       # 模拟"能加载但取不到字宽"
        res = mod.run(src, out, translate=_fake(),
                      options={"shared_cache": False, "font": "/nonexistent/x.ttf"})
    finally:
        (pt.FONT_CANDIDATES, pt.FALLBACK_CANDIDATES, pt.font_drawable,
         env) = saved
        if env is not None:
            os.environ["DOCBRIDGE_PDF_FONT"] = env
    assert os.path.isfile(out), "优雅降级失败：没有产出文件"
    assert "china-s" in res["detail"], res["detail"]
    assert mod.pt.CJK_FONTFILE is None, mod.pt.CJK_FONTFILE
    print("    [降级] 候选全不可用 -> 内置字体，任务仍完成：%s" % res["detail"][:60])


def test_draw_failure_is_counted_not_raised():
    """单个字符画不出来时只计数、继续画（最坏是个别字缺，不是整本书白翻）。"""
    class _BoomPage:
        def insert_text(self, *a, **kw):
            raise RuntimeError("模拟字体取不到字宽")

    mod.pt.reset_draw_failures()
    assert mod.pt._insert_char(_BoomPage(), fitz.Point(20, 50), "中", 12,
                              (0, 0, 0), 0, 0.0) is False
    assert mod.pt.draw_failures()["chars"] == 1, mod.pt.draw_failures()
    # 整串失败 → 逐字符重试 → 仍然不抛。计数按**字符数**：
    #   1（单字 "中"）+ 2（整串 "中文"）+ 1 + 1（逐字重试）= 5
    mod.pt._draw_text(_BoomPage(), fitz.Point(20, 50), "中文", 12, (0, 0, 0))
    assert mod.pt.draw_failures()["chars"] == 5, mod.pt.draw_failures()
    assert mod.pt.draw_failures()["errors"], "没有记录失败原因"
    mod.pt.reset_draw_failures()
    assert mod.pt.draw_failures()["chars"] == 0
    print("    [计数] 画不出来的字被计数并继续：%r" % (mod.pt.draw_failures(),))


def _load_fonts_module():
    """按路径加载 pipelines/fonts.py（与管线同样的加载方式，避免包上下文问题）。"""
    path = os.path.join(DOCBRIDGE, "pipelines", "fonts.py")
    spec = importlib.util.spec_from_file_location("test_dbg_fonts", path)
    fmod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = fmod
    spec.loader.exec_module(fmod)
    return fmod


def test_font_candidate_selection_picks_first_drawable():
    """候选按优先级取第一个**真的能画字**的；画不了字的被跳过；全不可用返回空。"""
    fmod = _load_fonts_module()
    good = mod.pt.CJK_FONTFILE or "/System/Library/Fonts/Supplemental/Songti.ttc"
    assert os.path.isfile(good), good
    saved = fmod.CJK_FONT_CANDIDATES
    try:
        fmod.CJK_FONT_CANDIDATES = ("/nonexistent/a.ttf", "/nonexistent/b.otf", good)
        assert fmod.cjk_font_candidates() == [good]
        assert fmod.usable_cjk_fonts(mod.pt.font_drawable) == [good]
        assert fmod.default_pdf_font(mod.pt.font_drawable) == good
        fmod.CJK_FONT_CANDIDATES = ("/nonexistent/a.ttf",)
        assert fmod.usable_cjk_fonts(mod.pt.font_drawable) == []
        assert fmod.default_pdf_font(mod.pt.font_drawable) == ""
    finally:
        fmod.CJK_FONT_CANDIDATES = saved
    print("    [字体候选] 按优先级 + 真画字校验 + 全缺失返回空")


def test_cjk_candidates_must_contain_han_characters():
    """候选清单里每个可用的字体都必须**含汉字**。

    DejaVu / Liberation 之类只有拉丁与符号的字体绝不能混进 CJK 候选
    （否则整份中文会被画成空白/缺字框）。这条断言就是防这个。
    """
    fmod = _load_fonts_module()
    checked = 0
    for path in fmod.cjk_font_candidates():
        if not mod.pt.font_drawable(path):
            continue
        font = fitz.Font(fontfile=path)
        assert font.has_glyph(ord("中")), "%s 不含汉字，不能当 CJK 字体" % path
        checked += 1
    assert checked >= 1, "本机一个可用的中文字体都没有（候选清单：%r）" % (
        fmod.cjk_font_candidates(),)
    print("    [字体候选] %d 个候选字体均含汉字" % checked)


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
