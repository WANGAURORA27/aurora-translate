# aurora-translate · GitHub Actions 运行环境镜像
# =============================================================================
# 为什么有这个东西：GitHub 托管的 runner 每次都是全新虚拟机，translate.yml 里
# 那两个安装步骤（apt 装 OCR/字体 ~20s + pip 装依赖 ~5s）每次都要重跑一遍。
# 把这一层固化进镜像后，job 只需要 docker pull 一次（有层缓存，通常几秒）。
#
# 镜像名（GHCR 要求全小写）：ghcr.io/wangaurora27/aurora-translate-runner:latest
# 由 .github/workflows/image.yml 构建推送。
#
# 用它的地方：translate.yml 的 translate / cloud 两个 job，以及 smoke.yml。
#
# ── 几个踩过的坑，别顺手删掉 ────────────────────────────────────────────────
# * git / curl / ca-certificates 必须装：actions/checkout@v4 是在**容器里**跑的，
#   它要求容器内有 git；取件、回传步骤用 curl。python:3.10-slim 这三样都没有。
# * gh 必须装：translate 这个 job 的「身份校验」「回帖」「失败说明」三步用的都是
#   GitHub CLI，宿主 runner 上预装了、容器里没有。
# * safe.directory：容器内跑 git 的工作区所有权跟当前用户对不上，
#   不配置会报 "detected dubious ownership" 直接失败。
# * bash 必须在（Debian slim 自带），因为容器 job 的默认 shell 是 sh(dash)，
#   而各步骤脚本里有 `set -euo pipefail`；workflow 里用 defaults.run.shell: bash
#   切回 bash。这里不用改镜像，但记一笔免得以后有人把 bash 删了。
# * tesseract-ocr-osd 不是必需的（管线走 PyMuPDF 的 get_textpage_ocr，只用到
#   语言包），但它很小且能让部分 tesseract 调用少一条警告，顺手装上。
# =============================================================================

FROM python:3.10-slim

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ── 1. 系统依赖：OCR 引擎 + 中文字体 + git/curl（一次性装完就清 apt 列表）──
# 字体说明见 pipelines/fonts.py：uming.ttc 必须存在，否则 PyMuPDF 会退回内置
# china-s（译文复制出来是乱码）或 Noto CJK（整份 20MB 字体嵌进每页）。
#
# 字体为什么要装这么多：线上出现过某份 PDF 因为字体问题直接崩（日志里
# `warning: unhandled font type` → `insert_font` 取不到字宽 → `NoneType.m_internal`）。
# 候选清单里多放几个真实存在的字体文件，就多几条退路。
#
# ⚠️ 全部选 **TrueType / TTC** 轮廓，**不要**换成 fonts-noto-cjk：
#    它是 CFF/OTF 轮廓，PyMuPDF 的 subset_fonts() 子集化会失败
#    （"Reserved charstring byte"），结果是整份 ~20MB 字体嵌进每一页。
#    实测同一份样例：Noto Serif CJK → 19.6MB，uming → 61KB。
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-chi-sim \
        tesseract-ocr-osd \
        fonts-arphic-uming \
        fonts-arphic-ukai \
        fonts-wqy-zenhei \
        fonts-wqy-microhei \
        fonts-droid-fallback \
        fonts-liberation \
        fonts-dejavu-core \
        git \
        curl \
        ca-certificates \
        ; \
    rm -rf /var/lib/apt/lists/*

# ── 2. GitHub CLI：Issue 通道那几步要用 gh ──────────────────────────────────
RUN set -eux; \
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /usr/share/keyrings/githubcli-archive-keyring.gpg; \
    chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg; \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends gh; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /github/workspace

# ── 3. Python 依赖：与 requirements.txt 保持单一事实来源 ────────────────────
# 只 COPY requirements.txt（不 COPY 整个仓库），改业务代码不会让这层缓存失效。
# 注意 requirements.txt 里**没有** pytesseract：OCR 走 PyMuPDF 的
# get_textpage_ocr，直接调系统 tesseract 可执行文件，不需要这个 Python 包。
COPY requirements.txt /tmp/requirements.txt
RUN set -eux; \
    python -m pip install --upgrade pip; \
    pip install -r /tmp/requirements.txt; \
    rm -f /tmp/requirements.txt

# ── 4. 容器内 git 工作区所有权与当前用户不一致，先豁免 ─────────────────────
RUN git config --system --add safe.directory '*'

# ── 5. 构建期自检：装漏了就在这里让构建失败，而不是等线上任务跑一半才炸 ──
# 字体部分既做断言、也把真实文件清单打进构建日志（有人要在代码里写候选路径时
# 直接照着日志抄，不用猜）。
RUN set -eux; \
    tesseract --version | head -1; \
    tesseract --list-langs; \
    tesseract --list-langs 2>&1 | grep -qx eng; \
    tesseract --list-langs 2>&1 | grep -qx chi_sim; \
    test -f /usr/share/fonts/truetype/arphic/uming.ttc; \
    test -f /usr/share/fonts/truetype/arphic/ukai.ttc; \
    test -f /usr/share/fonts/truetype/wqy/wqy-zenhei.ttc; \
    test -f /usr/share/fonts/truetype/wqy/wqy-microhei.ttc; \
    ls -l /usr/share/fonts/truetype/arphic/; \
    ls -l /usr/share/fonts/truetype/wqy/; \
    ls -l /usr/share/fonts/truetype/droid/; \
    ls -l /usr/share/fonts/truetype/liberation/; \
    ls -l /usr/share/fonts/truetype/dejavu/; \
    find /usr/share/fonts -type f \( -name '*.ttc' -o -name '*.ttf' \) | sort; \
    python -c "import fitz, docx, lxml, flask, waitress; print('python deps ok')"
