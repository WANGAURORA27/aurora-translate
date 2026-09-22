# -*- coding: utf-8 -*-
"""docbridge 管线 · PDF 双语对照（FORMAT="pdf" / MODE="bilingual"）。

输出与输入**页数严格 1:1**：每一源页在输出里新生成一页，
原文那一半用 ``page.show_pdf_page(rect, src_doc, pno)`` 把源页作为
Form XObject **矢量嵌入**（不是截图：清晰、体积小、文字仍可选中），
译文那一半用上层 ``pdf_translate.py`` 的排版函数重排中文。

两种版式（``options["bilingual_layout"]``）：

* ``"side"``（默认）—— 横向 A4，左半页原文、右半页译文；
* ``"stack"`` —— 纵向，上半页原文、下半页译文（页高按内容自动加高）。

设计约束（见 docbridge/CONTRACT.md）：

* 只通过 ``translate`` 回调与外界交互，不联网、不读密钥、不改源文件；
* 攒批调用 translate（单批 ≤ ``BATCH_MAX``=100 条，尽量凑到 ≥30 条再发车），
  返回数量不符立刻 ``RuntimeError("翻译返回数量不符：期望 N，实际 M")``；
* ``out_path + ".part"`` 原子落盘，成功后 ``os.replace``；
* 不写任何模块级可变状态（Flask 多线程调用），所有状态都在函数局部。

已知限制（诚实说明）：``show_pdf_page`` 搬运的是源页**原始坐标下的内容**，
不跟随源页 /Rotate。0°/180° 能按阅读器里的显示方向嵌入；90°/270° 的旋转页
改为按内容方向整页嵌入（保证内容不被裁掉，但原文那一半与阅读器里看到的
方向差 90°），这类页会记入返回值的 ``rotated_pages`` 并在 ``detail`` 里说明。

复用的上层函数（import，不抄）：直接调用 ``init_cjk_font`` / ``text_width`` /
``collect_lines`` / ``group_paragraphs`` / ``layout`` / ``draw_translation``；
禁则处理（``fix_kinsoku``）、字号拟合（``fit_size``）、缺字回退字体
（``_needs_fallback``）由 ``wrap_text`` / ``draw_translation`` 内部自动带上。
"""

from __future__ import annotations

import os
import sys

import fitz

# ---------------------------------------------------------------- 导入上层 CLI
# 查找顺序：DOCBRIDGE_PDF_TRANSLATE_DIR > 包目录 docbridge/ > 其父目录。
# 有了环境变量与包目录兜底，部署时把 pdf_translate.py 放进 docbridge/ 即可，
# 不再强依赖「docbridge 与 pdf_translate.py 同处一个父目录」这种本地布局。
_CANDIDATES = [
    os.environ.get("DOCBRIDGE_PDF_TRANSLATE_DIR") or "",
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),                   # docbridge/
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),  # 父目录
]
_ROOT = next((d for d in _CANDIDATES
              if d and os.path.isfile(os.path.join(d, "pdf_translate.py"))),
             _CANDIDATES[1])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:  # pragma: no cover - 环境缺失时给出中文提示
    import pdf_translate as pt
except Exception as _exc:  # pragma: no cover
    raise ImportError(
        "无法导入 pdf_translate.py（已尝试："
        + "、".join(d for d in _CANDIDATES if d)
        + "；可用环境变量 DOCBRIDGE_PDF_TRANSLATE_DIR 指定目录）："
        + str(_exc)
    ) from _exc


def _system_cjk_font() -> str:
    """返回系统里可用的中文字体路径（候选清单见 pipelines/fonts.py）。

    与本包内相对导入的区别：按**文件路径**加载，因此即使本模块被测试用
    importlib 直接按文件加载（没有包上下文）也能正常工作。
    """
    try:
        import importlib.util
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts.py")
        spec = importlib.util.spec_from_file_location("docbridge_pipeline_fonts", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.default_pdf_font()
    except Exception:                    # noqa: BLE001
        env = (os.environ.get("DOCBRIDGE_PDF_FONT") or "").strip()
        return env if env and os.path.isfile(env) else ""


# ---------------------------------------------------------------- 契约元数据
FORMAT = "pdf"
MODE = "bilingual"
LABEL = "双语对照"
NOTE = ("每页原页矢量嵌入在一侧，另一侧排版中文译文；页数与原文件一致，"
        "原页上的图片/表格/线条原样保留。")
OPTIONS = ["bilingual_layout", "font", "font_scale", "sim_bold",
           "source_lang", "target_lang"]


# ---------------------------------------------------------------- 版面常量
A4_W = 595.0                         # A4 纵向宽度（stack 版式用）
SIDE_W, SIDE_H = 842.0, 595.0        # A4 横向（左右并排）
MARGIN = 30.0                        # 页边距
COL_GAP = 20.0                       # 原文区 / 译文区之间的间隙
LABEL_BAND = 15.0                    # 每个半页顶部的“原文/译文”标签带高度
TR_TOP_PAD = 5.0                     # 译文区上方留白（避免与标签行贴住）
PARA_GAP = 6.5                       # 段落间距
BATCH_MAX = 100                      # 单次 translate 的最大条数
BATCH_MIN_TARGET = 30                # 攒批目标（不足则等下一页一起发）
BATCH_MAX_CHARS = 3500               # 单批最大字符数
HEAD_PT, BODY_PT, SMALL_PT = 14.0, 10.5, 8.5
MIN_PT = 7.0                         # 译文可读下限
SHRINK_FACTORS = (1.0, 0.95, 0.9, 0.85, 0.8, 0.75, 0.7, 0.65, 0.6)
STACK_MAX_TOP = 700.0                # stack 版式里原文区的最大高度
STACK_MAX_BOTTOM = 1800.0            # stack 版式里译文区的最大高度
NOTE_TRUNCATED = "（译文过长，本页部分内容已省略）"
HINT_NO_TEXT = "（本页无可翻译文本）"

_INK = (0.11, 0.11, 0.11)            # 译文正文色
_HEAD_INK = (0.05, 0.05, 0.05)
_GRAY = (0.58, 0.58, 0.58)
_RULE = (0.80, 0.80, 0.80)
_FROM_CALLBACK = "_docbridge_raised_by_translate"   # 回调异常的标记属性名


# ================================================================ 小工具

def _clamp(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


def _as_float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _visible(color):
    """源行颜色过浅（白字/浅底）时，译文改用深色，避免在译文区看不见。"""
    if not isinstance(color, (tuple, list)) or len(color) < 3:
        return _INK
    try:
        r, g, b = float(color[0]), float(color[1]), float(color[2])
    except (TypeError, ValueError):
        return _INK
    return _INK if max(r, g, b) > 0.82 else (r, g, b)


def _fit_rect(src_w, src_h, box):
    """把 src_w × src_h 的矩形按比例缩放居中放进 box（保纵横比）。"""
    if src_w <= 0 or src_h <= 0 or box.width <= 0 or box.height <= 0:
        return fitz.Rect(box)
    s = min(box.width / src_w, box.height / src_h)
    w, h = src_w * s, src_h * s
    x = box.x0 + (box.width - w) / 2.0
    y = box.y0 + (box.height - h) / 2.0
    return fitz.Rect(x, y, x + w, y + h)


def _embed_source(target_page, box, src, pno):
    """把源页矢量嵌入 box（Form XObject，不是截图）。返回 True 表示“未按页面
    /Rotate 的显示方向嵌入”（本函数对 90/270 的已知取舍，见模块 docstring）。

    ``show_pdf_page`` 只搬内容的原始坐标，**不跟随源页的 /Rotate**：
    所以 0/180 直接按显示尺寸取框并透传 rotate；90/270 若仍按显示尺寸取框，
    内容会被 1:1 贴上去并裁掉一半——宁可保留完整内容（横竖方向与阅读器里
    看到的差 90°），也不要把页面裁坏。
    """
    src_page = src[pno]
    rot = int(src_page.rotation) % 360
    if rot in (90, 270):
        cbox = fitz.Rect(src_page.cropbox)
        target_page.show_pdf_page(_fit_rect(cbox.width, cbox.height, box), src, pno)
        return True
    disp = fitz.Rect(src_page.rect)
    target_page.show_pdf_page(_fit_rect(disp.width, disp.height, box), src, pno,
                              rotate=rot)
    return False


def _join_lines(texts):
    """把同一段落的多行文本拼成一段（处理英文行尾连字符）。"""
    out = ""
    for raw in texts:
        t = (raw or "").strip()
        if not t:
            continue
        if not out:
            out = t
            continue
        if out.endswith("-") and not out.endswith("--") and t[:1].islower():
            out = out[:-1] + t          # exam-\nple -> example
        else:
            out += " " + t
    return out


def _count_skipped_lines(page):
    """统计“无需翻译”的文本行数：已是中文 / 纯数字符号。

    与上层 collect_lines 的过滤口径一致（CJK_RE / NUMBERISH_RE 直接复用），
    只是我们额外把这些行**计数**上报给前端。
    """
    n = 0
    try:
        d = page.get_text("dict")
    except Exception:
        return 0
    for block in d.get("blocks", []):
        if block.get("type", 0) != 0:
            continue
        for line in block.get("lines", []):
            text = "".join(s.get("text", "") for s in line.get("spans", [])).strip()
            if not text:
                continue
            if pt.CJK_RE.search(text) or pt.NUMBERISH_RE.match(text):
                n += 1
    return n


# ================================================================ 取段落

def _paragraph_style(avg_size, bold):
    """按源行字号/粗体挑译文基准字号。"""
    if avg_size >= 13.5 or (bold and avg_size >= 11.0):
        return HEAD_PT
    if avg_size >= 8.6:
        return BODY_PT
    return SMALL_PT


def _page_paragraphs(page):
    """取一页的翻译单元（段落），按 group_paragraphs 给出的阅读顺序。

    返回 ``[{"text", "size", "bold", "color"}, ...]``，text 已按行拼接成段。
    """
    lines = pt.collect_lines(page)          # 复用：已过滤中文/纯数字/无拉丁的行
    if not lines:
        return []
    work = [(i, rect, text, size, color, bold, base, specials)
            for i, (rect, text, size, color, bold, rotated, base, specials)
            in enumerate(lines)]
    records = []
    for para in pt.group_paragraphs(work):
        texts = [it[2] for it in para]
        text = _join_lines(texts)
        if not text:
            continue
        sizes = [it[3] for it in para] or [BODY_PT]
        avg = sum(sizes) / float(len(sizes))
        bold = any(bool(it[5]) for it in para)
        color = _visible(para[0][4])
        records.append({
            "text": text,
            "size": _paragraph_style(avg, bold),
            "bold": bold,
            "color": _HEAD_INK if avg >= 13.5 or bold else color,
        })
    return records


# ================================================================ 翻译（攒批）

def _chunk_texts(texts):
    """把译文单元切成 ≤BATCH_MAX 条 / ≤BATCH_MAX_CHARS 字符的批。"""
    chunks = []
    i = 0
    n = len(texts)
    while i < n:
        j = i
        chars = 0
        while j < n and (j - i) < BATCH_MAX:
            ln = len(texts[j])
            if j > i and chars + ln > BATCH_MAX_CHARS:
                break
            chars += ln
            j += 1
        chunks.append((i, j))
        i = j
    return chunks


def _translate_batched(texts, translate, context, pages):
    """攒批调用 translate，逐批校验数量（1:1 顺序，绝不错位）。"""
    out = []
    for i, j in _chunk_texts(texts):
        chunk = list(texts[i:j])
        ctx = dict(context)
        ctx["page"] = pages[0] if pages else None      # 1 基页码（批首所在页）
        ctx["pages"] = list(pages)                     # 跨页批会包含多页
        ctx["count"] = len(chunk)
        try:
            res = translate(chunk, ctx)
        except Exception as exc:
            # 契约：回调自身抛的异常原样向上冒泡（app.py 统一记失败/重试），
            # 这里只打个标记，别被本模块的 RuntimeError 包装吞掉类型。
            try:
                setattr(exc, _FROM_CALLBACK, True)
            except Exception:
                pass
            raise
        if isinstance(res, (str, bytes)) or not isinstance(res, (list, tuple)):
            raise RuntimeError(
                f"翻译返回数量不符：期望 {len(chunk)}，实际 1（回调未返回列表）")
        if len(res) != len(chunk):
            raise RuntimeError(
                f"翻译返回数量不符：期望 {len(chunk)}，实际 {len(res)}")
        for item in res:
            out.append("" if item is None else str(item))
    return out


# ================================================================ 译文排版

def _plan_paragraphs(records, width, avail, base_scale):
    """在 (width × avail) 内排好段落：必要时整体缩字号，再不行就截断并加注。

    返回 ``(items, truncated)``；items 为 ``[{"text","size","color","bold","height"}]``。
    """
    if not records:
        return [], False
    for idx, factor in enumerate(SHRINK_FACTORS):
        last = idx == len(SHRINK_FACTORS) - 1
        sizes, heights = [], []
        for rec in records:
            size = max(MIN_PT, rec["size"] * base_scale * factor)
            _lines, need = pt.layout(rec["text"], size, width)
            sizes.append(size)
            heights.append(need)
        total = sum(heights) + PARA_GAP * (len(records) - 1)
        if total <= avail or last:
            break
    items, used, truncated = [], 0.0, False
    keep = len(records)
    if total > avail:
        truncated = True
        note_h = SMALL_PT * pt.LINE_PITCH_FACTOR + PARA_GAP
        budget = max(0.0, avail - note_h)
        keep, used = 0, 0.0
        for k in range(len(records)):
            add = heights[k] + (PARA_GAP if k else 0.0)
            if used + add > budget:
                break
            used += add
            keep = k + 1
        if keep == 0 and records:      # 连一段都放不下，仍画第一段（宁可溢出一点）
            keep = 1
    for k in range(keep):
        rec = records[k]
        items.append({
            "text": rec["text"], "size": sizes[k], "color": rec["color"],
            "bold": rec["bold"], "height": heights[k], "kind": "para",
        })
    if truncated:
        items.append({"text": NOTE_TRUNCATED, "size": SMALL_PT, "color": _GRAY,
                      "bold": False,
                      "height": SMALL_PT * pt.LINE_PITCH_FACTOR, "kind": "note"})
    return items, truncated


def _draw_items(page, box, items, sim_bold=False):
    """把 plan 出来的段落依次画在 box 里（复用 draw_translation）。"""
    y = box.y0
    for item in items:
        h = item["height"]
        rm = 2 if (sim_bold and item.get("bold") and item["kind"] == "para") else 0
        bw = (0.04 * item["size"]) if rm else 0.0
        # justify=False：段中行按中文习惯两端对齐，段末行保持左对齐
        pt.draw_translation(page, box.x0, y, y + h, item["text"], item["size"],
                            item["color"], box.width, rm, bw, baseline=None,
                            justify=False)
        y += h + PARA_GAP


def _draw_hint(page, box, text, size=None):
    """译文区无内容时的居中灰色提示。"""
    size = size or BODY_PT
    tw = max(1.0, pt.text_width(text, size))
    x = box.x0 + max(0.0, (box.width - tw) / 2.0)
    cy = box.y0 + box.height / 2.0
    pt.draw_translation(page, x, cy - size, cy + size, text, size, _GRAY,
                        tw + 0.5, 0, 0.0, baseline=None, justify=False)


def _draw_label(page, box, text):
    """半页顶部的小灰标签，如「原文 · 第 3 页」。"""
    size = 8.5
    tw = pt.text_width(text, size)
    x = box.x0 + max(0.0, (box.width - tw) / 2.0)
    y = box.y0 + LABEL_BAND - 4.0
    pt.draw_translation(page, x, y - size, y + size * 0.3, text, size, _GRAY,
                        tw + 0.5, 0, 0.0, baseline=None, justify=False)


# ================================================================ 单页生成

def _emit_page(out_doc, src, pno, records, translations, *, layout_mode,
               base_scale, sim_bold):
    """生成输出的第 pno 页：嵌入源页 + 排译文。

    返回 ``(truncated, drawn, dropped, rotated_src)``。
    """
    src_page = src[pno]
    src_rect = fitz.Rect(src_page.rect)
    label_h = LABEL_BAND
    content = [{"text": t, "size": r["size"], "color": r["color"],
                "bold": r["bold"]} for r, t in zip(records, translations)
               if t and t.strip()]
    dropped = len(records) - len(content)

    if layout_mode == "stack":
        avail_w = A4_W - 2 * MARGIN
        top_h = min(STACK_MAX_TOP,
                    src_rect.height * (avail_w / max(1.0, src_rect.width)))
        top_h = max(80.0, top_h)
        if content:
            probe, _t = _plan_paragraphs(content, avail_w, 1e9, base_scale)
            need = sum(i["height"] for i in probe) + PARA_GAP * max(0, len(probe) - 1)
        else:
            need = 120.0
        bottom_h = _clamp(need, top_h, STACK_MAX_BOTTOM)
        tr_top = MARGIN + label_h + top_h + COL_GAP + label_h + TR_TOP_PAD
        page_h = tr_top + bottom_h + MARGIN
        page = out_doc.new_page(width=A4_W, height=page_h)
        src_box = fitz.Rect(MARGIN, MARGIN + label_h, A4_W - MARGIN,
                            MARGIN + label_h + top_h)
        tr_box = fitz.Rect(MARGIN, tr_top, A4_W - MARGIN, tr_top + bottom_h)
        label_boxes = [
            fitz.Rect(MARGIN, MARGIN, A4_W - MARGIN, MARGIN + label_h),
            fitz.Rect(MARGIN, MARGIN + label_h + top_h + COL_GAP,
                      A4_W - MARGIN, MARGIN + label_h + top_h + COL_GAP + label_h),
        ]
        rule_y = src_box.y1 + COL_GAP / 2.0
        rule = (fitz.Point(MARGIN, rule_y), fitz.Point(A4_W - MARGIN, rule_y))
    else:  # side（默认）
        page = out_doc.new_page(width=SIDE_W, height=SIDE_H)
        avail = SIDE_W - 2 * MARGIN
        half = (avail - COL_GAP) / 2.0
        left = fitz.Rect(MARGIN, MARGIN, MARGIN + half, SIDE_H - MARGIN)
        right = fitz.Rect(MARGIN + half + COL_GAP, MARGIN, SIDE_W - MARGIN,
                          SIDE_H - MARGIN)
        inner_l = fitz.Rect(left.x0, left.y0 + label_h, left.x1, left.y1)
        tr_box = fitz.Rect(right.x0, right.y0 + label_h + TR_TOP_PAD, right.x1,
                           right.y1)
        src_box = _fit_rect(src_rect.width, src_rect.height, inner_l)
        label_boxes = [fitz.Rect(left.x0, left.y0, left.x1, left.y0 + label_h),
                       fitz.Rect(right.x0, right.y0, right.x1, right.y0 + label_h)]
        rule = (fitz.Point(MARGIN + half + COL_GAP / 2.0, MARGIN),
                fitz.Point(MARGIN + half + COL_GAP / 2.0, SIDE_H - MARGIN))

    # 1) 原文：把源页作为 Form XObject 矢量嵌入（不是截图）
    rotated_src = _embed_source(page, src_box, src, pno)
    # 2) 分隔线 + 半页标签
    page.draw_line(rule[0], rule[1], color=_RULE, width=0.6, overlay=True)
    _draw_label(page, label_boxes[0], f"原文 · 第 {pno + 1} 页")
    _draw_label(page, label_boxes[1], f"译文 · 第 {pno + 1} 页")
    # 3) 译文
    if not content:
        _draw_hint(page, tr_box, HINT_NO_TEXT)
        return False, 0, dropped, rotated_src
    items, truncated = _plan_paragraphs(content, tr_box.width, tr_box.height,
                                        base_scale)
    _draw_items(page, tr_box, items, sim_bold=sim_bold)
    return truncated, len(content), dropped, rotated_src


# ================================================================ run()


def _install_fallback_fonts():
    """装上「缺字回退字体」（数学符号优先），返回生效的字体路径列表。

    与 ``_system_cjk_font`` 一样按**文件路径**加载 fonts.py，避免包上下文问题。
    """
    try:
        import importlib.util
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts.py")
        spec = importlib.util.spec_from_file_location("docbridge_pipeline_fonts", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.install_fallback_fonts(pt)
    except Exception:                    # noqa: BLE001
        return []


def run(src_path, out_path, *, translate, progress=None, options=None) -> dict:
    """把 ``src_path`` 转成双语对照 PDF 写到 ``out_path``，返回统计 dict。"""
    opts = dict(options or {})
    if not callable(translate):
        raise RuntimeError("缺少翻译回调 translate，无法生成双语对照 PDF")
    if not src_path or not os.path.isfile(src_path):
        raise RuntimeError(f"源 PDF 不存在：{src_path}")

    layout_mode = str(opts.get("bilingual_layout") or "side").strip().lower()
    if layout_mode not in ("side", "stack"):
        layout_mode = "side"
    font_scale = _clamp(_as_float(opts.get("font_scale"), 0.92), 0.5, 1.6)
    base_scale = font_scale / 0.92
    sim_bold = bool(opts.get("sim_bold", False))

    # 中文字体（上层模块级状态，每次调用按 options 重置）
    try:
        # 同 pdf_inplace：auto 时用系统 CJK 字体，避免 Linux 上退回 china-s
        # 导致译文「看得见却复制不出来」（缺 ToUnicode）。
        _font = (opts.get("font") or "").strip()
        if _font in ("", "auto"):
            _font = _system_cjk_font() or "auto"
        _install_fallback_fonts()
        pt.init_cjk_font(_font)
    except Exception as exc:
        raise RuntimeError(f"中文字体初始化失败：{exc}") from exc

    try:
        src = fitz.open(src_path)
    except Exception as exc:
        raise RuntimeError(f"无法打开源 PDF：{src_path}（{exc}）") from exc
    if not src.is_pdf:
        src.close()
        raise RuntimeError(f"不是有效的 PDF 文件：{src_path}")
    if getattr(src, "needs_pass", False):
        src.close()
        raise RuntimeError("源 PDF 已加密，需要密码，无法处理")

    part = out_path + ".part"
    out = fitz.open()
    stats = {"units": 0, "chars_in": 0, "chars_out": 0, "skipped": 0,
             "empty_pages": 0, "truncated_pages": 0, "rotated_pages": 0}
    done = 0
    total = len(src)
    ctx_base = {"kind": "pdf", "mode": "bilingual",
                "source_lang": opts.get("source_lang") or "auto",
                "target_lang": opts.get("target_lang") or "zh-Hans"}

    def _progress(d):
        if callable(progress):
            progress(d, total, f"第 {d}/{total} 页")

    def _flush(pending):
        """先攒批翻译 pending 里的所有段落，再逐页绘制并推进进度。"""
        nonlocal done
        texts = [r["text"] for _p, recs in pending for r in recs]
        pages = [p + 1 for p, _recs in pending]
        trans = (_translate_batched(texts, translate, ctx_base, pages)
                 if texts else [])
        pos = 0
        for pno, recs in pending:
            page_trans = trans[pos:pos + len(recs)]
            pos += len(recs)
            truncated, _drawn, dropped, rot90 = _emit_page(
                out, src, pno, recs, page_trans, layout_mode=layout_mode,
                base_scale=base_scale, sim_bold=sim_bold)
            stats["units"] += len(recs)
            stats["chars_in"] += sum(len(r["text"]) for r in recs)
            stats["chars_out"] += sum(len(t) for t in page_trans if t)
            stats["skipped"] += _count_skipped_lines(src[pno]) + dropped
            if not recs:
                stats["empty_pages"] += 1
            if truncated:
                stats["truncated_pages"] += 1
            if rot90:
                stats["rotated_pages"] += 1
            done += 1
            _progress(done)
        pending.clear()

    try:
        if total == 0:
            raise RuntimeError("源 PDF 没有任何页面")
        pending = []
        buffered = 0
        _progress(0)
        for pno in range(total):
            recs = _page_paragraphs(src[pno])
            pending.append((pno, recs))
            buffered += len(recs)
            if buffered >= BATCH_MIN_TARGET or pno == total - 1:
                _flush(pending)
                buffered = 0
        # 元数据尽量沿用源文件（失败无所谓）
        try:
            meta = src.metadata or {}
            out.set_metadata({k: v for k, v in meta.items() if v})
        except Exception:
            pass
        # 字体子集化：整份中文字体直接嵌入会让体积膨胀十几倍（实测 2.3MB -> 56KB），
        # 子集化后渲染完全一致。是可选优化，失败不影响正确性。
        try:
            out.subset_fonts()
        except Exception:
            pass
        out.save(part, garbage=4, deflate=True)
    except RuntimeError:
        _cleanup(part, out, src)
        raise
    except Exception as exc:
        _cleanup(part, out, src)
        if getattr(exc, _FROM_CALLBACK, False):
            raise                       # 翻译回调的异常原样冒泡
        raise RuntimeError(f"双语对照 PDF 生成失败：{exc}") from exc

    out.close()
    src.close()
    try:
        os.replace(part, out_path)
    except OSError as exc:
        _safe_unlink(part)
        raise RuntimeError(f"无法写出结果文件：{out_path}（{exc}）") from exc

    mode_name = "左右并排" if layout_mode == "side" else "上下堆叠"
    detail = (f"双语对照（{mode_name}）：共 {total} 页，翻译 {stats['units']} 段，"
              f"跳过 {stats['skipped']} 行（已是中文/纯数字）")
    if stats["empty_pages"]:
        detail += f"，{stats['empty_pages']} 页无可翻译文本"
    if stats["truncated_pages"]:
        detail += f"，{stats['truncated_pages']} 页译文过长已省略部分内容"
    if stats["rotated_pages"]:
        detail += (f"，{stats['rotated_pages']} 页页面带 90°/270° 旋转"
                   f"（原文按内容方向嵌入，不跟随页面旋转）")
    return {
        "units": stats["units"],
        "chars_in": stats["chars_in"],
        "chars_out": stats["chars_out"],
        "skipped": stats["skipped"],
        "pages": total,
        "detail": detail,
        "layout": layout_mode,
        "empty_pages": stats["empty_pages"],
        "truncated_pages": stats["truncated_pages"],
        "rotated_pages": stats["rotated_pages"],
    }


def _safe_unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _cleanup(part, out_doc, src_doc):
    """失败路径：关掉文档、删掉半成品 .part。"""
    _safe_unlink(part)
    for doc in (out_doc, src_doc):
        try:
            doc.close()
        except Exception:
            pass


# ================================================================ 手工自测

if __name__ == "__main__":  # pragma: no cover
    import argparse

    ap = argparse.ArgumentParser(description="PDF 双语对照（离线假翻译，仅自测）")
    ap.add_argument("input")
    ap.add_argument("output")
    ap.add_argument("--layout", default="side", choices=["side", "stack"])
    ap.add_argument("--font", default="auto")
    ap.add_argument("--font-scale", type=float, default=0.92)
    a = ap.parse_args()

    def _fake(texts, ctx=None):
        return [f"【译】{t}" for t in texts]

    print(run(a.input, a.output, translate=_fake, options={
        "bilingual_layout": a.layout, "font": a.font,
        "font_scale": a.font_scale,
    }, progress=lambda d, t, note="": print(f"  {d}/{t} {note}")))
