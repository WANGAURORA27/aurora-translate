# -*- coding: utf-8 -*-
"""docbridge 管线：PDF · 原位版式保留。

复用既有的生产脚本 ``<仓库目录>/pdf_translate.py``（PyMuPDF 实现：行提取 /
字体度量 / 底色采样 / 中英文字距与禁则 / 缺字回退 / 断点续跑缓存 / 图片零改动）
里的**工具函数与画笔**，但**自己拥有逐页处理循环**，以便实现两件上游做不到的事：

* **换行合并**：PDF 里一个段落被换行拆成多行时，先把明显属于同一段的相邻行合并成
  一个翻译单元整段翻译，再按原行的行盒宽度比例把译文分配回每一行 —— 解决
  "同一句翻两遍 / 半句拼不上"。判据见 ``_can_merge``。
* **自适应版面**：逐行按原行行盒宽度自动缩字号（下限 = 原字号 60%，再低就压字距）、
  基线钳制进"可用空白带"，并保证绘制出的墨迹盒不与同页任何其它文字行相交 ——
  解决"错位 / 字号不对 / 重叠"。实现见 ``_plan_line`` / ``_free_band`` /
  ``_draw_page_inplace``。

为什么不直接改 ``pdf_translate.py``：它的 ``process_pdf`` 逐行独立翻译、按段落统一
字号、并允许把译文折行画到下一行的位置（正是上面两个问题的根因），而这三件事都在
它的循环体里；另外 ``pdf_bilingual`` / ``pdf_ocr`` 也在共用它的 ``draw_translation``
等函数，改上游会波及它们。所以本模块只是**只读复用**上游函数，不修改上游文件。

其余包装职责不变：

* 进度上报：用 ``contextlib.redirect_stdout`` 捕获处理循环的 stdout，边写边解析
  ``p<N> 完成``，据此调用 ``progress(done, total, note)``；结束时补一次
  ``progress(total, total, "完成")``（没有文本的页不会打印完成行）。
* 原子落盘：先写 ``out_path + ".part"``，全部校验通过后 ``os.replace()``；
  任何失败都删除 ``.part``。
* 图片零改动校验：解析自检行「图片: 输入 N 张 -> 输出 M 张」，并用 fitz
  独立复核一遍，N != M 直接抛 ``RuntimeError`` 并删除 ``.part``。
* 线程安全：上游的字体度量与画笔依赖 ``pdf_translate`` 的模块级字体全局量，
  整段处理用模块级 ``threading.RLock()`` 串行化（Flask 多线程安全）。
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import math
import os
import re
import sys
import threading

import fitz  # PyMuPDF

# ------------------------------------------------------------------ 上游导入
# 查找顺序：DOCBRIDGE_PDF_TRANSLATE_DIR > 包目录 docbridge/ > 其父目录 > 本机开发路径。
# 与 pdf_bilingual 保持一致，部署时把 pdf_translate.py 放进 docbridge/ 就能用。
_HERE = os.path.dirname(os.path.abspath(__file__))
_UPSTREAM_CANDIDATES = [
    os.environ.get("DOCBRIDGE_PDF_TRANSLATE_DIR") or "",
    os.path.dirname(_HERE),                                   # docbridge/
    os.path.dirname(os.path.dirname(_HERE)),                  # 父目录
]
UPSTREAM_DIR = next((d for d in _UPSTREAM_CANDIDATES
                     if d and os.path.isfile(os.path.join(d, "pdf_translate.py"))),
                    _UPSTREAM_CANDIDATES[1])
if UPSTREAM_DIR not in sys.path:
    sys.path.insert(0, UPSTREAM_DIR)

import pdf_translate as pt  # noqa: E402  (既有生产代码，只读复用，绝不修改)


def _system_cjk_font() -> str:
    """返回系统里可用的中文字体路径（候选清单见 pipelines/fonts.py）。

    这里按**文件路径**加载 fonts.py，而不是包内相对导入：本模块也可能被
    测试用 importlib 直接按文件加载，那时没有包上下文，`from . import ...`
    会报 "attempted relative import with no known parent package"。
    """
    try:
        import importlib.util
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts.py")
        spec = importlib.util.spec_from_file_location("docbridge_pipeline_fonts", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        # 带上 probe：只返回**真的能画字**的字体（能加载但取不到字宽的会被刷掉，
        # 那正是线上那次整本书白翻的根因）。
        return mod.default_pdf_font(probe=getattr(pt, "font_drawable", None))
    except Exception:                    # noqa: BLE001 —— 取不到就走环境变量/内置字体
        env = (os.environ.get("DOCBRIDGE_PDF_FONT") or "").strip()
        return env if env and os.path.isfile(env) else ""

# ------------------------------------------------------------------ 契约元数据

FORMAT = "pdf"
MODE = "inplace"
LABEL = "原位版式保留"
NOTE = "图片、矢量图与表格完全不动，只把英文原文原位替换为中文译文，页数不变。"
OPTIONS = ["prefetch", "shared_cache", "sim_bold", "font_scale", "font"]
# 说明：`font`（auto | china-s | 字体文件路径）与 source_lang / target_lang 同样被识别，
# 由 pipelines/__init__.py 的 OPTION_SCHEMA / GLOBAL_OPTION_KEYS 决定前端怎么渲染。

DEFAULT_CACHE_SUBDIR = "_translate_cache"
DEFAULT_SHARED_CACHE_DIR = "_cache"

# 上游的字体度量（text_width）与画笔（_insert_char/_draw_text）都读写 pdf_translate
# 的**模块级字体全局量**（CJK_FONTFILE / CJK_FONT_OBJ / FALLBACK_* / 字体资源名缓存），
# 整段处理必须串行化，否则 Flask 多线程下会串字体。
# 用 RLock：允许 progress / translate 回调在极端情况下重入同线程，不会自锁。
_MODULE_LOCK = threading.RLock()

_PAGE_DONE_RE = re.compile(r"^p(\d+)\s*完成$")
_IMAGE_RE = re.compile(r"图片:\s*输入\s*(\d+)\s*张\s*->\s*输出\s*(\d+)\s*张")
_STATS_RE = re.compile(
    r"统计:\s*待译行\s*(\d+)\s*[，,]\s*已译\s*(\d+)\s*[，,]\s*跳过\s*(\d+)")
_CACHED_RE = re.compile(r"缓存复用\s*(\d+)\s*行")

IMAGE_MISMATCH_MSG = "图片数量发生变化：输入 {i} 张、输出 {o} 张，结果不可信"


# ------------------------------------------------------------------ 小工具

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
    if out != out or out == float("inf") or out == float("-inf"):  # NaN/inf
        return default
    return out


def _safe_name(text, fallback="input"):
    safe = re.sub(r"[^\w.-]", "_", str(text or "")).strip("._")
    return safe[:60] or fallback


def _remove_quiet(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _count_images(path):
    """按上游同样的口径统计图片数（逐页 get_images 求和）。"""
    with fitz.open(path) as doc:
        return sum(len(page.get_images(full=True)) for page in doc)


def _content_hash(path, chunk=1 << 20):
    """按**文件内容**算哈希（而不是路径）。

    跨任务共享缓存必须这么做：上传到每个任务目录后路径都不同，
    用路径当键就永远命不中；用内容哈希，同一份文件无论传多少次都共用一份译文。
    分块读取，几十 MB 的 PDF 也就几十毫秒。
    """
    h = hashlib.sha1()
    try:
        with open(path, "rb") as fh:
            while True:
                block = fh.read(chunk)
                if not block:
                    break
                h.update(block)
    except OSError:
        return hashlib.sha1(os.path.abspath(path).encode("utf-8")).hexdigest()[:12]
    return h.hexdigest()[:12]


def _translation_fingerprint(options) -> str:
    """翻译结果的「配方指纹」= 提示词版本 + 术语表内容。

    ★ 为什么必须进缓存键：跨任务共享缓存原本只按「文件内容 + 模型 + 语言对」，
    于是你**改了术语表再重跑同一本书，会命中旧缓存** —— 术语表静默失效，
    而且从界面上看不出任何异常（任务显示成功、译文却是旧的）。
    加上这个指纹，改术语表/改提示词就会自动重新翻译。
    """
    try:
        from translator import PROMPT_VERSION
    except Exception:                       # noqa: BLE001
        PROMPT_VERSION = "0"
    try:
        gloss = _load_glossary(options)
    except Exception:                       # noqa: BLE001
        gloss = {}
    blob = json.dumps(gloss, ensure_ascii=False, sort_keys=True) if gloss else ""
    return hashlib.sha1(("%s|%s" % (PROMPT_VERSION, blob)).encode("utf-8")).hexdigest()[:8]


def _shared_cache_root(options):
    """跨任务共享缓存的根目录；返回 "" 表示不启用（退回逐任务隔离）。

    默认开启，位置是项目目录下的 ``_cache``（可用 ``DOCBRIDGE_SHARED_CACHE``
    指定绝对路径，设成 0 关闭）。好处：同一本书换参数重跑、或多人传同一份文件时，
    已翻过的行**零成本**复用。
    """
    if not _as_bool(options.get("shared_cache"), True):
        return ""
    env = (os.environ.get("DOCBRIDGE_SHARED_CACHE") or "").strip()
    if env.lower() in ("0", "false", "no", "off"):
        return ""
    if env:
        return env
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        DEFAULT_SHARED_CACHE_DIR)


def _cache_dir_for(src_path, out_path, options):
    """可配置的逐任务缓存目录。

    结构：``<root>/<源语言>-<目标语言>/<文件名>-<大小>-<路径哈希>``

    * ``root`` 默认是输出文件同级的 ``_translate_cache/``（每个任务独立目录），
      也可用 ``options["cache_dir"]`` 指定；
    * 上游缓存键只跟文件名有关，所以这里额外混入输入文件**绝对路径哈希 + 大小**，
      避免不同用户上传同名 PDF 互相污染；
    * 语言对分目录，保证不同 source_lang/target_lang 的任务不共用译文缓存。
      （sim_bold / font_scale / font 只影响排版、不影响译文，不参与缓存键。）
    """
    root = options.get("cache_dir")
    if not root:
        shared = _shared_cache_root(options)
        if shared:
            # 共享模式：内容哈希 + 模型标识 + 语言对
            # —— 同一份文件在任何任务里都命中；换模型/语言对不会串味。
            stem_s = _safe_name(os.path.splitext(os.path.basename(src_path))[0])
            try:
                size_s = os.path.getsize(src_path)
            except OSError:
                size_s = 0
            return os.path.join(
                shared,
                _safe_name(str(options.get("model_tag") or "default"), "default"),
                _safe_name("%s-%s" % (options.get("source_lang") or "auto",
                                      options.get("target_lang") or "zh-Hans"),
                           "auto-zh-Hans"),
                "%s-%d-%s-%s" % (stem_s, size_s, _content_hash(src_path),
                                 _translation_fingerprint(options)))
        root = os.path.join(os.path.dirname(os.path.abspath(out_path)),
                            DEFAULT_CACHE_SUBDIR)
    lang_key = _safe_name(
        "%s-%s" % (options.get("source_lang") or "auto",
                   options.get("target_lang") or "zh-Hans"),
        "auto-zh-Hans")
    stem = _safe_name(os.path.splitext(os.path.basename(src_path))[0])
    abspath = os.path.abspath(src_path)
    try:
        size = os.path.getsize(abspath)
    except OSError:
        size = 0
    digest = hashlib.sha1(abspath.encode("utf-8", "replace")).hexdigest()[:10]
    return os.path.join(root, lang_key, "%s-%d-%s" % (stem, size, digest))


# ------------------------------------------------------------------ stdout 捕获

class _StdoutCapture(io.TextIOBase):
    """把上游 stdout 边写边解析：完整行立即回调，整体文本留给事后统计。"""

    def __init__(self, on_page_done=None):
        super().__init__()
        self._buf = io.StringIO()
        self._pending = ""
        self._on_page_done = on_page_done
        self._last_page = 0

    # -- io 接口
    def write(self, s):  # noqa: D401
        if not isinstance(s, str):
            s = str(s)
        self._buf.write(s)
        self._pending += s
        while "\n" in self._pending:
            line, self._pending = self._pending.split("\n", 1)
            self._handle(line)
        return len(s)

    def flush(self):
        return None

    def writable(self):
        return True

    def readable(self):
        return False

    def isatty(self):
        return False

    # -- 解析
    def _handle(self, line):
        m = _PAGE_DONE_RE.match(line.strip())
        if m and self._on_page_done is not None:
            pno = int(m.group(1))
            if pno > self._last_page:      # 单调递增
                self._last_page = pno
                self._on_page_done(pno)

    @property
    def text(self):
        if self._pending:
            self._handle(self._pending)
            self._pending = ""
        return self._buf.getvalue()


def _parse_stats(text):
    """解析上游统计行，返回 (待译行, 已译, 跳过, 缓存复用)；解析不到给 None。"""
    m = _STATS_RE.search(text or "")
    if not m:
        return None
    cached = _CACHED_RE.search(text)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)),
            int(cached.group(1)) if cached else 0)


def _parse_image_counts(text):
    """解析上游自检行，返回 (输入, 输出)；解析不到返回 None。"""
    m = _IMAGE_RE.search(text or "")
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)))


def _verify_images(stdout_text, src_path, out_path, part_path=None):
    """图片零改动校验：上游自检行 + 自己用 fitz 复核，双重保险。

    不一致时删除 ``part_path``（默认 ``out_path``）并抛 RuntimeError。
    返回 ``(n_in, n_out)``（用于写进 detail）。
    """
    part = part_path or out_path
    parsed = _parse_image_counts(stdout_text)
    n_in_self = _count_images(src_path)
    n_out_self = _count_images(out_path)

    if parsed is not None and parsed[0] != parsed[1]:
        _remove_quiet(part)
        raise RuntimeError(IMAGE_MISMATCH_MSG.format(i=parsed[0], o=parsed[1]))
    if n_in_self != n_out_self:
        _remove_quiet(part)
        raise RuntimeError(
            IMAGE_MISMATCH_MSG.format(i=n_in_self, o=n_out_self))
    return (parsed[0] if parsed else n_in_self, n_out_self)


# ------------------------------------------------------------------ 主入口

def _shrink_fonts_in_place(path):
    """对已生成的文件做一次字体子集化，返回 (原大小, 新大小)；失败返回 None。

    为什么需要：上游 ``process_pdf`` 只做 ``save(garbage=4, deflate=True)``，
    **不打字体子集**，于是整份中文字体被嵌进输出——明体那个 21MB 的字体能让
    一份 2 页文档变成 10MB。``subset_fonts()`` 会把只用到几百个字的字体压到
    几十 KB（同一条双语管线自带这一步，所以它输出只有几百 KB）。

    这是 best-effort：子集化只影响体积、不影响正确性，失败就保留原文件。
    """
    try:
        before = os.path.getsize(path)
    except OSError:
        return None
    tmp = path + ".sub"
    try:
        doc = fitz.open(path)
        try:
            doc.subset_fonts()
            doc.save(tmp, garbage=4, deflate=True)
        finally:
            doc.close()
        after = os.path.getsize(tmp)
        if after < before:            # 只有确实变小才替换，避免把文件搞大
            os.replace(tmp, path)
            return before, after
        _remove_quiet(tmp)
        return before, before
    except Exception:                 # noqa: BLE001
        _remove_quiet(tmp)
        return None


# ---------------------------------------------------------------- 批量预翻译
# 上游是「一页一次请求」：30 页 = 30 次请求，每次都要重发一遍系统提示与翻译规则，
# 而且完全是串行的。这里先把整份文档的行**跨页攒批**翻译好、直接写进上游的页缓存，
# 之后 process_pdf 全程命中缓存（不再发请求），只做排版。
#
# 三个好处：
#   * 更便宜：请求数从「页数」降到「约 行数/50」，每次请求的固定提示开销被摊薄；
#   * 更快：批次之间可以并发（上游逐页调用是串行的）；
#   * 更准：模型一次看到整份文档，术语与语气保持一致；短词/表格不再靠猜
#     （实测：孤立翻 Capital/Capitals 会得到「首都」「大写字母」，
#      有上下文时才是财务语境正确的「资本」）。
PREFETCH_LINES = max(1, int(os.environ.get("DOCBRIDGE_PREFETCH_LINES", "50")))
PREFETCH_CHARS = max(200, int(os.environ.get("DOCBRIDGE_PREFETCH_CHARS", "3000")))
PREFETCH_WORKERS = max(1, int(os.environ.get("DOCBRIDGE_PREFETCH_WORKERS",
                                              os.environ.get("TRANSLATE_CONCURRENCY", "3"))))
GLOSSARY_LIMIT = max(0, int(os.environ.get("DOCBRIDGE_GLOSSARY_LIMIT", "20")))


def _page_texts(page):
    """按上游 ``process_pdf`` 的口径取出该页的文本列表（跳过竖排）。

    必须与上游完全一致：缓存校验比的就是这份列表
    （``cached.get("texts") == texts``），差一条缓存就失效、退回逐页请求。
    """
    lines = pt.collect_lines(page)
    return [t for (_r, t, _s, _c, _b, rotated, _base, _sp) in lines if not rotated]


def _build_doc_context(doc):
    """给模型一点文档背景（标题 + 开头几行）。

    成本很低（每批多几十个 token），但能显著改善短词与表格的准确率——
    那些行本身没有上下文，孤立翻译时模型只能靠猜。
    """
    parts = []
    try:
        title = ((doc.metadata or {}).get("title") or "").strip()
        if len(title) > 3:
            parts.append(title[:120])
    except Exception:                       # noqa: BLE001
        pass
    for page in doc[:2]:
        for text in _page_texts(page)[:6]:
            text = (text or "").strip()
            if len(text) > 3:
                parts.append(text[:110])
        if len(parts) >= 8:
            break
    return " / ".join(parts)[:400]


def _load_glossary(options) -> dict:
    """读取术语表（委托给翻译层，保证与其它模式用的是同一份、同一套解析）。"""
    try:
        from translator import load_glossary
        return load_glossary(options.get("glossary_path"))
    except Exception:                       # noqa: BLE001
        return {}


def _prefetch_translations(doc, translate, cache_dir, options, target_lang, emit):
    """把整份 PDF 的待译内容攒批翻译好并写进上游页缓存。

    与旧版的区别：**先合并换行、整段翻译、再按行盒宽度比例分配回每一行**。
    分配结果按"一行一条"写进页缓存，所以下游的缓存校验、断点续跑、逐页兜底
    全部照旧可用。

    返回统计字典（批次/行数/页数/字符数）。任何一步出问题都由调用方决定
    是否降级——这里不吞异常：取不到译文时宁可按老路子逐页翻，也不要出半成品。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # ★ 页号口径：上游 process_pdf 用 parse_pages(None, n) 拿到的是 **0 基**索引，
    #   却拿它做 doc[pno - 1]，并以同一个 pno 作为缓存键（page_{pno}.json）。
    #   现在的自有循环用**自然页号**做缓存键，所以这里两种键各写一份：
    #   无论谁按哪种口径读都能命中（只有最后一页会多一个文件）。
    total_pages = len(doc)
    keys = [(i + 1) % total_pages if total_pages else 0
            for i in range(total_pages)]

    per_page = []          # [(缓存键, 自然页号, [text, ...], [行 entry, ...])]
    unit_meta = []         # [(页序号, [行号...], 单元文本, [行宽权重...])]
    reused: dict[tuple[int, int], str] = {}      # 缓存里已有的译文
    wanted_lines = 0
    for i in range(total_pages):
        page = doc[i]
        work, texts, _rotated = _collect_work(page)
        key = keys[i]
        # 先认缓存：断点续跑 / 重复提交时，不该把已经翻过的行再翻一遍
        # （既省钱也省时间；上游那份缓存本来就是为这个存在的）
        cached = pt.load_page_cache(cache_dir, key) or pt.load_page_cache(cache_dir, i + 1)
        hit = bool(cached) and cached.get("texts") == texts
        old_trans = (cached or {}).get("translations") or []
        entries = [{"i": idx, "rect": rect, "text": text, "size": size}
                   for (idx, rect, text, size, _c, _b, _base, _sp) in work]
        by_id = {e["i"]: e for e in entries}
        per_page.append((key, i + 1, texts, _line_columns(entries)))
        wanted = []
        for entry in entries:
            idx = entry["i"]
            if not _needs_translation(entry["text"], target_lang):
                continue
            if hit and idx < len(old_trans) and old_trans[idx]:
                reused[(i, idx)] = old_trans[idx]
            else:
                wanted.append(entry)
        wanted_lines += len(wanted)
        # 行距基线用**整页**的行来估：wanted 已经滤掉了不翻译的行，
        # 只按它估会把行距算大，反而容易误合并相邻段落
        gap_base = _gap_baseline(_line_columns(entries))
        for unit in _merge_units(wanted, gap_base):
            unit_meta.append((i, unit, _unit_text([by_id[k] for k in unit]),
                              [max(1.0, by_id[k]["rect"].width) for k in unit]))

    cache_hits = len(reused)
    if not unit_meta:
        return {"batches": 0, "lines": 0, "pages": len(per_page), "reused": cache_hits,
                "chars_in": 0, "chars_out": 0}

    # 跨页攒批：单元数与字符数双重上限，避免一个超长请求
    batches: list[list[tuple[int, list[int], str, list[float]]]] = []
    cur: list = []
    cur_chars = 0
    for item in unit_meta:
        ln = len(item[2])
        if cur and (len(cur) >= PREFETCH_LINES or cur_chars + ln > PREFETCH_CHARS):
            batches.append(cur)
            cur, cur_chars = [], 0
        cur.append(item)
        cur_chars += ln
    if cur:
        batches.append(cur)

    context = {
        "kind": "pdf",
        "doc_context": _build_doc_context(doc),
        "glossary": _load_glossary(options),
    }
    results: dict[tuple[int, int], str] = {}

    def run_batch(batch):
        texts = [t for _p, _u, t, _w in batch]
        out = translate(texts, dict(context, pages=sorted({p for p, _u, _t, _w in batch})))
        if not isinstance(out, (list, tuple)) or len(out) != len(texts):
            raise RuntimeError("预翻译返回数量不符：期望 %d，实际 %s"
                               % (len(texts), len(out) if hasattr(out, "__len__") else "?"))
        return batch, out

    def _absorb(batch, out):
        """把一批整段译文按行盒宽度比例分配回每一行。"""
        for (_pno, unit, _t, weights), zh in zip(batch, out):
            if zh is None:
                zh = ""
            elif not isinstance(zh, str):
                zh = str(zh)
            zh = zh.strip()
            if not zh:
                continue                    # 翻译失败 → 该单元保留原文
            if len(unit) == 1:
                results[(_pno, unit[0])] = zh
                continue
            for idx, part in zip(unit, _split_translation(zh, weights)):
                results[(_pno, idx)] = part if part else COVER_ONLY

    done = 0
    workers = min(PREFETCH_WORKERS, len(batches))
    emit(0, "批量预翻译：%d 行 → %d 批（并发 %d%s）"
         % (wanted_lines, len(batches), workers,
            "，命中缓存 %d 行" % cache_hits if cache_hits else ""))
    if workers <= 1:
        for batch in batches:
            b, out = run_batch(batch)
            _absorb(b, out)
            done += 1
            emit(0, "批量预翻译 %d/%d 批" % (done, len(batches)))
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(run_batch, b) for b in batches]
            for fut in as_completed(futures):
                b, out = fut.result()          # 异常照原样冒泡，交给上层
                _absorb(b, out)
                done += 1
                emit(0, "批量预翻译 %d/%d 批" % (done, len(batches)))

    # 写成上游能认的页缓存（texts 与 translations 一一对应，未译的留空）。
    # 去重放在最后、按页做：同一列里相邻且高度相似的译文只保留前一行。
    for i, (key, natural, texts, columns) in enumerate(per_page):
        if not texts:
            continue
        trans = {}
        for col in columns:
            for entry in col:
                idx = entry["i"]
                val = results.get((i, idx)) or reused.get((i, idx))
                if val:
                    trans[idx] = val
        _dedupe_page(columns, trans)
        payload = {"texts": texts,
                   "translations": [trans.get(idx) for idx in range(len(texts))]}
        pt.save_page_cache(cache_dir, key, payload)
        if natural != key:
            pt.save_page_cache(cache_dir, natural, payload)
    return {
        "batches": len(batches),
        "lines": wanted_lines,
        "pages": len(per_page),
        "reused": cache_hits,
        "chars_in": sum(len(t) for _p, _u, t, _w in unit_meta),
        "chars_out": sum(len(v) for v in results.values() if v != COVER_ONLY),
    }


def _needs_translation(text, target_lang):
    """与翻译层保持**同一口径**判断某行要不要翻。

    故意 import 翻译层的实现而不是自己再写一遍：两边口径一旦不一致，
    缓存里就会出现「本该跳过却被填了译文」的行，上游会拿原文当译文重绘。
    """
    try:
        from translator import needs_translation
        return needs_translation(text, target_lang)
    except Exception:                       # noqa: BLE001
        return bool(re.search(r"[A-Za-z]", text or ""))


# ============================================================ A. 换行合并
#
# 线上反馈（用户实测）：PDF 里一个段落常被换行拆成多行，逐行独立翻译会
#   （1）同一句被翻两遍（看到重复译文）；
#   （2）半句拼不上、上下文断裂（"句子怪怪的"）。
# 做法分三步：
#   1) 把"明显属于同一段落"的相邻行合并成一个翻译单元（判据见 _can_merge）；
#   2) 整段送翻译，再按原行**行盒宽度比例**把译文分配回每一行；
#   3) 兜底去重：同一列里相邻且高度相似的译文只保留前一行。

# 句末标点：上一行以它结尾 → 句子已结束，绝不与下一行合并。
SENTENCE_END = ".?!。！？…"
# 续行标点：下一行以它开头 → 明显是上一行的延续（即便上一行以句末标点结尾）。
CONT_PUNCT = ",;:)]}、，；：）》」』"
# 列表项/编号开头（"1."、"•"、"(a)"、"a) "）：都是新条目，绝不与上一行合并。
LIST_HEAD_RE = re.compile(r"^\s*(?:[-*\u2022\u2013\u2014]|\(?\d{1,3}[.)]|[a-z][.)]\s)")
# 「下一行是小写开头」判定：先剥掉行首引号/括号，再要求第一个字母是小写 ASCII。
LOWER_HEAD_RE = re.compile(r"^[\"'({\[\u2018\u201c\u300c\u300e]*[a-z]")
# 行距判据：正常行距的"空白间隙"≈0.2×字号，段间距通常 ≥0.8×字号。
# 阈值取 0.75×字号（且绝对值不超过 6pt）——实测 11pt 正文的行间隙约 2pt、
# 段间距 8~11pt，两者相差一个数量级，阈值落在中间不会误合并相邻段落。
MERGE_GAP_RATIO = 0.75
MERGE_GAP_ABS = 6.0
# 字号突变 >1.5pt 视为标题/图注/表格等不同层级，不合并。
MERGE_SIZE_TOL = 1.5
# 左缘（x0）漂移 >15pt 视为不同缩进或不同列，不合并。
MERGE_X_TOL = 15.0
# 同列聚类阈值（与上游 group_paragraphs 一致：x0 相差 ≤30pt 算同一列）。
COLUMN_X_TOL = 30.0
MERGE_MAX_LINES = 12          # 单个翻译单元最多合并的行数
MERGE_MAX_CHARS = 1500        # 单个翻译单元最多字符数（避免一个超长请求）
# 去重：字符二元组 Jaccard ≥0.45 视为重复（与 cf/live 的 dedupeSentences 同阈值），
# 且只对 ≥6 字的句子生效——过短的句子（表格词、编号、"是"/"否"）不去重，
# 否则会把正常的重复强调误删。
DEDUPE_SIM = 0.45
DEDUPE_MIN_CHARS = 6
# 缓存与传递用的哨兵：表示"该行原文已被覆盖，但不再绘制文字"（译文由相邻行承载）。
# 必须与"没有译文"（None，保留原英文）区分开，否则缓存会认为这行没翻过而反复重翻。
COVER_ONLY = "\x00"

_DEDUPE_STRIP_CHARS = (" \t\r\n"
                       "\u3002\uff0c\uff01\uff1f\u3001\uff1b\uff1a"
                       "\u201c\u201d\u2018\u2019\u300c\u300d\u300e\u300f"
                       "\uff08\uff09()[]\u3010\u3011\u00b7\u2026\u2014"
                       ",.!?;:\"'()[]{}<>\u00ab\u00bb-\u2013\u2014_/\\|")
_DEDUPE_STRIP_RE = re.compile("[" + re.escape(_DEDUPE_STRIP_CHARS) + "]+")


def _is_cjk_char(ch):
    return "\u4e00" <= ch <= "\u9fff"


def _word_char(ch):
    """拉丁单词/数字的组成字符（用于避免把单词从中间切开）。"""
    return ch.isascii() and (ch.isalnum() or ch in "-'")


def _join_delta(prev_raw, next_text):
    """续行拼接：返回 (要从上一行尾部删掉的字符, 要接上的文本)。

    为什么单独返回"要删的字符"：上一行以连字符断词时必须先去掉连字符再直接接上
    （``pub-`` + ``lic`` → ``public``），不能用空格拼（会拼出 ``pub- lic``）。
    """
    p = (prev_raw or "").rstrip()
    n = (next_text or "").lstrip()
    if (p.endswith("-") or p.endswith("\u00ad")) and n[:1].islower() and n[:1].isascii():
        return p[-1:], n
    return "", (" " + n)


def _unit_text(unit):
    """把单元内的若干行拼成一段文本（连字符断词会还原）。"""
    out = ""
    prev_raw = ""
    for entry in unit:
        t = (entry.get("text") or "").strip()
        if not t:
            continue
        if not out:
            out = t
        else:
            drop, add = _join_delta(prev_raw, t)
            if drop and out.endswith(drop):
                out = out[:-len(drop)]
            out += add
        prev_raw = entry.get("text") or ""
    return out.strip()


# 合并用的「正常行距」基线。
#
# ★ 原来的阈值是写死的：max(6pt, 0.75×字号)。注释里说这是拿 Word 生成的 PDF
#   标定的（行间隙≈2pt、段间距 8~11pt，相差一个数量级）。但**行距松的文档**
#   ——LaTeX 的 \onehalfspacing、InDesign 排版、很多教材和期刊——行间隙本来
#   就有 8~12pt，这个阈值直接把同一句话的换行判成"新段落"，于是永远合不上：
#   每一行都被当成独立片段送翻译，译文自然"半句拼不上、上下文断裂"，
#   甚至把下一行的内容猜进上一行去。
#   实测 samples/demo.pdf：两行间隙 11.68pt > 阈值 8.25pt，一句话被劈成两个单元。
# 改成按本页真实行距自适应，绝对阈值保留作兜底。
GAP_BASELINE_MIN_SAMPLES = 4    # 样本太少就不猜，退回绝对阈值（否则两行就能"证明"任何间距）
GAP_BASELINE_PCT = 0.2          # 取低分位：被跳过的行（纯数字/表格）会留下大间隙
GAP_BASELINE_REL = 1.35         # 允许行间隙比基线略大


def _gap_baseline(columns) -> float:
    """估计本页「正常行距」：同列相邻行间隙的低分位（pt）。样本不足返回 0（不启用）。

    为什么取**低分位**而不是中位数：``_merge_units`` 拿到的常常是"需要翻译的行"，
    其中被跳过的行（纯数字、表格里的符号）会在列表里留下被拉大的间隙；
    低分位天然忽略这类离群值，取到的才是真正的行间距。
    """
    gaps = []
    for col in columns:
        for a, b in zip(col, col[1:]):
            gap = b["rect"].y0 - a["rect"].y1
            if gap > 0:
                gaps.append(gap)
    if len(gaps) < GAP_BASELINE_MIN_SAMPLES:
        return 0.0
    gaps.sort()
    return gaps[int(len(gaps) * GAP_BASELINE_PCT)]


def _can_merge(prev, nxt, gap_baseline=0.0):
    """判定 ``nxt`` 是否是 ``prev`` 的续行（两个 entry：i/rect/text/size）。

    全部条件都满足才合并 —— 刻意保守，宁可漏合并（退回逐行翻译，最多是旧行为），
    也不要误合并（会把两个不同段落塞进一次翻译，产生驴唇不对马嘴的译文）：
      1. 上一行不以句末标点结尾（``.?!。！？…``），除非它以连字符断词；
      2. 下一行首字母是小写 ASCII（先剥掉行首引号/括号），或以续行标点开头；
      3. 下一行不是列表项/编号开头（``1.`` / ``•`` / ``(a)`` / ``a) ``）；
      4. 同列：左缘 x0 相差 ≤15pt；
      5. 行距正常：间隙 = 下一行 y0 − 上一行 y1，落在
         [−3pt, max(6pt, 0.75×字号, ``gap_baseline`` × 1.35)]；
      6. 字号接近：相差 ≤1.5pt。

    第 5 条里的 ``gap_baseline`` 由 ``_gap_baseline`` 按本页真实行距算出——
    没有它，行距松的文档一句话永远合不上。
    """
    p = (prev.get("text") or "").strip()
    n = (nxt.get("text") or "").strip()
    if not p or not n:
        return False
    if LIST_HEAD_RE.match(n):
        return False
    # 1) 上一行是否句末结束（连字符断词例外）
    hyphen_break = p.endswith("-") or p.endswith("\u00ad")
    if not hyphen_break and p[-1] in SENTENCE_END:
        return False
    # 2) 下一行是否"明显是延续"：小写开头，或续行标点开头
    if not LOWER_HEAD_RE.match(n) and n[0] not in CONT_PUNCT:
        return False
    # 4) 同列
    pr, nr = prev["rect"], nxt["rect"]
    if abs(nr.x0 - pr.x0) > MERGE_X_TOL:
        return False
    # 5) 行距正常：绝对阈值兜底，再按本页真实行距放宽（行距松的文档靠这条）
    gap = nr.y0 - pr.y1
    limit = max(MERGE_GAP_ABS, MERGE_GAP_RATIO * prev["size"])
    if gap_baseline > 0.0:
        limit = max(limit, gap_baseline * GAP_BASELINE_REL)
    if gap < -3.0 or gap > limit:
        return False
    # 6) 字号接近
    if abs(nxt["size"] - prev["size"]) > MERGE_SIZE_TOL:
        return False
    return True


def _line_columns(entries):
    """把行按 x0 聚成"列"，列内按 y0 排序（与上游 group_paragraphs 同口径）。

    先分列再排序，是为了多栏/页边注与正文交错时不会把不同栏的行当成相邻行。
    """
    cols = []
    for item in sorted(entries, key=lambda e: e["rect"].x0):
        if cols and abs(item["rect"].x0 - cols[-1][-1]["rect"].x0) <= COLUMN_X_TOL:
            cols[-1].append(item)
        else:
            cols.append([item])
    return [sorted(col, key=lambda e: (e["rect"].y0, e["rect"].x0)) for col in cols]


def _merge_units(entries, gap_baseline=None):
    """把行合并成翻译单元，返回 ``[[行号, ...], ...]``（单行单元也原样保留）。

    单元最终按"单元内最小行号"排序 —— 与上游逐行的批内顺序保持一致，
    这样译文与行的对应关系、以及译文写进缓存的顺序都不变。

    ``gap_baseline`` 省略时由 ``entries`` 自己估计；调用方若能拿到**整页**的行，
    应该把整页算出来的基线传进来 —— 因为 ``entries`` 常常已经滤掉了不需要翻译的行
    （纯数字、表格符号），只按它估计会把行距算大，反而容易误合并。
    """
    columns = _line_columns(entries)
    if gap_baseline is None:
        gap_baseline = _gap_baseline(columns)
    units = []
    for col in columns:
        cur = []
        for entry in col:
            if not cur:
                cur = [entry]
                continue
            if _can_merge(cur[-1], entry, gap_baseline):
                if len(cur) >= MERGE_MAX_LINES:
                    units.append(cur)
                    cur = [entry]
                    continue
                merged = _unit_text(cur + [entry])
                if len(merged) > MERGE_MAX_CHARS:
                    units.append(cur)
                    cur = [entry]
                    continue
                cur.append(entry)
            else:
                units.append(cur)
                cur = [entry]
        if cur:
            units.append(cur)
    units.sort(key=lambda u: min(e["i"] for e in u))
    return [[e["i"] for e in u] for u in units]


def _split_translation(zh, weights):
    """把整段译文按各原行的**行盒宽度比例**分配回每一行。

    * 每行至少分到 1 个字（只要译文总字数 ≥ 行数）；
    * 切点尽量不落在拉丁单词/数字中间；
    * 译文比行数还短（分配不了）时，整句放在最宽的那一行，其余行留空（只覆盖原文）
      —— **绝不整句重复**，那正是用户反馈里最刺眼的"同一句翻两遍"。
    """
    n = len(weights)
    if n <= 1:
        return [zh]
    if not zh:
        return [""] * n
    if len(zh) < n:
        best = max(range(n), key=lambda k: weights[k])
        return [zh if k == best else "" for k in range(n)]

    total = float(sum(weights)) or float(n)
    extra = len(zh) - n
    raw = [extra * (w / total) for w in weights]
    base = [1 + int(math.floor(x)) for x in raw]
    rest = len(zh) - sum(base)
    order = sorted(range(n),
                   key=lambda k: (raw[k] - math.floor(raw[k]), weights[k]),
                   reverse=True)
    for k in order[:rest]:
        base[k] += 1

    cuts = [0]
    for k in range(n):
        cuts.append(cuts[-1] + base[k])
    # 避免把拉丁单词/数字切开：切点只能往左挪（保证每行仍 ≥1 字）
    for k in range(1, n):
        c = cuts[k]
        lo = cuts[k - 1] + 1
        while c > lo and _word_char(zh[c - 1]) and _word_char(zh[c]):
            c -= 1
        cuts[k] = c
    # 挪过之后仍要严格递增，且最右切点不得越过串尾
    for k in range(1, n + 1):
        hi = len(zh) - (n - k)
        cuts[k] = max(cuts[k], cuts[k - 1] + 1)
        if cuts[k] > hi:
            cuts[k] = hi
    return [zh[cuts[k]:cuts[k + 1]].strip() for k in range(n)]


def _dedupe_grams(text):
    """去标点空白后的文本 + 字符二元组集合（与 cf/live 的 dedupeSentences 同思路）。"""
    clean = _DEDUPE_STRIP_RE.sub("", text or "")
    return clean, {clean[i:i + 2] for i in range(len(clean) - 1)}


def _similar(a, b):
    """两个译文的字符二元组 Jaccard 相似度是否 ≥ 阈值（过短的句子直接判不相似）。"""
    ca, ga = _dedupe_grams(a)
    cb, gb = _dedupe_grams(b)
    if len(ca) < DEDUPE_MIN_CHARS or len(cb) < DEDUPE_MIN_CHARS:
        return False
    if not ga or not gb:
        return False
    inter = len(ga & gb)
    union = len(ga) + len(gb) - inter
    return union > 0 and (inter / float(union)) >= DEDUPE_SIM


def _dedupe_page(columns, trans):
    """同一列里**相邻**译文高度相似时只保留前一行，后一行标记为"仅覆盖原文"。

    只跟"上一行保留下来的译文"比较（不是跟整列比），这样跨段落的正常重复
    （例如每段都出现"如图 1.1 所示"）不会被误删。
    返回被去掉文字的行号集合。
    """
    dropped = set()
    for col in columns:
        prev_kept = None
        for entry in col:
            i = entry["i"]
            zh = trans.get(i)
            if not zh or zh == COVER_ONLY:
                prev_kept = None
                continue
            if prev_kept is not None and _similar(prev_kept, zh):
                dropped.add(i)
                trans[i] = COVER_ONLY
                continue
            prev_kept = zh
    return dropped


# ============================================================ B. 版面几何
#
# 线上反馈：译文"错位、大小不对、重叠"。根因是 CJK 字形与拉丁字母的高度、基线、
# 字宽都不同，直接按原位置原字号画中文会顶到上一行或撑出右边界。这里做了三件事：
#   1) 逐行按**原行行盒宽度**自适应缩字号（下限 = 原字号 60%，再低就压字距）；
#   2) 基线修正：保持原英文基线（依据见 _clamp_baseline 注释），越界时整体平移回来；
#   3) 硬约束：每行给一条"可用空白带"，墨迹盒必须落在带内 —— 带与带之间留空隙，
#      因此绘制结果与同页任何其它文字行都不可能相交（_draw_page_inplace 还会自检）。

# CJK 墨迹盒（相对基线的 em 倍数）。实测：fitz.Font(宋体).glyph_bbox 给出的字体盒是
# y∈[−0.15, 0.86]，Noto Serif CJK 接近 [−0.16, 0.88]。取 0.88/0.16 作为绘制墨迹的
# 上下边界；拉丁大写字母的墨迹是 [−0.70, 0]em。
CJK_INK_ASCENT = 0.88
CJK_INK_DESCENT = 0.16
# 字号下限：原行字号的 60%。实测缩到 60% 仍可读；再小就与"看得清"冲突，
# 而中文在同字号下的视觉宽度本来就比英文窄（0.92 的视觉比例已留了余量）。
FONT_FLOOR_RATIO = 0.60
# 字距压缩上限 8%：只在相邻两个中文字符之间收紧，再多笔画就会粘连。
MAX_TRACK_EM = 0.08
TRACK_STEPS = (0.0, 0.02, 0.04, 0.06, 0.08)
# 与上下邻行保留的最小空白（pt）。实测正常行距间隙约 2pt，留 0.75pt 仍有视觉间隙。
V_PAD = 0.75
# 多行译文最多占用的高度（相对原字号）：避免把段间距吃光、看起来像多了一段。
MULTILINE_HEIGHT_CAP = 2.6
# 水平容差：中文禁则会允许行尾闭标点挤出一点点宽度。
H_TOL = 1.5
# 矩形视为"同一个"的容差（用于把原行从"其它文字行"里排除出去）。
RECT_TOL = 0.6


def _clean_translation(zh):
    """与上游同一套译文清理：剥掉行首闭标点、去掉中英文之间的空格。"""
    if not zh:
        return ""
    zh = zh.lstrip(pt.KINSOKU_HEAD)
    zh = re.sub(r"(?<=[A-Za-z0-9]) +(?=[\u4e00-\u9fff])", "", zh)
    zh = re.sub(r"(?<=[\u4e00-\u9fff]) +(?=[A-Za-z0-9])", "", zh)
    return zh.strip()


def _cjk_gaps(text):
    """可压缩的字距数：相邻两个中文字符之间的空隙（拉丁词内不动，避免字母粘连）。"""
    return sum(1 for a, b in zip(text, text[1:])
               if _is_cjk_char(a) and _is_cjk_char(b))


def _page_text_line_rects(page):
    """页面上**所有**文字行的 bbox（含中文行、纯数字行、竖排行）。

    用 get_text("dict") 而不是 collect_lines：后者已经过滤掉不翻译的行，
    而"不翻译的行"恰恰是最需要避让的（我们不会覆盖它们，就必须绕开）。
    """
    out = []
    try:
        data = page.get_text("dict")
    except Exception:                       # noqa: BLE001
        return out
    for block in data.get("blocks", []):
        if block.get("type", 0) != 0:       # 跳过图片块
            continue
        for line in block.get("lines", []):
            if line.get("spans"):
                out.append(fitz.Rect(line["bbox"]))
    return out


def _rect_close(a, b, tol=RECT_TOL):
    return (abs(a.x0 - b.x0) <= tol and abs(a.y0 - b.y0) <= tol
            and abs(a.x1 - b.x1) <= tol and abs(a.y1 - b.y1) <= tol)


def _obstacle_rects(page, min_side=12.0):
    """本页需要给译文让路的**图片与成块图形**的矩形。

    ★ 为什么必须有这个：``_free_band`` 原来只认文字行，图片完全不参与几何计算。
      于是紧挨图片上方（或下方）的那行文字，它的下界只能看到"下一个文字行"——
      那往往在图片另一侧老远的地方，译文一折行就**铺到图上**。
      用户反馈的"遇到图片就出问题、影响较大"就是它。

    两类都收：
      * 位图（``get_image_info``）;
      * 成块的矢量图形（填色、且宽高都 ≥ ``min_side``）——很多教材的插图是矢量画的，
        demo.pdf 里那个"图"就是矢量矩形而不是位图。
        细线（表格线、分隔线）和小装饰靠 ``min_side`` 排除。

    用不用得上由 ``_free_band`` 再判：只有**完全位于本行上方/下方**的障碍才约束；
    文字本来就压在图上（障碍把本行整个包住）时不约束 —— 否则空白带会被压成负高度。
    """
    rects = []
    try:
        for info in page.get_image_info():
            r = fitz.Rect(info["bbox"])
            if r.width >= 1.0 and r.height >= 1.0:
                rects.append(r)
    except Exception:                       # noqa: BLE001
        pass
    try:
        for g in page.get_drawings():
            if not g.get("fill"):
                continue                    # 只有描边（表格线、分隔线）不算障碍
            r = fitz.Rect(g["rect"])
            if r.width >= min_side and r.height >= min_side:
                rects.append(r)
    except Exception:                       # noqa: BLE001
        pass
    return rects


def _free_band(page_rect, all_rects, drawn_boxes, rect, obstacles=()):
    """给出该行可用的垂直空白带 ``[top, bottom]``。

    上界 = max(上方最近文字行的底、上方已绘译文墨迹的底) + V_PAD
    下界 = min(下方最近文字行的顶) - V_PAD

    ★ 为什么边界取「页面上所有文字行」而不是「同段相邻行」：
    上游 group_paragraphs 会按行距把一段切成多个"段落"（dy>6pt 就切），
    只跟同段相邻行比较的话，跨段的下一行就管不住了 —— 实测在一份 PPT 版式的
    真实 PDF 上，译文会折行后整体上移 18pt，压住上一行（几何脚本抓到的真问题）。
    这里改用**原行矩形**做上下界：无论那一行会不会被重画，都不许越过去。

    上界还要叠加"已绘译文的墨迹底"：如果上方那行的译文被整体下移了，
    它的墨迹可能低于它自己的原行矩形，必须一起避让。

    只统计**水平方向与本行重叠**的文字行：分栏排版里左右两栏互不构成垂直约束。

    ``obstacles`` 是图片与成块图形（见 ``_obstacle_rects``）。它们只在**完全位于
    本行上方或下方**时才收窄空白带 —— 文字本来就压在图上时不受约束，
    否则空白带会被压成负高度，译文反而被挤没。
    """
    top = page_rect.y0 + 1.0
    bottom = page_rect.y1 - 1.0
    x0, x1 = rect.x0 - 2.0, rect.x1 + 2.0
    for r in all_rects:
        if r.x1 <= x0 or r.x0 >= x1 or _rect_close(r, rect):
            continue
        # ★ 按**顶边**判定上下，而不是"底 <= 本行顶 / 顶 >= 本行底"：
        #   真实教材里存在相邻原行的 bbox 互相重叠（实测第 4 页两行基线只差 1.1pt、
        #   矩形重叠 1.9pt），用矩形边界判会让它们既不算上、也不算下 —— 夹在中间的
        #   译文就会折行铺下去，压住下一行。
        if r.y0 < rect.y0:
            if r.y1 > top:
                top = r.y1
        elif r.y0 > rect.y0:
            if r.y0 < bottom:
                bottom = r.y0
    for bx0, bx1, ink_bottom in drawn_boxes:
        # ★ 只有"位于本行上方"的已绘译文才能当下界（ink_bottom 不得低于本行矩形底）。
        #   少了 ink_bottom <= rect.y1 这个判断，先画的**下方**栏目（按列序可能先画）
        #   会把上界顶到页面中部 —— 实测真实教材第 2 页：一行译文被硬生生下移 44pt，
        #   看着像"散架"（几何脚本 + 新旧渲染对比抓到的真问题）。
        if bx1 > x0 and bx0 < x1 and ink_bottom <= rect.y1 and ink_bottom > top:
            top = ink_bottom
    for r in obstacles:
        # 图片/图形只在自己**完全在本行上方或下方**时才约束（理由见 docstring）
        if r.x1 <= x0 or r.x0 >= x1:
            continue
        if r.y1 <= rect.y0:
            if r.y1 > top:
                top = r.y1
        elif r.y0 >= rect.y1:
            if r.y0 < bottom:
                bottom = r.y0
    return top + V_PAD, bottom - V_PAD


def _fit_wrapped(text, width, size_max, size_floor, h_avail):
    """换行方案：从大到小试字号，直到行数与行高都装进空白带。装不下返回 None。"""
    size = size_max
    while size >= size_floor - 1e-6:
        lines = pt.wrap_text(text, size, width)
        if len(lines) >= 2:
            pitch = size * pt.LINE_PITCH_FACTOR
            h = ((len(lines) - 1) * pitch
                 + (CJK_INK_ASCENT + CJK_INK_DESCENT) * size)
            if h <= h_avail + 0.01 and all(
                    pt.text_width(ln, size) <= width + H_TOL for ln in lines):
                return size, 0.0, lines, pitch, 0.0
        size -= 0.5
    return None


def _fit_line(text, width, size_max, size_floor, h_avail):
    """返回 ``(size, track, lines, pitch, overflow)``。

    优先级：① 单行放得下 → 用它；② 缩到下限仍放不下 → 压缩字距（≤8%）；
    ③ 还不行 → 换行（只在空白带装得下时）；④ 都不行 → 下限字号 + 最大压缩的
    单行，接受一点水平溢出（宁可略微出界，也不要压到邻行造成重叠）。
    """
    ink_h1 = CJK_INK_ASCENT + CJK_INK_DESCENT
    unit = pt.text_width(text, 1.0)         # 字号 1.0 时的宽度（宽度与字号线性）
    if unit > 0:
        need = width / unit
        size = size_max if need >= size_max else max(
            size_floor, math.floor(need * 4) / 4.0)
        if unit * size <= width + 0.01 and ink_h1 * size <= h_avail + 0.01:
            return size, 0.0, [text], size * pt.LINE_PITCH_FACTOR, 0.0
        gaps = _cjk_gaps(text)
        if gaps > 0 and ink_h1 * size_floor <= h_avail + 0.01:
            for track in TRACK_STEPS[1:]:
                if unit * size_floor - track * size_floor * gaps <= width + 0.01:
                    return (size_floor, track, [text],
                            size_floor * pt.LINE_PITCH_FACTOR, 0.0)
    wrapped = _fit_wrapped(text, width, size_max, size_floor, h_avail)
    if wrapped:
        return wrapped
    size = size_floor
    gaps = _cjk_gaps(text)
    track = MAX_TRACK_EM if gaps > 0 else 0.0
    overflow = max(0.0, unit * size - track * size * gaps - width)
    return size, track, [text], size * pt.LINE_PITCH_FACTOR, overflow


def _clamp_baseline(base, size, n_lines, pitch, band_top, band_bottom):
    """基线修正：先保原英文基线，墨迹盒越出空白带时才整体平移回来。

    依据（实测）：CJK 墨迹在基线上方 0.88em、下方 0.16em，视觉中心在 −0.36em；
    拉丁大写字母墨迹在 −0.70em~0 之间，视觉中心在 −0.35em。两者只差 0.01em，
    所以"保持原基线"本身就是正确的视觉对齐，不需要整体上移/下移；
    真正会出问题的是 CJK 顶得更高（0.88 vs 0.70），于是这里做**钳制**：
    顶到上一行就往下挪，压到下一行就往上挪，挪不动就由字号下限兜底。
    """
    ink_top = base - CJK_INK_ASCENT * size
    ink_bottom = base + (n_lines - 1) * pitch + CJK_INK_DESCENT * size
    if ink_top < band_top - 0.01:
        shift = band_top - ink_top
        base += shift
        ink_bottom += shift
    if ink_bottom > band_bottom + 0.01:
        base -= (ink_bottom - band_bottom)
    return base


def _plan_line(zh, rect, size_orig, cjk_scale, band_top, band_bottom, base,
               cover_only):
    """为一行译文算出绘制方案：字号 / 折行 / 行距 / 首行基线 / 字距 / 溢出量。"""
    size_max = max(pt.MIN_SIZE, size_orig * cjk_scale)
    size_floor = max(pt.MIN_SIZE, size_orig * FONT_FLOOR_RATIO)
    if size_max >= pt.MIN_TRANSLATABLE_SIZE:
        # 可读下限：字号不得低于 5pt（与上游 MIN_TRANSLATABLE_SIZE 同口径），
        # 否则宁可不画（调用方会保留原文）。
        size_floor = max(size_floor, min(pt.MIN_TRANSLATABLE_SIZE, size_max))
    if cover_only:
        return {"size": size_floor, "lines": [], "pitch": 0.0,
                "first_base": base, "track": 0.0, "overflow": 0.0,
                "ink_top": base, "ink_bottom": base, "width": 0.0}
    h_avail = min(band_bottom - band_top, MULTILINE_HEIGHT_CAP * size_orig)
    if h_avail <= 0:
        # 空白带退化（上下行矩形本身就互相重叠的怪 PDF）：退回原行自己的高度，
        # 仍然不放宽"不许压到邻行"的约束 —— 由字号下限把高度压下来。
        h_avail = max(rect.height,
                      (CJK_INK_ASCENT + CJK_INK_DESCENT) * size_floor)
    width = max(4.0, rect.width)
    size, track, lines, pitch, overflow = _fit_line(
        zh, width, size_max, size_floor, h_avail)
    first_base = _clamp_baseline(base, size, len(lines), pitch,
                                 band_top, band_bottom)
    drawn_w = 0.0
    for ln in lines:
        w = pt.text_width(ln, size) - track * size * _cjk_gaps(ln)
        drawn_w = max(drawn_w, w)
    return {
        "size": size, "lines": lines, "pitch": pitch,
        "first_base": first_base, "track": track, "overflow": overflow,
        "ink_top": first_base - CJK_INK_ASCENT * size,
        "ink_bottom": (first_base + (len(lines) - 1) * pitch
                       + CJK_INK_DESCENT * size),
        "width": drawn_w,
    }


def _draw_line(page, x0, baseline, text, size, color, *, track=0.0,
               justify_width=None, render_mode=0, border_width=0.0,
               specials=None):
    """画一行译文：支持字距压缩、两端对齐、上下标保真与缺字回退字体。"""
    if not text:
        return
    marks = pt._apply_specials(text, specials) if specials else {}
    if marks:
        # 上下标保真：逐字符绘制，上下标缩小并偏移基线（口径与上游一致）
        x = x0
        for j, ch in enumerate(text):
            kind = marks.get(j)
            fs2 = size * (0.65 if kind else 1.0)
            off = (0.30 * size if kind == "sub"
                   else -0.20 * size if kind == "super" else 0.0)
            pt._insert_char(page, fitz.Point(x, baseline + off), ch, fs2,
                            color, render_mode, border_width)
            x += pt.text_width(ch, fs2 if kind else size)
        return
    if track > 0.0:
        # 字距压缩：只在相邻两个中文字符之间收紧
        x = x0
        for j, ch in enumerate(text):
            pt._insert_char(page, fitz.Point(x, baseline), ch, size, color,
                            render_mode, border_width)
            advance = pt.text_width(ch, size)
            if (j < len(text) - 1 and _is_cjk_char(ch)
                    and _is_cjk_char(text[j + 1])):
                advance -= track * size
            x += advance
        return
    if justify_width is not None:
        plan = pt._justify_plan(text, size, justify_width)
        if plan:
            per, gaps = plan
            x = x0
            for j, ch in enumerate(text):
                pt._insert_char(page, fitz.Point(x, baseline), ch, size, color,
                                render_mode, border_width)
                x += pt.text_width(ch, size) + (per if j in gaps else 0.0)
            return
    pt._draw_text(page, fitz.Point(x0, baseline), text, size, color,
                  render_mode, border_width)


def _boxes_intersect(a, b):
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def _collect_work(page):
    """按上游口径取该页的「水平待译行」＋ 缓存用的 texts 列表 ＋ 竖排行。

    texts 必须与 ``_page_texts``（也就是上游 collect_lines 的口径）**逐条一致**，
    否则页缓存的 texts 校验永远不通过，每页都要重新翻一遍。
    """
    work, texts, rotated_texts = [], [], []
    for rect, text, size, color, bold, rotated, base, specials in pt.collect_lines(page):
        if rotated:
            rotated_texts.append(text)
            continue
        work.append((len(texts), rect, text, size, color, bold, base, specials))
        texts.append(text)
    return work, texts, rotated_texts


def _translate_units(work, needed, translate, target_lang, stats, merge=True):
    """把一个页面的待译行合并成单元 → 调 translate → 按行盒宽度比例分配回每行。

    返回 ``{行号: 译文}``；值为 ``COVER_ONLY`` 表示"只覆盖原文、不再绘制文字"。
    """
    by_id = {w[0]: {"i": w[0], "rect": w[1], "text": w[2], "size": w[3]}
             for w in work if w[0] in needed}
    if not by_id:
        return {}
    entries = [by_id[k] for k in sorted(by_id)]
    units = _merge_units(entries) if merge else [[e["i"]] for e in entries]
    texts = [_unit_text([by_id[i] for i in u]) for u in units]
    stats["calls"] += 1
    out = translate(texts, {"kind": "pdf"})
    if not isinstance(out, (list, tuple)):
        raise RuntimeError("translate 回调必须返回与输入等长的列表")
    if len(out) != len(texts):
        raise RuntimeError(
            "翻译返回数量不符：期望 %d，实际 %d" % (len(texts), len(out)))
    trans = {}
    for unit, zh in zip(units, out):
        if zh is None:
            zh = ""
        elif not isinstance(zh, str):
            zh = str(zh)
        zh = zh.strip()
        stats["chars_in"] += sum(len(by_id[i]["text"]) for i in unit)
        stats["chars_out"] += len(zh)
        if not zh:
            continue                        # 翻译失败 → 保留原文
        if len(unit) == 1:
            trans[unit[0]] = zh
            continue
        weights = [max(1.0, by_id[i]["rect"].width) for i in unit]
        for i, part in zip(unit, _split_translation(zh, weights)):
            trans[i] = part if part else COVER_ONLY
    return trans


def _draw_page_inplace(page, pno, work, trans, cjk_scale, sim_bold,
                       rotated_texts=()):
    """把 ``trans`` 原位画到 ``page`` 上；返回 (已绘制, 仅覆盖, 溢出行数)。

    硬约束：绘制出的墨迹盒不得与同页其它任何文字行相交。靠三件事保证：
      * 每行先算一条"可用空白带"（上下邻行之间的空隙），带之间留 V_PAD；
      * 字号先按原行行盒宽度自适应缩小，再把墨迹盒钳制进带内；
      * 最后仍做一次计划级自检（相交就打警告），便于线上发现没预料到的版式。
    """
    page_rect = page.rect
    plans, keep = {}, []
    for (i, rect, text, size, color, bold, base, specials) in work:
        if i not in trans:
            keep.append((text, "翻译失败"))
            continue
        raw = trans[i]
        cover_only = (raw == COVER_ONLY)
        zh = "" if cover_only else _clean_translation(raw)
        if not zh and not cover_only:
            keep.append((text, "翻译失败"))
            continue
        if max(1.0, size * cjk_scale) < pt.MIN_TRANSLATABLE_SIZE:
            # 原字号本身就低于可读下限（表格窄格/角标）→ 保留原文（上游同口径）
            keep.append((text, "行宽过窄"))
            continue
        plans[i] = {"i": i, "rect": rect, "text": text, "size": size,
                    "color": color, "bold": bold, "base": base,
                    "specials": specials, "zh": zh, "cover_only": cover_only}

    drawn_rects = [p["rect"] for p in plans.values()]
    # 避让集合 = 页面上所有文字行 − 本页要重画的行（重画的行会被覆盖，不需要避让）
    reserved = [r for r in _page_text_line_rects(page)
                if not any(_rect_close(r, d) for d in drawn_rects)]
    # 上下界要用"页面上所有文字行"，所以把要重画的原行也放回来（见 _free_band 注释）
    all_rects = reserved + drawn_rects
    # 图片与成块图形也要参与避让：否则紧挨图片的那行文字一折行就铺到图上
    obstacles = _obstacle_rects(page)

    ok_work = [(p["i"], p["rect"], p["text"], p["size"], p["color"], p["bold"],
                p["base"], p["specials"]) for p in plans.values()]
    bg_pix = page.get_pixmap(dpi=150)
    drawn = covered = overflow = 0
    boxes = []
    # 已绘译文的墨迹底（含 x 范围）：上界要避让它们，否则跨段/折行时会压字
    drawn_boxes = []
    for para in pt.group_paragraphs(ok_work):
        for k, (i, rect, _t, _s, color, bold, _base, specials) in enumerate(para):
            p = plans[i]
            band_top, band_bot = _free_band(page_rect, all_rects, drawn_boxes, rect,
                                            obstacles)
            plan = _plan_line(p["zh"], rect, p["size"], cjk_scale,
                              band_top, band_bot, p["base"], p["cover_only"])
            bg = pt.sample_bg_color(bg_pix, rect, 150)
            page.draw_rect(rect + (-1, -1, 1, 1), color=None, fill=bg,
                           overlay=True)
            if p["cover_only"]:
                covered += 1
                drawn_boxes.append((rect.x0, rect.x1, plan["ink_bottom"]))
                continue
            rm = 2 if (sim_bold and bold) else 0
            bw = (0.04 * plan["size"]) if (sim_bold and bold) else 0.0
            # 段中行两端对齐（与上游一致），对齐宽度取**本行自己的右缘**，
            # 这样译文永远不会越过原行的 x 范围。
            justify = (k != len(para) - 1) and len(plan["lines"]) == 1 \
                and not specials
            for li, ln in enumerate(plan["lines"]):
                if not ln:
                    continue
                _draw_line(page, rect.x0,
                           plan["first_base"] + li * plan["pitch"], ln,
                           plan["size"], color, track=plan["track"],
                           justify_width=(rect.x1 if justify else None),
                           render_mode=rm, border_width=bw,
                           specials=specials if len(plan["lines"]) == 1 else None)
            box = (rect.x0, plan["ink_top"],
                   rect.x0 + max(plan["width"], 0.0), plan["ink_bottom"])
            boxes.append(box)
            drawn_boxes.append((box[0], box[2], box[3]))
            drawn += 1
            if plan["overflow"] > 0.5:
                overflow += 1

    bad = 0
    for a in range(len(boxes)):
        for b in range(a + 1, len(boxes)):
            if _boxes_intersect(boxes[a], boxes[b]):
                bad += 1
    if bad:
        print("  [warn] p%d: 译文墨迹盒相交 %d 处（空白带约束下不应出现，请反馈版式）"
              % (pno, bad))

    for text, reason in keep:
        print("  [skip] p%d: %s，保留原文: %r" % (pno, reason, text[:40]))
    for text in rotated_texts:
        print("  [skip] p%d: 竖排文字暂不支持: %r" % (pno, text[:40]))
    return drawn, covered, overflow


def _process_pdf_inplace(doc, out_path, cache_dir, translate, opts):
    """自有的逐页处理循环：合并换行 + 自适应字号/基线 + 不重叠约束。

    为什么不用上游 ``process_pdf``：它逐行独立翻译、按段落统一字号、并允许把译文
    折行画到下一行的位置 —— 这三件事正是用户看到的"重复译文 / 错位 / 重叠"的根因，
    而它们都在它的循环体里，没法靠替换单个辅助函数修好。
    其余能力（行提取 / 字体度量 / 底色采样 / 画笔 / 页缓存 / 打印口径）全部复用上游。
    """
    target_lang = opts.get("target_lang") or "zh-Hans"
    cjk_scale = _as_float(opts.get("font_scale", 0.92), 0.92)
    if not (0.3 <= cjk_scale <= 2.0):
        cjk_scale = 0.92
    sim_bold = _as_bool(opts.get("sim_bold", False))
    merge = _as_bool(opts.get("merge_lines"), True)

    n_img_in = sum(len(p.get_images(full=True)) for p in doc)
    total_lines = translated = skipped = cached_hits = 0
    overlaps = 0
    stats = {"calls": 0, "chars_in": 0, "chars_out": 0}

    for pno in range(len(doc)):
        page = doc[pno]
        work, texts, rotated_texts = _collect_work(page)
        if not work:
            continue
        total_lines += len(texts)
        cached = pt.load_page_cache(cache_dir, pno + 1)
        if cached is not None and cached.get("texts") == texts:
            trans = {i: t for i, t in enumerate(cached.get("translations") or []) if t}
            cached_hits += len(texts)
        else:
            needed = {w[0] for w in work
                      if _needs_translation(w[2], target_lang)}
            trans = _translate_units(work, needed, translate, target_lang,
                                     stats, merge=merge)
            # 兜底去重：同一列里相邻且高度相似的译文只保留前一行
            columns = _line_columns(
                [{"i": w[0], "rect": w[1], "text": w[2], "size": w[3]}
                 for w in work])
            _dedupe_page(columns, trans)
            pt.save_page_cache(cache_dir, pno + 1, {
                "texts": texts,
                "translations": [trans.get(i) for i in range(len(texts))],
            })
        drawn, covered, overflow = _draw_page_inplace(
            page, pno + 1, work, trans, cjk_scale, sim_bold, rotated_texts)
        translated += drawn + covered
        skipped += (len(texts) - drawn - covered) + len(rotated_texts)
        overlaps += overflow
        print("  p%d 完成" % (pno + 1))

    n_img_out = sum(len(p.get_images(full=True)) for p in doc)
    doc.save(out_path, garbage=4, deflate=True)
    print("\n输出: %s" % out_path)
    print("统计: 待译行 %d，已译 %d，跳过 %d" % (total_lines, translated, skipped)
          + ("，缓存复用 %d 行" % cached_hits if cached_hits else ""))
    print("图片: 输入 %d 张 -> 输出 %d 张" % (n_img_in, n_img_out)
          + ("  ✅ 数量一致" if n_img_in == n_img_out else "  ⚠️ 不一致！"))
    if overlaps:
        print("  [warn] 共 %d 行译文受宽度下限限制、略有水平溢出" % overlaps)
    return stats


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


def run(src_path, out_path, *, translate, progress=None, options=None):
    """把 ``src_path`` 原地翻译成 ``out_path``（版式保留，图片零改动）。

    :param src_path: 输入 PDF 绝对路径（只读，绝不修改）
    :param out_path: 输出 PDF 绝对路径
    :param translate: ``translate(texts, context) -> list[str]``，必须等长同序
    :param progress: 可选 ``progress(done, total, note)``，单调递增，最终 done == total
    :param options: sim_bold / font_scale / font / source_lang / target_lang / cache_dir
    :returns: 契约 dict（units / chars_in / chars_out / skipped / pages / detail）
    """
    if not callable(translate):
        raise RuntimeError("缺少 translate 翻译回调，无法翻译")
    if not src_path or not os.path.isfile(src_path):
        raise RuntimeError("找不到输入文件：%s" % (src_path,))

    opts = dict(options or {})
    # font_scale / sim_bold 在这里只做「明显不合理的值回退默认」的入口校验，
    # 真正的排版参数由 _process_pdf_inplace 读取（它会再校验一次范围）。
    _as_bool(opts.get("sim_bold", False))
    if not (0.3 <= _as_float(opts.get("font_scale", 0.92), 0.92) <= 2.0):
        opts["font_scale"] = 0.92
    # 字体：页面选 auto 时用系统里的 CJK 字体（候选清单在 pipelines/__init__.py）。
    # 不这么做的话 Linux 上会退回内置 china-s：字形画得出来，但 PDF 里缺
    # ToUnicode，译文复制/搜索出来是乱码（tools/check_pdf_font.py 可复现）。
    font_spec = (opts.get("font") or "").strip()
    if font_spec in ("", "auto"):
        font_spec = _system_cjk_font() or "auto"

    # 1) 只读探测：页数 + 输入图片数 + **有没有文本层**
    try:
        with fitz.open(src_path) as doc:
            pages = doc.page_count
            # 扫描件预检：整份文档一行文本都取不到，说明文字在图片里。
            # 不做这个检查的话，任务会"成功"地输出一份没翻过的文件 —— 用户看到的
            # 是「完成」但内容没变，比报错更让人困惑。
            has_text = False
            for page in doc:
                if _page_texts(page):
                    has_text = True
                    break
    except Exception as exc:                    # noqa: BLE001
        raise RuntimeError("无法打开输入 PDF：%s" % (exc,)) from exc
    if pages <= 0:
        raise RuntimeError("输入 PDF 没有页面")
    if not has_text:
        raise RuntimeError(
            "这份 PDF 没有文本层（整页是图片，属于扫描件），原位翻译无从下手。"
            "请改用「扫描版原位翻译（OCR）」模式：它会先做 OCR 识别文字再翻译。")

    cache_dir = _cache_dir_for(src_path, out_path, opts)
    out_dir = os.path.dirname(os.path.abspath(out_path)) or "."
    os.makedirs(out_dir, exist_ok=True)
    part_path = out_path + ".part"
    _remove_quiet(part_path)

    # 2) 字符统计（翻译调用的等长校验在 _translate_units 里做）
    stats = {"calls": 0, "chars_in": 0, "chars_out": 0}

    # 3) 进度上报（0 -> 页完成 -> total）
    progress_warned = [False]

    def emit(done, note=""):
        if progress is None:
            return
        done = max(0, min(int(done), int(pages)))
        try:
            progress(done, int(pages), note or ("已处理 %d/%d 页" % (done, pages)))
        except Exception as exc:               # noqa: BLE001
            if not progress_warned[0]:
                progress_warned[0] = True
                print("[warn] progress 回调异常（已忽略）：%s" % (exc,),
                      file=sys.stderr)

    prefetch_note = ""
    emit(0, "开始：共 %d 页" % pages)
    # 按**已完成页数**上报，而不是信打印出来的页号。
    # 自有循环按自然页序处理并打印 "p{n} 完成"，但"完成行数 == 已处理页数"这个
    # 口径最稳：没有文本的页不会打印完成行（上游同样如此）。
    # 线上真实反馈：125 页的文档进度条卡在 124/125，用户以为翻译失败了。
    done_pages = [0]

    def _on_page_done(_pno: int) -> None:
        done_pages[0] = min(done_pages[0] + 1, pages)
        emit(done_pages[0], "第 %d/%d 页完成" % (done_pages[0], pages))

    capture = _StdoutCapture(on_page_done=_on_page_done)

    # 4) 串行化 + 处理。上游的字体度量（text_width）与画笔（_insert_char /
    #    _draw_text）都依赖 pt 的**模块级字体全局量**，所以整段处理必须加锁，
    #    否则 Flask 多线程下会串字体、串译文。
    try:
        with _MODULE_LOCK:
            try:
                os.makedirs(cache_dir, exist_ok=True)
                # 绘制失败计数：每次任务开头清零，末尾把「有 N 个字未能绘制」
                # 写进日志与 detail（最坏是个别字缺，而不是整本书白翻）。
                pt.reset_draw_failures()
                try:
                    # 装缺字回退字体（数学符号等），必须在 init_cjk_font 之前
                    _install_fallback_fonts()
                    pt.init_cjk_font(font_spec)
                except Exception as exc:        # noqa: BLE001
                    raise RuntimeError(
                        "中文字体不可用：%s（%s）" % (font_spec, exc)) from exc
                # 4b) 批量预翻译：跨页攒批 + 并发 + **先合并换行再整段翻译**，
                # 把整份文档的译文先写进页缓存。失败不影响正确性（逐页处理时
                # 会自己再翻一遍），所以这里只在真的崩了的时候打一句警告。
                if _as_bool(opts.get("prefetch"), True) and os.environ.get(
                        "DOCBRIDGE_PREFETCH", "1") not in ("0", "false", "no"):
                    try:
                        with fitz.open(src_path) as pdoc:
                            pf = _prefetch_translations(
                                pdoc, lambda texts, ctx=None: translate(texts, ctx),
                                cache_dir, opts,
                                opts.get("target_lang") or "zh-Hans", emit)
                        stats["chars_in"] += pf["chars_in"]
                        stats["chars_out"] += pf["chars_out"]
                        prefetch_note = ("预翻译 %d 行 → %d 批（并发 %d%s）"
                                         % (pf["lines"], pf["batches"],
                                            min(PREFETCH_WORKERS, max(1, pf["batches"])),
                                            "，命中缓存 %d 行" % pf["reused"]
                                            if pf.get("reused") else ""))
                        print("[prefetch] " + prefetch_note, file=sys.stderr)
                    except Exception as exc:    # noqa: BLE001
                        print("[warn] 批量预翻译失败，退回逐页翻译：%s" % (exc,),
                              file=sys.stderr)
                with contextlib.redirect_stdout(capture):
                    with fitz.open(src_path) as wdoc:
                        pstats = _process_pdf_inplace(
                            wdoc, part_path, cache_dir, translate, opts)
                stats["chars_in"] += pstats["chars_in"]
                stats["chars_out"] += pstats["chars_out"]
            except BaseException:
                _remove_quiet(part_path)
                raise
    except BaseException:
        _remove_quiet(part_path)
        raise

    stdout_text = capture.text
    if not os.path.isfile(part_path):
        raise RuntimeError("PDF 生成失败：未产生输出文件")

    # 5) 字体子集化（先压体积，再校验，这样校验覆盖的是最终字节）
    #    ★ 这一步对大文件很慢（125 页的教材要跑几十秒），必须报进度，
    #      否则进度条会停在最后一页不动，看着像卡死。
    emit(pages, "全部 %d 页翻译完成，正在压缩字体（大文件需要一会儿）…" % pages)
    shrink = _shrink_fonts_in_place(part_path)
    emit(pages, "字体处理完成，正在校验图片并保存…")

    # 6) 图片零改动校验（失败会删掉 .part 并抛错）
    n_img_in, n_img_out = _verify_images(stdout_text, src_path, part_path,
                                         part_path)

    # 7) 原子落盘
    try:
        os.replace(part_path, out_path)
    except OSError as exc:
        _remove_quiet(part_path)
        raise RuntimeError("输出文件落盘失败：%s" % (exc,)) from exc

    # 8) 统计（解析失败不崩，给合理值）
    parsed = _parse_stats(stdout_text)
    total_lines = units = skipped = cached_lines = 0
    if parsed:
        total_lines, units, skipped, cached_lines = parsed
    chars_in = stats["chars_in"]
    chars_out = stats["chars_out"]

    font_desc = pt.CJK_FONTFILE or "china-s（内嵌兜底）"
    if pt.CJK_FONTFILE:
        font_desc = "%s（%s）" % (pt.CJK_FONTFILE,
                                getattr(pt, "CJK_FONT_NAME", "") or "?")
    # 绘制失败计数（字体坏 / 缺字形）：如实写进 detail，绝不静默产出坏结果。
    fails = pt.draw_failures() if hasattr(pt, "draw_failures") else {"chars": 0}
    fail_note = ""
    if fails.get("chars"):
        fail_note = "；⚠️ 有 %d 个字未能绘制（字体缺字形或不可用）%s" % (
            fails["chars"],
            "：" + "；".join(fails.get("errors") or []) if fails.get("errors") else "")
    if shrink and shrink[1] < shrink[0]:
        size_note = "；字体子集化 %.1fMB -> %.1fKB" % (
            shrink[0] / 1048576.0, shrink[1] / 1024.0)
    elif shrink:
        size_note = "；字体已是子集（%.1fKB）" % (shrink[1] / 1024.0,)
    else:
        size_note = "；字体子集化未执行（体积可能偏大）"
    detail = (
        "PDF 原位翻译完成：%d 页，待译 %d 行、已译 %d 行、跳过 %d 行%s；"
        "图片 输入 %d 张 -> 输出 %d 张（数量一致 ✅）；字体 %s%s；"
        "缓存目录 %s"
        % (pages, total_lines, units, skipped,
           "（缓存复用 %d 行）" % cached_lines if cached_lines else "",
           n_img_in, n_img_out, font_desc, size_note, cache_dir)
          + fail_note
          + ("；" + prefetch_note if prefetch_note else "")
    )

    emit(pages, "完成")
    return {
        "units": units,
        "chars_in": chars_in,
        "chars_out": chars_out,
        "skipped": skipped,
        "pages": pages,
        "detail": detail,
    }
