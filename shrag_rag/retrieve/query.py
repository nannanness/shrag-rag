# -*- coding: utf-8 -*-
"""查询理解：从自然语言问题里抽出**路由所需的结构化信息**。

## 为什么需要这一层

实测（10 家试点）表明纯向量检索对「某公司某指标是多少」明显不足：

| 问题 | 纯向量排名 |
| --- | --- |
| 白云机场2025年的营业收入是多少 | 80 |
| 华能国际2025年年报的审计机构是哪家 | 15 |
| 皖通高速2025年经营活动产生的现金流量净额 | 22 |

原因是**叙述性文字把财务表挤下去** —— 管理层讨论里"营业收入同比增长…"
与问题高度相似，而数字其实在表格里。

但只要能从问题里认出**公司**和**指标**，就可以把候选集从数百条缩到个位数：

    白云机场营业收入：候选 571 → 7，正确 chunk 排名 80 → 2

## 三条抽取规则

1. **公司**：6 位代码直接认；否则用已知公司名做**最长匹配**
2. **指标**：用 spec.METRICS 的别名在问题里找，**长别名优先**
   （否则「归母净利润」会被「净利润」先抢走）
3. **问题类型**：数值型 / 定性型 / 事实型，决定后续是否偏向表格
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..spec import METRICS, match_metric, norm_label

_CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")

# 数值型问题的信号词
_NUMERIC_HINTS = ("多少", "是多少", "几", "数值", "金额", "占比", "比例", "增长率",
                  "同比", "最高", "最低", "排名", "前几", "对比", "相比", "哪个更")
# 事实型
_FACT_HINTS = ("哪家", "是什么", "谁", "什么时候", "哪一年", "哪个")

# 排序型：问"哪家/最高/前几"，需要**跨公司**比较
_RANK_HINTS = ("最高", "最低", "最大", "最小", "最多", "最少", "排名", "前几",
               "前三", "前五", "前十", "哪家", "哪些公司", "谁最高", "谁最低")

_YEAR_RE = re.compile(r"((?:19|20)\d{2})\s*年")

# 指标别名按长度降序，保证「归母净利润」先于「净利润」被匹配
_ALIASES: list[tuple[str, str]] = sorted(
    ((a, name) for name, als in METRICS.items() for a in als),
    key=lambda x: -len(x[0]))


@dataclass
class QueryIntent:
    question: str
    code: str | None = None            # 识别出的公司代码
    company: str | None = None         # 识别出的公司简称
    metric: str | None = None          # 识别出的规范化指标名
    qtype: str = "unknown"             # numeric | fact | qualitative
    year: int | None = None            # 问题里提到的年份
    wants_ranking: bool = False        # 是否要**跨公司**排序（"哪家最高"）
    reasons: list[str] = field(default_factory=list)

    @property
    def is_numeric(self) -> bool:
        return self.qtype == "numeric"

    def describe(self) -> str:
        bits = []
        if self.code:
            bits.append(f"公司={self.code}/{self.company}")
        if self.metric:
            bits.append(f"指标={self.metric}")
        bits.append(f"类型={self.qtype}")
        if self.year:
            bits.append(f"年份={self.year}")
        if self.wants_ranking:
            bits.append("→跨公司排序")
        return "  ".join(bits) + (f"   ({'; '.join(self.reasons)})" if self.reasons else "")


def detect_company(question: str, companies: dict[str, str]) -> tuple[str | None, str | None, str]:
    """返回 ``(code, name, 说明)``。

    先看 6 位代码；再按**公司名最长匹配** —— 长名优先是为了避免
    「中国石化」被更短的「石化」之类误配。
    """
    m = _CODE_RE.search(question)
    if m and m.group(1) in companies:
        return m.group(1), companies[m.group(1)], f"命中代码 {m.group(1)}"

    norm = question.replace(" ", "")
    best: tuple[str, str] | None = None
    for code, name in companies.items():
        if not name:
            continue
        n = name.replace(" ", "")
        if n and n in norm:
            if best is None or len(n) > len(best[1].replace(" ", "")):
                best = (code, name)
    if best:
        return best[0], best[1], f"命中公司名「{best[1]}」"
    return None, None, "未识别公司"


def detect_metric(question: str) -> tuple[str | None, str]:
    """用指标别名做匹配，**长别名优先**。

    不直接用 ``match_metric``：那是为**表格行标签**设计的精确/前缀匹配，
    而问题里指标名是嵌在句子中间的（如「白云机场2025年的营业收入是多少」）。
    """
    norm = norm_label(question)
    for alias, name in _ALIASES:
        if alias.replace(" ", "") in norm:
            return name, f"命中指标别名「{alias}」"
    # 兜底：让 match_metric 试一次（它懂「三、利润总额」这类带序号的写法）
    m = match_metric(question)
    return (m, f"match_metric 兜底命中「{m}」") if m else (None, "未识别指标")


def classify(question: str, metric: str | None) -> tuple[str, str]:
    if metric or any(h in question for h in _NUMERIC_HINTS):
        return "numeric", "问题含指标名或数值信号词"
    if any(h in question for h in _FACT_HINTS):
        return "fact", "问题含事实型疑问词"
    return "qualitative", "按定性问题处理"


def analyze(question: str, companies: dict[str, str]) -> QueryIntent:
    """分析问题，得到路由所需的结构化意图。"""
    code, name, r1 = detect_company(question, companies)
    metric, r2 = detect_metric(question)
    qtype, r3 = classify(question, metric)
    m = _YEAR_RE.search(question)
    year = int(m.group(1)) if m else None
    # 跨公司排序：有指标但**没有指定公司**，且问题带排序信号
    wants_ranking = bool(metric and not code
                         and any(h in question for h in _RANK_HINTS))
    if wants_ranking:
        r3 += "；带排序信号且未指定公司 → 需跨公司比较"
    return QueryIntent(question=question, code=code, company=name,
                       metric=metric, qtype=qtype, year=year,
                       wants_ranking=wants_ranking, reasons=[r1, r2, r3])
