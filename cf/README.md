# 网页端（Cloudflare Worker）

GitHub Issue 那条路有个硬限制：**附件最大 25MB**，超过就只能拆书。这一层用 Cloudflare
补上三件事：

| 问题 | 这一层的做法 |
|---|---|
| 大文件传不动 | 原件与译文放 **R2**（对象存储），单个文件上限 95MB |
| 谁都能用会烧掉你的密钥 | **口令门**：没口令连上传接口都调不了 |
| 进度看不见、要跳去 GitHub 下载 | 页面上看进度、点一下直接下载译文 |

翻译本身仍然在 GitHub Actions 里跑（你的 API 密钥只存在仓库 Secret 里，不经过 Cloudflare）。
Worker 只负责"收件 → 派活 → 报进度 → 发件"。

## 一、要准备的凭据

### 1. Cloudflare API Token

面板右上角头像 → **My Profile** → **API Tokens** → **Create Token** → 用
**Edit Cloudflare Workers** 模板，然后确认这几项权限齐全：

| 范围 | 权限 | 用途 |
|---|---|---|
| Account · Workers Scripts | Edit | 部署 Worker |
| Account · Workers KV Storage | Edit | 建 KV（存任务进度） |
| Account · Workers R2 Storage | Edit | 建 R2 桶（存文件） |
| Account · Account Settings | Read | 读取 account id |

> 令牌**只在创建成功那一刻显示一次**。页面上有个 `Copy` 按钮，点它复制，
> 不要用鼠标框选 —— 少几个字符就会得到 `401 Invalid API Token`。

存到仓库的 `.secrets/cf_token`（这个目录已在 `.gitignore` 里，不会进 git）：

```bash
pbpaste > .secrets/cf_token        # macOS：把剪贴板内容写进文件
```

自检：

```bash
export CLOUDFLARE_API_TOKEN="$(tr -d ' \r\n' < .secrets/cf_token)"
wrangler whoami
```

> 输出里写着 **Account API Token** 是正常的（这种令牌挂在账户下）。
> 但别拿 `curl .../user/tokens/verify` 去验它 —— 那个接口只认**用户级**令牌，
> 账户级令牌去问一律回 `401 Invalid API Token`，会让人白白以为令牌坏了。
> 账户级的验证地址是 `/accounts/<account_id>/tokens/verify`。

### 2. 先开通 R2（一次性，必须）

大文件中转靠 R2，但它**必须先手动开通**，否则建桶时会回
`Please enable R2 through the Cloudflare Dashboard`。

打开 <https://dash.cloudflare.com> → 左侧 **R2** → 按提示开通。
免费额度是 **10GB 存储 / 月**，**出站流量不收费**；我们只放 7 天内的文件，
基本碰不到上限。开通需要绑一张卡，但免费额度内不会产生费用。

### 3. GitHub 令牌（给 Worker 用来触发 Actions）

Worker 需要调用 `workflow_dispatch` 派活。最省事的就是复用 `gh` 的登录态
（`gh auth token`），部署脚本会自动处理；想更规范就建一个**细粒度 PAT**
（Repository permissions → Actions: Read and write、Contents: Read），
放到 `.secrets/gh_pat`，脚本会优先用它。

### 4. 使用口令

`.secrets/password` 里写你想给使用者用的口令；文件不存在时脚本会自动生成一个并打印出来。

## 二、一条命令部署

```bash
bash cf/deploy.sh
```

脚本会依次：验证凭据 → 检查 R2 是否开通 → 建 KV → 建 R2 桶 → 写三个密钥
（PASSWORD / AGENT_KEY / GH_TOKEN）→ 部署 → 把网址和内部密钥同步给 GitHub
（`WORKER_URL` 变量、`AGENT_KEY` 密钥），最后打印网址与口令。重复跑是安全的，已存在的资源会跳过。

## 三、手动部署（脚本不好使时）

```bash
cd cf
export CLOUDFLARE_API_TOKEN="$(tr -d ' \r\n' < ../.secrets/cf_token)"

wrangler kv namespace create JOBS          # 把输出的 id 填进 wrangler.toml
wrangler r2 bucket create aurora-files
printf '%s' '你的口令'        | wrangler secret put PASSWORD
printf '%s' "$(openssl rand -hex 24)" | wrangler secret put AGENT_KEY   # 记下来
printf '%s' "$(gh auth token)"        | wrangler secret put GH_TOKEN

wrangler deploy                            # 输出里就是 *.workers.dev 地址
```

再到 GitHub 仓库 Settings → Secrets and variables → Actions：

* **Variables**：`WORKER_URL` = 上面那个地址
* **Secrets**：`AGENT_KEY` = 刚才那个内部密钥（必须与 Worker 上的完全一致）

## 四、接口一览

| 方法 | 路径 | 谁调 | 说明 |
|---|---|---|---|
| GET | `/` | 浏览器 | 页面本体（口令门 + 上传 + 进度 + 下载） |
| GET | `/api/verify` | 浏览器 | 校验口令 |
| PUT | `/api/upload` | 浏览器 | 文件流直传 R2 并派活（`x-password`/`x-filename`/`x-mode`/`x-target`） |
| GET | `/api/status?id=` | 浏览器 | 进度（KV 记录 + GitHub 运行状态） |
| GET | `/api/history` | 浏览器 | 最近 30 个任务 |
| GET | `/api/download?id=` | 浏览器 | 下载译文 |
| GET | `/api/input/<job>` | Actions | 取原件（`x-agent-key`） |
| POST | `/api/result/<job>` | Actions | 回传译文（`x-agent-key`） |
| POST | `/api/report/<job>` | Actions | 回传进度与统计（`x-agent-key`） |

数据清理：KV 记录 7 天过期；Worker 每天 04:23（UTC）扫一遍 R2，删掉超过 7 天的
原件和译文，和 `results` 分支的清理策略一致。

## 五、已知边界

* **单文件 95MB**：Cloudflare 免费版请求体上限 100MB，留了余量。
  译文回传也走同一个上限，所以**中英对照模式**（译文通常是原文的两倍大）建议原件控制在 40MB 以内。
* **口令是明文比较**：防的是"陌生人白用你的额度"，不是防定向攻击。口令别用你其他地方的密码。
* **同一时刻的任务数**：GitHub 免费账号并发有限，多人同时用会排队，页面会显示"排队中"。
* **第一次跑会慢**：Actions 要装 OCR 引擎和中文字体，前 1～2 分钟属于环境准备。
