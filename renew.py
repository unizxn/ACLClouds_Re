#!/usr/bin/env python3
"""
ACLClouds 自动续期脚本 (纯 API 版)
登录流程：
  1. GET /auth/login  → 从 HTML 里提取 captcha_token (已通过 /auth/captcha 模拟绕过)
  2. POST /auth/login → { user, password, captcha_answer: "human", captcha_token }
  3. 拿到 session cookie，后续请求全带上

项目列表 API: GET /api/client
续期 API: 需要手动抓包确定（脚本内置了几个常见候选）
"""

import os
import re
import sys
import json
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

RENEW_THRESHOLD_DAYS = 3

BASE_URL  = "https://dash.aclclouds.com"
LOGIN_URL = f"{BASE_URL}/auth/login"
API_BASE  = f"{BASE_URL}/api"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/148.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Origin": BASE_URL,
    "Referer": LOGIN_URL,
    "x-requested-with": "XMLHttpRequest",
}

# ── 日志 ─────────────────────────────────────────────────
def log(msg):       print(f"[INFO] {msg}", flush=True)
def log_warn(msg):  print(f"[WARN] {msg}", flush=True)
def log_error(msg): print(f"[ERROR] {msg}", flush=True)

# ── TG 推送 ──────────────────────────────────────────────
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

# ── 解析剩余时间 ──────────────────────────────────────────
def parse_expires(text):
    """
    支持：ISO 日期字符串 / '3j 3h' / '2d 12h' / 纯数字秒
    返回 float 天数，失败返回 None
    """
    if text is None:
        return None
    s = str(text).strip()

    # ISO 日期
    if re.search(r'\d{4}-\d{2}-\d{2}', s):
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return (dt - datetime.now(timezone.utc)).total_seconds() / 86400
        except Exception:
            pass

    # 纯数字（秒）
    try:
        return float(s) / 86400
    except Exception:
        pass

    # '3j 3h' / '2d 12h'
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
        """从 cookie 里取 XSRF-TOKEN，URL decode 后放进请求头"""
        from urllib.parse import unquote
        xsrf = self.session.cookies.get("XSRF-TOKEN", "")
        if xsrf:
            decoded = unquote(xsrf)
            self.session.headers["x-xsrf-token"] = decoded
            log(f"x-xsrf-token 已设置: {decoded[:30]}...")

    def _get_captcha_token(self):
        """
        通过模拟鼠标行为绕过验证码
        """
        log("GET 登录页，获取 XSRF-TOKEN ...")
        r = self.session.get(LOGIN_URL, timeout=20)
        r.raise_for_status()
        self._set_xsrf()

        captcha_url = f"{BASE_URL}/auth/captcha"
        fake_behavior = {
            "mouse_movements": 320,
            "mouse_distance":  5800,
            "clicks":          1,
            "key_presses":     3,
            "elapsed_ms":      45000,
        }
        log(f"POST {captcha_url} ...")
        cr = self.session.post(captcha_url, json=fake_behavior, timeout=20)
        log(f"  -> HTTP {cr.status_code}")

        if cr.status_code == 200:
            data = cr.json()
            log(f"  -> 响应: {data}")
            self._set_xsrf()
            token = data.get("token") or data.get("captcha_token")
            if token and data.get("passed"):
                log(f"captcha 通过，token: {str(token)[:20]}...")
                return str(token)
            elif token:
                log_warn("captcha passed=false，仍尝试用 token 登录")
                return str(token)
            else:
                log_warn(f"captcha 响应无 token: {data}")
                return ""
        else:
            log_warn(f"POST /auth/captcha 失败 HTTP {cr.status_code}，不带 token 尝试登录")
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
        r = self.session.post(
            LOGIN_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=20,
        )
        log(f"登录响应: HTTP {r.status_code}")

        # 更新 XSRF-TOKEN
        self._set_xsrf()

        if r.status_code == 200:
            if self.session.cookies.get("aclclouds_session"):
                log("登录成功 ✅（aclclouds_session 已设置）")
                return True
            try:
                data = r.json()
                log(f"响应 JSON keys: {list(data.keys())}")
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

        log_error(f"登录失败，响应: {r.text[:300]}")
        raise RuntimeError(f"登录失败，HTTP {r.status_code}")

    def get_projects(self):
        """获取项目列表 - 使用 /api/client"""
        url = f"{BASE_URL}/api/client"
        log(f"GET {url}")
        r = self.session.get(url, timeout=20)
        log(f"  → HTTP {r.status_code}")
        if r.status_code != 200:
            raise RuntimeError(f"获取项目列表失败 HTTP {r.status_code}")
        data = r.json()
        # 响应结构: {"object":"list", "data":[{"object":"server","attributes":{...}}]}
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
        """续期单个项目 - 内置常见续期端点候选"""
        # 可能的ID字段
        pid = (
            project.get("identifier") or
            project.get("internal_id") or
            project.get("id") or
            project.get("uuid")
        )
        if not pid:
            raise ValueError(f"无法获取项目 ID，字段: {list(project.keys())}")

        # 候选续期端点（按常见模式，需要根据实际抓包调整）
        candidates = [
            (f"{API_BASE}/servers/{pid}/renew", "POST"),
            (f"{API_BASE}/client/{pid}/renew", "POST"),
            (f"{API_BASE}/projects/{pid}/renew", "POST"),
            (f"{API_BASE}/renew", "POST"),  # body: {"server_id": pid}
        ]
        for ep, method in candidates:
            try:
                body = {}
                if ep.endswith("/renew") and pid not in ep:
                    body = {"server_id": pid}  # 常见传参方式
                log(f"  尝试 {method} {ep}")
                fn = self.session.post if method == "POST" else self.session.put
                r = fn(ep, json=body, timeout=20)
                log(f"    → HTTP {r.status_code}")
                if r.status_code in (200, 201, 204):
                    return True
                if r.status_code == 404:
                    continue
            except Exception as e:
                log_warn(f"    → 异常: {e}")

        raise RuntimeError(f"项目 {pid} 所有续期端点均失败，请手动抓包续期接口")

# ── 主流程 ────────────────────────────────────────────────
def run():
    if not EMAIL or not PASSWORD:
        raise RuntimeError("缺少环境变量 ACLCLOUDS_EMAIL 或 ACLCLOUDS_PASSWORD")

    api = ACLCloudsAPI()
    api.login(EMAIL, PASSWORD)

    projects = api.get_projects()
    if not projects:
        log_warn("项目列表为空，无需操作")
        return

    log(f"共 {len(projects)} 个项目")

    renewed_list = []
    skipped_list = []
    failed_list  = []

    for project in projects:
        name = (
            project.get("name") or project.get("title") or
            project.get("label") or project.get("hostname") or
            str(project.get("id", "未知"))
        )
        raw_expires = (
            project.get("expires_at") or project.get("expiry") or
            project.get("expiration") or project.get("expire_at") or
            project.get("expires") or project.get("remaining") or
            project.get("time_left") or project.get("timeLeft") or
            project.get("remainingTime") or project.get("expiresAt")
        )

        log(f"[{name}] 过期数据: {raw_expires!r}")
        remaining = parse_expires(raw_expires)

        if remaining is None:
            log_warn(
                f"[{name}] 无法解析剩余时间\n"
                f"  完整数据: {json.dumps(project, ensure_ascii=False, default=str)[:400]}"
            )
            failed_list.append(f"{name}（无法解析过期时间）")
            continue

        log(f"[{name}] 剩余 {remaining:.2f} 天")

        if remaining >= RENEW_THRESHOLD_DAYS:
            log(f"[{name}] 无需续期")
            skipped_list.append(f"{name}（剩余 {remaining:.1f} 天）")
            continue

        log(f"[{name}] 开始续期...")
        try:
            api.renew_project(project)
            log(f"[{name}] ✅ 续期成功")
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
        send_tg("\n".join(lines))
    elif failed_list:
        lines = ["❌ <b>ACLClouds 续期失败</b>", ""]
        lines += [f"• {i}" for i in failed_list]
        lines += ["", "ACLClouds Auto Renew"]
        send_tg("\n".join(lines))
    else:
        log("无续期操作，不发送 TG 推送")

if __name__ == "__main__":
    try:
        run()
        log("脚本执行完毕")
    except Exception:
        log_error("脚本失败")
        traceback.print_exc()
        send_tg(
            f"❌ <b>ACLClouds 续期脚本异常</b>\n\n"
            f"{traceback.format_exc()[:300]}\n\nACLClouds Auto Renew"
        )
        sys.exit(1)
