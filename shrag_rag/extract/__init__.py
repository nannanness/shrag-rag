# -*- coding: utf-8 -*-
"""抽取层：从 ParsedDoc 里抽结构化财务指标。

指标口径来自 ``shrag_rag/spec.py``，与解析验证层共用同一份定义。
"""

from ..spec import (CORE_METRICS, METRICS, METRIC_ORDER,  # noqa: F401
                    is_statement_table, is_summary_table, match_metric)
from .metrics import (HEADER_LABELS, MetricValue, SummaryRow,  # noqa: F401
                      extract_company, find_summary_blocks,
                      parse_summary_table)

__all__ = [
    "extract_company", "find_summary_blocks", "parse_summary_table",
    "MetricValue", "SummaryRow", "HEADER_LABELS",
    "METRICS", "METRIC_ORDER", "CORE_METRICS",
    "match_metric", "is_summary_table", "is_statement_table",
]
