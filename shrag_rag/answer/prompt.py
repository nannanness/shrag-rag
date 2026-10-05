# -*- coding: utf-8 -*-
"""问答 prompt 构造。

## 三个设计决定

1. **证据编号化**：只让模型引用 ``[1]`` ``[2]`` 这种编号，**由代码把编号渲染成
   「公司 年份 页码 章节」**。绝不让模型自己写页码 —— 那必然幻觉。

2. **注入已核实的结构化数据**：阶段 2 已经抽出了权威数值。问数字时直接把
   值（连同出处页码）放进 prompt，模型只需复述+标注出处。这是双通道设计
   在生成端的落点，也是"零幻觉"的来源。

3. **显式口径警告**：年报里「合并 vs 母公司」「归母 vs 全部净利润」
   「年度 vs 分季度」的数字都不同且都真实存在。不提醒的话模型会随手挑一个。
   实测检索层已经因此踩过坑（第 1 名一度是母公司表/分季度表）。
"""

from __future__ import annotations

SYSTEM_PROMPT = """你是一名严谨的上市公司财务分析师，只依据提供的【证据】回答问题。

必须遵守：
1. **每个事实性陈述后标注证据编号**，格式如 [1] 或 [1][3]。没有证据支撑的话不要说。
2. **证据不足就直接说**「提供的材料中没有找到相关信息」。
   不要根据常识推测，也不要用"通常""一般来说"来填补。
3. **数字必须与证据逐字一致**，不要四舍五入、不要换算单位、不要改写量纲。
   引用时保留原文的千分位与单位（元/万元/百万元）。
4. **注意口径**：区分「合并」与「母公司」、「归母净利润」与「净利润」、
   「年度」与「分季度」、「分部」与「整体」。
   若证据里同时存在多个口径，明确说明你采用哪一个。
5. 先给结论，再给依据。简洁，不重复证据原文。"""


def format_evidence(hits: list, max_chars: int = 1200) -> tuple[str, list[dict]]:
    """把检索结果渲染成编号证据，同时返回编号 → 来源的映射。"""
    lines: list[str] = []
    refs: list[dict] = []
    for i, h in enumerate(hits, 1):
        c = h.chunk
        kind = "表格" if c.get("kind") == "table" else "正文"
        pages = (f"p.{c['page_start']}" if c.get("page_start") == c.get("page_end")
                 else f"p.{c.get('page_start')}-{c.get('page_end')}")
        body = (c.get("body") or "")[:max_chars]
        lines.append(
            f"【证据 {i}】({kind}) {c.get('name')}{c.get('year')}年报 {pages} "
            f"§{c.get('section') or '（无章节）'}\n{body}")
        refs.append({"n": i, "chunk_id": h.chunk_id, "code": c.get("code"),
                     "name": c.get("name"), "year": c.get("year"),
                     "page": c.get("page_start"), "page_end": c.get("page_end"),
                     "kind": c.get("kind"), "section": c.get("section"),
                     "score": round(h.final, 4), "section_path": c.get("section")})
    return "\n\n".join(lines), refs


def format_facts(facts: list[dict]) -> str:
    """渲染已核实的结构化数据。"""
    if not facts:
        return ""
    lines = ["【已核实的结构化数据】（取自年报「主要会计数据 / 主要财务指标」表，"
             "可作为权威数值直接使用；**单位已给出，务必照用，不要换算或改写**；"
             "仍需标注出处证据）"]
    for f in facts:
        val = f["value"]
        s = f"{val:,.2f}" if isinstance(val, float) else str(val)
        unit = f.get("unit") or "（原文未标注单位）"
        page = f.get("page") or "?"
        lines.append(f"  · {f['name']}（{f['code']}）{f['metric']} {f['year']}年 = "
                     f"{s} {unit}   [出处：第 {page} 页，{f.get('source') or ''}]")
    return "\n".join(lines)


def format_ranking(ranking: list[dict]) -> str:
    """渲染跨公司排序结果。

    「哪家净利润最高」这类问题**必须**走结构化通道：检索只能返回 5 条证据
    （最多覆盖 5 家公司），而这里覆盖全部已索引的公司。
    """
    if not ranking:
        return ""
    metric = ranking[0]["metric"]
    year = ranking[0]["year"]
    n = ranking[0].get("n_total", len(ranking))
    n_all = ranking[0].get("n_companies_total") or n
    lines = [f"【跨公司排序】{n} 家公司按「{metric}」{year} 年从高到低"
             f"（**已按单位统一换算后比较**）："]
    for f in ranking:
        unit = f.get("unit") or ""
        lines.append(f"  {f['rank']:>2}. {f['name']}（{f['code']}） = "
                     f"{f['value']:,.2f} {unit}   [出处：第 {f.get('page') or '?'} 页]")
    if n < n_all:
        lines.append(
            f"  ⚠️ **覆盖范围**：本次排序只纳入 {n} 家 —— 这些公司的该指标"
            f"取自年报「主要会计数据 / 主要财务指标」标准表，可核实。"
            f"另有 {n_all - n} 家因该指标来自其他表（可靠性不足，实测存在抽错的情况）"
            f"**未纳入**。回答时必须明确告知用户这一局限，"
            f"不要说成'全部 {n_all} 家中最高'。")
    lines.append("  请依据本排序作答，给出第一名及其数值与单位，"
                 "不要自己换算单位。")
    return "\n".join(lines)


def build_messages(question: str, evidence: str, facts_text: str = "") -> list[dict]:
    user = f"""请依据下面的证据回答问题。

问题：{question}

{evidence}
"""
    if facts_text:
        user += f"\n{facts_text}\n"
    user += "\n请作答，并在每个事实后标注证据编号（如 [1]）。"
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user}]


def render_citation(ref: dict) -> str:
    """把证据编号渲染成人类可读的引用（**由代码生成，不经过模型**）。"""
    page = (f"p.{ref['page']}" if ref.get("page") == ref.get("page_end")
            else f"p.{ref.get('page')}-{ref.get('page_end')}")
    sec = (ref.get("section") or "").split(" > ")[-1]
    return (f"[{ref['n']}] {ref.get('name')}{ref.get('year')}年报 {page}"
            f" §{sec}   ({ref['chunk_id']})")
