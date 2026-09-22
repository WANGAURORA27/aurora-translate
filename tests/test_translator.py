"""docbridge · 翻译服务层离线测试（绝不联网）

用假 `call_chat` 替换真实接口，验证：
批量与顺序、校验失败重试、批量失败逐条兜底、彻底失败保留原文、
长度严格一致、字符统计、通道解析报错、真实端点上常见的坏回复形态。

跑法：
    cd <仓库目录>/docbridge
    python3 tests/test_translator.py          # 或 python3 -m pytest tests/test_translator.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import translator as T          # noqa: E402

CFG = {"provider": "openai", "baseUrl": "https://example.invalid/v1",
       "apiKey": "sk-test", "chatModel": "test-model"}
FAILED = []


def check(name, cond, extra=""):
    if cond:
        print(f"PASS {name}")
    else:
        print(f"FAIL {name} {extra}")
        FAILED.append(name)


def with_fake_call(fn):
    """临时替换 T.call_chat，返回 (结果, 调用记录)。"""
    calls = []
    orig = T.call_chat

    def wrapper(cfg, system, user, json_mode=True, **kw):
        calls.append({"user": user, "json_mode": json_mode})
        return fn(len(calls), user)

    T.call_chat = wrapper
    return calls, orig


def run_with_fake(fn, texts, **kw):
    calls, orig = with_fake_call(fn)
    try:
        tr = T.make_translator(CFG, **kw)
        return tr(texts), calls, tr.stats
    finally:
        T.call_chat = orig


# ---------------------------------------------------------------- 基础行为

def test_happy_path_is_one_to_one_in_order():
    def fake(n, user):
        # 数出请求里有多少条，逐条造译文
        nums = [ln.split(". ", 1)[1] for ln in user.splitlines()
                if ln[:1].isdigit() and ". " in ln]
        return "{" + ", ".join(f'"{i+1}": "译{i}"' for i in range(len(nums))) + "}"

    out, calls, stats = run_with_fake(fake, ["Hello", "World", "Again"])
    check("happy_path_order", out == ["译0", "译1", "译2"], out)
    check("happy_path_one_call", len(calls) == 1, len(calls))
    check("happy_path_stats", stats["chars_in"] == len("Hello") + len("World") + len("Again"), stats)


def test_single_batch_never_mixes_languages():
    """已中文/纯数字的条目根本不送内容，也会原样保留在同一位置。"""
    src = ["Machine learning is a field", "这是中文", "12345", "Budget line"]
    def fake(n, user):
        nums = [ln for ln in user.splitlines() if ln[:1].isdigit() and ". " in ln]
        return "{" + ", ".join(f'"{i+1}": "译文{i}"' for i in range(len(nums))) + "}"

    out, calls, stats = run_with_fake(fake, src)
    check("skip_keeps_position", out[1] == "这是中文" and out[2] == "12345", out)
    check("skip_not_sent", "这是中文" not in calls[0]["user"] and "12345" not in calls[0]["user"])
    check("skip_counted", stats["skipped"] == 2, stats)


def test_validation_retries_then_succeeds():
    """第一次故意把两条合并成同一条译文，校验必须拦下并重试。"""
    def fake(n, user):
        if n == 1:
            return '{"1": "同样的译文", "2": "同样的译文"}'
        return '{"1": "第一句", "2": "第二句"}'

    out, calls, stats = run_with_fake(fake, ["First line here", "Second line here"])
    check("retry_on_merge", out == ["第一句", "第二句"], out)
    check("retry_happened", len(calls) == 2 and stats["retries"] == 1, (len(calls), stats["retries"]))


def test_traditional_chinese_is_rejected():
    def fake(n, user):
        if n == 1:
            return '{"1": "這裡是繁體"}'
        return '{"1": "这里是简体"}'

    out, calls, _ = run_with_fake(fake, ["Here is the text"])
    check("reject_traditional", out == ["这里是简体"], out)


def test_ellipsis_and_meta_are_rejected():
    def meta(n, user):
        if n == 1:
            return '{"1": "原文未提供"}'
        return '{"1": "预算线描述了可购买的商品组合"}'

    out, _, _ = run_with_fake(meta, ["A budget line describes purchasable bundles"])
    check("reject_meta_comment", out == ["预算线描述了可购买的商品组合"], out)

    def ell(n, user):
        if n == 1:
            return '{"1": "预算线描述了……"}'
        return '{"1": "预算线描述了可购买的商品组合"}'

    out, _, _ = run_with_fake(ell, ["A budget line describes purchasable bundles"])
    check("reject_ellipsis", out == ["预算线描述了可购买的商品组合"], out)


def test_missing_key_triggers_retry_and_per_item_fallback():
    """批量始终坏 → 逐条兜底；某些条仍坏 → 保留原文并计入 failed。"""
    def fake(n, user):
        nums = [ln for ln in user.splitlines() if ln[:1].isdigit() and ". " in ln]
        if len(nums) > 1:
            return '{"1": "只有一条"}'         # 批量永远缺键
        text = nums[0].split(". ", 1)[1]
        if "BAD" in text:
            return "这不是 JSON"                # 该条永远失败
        return '{"1": "单条译好"}'

    out, calls, stats = run_with_fake(fake, ["GOOD one", "BAD two"])
    check("fallback_good_translated", out[0] == "单条译好", out)
    check("fallback_bad_keeps_original", out[1] == "BAD two", out)
    check("fallback_failed_counted", stats["failed"] == 1, stats)
    check("fallback_length_equal", len(out) == 2, len(out))


def test_total_failure_still_returns_same_length():
    def fake(n, user):
        raise T.TranslateError("模拟网络故障")

    out, calls, stats = run_with_fake(fake, ["Alpha", "Beta", "Gamma"])
    check("total_failure_same_length", out == ["Alpha", "Beta", "Gamma"], out)
    check("total_failure_counted", stats["failed"] == 3, stats)


def test_json_mode_falls_back_when_unsupported():
    """端点不支持 response_format=json_object 时应自动退回普通模式再试。"""
    seen = {"json": 0, "plain": 0}
    def fake(n, user):
        return '{"1": "译好了"}'

    calls, orig = with_fake_call(fake)
    def wrapper(cfg, system, user, json_mode=True, **kw):
        if json_mode:
            seen["json"] += 1
            raise T.TranslateError("HTTP 400 response_format unsupported")
        seen["plain"] += 1
        calls.append({"user": user})
        return '{"1": "译好了"}'
    T.call_chat = wrapper
    try:
        tr = T.make_translator(CFG)
        out = tr(["Something to translate"])
    finally:
        T.call_chat = orig
    check("json_mode_fallback", out == ["译好了"] and seen["plain"] == 1, (out, seen))


def test_batching_respects_chunk_limits():
    """长文档必须分批，不能一口气塞进一个请求（否则必然超出上下文/被截断）。

    假翻译**回显原文**（加前缀），这样跨批之后仍能逐条验证顺序没被打乱——
    批次内局部编号会从 1 重新开始，不能用来判断全局顺序。
    """
    def fake(n, user):
        items = [ln.split(". ", 1)[1] for ln in user.splitlines()
                 if ln[:1].isdigit() and ". " in ln]
        return "{" + ", ".join(f'"{i+1}": "译[{t}]"' for i, t in enumerate(items)) + "}"

    texts = [f"Paragraph number {i} with some English content to translate." for i in range(150)]
    out, calls, _ = run_with_fake(fake, texts)
    check("batching_multiple_calls", len(calls) > 1, len(calls))
    check("batching_no_oversize", all(len(c["user"]) < T.MAX_CHUNK_CHARS + 4000 for c in calls))
    check("batching_order_kept", out == [f"译[{t}]" for t in texts], out[:3])


def test_context_argument_is_accepted_and_ignored():
    def fake(n, user):
        return '{"1": "译好了"}'

    calls, orig = with_fake_call(fake)
    try:
        tr = T.make_translator(CFG)
        out = tr(["Hello"], {"kind": "pdf", "page": 3})
        out2 = tr(["Hello"], None)
    finally:
        T.call_chat = orig
    check("context_tolerated", out == ["译好了"] and out2 == ["译好了"], (out, out2))


def test_no_newlines_in_values():
    """译文里的换行必须被压平，否则写回 docx/单行时会破坏排版。"""
    def fake(n, user):
        return '{"1": "第一行\\n第二行"}'

    out, _, _ = run_with_fake(fake, ["Line one and line two"])
    check("newline_flattened", "\n" not in out[0] and out[0] == "第一行 第二行", repr(out[0]))


# ---------------------------------------------------------------- 配置层

def test_resolve_config_errors_are_actionable():
    try:
        T.resolve_config(None, None)
        check("no_channel_raises", False)
    except T.TranslateError as exc:
        check("no_channel_raises", "预设代号" in str(exc), str(exc))

    try:
        T.resolve_config("__no_such_code__", None)
        check("bad_code_raises", False)
    except T.TranslateError as exc:
        check("bad_code_raises", "不存在" in str(exc), str(exc))

    cfg = T.resolve_config(None, {"baseUrl": "https://x.invalid/v1/", "apiKey": "k", "chatModel": "m"})
    check("override_wins", cfg["baseUrl"] == "https://x.invalid/v1" and cfg["chatModel"] == "m", cfg)


def test_public_profiles_never_expose_keys():
    profs = T.public_profiles()
    check("profiles_shape", isinstance(profs, list), type(profs))
    for p in profs:
        if set(p) - {"code", "name", "note", "model", "usable", "issue"}:
            check("profiles_no_secret_fields", False, p)
            return
        # 仍然不许出现任何密钥类字段
        if any("key" in k.lower() or "token" in k.lower() for k in p):
            check("profiles_no_secret_fields", False, p)
            return
    check("profiles_no_secret_fields", True)


def test_needs_translation_rules():
    cases = [
        ("这是中文", False), ("12345", False), ("", False), ("  ", False),
        ("Hello world", True), ("图3.1 shows", True), ("$$", False),
    ]
    ok = all(T.needs_translation(t) == want for t, want in cases)
    check("needs_translation_rules", ok,
          [(t, T.needs_translation(t)) for t, _ in cases])


def test_parse_reply_tolerates_code_fences_and_prose():
    fenced = '```json\n{"1": "你好"}\n```'
    check("parse_fenced", T._parse_json_reply(fenced, 1) == ["你好"])
    prose = 'Sure! Here you go: {"1": "你好", "2": "世界"} Hope it helps.'
    check("parse_prose", T._parse_json_reply(prose, 2) == ["你好", "世界"])
    check("parse_rejects_short", T._parse_json_reply('{"1": "只有一条"}', 2) is None)
    check("parse_rejects_empty_value", T._parse_json_reply('{"1": "  "}', 1) is None)


def test_responses_protocol_support():
    """Responses 协议（/responses）：很多 GPT 中转站只支持它，不支持 /chat/completions。

    这里用假的 _post_json 抓请求，验证：URL 打对了、请求体用了
    instructions/input、两种返回结构都能解析出正文。
    """
    captured = {}

    def fake_post(url, body, api_key, timeout, *args, **kw):
        captured["url"] = url
        captured["body"] = body
        return {"output_text": '{"1":"企业实现利润最大化。"}'}

    orig = T._post_json
    T._post_json = fake_post
    try:
        cfg = {"provider": "openai", "api": "responses",
               "baseUrl": "https://relay.invalid/v1", "apiKey": "sk-x",
               "chatModel": "gpt-5.5"}
        out = T.call_chat(cfg, "SYS", "USER", json_mode=True)
    finally:
        T._post_json = orig

    check("responses_url", captured.get("url") == "https://relay.invalid/v1/responses",
          captured.get("url"))
    body = captured.get("body") or {}
    check("responses_body_shape",
          body.get("instructions") == "SYS" and body.get("input") == "USER"
          and body.get("model") == "gpt-5.5", body)
    check("responses_json_format",
          (body.get("text") or {}).get("format", {}).get("type") == "json_object", body)
    check("responses_parsed", out == '{"1":"企业实现利润最大化。"}', out)

    # 另一种返回结构：正文散在 output[].content[].text 里
    nested = {"output": [{"content": [{"text": '{"1":'}, {"text": '"乙"}'}]}]}
    check("responses_nested_parse", T._extract_responses_text(nested) == '{"1":"乙"}')
    try:
        T._extract_responses_text({"id": "rs_1"})
        check("responses_bad_shape_raises", False)
    except T.TranslateError as exc:
        check("responses_bad_shape_raises", "结构异常" in str(exc), str(exc))


def test_api_protocol_selection_and_aliases():
    """协议名要能从预设代号里读出来，别名也都要归一。"""
    for declared, want in [("chat", "chat"), ("chat/completions", "chat"),
                           ("", "chat"), ("responses", "responses"),
                           ("response", "responses")]:
        cfg = T.resolve_config(None, {"baseUrl": "https://x.invalid/v1",
                                      "apiKey": "sk-abc", "api": declared})
        check(f"api_alias_{declared or 'empty'}", cfg["api"] == want, cfg.get("api"))
    try:
        T.resolve_config(None, {"baseUrl": "https://x.invalid/v1",
                                "apiKey": "sk-abc", "api": "grpc"})
        check("api_unknown_rejected", False)
    except T.TranslateError as exc:
        check("api_unknown_rejected", "不认识" in str(exc), str(exc))


def test_bad_api_key_is_rejected_before_any_request():
    """密钥含中文 / 是占位符，必须在发请求前就拦下来并说清楚。

    真实事故：profiles.json 里留着一句没替换的占位符 ``sk-你的OpenAI密钥``，
    用户选了那个代号，结果 urllib 在拼 Authorization 头时抛出
    ``'latin-1' codec can't encode characters in position 10-11``——
    完全看不出问题在哪。
    """
    try:
        T.resolve_config(None, {"baseUrl": "https://x.invalid/v1",
                                "apiKey": "sk-你的OpenAI密钥"})
        check("non_ascii_key_rejected", False)
    except T.TranslateError as exc:
        check("non_ascii_key_rejected",
              "非 ASCII" in str(exc) and "占位符" in str(exc), str(exc))

    issue = T.profile_issue({"baseUrl": "https://api.openai.com/v1",
                             "apiKey": "sk-你的OpenAI密钥"})
    check("profile_issue_placeholder", "非 ASCII" in issue, issue)
    check("profile_issue_empty_key",
          "没有配置 apiKey" in T.profile_issue({"baseUrl": "https://x/v1", "apiKey": ""}))
    check("profile_issue_bad_api",
          "不认识" in T.profile_issue({"baseUrl": "https://x/v1", "apiKey": "sk-abc",
                                       "api": "grpc"}))
    check("profile_issue_ok",
          T.profile_issue({"baseUrl": "https://x/v1", "apiKey": "sk-abc"}) == "")


def test_duplicate_rule_only_flags_consecutive_merges():
    """「不同原文得到相同译文」只有**相邻**出现才算合并。

    真实事故：术语表一页里 Capital 与 Capitals 都该译「资本」、Easy 与 Ease 都该译
    「容易」。原来的规则见重复就判失败，把完全正确的译文拒了 → 白重试 3 次 →
    再退化成 51 次单独请求；更糟的是重试提示里写着 "repeated"，会把正确译文带偏。
    """
    src = ["Capital", "Firm", "Capitals", "Profits", "Easy", "Revenue", "Ease"]
    good = ["资本", "企业", "资本", "利润", "容易", "收入", "容易"]
    check("distant_duplicates_ok",
          T._validate(good, src, "zh-Hans", "line") is True,
          T._validate_reason(good, src, "zh-Hans", "line"))

    # 相邻两条不同原文撞同一译文 = 真的合并了（原文够长）
    src2 = ["This chapter covers the fundamentals of microeconomics.",
            "We begin with the concept of supply and demand in markets."]
    bad = ["本章介绍微观经济学基础。", "本章介绍微观经济学基础。"]
    reason = T._validate_reason(bad, src2, "zh-Hans", "line")
    check("consecutive_merge_rejected", "疑似合并" in reason, reason)

    # 短词相邻撞译文不算（表格里很常见）
    check("short_consecutive_ok",
          T._validate(["合计", "合计"], ["Total", "Sum"], "zh-Hans", "line") is True)


def test_validation_reason_is_actionable():
    """校验原因要能直接看出问题：带位置与短样本。"""
    r = T._validate_reason(["x"], ["a", "b"], "zh-Hans", "line")
    check("reason_count", "数量不符" in r, r)
    r = T._validate_reason(["這裡是繁體"], ["Here is the text"], "zh-Hans", "line")
    check("reason_traditional", "繁体" in r and "第 1 条" in r, r)
    r = T._validate_reason(["翻译如下"], ["Translate this"], "zh-Hans", "line")
    check("reason_meta", "元注释" in r, r)
    check("reason_ok_empty", T._validate_reason(["你好"], ["Hello"], "zh-Hans", "line") == "")


def test_correction_hint_matches_reason():
    """纠正提示要针对具体原因，且不能把「重复」一律说成错误。"""
    hint = T._correction_hint("第 3 条含繁体字：'這裡'")
    check("hint_traditional", "SIMPLIFIED" in hint, hint)
    hint = T._correction_hint("相邻第 1/2 条译文相同，疑似合并")
    check("hint_merge_allows_synonyms",
          "same translation" in hint or "share the same" in hint, hint)


def test_fallback_ladder_does_not_explode_into_single_calls():
    """批量失败后的兜底必须「先粗后细」，不能直接退化成 N 次单条请求。"""
    calls = {"n": 0, "sizes": []}

    def fake_post(url, body, api_key, timeout, *args, **kw):
        # 从请求体里数出这一批有多少条
        user = body.get("input") or body["messages"][-1]["content"]
        lines = [l for l in user.splitlines() if l[:1].isdigit() and ". " in l]
        n = len(lines)
        calls["n"] += 1
        calls["sizes"].append(n)
        if n > 5:                      # 大批量一律返回坏结果（模拟模型合并多行）
            return {"output_text": "{" + ", ".join(f'"{i+1}": "同样的话"' for i in range(n)) + "}"}
        return {"output_text": "{" + ", ".join(f'"{i+1}": "译文{i}"' for i in range(n)) + "}"}

    orig = T._post_json
    T._post_json = fake_post
    try:
        cfg = {"provider": "openai", "api": "responses", "baseUrl": "https://x.invalid/v1",
               "apiKey": "sk-x", "chatModel": "m"}
        tr = T.make_translator(cfg, kind="line")
        texts = [f"Paragraph fragment number {i} of the document text." for i in range(30)]
        out = tr(texts)
    finally:
        T._post_json = orig

    check("ladder_all_translated", all(o.startswith("译文") for o in out), out[:4])
    check("ladder_bounded_calls", calls["n"] < 30, calls["n"])
    check("ladder_used_grouping", 20 in calls["sizes"] or 5 in calls["sizes"], calls["sizes"][:12])


def test_refine_api_must_not_leak_into_main_channel():
    """主通道的协议名**不能**从 refineApi 继承。

    真实事故：snow 代号主通道走 /chat/completions，但它带 refineApi="responses"
    （精修通道用的）。我当初把 refineApi 也放进兜底链，结果 snow 的主通道被打到
    /responses 上，硅基流动回 404，整条通道不可用。
    """
    fake = {"snowish": {"name": "带精修的代号", "baseUrl": "https://x.invalid/v1",
                        "apiKey": "sk-abc", "chatModel": "m",
                        "refineBaseUrl": "https://relay.invalid/v1",
                        "refineApiKey": "sk-refine", "refineApi": "responses"}}
    orig = T.load_profiles
    T.load_profiles = lambda *a, **k: fake
    try:
        cfg = T.resolve_config("snowish", None)
    finally:
        T.load_profiles = orig
    check("refine_api_not_inherited", cfg["api"] == "chat", cfg["api"])

    # 显式声明 api 时当然要听（两种写法都认）
    fake2 = {"r": {"baseUrl": "https://x.invalid/v1", "apiKey": "sk-abc", "api": "responses"}}
    T.load_profiles = lambda *a, **k: fake2
    try:
        cfg2 = T.resolve_config("r", None)
    finally:
        T.load_profiles = orig
    check("explicit_api_respected", cfg2["api"] == "responses", cfg2["api"])


def test_real_profiles_resolve_to_sane_endpoints():
    """线上真实 profiles.json 的每个代号都要解析出合理端点（回归护栏）。"""
    for prof in T.public_profiles():
        if not prof["usable"]:
            continue
        cfg = T.resolve_config(prof["code"], None)
        expected = "/responses" if cfg["api"] == "responses" else "/chat/completions"
        # 中转站只支持 /responses；硅基流动这类只支持 /chat/completions
        if "siliconflow" in cfg["baseUrl"]:
            check(f"endpoint_{prof['code']}", expected == "/chat/completions",
                  f"{prof['code']} 被打到了 {expected}")
        else:
            check(f"endpoint_{prof['code']}", True)


def test_fallback_channel_takes_over_when_primary_dies():
    """主通道彻底不可用时，备用通道要接手，任务不能整个失败。

    真实场景：中转站出现过瞬时 404，主通道一挂整份文档就白跑。
    """
    seen = {"primary": 0, "backup": 0}

    def make(role):
        def fake(cfg, system, user, json_mode=True, **kw):
            seen[role] += 1
            if role == "primary":
                raise T.TranslateError("模拟主通道不可用（HTTP 404）")
            n = len([l for l in user.splitlines() if l[:1].isdigit() and ". " in l])
            return "{" + ", ".join(f'"{i+1}": "备用译{i}"' for i in range(n)) + "}"
        return fake

    orig = T.call_chat
    primary = {"provider": "openai", "api": "chat", "baseUrl": "https://p.invalid/v1",
               "apiKey": "sk-p", "chatModel": "cheap"}
    backup = {"provider": "openai", "api": "chat", "baseUrl": "https://b.invalid/v1",
              "apiKey": "sk-b", "chatModel": "strong"}

    def dispatch(cfg, system, user, json_mode=True, **kw):
        return (make("primary") if cfg is primary else make("backup"))(
            cfg, system, user, json_mode, **kw)

    T.call_chat = dispatch
    try:
        tr = T.make_translator(primary, fallback=backup, route="fallback",
                               channel_names={id(primary): "主", id(backup): "备"})
        out = tr(["First long english sentence here", "Second long english sentence here"])
    finally:
        T.call_chat = orig

    check("fallback_translated", all(v.startswith("备用译") for v in out), out)
    check("fallback_primary_tried", seen["primary"] > 0, seen)
    check("fallback_backup_used", seen["backup"] > 0, seen)
    check("fallback_counted", tr.stats["fallbacks"] >= 1, tr.stats["fallbacks"])


def test_hybrid_routes_hard_batches_to_strong_channel():
    """混合路由：术语表/表格这类难批次走强通道，普通长句仍走便宜通道。"""
    used = []

    def dispatch(cfg, system, user, json_mode=True, **kw):
        used.append(cfg["chatModel"])
        n = len([l for l in user.splitlines() if l[:1].isdigit() and ". " in l])
        return "{" + ", ".join(f'"{i+1}": "译{i}"' for i in range(n)) + "}"

    orig = T.call_chat
    T.call_chat = dispatch
    cheap = {"provider": "openai", "api": "chat", "baseUrl": "https://c.invalid/v1",
             "apiKey": "sk-c", "chatModel": "cheap-model"}
    strong = {"provider": "openai", "api": "chat", "baseUrl": "https://s.invalid/v1",
              "apiKey": "sk-s", "chatModel": "strong-model"}
    try:
        tr = T.make_translator(cheap, escalate=strong, route="hybrid")

        def fake(texts, ctx=None):
            # 直接调内部：这里要看的是「按批次内容选通道」这一步
            return None
        hard = ["Capital", "Capitals", "Easy", "Ease", "Private", "Public"]
        easy = ["This chapter examines how consumers allocate their limited incomes "
                "among the many available goods and services.",
                "A budget line describes the combinations of goods that can be "
                "purchased given a fixed income."]
        tr(hard)
        tr(easy)
    finally:
        T.call_chat = orig

    check("hybrid_hard_uses_strong", "strong-model" in used, used)
    check("hybrid_easy_uses_cheap", used.count("cheap-model") >= 1, used)
    check("hybrid_escalated_counted", tr.stats["escalated"] >= 1, tr.stats["escalated"])

    # 判据本身也单独钉一下，避免以后被"顺手调宽"
    check("is_hard_on_terms", T.is_hard_batch(hard) is True)
    check("is_hard_on_prose", T.is_hard_batch(easy) is False)


def test_rate_limit_switches_channel_immediately_and_cools_down():
    """被限流的通道：当批就换道，连续几次后进入冷却，不再被捶。

    真实场景：中转站回报 429「上游当前已达到使用限制」，旧逻辑还会对同一个
    通道重试 3 次（每次还要 sleep），一批就白等十几秒、白花几次调用。
    """
    calls = {"cheap": 0, "strong": 0}

    def dispatch(cfg, system, user, json_mode=True, **kw):
        n = len([l for l in user.splitlines() if l[:1].isdigit() and ". " in l])
        role = "strong" if cfg is strong else "cheap"
        calls[role] += 1
        if role == "strong":
            raise T.TranslateError(
                '接口返回 HTTP 429：{"error":{"message":"上游当前已达到使用限制",'
                '"type":"rate_limit_error"}}')
        return "{" + ", ".join(f'"{i+1}": "便宜译{i}"' for i in range(n)) + "}"

    orig = T.call_chat
    T.call_chat = dispatch
    cheap = {"provider": "openai", "api": "chat", "baseUrl": "https://c.invalid/v1",
             "apiKey": "sk-c", "chatModel": "cheap-model"}
    strong = {"provider": "openai", "api": "chat", "baseUrl": "https://s.invalid/v1",
              "apiKey": "sk-s", "chatModel": "strong-model"}
    hard = ["Capital", "Capitals", "Easy", "Ease", "Private", "Public"]
    try:
        tr = T.make_translator(cheap, escalate=strong, route="hybrid",
                               channel_names={id(cheap): "便宜", id(strong): "强"})
        outs = [tr(hard) for _ in range(3)]
    finally:
        T.call_chat = orig

    check("rate_limit_all_translated",
          all(all(v.startswith("便宜译") for v in o) for o in outs), outs[0])
    # 关键：强通道只被打了 2 次（达到 RATE_LIMIT_STRIKES 就冷却），
    # 而不是每批都重试 3 次（那会是 6~9 次）
    check("rate_limit_no_hammering", calls["strong"] == T.RATE_LIMIT_STRIKES,
          calls)
    check("rate_limit_cooled_then_skipped", calls["cheap"] == 3, calls)


def test_thinking_can_be_disabled_per_profile():
    """预设里写 thinking: false 时，请求体要带上关闭思考的参数。

    为什么重要：推理模型的思考 token 按**输出**计费。实测 DeepSeek-V4-Flash 翻同一页
    术语表，带思考 18.5s / 2340 输出 token，关掉后 3.1s / 359 输出 token。
    但没写这个字段的预设必须**原样不发**该参数，否则别的端点可能报错。
    """
    captured = {}

    def fake_post(url, body, api_key, timeout, *a, **kw):
        captured["body"] = body
        return {"choices": [{"message": {"content": '{"1":"甲"}'}}]}

    orig = T._post_json
    T._post_json = fake_post
    try:
        on = {"provider": "openai", "api": "chat", "baseUrl": "https://x.invalid/v1",
              "apiKey": "sk-a", "chatModel": "m", "thinking": False}
        T.call_chat(on, "S", "U")
        check("thinking_disabled_sent",
              (captured["body"].get("thinking") or {}).get("type") == "disabled",
              captured["body"].get("thinking"))

        off = {"provider": "openai", "api": "chat", "baseUrl": "https://x.invalid/v1",
               "apiKey": "sk-a", "chatModel": "m", "thinking": None}
        T.call_chat(off, "S", "U")
        check("thinking_not_sent_by_default", "thinking" not in captured["body"],
              captured["body"].keys())
    finally:
        T._post_json = orig


def test_both_prompt_templates_carry_context_placeholder():
    """line / paragraph 两个提示词模板都必须带 {context} 占位符。

    真实事故：`BATCH_PARA_PROMPT`（Word 走的 paragraph 模式）漏了这个占位符，
    结果术语表和文档背景被 `str.format` **静默丢弃** —— 不报错、翻译照跑，
    只是术语表完全不生效，很难发现。
    """
    for kind in ("line", "paragraph"):
        cap = {}

        def spy(cfg, system, user, json_mode=True, **kw):
            cap["p"] = user
            n = len([l for l in user.splitlines() if l[:1].isdigit() and ". " in l])
            return "{" + ", ".join(f'"{i+1}": "译{i}"' for i in range(n)) + "}"

        orig = T.call_chat
        T.call_chat = spy
        try:
            tr = T.make_translator(
                {"provider": "openai", "api": "chat", "baseUrl": "https://x.invalid/v1",
                 "apiKey": "sk-a", "chatModel": "m"}, kind=kind,
                glossary={"firm": "厂商"})
            tr(["The firm maximises profit."], {"doc_context": "Micro lecture"})
        finally:
            T.call_chat = orig
        prompt = cap.get("p", "")
        check(f"context_block_in_{kind}_prompt", "firm → 厂商" in prompt, prompt[:160])
        check(f"doc_context_in_{kind}_prompt", "Micro lecture" in prompt, prompt[:160])


if __name__ == "__main__":
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        fn()
    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项：" + ", ".join(FAILED))
        sys.exit(1)
    print("全部通过 ✅")
