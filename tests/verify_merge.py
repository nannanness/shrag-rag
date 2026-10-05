# -*- coding: utf-8 -*-
"""验证分片合并的正确性：页码偏移有没有加对、块顺序有没有乱。"""
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent      # tests/ 的上一级 = 项目根
sys.path.insert(0, str(ROOT))
from shrag_rag.parse import ParsedDoc  # noqa: E402

for code, expect_pages in (("600108", 134), ("600004", 215)):
    p = ROOT / "data" / "parsed" / code / "doc.json"
    if not p.exists():
        print(f"[{code}] 没有 doc.json")
        continue
    doc = ParsedDoc.load(p)
    st = doc.stats()
    pages = [b.page_idx for b in doc.blocks]
    print("=" * 88)
    print(f"[{code} {doc.name}]  {doc.pdf_name}")
    print("=" * 88)
    print(f"  块数 {st['n_blocks']}  文本 {st['n_text']}  标题 {st['n_heading']} "
          f"表格 {st['n_table']}  图片 {st['n_image']}  字符 {st['chars']:,}")
    print(f"  page_idx 范围 {min(pages)} ~ {max(pages)}   期望 0 ~ {expect_pages - 1}")
    ok_range = max(pages) == expect_pages - 1 and min(pages) == 0
    print(f"  页码范围正确: {'✅' if ok_range else '❌ 合并偏移有问题！'}")

    # 块顺序应当基本单调（同页内可能交错）
    inversions = sum(1 for a, b in zip(pages, pages[1:]) if b < a - 1)
    print(f"  顺序倒挂次数: {inversions}（同页交错允许，跨页倒挂应为 0）"
          f" {'✅' if inversions == 0 else '❌'}")

    # 每页块数分布，看有没有空洞页
    from collections import Counter
    c = Counter(pages)
    missing = [i for i in range(expect_pages) if i not in c]
    print(f"  无块的页: {len(missing)} 页 {missing[:12]}"
          f"{' ...' if len(missing) > 12 else ''}")

    # 分片边界附近（195 页）的块，检查跨片表格是否被切断
    if expect_pages > 195:
        bnd = [b for b in doc.blocks if 193 <= b.page_idx <= 196]
        print(f"\n  分片边界（第 194~197 页）附近的块共 {len(bnd)} 个：")
        for b in bnd[:10]:
            prev = (b.text or b.html or "")[:60].replace("\n", " ")
            print(f"    p{b.page:>4} {b.kind:<8} {prev}")
        tail = [b for b in doc.blocks if b.page_idx == 194]
        head = [b for b in doc.blocks if b.page_idx == 195]
        tt = [b for b in tail if b.kind == "table"]
        ht = [b for b in head if b.kind == "table"]
        print(f"    → 第195页(片尾)表格 {len(tt)} 个，第196页(片头)表格 {len(ht)} 个")
        if tt and ht:
            print("    ⚠️ 两侧都有表格，需人工确认是否被切断")
