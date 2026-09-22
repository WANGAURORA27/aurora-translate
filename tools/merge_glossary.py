#!/usr/bin/env python3
"""把术语表分片合并进 glossary.json（可重复跑，幂等）

用法：
    python3 tools/merge_glossary.py .glossary-parts            # 先看报告
    python3 tools/merge_glossary.py .glossary-parts --write    # 真的写回

规则：
    * 同名分组 → 合并（已存在的 key 以 glossary.json 为准，不覆盖）；
    * 新分组 → 追加到末尾；
    * 冲突（同一个英文词在两个分片里译法不同）→ 报出来，谁先声明用谁；
    * 太短（<3 字符）或空译文的条目会被跳过（翻译层本来也会忽略它们）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GLOSSARY = os.path.join(ROOT, "glossary.json")
MIN_KEY = 3


def load(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def groups_of(raw: dict) -> dict:
    """只保留 {分组: {英文: 中文}} 结构，跳过 _ 开头的注释。"""
    out = {}
    for key, val in raw.items():
        if str(key).startswith("_") or not isinstance(val, dict):
            continue
        out[str(key)] = {str(k): str(v).strip() for k, v in val.items()}
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("parts_dir", help="分片目录（含 *.json）")
    ap.add_argument("--write", action="store_true", help="写回 glossary.json")
    args = ap.parse_args()

    base = load(GLOSSARY)
    merged = groups_of(base)
    note = base.get("_说明")
    owner: dict[str, tuple[str, str]] = {}      # 英文 → (分组, 中文)
    for group, items in merged.items():
        for en, zh in items.items():
            owner.setdefault(en, (group, zh))

    conflicts, added, skipped = [], {}, []
    for name in sorted(os.listdir(args.parts_dir)):
        if not name.endswith(".json"):
            continue
        part = load(os.path.join(args.parts_dir, name))
        for group, items in groups_of(part).items():
            bucket = merged.setdefault(group, {})
            for en, zh in items.items():
                if len(en) < MIN_KEY or not zh:
                    skipped.append("%s:%s" % (name, en))
                    continue
                if en in owner:
                    if owner[en][1] != zh:
                        conflicts.append("%-34s 已有「%s」（%s） vs 分片「%s」（%s）"
                                         % (en, owner[en][1], owner[en][0], zh, name))
                    continue
                bucket[en] = zh
                owner[en] = (group, zh)
                added[group] = added.get(group, 0) + 1

    total = sum(len(v) for v in merged.values())
    print("分组情况：")
    for group, items in merged.items():
        print("  %-22s %3d 条%s" % (group, len(items),
                                    "（新增 %d）" % added[group] if group in added else ""))
    print("\n合计 %d 条（原 %d 条，新增 %d 条）" % (total, sum(len(v) for v in groups_of(base).values()),
                                                sum(added.values())))
    if conflicts:
        print("\n⚠️ 译法冲突（保留 glossary.json 里的那个）：")
        for line in conflicts:
            print("  " + line)
    if skipped:
        print("\n跳过的短/空条目（%d 条）：%s" % (len(skipped), ", ".join(skipped[:8])))

    if args.write:
        out = {"_说明": note} if note else {}
        # 保持"先原有分组、后新分组"的顺序
        for group in merged:
            out[group] = merged[group]
        if note:
            out["_说明"] = note
            out.move_to_end("_说明", last=False) if hasattr(out, "move_to_end") else None
        with open(GLOSSARY, "w", encoding="utf-8") as fh:
            json.dump(out, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        print("\n已写回 %s" % GLOSSARY)
    return 0


if __name__ == "__main__":
    sys.exit(main())
