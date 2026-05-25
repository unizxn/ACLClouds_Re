#!/usr/bin/env python3
"""
ACLClouds 自动续期脚本 (Playwright 全程浏览器版 · 多账号)
支持最多 3 个账号，通过编号 Secret 区分：
  ACCOUNT1_EMAIL / ACCOUNT1_PASSWORD
  ACCOUNT2_EMAIL / ACCOUNT2_PASSWORD
  ACCOUNT3_EMAIL / ACCOUNT3_PASSWORD（可选）
"""

import os
import re
import sys
import json
import time
import traceback
from urllib.request import Request, urlopen

# ── 代理配置 ──────────────────────────────────────────────
# v2ray/Xray 本地 SOCKS5 代理
PROXY_SERVER = "socks5://127.0.0.1:10808"

# ── 推送凭据（全局共用） ──────────────────────────────────
TG_BOT_TOKEN      = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID        = os.environ.get("TG_CHAT_ID", "").strip()
WXPUSHER_APPTOKEN = os.environ.get("WXPUSHER_APPTOKEN", "").strip()
WXPUSHER_UID      = os.environ.get("WXPUSHER_UID", "").strip()

BASE_URL  = "https://dash.aclclouds.com"
LOGIN_URL = f"{BASE_URL}/auth/login"

# ── 续期模式说明 ──────────────────────────────────────────
# normal : 剩余 < 2 天 时续期（普通免费账号，随时可续）
# short  : 剩余 < 2 小时 时续期（ACLClouds 限制：到期前2小时内才能续）
THRESHOLD = {
    "normal": 2.0,          # 天
    "short":  2 / 24,     # 天（= 2小时）
}

# ── 读取多账号列表 ────────────────────────────────────────
def load_accounts():
    accounts = []
    for i in range(1, 4):           # 支持 1~3 个账号
        email    = os.environ.get(f"ACCOUNT{i}_EMAIL", "").strip()
        password = os.environ.get(f"ACCOUNT{i}_PASSWORD", "").strip()
        if email and password:
            mode = os.environ.get(f"ACCOUNT{i}_RENEW_MODE", "normal").strip().lower()
            if mode not in THRESHOLD:
                mode = "normal"
            accounts.append({"index": i, "email": email, "password": password, "mode": mode})
    return accounts

# ── 日志 ─────────────────────────────────────────────────
def log(msg):       print(f"[INFO] {msg}", flush=True)
def log_warn(msg):  print(f"[WARN] {msg}", flush=True)
def log_error(msg): print(f"[ERROR] {msg}", flush=True)

def get_outbound_ip():
    try:
        data = urlopen("https://cloudflare.com/cdn-cgi/trace", timeout=5).read().decode()
        for line in data.splitlines():
            if line.startswith("ip="):
                return line.strip()
    except Exception as e:
        return f"ip=获取失败({e})"
    return "ip=未知"

def get_proxy_ip():
    try:
        import subprocess
        result = subprocess.run(
            ["curl", "-s", "--max-time", "5", "--socks5", "127.0.0.1:10808", "ifconfig.me"],
            capture_output=True, text=True, timeout=10
        )
        return result.stdout.strip() if result.returncode == 0 else "获取失败"
    except Exception as e:
        return f"获取失败({e})"

# ── 推送函数 ──────────────────────────────────────────────
def send_tg(text: str):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    try:
        body = json.dumps({"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"}).encode()
        req = Request(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                      data=body, headers={"Content-Type": "application/json"})
        urlopen(req, timeout=15)
        log("TG 推送成功")
    except Exception as e:
        log_warn(f"TG 推送失败: {e}")

def send_wxpusher(text: str):
    if not WXPUSHER_APPTOKEN or not WXPUSHER_UID:
        return
    try:
        payload = {"appToken": WXPUSHER_APPTOKEN, "content": text,
                   "summary": "ACLClouds 续期通知", "contentType": 1, "uids": [WXPUSHER_UID]}
        req = Request("https://wxpusher.zjiecode.com/api/send/message",
                      data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        result = json.loads(urlopen(req, timeout=15).read().decode())
        if result.get("code") == 1000:
            log("wxpusher 推送成功")
        else:
            log_warn(f"wxpusher 返回错误: {result}")
    except Exception as e:
        log_warn(f"wxpusher 推送失败: {e}")

def send_all_push(text: str):
    send_tg(text)
    send_wxpusher(text)

# ── 解析剩余时间 ──────────────────────────────────────────
def parse_expires(text):
    if text is None:
        return None
    s = str(text).strip()
    if re.search(r'\d{4}-\d{2}-\d{2}', s):
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return (dt - datetime.now(timezone.utc)).total_seconds() / 86400
        except Exception:
            pass
    try:
        return float(s) / 86400
    except Exception:
        pass
    sl = s.lower()
    days = hours = minutes = 0.0
    m = re.search(r'(\d+(?:\.\d+)?)\s*[dj]', sl)
    if m: days = float(m.group(1))
    m = re.search(r'(\d+(?:\.\d+)?)\s*h', sl)
    if m: hours = float(m.group(1))
    m = re.search(r'(\d+(?:\.\d+)?)\s*m(?!o)', sl)
    if m: minutes = float(m.group(1))
    total = days + hours / 24 + minutes / 1440
    return total if total > 0 else None

# ── 截图工具 ──────────────────────────────────────────────
def screenshot(page, name: str):
    os.makedirs("screenshots", exist_ok=True)
    path = f"screenshots/{name}.png"
    try:
        page.screenshot(path=path, full_page=True)
        log(f"截图已保存: {path}")
    except Exception as e:
        log_warn(f"截图失败 {path}: {e}")

# ── 单账号续期 ────────────────────────────────────────────
def run_account(account: dict):
    """对单个账号执行续期，返回 (renewed_list, skipped_list, failed_list)"""
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    idx       = account["index"]
    email     = account["email"]
    password  = account["password"]
    mode      = account.get("mode", "normal")
    threshold = THRESHOLD[mode]
    tag       = f"账号{idx}({email})"
    log(f"  续期模式: {mode}（阈值 {threshold*24:.1f} 小时）")

    log(f"\n{'='*50}")
    log(f"开始处理 {tag}")
    log(f"{'='*50}")

    renewed_list, skipped_list, failed_list = [], [], []

    with sync_playwright() as p:
        os.makedirs("screenshots", exist_ok=True)
        browser = p.chromium.launch(
            args=["--no-sandbox", "--disable-setuid-sandbox"],
            proxy={"server": PROXY_SERVER},
        )
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/148.0.0.0 Safari/537.36",
            locale="zh-CN",
            record_video_dir="screenshots/",
            record_video_size={"width": 1280, "height": 800},
        )
        page = ctx.new_page()

        try:
            # ── 1. 打开登录页 ─────────────────────────────
            log(f"[{tag}] 导航到登录页: {LOGIN_URL}")
            page.goto(LOGIN_URL, timeout=60000)
            page.wait_for_load_state("networkidle", timeout=30000)
            screenshot(page, f"acct{idx}_01_login_page")

            # ── 2. 填写登录表单 ───────────────────────────
            log(f"[{tag}] 填写登录表单...")
            email_selectors = [
                "input[type='email']", "input[name='user']", "input[name='email']",
                "input[placeholder*='mail']", "input[placeholder*='Email']", "input:first-of-type",
            ]
            email_filled = False
            for sel in email_selectors:
                try:
                    page.wait_for_selector(sel, timeout=3000)
                    page.fill(sel, email)
                    log(f"  邮箱字段使用选择器: {sel}")
                    email_filled = True
                    break
                except Exception:
                    continue

            if not email_filled:
                screenshot(page, f"acct{idx}_02_no_email_field")
                raise RuntimeError("找不到邮箱输入框")

            for sel in ["input[type='password']", "input[name='password']"]:
                try:
                    page.wait_for_selector(sel, timeout=3000)
                    page.fill(sel, password)
                    break
                except Exception:
                    continue

            screenshot(page, f"acct{idx}_02_form_filled")

            # ── 3. captcha ────────────────────────────────
            log(f"[{tag}] 点击 captcha 复选框...")
            page.click("div.auth-captcha-inner", timeout=10000)
            try:
                page.wait_for_selector(
                    "div.auth-captcha-box.verified, div.auth-captcha-inner[aria-checked='true']",
                    timeout=10000)
                log(f"[{tag}] captcha 验证通过 ✅")
            except Exception:
                log_warn(f"[{tag}] captcha 未检测到 verified，继续提交")
            screenshot(page, f"acct{idx}_02b_captcha")

            # ── 4. 提交登录 ───────────────────────────────
            for sel in ["button[type='submit']", "button:has-text('Login')",
                        "button:has-text('登录')", "button:has-text('Sign in')",
                        "input[type='submit']"]:
                try:
                    page.click(sel, timeout=3000)
                    break
                except Exception:
                    continue

            page.wait_for_load_state("networkidle", timeout=30000)
            screenshot(page, f"acct{idx}_03_after_submit")

            try:
                page.wait_for_url(lambda url: "login" not in url, timeout=20000)
                log(f"[{tag}] 登录成功 ✅，URL: {page.url}")
            except PWTimeout:
                screenshot(page, f"acct{idx}_03_login_timeout")
                raise RuntimeError(f"登录超时，仍在: {page.url}")

            screenshot(page, f"acct{idx}_04_after_login")

            # ── 5. 获取项目列表 ───────────────────────────
            result = page.evaluate("""async () => {
                const r = await fetch('/api/client', {headers: {'Accept': 'application/json'}});
                return {status: r.status, body: await r.text()};
            }""")
            if result['status'] != 200:
                raise RuntimeError(f"获取项目列表失败 HTTP {result['status']}")

            data = json.loads(result['body'])
            projects = [item['attributes'] for item in data.get('data', []) if item.get('attributes')]
            log(f"[{tag}] 找到 {len(projects)} 个项目")

            if not projects:
                log_warn(f"[{tag}] 项目列表为空")
                ctx.close(); browser.close()
                return renewed_list, skipped_list, failed_list

            # ── 6. 逐项目续期 ────────────────────────────
            for project in projects:
                name        = project.get("name", "未知项目")
                identifier  = project.get("identifier", "")
                raw_expires = project.get("expires_at")
                remaining   = parse_expires(raw_expires)

                if remaining is None:
                    failed_list.append(f"{tag} · {name}（无法解析过期时间）")
                    continue

                log(f"[{tag}] [{name}] 剩余 {remaining:.2f} 天")

                if remaining >= RENEW_THRESHOLD_DAYS:
                    skipped_list.append(f"{tag} · {name}（剩余 {remaining:.1f} 天）")
                    continue

                try:
                    renew_url = f"/api/client/servers/{identifier}/upgrade/renew"
                    renew_result = page.evaluate(f"""async () => {{
                        const xsrf = decodeURIComponent(
                            document.cookie.split('; ')
                            .find(c => c.startsWith('XSRF-TOKEN='))
                            ?.split('=')[1] || ''
                        );
                        const r = await fetch('{renew_url}', {{
                            method: 'POST',
                            headers: {{'Accept': 'application/json', 'X-XSRF-TOKEN': xsrf}}
                        }});
                        return {{status: r.status, body: await r.text()}};
                    }}""")

                    if renew_result['status'] == 200:
                        time.sleep(2)
                        new_result = page.evaluate("""async () => {
                            const r = await fetch('/api/client', {headers: {'Accept': 'application/json'}});
                            return await r.json();
                        }""")
                        new_expires = None
                        for item in new_result.get('data', []):
                            attrs = item.get('attributes', {})
                            if attrs.get('identifier') == identifier:
                                new_expires = attrs.get('expires_at')
                                break
                        if new_expires:
                            new_remaining = parse_expires(new_expires)
                            renewed_list.append(
                                f"{tag} · {name}（{remaining:.1f}天 → {new_remaining:.1f}天）")
                        else:
                            renewed_list.append(f"{tag} · {name}（续期前 {remaining:.1f} 天）")
                    else:
                        body = renew_result['body']
                        try:
                            err = json.loads(body).get('error', 'unknown')
                        except Exception:
                            err = body[:80]
                        raise RuntimeError(f"续期失败: {err}")

                except Exception as e:
                    log_error(f"[{tag}][{name}] 续期异常: {e}")
                    failed_list.append(f"{tag} · {name}（{str(e)[:80]}）")

            screenshot(page, f"acct{idx}_05_final")

        except Exception as e:
            screenshot(page, f"acct{idx}_99_error")
            ctx.close(); browser.close()
            failed_list.append(f"{tag} · 账号级异常: {str(e)[:120]}")
            return renewed_list, skipped_list, failed_list

        ctx.close()
        browser.close()

    return renewed_list, skipped_list, failed_list


# ── 主入口 ────────────────────────────────────────────────
if __name__ == "__main__":
    accounts = load_accounts()
    if not accounts:
        log_error("未找到任何账号！请设置 ACCOUNT1_EMAIL / ACCOUNT1_PASSWORD 等环境变量")
        sys.exit(1)

    log(f"[网络] 直连出口 IP: {get_outbound_ip()}")
    log(f"[网络] 代理出口 IP: {get_proxy_ip()}")
    log(f"共 {len(accounts)} 个账号待处理")

    all_renewed, all_skipped, all_failed = [], [], []

    for account in accounts:
        try:
            r, s, f = run_account(account)
            all_renewed.extend(r)
            all_skipped.extend(s)
            all_failed.extend(f)
        except Exception as ex:
            log_error(f"账号{account['index']} 顶层异常: {ex}")
            traceback.print_exc()
            all_failed.append(f"账号{account['index']}({account['email']}) · 顶层异常: {str(ex)[:100]}")

    # ── 汇总推送 ──────────────────────────────────────────
    log("=" * 50)
    log(f"续期成功: {len(all_renewed)} 个")
    log(f"无需续期: {len(all_skipped)} 个")
    log(f"失败/异常: {len(all_failed)} 个")

    if all_renewed:
        lines = ["✅ <b>ACLClouds 自动续期成功</b>", ""]
        lines += [f"• {i}" for i in all_renewed]
        if all_failed:
            lines += ["", "⚠️ 以下项目失败："] + [f"• {i}" for i in all_failed]
        lines += ["", "ACLClouds Auto Renew"]
        send_all_push("\n".join(lines))
    elif all_failed:
        lines = ["❌ <b>ACLClouds 续期失败</b>", ""]
        lines += [f"• {i}" for i in all_failed]
        lines += ["", "ACLClouds Auto Renew"]
        send_all_push("\n".join(lines))
    else:
        log("所有账号均无需续期，不发送推送")
