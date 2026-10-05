# -*- coding: utf-8 -*-
"""阶段 2：结构化财务指标抽取 —— 公司 × 指标 × 年份。

指标口径与文本工具统一来自 ``shrag_rag/spec.py``（验证层也用同一份，
避免两处定义分叉）。

## 为什么以「主要会计数据 / 主要财务指标」表为主

年报里这两张表是**强制标准化披露**的：
- 行标签全国统一（营业收入、利润总额、归母净利润、总资产…）
- **列就是年份**（2025年 / 2024年 / 2023年），一张表直接给出三年数据
- 还带同比增减列

所以从这里抽，一次就能拿到「一家公司 × 十几个指标 × 三年」，
而且精度远高于满篇扫表格（后者会撞上 MD&A 的分项数据、母公司口径等一堆同名行）。

## 表结构的一个坑

「主要会计数据」表**中间会再出现一次表头**（资产负债表类指标用
`2025年末 / 2024年末 / …`，而利润表类用 `2025年 / 2024年 / …`）。
所以列 → 年份的映射要**边走边更新**，不能只在第一行解析一次。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..parse.htmltable import parse_html_table
from ..parse.schema import KIND_TABLE, ParsedDoc
from ..spec import (METRICS, METRIC_ORDER, is_summary_table,  # noqa: F401
                    is_year_header_cell, match_metric, parse_number)

# 汇总表的表头文字，出现在第 0 列时表示"这是表头行"而不是数据行
HEADER_LABELS = ("主要会计数据", "主要财务指标", "主要财务数据", "项目", "")


@dataclass
class MetricValue:
    code: str
    name: str
    metric: str
    year: int
    value: float
    label: str              # 命中的原始行标签
    page: int
    source: str             # summary（主要会计数据/财务指标） | statement（正式报表）
    note: str = ""
    unit: str = ""          # 金额单位，如「元」「万元」「百万元」


# 「单位：元币种：人民币」「（单位：百万元）」「金额单位均为人民币百万元」
_UNIT_RE = re.compile(r"单位[均为]*[：:]\s*([^，,。；;\n]{0,12})")
_UNIT_TOKENS = ("百万元", "千元", "万元", "亿元", "元")


def normalize_unit(s: str) -> str:
    """从「人民币百万元」这类串里取出规范单位。"""
    t = (s or "").replace("人民币", "").replace("币种", "").strip(" ：:（）()")
    for u in _UNIT_TOKENS:                 # 顺序重要：百万元 必须早于 万元/元
        if u in t:
            return u
    return ""


def declared_unit(text: str, max_len: int = 30) -> str:
    """判断一个短文本块是不是**单位声明**，是则返回单位。

    为什么要放宽：不少年报不写「单位：」，而是写成
    ``（人民币百万元，特别注明除外）`` —— 实测招商银行就是这样，
    只认「单位：」会漏掉它，导致 178,993 被标成「元」（差 100 万倍）。

    误判防线：块必须**很短**，且带「单位/金额」字样、或整块被括号包住。
    纯正文里出现的「人民币1亿元」这类句子不会被当成声明。
    """
    t = (text or "").strip()
    if not t or len(t) > max_len:
        return ""
    signal = ("单位" in t) or ("金额" in t) or (
        t.startswith(("(", "（")) and t.endswith((")", "）")))
    if not signal:
        return ""
    for u in _UNIT_TOKENS:
        if u in t:
            return u
    return ""


def unit_index(doc: ParsedDoc) -> list[tuple[int, str]]:
    """全文档的「单位声明」位置列表 ``[(块下标, 单位)]``。

    为什么需要它：年报的金额单位往往是**文档级声明**，只在某处写一次
    （如「（单位：百万元）」出现在 p23），之后几十页的表都不再重复。
    实测华夏银行的总资产在 p121，而单位声明在 p23 —— 只看表附近是找不到的。
    """
    out: list[tuple[int, str]] = []
    for i, b in enumerate(doc.blocks):
        txt = b.text or ""
        m = _UNIT_RE.search(txt)
        u = normalize_unit(m.group(1)) if m else ""
        if not u:
            u = declared_unit(txt)
        if u:
            out.append((i, u))
    return out


def unit_for(units: list[tuple[int, str]], idx: int,
             fallback: str = "", window: int = 80) -> str:
    """判断该位置适用的金额单位。

    ## 为什么不能简单地"取之前最近一次声明"

    实测浦发银行全篇 43 处声明，其中 38 处是「百万元」，
    但 p104/p106/p127/p128 是「万元」、p139 是「亿元」——
    那些是**局部表格自己的单位**。若一路"粘"下去，
    p322 的总资产会被错配成 p139 的「亿元」，**差 100 倍**。

    所以只在 ``window`` 个块内取最近的声明；超出窗口说明那条声明
    不属于当前上下文，回退到**全文最常见**的单位。
    """
    from collections import Counter
    for pos, val in reversed(units):
        if pos <= idx:
            if idx - pos <= window:
                return val
            break
    if units:
        return Counter(v for _, v in units).most_common(1)[0][0]
    return fallback


@dataclass
class SummaryRow:
    label: str
    values: dict[int, float] = field(default_factory=dict)
    changes: dict[int, str] = field(default_factory=dict)


def parse_summary_table(grid: list[list[str]]) -> list[SummaryRow]:
    """把「主要会计数据 / 主要财务指标」的网格解析成 ``SummaryRow`` 列表。

    列 → 年份的映射**边走边更新**：这张表中间会重新出现一次表头
    （`2025年末 | 2024年末 | …`），只在首行解析一次会把后半段的
    资产负债表类指标全部漏掉或错配年份。
    """
    rows: list[SummaryRow] = []
    year_of_col: dict[int, int] = {}

    for r_i, row in enumerate(grid):
        if not row:
            continue
        label = (row[0] or "").strip()

        mapper: dict[int, int] = {}
        for i, cell in enumerate(row[1:], start=1):
            y = is_year_header_cell(cell)
            if y is not None:
                mapper[i] = y
        is_header = bool(mapper) and (
            not label
            or any(label.startswith(h) for h in HEADER_LABELS if h)
            or r_i == 0
        )
        if is_header:
            year_of_col = mapper
            continue
        if not label:
            continue

        sr = SummaryRow(label=label)
        for i, cell in enumerate(row[1:], start=1):
            y = year_of_col.get(i)
            v = parse_number(cell)
            if y is not None and v is not None:
                sr.values[y] = v
            elif v is not None:
                sr.changes[i] = (cell or "").strip()
        if sr.values:
            rows.append(sr)
    return rows


def find_summary_blocks(doc: ParsedDoc) -> list[tuple[int, list[list[str]], str, str]]:
    """找出所有汇总表块，返回 ``[(页码, 网格, 类别)]``。"""
    out: list[tuple[int, list[list[str]], str, str]] = []
    units = unit_index(doc)
    fallback = units[0][1] if units else ""
    for i, b in enumerate(doc.blocks):
        if b.kind != KIND_TABLE:
            continue
        grid = parse_html_table(b.html)
        if not grid or not is_summary_table(grid):
            continue
        flat = "|".join("|".join(r) for r in grid)
        kind = "主要会计数据" if "主要会计数据" in flat else "主要财务指标"
        out.append((b.page, grid, kind, unit_for(units, i, fallback)))
    return out


def extract_company(doc: ParsedDoc, prefer_summary: bool = True) -> list[MetricValue]:
    """抽出一家公司的全部指标。

    汇总表优先（精度高、口径统一）；缺的指标再从正式报表补
    （``source="statement"``）。
    """
    got: dict[tuple[str, int], MetricValue] = {}

    for page, grid, kind, unit in find_summary_blocks(doc):
        for sr in parse_summary_table(grid):
            name = match_metric(sr.label)
            if not name:
                continue
            for year, val in sr.values.items():
                key = (name, year)
                old = got.get(key)
                # 主要会计数据优先于主要财务指标
                if old is not None and old.source == "summary" and kind != "主要会计数据":
                    continue
                got[key] = MetricValue(
                    code=doc.code, name=doc.name, metric=name, year=year,
                    value=val, label=sr.label, page=page,
                    source="summary", note=kind, unit=unit)

    _fill_from_statements(doc, got, only_missing=prefer_summary)
    return list(got.values())


def _fill_from_statements(doc: ParsedDoc, got: dict, only_missing: bool = True) -> None:
    """用正式报表补汇总表缺失的指标。

    ## 表的选择：页码最小的那张

    标准顺序里「合并」在「母公司」之前，所以页码小的是合并口径。

    ## 曾经的一个致命 bug：``break`` 位置错了

    旧版在每个表的行循环里、**匹配到任意一个指标后就 break**：

        for row in grid:
            name = match_metric(row[0])
            if not name: continue
            if name not in best or b.page < best[name][0]:
                best[name] = (b.page, grid, ...)
            break          # ← 每张表只记录一个指标！

    后果是**每张表只贡献一个指标**。实测：

    - 招商银行 p14「2.1 本集团主要会计数据和财务指标」表里 利润总额 = 178,993（正确），
      但该表第一行匹配到的是别的指标，一 break 就整张表都没记下 利润总额，
      于是退到 p19「财务业绩摘要」的 (259)
    - 中国石化 p5「主要财务数据及指标」表里 经营现金流 = 162,496（正确），
      同样被跳过，最后用了 p102 现金流量表里被 MinerU 解析错位的 (3,280,213)

    现在遍历整表的所有行，每个指标都记。

    ## 另一处：选中的表取不到值时不能直接放弃

    同一指标可能在多张表里出现。旧版只保留**页码最小**的那一张，
    但那张表的对应行可能**没有数字**（合并单元格、跨页续表、解析错位），
    于是这个指标就整个丢了 —— 实测让「经营活动现金流量净额」从
    100/100 掉到 98/100。

    改为按页码顺序保留**候选列表**，逐个尝试直到取到值。
    """
    units = unit_index(doc)
    fallback = units[0][1] if units else ""
    # 指标 -> 按页码升序的候选 [(页码, 网格, 单位), ...]
    cands: dict[str, list[tuple[int, list[list[str]], str]]] = {}
    for i, b in enumerate(doc.blocks):
        if b.kind != KIND_TABLE:
            continue
        grid = parse_html_table(b.html)
        if not grid:
            continue
        # 遍历**所有行**：一张报表里通常有多个我们要的指标
        for row in grid:
            if not row or not (row[0] or "").strip():
                continue
            name = match_metric(row[0])
            if not name:
                continue
            lst = cands.setdefault(name, [])
            if all(p != b.page or g is not grid for p, g, _ in lst):
                lst.append((b.page, grid, unit_for(units, i, fallback)))
    for name in cands:
        cands[name].sort(key=lambda x: x[0])

    year = _guess_report_year(doc)
    for name, options in cands.items():
        for page, grid, unit in options:
            vals: list[float] = []
            for row in grid:
                if not row or not (row[0] or "").strip():
                    continue
                if match_metric(row[0]) != name:
                    continue
                vals = [v for v in (parse_number(c) for c in row[1:])
                        if v is not None]
                break
            if not vals:
                continue                     # 这张表取不到，试下一张
            key = (name, year)
            if key not in got:
                got[key] = MetricValue(
                    code=doc.code, name=doc.name, metric=name, year=year,
                    value=vals[0], label=name, page=page,
                    source="statement", unit=unit)
            break


def _guess_report_year(doc: ParsedDoc) -> int:
    """从文件名猜报告年份（``600108_20260430_90X1.pdf`` → 2025）。

    文件名里是**公告日期**（2026-04-30），报告年份 = 公告年 − 1。
    """
    m = re.search(r"_((?:19|20)\d{2})\d{4}_", doc.pdf_name or "")
    return int(m.group(1)) - 1 if m else 0
