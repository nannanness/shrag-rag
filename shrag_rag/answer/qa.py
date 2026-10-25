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
_NUM_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")
NO_INFO = "没有找到相关信息"

# 单位 → 换算到「元」的乘数。
# 跨公司排序必须先归一化单位：实测上汽集团总资产
# 960,207,461,450（元）与浦发银行 10,081,746（百万元 = 10.08 万亿）
# 直接比数值，会把浦发排到低位 —— 而它才是最大的。
UNIT_MUL = {"元": 1.0, "千元": 1e3, "万元": 1e4, "百万元": 1e6, "亿元": 1e8}


def _body_has_number(body: str, value: float, rel: float = 1e-6) -> bool:
    """正文里是否出现了这个数值（**按数值比，不是字符串比**）。

    字符串比会误判：浦发以百万元为单位，年报写 ``173,964`` 而期望串是
    ``173,964.00``，差一个 ``.00`` 就判错（评测层踩过这个坑）。
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


@dataclass
class Prepared:
    """检索加 prompt 组装的结果，供 ``ask`` 与 ``stream`` 共用。"""
    question: str
    hits: list
    intent: QueryIntent | None
    facts: list[dict]
    ranking: list[dict]
    refs: list[dict]
    msgs: list[dict]


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
                     min_source: str | None = None, corrob_hits: list | None = None) -> list[dict]:
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
        facts = facts[:max_years]

        # ---- 安全闸：statement 来源的值必须与检索到的证据一致才注入 ----
        #
        # 抽取层的兜底路径修好后准确率大幅提升，但它仍然是从**非标准表**
        # 读出来的，没有「主要会计数据」那种强制标准化保证。
        # 所以对这类值再加一道校验：**它必须出现在我们正要给模型看的证据里**。
        # 这样即使抽取又出错，也不会注入一个与证据矛盾的值 ——
        # 模型会自行从证据中读取正确数字。
        if corrob_hits is not None:
            kept = []
            for f in facts:
                if f.get("source") == "summary":
                    kept.append(f)
                    continue
                if any(_body_has_number(h.chunk.get("body") or "", f["value"])
                       for h in corrob_hits):
                    kept.append(f)
                else:
                    log.info("丢弃未获证据佐证的 %s %s：%s %s",
                             f["code"], f["metric"], f["value"], f.get("unit"))
            facts = kept
        return facts

    # 单位换算表在模块级（``UNIT_MUL``），Web 端排序也要用同一份，不重复定义
    _UNIT_MUL = UNIT_MUL

    def lookup_ranking(self, intent: QueryIntent, topn: int = 8,
                       min_source: str = "summary") -> list[dict]:
        """跨公司排序：把该指标下**所有公司**按值排序后取出前 N 名。

        这是当初做结构化指标层的头号理由：
        「哪家净利润最高」跟任何一段文本都不相似，纯向量检索在原理上答不了。
        检索只能返回 5 条证据（最多覆盖 5 家公司），而这里有全部 100 家。

        ## 优先标准表，退而用报表兜底

        默认只收 ``source == summary``。但**有些指标压根没有标准表来源** ——
        净利润就是典型：模板在「归母净利润 = 净利润」时只披露归母那一行，
        净利润只能从正式报表兜底算出来，于是 98 条全都是 ``statement``。
        这种情况下若一律丢弃，榜单会直接空掉、招牌问题答不出来，
        所以退一步用兜底值，同时把「来源可靠性偏低」标出来交给 prompt 声明。
        """
        if not (intent.metric and intent.wants_ranking):
            return []
        rows = self._rank_rows(intent, min_source)
        degraded = False
        if not rows and min_source:
            rows = self._rank_rows(intent, None)
            degraded = bool(rows)
        if not rows:
            return []

        year = intent.year or (rows[0]["year"] if rows else 0)
        rows, excluded = self._drop_implausible(rows, year)
        if not rows:
            return []

        n_all = len(self.retriever.companies)
        out = []
        for i, v in enumerate(rows[:topn], 1):
            out.append({**v, "rank": i, "n_total": len(rows),
                        "n_companies_total": n_all,
                        "n_excluded": excluded,
                        "degraded": degraded})
        return out

    def _rank_rows(self, intent: QueryIntent, min_source: str | None) -> list[dict]:
        """取每家公司该指标的最新一期，按统一单位换算后从高到低排序。"""
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
        for v in per_code.values():
            # 单位必须先折成同一个基准再比，否则「元」与「百万元」混着排会得出错误第一名
            v["value_base"] = v["value"] * self._UNIT_MUL.get(v.get("unit") or "", 1.0)
        return sorted(per_code.values(), key=lambda x: -x["value_base"])

    # 流量类指标：这些值不可能远超同一年的营业收入。
    # 存量类（总资产、净资产）没有这个上界，不能套用。
    _FLOW_METRICS = {"营业收入", "利润总额", "净利润", "归母净利润",
                     "扣非归母净利润", "经营活动现金流量净额"}
    # 营业收入低于 100 万元时认为这个锚点本身就不可信，放弃校验
    _MIN_REVENUE = 1e6

    def _revenue_base(self, code: str, year: int) -> float | None:
        """取某公司某年营业收入（折算成元），优先标准表来源。"""
        best = None
        for src in ("summary", None):
            for (c, m, y), v in self.metrics.items():
                if c != code or m != "营业收入" or y != year:
                    continue
                if src and v.get("source") != src:
                    continue
                mul = self._UNIT_MUL.get(v.get("unit") or "", 1.0)
                cand = v["value"] * mul
                if best is None or (src == "summary" and v.get("source") == "summary"):
                    best = cand
            if best is not None:
                break
        return best

    def _drop_implausible(self, rows: list[dict], year: int) -> tuple[list[dict], int]:
        """按「流量指标不该远超营业收入」剔除明显不可能的值。

        实测踩到的例子：保利发展 2025 年净利润被抽成
        ``5,022,154,432.21 万元``（数值其实是元，单位标错），换算后相当于 50 万亿，
        直接窜到跨公司排序第一名。这种值一旦进入 prompt，模型会照单全收。

        故意做得保守：只有营业收入这个锚点本身看着正常（≥100 万元）时才校验，
        锚点不可信就不下判断 —— 否则会把「营收抽错、净利润正确」的公司也误杀。
        """
        if not rows:
            return rows, 0
        metric = next(iter({r["metric"] for r in rows}), None)
        if metric not in self._FLOW_METRICS or metric == "营业收入":
            return rows, 0

        kept, dropped = [], 0
        for r in rows:
            rev = self._revenue_base(r["code"], year or r["year"])
            if rev is None or abs(rev) < self._MIN_REVENUE:
                kept.append(r)                     # 锚点不可信，不判
            elif abs(r["value_base"]) > 3 * abs(rev):
                dropped += 1
            else:
                kept.append(r)
        return kept, dropped

    # -- 主流程 ------------------------------------------------------------ #

    def prepare(self, question: str, topk: int = 5, max_chars: int = 1200):
        """检索 + 组装 prompt，返回 ``(Prepared | None, QueryIntent)``。

        抽出来是为了让 Web 端能先拿到证据、再流式接答案：
        证据要立刻画到页面上，不必等模型生成完。
        """
        hits, intent = self.retriever.retrieve(question, topk=topk)
        if not hits:
            return None, intent
        facts = self.lookup_facts(intent, corrob_hits=hits)
        ranking = self.lookup_ranking(intent)
        evidence, refs = format_evidence(hits, max_chars=max_chars)
        facts_text = "\n\n".join(x for x in (format_facts(facts),
                                             format_ranking(ranking)) if x)
        msgs = build_messages(question, evidence, facts_text)
        return Prepared(question=question, hits=hits, intent=intent,
                        facts=facts, ranking=ranking, refs=refs, msgs=msgs), intent

    def ask(self, question: str, topk: int = 5, max_chars: int = 1200) -> Answer:
        p, intent = self.prepare(question, topk, max_chars)
        if p is None:
            return Answer(question=question,
                          text=f"（{NO_INFO}：检索没有返回任何候选）",
                          intent=intent)

        raw = self.llm.chat(p.msgs)
        return self._finish(p, raw)

    def stream(self, question: str, topk: int = 5, max_chars: int = 1200):
        """流式问答，逐条 yield ``(event, payload)``。

        事件顺序：``meta``（意图/事实/证据，先画页面）→ ``delta``（答案增量）
        → ``done``（引用与统计）。库这边只吐元组，怎么编码成 SSE 由调用方决定。
        """
        p, intent = self.prepare(question, topk, max_chars)
        if p is None:
            yield "meta", {"intent": intent.describe() if intent else "",
                           "evidence": [], "facts": [], "ranking": []}
            yield "done", {"text": f"（{NO_INFO}：检索没有返回任何候选）",
                           "citations": [], "ok": False}
            return

        yield "meta", {
            "intent": intent.describe() if intent else "",
            "code": intent.code, "company": intent.company,
            "metric": intent.metric, "qtype": intent.qtype,
            "facts": p.facts, "ranking": p.ranking,
            "evidence": [self._evidence_item(i, h)
                         for i, h in enumerate(p.hits, 1)],
        }

        buf: list[str] = []
        try:
            for piece in self.llm.chat_stream(p.msgs):
                buf.append(piece)
                yield "delta", {"t": piece}
        except Exception as e:                            # noqa: BLE001
            log.exception("流式生成中断")
            yield "done", {"text": "".join(buf).strip(), "citations": [],
                           "ok": False, "error": f"{type(e).__name__}: {e}"}
            return

        ans = self._finish(p, "".join(buf))
        yield "done", {"text": ans.text, "citations": ans.citations,
                       "ok": True, "raw_len": len(ans.raw)}

    @staticmethod
    def _evidence_item(n: int, h) -> dict:
        """给前端用的证据卡片：元数据 + 正文片段。"""
        c = h.chunk
        body = (c.get("body") or "").strip()
        return {"n": n, "chunk_id": h.chunk_id, "code": c.get("code"),
                "name": c.get("name"), "year": c.get("year"),
                "kind": c.get("kind"), "page": c.get("page_start"),
                "page_end": c.get("page_end"), "section": c.get("section"),
                "score": round(h.final, 4),
                "snippet": body[:400], "length": len(body)}

    def _finish(self, p: "Prepared", raw: str) -> Answer:
        """把模型输出解析成带引用的 Answer（引用由代码渲染，不经过模型）。"""
        used = sorted({int(n) for n in _CITE_RE.findall(raw)
                       if 1 <= int(n) <= len(p.refs)})
        by_n = {r["n"]: r for r in p.refs}
        return Answer(question=p.question, text=raw.strip(),
                      citations=[by_n[n] for n in used],
                      evidence=p.hits, intent=p.intent,
                      facts=p.facts + p.ranking, raw=raw)
