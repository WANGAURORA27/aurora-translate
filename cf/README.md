# 网页端（Cloudflare Worker）

GitHub Issue 那条路有个硬限制：**附件最大 25MB**，超过就只能拆书。这一层用 Cloudflare
补上三件事：

| 问题 | 这一层的做法 |
|---|---|
| 大文件传不动 | 原件与译文存在 **Workers KV**（分块，单个文件上限 95MB），**不需要绑银行卡** |
| 谁都能用会烧掉你的密钥 | **口令门**：没口令连上传接口都调不了 |
| 进度看不见、要跳去 GitHub 下载 | 页面上看进度、点一下直接下载译文 |

存储为什么用 KV 而不是 R2：R2 空间更大（10GB）、更适合生产，但**必须绑一张银行卡**
才能开通；KV 属于 Workers 免费额度（1GB 存储、每天 1000 次写），**不绑卡**就能跑，
配合"3 天自动过期"足够个人使用。两者的接口在代码里是同一套 —— 哪天你想升级，
在 `wrangler.toml` 里把 `r2_buckets` 的注释去掉、部署一次即可，其它都不用改。

翻译本身仍然在 GitHub Actions 里跑（你的 API 密钥只存在仓库 Secret 里，不经过 Cloudflare）。
Worker 只负责"收件 → 派活 → 报进度 → 发件"。

## 一、要准备的凭据

### 1. Cloudflare API Token

面板右上角头像 → **My Profile** → **API Tokens** → **Create Token** → 用
**Edit Cloudflare Workers** 模板，然后确认这几项权限齐全：

| 范围 | 权限 | 用途 |
|---|---|---|
| Account · Workers Scripts | Edit | 部署 Worker |
| Account · Workers KV Storage | Edit | 建 KV（任务记录与文件都存在这里） |
| Account · Account Settings | Read | 读取 account id |

（走 KV 不需要 R2 权限；模板里若带上了 R2 也无所谓，用不到。）

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

### 2. 不需要开通 R2

这一版走 Workers KV，**不用绑卡、不用开通 R2**，所以本节没有要你做的事。
（如果你以后想换成 R2：面板左侧 → R2 → 开通，然后在 `wrangler.toml` 里
去掉 `r2_buckets` 的注释再部署一次。）

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

脚本会依次：验证凭据 → 建 KV → 写三个密钥（PASSWORD / AGENT_KEY / GH_TOKEN）
→ 部署 → 把网址和内部密钥同步给 GitHub（`WORKER_URL` 变量、`AGENT_KEY` 密钥），
最后打印网址与口令。**全程不需要绑卡**。重复跑是安全的，已存在的资源会跳过。

## 三、手动部署（脚本不好使时）

```bash
cd cf
export CLOUDFLARE_API_TOKEN="$(tr -d ' \r\n' < ../.secrets/cf_token)"

wrangler kv namespace create JOBS          # 把输出的 id 填进 wrangler.toml
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
| PUT | `/api/upload` | 浏览器 | 文件流直传存储层并派活（`x-password`/`x-filename`/`x-mode`/`x-target`） |
| GET | `/api/status?id=` | 浏览器 | 进度（KV 记录 + GitHub 运行状态） |
| GET | `/api/history` | 浏览器 | 最近 30 个任务 |
| GET | `/api/download?id=` | 浏览器 | 下载译文 |
| GET | `/api/input/<job>` | Actions | 取原件（`x-agent-key`） |
| POST | `/api/result/<job>` | Actions | 回传译文（`x-agent-key`） |
| POST | `/api/report/<job>` | Actions | 回传进度与统计（`x-agent-key`） |

数据清理：**原件与译文保留 3 天**（KV 的 `expirationTtl` 自动过期，不占用你的额度），
任务记录保留 **7 天**（页面"最近的任务"还能看到条目，但文件已删，点下载会提示过期）。
KV 免费额度是 1GB 存储，所以 Worker 另加了两道闸：

* 每天最多接 **30 个任务**、最多收 **250MB** 文件；
* 超出就友好拒绝（HTTP 429），不会产生任何费用 —— Workers 免费版只做限流，不会自动扣费。

另外注意：**流式下载不带 `Content-Length`**（Cloudflare 运行时的固定行为，会走 chunked
传输），浏览器下载时会显示"未知大小"，文件本身完整无损。

## 五、已知边界

* **单文件 95MB**：Cloudflare 免费版请求体上限 100MB，留了余量。
  译文回传也走同一个上限，所以**中英对照模式**（译文通常是原文的两倍大）建议原件控制在 40MB 以内。
* **KV 总共 1GB、文件只留 3 天**：够个人用；想存更大更多，就按前面的说明换成 R2（10GB）。
* **口令是明文比较**：防的是"陌生人白用你的额度"，不是防定向攻击。口令别用你其他地方的密码。
* **同一时刻的任务数**：GitHub 免费账号并发有限，多人同时用会排队，页面会显示"排队中"。
* **第一次跑会慢**：Actions 要装 OCR 引擎和中文字体，前 1～2 分钟属于环境准备。
* **大文件的分块**：20MiB 一块（KV 单值上限 25MiB）。45MB 文件实测切成 3 块，上传/下载
  字节完全一致，Actions 用 curl 取件也验过。
