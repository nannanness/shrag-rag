# -*- coding: utf-8 -*-
"""空表格到底是什么表？看它们前后文，判断丢的是不是关键财务数据。"""
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from shrag_rag.parse import KIND_TABLE, ParsedDoc, parse_html_table  # noqa: E402

KEY = ["主要会计数据", "资产负债表", "利润表", "现金流量表", "所有者权益",
       "财务指标", "营业收入", "净利润", "资产总计", "负债合计"]

for code in ("600108", "600004"):
    doc = ParsedDoc.load(ROOT / "data" / "parsed" / code / "doc.json")
    print("=" * 96)
    print(f"[{code} {doc.name}]")
    print("=" * 96)

    empties = [i for i, b in enumerate(doc.blocks)
               if b.kind == KIND_TABLE and not parse_html_table(b.html)]
    print(f"  空表格 {len(empties)} / {doc.count(KIND_TABLE)} "
          f"({len(empties) / doc.count(KIND_TABLE):.0%})\n")

    # 每个空表往前找最近的 heading，作为"这是什么表"的线索
    ctx = []
    for i in empties:
        head = ""
        for j in range(i - 1, max(-1, i - 25), -1):
            b = doc.blocks[j]
            if b.kind in ("heading",) and b.text.strip():
                head = b.text.strip()
                break
        ctx.append((doc.blocks[i].page, head, doc.blocks[i].img_path))

    from collections import Counter
    print("  空表所属章节（最近标题）频次 top15：")
    for h, n in Counter(h for _, h, _ in ctx).most_common(15):
        mark = "  ⚠️ 关键" if any(k in h for k in KEY) else ""
        print(f"    {n:>4}  {h[:70]}{mark}")

    has_img = sum(1 for _, _, im in ctx if im)
    print(f"\n  其中带切图的: {has_img}/{len(ctx)}  ← 有图就有二次处理的机会")

    # 空表按页分布
    pgs = [p for p, _, _ in ctx]
    print(f"  空表页码: {sorted(set(pgs))}")

    # 对照：非空表里，有没有覆盖关键财务表
    print(f"\n  加对照 —— 非空表格里这些关键词出现次数：")
    for k in KEY:
        n = sum(1 for b in doc.blocks if b.kind == KIND_TABLE
                and parse_html_table(b.html)
                and k in b.html)
        print(f"    {k:<12} {n}")
