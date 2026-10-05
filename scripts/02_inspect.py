# -*- coding: utf-8 -*-
"""阶段 1.5：解析质量验证（内容驱动）。

用法::

    python scripts/02_inspect.py                     # 全部已解析的
    python scripts/02_inspect.py --codes 600000

**验证什么**（都是"能不能用"级别的硬指标，不是风格问题）：

1. **关键财务指标可抽取性** —— 营收/净利/总资产/负债/经营现金流/归母净资产/EPS
   能否从表格里按行标签抽出来。抽不到 = 阶段 2 无从谈起。
2. **跨表数值一致性** —— 同一指标在两张不同页的表里抽到且数值相同。
   这是"两处独立抽对"的证据。
3. **红旗（分级）** —— high/mid/info，仅用于缩小人工检查范围。
   ``info`` 多为噪音（空表格其实是跨页表的重复检出）。

产出::

    data/parsed/<code>/doc.md / doc.html   人眼比对（直接打开 doc.html 和 PDF 并排看）
    data/meta/quality_report.csv           逐份指标
    data/meta/metrics_preview.csv          抽出来的指标值（阶段 2 的原型）
    logs/inspect.txt                       明细
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from shrag_rag.parse import (KEY_METRICS, ParsedDoc, check_report,  # noqa: E402
                             extract_metrics, severity_counts)
from shrag_rag.parse.export import doc_to_html, doc_to_markdown    # noqa: E402
from shrag_rag.parse.verify import find_suspicious                 # noqa: E402

PARSED = ROOT / "data" / "parsed"
META = ROOT / "data" / "meta"
LOGS = ROOT / "logs"

FIELDS = ["code", "name", "pages", "blocks", "tables", "chars", "chars_per_page",
          "metrics_found", "metrics_total", "cross_checked", "cross_match",
          "high", "mid", "info", "verdict", "problems"]
METRIC_FIELDS = ["code", "name", "metric", "value", "page", "label", "n_hits"]


def main() -> int:
    ap = argparse.ArgumentParser(description="验证解析质量")
    ap.add_argument("--codes", type=str, default=None)
    ap.add_argument("--no-export", action="store_true")
    args = ap.parse_args()

    LOGS.mkdir(parents=True, exist_ok=True)
    want = {c.strip() for c in args.codes.split(",")} if args.codes else None
    dirs = sorted(d for d in PARSED.iterdir()
                  if d.is_dir() and (d / "doc.json").exists()
                  and (want is None or d.name in want))
    if not dirs:
        print("没有已解析的 doc.json")
        return 1

    rows: list[dict] = []
    metric_rows: list[dict] = []
    detail: list[str] = []
    print(f"验证 {len(dirs)} 份\n")

    for d in dirs:
        doc = ParsedDoc.load(d / "doc.json")
        st = doc.stats()
        rep = check_report(doc)
        flags = find_suspicious(doc)
        sc = severity_counts(flags)

        if rep["ok"]:
            verdict = "PASS"
        elif rep["n_found"] >= len(KEY_METRICS) - 2:
            verdict = "WARN"
        else:
            verdict = "FAIL"

        rows.append({
            "code": doc.code, "name": doc.name, "pages": st["n_pages"],
            "blocks": st["n_blocks"], "tables": st["n_table"], "chars": st["chars"],
            "chars_per_page": round(st["chars"] / max(st["n_pages"], 1)),
            "metrics_found": rep["n_found"], "metrics_total": rep["n_total"],
            "cross_checked": len(rep["cross"]),
            "cross_match": sum(1 for c in rep["cross"] if c["match"]),
            "high": sc["high"], "mid": sc["mid"], "info": sc["info"],
            "verdict": verdict, "problems": " | ".join(rep["problems"])[:230],
        })

        hits = extract_metrics(doc)
        for name, _ in KEY_METRICS:
            hs = hits[name]
            metric_rows.append({
                "code": doc.code, "name": doc.name, "metric": name,
                "value": (f"{hs[0].first:,.2f}" if hs and hs[0].first is not None else ""),
                "page": hs[0].page if hs else "",
                "label": hs[0].label if hs else "",
                "n_hits": len(hs),
            })

        if not args.no_export:
            (d / "doc.md").write_text(doc_to_markdown(doc), encoding="utf-8")
            (d / "doc.html").write_text(doc_to_html(doc), encoding="utf-8")

        detail.append(f"\n{'=' * 92}\n{doc.code} {doc.name}  [{verdict}]  "
                      f"{st['n_pages']}页 {st['n_table']}表\n{'=' * 92}")
        detail.append("  关键指标抽取:")
        for name, v in rep["metrics"].items():
            if v["page"] is not None:
                detail.append(f"    ✅ {name:<14} {v['value']:>18,.2f}  "
                              f"(p{v['page']}, {v['n']} 处命中, 标签「{v['label']}」)")
            else:
                detail.append(f"    ❌ {name:<14} 未找到")
        if rep["cross"]:
            detail.append("  跨表数值比对:")
            for c in rep["cross"]:
                detail.append(f"    {'✅' if c['match'] else '❌'} {c['metric']}: "
                              f"p{c['page_a']}={c['val_a']:,.2f} vs "
                              f"p{c['page_b']}={c['val_b']:,.2f}")
        if rep["problems"]:
            detail.append("  问题:")
            for p in rep["problems"]:
                detail.append(f"    - {p}")
        detail.append(f"  红旗: high={sc['high']} mid={sc['mid']} info={sc['info']}")
        for f in flags:
            if f["severity"] != "info":
                detail.append(f"    [{f['severity']:<4}] p{f['page']:>4} {f['why']}"
                              f"  {f.get('detail', '')[:60]}")

    rows.sort(key=lambda r: ({"FAIL": 0, "WARN": 1, "PASS": 2}[r["verdict"]],
                             -r["high"], r["code"]))
    META.mkdir(parents=True, exist_ok=True)
    for path, fields, data in ((META / "quality_report.csv", FIELDS, rows),
                               (META / "metrics_preview.csv", METRIC_FIELDS, metric_rows)):
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            w.writerows(data)
    (LOGS / "inspect.txt").write_text("\n".join(detail), encoding="utf-8")

    # ---- 汇总 ------------------------------------------------------------ #
    vc = Counter(r["verdict"] for r in rows)
    print("=" * 106)
    print("验证结果")
    print("=" * 106)
    print(f"  {vc['PASS']} PASS / {vc['WARN']} WARN / {vc['FAIL']} FAIL\n")
    print(f"  {'代码':<8}{'公司':<10}{'页':>5}{'表':>5}{'字符/页':>8}"
          f"{'指标':>7}{'跨表比对':>10}{'high':>6}{'mid':>5}{'info':>6}  判定")
    print("  " + "-" * 102)
    for r in rows:
        print(f"  {r['code']:<8}{r['name'][:8]:<10}{r['pages']:>5}{r['tables']:>5}"
              f"{r['chars_per_page']:>8}"
              f"{r['metrics_found']:>4}/{r['metrics_total']:<3}"
              f"{r['cross_match']:>6}/{r['cross_checked']:<4}"
              f"{r['high']:>6}{r['mid']:>5}{r['info']:>6}  {r['verdict']}")

    print(f"\n  各指标命中率（{len(rows)} 份）：")
    cnt: Counter = Counter(m["metric"] for m in metric_rows if m["value"] != "")
    for name, _ in KEY_METRICS:
        n = cnt[name]
        print(f"    {name:<16} {n:>3}/{len(rows)}  {'█' * int(n / max(len(rows), 1) * 28)}")

    bad = [r for r in rows if r["verdict"] != "PASS"]
    if bad:
        print(f"\n  需要看的（{len(bad)} 份）：")
        for r in bad:
            print(f"    {r['code']} {r['name']}  [{r['verdict']}] "
                  f"指标 {r['metrics_found']}/{r['metrics_total']}  {r['problems'][:130]}")

    print(f"\n  明细       {LOGS / 'inspect.txt'}")
    print(f"  质量表     {META / 'quality_report.csv'}")
    print(f"  指标预览   {META / 'metrics_preview.csv'}")
    print(f"  人眼比对   data/parsed/<code>/doc.html")
    return 0 if not bad else 2


if __name__ == "__main__":
    raise SystemExit(main())
