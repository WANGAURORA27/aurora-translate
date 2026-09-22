"""docbridge · 管线注册表

`app.py` 只认这张表：格式 × 模式 → 管线模块。
各模块按 CONTRACT.md 导出 ``FORMAT`` / ``MODE`` / ``LABEL`` / ``NOTE`` /
``OPTIONS`` / ``run()``，前端据此自动渲染「格式 / 模式 / 参数」控件，
不需要前后端各写一遍。

导入采用**容错**策略：某个模块缺失或自身有语法/依赖错误时，其余模式仍可正常使用，
出错原因会通过 ``IMPORT_ERRORS`` 一路透到页面（而不是让整个站点 500）。
"""

from __future__ import annotations

import importlib
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 让管线模块能 import 既有的 pdf_translate.py（注意上级目录名末尾有空格）
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# (格式, 模式, 模块名)
_SPECS = [
    ("pdf", "inplace", "pdf_inplace"),
    ("pdf", "bilingual", "pdf_bilingual"),
    ("pdf", "ocr", "pdf_ocr"),          # 扫描版 PDF（无文本层）：OCR 后原位替换
    ("docx", "inplace", "docx_pipeline"),
    ("docx", "bilingual", "docx_pipeline"),
]

# ---------------------------------------------------------------- 中文字体
# 候选清单与判定逻辑放在叶子模块 fonts.py，这里只做转出，
# 供 app.py / jobs.py 使用（管线自己也能按路径加载，见 fonts.py 的说明）。
from .fonts import CJK_FONT_CANDIDATES, default_pdf_font  # noqa: E402,F401


# 参数控件的展示定义：模块只声明「我认得哪些键」，长什么样在这里统一描述
OPTION_SCHEMA = {
    "ocr_lang": {
        "label": "OCR 语言",
        "type": "select",
        "default": "eng",
        "choices": [("eng", "英文（扫描件默认）"), ("chi_sim", "简体中文"),
                    ("eng+chi_sim", "中英混排（较慢）")],
        "hint": "tesseract 语言包；中文需要 apt install tesseract-ocr-chi-sim",
    },
    "ocr_dpi": {
        "label": "OCR 分辨率",
        "type": "number",
        "default": 200, "min": 150, "max": 400, "step": 50,
        "hint": "越高识别越准但越慢；清晰扫描件 200 足够，字小/模糊可调到 300",
    },
    "shared_cache": {
        "label": "跨任务共享缓存",
        "type": "bool",
        "default": True,
        "hint": "按文件内容+语言对+模型共享译文缓存：同一份文件重跑（或换排版参数"
                "重跑）几乎零成本；默认 72 小时后自动清理",
    },
    "prefetch": {
        "label": "批量预翻译",
        "type": "bool",
        "default": True,
        "hint": "先把整份文档跨页攒批并发翻译好（更便宜、更快、术语更一致）；"
                "关掉则退回上游的逐页串行翻译",
    },
    "sim_bold": {
        "label": "粗体描边",
        "type": "bool",
        "default": False,
        "hint": "原文粗体行用描边模拟粗体中文（内嵌中文字体没有粗体变体）",
    },
    "font_scale": {
        "label": "中文字号比例",
        "type": "number",
        "default": 0.92,
        "min": 0.7, "max": 1.2, "step": 0.02,
        "hint": "相对英文原字号的视觉比例；偏小更保险，不会撑出原行框",
    },
    "bilingual_layout": {
        "label": "对照排版",
        "type": "select",
        "default": "side",
        "choices": [("side", "左右并排"), ("stack", "上下堆叠")],
        "hint": "原文与译文怎么摆",
    },
    "keep_original_style": {
        "label": "保留原样式",
        "type": "bool",
        "default": True,
        "hint": "译文段落尽量沿用原文的样式（标题仍是标题）",
    },
    "insert_after": {
        "label": "译文插在原文后",
        "type": "bool",
        "default": True,
        "hint": "双语模式下，译文段落紧跟原文段落",
    },
    "font": {
        "label": "中文字体",
        "type": "select",
        "default": "auto",
        "choices": [("auto", "系统宋体（推荐）"), ("china-s", "内嵌兜底字体")],
        "hint": "服务器上没有宋体时用内嵌字体，避免中文画不出来",
    },
}

# 由页面顶部的「源语言/目标语言」下拉统一提供的键，
# 不重复出现在每个管线的参数区（模块可能在 OPTIONS 里声明它们）。
GLOBAL_OPTION_KEYS = {"source_lang", "target_lang"}

PIPELINES: dict[tuple[str, str], object] = {}
IMPORT_ERRORS: dict[tuple[str, str], str] = {}
_MODULES: dict[str, object] = {}


def _load(module_name: str):
    if module_name in _MODULES:
        return _MODULES[module_name]
    mod = None
    try:
        mod = importlib.import_module(f"pipelines.{module_name}")
    except Exception as exc:                                  # noqa: BLE001
        IMPORT_ERRORS[("?", module_name)] = f"{type(exc).__name__}: {exc}"
        mod = None
    _MODULES[module_name] = mod
    return mod


for _fmt, _mode, _mod_name in _SPECS:
    _mod = _load(_mod_name)
    if _mod is None:
        IMPORT_ERRORS[(_fmt, _mode)] = IMPORT_ERRORS.get(
            ("?", _mod_name), f"模块 pipelines.{_mod_name} 不可用")
        continue
    if not hasattr(_mod, "run"):
        IMPORT_ERRORS[(_fmt, _mode)] = f"pipelines.{_mod_name} 没有导出 run()"
        continue
    PIPELINES[(_fmt, _mode)] = _mod

FORMATS = sorted({fmt for fmt, _ in PIPELINES}) or ["pdf", "docx"]

# 一个模块同时承担两种模式时（MODE == "both"），标签/说明按模式区分，
# 否则前端会出现两张一模一样的卡片。模块可用 LABELS / NOTES 字典自行指定。
MODE_LABELS = {"inplace": "原位版式保留", "bilingual": "双语对照",
               "ocr": "扫描版原位翻译（OCR）"}
FORMAT_LABELS = {"pdf": "PDF", "docx": "Word"}


def _label_for(mod, fmt: str, mode: str) -> str:
    labels = getattr(mod, "LABELS", None)
    if isinstance(labels, dict) and labels.get(mode):
        return str(labels[mode])
    # 只承担一种模式的模块：直接用它自己声明的 LABEL，比维护「模式名→中文」映射更不容易漏
    if str(getattr(mod, "MODE", "")).lower() != "both":
        lab = str(getattr(mod, "LABEL", "") or "").strip()
        if lab:
            return lab
    prefix = FORMAT_LABELS.get(fmt, fmt.upper()) + " · "
    return prefix + MODE_LABELS.get(mode, mode)


def _note_for(mod, mode: str) -> str:
    notes = getattr(mod, "NOTES", None)
    if isinstance(notes, dict) and notes.get(mode):
        return str(notes[mode])
    note = str(getattr(mod, "NOTE", "") or "")
    # "both" 模块的 NOTE 往往是一句话概括两种模式，这里不做拆分，原样展示
    return note


def pipeline_info() -> list[dict]:
    """给前端的管线清单：每行含可用状态、说明与参数控件定义。"""
    out = []
    seen: set[tuple[str, str]] = set()
    for fmt, mode, _name in _SPECS:
        if (fmt, mode) in seen:
            continue
        seen.add((fmt, mode))
        mod = PIPELINES.get((fmt, mode))
        if mod is None:
            out.append({
                "format": fmt, "mode": mode, "available": False,
                "label": _label_for(None, fmt, mode) if False else
                         FORMAT_LABELS.get(fmt, fmt.upper()) + " · " + MODE_LABELS.get(mode, mode),
                "note": "",
                "error": IMPORT_ERRORS.get((fmt, mode), "管线不可用"),
                "options": [],
            })
            continue
        option_keys = [k for k in (getattr(mod, "OPTIONS", []) or [])
                       if k not in GLOBAL_OPTION_KEYS]
        options = []
        for key in option_keys:
            spec = dict(OPTION_SCHEMA.get(key, {"label": key, "type": "text"}))
            spec["key"] = key
            options.append(spec)
        out.append({
            "format": fmt,
            "mode": mode,
            "available": True,
            "label": _label_for(mod, fmt, mode),
            "note": _note_for(mod, mode),
            "options": options,
        })
    return out
