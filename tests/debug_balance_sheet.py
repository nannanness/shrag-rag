# -*- coding: utf-8 -*-
"""关键验证：合并资产负债表/利润表 这些核心财报，MinerU 到底给了什么。

这决定 MinerU 方案能不能用 —— 如果三大报表全丢，结构化指标层就无从谈起。
"""
import io
import json
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from shrag_rag.parse import ParsedDoc, parse_html_table  # noqa: E402

code = "600108"
doc = ParsedDoc.load(ROOT / "data" / "parsed" / code / "doc.json")

print("=" * 96)
print(f"[{code} {doc.name}] 三大报表附近的块")
print("=" * 96)

targets = ["合并资产负债表", "母公司资产负债表", "合并利润表", "合并现金流量表",
           "合并所有者权益变动表"]
for t in targets:
    idxs = [i for i, b in enumerate(doc.blocks)
            if b.kind == "heading" and t in b.text]
    print(f"\n{'─' * 92}\n### 「{t}」出现 {len(idxs)} 次（标题块下标 {idxs}）")
    for i in idxs[:1]:
        for b in doc.blocks[i:i + 8]:
            body = b.text if b.kind != "table" else b.html
            grid = parse_html_table(b.html) if b.kind == "table" else None
            extra = f" [网格 {len(grid)}行 x {max((len(r) for r in grid), default=0)}列]" if grid is not None else ""
            print(f"  p{b.page:>4} {b.kind:<8}{extra} img={bool(b.img_path)}")
            if b.kind == "table":
                if grid:
                    for r in grid[:4]:
                        print(f"        | {' | '.join(c[:16] for c in r[:6])}")
                    print(f"        ...共 {len(grid)} 行")
                else:
                    print(f"        ⚠️ 空！html 长度={len(b.html)}  "
                          f"img_path={b.img_path}")
            else:
                print(f"        {body[:90]}")

# 直接看原始 content_list.json 里这些空表的 img_path
print("\n" + "=" * 96)
print("原始 content_list.json：table_body 为空的表格，有没有 img_path？")
print("=" * 96)
raw_dir = ROOT / "data" / "parsed" / code / "raw"
cl = next(p for p in raw_dir.glob("*_content_list.json") if not p.name.endswith("_v2.json"))
data = json.loads(cl.read_text(encoding="utf-8"))
tabs = [b for b in data if b.get("type") == "table"]
empty = [b for b in tabs if not (b.get("table_body") or "").strip()]
print(f"  content_list 里 table 块 {len(tabs)} 个，table_body 为空 {len(empty)} 个")
print(f"  空表里带 img_path 的: {sum(1 for b in empty if b.get('img_path'))}/{len(empty)}")
print(f"  空表里带 table_caption 的: {sum(1 for b in empty if b.get('table_caption'))}/{len(empty)}")
print(f"  空表里带 table_footnote 的: {sum(1 for b in empty if b.get('table_footnote'))}/{len(empty)}")
for b in empty[:5]:
    print(f"\n  page_idx={b.get('page_idx')} img_path={b.get('img_path')}")
    print(f"    caption={b.get('table_caption')} footnote={b.get('table_footnote')}")
    print(f"    keys={sorted(b.keys())}")
    print(f"    bbox={b.get('bbox')}")
