#!/usr/bin/env python3
"""
ACLClouds 自动续期脚本 (纯 API 版)
支持 Telegram 和 wxpusher 双推送
续期成功后自动重新获取新的过期时间并打印
"""

import os
import re
import sys
import json
import time
import traceback
from urllib.request import Request, urlopen

try:
    import requests
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests", "-q"])
    import requests

# ── 环境变量 ─────────────────────────────────────────────
EMAIL        = os.environ.get("ACLCLOUDS_EMAIL", "").strip()
PASSWORD     = os.environ.get("ACLCLOUDS_PASSWORD", "").strip()

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID   = os.environ.get("TG_CHAT_ID", "").strip()

WXPUSHER_APPTOKEN = os.environ.get("WXPUSHER_APPTOKEN", "").strip()
WXPUSHER_UID      = os.environ.get("WXPUSHER_UID", "").strip()

RENEW_THRESHOLD_DAYS = 2

BASE_URL  = "https://dash.aclclouds.com"
LOGIN_URL = f"{BASE_URL}/auth/login"
API_BASE  = f"{BASE_URL}/api"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/148.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": f"{BASE_URL}/projects",
}

# 登录/captcha 请求额外携带的头，模拟浏览器同域提交
LOGIN_EXTRA_HEADERS = {
    "Origin": BASE_URL,
    "x-requested-with": "XMLHttpRequest",
    "Content-Type": "application/json",
}

# ── 日志 ─────────────────────────────────────────────────
def log(msg):       print(f"[INFO] {msg}", flush=True)
def log_warn(msg):  print(f"[WARN] {msg}", flush=True)
def log_error(msg): print(f"[ERROR] {msg}", flush=True)

def get_outbound_ip():
    try:
        import urllib.request as _ur
        data = _ur.urlopen("https://cloudflare.com/cdn-cgi/trace", timeout=5).read().decode()
        for line in data.splitlines():
            if line.startswith("ip="):
                return line.strip()
        return "ip=未知"
    except Exception as e:
        return f"ip=获取失败({e})"

def mask_cookies(cookies: dict) -> dict:
    return {k: v[:6] + "***" for k, v in cookies.items()}

# ── 截图 ─────────────────────────────────────────────────
def take_screenshot(cookies: dict, label: str = "projects"):
    try:
        from playwright.sync_api import sync_playwright
        os.makedirs("screenshots", exist_ok=True)
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox", "--disable-setuid-sandbox"])
            ctx = browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent=HEADERS["User-Agent"],
            )
            for name, value in cookies.items():
                try:
                    ctx.add_cookies([{
                        "name": name, "value": value,
                        "domain": "dash.aclclouds.com", "path": "/",
                    }])
                except Exception:
                    pass
            page = ctx.new_page()
            page.goto(f"{BASE_URL}/projects", timeout=30000)
            page.wait_for_timeout(3000)
            path = f"screenshots/{label}.png"
            page.screenshot(path=path, full_page=True)
            log(f"截图已保存: {path}")
            browser.close()
    except Exception as e:
        log_warn(f"截图失败（不影响续期）: {e}")

# ── 推送函数 ──────────────────────────────────────────────
def send_tg(text: str):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        log_warn("TG 未配置，跳过推送")
        return
    try:
        body = json.dumps({
            "chat_id": TG_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
        }).encode()
        req = Request(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        urlopen(req, timeout=15)
        log("TG 推送成功")
    except Exception as e:
        log_warn(f"TG 推送失败: {e}")

def send_wxpusher(text: str):
    if not WXPUSHER_APPTOKEN or not WXPUSHER_UID:
        log_warn("wxpusher 未配置，跳过推送")
        return
    try:
        url = "https://wxpusher.zjiecode.com/api/send/message"
        payload = {
            "appToken": WXPUSHER_APPTOKEN,
            "content": text,
            "summary": "ACLClouds 续期通知",
            "contentType": 1,
            "uids": [WXPUSHER_UID],
        }
        req = Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        resp = urlopen(req, timeout=15)
        result = json.loads(resp.read().decode())
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

# ── API 封装 ──────────────────────────────────────────────
class ACLCloudsAPI:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    def _set_xsrf(self):
        from urllib.parse import unquote
        xsrf = self.session.cookies.get("XSRF-TOKEN", "")
        if xsrf:
            decoded = unquote(xsrf)
            self.session.headers["x-xsrf-token"] = decoded
            log(f"x-xsrf-token 已设置: {decoded[:6]}***（已脱敏）")

    def _get_captcha_token(self):
        log(f"[网络] 出口 IP: {get_outbound_ip()}")
        log("GET 登录页，获取 XSRF-TOKEN ...")
        r = self.session.get(LOGIN_URL, timeout=60)
        r.raise_for_status()
        self._set_xsrf()
        log(f"登录页 cookie: {mask_cookies(dict(self.session.cookies))}")

        captcha_url = f"{BASE_URL}/auth/captcha"
        fake_behavior = {
            "mouse_movements": 320,
            "mouse_distance":  5800,
            "clicks":          1,
            "key_presses":     3,
            "elapsed_ms":      45000,
        }
        log(f"POST {captcha_url} ...")
        cr = self.session.post(captcha_url, json=fake_behavior, timeout=60, headers=LOGIN_EXTRA_HEADERS)
        log(f"  -> HTTP {cr.status_code}")
        if cr.status_code == 200:
            data = cr.json()
            log(f"  -> 响应: {{'passed': {data.get('passed')}, 'token': '{str(data.get('token', ''))[:4]}***（已脱敏）'}}")
            self._set_xsrf()
            token = data.get("token") or data.get("captcha_token")
            if token and data.get("passed"):
                log(f"captcha 通过，token: {str(token)[:4]}***（已脱敏）")
                return str(token)
            elif token:
                log_warn("captcha passed=false，仍尝试用 token 登录")
                return str(token)
            else:
                log_warn(f"captcha 响应无 token: {data}")
                return ""
        else:
            log_warn(f"POST /auth/captcha 失败 HTTP {cr.status_code}，body: {cr.text[:200]}")
            return ""

    def login(self, email, password):
        captcha_token = self._get_captcha_token()
        payload = {
            "user":           email,
            "password":       password,
            "captcha_answer": "human",
            "captcha_token":  captcha_token,
        }
        log(f"POST {LOGIN_URL}")
        r = self.session.post(LOGIN_URL, json=payload, headers=LOGIN_EXTRA_HEADERS, timeout=60)
        log(f"登录响应: HTTP {r.status_code}")
        log(f"登录响应头: {dict(r.headers)}")
        log(f"登录响应 Set-Cookie: {r.headers.get('Set-Cookie', '无')}")
        self._set_xsrf()

        if r.status_code == 200:
            log(f"登录后 session cookie: {mask_cookies(dict(self.session.cookies))}")
            if self.session.cookies.get("aclclouds_session"):
                log("登录成功 ✅（aclclouds_session 已设置）")
                return True
            try:
                data = r.json()
                log(f"登录响应体: {str(data)[:200]}")
                if data.get("token") or data.get("access_token"):
                    tok = data.get("token") or data.get("access_token")
                    self.session.headers["Authorization"] = f"Bearer {tok}"
                    log("登录成功 ✅（Bearer token）")
                    return True
                if r.cookies or self.session.cookies:
                    log("登录成功 ✅（Cookie 模式）")
                    return True
            except Exception:
                pass
        log_error(f"登录失败，响应体: {r.text[:300]}")
        raise RuntimeError(f"登录失败，HTTP {r.status_code}")

    def get_projects(self):
        url = f"{BASE_URL}/api/client"
        log(f"[网络] 出口 IP: {get_outbound_ip()}")
        log(f"GET {url}")
        log(f"  → 请求 cookie: {mask_cookies(dict(self.session.cookies))}")
        log(f"  → 请求头 x-xsrf-token: {self.session.headers.get('x-xsrf-token', '未设置')[:10]}***")
        r = self.session.get(url, timeout=60)
        log(f"  → HTTP {r.status_code}")
        if r.status_code != 200:
            log_warn(f"  → 响应体: {r.text[:500]}")
            log_warn(f"  → 响应头: {dict(r.headers)}")
            raise RuntimeError(f"获取项目列表失败 HTTP {r.status_code}")
        data = r.json()
        if not isinstance(data, dict) or "data" not in data:
            raise RuntimeError(f"意外的响应结构: {data}")
        projects = []
        for item in data["data"]:
            attrs = item.get("attributes")
            if attrs:
                projects.append(attrs)
        log(f"  → 找到 {len(projects)} 个项目")
        return projects

    def renew_project(self, project):
        identifier = project.get("identifier")
        if not identifier:
            raise ValueError(f"无法获取 identifier，字段: {list(project.keys())}")

        url = f"{API_BASE}/client/servers/{identifier}/upgrade/renew"
        log(f"POST {url}")
        log(f"  → 请求 cookie: {mask_cookies(dict(self.session.cookies))}")
        r = self.session.post(url, timeout=60)
        log(f"  → HTTP {r.status_code}")
        log(f"  → 响应体: {r.text[:300]}")

        if r.status_code == 200:
            log("  续期请求成功，等待2秒后重新获取项目信息...")
            time.sleep(2)
            new_data = self.session.get(f"{BASE_URL}/api/client").json()
            for item in new_data.get("data", []):
                attrs = item.get("attributes", {})
                if attrs.get("identifier") == identifier:
                    new_expires = attrs.get("expires_at")
                    log(f"  新的过期时间: {new_expires}")
                    return new_expires
            log_warn("  未能获取到新的过期时间")
            return None
        else:
            try:
                err = r.json()
                log_warn(f"续期失败: {err}")
                raise RuntimeError(f"续期失败: {err.get('error', 'unknown')}")
            except (json.JSONDecodeError, KeyError):
                log_warn(f"续期失败，HTTP {r.status_code}, body: {r.text[:200]}")
                raise RuntimeError(f"续期失败，HTTP {r.status_code}")

# ── 主流程 ────────────────────────────────────────────────
def run():
    if not EMAIL or not PASSWORD:
        raise RuntimeError("缺少环境变量 ACLCLOUDS_EMAIL 或 ACLCLOUDS_PASSWORD")

    api = ACLCloudsAPI()
    for _attempt in range(3):
        try:
            api.login(EMAIL, PASSWORD)
            break
        except Exception as e:
            if _attempt < 2:
                log_warn(f"登录失败，第 {_attempt + 1} 次重试... ({e})")
                time.sleep(5)
            else:
                raise

    projects = api.get_projects()
    if not projects:
        log_warn("项目列表为空，无需操作")
        return

    log(f"共 {len(projects)} 个项目")

    take_screenshot(dict(api.session.cookies), label="before_renew")

    renewed_list = []
    skipped_list = []
    failed_list  = []

    for project in projects:
        name = project.get("name", "未知项目")
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
            new_expires = api.renew_project(project)
            log(f"[{name}] ✅ 续期成功")
            if new_expires:
                new_remaining = parse_expires(new_expires)
                log(f"[{name}] 续期后剩余天数: {new_remaining:.2f} 天（新过期时间: {new_expires}）")
                renewed_list.append(f"{name}（续期前剩余 {remaining:.1f} 天，续期后剩余 {new_remaining:.1f} 天）")
            else:
                renewed_list.append(f"{name}（续期前剩余 {remaining:.1f} 天）")
        except Exception as e:
            log_error(f"[{name}] 续期失败: {e}")
            failed_list.append(f"{name}（{str(e)[:80]}）")

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

if __name__ == "__main__":
    try:
        run()
        log("脚本执行完毕")
    except Exception:
        log_error("脚本失败")
        traceback.print_exc()
