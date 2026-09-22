#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PDF 原位翻译工具（英文 -> 简体中文）

原理：
  1. 用 PyMuPDF 提取每页所有文本行（含精确坐标、字号、颜色）。
  2. 把英文行批量发给 DeepSeek API 翻译（失败时回退 Google 免费接口）。
  3. 用白色矩形盖住原文，然后在【同一位置】用内嵌中文字体(china-s)
     以"能放下"的字号重绘译文 —— 图片、矢量图完全不动。

用法：
  python3 pdf_translate.py input.pdf [output.pdf] [--engine auto|deepseek|google]
                        [--sim-bold] [--pages 1-3,5] [--font-scale 0.92]

输出默认 <input>_zh.pdf。
"""

import argparse
import json
import math
import os
import re
import sys
import time
import urllib.request

import fitz  # PyMuPDF

CJK_FONT = "china-s"  # 兜底字体（PyMuPDF 内嵌，无需外部文件）
CJK_FONTFILE = None    # 检测到的系统字体（宋体等，拉丁为比例字形）
CJK_FONT_OBJ = None    # fitz.Font 对象（度量用）
# 候选系统字体：优先宋体（正文衬线，拉丁比例）
FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
]
MIN_TRANSLATABLE_SIZE = 5.0  # 拟合字号低于此值的行跳过（窄格/缩写）
LINE_PITCH_FACTOR = 1.15  # 行距 = 字号 * 该系数（CJK 视觉行距）
MIN_SIZE = 3.5            # 允许的最小字号（pt）
MAX_CHUNK_LINES = 25      # 每次 API 请求的行数
MAX_CHUNK_CHARS = 4000    # 每次 API 请求的最大字符数


FALLBACK_FONTFILE = None   # 缺字回退字体（Arial Unicode，覆盖数学符号）
FALLBACK_FONT_OBJ = None
FALLBACK_CANDIDATES = [
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
]
# 常见缺字候选（用于构建 MISSING 集合；命中即用回退字体）
MISSING_CANDIDATES = ("–†‡•⇒⇔∂∇∉⊂⊇⋅∃∄∅∝∫∮≅∠∥⊥⊕⊗∑∏"
                      "∞≡≤≥≠≈±×÷°′″‰§¶←→↔↑↓⇒⇔∈∉⊂⊆⊇∪∩∀¬∧∨")
_GLYPH_CACHE = {}


def init_cjk_font(font_spec="auto"):
    """初始化中文字体。font_spec: auto | china-s | 字体文件路径。"""
    global CJK_FONT, CJK_FONTFILE, CJK_FONT_OBJ
    global FALLBACK_FONTFILE, FALLBACK_FONT_OBJ
    if font_spec == "china-s":
        CJK_FONT, CJK_FONTFILE = "china-s", None
    elif font_spec == "auto":
        CJK_FONT, CJK_FONTFILE = "china-s", None
        for p in FONT_CANDIDATES:
            if os.path.isfile(p):
                try:
                    fitz.Font(fontfile=p)
                    CJK_FONT, CJK_FONTFILE = "F0", p
                    break
                except Exception:
                    continue
    else:  # 显式路径
        CJK_FONT, CJK_FONTFILE = "F0", font_spec
    if CJK_FONTFILE:
        CJK_FONT_OBJ = fitz.Font(fontfile=CJK_FONTFILE)
    else:
        CJK_FONT_OBJ = fitz.Font(CJK_FONT)
    # 回退字体：Arial Unicode（数学符号全覆盖）
    FALLBACK_FONTFILE = FALLBACK_FONT_OBJ = None
    for p in FALLBACK_CANDIDATES:
        if os.path.isfile(p):
            try:
                FALLBACK_FONT_OBJ = fitz.Font(fontfile=p)
                FALLBACK_FONTFILE = p
                break
            except Exception:
                continue
    _GLYPH_CACHE.clear()


def _songti_has(ch):
    """主字体是否有该字形（带缓存，动态判定，不依赖固定候选列表）。"""
    if CJK_FONT_OBJ is None:
        return True
    ok = _GLYPH_CACHE.get(ch)
    if ok is None:
        ok = bool(CJK_FONT_OBJ.has_glyph(ord(ch)))
        _GLYPH_CACHE[ch] = ok
    return ok


def _needs_fallback(ch):
    """该字符主字体缺字形且回退字体有 → 用回退字体。"""
    return (FALLBACK_FONT_OBJ is not None and CJK_FONTFILE is not None
            and not _songti_has(ch))


def text_width(text, size):
    """按字形可用性分字体测量宽度（缺字用回退字体度量）。"""
    if not text:
        return 0.0
    if CJK_FONTFILE is None or FALLBACK_FONT_OBJ is None:
        return CJK_FONT_OBJ.text_length(text, fontsize=size)
    if not any(_needs_fallback(c) for c in text):
        return CJK_FONT_OBJ.text_length(text, fontsize=size)
    total = 0.0
    for ch in text:
        if _needs_fallback(ch):
            total += FALLBACK_FONT_OBJ.text_length(ch, fontsize=size)
        else:
            total += CJK_FONT_OBJ.text_length(ch, fontsize=size)
    return total

LATIN_RE = re.compile(r"[A-Za-z]")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
NUMBERISH_RE = re.compile(r"^[\d\s\W_]*$")  # 纯数字/符号/空白


# ---------------------------------------------------------------- 翻译引擎

def load_api_key():
    """从 DSH 配置读取 DeepSeek API key（不回显内容）。"""
    for path in (os.path.expanduser("~/.dsh/.credentials.yaml"),
                 os.path.expanduser("~/.dsh/settings.yaml")):
        try:
            txt = open(path, encoding="utf-8").read()
        except OSError:
            continue
        m = re.search(r"DEEPSEEK_API_KEY:\s*[\"']?([^\"'\s]+)", txt)
        if m:
            return m.group(1)
    return None


def call_deepseek(system, user, key, model="deepseek-chat", json_mode=False):
    url = "https://api.deepseek.com/chat/completions"
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
        "max_tokens": 4000,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + key},
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


SYSTEM_PROMPT = (
    "You are a professional technical translator. Translate the given English "
    "PDF text into natural Simplified Chinese (简体中文). Keep technical terms "
    "accurate and concise. Output ONLY valid JSON, nothing else."
)

BATCH_PROMPT = (
    "Translate each numbered English line below into Simplified Chinese.\n"
    "Return ONLY a JSON object mapping line numbers to translations, e.g.\n"
    '{{"1": "译文1", "2": "译文2"}}.\n'
    "Rules:\n"
    "- The JSON object MUST contain exactly the keys 1..{n} ({n} keys).\n"
    "- Each value translates EXACTLY ONE input line, written as a single "
    "line of text (no line breaks inside a value).\n"
    "- Output SIMPLIFIED Chinese (简体中文) only. Never use Traditional "
    "Chinese (繁體).\n"
    "- NEVER output ellipsis (……, …, ...) or placeholders. NEVER output "
    "meta comments or instructions such as \"原文未提供\" or \"保持原样\". "
    "Always produce the translation itself.\n"
    "- IMPORTANT: each input line is an INDEPENDENT FRAGMENT of a paragraph. "
    "Lines may end or start mid-word due to line wrapping (e.g. \"pub-\" "
    "or \"lic policy.\"). Translate each fragment literally, fragment by "
    "fragment. Never merge two lines into one value, never complete a "
    "sentence across lines, never repeat the same translation for "
    "different lines, never invent text.\n"
    "- If a line is pure numbers/symbols/code, keep it unchanged.\n"
    "- No text outside the JSON object.\n"
    "{extra}\n"
    "Lines:\n{numbered}"
)


def _parse_json_reply(reply, n):
    """从回复中解析 {"1": ...} 映射，返回 list[str]（长度 n）或 None。"""
    text = reply.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        obj = json.loads(text[start:end + 1])
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    result = []
    for i in range(1, n + 1):
        v = obj.get(str(i))
        if v is None or not isinstance(v, str) or not v.strip():
            return None
        result.append(v.strip())
    return result


def _validate_result(values, lines):
    """行数一致、无重复值（输入不同时）、无换行、无明显合并。"""
    if len(values) != len(lines):
        return False
    seen = {}
    for v, ln in zip(values, lines):
        if not v or "\n" in v or "\r" in v:
            return False
        if v in seen and seen[v] != ln:
            return False
        seen[v] = ln
        if len(ln) >= 10 and len(v) > 2.2 * len(ln) + 30:
            return False  # 疑似合并了多行
    return True


def translate_batch_deepseek(lines, key):
    """lines: list[str] -> list[str]，长度一致。彻底失败返回 None。"""
    if not lines:
        return []
    numbered = "\n".join(f"{i+1}. {ln}" for i, ln in enumerate(lines))
    extra = ""
    for attempt in range(3):
        prompt = BATCH_PROMPT.format(n=len(lines), numbered=numbered,
                                     extra=extra)
        try:
            reply = call_deepseek(SYSTEM_PROMPT, prompt, key, json_mode=True)
        except Exception:
            try:
                reply = call_deepseek(SYSTEM_PROMPT, prompt, key,
                                      json_mode=False)
            except Exception as exc:  # noqa: BLE001
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
                else:
                    print(f"  [warn] DeepSeek 请求失败: {exc}", file=sys.stderr)
                continue
        result = _parse_json_reply(reply, len(lines))
        if result is not None and _validate_result(result, lines):
            return result
        extra = ("CRITICAL: your previous answer was rejected because lines "
                 "were merged, repeated, or split. Translate each line "
                 "strictly one-to-one, keeping every fragment separate.")
        if attempt < 2:
            print("  [warn] DeepSeek 结果校验未通过，重试中", file=sys.stderr)
    # 批量失败：逐行单独请求
    print("  [warn] 批量翻译失败，改为逐行请求", file=sys.stderr)
    result = []
    for ln in lines:
        single = translate_batch_deepseek([ln], key)
        result.append(single[0] if single else None)
    return result


def translate_batch_google(lines):
    """Google 免费接口兜底。返回 list[str] 或 None。"""
    try:
        _vendor = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "vendor")
        if os.path.isdir(_vendor):
            sys.path.insert(0, _vendor)
        from deep_translator import GoogleTranslator
        tr = GoogleTranslator(source="en", target="zh-CN")
        try:
            return tr.translate_batch(lines)
        except Exception:
            return [tr.translate(ln) for ln in lines]
    except Exception as exc:  # noqa: BLE001
        print(f"  [warn] Google 翻译失败: {exc}", file=sys.stderr)
        return None


def translate_lines(lines, engine):
    """按引擎批量翻译；返回 dict[int, str]。auto 仅用 DeepSeek（Google 兜底
    不可靠：可能超时或输出繁体，故不再自动回退）。"""
    out = {}
    todo = [(i, ln) for i, ln in enumerate(lines)
            if ln.strip() and LATIN_RE.search(ln) and not NUMBERISH_RE.match(ln)]
    if not todo:
        return out
    key = load_api_key() if engine in ("deepseek", "auto") else None

    chunks, chunk = [], []
    for i, ln in todo:
        chunk.append((i, ln))
        n_chars = sum(len(l) for _, l in chunk)
        if len(chunk) >= MAX_CHUNK_LINES or n_chars >= MAX_CHUNK_CHARS:
            chunks.append(chunk)
            chunk = []
    if chunk:
        chunks.append(chunk)

    for c in chunks:
        idxs = [i for i, _ in c]
        texts = [ln for _, ln in c]
        translated = None
        if key:
            translated = translate_batch_deepseek(texts, key)
        if translated is None and engine == "google":
            translated = translate_batch_google(texts)
        if translated is None:
            continue
        for j, i in enumerate(idxs):
            t = translated[j]
            if t and t.strip():
                out[i] = t.strip()
    return out


# ---------------------------------------------------------------- 排版

def wrap_text(text, size, width):
    """按宽度贪心换行（支持 CJK 无空格长串），并应用中文禁则。返回行列表。"""
    text = text.replace("\r", "").replace("\n", " ")
    if not text:
        return [""]
    tokens = re.findall(r"[^\s]+|\s+", text)
    lines, cur, cur_w = [], "", 0.0
    for tok in tokens:
        if not tok.strip():
            if cur:
                cur += " "
                cur_w += text_width(" ", size)
            continue
        tok_w = text_width(tok, size)
        if tok_w > width:  # 超长 token（如无空格中文），逐字符拆
            for ch in tok:
                ch_w = text_width(ch, size)
                if cur and cur_w + ch_w > width:
                    lines.append(cur.rstrip())
                    cur, cur_w = "", 0.0
                cur += ch
                cur_w += ch_w
            continue
        if cur and cur_w + tok_w > width:
            lines.append(cur.rstrip())
            cur, cur_w = "", 0.0
        cur += tok
        cur_w += tok_w
    if cur.strip():
        lines.append(cur.rstrip())
    lines = fix_kinsoku(lines, size, width)
    return lines or [""]


# 行首禁则字符：不能出现在行首（并入上一行行尾）
KINSOKU_HEAD = "，。；：？！、）》」』”’…"


def fix_kinsoku(lines, size, width):
    """中文禁则：行首不得出现闭标点。把行首闭标点并入上一行（容差放宽到可容纳
    一个全角标点）。"""
    out = []
    tol = size * 1.2
    for ln in lines:
        while ln and ln[0] in KINSOKU_HEAD and out:
            candidate = out[-1] + ln[0]
            if text_width(candidate, size) <= width + tol:
                out[-1] = candidate
                ln = ln[1:]
            else:
                break
        out.append(ln)
    return out


def layout(text, size, width):
    """返回 (行列表, 总高度)。"""
    lines = wrap_text(text, size, width)
    height = len(lines) * size * LINE_PITCH_FACTOR
    return lines, height


def fit_size(text, width, height, max_size, min_size=MIN_SIZE):
    """在给定宽度/高度内找到能放下 text 的最大字号。"""
    size = max_size
    while size >= min_size:
        lines, need = layout(text, size, width)
        if need <= height + 0.5 or len(lines) == 1:
            return size
        size -= 0.5
    return min_size


def _is_cjk(ch):
    """纯中文字符（4e00-9fff），用于两端对齐的字距边界判定。"""
    return "\u4e00" <= ch <= "\u9fff"


def _insert_char(page, pt, text, size, color, render_mode, border_width):
    """插入文本（单字符缺字时用回退字体；整段文本走主字体）。"""
    if len(text) == 1 and _needs_fallback(text):
        page.insert_text(pt, text, fontname="F1", fontfile=FALLBACK_FONTFILE,
                         fontsize=size, color=color,
                         render_mode=render_mode, border_width=border_width,
                         overlay=True)
    elif CJK_FONTFILE:
        page.insert_text(pt, text, fontname=CJK_FONT, fontfile=CJK_FONTFILE,
                         fontsize=size, color=color,
                         render_mode=render_mode, border_width=border_width,
                         overlay=True)
    else:
        page.insert_text(pt, text, fontname=CJK_FONT, fontsize=size,
                         color=color, render_mode=render_mode,
                         border_width=border_width, overlay=True)


def _draw_text(page, pt, text, size, color, render_mode=0, border_width=0.0):
    """整串绘制：含缺字时按字形分段（CJK 段用主字体，缺字单画用回退字体）。"""
    if not text:
        return
    if not any(_needs_fallback(c) for c in text):
        _insert_char(page, pt, text, size, color, render_mode, border_width)
        return
    x = pt.x
    buf = ""
    for ch in text:
        if _needs_fallback(ch):
            if buf:
                _insert_char(page, fitz.Point(x, pt.y), buf, size, color,
                             render_mode, border_width)
                x += text_width(buf, size)
                buf = ""
            _insert_char(page, fitz.Point(x, pt.y), ch, size, color,
                         render_mode, border_width)
            x += text_width(ch, size)
        else:
            buf += ch
    if buf:
        _insert_char(page, fitz.Point(x, pt.y), buf, size, color,
                     render_mode, border_width)


def _justify_plan(line, size, width):
    """两端对齐方案：只在相邻两个中文字符之间分配拉伸，拉丁词/数字/标点
    保持紧凑（不拆散 IBM、——、第2章 等）。拉伸过大返回 None（保持左对齐）。"""
    chars = list(line)
    gaps = [i for i in range(len(chars) - 1)
            if _is_cjk(chars[i]) and _is_cjk(chars[i + 1])]
    if not gaps:
        return None
    extra = width - text_width(line, size)
    if extra < 0.5:
        return None
    per = extra / len(gaps)
    if per > size * 0.45:
        return None
    return per, set(gaps)


def _apply_specials(text, specials):
    """把原文的上下标 run 顺序映射到译文文本上，返回 {char_idx: kind}。"""
    marks = {}
    search_from = 0
    for run_text, kind in specials:
        if not run_text:
            continue
        idx = text.find(run_text, search_from)
        if idx < 0:
            m = re.search(r"\d+", run_text)  # 退化：只匹配数字部分
            if m:
                idx = text.find(m.group(0), search_from)
                run_text = m.group(0)
        if idx >= 0:
            for k in range(idx, idx + len(run_text)):
                marks[k] = kind
            search_from = idx + len(run_text)
    return marks


def draw_translation(page, x0, y0, y1, text, size, color, width,
                     render_mode=0, border_width=0.0, baseline=None,
                     justify=False, specials=None):
    """在 (x0, y0)-(x0+width, y1) 区域内绘制中文，右缘对齐 width。

    baseline: 原英文基线 y。给定则把首行基线锚定到原基线。
    justify:  非末行按中文内部字距拉伸两端对齐（拉伸过大时自动放弃）。
    specials: 原文上下标 run [(text, 'sub'|'super')]，单行时映射到译文重绘。
    """
    lines, need = layout(text, size, width)
    pitch = size * LINE_PITCH_FACTOR
    if baseline is not None:
        first_base = baseline
        ink_top = first_base - size * 0.82
        ink_bottom = first_base + (len(lines) - 1) * pitch + size * 0.18
        if ink_top < y0 - 0.5:
            first_base = y0 + size * 0.82
        if ink_bottom > y1 + 0.5:
            first_base = y1 - (len(lines) - 1) * pitch - size * 0.18
    else:
        top = y0 + max(0.0, ((y1 - y0) - need) / 2.0)
        first_base = top + size * 0.82
    marks = (_apply_specials(text, specials)
             if specials and len(lines) == 1 else {})
    for i, ln in enumerate(lines):
        if not ln:
            continue
        pt = fitz.Point(x0, first_base + i * pitch)
        if marks:
            # 上下标保真：逐字符绘制，上下标缩小并偏移基线
            x = x0
            for j, ch in enumerate(ln):
                kind = marks.get(j)
                fs2 = size * (0.65 if kind else 1.0)
                off = (0.30 * size if kind == "sub"
                       else -0.20 * size if kind == "super" else 0.0)
                _insert_char(page, fitz.Point(x, pt.y + off), ch, fs2,
                             color, render_mode, border_width)
                x += text_width(ch, fs2 if kind else size)
            continue
        # 两端对齐：段中行全部对齐；段末行仅最后一个视觉行不齐（中文排版惯例）
        is_last_visual = (i == len(lines) - 1)
        do_justify = justify or not is_last_visual
        if do_justify:
            plan = _justify_plan(ln, size, width)
            if plan:
                per, gaps = plan
                x = x0
                for j, ch in enumerate(ln):
                    _insert_char(page, fitz.Point(x, pt.y), ch, size, color,
                                 render_mode, border_width)
                    x += text_width(ch, size) + (per if j in gaps else 0.0)
                continue
        _draw_text(page, pt, ln, size, color, render_mode, border_width)


def group_paragraphs(work):
    """把相邻同风格的行聚成段落，返回 [[(idx, rect, text, size, color, bold, base)], ...]。

    先把行按 x 聚类成"列"（同一列 x0 相差 ≤30pt），再在列内按 y 聚段——
    避免多栏/页边术语与正文交错时把正文行拆散，也允许段首缩进同行。
    换段条件：y 回跳（换栏/换列）、行距过大、左缘明显偏移、字号风格突变。
    """
    cols = []
    for item in sorted(work, key=lambda w: w[1].x0):
        if cols and abs(item[1].x0 - cols[-1][-1][1].x0) <= 30:
            cols[-1].append(item)
        else:
            cols.append([item])
    paras = []
    for col in cols:
        items = sorted(col, key=lambda w: (w[1].y0, w[1].x0))
        cur, prev = [], None
        for item in items:
            rect, _t, size = item[1], item[2], item[3]
            if prev is not None:
                dy = rect.y0 - prev[0].y1
                same_x = abs(rect.x0 - prev[0].x0) <= 15
                same_size = abs(size - prev[1]) <= 2.5
                if dy < -4 or dy > 6 or not same_x or not same_size:
                    paras.append(cur)
                    cur = []
            cur.append(item)
            prev = (rect, size)
        if cur:
            paras.append(cur)
    return paras


def block_font_size(para, cjk_scale, trans):
    """段内统一字号：按【中文译文】拟合，取最小并向下取 0.5。

    返回 (fontsize, skip_indices)：拟合字号低于可读下限的行加入 skip_indices
    （保留原文，不覆盖不绘制，如表格窄格里的缩写）。
    """
    x1 = max(r.x1 for _, r, _t, _s, _c, _b, _base, _sp in para)
    fits = []          # (line_idx, fit_size)
    skip = []
    for i, rect, _t, size, _c, _b, _base, _sp in para:
        zh = trans.get(i)
        if not zh:
            continue
        width = max(8.0, x1 - rect.x0)
        max_sz = max(4.0, size * cjk_scale)
        fs = fit_size(zh, width, rect.height, max_sz)
        if fs < MIN_TRANSLATABLE_SIZE:
            skip.append(i)
            continue
        fits.append((i, fs))
    if not fits:
        return MIN_SIZE, skip
    fs = math.floor(min(f for _, f in fits) * 2) / 2.0
    return max(MIN_SIZE, fs), skip


def sample_bg_color(pix, rect, dpi):
    """取行 bbox 顶部条带像素的中位色，作为覆盖矩形底色（防白色块盖住底色）。"""
    y0 = int(rect.y0 * dpi / 72.0)
    y1 = int((rect.y0 + rect.height * 0.10) * dpi / 72.0)
    x0 = int(rect.x0 * dpi / 72.0)
    x1 = int(rect.x1 * dpi / 72.0)
    samples = pix.samples  # 重要：一次取整段缓冲，勿在循环内反复访问属性
    n = pix.n
    width = pix.width
    height = pix.height
    out = []
    for yy in range(max(0, y0), min(height, y1 + 1)):
        row = yy * width
        for xx in range(max(0, x0), min(width, x1)):
            i = (row + xx) * n
            out.append((samples[i], samples[i + 1], samples[i + 2]))
    if not out:
        return (1, 1, 1)
    out.sort()
    c = out[len(out) // 2]
    return (c[0] / 255.0, c[1] / 255.0, c[2] / 255.0)


def color_of(spans):
    """行内最常见的前景色（int -> (r,g,b) 浮点）。"""
    from collections import Counter
    cnt = Counter(sp["color"] for sp in spans)
    c = cnt.most_common(1)[0][0]
    r = ((c >> 16) & 255) / 255.0
    g = ((c >> 8) & 255) / 255.0
    b = (c & 255) / 255.0
    return (r, g, b)


# ---------------------------------------------------------------- 主流程

def collect_lines(page):
    """返回 [(rect, text, size, color, is_bold, is_rotated)]，仅含需翻译的行。"""
    out = []
    d = page.get_text("dict")
    for block in d["blocks"]:
        if block.get("type", 0) != 0:  # 跳过图片块
            continue
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            if not spans:
                continue
            text = "".join(s["text"] for s in spans).strip()
            if not text or not LATIN_RE.search(text):
                continue  # 空行 / 无英文
            if CJK_RE.search(text):
                continue  # 已经是中文的行不动
            if NUMBERISH_RE.match(text):
                continue  # 纯数字/符号
            rect = fitz.Rect(line["bbox"])
            size = max(s["size"] for s in spans)
            base = max(s["origin"][1] for s in spans)  # 原英文基线 y
            # 上下标检测：字号明显小于主字号且基线偏移的 span
            main_size = size
            specials = []
            prev_origin = None
            for s in spans:
                if (prev_origin is not None and s["size"] < main_size * 0.8
                        and s["text"].strip()):
                    kind = ("sub" if s["origin"][1] > prev_origin + 0.3
                            else "super")
                    specials.append((s["text"].strip(), kind))
                prev_origin = s["origin"][1]
            dirx, _diry = spans[0].get("dir", (1, 0))
            rotated = abs(dirx) < 0.5  # 竖排文字
            bold = any(s["flags"] & 16 for s in spans)
            out.append((rect, text, size, color_of(spans), bold, rotated,
                        base, specials))
    return out


def parse_pages(spec, total):
    """解析 '1-3,5' 形式的页选择（1 基），返回 0 基索引列表。"""
    if not spec:
        return list(range(total))
    pages = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            a, b = int(a), int(b)
            pages.update(range(a, b + 1))
        else:
            pages.add(int(part))
    return sorted(p for p in pages if 1 <= p <= total)


def cache_dir_for(in_path):
    """每输入文件一个缓存目录，存逐页译文（断点续跑用）。"""
    base = os.path.splitext(os.path.basename(in_path))[0]
    safe = re.sub(r"[^\w.-]", "_", base)
    return os.path.join("_translate_cache", safe)


def load_page_cache(cache_dir, pno):
    p = os.path.join(cache_dir, f"page_{pno}.json")
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "texts" in data and "translations" in data:
            return data
    except Exception:
        pass
    return None


def save_page_cache(cache_dir, pno, data):
    os.makedirs(cache_dir, exist_ok=True)
    tmp = os.path.join(cache_dir, f"page_{pno}.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, os.path.join(cache_dir, f"page_{pno}.json"))


def process_pdf(in_path, out_path, engine, sim_bold, pages_spec=None,
                cjk_scale=0.92):
    doc = fitz.open(in_path)
    n_img_in = sum(len(p.get_images(full=True)) for p in doc)
    total_lines = translated = skipped = cached_hits = 0
    page_idxs = parse_pages(pages_spec, len(doc))
    cache_dir = cache_dir_for(in_path)

    for pno in page_idxs:
        page = doc[pno - 1]
        lines = collect_lines(page)
        if not lines:
            continue
        # 1) 先翻译（仅水平行；优先用缓存，断点续跑）
        work = [(rect, text, size, color, bold, base, specials)
                for rect, text, size, color, bold, rotated, base, specials
                in lines
                if not rotated]
        texts = [t for _, t, _, _, _, _, _ in work]
        total_lines += len(texts)
        cached = load_page_cache(cache_dir, pno)
        if cached is not None and cached.get("texts") == texts:
            trans = {i: t for i, t in enumerate(cached["translations"]) if t}
            cached_hits += len(texts)
        else:
            trans = translate_lines(texts, engine)
            save_page_cache(cache_dir, pno, {
                "texts": texts,
                "translations": [trans.get(i) for i in range(len(texts))],
            })
        # 2) 按段落统一字号，盖住原文并原位绘制（翻译失败/过窄的行保留原文）
        ok_work = [(i, rect, t, size, color, bold, base, specials)
                   for i, (rect, t, size, color, bold, base, specials)
                   in enumerate(work)
                   if trans.get(i)]
        narrow_skip = set()
        # 页面背景采样（150dpi，供覆盖矩形取底色）
        bg_pix = page.get_pixmap(dpi=150)
        for para in group_paragraphs(ok_work):
            fs, skip = block_font_size(para, cjk_scale, trans)
            x1 = max(r.x1 for _, r, _t, _s, _c, _b, _base, _sp in para)
            para_last = para[-1][0]
            for i, rect, _t, size, color, bold, base, specials in para:
                if i in skip:
                    narrow_skip.add(i)
                    continue
                zh = trans[i]
                zh = zh.lstrip(KINSOKU_HEAD)  # 剥掉译文行首闭标点（模型输出偶尔带）
                # 去掉 CJK 与拉丁/数字之间的空格（中文排版不留）
                zh = re.sub(r"(?<=[A-Za-z0-9]) +(?=[\u4e00-\u9fff])", "", zh)
                zh = re.sub(r"(?<=[\u4e00-\u9fff]) +(?=[A-Za-z0-9])", "", zh)
                if not zh:
                    narrow_skip.add(i)
                    continue
                bg = sample_bg_color(bg_pix, rect, 150)
                page.draw_rect(rect + (-1, -1, 1, 1), color=None, fill=bg,
                               overlay=True)
                rm = 2 if (sim_bold and bold) else 0
                bw = (0.04 * fs) if (sim_bold and bold) else 0.0
                justify = (i != para_last and len(zh) >= 6 and not specials)
                draw_translation(page, rect.x0, rect.y0, rect.y1, zh, fs,
                                 color, max(8.0, x1 - rect.x0), rm, bw,
                                 baseline=base, justify=justify,
                                 specials=specials)
                translated += 1
        for wpos, (rect, _t, size, color, bold, _base, _sp) in enumerate(work):
            if not trans.get(wpos):
                print(f"  [skip] p{pno}: 翻译失败，保留原文: {_t[:40]!r}")
                skipped += 1
        for i in narrow_skip:
            print(f"  [skip] p{pno}: 行宽过窄，保留原文: "
                  f"{work[i][1][:40]!r}")
            skipped += 1
        for rect, _t, _s, _c, _b, rotated, _base, _sp in lines:
            if rotated:
                print(f"  [skip] p{pno}: 竖排文字暂不支持: {_t[:40]!r}")
                skipped += 1
        print(f"  p{pno} 完成")

    n_img_out = sum(len(p.get_images(full=True)) for p in doc)
    doc.save(out_path, garbage=4, deflate=True)
    doc.close()
    print(f"\n输出: {out_path}")
    print(f"统计: 待译行 {total_lines}，已译 {translated}，跳过 {skipped}"
          + (f"，缓存复用 {cached_hits} 行" if cached_hits else ""))
    print(f"图片: 输入 {n_img_in} 张 -> 输出 {n_img_out} 张"
          + ("  ✅ 数量一致" if n_img_in == n_img_out else "  ⚠️ 不一致！"))


def main():
    ap = argparse.ArgumentParser(description="PDF 英文->中文原位翻译（图不动）")
    ap.add_argument("input", help="输入 PDF 路径")
    ap.add_argument("output", nargs="?", default=None, help="输出 PDF 路径")
    ap.add_argument("--engine", default="auto",
                    choices=["auto", "deepseek", "google"])
    ap.add_argument("--sim-bold", action="store_true",
                    help="用加粗描边模拟粗体中文（原粗体行）")
    ap.add_argument("--pages", default=None,
                    help="只处理指定页，如 1-3 或 1,2,5（1 基）")
    ap.add_argument("--font-scale", type=float, default=0.92,
                    help="中文字号相对英文原字号的视觉比例（默认 0.92）")
    ap.add_argument("--font", default="auto",
                    help="中文字体: auto|china-s|字体文件路径（默认 auto=宋体等系统字体）")
    args = ap.parse_args()

    if not os.path.isfile(args.input):
        sys.exit(f"找不到输入文件: {args.input}")
    out = args.output or re.sub(r"\.pdf$", "", args.input) + "_zh.pdf"
    if args.engine == "deepseek" and not load_api_key():
        sys.exit("--engine deepseek 但未找到 DEEPSEEK_API_KEY")
    init_cjk_font(args.font)
    if CJK_FONTFILE:
        print(f"使用中文字体: {CJK_FONTFILE}")
    else:
        print("使用中文字体: china-s（内嵌兜底）")

    process_pdf(args.input, out, args.engine, args.sim_bold, args.pages,
                args.font_scale)


if __name__ == "__main__":
    main()
