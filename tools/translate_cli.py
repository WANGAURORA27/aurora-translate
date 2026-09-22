#!/usr/bin/env python3
"""aurora-translate · 命令行入口（供 GitHub Actions / 命令行 / 定时任务调用）

网页版走的是 Flask + 任务队列；这里提供一个**同步**的极简入口：
读一份输入文件 → 跑对应管线 → 写出结果。没有队列、没有 HTTP，日志直接打到 stdout。

用法：
    python3 tools/translate_cli.py --input book.pdf --output book_zh.pdf --mode inplace
    python3 tools/translate_cli.py --input scan.pdf --output scan_zh.pdf --mode ocr \\
        --options '{"ocr_lang": "eng", "ocr_dpi": 200}'

密钥来源（二选一）：
    * 环境变量 ``DOCBRIDGE_PROFILES_JSON`` 直接给出 profiles.json 的内容
      （GitHub Actions 里就把仓库 secret 塞进这个变量，**不落盘**）；
    * 或 ``PROFILES_FILE`` 指向一个文件（默认找 ./profiles.json）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _materialise_profiles() -> str | None:
    """把密钥准备好，返回可用的 PROFILES_FILE 路径（没有就返回 None）。

    优先用环境变量里的**内容**：这样文件只存在于临时目录，不会被误提交。
    """
    raw = (os.environ.get("DOCBRIDGE_PROFILES_JSON") or "").strip()
    if not raw:
        return os.environ.get("PROFILES_FILE") or None
    try:
        json.loads(raw)                      # 先验一下是不是合法 JSON
    except ValueError as exc:
        print("[error] DOCBRIDGE_PROFILES_JSON 不是合法 JSON：%s" % exc, file=sys.stderr)
        sys.exit(2)
    fd, path = tempfile.mkstemp(prefix="aurora_profiles_", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(raw)
    os.chmod(path, 0o600)
    os.environ["PROFILES_FILE"] = path
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description="文档翻译命令行入口")
    ap.add_argument("--input", required=True, help="输入文件（.pdf / .docx）")
    ap.add_argument("--output", required=True, help="输出文件路径")
    ap.add_argument("--mode", default="inplace",
                    choices=["inplace", "bilingual", "ocr"],
                    help="inplace=原位版式保留 / bilingual=双语对照 / ocr=扫描件（PDF）")
    ap.add_argument("--profile", default=None, help="预设代号（默认取第一个可用的）")
    ap.add_argument("--strategy", default="fallback",
                    choices=["single", "fallback", "hybrid"], help="通道策略")
    ap.add_argument("--target", default="zh-Hans", help="目标语言")
    ap.add_argument("--source", default="auto", help="源语言")
    ap.add_argument("--options", default="{}", help="额外的管线参数（JSON）")
    args = ap.parse_args()

    profiles_path = _materialise_profiles()

    import jobs as J                                    # noqa: PLC0415
    import translator as T                              # noqa: PLC0415
    from pipelines import PIPELINES                     # noqa: PLC0415

    if not os.path.isfile(args.input):
        print("[error] 找不到输入文件：%s" % args.input, file=sys.stderr)
        return 2

    fmt = J.JobManager.detect_format(args.input)
    key = (fmt, args.mode)
    if key not in PIPELINES:
        names = ", ".join("%s/%s" % k for k in sorted(PIPELINES))
        print("[error] %s 不支持模式 %r；可用：%s" % (fmt, args.mode, names), file=sys.stderr)
        return 2
    module = PIPELINES[key]

    # 通道：优先用指定的代号，否则挑第一个可用的
    code = args.profile
    usable = [p for p in T.public_profiles() if p["usable"]]
    if not code:
        if not usable:
            print("[error] profiles 里没有可用通道；请检查仓库 secret 里的密钥配置",
                  file=sys.stderr)
            return 2
        code = usable[0]["code"]
    try:
        cfg = T.resolve_config(code, None)
    except T.TranslateError as exc:
        print("[error] 通道 %s 不可用：%s" % (code, exc), file=sys.stderr)
        return 2

    print("== aurora-translate ==")
    print("  输入      %s (%s)" % (args.input, fmt))
    print("  模式      %s -> %s" % (args.mode, getattr(module, "LABEL", args.mode)))
    print("  通道      %s:%s" % (code, cfg.get("chatModel")))
    print("  目标语言  %s" % args.target)

    # 备用/攻坚通道（与网页版同一套策略）
    chain = J.pick_channels(code, args.strategy)
    extras = {}
    names = {id(cfg): "%s:%s" % (code, cfg.get("chatModel"))}
    for slot in ("fallback", "escalate"):
        other = chain.get(slot)
        if not other or other == code:
            continue
        try:
            c = T.resolve_config(other, None)
        except T.TranslateError:
            continue
        names[id(c)] = "%s:%s" % (other, c.get("chatModel"))
        extras[slot] = c
    if extras:
        print("  通道链    " + " / ".join(
            ["主 " + names[id(cfg)]] + ["%s %s" % (k, names[id(v)]) for k, v in extras.items()]))

    glossary = T.load_glossary()
    if glossary:
        print("  术语表    %d 条（命中本批的自动注入）" % len(glossary))

    options = json.loads(args.options or "{}")
    options.update({"source_lang": args.source, "target_lang": args.target})

    tr = T.make_translator(cfg, args.source, args.target,
                           kind="line" if fmt == "pdf" else "paragraph",
                           log=lambda m: print("  " + str(m), flush=True),
                           fallback=extras.get("fallback"),
                           escalate=extras.get("escalate"),
                           route=args.strategy, channel_names=names,
                           glossary=glossary)

    last = [""]

    def progress(done, total, note=""):
        note = note or ""
        line = "%s/%s %s" % (done, total, note)
        if line != last[0]:
            last[0] = line
            print("  [进度] " + line, flush=True)

    os.makedirs(os.path.dirname(os.path.abspath(args.output)) or ".", exist_ok=True)
    try:
        result = module.run(args.input, args.output, translate=tr,
                            progress=progress, options=options)
    except Exception as exc:                             # noqa: BLE001
        import traceback                                 # noqa: PLC0415
        print("[error] 翻译失败：%s" % exc, file=sys.stderr)
        traceback.print_exc()
        return 1

    stats = dict(result or {})
    stats.update({"api_" + k: v for k, v in tr.stats.items()})
    size = os.path.getsize(args.output) if os.path.isfile(args.output) else 0
    print("== 完成 ==")
    print("  输出      %s (%.1f MB)" % (args.output, size / 1048576))
    print("  用量      接口调用 %s 次 · 入 %s / 出 %s tokens" % (
        stats.get("api_calls"), stats.get("api_tokens_in"), stats.get("api_tokens_out")))
    print("  详情      %s" % (stats.get("detail") or "")[:400])

    # 给 Actions 用的机器可读结果
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write("| 项 | 值 |\n|---|---|\n")
            fh.write("| 输入 | `%s` |\n" % os.path.basename(args.input))
            fh.write("| 模式 | %s |\n" % getattr(module, "LABEL", args.mode))
            fh.write("| 通道 | %s |\n" % names[id(cfg)])
            fh.write("| 输出 | `%s` (%.1f MB) |\n" % (
                os.path.basename(args.output), size / 1048576))
            for k in ("units", "pages", "skipped", "api_calls",
                      "api_tokens_in", "api_tokens_out"):
                if stats.get(k) is not None:
                    fh.write("| %s | %s |\n" % (k, stats[k]))
    if profiles_path and profiles_path.startswith(tempfile.gettempdir()):
        try:
            os.unlink(profiles_path)                     # 用完即焚
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
