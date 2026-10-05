# -*- coding: utf-8 -*-
"""阶段 2：结构化财务指标抽取 —— 公司 × 指标 × 年份。

## 为什么以「主要会计数据 / 主要财务指标」表为主

年报里这两张表是**强制标准化披露**的：
- 行标签全国统一（营业收入、利润总额、归母净利润、总资产…）
- **列就是年份**（2025年 / 2024年 / 2023年），一张表直接给出三年数据
- 还带同比增减列

所以从这里抽，一次就能拿到「一家公司 × 十几个指标 × 三年」，
而且精度远高于满篇扫表格（后者会撞上 MD&A 的分项数据、母公司口径等一堆同名行）。

**口径必须分开**：`归母净利润`（归属于上市公司股东的净利润）与 `净利润`
（含少数股东损益）是两个不同的数，混为一谈会给出看似合理实则错误的答案 ——
首轮验证里 7/11 份的"交叉验证不一致"就是这么来的。

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
from ..parse.verify import _norm_label, parse_number

YEAR_RE = re.compile(r"((?:19|20)\d{2})\s*年")

# 汇总表的表头文字，出现在第 0 列时表示"这是表头行"而不是数据行
HEADER_LABELS = ("主要会计数据", "主要财务指标", "主要财务数据", "项目", "")

# 规范化指标名 -> 可能的行标签（顺序即优先级）
METRICS: dict[str, tuple[str, ...]] = {
    "营业收入": ("营业收入", "营业总收入"),
    "利润总额": ("利润总额",),
    "净利润": ("净利润",),
    "归母净利润": ("归属于上市公司股东的净利润", "归属于母公司股东的净利润",
                   "归属于母公司所有者的净利润"),
    "扣非归母净利润": (
        "归属于上市公司股东的扣除非经常性损益的净利润",
        "扣除非经常性损益后的归属于母公司股东的净利润",
        "归属于上市公司股东的扣除非经常性损益后的净利润",
        "扣除非经常性损益后归属于母公司股东的净利润"),
    "经营活动现金流量净额": ("经营活动产生的现金流量净额", "经营活动现金流量净额"),
    "归母净资产": ("归属于上市公司股东的净资产", "归属于母公司所有者权益合计",
                   "归属于母公司所有者权益（或股东权益）合计",
                   "归属于上市公司股东的所有者权益"),
    "总资产": ("总资产", "资产总计", "资产合计"),
    "基本每股收益": ("基本每股收益", "基本每股收益（元／股）", "基本每股收益（元/股）"),
    "稀释每股收益": ("稀释每股收益", "稀释每股收益（元／股）", "稀释每股收益（元/股）"),
    "加权平均净资产收益率": ("加权平均净资产收益率", "加权平均净资产收益率（%）",
                             "加权平均净资产收益率(%)"),
    "扣非加权平均净资产收益率": ("扣除非经常性损益后的加权平均净资产收益率",
                                 "扣除非经常性损益后的加权平均净资产收益率（%）"),
}

METRIC_ORDER = list(METRICS)


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


@dataclass
class SummaryRow:
    label: str
    values: dict[int, float] = field(default_factory=dict)
    changes: dict[int, str] = field(default_factory=dict)


def _is_year_header_cell(text: str) -> int | None:
    """表头单元格里若含年份则返回该年，否则 None。"""
    t = (text or "").strip()
    if not t or "增减" in t or "变动" in t or "%" in t or "％" in t:
        return None
    m = YEAR_RE.search(t)
    return int(m.group(1)) if m else None


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

        # 这一行是不是（新的）表头？
        mapper: dict[int, int] = {}
        for i, cell in enumerate(row[1:], start=1):
            y = _is_year_header_cell(cell)
            if y is not None:
                mapper[i] = y
        is_header = bool(mapper) and (
            not label
            or any(label.startswith(h) for h in HEADER_LABELS if h)
            or (r_i == 0)
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


def _match_metric(label: str) -> str | None:
    """行标签 → 规范化指标名。精确优先，失败再前缀匹配。"""
    norm = _norm_label(label)
    if not norm:
        return None
    for name, aliases in METRICS.items():
        if any(norm == _norm_label(a) for a in aliases):
            return name
    for name, aliases in METRICS.items():
        if any(norm.startswith(_norm_label(a)) for a in aliases
               if len(_norm_label(a)) >= 4):
            return name
    return None


def find_summary_blocks(doc: ParsedDoc) -> list[tuple[int, list[list[str]], str]]:
    """找出所有汇总表块，返回 ``[(页码, 网格, 类别)]``。

    类别为 ``主要会计数据`` / ``主要财务指标``，用来判断优先级。
    """
    out: list[tuple[int, list[list[str]], str]] = []
    for b in doc.blocks:
        if b.kind != KIND_TABLE:
            continue
        grid = parse_html_table(b.html)
        if not grid:
            continue
        flat = "|".join("|".join(r) for r in grid)
        if "主要会计数据" in flat:
            out.append((b.page, grid, "主要会计数据"))
        elif "主要财务指标" in flat:
            out.append((b.page, grid, "主要财务指标"))
    return out


def extract_company(doc: ParsedDoc, prefer_summary: bool = True) -> list[MetricValue]:
    """抽出一家公司的全部指标。

    ``prefer_summary=True`` 时只用汇总表（精度高、口径统一）；
    汇总表缺的指标再从正式报表补（``source="statement"``）。
    """
    got: dict[tuple[str, int], MetricValue] = {}

    for page, grid, kind in find_summary_blocks(doc):
        for sr in parse_summary_table(grid):
            name = _match_metric(sr.label)
            if not name:
                continue
            for year, val in sr.values.items():
                key = (name, year)
                # 主要会计数据优先于主要财务指标
                old = got.get(key)
                if old is not None and old.source == "summary" and kind != "主要会计数据":
                    continue
                got[key] = MetricValue(
                    code=doc.code, name=doc.name, metric=name, year=year,
                    value=val, label=sr.label, page=page,
                    source="summary", note=kind)

    if not prefer_summary:
        _fill_from_statements(doc, got)
    else:
        _fill_from_statements(doc, got, only_missing=True)
    return list(got.values())


def _fill_from_statements(doc: ParsedDoc, got: dict, only_missing: bool = True) -> None:
    """用正式报表补汇总表缺失的指标。

    **只用页码最小的那张报表**（标准顺序里「合并」在「母公司」之前），
    否则会把母公司口径混进来 —— 那是合法但不同的数字。
    """
    best: dict[str, tuple[int, list[list[str]]]] = {}
    for b in doc.blocks:
        if b.kind != KIND_TABLE:
            continue
        grid = parse_html_table(b.html)
        if not grid:
            continue
        for row in grid:
            if not row or not (row[0] or "").strip():
                continue
            name = _match_metric(row[0])
            if not name:
                continue
            if name in best and best[name][0] <= b.page:
                continue
            best[name] = (b.page, grid)
            break

    for name, (page, grid) in best.items():
        for row in grid:
            if not row or not (row[0] or "").strip():
                continue
            if _match_metric(row[0]) != name:
                continue
            # 报表里第一列数字通常是本期（最新年）—— 年份回填由调用方处理
            vals = [parse_number(c) for c in row[1:]]
            vals = [v for v in vals if v is not None]
            if not vals:
                continue
            year = _guess_latest_year(doc)
            key = (name, year)
            if only_missing and key in got:
                continue
            if key not in got:
                got[key] = MetricValue(
                    code=doc.code, name=doc.name, metric=name, year=year,
                    value=vals[0], label=row[0].strip(), page=page,
                    source="statement")
            break


def _guess_latest_year(doc: ParsedDoc) -> int:
    """从文件名/标题猜报告年份（如 600108_20260430_90X1.pdf → 2025）。

    年报的文件名里是**公告日期**（2026-04-30），报告年份 = 公告年 − 1。
    """
    m = re.search(r"_((?:19|20)\d{2})\d{4}_", doc.pdf_name or "")
    if m:
        return int(m.group(1)) - 1
    return 0
