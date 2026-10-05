# -*- coding: utf-8 -*-
"""shrag-rag —— 上交所年报 RAG 系统。

与 ``D:\\Project\\shrag`` 的分工：
    shrag      数据准备（爬取年报 PDF，产出 data/meta/annual_reports.csv）
    shrag-rag  解析 / 抽取 / 切片 / 索引 / 检索 / 问答

当前进度：阶段 1 解析。
"""

from .parse import Block, MineruClient, ParsedDoc, from_mineru_content_list

__all__ = ["MineruClient", "ParsedDoc", "Block", "from_mineru_content_list"]
__version__ = "0.1.0"
