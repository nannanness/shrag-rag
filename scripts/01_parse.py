# -*- coding: utf-8 -*-
"""阶段 1：用 MinerU 解析年报 PDF（自动处理 200 页上限）。

用法::

    python scripts/01_parse.py --limit 10          # 试点 10 家
    python scripts/01_parse.py --codes 600108,600000
    python scripts/01_parse.py --limit 0           # 全量（已有产物自动跳过）

为什么会有"分片"这层：MinerU 单文件硬上限 200 页，而本批年报页数中位数 234，
**75% 超限**（100 份 → 177 个请求）。所以超限的先本地拆片，解析完再按页偏移合并。

产出::

    data/parsed/<code>/doc.json        归一化 ParsedDoc（下游只用这个）
    data/parsed/<code>/raw/...         MinerU 原始产物
    data/parsed/<code>/parts/*.pdf     拆出来的分片（可删）
    data/meta/parse_manifest.csv       每份的耗时/页数/块数/表格数
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from shrag_rag.parse import (MineruClient, from_mineru_content_list,  # noqa: E402
                             merge_content_lists, page_count, split_pdf)
from shrag_rag.parse.mineru import MAX_FILES_PER_BATCH                # noqa: E402
from shrag_rag.parse.pdfsplit import DEFAULT_MAX_PAGES                # noqa: E402

SHARG_ROOT = Path(r"D:\Project\shrag")
SHARG_META = SHARG_ROOT / "data" / "meta" / "annual_reports.csv"
SHARG_PDF_ROOT = SHARG_ROOT / "data" / "pdfs"

DATA = ROOT / "data"
PARSED = DATA / "parsed"
META = DATA / "meta"
LOGS = ROOT / "logs"

MANIFEST_FIELDS = ["code", "name", "pdf_name", "state", "n_pages", "n_parts",
                   "boundaries", "blocks", "text", "heading", "table", "image",
                   "chars", "seconds", "err_msg"]


def setup_logging(verbose: bool) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(LOGS / "parse.log", encoding="utf-8")],
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def load_reports(limit: int, codes: list[str] | None) -> list[dict]:
    if not SHARG_META.exists():
        raise SystemExit(f"找不到 {SHARG_META}，先跑 shrag 的采集脚本")
    with open(SHARG_META, encoding="utf-8-sig", newline="") as f:
        rows = [r for r in csv.DictReader(f) if r.get("status") == "ok"]
    if codes:
        want = {c.strip() for c in codes if c.strip()}
        rows = [r for r in rows if r["code"] in want]
        missing = want - {r["code"] for r in rows}
        if missing:
            logging.warning("元数据里没有：%s", ",".join(sorted(missing)))
    return rows[:limit] if limit else rows


def build_tasks(reports: list[dict], max_pages: int) -> tuple[list[dict], list[dict]]:
    """把报告展开成"每片一个提交任务"。返回 (任务列表, 直接失败的行)。"""
    tasks: list[dict] = []
    failed: list[dict] = []
    for r in reports:
        code = r["code"]
        pdf = SHARG_PDF_ROOT / code / r["pdf_name"]
        row = {**{k: "" for k in MANIFEST_FIELDS}, "code": code,
               "name": r.get("name", ""), "pdf_name": r["pdf_name"]}
        if not pdf.exists():
            row["state"] = "pdf_missing"
            failed.append(row)
            logging.warning("%s PDF 不存在：%s", code, pdf)
            continue
        try:
            n = page_count(pdf)
            parts = split_pdf(pdf, PARSED / code / "parts", max_pages)
        except Exception as e:                           # noqa: BLE001
            row["state"] = "split_failed"
            row["err_msg"] = f"{type(e).__name__}: {e}"
            failed.append(row)
            logging.exception("%s 拆片失败", code)
            continue

        bounds = [off for _, off in parts][1:]           # 分片边界（可能切断表格的页）
        for k, (ppath, off) in enumerate(parts):
            tasks.append({
                "data_id": f"{code}__p{k}" if len(parts) > 1 else code,
                "code": code, "part": k, "offset": off, "path": ppath,
                "n_pages": n, "n_parts": len(parts), "boundaries": bounds,
                "row": row,
            })
    return tasks, failed


def main() -> int:
    ap = argparse.ArgumentParser(description="MinerU 解析年报 PDF")
    ap.add_argument("--limit", type=int, default=10, help="取前 N 份（0 = 全部）")
    ap.add_argument("--codes", type=str, default=None, help="指定代码，逗号分隔")
    ap.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES,
                    help=f"单片页数上限（MinerU 硬上限 200，默认 {DEFAULT_MAX_PAGES}）")
    ap.add_argument("--force", action="store_true", help="重解析已有产物")
    ap.add_argument("--keep-images", action="store_true", help="保留 images/")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    setup_logging(args.verbose)
    codes = args.codes.split(",") if args.codes else None
    reports = load_reports(args.limit, codes)
    todo = [r for r in reports if args.force or not (PARSED / r["code"] / "doc.json").exists()]
    logging.info("待解析 %d 份（跳过已完成 %d 份）", len(todo), len(reports) - len(todo))
    if not todo:
        logging.info("没有要处理的，退出")
        return 0

    PARSED.mkdir(parents=True, exist_ok=True)
    META.mkdir(parents=True, exist_ok=True)

    t_all = time.time()
    tasks, manifest = build_tasks(todo, args.max_pages)
    if not tasks:
        logging.error("没有可提交的任务")
    else:
        n_split = sum(1 for t in tasks if t["n_parts"] > 1)
        logging.info("共 %d 个提交任务（其中 %d 个来自需拆片的报告）", len(tasks), n_split)

    client = MineruClient()
    # data_id -> [(offset, content_list)]，用于合并
    collected: dict[str, list[tuple[int, list[dict]]]] = {}
    part_results: dict[str, tuple[str, str]] = {}       # data_id -> (state, err)
    t_submit: dict[str, float] = {}                     # 每个任务的提交时刻
    t_finish: dict[str, float] = {}                     # 每个任务的完成时刻

    # ---- 分批提交 + 轮询 ------------------------------------------------ #
    for bi in range(0, len(tasks), MAX_FILES_PER_BATCH):
        chunk = tasks[bi:bi + MAX_FILES_PER_BATCH]
        t0 = time.time()
        try:
            batch_id = client.submit_batch([(t["data_id"], t["path"]) for t in chunk])
            for t in chunk:
                t_submit[t["data_id"]] = time.time()
        except Exception as e:                           # noqa: BLE001
            logging.exception("批次提交失败")
            for t in chunk:
                part_results[t["data_id"]] = ("submit_failed", f"{type(e).__name__}: {e}")
            continue

        def on_progress(res, _t0=t0, _n=len(chunk)):
            done = sum(1 for x in res if x.state in ("done", "failed"))
            logging.info("  进度 %d/%d  (%.0fs)", done, _n, time.time() - _t0)

        results = client.poll_batch(batch_id, timeout=7200, on_progress=on_progress)
        by_id = {x.data_id: x for x in results}
        logging.info("批次 %s 完成，%d 个结果，耗时 %.0fs",
                     batch_id, len(results), time.time() - t0)

        for t in chunk:
            did = t["data_id"]
            t_finish[did] = time.time()
            fr = by_id.get(did)
            if fr is None or fr.state != "done" or not fr.zip_url:
                part_results[did] = (fr.state if fr else "no_result",
                                     fr.err_msg if fr else "没有返回结果")
                continue
            try:
                outdir = PARSED / t["code"] / "parts_raw" / f"p{t['part']}"
                zpath = outdir / f"{batch_id}.zip"
                client.download_zip(fr.zip_url, zpath)
                client.unzip(zpath, outdir / "raw", keep_images=args.keep_images)
                cl = client.find_content_list(outdir / "raw")
                if cl is None:
                    raise RuntimeError("zip 里没有 content_list.json")
                blocks = json.loads(cl.read_text(encoding="utf-8"))
                collected.setdefault(t["code"], []).append((t["offset"], blocks))
                part_results[did] = ("ok", "")
            except Exception as e:                       # noqa: BLE001
                part_results[did] = ("error", f"{type(e).__name__}: {e}")
                logging.exception("%s 产物处理失败", did)

    # ---- 按公司合并 + 归一化 -------------------------------------------- #
    by_code: dict[str, list[dict]] = {}
    for t in tasks:
        by_code.setdefault(t["code"], []).append(t)

    for code, ts in by_code.items():
        t0 = ts[0]
        row = dict(t0["row"])
        row["n_pages"] = t0["n_pages"]
        row["n_parts"] = t0["n_parts"]
        row["boundaries"] = ",".join(str(b) for b in t0["boundaries"])

        # 该公司的解析耗时 = 最早提交到最晚完成
        subs = [t_submit[t["data_id"]] for t in ts if t["data_id"] in t_submit]
        fins = [t_finish[t["data_id"]] for t in ts if t["data_id"] in t_finish]
        row["seconds"] = round(max(fins) - min(subs), 1) if subs and fins else ""

        states = [part_results.get(t["data_id"], ("missing", "")) for t in ts]
        if any(s != "ok" for s, _ in states):
            bad = [(t["data_id"], s, e) for t, (s, e) in zip(ts, states) if s != "ok"]
            row["state"] = "failed"
            row["err_msg"] = "; ".join(f"{d}:{s}:{e}" for d, s, e in bad)[:300]
            manifest.append(row)
            logging.error("%s 解析失败：%s", code, row["err_msg"])
            continue

        try:
            merged = merge_content_lists(collected[code])
            doc = from_mineru_content_list(
                merged, code=code, name=t0["row"]["name"],
                report_year=0, pdf_name=t0["row"]["pdf_name"],
                source_pdf=f"data/pdfs/{code}/{t0['row']['pdf_name']}",
                parse_seconds=row["seconds"] or 0.0,
            )
            doc.save(PARSED / code / "doc.json")
            st = doc.stats()
            row.update(state="ok", blocks=st["n_blocks"], text=st["n_text"],
                       heading=st["n_heading"], table=st["n_table"],
                       image=st["n_image"], chars=st["chars"])
            logging.info("  ✅ %s %s  %d页/%d片 %d块(表%d) %d字符  %.0fs",
                         code, t0["row"]["name"], t0["n_pages"], t0["n_parts"],
                         st["n_blocks"], st["n_table"], st["chars"],
                         row["seconds"] or 0)
        except Exception as e:                           # noqa: BLE001
            row["state"] = "merge_failed"
            row["err_msg"] = f"{type(e).__name__}: {e}"
            logging.exception("%s 合并失败", code)
        manifest.append(row)

    # ---- 写清单 --------------------------------------------------------- #
    mpath = META / "parse_manifest.csv"
    done_codes = {m["code"] for m in manifest}
    old: list[dict] = []
    if mpath.exists():
        with open(mpath, encoding="utf-8-sig", newline="") as f:
            old = [x for x in csv.DictReader(f) if x.get("code") not in done_codes]
    with open(mpath, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=MANIFEST_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(old + manifest)

    # ---- 汇总 ----------------------------------------------------------- #
    ok = [m for m in manifest if m.get("state") == "ok"]
    bad = [m for m in manifest if m.get("state") != "ok"]
    pg = sum(int(m["n_pages"] or 0) for m in ok)
    tb = sum(int(m["table"] or 0) for m in ok)
    ch = sum(int(m["chars"] or 0) for m in ok)
    wall = time.time() - t_all
    rate = pg / wall if wall else 0

    print("\n" + "=" * 72)
    print("解析汇总")
    print("=" * 72)
    print(f"  成功 / 失败      {len(ok)} / {len(bad)}")
    print(f"  提交任务数       {len(tasks)}")
    print(f"  总页数           {pg}")
    print(f"  总表格数         {tb}")
    print(f"  总字符数         {ch:,}")
    if ok:
        print(f"  平均             {pg / len(ok):.0f} 页/份，{tb / len(ok):.0f} 表/份，"
              f"{ch / max(pg, 1):.0f} 字符/页")
    print(f"  墙上时间         {wall / 60:.1f} 分钟   吞吐 {rate:.2f} 页/秒")
    if rate:
        print(f"  外推全量         {24224} 页约需 {24224 / rate / 60:.0f} 分钟")
    print(f"  清单             {mpath}")
    if bad:
        print("\n  失败的：")
        for m in bad:
            print(f"    {m['code']} {m.get('name', ''):<8} {m.get('state')}  "
                  f"{(m.get('err_msg') or '')[:70]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
