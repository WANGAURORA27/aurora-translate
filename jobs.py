"""docbridge · 任务队列

上传的文件落到每个任务独立的目录，排队交给管线线程执行；
进度、日志、产物都记在 Job 上，前端通过 SSE 订阅。

为什么每个任务一个独立目录：既有 PDF 原位流水线用「文件名」做翻译缓存键，
两个用户上传同名文件会互相污染缓存。任务目录天然把它们隔开
（缓存落在任务目录内，跑完随任务一起清理）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import traceback
import uuid
from collections import deque

import translator as T
from pipelines import (FORMATS, IMPORT_ERRORS, PIPELINES,  # noqa: F401
                        default_pdf_font, pipeline_info)

ROOT = os.path.dirname(os.path.abspath(__file__))

JOBS_DIR = os.environ.get("DOCBRIDGE_JOBS_DIR") or os.path.join(ROOT, "_jobs")
# 多用户：默认开 3 个 worker。注意 PDF 原位管线内部用模块级锁串行（要临时替换上游全局函数），
# 所以多个 PDF 任务仍会排队；受益的是 Word 任务与 OCR 任务，它们能真正并行起来。
MAX_WORKERS = max(1, int(os.environ.get("DOCBRIDGE_WORKERS", "3")))
# 同一个来源同时排队/执行的任务上限
MAX_ACTIVE_PER_IP = max(1, int(os.environ.get("DOCBRIDGE_MAX_ACTIVE_PER_IP", "2")))
# 同一个来源每天的任务上限（0 = 不限）
MAX_JOBS_PER_IP_PER_DAY = max(0, int(os.environ.get("DOCBRIDGE_MAX_JOBS_PER_IP_PER_DAY", "0")))
JOB_TTL_HOURS = float(os.environ.get("DOCBRIDGE_JOB_TTL_HOURS", "6"))
SHARED_CACHE_TTL_HOURS = float(os.environ.get("DOCBRIDGE_SHARED_CACHE_TTL_HOURS", "72"))
MAX_JOBS_KEPT = int(os.environ.get("DOCBRIDGE_MAX_JOBS", "500"))
MAX_EVENTS = 2000

# 允许的扩展名 → 格式名
EXT_FORMATS = {".pdf": "pdf", ".docx": "docx"}
MAX_UPLOAD_MB = int(os.environ.get("DOCBRIDGE_MAX_UPLOAD_MB", "200"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024


class JobCancelled(BaseException):
    """用户取消任务。

    故意继承 BaseException：管线内部常有 ``except Exception`` 兜底，
    若继承 Exception 会被吞掉，取消就失效了。
    """


def safe_filename(name: str, fallback: str = "document") -> str:
    """把上传文件名洗干净：去掉路径、控制字符与危险符号，保留原扩展名。"""
    base = os.path.basename((name or "").replace("\\", "/")).strip()
    ext = os.path.splitext(base)[1].lower()
    stem = os.path.splitext(base)[0]
    stem = re.sub(r"[\x00-\x1f/\\:*?\"<>|]+", "_", stem).strip(" ._")
    stem = re.sub(r"_{2,}", "_", stem)[:80] or fallback
    return stem + ext


# ---------------------------------------------------------------- 通道策略
# 强模型的粗略判据：中转站通常转的是 GPT/Claude 这类旗舰；DeepSeek/Qwen 等算便宜档。
# 仅用于「混合路由」时挑攻坚通道，不参与计费判断。
# 注意别把 "pro" 当强信号：硅基流动的档位前缀就叫 "Pro/deepseek-ai/..."，
# 会把便宜的 DeepSeek 误判成旗舰。
_STRONG_HINTS = ("gpt-5", "gpt5", "gpt-6", "gpt6", "claude", "gemini",
                 "o1-", "o3-", "opus", "sonnet", "grok")
_CHEAP_HINTS = ("deepseek", "qwen", "glm", "mini", "flash", "lite", "haiku", "small")


def _looks_strong(model: str) -> bool:
    low = (model or "").lower()
    if any(h in low for h in _CHEAP_HINTS):
        return False
    return any(h in low for h in _STRONG_HINTS)


def pick_channels(primary_code: str, strategy: str = "fallback") -> dict:
    """按策略挑出「备用通道」与「攻坚通道」。

    * ``single``   —— 只用选中的通道；
    * ``fallback`` —— 另外挑一个可用通道兜底（默认）；
    * ``hybrid``   —— 难批次（术语表/表格）走攻坚通道，其余走选中的通道。

    可用 ``DOCBRIDGE_FALLBACK`` / ``DOCBRIDGE_ESCALATE`` 环境变量指定具体代号，
    不指定就自动挑：备用=任意另一个可用通道；攻坚=模型看着更强的那个。
    """
    if strategy == "single":
        return {"fallback": None, "escalate": None, "primary": primary_code}
    usable = [p for p in T.public_profiles() if p["usable"]]
    others = [p for p in usable if p["code"] != primary_code]
    primary = next((p for p in usable if p["code"] == primary_code), None)

    def _resolve(env_name: str, pool: list[dict]) -> str | None:
        want = (os.environ.get(env_name) or "").strip()
        if want and any(p["code"] == want for p in pool):
            return want
        return pool[0]["code"] if pool else None

    fallback_code = _resolve("DOCBRIDGE_FALLBACK", others)
    if strategy != "hybrid":
        return {"fallback": fallback_code, "escalate": None, "primary": primary_code}

    strong_pool = [p for p in others if _looks_strong(p["model"])]
    weak_pool = [p for p in others if not _looks_strong(p["model"])]
    if primary and _looks_strong(primary["model"]):
        # 选中的已经是强通道：别拿它当攻坚通道，攻坚=它自己（顺序里本来就在前）
        escalate_code = primary_code
        fallback_code = _resolve("DOCBRIDGE_FALLBACK", weak_pool or others)
    else:
        escalate_code = _resolve("DOCBRIDGE_ESCALATE", strong_pool or others)
        fallback_code = _resolve(
            "DOCBRIDGE_FALLBACK",
            [p for p in (others) if p["code"] != escalate_code] or others)
    return {"fallback": fallback_code, "escalate": escalate_code, "primary": primary_code}


class Job:
    def __init__(self, fmt: str, mode: str, src_name: str, src_size: int,
                 options: dict, profile: str, ip: str):
        self.id = uuid.uuid4().hex[:12]
        self.fmt = fmt
        self.mode = mode
        self.src_name = src_name
        self.src_size = src_size
        self.options = options
        self.profile = profile
        self.ip = ip
        self.status = "queued"              # queued|running|done|error|canceled
        self.created = time.time()
        self.started: float | None = None
        self.finished: float | None = None
        self.progress = {"done": 0, "total": 0, "note": "排队中"}
        self.stats: dict = {}
        self.result: dict | None = None
        self.error: str | None = None

        self.dir = os.path.join(JOBS_DIR, self.id)
        self.in_dir = os.path.join(self.dir, "in")
        self.out_dir = os.path.join(self.dir, "out")
        self._init_runtime()

    def _init_runtime(self) -> None:
        self.logs: deque[str] = deque(maxlen=200)
        self.cancel_requested = False
        self._events: list[dict] = []
        self._cond = threading.Condition()
        self._seq = 0

    @classmethod
    def from_meta(cls, meta: dict) -> "Job":
        """从磁盘上的 meta.json 恢复一个已结束的任务。

        服务重启后内存里的任务会丢，但产物与 meta.json 还在；
        恢复出来用户仍能下载结果，也便于管理员回溯。
        中断时还在跑的任务标注为失败（产物不完整，不可信）。
        """
        job = cls.__new__(cls)
        job.id = str(meta.get("id") or "")
        job.fmt = str(meta.get("format") or "")
        job.mode = str(meta.get("mode") or "")
        job.src_name = str(meta.get("srcName") or "")
        job.src_size = int(meta.get("srcSize") or 0)
        job.options = {}
        job.profile = str(meta.get("profile") or "")
        job.ip = str(meta.get("ip") or "-")
        job.status = str(meta.get("status") or "error")
        job.created = float(meta.get("created") or time.time())
        job.finished = float(meta.get("finished") or job.created)
        job.started = job.finished
        job.progress = meta.get("progress") or {"done": 0, "total": 0, "note": ""}
        job.stats = meta.get("stats") or {}
        job.result = meta.get("result") or None
        job.error = meta.get("error")
        job.dir = os.path.join(JOBS_DIR, job.id)
        job.in_dir = os.path.join(job.dir, "in")
        job.out_dir = os.path.join(job.dir, "out")
        job._init_runtime()

        if job.status in ("queued", "running"):
            job.status = "error"
            job.error = "服务重启导致任务中断，请重新提交"
            job.log("服务重启，任务中断（产物不完整，已标记失败）")
        if job.result:
            if not os.path.isfile(os.path.join(job.out_dir, job.result.get("name", ""))):
                job.result = None
        if not job.id:
            raise ValueError("meta.json 缺少 id")
        return job

    # ------------------------------------------------------------ 状态更新
    def emit(self, kind: str, **payload) -> None:
        with self._cond:
            self._seq += 1
            self._events.append({"seq": self._seq, "type": kind,
                                 "t": round(time.time() - self.created, 3), **payload})
            if len(self._events) > MAX_EVENTS:
                del self._events[:len(self._events) - MAX_EVENTS]
            self._cond.notify_all()

    def log(self, msg: str) -> None:
        line = str(msg).rstrip()
        if not line:
            return
        self.logs.append(line)
        self.emit("log", message=line)

    def set_status(self, status: str, error: str | None = None) -> None:
        self.status = status
        if error:
            self.error = error
        if status in ("done", "error", "canceled"):
            self.finished = time.time()
        self.emit("status", status=status, error=self.error,
                  progress=self.progress)

    def set_progress(self, done: int, total: int, note: str = "") -> None:
        # 单调递增，避免个别管线回调乱序把进度条拽回去
        done = max(int(done), self.progress["done"])
        total = max(int(total), self.progress["total"])
        self.progress = {"done": done, "total": total,
                         "note": note or self.progress["note"]}
        self.emit("progress", done=done, total=total, note=self.progress["note"])
        if self.cancel_requested:
            raise JobCancelled()

    def public(self, include_logs: bool = True) -> dict:
        out = {
            "id": self.id,
            "status": self.status,
            "format": self.fmt,
            "mode": self.mode,
            "srcName": self.src_name,
            "srcSize": self.src_size,
            "profile": self.profile,
            "progress": dict(self.progress),
            "stats": dict(self.stats),
            "result": dict(self.result) if self.result else None,
            "error": self.error,
            "created": self.created,
            "finished": self.finished,
            "ip": self.ip,
            "elapsed": round((self.finished or time.time()) - (self.started or self.created), 1),
        }
        if include_logs:
            out["logs"] = list(self.logs)[-60:]
        return out

    # ------------------------------------------------------------ 供 SSE 订阅
    def drain_events(self, since_seq: int, timeout: float = 15.0):
        """返回 (事件列表, since_seq)；超时返回空列表用于发心跳。"""
        with self._cond:
            if not self._events or self._events[-1]["seq"] <= since_seq:
                self._cond.wait(timeout=timeout)
            events = [e for e in self._events if e["seq"] > since_seq]
        if events:
            since_seq = events[-1]["seq"]
        return events, since_seq


class JobManager:
    def __init__(self):
        os.makedirs(JOBS_DIR, exist_ok=True)
        self._jobs: dict[str, Job] = {}
        self._order: deque[str] = deque()
        self._lock = threading.RLock()
        self._queue: deque[str] = deque()
        self._wake = threading.Condition(self._lock)
        self._stop = False
        self.on_job_finished = None      # app.py 挂用量统计回调
        self._workers = [
            threading.Thread(target=self._worker, name=f"docbridge-w{i}", daemon=True)
            for i in range(MAX_WORKERS)
        ]
        for w in self._workers:
            w.start()
        self._hydrate()
        self._janitor = threading.Thread(target=self._janitor_loop, daemon=True)
        self._janitor.start()

    def _hydrate(self) -> None:
        """从磁盘恢复历史任务（服务重启后仍能下载已完成的产物）。"""
        restored = 0
        try:
            entries = os.listdir(JOBS_DIR)
        except OSError:
            return
        for name in entries:
            meta_path = os.path.join(JOBS_DIR, name, "meta.json")
            if not os.path.isfile(meta_path):
                continue
            try:
                with open(meta_path, encoding="utf-8") as fh:
                    job = Job.from_meta(json.load(fh))
            except (OSError, ValueError, KeyError):
                continue
            if job.id in self._jobs:
                continue
            self._jobs[job.id] = job
            self._order.append(job.id)
            restored += 1
        if restored:
            # 按创建时间重排，保证「最近任务」顺序正确、淘汰时先删最旧的
            try:
                self._order = deque(sorted(
                    self._order,
                    key=lambda jid: self._jobs[jid].created))
                self._trim_locked()
            except KeyError:
                pass
            print(f"  ↻ 从磁盘恢复了 {restored} 个历史任务")

    # ------------------------------------------------------------ 提交/查询
    @staticmethod
    def detect_format(filename: str) -> str:
        ext = os.path.splitext(filename or "")[1].lower()
        fmt = EXT_FORMATS.get(ext)
        if not fmt:
            raise ValueError(f"不支持的文件类型 {ext or '（无扩展名）'}；目前支持 "
                             + "、".join(sorted(EXT_FORMATS)))
        return fmt

    def submit(self, src_name: str, stream, size: int, mode: str,
               options: dict, profile: str, ip: str) -> Job:
        """把上传流写进任务目录并落队；返回 Job（调用方负责返回 job.public()）。"""
        if size and size > MAX_UPLOAD_BYTES:
            raise ValueError(f"文件太大（{size / 1048576:.1f}MB），上限 {MAX_UPLOAD_MB}MB")
        fmt = self.detect_format(src_name)
        if (fmt, mode) not in PIPELINES:
            raise ValueError(f"{fmt} 不支持模式 {mode!r}")
        if self._active_count_for_ip(ip) >= MAX_ACTIVE_PER_IP:
            raise ValueError("你在排队/执行中的任务已有 %d 个，请等它们跑完再提交"
                             % MAX_ACTIVE_PER_IP)

        job = Job(fmt, mode, safe_filename(src_name), size, options, profile, ip)
        os.makedirs(job.in_dir, exist_ok=True)
        os.makedirs(job.out_dir, exist_ok=True)
        target = os.path.join(job.in_dir, job.src_name)
        written = 0
        with open(target, "wb") as fh:
            while True:
                chunk = stream.read(1024 * 256)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    fh.close()
                    shutil.rmtree(job.dir, ignore_errors=True)
                    raise ValueError(f"文件超过 {MAX_UPLOAD_MB}MB 上限")
                fh.write(chunk)
        if written == 0:
            shutil.rmtree(job.dir, ignore_errors=True)
            raise ValueError("上传的文件是空的")
        job.src_size = written
        job.emit("queued", message="已排队")

        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._queue.append(job.id)
            self._trim_locked()
            self._wake.notify()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def _pick_next_locked(self) -> str | None:
        """挑下一个任务：优先「当前在跑的任务最少」的来源 IP（近似轮转）。

        多用户场景下 FIFO 会让先来的人把队列占满（他连传 10 个文件，别人只能干等）；
        按来源轮转后，每个人的第一个任务都会排在别人的第二个任务前面。
        """
        best_id, best_key = None, None
        for jid in list(self._queue):
            job = self._jobs.get(jid)
            if not job or job.status != "queued":
                try:
                    self._queue.remove(jid)
                except ValueError:
                    pass
                continue
            running = sum(1 for j in self._jobs.values()
                          if j.ip == job.ip and j.status == "running")
            key = (running, job.created)
            if best_key is None or key < best_key:
                best_id, best_key = jid, key
        if best_id:
            try:
                self._queue.remove(best_id)
            except ValueError:
                return None
        return best_id

    def queue_stats(self) -> dict:
        """给前端/管理页看的队列概况。"""
        with self._lock:
            queued = sum(1 for j in self._jobs.values() if j.status == "queued")
            running = sum(1 for j in self._jobs.values() if j.status == "running")
            ips = {j.ip for j in self._jobs.values() if j.status in ("queued", "running")}
        return {"queued": queued, "running": running, "workers": MAX_WORKERS,
                "activeSources": len(ips), "maxActivePerIp": MAX_ACTIVE_PER_IP,
                "maxJobsPerIpPerDay": MAX_JOBS_PER_IP_PER_DAY}

    def _active_count_for_ip(self, ip: str) -> int:
        with self._lock:
            return sum(1 for j in self._jobs.values()
                       if j.ip == ip and j.status in ("queued", "running"))

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if not job or job.status in ("done", "error", "canceled"):
            return False
        job.cancel_requested = True
        if job.status == "queued":
            # 还没开始跑：直接落终态。
            # ★ 不能走 set_progress()：那里会因为 cancel_requested 抛 JobCancelled，
            #   异常冒到 Flask 视图就变成 HTTP 500（「取消」失败）。
            job.progress = {"done": job.progress.get("done", 0),
                            "total": job.progress.get("total", 0),
                            "note": "已取消"}
            job.log("任务已取消（尚未开始）")
            job.set_status("canceled")
        else:
            # 已经跑起来了：只能合作式取消，等管线走到下一个进度点
            job.log("已请求取消，正在收尾…")
        return True

    # ------------------------------------------------------------ 工作线程
    def _worker(self) -> None:
        """工作线程主循环。

        ⚠ 整个循环体套了兜底 try：worker 一旦因为意外异常退出，**之后所有任务都不会再被处理**
        （表现为「排队中的任务永远不动」），而且服务看着还是 active —— 极难排查。
        线上真踩过一次：挑选队列任务的逻辑在"队列里只剩已取消任务"时返回 None，
        随后的 popleft() 抛异常把 worker 打死了。
        """
        while True:
            try:
                with self._wake:
                    while not self._queue and not self._stop:
                        self._wake.wait(timeout=5)
                    if self._stop:
                        return
                    job_id = self._pick_next_locked()
                if job_id is None:
                    # 队列里剩下的都是已取消/已结束的条目，已由 _pick_next_locked 清掉
                    continue
                job = self.get(job_id)
                if not job or job.status != "queued":
                    continue
                try:
                    self._run_job(job)
                except JobCancelled:
                    job.set_status("canceled")
                    job.log("任务已取消")
                except Exception as exc:                  # noqa: BLE001
                    # 关键：把完整堆栈写进任务日志与 journal。
                    # 只记 str(exc) 的话，像 "'latin-1' codec can't encode ..." 这种
                    # 消息根本看不出是哪一行、哪一步出的问题，只能靠猜。
                    tb = traceback.format_exc()
                    job.log(f"[error] {type(exc).__name__}: {exc}")
                    for line in tb.strip().splitlines()[-14:]:
                        job.log("  " + line)
                    print(f"[docbridge] 任务 {job.id} 失败：\n{tb}", flush=True)
                    job.set_status("error", f"{type(exc).__name__}: {exc}")
                finally:
                    self._record_usage(job)  # 失败/取消也要入账（内部保证只记一次）
                    self._persist(job)
                    self._release_memory(job)
            except Exception:                             # noqa: BLE001
                print("[docbridge] worker 循环异常（已兜住，继续干活）：\n"
                      + traceback.format_exc(), flush=True)
                time.sleep(0.5)

    def _record_usage(self, job: Job) -> None:
        """把用量记上账——**只能记一次**，且要在状态变成 done 之前。

        为什么顺序重要：前端是靠 SSE 的 done 事件判断任务结束的，如果记账发生在那之后，
        「任务已完成」与「用量已入账」之间就有一个窗口：这时刷新管理页会看到
        完成的任务却没计入用量（集成测试因此偶发失败过）。崩溃时也会丢账。
        """
        if getattr(job, "_usage_recorded", False):
            return
        job._usage_recorded = True
        if self.on_job_finished:
            try:
                self.on_job_finished(job)
            except Exception:                                # noqa: BLE001
                pass

    def _run_job(self, job: Job) -> None:
        module = PIPELINES[(job.fmt, job.mode)]
        job.started = time.time()
        job.set_status("running")
        job.set_progress(0, 0, "准备中")
        job.log(f"开始处理 {job.src_name}（{job.fmt}/{job.mode}）")

        cfg = T.resolve_config(job.profile or None, job.options.get("_override"))
        kind = "line" if job.fmt == "pdf" else "paragraph"
        target_lang = job.options.get("target_lang") or "zh-Hans"

        # 通道策略：备用兜底 / 难批次攻坚（见 pick_channels 的说明）
        strategy = (job.options.get("strategy") or "fallback").strip().lower()
        chain = pick_channels(job.profile or "", strategy)
        cfgs, names = {"primary": cfg}, {id(cfg): f"{job.profile or '自定义'}:{cfg['chatModel']}"}
        fallback = escalate = None
        for key, slot in (("fallback", "fallback"), ("escalate", "escalate")):
            code = chain.get(key)
            if not code or code == (job.profile or ""):
                continue
            try:
                c = T.resolve_config(code, None)
            except T.TranslateError as exc:
                job.log(f"[warn] {slot} 通道 {code} 不可用，已跳过：{exc}")
                continue
            names[id(c)] = f"{code}:{c['chatModel']}"
            if slot == "fallback":
                fallback = c
            else:
                escalate = c
        if strategy == "single":
            job.log("通道策略：只用当前通道（不兜底、不攻坚）")
        else:
            parts = [names[id(cfg)]]
            if escalate is not None:
                parts.append("攻坚 " + names[id(escalate)])
            if fallback is not None:
                parts.append("兜底 " + names[id(fallback)])
            job.log("通道策略：%s（%s）" % (strategy, " → ".join(parts)))
        # 术语表加载一次、注入所有模式（经管类文档靠它保证全书译名统一）
        glossary = T.load_glossary(job.options.get("glossary_path"))
        if glossary:
            job.log("术语表：%d 条（命中本批的会自动注入）" % len(glossary))
        tr = T.make_translator(cfg, job.options.get("source_lang") or "auto",
                               target_lang, kind=kind, log=job.log,
                               fallback=fallback, escalate=escalate,
                               route=strategy, channel_names=names,
                               glossary=glossary)

        out_name = self._output_name(job)
        out_path = os.path.join(job.out_dir, out_name)
        src_path = os.path.join(job.in_dir, job.src_name)

        options = {k: v for k, v in job.options.items() if not k.startswith("_")}
        options["__mode__"] = job.mode
        # 模型标识：跨任务共享缓存按它分目录，换通道/换模型不会命中旧译文
        options.setdefault(
            "model_tag", "%s:%s" % (job.profile or "custom",
                                    cfg.get("chatModel") or "default"))
        result = module.run(src_path, out_path, translate=tr,
                            progress=job.set_progress, options=options)

        if not os.path.isfile(out_path):
            raise RuntimeError("管线没有产出结果文件")
        stats = dict(result or {})
        stats.update({f"api_{k}": v for k, v in tr.stats.items()})
        if tr.stats.get("escalated"):
            job.log(f"  其中 {tr.stats['escalated']} 批走了攻坚通道（术语表/表格这类难内容）")
        if tr.stats.get("fallbacks"):
            job.log(f"  其中 {tr.stats['fallbacks']} 批由备用通道完成（主通道没扛住）")
        failed = tr.stats.get("failed", 0)
        if failed:
            job.log(f"[warn] 有 {failed} 处未能翻译，已保留原文")
        job.stats = stats
        job.result = {
            "name": out_name,
            "size": os.path.getsize(out_path),
            "url": f"api/jobs/{job.id}/download",
        }
        job.set_progress(max(job.progress["done"], job.progress["total"] or 1),
                         job.progress["total"] or 1, "完成")
        self._record_usage(job)          # ★ 先记账，再宣布完成
        job.set_status("done")
        job.log("完成：" + stats.get("detail", out_name))
        try:
            shutil.rmtree(os.path.join(job.dir, "_translate_cache"), ignore_errors=True)
        except OSError:
            pass

    @staticmethod
    def _output_name(job: Job) -> str:
        stem = os.path.splitext(job.src_name)[0]
        ext = os.path.splitext(job.src_name)[1]
        tag = "双语" if job.mode == "bilingual" else "zh"
        return f"{stem}_{tag}{ext}"

    # ------------------------------------------------------------ 清理
    def _persist(self, job: Job) -> None:
        try:
            with open(os.path.join(job.dir, "meta.json"), "w", encoding="utf-8") as fh:
                json.dump(job.public(), fh, ensure_ascii=False, indent=1)
        except OSError:
            pass

    def _release_memory(self, job: Job) -> None:
        """删掉中间产物（上传原件保留到任务被清理，便于重试/下载源文件）。"""
        try:
            shutil.rmtree(os.path.join(job.out_dir, "_cache"), ignore_errors=True)
        except OSError:
            pass

    def _trim_locked(self) -> None:
        while len(self._order) > MAX_JOBS_KEPT:
            old = self._order.popleft()
            j = self._jobs.pop(old, None)
            if j:
                shutil.rmtree(j.dir, ignore_errors=True)

    def _purge_shared_cache(self) -> None:
        """清理跨任务共享缓存里的过期条目。

        共享缓存没有「任务」这个生命周期，所以必须按最后使用时间单独清，
        否则会随着上传的文档一直长下去。
        """
        root = (os.environ.get("DOCBRIDGE_SHARED_CACHE") or "").strip()
        if root.lower() in ("0", "false", "no", "off"):
            return
        root = root or os.path.join(ROOT, "_cache")
        if not os.path.isdir(root):
            return
        cutoff = time.time() - SHARED_CACHE_TTL_HOURS * 3600
        removed = 0
        for dirpath, dirnames, filenames in os.walk(root, topdown=False):
            if dirpath == root:
                continue
            if not filenames and not dirnames:
                try:
                    os.rmdir(dirpath)          # 顺手收掉空的父目录
                except OSError:
                    pass
                continue
            try:
                newest = max(os.path.getmtime(os.path.join(dirpath, f))
                             for f in filenames) if filenames else 0
            except OSError:
                continue
            if newest and newest < cutoff:
                shutil.rmtree(dirpath, ignore_errors=True)
                removed += 1
        if removed:
            print(f"  ↻ 清理了 {removed} 份过期共享缓存", flush=True)

    def _janitor_loop(self) -> None:
        while not self._stop:
            time.sleep(600)
            try:
                self._purge_shared_cache()
            except Exception:                                # noqa: BLE001
                pass
            cutoff = time.time() - JOB_TTL_HOURS * 3600
            with self._lock:
                stale = [jid for jid, j in self._jobs.items()
                         if (j.finished or j.created) < cutoff
                         and j.status in ("done", "error", "canceled")]
                for jid in stale:
                    j = self._jobs.pop(jid, None)
                    if j:
                        shutil.rmtree(j.dir, ignore_errors=True)
                    try:
                        self._order.remove(jid)
                    except ValueError:
                        pass
        return

    def shutdown(self) -> None:
        self._stop = True
        with self._wake:
            self._wake.notify_all()


def capability_payload() -> dict:
    """给前端的一份「本站能做什么」清单（格式 / 模式 / 可选参数 / 限制）。"""
    return {
        "formats": FORMATS,
        "pipelines": pipeline_info(),
        "maxUploadMB": MAX_UPLOAD_MB,
        "workers": MAX_WORKERS,
        "maxActivePerIp": MAX_ACTIVE_PER_IP,
        "maxJobsPerIpPerDay": MAX_JOBS_PER_IP_PER_DAY,
        "jobTTLHours": JOB_TTL_HOURS,
    }
