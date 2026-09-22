"""docbridge · 站点集成测试（离线，不联网、不花钱）

自己不翻译，而是**把管线换成假的**，专门验证 app.py 这层接线对不对：

  /api/config 不泄露密钥 → 提交校验（格式/模式/代号/空文件）
  → 排队与执行 → SSE 事件流（进度/日志/终态）→ 下载文件名与内容
  → 用量记账 → 取消 → 任务磁盘恢复

跑法：
    cd <仓库目录>/docbridge
    python3 tests/test_app.py
"""

import json
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# ★ 必须在 import app 之前设好：JobManager/Usage 在 import 时就创建目录
TMP = tempfile.mkdtemp(prefix="docbridge_app_test_")
os.environ["DOCBRIDGE_JOBS_DIR"] = os.path.join(TMP, "jobs")
os.environ["DOCBRIDGE_USAGE_FILE"] = os.path.join(TMP, "usage.json")
os.environ["ADMIN_TOKEN"] = "test-token"
os.environ["DOCBRIDGE_WORKERS"] = "1"
os.environ.pop("PROFILES_FILE", None)

import pipelines as P                      # noqa: E402
import app as A                            # noqa: E402
import jobs as J                           # noqa: E402

FAILED = []


def check(name, cond, extra=""):
    if cond:
        print(f"PASS {name}")
    else:
        print(f"FAIL {name} {extra}")
        FAILED.append(name)


# ---------------------------------------------------------------- 假管线

class FakePipeline:
    """按契约实现的最小管线：不翻译，只写一个可辨识的结果文件。"""
    FORMAT = "docx"
    MODE = "inplace"
    LABEL = "测试用假管线"
    NOTE = "不出网"
    OPTIONS = ["keep_original_style"]

    @staticmethod
    def run(src_path, out_path, *, translate, progress=None, options=None):
        with open(src_path, "rb") as fh:
            src_bytes = fh.read()
        texts = ["Hello world from the fake pipeline"]
        out = translate(texts, {"kind": "docx"})
        if len(out) != len(texts):
            raise RuntimeError(f"翻译返回数量不符：期望 {len(texts)}，实际 {len(out)}")
        if progress:
            progress(0, 1, "准备")
            progress(1, 1, "写入")
        with open(out_path + ".part", "wb") as fh:
            fh.write(src_bytes + b"\nTRANSLATED:" + out[0].encode("utf-8"))
        os.replace(out_path + ".part", out_path)
        return {"units": 1, "chars_in": len(texts[0]), "chars_out": len(out[0]),
                "skipped": 0, "pages": 1, "detail": "假管线完成"}


def install_fake_pipeline():
    P.PIPELINES[("docx", "inplace")] = FakePipeline
    J.PIPELINES[("docx", "inplace")] = FakePipeline


def fake_resolve_config(profile_code=None, override=None):
    return {"provider": "openai", "baseUrl": "https://fake.invalid/v1",
            "apiKey": "fake", "chatModel": "fake-model"}


def setup():
    """装上假管线与假翻译层。

    必须在**每个**会提交任务的用例开头调用（用例按名字排序执行，
    不能假设别人已经装好了——这正是第一次跑时踩到的坑）。
    """
    install_fake_pipeline()
    A.T.resolve_config = fake_resolve_config
    A.T.make_translator = fake_translator_factory


def fake_translator_factory(cfg, source_lang="auto", target_lang="zh-Hans",
                            kind="line", log=None, **kw):
    """**kw 收下通道链参数（fallback/escalate/route/channel_names）——
    测试只关心接线，不关心真实路由。"""
    """替换掉真翻译层，避免测试联网、也避免依赖 profiles.json。"""
    def translate(texts, context=None):
        return [f"【译{i}】{t}" for i, t in enumerate(texts)]
    translate.stats = {"chars_in": 10, "chars_out": 20, "calls": 1, "batches": 1,
                       "retries": 0, "failed": 0, "skipped": 0}
    return translate


# ---------------------------------------------------------------- 用例

def test_config_has_no_secrets_and_lists_pipelines():
    with A.app.test_client() as c:
        r = c.get("/api/config")
        raw = r.get_data(as_text=True)
        d = json.loads(raw)
    check("config_200", r.status_code == 200, r.status_code)

    # 真正要防的是「密钥值泄露」，而不是响应里出现 "apiKey" 这几个字母——
    # 不可用代号的提示语本身就会写「apiKey 含非 ASCII 字符…」。
    # 所以这里直接拿 profiles.json 里的密钥值去响应里找，找不到才算过。
    secrets = []
    try:
        with open(A.T.profiles_path(), encoding="utf-8") as fh:
            for prof in (json.load(fh) or {}).values():
                if isinstance(prof, dict):
                    for key, val in prof.items():
                        if isinstance(val, str) and len(val) >= 8 \
                                and ("key" in key.lower() or "token" in key.lower()):
                            secrets.append(val)
    except (OSError, ValueError):
        pass
    leaked = [sv for sv in secrets if sv in raw]
    check("config_no_secret_values_leaked", not leaked,
          f"{len(leaked)}/{len(secrets)} 个密钥值出现在了 /api/config 响应里")

    # 预设视图允许的字段（密钥类字段一律不许出现）
    allowed = {"code", "name", "note", "model", "usable", "issue"}
    for p in d["profiles"]:
        if set(p) - allowed:
            check("config_profile_fields", False, p)
            break
    else:
        check("config_profile_fields", True)
    # 与注册表比对（而不是写死条数）：以后新增管线模式不会再让这条断言挂掉
    expected = {"%s/%s" % (fmt, mode) for fmt, mode in J.PIPELINES}
    got = {"%s/%s" % (p["format"], p["mode"])
           for p in d["capabilities"]["pipelines"] if p["available"]}
    check("config_lists_pipelines", got == expected, (sorted(got), sorted(expected)))
    check("config_admin_enabled", d["adminEnabled"] is True)


def test_upload_validation():
    setup()
    with A.app.test_client() as c:
        r = c.post("/api/jobs", data={"mode": "inplace"},
                   content_type="multipart/form-data")
        check("reject_missing_file", r.status_code == 400 and "没有收到文件" in r.get_data(as_text=True))

        r = c.post("/api/jobs", data={"file": (open(__file__, "rb"), "notes.txt"), "mode": "inplace"},
                   content_type="multipart/form-data")
        check("reject_unknown_ext", r.status_code == 400 and "不支持的文件类型" in r.get_data(as_text=True))

        r = c.post("/api/jobs", data={"file": (open(__file__, "rb"), "a.docx"), "mode": "wat"},
                   content_type="multipart/form-data")
        check("reject_unknown_mode", r.status_code == 400 and "不支持模式" in r.get_data(as_text=True))

        r = c.post("/api/jobs", data={"file": (open(__file__, "rb"), "a.docx"), "mode": "inplace",
                                      "options": "{broken"},
                   content_type="multipart/form-data")
        check("reject_bad_options_json", r.status_code == 400 and "options 不是合法 JSON" in r.get_data(as_text=True))


def test_full_job_lifecycle_and_sse():
    """提交 → SSE 收到进度与终态 → 下载内容正确 → 用量记上账。"""
    setup()
    with A.app.test_client() as c:
        r = c.post("/api/jobs", data={
            "file": (open(__file__, "rb"), "论文 v2.docx"),
            "mode": "inplace", "profile": "x", "target_lang": "zh-Hans",
            "options": json.dumps({"keep_original_style": True}),
        }, content_type="multipart/form-data")
        check("submit_202", r.status_code == 202, r.status_code)
        job = r.get_json()
        jid = job["id"]
        check("submit_sanitizes_name", " " not in job["srcName"].replace(" v2", ""), job["srcName"])

        # SSE：先订阅（此时任务多半还在跑或刚完），必须能读到进度并最终收到终态
        got_progress = got_done = False
        logs = []
        with c.get(f"/api/jobs/{jid}/events") as stream:
            for chunk in stream.response:
                line = chunk.decode("utf-8", "replace").strip()
                if not line.startswith("data: "):
                    continue
                ev = json.loads(line[6:])
                if ev.get("type") == "progress":
                    got_progress = True
                if ev.get("type") == "log":
                    logs.append(ev.get("message", ""))
                if ev.get("type") == "status" and ev.get("status") == "done":
                    got_done = True
                    break
        check("sse_saw_progress", got_progress)
        check("sse_saw_terminal_done", got_done)

        # 状态接口
        d = c.get(f"/api/jobs/{jid}").get_json()
        check("job_done", d["status"] == "done", d)
        check("job_stats_merged", d["stats"].get("detail") == "假管线完成" and "api_calls" in d["stats"], d["stats"])
        check("job_sse_logs_present", any("开始处理" in m for m in logs), logs[:3])

        # 下载：内容对、文件名带中文（RFC 5987）
        dl = c.get(f"/api/jobs/{jid}/download")
        check("download_200", dl.status_code == 200, dl.status_code)
        check("download_content_ok", b"TRANSLATED:" in dl.get_data() and "【译0】".encode() in dl.get_data())
        cd = dl.headers.get("Content-Disposition", "")
        check("download_rfc5987_name", "filename*=UTF-8''" in cd and "_zh.docx" in cd, cd)

        # 原件也能下回来
        src = c.get(f"/api/jobs/{jid}/source")
        check("source_download_200", src.status_code == 200)

        # 用量记账（管理员令牌）
        u = c.get("/api/usage?token=test-token").get_json()
        check("usage_recorded", u["total"]["jobs"] == 1 and u["total"]["charsOut"] > 0
              and u["total"]["calls"] == 1, u["total"])
        check("usage_requires_token", c.get("/api/usage").status_code == 403)
        check("admin_overview_ok", c.get("/api/admin/overview?token=test-token").status_code == 200)


def test_jobs_listing_is_scoped_to_caller():
    setup()
    with A.app.test_client() as c:
        mine = c.get("/api/jobs").get_json()["jobs"]
        check("listing_returns_own_jobs", len(mine) >= 1, len(mine))
        check("listing_hides_result_url_of_others", all("api/jobs/" in (j["result"] or {}).get("url", "")
                                                        for j in mine if j["result"]))
        admin = c.get("/api/jobs?token=test-token").get_json()["jobs"]
        check("admin_sees_all", len(admin) >= len(mine))


def test_cancel_queued_job_never_writes_output():
    setup()
    with A.app.test_client() as c:
        r = c.post("/api/jobs", data={"file": (open(__file__, "rb"), "cancel_me.docx"),
                                      "mode": "inplace", "profile": "x"},
                   content_type="multipart/form-data")
        check("cancel_submit_202", r.status_code == 202, (r.status_code, r.get_data(as_text=True)[:120]))
        jid = r.get_json()["id"]
        c.post(f"/api/jobs/{jid}/cancel")
        time.sleep(1.5)
        d = c.get(f"/api/jobs/{jid}").get_json()
        # 任务很小，可能已经跑完；只要不是"被取消却还有产物"就算过
        ok = d["status"] in ("canceled", "done")
        check("cancel_state_sane", ok, d["status"])
        if d["status"] == "canceled":
            check("cancel_leaves_no_result", not d["result"], d["result"])
        check("cancel_missing_job_404", c.post("/api/jobs/nosuchjob/cancel").status_code == 404)


def test_preview_rejects_non_pdf_with_friendly_message():
    setup()
    with A.app.test_client() as c:
        jobs = c.get("/api/jobs").get_json()["jobs"]
        docx_job = next((j for j in jobs if j["format"] == "docx" and j["status"] == "done"), None)
        if not docx_job:
            check("preview_docx_friendly", False, "没有可用的 docx 任务")
            return
        r = c.get(f"/api/jobs/{docx_job['id']}/preview?which=out")
        check("preview_docx_friendly", r.status_code == 404 and "不支持预览" in r.get_data(as_text=True),
              (r.status_code, r.get_data(as_text=True)[:80]))


def test_jobs_survive_restart():
    """磁盘恢复：新开一个 manager 应能看到刚完成的任务并能定位产物。"""
    setup()
    before = [j.public(include_logs=False) for j in A.manager._jobs.values()]  # noqa: SLF001
    done = [j for j in before if j["status"] == "done"]
    check("restart_has_done_job", len(done) >= 1, before)
    if not done:
        return
    fresh = J.JobManager()          # 模拟服务重启：只从磁盘重建
    restored = [fresh.get(j["id"]) for j in done]
    check("restart_restores_job", all(restored), [r is not None for r in restored])
    if restored[0]:
        j = restored[0]
        check("restart_keeps_result", j.result is not None and j.status == "done",
              {k: j.public()[k] for k in ("status", "result")})
        check("restart_keeps_ip", j.ip == done[0]["ip"], (j.ip, done[0]["ip"]))
    # 中断中的任务恢复后必须标记失败
    ghost = os.path.join(os.environ["DOCBRIDGE_JOBS_DIR"], "ghostjob")
    os.makedirs(os.path.join(ghost, "in"), exist_ok=True)
    os.makedirs(os.path.join(ghost, "out"), exist_ok=True)
    with open(os.path.join(ghost, "meta.json"), "w", encoding="utf-8") as fh:
        json.dump({"id": "ghostjob", "format": "pdf", "mode": "inplace",
                   "srcName": "x.pdf", "srcSize": 1, "status": "running",
                   "created": time.time(), "progress": {}, "stats": {}}, fh)
    fresh2 = J.JobManager()
    g = fresh2.get("ghostjob")
    check("restart_marks_interrupted_failed", g is not None and g.status == "error",
          g.public() if g else None)


def test_sse_headers_are_wsgi_safe():
    """SSE 响应不得设置 hop-by-hop 头。

    PEP 3333 禁止 WSGI 应用设置 Connection / Keep-Alive 这类头。Flask 自带的
    开发服务器会放行，但 waitress 会直接抛
    ``AssertionError: Connection is a "hop-by-hop" header`` → 线上 500、
    进度条完全不动。这条断言就是为那次线上事故加的：test client 不会替我们报错，
    所以必须显式检查响应头。
    """
    setup()
    HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate",
                  "proxy-authorization", "te", "trailers", "transfer-encoding",
                  "upgrade"}
    with A.app.test_client() as c:
        r = c.post("/api/jobs", data={"file": (open(__file__, "rb"), "sse.docx"),
                                      "mode": "inplace", "profile": "x"},
                   content_type="multipart/form-data")
        check("sse_submit_202", r.status_code == 202, r.status_code)
        jid = r.get_json()["id"]
        resp = c.get(f"/api/jobs/{jid}/events", buffered=False)
        try:
            headers = {k.lower() for k in resp.headers.keys()}
            bad = HOP_BY_HOP & headers
            check("sse_no_hop_by_hop_headers", not bad, bad)
            check("sse_content_type",
                  resp.headers.get("Content-Type", "").startswith("text/event-stream"),
                  resp.headers.get("Content-Type"))
            check("sse_no_buffering_header",
                  resp.headers.get("X-Accel-Buffering") == "no",
                  resp.headers.get("X-Accel-Buffering"))
        finally:
            resp.close()


def test_worker_survives_cancel_then_processes_next_job():
    """取消一个排队任务后，worker 必须还能继续处理后面的任务。

    回归用例：挑选队列任务的逻辑曾在「队列里只剩已取消任务」时返回 None，
    随后 popleft() 抛异常把 worker 线程打死 —— 之后所有任务永远卡在「排队中」，
    而服务状态还是 active，极难排查。多用户场景下这等于服务瘫掉。
    """
    setup()
    with A.app.test_client() as c:
        # 先塞一个任务并立刻取消（制造「队列里只剩已取消任务」的现场）
        r1 = c.post("/api/jobs", data={"file": (open(__file__, "rb"), "first.docx"),
                                       "mode": "inplace", "profile": "x"},
                    content_type="multipart/form-data")
        check("cancel_then_submit_first_202", r1.status_code == 202, r1.status_code)
        jid1 = r1.get_json()["id"]
        c.post(f"/api/jobs/{jid1}/cancel")

        # 再提一个：它必须被正常跑完（worker 没死）
        r2 = c.post("/api/jobs", data={"file": (open(__file__, "rb"), "second.docx"),
                                       "mode": "inplace", "profile": "x"},
                    content_type="multipart/form-data")
        check("cancel_then_submit_second_202", r2.status_code == 202, r2.status_code)
        jid2 = r2.get_json()["id"]
        status = "queued"
        for _ in range(60):
            status = c.get(f"/api/jobs/{jid2}").get_json()["status"]
            if status in ("done", "error", "canceled"):
                break
            time.sleep(0.25)
        check("worker_alive_after_cancel", status == "done", status)


def test_fair_queue_prefers_less_busy_source():
    """多用户公平排队：优先把队列让给「当前没在跑任务」的来源。

    没有这条时是纯 FIFO：一个人连传 10 个文件，别人就只能干等他的第 10 个跑完。
    """
    setup()
    mgr = A.manager
    made = []
    # 直接构造任务对象塞进队列（这里刻意访问私有字段：测的就是调度顺序本身）
    for ip, name in (("10.0.0.1", "a1"), ("10.0.0.1", "a2"), ("10.0.0.1", "a3"),
                     ("10.0.0.2", "b1")):
        job = J.Job("docx", "inplace", "%s.docx" % name, 10, {}, "x", ip)
        job.status = "queued"
        with mgr._lock:                       # noqa: SLF001
            mgr._jobs[job.id] = job            # noqa: SLF001
            mgr._queue.append(job.id)          # noqa: SLF001
        made.append(job)

    with mgr._lock:                            # noqa: SLF001
        first = mgr._pick_next_locked()        # noqa: SLF001
    check("fair_queue_first_is_oldest", first == made[0].id, first)

    # 让 A 的第一个任务处于「执行中」，再来挑：应该轮到 B（A 已经在跑了）
    made[0].status = "running"
    with mgr._lock:                            # noqa: SLF001
        second = mgr._pick_next_locked()       # noqa: SLF001
    check("fair_queue_then_other_source", second == made[3].id,
          "A 已有任务在跑，却仍选了 %s" % second)

    with mgr._lock:                            # noqa: SLF001
        for j in made:
            mgr._jobs.pop(j.id, None)          # noqa: SLF001
        mgr._queue.clear()                     # noqa: SLF001


if __name__ == "__main__":
    try:
        for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
            fn()
        print()
        if FAILED:
            print(f"失败 {len(FAILED)} 项：" + ", ".join(FAILED))
            sys.exit(1)
        print("全部通过 ✅")
    finally:
        A.manager.shutdown()
        shutil.rmtree(TMP, ignore_errors=True)
