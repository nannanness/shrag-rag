# -*- coding: utf-8 -*-
"""解析质量验证 —— **内容驱动**，不依赖标题。

指标口径一律取自 ``shrag_rag/spec.py``（与抽取层同一份）。

## 为什么推翻了标题驱动的做法

初版按 "合并资产负债表" 这类**标题**去找表，结果 11 份里 3 份 FAIL。追下去发现
是误报：银行类年报按证监会规定把合并与母公司报表**合并列示**，标题写作
「合并资产负债表和资产负债表」（浦发银行 p157）、「合并利润表和利润表」（p160）。
另外 MinerU 还会把某些标题判成 ``text`` 而不是 ``heading``（如 600108 的
「合并现金流量表」被标成 text，块 #700）。

**结论：靠标题定位报表太脆。** 改为**按行标签找数字**。

## 跨表交叉验证踩过的坑（四次假警报，最终降级为参考信号）

这条检查前后错了四版，每次都报出一堆假警报 —— 而**假警报比不检查更糟**，
会让人不再信任检查结果：

1. 拿"同一指标在不同页的前两个命中"对比 → 5/6 报不一致。
   错在 MD&A 的**分项营收**与利润表的**营收总额**本就不该相等
2. 加量级过滤后仍误报 → 没有区分"报表"与"非报表"，分项表还在参与比较
3. 拿**合并**总资产（98.9 亿）比**母公司**总资产（90.6 亿）→ 合法差异被报成错误
4. 改成"页码最小的非汇总报表"后，又因报表被 MinerU 拆成多个块
   （合并利润表横跨 p52+p53，续块的报表特征行不足 3 个而未被判为报表），
   退到去比 p126 的**母公司**利润表

**结论：同一个指标在年报里合法地存在多个不同值**（合并/母公司、分项/总额、
不同年份列），所以一致性**不适合当门槛**。

现在的分工：
- **判定**只看**指标覆盖度**（能不能抽到 —— 这才是硬要求）
- **一致性**作为参考信号报出来：同一指标在其它表里能否找到相同值
"""

from __future__ import annotations

from .htmltable import parse_html_table
from .schema import KIND_HEADING, KIND_IMAGE, KIND_TABLE, ParsedDoc
from ..spec import (CORE_METRICS, METRICS, is_statement_table,  # noqa: F401
                    is_summary_table, match_metric, norm_label, parse_number)


# --------------------------------------------------------------------------- #
# 指标抽取（验证用，正式抽取在 extract/ 层）
# --------------------------------------------------------------------------- #

def extract_metrics(doc: ParsedDoc) -> dict[str, list[dict]]:
    """扫描全文所有表格，按行标签抽核心指标。

    返回 ``指标名 -> [{page, label, value, is_summary, is_statement}]``。
    """
    out: dict[str, list[dict]] = {m: [] for m in CORE_METRICS}
    rows: list[tuple[list[str], int, bool, bool]] = []
    for b in doc.blocks:
        if b.kind != KIND_TABLE:
            continue
        grid = parse_html_table(b.html)
        if not grid:
            continue
        is_sum = is_summary_table(grid)
        is_stmt = is_statement_table(grid)
        for row in grid:
            if row and (row[0] or "").strip():
                rows.append((row, b.page, is_sum, is_stmt))

    for name in CORE_METRICS:
        aliases = [_norm(a) for a in METRICS[name]]
        for mode in ("exact", "prefix"):
            hits: list[dict] = []
            for row, page, is_sum, is_stmt in rows:
                n = _norm(row[0])
                if not n:
                    continue
                if mode == "exact":
                    if n not in aliases:
                        continue
                elif not any(n.startswith(a) for a in aliases if len(a) >= 4):
                    continue
                vals = [v for v in (parse_number(c) for c in row[1:]) if v is not None]
                if vals:
                    hits.append({"page": page, "label": row[0].strip(),
                                 "value": vals[0], "is_summary": is_sum,
                                 "is_statement": is_stmt, "metric": name})
            if hits:
                out[name] = hits
                break
    return out


def _norm(s: str) -> str:
    return norm_label(s)


def _close(a: float, b: float) -> bool:
    return abs(a - b) < max(abs(a) * 1e-9, 0.01)


def check_report(doc: ParsedDoc) -> dict:
    """核心指标覆盖度（**判定门槛**）+ 跨表一致性（**参考信号**）。

    ## 为什么一致性不能当门槛

    同一个指标在年报里**合法地存在多个不同值**：
    - **合并 vs 母公司**：亚盛集团 p48 合并总资产 98.9 亿 vs p50 母公司 90.6 亿
    - **分项 vs 总额**：MD&A 按行业拆的营业收入 vs 利润表的营业收入总额
    - **不同年份列**：同一张表里 2025/2024/2023 三列

    我先后试了三版把它做成 pass/fail 门槛，每次都在不同公司上产生假警报
    （详见模块 docstring）。**假警报比不检查更糟**，所以现在：

    - ``ok`` **只由覆盖度决定** —— "能不能抽到"才是硬要求
    - 一致性作为 ``agree`` 返回，**报出来供参考，不影响判定**
    """

    def collect():
        hits = extract_metrics(doc)
        found: dict[str, dict] = {}
        problems: list[str] = []
        agree: list[dict] = []

        for name in CORE_METRICS:
            hs = hits[name]
            if not hs:
                found[name] = {"page": None, "value": None, "n": 0, "label": ""}
                problems.append(f"{name}: 全文表格里找不到该行标签")
                continue
            # 取值优先汇总表（合并口径、口径统一）
            summary = next((h for h in hs if h["is_summary"]), None)
            auth = summary or hs[0]
            found[name] = {"page": auth["page"], "value": auth["value"],
                           "n": len(hs), "label": auth["label"],
                           "from_key_table": bool(summary)}
            others = [h for h in hs if h is not auth]
            n_agree = sum(1 for h in others if _close(h["value"], auth["value"]))
            agree.append({
                "metric": name, "page": auth["page"], "value": auth["value"],
                "label": auth["label"], "n_others": len(others),
                "n_agree": n_agree,
                # 没有其它命中时无从判断，视为"无异议"
                "consistent": (len(others) == 0) or n_agree >= 1,
            })

        n_found = sum(1 for v in found.values() if v["page"] is not None)
        return found, problems, agree, n_found

    found, problems, agree, n_found = collect()
    return {"metrics": found, "agree": agree,
            "n_found": n_found, "n_total": len(CORE_METRICS),
            # 判定只看覆盖度
            "ok": n_found == len(CORE_METRICS),
            "n_consistent": sum(1 for a in agree if a["consistent"]),
            "problems": problems}


# --------------------------------------------------------------------------- #
# 红旗（分级）
# --------------------------------------------------------------------------- #

def find_suspicious(doc: ParsedDoc, min_text_len: int = 4,
                    max_cols: int = 18) -> list[dict]:
    """红旗，带 ``severity``。

    - ``high``：有内容的表格却一个数字都没有 —— 可能抽错了行
    - ``mid`` ：列数异常多、标题过长、整页仅一块
    - ``info``：空表格、超短文本

    **空表格为什么只算 info**：实测 600108 合并资产负债表被 MinerU 合并成
    105 行一张表（末行「负债和所有者权益总计 9,889,476,545.41」与主要会计
    数据的总资产逐位一致），而 p49/p50 另有空的 table 块属于**重复检出**。
    把这种报成高危会淹没真正的问题。
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
