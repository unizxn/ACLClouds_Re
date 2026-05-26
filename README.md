# ACLClouds 自动续期

基于 GitHub Actions + Playwright 的 ACLClouds 全自动续期工具，支持多账号普通服务续期和 Minecraft 服务器专属续期（含离线检测与自动启动）。

---

## 目录

- [功能特性](#功能特性)
- [项目结构](#项目结构)
- [快速开始](#快速开始)
- [Secret 配置说明](#secret-配置说明)
- [触发方式](#触发方式)
- [调度建议](#调度建议)
- [推送通知](#推送通知)
- [截图与调试](#截图与调试)
- [隐私保护](#隐私保护)
- [常见问题](#常见问题)

---

## 功能特性

### 通用续期（`renew.py` + `ACLClouds_Renew.yml`）

- 支持最多 **4 个账号**同时续期，互不干扰
- 自动识别剩余时间，**仅在剩余 < 2 天时触发续期**，不会重复操作
- 截图全程记录登录、表单填写、captcha、结果等关键节点
- 续期结果通过 Telegram Bot 或 wxpusher 推送

### MC 服务器专属续期（`renew_accountmc.py` + `ACLClouds_Accountmc.yml`）

- 续期阈值为**剩余 < 2 小时**（与普通账号不同）
- 每次运行自动检测服务器在线状态
- 服务器离线时自动发送 `power/start` 启动指令，轮询等待最多 120 秒
- 启动失败时单独推送离线告警，与续期结果分开通知
- 由外部 cron-job.org 定时触发（每小时），无需 GitHub Actions 内置 cron

---

## 项目结构

```
ACLClouds_Renew/
├── renew.py                          # 普通多账号续期脚本
├── renew_accountmc.py                # MC 服务器专属续期脚本
└── .github/
    └── workflows/
        ├── ACLClouds_Renew.yml       # 普通续期 Workflow（每天定时）
        └── ACLClouds_Accountmc.yml   # MC 续期 Workflow（外部触发）
```

---

## 快速开始

### 1. Fork 本仓库

点击右上角 **Fork**，将仓库复制到你自己的账号下。

### 2. 准备代理配置

脚本通过 **Xray（SOCKS5 127.0.0.1:10808）** 代理访问 ACLClouds，需要提前准备一份 Xray 配置文件（`config.json`）并将其内容填入 Secret `V2RAY_CONFIG`。

> 如果你使用其他代理工具，修改 Workflow 中启动代理的步骤即可，脚本本身不需要改动。

### 3. 配置 GitHub Secrets

进入仓库 **Settings → Secrets and variables → Actions → New repository secret**，按下表添加所需 Secret。

---

## Secret 配置说明

### 普通续期（`renew.py`）

| Secret 名称 | 必填 | 说明 |
|---|---|---|
| `V2RAY_CONFIG` | ✅ | Xray 代理配置文件内容（JSON） |
| `ACCOUNT1_EMAIL` | ✅ | 账号1 登录邮箱 |
| `ACCOUNT1_PASSWORD` | ✅ | 账号1 登录密码 |
| `ACCOUNT2_EMAIL` | ❌ | 账号2 登录邮箱（留空跳过） |
| `ACCOUNT2_PASSWORD` | ❌ | 账号2 登录密码（留空跳过） |
| `ACCOUNT3_EMAIL` | ❌ | 账号3 登录邮箱（留空跳过） |
| `ACCOUNT3_PASSWORD` | ❌ | 账号3 登录密码（留空跳过） |
| `ACCOUNT4_EMAIL` | ❌ | 账号4 登录邮箱（留空跳过） |
| `ACCOUNT4_PASSWORD` | ❌ | 账号4 登录密码（留空跳过） |
| `TG_BOT_TOKEN` | ❌ | Telegram Bot Token（不填则不推送） |
| `TG_CHAT_ID` | ❌ | Telegram Chat ID |
| `WXPUSHER_APPTOKEN` | ❌ | wxpusher App Token（不填则不推送） |
| `WXPUSHER_UID` | ❌ | wxpusher 用户 UID |

### MC 续期（`renew_accountmc.py`）

| Secret 名称 | 必填 | 说明 |
|---|---|---|
| `V2RAY_CONFIG` | ✅ | 同上，与普通续期共用 |
| `MC_EMAIL` | ✅ | MC 账号登录邮箱 |
| `MC_PASSWORD` | ✅ | MC 账号登录密码 |
| `TG_BOT_TOKEN` | ❌ | 同上，共用 |
| `TG_CHAT_ID` | ❌ | 同上，共用 |
| `WXPUSHER_APPTOKEN` | ❌ | 同上，共用 |
| `WXPUSHER_UID` | ❌ | 同上，共用 |

> TG 和 wxpusher 至少配置一个，否则无法收到续期通知。两者可同时配置。

---

## 触发方式

### 普通续期

`ACLClouds_Renew.yml` 支持两种触发方式：

**自动定时触发**（GitHub Actions 内置 cron）

```yaml
schedule:
  - cron: '0 2 * * *'   # 每天 UTC 2:00（北京时间 10:00）运行
```

**手动触发**

进入仓库 **Actions → ACLClouds 自动续期 → Run workflow**，可选择是否开启录屏。

---

### MC 续期

`ACLClouds_Accountmc.yml` **只支持 `workflow_dispatch`（外部/手动触发）**，不内置 cron，需要配合 **cron-job.org** 实现定时调用。

#### 配置 cron-job.org

1. 注册并登录 [cron-job.org](https://cron-job.org)
2. 新建计划任务，URL 填写：
   ```
   https://api.github.com/repos/<你的用户名>/<仓库名>/actions/workflows/ACLClouds_Accountmc.yml/dispatches
   ```
3. 请求方式选 **POST**，添加以下 Header：
   ```
   Authorization: Bearer <你的 GitHub PAT>
   Content-Type: application/json
   Accept: application/vnd.github+json
   ```
4. 请求体（Body）填写：
   ```json
   {"ref": "main"}
   ```
5. 执行计划选择 **每 1 小时**（推荐，见[调度建议](#调度建议)）

> GitHub PAT 需要 `repo` + `actions:write` 权限，在 GitHub **Settings → Developer settings → Personal access tokens** 中生成。

---

## 调度建议

| 脚本 | 续期阈值 | 推荐 cron | 说明 |
|---|---|---|---|
| `renew.py` | 剩余 < 2 天 | `0 2 * * *`（每天） | 每天检查一次完全足够 |
| `renew_accountmc.py` | 剩余 < 2 小时 | `0 * * * *`（每小时） | 每 2 小时跑存在漏续期风险 |

**为什么 MC 续期不能用每 2 小时？**

续期阈值是 2 小时，如果 cron 也是每 2 小时，当服务器到期时间恰好落在两次 cron 之间（例如 cron 在 0:00 和 2:00 跑，服务器 1:59 到期），0:00 时剩余时间可能刚好超过 2 小时阈值而跳过，导致服务器在下次 cron 跑之前就已过期。

每 1 小时检查一次（`0 * * * *`），阈值 2 小时，实际安全余量为 1 小时，不会漏。

---

## 推送通知

### Telegram

续期成功示例：
```
✅ ACLClouds 账号1 续期成功

• aclnode（1h 45min → 47h 52min）

ACLClouds Auto Renew
```

续期跳过示例：
```
⏳ ACLClouds 账号1 无需续期

• aclnode（剩余 45h 12min）

ACLClouds Auto Renew
```

### MC 专属推送

**服务离线告警**（自动启动失败时单独推送）：
```
🚨 ACLClouds MC账号 服务离线且启动失败！

• mc-server-1

已尝试自动启动但超时，请手动检查！
ACLClouds Auto Renew
```

**续期结果**：
```
✅ ACLClouds MC账号 续期成功

• mc-server-1（1h 23min → 3h 21min）

ACLClouds Auto Renew
```

---

## 截图与调试

每次运行后，截图会自动上传到 Actions Artifacts，保留 2~3 天。

| 截图文件名 | 含义 |
|---|---|
| `01_login.png` | 打开登录页 |
| `02_form_filled.png` | 填写邮箱密码后（captcha 点击前） |
| `02b_captcha.png` | captcha 验证通过后 |
| `03_dashboard.png` | 登录成功，进入控制台 |
| `04_final.png` | 所有项目处理完毕 |
| `99_error.png` | 发生异常时的页面状态 |
| `99_login_fail.png` | 登录失败时的页面状态 |

**查看截图**：进入仓库 Actions → 对应 run → 页面底部 Artifacts → 下载对应压缩包。

**开启录屏**（仅手动触发时可选）：

手动触发 Workflow 时，将 `enable_video` 参数选为 `true`，录屏文件会随截图一并上传到 Artifacts。

---

## 隐私保护

脚本对所有截图应用 JS 模糊处理，以下内容在截图中均不可见：

- 登录表单中的邮箱和密码（`input` 字段）
- 顶栏右上角用户名（`header button` 及叶节点文字）
- 欢迎语中的用户名（`h1/h2/h3` 子节点）
- 项目/服务器列表中含数字的列（ID、到期日、IP）
- 所有含 `address`、`ip`、`host`、`expire`、`renew` 等关键词的元素

日志中的邮箱显示为 `a**@e******.com`，IP 显示为 `208.77.*.*`。

---

## 常见问题

**Q：运行后提示 `获取项目列表失败 HTTP 500`**

该账号在 ACLClouds 面板上可能没有任何项目。`/api/client` 接口在账号无项目时会返回 500（面板自身行为），脚本会将其视为"无项目"并推送告警，不会崩溃退出。

---

**Q：captcha 验证失败**

脚本会最多重试 3 次点击 captcha，每次给 15 秒等待验证完成，确认通过后再等 500ms 稳定，才会点击提交。如果 3 次全部失败，说明代理 IP 被 captcha 服务拦截，建议更换代理节点。

---

**Q：登录成功但获取项目列表 500**

`03_dashboard.png` 截图若显示页面还在转圈，说明 SPA 未完成初始化就发出了 API 请求。脚本会等待侧边栏关键元素出现后才调用 API，并在 500 时最多重试 3 次（每次间隔 5 秒）。如果持续 500，参考上一条。

---

**Q：如何验证代理是否生效**

查看 Actions 日志中的 `代理出口 IP` 一行，显示格式为 `208.77.*.*`（已脱敏）。如果显示"获取失败"，说明代理未正常启动，检查 `V2RAY_CONFIG` 内容是否正确。

---

**Q：MC 服务器每次都显示离线告警但实际在线**

`/api/client/servers/{id}/resources` 接口的 `current_state` 字段判断在线状态。如果服务器面板返回的状态不是 `running` 或 `starting`，会被识别为离线。可查看对应截图和日志中的 `current_state` 原始值进行排查。

---

**Q：想手动立刻触发一次续期**

普通续期：进入 **Actions → ACLClouds 自动续期 → Run workflow**。

MC 续期：进入 **Actions → ACLClouds MC续期 → Run workflow**，或在 cron-job.org 手动执行一次任务。
