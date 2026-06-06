"""
图片下载 / 缓存工具
- 自动从 https://api.nmb.best/api/getCDNPath 选择带宽最高的 CDN
- 共享一个全局 RateLimiter，遵循 429 退避
- 本地落盘到 data/images/{quality}/{img}{ext}
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import requests

IMAGE_DIR = "data/images"
CDN_FALLBACK = "https://image.nmb.best/"
CDN_TTL = 3600
USER_AGENT = "Mozilla/5.0 (compatible; NmbCacheScript/3.2)"

_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT})

_cdn_cache: Dict[str, Any] = {"url": None, "expire": 0.0}
_cdn_lock = threading.Lock()


# ============================== CDN ==============================
def get_cdn_url(session: Optional[requests.Session] = None,
                limiter=None) -> str:
    with _cdn_lock:
        if _cdn_cache["url"] and time.time() < _cdn_cache["expire"]:
            return _cdn_cache["url"]
    sess = session or _session
    url = CDN_FALLBACK
    try:
        if limiter: limiter.wait()
        r = sess.get("https://api.nmb.best/api/getCDNPath", timeout=15)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list) and data:
                best = max(data, key=lambda x: float(x.get("rate", 0) or 0))
                url = (best.get("url") or CDN_FALLBACK).strip()
        if limiter: limiter.report_success()
    except Exception:
        pass
    if not url.endswith("/"):
        url += "/"
    with _cdn_lock:
        _cdn_cache["url"] = url
        _cdn_cache["expire"] = time.time() + CDN_TTL
    return url


# ============================== 路径 ==============================
def image_local_filename(img: str, ext: str) -> str:
    safe_img = str(img or "").replace("/", "_").replace("\\", "_")
    return f"{safe_img}{ext}"


def image_local_path(img: str, ext: str, quality: str,
                      base_dir: str = IMAGE_DIR) -> Path:
    return Path(base_dir) / quality / image_local_filename(img, ext)


# ============================== 下载 ==============================
def download_image(img: str, ext: str, quality: str,
                    base_dir: str = IMAGE_DIR,
                    session: Optional[requests.Session] = None,
                    limiter=None,
                    timeout: int = 30,
                    max_attempts: int = 5) -> Optional[str]:
    """下载单张图片到本地，返回本地路径（已存在时直接返回）。"""
    if not img or not ext: return None
    if quality not in ("thumb", "image"): return None

    target = image_local_path(img, ext, quality, base_dir)
    if target.exists() and target.stat().st_size > 0:
        return str(target)

    sess = session or _session
    cdn = get_cdn_url(sess, limiter)
    url = f"{cdn.rstrip('/')}/{quality}/{img}{ext}"

    for attempt in range(max_attempts):
        if limiter: limiter.wait()
        try:
            r = sess.get(url, timeout=timeout)
        except requests.RequestException:
            time.sleep(min(2 ** attempt, 8))
            continue

        if r.status_code == 429:
            if limiter: limiter.report_429()
            else: time.sleep(min(2 ** (attempt + 1), 30))
            continue
        if r.status_code != 200:
            return None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(r.content)
        except OSError:
            return None
        if limiter: limiter.report_success()
        return str(target)
    return None


def download_images_for_posts(
    posts: List[Dict[str, Any]],
    quality: str,
    *,
    base_dir: str = IMAGE_DIR,
    session: Optional[requests.Session] = None,
    limiter=None,
    threads: int = 5,
    on_progress: Optional[Callable[[Dict[str, int]], None]] = None,
) -> Dict[str, int]:
    """批量下载所有 post 中的图片。"""
    if quality not in ("thumb", "image"):
        return {"total": 0, "ok": 0, "fail": 0}

    targets = list({(p.get("img"), p.get("ext")) for p in posts
                    if p.get("img") and p.get("ext")})
    if not targets:
        return {"total": 0, "ok": 0, "fail": 0}

    sess = session or _session
    get_cdn_url(sess, limiter)  # 预热

    ok = fail = 0

    def work(t):
        i, e = t
        return download_image(i, e, quality, base_dir, sess, limiter)

    with ThreadPoolExecutor(max_workers=max(1, threads)) as ex:
        futs = [ex.submit(work, t) for t in targets]
        for fut in as_completed(futs):
            try:
                res = fut.result()
            except Exception:
                res = None
            if res: ok += 1
            else:   fail += 1
            if on_progress:
                try:
                    on_progress({"done": ok + fail, "total": len(targets),
                                 "ok": ok, "fail": fail})
                except Exception:
                    pass
    return {"total": len(targets), "ok": ok, "fail": fail}