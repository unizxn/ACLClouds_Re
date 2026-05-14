#!/usr/bin/env python3
"""
ACLClouds 自动续期脚本
- 每天检测剩余时间，小于 3 天才续期
- 续期成功后发送 TG 推送
- 未到续期时间不发任何消息
"""

import os
import re
import sys
import json
import traceback
from urllib.request import Request, urlopen
from urllib.parse import urlencode
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# ── 环境变量 ─────────────────────────────────────────────
EMAIL         = os.environ.get("ACLCLOUDS_EMAIL", "").strip()
PASSWORD      = os.environ.get("ACLCLOUDS_PASSWORD", "").strip()
TG_BOT_TOKEN  = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID    = os.environ.get("TG_CHAT_ID", "").strip()

RENEW_THRESHOLD_DAYS = 3   # 剩余天数低于此值才续期

BASE_URL       = "https://dash.aclclouds.com"
PROJECTS_URL   = f"{BASE_URL}/projects"
LOGIN_URL      = f"{BASE_URL}/auth/login"
TIMEOUT        = 60_000
VIEWPORT_W     = 1280
VIEWPORT_H     = 900

SCREENSHOT_DIR = Path("screenshots")
SCREENSHOT_DIR.mkdir(exist_ok=True)

# ── 日志 ─────────────────────────────────────────────────
def log(msg):
    print(f"[INFO] {msg}", flush=True)

def log_warn(msg):
    print(f"[WARN] {msg}", flush=True)

def log_error(msg):
    print(f"[ERROR] {msg}", flush=True)

# ── TG 推送 ──────────────────────────────────────────────
def send_tg(text: str):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        log_warn("TG 未配置，跳过推送")
        return
    try:
        body = json.dumps({"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"}).encode()
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
def parse_expires(text: str):
    """
    解析 '3j 3h' / '2j 12h' / '1j' / '5h' 等格式
    返回 (days: float) 总天数，解析失败返回 None
    """
    if not text:
        return None
    text = text.strip().lower()
    days  = 0.0
    hours = 0.0
    m = re.search(r'(\d+)\s*j', text)
    if m:
        days = float(m.group(1))
    m = re.search(r'(\d+)\s*h', text)
    if m:
        hours = float(m.group(1))
    total = days + hours / 24
    return total if (days or hours) else None

# ── 截图 ─────────────────────────────────────────────────
def screenshot(page, name: str):
    try:
        path = SCREENSHOT_DIR / f"{name}.png"
        page.screenshot(path=str(path), full_page=False)
        log(f"截图已保存: {path}")
    except Exception as e:
        log_warn(f"截图失败: {e}")

# ── 主流程 ────────────────────────────────────────────────
def run():
    if not EMAIL or not PASSWORD:
        raise RuntimeError("缺少 ACLCLOUDS_EMAIL 或 ACLCLOUDS_PASSWORD")

    log("启动浏览器")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": VIEWPORT_W, "height": VIEWPORT_H})
        page.set_default_timeout(TIMEOUT)

        try:
            # ── 登录 ─────────────────────────────────────
            log(f"打开登录页: {LOGIN_URL}")
            page.goto(LOGIN_URL, wait_until="domcontentloaded")
            # 等待邮箱输入框出现，确认页面 JS 已渲染完成
            page.wait_for_selector('#username, input[name="username"]', timeout=TIMEOUT)
            page.wait_for_timeout(1000)

            # 填写邮箱（实际 HTML: id="username" type="text" name="username"）
            page.fill('#username, input[name="username"]', EMAIL)
            # 填写密码
            page.fill('input[type="password"], input[name="password"]', PASSWORD)
            page.wait_for_timeout(500)

            # 处理验证码（实际 HTML: div.auth-captcha-checkbox role="checkbox"，不是 input）
            try:
                captcha = page.locator('div.auth-captcha-checkbox, [role="checkbox"]').first
                if captcha.is_visible(timeout=5000):
                    captcha.click()
                    log("已点击验证码复选框")
                    page.wait_for_timeout(2000)
            except Exception:
                log_warn("未找到验证码复选框，跳过")

            # 点击登录
            page.click('button[type="submit"], button:has-text("Sign in")')
            page.wait_for_url(lambda u: "/auth/login" not in u, timeout=30000)
            log("登录成功")
            screenshot(page, "login-success")

            # ── 打开项目页 ────────────────────────────────
            log(f"打开项目页: {PROJECTS_URL}")
            page.goto(PROJECTS_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            screenshot(page, "projects-page")

            # ── 找所有项目卡片 ────────────────────────────
            # 每个项目卡片包含 "Expires in" 和 "Renew" 按钮
            cards = page.locator('div:has(button:has-text("Renew"))').all()
            if not cards:
                log_warn("未找到任何项目卡片（含 Renew 按钮）")
                return

            log(f"发现 {len(cards)} 个项目")

            renewed_list  = []
            skipped_list  = []
            failed_list   = []

            for i, card in enumerate(cards):
                idx = i + 1
                try:
                    # 项目名称
                    try:
                        name = card.locator('h2, h3, [class*="name"], [class*="title"]').first.inner_text(timeout=3000).strip()
                    except Exception:
                        name = f"项目#{idx}"

                    # 读取剩余时间文字
                    expires_text = ""
                    try:
                        # 找包含 "Expires" 或 "j" 字样的元素
                        exp_el = card.locator('text=/Expires/i').first
                        parent = exp_el.locator("..").first
                        expires_text = parent.inner_text(timeout=3000).strip()
                    except Exception:
                        pass

                    # 尝试直接找时间格式 如 "3j 3h"
                    if not expires_text:
                        try:
                            all_text = card.inner_text(timeout=3000)
                            m = re.search(r'\d+j\s*\d*h?', all_text)
                            if m:
                                expires_text = m.group(0)
                        except Exception:
                            pass

                    log(f"[{name}] 剩余时间文字: '{expires_text}'")
                    remaining = parse_expires(expires_text)

                    if remaining is None:
                        log_warn(f"[{name}] 无法解析剩余时间，跳过")
                        failed_list.append(f"{name}（解析失败: {expires_text!r}）")
                        continue

                    log(f"[{name}] 剩余 {remaining:.2f} 天")

                    if remaining >= RENEW_THRESHOLD_DAYS:
                        log(f"[{name}] 剩余 {remaining:.2f} 天 ≥ {RENEW_THRESHOLD_DAYS} 天，无需续期")
                        skipped_list.append(f"{name}（剩余 {remaining:.1f} 天）")
                        continue

                    # ── 执行续期 ──────────────────────────
                    log(f"[{name}] 剩余不足 {RENEW_THRESHOLD_DAYS} 天，开始续期...")
                    renew_btn = card.locator('button:has-text("Renew")').first
                    renew_btn.click()
                    page.wait_for_timeout(5000)

                    # 确认弹窗（如果有）
                    try:
                        confirm = page.locator('button:has-text("Confirm"), button:has-text("Yes"), button:has-text("OK")').first
                        if confirm.is_visible(timeout=3000):
                            confirm.click()
                            page.wait_for_timeout(3000)
                    except Exception:
                        pass

                    screenshot(page, f"renew-{idx}")

                    # 读取续期后状态
                    page.goto(PROJECTS_URL, wait_until="domcontentloaded")
                    page.wait_for_timeout(3000)

                    # 重新找该卡片
                    new_cards = page.locator('div:has(button:has-text("Renew"))').all()
                    new_expires = ""
                    if i < len(new_cards):
                        try:
                            all_text = new_cards[i].inner_text(timeout=3000)
                            m = re.search(r'\d+j\s*\d*h?', all_text)
                            if m:
                                new_expires = m.group(0)
                        except Exception:
                            pass

                    log(f"[{name}] 续期成功！续期后剩余: {new_expires or '?'}")
                    renewed_list.append(f"{name}（续期前 {remaining:.1f} 天 → {new_expires or '?'}）")

                except Exception as e:
                    log_error(f"[项目#{idx}] 处理异常: {e}")
                    failed_list.append(f"项目#{idx}（异常: {str(e)[:60]}）")
                    screenshot(page, f"error-{idx}")

            # ── 汇总 & 推送 ──────────────────────────────
            log("=" * 50)
            log(f"续期成功: {len(renewed_list)} 个")
            log(f"无需续期: {len(skipped_list)} 个")
            log(f"失败/异常: {len(failed_list)} 个")

            # 只有续期成功才发 TG
            if renewed_list:
                lines = ["✅ <b>ACLClouds 自动续期成功</b>", ""]
                for item in renewed_list:
                    lines.append(f"• {item}")
                if failed_list:
                    lines.append("")
                    lines.append("⚠️ 以下项目失败：")
                    for item in failed_list:
                        lines.append(f"• {item}")
                lines.append("")
                lines.append("ACLClouds Auto Renew")
                send_tg("\n".join(lines))
            elif failed_list:
                # 有失败但无成功，也推送告警
                lines = ["❌ <b>ACLClouds 续期失败</b>", ""]
                for item in failed_list:
                    lines.append(f"• {item}")
                lines.append("")
                lines.append("ACLClouds Auto Renew")
                send_tg("\n".join(lines))
            else:
                log("无续期操作，不发送 TG 推送")

        except Exception as e:
            log_error(f"主流程异常: {e}")
            try:
                screenshot(page, "fatal-error")
            except Exception:
                pass
            send_tg(f"❌ <b>ACLClouds 续期脚本异常</b>\n\n{str(e)[:200]}\n\nACLClouds Auto Renew")
            raise
        finally:
            browser.close()


if __name__ == "__main__":
    try:
        run()
        log("脚本执行完毕")
    except Exception:
        log_error("脚本失败")
        traceback.print_exc()
        sys.exit(1)
