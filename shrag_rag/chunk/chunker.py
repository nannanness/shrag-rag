# -*- coding: utf-8 -*-
"""阶段 3：章节感知切片。

## 三条设计原则

1. **表格不切**：一张表 = 一个 chunk，绝不跨 chunk 切开。
   表头一旦和数字分离，数字就失去意义 —— 而财报问答要的恰恰是"这个数字是什么"。
2. **按章节切，不按固定字数**：年报的章节边界（`第三节 管理层讨论与分析`、
   `一、经营情况讨论与分析`）天然干净，比按 500 字硬切好得多。
   只有当**同一节内**内容超过上限时才二次切分。
3. **每个 chunk 带全元数据**：code / 公司名 / 年份 / 章节 / 页码 / 类型 / 指标名。
   这是后面引用溯源（"翻回原文核对"）的基础。

## 章节层级的两个来源

- MinerU 的 ``text_level``（标题块自带层级）
- **文本块里的章节标记**：实测 MinerU 会把某些标题判成 ``text`` 而非 ``heading``
  （如 600108 的「合并现金流量表」被标成 text，块 #700），所以还要按
  ``第X节`` / ``一、`` / ``（一）`` 这类模式兜底识别。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Iterator

from ..parse.htmltable import parse_html_table, table_to_markdown
from ..parse.schema import KIND_HEADING, KIND_TABLE, KIND_TEXT, Block, ParsedDoc
from ..spec import ENUM_RE, match_metric

# 兜底识别章节：MinerU 会把部分标题判成 text
_SECTION_RE = re.compile(r"^第\s*[一二三四五六七八九十]+\s*节")
_L2_RE = re.compile(r"^[一二三四五六七八九十]+\s*[、.]")
_L3_RE = re.compile(r"^[\(（]\s*[一二三四五六七八九十]+\s*[\)）]")

# 章节路径最多保留几级（太长会让元数据很啰嗦）
MAX_SECTION_DEPTH = 3

# 只有**层级 ≤ 此值**的标题才触发 flush。
# 3/4 级小标题（「(2).按坏账计提方法分类披露」这类）数量极多，
# 若每个都切一刀，chunk 中位数会掉到 72 字 —— 切碎了对检索毫无好处。
FLUSH_LEVEL = 2

# MinerU 会把勾选框行误判成 heading（如「√适用 □不适用」）。
# 实测它一度成为"出现最多的章节"（10766 个 chunk）。这类文本不是章节。
_NOT_A_HEADING_RE = re.compile(r"[□√]|适用|不适用|^无$|^是$|^否$")


@dataclass
class Chunk:
    chunk_id: str
    code: str
    name: str
    year: int
    kind: str                   # text | table
    section: str                # 章节路径，如 "第三节 管理层讨论与分析 > 一、经营情况讨论与分析"
    page_start: int             # 1 基（对外展示口径）
    page_end: int
    text: str                   # 嵌入/检索用：[章节] 前缀 + 正文（短碎片靠它才有意义）
    body: str = ""                  # 纯正文（不含章节前缀），供展示与引用
    n_chars: int = 0
    metrics: list[str] = field(default_factory=list)   # 该表格命中的财务指标
    labels: list[str] = field(default_factory=list)    # 行标签样本（便于排查）

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- #
# 章节识别
# --------------------------------------------------------------------------- #

def _level_of(b: Block) -> int | None:
    """判断一个块是不是**章节标题**，是则返回其层级（1 最高）。

    ## 为什么不信 MinerU 的 ``text_level``

    实测 600108：MinerU 把**所有**标题都标成 ``text_level=2``
    （只有 p1 的报告大标题是 1）。于是 ``第一节 释义`` 与 ``一、 释义``
    变成同级，章节层级完全塌掉。所以层级改由**文本编号模式**推断。

    ## 为什么要区分「标题」与「正文句子」

    正文里大量段落以「一、二、三、」开头，例如
    ``三、 大信会计师事务所（特殊普通合伙）…出具了标准无保留意见的审计报告。``
    —— 按编号模式它会被误判成标题。用**句读特征**挡住：
    标题里不该出现 `。；，！？`，且剥掉序号后应当很短。
    """
    t = (b.text or "").strip()
    if not t or len(t) > 56:
        return None

    m = ENUM_RE.match(t)
    rest = t[m.end():].strip() if m else t
    if not rest or len(rest) > 40:
        return None
    if re.search(r"[。；，！？]", rest):        # 标题不含句读
        return None
    if _NOT_A_HEADING_RE.search(t):             # 勾选框行不是标题
        return None

    if _SECTION_RE.match(t):                    # 第X节
        return 1
    if _L2_RE.match(t):                         # 一、
        return 2
    if _L3_RE.match(t):                         # （一）
        return 3
    if re.match(r"^[0-9]+\s*[、.]", t):         # 1、
        return 4
    # 没有编号标记：只在 MinerU 判为标题时才认（如「重要提示」「目录」）
    return 1 if b.kind == KIND_HEADING else None


def _section_path(stack: list[tuple[int, str]]) -> str:
    return " > ".join(t for _, t in stack[-MAX_SECTION_DEPTH:])


def _top_section(section: str) -> str:
    """章节路径的**顶层大节**，用于判断两个 chunk 是否属于同一大节。"""
    return section.split(" > ")[0] if section else ""


def _last_text_chunk(out: list["Chunk"], top: str, lookback: int = 8):
    """往回找最近的、属于同一顶层大节的文本 chunk。

    为什么不能只看 ``out[-1]``：清单类内容（「六、是否存在…」→「否」）里，
    每条问题各成一节，且中间常夹表格。只比相邻项的话，1 个字的「否」会
    独立成 chunk —— 实测这类碎片占文本 chunk 的 37.9%，对检索毫无价值。
    """
    for c in reversed(out[-lookback:]):
        if c.kind == "text" and _top_section(c.section) == top:
            return c
    return None


# --------------------------------------------------------------------------- #
# 切片主逻辑
# --------------------------------------------------------------------------- #

def chunk_document(doc: ParsedDoc, year: int, max_chars: int = 800,
                   min_chars: int = 200, min_table_chars: int = 20,
                   max_table_chars: int = 12000) -> list[Chunk]:
    """把一份 ``ParsedDoc`` 切成 chunk 列表。

    :param max_chars: 文本 chunk 的目标上限（同一节内超出才二次切分）
    :param min_chars: 低于此长度的尾部文本并入上一个 chunk，避免碎片
    :param min_table_chars: 表格短于此长度视为噪声丢弃（如「√适用 □不适用」）
    :param max_table_chars: 表格超长时**仍然不切**，但会截断并标注 —— 后续可换成摘要
    """
    out: list[Chunk] = []
    stack: list[tuple[int, str]] = []
    buf: list[str] = []
    buf_pages: list[int] = []
    seq = 0

    def emit_text() -> None:
        nonlocal buf, buf_pages, seq
        body = "\n".join(x for x in buf if x).strip()
        pages = buf_pages               # 先留住：下面会把 buf_pages 清空
        sec = _section_path(stack)
        buf, buf_pages = [], []
        if not body:
            return
        # 小碎片并入同一顶层大节内最近的文本 chunk（见 _last_text_chunk 的说明）
        if len(body) < min_chars and out:
            prev = _last_text_chunk(out, _top_section(sec))
            if prev is not None:
                prev.body = (prev.body + "\n" + body).strip()
                prev.text = (f"[{prev.section}]\n{prev.body}" if prev.section
                             else prev.body)
                prev.n_chars = len(prev.text)
                prev.page_end = max(prev.page_end, max(pages))
                return
        seq += 1
        # 章节前缀必须进 text：清单式内容（「是否存在…」→「否」）里，
        # 碎片只有配上它所属的问题才可检索。表格 chunk 早就这么做了。
        text = f"[{sec}]`n{body}" if sec else body
        # 丢弃封面碎片：无章节归属又极短的（如「诺德新材」「2025年度报告」），
        # 检索不到也有没有的信息量
        if not sec and len(body) < 12:
            seq -= 1
            return
        out.append(Chunk(
            chunk_id=f"{doc.code}-t{seq:05d}",
            code=doc.code, name=doc.name, year=year, kind="text",
            section=sec,
            page_start=min(pages), page_end=max(pages),
            text=text, body=body, n_chars=len(text),
        ))

    for b in doc.blocks:
        # ---- 标题：更新章节栈；只在**大节边界**才 flush ---- #
        lvl = _level_of(b)
        if lvl is not None:
            if lvl <= FLUSH_LEVEL:
                emit_text()
            while stack and stack[-1][0] >= lvl:
                stack.pop()
            stack.append((lvl, (b.text or "").strip()))
            continue

        # ---- 表格：独立成 chunk，绝不与文本混切 ---- #
        if b.kind == KIND_TABLE:
            # **不无条件 flush 文本缓冲**：表格本来就是独立 chunk，文本可以
            # 跨过它继续累积。否则每遇一张表就切一刀，而全库有 2.5 万张表，
            # 文本会被切得极碎（实测 chunk 中位数因此掉到 139 字）。
            # 只有缓冲已有相当内容时才先落地，避免文本 chunk 跨表跨得太远。
            if sum(len(x) + 1 for x in buf) >= min_chars:
                emit_text()
            grid = parse_html_table(b.html)
            # 过滤噪声表：单行表、以及"无任何数字且不足 3 行"的表
            # （典型是「√适用 □不适用」这类勾选框，对问答没有价值）
            if len(grid) < 2:
                continue
            has_digit = any(ch.isdigit()
                            for row in grid for cell in row for ch in (cell or ""))
            if not has_digit and len(grid) < 3:
                continue
            md = table_to_markdown(b.html)
            if not md:
                continue
            cap = " ".join(b.caption).strip()
            note = " ".join(b.footnote).strip()
            head = f"[{_section_path(stack)}]" if stack else ""
            parts = [head, f"表：{cap}" if cap else "", md,
                     f"注：{note}" if note else ""]
            text = "\n".join(p for p in parts if p).strip()
            if len(text) < min_table_chars:
                continue
            truncated = False
            if len(text) > max_table_chars:
                text = text[:max_table_chars] + "\n…（超长表格已截断）"
                truncated = True
            # 记录该表命中的指标与行标签，供结构化路由关联
            metrics, labels = _table_metrics(b)
            seq += 1
            out.append(Chunk(
                chunk_id=f"{doc.code}-b{seq:05d}",
                code=doc.code, name=doc.name, year=year, kind="table",
                section=_section_path(stack),
                page_start=b.page, page_end=b.page,
                text=text, body=md, n_chars=len(text),
                metrics=metrics, labels=labels[:8],
            ))
            if truncated:
                out[-1].labels.append("__truncated__")
            continue

        # ---- 正文：累积，超过上限就在**同一节内**切一刀 ---- #
        if b.kind == KIND_TEXT and (b.text or "").strip():
            buf.append(b.text.strip())
            buf_pages.append(b.page)
            if sum(len(x) + 1 for x in buf) >= max_chars:
                emit_text()

    emit_text()
    return out


def _table_metrics(b: Block) -> tuple[list[str], list[str]]:
    """这张表里出现了哪些财务指标 / 行标签。"""
    grid = parse_html_table(b.html)
    metrics: list[str] = []
    labels: list[str] = []
    for row in grid:
        if not row or not (row[0] or "").strip():
            continue
        lab = row[0].strip()
        labels.append(lab)
        m = match_metric(lab)
        if m and m not in metrics:
            metrics.append(m)
    return metrics, labels


def iter_chunks(docs: Iterator[tuple[ParsedDoc, int]], **kw) -> Iterator[Chunk]:
    for doc, year in docs:
        yield from chunk_document(doc, year, **kw)
