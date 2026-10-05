# -*- coding: utf-8 -*-
"""阶段 5：检索与重排 —— 用评估集跑分。

用法::

    python scripts/07_search.py                 # 跑评估集，出命中率
    python scripts/07_search.py -q "白云机场2025年的营业收入是多少"   # 单问
    python scripts/07_search.py --compare       # 对比消融（纯向量 / +BM25 / +重排）
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from shrag_rag.retrieve import HybridRetriever                  # noqa: E402
from shrag_rag.retrieve.query import analyze                    # noqa: E402
from shrag_rag.retrieve.rerank import score_adjust              # noqa: E402

INDEX_DIR = ROOT / "data" / "index"
CHUNKS = ROOT / "data" / "chunks" / "chunks.jsonl"
EVAL = ROOT / "configs" / "retrieval_eval.jsonl"
LOGS = ROOT / "logs"


def setup_logging(v: bool) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.DEBUG if v else logging.WARNING,
                        format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S", stream=sys.stdout)


def load_eval() -> list[dict]:
    rows = []
    with open(EVAL, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def hit_rank(hits, expect: str) -> int | None:
    for r, h in enumerate(hits, 1):
        if expect in (h.chunk.get("body") or ""):
            return r
    return None


def run_eval(retr: HybridRetriever, cases: list[dict], topk: int = 10,
             mode: str = "full", quiet: bool = False) -> dict:
    """mode: full | vec_only | no_rerank"""
    n1 = n5 = n10 = 0
    details = []
    for case in cases:
        q = case["question"]
        expect = case.get("expect") or ""
        if mode == "vec_only":
            intent = analyze(q, retr.companies)
            import numpy as np
            from shrag_rag.retrieve.query import QueryIntent  # noqa: F401
            qv = retr.embedder.embed_texts([q])[0]
            allowed = None
            if intent.code:
                allowed = {cid for cid in retr.ids
                           if retr.chunks[cid]["code"] == intent.code}
            raw = retr.vs.search(qv, topk=topk, allowed=allowed)
            from shrag_rag.retrieve.hybrid import Hit
            hits = [Hit(chunk_id=c, final=s, chunk=retr.chunks[c]) for c, s in raw]
        else:
            hits, intent = retr.retrieve(q, topk=topk)
            if mode == "no_rerank":
                for h in hits:
                    h.final = h.vec
                hits.sort(key=lambda h: -h.final)

        r = hit_rank(hits, expect) if expect else None
        if r:
            n1 += r == 1
            n5 += r <= 5
            n10 += r <= 10
        details.append((case["id"], q, expect, r, hits[:3]))

    n = len(cases)
    res = {"mode": mode, "n": n, "top1": n1, "top5": n5, "top10": n10}
    if not quiet:
        for cid, q, expect, r, top in details:
            mark = "✅" if r == 1 else ("○" if r and r <= 5 else "❌")
            print(f"  {mark} {cid}  排名={r}  {q[:44]}")
            if r != 1:
                for h in top[:2]:
                    print(f"        {h.brief(72)}")
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description="检索与重排评估")
    ap.add_argument("-q", "--question", type=str, default=None)
    ap.add_argument("--topk", type=int, default=10)
    ap.add_argument("--compare", action="store_true", help="消融对比")
    ap.add_argument("--w-bm25", type=float, default=0.3)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    setup_logging(args.verbose)
    if not INDEX_DIR.exists():
        print("没有索引，先跑 06_index.py")
        return 1

    retr = HybridRetriever(INDEX_DIR, CHUNKS, w_bm25=args.w_bm25)
    print(f"索引 {retr.vs.ntotal} 向量 / {len(retr.companies)} 家公司 / "
          f"{len(retr.chunks)} 条元数据\n")

    if args.question:
        hits, intent = retr.retrieve(args.question, topk=args.topk)
        print(f"Q: {args.question}")
        print(f"   意图: {intent.describe()}\n")
        for i, h in enumerate(hits, 1):
            print(f"  {i}. {h.brief()}")
            if h.why:
                print(f"     调整: {'; '.join(h.why)}")
        return 0

    cases = load_eval()
    if args.compare:
        print("=" * 88)
        print("消融对比（同一评估集）")
        print("=" * 88)
        rows = []
        for mode, name in (("vec_only", "纯向量"),
                           ("no_rerank", "向量+BM25（无重排）"),
                           ("full", "完整（+元数据重排）")):
            print(f"\n--- {name} ---")
            rows.append(run_eval(retr, cases, args.topk, mode, quiet=True))
        print(f"\n{'方案':<22}{'top1':>10}{'top5':>10}{'top10':>10}")
        print("-" * 54)
        for r in rows:
            print(f"{r['mode']:<22}{r['top1']:>4}/{r['n']:<5}"
                  f"{r['top5']:>4}/{r['n']:<5}{r['top10']:>4}/{r['n']:<5}")
        return 0

    print("=" * 88)
    print(f"评估集（{len(cases)} 条）")
    print("=" * 88)
    r = run_eval(retr, cases, args.topk, "full")
    print(f"\n  top-1 {r['top1']}/{r['n']}   top-5 {r['top5']}/{r['n']}"
          f"   top-10 {r['top10']}/{r['n']}")
    return 0 if r["top5"] == r["n"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
