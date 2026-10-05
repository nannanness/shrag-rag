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


def find_summary_blocks(doc: ParsedDoc) -> list[tuple[int, list[list[str]], str]]:
    """找出所有汇总表块，返回 ``[(页码, 网格, 类别)]``。"""
    out: list[tuple[int, list[list[str]], str]] = []
    for b in doc.blocks:
        if b.kind != KIND_TABLE:
            continue
        grid = parse_html_table(b.html)
        if not grid or not is_summary_table(grid):
            continue
        flat = "|".join("|".join(r) for r in grid)
        kind = "主要会计数据" if "主要会计数据" in flat else "主要财务指标"
        out.append((b.page, grid, kind))
    return out


def extract_company(doc: ParsedDoc, prefer_summary: bool = True) -> list[MetricValue]:
    """抽出一家公司的全部指标。

    汇总表优先（精度高、口径统一）；缺的指标再从正式报表补
    （``source="statement"``）。
    """
    got: dict[tuple[str, int], MetricValue] = {}

    for page, grid, kind in find_summary_blocks(doc):
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
                    source="summary", note=kind)

    _fill_from_statements(doc, got, only_missing=prefer_summary)
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
            name = match_metric(row[0])
            if not name:
                continue
            if name not in best or b.page < best[name][0]:
                best[name] = (b.page, grid)
            break

    year = _guess_report_year(doc)
    for name, (page, grid) in best.items():
        for row in grid:
            if not row or not (row[0] or "").strip():
                continue
            if match_metric(row[0]) != name:
                continue
            vals = [v for v in (parse_number(c) for c in row[1:]) if v is not None]
            if not vals:
                continue
            key = (name, year)
            if key not in got:
                got[key] = MetricValue(
                    code=doc.code, name=doc.name, metric=name, year=year,
                    value=vals[0], label=row[0].strip(), page=page,
                    source="statement")
            break


def _guess_report_year(doc: ParsedDoc) -> int:
    """从文件名猜报告年份（``600108_20260430_90X1.pdf`` → 2025）。

    文件名里是**公告日期**（2026-04-30），报告年份 = 公告年 − 1。
    """
    m = re.search(r"_((?:19|20)\d{2})\d{4}_", doc.pdf_name or "")
    return int(m.group(1)) - 1 if m else 0
