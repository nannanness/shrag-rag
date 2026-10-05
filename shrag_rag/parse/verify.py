# -*- coding: utf-8 -*-
"""解析质量验证 —— **内容驱动**，不依赖标题。

## 为什么推翻了标题驱动的做法

初版按 "合并资产负债表" 这类**标题**去找表，结果 11 份里 3 份 FAIL。追下去发现
是误报：银行类年报按证监会规定把合并与母公司报表**合并列示**，标题写作
「合并资产负债表和资产负债表」（浦发银行 p157）、「合并利润表和利润表」（p160）。
另外 MinerU 还会把某些标题判成 ``text`` 而不是 ``heading``（如 600108 的
「合并现金流量表」被标成 text，块 #700）。

**结论：靠标题定位报表太脆。** 改为**按行标签找数字** —— 我们真正要的是
「营业收入是多少」这类可抽取的数字，而不是"表格叫什么名字"。
这样顺带就成了阶段 2 指标抽取的原型。

## 三层验证

1. ``extract_metrics``  —— 按行标签抽关键财务指标（营收/净利/总资产/…）
2. ``check_report``     —— 指标覆盖度 + **跨表数值交叉验证**
   （同一数字在两张独立表里一致 → 两处都抽对了，这是最有力的证据）
3. ``find_suspicious``  —— 分级红旗，仅用于缩小人工检查范围
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .htmltable import parse_html_table
from .schema import KIND_HEADING, KIND_IMAGE, KIND_TABLE, ParsedDoc

# 关键财务指标：展示名 -> 可能的行标签（按优先级）
KEY_METRICS: list[tuple[str, tuple[str, ...]]] = [
    ("营业收入", ("营业收入", "营业总收入", "其中：营业收入")),
    ("净利润", ("归属于上市公司股东的净利润", "归属于母公司股东的净利润",
                "归属于母公司所有者的净利润", "净利润")),
    ("总资产", ("资产总计", "资产合计", "总资产")),
    ("负债合计", ("负债合计", "负债总计")),
    ("归母净资产", ("归属于母公司所有者权益（或股东权益）合计",
                    "归属于母公司所有者权益合计", "归属于上市公司股东的净资产",
                    "归属于母公司股东权益合计")),
    ("经营现金流净额", ("经营活动产生的现金流量净额", "经营活动现金流量净额")),
    ("基本每股收益", ("基本每股收益", "基本每股收益（元／股）", "基本每股收益（元/股）")),
]

_SEV = {"high": 0, "mid": 1, "info": 2}
_NUM = re.compile(r"^-?\d[\d,]*(\.\d+)?$")
_UNITS = {"元", "千元", "万元", "百万元", "亿元", "人民币", "币种"}
# 行标签前缀，匹配前先剥掉（"其中：营业收入" 应视为 "营业收入"）
_LABEL_PREFIX = ("其中：", "其中:", "减：", "减:", "加：", "加:", "其中", "减", "加")


def _norm_label(s: str) -> str:
    t = (s or "").replace(" ", "").replace("\u3000", "")
    for p in _LABEL_PREFIX:
        if t.startswith(p) and len(t) > len(p):
            t = t[len(p):]
            break
    return t


def parse_number(s: str) -> float | None:
    """解析表格里的数字单元格。支持 ``(1,234)`` 负数写法与全角括号。"""
    t = (s or "").strip().replace(",", "").replace("，", "")
    t = t.replace("（", "(").replace("）", ")")
    if not t:
        return None
    neg = t.startswith("(") and t.endswith(")")
    t = t.strip("()")
    if not t or t in _UNITS:
        return None
    if not _NUM.match(t):
        return None
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


@dataclass
class MetricHit:
    metric: str
    label: str              # 命中的原始行标签
    page: int
    values: list[float] = field(default_factory=list)
    raw: list[str] = field(default_factory=list)
    is_summary: bool = False    # 来自「主要会计数据」汇总表
    is_statement: bool = False  # 来自正式财务报表

    @property
    def first(self) -> float | None:
        return self.values[0] if self.values else None


def extract_metrics(doc: ParsedDoc,
                    metrics=KEY_METRICS) -> dict[str, list[MetricHit]]:
    """扫描全文所有表格，按行标签抽关键指标。返回 ``指标 -> 命中列表``。

    **两轮匹配**（这是必须的）：
    - 第一轮只认**精确相等**的标签；
    - 只有该指标一个精确命中都没有时，才退到 ``startswith``。

    为什么不一上来就用 startswith：别名 ``净利润`` 会 startswith 命中
    ``净利润率`` 这种**比率行**，把百分比当成净利润金额 —— 那是静默错误。
    两轮匹配能在保留 ``基本每股收益（元／股）`` 这类后缀变体的同时挡住它。
    """
    out: dict[str, list[MetricHit]] = {name: [] for name, _ in metrics}
    rows_all: list[tuple[list[str], int, bool, bool]] = []
    for b in doc.blocks:
        if b.kind != KIND_TABLE:
            continue
        grid = parse_html_table(b.html)
        if not grid:
            continue
        flat = "|".join("|".join(r) for r in grid)
        is_sum = ("主要会计数据" in flat or "主要财务指标" in flat)
        is_stmt = _is_statement_table(grid)
        for row in grid:
            if row and (row[0] or "").strip():
                rows_all.append((row, b.page, is_sum, is_stmt))

    for name, aliases in metrics:
        als = [_norm_label(a) for a in aliases]
        for mode in ("exact", "prefix"):
            hits: list[MetricHit] = []
            for row, page, is_sum, is_stmt in rows_all:
                norm = _norm_label(row[0])
                if not norm:
                    continue
                if mode == "exact":
                    if norm not in als:
                        continue
                else:
                    if not any(norm.startswith(a) for a in als if len(a) >= 4):
                        continue
                vals, raws = [], []
                for c in row[1:]:
                    v = parse_number(c)
                    if v is not None:
                        vals.append(v)
                        raws.append(c)
                if vals:
                    hits.append(MetricHit(name, row[0].strip(), page, vals, raws,
                                          is_summary=is_sum, is_statement=is_stmt))
            if hits:
                out[name] = hits
                break
    return out


# 正式财务报表的"特征行"。一张表命中 ≥3 个才算报表，
# 用来把 MD&A 里的分项表排除在交叉验证之外。
_STATEMENT_ROWS = ("营业总收入", "营业总成本", "营业成本", "营业利润", "利润总额",
                   "净利润", "资产总计", "负债合计", "流动资产合计",
                   "非流动资产合计", "流动负债合计", "所有者权益", "实收资本",
                   "经营活动产生的现金流量净额", "投资活动产生的现金流量净额",
                   "筹资活动产生的现金流量净额", "货币资金", "应收账款", "存货")


def _is_statement_table(grid: list[list[str]]) -> bool:
    """这张表是不是正式财务报表？"""
    labels = {_norm_label(r[0]) for r in grid if r and r[0]}
    return sum(1 for s in _STATEMENT_ROWS if any(s in x for x in labels)) >= 3


def _key_table_pages(doc: ParsedDoc) -> set[int]:
    """「主要会计数据」表所在的页 —— 年报里最权威的汇总表，用于交叉验证。"""
    pages: set[int] = set()
    for b in doc.blocks:
        if b.kind != KIND_TABLE:
            continue
        grid = parse_html_table(b.html)
        flat = "|".join("|".join(r) for r in grid)
        if "主要会计数据" in flat or ("主要财务指标" in flat and "营业收入" in flat):
            pages.add(b.page)
    return pages


def check_report(doc: ParsedDoc) -> dict:
    """指标覆盖度 + **跨表数值一致性**。

    交叉验证只做**有意义的那种**：把「主要会计数据」汇总表里的值与它在
    **正式财务报表**（利润表/资产负债表/现金流量表）里的值对比 ——
    这两处按会计准则必须一致，不一致才是真问题。

    > 两版都错在这里，值得记下来：
    > 初版拿"同一指标在不同页的前两个命中"对比，5/6 报不一致；
    > 二版加了量级过滤仍然误报，因为 MD&A 的**分项营业收入**（按行业/产品拆分）
    > 与利润表的**营业收入总额**量级相同但数值本就不同，不该拿来比。
    > 假警报比不检查更糟 —— 会让人不再信任检查结果。
    """
    hits = extract_metrics(doc)
    found: dict[str, dict] = {}
    problems: list[str] = []
    cross: list[dict] = []

    for name, _ in KEY_METRICS:
        hs = hits[name]
        if not hs:
            found[name] = {"page": None, "value": None, "n": 0}
            problems.append(f"{name}: 全文表格里找不到该行标签")
            continue

        summary = next((h for h in hs if h.is_summary), None)
        # 正式报表取**页码最小**的**非汇总表**：「主要会计数据」本身也会命中
        # 报表特征行（它有利润总额/净利润），不排除掉就会拿它跟自己比，
        # 导致交叉验证静默失效（曾经 11 份全是 0/0）。
        # 标准顺序里「合并」在「母公司」之前，所以取页码最小的那张即合并口径。
        stmt = min((h for h in hs if h.is_statement and not h.is_summary),
                   key=lambda h: h.page, default=None)
        authoritative = summary or stmt or hs[0]
        found[name] = {"page": authoritative.page, "value": authoritative.first,
                       "n": len(hs), "label": authoritative.label,
                       "from_key_table": bool(summary),
                       "stmt_page": stmt.page if stmt else None}

        # 只在**同类口径**之间比：汇总表（合并口径） vs 合并报表
        if summary and stmt and stmt.page != summary.page:
            va, vb = summary.first, stmt.first
            if va is not None and vb is not None:
                cross.append({
                    "metric": name, "page_a": summary.page, "val_a": va,
                    "page_b": stmt.page, "val_b": vb,
                    "match": abs(va - vb) < max(abs(va) * 1e-9, 0.01)})

    n_found = sum(1 for v in found.values() if v["page"] is not None)
    n_bad_cross = sum(1 for c in cross if not c["match"])
    return {"metrics": found, "cross": cross,
            "n_found": n_found, "n_total": len(KEY_METRICS),
            "ok": n_found == len(KEY_METRICS) and n_bad_cross == 0,
            "problems": problems}


def find_suspicious(doc: ParsedDoc, min_text_len: int = 4,
                    max_cols: int = 18) -> list[dict]:
    """红旗，带 ``severity``。

    - ``high``：有内容的表格却一个数字都没有 —— 可能抽错了行
    - ``mid`` ：列数异常多、标题过长、整页仅一块
    - ``info``：空表格（**多为跨页表的重复检出**，非真丢失）、超短文本

    空表格为什么只算 info：实测 600108 合并资产负债表被 MinerU 合并成
    105 行一张表（末尾是「负债和所有者权益总计 9,889,476,545.41」，
    与主要会计数据的总资产逐位一致），而 p49/p50 另有空的 table 块属于
    重复检出。把这种报成高危会让人不再信任检查结果。
    """
    flags: list[dict] = []
    per_page: dict[int, list] = {}
    for b in doc.blocks:
        per_page.setdefault(b.page_idx, []).append(b)

    for b in doc.blocks:
        if b.kind == KIND_TABLE:
            grid = parse_html_table(b.html)
            if not grid:
                flags.append({"page": b.page, "kind": b.kind, "severity": "info",
                              "why": "空表格（多为跨页表重复检出）"})
                continue
            digits = sum(1 for r in grid for c in r if any(ch.isdigit() for ch in c))
            ncols = max(len(r) for r in grid)
            if digits == 0 and len(grid) >= 3:
                flags.append({"page": b.page, "kind": b.kind, "severity": "high",
                              "why": f"{len(grid)}行表格但无任何数字",
                              "detail": str(grid[0])[:80]})
            elif ncols > max_cols:
                flags.append({"page": b.page, "kind": b.kind, "severity": "mid",
                              "why": f"表格 {ncols} 列"})
        elif b.kind == KIND_HEADING and len(b.text) > 60:
            flags.append({"page": b.page, "kind": b.kind, "severity": "mid",
                          "why": f"标题过长({len(b.text)}字)", "detail": b.text[:60]})
        else:
            t = b.text.strip()
            if len(t) < min_text_len:
                flags.append({"page": b.page, "kind": b.kind, "severity": "info",
                              "why": f"文本仅 {len(t)} 字", "detail": t})
            else:
                bad = sum(1 for ch in t if ch in "\ufffd\u25a1")
                if bad / len(t) > 0.3:
                    flags.append({"page": b.page, "kind": b.kind, "severity": "high",
                                  "why": f"异常字符占比 {bad / len(t):.0%}",
                                  "detail": t[:60]})

    for pi, bs in per_page.items():
        if len(bs) == 1 and bs[0].kind not in (KIND_TABLE, KIND_IMAGE):
            flags.append({"page": pi + 1, "kind": bs[0].kind, "severity": "mid",
                          "why": "整页仅 1 个块",
                          "detail": (bs[0].text or "")[:50]})
    return flags


def severity_counts(flags: list[dict]) -> dict:
    out = {"high": 0, "mid": 0, "info": 0}
    for f in flags:
        s = f.get("severity", "info")
        out[s] = out.get(s, 0) + 1
    return out
