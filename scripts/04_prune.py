# -*- coding: utf-8 -*-
"""清理解析中间产物，只留下真正有用的部分。

## 背景

MinerU 一次解析会在 ``data/parsed/<code>/`` 留下七八个大文件，其中大部分
我们不使用。全量 100 份时中间产物约 2.4 GB，而真正必需的只有约 250 MB。

## 各文件的去留判断

| 文件 | 判断 | 理由 |
| --- | --- | --- |
| ``doc.json`` | **必须留** | 下游（抽取/切片/检索）唯一入口 |
| ``raw/*_content_list.json`` | **留** | 归一化的唯一输入；留着重跑归一化就不必再调 MinerU |
| ``<batch>.zip`` | 删 | 与 raw/ 重复；体积最大（约 19 MB/份） |
| ``parts/*.pdf`` | 删 | 可由 ``data/pdfs/`` 重新拆出 |
| ``layout.json`` | 删 | 没人用（约 7 MB/份） |
| ``*_model.json`` | 删 | 版面检测器原始输出，没人用 |
| ``*_content_list_v2.json`` | 删 | 另一种 schema，无 page_idx，做不了引用 |
| ``*_origin.pdf`` | 删 | MinerU 回传的输入副本，与 parts/ 重复 |
| ``full.md`` | 删 | 有 ``doc.md`` 替代 |
| ``doc.md`` / ``doc.html`` | 删 | 可随时用 ``02_inspect.py`` 重新导出 |

## 表格切图的悬空引用

``doc.json`` 里非空表格的 ``img_path`` 形如 ``images/xxx.jpg``，但解压时
**刻意跳过了 images/**（否则体积约 20 倍），所以这些路径当前是**悬空的**。

``--extract-table-images`` 会在删 zip 之前，**只把 doc.json 引用到的那些图**
抽到 ``data/table_images/<code>/``，既补上引用又不浪费空间
（整包 images 约 250 张/份，实际被引用的只有几十张）。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PARSED = ROOT / "data" / "parsed"
TABLE_IMG = ROOT / "data" / "table_images"

# 这些名字一律保留
KEEP_EXACT = {"doc.json"}


def classify(p: Path) -> str:
    """返回 ``keep`` / ``drop``。"""
    n = p.name
    if n in KEEP_EXACT:
        return "keep"
    # 归一化的输入，留
    if n.endswith("_content_list.json") and not n.endswith("_content_list_v2.json"):
        return "keep"
    return "drop"


def scan(dry: bool) -> dict:
    stats = {"keep_n": 0, "keep_b": 0, "drop_n": 0, "drop_b": 0}
    by_kind: dict[str, list[int]] = {}

    for code_dir in sorted(PARSED.iterdir()):
        if not code_dir.is_dir():
            continue
        if not (code_dir / "doc.json").exists():
            print(f"  ⚠️ {code_dir.name} 缺 doc.json，跳过（未完成的解析？）")
            continue
        for p in code_dir.rglob("*"):
            if not p.is_file():
                continue
            kind = classify(p)
            size = p.stat().st_size
            if kind == "keep":
                stats["keep_n"] += 1
                stats["keep_b"] += size
            else:
                stats["drop_n"] += 1
                stats["drop_b"] += size
                # 归类统计
                key = _label(p)
                e = by_kind.setdefault(key, [0, 0])
                e[0] += 1
                e[1] += size
                if not dry:
                    p.unlink()
    return {"stats": stats, "by_kind": by_kind}


def _label(p: Path) -> str:
    n = p.name
    if n.endswith(".zip"):
        return "<batch>.zip"
    if n == "layout.json":
        return "layout.json"
    if n.endswith("_model.json"):
        return "*_model.json"
    if n.endswith("_content_list_v2.json"):
        return "*_content_list_v2.json"
    if n.endswith("_origin.pdf"):
        return "*_origin.pdf"
    if n == "full.md":
        return "full.md"
    if n in ("doc.md", "doc.html"):
        return n
    if n.endswith(".pdf"):
        return "分片 PDF (parts/)"
    return "其它"


def extract_table_images() -> tuple[int, float]:
    """把 doc.json 引用到的表格切图从 zip 里抽出来。返回 (张数, MB)。"""
    n_ok = n_miss = 0
    total = 0
    for code_dir in sorted(PARSED.iterdir()):
        dj = code_dir / "doc.json"
        if not dj.is_file():
            continue
        doc = json.loads(dj.read_text(encoding="utf-8"))
        wants: set[str] = set()
        for b in doc.get("blocks", []):
            ip = b.get("img_path")
            if b.get("kind") == "table" and ip:
                wants.add(ip.replace("\\", "/"))
        if not wants:
            continue
        outdir = TABLE_IMG / code_dir.name
        outdir.mkdir(parents=True, exist_ok=True)
        for zp in sorted(code_dir.rglob("*.zip")):
            try:
                with zipfile.ZipFile(zp) as z:
                    names = set(z.namelist())
                    for want in list(wants):
                        if want not in names:
                            continue
                        data = z.read(want)
                        dst = outdir / Path(want).name
                        dst.write_bytes(data)
                        total += len(data)
                        n_ok += 1
                        wants.discard(want)
            except Exception as e:                       # noqa: BLE001
                print(f"  ⚠️ {zp.name} 读取失败 {type(e).__name__}: {e}")
        n_miss += len(wants)
    return n_ok, total / 1e6, n_miss


def main() -> int:
    ap = argparse.ArgumentParser(description="清理解析中间产物")
    ap.add_argument("--apply", action="store_true", help="真的删除（默认只预览）")
    ap.add_argument("--extract-table-images", action="store_true",
                    help="删 zip 前，先抽出 doc.json 引用到的表格切图")
    args = ap.parse_args()

    if not PARSED.exists():
        print(f"没有 {PARSED}")
        return 1

    before = sum(f.stat().st_size for f in PARSED.rglob("*") if f.is_file())
    print(f"清理前 data/parsed 体积: {before / 2**30:.2f} GB"
          f"（{'真实删除' if args.apply else '预览模式，未改动任何文件'}）\n")

    if args.extract_table_images:
        print("先抽表格切图 …")
        n, mb, miss = extract_table_images()
        print(f"  抽出 {n} 张 / {mb:.1f} MB   未找到 {miss} 张")
        print(f"  存放于 {TABLE_IMG}\n")

    r = scan(dry=not args.apply)
    s = r["stats"]
    print("按类型（将被删除的）:")
    for k, (c, b) in sorted(r["by_kind"].items(), key=lambda x: -x[1][1]):
        print(f"  {k:<28} {c:>5} 个  {b / 2**20:>9.1f} MB")

    print(f"\n保留 {s['keep_n']:>5} 个  {s['keep_b'] / 2**20:>9.1f} MB")
    print(f"删除 {s['drop_n']:>5} 个  {s['drop_b'] / 2**20:>9.1f} MB")
    if args.apply:
        after = sum(f.stat().st_size for f in PARSED.rglob("*") if f.is_file())
        print(f"\n清理后 data/parsed 体积: {after / 2**30:.2f} GB（实际）")
    else:
        # dry-run 下没真删，必须按预计值算，否则会显示成"清理前后一样"
        print(f"\n清理后 data/parsed 体积: "
              f"{(before - s['drop_b']) / 2**30:.2f} GB（预计）")
        print("\n这是预览。确认无误后加 --apply 执行。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
