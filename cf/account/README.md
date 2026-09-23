# Aurora 账户系统 · 阶段 1

`ourmetaverse.cn` 的统一账号：注册（邮箱验证码）、登录、退出、忘记密码、权限、用量额度。
部署在 **account.ourmetaverse.cn**，跑在 Cloudflare Workers + D1 上，**不需要服务器、不需要备案**。

## 一、已经做好的功能

| 功能 | 说明 |
|---|---|
| 注册 | 邮箱 + 6 位验证码 + 密码；第一个注册的账号自动是管理员 |
| 登录 / 退出 | 服务端会话，30 天有效 |
| 忘记密码 | 邮箱验证码重置；重置后把该用户所有会话踢下线 |
| 修改密码 | 需要原密码；改完同样踢掉所有会话 |
| 权限 | `user` / `vip` / `admin` 三级，管理接口只有 admin 能用 |
| 用量额度 | 每人每月默认 200 页，管理员可单独调整（0 = 不限） |
| 用量账本 | 每次翻译写一条记录（阶段 2 接入翻译站时自动写） |
| 管理后台 | 页面下方"管理员 · 用户管理"，可改角色、发额度、封号 |
| 审计日志 | 注册、登录、改密码、重置、管理员操作全部留痕 |
| 限流 | 发验证码：同邮箱 1 分钟 1 条 / 1 天 10 条，同 IP 1 天 20 条；登录失败 15 分钟 8 次 |

## 二、安全设计（为什么这样做）

| 项目 | 做法 | 为什么 |
|---|---|---|
| 密码 | PBKDF2-SHA256，随机 16 字节盐，**10 万次迭代** | 数据库泄露也算不出原密码 |
| 会话 | Cookie 里放 32 字节随机 token，**数据库只存它的 SHA-256** | 数据库泄露也没法直接冒充别人 |
| 验证码 | 只存 HMAC 哈希，10 分钟过期，**用过即废** | 泄露也没法反推验证码 |
| Cookie | `HttpOnly; Secure; SameSite=Lax` | JS 读不到；跨站请求带不出去 |
| CSRF | 写接口校验 `Origin` + 只接受 POST + JSON | 双保险 |
| 用户枚举 | 登录失败不区分"邮箱不存在"和"密码错" | 防止被用来探测哪些邮箱注册过 |
| 敏感操作 | 全部写 `audit` 表 | 出事能追溯 |

已经实测验证过：数据库里搜不到明文密码、会话表里是哈希、验证码表里是哈希、跨站 Origin 的写请求返回 403。

## 三、部署

```bash
# 1. 令牌补一个权限：Account → D1 → Edit（现有令牌是 Workers 模板建的，没有 D1）
#    地址：https://dash.cloudflare.com/<你的账户ID>/api-tokens → aurora-translating → Edit
# 2. 一条命令部署
bash cf/deploy-account.sh
```

脚本会：建 D1 → 建限流 KV → 灌表结构 → 写 SESSION_SECRET → 部署 → 绑 `account.ourmetaverse.cn`。

**部署完第一件事：用你自己的邮箱注册** —— 第一个注册的账号自动成为管理员。

### 发验证码邮件的两种模式

| 模式 | 怎么做 | 适合 |
|---|---|---|
| **Resend（推荐）** | 去 <https://resend.com> 免费注册（3000 封/月）→ 验证 `ourmetaverse.cn`（它会给你 3 条 DNS 记录，加到 Cloudflare 的 DNS 页）→ 把 API Key 写进 `.secrets/resend_key` → 重跑部署脚本 | 正式使用 |
| **无邮件** | 不配 `resend_key` 时，线上发不出验证码 | 不推荐 |

> 如果暂时不想折腾邮件，我下一步可以给你加"**邀请码注册**"模式：管理员在后台生成邀请码，朋友用邀请码 + 邮箱密码注册，全程不需要发信。表 `invites` 已经建好了，接上界面即可。

## 四、接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 登录/注册/个人中心页面 |
| POST | `/api/send-code` | 发验证码（`purpose`: register / reset） |
| POST | `/api/register` | 注册并自动登录 |
| POST | `/api/login` | 登录 |
| POST | `/api/logout` | 退出 |
| POST | `/api/reset-password` | 用验证码重置密码 |
| POST | `/api/change-password` | 登录后修改密码 |
| GET | `/api/me` | 当前用户 + 额度 + 累计用量 |
| GET | `/api/usage` | 最近 50 条用量 |
| GET | `/api/admin/users` | 用户列表（管理员） |
| POST | `/api/admin/update` | 改角色/额度/封号（管理员） |

## 五、本地开发（不需要任何线上权限）

```bash
cd cf/account
wrangler d1 execute aurora-account --local --file=schema.sql   # 建表
wrangler dev --port 8790                                       # 起本地服务
# .dev.vars 里 DEV_SHOW_CODE=1 时，验证码会直接回给调用方，方便调试
```

## 六、进度（阶段 2 已完成）

* ✅ **阶段 2 · 接入翻译站**：`doc.ourmetaverse.cn` 已改为"登录后才能用"，
  每翻完一个文件按实际页数扣额度并写 `usage` 流水；任务归属隔离（读别人的任务 403）。
  两个 Worker 共用 `.ourmetaverse.cn` 作用域的会话 Cookie 与同一个 D1
  （共享模块 `cf/shared/session.js`）。
* **阶段 3 · 后台完善**：现在的管理功能是"能用"级别（用浏览器 prompt 弹框改），
  可以做成正式的管理页面，加用户搜索、用量图表、批量发额度。
* **阶段 4 · 充值**：卡密兑换（个人可做）或微信/支付宝（需营业执照 + 备案）。
