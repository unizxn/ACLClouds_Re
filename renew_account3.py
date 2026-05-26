#!/usr/bin/env python3
"""
ACLClouds 账号3 专用脚本
- 续期模式：short（剩余 < 2小时 才续期，符合 ACLClouds 限制）
- 离线检测：每次运行时检查服务是否在线，离线自动发 start 指令并等待 running，失败才推送告警
- 由 cron-job.org 每5小时触发一次
"""

import os
import re
import sys
import json
import time
import traceback
from urllib.request import Request, urlopen

# ── 代理配置 ──────────────────────────────────────────────
PROXY_SERVER = "socks5://127.0.0.1:10808"

# ── 录屏开关 ──────────────────────────────────────────────
ENABLE_VIDEO = os.environ.get("ENABLE_VIDEO", "false").strip().lower() == "true"

# ── 账号3 凭据 ────────────────────────────────────────────
EMAIL    = os.environ.get("ACCOUNT3_EMAIL", "").strip()
PASSWORD = os.environ.get("ACCOUNT3_PASSWORD", "").strip()

# ── 推送凭据 ──────────────────────────────────────────────
TG_BOT_TOKEN      = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID        = os.environ.get("TG_CHAT_ID", "").strip()
WXPUSHER_APPTOKEN = os.environ.get("WXPUSHER_APPTOKEN", "").strip()
WXPUSHER_UID      = os.environ.get("WXPUSHER_UID", "").strip()

# ── 续期阈值：剩余 < 2小时 才续期 ────────────────────────
RENEW_THRESHOLD_DAYS = 2 / 24   # = 0.0833 天

BASE_URL  = "https://dash.aclclouds.com"
LOGIN_URL = f"{BASE_URL}/auth/login"

# ── 日志 ─────────────────────────────────────────────────
def log(msg):       print(f"[INFO] {msg}", flush=True)
def log_warn(msg):  print(f"[WARN] {msg}", flush=True)
def log_error(msg): print(f"[ERROR] {msg}", flush=True)

def get_proxy_ip():
    try:
        import subprocess
        r = subprocess.run(
            ["curl", "-s", "--max-time", "5", "--socks5", "127.0.0.1:10808", "ifconfig.me"],
            capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else "获取失败"
    except Exception as e:
        return f"获取失败({e})"

# ── 推送 ──────────────────────────────────────────────────
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
                   "summary": "ACLClouds 账号3 通知", "contentType": 1, "uids": [WXPUSHER_UID]}
        req = Request("https://wxpusher.zjiecode.com/api/send/message",
                      data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        result = json.loads(urlopen(req, timeout=15).read().decode())
        if result.get("code") != 1000:
            log_warn(f"wxpusher 返回错误: {result}")
    except Exception as e:
        log_warn(f"wxpusher 推送失败: {e}")

def send_all(text: str):
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

def fmt_remaining(days: float) -> str:
    """把天数格式化为易读字符串，如 5h 30min"""
    total_minutes = int(days * 24 * 60)
    h, m = divmod(total_minutes, 60)
    if h > 0:
        return f"{h}h {m}min" if m else f"{h}h"
    return f"{m}min"

# ── 截图 ──────────────────────────────────────────────────
def screenshot(page, name: str):
    os.makedirs("screenshots", exist_ok=True)
    path = f"screenshots/{name}.png"
    try:
        page.screenshot(path=path, full_page=True)
        log(f"截图: {path}")
    except Exception as e:
        log_warn(f"截图失败: {e}")

# ── 离线检测：ping 服务管理页面 ──────────────────────────
def check_server_online(page, identifier: str) -> bool:
    """
    通过 Pterodactyl /api/client/servers/{id}/resources 获取容器真实运行状态。
    current_state: running=在线, offline/stopping/starting=离线或异常
    """
    try:
        result = page.evaluate(f"""async () => {{
            const r = await fetch('/api/client/servers/{identifier}/resources', {{
                headers: {{'Accept': 'application/json'}}
            }});
            return {{status: r.status, body: await r.text()}};
        }}""")
        if result['status'] != 200:
            log_warn(f"  离线检测 HTTP {result['status']}，尝试备用检测...")
            # 备用：检查 suspended 字段
            result2 = page.evaluate(f"""async () => {{
                const r = await fetch('/api/client/servers/{identifier}', {{
                    headers: {{'Accept': 'application/json'}}
                }});
                return {{status: r.status, body: await r.text()}};
            }}""")
            if result2['status'] != 200:
                return None
            data2 = json.loads(result2['body'])
            attrs2 = data2.get('attributes', data2.get('data', {}).get('attributes', {}))
            suspended = attrs2.get('suspended', False)
            log(f"  备用检测: suspended={suspended!r}")
            return False if suspended else None
        data = json.loads(result['body'])
        attrs = data.get('attributes', {})
        current_state = attrs.get('current_state', 'unknown')
        is_suspended  = attrs.get('is_suspended', False)
        log(f"  服务状态: current_state={current_state!r}, is_suspended={is_suspended!r}")
        if is_suspended:
            return False
        if current_state in ('running', 'starting'):
            return True
        if current_state in ('offline', 'stopping', 'stopped'):
            return False
        return None  # unknown状态无法判断
    except Exception as e:
        log_warn(f"  离线检测异常: {e}")
        return None

# ── 启动服务器 ───────────────────────────────────────────
def start_server(page, identifier: str) -> bool:
    """发送 power/start 指令"""
    try:
        result = page.evaluate(f"""async () => {{
            const xsrf = decodeURIComponent(
                document.cookie.split('; ')
                .find(c => c.startsWith('XSRF-TOKEN='))
                ?.split('=')[1] || ''
            );
            const r = await fetch('/api/client/servers/{identifier}/power', {{
                method: 'POST',
                headers: {{
                    'Accept': 'application/json',
                    'Content-Type': 'application/json',
                    'X-XSRF-TOKEN': xsrf
                }},
                body: JSON.stringify({{signal: 'start'}})
            }});
            return {{status: r.status, body: await r.text()}};
        }}""")
        log(f"  start指令 HTTP {result['status']}，body: {result['body'][:100]}")
        return result['status'] in (200, 204)
    except Exception as e:
        log_warn(f"  start指令异常: {e}")
        return False

def wait_until_running(page, identifier: str, max_wait: int = 120, interval: int = 10) -> bool:
    """
    轮询等待服务器变成 running 状态
    max_wait: 最多等待秒数（默认120秒）
    interval: 每次轮询间隔秒数
    """
    elapsed = 0
    while elapsed < max_wait:
        time.sleep(interval)
        elapsed += interval
        state = get_server_state(page, identifier)
        log(f"  等待启动中... {elapsed}s / {max_wait}s，当前状态: {state!r}")
        if state == 'running':
            return True
        if state in ('offline', 'stopped'):
            # 还没启动起来，继续等
            pass
    return False

def get_server_state(page, identifier: str) -> str:
    """获取服务器当前 current_state"""
    try:
        result = page.evaluate(f"""async () => {{
            const r = await fetch('/api/client/servers/{identifier}/resources', {{
                headers: {{'Accept': 'application/json'}}
            }});
            return {{status: r.status, body: await r.text()}};
        }}""")
        if result['status'] != 200:
            return 'unknown'
        data = json.loads(result['body'])
        return data.get('attributes', {}).get('current_state', 'unknown')
    except Exception:
        return 'unknown'

# ── 主流程 ────────────────────────────────────────────────
def run():
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    log(f"代理出口 IP: {get_proxy_ip()}")
    log(f"续期阈值: < {RENEW_THRESHOLD_DAYS*24:.1f} 小时")

    with sync_playwright() as p:
        os.makedirs("screenshots", exist_ok=True)
        browser = p.chromium.launch(
            args=["--no-sandbox", "--disable-setuid-sandbox"],
            proxy={"server": PROXY_SERVER},
        )
        ctx_kwargs = dict(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/148.0.0.0 Safari/537.36",
            locale="zh-CN",
        )
        if ENABLE_VIDEO:
            ctx_kwargs["record_video_dir"]  = "screenshots/"
            ctx_kwargs["record_video_size"] = {"width": 1280, "height": 800}
            log("录屏已开启")
        ctx = browser.new_context(**ctx_kwargs)
        page = ctx.new_page()

        try:
            # ── 登录 ─────────────────────────────────────
            log(f"登录: {LOGIN_URL}")
            page.goto(LOGIN_URL, timeout=60000)
            page.wait_for_load_state("networkidle", timeout=30000)
            screenshot(page, "01_login")

            for sel in ["input[type='email']", "input[name='user']", "input[name='email']",
                        "input[placeholder*='mail']", "input:first-of-type"]:
                try:
                    page.wait_for_selector(sel, timeout=3000)
                    page.fill(sel, EMAIL)
                    break
                except Exception:
                    continue

            for sel in ["input[type='password']", "input[name='password']"]:
                try:
                    page.wait_for_selector(sel, timeout=3000)
                    page.fill(sel, PASSWORD)
                    break
                except Exception:
                    continue

            # captcha
            try:
                page.click("div.auth-captcha-inner", timeout=8000)
                page.wait_for_selector(
                    "div.auth-captcha-box.verified, div.auth-captcha-inner[aria-checked='true']",
                    timeout=10000)
                log("captcha ✅")
            except Exception:
                log_warn("captcha 未检测到 verified，继续")

            for sel in ["button[type='submit']", "button:has-text('Login')",
                        "button:has-text('登录')", "input[type='submit']"]:
                try:
                    page.click(sel, timeout=3000)
                    break
                except Exception:
                    continue

            page.wait_for_load_state("networkidle", timeout=30000)
            try:
                page.wait_for_url(lambda url: "login" not in url, timeout=20000)
                log(f"登录成功 ✅  URL: {page.url}")
            except PWTimeout:
                screenshot(page, "99_login_fail")
                raise RuntimeError(f"登录超时，仍在: {page.url}")

            screenshot(page, "02_dashboard")

            # ── 获取项目列表 ─────────────────────────────
            result = page.evaluate("""async () => {
                const r = await fetch('/api/client', {headers: {'Accept': 'application/json'}});
                return {status: r.status, body: await r.text()};
            }""")
            if result['status'] != 200:
                raise RuntimeError(f"获取项目列表失败 HTTP {result['status']}")

            data     = json.loads(result['body'])
            projects = [item['attributes'] for item in data.get('data', []) if item.get('attributes')]
            log(f"找到 {len(projects)} 个项目")

            if not projects:
                send_all("⚠️ <b>ACLClouds 账号3</b>\n\n项目列表为空，请检查账号！")
                ctx.close(); browser.close()
                return

            # ── 逐项目：离线检测 + 续期 ──────────────────
            renewed_list  = []
            offline_list  = []
            skipped_list  = []
            failed_list   = []

            for project in projects:
                name        = project.get("name", "未知")
                identifier  = project.get("identifier", "")
                raw_expires = project.get("expires_at")
                remaining   = parse_expires(raw_expires)

                log(f"\n── 项目: {name} ──")

                # 离线检测 + 自动启动
                online = check_server_online(page, identifier)
                if online is False:
                    log_warn(f"  ❌ 服务离线，尝试自动启动...")
                    started = start_server(page, identifier)
                    if started:
                        log(f"  start指令已发送，等待服务器启动（最多120秒）...")
                        running = wait_until_running(page, identifier, max_wait=120, interval=10)
                        if running:
                            log(f"  ✅ 服务器已成功启动！")
                            online = True
                        else:
                            log_warn(f"  ⚠️ 等待超时，服务器未能启动")
                            offline_list.append(name)
                    else:
                        log_warn(f"  ❌ start指令发送失败")
                        offline_list.append(name)
                elif online is True:
                    log(f"  ✅ 服务在线")
                else:
                    log_warn(f"  ❓ 服务状态无法判断")

                if remaining is None:
                    failed_list.append(f"{name}（无法解析过期时间）")
                    continue

                remaining_str = fmt_remaining(remaining)
                log(f"  剩余: {remaining_str}（{remaining:.4f} 天）")

                # 判断是否在续期窗口内（< 2小时）
                if remaining >= RENEW_THRESHOLD_DAYS:
                    log(f"  暂不续期（未到2小时窗口）")
                    skipped_list.append(f"{name}（剩余 {remaining_str}）")
                    continue

                # 续期
                log(f"  进入续期窗口，开始续期...")
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
                        new_remaining = None
                        for item in new_result.get('data', []):
                            attrs = item.get('attributes', {})
                            if attrs.get('identifier') == identifier:
                                new_remaining = parse_expires(attrs.get('expires_at'))
                                break
                        if new_remaining is not None:
                            renewed_list.append(
                                f"{name}（{remaining_str} → {fmt_remaining(new_remaining)}）")
                        else:
                            renewed_list.append(f"{name}（续期前剩余 {remaining_str}）")
                        log(f"  续期成功 ✅")
                    else:
                        body = renew_result['body']
                        try:
                            err = json.loads(body).get('error', 'unknown')
                        except Exception:
                            err = body[:80]
                        raise RuntimeError(f"续期失败: {err}")

                except Exception as e:
                    log_error(f"  续期异常: {e}")
                    failed_list.append(f"{name}（{str(e)[:80]}）")

            try:
            screenshot(page, "03_final")
        except Exception:
            pass

        except Exception as e:
            screenshot(page, "99_error")
            ctx.close(); browser.close()
            send_all(f"❌ <b>ACLClouds 账号3 脚本异常</b>\n\n{str(e)[:200]}")
            if ENABLE_VIDEO:
                try:
                    page.video.save_as("screenshots/error_video.webm")
                except Exception:
                    pass
            raise

        if ENABLE_VIDEO:
            try:
                page.video.save_as("screenshots/video.webm")
            except Exception:
                pass
        ctx.close()
        browser.close()

    # ── 汇总推送 ──────────────────────────────────────────
    log("\n" + "="*50)
    log(f"续期: {len(renewed_list)}  跳过: {len(skipped_list)}  "
        f"离线: {len(offline_list)}  失败: {len(failed_list)}")

    # 离线告警（自动启动失败才推送）
    if offline_list:
        lines = ["🚨 <b>ACLClouds 账号3 服务离线且启动失败！</b>", ""]
        lines += [f"• {n}" for n in offline_list]
        lines += ["", "已尝试自动启动但超时，请手动检查！", "ACLClouds Auto Renew"]
        send_all("\n".join(lines))

    # 续期结果推送
    if renewed_list or failed_list:
        lines = []
        if renewed_list:
            lines += ["✅ <b>ACLClouds 账号3 续期成功</b>", ""]
            lines += [f"• {i}" for i in renewed_list]
        if failed_list:
            lines += ["", "❌ 失败项目："]
            lines += [f"• {i}" for i in failed_list]
        if skipped_list:
            lines += ["", "⏳ 暂不续期（未到窗口）："]
            lines += [f"• {i}" for i in skipped_list]
        lines += ["", "ACLClouds Auto Renew"]
        send_all("\n".join(lines))
    else:
        log("无续期操作，无续期推送（离线告警已单独发送）")


if __name__ == "__main__":
    if not EMAIL or not PASSWORD:
        log_error("缺少 ACCOUNT3_EMAIL 或 ACCOUNT3_PASSWORD")
        sys.exit(1)
    try:
        run()
        log("脚本执行完毕")
    except Exception as ex:
        log_error("脚本失败")
        traceback.print_exc()
        sys.exit(1)
