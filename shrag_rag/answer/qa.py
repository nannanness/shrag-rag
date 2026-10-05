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

    def lookup_facts(self, intent: QueryIntent, max_years: int = 3,
                     min_source: str = "summary") -> list[dict]:
        """问数字时，从指标表取出**高置信**的权威值。

        ## 为什么默认只取 ``source == "summary"``

        抽取层有两条来源：

        - ``summary``：从「主要会计数据 / 主要财务指标」标准表抽的。
          这张表强制标准化披露、行标签全国统一，可靠。
        - ``statement``：汇总表缺该指标时，从正式报表的行标签兜底抽的。
          **这条路出过错** —— 实测：

          | 公司 | 指标表的值 | 年报真实值 |
          | --- | --- | --- |
          | 中国石化 | -3,280,213 百万元 | ``(1,004,962)`` 百万元 |
          | 招商银行 | -259 元 | ``(24,689)`` / ``165,173`` 百万元 |

          原因是报表里同一行标签可能出现多次、或首列不是本期数，
          ``vals[0]`` 就取错了。

        错值被当作「已核实的权威数值」注入 prompt，模型会**自信地说出错数** ——
        这比检索不到更危险。所以宁可少给，不可给错：
        低置信值不进结构化通道，交给证据由模型自己读。
        """
        if not (intent.code and intent.metric):
            return []
        facts = [v for (c, m, y), v in self.metrics.items()
                 if c == intent.code and m == intent.metric
                 and (min_source is None or v.get("source") == min_source)]
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
