# -*- coding: utf-8 -*-
"""对比实验：pipeline 后端 vs vlm 后端，谁的表格还原更完整。

背景：pipeline 后端在 600108 上有 37/256 个 table_body 为空，且这些空表
连 img_path 都没有（无图可兜底）。空表集中在跨页财报的**续页**上。
vlm 后端可能更稳，值得一试。
"""
import io
import json
import os
import sys
import time
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from shrag_rag.parse import MineruClient, from_mineru_content_list, parse_html_table  # noqa: E402

SRC = Path(r"D:\Project\shrag\data\pdfs\600108\600108_20260430_90X1.pdf")
OUT = ROOT / "data" / "_backend_cmp"


def run(model_version: str) -> dict:
    print(f"\n{'=' * 92}\n后端 = {model_version}\n{'=' * 92}")
    client = MineruClient(model_version=model_version)
    t0 = time.time()
    bid = client.submit_batch([("600108", SRC)])
    res = client.poll_batch(bid, timeout=1800)
    el = time.time() - t0
    fr = res[0]
    print(f"  state={fr.state}  耗时 {el:.0f}s")
    if fr.state != "done":
        return {"backend": model_version, "state": fr.state, "err": fr.err_msg}

    outdir = OUT / model_version
    z = outdir / f"{bid}.zip"
    client.download_zip(fr.zip_url, z)
    client.unzip(z, outdir / "raw", keep_images=False)
    cl = client.find_content_list(outdir / "raw")
    data = json.loads(cl.read_text(encoding="utf-8"))
    doc = from_mineru_content_list(data, code="600108", name="亚盛集团",
                                   report_year=2025, pdf_name=SRC.name,
                                   source_pdf=str(SRC))
    doc.save(outdir / "doc.json")

    tabs = [b for b in doc.blocks if b.kind == "table"]
    empty = [b for b in tabs if not parse_html_table(b.html)]
    big = [b for b in tabs if len(parse_html_table(b.html)) >= 20]
    chars = sum(len(b.text) + len(b.html) for b in doc.blocks)
    return {
        "backend": model_version, "state": "ok", "seconds": round(el),
        "pages": doc.n_pages, "blocks": doc.n_blocks, "tables": len(tabs),
        "empty_tables": len(empty), "empty_pct": round(len(empty) / max(len(tabs), 1), 3),
        "big_tables": len(big), "chars": chars,
        "headings": doc.count("heading"),
        "_doc": doc,
    }


def probe(doc, label):
    """看关键报表在哪个后端里更完整。"""
    print(f"\n  --- {label} 关键报表 ---")
    for key in ("合并资产负债表", "母公司资产负债表", "合并利润表",
                "母公司利润表", "现金流量表", "所有者权益变动表"):
        idxs = [i for i, b in enumerate(doc.blocks)
                if b.kind == "heading" and key in b.text]
        if not idxs:
            print(f"    {key:<16} 标题未找到")
            continue
        # 标题之后到下一个标题之间的表格
        i = idxs[0]
        j = next((k for k in range(i + 1, min(i + 30, len(doc.blocks)))
                  if doc.blocks[k].kind == "heading"), len(doc.blocks))
        seg = doc.blocks[i:j]
        grids = [(b.page, len(parse_html_table(b.html))) for b in seg if b.kind == "table"]
        tot = sum(n for _, n in grids)
        empt = sum(1 for _, n in grids if n == 0)
        print(f"    {key:<16} 标题p{seg[0].page}  表格 {len(grids)} 个 "
              f"(空 {empt})  总行数 {tot}  {grids[:6]}")


if __name__ == "__main__":
    results = []
    for mv in ("pipeline", "vlm"):
        try:
            r = run(mv)
        except Exception as e:                           # noqa: BLE001
            print(f"  ❌ {mv} 失败 {type(e).__name__}: {e}")
            r = {"backend": mv, "state": "error", "err": str(e)}
        results.append(r)

    print("\n" + "=" * 92)
    print("对比结果")
    print("=" * 92)
    keys = ["backend", "state", "seconds", "pages", "blocks", "tables",
            "empty_tables", "empty_pct", "big_tables", "chars", "headings"]
    print("  " + "".join(f"{k:>13}" for k in keys))
    for r in results:
        print("  " + "".join(f"{str(r.get(k, '-')):>13}" for k in keys))

    for r in results:
        if r.get("_doc"):
            probe(r["_doc"], r["backend"])
