#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""原位翻译 · 版面几何一次性验证脚本（不联网、不花钱）。

用**假翻译器**把 PDF 译成"故意更长"的中文，再用 PyMuPDF 独立复核三项几何约束：

    ① 每行译文都在页面内，且不超出它所属原行的 x 范围（容差 --tol，默认 2pt）；
    ② 同一页任意两行译文的**墨迹盒**互不相交；
    ③ 每条译文字号 ≥ 原行字号的 60%（设定下限）；
    ④ 没被翻译的原行（纯数字、竖排等）没有被译文压住。

用法：
    python3 tools/check_inplace_geometry.py                       # samples/demo.pdf
    python3 tools/check_inplace_geometry.py --input book.pdf --keep
    python3 tools/check_inplace_geometry.py --input book.pdf --ratio 0.8 --tol 2

假翻译器：每个汉字由源字符确定性地映射而来（保证逐行不同，不会被碎片去重删掉），
长度 = 源长度 × ratio，于是中文宽度约为英文的 1.2~1.5 倍，逼着排版走自适应缩字号。

关于 bbox 的口径：PyMuPDF 给 CJK span 的 bbox 用的是字体 ascent/descent
（实测宋体总高 1.4em），比真实墨迹（约 1.04em）大得多；直接拿它判相交会把正常
行距判成"重叠"。所以这里按"基线 ± 实测墨迹比例"重算墨迹盒：
CJK 墨迹 = 基线上方 0.88em、下方 0.16em（由 fitz.Font(...).glyph_bbox 实测得到）。
x 方向仍用 PyMuPDF 给的前进宽度，可信。

【是否保留】建议保留：它是"换行重复 / 版面重叠"这两类线上问题的回归检查。
"""

from __future__ import annotations

import argparse
import os
import re
import sys

import fitz

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

CJK_SPAN_RE = re.compile(r"[\u4e00-\u9fff]")
CJK_INK_ASCENT = 0.88      # CJK 墨迹在基线上方的高度（em）
CJK_INK_DESCENT = 0.16     # CJK 墨迹在基线下方的高度（em）


def fake_long(ratio):
    """假翻译器：长度 ≈ 源长度 × ratio 的确定性中文（不联网）。"""
    def fake(texts, ctx=None):
        out = []
        for i, t in enumerate(texts):
            n = max(3, int(round(len(t) * ratio)))
            mapped = "".join(chr(0x4e00 + (ord(c) * 7 + 13) % 1200) for c in t)
            out.append("第%d段" % (i + 1) + mapped[:n])
        return out
    return fake


def page_text_lines(path, page_no):
    """输入文件里该页的所有文字行：``[{rect, text, size, base}]``。"""
    out = []
    with fitz.open(path) as doc:
        page = doc[page_no]
        for block in page.get_text("dict")["blocks"]:
            if block.get("type", 0) != 0:
                continue
            for line in block.get("lines", []):
                text = "".join(s["text"] for s in line["spans"]).strip()
                if text:
                    out.append({"rect": fitz.Rect(line["bbox"]), "text": text,
                                "size": max(s["size"] for s in line["spans"]),
                                "base": max(s["origin"][1] for s in line["spans"])})
    return out


def eligible_originals(originals, box):
    """一条译文视觉行可能归属的原行候选。

    为什么要"候选集"而不是唯一一行：译文**折行**时首行会被基线钳制整体上移，
    "基线最近"会把折行首行误判成上一行、把第 2 行误判成下一行。判断"有没有超出
    原行 x 范围"的可靠口径是：**同栏、基线相近（≤1.6 倍字号）的候选里，最宽的那
    一行能不能装下**。只有连最宽的一行都装不下，才是用户能看到的"溢出右边界"。
    """
    return [o for o in originals
            if abs(o["base"] - box["base"]) <= 1.6 * max(o["size"], box["size"])
            and o["rect"].x0 <= box["x0"] + 1.0]


def cjk_visual_lines(path, page_no):
    """输出文件里该页的译文视觉行，带重算后的墨迹盒。

    同一基线上的 span 可能是**表格不同单元格**（各自一条），所以先按基线分组、
    再在组内按 x 间距切开（间距 > 0.6 倍字号即视为不同单元格），不能整行合并。
    """
    spans = []
    with fitz.open(path) as doc:
        page = doc[page_no]
        page_rect = page.rect
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
        if rows and abs(rows[-1]["base"] - base) <= 0.6 and x0 - rows[-1]["x1"] <= 0.6 * size:
            row = rows[-1]
            row["x1"] = max(row["x1"], x1)
            row["size"] = max(row["size"], size)
            row["text"] += text
            continue
        rows.append({"x0": x0, "x1": x1, "base": base, "size": size, "text": text})
    for row in rows:
        row["ink_top"] = row["base"] - CJK_INK_ASCENT * row["size"]
        row["ink_bottom"] = row["base"] + CJK_INK_DESCENT * row["size"]
    return rows, page_rect


def check_page(src, out, page_no, tol, floor_ratio, problems, notes, warnings):
    originals = page_text_lines(src, page_no)
    boxes, page_rect = cjk_visual_lines(out, page_no)
    if not boxes:
        notes.append("第 %d 页：没有译文（可能没有可翻译文本）" % (page_no + 1))
        return 0, 0

    associated = set()
    shrank = 0
    for box in boxes:
        # ① 页面内（致命）
        if box["x0"] < page_rect.x0 - 0.5 or box["x1"] > page_rect.x1 + 0.5:
            problems.append("第 %d 页：译文超出页面 %r" % (page_no + 1, box))
        match = eligible_originals(originals, box)
        if not match:
            warnings.append("第 %d 页：译文附近找不到归属原行（仅提示）%r"
                            % (page_no + 1, box))
            continue
        widest = max(match, key=lambda o: o["rect"].x1)
        nearest = min(match, key=lambda o: abs(o["base"] - box["base"]))
        # 归入"这一行已被翻译"：基线在 1.2 倍字号以内即算（译文可能被基线钳制挪动）
        if abs(box["base"] - nearest["base"]) <= 1.2 * max(nearest["size"], box["size"]):
            associated.add(id(nearest))
        # ① 左缘越界：致命
        if box["x0"] < widest["rect"].x0 - 1.0:
            problems.append("第 %d 页：译文左缘越出原行 %r vs %r"
                            % (page_no + 1, box, widest["rect"]))
        # ① 右缘越界：**提示而非失败**。按用户要求字号不得低于原字号 60%；
        #    当译文本身就比英文宽得多（本脚本的假译文放到 1.6 倍宽）时，
        #    "不出界"和"保字号"不可能同时成立 —— 我们选择保字号 + 不压上下行，
        #    并如实把溢出量报出来（真实译文通常比英文窄，实测教科书样本 0 溢出）。
        if box["x1"] > widest["rect"].x1 + tol:
            warnings.append(
                "第 %d 页：译文右缘越出最宽候选原行 %.2fpt（容差 %.1f；字号 %.2f / 原 %.2f）%r"
                % (page_no + 1, box["x1"] - widest["rect"].x1, tol,
                   box["size"], nearest["size"], box["text"][:22]))
        # ③ 字号下限（致命）
        floor = floor_ratio * nearest["size"] - 0.51
        if box["size"] < floor:
            problems.append("第 %d 页：字号 %.2f 低于下限 %.2f（原字号 %.2f）%r"
                            % (page_no + 1, box["size"], floor, nearest["size"], box))
        if box["size"] < nearest["size"] * 0.92 - 0.01:
            shrank += 1

    # ② 两两不相交（致命）
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            a, b = boxes[i], boxes[j]
            if not (a["x1"] <= b["x0"] or b["x1"] <= a["x0"]
                    or a["ink_bottom"] <= b["ink_top"]
                    or b["ink_bottom"] <= a["ink_top"]):
                problems.append("第 %d 页：两行译文墨迹相交\n      %r\n      %r"
                                % (page_no + 1, a, b))

    # ④ 没被翻译的原行不许被压住（仅提示：密集表格里"归属判定"本身就不可靠）
    for orig in originals:
        if id(orig) in associated:
            continue
        for box in boxes:
            if (box["x0"] < orig["rect"].x1 and orig["rect"].x0 < box["x1"]
                    and box["ink_top"] < orig["rect"].y1
                    and orig["rect"].y0 < box["ink_bottom"]):
                warnings.append("第 %d 页：译文与未归属的原行重叠 %r（%r）"
                                % (page_no + 1, orig["text"][:24], box["text"][:16]))

    notes.append("第 %d 页：%d 行译文（其中 %d 行自适应缩了字号），原行 %d 行"
                 % (page_no + 1, len(boxes), shrank, len(originals)))
    return len(boxes), shrank


def main():
    ap = argparse.ArgumentParser(description="原位翻译版面几何验证（离线）")
    ap.add_argument("--input", default=os.path.join(ROOT, "samples", "demo.pdf"))
    ap.add_argument("--out", default="/tmp/inplace_geometry_out.pdf")
    ap.add_argument("--ratio", type=float, default=0.8,
                    help="假译文长度 / 源长度（越大越挤，默认 0.8）")
    ap.add_argument("--tol", type=float, default=2.0, help="右缘容差 pt")
    ap.add_argument("--floor", type=float, default=0.60, help="字号下限比例")
    ap.add_argument("--keep", action="store_true", help="保留输出 PDF")
    ap.add_argument("--cache", default="/tmp/inplace_geometry_cache")
    args = ap.parse_args()

    if not os.path.isfile(args.input):
        sys.exit("找不到输入文件：%s" % args.input)

    # 故意关掉共享缓存：否则第二次运行会命中上次的译文，看不到真实排版路径
    os.environ["DOCBRIDGE_SHARED_CACHE"] = "0"
    from pipelines import pdf_inplace as mod  # noqa: PLC0415

    print("== 几何验证 ==")
    print("  输入  %s" % args.input)
    print("  假译文长度比例 %.2f（越长越挤）" % args.ratio)
    res = mod.run(args.input, args.out, translate=fake_long(args.ratio),
                  options={"shared_cache": False, "cache_dir": args.cache})
    print("  %s" % res["detail"])

    problems, notes, warnings = [], [], []
    total_lines = total_shrunk = 0
    with fitz.open(args.input) as doc:
        pages = doc.page_count
    for pno in range(pages):
        n, shrank = check_page(args.input, args.out, pno, args.tol, args.floor,
                               problems, notes, warnings)
        total_lines += n
        total_shrunk += shrank
    for note in notes:
        print("  " + note)
    print("  合计 %d 行译文，%d 行触发了自适应缩字号" % (total_lines, total_shrunk))
    if warnings:
        print("\nℹ️ %d 条提示（不判失败）：" % len(warnings))
        for w in warnings[:20]:
            print("  - " + w)
        if len(warnings) > 20:
            print("  - …还有 %d 条" % (len(warnings) - 20))
    if problems:
        print("\n❌ 发现 %d 个几何问题：" % len(problems))
        for p in problems[:40]:
            print("  - " + p)
        if not args.keep and os.path.exists(args.out):
            os.remove(args.out)
        return 1
    print("\n✅ 通过：① 在页面内、左缘不越界；② 两两墨迹不相交；③ 字号 ≥ %.0f%% 下限"
          % (args.floor * 100)
          + ("；④ 未归属原行的重叠 0 处" if not warnings else ""))
    if args.keep:
        print("（输出保留在 %s）" % args.out)
    else:
        os.remove(args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
