# -*- coding: utf-8 -*-
"""重排规则：**靠元数据选对哪张表**，而不是改检索本身。

## 为什么需要它

指标预过滤能把候选集从数百条缩到个位数，但**仍会选错表**。实测三例：

| 问题 | 过滤后第 1 名 | 问题在哪 |
| --- | --- | --- |
| 首创环保营业收入 | `九、2025年分季度主要财务数据` | 是**分季度**，不是年度合计 |
| 皖通高速经营现金流 | `母公司现金流量表` | 是**母公司**，不是合并 |
| 白云机场营业收入 | `(2).报告分部的财务信息` | 是**分部**数据 |

还有一类是**封面碎片**：问题里带公司名时，封面标题因字面重合而得分偏高。
实测「首创环保2025年的营业收入」的第 1 名就是
``'首创环保集团CAPITAL ECO-PRO GROUP'``（第 1 页封面）。

## 规则设计原则

- **加/减分而非硬过滤**：硬过滤一旦规则写错会直接丢答案，加减分最差只是排序变差
- **规则来自实测**：每一条都能对上上面那些具体反例
- 数值型问题偏向**表格**，定性型问题偏向**正文**
"""

from __future__ import annotations

import re

from .query import QueryIntent

# (正则, 加分, 说明) —— **每组只取第一条命中的规则**
#
# 为什么必须"只取第一条"：``近三年主要会计数据和财务指标`` 里**同时含**
# ``主要会计数据``，两条规则一起命中会叠成 +0.65，实测把无关的财务汇总节
# 顶到了业务描述之上（q008 因此掉到第 2 名）。所以按优先级顺序，
# 命中即止。
SUMMARY_RULES: list[tuple[str, float, str]] = [
    (r"主要会计数据|主要财务指标", +0.35, "汇总表（口径权威、含三年数据）"),
]
STATEMENT_RULES: list[tuple[str, float, str]] = [
    (r"合并资产负债表|合并利润表|合并现金流量表", +0.15, "合并报表"),
]

# (正则, 减分, 说明) —— 同样只取第一条命中
PENALTY_RULES: list[tuple[str, float, str]] = [
    (r"目录|备查文件", -0.40, "目录/备查文件无信息量"),
    # 联营/合营/子公司/关联方的表也是"财务表且含该指标"，同样会拿到指标加分，
    # 但它们是**被投资方或被关联方**的数据，不是本公司口径。
    # 实测这让「白云机场营业收入」的第 1 名变成了联营企业表。
    (r"联营企业|合营企业|子公司|关联方|被投资单位", -0.35, "被投资方/关联方数据 ≠ 本公司"),
    (r"母公司", -0.30, "母公司口径 ≠ 合并口径"),
    (r"分季度|季度主要财务数据", -0.30, "分季度数据 ≠ 年度合计"),
    (r"分部", -0.20, "分部数据 ≠ 整体数据"),
    (r"附注", -0.12, "附注是解释，不是主表"),
    (r"内部控制|社会责任|ESG|环境和社会", -0.10, "非财务主题"),
]

# 主题路由：问题里的**话题词** → 应当去看哪个章节
#
# 实测动机：问「华能国际的审计机构是哪家」时，审计报告正文（满是「审计」字面）
# 压过了真正的《聘任、解聘会计师事务所情况》表，正确 chunk 掉到 15 名开外。
# 这类"问某类信息"的问题，靠字面相似度反而容易被同名但不同用途的段落抢走，
# 必须靠 section 语义来路由。
TOPIC_RULES: list[tuple[str, str, float, str]] = [
    (r"审计机构|会计师事务所|审计师|哪家所",
     r"聘任.{0,4}解聘.{0,4}会计师事务所|会计师事务所情况",
     +0.55, "问会计师事务所 → 定位到聘任/解聘章节"),
    (r"董事长|总经理|总裁|高管|薪酬|年薪",
     r"董事、监事和高级管理人员|高级管理人员.*薪酬|薪酬情况",
     +0.35, "问高管/薪酬 → 定位到董监高章节"),
    (r"股东|持股|十大股东|实际控制人",
     r"股东情况|股份变动|控股股东|实际控制人",
     +0.35, "问股东 → 定位到股东与股份变动章节"),
    (r"分红|股利|利润分配",
     r"利润分配|分红",
     +0.35, "问分红 → 定位到利润分配章节"),
    # 注意**不要**把「经营情况讨论」写进来：它是父级章节名，
    # 其下包含党建、安全、环保等一堆无关小节。
    # 实测它导致「皖通高速的主要业务是什么」第 1 名变成「（六）党建引领」。
    (r"主营业务|经营范围|做什么|主要业务|业务概要",
     r"公司业务概要|主营业务|业务概况|经营范围|公司简介|主要业务",
     +0.30, "问主营业务 → 定位到业务概要/公司简介章节"),
    (r"研发|专利|技术人员",
     r"研发投入|研发人员|专利",
     +0.30, "问研发 → 定位到研发章节"),
    (r"员工|人数|人员构成",
     r"员工情况|在职员工|员工构成",
     +0.30, "问员工 → 定位到员工情况章节"),
]

# 公司名重合导致的封面误命中：第 1 页的短文本
COVER_PAGES = 1
COVER_MAX_BODY = 60


def is_cover_like(c: dict) -> bool:
    """是不是封面/报头碎片？

    判定依据：**第 1 页**的**短正文**。实测「首创环保集团CAPITAL ECO-PRO GROUP」
    这类碎片只有几十个字符，却因为含公司名而排在第一位。
    注意不能用"有无章节"判断 —— MinerU 会把报告大标题标成 level=1 heading，
    所以这类碎片**是有章节的**，之前那版过滤正是因此漏掉了它。
    """
    return (c.get("kind") == "text"
            and c.get("page_start", 99) <= COVER_PAGES
            and len(c.get("body") or "") < COVER_MAX_BODY)


def _first_match(rules, text: str):
    """按优先级返回**第一条**命中的规则（避免叠加重复计分）。"""
    for pat, val, desc in rules:
        if re.search(pat, text):
            return val, desc
    return 0.0, None


def score_adjust(c: dict, intent: QueryIntent) -> tuple[float, list[str]]:
    """返回该 chunk 的分数调整量与命中的规则说明。"""
    adj = 0.0
    why: list[str] = []
    sec = c.get("section") or ""
    q = intent.question

    # 汇总表加分**只在问数字时**才给。
    # 否则问「皖通高速的主要业务是什么」时，财务汇总节会被顶到业务描述之上。
    if intent.metric or intent.is_numeric:
        v, d = _first_match(SUMMARY_RULES, sec)
        if d:
            adj += v
            why.append(f"+{v} {d}")
        v, d = _first_match(STATEMENT_RULES, sec)
        if d:
            adj += v
            why.append(f"+{v} {d}")

    v, d = _first_match(PENALTY_RULES, sec)
    if d:
        adj += v
        why.append(f"{v} {d}")

    # 主题路由
    for qpat, spat, val, desc in TOPIC_RULES:
        if re.search(qpat, q) and re.search(spat, sec):
            adj += val
            why.append(f"+{val} {desc}")
            break

    if is_cover_like(c):
        adj -= 0.50
        why.append("-0.50 封面/报头碎片")

    # 题型与 chunk 类型的匹配
    if intent.is_numeric:
        if c.get("kind") == "table":
            adj += 0.25
            why.append("+0.25 数值问题偏向表格")
        elif c.get("kind") == "text":
            adj -= 0.10
            why.append("-0.10 数值问题不偏向正文")
    else:
        if c.get("kind") == "text":
            adj += 0.12
            why.append("+0.12 定性问题偏向正文")
        else:
            adj -= 0.08
            why.append("-0.08 定性问题不偏向表格")

    # 该表确实含所问指标 → 强加分（这是最可靠的信号）
    if intent.metric and intent.metric in (c.get("metrics") or []):
        adj += 0.30
        why.append(f"+0.30 该表含所问指标「{intent.metric}」")

    return adj, why
