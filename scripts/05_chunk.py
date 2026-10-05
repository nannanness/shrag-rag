# -*- coding: utf-8 -*-
"""阶段 3：章节感知切片。

用法::

    python scripts/05_chunk.py                    # 全部 100 份
    python scripts/05_chunk.py --codes 600108     # 抽查某几份
    python scripts/05_chunk.py --show 600108      # 打印某份的切片结果

产出::

    data/chunks/chunks.jsonl    每行一个 chunk（含全部元数据）
    data/chunks/chunk_stats.csv 逐份的切片统计
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from shrag_rag.chunk import chunk_document          # noqa: E402
from shrag_rag.parse import ParsedDoc               # noqa: E402

PARSED = ROOT / "data" / "parsed"
OUT = ROOT / "data" / "chunks"

STAT_FIELDS = ["code", "name", "year", "pages", "chunks", "text_chunks",
               "table_chunks", "chars", "avg_chars", "max_chars",
               "sections", "table_no_metrics"]


def guess_year(pdf_name: str) -> int:
    """文件名里是公告日期（2026-04-30），报告年份 = 公告年 − 1。"""
    import re
    m = re.search(r"_((?:19|20)\d{2})\d{4}_", pdf_name or "")
    return int(m.group(1)) - 1 if m else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="章节感知切片")
    ap.add_argument("--codes", type=str, default=None)
    ap.add_argument("--max-chars", type=int, default=800)
    ap.add_argument("--min-chars", type=int, default=200)
    ap.add_argument("--min-table-chars", type=int, default=20)
    ap.add_argument("--show", type=str, default=None, help="打印某公司的切片明细")
    args = ap.parse_args()

    want = {c.strip() for c in args.codes.split(",")} if args.codes else None
    dirs = sorted(d for d in PARSED.iterdir()
                  if d.is_dir() and (d / "doc.json").exists()
                  and (want is None or d.name in want))
    if not dirs:
        print("没有已解析的 doc.json")
        return 1
    OUT.mkdir(parents=True, exist_ok=True)

    stats: list[dict] = []
    all_chunks: list = []
    print(f"切片 {len(dirs)} 份（max={args.max_chars} min={args.min_chars} "
          f"表长下限={args.min_table_chars}）\n")

    for d in dirs:
        doc = ParsedDoc.load(d / "doc.json")
        year = guess_year(doc.pdf_name)
        chunks = chunk_document(doc, year, max_chars=args.max_chars,
                                min_chars=args.min_chars,
                                min_table_chars=args.min_table_chars)
        all_chunks.extend(chunks)
        tc = [c for c in chunks if c.kind == "text"]
        bc = [c for c in chunks if c.kind == "table"]
        sizes = [c.n_chars for c in chunks] or [0]
        stats.append({
            "code": doc.code, "name": doc.name, "year": year,
            "pages": doc.n_pages, "chunks": len(chunks),
            "text_chunks": len(tc), "table_chunks": len(bc),
            "chars": sum(sizes), "avg_chars": round(sum(sizes) / len(sizes)),
            "max_chars": max(sizes),
            "sections": len({c.section for c in chunks}),
            "table_no_metrics": sum(1 for c in bc if not c.metrics),
        })
        if args.show and doc.code == args.show:
            print(f"===== {doc.code} {doc.name} 共 {len(chunks)} 个 chunk =====")
            for c in chunks[:40]:
                head = c.text[:70].replace("\n", " ")
                print(f"  {c.chunk_id} {c.kind:<5} p{c.page_start:<4} "
                      f"{c.n_chars:>5}字  [{c.section[:34]}]")
                print(f"        {head}")
            print()

    # ---- 写 chunks.jsonl ------------------------------------------------- #
    with open(OUT / "chunks.jsonl", "w", encoding="utf-8") as f:
        for c in all_chunks:
            f.write(json.dumps(c.to_dict(), ensure_ascii=False) + "\n")

    with open(OUT / "chunk_stats.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=STAT_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(stats)

    # ---- 汇总 ------------------------------------------------------------ #
    n = len(all_chunks)
    tc = sum(1 for c in all_chunks if c.kind == "text")
    bc = n - tc
    sizes = [c.n_chars for c in all_chunks] or [0]
    print("=" * 86)
    print("切片汇总")
    print("=" * 86)
    print(f"  总 chunk 数    {n}   （文本 {tc} / 表格 {bc}）")
    print(f"  平均每份       {n / len(dirs):.0f} 个")
    print(f"  字符总数       {sum(sizes):,}")
    print(f"  chunk 大小     中位 {int(statistics.median(sizes))}  "
          f"均值 {sum(sizes) // len(sizes)}  最大 {max(sizes)}")
    print(f"  章节数         合计 {sum(s['sections'] for s in stats)}"
          f"（平均每份 {sum(s['sections'] for s in stats) / len(stats):.0f} 节）")

    q = sorted(sizes)
    for p in (50, 75, 90, 95, 99):
        print(f"    P{p:<3} = {q[min(int(len(q) * p / 100), len(q) - 1)]}")

    # 表格 chunk 的指标关联情况
    no_m = sum(1 for c in all_chunks if c.kind == "table" and not c.metrics)
    with_m = bc - no_m
    print(f"\n  表格 chunk 带指标     {with_m}/{bc}"
          f"（{with_m / max(bc, 1):.0%}）—— 供结构化路由关联用")

    # 章节覆盖
    secs = Counter()
    for c in all_chunks:
        top = (c.section.split(" > ")[0] if c.section else "(无章节)")
        secs[top] += 1
    print(f"\n  按顶层章节分布（top 12）：")
    for s, cnt in secs.most_common(12):
        print(f"    {cnt:>6}  {s[:60]}")

    print(f"\n  chunks.jsonl   {OUT / 'chunks.jsonl'}")
    print(f"  统计表         {OUT / 'chunk_stats.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
