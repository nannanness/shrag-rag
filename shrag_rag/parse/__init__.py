# -*- coding: utf-8 -*-
"""解析层：PDF → ParsedDoc。

对外只暴露 ``ParsedDoc`` / ``Block`` 和 ``parse_pdfs()``，
MinerU 只是当前选用的后端，下游不应依赖它。
"""

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
from .verify import (KEY_METRICS, MetricHit, check_report, extract_metrics,  # noqa: F401
                     find_suspicious, parse_number, severity_counts)

__all__ = [
    "MineruClient", "MineruError", "FileResult", "get_token",
    "split_pdf", "merge_content_lists", "page_count", "DEFAULT_MAX_PAGES",
    "Block", "ParsedDoc", "from_mineru_content_list",
    "parse_html_table", "table_to_markdown", "table_to_tsv",
    "doc_to_markdown", "doc_to_html",
    "extract_metrics", "check_report", "MetricHit", "KEY_METRICS",
    "find_suspicious", "severity_counts", "parse_number",
    "KIND_TEXT", "KIND_HEADING", "KIND_TABLE", "KIND_IMAGE",
]
