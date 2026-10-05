# -*- coding: utf-8 -*-
"""混合检索：查询理解 → 候选集路由 → 向量+BM25 → 元数据重排。

## 整体流程

    问题
     │  query.analyze()      认出公司 / 指标 / 题型
     ▼
    候选集路由                公司 → 该公司 chunk
     │                        公司+指标 → 只留含该指标的**表**
     ▼
    向量检索 + BM25           两路各自排序
     │
     ▼
    rerank.score_adjust()     按 section / 页码 / 类型加减分
     │
     ▼
    top-k

## 分数怎么合成（每个权重都能解释）

    final = 1.0 × 向量分(归一化) + 0.3 × BM25分(归一化) + 重排调整量

- 向量分与 BM25 分各自在**候选集内** min-max 归一到 [0,1]，避免量纲问题
  （余弦分在 0.6~0.8 窄区间，BM25 是无界值，直接相加没有意义）
- BM25 权重给 0.3 而不是 0.5：实测它对「白云机场营业收入」这类问题排名
  反而更差（106 vs 80），属于**补充信号**而非主力
- 重排调整量直接相加：在指标预过滤后候选只剩几条时，
  元数据规则的区分力**强于**向量分（向量分彼此只差 0.01~0.05）
"""

from __future__ import annotations

import json
import logging
import pickle
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ..index import DashScopeEmbedder, VectorStore
from .query import QueryIntent, analyze
from .rerank import score_adjust

log = logging.getLogger(__name__)


@dataclass
class Hit:
    chunk_id: str
    final: float
    vec: float = 0.0
    bm25: float = 0.0
    adj: float = 0.0
    why: list[str] = field(default_factory=list)
    chunk: dict = field(default_factory=dict)

    def brief(self, width: int = 88) -> str:
        c = self.chunk
        body = (c.get("body") or "").replace("\n", " ")[:width]
        return (f"{self.final:.4f} (v{self.vec:.3f} b{self.bm25:.2f} a{self.adj:+.2f})  "
                f"{self.chunk_id}  {c.get('kind','?'):<5} p{c.get('page_start','?'):<4} "
                f"[{(c.get('section') or '')[:36]}]\n        {body}")


class HybridRetriever:
    def __init__(self, index_dir: Path, chunks_path: Path,
                 bm25_cache: Path | None = None,
                 w_vec: float = 1.0, w_bm25: float = 0.3,
                 embedder: DashScopeEmbedder | None = None):
        self.index_dir = Path(index_dir)
        self.vs = VectorStore.load(self.index_dir)
        self.chunks: dict[str, dict] = {}
        with open(chunks_path, encoding="utf-8") as f:
            for line in f:
                c = json.loads(line)
                self.chunks[c["chunk_id"]] = c
        # 只保留索引里有的 chunk（全量时 chunks.jsonl 可能包含未索引的公司）
        self.ids = list(self.vs.ids)
        self.companies = {}
        for cid in self.ids:
            c = self.chunks[cid]
            self.companies.setdefault(c["code"], c["name"])
        self.embedder = embedder or DashScopeEmbedder()
        self.w_vec, self.w_bm25 = w_vec, w_bm25
        self.bm25_cache = bm25_cache or (self.index_dir / "bm25_tokens.pkl")
        self._bm25 = None

    # -- BM25（延迟构建 + 落盘缓存）--------------------------------------- #

    def _ensure_bm25(self):
        if self._bm25 is not None:
            return self._bm25
        import jieba
        from rank_bm25 import BM25Okapi
        cache = self.bm25_cache
        if cache.exists():
            toks = pickle.loads(cache.read_bytes())
            if len(toks) == len(self.ids):
                log.info("BM25 分词读自缓存（%d 条）", len(toks))
                self._bm25 = BM25Okapi(toks)
                return self._bm25
        log.info("BM25 分词中（%d 条，首次较慢）…", len(self.ids))
        toks = [list(jieba.cut(self.chunks[cid]["text"])) for cid in self.ids]
        cache.write_bytes(pickle.dumps(toks))
        self._bm25 = BM25Okapi(toks)
        return self._bm25

    def _bm25_scores(self, question: str) -> np.ndarray:
        import jieba
        return np.asarray(self._ensure_bm25().get_scores(list(jieba.cut(question))),
                          dtype="float64")

    # -- 候选集路由 -------------------------------------------------------- #

    def _candidates(self, intent: QueryIntent) -> list[int] | None:
        """返回候选的**行号**列表；None 表示全库。

        这是实测最有效的一步：指标预过滤把候选从数百条缩到个位数，
        正确 chunk 排名 80 → 2。
        """
        if not intent.code and not intent.metric:
            return None
        keep: list[int] = []
        for i, cid in enumerate(self.ids):
            c = self.chunks[cid]
            if intent.code and c["code"] != intent.code:
                continue
            if intent.metric and intent.metric not in (c.get("metrics") or []):
                continue
            keep.append(i)
        return keep

    # -- 检索主入口 -------------------------------------------------------- #

    def retrieve(self, question: str, topk: int = 10,
                 qvec: np.ndarray | None = None,
                 debug: bool = False) -> tuple[list[Hit], QueryIntent]:
        """``qvec`` 可传入预先算好的查询向量。

        评估时要跑上千道题，逐题调嵌入接口会慢到不可用
        （每次一个请求，串行上千次）。调用方可先用 ``embed_texts``
        批量算好所有问题向量再逐题检索。
        """
        intent = analyze(question, self.companies)
        cand = self._candidates(intent)

        if qvec is None:
            qvec = self.embedder.embed_texts([question])[0]
        n_cand = self.vs.ntotal if cand is None else len(cand)
        if n_cand == 0:
            log.warning("候选集为空：%s", intent.describe())
            return [], intent

        # 1) 向量检索（预过滤到候选集）
        allowed = None if cand is None else {self.ids[i] for i in cand}
        vhits = self.vs.search(qvec, topk=n_cand, allowed=allowed)
        vec = {cid: s for cid, s in vhits}

        # 2) BM25（全库算分后按候选集筛）
        bs = self._bm25_scores(question)
        if cand is None:
            pool = list(range(len(self.ids)))
        else:
            pool = cand
        bs_pool = np.array([bs[i] for i in pool], dtype="float64")

        def mm(a: np.ndarray) -> np.ndarray:
            lo, hi = float(a.min()), float(a.max())
            return (a - lo) / (hi - lo) if hi > lo else np.ones_like(a)

        vec_pool = np.array([vec.get(self.ids[i], 0.0) for i in pool], dtype="float64")
        vn, bn = mm(vec_pool), mm(bs_pool)

        # 3) 融合 + 重排
        hits: list[Hit] = []
        for k, i in enumerate(pool):
            cid = self.ids[i]
            c = self.chunks[cid]
            adj, why = score_adjust(c, intent)
            final = self.w_vec * vn[k] + self.w_bm25 * bn[k] + adj
            hits.append(Hit(chunk_id=cid, final=final, vec=float(vec_pool[k]),
                            bm25=float(bs_pool[k]), adj=adj, why=why, chunk=c))
        hits.sort(key=lambda h: -h.final)
        return hits[:topk], intent
