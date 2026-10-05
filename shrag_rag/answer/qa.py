# -*- coding: utf-8 -*-
"""问答流水线：检索 → 注入结构化数据 → 生成 → 解析引用。

    问题
     │  HybridRetriever.retrieve()     阶段5：路由 + 向量/BM25 + 重排
     ▼
    top-k 证据 + QueryIntent
     │  lookup_facts()                 ★ 结构化通道：查权威数值
     ▼
    编号证据 + 已核实数据
     │  prompt.build_messages()
     ▼
    LLM 生成（只输出 [1] [2] 这类编号）
     │  代码把编号渲染成「公司 年份 页码 章节」
     ▼
    Answer(text, citations, evidence)

**引用由代码渲染，不经过模型** —— 让模型自己写页码必然幻觉。
"""

from __future__ import annotations

import csv
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..retrieve import HybridRetriever, QueryIntent
from ..retrieve.query import analyze
from .llm import DashScopeLLM
from .prompt import (build_messages, format_evidence, format_facts,
                     render_citation)

log = logging.getLogger(__name__)

_CITE_RE = re.compile(r"\[(\d{1,2})\]")
NO_INFO = "没有找到相关信息"


@dataclass
class Answer:
    question: str
    text: str
    citations: list[dict] = field(default_factory=list)
    evidence: list = field(default_factory=list)
    intent: QueryIntent | None = None
    facts: list[dict] = field(default_factory=list)
    raw: str = ""

    def render(self) -> str:
        out = [self.text]
        if self.citations:
            out.append("\n引用：")
            for c in self.citations:
                out.append("  " + render_citation(c))
        return "\n".join(out)


class RagQA:
    def __init__(self, retriever: HybridRetriever,
                 llm: DashScopeLLM | None = None,
                 metrics_path: Path | None = None):
        self.retriever = retriever
        self.llm = llm or DashScopeLLM()
        self.metrics: dict[tuple[str, str, int], dict] = {}
        if metrics_path and Path(metrics_path).exists():
            # 读**长表**而不是宽表：长表带 page 与 unit，
            # 少了它们模型只能猜单位 —— 实测把「百万元」猜成「万元」，
            # 银行总资产直接差 100 倍。
            with open(metrics_path, encoding="utf-8-sig") as f:
                for r in csv.DictReader(f):
                    try:
                        val = float(r["value"])
                    except (KeyError, ValueError, TypeError):
                        continue
                    try:
                        yr = int(r["year"])
                    except (KeyError, ValueError, TypeError):
                        continue
                    self.metrics[(r["code"], r["metric"], yr)] = {
                        "code": r["code"], "name": r.get("name", ""),
                        "metric": r["metric"], "year": yr, "value": val,
                        "unit": (r.get("unit") or "").strip(),
                        "page": r.get("page") or "",
                        "label": r.get("label") or "",
                        "source": r.get("source") or ""}
            log.info("结构化指标表载入 %d 条", len(self.metrics))

    # -- 结构化通道 -------------------------------------------------------- #

    def lookup_facts(self, intent: QueryIntent, max_years: int = 3) -> list[dict]:
        """问数字时，从指标表取出已核实的权威值。

        这是双通道设计的落点：模型不必从表格文本里"读"数字，直接复述即可。
        """
        if not (intent.code and intent.metric):
            return []
        facts = [v for (c, m, y), v in self.metrics.items()
                 if c == intent.code and m == intent.metric]
        facts.sort(key=lambda f: -f["year"])
        return facts[:max_years]

    # -- 主流程 ------------------------------------------------------------ #

    def ask(self, question: str, topk: int = 5, max_chars: int = 1200) -> Answer:
        hits, intent = self.retriever.retrieve(question, topk=topk)
        if not hits:
            return Answer(question=question,
                          text=f"（{NO_INFO}：检索没有返回任何候选）",
                          intent=intent)

        facts = self.lookup_facts(intent)
        evidence, refs = format_evidence(hits, max_chars=max_chars)
        msgs = build_messages(question, evidence, format_facts(facts))
        raw = self.llm.chat(msgs)

        # 解析引用编号 → 只保留真实存在的
        used = sorted({int(n) for n in _CITE_RE.findall(raw) if 1 <= int(n) <= len(refs)})
        by_n = {r["n"]: r for r in refs}
        return Answer(question=question, text=raw.strip(),
                      citations=[by_n[n] for n in used],
                      evidence=hits, intent=intent, facts=facts, raw=raw)
