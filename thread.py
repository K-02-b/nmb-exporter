#!/usr/bin/env python3
"""
X岛内容缓存工具
- 多线程下载 + 全局 429 退避
- 数据按页存储: data/{tid}.json = {"0":[op], "1":[...], "2":[...], ...}
- data/{tid}.meta.json 保存已抓取页号
- data/index.json 保存所有已缓存串的索引（用于 WebUI 浏览）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set
from images import IMAGE_DIR, download_images_for_posts

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from export import EXPORTERS, export_data

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ============================== 常量 ==============================
API_BASE = "https://api.nmb.best/api"
PAGE_SIZE = 19
TEMP_DIR_BASE = ".temp"
DATA_DIR = "data"
CONFIG_FILE = "config.json"
INDEX_FILE = "index.json"
IGNORED_POST_IDS = {"9999999"}

RL_BASE_DELAY = 5.0
RL_MAX_DELAY = 60.0
RL_FACTOR = 2.0


# ============================== 限流器 ==============================
class RateLimiter:
    def __init__(self, on_pause: Optional[Callable[[float, float], None]] = None):
        self.lock = threading.Lock()
        self.cv = threading.Condition(self.lock)
        self.pause_until = 0.0
        self.consecutive = 0
        self.on_pause = on_pause

    def wait(self):
        with self.cv:
            while True:
                now = time.time()
                if now >= self.pause_until:
                    return
                self.cv.wait(timeout=min(self.pause_until - now, 1.0))

    def report_429(self) -> float:
        with self.cv:
            self.consecutive += 1
            wait_s = min(RL_BASE_DELAY * (RL_FACTOR ** (self.consecutive - 1)),
                         RL_MAX_DELAY)
            new_until = time.time() + wait_s
            if new_until > self.pause_until:
                self.pause_until = new_until
            until = self.pause_until
        if self.on_pause:
            try: self.on_pause(wait_s, until)
            except Exception: pass
        return wait_s

    def report_success(self):
        with self.cv:
            if self.consecutive > 0:
                self.consecutive = 0
                self.cv.notify_all()


# ============================== 工具 ==============================
def parse_pages_arg(page_strs: List[str]) -> Optional[List[int]]:
    if not page_strs: return None
    pages: Set[int] = set()
    for part in page_strs:
        for sub in part.split(","):
            sub = sub.strip()
            if not sub: continue
            if "-" in sub:
                a, _, b = sub.partition("-")
                start = int(a) if a else 0
                end = int(b) if b else 10**9
                if start > end: raise ValueError(f"无效页码范围: {sub}")
                pages.update(range(start, end + 1))
            else:
                pages.add(int(sub))
    return sorted(pages)


def compress_pages(pages: List[int]) -> str:
    """[1,2,3,5,7,8,9] -> '1-3, 5, 7-9'"""
    if not pages: return ""
    pages = sorted(set(pages))
    out = []
    s = e = pages[0]
    for p in pages[1:]:
        if p == e + 1:
            e = p
        else:
            out.append(str(s) if s == e else f"{s}-{e}")
            s = e = p
    out.append(str(s) if s == e else f"{s}-{e}")
    return ", ".join(out)


def load_config() -> Dict[str, Any]:
    p = Path(CONFIG_FILE)
    if not p.exists(): return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def get_userhash(args) -> str:
    if getattr(args, "user_hash", None): return args.user_hash
    if os.getenv("NM_USER_HASH"): return os.getenv("NM_USER_HASH")
    cookie = load_config().get("cookie")
    if cookie: return cookie
    return os.getenv("USER_HASH", "")


def build_session() -> requests.Session:
    session = requests.Session()
    retries = Retry(
        total=2, backoff_factor=1,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter); session.mount("http://", adapter)
    session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; NmbCacheScript/3.1)"})
    return session


def get_temp_dir(thread_id: int, only_po: bool) -> Path:
    return Path(TEMP_DIR_BASE) / f"{thread_id}{'_po' if only_po else ''}"


def get_temp_path(thread_id: int, page: int, only_po: bool) -> Path:
    return get_temp_dir(thread_id, only_po) / f"page{page}.json"


def get_data_path(thread_id: int, only_po: bool) -> Path:
    return Path(DATA_DIR) / f"{thread_id}{'_po' if only_po else ''}.json"


def get_meta_path(thread_id: int, only_po: bool) -> Path:
    return Path(DATA_DIR) / f"{thread_id}{'_po' if only_po else ''}.meta.json"


def get_index_path() -> Path:
    return Path(DATA_DIR) / INDEX_FILE


def load_meta(thread_id: int, only_po: bool) -> Dict[str, Any]:
    p = get_meta_path(thread_id, only_po)
    if not p.exists(): return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_meta(thread_id: int, only_po: bool, meta: Dict[str, Any]):
    p = get_meta_path(thread_id, only_po)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def _truthy(v) -> bool:
    if isinstance(v, bool): return v
    if isinstance(v, (int, float)): return v != 0
    if isinstance(v, str): return v.strip() not in ("", "0", "false", "False")
    return bool(v)


# === 修改 parse_post：保留 img / ext ===
def parse_post(raw: Dict[str, Any], *, is_po: bool = False, is_sage: bool = False) -> Dict[str, Any]:
    return {
        "id": str(raw.get("id", "")),
        "cookie": raw.get("user_hash", ""),
        "timestamp": raw.get("now", ""),
        "title": raw.get("title", ""),
        "name": raw.get("name", ""),
        "content": raw.get("content", ""),
        "img": raw.get("img", "") or "",
        "ext": raw.get("ext", "") or "",
        "is_po": is_po,
        "is_admin": _truthy(raw.get("admin", 0)),
        "is_sage": is_sage,
    }


# ============================== 索引 (data/index.json) ==============================
def load_index_data() -> Dict[str, Any]:
    p = get_index_path()
    if not p.exists():
        return {"threads": {}}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"threads": {}}
    if not isinstance(d, dict):
        return {"threads": {}}
    th = d.get("threads")
    if isinstance(th, list):
        d["threads"] = {t.get("key"): t for t in th if t.get("key")}
    elif not isinstance(th, dict):
        d["threads"] = {}
    return d


def save_index_data(idx: Dict[str, Any]):
    p = get_index_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")


def update_thread_index(thread_id: int, only_po: bool,
                        paged: Dict[str, List[Dict[str, Any]]],
                        saved_pages: List[int],
                        total_pages_known: int):
    idx = load_index_data()
    threads = idx.get("threads", {})
    key = f"{thread_id}{'_po' if only_po else ''}"
    op_post: Optional[Dict[str, Any]] = None
    if "0" in paged and paged["0"]:
        op_post = paged["0"][0]
    elif key in threads:
        op_post = threads[key].get("op")

    threads[key] = {
        "key": key,
        "thread_id": thread_id,
        "only_po": only_po,
        "op": op_post,
        "saved_pages": list(saved_pages),
        "total_pages_known": total_pages_known,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    idx["threads"] = threads
    save_index_data(idx)


# ============================== 网络 ==============================
def _request_json(session, url, params, limiter, max_attempts=8):
    for attempt in range(max_attempts):
        limiter.wait()
        try:
            resp = session.get(url, params=params, timeout=30)
        except requests.RequestException:
            if attempt == max_attempts - 1: raise
            time.sleep(min(2 ** attempt, 10)); continue

        if resp.status_code == 429:
            limiter.report_429(); continue
        try:
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            if attempt == max_attempts - 1: raise
            time.sleep(min(2 ** attempt, 10)); continue

        limiter.report_success()
        return data
    return None


def fetch_page(session, thread_id, page, api_prefix, userhash, limiter):
    url = f"{API_BASE}/{api_prefix}/id/{thread_id}/page/{page}"
    params = {"userhash": userhash} if userhash else {}
    try:
        data = _request_json(session, url, params, limiter)
    except Exception as e:
        print(f"[错误] 第 {page} 页请求失败: {e}")
        return None
    if data is None: return None

    if page == 0:
        is_sage = _truthy(data.get("Sage", 0))
        op = parse_post(data, is_po=True, is_sage=is_sage)
        return {"page": 0, "op": op}

    replies = []
    for r in data.get("Replies", []):
        if r.get("user_hash") == "Tips": continue
        if str(r.get("id", "")) in IGNORED_POST_IDS: continue
        replies.append(parse_post(r))
    return {"page": page, "replies": replies}


def get_total_pages(session, thread_id, api_prefix, userhash, limiter) -> int:
    url = f"{API_BASE}/{api_prefix}/id/{thread_id}/page/0"
    params = {"userhash": userhash} if userhash else {}
    try:
        data = _request_json(session, url, params, limiter)
        if data is None: return 0
        count = data.get("ReplyCount", 0)
        if isinstance(count, str):
            count = int(count) if count.isdigit() else 0
        return 0 if count == 0 else (count + PAGE_SIZE - 1) // PAGE_SIZE
    except Exception as e:
        print(f"[警告] 获取总页数失败: {e}")
        return 0


# ============================== 数据合并 / 展平 ==============================
def _sort_page_posts(posts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    def k(p):
        try: pid = int(str(p.get("id", "0")))
        except Exception: pid = 0
        return (pid, str(p.get("timestamp", "")))
    return sorted(posts, key=k)


def _migrate_legacy_list(posts: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """旧版扁平列表 -> 新结构（OP 在 page 0，其余统一放 page 1，后续重抓覆盖）。"""
    paged: Dict[str, List[Dict[str, Any]]] = {}
    for p in posts:
        pid = str(p.get("id", ""))
        if not pid or pid in IGNORED_POST_IDS:
            continue
        if p.get("is_po") and "0" not in paged:
            paged["0"] = [p]
        else:
            paged.setdefault("1", []).append(p)
    if "1" in paged:
        paged["1"] = _sort_page_posts(paged["1"])
    return paged


def merge_and_save_data(data_path: Path,
                        new_paged: Dict[int, List[Dict[str, Any]]]
                        ) -> Dict[str, List[Dict[str, Any]]]:
    """按页合并并落盘。new_paged 中的页会**覆盖**旧的同一页（视为权威值）。"""
    data_path.parent.mkdir(parents=True, exist_ok=True)
    existing: Dict[str, List[Dict[str, Any]]] = {}
    if data_path.exists():
        try:
            d = json.loads(data_path.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                existing = d
            elif isinstance(d, list):
                existing = _migrate_legacy_list(d)
        except Exception as e:
            print(f"[警告] 读取已有数据失败: {e}")

    for page, posts in new_paged.items():
        m: Dict[str, Dict[str, Any]] = {}
        for p in posts:
            pid = str(p.get("id", ""))
            if pid and pid not in IGNORED_POST_IDS:
                m[pid] = p
        existing[str(page)] = _sort_page_posts(list(m.values()))

    data_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    return existing


def flatten_paged(paged: Dict[str, List[Dict[str, Any]]],
                  pages: Optional[List[int]] = None) -> List[Dict[str, Any]]:
    """按页号升序展平。pages 指定时只取这些页（自动并入 page 0）。"""
    available = []
    for k in paged.keys():
        try: available.append(int(k))
        except (TypeError, ValueError): pass
    available = sorted(available)

    if pages is None:
        keys = available
    else:
        wanted = set(pages) | {0}
        keys = sorted(set(available) & wanted)

    out: List[Dict[str, Any]] = []
    for k in keys:
        out.extend(paged.get(str(k), []))
    return out


# ============================== 串内 op_cookie / 单页解析 ==============================
def _op_cookie_from_data(thread_id, only_po, page_data):
    if 0 in page_data and "op" in page_data[0]:
        return page_data[0]["op"].get("cookie")
    p = get_data_path(thread_id, only_po)
    if p.exists():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                if "0" in d and d["0"]:
                    return d["0"][0].get("cookie")
            elif isinstance(d, list):
                for x in d:
                    if x.get("is_po"):
                        return x.get("cookie")
        except Exception:
            pass
    return None


def _posts_of_page(page_data, op_cookie):
    out: List[Dict[str, Any]] = []
    if "op" in page_data:
        out.append(page_data["op"])
    for r in page_data.get("replies", []):
        if str(r.get("id", "")) in IGNORED_POST_IDS: continue
        if op_cookie and r.get("cookie") == op_cookie:
            r["is_po"] = True
        out.append(r)
    return out


# ============================== 核心：统一下载入口 ==============================
PREVIEW_PAGE_LIMIT = 5  # 预览最多渲染前 5 个非 0 页（page 0 总是渲染）


# === 修改 download_thread 签名，新增图片参数 ===
def download_thread(
    thread_id: int,
    *,
    only_po: bool = False,
    pages_arg: Optional[str] = None,
    threads: int = 5,
    resume: bool = True,
    userhash: str = "",
    on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
    render_page_fn: Optional[Callable[[List[Dict[str, Any]]], str]] = None,
    save_images: bool = False,
    image_quality: str = "thumb",
) -> Dict[str, Any]:
    """统一下载入口。on_event 回调按页码顺序产出 page_done。"""
    def emit(ev):
        if on_event:
            try: on_event(ev)
            except Exception: pass

    api_prefix = "po" if only_po else "thread"
    session = build_session()
    limiter = RateLimiter(on_pause=lambda w, u: emit({"event": "rate_limit",
                                                       "wait": w, "until": u}))

    try:
        # 1. 总页数
        explicit_pages = parse_pages_arg([pages_arg]) if pages_arg else None
        emit({"event": "stage", "stage": "fetch_total"})
        total = get_total_pages(session, thread_id, api_prefix, userhash, limiter)
        emit({"event": "total_pages", "total": total})

        # 2. 计划
        if explicit_pages is not None:
            target = sorted(set(explicit_pages) | {0})
            force_refresh: Set[int] = set()
        else:
            all_pages = [0] + list(range(1, total + 1))
            meta = load_meta(thread_id, only_po) if resume else {}
            saved: Set[int] = set(meta.get("saved_pages", []))
            if resume and saved:
                last_saved = max(saved)
                skip = saved - {last_saved}
                target = [p for p in all_pages if p not in skip]
                force_refresh = {last_saved} if last_saved in target else set()
            else:
                target = all_pages
                force_refresh = set()

        target = sorted(set(target))
        emit({"event": "plan", "pages": target,
              "force_refresh": sorted(force_refresh)})

        # 3. 计算预览限定页（前 5 个非 0 页 + page 0）
        preview_pages: Set[int] = {0}
        for p in target:
            if p == 0: continue
            if len(preview_pages) > PREVIEW_PAGE_LIMIT: break
            preview_pages.add(p)
        emit({"event": "preview_pages", "pages": sorted(preview_pages)})

        if not target:
            emit({"event": "all_done", "count": 0,
                  "successful_pages": [], "failed_pages": [],
                  "saved_pages": list(load_meta(thread_id, only_po).get("saved_pages", [])),
                  "total_pages_known": total})
            return {"successful_pages": [], "failed_pages": [], "merged": []}

        # 4. 并发下载
        page_data: Dict[int, Dict[str, Any]] = {}
        successful: Set[int] = set()
        failed: Set[int] = set()

        def fetch_with_cache(p: int):
            tp = get_temp_path(thread_id, p, only_po)
            if resume and (p not in force_refresh) and tp.exists() and tp.stat().st_size > 0:
                try:
                    return p, json.loads(tp.read_text(encoding="utf-8"))
                except Exception:
                    pass
            data = fetch_page(session, thread_id, p, api_prefix, userhash, limiter)
            if data is not None:
                tp.parent.mkdir(parents=True, exist_ok=True)
                tp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                              encoding="utf-8")
            return p, data

        ordered = sorted(target)
        next_idx = 0
        completed: Dict[int, Optional[Dict[str, Any]]] = {}

        with ThreadPoolExecutor(max_workers=max(1, threads)) as ex:
            futs = [ex.submit(fetch_with_cache, p) for p in target]
            for fut in as_completed(futs):
                try:
                    p, data = fut.result()
                except Exception as e:
                    emit({"event": "error", "error": f"线程异常: {e}"})
                    continue

                completed[p] = data
                if data is not None:
                    page_data[p] = data

                while next_idx < len(ordered) and ordered[next_idx] in completed:
                    pp = ordered[next_idx]
                    pd = completed[pp]
                    if pd is None:
                        failed.add(pp)
                        emit({"event": "page_failed", "page": pp})
                    else:
                        successful.add(pp)
                        op_cookie = _op_cookie_from_data(thread_id, only_po, page_data)
                        posts = _posts_of_page(pd, op_cookie)
                        if pp == 0 and posts:
                            posts[0]["is_po"] = True
                        ev: Dict[str, Any] = {"event": "page_done",
                                               "page": pp, "count": len(posts)}
                        if render_page_fn is not None and pp in preview_pages:
                            try: ev["html"] = render_page_fn(posts)
                            except Exception as e:
                                ev["html"] = f"<div class='post'><div class='content'>渲染失败: {e}</div></div>"
                        emit(ev)
                    next_idx += 1

        # 5. 合并落盘（按页）
        all_new_paged: Dict[int, List[Dict[str, Any]]] = {}
        op_cookie = _op_cookie_from_data(thread_id, only_po, page_data)
        for pp in sorted(successful):
            posts = _posts_of_page(page_data[pp], op_cookie)
            if pp == 0 and posts:
                posts[0]["is_po"] = True
            all_new_paged[pp] = posts

        merged_paged = merge_and_save_data(get_data_path(thread_id, only_po),
                                           all_new_paged)
        merged = flatten_paged(merged_paged)

        # 6. meta + index
        meta = load_meta(thread_id, only_po)
        old_saved = set(meta.get("saved_pages", []))
        new_saved = sorted(old_saved | successful)
        total_known = max(meta.get("total_pages_known", 0), total)
        meta.update({
            "thread_id": thread_id,
            "only_po": only_po,
            "saved_pages": new_saved,
            "total_pages_known": total_known,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        })
        save_meta(thread_id, only_po, meta)
        update_thread_index(thread_id, only_po, merged_paged, new_saved, total_known)
        
        # 7. 图片预下载
        if save_images and image_quality in ("thumb", "image"):
            try:
                emit({"event": "stage", "stage": "fetch_images",
                      "quality": image_quality})
                image_base_dir = str(Path(IMAGE_DIR) / str(thread_id))
                res = download_images_for_posts(
                    merged, image_quality, base_dir=image_base_dir,
                    session=session, limiter=limiter, threads=threads,
                    on_progress=lambda d: emit({"event": "image_progress", **d}),
                )
                emit({"event": "images_done", **res})
            except Exception as e:
                emit({"event": "image_error", "error": str(e)})

        emit({
            "event": "all_done",
            "count": len(merged),
            "successful_pages": sorted(successful),
            "failed_pages": sorted(failed),
            "saved_pages": new_saved,
            "total_pages_known": total_known,
        })
        return {
            "successful_pages": sorted(successful),
            "failed_pages": sorted(failed),
            "merged": merged,
        }
    except Exception as e:
        import traceback
        emit({"event": "error", "error": str(e), "trace": traceback.format_exc()})
        raise


# ============================== CLI ==============================
def _cli_event_handler(ev: Dict[str, Any]):
    e = ev.get("event")
    if e == "stage" and ev.get("stage") == "fetch_total":
        print("正在获取总页数...")
    elif e == "stage" and ev.get("stage") == "fetch_images":
        print(f"[图片] 开始下载图片（quality={ev.get('quality')}）")
    elif e == "image_progress":
        print(f"\r[图片] {ev['done']}/{ev['total']} (ok={ev['ok']}, fail={ev['fail']})", end="")
    elif e == "images_done":
        print(f"\n[图片] 完成：ok={ev['ok']}, fail={ev['fail']}, total={ev['total']}")
    elif e == "total_pages":
        print(f"总回复页数: {ev['total']}")
    elif e == "plan":
        print(f"计划下载页面: {ev['pages']}"
              + (f"（强制重抓: {ev['force_refresh']}）" if ev.get('force_refresh') else ""))
    elif e == "rate_limit":
        print(f"[限流] 命中 429，全部线程暂停 {ev['wait']:.1f}s")
    elif e == "page_done":
        print(f"[完成] 第 {ev['page']:>3} 页（{ev['count']} 条）")
    elif e == "page_failed":
        print(f"[失败] 第 {ev['page']:>3} 页")
    elif e == "all_done":
        print(f"全部完成：合计 {ev['count']} 条；"
              f"成功页: {ev['successful_pages']}；失败页: {ev['failed_pages']}")
    elif e == "error":
        print(f"[错误] {ev['error']}")


def main():
    parser = argparse.ArgumentParser(description="X岛串缓存与导出工具")
    parser.add_argument("thread_id", type=int)
    parser.add_argument("--only-po", action="store_true")
    parser.add_argument("--user-hash", type=str, default=None)
    parser.add_argument("--pages", type=str, action="append")
    parser.add_argument("--threads", type=int, default=5)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--output", type=str, default=None)
    parser.add_argument("--format", choices=list(EXPORTERS.keys()), default="json")
    parser.add_argument("--export-pages", type=str, default=None,
                        help="导出时只包含这些页（自动包含 page 0）")
    parser.add_argument("--no-header", dest="show_header", action="store_false")
    parser.add_argument("--no-meta", dest="show_meta", action="store_false")
    parser.add_argument("--no-divider", dest="show_divider", action="store_false")
    parser.add_argument("--width", type=int, default=1080)
    parser.set_defaults(show_header=True, show_meta=True, show_divider=True)
    parser.add_argument("--save-images", action="store_true", help="同时保存图片")
    parser.add_argument("--image-quality", choices=["thumb", "image"], default="thumb")

    args = parser.parse_args()

    userhash = get_userhash(args)
    if not userhash:
        print("错误：未提供 userhash"); sys.exit(1)

    pages_arg = ",".join(args.pages) if args.pages else None
    result = download_thread(
        args.thread_id, only_po=args.only_po, pages_arg=pages_arg,
        threads=args.threads, resume=args.resume, userhash=userhash,
        on_event=_cli_event_handler,
        save_images=args.save_images,
        image_quality=args.image_quality,
    )

    # 处理 --export-pages
    paged = None
    data_path = get_data_path(args.thread_id, args.only_po)
    if data_path.exists():
        try:
            d = json.loads(data_path.read_text(encoding="utf-8"))
            paged = d if isinstance(d, dict) else _migrate_legacy_list(d)
        except Exception:
            paged = {}

    if args.export_pages:
        wanted = parse_pages_arg([args.export_pages]) or []
        wanted_set = set(wanted) | {0}
        available = set(int(k) for k in paged.keys()) if paged else set()
        missing = sorted(wanted_set - available)
        if missing:
            print(f"错误：以下页未保存，无法导出: {missing}"); sys.exit(2)
        merged = flatten_paged(paged, sorted(wanted_set))
    else:
        merged = result["merged"]

    if not merged:
        print("没有可导出内容。"); return

    if args.format == "json":
        if args.output and os.path.abspath(args.output) != os.path.abspath(str(data_path)):
            export_data(merged, "json", args.output)
    else:
        out = args.output or f"{args.thread_id}{'_po' if args.only_po else ''}.{args.format}"
        image_opts = None
        if args.save_images:
            image_opts = {
                "quality": args.image_quality,
                "mode": "embed",
                "fetch": True,
                "base_dir": str(Path(IMAGE_DIR) / str(args.thread_id)),
            }

        export_data(merged, args.format, out,
                    show_header=args.show_header, show_meta=args.show_meta,
                    show_divider=args.show_divider, width=args.width,
                    image_opts=image_opts)


if __name__ == "__main__":
    main()