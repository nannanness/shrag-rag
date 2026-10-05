# -*- coding: utf-8 -*-
"""解析层的统一数据模型。

设计要点：这一层**不暴露任何 MinerU 细节**。无论后面换成 PyMuPDF、pdfplumber
还是本地 MinerU，下游（切片 / 索引 / 检索）只认这里的 ``ParsedDoc``。

几个关键决定：
1. ``page_idx`` 是 **0 基**，与 MinerU 一致；对外展示时 +1。引用溯源全靠它。
2. 页眉（running header）和页码**在解析层就丢掉** —— 它们对检索没有信息量，
   却会污染切片（实测朴素抽取会产出 ``48/134二、财务报表`` 这种粘连串）。
3. 表格用 ``html`` 字段原样保留。MinerU 输出的是 HTML ``<table>``，不是 Markdown，
   所以任何"按 | 切分"的想法都是错的。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path

# 块类型（我们的口径，不是 MinerU 的）
KIND_TEXT = "text"
KIND_HEADING = "heading"
KIND_TABLE = "table"
KIND_IMAGE = "image"

# MinerU 的类型 → 我们的类型。None 表示丢弃
MINERU_KIND_MAP = {
    "text": KIND_TEXT,
    "header": None,          # 页眉，丢弃
    "page_number": None,     # 页码，丢弃
    "page_footnote": None,   # 页脚，丢弃
    "table": KIND_TABLE,
    "image": KIND_IMAGE,
    "equation": KIND_TEXT,
}


@dataclass
class Block:
    """一个内容块。"""
    kind: str                       # text / heading / table / image
    page_idx: int                   # 0 基页码
    text: str = ""                  # 文本或标题内容
    html: str = ""                  # 表格的 HTML
    level: int | None = None        # 标题层级（kind == heading 时有意义）
    caption: list[str] = field(default_factory=list)
    footnote: list[str] = field(default_factory=list)
    bbox: list[float] | None = None
    img_path: str | None = None

    @property
    def page(self) -> int:
        """1 基页码，用于展示和引用。"""
        return self.page_idx + 1

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ParsedDoc:
    """一份解析完成的年报。"""
    code: str
    name: str
    report_year: int
    pdf_name: str
    source_pdf: str                 # 相对 shrag 项目根
    parser: str = "mineru"
    blocks: list[Block] = field(default_factory=list)
    parse_seconds: float = 0.0
    raw_zip: str | None = None

    # -- 统计 -------------------------------------------------------------- #

    @property
    def n_blocks(self) -> int:
        return len(self.blocks)

    def count(self, kind: str) -> int:
        return sum(1 for b in self.blocks if b.kind == kind)

    @property
    def n_pages(self) -> int:
        return (max(b.page_idx for b in self.blocks) + 1) if self.blocks else 0

    def stats(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "n_pages": self.n_pages,
            "n_blocks": self.n_blocks,
            "n_text": self.count(KIND_TEXT),
            "n_heading": self.count(KIND_HEADING),
            "n_table": self.count(KIND_TABLE),
            "n_image": self.count(KIND_IMAGE),
            "chars": sum(len(b.text) for b in self.blocks)
                     + sum(len(b.html) for b in self.blocks),
        }

    def to_dict(self) -> dict:
        d = asdict(self)
        d["stats"] = self.stats()
        return d

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=1),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: Path) -> "ParsedDoc":
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        d.pop("stats", None)
        blocks = [Block(**b) for b in d.pop("blocks", [])]
        return cls(blocks=blocks, **d)


def from_mineru_content_list(data: list[dict], **meta) -> ParsedDoc:
    """把 MinerU 的 ``content_list.json`` 归一化成 ``ParsedDoc``。

    MinerU 每个块形如::

        {"type": "text",  "text": "...", "bbox": [...], "page_idx": 0}
        {"type": "table", "table_body": "<table>...", "table_caption": [],
         "table_footnote": [], "img_path": "images/xx.jpg", "page_idx": 2}
        {"type": "page_number", "text": "1 / 134", "page_idx": 0}   ← 丢弃
    """
    blocks: list[Block] = []
    for b in data:
        kind = MINERU_KIND_MAP.get(b.get("type"))
        if kind is None:
            continue

        page_idx = b.get("page_idx")
        if page_idx is None:                     # 没有页码的块不要，引用会断
            continue

        if kind == KIND_TABLE:
            blocks.append(Block(
                kind=kind,
                page_idx=page_idx,
                html=b.get("table_body") or "",
                caption=list(b.get("table_caption") or []),
                footnote=list(b.get("table_footnote") or []),
                bbox=b.get("bbox"),
                img_path=b.get("img_path"),
            ))
            continue

        if kind == KIND_IMAGE:
            blocks.append(Block(
                kind=kind,
                page_idx=page_idx,
                caption=list(b.get("image_caption") or []),
                footnote=list(b.get("image_footnote") or []),
                bbox=b.get("bbox"),
                img_path=b.get("img_path"),
            ))
            continue

        # text / equation —— 带 text_level 的算标题
        text = (b.get("text") or "").strip()
        if not text:
            continue
        level = b.get("text_level")
        blocks.append(Block(
            kind=KIND_HEADING if level else KIND_TEXT,
            page_idx=page_idx,
            text=text,
            level=level,
            bbox=b.get("bbox"),
        ))

    return ParsedDoc(blocks=blocks, **meta)
