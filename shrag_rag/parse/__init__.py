# -*- coding: utf-8 -*-
"""解析层：PDF → ParsedDoc，外加解析质量验证。

对外只暴露 ``ParsedDoc`` / ``Block`` 和几个工具函数；
MinerU 只是当前选用的后端，下游不应依赖它。

指标口径统一来自 ``shrag_rag/spec.py``（与抽取层同一份）。
"""

from ..spec import (CORE_METRICS, METRICS, METRIC_ORDER,  # noqa: F401
                    match_metric, norm_label, parse_number)
from .export import doc_to_html, doc_to_markdown
from .htmltable import parse_html_table, table_to_markdown, table_to_tsv
from .mineru import FileResult, MineruClient, MineruError, get_token
from .pdfsplit import DEFAULT_MAX_PAGES, merge_content_lists, page_count, split_pdf
from .schema import (
    KIND_HEADING,
    KIND_IMAGE,
    KIND_TABLE,
    KIND_TEXT,
    Block,
    ParsedDoc,
    from_mineru_content_list,
)
from .verify import check_report, extract_metrics, find_suspicious, severity_counts

__all__ = [
    "MineruClient", "MineruError", "FileResult", "get_token",
    "split_pdf", "merge_content_lists", "page_count", "DEFAULT_MAX_PAGES",
    "Block", "ParsedDoc", "from_mineru_content_list",
    "parse_html_table", "table_to_markdown", "table_to_tsv",
    "doc_to_markdown", "doc_to_html",
    "check_report", "extract_metrics", "find_suspicious", "severity_counts",
    "CORE_METRICS", "METRICS", "METRIC_ORDER",
    "match_metric", "norm_label", "parse_number",
    "KIND_TEXT", "KIND_HEADING", "KIND_TABLE", "KIND_IMAGE",
]
