# -*- coding: utf-8 -*-
"""PDF 分片：绕开 MinerU 单文件 200 页上限。

实测：600004（215 页）被拒，报
``number of pages exceeds limit (200 pages), please split the file and try again``。
而本批年报页数中位数 234，**75% 都要拆**（100 个请求变 177 个）。

合并时的关键点：分片里 ``content_list.json`` 的 ``page_idx`` 是**片内 0 基**，
合并回全文必须加上该片起始页的偏移，否则引用页码全错。

已知代价：跨片边界的表格会被切成两半（MinerU 在单片内能跨页合并表格）。
本批每份平均 256 张表，边界处最多影响 1 张，属于可接受损失 —— 但要把
边界页记进清单，方便事后核对。
"""

from __future__ import annotations

import logging
from pathlib import Path

from PyPDF2 import PdfReader, PdfWriter

log = logging.getLogger(__name__)

# MinerU 硬上限 200；留 5 页余量，避免双方页数口径不一致导致刚好被拒
DEFAULT_MAX_PAGES = 195


def page_count(pdf: Path) -> int:
    r = PdfReader(str(pdf))
    n = len(r.pages)
    r.stream.close()
    return n


def split_pdf(pdf: Path, out_dir: Path, max_pages: int = DEFAULT_MAX_PAGES
              ) -> list[tuple[Path, int]]:
    """把 ``pdf`` 拆成每片 ≤ ``max_pages`` 页。

    返回 ``[(分片路径, 该片首頁的全文 0 基偏移), ...]``；页数不超限时返回单片
    （偏移 0），此时不复制文件，直接用原文件。
    """
    n = page_count(pdf)
    if n <= max_pages:
        return [(pdf, 0)]

    out_dir.mkdir(parents=True, exist_ok=True)
    reader = PdfReader(str(pdf))
    parts: list[tuple[Path, int]] = []
    for start in range(0, n, max_pages):
        end = min(start + max_pages, n)
        writer = PdfWriter()
        for i in range(start, end):
            writer.add_page(reader.pages[i])
        dst = out_dir / f"{pdf.stem}__p{start // max_pages + 1:02d}_{start}-{end - 1}.pdf"
        with open(dst, "wb") as f:
            writer.write(f)
        parts.append((dst, start))
        log.debug("拆片 %s 页 %d-%d -> %s", pdf.name, start, end - 1, dst.name)

    reader.stream.close()
    log.info("%s 共 %d 页，拆成 %d 片（上限 %d）", pdf.name, n, len(parts), max_pages)
    return parts


def merge_content_lists(parts: list[tuple[int, list[dict]]]) -> list[dict]:
    """把各片的 ``content_list`` 合并成全文的，并修正 ``page_idx``。

    ``parts`` 为 ``[(片首页偏移, 该片的 content_list), ...]``。
    输出顺序即阅读顺序（按偏移升序）。
    """
    merged: list[dict] = []
    for offset, blocks in sorted(parts, key=lambda x: x[0]):
        for b in blocks:
            nb = dict(b)
            if isinstance(nb.get("page_idx"), int):
                nb["page_idx"] += offset
            merged.append(nb)
    return merged
