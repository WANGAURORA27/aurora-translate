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
    "/usr/share/fonts/truetype/arphic/uming.ttc",               # AR PL 明体（宋体风格，TrueType）
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",             # 文泉驿正黑（TrueType）
    "/System/Library/Fonts/Supplemental/Songti.ttc",            # macOS 宋体（TrueType）
    "/System/Library/Fonts/Supplemental/Songti.ttf",
    "/System/Library/Fonts/PingFang.ttc",                       # macOS 苹方
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",  # CFF：能正确提取但会撑大文件
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSerifCJK-Regular.ttc",
    "/Library/Fonts/Arial Unicode.ttf",                         # 兜底：符号覆盖面广
)


def default_pdf_font() -> str:
    """返回可用的中文字体路径；返回 "" 表示没找到（调用方会退回内置字体）。

    优先级：环境变量 ``DOCBRIDGE_PDF_FONT`` > 内置候选清单里第一个存在的文件。
    环境变量指向的文件不存在时**不回退**到候选清单，而是返回 ""：
    这样「配置错了」会以可见的方式暴露，而不是悄悄换成另一个字体。
    """
    env = (os.environ.get("DOCBRIDGE_PDF_FONT") or "").strip()
    if env:
        return env if os.path.isfile(env) else ""
    for path in CJK_FONT_CANDIDATES:
        if os.path.isfile(path):
            return path
    return ""


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
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
    "/Library/Fonts/Arial Unicode.ttf",                            # macOS
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
)


def math_fallback_fonts() -> list[str]:
    """可用的缺字回退字体（按优先级，只返回真实存在的）。"""
    return [p for p in MATH_FONT_CANDIDATES if os.path.isfile(p)]


def install_fallback_fonts(upstream) -> list[str]:
    """把回退字体候选装进上游模块，返回实际生效的列表。

    调用时机：**必须在 ``upstream.init_cjk_font()`` 之前**——上游是在那次调用里
    按 FALLBACK_CANDIDATES 挑回退字体的。
    """
    found = math_fallback_fonts()
    if found:
        upstream.FALLBACK_CANDIDATES = list(found)
    return found
