"""docbridge · 翻译通道比价

回答一个问题：**用哪个通道更合算？**

做法：拿同一份真实文档跑一遍两个通道，量出各自的 token 用量与耗时，
再按你给的单价比出每千行的成本，并算出「中转站单价要低到多少才比另一个合算」。

为什么不直接把价格写死：中转站是各家自己定价的，公开抓不到；而模型方也在调价。
与其猜，不如把单价做成参数，其余全部实测。

用法：
    cd /opt/docbridge
    .venv/bin/python tools/compare_channels.py \
        --pdf "/path/to/book.pdf" --pages 6 \
        --snow-in 2 --snow-out 8 \
        --relay-in 0.5 --relay-out 1.5

    单价单位：元 / 百万 tokens（输入、输出分别给）。
    不给单价也能跑：只报 token 与耗时，并给出「谁更省 token」的结论。

线上实测参考（同一份财务教材章节，45 行样本 / 51 行术语页）：
    snow（硅基流动 DeepSeek-V3.2）  输入 ~650 / 输出 ~450 tokens，单批 19~56 秒
    relay（中转站 gpt-5.5）          输入 ~950 / 输出 ~570 tokens，单批 11~12 秒
    → 中转站要的 token 多约 35~45%，但明显更快。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fitz                                    # noqa: E402

import translator as T                         # noqa: E402
import pdf_translate as pt                     # noqa: E402


def sample_lines(pdf_path: str, pages: int) -> list[str]:
    with fitz.open(pdf_path) as doc:
        lines: list[str] = []
        for i in range(min(pages, len(doc))):
            lines += [r[1] for r in pt.collect_lines(doc[i])
                      if pt.LATIN_RE.search(r[1])
                      and not pt.NUMBERISH_RE.match(r[1].strip())]
    return lines


def call_once(cfg: dict, items: list[str]) -> dict:
    """跑一次批量翻译，返回 token 用量与耗时。"""
    prompt = T._build_prompt(items, "Simplified Chinese (简体中文)", "line")
    sink: dict = {}
    started = time.time()
    text = T.call_chat(cfg, T.SYSTEM_LINE, prompt, json_mode=True, usage_sink=sink)
    elapsed = time.time() - started
    parsed = T._parse_json_reply(text, len(items))
    tin = sink.get("tokens_in") or 0
    tout = sink.get("tokens_out") or 0
    if not tin:                     # 拿不到就按字符粗略估
        tin = int(sum(len(i) for i in items) / 2.5)
        tout = int(sum(len(v) for v in (parsed or [])) / 1.6)
    return {"sec": round(elapsed, 1), "in": tin, "out": tout,
            "parsed": len(parsed or []), "lines": len(items)}


def per_1k_tokens(m: dict) -> tuple[float, float]:
    if not m["lines"]:
        return 0.0, 0.0
    k = 1000.0 / m["lines"]
    return m["in"] * k, m["out"] * k


def main() -> int:
    ap = argparse.ArgumentParser(description="翻译通道比价（实测 token + 你的单价）")
    ap.add_argument("--pdf", help="用来测量的 PDF（不给则用线上实测的参考值）")
    ap.add_argument("--pages", type=int, default=6)
    ap.add_argument("--snow-in", type=float, default=None, help="snow 输入单价（元/百万 tokens）")
    ap.add_argument("--snow-out", type=float, default=None)
    ap.add_argument("--relay-in", type=float, default=None, help="relay 输入单价（元/百万 tokens）")
    ap.add_argument("--relay-out", type=float, default=None)
    args = ap.parse_args()

    profiles = T.load_profiles()
    channels = [c for c in ("snow", "relay") if c in profiles]
    if not channels:
        print("profiles.json 里没有 snow / relay 通道，先配好再跑")
        return 1

    print("=" * 72)
    if args.pdf:
        items = sample_lines(args.pdf, args.pages)
        print(f"样本：{args.pdf} 前 {args.pages} 页，共 {len(items)} 行待译")
        if not items:
            print("  这个样本没有可翻译的行")
            return 1
        results = {}
        for code in channels:
            cfg = T.resolve_config(code, None)
            print(f"  正在测 {code}（{cfg['api']} / {cfg['chatModel']}）…")
            results[code] = call_once(cfg, items)
    else:
        print("未指定 --pdf，使用线上实测的参考值（45 行样本）")
        results = {
            "snow": {"sec": 19.0, "in": 649, "out": 449, "parsed": 45, "lines": 45},
            "relay": {"sec": 11.9, "in": 945, "out": 575, "parsed": 45, "lines": 45},
        }
        results = {k: v for k, v in results.items() if k in channels}

    print("-" * 72)
    print("%-8s %8s %14s %14s" % ("通道", "耗时", "输入/千行", "输出/千行"))
    for code, m in results.items():
        pin, pout = per_1k_tokens(m)
        print("%-8s %7.1fs %14.0f %14.0f" % (code, m["sec"], pin, pout))

    prices = {
        "snow": (args.snow_in, args.snow_out),
        "relay": (args.relay_in, args.relay_out),
    }
    have_prices = all(p[0] is not None and p[1] is not None
                      for c, p in prices.items() if c in results)

    print("-" * 72)
    if not have_prices:
        print("没给全单价，只能比 token 与耗时：")
        if "snow" in results and "relay" in results:
            si, so = per_1k_tokens(results["snow"])
            ri, ro = per_1k_tokens(results["relay"])
            print("  每千行 token：snow %.0f+%.0f，relay %.0f+%.0f" % (si, so, ri, ro))
            print("  → 中转站要多花约 %.0f%% 的 token（输出按 2 倍输入折算）"
                  % (((ri + 2 * ro) / (si + 2 * so) - 1) * 100))
            need = (si + 2 * so) / (ri + 2 * ro)
            print("  结论：**中转站的单价必须低于另一个通道的 %.0f%% 才更合算**"
                  % (need * 100))
        print("  想直接比价，把单价补上：--snow-in/--snow-out/--relay-in/--relay-out")
        return 0

    costs = {}
    for code, m in results.items():
        pin, pout = per_1k_tokens(m)
        pr_in, pr_out = prices[code]
        costs[code] = (pin * pr_in + pout * pr_out) / 1e6
        print("%-8s 每千行 ≈ %.4f 元  （输入 %.2f + 输出 %.2f 元/百万）"
              % (code, costs[code], pr_in, pr_out))

    if len(costs) == 2:
        win = min(costs, key=costs.get)
        lose = max(costs, key=costs.get)
        ratio = costs[lose] / costs[win] if costs[win] else 0
        print("-" * 72)
        print("更合算：%s（比 %s 便宜 %.1f 倍）" % (win, lose, ratio))
        # 反推盈亏平衡点：另一个通道的单价要降到多少才反过来
        pin_w, pout_w = per_1k_tokens(results[win])
        pin_l, pout_l = per_1k_tokens(results[lose])
        print("盈亏平衡：当 %s 的输入单价低于 %.3f 元/百万（输出按当前比例）时反过来更合算"
              % (lose, (costs[win] - pout_l * prices[lose][1] / 1e6) / (pin_l / 1e6)
                 if pin_l else 0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
