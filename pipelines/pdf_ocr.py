# -*- coding: utf-8 -*-
"""docbridge 管线 · 扫描版 PDF 原位翻译（OCR）· ``FORMAT="pdf"`` / ``MODE="ocr"``。

**适用对象**：整页是图片、没有文本层的 PDF（扫描教材、影印书、图片型导出）。
处理方式与 ``pdf_inplace`` 的区别：原文不在文本层里，所以先用 OCR 认出
「词 + 坐标」，再把词聚成行、行聚成段，攒批翻译，最后**在原扫描图上**
（像素一个都不动）用底色矩形盖住英文、绘制中文译文，页数严格不变。

处理流程（每页）：

1. **OCR**：``page.get_textpage_ocr(...)`` + ``page.get_text("words", textpage=tp)``
   拿到 ``(x0, y0, x1, y1, word, ...)``（PyMuPDF 原生能力，走系统 tesseract）；
2. **聚行**：按 y 方向重叠把词并成行，行内按 x 排序、空格连接，行框 = 词框并集；
3. **聚段**：先按 x0 把行聚成「列」（多栏扫描件），列内按行距 / 缩进 / 行高
   聚成段落（思路同上层 ``group_paragraphs``）；
4. **跳过**：纯数字/符号、已是中文的行不送翻译，计入 ``skipped``；
5. **翻译**：段落为单位攒批（单批 ≤ ``BATCH_MAX``=100 条，尽量 ≥30 条），
   ``context`` 传 ``{"kind": "pdf", "page": n, "ocr": True}``；
   返回数量不符立刻 ``RuntimeError("翻译返回数量不符：期望 N，实际 M")``；
6. **绘制**：先 ``sample_bg_color`` 给行框铺底色矩形盖住原文，再按行框尺寸
   绘制中文（字号用 ``fit_size``/``layout`` 自适应，放不下继续缩小；
   整段拟合不到 5pt 就保留原文并计入 ``skipped``）。

设计约束（见 ``docbridge/CONTRACT.md``）：

* 只通过 ``translate`` 回调与外界交互：不联网、不读密钥、不 import requests；
* ``out_path + ".part"`` 原子落盘，成功后 ``os.replace``；任何失败都删 ``.part``；
* 不写模块级可变状态（Flask 多线程调用）：所有状态都是函数局部变量；
  唯一的模块级对象是一把 ``threading.RLock()``（不是可变状态），用于串行化
  上层 ``pdf_translate`` 的**模块级字体全局量**（``init_cjk_font`` 会改
  ``pt.CJK_FONT`` 等），与 ``pdf_inplace`` 的做法一致；
* ``progress(done, total, note)`` 单调递增、末次 ``done == total``、按页推进；
  进度回调抛 ``JobCancelled``（``BaseException``）时必须穿过去，所以只
  ``except Exception``；
* 不改源文件、页数不变、图片零改动（保存后独立复核图片数量）。

已知限制（诚实说明）：

* 版面：行/段聚合是启发式的（y 重叠 + 水平空档 + 行距/缩进/行高），跨栏、表格线、
  脚注、公式、图文混排等复杂版面会有误差——栏间距极窄时左右两栏可能被当成一行，
  表格同一行的多个格子也可能被当成多行；
* 旋转页：``/Rotate`` 为 90°/270° 的页**跳过不译**（原因见 ``run()`` 里的注释：
  MuPDF 的 OCR 按显示方向栅格化、却返回内容系坐标，而上层绘制只能画水平文本），
  原文原样保留、页数不变，数量在 ``detail`` 与返回值 ``rotated_pages`` 里如实上报；
* 竖排文字、手写体、印章、低分辨率/歪斜/带噪点的扫描件识别率取决于 tesseract
  与语言包（中文扫描件要装 ``tesseract-ocr-chi-sim`` 并传 ``ocr_lang="chi_sim"``）；
* 译文按「行框」重排：原文行数与译文行数不一致时，段落末尾多余的原文行会被底色
  盖住但不写新字（正常）；译文比原文长太多（拟合字号 < 5pt）时整段保留原文；
* 取底色需要把每页按 150dpi 栅格化一次，超大页面会多占一份内存。
"""

from __future__ import annotations

import math
import os
import sys
import threading

import fitz  # PyMuPDF

# ------------------------------------------------------------------ 上层导入
# 查找顺序：DOCBRIDGE_PDF_TRANSLATE_DIR > 包目录 docbridge/ > 其父目录 > 本机开发路径。
# 与 pdf_inplace / pdf_bilingual 保持一致：部署时把 pdf_translate.py 放进
# docbridge/ 就能用（服务器 /opt/docbridge 就是这种布局）。
_HERE = os.path.dirname(os.path.abspath(__file__))
_DOCBRIDGE = os.path.dirname(_HERE)                       # docbridge/
_UPSTREAM_CANDIDATES = [
    os.environ.get("DOCBRIDGE_PDF_TRANSLATE_DIR") or "",
    _DOCBRIDGE,
    os.path.dirname(_DOCBRIDGE),                          # 父目录
]
UPSTREAM_DIR = next((d for d in _UPSTREAM_CANDIDATES
                     if d and os.path.isfile(os.path.join(d, "pdf_translate.py"))),
                    _UPSTREAM_CANDIDATES[1])
if UPSTREAM_DIR not in sys.path:
    sys.path.insert(0, UPSTREAM_DIR)

import pdf_translate as pt  # noqa: E402  (既有生产代码，只读复用，绝不修改)


def _install_fallback_fonts():
    """装上「缺字回退字体」（数学符号优先）。与其它 PDF 管线同一套做法：
    按文件路径加载 fonts.py，避免包上下文问题。必须在 init_cjk_font 之前调用。"""
    try:
        import importlib.util
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts.py")
        spec = importlib.util.spec_from_file_location("docbridge_pipeline_fonts", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.install_fallback_fonts(pt)
    except Exception:                    # noqa: BLE001
        return []


def _system_cjk_font() -> str:
    """返回系统里可用的中文字体路径（候选清单见 pipelines/fonts.py）。

    按**文件路径**加载 fonts.py，而不是包内相对导入：本模块也可能被测试用
    importlib 直接按文件加载，那时没有包上下文，``from . import ...`` 会报
    "attempted relative import with no known parent package"。
    """
    try:
        import importlib.util
        path = os.path.join(_HERE, "fonts.py")
        spec = importlib.util.spec_from_file_location("docbridge_pipeline_fonts", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.default_pdf_font()
    except Exception:                    # noqa: BLE001 —— 取不到就走环境变量/内置字体
        env = (os.environ.get("DOCBRIDGE_PDF_FONT") or "").strip()
        return env if env and os.path.isfile(env) else ""


# ------------------------------------------------------------------ 契约元数据

FORMAT = "pdf"
MODE = "ocr"
LABEL = "扫描版原位翻译（OCR）"
NOTE = ("适用于整页是图片、没有文本层的扫描件：先用 OCR 识别文字与坐标，"
        "再在原扫描图上原位绘制中文译文。图片像素不动、页数不变；"
        "识别效果取决于 tesseract 与扫描质量。")
OPTIONS = ["ocr_lang", "ocr_dpi", "font", "font_scale", "sim_bold",
           "source_lang", "target_lang"]

# ------------------------------------------------------------------ 版面常量

DEFAULT_OCR_LANG = "eng"        # 中文扫描件传 "chi_sim" / "eng+chi_sim"
DEFAULT_OCR_DPI = 200
MIN_DPI, MAX_DPI = 72, 600
SAMPLE_DPI = 150                # 取底色用的页面栅格化精度（同上层 process_pdf）

LINE_OVERLAP_RATIO = 0.45       # 词与行的 y 重叠达到「较矮一方高度」的该比例即归入该行
LINE_LOOKBACK = 12              # 只跟最近若干行比较（词按 y 排序，够用且快）
LINE_X_GAP_FACTOR = 2.4         # 同一行内允许的最大水平空档（相对行高）
LINE_X_GAP_MIN = 18.0           # 上述空档的绝对下限（pt）：超过就当成另一栏/另一格
COLUMN_X_TOL = 30.0             # x0 相差不超过该值算同一列（同 group_paragraphs）
PARA_X_TOL = 18.0               # 段内左缘允许的偏移（段首缩进）
PARA_GAP_FACTOR = 0.9           # 行间距 > 行高 * 该系数 → 断段
PARA_SIZE_TOL = 0.45            # 行高差异超过较大者该比例 → 断段

MIN_TRANSLATABLE_PT = 5.0       # 拟合字号低于此值：保留原文，计入 skipped
PARA_PITCH_CAP = 0.85           # 字号 ≤ 原行距 * 该系数（1.15 * size ≤ 行距）

BATCH_MAX = 100                 # 单次 translate 的最大条数（契约：别超 ~200）
BATCH_MAX_CHARS = 3500          # 单批最大字符数（避免超长请求 / JSON 截断）

_INK_DARK = (0.07, 0.07, 0.07)
_INK_LIGHT = (0.95, 0.95, 0.95)

# 上层 pdf_translate 用模块级全局量保存字体（init_cjk_font 会改它），
# 多线程同时跑会互相踩，所以所有「量字号 + 绘制」的调用都在锁里做。
# 这是锁而不是可变状态：不会在任务之间泄漏任何业务数据。
_FONT_LOCK = threading.RLock()


# ================================================================ 小工具

def _as_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in ("1", "true", "yes", "on", "是")


def _as_float(value, default):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if out != out or out in (float("inf"), float("-inf")):    # NaN / inf
        return default
    return out


def _as_int(value, default):
    try:
        out = int(float(value))
    except (TypeError, ValueError):
        return default
    return out


def _clamp(value, lo, hi):
    return lo if value < lo else (hi if value > hi else value)


def _remove_quiet(path):
    """尽力删除（失败不抛）：失败路径上绝不能因为清理再炸一次。"""
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _count_images(path):
    """逐页统计图片数量（与上游 ``process_pdf`` 同一口径）。"""
    with fitz.open(path) as doc:
        return sum(len(page.get_images(full=True)) for page in doc)


def _median(values, default=0.0):
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return default
    return vals[len(vals) // 2]


# ================================================================ OCR

def _ocr_error_message(exc, language):
    """把 PyMuPDF/tesseract 的原始报错变成可操作的中文原因。

    一律带上安装提示：OCR 初始化失败最常见的两个原因就是**没装 tesseract**
    与**没装对应语言包**，报错原文又往往只有一句 "language initialisation
    failed"，用户看不出该怎么办。
    """
    msg = str(exc).strip() or exc.__class__.__name__
    return ("OCR 失败（语言 %s）：%s；请确认已安装 tesseract 与对应语言包："
            "apt install tesseract-ocr tesseract-ocr-eng"
            "（中文扫描件再加 tesseract-ocr-chi-sim），"
            "并用 tesseract --list-langs 确认语言可用"
            % (language, msg))


def _ocr_words(page, language, dpi):
    """对一页做 OCR，返回 ``(words, error)``；``error == ""`` 表示成功。

    单页 OCR 失败（语言包缺失 / 图像异常）不直接抛：先记下来继续处理别的页，
    最后如果整份文档一个词都没认出来，再统一抛中文 ``RuntimeError``
    （见 ``run``）。
    """
    try:
        tp = page.get_textpage_ocr(flags=0, language=language, dpi=dpi, full=True)
        words = page.get_text("words", textpage=tp)
        return list(words or []), ""
    except Exception as exc:              # noqa: BLE001 —— 转成中文原因，绝不吞掉
        return [], _ocr_error_message(exc, language)


def ocr_probe(language=DEFAULT_OCR_LANG, dpi=120):
    """探测 OCR 是否真的可用：内存里造一张写着 "OCR OK" 的小图跑一次。

    返回 ``(是否可用, 错误说明)``。给测试与调用方用：tesseract 没装或语言包
    缺失时可以提前知道，而不是等一本 500 页的书跑到一半才失败。
    """
    try:
        doc = fitz.open()
    except Exception as exc:              # noqa: BLE001
        return False, "无法初始化 PyMuPDF：%s" % (exc,)
    try:
        page = doc.new_page(width=240, height=90)
        page.insert_text((24, 58), "OCR OK", fontsize=22)
        pix = page.get_pixmap(dpi=dpi)
        scan = fitz.open()
        try:
            sp = scan.new_page(width=pix.width * 72.0 / dpi,
                               height=pix.height * 72.0 / dpi)
            sp.insert_image(sp.rect, pixmap=pix)
            words, err = _ocr_words(sp, language, dpi)
            if err:
                return False, err
            if not words:
                return False, "OCR 可用但未识别出任何文字（语言 %s）" % (language,)
            return True, ""
        finally:
            scan.close()
    finally:
        doc.close()


def ocr_available(language=DEFAULT_OCR_LANG, dpi=120):
    """``ocr_probe`` 的布尔版（测试里用它决定 SKIP 还是真跑）。"""
    ok, _err = ocr_probe(language, dpi)
    return ok


# ================================================================ 版面聚合

def _collect_words(words):
    """过滤出可用的词，返回 ``[(rect, text)]``（按 y0、x0 排序）。"""
    items = []
    for w in words or []:
        try:
            rect = fitz.Rect(float(w[0]), float(w[1]), float(w[2]), float(w[3]))
            text = str(w[4])
        except (IndexError, TypeError, ValueError):
            continue
        text = text.strip()
        if not text or rect.width <= 0 or rect.height <= 0:
            continue
        items.append((rect, text))
    items.sort(key=lambda it: (it[0].y0, it[0].x0))
    return items


def _group_lines(words):
    """把词按 y 重叠聚成行，返回 ``[{"rect", "text", "height"}, ...]``。

    词按 y0 排序后逐个归入「垂直重叠最大」的已有行，两个条件都要满足：

    * y 方向重叠达到较矮一方高度的 ``LINE_OVERLAP_RATIO``（相邻行、上下标不串）；
    * x 方向「挨着」：水平空档不超过 ``LINE_X_GAP_FACTOR`` × 行高（绝对下限
      ``LINE_X_GAP_MIN``）——多栏排版 / 表格分栏的空档远大于词间距，据此把左右
      两栏拆成不同的行（否则两栏会被连成一行，中文横跨整页盖掉版式）。

    行内词按 x0 排序后用空格连接；行框是该行所有词框的并集。
    """
    lines = []
    for rect, text in _collect_words(words):
        target, best = None, 0.0
        for ln in lines[-LINE_LOOKBACK:]:
            lr = ln["rect"]
            ov = min(lr.y1, rect.y1) - max(lr.y0, rect.y0)
            if ov <= 0:
                continue
            limit = max(LINE_X_GAP_FACTOR * min(lr.height, rect.height),
                        LINE_X_GAP_MIN)
            if (rect.x0 - lr.x1 > limit) or (lr.x0 - rect.x1 > limit):
                continue                     # 水平空档太大：不同栏 / 不同格
            if ov > best:
                target, best = ln, ov
        if (target is not None and
                best >= LINE_OVERLAP_RATIO * min(target["rect"].height,
                                                 rect.height)):
            target["rect"] |= rect
            target["items"].append((rect, text))
        else:
            lines.append({"rect": fitz.Rect(rect), "items": [(rect, text)]})
    out = []
    for ln in lines:
        parts = [t for _r, t in sorted(ln["items"], key=lambda it: it[0].x0)]
        text = " ".join(parts).strip()
        if not text:
            continue
        out.append({"rect": fitz.Rect(ln["rect"]), "text": text,
                    "height": ln["rect"].height})
    out.sort(key=lambda l: (round(l["rect"].y0, 1), l["rect"].x0))
    return out


def _split_columns(lines):
    """按 x0 把行聚成「列」（多栏扫描件），返回列的列表。

    与上层 ``group_paragraphs`` 同思路（x0 相差 ≤ ``COLUMN_X_TOL`` 算同一列）；
    列之间按「首行纵坐标、再左缘」排序，保证阅读顺序（标题居中也不会被排到
    正文后面）。列内按 (y0, x0) 排序。
    """
    cols = []
    for ln in sorted(lines, key=lambda l: l["rect"].x0):
        if cols and abs(ln["rect"].x0 - cols[-1][-1]["rect"].x0) <= COLUMN_X_TOL:
            cols[-1].append(ln)
        else:
            cols.append([ln])
    for col in cols:
        col.sort(key=lambda l: (l["rect"].y0, l["rect"].x0))
    cols.sort(key=lambda c: (round(min(l["rect"].y0 for l in c), 1),
                             min(l["rect"].x0 for l in c)))
    return cols


def _blocked_between(upper, lower, blockers):
    """两行之间是否夹着「不需要翻译的行」（图注/页码/表格数字）→ 断段。"""
    for br in blockers:
        if (br.y0 >= upper.y1 - 1.0 and br.y1 <= lower.y0 + 1.0
                and br.x1 > min(upper.x0, lower.x0)
                and br.x0 < max(upper.x1, lower.x1)):
            return True
    return False


def _same_paragraph(prev, cur, blockers):
    """相邻两行是否属于同一段：行距、左缘、行高三项都要像。"""
    pr, cr = prev["rect"], cur["rect"]
    h = max(1.0, min(pr.height, cr.height))
    gap = cr.y0 - pr.y1
    if gap < -0.35 * h:                       # y 回跳（换栏/乱序）：断
        return False
    if gap > max(1.6, PARA_GAP_FACTOR * h):   # 行距过大：断
        return False
    if abs(pr.height - cr.height) > PARA_SIZE_TOL * max(pr.height, cr.height):
        return False
    dx = cr.x0 - pr.x0
    if dx < -PARA_X_TOL or dx > PARA_X_TOL:   # 左缘明显偏移：断
        return False
    return not _blocked_between(pr, cr, blockers)


def _group_paragraphs(col, blockers):
    """把一列里相邻的待译行聚成段落，返回 ``[[line, ...], ...]``。"""
    paras, cur = [], []
    for ln in col:
        if cur and not _same_paragraph(cur[-1], ln, blockers):
            paras.append(cur)
            cur = []
        cur.append(ln)
    if cur:
        paras.append(cur)
    return paras


def _join_lines(texts):
    """把同段的多行拼成一段（处理英文行尾连字符，同上层 pdf_bilingual）。"""
    out = ""
    for raw in texts:
        t = (raw or "").strip()
        if not t:
            continue
        if not out:
            out = t
            continue
        if out.endswith("-") and not out.endswith("--") and t[:1].islower():
            out = out[:-1] + t
        else:
            out += " " + t
    return out


def _needs_translation(text, target_lang):
    """判断一行要不要送翻译：纯数字/符号、已是中文的行跳过。

    判定口径与 ``translator.needs_translation`` 一致（同一套正则），但**故意
    不 import translator**：那个模块会拉起 LLM 客户端依赖，而契约要求
    pipeline 自身不碰网络/密钥相关的东西。
    """
    t = (text or "").strip()
    if not t:
        return False
    if pt.NUMBERISH_RE.match(t):
        return False
    if str(target_lang or "").startswith("zh"):
        cjk = len(pt.CJK_RE.findall(t))
        letters = len(pt.LATIN_RE.findall(t))
        if cjk and cjk * 3 >= letters:
            return False
        return bool(pt.LATIN_RE.search(t))
    return True


def _page_units(lines, target_lang):
    """把一页的行聚成翻译单元：``[(段落行列表, 段落原文), ...]``。

    * ``line["tr"]`` 标记该行是否需要翻译（不需要的计入 skipped，原文保留）；
    * 段落只由待译行组成，且中间夹着「不需要翻译的行」时断开——
      这样既不把图注/页码混进正文，也不会让中文盖到那些行上。
    """
    for ln in lines:
        ln["tr"] = _needs_translation(ln["text"], target_lang)
    ok_lines = [ln for ln in lines if ln["tr"]]
    if not ok_lines:
        return []
    blockers = [ln["rect"] for ln in lines if not ln["tr"]]
    units = []
    for col in _split_columns(ok_lines):
        for para in _group_paragraphs(col, blockers):
            text = _join_lines([ln["text"] for ln in para])
            if text:
                units.append((para, text))
    return units


# ================================================================ 翻译（攒批）

def _chunk_units(texts):
    """把单元切成单批 ≤ ``BATCH_MAX`` 条、≤ ``BATCH_MAX_CHARS`` 字符的批。

    条数上**均分**而不是"装满 100 再收尾"：105 条会切成 53 + 52（而不是
    100 + 5）——尾巴太小的一次请求同样要付固定的提示开销，均分更划算，
    也让每一批都落在「30~100 条」这个契约建议区间里（页内不足 30 条时有多少
    发多少，没有跨页攒批，因为 ``context`` 要带页码）。
    返回 ``[(start, end), ...]``。
    """
    count = len(texts)
    chunks, i = [], 0
    while i < count:
        remain = count - i
        parts = max(1, (remain + BATCH_MAX - 1) // BATCH_MAX)
        take = min(BATCH_MAX, max(1, (remain + parts - 1) // parts))
        chars, j = 0, i
        while j < count and (j - i) < take:
            ln = len(texts[j])
            if j > i and chars + ln > BATCH_MAX_CHARS:
                break                      # 单批字符数封顶（避免超长请求）
            chars += ln
            j += 1
        chunks.append((i, j))
        i = j
    return chunks


def _translate_batched(texts, translate, page_no, source_lang, target_lang):
    """按批调用 ``translate``，逐批校验 1:1 等长（绝不错位、绝不静默截断）。"""
    out = []
    for start, end in _chunk_units(texts):
        chunk = [str(t) for t in texts[start:end]]
        ctx = {"kind": "pdf", "page": page_no, "ocr": True,
               "count": len(chunk), "source_lang": source_lang,
               "target_lang": target_lang}
        res = translate(chunk, ctx)            # 回调异常原样冒泡（app 层记失败）
        if isinstance(res, (str, bytes)) or not isinstance(res, (list, tuple)):
            raise RuntimeError("翻译返回数量不符：期望 %d，实际 1（回调未返回列表）"
                               % len(chunk))
        if len(res) != len(chunk):
            raise RuntimeError("翻译返回数量不符：期望 %d，实际 %d"
                               % (len(chunk), len(res)))
        for item in res:
            out.append("" if item is None else str(item))
    return out


# ================================================================ 译文绘制

def _ink_on(bg):
    """按底色亮度选字色：深底用白字，浅底用黑字（反色扫描件也能看清）。"""
    try:
        r, g, b = float(bg[0]), float(bg[1]), float(bg[2])
    except (TypeError, ValueError, IndexError):
        return _INK_DARK
    return _INK_LIGHT if (0.299 * r + 0.587 * g + 0.114 * b) < 0.5 else _INK_DARK


def _paragraph_metrics(boxes):
    """段落的行框统计：中位行高、中位行距、块的左右边界。"""
    heights = sorted(b.height for b in boxes)
    med_h = heights[len(heights) // 2] if heights else 0.0
    if len(boxes) > 1:
        ys = sorted(b.y0 for b in boxes)
        pitches = [ys[i + 1] - ys[i] for i in range(len(ys) - 1)]
        med_pitch = _median(pitches, med_h * 1.3)
    else:
        med_pitch = med_h * 1.3
    left = min(b.x0 for b in boxes)
    right = max(b.x1 for b in boxes)
    return med_h, med_pitch, max(12.0, right - left)


def _fit_lines(text, width, max_lines, max_size):
    """在 ``width`` 宽度内把译文排成不超过 ``max_lines`` 行，返回 (字号, 行列表)。

    字号从 ``max_size`` 起步逐 0.5pt 缩小；低于 ``MIN_TRANSLATABLE_PT``（5pt）
    仍放不下就返回 ``(0.0, [])``，调用方据此保留原文并计入 skipped。
    """
    size = max_size
    while size >= MIN_TRANSLATABLE_PT:
        lines = pt.wrap_text(text, size, width)   # 自带中文禁则处理
        if len(lines) <= max_lines:
            return size, lines
        size -= 0.5
    return 0.0, []


def _draw_paragraph(page, para, zh, bg_pix, sample_dpi, font_scale, sim_bold):
    """把一段译文按该段的行框绘制：先铺底色盖住原文，再写中文。

    返回 ``(画出中文的行数, 保留原文的行数)``。

    * 原文**一个像素都不改**：只在行框上盖一个采样自该处底色的填充矩形，
      再把中文画上去（``overlay=True``，不动底层图像对象）；
    * 字号自适应：以段内中位行高、中位行距和 ``font_scale`` 定出上限，
      再用 ``pt.wrap_text``/``fit_size`` 的口径缩到「段内行数装得下」；
    * 绘制的每一行用 ``pt.draw_translation``，宽度取「行框宽度」与「该行实测
      宽度」的较大者，保证它不会在行框里被二次折行、溢出到下一行；
    * 整段拟合不到 5pt：整段保留原文（不盖不画），调用方计入 skipped。
    """
    items = [ln for ln in para if ln.get("tr") and ln["rect"].height > 0.5]
    zh = (zh or "").strip()
    if not items or not zh:
        return 0, 0
    boxes = [ln["rect"] for ln in items]
    med_h, med_pitch, block_width = _paragraph_metrics(boxes)
    if med_h <= 0.5:
        return 0, len(items)
    # 字号上限：不能超过行高（否则字比原行还高），也不能超过原行距
    # （1.15 * size 是上游的行距系数，超了中文相邻行会叠在一起）。
    max_size = min(med_h, PARA_PITCH_CAP * med_pitch) * font_scale
    if max_size < MIN_TRANSLATABLE_PT:
        return 0, len(items)
    size, lines = _fit_lines(zh, block_width, len(boxes), max_size)
    if size <= 0:
        return 0, len(items)

    render_mode = 2 if sim_bold else 0
    border = (0.04 * size) if sim_bold else 0.0
    drawn = 0
    for i, box in enumerate(boxes):
        bg = pt.sample_bg_color(bg_pix, box, sample_dpi)   # 逐行取底色
        # 盖住原文（多扩 1pt，避免抗锯齿边缘残留）
        page.draw_rect(box + (-1.0, -1.0, 1.0, 1.0), color=None, fill=bg,
                       overlay=True)
        if i >= len(lines):
            continue                                      # 译文比原文行少：留空
        text = lines[i]
        width = max(box.width, pt.text_width(text, size) + 0.5)
        pt.draw_translation(page, box.x0, box.y0, box.y1, text, size, _ink_on(bg),
                            width, render_mode, border, baseline=None,
                            justify=False)
        drawn += 1
    return drawn, 0


# ================================================================ run()

def run(src_path, out_path, *, translate, progress=None, options=None) -> dict:
    """扫描版 PDF → OCR 后原位翻译，写出 ``out_path``，返回统计 dict。

    :param src_path: 输入 PDF 绝对路径（只读，绝不修改）
    :param out_path: 输出 PDF 绝对路径
    :param translate: ``translate(texts, context) -> list[str]``，必须等长同序
    :param progress: 可选 ``progress(done, total, note)``；单调递增、末次 done == total
    :param options: ``ocr_lang`` / ``ocr_dpi`` / ``font`` / ``font_scale`` /
                    ``sim_bold`` / ``source_lang`` / ``target_lang``
    :returns: 契约 dict（units / chars_in / chars_out / skipped / pages / detail）
    """
    if not callable(translate):
        raise RuntimeError("缺少 translate 翻译回调，无法进行 OCR 翻译")
    if not src_path or not os.path.isfile(src_path):
        raise RuntimeError("找不到输入文件：%s" % (src_path,))

    opts = dict(options or {})
    language = str(opts.get("ocr_lang") or DEFAULT_OCR_LANG).strip() or DEFAULT_OCR_LANG
    dpi = _clamp(_as_int(opts.get("ocr_dpi"), DEFAULT_OCR_DPI), MIN_DPI, MAX_DPI)
    font_scale = _clamp(_as_float(opts.get("font_scale"), 0.92), 0.3, 2.0)
    sim_bold = _as_bool(opts.get("sim_bold"), False)
    source_lang = opts.get("source_lang") or "auto"
    target_lang = opts.get("target_lang") or "zh-Hans"
    font_spec = str(opts.get("font") or "").strip()
    if font_spec in ("", "auto"):
        font_spec = _system_cjk_font() or "auto"

    # ---- 打开与校验（坏输入一律给中文原因）
    try:
        doc = fitz.open(src_path)
    except Exception as exc:                  # noqa: BLE001
        raise RuntimeError("无法打开输入 PDF：%s" % (exc,)) from exc
    pages = 0
    try:
        if not doc.is_pdf:
            raise RuntimeError("不是有效的 PDF 文件：%s" % (src_path,))
        if getattr(doc, "needs_pass", False):
            raise RuntimeError("输入 PDF 已加密，需要密码，无法处理")
        pages = doc.page_count
        if pages <= 0:
            raise RuntimeError("输入 PDF 没有页面")
    except BaseException:
        doc.close()
        raise

    part_path = out_path + ".part"
    _remove_quiet(part_path)
    try:                                  # 契约说父目录已存在；保险起见自己兜一下
        os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    except OSError as exc:
        doc.close()
        raise RuntimeError("无法创建输出目录：%s（%s）"
                           % (os.path.dirname(os.path.abspath(out_path)), exc)) from exc

    stats = {"units": 0, "chars_in": 0, "chars_out": 0, "skipped": 0,
             "ocr_words": 0, "empty_pages": 0, "drawn_units": 0, "drawn_lines": 0,
             "kept_lines": 0, "ocr_error": "", "no_unit_pages": 0,
             "rotated_pages": 0}
    state = {"done": 0, "warned": False}

    def emit(done, note=""):
        """进度上报：单调递增；``except Exception``（JobCancelled 是
        BaseException，必须让它穿过去，否则「取消」会失效）。"""
        if progress is None:
            return
        done = max(state["done"], min(int(done), int(pages)))
        state["done"] = done
        try:
            progress(done, int(pages),
                     note or ("已处理 %d/%d 页" % (done, pages)))
        except Exception as exc:              # noqa: BLE001
            if not state["warned"]:
                state["warned"] = True
                print("[warn] progress 回调异常（已忽略）：%s" % (exc,),
                      file=sys.stderr)

    try:
        # 字体先验一遍：早失败，别等到画的时候才发现没有中文字体
        with _FONT_LOCK:
            try:
                _install_fallback_fonts()
                pt.init_cjk_font(font_spec)
            except Exception as exc:          # noqa: BLE001
                raise RuntimeError("中文字体不可用：%s（%s）" % (font_spec, exc)) from exc

        emit(0, "开始：共 %d 页，OCR 语言 %s @%ddpi" % (pages, language, dpi))

        for pno in range(pages):
            page = doc[pno]
            rotate = int(getattr(page, "rotation", 0) or 0) % 360
            if rotate in (90, 270):
                # ★ 旋转页的处理取舍（见模块末尾「已知限制」）：
                # MuPDF 的 OCR 按**显示方向**（含 /Rotate）栅格化，但返回的坐标是
                # **内容坐标系**；而本文档的绘制走上层 draw_translation，只能画
                # 水平文本。两者在 90°/270° 页上不一致——照画会把中文横着盖在
                # 竖排的原文上。宁可不译（原文原样保留、如实上报），也不画歪。
                stats["rotated_pages"] += 1
                emit(pno + 1, "第 %d/%d 页：带 %d° 旋转，已跳过（不画歪）"
                     % (pno + 1, pages, rotate))
                continue
            emit(pno, "第 %d/%d 页：OCR 识别中" % (pno + 1, pages))
            words, err = _ocr_words(page, language, dpi)
            if err and not stats["ocr_error"]:
                stats["ocr_error"] = err
            stats["ocr_words"] += len(words)
            lines = _group_lines(words)
            if not lines:
                stats["empty_pages"] += 1
                emit(pno + 1, "第 %d/%d 页：无可识别文本" % (pno + 1, pages))
                continue

            units = _page_units(lines, target_lang)
            stats["skipped"] += sum(0 if ln["tr"] else 1 for ln in lines)
            if not units:
                stats["no_unit_pages"] += 1
                emit(pno + 1, "第 %d/%d 页：无需翻译的文本" % (pno + 1, pages))
                continue

            texts = [t for _para, t in units]
            translations = _translate_batched(texts, translate, pno + 1,
                                              source_lang, target_lang)
            stats["chars_in"] += sum(len(t) for t in texts)
            stats["chars_out"] += sum(len(t) for t in translations)
            stats["units"] += sum(1 for t in translations if t.strip())

            # 取底色用的页面栅格化：在**绘制之前**截取，且放在锁外做
            # （栅格化慢、又不碰上层字体全局量，没必要占着锁）。
            bg_pix = page.get_pixmap(dpi=SAMPLE_DPI)

            # 绘制：上面这些 pt.* 函数依赖模块级字体全局量，加锁 + 每页重置，
            # 保证并发任务之间不会用错字体。
            with _FONT_LOCK:
                _install_fallback_fonts()
                pt.init_cjk_font(font_spec)
                for (para, _text), zh in zip(units, translations):
                    if not (zh or "").strip():
                        continue
                    drawn, kept = _draw_paragraph(page, para, zh, bg_pix,
                                                  SAMPLE_DPI, font_scale, sim_bold)
                    stats["drawn_lines"] += drawn
                    stats["kept_lines"] += kept
                    stats["skipped"] += kept
                    if drawn:
                        stats["drawn_units"] += 1
            del bg_pix
            emit(pno + 1, "第 %d/%d 页：译 %d 段" % (pno + 1, pages, len(units)))

        # ---- 整份文档一个词都没认出来：不要假装成功
        if stats["ocr_words"] <= 0:
            if stats["rotated_pages"] >= pages:
                raise RuntimeError(
                    "全部 %d 页都带 90°/270° 旋转，本模块暂不处理（OCR 按显示"
                    "方向识别、坐标却是内容坐标系，照画会把中文画歪），"
                    "没有可翻译的内容" % pages)
            hint = ""
            if stats["ocr_error"]:
                hint = "；OCR 报错：" + stats["ocr_error"]
            if stats["rotated_pages"]:
                hint += "；另有 %d 页因带 90°/270° 旋转被跳过" % stats["rotated_pages"]
            raise RuntimeError(
                "整份文档都没有 OCR 出任何文字（%d 页）：可能是空扫描件、"
                "图片太小/太模糊，或者 ocr_lang 选错了（当前 %s，"
                "中文扫描件请用 chi_sim 或 eng+chi_sim）%s"
                % (pages, language, hint))

        # ---- 保存（原子落盘）
        try:
            doc.subset_fonts()          # 中文字体子集化：体积小一大截（best-effort）
        except Exception:               # noqa: BLE001
            pass
        doc.save(part_path, garbage=4, deflate=True)
    except BaseException:               # 失败/被取消：清掉半成品再往上抛
        _remove_quiet(part_path)
        try:
            doc.close()
        except Exception:               # noqa: BLE001
            pass
        raise

    try:
        doc.close()
    except Exception:                   # noqa: BLE001
        pass

    # ---- 图片零改动复核（扫描图必须原样保留，数量不一致就别交付）
    try:
        n_img_in = _count_images(src_path)
        n_img_out = _count_images(part_path)
    except Exception as exc:            # noqa: BLE001
        _remove_quiet(part_path)
        raise RuntimeError("输出 PDF 校验失败：%s" % (exc,)) from exc
    if n_img_in != n_img_out:
        _remove_quiet(part_path)
        raise RuntimeError("图片数量发生变化：输入 %d 张、输出 %d 张，结果不可信"
                           % (n_img_in, n_img_out))

    try:
        os.replace(part_path, out_path)
    except OSError as exc:
        _remove_quiet(part_path)
        raise RuntimeError("输出文件落盘失败：%s" % (exc,)) from exc

    emit(pages, "完成")
    detail = ("扫描版 OCR 原位翻译完成：共 %d 页（OCR 识别 %d 词），"
              "翻译 %d 段、绘制 %d 段（%d 行），跳过 %d 行"
              "（已是中文/纯数字/字号小于 %.0fpt 保留原文）"
              % (pages, stats["ocr_words"], stats["units"], stats["drawn_units"],
                 stats["drawn_lines"], stats["skipped"], MIN_TRANSLATABLE_PT))
    if stats["empty_pages"]:
        detail += "，其中 %d 页无可识别文本" % stats["empty_pages"]
    if stats["no_unit_pages"]:
        detail += "，%d 页没有需要翻译的文本" % stats["no_unit_pages"]
    if stats["rotated_pages"]:
        detail += ("，%d 页带 90°/270° 旋转已跳过（OCR 按显示方向识别、"
                   "坐标却是内容坐标系，照画会把中文画歪，故保留原文）"
                   % stats["rotated_pages"])
    if stats["kept_lines"]:
        detail += "，%d 行译文放不下已保留原文" % stats["kept_lines"]
    if stats["ocr_error"]:
        detail += "；部分页 OCR 报错：%s" % stats["ocr_error"]
    detail += ("；图片 输入 %d 张 -> 输出 %d 张（数量一致 ✅）；"
               "OCR %s @%ddpi；字体 %s"
               % (n_img_in, n_img_out, language, dpi,
                  pt.CJK_FONTFILE or "china-s（内嵌兜底）"))
    return {
        "units": stats["units"],
        "chars_in": stats["chars_in"],
        "chars_out": stats["chars_out"],
        "skipped": stats["skipped"],
        "pages": pages,
        "detail": detail,
        # 下面是本模块特有的补充统计（契约允许额外键）
        "ocr_words": stats["ocr_words"],
        "empty_pages": stats["empty_pages"],
        "drawn_units": stats["drawn_units"],
        "drawn_lines": stats["drawn_lines"],
        "kept_lines": stats["kept_lines"],
        "rotated_pages": stats["rotated_pages"],
        "ocr_lang": language,
        "ocr_dpi": dpi,
    }


# ================================================================ 手工自测

if __name__ == "__main__":  # pragma: no cover
    import argparse

    ap = argparse.ArgumentParser(description="扫描版 PDF OCR 原位翻译（离线假翻译，仅自测）")
    ap.add_argument("input")
    ap.add_argument("output")
    ap.add_argument("--lang", default=DEFAULT_OCR_LANG)
    ap.add_argument("--dpi", type=int, default=DEFAULT_OCR_DPI)
    ap.add_argument("--font-scale", type=float, default=0.92)
    a = ap.parse_args()

    ok, why = ocr_probe(a.lang, a.dpi)
    print("OCR 可用:", ok, why)

    def _fake(texts, ctx=None):
        return ["【译%d】%s" % (i, t) for i, t in enumerate(texts)]

    print(run(a.input, a.output, translate=_fake,
              options={"ocr_lang": a.lang, "ocr_dpi": a.dpi,
                       "font_scale": a.font_scale},
              progress=lambda d, t, note="": print("  %d/%d %s" % (d, t, note))))
