# DocBridge 上线手册（挂到现有 同类项目 服务器上）

> **线上现状（2026-09-11 已部署）**
>
> | 项 | 值 |
> |---|---|
> | 文档翻译站 | `https://translate.example.com/` — *待 DNS 生效* |
> | 同声传译站 | `https://app.example.com/` — 正常，未受影响 |
> | 服务器 | `203.0.113.10`（Ubuntu 22.04，Python 3.10.12） |
> | 服务 | `/opt/docbridge`，systemd 单元 `docbridge.service`，监听 `127.0.0.1:8788` |
> | 模型配置 | `/opt/docbridge/profiles.json`（本站专属，**与语音站两条线分开**） |
> | 反向代理 | Caddy，`/etc/caddy/Caddyfile`（改动前备份为 `Caddyfile.bak.*`） |
>
> **唯一待办**：DNS 加一条 A 记录
> `translate.example.com  →  203.0.113.10`
> 加完后 Caddy 会在 1 分钟内自动签发证书，无需再动服务器。

    公网 443 ── Caddy ─┬─ translate.example.com → 127.0.0.1:8788  DocBridge（本手册）
                       └─ app.example.com → 127.0.0.1:8787  同类项目（原有）

## 0. 前提

| 项 | 要求 | 线上实测 |
|---|---|---|
| 已有 同类项目 | Node ≥18、Caddy 已签发证书 | ✅ Node v20.20.2 |
| Python | **≥3.10** | ✅ 3.10.12 |
| 中文字体 | `apt install fonts-arphic-uming fonts-wqy-zenhei`（TrueType 轮廓，见坑二） | ✅ 已装 |
| 数学符号字体 | `fonts-dejavu`（Ubuntu 默认自带）：补 uming 缺的 `∂ ∆ ⊂` | ✅ 已装 |
| OCR（扫描件） | `apt install tesseract-ocr tesseract-ocr-eng tesseract-ocr-chi-sim` | ✅ 已装 |
| 磁盘 | 建议 ≥10GB（产物比原文大几倍，双语模式相反） | ✅ 30G 空闲 |
| 内存 | ≥1GB | ✅ 1.7G（可用 1.3G） |
| 架构 | x86_64（PyMuPDF 官方 wheel） | ✅ x86_64 |

## 1. 上传代码

    cd <仓库目录>
    rsync -az --exclude '_jobs' --exclude '__pycache__' --exclude 'tests/_tmp' \
          --exclude '.venv' --exclude 'usage.json' \
          docbridge/ ourmeta:/opt/docbridge/
    # ★ 别漏了这个：PDF 管线依赖它（见下面的「坑一」）
    rsync -az pdf_translate.py ourmeta:/opt/docbridge/pdf_translate.py
    ssh ourmeta 'chown -R root:root /opt/docbridge'

## 2. 服务器上准备 Python 环境

    ssh ourmeta
    apt-get update && apt-get install -y fonts-arphic-uming fonts-wqy-zenhei python3-venv
    cd /opt/docbridge
    python3 -m venv .venv && .venv/bin/pip install -U pip
    .venv/bin/pip install -r requirements.txt

装完先自检依赖与管线（**改完代码或换服务器都要跑**）：

    .venv/bin/python -c "
    import pipelines as P
    print('管线:', [f'{f}/{m}' for f, m in P.PIPELINES])
    print('失败:', P.IMPORT_ERRORS or '无')
    print('中文字体:', P.default_pdf_font() or '⚠ 没找到（会退回内置 china-s）')
    "

离线测试四件套（不联网、不花钱，样例现场生成）：

    .venv/bin/python tests/test_translator.py
    .venv/bin/python tests/test_pdf_inplace.py
    .venv/bin/python tests/test_pdf_bilingual.py
    .venv/bin/python tests/test_docx_pipeline.py

> 顺序执行：`test_pdf_inplace.py` 跑完会清理 `tests/_tmp/`，并发跑会互相清目录。

## 3. 两个必须知道的坑

### 坑一：`pdf_translate.py` 要一起传，且放在 `docbridge/` 里面

`pipelines/pdf_inplace.py` 与 `pipelines/pdf_bilingual.py` 都要 import 那个 33KB 的既有
PDF 流水线。它们按以下顺序找它：

    DOCBRIDGE_PDF_TRANSLATE_DIR 环境变量
      → docbridge/            ← 部署时放这里
      → docbridge 的父目录     ← 本机开发时它在父目录
      → 本机 macOS 开发路径

所以只要把 `pdf_translate.py` 拷进 `/opt/docbridge/` 就什么都不用配。**忘了传的话**，
四条管线里会只剩 Word 那两条能用（页面会把 PDF 标成不可用并给出原因，不会静默失败）。

### 坑二：中文字体要「能提取」且「能子集化」，两条都有讲究

**（a）不装字体 → 译文「看得见但复制不出来」。**
Linux 上若没有 CJK 字体，PyMuPDF 会退回内置 `china-s`：字形能画出来，
但 PDF 里缺 ToUnicode 映射，**复制/搜索译文得到的是乱码**（双语模式尤其明显）。

**（b）字体选错 → 输出被撑大几十倍。**
要用 **TrueType 轮廓**的中文字体。Noto CJK 是 CFF/OTF 轮廓，
PyMuPDF 的 `subset_fonts()` 对它无效（日志里出现
`MuPDF error: format error: Reserved charstring byte`），
于是整份 ~20MB 字体被嵌进每一份输出。

线上实测（同一份 2 页样例）：

| 字体 | 轮廓类型 | 双语输出 | 乱码 | 译文可提取 |
|---|---|---|---|---|
| Noto Serif CJK | CFF/OTF | **19.6 MB** | 0 | ✅ |
| uming 明体 | TrueType | **61 KB** | 0 | ✅ |
| wqy-zenhei 文泉驿 | TrueType | **25 KB** | 0 | ✅ |
| 内置 china-s | — | 12 KB | **280** | ❌ |

结论：**装 TrueType 轮廓的字体**，候选清单已经把它们排在最前：

    apt install fonts-arphic-uming fonts-wqy-zenhei
    # 明体是宋体风格，与既有 pdf_translate.py 在 macOS 用的宋体观感一致，故首选

自查工具（部署后跑一次，三个问题一次回答：用哪个字体、
译文有没有画上去、能不能复制出来）：

    .venv/bin/python tools/check_pdf_font.py

> 另外：**PDF 原位模式的输出体积**取决于字体子集化是否成功。上游
> `pdf_translate.py` 只做 `save(garbage, deflate)`、不打子集，所以包装层
> （`pipelines/pdf_inplace.py`）在落盘前补了一次 `subset_fonts()`。
> 线上效果：同一份 2 页 PDF **9.8MB → 289KB**。结果 `detail` 里会写明这一步的
> 前后体积；若显示「字体子集化未执行」，就是字体类型不对。

## 4. 安装为系统服务

    cp /opt/docbridge/deploy/docbridge.service /etc/systemd/system/
    vim /etc/systemd/system/docbridge.service   # ★ 改 ADMIN_TOKEN（openssl rand -hex 16）
    systemctl daemon-reload
    systemctl enable --now docbridge
    journalctl -u docbridge -n 25 --no-pager

启动日志应当长这样（**重点看密钥来源、管线、中文字体三行**）：

    DocBridge 文档翻译 · 启动
      地址        http://127.0.0.1:8788/
      密钥来源    /opt/livebridge/profiles.json
      可用代号    ['openai', 'snow']
      管线        ['pdf/inplace', 'pdf/bilingual', 'docx/inplace', 'docx/bilingual']
      中文字体    /usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc
      用 waitress 启动（生产模式）

> 看不到这段横幅？说明 stdout 被块缓冲了。代码里已用
> `sys.stdout.reconfigure(line_buffering=True)` 修掉，若你用的是旧版本，
> 在 systemd 里加 `Environment=PYTHONUNBUFFERED=1` 也能解决。

## 5. 配置 Caddy

    cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak.$(date +%F)
    vim /etc/caddy/Caddyfile     # 内容见 deploy/Caddyfile.docbridge
    caddy validate --config /etc/caddy/Caddyfile     # 语法检查，必做
    systemctl reload caddy                          # reload，不要 restart

两个站并存（子域名方案，现有站点块完全不用动）：

    app.example.com {
        reverse_proxy 127.0.0.1:8787
        tls { ca https://acme-v02.api.letsencrypt.org/directory }
    }

    translate.example.com {
        reverse_proxy 127.0.0.1:8788 {
            flush_interval -1        # ★ 关缓冲，否则 SSE 进度条不动
        }
        tls { ca https://acme-v02.api.letsencrypt.org/directory }
    }

加 DNS A 记录（这一步只能你在域名商那边做）：

    translate.example.com.   A   203.0.113.10

DNS 生效前，Caddy 的 ACME 会报 `NXDOMAIN` 并每 60 秒自动重试（最多 30 天）——
**这期间 同类项目 完全不受影响**；DNS 一生效证书会自动签发，不用再登录服务器。

## 6. 上线自检清单

服务端自身：

    ssh ourmeta '
      systemctl is-active docbridge livebridge caddy
      curl -s http://127.0.0.1:8788/api/health | head -c 200; echo
      curl -s http://127.0.0.1:8788/api/config | grep -c apiKey     # 必须是 0（密钥不外泄）
    '

公网：

    curl -sI https://app.example.com/      | head -2   # 200，原有站点
    curl -sI https://translate.example.com/       | head -2   # 200，DNS 生效 + 证书签发后

浏览器里逐项确认：

- [ ] `https://translate.example.com/` 打开是文档翻译页，样式正常
- [ ] 页头「翻译通道」能看到代号；点「测试通道」显示 ✓ 与样例译文
- [ ] 传一个几页的 PDF → 原位模式 → **进度条真的在动**（不动 → `flush_interval -1` 没配）
- [ ] 下载译文：中文正常、版式与原文一致、图片还在、**能选中复制中文**（字体配对了）
- [ ] 换双语模式再跑一次：横向页、左原文右译文
- [ ] 传一个 `.docx`：原位译后样式保留；双语是原文段后紧跟深蓝译文段
- [ ] 同类项目 页头出现「📄 文档翻译」，点过去能到（DocBridge 页头也有「🎙 同声传译」回链）

## 7. 日常运维

    journalctl -u docbridge -f                  # 实时日志
    du -sh /opt/docbridge/_jobs                 # 产物占用（超 TTL 自动清理）
    systemctl restart docbridge                 # 重启；已完成的产物会从磁盘恢复

* **清理**：产物保留 `DOCBRIDGE_JOB_TTL_HOURS`（默认 6 小时），后台每 10 分钟扫一次；
  任务数超 `DOCBRIDGE_MAX_JOBS`（默认 500）时先删最旧的。
* **限额**：同一 IP 同时排队/执行的任务最多 2 个；单文件 150MB（线上设置）。
* **备份**：只需 `profiles.json`（同类项目 那份）与 `/etc/systemd/system/docbridge.service`。
* **升级**：rsync 新代码 → `systemctl restart docbridge`；依赖变了才重跑 pip。
* **回滚**：`cp /etc/caddy/Caddyfile.bak.<时间> /etc/caddy/Caddyfile && systemctl reload caddy`，
  再 `systemctl disable --now docbridge`。整个部署是**纯增量**的，不动 同类项目 的文件。
* **成本**：`/doc/admin`（`ADMIN_TOKEN`）里有按 token 粗估的当日花费；
  单价用 `TR_PRICE_IN` / `TR_PRICE_OUT` 按实际模型改。

## 8. 排错对照表

| 现象 | 原因与处理 |
|---|---|
| 页面样式/接口全 404（挂在路径下时） | 访问了 `/doc` 而无尾斜杠 → 检查 `redir /doc /doc/`；或用了 `handle` 但没设 `DOCBRIDGE_URL_PREFIX=/doc` |
| 进度条半天不动、最后跳到完成 | `flush_interval -1` 没配，反代把 SSE 缓冲住了 |
| PDF 两种模式都标「不可用」 | 没传 `pdf_translate.py` 到 `/opt/docbridge/`（见坑一），`/api/health` 的 `pipelineErrors` 会说明原因 |
| 译文能看，但复制/搜索出来是乱码 | 字体缺 ToUnicode（见坑二 a）：装 TrueType 字体并检查 `DOCBRIDGE_PDF_FONT` |
| 输出 PDF 体积异常大（十几 MB） | 字体是 CFF 轮廓、子集化失败（见坑二 b）：换 uming / wqy-zenhei；`detail` 里会写「字体子集化未执行」 |
| `journalctl` 里看不到启动横幅 | stdout 块缓冲：加 `Environment=PYTHONUNBUFFERED=1`（新版本代码已修） |
| 任务失败：`图片数量发生变化` | 上游自检不过（结果不可信），**这是有意的硬失败**，把文件发给开发者排查 |
| 任务失败：`翻译返回数量不符` | 模型把多行合并/拆分且重试与逐条兜底都失败；换个代号或模型再试 |
| 任务失败：连接超时 / `HTTPSConnectionPool` | 服务器出网被限或上游限流；`curl -I https://api.siliconflow.cn` 验证出网 |
| 管理页 403 | `ADMIN_TOKEN` 没设或填错；systemd 改完要 `daemon-reload && restart` |
| 用量统计里 IP 全是 `127.0.0.1` | `DOCBRIDGE_TRUST_PROXY=1` 没设 |
| 译文里的数学符号变成空白/方框 | 缺字回退字体没生效：装 `fonts-dejavu`，并确认启动日志里的字体检测正常（本站在 `pipelines/fonts.py` 里维护候选） |
| 任务一直卡在「排队中」不动 | 旧版有个 worker 崩溃的 bug（队列里只剩已取消任务时会打死线程），现已修复并有回归测试；升级后重启即可 |
| 重启后老任务不见了 | 产物超过 TTL 已被清理；TTL 内会从 `_jobs/<id>/meta.json` 恢复 |
| 任务失败：`'latin-1' codec can't encode characters in position N-M` | 预设代号的 apiKey 是**没替换的占位符或含中文**（HTTP 头只能 latin-1）。新版会在提交时就拦住并指名道姓；旧版请看 `profiles.json` 里那个代号 |
| 任务失败：`405 Method Not Allowed` | 你用的中转站**不支持 `/chat/completions`**，只支持 Responses 接口。在该代号里加 `"api": "responses"` |
| 任务失败：`403 error code: 1010` | 中转站前面挂着 Cloudflare，按 UA 拦掉了 Python 默认 UA。设 `Environment=DOCBRIDGE_UA=<浏览器 UA>` |
| 证书一直签发不了 | `journalctl -u caddy \| grep -i acme` 看是不是 NXDOMAIN；A 记录要指向 `203.0.113.10` |
