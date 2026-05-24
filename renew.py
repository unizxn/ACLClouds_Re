#!/usr/bin/env python3
"""
ACLClouds 自动续期脚本 (Playwright 全程浏览器版)
用真实浏览器操作，完全绕开 requests session 问题
"""

import os
import re
import sys
import json
import time
import traceback
from urllib.request import Request, urlopen

# ── 环境变量 ─────────────────────────────────────────────
EMAIL             = os.environ.get("ACLCLOUDS_EMAIL", "").strip()
PASSWORD          = os.environ.get("ACLCLOUDS_PASSWORD", "").strip()
TG_BOT_TOKEN      = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID        = os.environ.get("TG_CHAT_ID", "").strip()
WXPUSHER_APPTOKEN = os.environ.get("WXPUSHER_APPTOKEN", "").strip()
WXPUSHER_UID      = os.environ.get("WXPUSHER_UID", "").strip()

RENEW_THRESHOLD_DAYS = 2
BASE_URL  = "https://dash.aclclouds.com"
LOGIN_URL = f"{BASE_URL}/auth/login"

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

# ── 推送函数 ──────────────────────────────────────────────
def send_tg(text: str):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        log_warn("TG 未配置，跳过推送")
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
        log_warn("wxpusher 未配置，跳过推送")
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

# ── Playwright 核心 ───────────────────────────────────────
def run_with_browser():
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    log(f"[网络] 出口 IP: {get_outbound_ip()}")

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox", "--disable-setuid-sandbox"])
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/148.0.0.0 Safari/537.36",
            locale="zh-CN",
        )
        page = ctx.new_page()

        # ── 1. 登录 ──────────────────────────────────────
        log(f"导航到登录页: {LOGIN_URL}")
        page.goto(LOGIN_URL, timeout=60000)
        page.wait_for_load_state("networkidle", timeout=30000)

        log("填写登录表单...")
        # 等待邮箱输入框
        page.wait_for_selector("input[type='email'], input[name='user'], input[placeholder*='mail']", timeout=15000)
        page.fill("input[type='email'], input[name='user'], input[placeholder*='mail']", EMAIL)
        page.fill("input[type='password']", PASSWORD)

        log("提交登录...")
        page.click("button[type='submit'], button:has-text('Login'), button:has-text('登录')")
        page.wait_for_load_state("networkidle", timeout=30000)

        # 等待跳转到 dashboard
        try:
            page.wait_for_url(f"{BASE_URL}/projects**", timeout=20000)
            log("登录成功 ✅，已跳转到项目页")
        except PWTimeout:
            # 可能已经在 projects 页或其他页
            current = page.url
            log_warn(f"等待跳转超时，当前 URL: {current}")
            if "login" in current:
                # 截图留证
                os.makedirs("screenshots", exist_ok=True)
                page.screenshot(path="screenshots/login_failed.png", full_page=True)
                raise RuntimeError("登录失败，仍在登录页")

        # ── 2. 截图（登录后） ─────────────────────────────
        os.makedirs("screenshots", exist_ok=True)
        page.screenshot(path="screenshots/after_login.png", full_page=True)
        log("截图已保存: screenshots/after_login.png")

        # ── 3. 调用 API 获取项目列表 ─────────────────────
        log("通过浏览器 fetch 获取项目列表...")
        result = page.evaluate("""async () => {
            const r = await fetch('/api/client', {
                headers: {'Accept': 'application/json'}
            });
            return {status: r.status, body: await r.text()};
        }""")
        log(f"  → HTTP {result['status']}")
        if result['status'] != 200:
            log_warn(f"  → 响应体: {result['body'][:300]}")
            raise RuntimeError(f"获取项目列表失败 HTTP {result['status']}")

        data = json.loads(result['body'])
        projects = [item['attributes'] for item in data.get('data', []) if item.get('attributes')]
        log(f"  → 找到 {len(projects)} 个项目")

        if not projects:
            log_warn("项目列表为空，无需操作")
            browser.close()
            return

        # ── 4. 逐项目检查并续期 ──────────────────────────
        renewed_list = []
        skipped_list = []
        failed_list  = []

        for project in projects:
            name       = project.get("name", "未知项目")
            identifier = project.get("identifier", "")
            raw_expires = project.get("expires_at")
            log(f"[{name}] 过期数据: {raw_expires!r}")
            remaining = parse_expires(raw_expires)

            if remaining is None:
                log_warn(f"[{name}] 无法解析剩余时间")
                failed_list.append(f"{name}（无法解析过期时间）")
                continue

            log(f"[{name}] 剩余 {remaining:.2f} 天")

            if remaining >= RENEW_THRESHOLD_DAYS:
                log(f"[{name}] 无需续期")
                skipped_list.append(f"{name}（剩余 {remaining:.1f} 天）")
                continue

            log(f"[{name}] 开始续期...")
            try:
                renew_url = f"/api/client/servers/{identifier}/upgrade/renew"
                renew_result = page.evaluate(f"""async () => {{
                    const r = await fetch('{renew_url}', {{
                        method: 'POST',
                        headers: {{
                            'Accept': 'application/json',
                            'X-XSRF-TOKEN': decodeURIComponent(
                                document.cookie.split('; ')
                                .find(c => c.startsWith('XSRF-TOKEN='))
                                ?.split('=')[1] || ''
                            )
                        }}
                    }});
                    return {{status: r.status, body: await r.text()}};
                }}""")
                log(f"  → 续期 HTTP {renew_result['status']}, body: {renew_result['body'][:200]}")

                if renew_result['status'] == 200:
                    log(f"[{name}] ✅ 续期成功，等待2秒后获取新到期时间...")
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
                        log(f"[{name}] 续期后剩余 {new_remaining:.2f} 天（新到期: {new_expires}）")
                        renewed_list.append(f"{name}（续期前剩余 {remaining:.1f} 天，续期后剩余 {new_remaining:.1f} 天）")
                    else:
                        renewed_list.append(f"{name}（续期前剩余 {remaining:.1f} 天）")
                else:
                    body = renew_result['body']
                    try:
                        err = json.loads(body).get('error', 'unknown')
                    except Exception:
                        err = body[:80]
                    raise RuntimeError(f"续期失败: {err}")

            except Exception as e:
                log_error(f"[{name}] 续期异常: {e}")
                failed_list.append(f"{name}（{str(e)[:80]}）")

        # ── 5. 截图（操作后） ─────────────────────────────
        page.screenshot(path="screenshots/after_renew.png", full_page=True)
        log("截图已保存: screenshots/after_renew.png")

        browser.close()

        # ── 6. 汇总推送 ──────────────────────────────────
        log("=" * 50)
        log(f"续期成功: {len(renewed_list)} 个")
        log(f"无需续期: {len(skipped_list)} 个")
        log(f"失败/异常: {len(failed_list)} 个")

        if renewed_list:
            lines = ["✅ <b>ACLClouds 自动续期成功</b>", ""]
            lines += [f"• {i}" for i in renewed_list]
            if failed_list:
                lines += ["", "⚠️ 以下项目失败："] + [f"• {i}" for i in failed_list]
            lines += ["", "ACLClouds Auto Renew"]
            send_all_push("\n".join(lines))
        elif failed_list:
            lines = ["❌ <b>ACLClouds 续期失败</b>", ""]
            lines += [f"• {i}" for i in failed_list]
            lines += ["", "ACLClouds Auto Renew"]
            send_all_push("\n".join(lines))
        else:
            log("无续期操作，不发送推送")


# ── 主入口 ────────────────────────────────────────────────
if __name__ == "__main__":
    if not EMAIL or not PASSWORD:
        log_error("缺少环境变量 ACLCLOUDS_EMAIL 或 ACLCLOUDS_PASSWORD")
        sys.exit(1)
    try:
        run_with_browser()
        log("脚本执行完毕")
    except Exception:
        log_error("脚本失败")
        traceback.print_exc()
        sys.exit(1)
