# -*- coding: utf-8 -*-
"""查：600108 的现金流量表到底在不在？为什么按标题找不到。"""
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from shrag_rag.parse import ParsedDoc, parse_html_table  # noqa: E402

for code in ("600108", "600004"):
    doc = ParsedDoc.load(ROOT / "data" / "parsed" / code / "doc.json")
    print("=" * 94)
    print(f"[{code} {doc.name}]")
    print("=" * 94)

    print("  含「现金流量」的块（任意类型）：")
    for i, b in enumerate(doc.blocks):
        blob = (b.text or "") + (b.html or "")
        if "现金流量" in blob:
            n = len(parse_html_table(b.html)) if b.kind == "table" else 0
            head = (b.text or b.html)[:70].replace("\n", " ")
            print(f"    #{i:<5} p{b.page:>4} {b.kind:<8} 行={n:<4} {head}")

    # 大表（>15 行）的清单，看看有没有一张像现金流量表
    print("\n  行数 >15 的表格一览（页码, 行数, 首行前3列）：")
    for b in doc.blocks:
        if b.kind != "table":
            continue
        g = parse_html_table(b.html)
        if len(g) > 15:
            first = " | ".join(c[:14] for c in g[0][:3])
            print(f"    p{b.page:>4}  {len(g):>4}行 x {max(len(r) for r in g):>2}列   {first}")
