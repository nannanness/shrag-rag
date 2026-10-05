# -*- coding: utf-8 -*-
"""阶段 6：带引用的问答。

用法::

    python scripts/09_ask.py -q "白云机场2025年的营业收入是多少"
    python scripts/09_ask.py                 # 跑内置问题集
    python scripts/09_ask.py --evidence      # 同时打印检索到的证据
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from shrag_rag.answer import DashScopeLLM, RagQA           # noqa: E402
from shrag_rag.retrieve import HybridRetriever             # noqa: E402

INDEX_DIR = ROOT / "data" / "index"
CHUNKS = ROOT / "data" / "chunks" / "chunks.jsonl"
METRICS = ROOT / "data" / "metrics" / "metrics_long.csv"
LOGS = ROOT / "logs"

# 内置问题集：覆盖 数值 / 定性 / 事实 / 跨公司 / **该拒答的**
DEMO = [
    ("数值", "白云机场2025年的营业收入是多少"),
    ("数值", "华能国际2025年的经营活动产生的现金流量净额是多少"),
    ("数值", "浦发银行2025年归属于母公司股东的净利润是多少"),
    ("数值", "华夏银行2025年末的总资产是多少"),
    ("定性", "皖通高速的主要业务是什么"),
    ("事实", "华能国际2025年年报的审计机构是哪家"),
    ("事实", "中国国贸的利润分配预案是什么"),
    ("跨公司", "哪家公司的审计机构是立信会计师事务所"),
    # 下面两条是**故意问不出来的**，用来验证会不会硬编
    ("应拒答", "贵州茅台2025年的营业收入是多少"),        # 不在索引里（只索引了前 10 家）
    ("应拒答", "公司2025年的员工持股计划参与人数是多少"),  # 问得太泛，证据不足
]


def setup_logging(v: bool) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.DEBUG if v else logging.WARNING,
                        format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S", stream=sys.stdout)


def main() -> int:
    ap = argparse.ArgumentParser(description="带引用的年报问答")
    ap.add_argument("-q", "--question", type=str, default=None)
    ap.add_argument("--topk", type=int, default=5)
    ap.add_argument("--model", type=str, default=None)
    ap.add_argument("--evidence", action="store_true", help="打印检索到的证据")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    setup_logging(args.verbose)
    if not INDEX_DIR.exists():
        print("没有索引，先跑 06_index.py")
        return 1

    retr = HybridRetriever(INDEX_DIR, CHUNKS)
    llm = DashScopeLLM(model=args.model) if args.model else DashScopeLLM()
    qa = RagQA(retr, llm, metrics_path=METRICS)
    print(f"索引 {retr.vs.ntotal} 向量 / {len(retr.companies)} 家公司"
          f"；指标表 {len(qa.metrics)} 条；模型 {llm.model}\n")

    items = [("单问", args.question)] if args.question else DEMO
    for tag, q in items:
        print("=" * 96)
        print(f"【{tag}】{q}")
        print("=" * 96)
        ans = qa.ask(q, topk=args.topk)
        if ans.intent:
            print(f"  意图: {ans.intent.describe()}")
        if ans.facts:
            print(f"  结构化数据: " + "; ".join(
                f"{f['metric']}{f['year']}={f['value']:,.2f}" for f in ans.facts))
        print(f"\n{ans.render()}\n")
        if args.evidence:
            print("  --- 检索到的证据 ---")
            for i, h in enumerate(ans.evidence, 1):
                print(f"  [{i}] {h.brief(78)}")
            print()

    print("=" * 96)
    print(f"本次共 {llm.total_in} 输入 tokens / {llm.total_out} 输出 tokens")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
