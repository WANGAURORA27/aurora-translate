"""docbridge · Word(docx) 翻译管线。

一个模块同时支持两种模式（由 ``options["__mode__"]`` 决定）：

* ``inplace``   原位替换：整段翻译后把译文写回段落的第一个有文本 run，
                其余 run 的文本清空，从而尽量保留段落样式与字体。
* ``bilingual`` 双语对照：在每个原文段落之后紧邻插入一个新段落承载译文。

覆盖范围：正文段落、表格（含嵌套表格）单元格段落、各 section 的页眉/页脚。

约束（见 CONTRACT.md）：不联网、不读密钥、不写全局可变状态、不改源文件、
批量调用 ``translate``、原子落盘、失败抛 ``RuntimeError("中文原因")``。
"""

from __future__ import annotations

import copy
import os
import re

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor
from docx.text.run import Run

FORMAT = "docx"
MODE = "both"
LABEL = "Word · 原位替换 / 双语对照"
NOTE = (
    "原位替换：译文覆盖原文，尽量保留原段落样式与字体（含标题、表格、页眉页脚）；"
    "双语对照：每段原文后插入一段深蓝色译文便于校对。"
    "含图片/超链接/域代码(TOC 等)的段落、空段落、纯数字与已是中文的段落会被跳过。"
)
OPTIONS = ["keep_original_style", "font_scale"]

# ---------------------------------------------------------------- 常量与正则

_MAX_TABLE_DEPTH = 6
_BATCH_MIN = 30
_BATCH_MAX = 100
_BATCH_DEFAULT = 40
_DEFAULT_EAST_ASIA_FONT = "宋体"
_DEFAULT_TRANSLATION_COLOR = "1F4E79"  # 深蓝，一眼看出是译文

# 段落里出现这些元素时直接跳过：清空 run 文本会破坏它们
_PROTECTED_TAGS = tuple(
    qn(tag)
    for tag in (
        "w:drawing",
        "w:pict",
        "w:object",
        "w:embeddedObject",
        "w:hyperlink",
        "w:fldChar",
        "w:instrText",
        "w:fldSimple",
        "w:txbxContent",
        "w:footnoteReference",
        "w:endnoteReference",
        "w:commentReference",
        "w:commentRangeStart",
        "w:commentRangeEnd",
    )
)

# XML 1.0 不允许的控制字符，避免译文里混入导致序列化失败
_XML_ILLEGAL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")
_LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)  # 任意语言的“字母”，数字/符号不算
_LATIN_RE = re.compile(r"[A-Za-z]")
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]")

_MODE_ALIASES = {
    "inplace": "inplace",
    "in_place": "inplace",
    "replace": "inplace",
    "原位": "inplace",
    "原位替换": "inplace",
    "bilingual": "bilingual",
    "dual": "bilingual",
    "对照": "bilingual",
    "双语": "bilingual",
    "双语对照": "bilingual",
}


# ---------------------------------------------------------------- 小工具


def _clean(text: str) -> str:
    """去掉 XML 非法字符，并把 CRLF 归一化。"""
    return _XML_ILLEGAL_RE.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))


def _resolve_mode(opts: dict) -> str:
    """按契约从 ``options["__mode__"]`` 读模式，兼容 mode / bilingual 键。"""
    raw = opts.get("__mode__")
    if raw is None:
        if isinstance(opts.get("bilingual"), bool):
            raw = "bilingual" if opts["bilingual"] else "inplace"
        else:
            raw = opts.get("mode", "inplace")
    mode = _MODE_ALIASES.get(str(raw).strip().lower())
    if mode is None:
        raise RuntimeError(f"未知的 docx 处理模式：{raw!r}（应为 inplace 或 bilingual）")
    return mode


def _batch_size(opts: dict) -> int:
    try:
        size = int(opts.get("batch_size", _BATCH_DEFAULT))
    except (TypeError, ValueError):
        size = _BATCH_DEFAULT
    return max(_BATCH_MIN, min(_BATCH_MAX, size))


def _looks_chinese(text: str) -> bool:
    cjk = len(_CJK_RE.findall(text))
    if cjk == 0:
        return False
    latin = len(_LATIN_RE.findall(text))
    if latin == 0:
        return True
    return cjk / float(cjk + latin) >= 0.6


def _classify(text: str, target_is_chinese: bool):
    """返回 (是否需要翻译, 跳过原因)。"""
    stripped = text.strip()
    if not stripped:
        return False, "blank"
    if not _LETTER_RE.search(stripped):
        return False, "symbol"
    if target_is_chinese and _looks_chinese(stripped):
        return False, "already_cn"
    return True, ""


def _has_protected_element(paragraph) -> bool:
    """段落里是否含图片/超链接/域代码等不能碰的元素。"""
    for node in paragraph._p.iter():
        if node.tag in _PROTECTED_TAGS:
            return True
    return False


def _set_east_asia_font(run, font_name: str) -> None:
    """只设置东亚字体，**绝不**动 ascii/hAnsi（原文的 Latin 字体要保留）。"""
    if not font_name:
        return
    rPr = run._r.get_or_add_rPr()
    rFonts = rPr.get_or_add_rFonts()
    rFonts.set(qn("w:eastAsia"), font_name)
    rFonts.set(qn("w:hint"), "eastAsia")


def _first_text_run(paragraph):
    """段落里第一个有可见文本的 run（返回 Run 或 None）。"""
    for run in paragraph.runs:
        if run.text and run.text.strip():
            return run
    return None


# ---------------------------------------------------------------- 单元收集


def _iter_paragraphs(container, part: str, depth: int = 0):
    """递归产出 (Paragraph, part) —— 段落 + 表格（含嵌套）单元格段落。"""
    try:
        paragraphs = list(container.paragraphs)
    except Exception:  # 某些容器（异常 XML）取不到段落，跳过即可
        paragraphs = []
    for paragraph in paragraphs:
        yield paragraph, part

    if depth >= _MAX_TABLE_DEPTH:
        return
    try:
        tables = list(container.tables)
    except Exception:
        tables = []
    for table in tables:
        try:
            rows = list(table.rows)
        except Exception:
            continue
        for row in rows:
            for cell in row.cells:
                yield from _iter_paragraphs(cell, part, depth + 1)


def _iter_hdrftr_containers(document):
    """产出 (容器, part 名)：各 section 已定义的页眉/页脚（合并单元格式去重）。"""
    seen = set()
    slots = (
        ("header", "header"),
        ("footer", "footer"),
        ("first_page_header", "header"),
        ("first_page_footer", "footer"),
        ("even_page_header", "header"),
        ("even_page_footer", "footer"),
    )
    for section in document.sections:
        for attr, part in slots:
            try:
                container = getattr(section, attr)
            except Exception:
                continue
            try:
                if container.is_linked_to_previous:
                    # 继承上一节的页眉/页脚：内容已由定义它的那一节处理，避免重复翻译
                    continue
                key = str(container._definition.partname)
            except Exception:
                key = f"{id(container)}"
            if key in seen:
                continue
            seen.add(key)
            yield container, part


def _collect_units(document, mode: str, target_is_chinese: bool):
    """遍历全文，收集待翻译单元与跳过统计。"""
    units = []
    stats = {"blank": 0, "symbol": 0, "already_cn": 0, "locked": 0}
    seen_paras = set()
    keepalive = []  # 持有 lxml 代理引用，保证 id() 稳定（合并单元格会重复出现）

    def visit(paragraph, part):
        key = id(paragraph._p)
        if key in seen_paras:
            return
        seen_paras.add(key)
        keepalive.append(paragraph._p)

        text = "".join(run.text for run in paragraph.runs)
        if _has_protected_element(paragraph):
            stats["locked"] += 1
            return
        need, reason = _classify(text, target_is_chinese)
        if not need:
            stats[reason] += 1
            return
        style_name = ""
        try:
            style_name = paragraph.style.name or ""
        except Exception:
            pass
        units.append(
            {
                "para": paragraph,
                "part": part,
                "text": text,
                "style": style_name,
                "context": {"kind": "docx", "mode": mode, "part": part, "style": style_name},
            }
        )

    for paragraph, part in _iter_paragraphs(document, "body"):
        visit(paragraph, part)
    for container, part in _iter_hdrftr_containers(document):
        for paragraph, sub_part in _iter_paragraphs(container, part):
            visit(paragraph, sub_part)

    stats["skipped"] = stats["blank"] + stats["symbol"] + stats["already_cn"] + stats["locked"]
    return units, stats, keepalive


# ---------------------------------------------------------------- 写回


def _apply_inplace(unit, translated: str, opts: dict, target_is_chinese: bool) -> None:
    paragraph = unit["para"]
    runs = list(paragraph.runs)
    target = None
    target_idx = -1
    for idx, run in enumerate(runs):
        if run.text and run.text.strip():
            target = run
            target_idx = idx
            break
    if target is None:  # 理论上不会发生（收集阶段已保证有文本）
        return

    if opts.get("keep_original_style", True) is False:
        rPr = target._r.rPr
        if rPr is not None:
            target._r.remove(rPr)

    target.text = _clean(translated)
    for idx, run in enumerate(runs):
        if idx != target_idx and run.text:
            run.text = ""

    if target_is_chinese:
        _set_east_asia_font(target, str(opts.get("east_asia_font") or _DEFAULT_EAST_ASIA_FONT))

    # 只有显式传了 font_scale 才缩放（避免默认 0.92 在 docx 里意外缩小字号）
    if "font_scale" in opts:
        try:
            scale = float(opts["font_scale"])
        except (TypeError, ValueError):
            scale = 0.0
        size = target.font.size
        if scale > 0 and scale != 1.0 and size is not None:
            target.font.size = Pt(round(size.pt * scale, 2))


def _build_bilingual_element(unit, translated: str, opts: dict, target_is_chinese: bool):
    """基于原段落 XML 克隆出译文段落元素（保留 pPr / 样式），返回元素。"""
    paragraph = unit["para"]
    new_p = copy.deepcopy(paragraph._p)

    pPr = new_p.find(qn("w:pPr"))
    for child in list(new_p):
        if child.tag != qn("w:pPr"):
            new_p.remove(child)
    if pPr is not None:
        # 克隆来的分节符必须去掉，否则会凭空多出一个分节
        for sect in pPr.findall(qn("w:sectPr")):
            pPr.remove(sect)

    new_r = OxmlElement("w:r")
    if pPr is None:
        new_p.insert(0, new_r)
    else:
        new_p.append(new_r)

    # 复制原首个 run 的直接格式（粗体/字号等），再覆盖颜色
    src_run = _first_text_run(paragraph)
    if src_run is not None:
        src_rPr = src_run._r.rPr
        if src_rPr is not None:
            copied = copy.deepcopy(src_rPr)
            for tag in ("w:color", "w:highlight"):
                for node in copied.findall(qn(tag)):
                    copied.remove(node)
            new_r.insert(0, copied)

    new_run = Run(new_r, paragraph)
    new_run.text = _clean(translated)

    color = str(opts.get("translation_color") or _DEFAULT_TRANSLATION_COLOR).lstrip("#")
    try:
        new_run.font.color.rgb = RGBColor.from_string(color.upper())
    except Exception:
        new_run.font.color.rgb = RGBColor.from_string(_DEFAULT_TRANSLATION_COLOR)
    new_run.font.highlight_color = None  # 明确不要高亮

    if target_is_chinese:
        _set_east_asia_font(new_run, str(opts.get("east_asia_font") or _DEFAULT_EAST_ASIA_FONT))
    return new_p


def _apply_bilingual(pairs, opts: dict, target_is_chinese: bool) -> None:
    """pairs 已经按文档顺序收集好；先收集元素再逐个 addnext，避免迭代器被扰动。"""
    for unit, translated in pairs:
        src_el = unit["para"]._p
        new_el = _build_bilingual_element(unit, translated, opts, target_is_chinese)
        addnext = getattr(src_el, "addnext", None)
        if callable(addnext):
            addnext(new_el)
        else:  # lxml 未提供时的等价实现
            parent = src_el.getparent()
            parent.insert(parent.index(src_el) + 1, new_el)


# ---------------------------------------------------------------- 主入口


def run(src_path, out_path, *, translate, progress=None, options=None) -> dict:
    """按契约执行一次 docx 翻译，返回统计 dict。"""
    if not callable(translate):
        raise RuntimeError("缺少可调用的翻译回调 translate")
    opts = dict(options) if options else {}
    mode = _resolve_mode(opts)
    target_lang = str(opts.get("target_lang") or "zh-Hans")
    target_is_chinese = target_lang.strip().lower().startswith("zh")

    src_path = str(src_path)
    out_path = str(out_path)
    if not os.path.isfile(src_path):
        raise RuntimeError(f"源文件不存在或不是文件：{src_path}")

    try:
        document = Document(src_path)
    except Exception as exc:  # PackageNotFoundError / BadZipFile / lxml 错误……
        raise RuntimeError(f"无法打开 Word 文档（不是有效的 .docx 或文件损坏）：{exc}") from exc

    try:
        units, stats, _keepalive = _collect_units(document, mode, target_is_chinese)
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"解析 Word 文档结构失败：{exc}") from exc

    total = len(units)
    batch_size = _batch_size(opts)
    chars_in = 0
    chars_out = 0
    translated_units = []

    def report(done, note=""):
        if callable(progress):
            try:
                progress(int(done), int(total), note)
            except Exception:
                pass  # 进度上报失败不应连累翻译任务

    report(0, f"解析完成：待翻译 {total} 段，跳过 {stats['skipped']} 段")

    for start in range(0, total, batch_size):
        chunk = units[start : start + batch_size]
        texts = [u["text"] for u in chunk]
        result = translate(texts, chunk[0]["context"])
        if not isinstance(result, (list, tuple)):
            raise RuntimeError(f"翻译返回类型异常：期望 list，实际 {type(result).__name__}")
        if len(result) != len(texts):
            raise RuntimeError(f"翻译返回数量不符：期望 {len(texts)}，实际 {len(result)}")
        for unit, translated in zip(chunk, result):
            text_out = "" if translated is None else str(translated)
            chars_in += len(unit["text"])
            chars_out += len(text_out)
            translated_units.append((unit, text_out))
        report(start + len(chunk), f"已翻译 {start + len(chunk)}/{total} 段")

    try:
        if mode == "bilingual":
            _apply_bilingual(translated_units, opts, target_is_chinese)
        else:
            for unit, translated in translated_units:
                _apply_inplace(unit, translated, opts, target_is_chinese)
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"写回译文失败：{exc}") from exc

    # ---- 原子落盘 ----
    parent = os.path.dirname(os.path.abspath(out_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    part_path = out_path + ".part"
    try:
        document.save(part_path)
        os.replace(part_path, out_path)
    except Exception as exc:
        if os.path.exists(part_path):
            try:
                os.remove(part_path)
            except OSError:
                pass
        raise RuntimeError(f"保存结果文件失败：{exc}") from exc

    report(total, "写入完成")
    mode_label = "双语对照" if mode == "bilingual" else "原位替换"
    paragraphs_total = total + stats["skipped"]
    detail = (
        f"Word {mode_label}：翻译 {total} 段（{chars_in} → {chars_out} 字符），"
        f"跳过 {stats['skipped']} 段"
        f"（空 {stats['blank']}／数字符号 {stats['symbol']}／已是中文 {stats['already_cn']}"
        f"／含图片或域 {stats['locked']}）；已处理正文、表格与页眉页脚。"
    )
    return {
        "units": total,
        "chars_in": chars_in,
        "chars_out": chars_out,
        "skipped": stats["skipped"],
        "pages": paragraphs_total,  # docx 无页数概念，填段落数
        "detail": detail,
        "mode": mode,
        "paragraphs": paragraphs_total,
    }
