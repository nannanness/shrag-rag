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
                     format_ranking, render_citation)

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

    # 单位 → 换算到「元」的乘数。
    # **跨公司排序必须先归一化单位**：实测上汽集团总资产
    # 960,207,461,450（元）与浦发银行 10,081,746（百万元 = 10.08 万亿）
    # 直接比数值，会把浦发排到低位 —— 而它才是最大的。
    _UNIT_MUL = {"元": 1.0, "千元": 1e3, "万元": 1e4, "百万元": 1e6, "亿元": 1e8}

    def lookup_ranking(self, intent: QueryIntent, topn: int = 8,
                       min_source: str = "summary") -> list[dict]:
        """跨公司排序：把该指标下**所有公司**按值排序后取出前 N 名。

        这是当初做结构化指标层的**头号理由** ——
        「哪家净利润最高」跟任何一段文本都不相似，纯向量检索在原理上答不了。
        检索只能返回 5 条证据（最多覆盖 5 家公司），而这里有全部 100 家。

        两个必须做的处理：
        1. **单位归一化**再比较（否则 元 与 百万元 混在一起比，结论会错）
        2. 只取 ``source == summary``（可靠来源）。低置信值可能本身就是错的，
           把它排进榜单会得出错误的第一名。因此会明确告知纳入了多少家。
        """
        if not (intent.metric and intent.wants_ranking):
            return []
        per_code: dict[str, dict] = {}
        for (c, m, y), v in self.metrics.items():
            if m != intent.metric:
                continue
            if intent.year and y != intent.year:
                continue
            if min_source and v.get("source") != min_source:
                continue
            cur = per_code.get(c)
            if cur is None or v["year"] > cur["year"]:
                per_code[c] = v
        if not per_code:
            return []
        for v in per_code.values():
            v["value_base"] = v["value"] * self._UNIT_MUL.get(v.get("unit") or "", 1.0)
        ranked = sorted(per_code.values(), key=lambda x: -x["value_base"])
        out = []
        n_all = len(self.retriever.companies)
        for i, v in enumerate(ranked[:topn], 1):
            out.append({**v, "rank": i, "n_total": len(ranked),
                        "n_companies_total": n_all})
        return out

    # -- 主流程 ------------------------------------------------------------ #

    def ask(self, question: str, topk: int = 5, max_chars: int = 1200) -> Answer:
        hits, intent = self.retriever.retrieve(question, topk=topk)
        if not hits:
            return Answer(question=question,
                          text=f"（{NO_INFO}：检索没有返回任何候选）",
                          intent=intent)

        facts = self.lookup_facts(intent)
        ranking = self.lookup_ranking(intent)
        evidence, refs = format_evidence(hits, max_chars=max_chars)
        facts_text = "\n\n".join(x for x in (format_facts(facts),
                                             format_ranking(ranking)) if x)
        msgs = build_messages(question, evidence, facts_text)
        raw = self.llm.chat(msgs)

        # 解析引用编号 → 只保留真实存在的
        used = sorted({int(n) for n in _CITE_RE.findall(raw) if 1 <= int(n) <= len(refs)})
        by_n = {r["n"]: r for r in refs}
        return Answer(question=question, text=raw.strip(),
                      citations=[by_n[n] for n in used],
                      evidence=hits, intent=intent,
                      facts=facts + ranking, raw=raw)
