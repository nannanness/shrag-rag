# -*- coding: utf-8 -*-
"""决定性检查：p48 那张 105 行的表，是完整资产负债表，还是只到一半？

如果它包含到「负债和所有者权益总计」这种期末合计行，说明 p49 的"空表"
只是重复检出（spurious），内容并没丢。
"""
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from shrag_rag.parse import ParsedDoc, parse_html_table  # noqa: E402

doc = ParsedDoc.load(ROOT / "data" / "parsed" / "600108" / "doc.json")

for want_page, label in ((48, "合并资产负债表 p48"), (50, "母公司资产负债表 p50")):
    tabs = [b for b in doc.blocks if b.kind == "table" and b.page == want_page
            and parse_html_table(b.html)]
    for b in tabs:
        g = parse_html_table(b.html)
        if len(g) < 20:
            continue
        print("=" * 96)
        print(f"{label}  网格 {len(g)} 行 x {max(len(r) for r in g)} 列")
        print("=" * 96)
        print("  头部 4 行：")
        for r in g[:4]:
            print("    | " + " | ".join(c[:22] for c in r))
        print("  ...")
        print("  尾部 12 行：")
        for r in g[-12:]:
            print("    | " + " | ".join(c[:22] for c in r))
        # 关键合计行是否都在
        flat = "\n".join("|".join(r) for r in g)
        keys = ["流动资产合计", "非流动资产合计", "资产总计",
                "流动负债合计", "非流动负债合计", "负债合计",
                "所有者权益合计", "负债和所有者权益", "负债和股东权益"]
        print("\n  关键合计行命中情况：")
        for k in keys:
            print(f"    {'✅' if k in flat else '❌'} {k}")
        print()
