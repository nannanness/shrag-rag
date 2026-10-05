# -*- coding: utf-8 -*-
"""阶段 2：抽取结构化财务指标，产出「公司 × 指标 × 年份」宽表。

用法::

    python scripts/03_metrics.py                    # 全部已解析的
    python scripts/03_metrics.py --codes 600108

产出::

    data/metrics/metrics_long.csv   长表：code,name,metric,year,value,label,page,source
    data/metrics/metrics_wide.csv   宽表：一行一家公司，列出各指标（默认取最新年）
    data/metrics/extract_report.csv 逐份的抽取情况
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from shrag_rag.extract import METRIC_ORDER, extract_company  # noqa: E402
from shrag_rag.parse import ParsedDoc                          # noqa: E402

PARSED = ROOT / "data" / "parsed"
OUT = ROOT / "data" / "metrics"

LONG_FIELDS = ["code", "name", "metric", "year", "value", "label", "page", "source", "note"]
REPORT_FIELDS = ["code", "name", "report_year", "pages", "metrics", "years",
                 "from_summary", "from_statement", "missing"]


def main() -> int:
    ap = argparse.ArgumentParser(description="抽取结构化财务指标")
    ap.add_argument("--codes", type=str, default=None)
    ap.add_argument("--latest-year", type=int, default=0,
                    help="宽表按该年取数（0 = 自动取每家公司最大年）")
    args = ap.parse_args()

    want = {c.strip() for c in args.codes.split(",")} if args.codes else None
    dirs = sorted(d for d in PARSED.iterdir()
                  if d.is_dir() and (d / "doc.json").exists()
                  and (want is None or d.name in want))
    if not dirs:
        print("没有已解析的 doc.json")
        return 1

    OUT.mkdir(parents=True, exist_ok=True)
    long_rows: list[dict] = []
    report_rows: list[dict] = []
    cover: Counter = Counter()
    print(f"抽取 {len(dirs)} 份\n")

    for d in dirs:
        doc = ParsedDoc.load(d / "doc.json")
        mvs = extract_company(doc)
        by_metric: dict[str, list] = defaultdict(list)
        for mv in mvs:
            by_metric[mv.metric].append(mv)
            long_rows.append({
                "code": mv.code, "name": mv.name, "metric": mv.metric,
                "year": mv.year, "value": f"{mv.value:.4f}".rstrip("0").rstrip("."),
                "label": mv.label, "page": mv.page, "source": mv.source,
                "note": mv.note,
            })

        ry = max((mv.year for mv in mvs if mv.year), default=0)
        found = [m for m in METRIC_ORDER if by_metric.get(m)]
        missing = [m for m in METRIC_ORDER if not by_metric.get(m)]
        for m in found:
            cover[m] += 1

        report_rows.append({
            "code": doc.code, "name": doc.name, "report_year": ry,
            "pages": doc.n_pages, "metrics": len(found),
            "years": ",".join(str(y) for y in sorted({mv.year for mv in mvs if mv.year})),
            "from_summary": sum(1 for mv in mvs if mv.source == "summary"),
            "from_statement": sum(1 for mv in mvs if mv.source == "statement"),
            "missing": ",".join(missing),
        })

    # ---- 写长表 ---------------------------------------------------------- #
    long_rows.sort(key=lambda r: (r["code"], r["metric"], -int(r["year"])))
    with open(OUT / "metrics_long.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LONG_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(long_rows)

    # ---- 写宽表 ---------------------------------------------------------- #
    wide_fields = ["code", "name", "year"] + METRIC_ORDER
    wide: dict[str, dict] = {}
    for r in long_rows:
        wide.setdefault(r["code"], {"code": r["code"], "name": r["name"]})
    # 按 (code, metric) 选最优：summary 优先，其次年份大
    pick: dict[tuple[str, str], dict] = {}
    for r in long_rows:
        k = (r["code"], r["metric"])
        cur = pick.get(k)
        if cur is None:
            pick[k] = r
            continue
        better = ((r["source"] == "summary" and cur["source"] != "summary")
                  or (r["source"] == cur["source"] and int(r["year"]) > int(cur["year"])))
        if better:
            pick[k] = r
    for (code, metric), r in pick.items():
        wide[code][metric] = r["value"]
        wide[code]["year"] = max(int(wide[code].get("year") or 0), int(r["year"]))
    with open(OUT / "metrics_wide.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=wide_fields, extrasaction="ignore")
        w.writeheader()
        for code in sorted(wide):
            w.writerow(wide[code])

    with open(OUT / "extract_report.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=REPORT_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(report_rows)

    # ---- 汇总 ------------------------------------------------------------ #
    print("=" * 84)
    print("指标覆盖度（按公司计）")
    print("=" * 84)
    n = len(dirs)
    for m in METRIC_ORDER:
        c = cover[m]
        print(f"  {m:<20} {c:>3}/{n}  {'█' * int(c / max(n, 1) * 34)}")

    bad = [r for r in report_rows if int(r["metrics"]) < len(METRIC_ORDER)]
    print(f"\n  共 {len(long_rows)} 个观测值；"
          f"完整命中 {n - len(bad)}/{n} 份")
    if bad:
        print(f"  不完整的（{len(bad)} 份）：")
        for r in bad[:15]:
            print(f"    {r['code']} {r['name']:<8} {r['metrics']:>2}/{len(METRIC_ORDER)}"
                  f"  缺: {r['missing'][:70]}")
    print(f"\n  长表   {OUT / 'metrics_long.csv'}")
    print(f"  宽表   {OUT / 'metrics_wide.csv'}")
    print(f"  报告   {OUT / 'extract_report.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
