# -*- coding: utf-8 -*-
"""docbridge 管线：PDF · 原位版式保留。

本模块**不重新实现**翻译/排版逻辑，而是把既有的生产脚本
``<仓库目录>//pdf_translate.py``（PyMuPDF 实现，
段落字号统一 / 基线对齐 / 两端对齐 / 中文禁则 / 背景色采样覆盖 / 断点续跑缓存 /
图片零改动）包装成网站可调用的 ``run()``。

包装方式（不改动上游文件）：

* 注入翻译回调：临时把 ``pt.translate_lines`` 换成 shim，接上 ``translate`` 回调；
  整段替换 + 调用用模块级 ``threading.RLock()`` 串行化（Flask 多线程安全）。
  同时临时替换 ``pt.cache_dir_for``，把断点续跑缓存指到可配置目录。
  两处替换都在 ``try/finally`` 里还原，无论成功失败都不留副作用。
* 进度上报：用 ``contextlib.redirect_stdout`` 捕获上游 stdout，边写边解析
  ``p<N> 完成``，据此调用 ``progress(done, total, note)``；结束时补一次
  ``progress(total, total, "完成")``（上游会跳过没有文本的页，不打印完成行）。
* 原子落盘：让上游写 ``out_path + ".part"``，全部校验通过后 ``os.replace()``；
  任何失败都删除 ``.part``。
* 图片零改动校验：解析上游自检行「图片: 输入 N 张 -> 输出 M 张」，并用 fitz
  独立复核一遍，N != M 直接抛 ``RuntimeError`` 并删除 ``.part``。
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
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
        return mod.default_pdf_font()
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

# 上游 process_pdf / translate_lines 都是模块级函数，替换期间必须串行化。
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
    """把整份 PDF 的待译行攒批翻译好并写进上游页缓存。

    返回统计字典（批次/行数/页数/字符数）。任何一步出问题都由调用方决定
    是否降级——这里不吞异常：取不到译文时宁可按老路子逐页翻，也不要出半成品。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # ★ 上游的页号口径有个坑，必须跟着它走：
    #   process_pdf 用 parse_pages(None, n) 拿到的是 **0 基**索引 0..n-1，
    #   却拿它做 doc[pno - 1]，并以同一个 pno 作为缓存键（page_{pno}.json）。
    #   于是**最后一页**被当作 pno=0 处理，缓存键是 page_0.json 而不是 page_n.json。
    #   不跟着走的话，最后一页永远命中不了缓存（实测：5 页里只命中 4 页）。
    total_pages = len(doc)
    keys = [(i + 1) % total_pages if total_pages else 0
            for i in range(total_pages)]

    per_page = []          # [(缓存键, 自然页号, [text, ...])]
    wanted = []            # [(页序号, idx, text)] 需要翻译的行
    reused: dict[tuple[int, int], str] = {}      # 缓存里已有的译文
    for i in range(total_pages):
        page = doc[i]
        texts = _page_texts(page)
        key = keys[i]
        per_page.append((key, i + 1, texts))
        # 先认缓存：断点续跑 / 重复提交时，不该把已经翻过的行再翻一遍
        # （既省钱也省时间；上游那份缓存本来就是为这个存在的）
        cached = pt.load_page_cache(cache_dir, key) or pt.load_page_cache(cache_dir, i + 1)
        hit = bool(cached) and cached.get("texts") == texts
        old_trans = (cached or {}).get("translations") or []
        for idx, text in enumerate(texts):
            if not _needs_translation(text, target_lang):
                continue
            if hit and idx < len(old_trans) and old_trans[idx]:
                reused[(i, idx)] = old_trans[idx]
            else:
                wanted.append((i, idx, text))

    cache_hits = len(reused)
    if not wanted:
        return {"batches": 0, "lines": 0, "pages": len(per_page), "reused": cache_hits,
                "chars_in": 0, "chars_out": 0}

    # 跨页攒批：行数与字符数双重上限，避免一个超长请求
    batches: list[list[tuple[int, int, str]]] = []
    cur: list[tuple[int, int, str]] = []
    cur_chars = 0
    for item in wanted:
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
        texts = [t for _p, _i, t in batch]
        out = translate(texts, dict(context, pages=sorted({p for p, _i, _t in batch})))
        if not isinstance(out, (list, tuple)) or len(out) != len(texts):
            raise RuntimeError("预翻译返回数量不符：期望 %d，实际 %s"
                               % (len(texts), len(out) if hasattr(out, "__len__") else "?"))
        return batch, out

    done = 0
    workers = min(PREFETCH_WORKERS, len(batches))
    emit(0, "批量预翻译：%d 行 → %d 批（并发 %d%s）"
         % (len(wanted), len(batches), workers,
            "，命中缓存 %d 行" % cache_hits if cache_hits else ""))
    if workers <= 1:
        for batch in batches:
            b, out = run_batch(batch)
            for (pno, idx, _t), val in zip(b, out):
                results[(pno, idx)] = val
            done += 1
            emit(0, "批量预翻译 %d/%d 批" % (done, len(batches)))
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(run_batch, b) for b in batches]
            for fut in as_completed(futures):
                b, out = fut.result()          # 异常照原样冒泡，交给上层
                for (pno, idx, _t), val in zip(b, out):
                    results[(pno, idx)] = val
                done += 1
                emit(0, "批量预翻译 %d/%d 批" % (done, len(batches)))

    # 写成上游能认的页缓存（texts 与 translations 一一对应，未译的留空）。
    # 两种页号口径各写一份：只有最后一页会多出一个文件，换来的是
    # 上游无论按哪种口径读都能命中。
    for key, natural, texts in per_page:
        if not texts:
            continue
        translations = [results.get((natural - 1, i)) or reused.get((natural - 1, i))
                        for i in range(len(texts))]
        payload = {"texts": texts, "translations": translations}
        pt.save_page_cache(cache_dir, key, payload)
        if natural != key:
            pt.save_page_cache(cache_dir, natural, payload)
    return {
        "batches": len(batches),
        "lines": len(wanted),
        "pages": len(per_page),
        "reused": cache_hits,
        "chars_in": sum(len(t) for _p, _i, t in wanted),
        "chars_out": sum(len(v) for v in results.values()),
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
    sim_bold = _as_bool(opts.get("sim_bold", False))
    font_scale = _as_float(opts.get("font_scale", 0.92), 0.92)
    if not (0.3 <= font_scale <= 2.0):          # 明显不合理的值回退默认
        font_scale = 0.92
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

    # 2) 翻译回调 shim：等长校验 + 字符统计
    stats = {"calls": 0, "chars_in": 0, "chars_out": 0}

    def shim(texts, engine=None):              # noqa: ARG001  (engine 由上游传入，忽略)
        out = translate(list(texts), {"kind": "pdf"})
        if not isinstance(out, (list, tuple)):
            raise RuntimeError("translate 回调必须返回与输入等长的列表")
        if len(out) != len(texts):
            raise RuntimeError(
                "翻译返回数量不符：期望 %d，实际 %d" % (len(texts), len(out)))
        stats["calls"] += 1
        clean = ["" if t is None else (t if isinstance(t, str) else str(t))
                 for t in out]
        stats["chars_in"] += sum(len(t) for t in texts)
        stats["chars_out"] += sum(len(t) for t in clean)
        return {i: t for i, t in enumerate(clean)}

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
    # 按**已完成页数**上报，而不是信上游给的页号。
    # 上游 process_pdf 用 parse_pages(None, n) 拿到的是 0 基索引（0..n-1），却拿它做
    # doc[pno - 1] —— 于是**最后一页是以 pno=0 的身份被处理的**，照页号上报的话
    # 进度条永远停在 (总页数-1)/总页数。线上真实反馈：125 页的文档卡在 124/125，
    # 用户以为翻译失败了。
    done_pages = [0]

    def _on_page_done(_pno: int) -> None:
        done_pages[0] = min(done_pages[0] + 1, pages)
        emit(done_pages[0], "第 %d/%d 页完成" % (done_pages[0], pages))

    capture = _StdoutCapture(on_page_done=_on_page_done)

    # 4) 串行化替换 + 调用（模块级全局，必须锁）
    try:
        with _MODULE_LOCK:
            orig_translate_lines = pt.translate_lines
            orig_cache_dir_for = pt.cache_dir_for

            def _patched_cache_dir(_in_path):
                return cache_dir

            pt.translate_lines = shim
            pt.cache_dir_for = _patched_cache_dir
            try:
                os.makedirs(cache_dir, exist_ok=True)
                try:
                    # 装缺字回退字体（数学符号等），必须在 init_cjk_font 之前
                    _install_fallback_fonts()
                    pt.init_cjk_font(font_spec)
                except Exception as exc:        # noqa: BLE001
                    raise RuntimeError(
                        "中文字体不可用：%s（%s）" % (font_spec, exc)) from exc
                # 4b) 批量预翻译：跨页攒批 + 并发，把整份文档的译文先写进页缓存。
                # 失败不影响正确性（缓存不干净时上游会退回逐页翻译），所以这里
                # 只在真的崩了的时候打一句警告，然后照常往下走。
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
                    pt.process_pdf(src_path, part_path, "auto", sim_bold,
                                   None, font_scale)
            finally:
                pt.translate_lines = orig_translate_lines
                pt.cache_dir_for = orig_cache_dir_for
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
