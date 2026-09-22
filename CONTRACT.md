# docbridge 管线接口契约（v1 · 冻结）

> 三个处理模块（`pdf_inplace` / `pdf_bilingual` / `docx_pipeline`）并行开发，
> 一律遵守本契约，互不依赖彼此的内部实现。

## 1. 目录结构

    docbridge/
      app.py                  Flask 应用（网页 + API + SSE 进度）
      jobs.py                 任务队列、进度、产物落盘
      translator.py           profiles.json 预设代号 → LLM 批量翻译
      pipelines/
        __init__.py           注册表 PIPELINES（app.py 只认这张表）
        pdf_inplace.py        PDF · 原位版式保留（复用上层 pdf_translate.py）
        pdf_bilingual.py      PDF · 双语对照（原文页 + 译文并排）
        docx_pipeline.py      Word · 原位替换 / 双语对照
      public/                 前端静态资源（index.html / styles.css / app.js）
      tests/                  离线单元测试（禁止联网）
      deploy/                 Caddy 路由 + systemd + 部署手册
      requirements.txt
      README.md

## 2. 每个模块必须导出的东西

```python
FORMAT   = "pdf"            # "pdf" | "docx"
MODE     = "inplace"        # "inplace"（原位版式保留）| "bilingual"（双语对照）
LABEL    = "原位版式保留"     # 前端显示用的中文名
NOTE     = "一句话说明"       # 前端显示给用户的注意事项
OPTIONS  = ["sim_bold", "font_scale"]   # 本模块认的 options 键，前端据此渲染控件

def run(src_path, out_path, *, translate, progress=None, options=None) -> dict:
    ...
```

`pipelines/__init__.py` 里注册：

```python
from . import pdf_inplace, pdf_bilingual, docx_pipeline
PIPELINES = {
    ("pdf",  "inplace"):   pdf_inplace,
    ("pdf",  "bilingual"): pdf_bilingual,
    ("docx", "inplace"):   docx_pipeline,   # 由 docx_pipeline 内部按 options/mode 区分
    ("docx", "bilingual"): docx_pipeline,
}
```

`docx_pipeline.run` 用 `options["mode"]` 或 `options["bilingual"]` 区分两种模式——
**若由你实现，请用 `options.get("__mode__")` 读取**（app.py 会注入 `__mode__`，
值为 `"inplace"` 或 `"bilingual"`），并让 `FORMAT = "docx"`、`MODE = "both"`。

## 3. run() 的契约（最重要的部分）

**入参**

| 参数 | 说明 |
|---|---|
| `src_path` | 绝对路径，只读输入文件 |
| `out_path` | 绝对路径，要写出的结果文件；父目录已存在 |
| `translate(texts: list[str], context: dict \| None = None) -> list[str]` | 批量翻译回调（见第 4 节） |
| `progress(done: int, total: int, note: str = "") -> None` | 可选，进度上报；请保证单调递增、最终 `done == total` |
| `options: dict` | 见第 5 节；一律用 `.get(key, default)` 取值，缺失要有合理默认 |

**返回值**：dict，用于统计与前端展示，至少包含

```python
{
  "units": 12,        # 实际翻译的段落/行数
  "chars_in": 3456,   # 送入翻译的原始字符数
  "chars_out": 3210,  # 译文字符数
  "skipped": 3,       # 判定为无需翻译而跳过的单元数
  "pages": 4,         # PDF 页数；docx 可省或填段落数
  "detail": "人类可读的一句话总结",
}
```

**硬性要求**

1. **1:1 顺序**：`translate()` 返回的列表必须与传入等长且顺序一致。
   若长度不符，**立刻 `raise RuntimeError("翻译返回数量不符：期望 N，实际 M")`**，绝不静默错位或截断。
2. **原子落盘**：先写 `out_path + ".part"`，成功后 `os.replace(part, out_path)`。
   中途失败绝不能留下半成品顶掉同名文件。
3. **失败要说话**：抛 `RuntimeError("人类可读的中文原因")`。不要吞异常返回空 dict。
4. **不联网、不读密钥**：pipeline 只通过 `translate` 回调与外界交互，不得自己发 HTTP 请求、
   不得 import requests/openai、不得读 profiles.json。
5. **不改源文件**：`src_path` 只读。
6. **跳过已中文/纯数字单元**：判定为无需翻译的单元不送翻译，计入 `skipped`，原文保持不动。
7. **进度必须真报**：按单元推进调用 `progress`，让前端进度条真的在动。
8. **单文件零共享状态**：不要写全局可变状态 / 模块级缓存（Flask 会多线程调用），
   所有状态放函数局部或传入的 options。
9. **让取消异常冒泡**（v1.1 新增）：用户点「取消」时，`progress()` 回调会抛出
   `jobs.JobCancelled`——它继承 `BaseException`，**必须让它穿过去**：

   ```python
   try:
       progress(done, total, note)      # 可能抛 JobCancelled
   except Exception:                    # ✅ 安全：JobCancelled 不是 Exception 子类
       pass                             #    只为「进度上报失败不连累任务」
   # ❌ 绝对不要用 `except BaseException:` 或裸 `except:` —— 那会把取消吞掉，
   #    任务将无法被取消（前端表现为「点了取消没反应」）
   ```

   取消是**合作式**的：只会在你调用 `progress()` 的那个点生效，
   正在飞的 `translate()` 请求不会被打断。所以 `progress()` 要按页/按批**勤调**，
   否则大文件会长时间无法取消。同理，`translate()` 自己抛的异常也应冒泡（app 层记为任务失败）。

## 4. translate() 回调的用法

```python
texts = ["Introduction", "This chapter covers ...", "Figure 1.1"]
out = translate(texts, {"kind": "pdf", "page": 3, "style": "Heading"})
# out == ["引言", "本章介绍……", "图 1.1"]，等长、同序
```

- **批量化**：请把同一页/同一段落的单元攒成一批调用（一次几十~上百条），
  这是成本和速度的关键；不要一行一次调用。
- 但单批别超过 ~200 条，避免超长请求与 JSON 截断。
- `context` 只是提示（页码/样式），可传 `None`，也可传额外键（如 `{"style": "Heading"}`）
  帮助翻译更准；**不要依赖它返回任何东西**。
- 回调**可能抛异常**（网络/额度/解析失败）。pipeline 应让异常向上冒泡，
  由 app.py 统一记为该任务失败——不要自行重试整批（app 层有重试与兜底）。

## 5. options 通用键

| 键 | 取值 | 说明 |
|---|---|---|
| `source_lang` | `"auto"`（默认）/ `"en"` / … | 源语言提示 |
| `target_lang` | `"zh-Hans"`（默认） | 目标语言 |
| `__mode__` | `"inplace"` / `"bilingual"` | 由 app.py 注入，供 docx 区分模式 |
| `sim_bold` | bool，默认 False | 原粗体行用描边模拟粗体中文（PDF 原位沿用） |
| `font_scale` | float，默认 0.92 | 中文字号相对英文原字号的视觉比例 |
| `font` | `"auto"` 默认 | `auto` / `china-s` / 字体文件路径 |
| `bilingual_layout` | `"side"`（默认）/ `"stack"` | 双语 PDF：左右并排 / 上下堆叠 |
| `keep_original_style` | bool，默认 True | docx 原位：尽量保留原段落样式 |

未列出的键请忽略，不要因为出现未知键而报错。

## 6. 测试要求（离线，禁止联网）

每个模块自带 `tests/test_<模块名>.py`，**用假翻译函数，绝不调用真实 API**：

```python
def fake(texts, ctx=None):
    assert isinstance(texts, list)
    return [f"【译{i}】{t}" for i, t in enumerate(texts)]
```

必须覆盖的断言：

- 输出文件存在、能被对应库重新打开（`fitz.open` / `Document`）；
- 原文确实被替换（原位）或确实被追加（双语）：读回来检查特征串 `【译0】` 之类；
- PDF 原位：**图片数量与放置矩形不变**（这是上层 `pdf_translate.py` 的核心保证，
  包装时务必透传并验证）；页数不变；
- docx：段落数/段落样式符合预期，表格与页眉页脚（若支持）也处理到；
- `translate` 返回数量不符时**必须抛错**（专门写一个返回少一条的假翻译函数来断言这点）；
- 进度回调被调用过，且最后一次 `done == total`。

运行方式（自己保证能跑）：

    cd <仓库目录>/docbridge
    python3 -m pytest tests/test_pdf_inplace.py -q     # 或 python3 tests/test_pdf_inplace.py

> 测试样例请**自己用 PyMuPDF / python-docx 现场生成**（写进 `tests/_tmp/`），
> 不要依赖工作目录里的教材 PDF（24MB，太慢）。

## 7. 环境与路径注意

- Python 3.10，已装：`pymupdf 1.28.0`、`python-docx 1.2.0`、`flask 3.1.3`、`lxml`。
- 复用现有 CLI 逻辑在：`<仓库目录>//pdf_translate.py`
  （注意：**工作目录名末尾有一个空格**，所有命令与路径都要加引号）。
- 运行时工作目录就是 `<仓库目录>//docbridge`。
- 不要修改 `pdf_translate.py`（它是既有生产代码）；要包装/导入它，不要改动它。
  如果确实必须小改，先在最终报告里明确指出改了什么、为什么。
