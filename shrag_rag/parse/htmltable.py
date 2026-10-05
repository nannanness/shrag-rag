# -*- coding: utf-8 -*-
"""把 MinerU 输出的 HTML 表格解析成二维网格。

MinerU 的表格是 ``<table><tr><td rowspan="3">…`` 形式，**不是 Markdown**。
用标准库 ``html.parser`` 解析，不引入额外依赖。

必须处理 ``rowspan`` / ``colspan`` —— 年报里的表头合并单元格非常常见，
不展开的话列会错位（例如资产负债表里"流动资产"横跨多列）。
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

_WS = re.compile(r"[\s\u00a0]+")


def _clean(s: str) -> str:
    return _WS.sub(" ", (s or "").replace("\n", " ")).strip()


class _TableParser(HTMLParser):
    """收集 (text, colspan, rowspan) 三元组。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[tuple[str, int, int]]] = []
        self._row: list[tuple[str, int, int]] | None = None
        self._buf: list[str] = []
        self._span = (1, 1)
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            if self._row is None:
                self._row = []
            self._buf = []
            self._span = (max(1, int(a.get("colspan") or 1)),
                          max(1, int(a.get("rowspan") or 1)))
        elif tag in ("br", "p") and self._buf is not None:
            self._buf.append(" ")

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._row is not None:
            self._row.append((_clean("".join(self._buf)), *self._span))
            self._buf = []
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._buf is not None:
            self._buf.append(data)


def parse_html_table(html: str) -> list[list[str]]:
    """解析成二维网格，``rowspan``/``colspan`` 已展开（重复单元格填同样的值）。

    展开策略：``colspan=3`` 的值占 3 列（第 2、3 列填空串）；
    ``rowspan=2`` 的值在下一行的同列再出现一次。
    """
    if not html or "<tr" not in html.lower():
        return []

    p = _TableParser()
    p.feed(html)
    p.close()
    raw = p.rows
    if not raw:
        return []

    ncols = sum(c for _, c, _ in raw[0])
    for r in raw[1:]:
        ncols = max(ncols, sum(c for _, c, _ in r))
    ncols = max(ncols, 1)

    grid: list[list[str]] = []
    # pending[col] = (剩余行数, 值)，用于 rowspan 回填
    pending: dict[int, tuple[int, str]] = {}

    for r in raw:
        row: list[str | None] = [None] * ncols
        # 先填上一行 rowspan 遗留
        for col, (left, val) in list(pending.items()):
            if col < ncols:
                row[col] = val
            if left - 1 <= 0:
                pending.pop(col, None)
            else:
                pending[col] = (left - 1, val)
        # 再放本行单元格
        ci = 0
        for text, cs, rs in r:
            while ci < ncols and row[ci] is not None:
                ci += 1
            if ci >= ncols:
                break
            row[ci] = text
            for k in range(1, cs):
                if ci + k < ncols:
                    row[ci + k] = ""
            if rs > 1:
                for k in range(cs):
                    if ci + k < ncols:
                        pending[ci + k] = (rs - 1, text)
            ci += cs
        grid.append(["" if x is None else x for x in row])

    return grid


def table_to_markdown(html: str) -> str:
    """HTML 表格 → Markdown 管道表（便于人眼查看 / 纯文本检索）。"""
    grid = parse_html_table(html)
    if not grid:
        return ""
    ncols = max(len(r) for r in grid)
    grid = [r + [""] * (ncols - len(r)) for r in grid]

    def cell(s: str) -> str:
        return s.replace("|", "\\|").replace("\n", " ") or " "

    out = ["| " + " | ".join(cell(c) for c in grid[0]) + " |",
           "|" + "---|" * ncols]
    for r in grid[1:]:
        out.append("| " + " | ".join(cell(c) for c in r) + " |")
    return "\n".join(out)


def table_to_tsv(html: str) -> str:
    grid = parse_html_table(html)
    return "\n".join("\t".join(c.replace("\t", " ") for c in r) for r in grid)
