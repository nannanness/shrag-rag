# -*- coding: utf-8 -*-
"""MinerU 在线 API v4 客户端。

实测确认的接口行为（不是抄文档，是真打过）::

    POST {base}/file-urls/batch                    申请上传链接（支持本地文件！）
      body  {"enable_formula": true, "enable_table": true, "language": "ch",
             "model_version": "pipeline",
             "files": [{"name": "...", "is_ocr": false, "data_id": "..."}]}
      resp  {"code": 0, "data": {"batch_id": "...", "file_urls": ["https://...oss..."]}}

    PUT  <file_urls[i]>                            直传 PDF 字节（预签名，不用带 token）
    GET  {base}/extract-results/batch/{batch_id}   轮询
      resp  {"data": {"extract_result": [{"file_name","state","full_zip_url",
                                          "err_msg","data_id"}]}}
    GET  <full_zip_url>                            下载产物 zip

注意两点（第 12 章那份老代码在这里是错的 / 不必要的）：
1. 老代码走 ``extract/task`` 且要求 PDF 先有**公网 URL**。v4 的 ``file-urls/batch``
   直接支持本地文件，不必先传 OSS。
2. 老代码写死 ``is_ocr: true``。本批年报实测全是**文本型 PDF**，OCR 只会白白
   拉长耗时、消耗配额，所以这里默认 ``is_ocr=False``。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

import requests

log = logging.getLogger(__name__)

BASE = "https://mineru.net/api/v4"
DEFAULT_MODEL = "pipeline"      # pipeline 后端；vlm 也可用，实测两者都返回 code 0
MAX_FILES_PER_BATCH = 20        # 保守值，避免单批过大


class MineruError(RuntimeError):
    pass


def get_token(explicit: str | None = None) -> str:
    """取 token：显式传入 > 进程环境变量 > Windows User 级环境变量。"""
    if explicit:
        return explicit
    tok = os.environ.get("MINERU_API_KEY", "").strip()
    if tok:
        return tok
    # DSH 的 pwsh 进程不继承 User 级变量，所以这里兜一层
    try:
        import subprocess
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "[Environment]::GetEnvironmentVariable('MINERU_API_KEY','User')"],
            capture_output=True, text=True, timeout=20)
        tok = (out.stdout or "").strip()
    except Exception as e:                       # noqa: BLE001
        log.debug("读取 User 级环境变量失败: %s", e)
    if not tok:
        raise MineruError("没找到 MINERU_API_KEY（进程环境和 User 级都没有）")
    return tok


@dataclass
class FileResult:
    data_id: str
    file_name: str
    state: str                  # pending / running / done / failed
    zip_url: str | None = None
    err_msg: str = ""


class MineruClient:
    def __init__(self, token: str | None = None, base: str = BASE,
                 model_version: str = DEFAULT_MODEL, language: str = "ch",
                 is_ocr: bool = False, enable_formula: bool = True,
                 enable_table: bool = True, timeout: int = 60, retries: int = 3):
        self.token = get_token(token)
        self.base = base.rstrip("/")
        self.model_version = model_version
        self.language = language
        self.is_ocr = is_ocr
        self.enable_formula = enable_formula
        self.enable_table = enable_table
        self.timeout = timeout
        self.retries = retries

    # -- 基础设施 ---------------------------------------------------------- #

    @property
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json", "Accept": "*/*"}

    def _req(self, method: str, url: str, **kw) -> requests.Response:
        last: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                r = requests.request(method, url, timeout=self.timeout, **kw)
                if r.status_code < 500:
                    return r
                last = MineruError(f"HTTP {r.status_code}: {r.text[:200]}")
            except requests.RequestException as e:
                last = e
            if attempt < self.retries:
                time.sleep(2 * attempt)
        raise MineruError(f"{method} {url} 失败（{self.retries} 次）：{last}")

    def _post_json(self, path: str, payload: dict) -> dict:
        r = self._req("POST", f"{self.base}{path}", headers=self._headers, json=payload)
        try:
            d = r.json()
        except ValueError:
            raise MineruError(f"非 JSON 响应 HTTP {r.status_code}: {r.text[:300]}")
        if d.get("code") != 0:
            raise MineruError(f"接口返回错误 code={d.get('code')} msg={d.get('msg')}")
        return d["data"]

    # -- 批量提交 ---------------------------------------------------------- #

    def submit_batch(self, items: list[tuple[str, Path]]) -> str:
        """``items`` 为 ``[(data_id, pdf_path), ...]``；返回 batch_id。

        包含两步：申请预签名链接 → 逐个 PUT 上传。
        """
        if not items:
            raise ValueError("items 为空")
        if len(items) > MAX_FILES_PER_BATCH:
            raise ValueError(f"单批最多 {MAX_FILES_PER_BATCH} 个文件，收到 {len(items)}")

        payload = {
            "enable_formula": self.enable_formula,
            "enable_table": self.enable_table,
            "language": self.language,
            "model_version": self.model_version,
            "files": [{"name": p.name, "is_ocr": self.is_ocr, "data_id": did}
                      for did, p in items],
        }
        data = self._post_json("/file-urls/batch", payload)
        batch_id = data["batch_id"]
        urls = data["file_urls"]
        if len(urls) != len(items):
            raise MineruError(f"链接数 {len(urls)} 与文件数 {len(items)} 不符")

        for (did, path), url in zip(items, urls):
            self._put_file(url, path, did)
        log.info("批次 %s 已上传 %d 个文件", batch_id, len(items))
        return batch_id

    def _put_file(self, url: str, path: Path, data_id: str) -> None:
        last: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                with open(path, "rb") as f:
                    r = requests.put(url, data=f, timeout=600)
                if r.status_code in (200, 201, 204):
                    log.debug("上传成功 %s (%.1f MB)", path.name, path.stat().st_size / 1e6)
                    return
                last = MineruError(f"HTTP {r.status_code}: {r.text[:200]}")
            except requests.RequestException as e:
                last = e
            if attempt < self.retries:
                time.sleep(2 * attempt)
        raise MineruError(f"上传 {data_id} ({path.name}) 失败：{last}")

    # -- 轮询 -------------------------------------------------------------- #

    def poll_batch(self, batch_id: str, timeout: float = 3600,
                   interval: float = 8.0, on_progress=None) -> list[FileResult]:
        """轮询到全部结束（done / failed）或超时。"""
        deadline = time.time() + timeout
        last_snapshot = None
        while True:
            r = self._req("GET", f"{self.base}/extract-results/batch/{batch_id}",
                          headers=self._headers)
            body = r.json()
            if body.get("code") != 0:
                raise MineruError(f"轮询失败 code={body.get('code')} msg={body.get('msg')}")
            raw = (body.get("data") or {}).get("extract_result") or []
            results = [FileResult(
                data_id=str(x.get("data_id") or ""),
                file_name=x.get("file_name") or "",
                state=x.get("state") or "unknown",
                zip_url=x.get("full_zip_url") or None,
                err_msg=x.get("err_msg") or "",
            ) for x in raw]

            snapshot = tuple(sorted((x.data_id, x.state) for x in results))
            if on_progress and snapshot != last_snapshot:
                on_progress(results)
                last_snapshot = snapshot

            if results and all(x.state in ("done", "failed") for x in results):
                return results
            if time.time() > deadline:
                log.warning("轮询超时（%.0fs），返回当前状态", timeout)
                return results
            time.sleep(interval)

    # -- 产物 -------------------------------------------------------------- #

    def download_zip(self, zip_url: str, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        r = self._req("GET", zip_url, stream=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 16):
                if chunk:
                    f.write(chunk)
        tmp.replace(dest)
        return dest

    @staticmethod
    def unzip(zip_path: Path, out_dir: Path, keep_images: bool = False) -> Path:
        """解压产物，默认丢掉 ``images/``（表格图，RAG 阶段用不上，占 20 倍体积）。"""
        out_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path) as z:
            for name in z.namelist():
                if not keep_images and name.startswith("images/"):
                    continue
                z.extract(name, out_dir)
        return out_dir

    @staticmethod
    def find_content_list(root: Path) -> Path | None:
        """找 ``*_content_list.json``（注意排除 ``_v2``，两者 schema 不同）。"""
        cands = [p for p in root.rglob("*_content_list.json")
                 if not p.name.endswith("_v2.json")]
        return cands[0] if cands else None
