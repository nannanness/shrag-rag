# -*- coding: utf-8 -*-
"""把 ParsedDoc 导出成人能看的格式，用于人工抽检解析质量。

两种产物：
- **Markdown**：``<!-- page:N -->`` 注释标页，表格转成管道表。便于 grep / diff。
- **HTML**：每页一个带边框的小节，表格按原样渲染。便于和原始 PDF 并排比对。
"""

from __future__ import annotations

import html as _html

from .htmltable import parse_html_table, table_to_markdown
from .schema import KIND_HEADING, KIND_IMAGE, KIND_TABLE, ParsedDoc

def doc_to_markdown(doc: ParsedDoc, page_markers: bool = True) -> str:
    out: list[str] = [f"# {doc.name}（{doc.code}）{doc.pdf_name}", ""]
    cur_page = -1
    for b in doc.blocks:
        if page_markers and b.page_idx != cur_page:
            cur_page = b.page_idx
            out.append(f"\n<!-- page:{b.page} -->\n")
        if b.kind == KIND_HEADING:
            lvl = min(max(int(b.level or 2), 2), 6)
            out.append("#" * lvl + " " + b.text)
        elif b.kind == KIND_TABLE:
            cap = " ".join(b.caption).strip()
            if cap:
                out.append(f"**表：{cap}**")
            md = table_to_markdown(b.html)
            out.append(md if md else "_(空表格)_")
            for f in b.footnote:
                out.append(f"> 注：{f}")
        elif b.kind == KIND_IMAGE:
            cap = " ".join(b.caption).strip()
            out.append(f"_(图片{('：' + cap) if cap else ''})_")
        else:
            out.append(b.text)
        out.append("")
    return "\n".join(out)


def doc_to_html(doc: ParsedDoc) -> str:
    parts: list[str] = [
        "<!doctype html><meta charset='utf-8'>",
        f"<title>{_html.escape(doc.name)} {doc.code}</title>",
        """<style>
        body{font-family:"Microsoft YaHei",sans-serif;max-width:1100px;margin:24px auto;
             line-height:1.7;color:#222}
        h1{border-bottom:2px solid #333;padding-bottom:8px}
        .page{border:1px solid #ddd;border-left:4px solid #4a90d9;margin:18px 0;
              padding:10px 16px;background:#fafafa}
        .ptag{color:#4a90d9;font-weight:700;font-size:13px;letter-spacing:1px}
        table{border-collapse:collapse;margin:10px 0;font-size:13px;background:#fff}
        td,th{border:1px solid #bbb;padding:3px 7px;vertical-align:top}
        .cap{color:#666;font-size:13px;font-style:italic}
        .img{color:#999;font-size:13px}
        .stale{color:#c00}
        </style>""",
        f"<h1>{_html.escape(doc.name)}（{doc.code}）</h1>",
        f"<p class='cap'>{_html.escape(doc.pdf_name)} &nbsp;|&nbsp; "
        f"{doc.n_pages} 页 / {doc.n_blocks} 块 / {doc.count(KIND_TABLE)} 表</p>",
    ]
    cur = -1
    for b in doc.blocks:
        if b.page_idx != cur:
            if cur >= 0:
                parts.append("</div>")
            cur = b.page_idx
            parts.append(f"<div class='page'><div class='ptag'>第 {b.page} 页</div>")
        if b.kind == KIND_HEADING:
            lvl = min(max(int(b.level or 2), 2), 5)
            parts.append(f"<h{lvl}>{_html.escape(b.text)}</h{lvl}>")
        elif b.kind == KIND_TABLE:
            cap = " ".join(b.caption).strip()
            if cap:
                parts.append(f"<div class='cap'>表：{_html.escape(cap)}</div>")
            parts.append(b.html or "<div class='stale'>(空表格)</div>")
            for f in b.footnote:
                parts.append(f"<div class='cap'>注：{_html.escape(f)}</div>")
        elif b.kind == KIND_IMAGE:
            cap = " ".join(b.caption).strip()
            parts.append(f"<div class='img'>(图片{('：' + _html.escape(cap)) if cap else ''})</div>")
        else:
            parts.append(f"<p>{_html.escape(b.text)}</p>")
    if cur >= 0:
        parts.append("</div>")
    return "\n".join(parts)
