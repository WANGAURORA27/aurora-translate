"""docbridge · 文档翻译网站（Flask）

一条流水线的网站外壳：上传文档 → 排队 → 翻译 → 下载。挂在
app.example.com 上和 同类项目 并存（同一域名、不同路径或子域）。

与前端的约定
------------
* 页面里所有请求都用**相对 URL**（``api/jobs``），因此站点挂在
  ``/doc/`` 路径下或独立子域都能直接跑，不需要改代码；
* 密钥只走服务端：浏览器只用「预设代号」；页面手填 Key 仅作为兜底，
  且只随该次任务存在于内存，不落盘、不回显。
"""

from __future__ import annotations

import json
import os
import sys
import time
from urllib.parse import quote

from flask import (Flask, Response, abort, jsonify, request, send_file,
                   send_from_directory)

import jobs as J
import translator as T
from usage import Usage

ROOT = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.path.join(ROOT, "public")

URL_PREFIX = (os.environ.get("DOCBRIDGE_URL_PREFIX") or "").rstrip("/")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "").strip()
TRUST_PROXY = (os.environ.get("DOCBRIDGE_TRUST_PROXY", "") or "").lower() in ("1", "true", "yes")
STARTED = time.time()

app = Flask(__name__, static_folder=PUBLIC, static_url_path="")
app.config["MAX_CONTENT_LENGTH"] = J.MAX_UPLOAD_BYTES + 8 * 1024 * 1024
# 让 JSON 直接输出中文（Flask ≥2.3 用 app.json.ensure_ascii；
# 旧写法 app.config["JSON_AS_ASCII"] 在 Flask 3 上已失效，会被忽略）
app.json.ensure_ascii = False

manager = J.JobManager()
usage = Usage()


def _client_ip() -> str:
    if TRUST_PROXY:
        fwd = request.headers.get("X-Forwarded-For", "")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.remote_addr or "-"


def _is_admin() -> bool:
    if not ADMIN_TOKEN:
        return False
    token = request.args.get("token") or request.headers.get("X-Admin-Token") or ""
    return token == ADMIN_TOKEN


def _content_disposition(filename: str) -> str:
    """中文文件名必须走 RFC 5987，否则部分浏览器拿到乱码名。"""
    ascii_name = filename.encode("ascii", "ignore").decode() or "translated"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


# 任务跑完把用量记上（JobManager 完成时会回调这里）
def _on_job_finished(job: J.Job) -> None:
    stats = job.stats or {}
    usage.record(
        job.ip,
        jobs=1,
        charsIn=int(stats.get("chars_in") or stats.get("api_chars_in") or 0),
        charsOut=int(stats.get("chars_out") or stats.get("api_chars_out") or 0),
        calls=int(stats.get("api_calls") or 0),
        pages=int(stats.get("pages") or 0),
        filesIn=1,
        failed=int(stats.get("api_failed") or 0),
        tokensIn=int(stats.get("api_tokens_in") or 0),
        tokensOut=int(stats.get("api_tokens_out") or 0),
    )


manager.on_job_finished = _on_job_finished


# ---------------------------------------------------------------- 页面

@app.get("/")
def index():
    return send_from_directory(PUBLIC, "index.html")


@app.get("/admin")
def admin_page():
    return send_from_directory(PUBLIC, "admin.html")


@app.get("/api/health")
def health():
    return jsonify({
        "ok": True,
        "service": "docbridge",
        "uptime": int(time.time() - STARTED),
        "workers": J.MAX_WORKERS,
        "queued": sum(1 for j in manager._jobs.values() if j.status == "queued"),  # noqa: SLF001
        "running": sum(1 for j in manager._jobs.values() if j.status == "running"),  # noqa: SLF001
        "pipelines": [f"{fmt}/{mode}" for fmt, mode in J.PIPELINES],
        "pipelineErrors": [f"{k[0]}/{k[1]}: {v}" for k, v in J.IMPORT_ERRORS.items()],
    })


@app.get("/api/config")
def config():
    """前端开局拉一次：能翻哪些格式/模式、有哪些代号、支持哪些语言。"""
    return jsonify({
        "site": "DocBridge 文档翻译",
        "profiles": T.public_profiles(),
        "capabilities": J.capability_payload(),
        "languages": [{"code": c, "name": n} for c, n in T.LANGUAGES],
        "strategies": [
            {"code": "fallback", "name": "自动兜底",
             "note": "主通道失败时自动切备用通道（推荐：中转站偶发故障时任务不会白跑）"},
            {"code": "hybrid", "name": "混合路由",
             "note": "术语表/表格这类难内容走强通道，普通段落走当前通道——质量与成本兼顾"},
            {"code": "single", "name": "只用当前通道",
             "note": "不兜底也不攻坚，成本最可预测"},
        ],
        "defaultStrategy": "fallback",
        "defaults": {"source_lang": "auto", "target_lang": "zh-Hans"},
        "limits": {
            "maxUploadMB": J.MAX_UPLOAD_MB,
            "jobTTLHours": J.JOB_TTL_HOURS,
            "maxConcurrentPerIp": 2,
            "note": "站点按任务隔离，产物保留一段时间后自动清理",
        },
        "queue": manager.queue_stats(),
        "adminEnabled": bool(ADMIN_TOKEN),
        "profilesFile": os.path.basename(T.profiles_path()),
        "links": {
            # 同域下的 同类项目 入口；留空则页面不显示该链接
            "live": (os.environ.get("DOCBRIDGE_LIVE_URL") or "").strip(),
        },
    })


@app.get("/api/profiles")
def profiles():
    return jsonify({"profiles": T.public_profiles()})


# ---------------------------------------------------------------- 任务

@app.post("/api/jobs")
def create_job():
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return jsonify({"error": "没有收到文件（表单字段名应为 file）"}), 400

    mode = (request.form.get("mode") or "inplace").strip()
    profile = (request.form.get("profile") or "").strip()
    source_lang = (request.form.get("source_lang") or "auto").strip()
    target_lang = (request.form.get("target_lang") or "zh-Hans").strip()

    # 通道策略：single=只用选中通道 / fallback=失败自动兜底 / hybrid=难内容走强通道
    strategy = (request.form.get("strategy") or "fallback").strip().lower()
    if strategy not in ("single", "fallback", "hybrid"):
        return jsonify({"error": "strategy 只能是 single / fallback / hybrid"}), 400

    options: dict = {"source_lang": source_lang, "target_lang": target_lang,
                     "strategy": strategy}
    raw_options = request.form.get("options")
    if raw_options:
        try:
            parsed = json.loads(raw_options)
            if isinstance(parsed, dict):
                for key, val in parsed.items():
                    if not str(key).startswith("_"):
                        options[key] = val
        except ValueError:
            return jsonify({"error": "options 不是合法 JSON"}), 400

    # 页面手填通道（兜底）：只在内存里用，不落盘、不回显
    override = {}
    base_url = (request.form.get("apiBaseUrl") or "").strip()
    api_key = (request.form.get("apiKey") or "").strip()
    if base_url or api_key:
        override = {"baseUrl": base_url, "apiKey": api_key,
                    "chatModel": (request.form.get("apiModel") or "").strip()}
        options["_override"] = override

    # 多用户：同一个来源每天的任务上限（防一个人把额度用光）
    if J.MAX_JOBS_PER_IP_PER_DAY:
        ip = _client_ip()
        used = int((usage.snapshot().get("perIp", {}).get(ip) or {}).get("jobs") or 0)
        if used >= J.MAX_JOBS_PER_IP_PER_DAY:
            return jsonify({"error": "今天的额度用完了（每个来源每天最多 %d 个任务），"
                                     "明天再来或找管理员" % J.MAX_JOBS_PER_IP_PER_DAY}), 429

    # 扫描件预检：PDF 没有文本层却选了「原位/双语」，提交时就告诉他改用 OCR 模式。
    # 放在这里是为了**不浪费一次排队**——否则要等任务跑起来才报错。
    size = request.content_length or 0
    try:
        fmt = J.JobManager.detect_format(upload.filename)
    except ValueError:
        fmt = None          # 不支持的扩展名交给 submit 统一报错
    if fmt == "pdf" and mode in ("inplace", "bilingual"):
        tmp_path = None
        try:
            import tempfile as _tf
            fd, tmp_path = _tf.mkstemp(suffix=".pdf")
            with os.fdopen(fd, "wb") as fh:
                upload.stream.seek(0)
                fh.write(upload.stream.read())
                upload.stream.seek(0)
            import fitz                                          # noqa: PLC0415
            with fitz.open(tmp_path) as doc:
                has_text = any(page.get_text().strip() for page in doc)
        except Exception:                                        # noqa: BLE001
            has_text = True          # 探测本身出错就不拦，交给管线去报真正的错
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        if not has_text:
            return jsonify({"error": "这份 PDF 没有文本层（整页是图片，属于扫描件）。"
                                     "请把「输出形式」改成「扫描版原位翻译（OCR）」再提交，"
                                     "它会先 OCR 识别文字再翻译。"}), 400

    # 通道能不能解析，提交时就先判掉：别让用户排完队、跑起来了才发现代号写错
    try:
        T.resolve_config(profile or None, override or None)
    except T.TranslateError as exc:
        return jsonify({"error": str(exc)}), 400

    try:
        job = manager.submit(upload.filename, upload.stream, size, mode,
                             options, profile, _client_ip())
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except T.TranslateError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(job.public()), 202


@app.get("/api/jobs")
def list_jobs():
    """只列出**本请求 IP 自己**的任务（管理员可加 token 看全部）。"""
    ip = _client_ip()
    with manager._lock:                                        # noqa: SLF001
        items = [j.public(include_logs=False) for j in manager._jobs.values()
                 if _is_admin() or j.ip == ip]
    items.sort(key=lambda x: x["created"], reverse=True)
    return jsonify({"jobs": items[:40]})


@app.get("/api/jobs/<job_id>")
def job_detail(job_id: str):
    job = manager.get(job_id)
    if not job:
        return jsonify({"error": "任务不存在或已被清理"}), 404
    return jsonify(job.public())


@app.post("/api/jobs/<job_id>/cancel")
def job_cancel(job_id: str):
    if not manager.cancel(job_id):
        return jsonify({"error": "任务不存在或已结束"}), 404
    return jsonify(manager.get(job_id).public())


@app.get("/api/jobs/<job_id>/events")
def job_events(job_id: str):
    """SSE 进度流：progress / log / status 事件，空闲 15 秒发心跳。"""
    job = manager.get(job_id)
    if not job:
        abort(404)

    def stream():
        since = 0
        # 先补发当前状态，避免订阅者错过开头
        yield _sse({"type": "status", "seq": 0, "status": job.status,
                    "progress": job.progress, "error": job.error})
        idle = 0.0
        while True:
            events, since = job.drain_events(since, timeout=15.0)
            if not events:
                idle += 15.0
                yield ": ping\n\n"
                if job.status in ("done", "error", "canceled") and idle >= 15.0:
                    break
                continue
            idle = 0.0
            for ev in events:
                yield _sse(ev)
                if ev.get("type") == "status" and ev.get("status") in ("done", "error", "canceled"):
                    return

    return Response(stream(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",     # 反代别缓冲，否则进度条会被憋住
        # ⚠ 不要在这里设 Connection 之类的 hop-by-hop 头：PEP 3333 禁止 WSGI 应用设置它们，
        #   Flask 自带服务器会放行，但 waitress 会直接抛 AssertionError（线上实测 500）。
        #   长连接的保持由 waitress / 反向代理自己负责。
    })


def _sse(payload: dict) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


@app.get("/api/jobs/<job_id>/download")
def job_download(job_id: str):
    job = manager.get(job_id)
    if not job or not job.result:
        return jsonify({"error": "结果还没生成好"}), 404
    path = os.path.join(job.out_dir, job.result["name"])
    if not os.path.isfile(path):
        return jsonify({"error": "结果文件已被清理"}), 404
    resp = send_file(path, as_attachment=True, download_name=job.result["name"])
    resp.headers["Content-Disposition"] = _content_disposition(job.result["name"])
    resp.headers["Content-Length"] = str(os.path.getsize(path))
    return resp


@app.get("/api/jobs/<job_id>/source")
def job_source(job_id: str):
    job = manager.get(job_id)
    if not job:
        abort(404)
    path = os.path.join(job.in_dir, job.src_name)
    if not os.path.isfile(path):
        return jsonify({"error": "原文件已被清理"}), 404
    resp = send_file(path, as_attachment=True, download_name=job.src_name)
    resp.headers["Content-Disposition"] = _content_disposition(job.src_name)
    return resp


@app.get("/api/jobs/<job_id>/preview")
def job_preview(job_id: str):
    """把 PDF 的某一页渲染成 PNG，用于「译文预览」。docx 暂不支持预览。"""
    job = manager.get(job_id)
    if not job:
        abort(404)
    which = request.args.get("which", "out")
    try:
        page_no = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page_no = 1
    if which == "in":
        path = os.path.join(job.in_dir, job.src_name)
    else:
        if not job.result:
            return jsonify({"error": "结果还没生成好"}), 404
        path = os.path.join(job.out_dir, job.result["name"])
    if not os.path.isfile(path) or not path.lower().endswith(".pdf"):
        return jsonify({"error": "该格式暂不支持预览，请下载后用本地软件打开"}), 404
    try:
        import fitz                                            # noqa: PLC0415
    except ImportError:
        return jsonify({"error": "服务端缺少 PyMuPDF，无法预览"}), 500
    try:
        with fitz.open(path) as doc:
            page_count = len(doc)
            if page_no > page_count:
                return jsonify({"error": f"只有 {page_count} 页"}), 404
            page = doc[page_no - 1]
            pix = page.get_pixmap(dpi=110)
            data = pix.tobytes("png")
        # doc 已关闭，页数在上面先取好（否则这里是 "document closed"）
        return Response(data, mimetype="image/png", headers={
            "Cache-Control": "private, max-age=300",
            "X-Page-Count": str(page_count),
        })
    except Exception as exc:                                   # noqa: BLE001
        return jsonify({"error": f"渲染预览失败：{exc}"}), 500


# ---------------------------------------------------------------- 自检与用量

@app.post("/api/test")
def api_test():
    """连通性自检：真发一次最小请求，让用户知道代号是否可用。"""
    body = request.get_json(silent=True) or {}
    profile = (body.get("profile") or "").strip()
    target_lang = (body.get("target_lang") or "zh-Hans").strip()
    override = None
    if body.get("apiBaseUrl") or body.get("apiKey"):
        override = {"baseUrl": (body.get("apiBaseUrl") or "").strip(),
                    "apiKey": (body.get("apiKey") or "").strip(),
                    "chatModel": (body.get("apiModel") or "").strip()}
    try:
        cfg = T.resolve_config(profile, override)
    except T.TranslateError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    result = T.test_connection(cfg, target_lang)
    return jsonify(result), (200 if result.get("ok") else 502)


@app.get("/api/usage")
def api_usage():
    if not _is_admin():
        return jsonify({"error": "forbidden"}), 403
    snap = usage.snapshot()
    with manager._lock:                                        # noqa: SLF001
        by_status: dict[str, int] = {}
        for job in manager._jobs.values():
            by_status[job.status] = by_status.get(job.status, 0) + 1
    snap["jobs"] = by_status
    snap["uptime"] = int(time.time() - STARTED)
    return jsonify(snap)


@app.post("/api/usage/reset")
def api_usage_reset():
    if not _is_admin():
        return jsonify({"error": "forbidden"}), 403
    usage.reset()
    return jsonify({"ok": True})


@app.get("/api/admin/overview")
def api_admin_overview():
    if not ADMIN_TOKEN:
        return jsonify({"error": "未设置 ADMIN_TOKEN，管理接口已关闭"}), 403
    if not _is_admin():
        return jsonify({"error": "forbidden"}), 403
    with manager._lock:                                        # noqa: SLF001
        recent = [j.public(include_logs=False) for j in
                  sorted(manager._jobs.values(), key=lambda x: x.created, reverse=True)[:30]]
    return jsonify({
        "usage": usage.snapshot(),
        "recent": recent,
        "queue": {"queued": sum(1 for r in recent if r["status"] == "queued"),
                  "running": sum(1 for r in recent if r["status"] == "running")},
        "uptime": int(time.time() - STARTED),
        "pipelineErrors": [f"{k[0]}/{k[1]}: {v}" for k, v in J.IMPORT_ERRORS.items()],
        "version": "1.0.0",
    })


@app.errorhandler(413)
def too_large(_err):
    return jsonify({"error": f"文件超过 {J.MAX_UPLOAD_MB}MB 上限"}), 413


# ---------------------------------------------------------------- 挂到前缀下

class _PrefixMiddleware:
    """在 Caddy 用 ``handle``（不剥前缀）挂载时，把 /doc 前缀去掉再交给 Flask。"""

    def __init__(self, wsgi_app, prefix: str):
        self.wsgi_app = wsgi_app
        self.prefix = prefix

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "")
        if self.prefix and (path == self.prefix or path.startswith(self.prefix + "/")):
            environ["PATH_INFO"] = path[len(self.prefix):] or "/"
            environ["SCRIPT_NAME"] = self.prefix
        return self.wsgi_app(environ, start_response)


if TRUST_PROXY:
    from werkzeug.middleware.proxy_fix import ProxyFix              # noqa: PLC0415
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
if URL_PREFIX:
    app.wsgi_app = _PrefixMiddleware(app.wsgi_app, URL_PREFIX)


# ---------------------------------------------------------------- 中文字体
# 候选清单与判定逻辑在 pipelines/__init__.py（单一来源，管线自己也用它）。
# 这里只负责：启动时确定一个字体并写回环境变量，让任何调用方都拿到同一个值。
def resolve_pdf_font() -> str:
    font = J.default_pdf_font()
    if font:
        os.environ["DOCBRIDGE_PDF_FONT"] = font
    return font


PDF_FONT = resolve_pdf_font()


def main() -> None:
    # systemd/journalctl 下 stdout 是块缓冲的：不改成行缓冲，下面这段启动横幅
    # 会一直卡在缓冲区里，出问题时 journalctl 什么都看不到。
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:                                          # noqa: BLE001
        pass

    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8788"))
    errs = [f"{k[0]}/{k[1]}: {v}" for k, v in J.IMPORT_ERRORS.items()]
    print("=" * 62)
    print("DocBridge 文档翻译 · 启动")
    print(f"  地址        http://{host}:{port}{URL_PREFIX}/")
    print(f"  密钥来源    {T.profiles_path()}")
    print(f"  可用代号    {[p['code'] for p in T.public_profiles()] or '（无）'}")
    print(f"  管线        {[f'{f}/{m}' for f, m in J.PIPELINES]}")
    print(f"  中文字体    {PDF_FONT or 'china-s（内嵌兜底）'}")
    # 并发与策略也打出来：这几个值直接决定速度，出问题时第一眼就该看到
    try:
        _pi = J.PIPELINES.get(("pdf", "inplace"))
        _pf = getattr(_pi, "PREFETCH_WORKERS", "?")
    except Exception:                                          # noqa: BLE001
        _pf = "?"
    print(f"  并发        任务 {J.MAX_WORKERS} · 预取 {_pf} · 批内 {T.CONCURRENCY}"
          f"（DOCBRIDGE_WORKERS / DOCBRIDGE_PREFETCH_WORKERS / TRANSLATE_CONCURRENCY）")
    if J.MAX_JOBS_PER_IP_PER_DAY:
        print(f"  限额        每来源同时 {J.MAX_ACTIVE_PER_IP} 个 · 每天 "
              f"{J.MAX_JOBS_PER_IP_PER_DAY} 个")
    if not PDF_FONT:
        print("  ⚠ 未找到系统中文 字体：PDF 译文将用内置 china-s；")
        print("    它在部分平台（如 Linux + 新版 MuPDF）缺 ToUnicode，")
        print("    译文会「看得见但复制不出来」。装字体即可解决：")
        print("      apt install fonts-noto-cjk")
    if errs:
        print("  ⚠ 管线加载失败：")
        for e in errs:
            print(f"      - {e}")
    if not ADMIN_TOKEN:
        print("  ℹ 未设置 ADMIN_TOKEN，管理接口已关闭")
    print("=" * 62)
    try:
        from waitress import serve                                # noqa: PLC0415
        print("  用 waitress 启动（生产模式）")
        serve(app, host=host, port=port, threads=8, channel_timeout=600)
    except ImportError:
        app.run(host=host, port=port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
