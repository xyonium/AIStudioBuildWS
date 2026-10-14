import os
import time
from urllib.parse import urlparse

from utils.common import clean_env_value
from browser.window_placer import place_window

# 停车时把实例导航到的无害页面：离开 AI Studio，停止一切脚本交互，
# 避免在"已认输"状态下继续触发 Google 反爬。
PARK_URL = "https://www.google.com/"


def _is_back_at_target(page_url: str, target_hosts, expected_path: str = "") -> bool:
    """判断人工是否已把页面开回目标域名（可选地比对路径）。"""
    host = urlparse(page_url).hostname or ""
    if not any(host == h or host.endswith("." + h) for h in target_hosts):
        return False
    if not expected_path:
        return True
    return expected_path in (urlparse(page_url).path or "")


def park_and_wait(page, diagnostic_tag, logger, is_ready, shutdown_event=None):
    """
    实例认输但浏览器还活着时调用：
    1. 把页面导航到 PARK_URL（google.com），停止所有自动交互
    2. 把窗口摆回本实例槽位，方便人工从 VNC 找到它
    3. 每 10 秒轮询 URL，等待人工把地址开回 AI Studio；检测到后返回 True
       （浏览器/页面仍是原对象，调用方继续走完初始化即可接管）

    PARK_TIMEOUT 环境变量控制最长等待秒数：0（默认）= 永久等人工，
    之后加 webhook 通知时再调短。返回 False 只有两种情况：超时或收到全局关闭信号。

    注意：本函数绝不自动导航回 AI Studio——从停车页离开的动作必须来自人工，
    否则等同于脚本反复触发报错页面，正是反爬要防的行为。
    """
    manual_timeout = int(clean_env_value(os.getenv("MANUAL_LOGIN_TIMEOUT")) or 600)
    try:
        park_timeout = int(clean_env_value(os.getenv("PARK_TIMEOUT")) or 0)
    except (ValueError, TypeError):
        park_timeout = 0

    poll_seconds = 10

    # ---- 第 1 步：停车到无害页 ----
    try:
        page.goto(PARK_URL, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        # 停车失败（如网络已断）：页面大概率已经不在 AI Studio，同样不构成
        # 反爬风险。记录后继续轮询——人工仍可通过地址栏恢复。
        logger.warning(f"[{diagnostic_tag}] 停车导航到 {PARK_URL} 失败: {e}（仍进入等待，URL 状态未知）")

    # ---- 第 2 步：窗口归位 ----
    try:
        place_window(diagnostic_tag, logger, attempts=2, interval=1)
    except Exception as e:
        logger.warning(f"[{diagnostic_tag}] 停车后窗口归位失败（不影响等待）: {e}")

    # ---- 第 3 步：等待人工 ----
    if park_timeout > 0:
        deadline = time.time() + park_timeout
        logger.warning(
            f"[{diagnostic_tag}] 已停车到 {PARK_URL}，等待人工将地址开回 AI Studio "
            f"(上限 {park_timeout} 秒，可通过 PARK_TIMEOUT 调整)..."
        )
    else:
        deadline = None
        logger.warning(
            f"[{diagnostic_tag}] 已停车到 {PARK_URL}，无限期等待人工将地址开回 AI Studio "
            f"(PARK_TIMEOUT=0；脚本不会自动触碰目标站)..."
        )

    announcement_interval = manual_timeout
    next_announce = time.time() + announcement_interval

    while True:
        # 全局关闭优先于一切
        if shutdown_event and shutdown_event.is_set():
            logger.info(f"[{diagnostic_tag}] 停车等待期间收到全局关闭信号，退出")
            return False

        if deadline and time.time() >= deadline:
            logger.error(f"[{diagnostic_tag}] 停车等待超过 {park_timeout} 秒，放弃该实例")
            try:
                from utils.paths import logs_dir
                page.screenshot(path=os.path.join(str(logs_dir()), f"FAIL_park_timeout_{diagnostic_tag}.png"))
            except Exception:
                pass
            return False

        # 人工回来了？
        try:
            current_url = page.url or ""
        except Exception as e:
            logger.warning(f"[{diagnostic_tag}] 停车轮询读取 URL 失败: {e}，继续等待")
            current_url = ""

        if current_url and _is_back_at_target(current_url, ("ai.studio", "aistudio.google.com")) and is_ready():
            logger.warning(f"[{diagnostic_tag}] 检测到人工已将页面开回 AI Studio，准备接管并重新初始化")
            return True

        # 每过 MANUAL_LOGIN_TIMEOUT 秒重新播报一次，提醒人工这个实例在等人
        if time.time() >= next_announce:
            logger.warning(
                f"[{diagnostic_tag}] 仍在停车等待人工：请通过 VNC (http://IP:6080) "
                f"把该窗口地址开回 AI Studio"
            )
            next_announce = time.time() + announcement_interval

        # 可中断的 10 秒轮询
        for _ in range(poll_seconds):
            if shutdown_event and shutdown_event.is_set():
                logger.info(f"[{diagnostic_tag}] 停车等待期间收到全局关闭信号，退出")
                return False
            time.sleep(1)
