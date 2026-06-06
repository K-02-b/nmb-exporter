"""
X岛缓存工具 - Web UI
"""
import json
import os
import queue
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from flask import (Flask, Response, abort, after_this_request, jsonify,
                   render_template, request, send_file, stream_with_context)

from export import (EXPORTERS, export_data, render_html, render_post,
                    render_posts_fragment)
from images import IMAGE_DIR, download_image, image_local_path
from thread import download_thread
from parse_qrcode import parse_qr_cookie_file

app = Flask(__name__)

DATA_DIR = Path("data")
INDEX_FILE = DATA_DIR / "index.json"
CONFIG_FILE = Path("config.json")
PREVIEW_PAGE_LIMIT = 5


def read_config() -> Dict[str, Any]:
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_config(cfg: Dict[str, Any]) -> None:
    CONFIG_FILE.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_index_data() -> Dict[str, Any]:
    if not INDEX_FILE.exists():
        return {"threads": {}}

    try:
        d = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"threads": {}}

    if not isinstance(d, dict):
        return {"threads": {}}

    threads = d.get("threads")

    if isinstance(threads, list):
        d["threads"] = {
            t.get("key"): t
            for t in threads
            if isinstance(t, dict) and t.get("key")
        }
    elif not isinstance(threads, dict):
        d["threads"] = {}

    return d


def save_index_data(idx: Dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_FILE.write_text(
        json.dumps(idx, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def thread_key(tid: int, only_po: bool) -> str:
    return f"{tid}{'_po' if only_po else ''}"


def thread_data_file(tid: int, only_po: bool) -> Path:
    return DATA_DIR / f"{thread_key(tid, only_po)}.json"


def thread_meta_file(tid: int, only_po: bool) -> Path:
    return DATA_DIR / f"{thread_key(tid, only_po)}.meta.json"


def thread_data_dir(tid: int, only_po: bool) -> Path:
    return DATA_DIR / thread_key(tid, only_po)


def thread_image_base_dir(tid: int) -> str:
    return str(Path(IMAGE_DIR) / str(tid))


def thread_image_url_pattern(tid: int) -> str:
    return f"/api/image/{tid}/{{quality}}/{{img}}{{ext}}"


def _legacy_list_to_paged(posts: List[Dict]) -> Optional[Dict[int, List[Dict]]]:
    paged: Dict[int, List[Dict]] = {}

    for p in posts:
        if not isinstance(p, dict):
            continue

        if p.get("is_po") and 0 not in paged:
            paged[0] = [p]
        else:
            paged.setdefault(1, []).append(p)

    return paged or None


def load_paged_for(tid: int, only_po: bool) -> Optional[Dict[int, List[Dict]]]:
    data_file = thread_data_file(tid, only_po)

    if data_file.exists():
        try:
            raw = json.loads(data_file.read_text(encoding="utf-8"))
        except Exception:
            raw = None

        if isinstance(raw, dict):
            paged: Dict[int, List[Dict]] = {}

            for k, v in raw.items():
                try:
                    page_no = int(k)
                except Exception:
                    continue

                if isinstance(v, list):
                    paged[page_no] = v

            return paged or None

        if isinstance(raw, list):
            return _legacy_list_to_paged(raw)

    d = thread_data_dir(tid, only_po)

    if d.exists() and d.is_dir():
        paged: Dict[int, List[Dict]] = {}

        for f in d.glob("page_*.json"):
            try:
                page_no = int(f.stem.split("_")[1])
                posts = json.loads(f.read_text(encoding="utf-8"))

                if isinstance(posts, list):
                    paged[page_no] = posts
            except Exception:
                continue

        return paged or None

    return None


def _available_pages(paged: Dict[int, List[Dict]]) -> List[int]:
    return sorted(paged.keys())


def flatten_paged(paged: Dict[int, List[Dict]],
                  pages: Optional[List[int]] = None) -> List[Dict]:
    if pages is None:
        pages = sorted(paged.keys())

    out: List[Dict] = []

    for p in pages:
        if p in paged:
            out.extend(paged[p])

    return out


def parse_pages_param(s: Optional[str]) -> Optional[List[int]]:
    if not s:
        return None

    s = s.strip()

    if not s:
        return None

    out = set()

    for part in s.split(","):
        part = part.strip()

        if not part:
            continue

        if "-" in part:
            try:
                a, b = part.split("-", 1)
                a, b = int(a), int(b)

                if a > b:
                    a, b = b, a

                out.update(range(a, b + 1))
            except ValueError:
                continue
        else:
            try:
                out.add(int(part))
            except ValueError:
                continue

    return sorted(out)


def compress_pages(pages: List[int]) -> str:
    if not pages:
        return ""

    pages = sorted(set(pages))
    parts: List[str] = []
    i = 0

    while i < len(pages):
        j = i

        while j + 1 < len(pages) and pages[j + 1] == pages[j] + 1:
            j += 1

        if j > i:
            parts.append(f"{pages[i]}-{pages[j]}")
        else:
            parts.append(str(pages[i]))

        i = j + 1

    return ",".join(parts)


def parse_bool(v: Any, default: bool = False) -> bool:
    if v is None:
        return default

    if isinstance(v, bool):
        return v

    s = str(v).strip().lower()

    if s in ("1", "true", "yes", "y", "on"):
        return True

    if s in ("0", "false", "no", "n", "off", ""):
        return False

    return default


def _sse(payload: Dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _image_opts_from_request(*, tid: int,
                             mode: str = "url",
                             fetch: bool = False) -> Optional[Dict]:
    quality = (request.args.get("image_quality") or "").strip()

    if quality not in ("thumb", "image"):
        return None

    return {
        "quality": quality,
        "mode": mode,
        "fetch": fetch,
        "base_dir": thread_image_base_dir(tid),
        "url_pattern": thread_image_url_pattern(tid),
    }


def _serve_image_from_base(base_dir: str, quality: str, rel: str):
    if quality not in ("thumb", "image"):
        abort(404)

    p = Path(rel)
    ext = p.suffix

    if not ext:
        abort(400)

    img_id = str(p.with_suffix(""))
    local = image_local_path(img_id, ext, quality, base_dir)

    if not (local.exists() and local.stat().st_size > 0):
        download_image(img_id, ext, quality, base_dir=base_dir)

    if not (local.exists() and local.stat().st_size > 0):
        abort(404)

    try:
        base = Path(base_dir).resolve()
        local_resolved = local.resolve()
        local_resolved.relative_to(base)
    except Exception:
        abort(403)

    return send_file(str(local_resolved))


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "GET":
        return jsonify({"ok": True, "config": read_config()})

    data = request.get_json(force=True, silent=True) or {}
    cfg = read_config()

    if "cookie" in data:
        cfg["cookie"] = (data.get("cookie") or "").strip()

    write_config(cfg)

    return jsonify({"ok": True})


@app.route("/api/config/qrcode", methods=["POST"])
def api_config_qrcode():
    f = request.files.get("image")

    if not f:
        return jsonify({"ok": False, "error": "未上传二维码图片"}), 400

    suffix = Path(f.filename or "").suffix or ".png"

    fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)

    try:
        f.save(tmp_path)
        info = parse_qr_cookie_file(tmp_path)

        cfg = read_config()
        cfg["cookie"] = info["cookie"]

        if info.get("name"):
            cfg["name"] = info["name"]

        write_config(cfg)

        return jsonify({
            "ok": True,
            "cookie": info["cookie"],
            "name": info.get("name", ""),
        })

    except Exception as e:
        return jsonify({
            "ok": False,
            "error": str(e),
        }), 400

    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


@app.route("/api/threads")
def api_threads():
    idx = load_index_data()
    threads = idx.get("threads", {})
    out = []

    for key, t in threads.items():
        if not isinstance(t, dict):
            continue

        op = t.get("op")

        if not op:
            continue

        thread_id = t.get("thread_id")
        op_img_opts = None

        if thread_id and op.get("img") and op.get("ext"):
            op_img_opts = {
                "quality": "thumb",
                "mode": "url",
                "base_dir": thread_image_base_dir(int(thread_id)),
                "url_pattern": thread_image_url_pattern(int(thread_id)),
            }

        op_html = render_post(
            op,
            0,
            1,
            show_header=True,
            show_meta=True,
            show_divider=False,
            image_opts=op_img_opts,
        )

        saved = t.get("saved_pages", [])

        out.append({
            "key": key,
            "thread_id": thread_id,
            "only_po": bool(t.get("only_po")),
            "saved_pages": saved,
            "saved_pages_text": compress_pages(saved),
            "total_pages_known": t.get("total_pages_known", 0),
            "updated_at": t.get("updated_at", ""),
            "op_html": op_html,
        })

    out.sort(key=lambda x: x.get("updated_at", ""), reverse=True)

    return jsonify({"ok": True, "threads": out})


@app.route("/api/thread/<int:tid>", methods=["DELETE"])
def api_delete_thread(tid):
    only_po = parse_bool(request.args.get("po"), False)
    key = thread_key(tid, only_po)

    idx = load_index_data()
    threads = idx.get("threads", {})

    if key in threads:
        del threads[key]
        idx["threads"] = threads
        save_index_data(idx)

    for p in [
        thread_data_file(tid, only_po),
        thread_meta_file(tid, only_po),
    ]:
        try:
            if p.exists():
                p.unlink()
        except OSError:
            pass

    old_dir = thread_data_dir(tid, only_po)

    if old_dir.exists() and old_dir.is_dir():
        shutil.rmtree(old_dir, ignore_errors=True)

    tmp_dir = Path(".temp") / key

    if tmp_dir.exists() and tmp_dir.is_dir():
        shutil.rmtree(tmp_dir, ignore_errors=True)

    if not only_po:
        img_dir = Path(IMAGE_DIR) / str(tid)

        if img_dir.exists() and img_dir.is_dir():
            shutil.rmtree(img_dir, ignore_errors=True)

    return jsonify({"ok": True})


@app.route("/api/preview")
def api_preview():
    try:
        tid = int(request.args.get("tid"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "无效的串号"}), 400

    only_po = parse_bool(request.args.get("po"), False)
    paged = load_paged_for(tid, only_po)

    if paged is None:
        return jsonify({"ok": False, "error": "数据不存在"}), 404

    available = _available_pages(paged)
    requested = parse_pages_param(request.args.get("pages"))

    if requested is None:
        non_zero = [p for p in available if p != 0][:PREVIEW_PAGE_LIMIT]
    else:
        non_zero = [p for p in requested if p != 0][:PREVIEW_PAGE_LIMIT]

    chosen = sorted(set(non_zero) | {0})
    chosen = [p for p in chosen if p in available]

    posts = flatten_paged(paged, chosen)
    image_opts = _image_opts_from_request(tid=tid, mode="url", fetch=False)

    html = render_html(
        posts,
        show_header=parse_bool(request.args.get("show_header"), True),
        show_meta=parse_bool(request.args.get("show_meta"), True),
        show_divider=parse_bool(request.args.get("show_divider"), True),
        full_document=True,
        title=f"No.{tid}",
        image_opts=image_opts,
    )

    return jsonify({
        "ok": True,
        "html": html,
        "count": len(posts),
        "rendered_pages": chosen,
        "available_pages": available,
    })


@app.route("/api/image/<int:tid>/<quality>/<path:rel>")
def api_thread_image(tid, quality, rel):
    return _serve_image_from_base(thread_image_base_dir(tid), quality, rel)


@app.route("/api/image/<quality>/<path:rel>")
def api_image(quality, rel):
    return _serve_image_from_base(IMAGE_DIR, quality, rel)


@app.route("/api/export/<fmt>")
def api_export(fmt):
    if fmt not in EXPORTERS:
        abort(404)

    try:
        tid = int(request.args.get("tid"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "无效的串号"}), 400

    only_po = parse_bool(request.args.get("po"), False)
    paged = load_paged_for(tid, only_po)

    if paged is None:
        return jsonify({"ok": False, "error": "数据不存在"}), 404

    available = set(_available_pages(paged))
    requested = parse_pages_param(request.args.get("pages"))

    if requested is not None:
        wanted = set(requested) | {0}
        missing = sorted(wanted - available)

        if missing:
            return jsonify({
                "ok": False,
                "error": f"以下页未保存，无法导出: {compress_pages(missing)}",
            }), 400

        posts = flatten_paged(paged, sorted(wanted))
    else:
        posts = flatten_paged(paged)

    if not posts:
        return jsonify({"ok": False, "error": "没有可导出的内容"}), 400

    show_header = parse_bool(request.args.get("show_header"), True)
    show_meta = parse_bool(request.args.get("show_meta"), True)
    show_divider = parse_bool(request.args.get("show_divider"), True)

    try:
        width = int(request.args.get("width") or 1080)
    except ValueError:
        width = 1080

    image_opts = _image_opts_from_request(
        tid=tid,
        mode="embed" if fmt == "html" else "url",
        fetch=True,
    )

    suffix = "_po" if only_po else ""
    download_name = f"{tid}{suffix}.{fmt}"

    fd, tmp_path = tempfile.mkstemp(suffix=f".{fmt}")
    os.close(fd)

    try:
        export_data(
            posts,
            fmt,
            tmp_path,
            show_header=show_header,
            show_meta=show_meta,
            show_divider=show_divider,
            width=width,
            image_opts=image_opts,
        )
    except Exception as e:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

        return jsonify({"ok": False, "error": f"导出失败: {e}"}), 500

    @after_this_request
    def _cleanup(resp):
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

        return resp

    return send_file(tmp_path, as_attachment=True, download_name=download_name)


@app.route("/api/download_stream")
def api_download_stream():
    try:
        tid = int(request.args.get("tid"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "无效的串号"}), 400

    only_po = parse_bool(request.args.get("only_po"), False)
    pages_arg = (request.args.get("pages") or "").strip() or None

    try:
        threads_n = max(1, min(int(request.args.get("threads") or 5), 32))
    except ValueError:
        threads_n = 5

    resume = parse_bool(request.args.get("resume"), True)
    show_header = parse_bool(request.args.get("show_header"), True)
    show_meta = parse_bool(request.args.get("show_meta"), True)
    show_divider = parse_bool(request.args.get("show_divider"), True)

    save_images = parse_bool(request.args.get("save_images"), False)
    image_quality = (request.args.get("image_quality") or "thumb").strip()

    if image_quality not in ("thumb", "image"):
        image_quality = "thumb"

    userhash = read_config().get("cookie", "") or os.getenv("NM_USER_HASH", "")

    if not userhash:
        def _err():
            yield _sse({"event": "error", "error": "未配置 userhash"})

        return Response(
            stream_with_context(_err()),
            mimetype="text/event-stream",
        )

    q: "queue.Queue[Optional[dict]]" = queue.Queue()

    preview_image_opts = ({
        "quality": image_quality,
        "mode": "url",
        "base_dir": thread_image_base_dir(tid),
        "url_pattern": thread_image_url_pattern(tid),
    } if save_images else None)

    def render_page(posts):
        return render_posts_fragment(
            posts,
            show_header=show_header,
            show_meta=show_meta,
            show_divider=show_divider,
            divider_after_last=True,
            image_opts=preview_image_opts,
        )

    def on_event(ev):
        q.put(ev)

        if ev.get("event") == "all_done" or (
            ev.get("event") == "error" and not ev.get("recoverable")
        ):
            q.put(None)

    def worker():
        try:
            download_thread(
                tid,
                only_po=only_po,
                pages_arg=pages_arg,
                threads=threads_n,
                resume=resume,
                userhash=userhash,
                on_event=on_event,
                render_page_fn=render_page,
                save_images=save_images,
                image_quality=image_quality,
            )
        except Exception as e:
            q.put({"event": "error", "error": str(e)})
            q.put(None)

    threading.Thread(target=worker, daemon=True).start()

    def stream():
        yield _sse({
            "event": "open",
            "thread_id": tid,
            "only_po": only_po,
        })

        while True:
            try:
                ev = q.get(timeout=20)
            except queue.Empty:
                yield ": keepalive\n\n"
                continue

            if ev is None:
                yield _sse({"event": "close"})
                return

            yield _sse(ev)

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    }

    return Response(
        stream_with_context(stream()),
        mimetype="text/event-stream",
        headers=headers,
    )


if __name__ == "__main__":
    DATA_DIR.mkdir(exist_ok=True)
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)