# Aurora Translate · 文档翻译

把 **PDF / Word 整本书**翻成中文的免费工具。跑在 GitHub 上，**不需要服务器**。

> 为什么不用 Google 翻译 / DeepL？因为它们翻译 PDF 通常会**重排版面**——图、公式、表格全乱。
> 这个工具把译文**原位画回原文的位置**，版式和图片一个像素都不动。

| 能力 | Aurora Translate | 常见在线翻译 |
|---|---|---|
| **原位版式保留**（图片、公式、表格位置不变） | ✅ | ❌ 只给文本或重排 |
| **双语对照**（原文 / 译文并列） | ✅ | ❌ |
| **扫描件 OCR 翻译**（整页是图片的老书） | ✅ | ❌ 或需付费 |
| **数学符号保留**（`√ ∑ ∫ ∂ ∆`） | ✅ | 经常丢失 |
| **专业术语统一**（内置经管术语库，可自定义） | ✅ 300+ 条 | ❌ |
| **成本** | 700 页 ≈ ¥1.4（自己的 API 密钥） | 按页收费或功能受限 |

## 怎么用（不用会 git）

1. 点 **[新建 Issue](../../issues/new?template=translate.yml)**
2. **把 PDF / Word 拖进「文件」框**（自动上传），选好「输出形式」和「目标语言」
3. 提交 → 等 1-5 分钟 → 机器人在 Issue 下回复**下载链接**

| 输出形式 | 效果 |
|---|---|
| **原位版式保留** | 译文覆盖原文，图/公式/表格位置完全不动（教材通读首选） |
| **双语对照** | 原文与译文对照（精读、校对） |
| **扫描版 OCR 翻译** | 整页是图片的扫描件，先 OCR 识别再翻译 |

限制：**单个文件 ≤ 25MB**（GitHub 附件上限）；译文链接保留 7 天。

**想先试试？** 用仓库里的样例文件 [`samples/demo.pdf`](samples/demo.pdf)（自带数学符号 `√ ∂ ∆ ⊂` 与一个图形），
把它拖进文件框就能看到完整效果。

## 给仓库所有者：配置密钥（一次性）

翻译用的是你自己的 API 密钥，**存在仓库 Secret 里，使用者看不到**：

1. 本地准备好 `profiles.json`（格式见下）
2. 仓库 **Settings → Secrets and variables → Actions → New repository secret**
   - Name: `PROFILES_JSON`
   - Secret: 把 `profiles.json` 的**全文**粘进去
3. （可选）**Variables** 里加 `DEFAULT_PROFILE` 指定默认通道代号

`profiles.json` 格式（一个代号对应一个通道）：

```json
{
  "ds4": {
    "name": "DeepSeek V4-Flash",
    "baseUrl": "https://api.siliconflow.cn/v1",
    "apiKey": "sk-你的密钥",
    "chatModel": "deepseek-ai/DeepSeek-V4-Flash",
    "thinking": false
  },
  "backup": {
    "name": "备用通道",
    "baseUrl": "https://api.deepseek.com/v1",
    "apiKey": "sk-另一个密钥",
    "chatModel": "deepseek-chat"
  }
}
```

几个字段的讲究：

- **`thinking: false`**：推理模型（如 DeepSeek-V4）回答前会先输出一大段思考，而**思考按输出计费**。
  批量翻译不需要思考，关掉它实测快 6 倍、token 少 85%。只对支持该参数的端点生效。
- **`api: "responses"`**：有些 GPT 中转站只支持 `/responses` 接口，不支持 `/chat/completions`。
  加这个字段即可切换（默认走 `/chat/completions`）。
- 多个代号时，第一个可用的是默认通道，其余自动作为**失败兜底**与**难内容攻坚**（见下）。

## 谁能用我的额度

公开仓库任何人都能开 Issue，所以工作流**先校验发起者身份**：
只有 **所有者 / 组织成员 / 协作者（Collaborator）** 能触发，其他人提交会被礼貌拒绝。

想让谁用，就在 **Settings → Collaborators** 里邀请他（GitHub 免费账号即可）。
如果你想给完全不认识的人用，请让他们 **fork 这个仓库、配自己的密钥**（见下）。

## 给想自己搭一份的人（Fork）

1. **Fork** 这个仓库
2. 按上面「配置密钥」把自己的 `PROFILES_JSON` 填进你 fork 的 Secrets
3. Settings → Actions → General → 允许运行工作流
4. 自己的额度自己花，与原作者无关

想跑在自己的服务器上（不上 GitHub）：见 [`docs/自建服务器部署.md`](docs/自建服务器部署.md)。

## 它是怎么工作的

```
你在 Issue 里拖入文件
   ↓  GitHub Actions 触发（工作流 .github/workflows/translate.yml）
校验身份 → 下载附件 → 装 tesseract + 中文字体 → 跑翻译管线
   ↓
   ├─ 批量预翻译：跨页攒批 + 并发（比逐页请求快 10 倍、便宜 15 倍）
   ├─ 术语表注入：只注入本批真正出现的词（词边界匹配，避免 confirm 命中 firm）
   ├─ 数学符号三层保障：提示词强保留 → 校验兜底 → DejaVu 回退字体补缺字
   └─ 渲染：原位绘制中文，图片零改动（有硬校验，数量不一致直接判失败）
   ↓
推送到 results 分支 + 上传 artifact + 在 Issue 下回复下载链接
```

核心代码：

| 目录 | 作用 |
|---|---|
| `pipelines/` | 四种模式：PDF 原位 / PDF 双语 / 扫描件 OCR / Word（原位与双语） |
| `translator.py` | 批量翻译：多通道、失败兜底、难内容攻坚、限流熔断、译文校验 |
| `pdf_translate.py` | PDF 逐行文本提取与原位重绘的底层实现 |
| `jobs.py` · `app.py` | 自建服务器模式的队列与网页（GitHub 模式不经过它们） |
| `glossary.json` | 经管术语库（微观/宏观/金融会计/管理营销/统计计量，300+ 条） |
| `tools/translate_cli.py` | 命令行入口（GitHub Actions 调用的就是它） |
| `tests/` | 6 个离线测试套件、55+ 用例，不联网、不花钱 |

## 本地跑

```bash
pip install -r requirements.txt
python3 tools/translate_cli.py --input 你的书.pdf --output 你的书_zh.pdf --mode inplace
```

自检（全部离线，用假翻译函数，不消耗额度）：

```bash
for f in tests/test_*.py; do python3 "$f"; done
```

## 已知限制（诚实说）

- **扫描件**的识别率取决于 tesseract 与扫描质量；复杂版面（窄栏距、表格格、脚注公式）的行/段聚合会有误差。
- **旋转页**（`/Rotate` 90°/270°）在 OCR 模式会跳过并在结果里说明，不会假装成功。
- **Word** 里含域代码（目录、页码）或超链接的段落会整段跳过，以免破坏版面。
- 语言对以**中英为主**；其它语言能翻，但没针对调优。
- GitHub 的附件上限是 **25MB**，更大的书请拆分。
- 结果链接保留 **7 天**，之后由定时任务清理。

## 许可

MIT（见 [LICENSE](LICENSE)）。翻译质量与用量由使用者自己的 API 密钥决定，与本项目无关。
