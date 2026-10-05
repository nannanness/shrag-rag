# -*- coding: utf-8 -*-
"""追查"空表格"红旗：是 MinerU 真的输出空表，还是我的 HTML 解析器有 bug。"""
import io
import sys
from collections import Counter
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent      # tests/ 的上一级 = 项目根
sys.path.insert(0, str(ROOT))

from shrag_rag.parse import ParsedDoc, KIND_TABLE, parse_html_table  # noqa: E402

for code in ("600108", "600004"):
    p = ROOT / "data" / "parsed" / code / "doc.json"
    doc = ParsedDoc.load(p)
    empties = [b for b in doc.blocks if b.kind == KIND_TABLE and not parse_html_table(b.html)]
    print("=" * 92)
    print(f"[{code} {doc.name}] 表格块 {doc.count(KIND_TABLE)} 个，解析为空 {len(empties)} 个")
    print("=" * 92)
    print("  空表 html 长度分布:", dict(Counter(len(b.html) for b in empties)))
    for b in empties[:6]:
        print(f"\n  --- p{b.page}  html长度={len(b.html)} ---")
        print(f"      {b.html[:400]!r}")
    # 有 html 但没 <tr 的
    no_tr = [b for b in doc.blocks if b.kind == KIND_TABLE and b.html and "<tr" not in b.html.lower()]
    print(f"\n  有 html 但不含 <tr 的: {len(no_tr)}")
    for b in no_tr[:3]:
        print(f"      p{b.page}: {b.html[:200]!r}")
    truly_empty = [b for b in empties if not b.html.strip()]
    print(f"  真正的空串 html: {len(truly_empty)}")
