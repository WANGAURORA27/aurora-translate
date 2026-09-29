"""docbridge · 中文字体候选与选择

**这是一个叶子模块**：只依赖标准库，绝不 import 本包的其它模块。
原因：管线模块可能被「按文件路径」直接加载（测试就是这么做，
`importlib.util.spec_from_file_location`），那时没有包上下文，
模块内的相对导入 `from . import ...` 会直接报
``attempted relative import with no known parent package``。
所以管线用「按路径加载本文件」的方式取候选清单，任何导入方式都成立。

为什么需要它：Linux 上 PyMuPDF 的内置 `china-s` 字体能把字形画出来，
但 PDF 里缺 ToUnicode 映射——译文看着正常，复制/搜索出来却是乱码
（`tools/check_pdf_font.py` 可以复现整件事）。指定一个真正的系统 CJK
字体就能解决。
"""

from __future__ import annotations

import os

# 按优先级排列。**顺序很重要，不要随意改成「最新的字体放前面」**：
#
#   1) TrueType 轮廓的中文字体放最前（uming 明体 / wqy 文泉驿）：
#      PyMuPDF 的 subset_fonts() 能把它们正确子集化，输出只有几十 KB。
#   2) Noto CJK 是 **CFF/OTF 轮廓**，子集化会失败（日志里出现
#      "MuPDF error: format error: Reserved charstring byte"），
#      结果是整份 ~20MB 字体被嵌进每一页 —— 实测同一份样例：
#          Noto Serif CJK → 19.6MB      uming 明体 → 61KB      wqy-zenhei → 25KB
#      所以它们只作为兜底候选。
#   3) 内置 china-s 不在候选里：它缺 ToUnicode，译文复制出来是乱码。
#
# 明体(uming)是宋体风格，与既有 pdf_translate.py 在 macOS 上用的宋体观感一致，
# 故排第一。
CJK_FONT_CANDIDATES = (
    # ---- Linux：apt 装的 TrueType 中文字体（优先级最高；不要 CFF/OTF）----
    #   fonts-arphic-uming   → /usr/share/fonts/truetype/arphic/uming.ttc
    #   fonts-wqy-zenhei     → /usr/share/fonts/truetype/wqy/wqy-zenhei.ttc
    #   fonts-wqy-microhei   → /usr/share/fonts/truetype/wqy/wqy-microhei.ttc
    #   fonts-arphic-ukai    → /usr/share/fonts/truetype/arphic/ukai.ttc
    #   fonts-droid-fallback → /usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf
    "/usr/share/fonts/truetype/arphic/uming.ttc",               # AR PL 明体（宋体风格，TrueType）
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",             # 文泉驿正黑（TrueType）
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",           # 文泉驿微米黑（TrueType）
    # ukai 是楷体：正文观感偏书法，故排在两款黑体之后（但它同样是 TrueType，
    # 且镜像里确实装了，所以留作第 4 顺位）。
    "/usr/share/fonts/truetype/arphic/ukai.ttc",                # AR PL 楷体（TrueType）
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",  # Droid 全字库回退（TrueType）
    # ---- macOS 开发机 ----
    "/System/Library/Fonts/Supplemental/Songti.ttc",            # macOS 宋体（TrueType）
    "/System/Library/Fonts/Supplemental/Songti.ttf",
    "/System/Library/Fonts/PingFang.ttc",                       # macOS 苹方
    # ---- 兜底：Noto CJK 是 CFF/OTF 轮廓，子集化会失败，只作最后手段 ----
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSerifCJK-Regular.ttc",
    "/Library/Fonts/Arial Unicode.ttf",                         # 兜底：符号覆盖面广
)

# 这些候选"存在"不代表"能用"：CFF/OTF、损坏的 .ttc、缺 cmap 的字体都可能
# 加载成功却取不到字宽（线上真实崩溃）。调用方用 usable_cjk_fonts(probe) 逐个
# 真测一遍（probe 由管线注入，因为本模块刻意只依赖标准库）。
APT_PACKAGES = (
    "fonts-arphic-uming",
    "fonts-wqy-zenhei",
    "fonts-wqy-microhei",
    "fonts-arphic-ukai",
    "fonts-droid-fallback",
)


def cjk_font_candidates() -> list[str]:
    """候选清单里**真实存在**的中文字体路径（按优先级，含重复路径去重）。"""
    out, seen = [], set()
    for path in CJK_FONT_CANDIDATES:
        if path in seen or not os.path.isfile(path):
            continue
        seen.add(path)
        out.append(path)
    return out


def usable_cjk_fonts(probe=None) -> list[str]:
    """候选里**真的能用来画字**的字体路径（按优先级）。

    probe: ``callable(path) -> bool``，由调用方注入（管线用 pdf_translate.font_drawable，
    它会在临时页上真的 insert 一个字）。probe 为 None 时退化成"文件存在"。
    """
    found = cjk_font_candidates()
    if probe is None:
        return found
    out = []
    for path in found:
        try:
            if probe(path):
                out.append(path)
        except Exception:                   # noqa: BLE001 —— 探测本身出错就当不可用
            continue
    return out


def default_pdf_font(probe=None) -> str:
    """返回可用的中文字体路径；返回 "" 表示没找到（调用方会退回内置字体）。

    优先级：环境变量 ``DOCBRIDGE_PDF_FONT`` > 内置候选清单里第一个存在的文件。
    环境变量指向的文件不存在时**不回退**到候选清单，而是返回 ""：
    这样「配置错了」会以可见的方式暴露，而不是悄悄换成另一个字体。

    probe 非空时按「真的能画字」筛候选（由管线注入 ``pdf_translate.font_drawable``）；
    不传则退化成"文件存在即可"（本模块刻意只依赖标准库）。
    """
    env = (os.environ.get("DOCBRIDGE_PDF_FONT") or "").strip()
    if env:
        return env if os.path.isfile(env) else ""
    found = usable_cjk_fonts(probe)
    return found[0] if found else ""


def selected_font_name(upstream) -> str:
    """上游当前选中字体的**字体名**（写日志/stats 用；取不到给文件名）。"""
    path = getattr(upstream, "CJK_FONTFILE", None)
    if not path:
        return "china-s（内嵌兜底）"
    name = getattr(upstream, "CJK_FONT_NAME", None)
    return "%s（%s）" % (path, name) if name else path


# ---------------------------------------------------------------- 缺字回退字体
# 上游 pdf_translate 的 FALLBACK_CANDIDATES 全是 macOS 路径，在 Linux 上等于**没有回退字体**：
# 主字体（uming）缺 ∂ ∆ ⊂ 这类数学符号时，会静默画成空白/缺字框——排版看着"成功"，实际丢了符号。
#
# 实测 20 个常见数学符号的覆盖：
#     uming（当前中文主字体）17/20，缺 ∂ ∆ ⊂
#     DejaVuSans             20/20   ← 选它
#     NotoSansCJK            20/20   （CFF 轮廓，会把输出撑大几十倍，不用）
# DejaVuSans 是 TrueType 轮廓，字体子集化正常，且 Ubuntu/Debian 默认就装了。
MATH_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansMath-Regular.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
    "/Library/Fonts/Arial Unicode.ttf",                            # macOS
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
)
# ★ 注意：DejaVu / Liberation / NotoSansMath **只有拉丁与符号，不含汉字**，
#   所以它们只能当"数学符号回退字体"，绝不能进 CJK_FONT_CANDIDATES 当汉字兜底
#   （否则整份中文会被画成空白/缺字框）。真正含汉字的是上面那 5 个 TrueType/TTC。


def math_fallback_fonts() -> list[str]:
    """可用的缺字回退字体（按优先级，只返回真实存在的）。"""
    return [p for p in MATH_FONT_CANDIDATES if os.path.isfile(p)]


def install_fallback_fonts(upstream) -> list[str]:
    """把回退字体候选 + 「按可用性筛过的中文字体候选」装进上游模块。

    调用时机：**必须在 ``upstream.init_cjk_font()`` 之前**——上游是在那次调用里
    按 FALLBACK_CANDIDATES / FONT_CANDIDATES 挑字体的。

    为什么要注入 FONT_CANDIDATES：上游自带的候选清单是 macOS 路径，Linux 上等于
    空清单；而显式指定的字体一旦不可用，上游会退到这份清单里找替代（见
    ``pdf_translate.init_cjk_font`` 的优雅降级）。这里注入的是**逐个真测过能画字**
    的 Linux/macOS 候选（优先 uming），坏字体在选字体阶段就被刷掉。
    """
    found = math_fallback_fonts()
    if found:
        upstream.FALLBACK_CANDIDATES = list(found)
    probe = getattr(upstream, "font_drawable", None)
    usable = usable_cjk_fonts(probe)
    if usable:
        rest = [p for p in getattr(upstream, "FONT_CANDIDATES", ()) if p not in usable]
        upstream.FONT_CANDIDATES = usable + rest
    return found
