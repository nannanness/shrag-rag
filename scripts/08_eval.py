# -*- coding: utf-8 -*-
"""检索评估：**用指标表自动生成带标准答案的问题**，避免在 10 条手写用例上过拟合。

思路：阶段 2 已经从「主要会计数据」表抽出了 `metrics_wide.csv`（公司 × 指标 × 值）。
把它反过来用 —— 每个「公司 + 指标 + 正确的值」自动构成一道题，
标准答案就是这个值。检索时若 top-1 的 chunk 正文里含这个值，就算命中。

这样能得到**上百条**用例，覆盖面远超手写的 10 条，
而且答案不是我编的，是从年报里抽出来的实测值。
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from shrag_rag.retrieve import HybridRetriever      # noqa: E402
from shrag_rag.spec import METRICS                  # noqa: E402

INDEX_DIR = ROOT / "data" / "index"
CHUNKS = ROOT / "data" / "chunks" / "chunks.jsonl"
WIDE = ROOT / "data" / "metrics" / "metrics_wide.csv"

# 不参与评估的指标：口径变体太多，或不是"一个数"
SKIP = {"扣非加权平均净资产收益率"}

# (指标, 中文问法模板)
TEMPLATES = {
    "营业收入": "{co}2025年的营业收入是多少",
    "利润总额": "{co}2025年的利润总额是多少",
    "净利润": "{co}2025年的净利润是多少",
    "归母净利润": "{co}2025年归属于母公司股东的净利润是多少",
    "扣非归母净利润": "{co}2025年扣除非经常性损益后的归母净利润是多少",
    "经营活动现金流量净额": "{co}2025年经营活动产生的现金流量净额是多少",
    "归母净资产": "{co}2025年末归属于上市公司股东的净资产是多少",
    "总资产": "{co}2025年末的总资产是多少",
    "基本每股收益": "{co}2025年的基本每股收益是多少",
    "稀释每股收益": "{co}2025年的稀释每股收益是多少",
    "加权平均净资产收益率": "{co}2025年的加权平均净资产收益率是多少",
}


def fmt(v: float) -> str:
    """展示用（带千分位）。"""
    return f"{v:,.2f}"


_NUM_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def contains_value(body: str, value: float, rel: float = 1e-6) -> bool:
    """正文里是否出现了这个数值。

    **必须按数值比而不是字符串比**。初版直接做 ``expect in body``，
    结果浦发银行全线"未命中" —— 它以**百万元**为单位，年报里写 ``173,964``，
    而期望串是 ``173,964.00``，差一个 ``.00`` 就判错。
    那是评测的 bug，不是检索的 bug（正确 chunk 其实排在第 1）。
    """
    for m in _NUM_RE.finditer(body or ""):
        try:
            v = float(m.group().replace(",", ""))
        except ValueError:
            continue
        if value == 0:
            if abs(v) < 1e-9:
                return True
        elif abs(v - value) <= max(abs(value) * rel, 0.01):
            return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="自动生成用例并评估检索")
    ap.add_argument("--topk", type=int, default=5)
    ap.add_argument("--show-fail", type=int, default=12)
    ap.add_argument("--metric", type=str, default=None, help="只评估某个指标")
    args = ap.parse_args()

    rows = list(csv.DictReader(open(WIDE, encoding="utf-8-sig")))
    retr = HybridRetriever(INDEX_DIR, CHUNKS)
    indexed = set(retr.companies)
    rows = [r for r in rows if r["code"] in indexed]
    print(f"索引 {retr.vs.ntotal} 向量 / {len(indexed)} 家公司；"
          f"指标表 {len(rows)} 行\n")

    cases = []
    for r in rows:
        for metric in METRICS:
            if metric in SKIP or (args.metric and metric != args.metric):
                continue
            raw = (r.get(metric) or "").strip()
            if not raw:
                continue
            try:
                val = float(raw)
            except ValueError:
                continue
            cases.append({
                "code": r["code"], "co": r["name"], "metric": metric,
                "value": val, "expect": fmt(val),
                "question": TEMPLATES[metric].format(co=r["name"]),
            })

    # 批量预嵌入：逐题调接口会串行上千次请求，慢到不可用
    print(f"批量嵌入 {len(cases)} 个问题…")
    qvecs = retr.embedder.embed_texts([c["question"] for c in cases])
    print(f"  完成（{retr.embedder.total_tokens} tokens）\n")
    print(f"自动生成 {len(cases)} 道题（标准答案取自 metrics_wide.csv）\n")
    print("=" * 96)

    n1 = n5 = 0
    fails = []
    by_metric: dict[str, list[int]] = {}
    for qi, c in enumerate(cases):
        hits, intent = retr.retrieve(c["question"], topk=args.topk, qvec=qvecs[qi])
        rank = next((i for i, h in enumerate(hits, 1)
                     if contains_value(h.chunk.get("body") or "", c["value"])), None)
        by_metric.setdefault(c["metric"], []).append(1 if rank else 0)
        if rank == 1:
            n1 += 1
        if rank:
            n5 += 1
        else:
            fails.append((c, hits[:2], intent))

    n = len(cases)
    print(f"  top-1 命中  {n1}/{n}  ({n1/n:.1%})")
    print(f"  top-{args.topk} 命中  {n5}/{n}  ({n5/n:.1%})")
    print("=" * 96)

    print("\n按指标：")
    for m, arr in sorted(by_metric.items(), key=lambda x: -sum(x[1]) / len(x[1])):
        hit = sum(arr)
        print(f"  {m:<22} {hit:>3}/{len(arr):<3} {hit/len(arr):>6.1%}  "
              f"{'█' * int(hit / len(arr) * 28)}")

    if fails:
        print(f"\n未命中的 {len(fails)} 条（前 {args.show_fail} 条）：")
        for c, top, intent in fails[:args.show_fail]:
            print(f"\n  ✗ {c['question']}")
            print(f"    期望值 {c['expect']}   意图: {intent.describe()}")
            for h in top:
                print(f"      {h.brief(78)}")
    return 0 if n5 == n else 2


if __name__ == "__main__":
    raise SystemExit(main())
