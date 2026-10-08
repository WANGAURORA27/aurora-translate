"""docbridge · 翻译服务层

把 同域项目 的 ``profiles.json``（代号 → 密钥）解析成一个批量翻译回调，
供各管线通过 ``translate(texts, context) -> list[str]`` 调用。

设计要点（沿用既有 pdf_translate.py 已验证的策略）：

* **密钥只留在服务端**：浏览器只拿得到代号与名称，永远拿不到 apiKey；
* **JSON 键值映射 + 校验 + 重试**：批量请求要求模型返回 ``{"1": "…"}``，
  逐条校验「数量一致 / 无换行 / 无重复 / 无合并 / 无繁体 / 无省略号 / 无元注释」，
  不通过就带着纠正提示重试，仍失败则退化为逐条请求；
* **绝不静默错位**：无论中间怎么重试，最终返回的列表与入参**严格等长同序**；
* **失败不丢内容**：单条彻底失败时返回原文并计入 ``stats["failed"]``，
  由上层把「N 条未能翻译」显示给用户，而不是丢一段空白。

本模块零第三方依赖（只用 urllib），与既有代码保持同一风格。
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.abspath(__file__))

DEFAULT_TIMEOUT = int(os.environ.get("TRANSLATE_TIMEOUT", "180"))
MAX_CHUNK_LINES = int(os.environ.get("TRANSLATE_CHUNK_LINES", "60"))
MAX_CHUNK_CHARS = int(os.environ.get("TRANSLATE_CHUNK_CHARS", "6000"))
CONCURRENCY = max(1, int(os.environ.get("TRANSLATE_CONCURRENCY", "3")))
MAX_ATTEMPTS = 3

# 提示词/校验规则的版本号：**改动提示词或校验逻辑时必须 +1**。
# 它会进跨任务共享缓存的键——否则改了提示词，旧文件重跑仍命中旧译文。
PROMPT_VERSION = "2026-10-08.1"

# 数学符号保全：源文里有这些符号时，译文必须保留符号本身**或**约定俗成的中文说法
# （√ 写成「根号」当然也是对的，不能因此判错——这是之前"重复=合并"那条规则踩过的坑）。
MATH_SYMBOL_EQUIVALENTS = {
    "√": ("根号", "平方根", "开方"),
    "∑": ("求和", "总和", "累加", "Σ"),
    "∏": ("连乘", "乘积", "Π"),
    "∫": ("积分", "∫"),
    "∂": ("偏导", "偏微分", "∂"),
    "∆": ("变化量", "增量", "△", "Δ"),
    "≈": ("约等于", "近似", "约"),
    "≠": ("不等于",),
    "≤": ("小于等于", "不大于", "≤"),
    "≥": ("大于等于", "不小于", "≥"),
    "±": ("正负", "加减"),
    "∞": ("无穷", "无限"),
    "∈": ("属于",),
    "⊂": ("包含于", "子集"),
    "∪": ("并集", "并"),
    "∩": ("交集", "交"),
    "→": ("趋近", "得到", "推出", "→"),
    "⇒": ("推出", "因此", "⇒"),
    "π": ("圆周率", "π"),
    "θ": ("θ", "角度"),
    "α": ("α",), "β": ("β",), "λ": ("λ",), "μ": ("μ",), "σ": ("σ",), "Ω": ("Ω",),
}


def missing_math_symbols(source: str, translation: str) -> list[str]:
    """译文里丢了哪些数学符号（源文有、译文既没保留符号也没用中文说法）。

    只报警不臆断：源文的 「√」 写成译文的「根号」是**正确**译法，所以两者都算保留。
    """
    lost = []
    for ch in set(source or ""):
        rule = MATH_SYMBOL_EQUIVALENTS.get(ch)
        if not rule:
            continue
        if ch in (translation or ""):
            continue
        low = (translation or "").lower()
        if any(alt.lower() in low for alt in rule):
            continue
        lost.append(ch)
    return lost

# 批量失败后按这个粒度由粗到细重试，最后才逐条。见 translate() 里的说明。
FALLBACK_GROUP_SIZES = (20, 5, 1)

# 限流熔断：被限流的通道再捶它也没用，只会白等+白花钱（线上实测：中转站
# 回报 429「上游当前已达到使用限制」后，仍被重试 3 次才换道）。
_RATE_LIMIT_HINTS = ("429", "rate_limit", "rate limit", "too many requests",
                     "使用限制", "限流", "quota",
                     # ★ 线上踩过：主通道余额不足时只报 "HTTP 402 / account balance is
                     #   insufficient"，原先不在特征里 → 不给降级 → 全篇 0 译文还崩在画字上。
                     #   这类错误再重试也没用，必须立刻切备用通道。
                     "402", "insufficient", "balance", "余额不足", "余额不够",
                     "欠费", "arrears", "billing", "payment required")
RATE_LIMIT_STRIKES = int(os.environ.get("DOCBRIDGE_RATE_LIMIT_STRIKES", "2"))
RATE_LIMIT_COOLDOWN = float(os.environ.get("DOCBRIDGE_RATE_LIMIT_COOLDOWN", "120"))


def _is_rate_limited(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(h.lower() in msg for h in _RATE_LIMIT_HINTS)

# ---------------------------------------------------------------- 文本判定

NUMBERISH_RE = re.compile(r"^[\d\s\W_]*$")           # 纯数字/符号/空白
LATIN_RE = re.compile(r"[A-Za-z]")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
# 繁体专用字（简繁判断用）——判断「译文里混进繁体」时只看这张表。
#
# 收录标准是「**这个字本身只属于繁体**」：要么简体写法不同（經/经、產/产、價/价），
# 要么它本来就是繁体专有字形（裡/里、隻/只、曆/历、捲/卷）。
#
# ★ 刻意**不收**这些「简体也在用」的字：
#     干 后 里 台 面 只 系 划 历 云 复 发 卷 谷 冲 余 郁 繁 …
#   收了会把正常的简体译文误判成繁体，而误判的代价是整批被打回重译，
#   甚至降级成「保留原文」——比漏判严重得多。所以只求覆盖常见泄漏，不求穷尽。
#   同理不收 於（简体里偶有出现）与 係。
_TRAD_CHARS = (
    # 高频功能字与最常泄漏的一批
    "這個們來時說對會後國開問間關與為應麼樣號據處條種類總計認識試題課讀語詞讓變邊進運選過適遞"
    "該誰隨雖歲雙書術樹實勢設紹審聲勝濕釋飾壽順碩絲鬆蘇訴傳偉內決況淨涼減軍創別辦廠厭嗎場堅"
    "備媽寶宮寬層師幫張徑戰戶擇掛揮標殘畢澤測濃漲漸滾靈災煩愛畫蓋筆簡紅約級紀納紙線織終統績"
    "綠職藍補規視覺訂訪評話請調談燈點電霧靜頂頁車東萬專參倉嘗徹塵陳稱懲遲衝醜瘡純辭聰從叢錯"
    # 经济、金融与商科高频
    "經濟學譯產資價現發體圖債貨貨幣銀錢財務險報潤損虧額營業銷費稅貸儲匯兌換賬帳記購販賣買償"
    "賺質貝負貢責敗貪貴贏鐘鋼鐵針錯鍵遺預領頻題項須風飛驗黃"
    # 常用动词、形容词与名词
    "達帶單擔膽當黨蕩導島鄧敵惡罰閥豐風馮婦復複剛岡鞏溝構穀顧觀館慣廣歸漢轟壺護劃懷壞歡環還"
    "穢渾夥獲擊機積極際劑繼艱殲揀檢見劍鍵講獎將膠階節潔結緊僅盡驚競舊劇懼捲凱殼墾懇誇塊擴臘"
    "蘭攔欄爛勞樂淚離裡禮麗歷曆厲勵連憐聯練煉糧兩輛療遼獵臨鄰嶺劉婁樓陸錄慮亂論羅絡駱馬邁麥"
    "滿貓夢滅憫畝惱腦擬釀聶寧農瘧歐嘔盤龐賠噴貧頻憑撲僕樸譜齊騎豈氣棄遷簽淺譴槍牆強搶鍬橋竅"
    "竊親輕慶瓊窮區驅權勸確擾熱榮軟銳灑傘喪掃澀殺臺態攤壇嘆湯濤討騰題鐵聽廳頭塗團橢窪襪彎網"
    "韋違圍維衛溫聞穩務霧誤犧習戲細蝦嚇鹹獻縣憲鄉詳響蕭銷曉嘯協挾攜脅寫謝鋅興須許緒續懸學尋"
    "詢訓訊遜壓亞啞嚴巖鹽陽養謠藥爺業葉醫億義議藝憶癰擁傭踴憂優郵猶遊誘餘漁嶼籲鬱譽淵園員圓"
    "緣遠願躍嶽雲鄭證隻幟製質種眾晝硃燭築莊裝壯狀準濁鑽組採櫃捨裏幹髮"
)
TRAD_RE = re.compile("[" + _TRAD_CHARS + "]")
ELLIPSIS_RE = re.compile(r"……|…|\.\.\.|。。。")
META_RE = re.compile(
    r"原文未提供|保持原样|无法翻译|未提供原文|此处省略|翻译如下|译文[:：]|原文[:：]"
    r"|<[a-z/][^>]*>|\[(?:TODO|待译|略)\]"
)


def needs_translation(text: str, target_lang: str = "zh-Hans") -> bool:
    """判定一个单元是否需要送翻译（已是对应语言/纯数字的行不送）。"""
    t = (text or "").strip()
    if not t:
        return False
    if NUMBERISH_RE.match(t):
        return False
    if target_lang.startswith("zh"):
        # 已基本是中文（且没有成句英文）→ 不译
        cjk = len(CJK_RE.findall(t))
        letters = len(LATIN_RE.findall(t))
        if cjk and cjk * 3 >= letters:
            return False
        return bool(LATIN_RE.search(t))
    return True


# ---------------------------------------------------------------- profiles

LANGUAGES = [
    ("zh-Hans", "简体中文"),
    ("zh-Hant", "繁體中文"),
    ("en", "English"),
    ("ja", "日本語"),
    ("ko", "한국어"),
    ("fr", "Français"),
    ("de", "Deutsch"),
    ("es", "Español"),
    ("ru", "Русский"),
    ("pt", "Português"),
    ("it", "Italiano"),
    ("ar", "العربية"),
]

_LANG_PROMPT_NAME = {
    "zh-Hans": "Simplified Chinese (简体中文)",
    "zh-Hant": "Traditional Chinese (繁體中文)",
    "en": "English",
    "ja": "Japanese (日本語)",
    "ko": "Korean (한국어)",
    "fr": "French (Français)",
    "de": "German (Deutsch)",
    "es": "Spanish (Español)",
    "ru": "Russian (Русский)",
    "pt": "Portuguese (Português)",
    "it": "Italian (Italiano)",
    "ar": "Arabic (العربية)",
}

_profiles_lock = threading.Lock()
# {"stamp": mtime, "at": 读取时刻, "data": {代号: 配置}}
_profiles_cache: dict | None = None


class TranslateError(RuntimeError):
    """翻译通道不可用（配置错误 / 网络失败 / 额度耗尽）。"""


def profiles_path() -> str:
    """profiles.json 位置：环境变量 > docbridge 自带 > 兄弟目录 同域项目。"""
    env = (os.environ.get("PROFILES_FILE") or "").strip()
    if env:
        return env
    here = os.path.join(ROOT, "profiles.json")
    if os.path.isfile(here):
        return here
    sibling = os.path.join(os.path.dirname(ROOT), "同域项目", "profiles.json")
    if os.path.isfile(sibling):
        return sibling
    return here


def load_profiles(max_age: float = 5.0) -> dict:
    """读取 profiles.json（带 mtime + 短 TTL 缓存，改完重启或等 5 秒即生效）。"""
    global _profiles_cache
    path = profiles_path()
    try:
        stamp = os.path.getmtime(path)
    except OSError:
        return {}
    with _profiles_lock:
        cached = _profiles_cache
        if cached and cached["stamp"] == stamp and time.time() - cached["at"] < max_age:
            return cached["data"]
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return {}
    out = {}
    for code, prof in (raw or {}).items():
        if code.startswith("_") or not isinstance(prof, dict):
            continue          # 跳过 "_说明" 之类注释键
        out[str(code)] = prof
    with _profiles_lock:
        _profiles_cache = {"stamp": stamp, "at": time.time(), "data": out}
    return out


def profile_issue(prof: dict) -> str:
    """检查一个预设代号是否可用；返回空字符串表示可用，否则返回中文原因。

    为什么必须检查：**HTTP 请求头只能用 latin-1 编码**。如果 apiKey 里混进了
    中文字符（最常见的是 profiles.json 里那句没被替换的占位符
    ``sk-你的OpenAI密钥``），urllib 会在拼 Authorization 头时抛出

        'latin-1' codec can't encode characters in position 10-11

    这种完全看不出问题所在的错误。与其让用户对着它发呆，
    不如在这里提前判掉并说清楚「去改哪个文件的哪个字段」。
    """
    base = str(prof.get("baseUrl") or "").strip()
    key = str(prof.get("apiKey") or "").strip()
    if not base:
        return "没有配置 baseUrl"
    if not key:
        return "没有配置 apiKey"
    for field, val in (("baseUrl", base), ("apiKey", key)):
        bad = sorted({c for c in val if ord(c) > 126})
        if bad:
            return (f"{field} 含非 ASCII 字符（{'、'.join(bad[:6])}），"
                    f"像是占位符没替换")
    low = key.lower()
    if any(h in low for h in ("your", "change", "xxx", "placeholder",
                              "todo", "replace")) \
            or any(h in key for h in ("你的", "替换", "填写", "占位", "密钥")):
        return "apiKey 看起来仍是占位符（不是真实密钥）"
    api = str(prof.get("api") or prof.get("chatApi") or "").strip().lower()
    if api and api not in _API_ALIASES:
        return (f"接口类型 api={api!r} 不认识；只支持 chat"
                f"（/chat/completions）与 responses（/responses）")
    return ""


def public_profiles() -> list[dict]:
    """给浏览器的安全视图：**不含任何密钥字段**，但带上「能不能用」的结论。"""
    out = []
    for code, p in sorted(load_profiles().items()):
        issue = profile_issue(p)
        out.append({
            "code": code,
            "name": str(p.get("name") or code),
            "note": str(p.get("note") or ""),
            "model": str(p.get("chatModel") or ""),
            "usable": not issue,
            "issue": issue,
        })
    return out


def _check_ascii(field: str, value: str) -> None:
    """确认字段能安全放进 HTTP 请求头。

    HTTP 头只能编码 latin-1，混入中文会让 urllib 抛出
    ``'latin-1' codec can't encode characters in position N-M`` ——
    这个报错完全看不出问题在哪（真实案例：profiles.json 里没删掉的占位符
    ``sk-你的OpenAI密钥``）。
    """
    bad = sorted({c for c in value if ord(c) > 126})
    if bad:
        raise TranslateError(
            f"{field} 含非 ASCII 字符（{'、'.join(bad[:6])}），"
            f"像是占位符没替换成真实值；请在服务端 profiles.json 里改掉，"
            f"或换一个预设代号")


# 预设代号里声明的协议名 → 内部取值
_API_ALIASES = {
    "": "chat", "chat": "chat", "chat/completions": "chat", "chat_completions": "chat",
    "completions": "chat", "openai": "chat",
    "responses": "responses", "response": "responses", "/responses": "responses",
}


def resolve_config(profile_code: str | None = None, override: dict | None = None) -> dict:
    """把「代号」或「页面手填的 baseUrl/key」解析成一份可用的连接配置。

    优先级：页面手填（仅本次任务、只在内存） > 预设代号 > 环境变量。
    只返回本次调用需要的字段，绝不把密钥写进日志或响应。
    """
    override = override or {}
    base_url = (override.get("baseUrl") or "").strip()
    api_key = (override.get("apiKey") or "").strip()
    model = (override.get("chatModel") or override.get("model") or "").strip()
    api = (override.get("api") or "").strip().lower()
    thinking = None

    if not (base_url and api_key):
        prof = None
        if profile_code:
            prof = load_profiles().get(profile_code)
            if prof is None:
                raise TranslateError(
                    f"预设代号 {profile_code!r} 不存在或 PROFILES_FILE 未配置")
        if prof:
            # 代号本身有问题（占位符/含中文）就在这儿拦下来，
            # 别等跑起来才抛出那句看不懂的 latin-1 错误
            issue = profile_issue(prof)
            if issue:
                raise TranslateError(
                    f"预设代号 {profile_code!r} 不可用：{issue}。"
                    f"请让管理员在服务端 profiles.json 里补齐，或换一个代号")
            base_url = base_url or str(prof.get("baseUrl") or "").strip()
            api_key = api_key or str(prof.get("apiKey") or "").strip()
            model = model or str(prof.get("chatModel") or "").strip()
            thinking = prof.get("thinking")
            # ★ 只认 api / chatApi，**绝不能用 refineApi 兜底**：
            #   refineApi 是「精修通道」的协议，与主通道无关。snow 这类代号
            #   主通道走 /chat/completions、精修走 /responses，一旦继承就会把
            #   主通道打到不存在的端点上（实测：硅基流动返回 404 page not found）。
            api = api or str(prof.get("api") or prof.get("chatApi") or "").strip().lower()
        else:
            base_url = base_url or (os.environ.get("AI_BASE_URL") or "").strip()
            api_key = api_key or (os.environ.get("AI_API_KEY") or "").strip()
            model = model or (os.environ.get("CHAT_MODEL") or "").strip()
            api = api or (os.environ.get("DOCBRIDGE_API") or "").strip().lower()

    base_url = base_url.rstrip("/")
    if not base_url:
        raise TranslateError("没有可用的翻译通道：请在页面上选一个预设代号，或填写 Base URL 与 Key")
    if not api_key:
        raise TranslateError("所选预设代号没有配置 apiKey，请在服务端 profiles.json 里补齐")
    _check_ascii("API Key", api_key)
    _check_ascii("Base URL", base_url)
    if api not in _API_ALIASES:
        raise TranslateError(
            f"预设里的接口类型 {api!r} 不认识；只支持 chat（/chat/completions）"
            f"与 responses（/responses）")
    return {
        "provider": (override.get("provider") or "").strip() or ("google" if base_url == "google" else "openai"),
        "api": _API_ALIASES[api],
        "baseUrl": base_url,
        "apiKey": api_key,
        "chatModel": model or "deepseek-chat",
        # False = 显式关闭推理模型的思考；None = 不动它（端点默认行为）
        "thinking": thinking,
    }


def resolve_refine_config(profile_code: str | None = None) -> dict | None:
    """解析「精修通道」的配置；**没配置就返回 None**（调用方据此走"精修未完成"）。

    为什么单独一张配置、而不是复用主通道：精修通常挂在**另一个供应商**上
    （profiles.json 里 snow 的 refineBaseUrl/refineApiKey/refineModel/refineApi 就是
    指向一个 GPT 中转站；同传站 LiveBridge 用的也是同一组字段），与主通道的
    baseUrl / 模型都没有关系，推导不出来。

    为什么不在没配时退回主通道：那等于用同一个模型把译文再翻一遍，白花钱且看不出差别。
    「花两遍钱」这件事必须由 profiles.json 显式声明，而不是悄悄发生。

    注意 refineApi 的默认值：精修换了 baseUrl（= 另一家端点）却没写协议名时按
    ``responses``——这与同传站 server.mjs 的取值一致（很多 GPT 中转站只有 /responses）。
    同一家端点则沿用主通道的协议。协议名不认识就当作没配精修，不硬试。
    """
    prof = (load_profiles().get(profile_code) if profile_code else None) or None
    if not prof:
        return None
    model = str(prof.get("refineModel") or "").strip()
    key = str(prof.get("refineApiKey") or "").strip()
    base = str(prof.get("refineBaseUrl") or prof.get("baseUrl") or "").strip().rstrip("/")
    if not (model and key and base):
        return None
    api = str(prof.get("refineApi") or "").strip().lower()
    if not api:
        main_base = str(prof.get("baseUrl") or "").strip().rstrip("/")
        main_api = str(prof.get("api") or prof.get("chatApi") or "").strip().lower()
        api = main_api if base == main_base else "responses"
    if api not in _API_ALIASES:
        return None
    try:
        # 配置坏掉（占位符/中文）时**不抛错**：那是"精修用不了"，
        # 不是"这份文件翻译不了"，调用方要能安静地退回初译。
        _check_ascii("精修 Base URL", base)
        _check_ascii("精修 API Key", key)
    except TranslateError:
        return None
    return {
        "provider": "openai",
        "api": _API_ALIASES[api],
        "baseUrl": base,
        "apiKey": key,
        "chatModel": model,
        "thinking": None,
    }


# ---------------------------------------------------------------- HTTP 调用


def _add_usage(sink: dict | None, data: dict) -> None:
    """把接口返回的**真实 token 用量**累加到 sink 里。

    为什么要记：管理页的成本估算原来只能按「字符数/4」这种经验系数折算，
    按 token 计价的时候会偏；接口既然回了 usage，就别浪费。
    """
    if sink is None or not isinstance(data, dict):
        return
    usage = data.get("usage") or {}
    tin = usage.get("prompt_tokens") or usage.get("input_tokens")
    tout = usage.get("completion_tokens") or usage.get("output_tokens")
    if tin:
        sink["tokens_in"] = sink.get("tokens_in", 0) + int(tin)
    if tout:
        sink["tokens_out"] = sink.get("tokens_out", 0) + int(tout)


def _post_json(url: str, body: dict, api_key: str, timeout: int,
               usage_sink: dict | None = None) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + api_key,
            # 有些中转站前面挂着 Cloudflare，会按 UA 拦掉 Python 默认的
            # "Python-urllib/3.x"（报 error code: 1010），但放行自定义 UA。
            # 真被拦了可以用 DOCBRIDGE_UA 换成浏览器 UA。
            "User-Agent": (os.environ.get("DOCBRIDGE_UA") or "docbridge/1.0").strip(),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        _add_usage(usage_sink, data)
        return data
    except urllib.error.HTTPError as exc:            # noqa: PERF203
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:                            # noqa: BLE001
            pass
        raise TranslateError(f"接口返回 HTTP {exc.code}：{detail}") from exc
    except urllib.error.URLError as exc:
        raise TranslateError(f"网络不可达：{exc.reason}") from exc
    except TimeoutError as exc:
        raise TranslateError(f"请求超时（{timeout}s）") from exc
    except json.JSONDecodeError as exc:
        raise TranslateError("接口返回的不是合法 JSON") from exc


def _extract_responses_text(data: dict) -> str:
    """从 Responses 协议（/responses）的返回里取出正文。

    这个协议的返回结构比 chat/completions 绕：正文可能在顶层 output_text，
    也可能散在 output[].content[].text 里，所以两处都找一遍。
    """
    text = data.get("output_text")
    if isinstance(text, str) and text.strip():
        return text
    parts: list[str] = []
    for item in (data.get("output") or []):
        if not isinstance(item, dict):
            continue
        for chunk in (item.get("content") or []):
            if isinstance(chunk, dict):
                piece = chunk.get("text") or chunk.get("output_text")
                if isinstance(piece, str):
                    parts.append(piece)
    joined = "".join(parts).strip()
    if joined:
        return joined
    err = data.get("error")
    if err:
        raise TranslateError(f"Responses 接口报错：{str(err)[:200]}")
    raise TranslateError(f"Responses 接口返回结构异常：{str(data)[:200]}")


def _call_responses(cfg: dict, system: str, user: str, json_mode: bool = True,
                    usage_sink: dict | None = None) -> str:
    """调用 OpenAI **Responses** 协议（``/responses``）。

    为什么要支持它：很多 GPT 中转站只提供这个协议，对 ``/chat/completions``
    直接回 405（实测 某个 GPT 中转站 就是如此）。同域部署的另一个同类项目也有一层同样的
    兼容——它的注释写得很直白：「很多 GPT 中转站只支持这个」。

    请求体用 ``instructions``（系统提示）+ ``input``（用户内容）；
    ``json_mode`` 时加 ``text.format=json_object``（不支持时上层会自动退回普通模式重试）。
    """
    body: dict = {"model": cfg["chatModel"], "instructions": system, "input": user}
    if json_mode:
        body["text"] = {"format": {"type": "json_object"}}
    data = _post_json(cfg["baseUrl"] + "/responses", body, cfg["apiKey"],
                      DEFAULT_TIMEOUT, usage_sink)
    return _extract_responses_text(data)


def call_chat(cfg: dict, system: str, user: str, json_mode: bool = True,
              usage_sink: dict | None = None) -> str:
    """调用一次对话接口，返回助手文本。按 ``cfg["api"]`` 选择协议。"""
    if cfg.get("provider") == "google":
        return _call_google(user)
    if cfg.get("api") == "responses":
        return _call_responses(cfg, system, user, json_mode, usage_sink)
    # 关掉「思考」：推理模型的思考 token 是**按输出计费**的，批量翻译纯属烧钱。
    # 实测 DeepSeek-V4-Flash 翻同一页术语表：带思考 18.5s / 2340 输出 token，
    # 关掉后 3.1s / 359 输出 token —— 快 6 倍且便宜得多。
    # 只在预设里显式写了 thinking: false 时才发这个参数，免得别的端点不认识它。
    if cfg.get("thinking") is False:
        body_thinking = {"type": "disabled"}
    body = {
        "model": cfg["chatModel"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
        "max_tokens": 8000,
        "stream": False,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    if cfg.get("thinking") is False:
        body["thinking"] = {"type": "disabled"}
    data = _post_json(cfg["baseUrl"] + "/chat/completions", body, cfg["apiKey"],
                      DEFAULT_TIMEOUT, usage_sink)
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise TranslateError(f"接口返回结构异常：{str(data)[:200]}") from exc


def _call_google(user: str) -> str:
    """Google 免费通道（可选兜底）：仅当配置了 provider=google 时使用。"""
    vendor = os.path.join(os.path.dirname(ROOT), "vendor")
    if os.path.isdir(vendor) and vendor not in sys.path:
        sys.path.insert(0, vendor)
    try:
        from deep_translator import GoogleTranslator
    except ImportError as exc:
        raise TranslateError("未安装 deep_translator，无法使用 Google 免费通道") from exc
    lines = [ln for ln in user.splitlines() if re.match(r"^\d+\. ", ln)]
    texts = [re.sub(r"^\d+\. ", "", ln) for ln in lines]
    try:
        out = GoogleTranslator(source="auto", target="zh-CN").translate_batch(texts)
    except Exception as exc:                         # noqa: BLE001
        raise TranslateError(f"Google 通道失败：{exc}") from exc
    return json.dumps({str(i + 1): (t or "") for i, t in enumerate(out)}, ensure_ascii=False)


# ---------------------------------------------------------------- 提示词

SYSTEM_LINE = (
    "You are a professional technical translator. You translate document text "
    "faithfully and concisely. You output ONLY valid JSON, nothing else."
)

BATCH_LINE_PROMPT = """\
Translate each numbered source line below into {target}.

Return ONLY a JSON object mapping line numbers to translations, e.g. {{"1": "译文1", "2": "译文2"}}.

Rules:
- The JSON object MUST contain exactly the keys 1..{n} ({n} keys), no more, no less.
- Each value translates EXACTLY ONE input line, written as a single line of text \
(no line breaks inside a value).
- Output {target} only.
- NEVER output ellipsis (……, …, ...) or placeholders. NEVER output meta comments \
or instructions such as "原文未提供", "保持原样", "translation:". Always produce the \
translation itself.
- IMPORTANT: each input line is an INDEPENDENT FRAGMENT of a paragraph. Lines may \
begin or end mid-word because of line wrapping (e.g. "pub-" or "lic policy."). \
Translate each fragment literally, fragment by fragment. Never merge two lines into \
one value, never complete a sentence across lines, never repeat the same translation \
for different lines, never invent text.
- Preserve numbers, formulas, code identifiers and citation markers as-is.
- **Keep all mathematical symbols and formulas exactly as they are** (√ ∑ ∫ ∏ ∂ ∆ ≈ ≠ ≤ ≥ ± ∞ ∈ ⊂ ∪ ∩ → ⇒ π θ α β λ μ σ Ω).
  Do not drop them, do not rewrite a formula as prose. Writing the Chinese name is acceptable
  (√ → 根号, ∑ → 求和, ∫ → 积分), but never lose the meaning.
- If a line is pure numbers/symbols/code, keep it unchanged.
{extra}{context}
Lines:
{numbered}"""

BATCH_PARA_PROMPT = """\
Translate each numbered source paragraph below into {target}.

Return ONLY a JSON object mapping item numbers to translations, e.g. {{"1": "译文1", "2": "译文2"}}.

Rules:
- The JSON object MUST contain exactly the keys 1..{n} ({n} keys), no more, no less.
- Each value translates EXACTLY ONE input item. Keep each value a single line of \
text (no line breaks inside a value).
- Output {target} only.
- Translate naturally and completely: keep the full meaning, do not summarise, do \
not shorten, do not add explanations or notes.
- NEVER output meta comments such as "原文未提供" or "以下是译文". Always produce the \
translation itself.
- Preserve numbers, formulas, code identifiers and citation markers as-is.
- If an item is pure numbers/symbols/code, keep it unchanged.
- Keep the tone and register of the source (heading stays a heading, caption stays a \
caption, table cell stays short).
{extra}{context}
Items:
{numbered}"""


def _build_prompt(items: list[str], target: str, kind: str, extra: str = "",
                  context_block: str = "") -> str:
    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(items))
    tpl = BATCH_LINE_PROMPT if kind == "line" else BATCH_PARA_PROMPT
    return tpl.format(n=len(items), numbered=numbered, target=target, extra=extra,
                      context=context_block)


# ---------------------------------------------------------------- 精修（第二遍打磨）
#
# 「精修」= 初译之上再用第二个模型（通常是 GPT）把每一段译文重写一遍。
# 开关与凭据都来自 profiles.json 的 refineBaseUrl / refineApiKey / refineModel / refineApi，
# 与同传站 LiveBridge 用的是同一组字段（见 live-interpreter/server.mjs 的 task='refine'）。

REFINE_SYSTEM_LINE = (
    "You are a senior bilingual editor. You improve existing translations of document "
    "text and never change the meaning. You output ONLY valid JSON, nothing else."
)

REFINE_PROMPT = """\
Below are numbered pairs of source text and a first-pass {target} translation.

Polish EACH pair: fix mistranslations, wrong terminology and awkward wording, and make \
the {target} read like it was written by a native professional. Keep every piece of \
information the source carries.

Return ONLY a JSON object mapping item numbers to improved translations, e.g. {{"1": "译文1"}}.

Rules:
- The JSON object MUST contain exactly the keys 1..{n} ({n} keys), no more, no less.
- {shape}
- Output {target} only.
- NEVER add or drop information: no summarising, no explanations, no notes.
- NEVER output ellipsis (……, …, ...) or placeholders like "原文未提供" or "保持原样"; \
always output the translation itself.
- If a first-pass translation is already correct and idiomatic, return it unchanged.
- Preserve numbers, formulas, code identifiers and citation markers as-is, and keep all \
mathematical symbols exactly as they are.
{context}
Pairs:
{numbered}"""


def _build_refine_prompt(pairs: list[tuple[str, str]], target: str, kind: str,
                         context_block: str = "") -> str:
    numbered = "\n".join(
        f"{i + 1}. SOURCE: {s}\n   DRAFT: {d}" for i, (s, d) in enumerate(pairs))
    shape = ("Each value must cover EXACTLY ONE pair and be a single line of text "
             "(no line breaks inside a value). The sources are independent fragments, "
             "so never merge two pairs into one value."
             if kind == "line" else
             "Each value must cover EXACTLY ONE pair and be a single line of text "
             "(no line breaks inside a value). Never merge two pairs into one value.")
    return REFINE_PROMPT.format(n=len(pairs), numbered=numbered, target=target,
                                shape=shape, context=context_block)


# 术语表默认位置：先找 docbridge 自己的，再找兄弟目录（与 同类项目 共用那份）
def _glossary_candidates(explicit: str | None = None) -> list[str]:
    here = os.path.dirname(os.path.abspath(__file__))
    return [p for p in (
        explicit,
        os.environ.get("DOCBRIDGE_GLOSSARY"),
        os.path.join(here, "glossary.json"),
        os.path.join(os.path.dirname(here), "同域项目", "glossary.json"),
    ) if p]


def load_glossary(explicit: str | None = None) -> dict:
    """读取术语表并扁平化为 ``{英文: 中文}``。

    支持 ``{分组: {英文: 中文}}`` 的嵌套结构（以 ``_`` 开头的键视为注释）。
    找不到文件就返回空字典——术语表是锦上添花，不该让翻译失败。
    """
    for path in _glossary_candidates(explicit):
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            continue
        flat: dict = {}

        def walk(node):
            for key, val in (node or {}).items():
                if str(key).startswith("_"):
                    continue
                if isinstance(val, dict):
                    walk(val)
                elif isinstance(val, str) and val.strip() and not str(key).isdigit():
                    flat.setdefault(str(key), val.strip())

        walk(raw)
        return flat
    return {}


_GLOSSARY_RE_CACHE: dict = {}


def _en_plural(word: str) -> str:
    """英文名词的规则复数。

    术语表里都是普通名词，规则变化够用。**刻意不猜不规则复数**
    （index→indices、analysis→analyses）：猜错会往提示词里塞一个不存在的词，
    比漏掉更糟。宁可漏，不可错。
    """
    low = word.lower()
    if low.endswith(("s", "x", "z", "ch", "sh")):
        return word + "es"
    if len(word) > 1 and low.endswith("y") and low[-2] not in "aeiou":
        return word[:-1] + "ies"
    return word + "s"


def _glossary_variants(key: str) -> tuple:
    """术语的匹配变体：原形 + 规则复数。

    ★ 为什么必须加复数：教材里 ``firms`` / ``markets`` / ``costs`` 出现的次数
      往往比单数还多，而原来的词边界正则 ``(?<![A-Za-z])firm(?![A-Za-z])``
      匹配不到 ``firms`` —— firm 后面跟着 s，正好被 ``(?![A-Za-z])`` 挡掉。
      结果是术语表在**复数段落上整片失效**：该被统一的词反而最不统一。
      示例里 ``firm → 厂商`` 这类规则，在真实教材上有一大半是白给的。
    """
    if not key:
        return ()
    if " " in key or "-" in key:
        # 多词术语只把**最后一个词**变复数：marginal cost → marginal costs
        cut = max(key.rfind(" "), key.rfind("-"))
        return (key, key[:cut + 1] + _en_plural(key[cut + 1:]))
    return (key, _en_plural(key))


def _glossary_pattern(key: str, variants: tuple):
    """单词类术语的「按词边界匹配」正则，编译结果缓存起来。

    术语表有 800+ 条时，每批都要跑 800 多次匹配；Python 自带的正则缓存只有
    512 个槽位，会不停重编译同一个 pattern。自己缓存后开销基本只剩匹配本身。
    """
    pat = _GLOSSARY_RE_CACHE.get(key)
    if pat is None:
        body = "|".join(re.escape(v) for v in variants)
        pat = re.compile(r"(?<![A-Za-z])(?:" + body + r")(?![A-Za-z])", re.I)
        _GLOSSARY_RE_CACHE[key] = pat
    return pat


def _glossary_hit(key: str, variants: tuple, blob: str) -> bool:
    """术语是否出现在这段文本里（单词按词边界，多词按子串）。"""
    if " " in key or "-" in key:
        low = blob.lower()
        return any(v.lower() in low for v in variants)
    return _glossary_pattern(key, variants).search(blob) is not None


def _glossary_hits(context: dict | None, items: list) -> list:
    """本批真的出现的术语，**长术语优先，最多 20 条**。

    术语表大了以后，一批里命中几十条是常事，而注入上限是 20 条。
    必须让更长、更具体的术语优先（marginal revenue > revenue），
    否则泛词会挤掉真正需要约束的专业词。

    返回值有两个用处：拼提示词（``_context_block``），以及译完之后做
    「术语到底有没有被遵守」的软校验（``_soft_reason``）。
    """
    glossary = (context or {}).get("glossary") or {}
    if not isinstance(glossary, dict) or not glossary:
        return []
    blob = "\n".join(items)
    hits = []
    for key, val in glossary.items():
        key = str(key)
        if len(key) < 3:
            continue
        # ★ 单词术语要按**词边界**匹配：子串匹配会让 firm 命中 confirm、
        #   cost 命中 cost-effective，把不相干的术语提示塞进提示词，反而误导模型。
        if _glossary_hit(key, _glossary_variants(key), blob):
            hits.append((key, val))
    hits.sort(key=lambda kv: (-len(kv[0]), kv[0]))
    return hits[:20]


def _context_block(context: dict | None, items: list,
                   hits: list | None = None) -> str:
    """把「文档背景 + 相关术语」拼成提示词里的一小段。

    为什么值得加：短行、表格单元格、术语表条目本身没有上下文，孤立翻译时模型
    只能靠猜——实测把 Capital/Capitals 单独拿出来，会得到「首都」「大写字母」，
    而在财务文档上下文里正确答案是「资本」。每批只多几十个 token，
    换来的是术语一致与语境正确。

    ``hits`` 由调用方传入时复用（调用方算过一次，用来做译后校验），
    避免同一批把 800 条术语匹配跑两遍。
    """
    if not context:
        return ""
    parts = []
    doc_context = str(context.get("doc_context") or "").strip()
    if doc_context:
        parts.append("Document context (use it only to disambiguate terminology):\n"
                     + doc_context[:400])
    if hits is None:
        hits = _glossary_hits(context, items)
    if hits:
        parts.append("Established terminology (must be followed):\n"
                     + "; ".join(f"{k} → {v}" for k, v in hits))
    if not parts:
        return ""
    return "\n" + "\n".join(parts) + "\n"


# ---------------------------------------------------------------- 解析与校验


def _parse_json_reply(reply: str, n: int) -> list[str] | None:
    """从模型回复里解析 {"1": …} 映射；缺键/空值即判失败（返回 None）。"""
    if not isinstance(reply, str):
        return None
    text = reply.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        obj = json.loads(text[start:end + 1])
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    out = []
    for i in range(1, n + 1):
        v = obj.get(str(i))
        if v is None or not isinstance(v, str) or not v.strip():
            return None
        out.append(_tidy(v))
    return out


def _tidy(text: str) -> str:
    """压平换行与多余空白（译文值必须能安全写回单行/单个段落）。"""
    t = re.sub(r"\s*\r?\n\s*", " ", text or "")
    return re.sub(r"[ \t]{2,}", " ", t).strip()


def _validate_reason(values: list[str], sources: list[str], target: str,
                     kind: str) -> str:
    """校验一批译文；通过返回 ""，否则返回**具体原因**（用于日志与重试提示）。

    返回原因而不是布尔值，是因为线上真出过「只看到『重试中』却不知道为啥」的
    情况——排查得靠猜。原因里带上位置与短样本，一眼就能定位。
    """
    if len(values) != len(sources):
        return f"数量不符（期望 {len(sources)}，实际 {len(values)}）"
    prev_v = prev_s = None
    for idx, (v, s) in enumerate(zip(values, sources), 1):
        if not v or "\n" in v or "\r" in v:
            return f"第 {idx} 条为空或含换行：{v[:30]!r}"
        if META_RE.search(v):
            return f"第 {idx} 条含元注释：{v[:40]!r}"
        if v != s and ELLIPSIS_RE.search(v):
            return f"第 {idx} 条含省略号：{v[:40]!r}"
        if v != s and target.startswith("zh-Hans") and TRAD_RE.search(v):
            return f"第 {idx} 条含繁体字：{v[:40]!r}"
        # 合并检测：**只看相邻两条**是否撞了同一个译文。
        # 原来的写法是「任意两条不同原文撞译文即判失败」，那是错的：
        # 术语表里 Capital 与 Capitals 本来就都译「资本」、Easy 与 Ease 都译「容易」，
        # 结果把完全正确的译文判成失败，白重试三次再退化成 51 次单独请求。
        # 模型真的把多行合并成一条时，重复必然出现在**相邻**位置，且原文够长。
        if (prev_v is not None and v == prev_v and s != prev_s
                and len(s) >= 10 and len(prev_s or "") >= 10):
            return (f"相邻第 {idx - 1}/{idx} 条译文相同，疑似合并："
                    f"{s[:24]!r} 与 {prev_s[:24]!r} 都译成 {v[:24]!r}")
        # 长度疑似合并（译文远长于原文）
        lost = missing_math_symbols(s, v)
        if lost:
            return (f"第 {idx} 条丢失数学符号 {' '.join(lost)}（原文 {s[:26]!r}，"
                    f"译文 {v[:26]!r}）")
        if kind == "line" and len(s) >= 10 and len(v) > 2.2 * len(s) + 30:
            return f"第 {idx} 条译文异常长（{len(v)} 字符 vs 原文 {len(s)}），疑似合并"
        prev_v, prev_s = v, s
    return ""


def _validate(values: list[str], sources: list[str], target: str, kind: str) -> bool:
    """校验是否通过（薄封装，保留原有调用方式）。"""
    return _validate_reason(values, sources, target, kind) == ""


# ------------------------------------------------- 软校验（只用来「多打磨一次」）
#
# 分工必须分清楚：
#   • 硬校验（_validate_reason）失败会一路降级 —— 换通道 → 分组重试 → 逐条重试
#     → 最后**保留原文**。所以它只能放极保守的规则，判错一次就毁掉一段内容。
#   • 软校验针对的是「大概率译错、但个别情况下也说得通」的问题：数字被吃掉、
#     术语表没被遵守。这类问题判错了不该毁掉一条正确的译文。
# 做法：只多给模型**一次**机会，并给出针对性的提示；第二次无论怎样都收下。

NUM_RE = re.compile(r"\d[\d,\s]*")
# 中文数字（含「百分之五十」这类写法）。用来避免误判：译文用中文数字表达时，
# 阿拉伯数字当然找不到，那不是「丢数字」。
CN_NUM_RE = re.compile(r"[一二三四五六七八九十百千万亿零两〇]")
# 只对「够具体、不歧义」的术语强制：多词，或足够长的单词。
# 短词反过来会误伤 —— capital 在「the capital of France」里本来就该译「首都」，
# 硬按术语表逼成「资本」反而错。这类短词继续只在提示词里做建议。
GLOSSARY_ENFORCE_MIN_LEN = 8


def _numbers(text: str) -> set:
    """抽出数字串，去掉千分位与空格，便于跨写法比对（1,000 与 1000 视作同一个）。"""
    return {re.sub(r"[,\s]", "", m) for m in NUM_RE.findall(text or "")}


def _term_present(en: str, text: str) -> bool:
    """术语（含复数变体）是否出现在这段原文里。"""
    en = str(en)
    return _glossary_hit(en, _glossary_variants(en), text)


def _soft_reason(values: list[str], sources: list[str], target: str, kind: str,
                 terms: list | None = None) -> str:
    """软校验：返回「值得再打磨一次」的原因，没问题返回空串。

    只查两件事，且**一发现就返回**（每批最多多花一次请求）：

    1. **数字整个丢了**（逐行判）——技术/金融文档里被吃掉的数字是最严重的隐蔽
       错误，而原来的硬校验只看了数学符号，完全没看数字（``Revenue was
       $1,234,567`` 译成「收入很高」是能过校验的）。判定用「一个字面数字都没有、
       也没有中文数字」这个很紧的条件，所以「百分之五十」「第2章」都不会误判。
    2. **术语表整段没被遵守**（**按整段判，不逐行**）——理由见下面 ★。
    """
    if len(values) != len(sources):
        return ""
    for idx, (v, s) in enumerate(zip(values, sources), 1):
        want = {n for n in _numbers(s) if len(n) >= 2}      # 单位数常写成「二/两」，不查
        if want and not (_numbers(v) & want) and not CN_NUM_RE.search(v):
            return (f"第 {idx} 条疑似丢失数字（原文有 {'、'.join(sorted(want)[:3])}，"
                    f"译文里找不到）：{v[:30]!r}")
    # ★ 术语这一条**必须按整段判，不能逐行**。
    #   译文是整段翻完、再按原行的行盒宽度切回每一行的（见
    #   pdf_inplace._split_translation），所以单独一行里当然可能找不到那个词——
    #   「消费者」正好被切到了下一行。
    #   实测（真实教材缓存，10 个批次）：逐行判触发 5 个，**全是误报**——
    #     · `substitute → 替代品`：译文写的是「用一种商品替代另一种商品」，词用了，
    #       只是词性不同；
    #     · `consumer → 消费者`：整段译文里明明有，只是没落在这一行。
    #   而每误报一次就要多打一次模型，直接让整本书变慢、变贵。改成整段判之后，
    #   同样 10 个批次触发 0 个，仍然兜得住「整批完全无视术语表」的情况。
    if terms:
        joined_v = " ".join(values)
        joined_s = " ".join(sources)
        for en, zh in terms:
            zh = str(zh)
            if len(zh) < 2 or len(str(en)) < GLOSSARY_ENFORCE_MIN_LEN:
                continue
            if zh not in joined_v and _term_present(en, joined_s):
                return f"整段译文未使用约定的术语「{en} → {zh}」"
    return ""


# ---------------------------------------------------------------- 回调工厂


def _correction_hint(reason: str, soft: bool = False) -> str:
    """把校验原因翻译成给模型的纠正提示。

    比笼统的「你被拒了，请一一对应」有效得多，也更不容易把正确的译文带偏——
    之前那句笼统提示里写着 "repeated"，模型会以为「重复」是错的，
    于是把本该相同的译文硬改成不同的词。

    ``soft=True`` 用于软校验：那是「还能更好」，不是「你被拒了」。措辞必须
    分开——写成「被拒」模型会以为整批不合格，把本来正确的译文也一起改掉。
    """
    base = ("CRITICAL: your previous answer was rejected. Keep the JSON strictly "
            "one-to-one with the input items.\n")
    if soft:
        base = ("Your previous answer was accepted. Only fix the points below, keep "
                "everything else exactly as it was, and keep the JSON strictly "
                "one-to-one with the input items.\n")
    if "繁体" in reason:
        return base + "Use SIMPLIFIED Chinese only (简体中文), never Traditional (繁體).\n"
    if "省略号" in reason:
        return base + "Never use ellipsis (……) or placeholders; write the full translation.\n"
    if "元注释" in reason:
        return base + "Never output comments like 原文未提供; output the translation itself.\n"
    if "数学符号" in reason:
        return base + ("You dropped mathematical symbols. Keep every math symbol "
                       "exactly as in the source (√ ∑ ∫ ∂ ∆ ≈ ≤ ≥ ± ∞ …); writing the "
                       "Chinese name (根号, 求和, 积分) is also acceptable.\n")
    if "合并" in reason:
        return base + ("Do not merge items. Each value must translate only its own item — "
                       "but if two different items genuinely mean the same thing, it is "
                       "correct for them to share the same translation.\n")
    if "换行" in reason or "为空" in reason:
        return base + "Every value must be non-empty and on a single line.\n"
    if "术语" in reason:
        return base + ("Use the established terminology exactly as listed "
                       "(e.g. marginal revenue → 边际收益). Do not swap in a synonym.\n")
    if "数字" in reason:
        return base + ("You dropped a number. Keep every number from the source exactly "
                       "as it appears (amounts, percentages, years).\n")
    return base


def is_hard_batch(items: list[str]) -> bool:
    """判断一批内容是不是「需要强通道攻坚」的难活。

    依据是线上真实踩过的坑：术语表/表格页每行都很短、大量两三个词的首字母大写条目，
    这类内容孤立翻译时模型只能靠猜——实测 `Capital`/`Capitals` 在没有上下文时被译成
    「首都」「大写字母」，而在财务文档上下文里正确答案是「资本」。
    这类批次交给强模型收益最大；普通长句段落用便宜模型已经足够。
    """
    if not items:
        return False
    n = len(items)
    short = sum(1 for t in items if len(t.strip()) <= 24)
    termish = sum(1 for t in items
                  if 0 < len(t.split()) <= 3 and re.search(r"[A-Za-z]", t))
    digitish = sum(1 for t in items if re.search(r"\d", t))
    return (short / n >= 0.5) or (termish / n >= 0.4) or (digitish / n >= 0.4)


def make_translator(cfg: dict, source_lang: str = "auto",
                    target_lang: str = "zh-Hans", kind: str = "line",
                    log=None, fallback: dict | None = None,
                    escalate: dict | None = None, route: str = "fallback",
                    channel_names: dict | None = None,
                    glossary: dict | None = None) -> callable:
    """构造 ``translate(texts, context=None) -> list[str]`` 回调（严格等长同序）。

    通道链与路由（``route``）：

    * ``single``  —— 只用 ``cfg``；
    * ``fallback``—— ``cfg`` 彻底失败时自动切到 ``fallback``（默认）；
    * ``hybrid``  —— 难批次（术语表/表格等）优先用 ``escalate`` 那个强通道，
      普通批次仍用 ``cfg``；两者互为兜底。

    实现上就是「每批按内容决定通道顺序，按顺序试到成功为止」——
    兜底与攻坚共用同一套机制，不额外引入状态。
    """
    target = _LANG_PROMPT_NAME.get(target_lang, target_lang)
    stats = {
        "chars_in": 0, "chars_out": 0, "calls": 0,
        "batches": 0, "retries": 0, "soft_retries": 0, "failed": 0, "skipped": 0,
        "tokens_in": 0, "tokens_out": 0,      # 接口回的真实用量，用于准确算钱
        "fallbacks": 0, "escalated": 0,       # 走了备用通道 / 走了强通道的批次数
        "by_channel": {},                     # 分通道 token 用量（混合路由下要知道钱花在哪）
    }
    lock = threading.Lock()
    names = channel_names or {}
    strikes: dict[str, int] = {}          # 通道 -> 连续被限流次数
    cooling: dict[str, float] = {}        # 通道 -> 冷却截止时间戳

    def _is_cooling(chan: dict) -> bool:
        return time.time() < cooling.get(_label(chan), 0.0)

    def _label(c: dict) -> str:
        return names.get(id(c)) or c.get("chatModel") or c.get("baseUrl") or "?"

    primary = cfg
    strong = escalate if route == "hybrid" else None
    reserve = fallback

    def _chain_for(items: list[str]) -> list[dict]:
        """按批次内容决定尝试顺序；去重；跳过正在冷却（被限流）的通道。"""
        order: list[dict] = []
        if strong is not None and is_hard_batch(items):
            order.append(strong)
        order.append(primary)
        for extra in (strong, reserve):
            if extra is not None and all(extra is not c for c in order):
                order.append(extra)
        alive = [c for c in order if not _is_cooling(c)]
        return alive or order[:1]        # 全都在冷却就还是用首选，别什么都不做

    def say(msg: str) -> None:
        if log:
            try:
                log(msg)
            except Exception:                        # noqa: BLE001
                pass

    def _chunks(items: list[tuple[int, str]]):
        chunk: list[tuple[int, str]] = []
        chars = 0
        for pair in items:
            ln = len(pair[1])
            if chunk and (len(chunk) >= MAX_CHUNK_LINES or chars + ln > MAX_CHUNK_CHARS):
                yield chunk
                chunk, chars = [], 0
            chunk.append(pair)
            chars += ln
        if chunk:
            yield chunk

    def _account(chan: dict, sink: dict) -> None:
        """把一次请求的 token 同时记到总量和**该通道**名下。

        为什么分通道记：混合路由下一次任务会同时用两个通道，
        只有分开记才知道钱花在哪边（总量对算钱没用）。
        """
        tin = int(sink.get("tokens_in") or 0)
        tout = int(sink.get("tokens_out") or 0)
        if not (tin or tout):
            return
        with lock:
            stats["tokens_in"] += tin
            stats["tokens_out"] += tout
            per = stats.setdefault("by_channel", {})
            slot = per.setdefault(_label(chan), {"tokens_in": 0, "tokens_out": 0, "calls": 0})
            slot["tokens_in"] += tin
            slot["tokens_out"] += tout
            slot["calls"] += 1

    def _try_channel(chan: dict, items: list[str], ctx_block: str,
                     terms: list | None = None) -> list[str] | None:
        """在**单个通道**上试 MAX_ATTEMPTS 次；成功返回结果，彻底失败返回 None。"""
        extra = ""
        for attempt in range(MAX_ATTEMPTS):
            prompt = _build_prompt(items, target, kind, extra, ctx_block)
            reply = None
            sink: dict = {}
            try:
                reply = call_chat(chan, SYSTEM_LINE, prompt, json_mode=True,
                                  usage_sink=sink)
                _account(chan, sink)
                with lock:
                    strikes.pop(_label(chan), None)      # 成功即清零
            except TranslateError as exc:
                if _is_rate_limited(exc):
                    # 被限流：立刻放弃这个通道，别捶它（重试只会白等）
                    with lock:
                        n = strikes[_label(chan)] = strikes.get(_label(chan), 0) + 1
                        tripped = n >= RATE_LIMIT_STRIKES
                        if tripped:
                            cooling[_label(chan)] = time.time() + RATE_LIMIT_COOLDOWN
                    if tripped:
                        say(f"[warn] {_label(chan)} 连续被限流 {n} 次，"
                            f"{int(RATE_LIMIT_COOLDOWN)} 秒内不再使用它")
                    else:
                        say(f"[warn] {_label(chan)} 被限流（{exc}），本批换道")
                    break
                if attempt == 0:
                    # 有些端点不支持 response_format=json_object，退回普通模式再试
                    try:
                        sink = {}
                        reply = call_chat(chan, SYSTEM_LINE, prompt, json_mode=False,
                                          usage_sink=sink)
                        _account(chan, sink)
                    except TranslateError as exc2:
                        say(f"[warn] {_label(chan)} 请求失败：{exc2}")
                        if attempt < MAX_ATTEMPTS - 1:
                            time.sleep(1.5 * (attempt + 1))
                        continue
                else:
                    say(f"[warn] {_label(chan)} 请求失败：{exc}")
                    if attempt < MAX_ATTEMPTS - 1:
                        time.sleep(1.5 * (attempt + 1))
                    continue
            with lock:
                stats["calls"] += 1
            parsed = _parse_json_reply(reply, len(items)) if reply else None
            if parsed is None:
                reason = "回复不是合法 JSON，或缺少/多出条目"
            else:
                reason = _validate_reason(parsed, items, target_lang, kind)
                if not reason:
                    # 硬校验过了，再顺手看一眼「软问题」：数字丢了、术语表没被遵守。
                    # 只在第一次尝试时多给一次机会，第二次无论怎样都收下——
                    # 软问题判错的代价，绝不能是丢掉一条正确的译文。
                    soft = (_soft_reason(parsed, items, target_lang, kind, terms)
                            if attempt == 0 else "")
                    if not soft:
                        return parsed
                    with lock:
                        stats["soft_retries"] += 1
                    say(f"[info] 第 {attempt + 1}/{MAX_ATTEMPTS} 次软校验提示"
                        f"（{soft}），再打磨一次")
                    extra = _correction_hint(soft, soft=True)
                    continue
            with lock:
                stats["retries"] += 1
            say(f"[warn] 第 {attempt + 1}/{MAX_ATTEMPTS} 次校验未通过（{reason}），重试中")
            extra = _correction_hint(reason)
        return None

    def _one_batch(items: list[str], context: dict | None = None) -> list[str] | None:
        """按通道链依次尝试；任一通道成功即返回。全失败返回 None。

        术语匹配在这里算一次、给两处用：拼提示词，以及译完之后做
        「术语到底有没有被遵守」的软校验。所以这里收的是 context，不是拼好的串。
        """
        hits = _glossary_hits(context, items)
        ctx_block = _context_block(context, items, hits)
        chain = _chain_for(items)
        for pos, chan in enumerate(chain):
            got = _try_channel(chan, items, ctx_block, hits)
            if got is not None:
                if pos > 0:                    # 首个通道没扛住，用了后面的
                    with lock:
                        stats["fallbacks"] += 1
                        if strong is not None and chan is strong:
                            stats["escalated"] += 1
                    say(f"[warn] {_label(chain[0])} 未能完成这一批，已改用 "
                        f"{_label(chan)}（{len(items)} 条）")
                elif strong is not None and chan is strong:
                    with lock:
                        stats["escalated"] += 1
                return got
        return None

    def translate(texts, context=None) -> list[str]:
        # 术语表在这里统一注入：这样**所有模式**（原位/双语/Word/OCR）都吃得到，
        # 不用每个管线各自处理一遍（之前只有 PDF 原位那条路有，是覆盖缺口）。
        if glossary and not (context or {}).get("glossary"):
            context = dict(context or {}, glossary=glossary)
        src = ["" if t is None else str(t) for t in texts]
        out: list[str] = list(src)
        todo = [(i, t) for i, t in enumerate(src) if needs_translation(t, target_lang)]
        with lock:
            stats["skipped"] += len(src) - len(todo)
            stats["chars_in"] += sum(len(t) for _, t in todo)
        if not todo:
            return out

        batches = list(_chunks(todo))
        workers = min(CONCURRENCY, len(batches))
        say(f"  翻译 {len(todo)} 条（{len(batches)} 批，并发 {workers}）")

        def work(batch: list[tuple[int, str]]):
            texts_only = [t for _, t in batch]
            # 上下文按批计算：术语表只注入「本批真的出现」的词，避免白白塞满提示词
            return batch, _one_batch(texts_only, context)

        if workers <= 1:
            results = [work(b) for b in batches]
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                results = list(pool.map(work, batches))

        failed: list[tuple[int, str]] = []
        for batch, res in results:
            if res is None:
                failed.extend(batch)
                continue
            for (i, _t), v in zip(batch, res):
                out[i] = v
            with lock:
                stats["batches"] += 1
                stats["chars_out"] += sum(len(v) for v in res)

        # 批量失败的条目按「先粗后细」重试，最后才逐条。
        # 原来直接逐条：一个 51 行的批量失败就退化成 51 次单独请求，又慢又贵
        # （线上真实案例：术语表那两页各触发一次）。
        pending = list(failed)
        for group_size in FALLBACK_GROUP_SIZES:
            if not pending:
                break
            still: list[tuple[int, str]] = []
            for start in range(0, len(pending), group_size):
                group = pending[start:start + group_size]
                res = _one_batch([t for _, t in group], context)
                if res is None:
                    still.extend(group)
                    continue
                for (i, _t), v in zip(group, res):
                    out[i] = v
                with lock:
                    stats["batches"] += 1
                    stats["chars_out"] += sum(len(v) for v in res)
            pending = still

        for i, text in pending:
            with lock:
                stats["failed"] += 1
            say(f"[warn] 该条未能翻译，保留原文：{text[:40]!r}")
        return out

    translate.stats = stats          # type: ignore[attr-defined]
    return translate


def make_refiner(draft_translate, refine_cfg: dict | None,
                 target_lang: str = "zh-Hans", kind: str = "line",
                 log=None) -> callable:
    """把初译回调包一层「精修」，返回同样签名的 ``translate(texts, context=None)``。

    精修是**逐批就地**做的：先拿初译，再把这批的「原文 + 初译」交给精修模型重写，
    返回精修后的译文；管线拿到的仍是等长同序的列表，所以所有模式（原位/双语/OCR/Word）
    都不用改一行代码就吃得到精修。

    ★ 设计上最重要的一条：**精修失败绝不能拖垮这份文件**。
      没有配精修通道、网络失败、模型回的不是合法 JSON、批量对不上号……任何一种情况
      都只把那一批退回初译，函数照常返回结果，任务状态仍是成功。失败只体现在
      ``stats["refine"]``（ok / partial / failed）与 ``stats["refine_note"]`` 上，
      由上层在任务备注里写「精修未完成，已使用初译」。

    为什么把初译和精修的统计记在**同一份 dict** 里（而不是各自一份）：
    ``translate_cli.py`` / ``jobs.py`` 都是读 ``tr.stats`` 汇总用量，
    两份 dict 会让初译的 token 统计在包装后读不到（看起来像"没花钱"）。
    """
    stats = getattr(draft_translate, "stats", None)
    if stats is None:
        stats = {}
    target = _LANG_PROMPT_NAME.get(target_lang, target_lang)
    stats.setdefault("refine_batches", 0)
    stats.setdefault("refine_failed_batches", 0)
    stats.setdefault("refine_calls", 0)
    stats.setdefault("refine_tokens_in", 0)
    stats.setdefault("refine_tokens_out", 0)
    # 精修是并发的（有线程池），统计要加锁，不然 token 数会丢
    refine_lock = threading.Lock()

    def say(msg: str) -> None:
        if log:
            try:
                log(msg)
            except Exception:                        # noqa: BLE001
                pass

    if not refine_cfg:
        stats["refine"] = "failed"
        stats["refine_note"] = "通道没有配置精修模型（refineModel / refineApiKey），已使用初译"
    else:
        stats["refine"] = "ok"          # 没有可精修的条目时就是 ok（配置是好的）
        stats["refine_note"] = ""

    def _chunks(pairs: list[tuple[int, str, str]]):
        chunk: list[tuple[int, str, str]] = []
        chars = 0
        for pair in pairs:
            ln = len(pair[1]) + len(pair[2])
            if chunk and (len(chunk) >= MAX_CHUNK_LINES or chars + ln > MAX_CHUNK_CHARS):
                yield chunk
                chunk, chars = [], 0
            chunk.append(pair)
            chars += ln
        if chunk:
            yield chunk

    def _refine_one(batch: list[tuple[int, str, str]],
                    ctx_block: str = "", terms: list | None = None) -> list[str] | None:
        """精修一批；成功返回译文列表，彻底失败返回 None（调用方保留初译）。"""
        pairs = [(s, d) for _i, s, d in batch]
        sources = [s for _i, s, _d in batch]
        for attempt in range(2):
            prompt = _build_refine_prompt(pairs, target, kind, ctx_block)
            # 第一次要 JSON（好解析），失败再退回普通模式重试一次
            sink: dict = {}
            try:
                reply = call_chat(refine_cfg, REFINE_SYSTEM_LINE, prompt,
                                  json_mode=(attempt == 0), usage_sink=sink)
            except TranslateError as exc:
                say(f"[warn] 精修请求失败（{exc}）")
                continue
            with refine_lock:
                stats["refine_calls"] += 1
                stats["refine_tokens_in"] += int(sink.get("tokens_in") or 0)
                stats["refine_tokens_out"] += int(sink.get("tokens_out") or 0)
            parsed = _parse_json_reply(reply, len(pairs)) if reply else None
            if parsed is None:
                say("[warn] 精修回复不是合法 JSON 或条目对不上号")
                continue
            # 用**原文**当基准校验（数学符号不能丢、目标语言比例要对），
            # 否则精修可以悄悄把公式删掉而没人发现
            reason = _validate_reason(parsed, sources, target_lang, kind)
            if not reason:
                # 精修最容易犯的两个错：顺手把术语改成同义词、把数字改写掉。
                # 这里同样过一遍软校验，也只多给一次机会就收手（第二次照收）。
                soft = (_soft_reason(parsed, sources, target_lang, kind, terms)
                        if attempt == 0 else "")
                if not soft:
                    return parsed
                say(f"[info] 精修软校验提示（{soft}），再打磨一次")
                continue
            say(f"[warn] 精修结果校验未通过（{reason}）")
        return None

    def translate(texts, context=None) -> list[str]:
        drafts = draft_translate(texts, context)
        if not refine_cfg:
            return drafts
        try:
            src = ["" if t is None else str(t) for t in texts]
            out = list(drafts)
            # 只精修「真的被翻译过」的条目：原文本身就是中文/纯符号的没得打磨，
            # 初译失败的条目（draft == 原文）也跳过，免得把原文交给精修模型去意译
            todo = [(i, s, d) for i, (s, d) in enumerate(zip(src, drafts))
                    if needs_translation(s, target_lang) and d and d != s]
            if not todo:
                return out
            batches = list(_chunks(todo))
            workers = min(CONCURRENCY, len(batches))
            say(f"  精修 {len(todo)} 条（{len(batches)} 批）")

            def work(batch):
                srcs = [s for _i, s, _d in batch]
                hits = _glossary_hits(context, srcs)
                return batch, _refine_one(batch, _context_block(context, srcs, hits), hits)

            if workers <= 1:
                results = [work(b) for b in batches]
            else:
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    results = list(pool.map(work, batches))

            ok_batches = 0
            for batch, res in results:
                if res is None:
                    # 这一批退回初译 —— 不抛错、不中断，其余批次继续
                    with refine_lock:
                        stats["refine_failed_batches"] += 1
                    continue
                for (i, _s, _d), v in zip(batch, res):
                    out[i] = v
                with refine_lock:
                    stats["refine_batches"] += 1
                ok_batches += 1
            if stats["refine_failed_batches"]:
                total = ok_batches + stats["refine_failed_batches"]
                stats["refine"] = "failed" if ok_batches == 0 else "partial"
                stats["refine_note"] = (
                    "精修未完成，已使用初译" if ok_batches == 0
                    else f"精修部分未完成（{stats['refine_failed_batches']}/{total} 批），"
                         f"这些内容已使用初译")
                say(f"[warn] {stats['refine_note']}")
            return out
        except Exception as exc:                     # noqa: BLE001
            # 兜底：精修这一段无论出什么意外（含线程池、内存、第三方库的怪异常），
            # 都必须让初译结果照常交付
            stats["refine"] = "failed"
            stats["refine_note"] = "精修未完成，已使用初译"
            say(f"[warn] 精修异常，已使用初译：{exc}")
            return drafts

    translate.stats = stats          # type: ignore[attr-defined]
    return translate


def test_connection(cfg: dict, target_lang: str = "zh-Hans") -> dict:
    """连通性自检：真发一次最小请求，返回耗时与样例译文。"""
    tr = make_translator(cfg, target_lang=target_lang, kind="line")
    started = time.time()
    try:
        out = tr(["Artificial intelligence is changing the world."])
    except TranslateError as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "ms": int((time.time() - started) * 1000),
        "sample": out[0] if out else "",
        "model": cfg.get("chatModel", ""),
    }
