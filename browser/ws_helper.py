import time
import random
from playwright.sync_api import Page, FrameLocator


def get_preview_frame(page: Page, logger=None) -> FrameLocator:
    """
    获取预览iframe的FrameLocator。
    """
    try:
        # 查找title为"Preview"的iframe
        frame = page.frame_locator('iframe[title="Preview"]')
        return frame
    except Exception as e:
        if logger:
            logger.warning(f"获取Preview iframe失败: {e}")
        return None


def get_ws_status(page: Page, logger=None) -> str:
    """
    获取页面中WS连接状态（在iframe内部）。
    返回: CONNECTED, IDLE, CONNECTING 或 UNKNOWN
    """
    try:
        frame = get_preview_frame(page, logger)
        if not frame:
            return "UNKNOWN"
        
        # 在iframe内查找包含 "WS:" 的状态文本元素
        # 根据截图，状态显示为 "WS: CONNECTED" 等格式
        status_element = frame.locator('text=/WS:\\s*(CONNECTED|IDLE|CONNECTING)/i').first
        if status_element.is_visible(timeout=3000):
            text = status_element.text_content()
            if text:
                if "CONNECTED" in text.upper():
                    return "CONNECTED"
                elif "IDLE" in text.upper():
                    return "IDLE"
                elif "CONNECTING" in text.upper():
                    return "CONNECTING"
        return "UNKNOWN"
    except Exception as e:
        if logger:
            logger.warning(f"获取WS状态时出错: {e}")
        return "UNKNOWN"


def click_disconnect(page: Page, logger=None) -> bool:
    """
    点击Disconnect按钮断开WS连接（在iframe内部）。
    优先使用 Playwright 的 locator 方法，兜底使用 JavaScript 直接触发。
    """
    try:
        frame = get_preview_frame(page, logger)
        if not frame:
            return False

        disconnect_btn = frame.locator('button:has-text("Disconnect")')
        if disconnect_btn.count() > 0 and disconnect_btn.first.is_visible(timeout=3000):
            try:
                disconnect_btn.first.click(timeout=5000)
                if logger:
                    logger.info("已点击 Disconnect 按钮")
                time.sleep(1)
                return True
            except Exception as click_err:
                if logger:
                    logger.debug(f"Playwright Disconnect 点击失败，尝试 JS 兜底: {click_err}")
                # JS 兜底
                page.evaluate("""
                    () => {
                        const iframe = document.querySelector('iframe[title="Preview"]');
                        if (iframe && iframe.contentDocument) {
                            const btn = Array.from(iframe.contentDocument.querySelectorAll('button'))
                                .find(b => b.textContent.includes('Disconnect'));
                            if (btn) btn.click();
                        }
                    }
                """)
                time.sleep(1)
                return True
        if logger:
            logger.warning("未找到可见的 Disconnect 按钮")
        return False
    except Exception as e:
        if logger:
            logger.warning(f"点击 Disconnect 按钮失败: {e}")
        return False


def click_connect(page: Page, logger=None) -> bool:
    """
    点击Connect按钮建立WS连接（在iframe内部）。
    如果按钮处于 disabled 状态（CONNECTING 过渡态），会等待最多15秒直到变为可用。
    兜底使用 JavaScript 直接触发。
    """
    try:
        frame = get_preview_frame(page, logger)
        if not frame:
            return False

        connect_btn = frame.locator('button:has-text("Connect")')
        if connect_btn.count() == 0:
            if logger:
                logger.warning("未找到 Connect 按钮")
            return False

        if not connect_btn.first.is_visible(timeout=3000):
            if logger:
                logger.warning("Connect 按钮不可见")
            return False

        # 等待按钮变为 enabled 状态 (处理 CONNECTING 过渡态)
        try:
            connect_btn.first.wait_for(state='attached', timeout=15000)
            start = time.time()
            while time.time() - start < 15:
                is_disabled = connect_btn.first.is_disabled()
                if not is_disabled:
                    break
                if logger:
                    logger.debug("Connect 按钮当前为 disabled，等待可用...")
                time.sleep(1)
        except Exception:
            if logger:
                logger.warning("等待 Connect 按钮变为可用超时，尝试强制点击")

        try:
            connect_btn.first.click(timeout=5000)
        except Exception as click_err:
            if logger:
                logger.debug(f"Playwright Connect 点击失败，尝试 JS 兜底: {click_err}")
            page.evaluate("""
                () => {
                    const iframe = document.querySelector('iframe[title="Preview"]');
                    if (iframe && iframe.contentDocument) {
                        const btn = Array.from(iframe.contentDocument.querySelectorAll('button'))
                            .find(b => b.textContent.includes('Connect'));
                        if (btn && !btn.disabled) btn.click();
                    }
                }
            """)
        if logger:
            logger.info("已点击 Connect 按钮")
        time.sleep(1)
        return True
    except Exception as e:
        if logger:
            logger.warning(f"点击 Connect 按钮失败: {e}")
        return False


def wait_for_ws_connected(page: Page, logger=None, timeout: int = 30) -> bool:
    """
    等待WS状态变为CONNECTED。
    """
    start_time = time.time()
    while time.time() - start_time < timeout:
        status = get_ws_status(page, logger)
        if status == "CONNECTED":
            return True
        time.sleep(1)
    return False


def reconnect_ws(page: Page, logger=None) -> str:
    """
    智能执行WS重连流程，并返回最终WS状态。
    处理UNKNOWN/CONNECTING等过渡态：
    - UNKNOWN/CONNECTING → 短暂等待，若恢复就不操作
    - IDLE → 直接 Connect
    - CONNECTED 但Disconnect按钮不存在 → 说明可能是误报，跳过
    - 其他 → 先 Disconnect 再 Connect
    """
    if logger:
        logger.info("开始执行WS重连流程...")

    # 先关闭 interaction-modal 遮罩层（如果存在）
    dismiss_interaction_modal(page, logger)

    # 获取当前状态
    current_status = get_ws_status(page, logger)
    if logger:
        logger.info(f"重连前WS状态: {current_status}")

    # UNKNOWN/CONNECTING 是过渡态，短暂等待看是否自行恢复
    if current_status in ("UNKNOWN", "CONNECTING"):
        if logger:
            logger.info(f"检测到过渡态 {current_status}，等待3秒观察是否自行恢复...")
        time.sleep(3)
        current_status = get_ws_status(page, logger)
        if logger:
            logger.info(f"等待后WS状态: {current_status}")
        # 如果恢复了，直接返回
        if current_status == "CONNECTED":
            if logger:
                logger.info("WS状态已自行恢复为CONNECTED，跳过重连")
            return current_status

    # IDLE状态：已断开，直接Connect
    if current_status == "IDLE":
        if logger:
            logger.info("当前已是IDLE状态，跳过Disconnect，直接Connect...")
        click_connect(page, logger)
        time.sleep(2)
    else:
        # 尝试先断开再连接
        disconnected = click_disconnect(page, logger)
        if not disconnected:
            # Disconnect按钮不存在，可能是误报或已经处于半断开状态
            if current_status == "CONNECTED":
                if logger:
                    logger.info("Disconnect按钮不存在但状态为CONNECTED，视为误报，跳过重连")
                return current_status
            if logger:
                logger.info("Disconnect按钮不存在，直接尝试Connect...")
        else:
            time.sleep(2)

        status = get_ws_status(page, logger)
        if logger:
            logger.info(f"断开后WS状态: {status}")
        click_connect(page, logger)
        time.sleep(2)

    # 等待连接成功
    if wait_for_ws_connected(page, logger, timeout=15):
        status = get_ws_status(page, logger)
        if logger:
            logger.info(f"WS重连成功，当前状态: {status}")
        return status
    else:
        status = get_ws_status(page, logger)
        if logger:
            logger.warning(f"WS重连超时，当前状态: {status}")
        return status


def dismiss_interaction_modal(page: Page, logger=None) -> bool:
    """
    检测并关闭 interaction-modal 遮罩层。
    通过在 iframe 区域内模拟鼠标移动来触发遮罩层关闭。
    
    返回: True 如果成功关闭遮罩，False 如果未找到遮罩或关闭失败
    """
    try:
        modal = page.locator('div.interaction-modal')
        if modal.count() == 0 or not modal.first.is_visible(timeout=500):
            return False
        
        if logger:
            logger.info("检测到 interaction-modal 遮罩层，尝试关闭...")
        
        iframe = page.locator('iframe[title="Preview"]')
        if iframe.count() > 0:
            iframe_box = iframe.first.bounding_box()
            if iframe_box:
                # 随机起点
                curr_x = iframe_box['x'] + random.randint(50, int(iframe_box['width']) - 50)
                curr_y = iframe_box['y'] + random.randint(50, int(iframe_box['height']) - 50)

                # 持续连续移动直到遮罩关闭，最多尝试30次
                for i in range(30):
                    # 从当前位置随机移动一段距离
                    delta_x = random.randint(-30, 30)
                    delta_y = random.randint(-20, 20)
                    curr_x = max(iframe_box['x'] + 20, min(iframe_box['x'] + iframe_box['width'] - 20, curr_x + delta_x))
                    curr_y = max(iframe_box['y'] + 20, min(iframe_box['y'] + iframe_box['height'] - 20, curr_y + delta_y))

                    page.bring_to_front()
                    page.mouse.move(curr_x, curr_y)
                    time.sleep(0.05)

                    # 每次移动后检查遮罩是否关闭
                    if modal.count() == 0 or not modal.first.is_visible(timeout=100):
                        if logger:
                            logger.info("已成功关闭 interaction-modal 遮罩层")
                        return True
        
        return False
    except Exception as e:
        if logger:
            logger.debug(f"关闭 interaction-modal 时出错: {e}")
        return False


def click_in_iframe(page: Page, logger=None) -> bool:
    """
    在 iframe 内随机移动鼠标并点击一次，用于保活。
    避开顶部（状态栏和按钮区域）和右侧区域。

    返回: True 如果成功点击，False 如果失败
    """
    try:
        if logger:
            logger.debug("开始执行 click_in_iframe 保活检测...")

        iframe = page.locator('iframe[title="Preview"]')
        if iframe.count() == 0:
            if logger:
                logger.debug("click_in_iframe: 失败，未找到 title='Preview' 的 iframe")
            return False

        iframe_box = iframe.first.bounding_box()
        if not iframe_box:
            if logger:
                logger.warning("click_in_iframe: 失败，iframe 存在但 bounding_box 返回 None (可能被隐藏或渲染引擎休眠)")
            return False

        if logger:
            logger.debug(f"click_in_iframe: 获取到 iframe 位置 {iframe_box}")

        # 安全区域：避开顶部120像素（WS状态栏+Connect/Disconnect按钮）和右侧200像素（按钮区域）
        safe_left = iframe_box['x'] + 50
        safe_right = iframe_box['x'] + iframe_box['width'] - 200
        safe_top = iframe_box['y'] + 120
        safe_bottom = iframe_box['y'] + iframe_box['height'] - 50

        # 确保安全区域有效
        if safe_right <= safe_left or safe_bottom <= safe_top:
            if logger:
                logger.warning(f"click_in_iframe: 安全区域计算无效 (left:{safe_left}, right:{safe_right}, top:{safe_top}, bottom:{safe_bottom})")
            return False

        # 随机起点（在安全区域内）
        curr_x = random.randint(int(safe_left), int(safe_right))
        curr_y = random.randint(int(safe_top), int(safe_bottom))

        # 使用 page.mouse 进行模拟 (红点独立在每个 Firefox 进程内，不存在系统鼠标争夺)
        for _ in range(random.randint(2, 4)):
            delta_x = random.randint(-30, 30)
            delta_y = random.randint(-20, 20)
            curr_x = max(int(safe_left), min(int(safe_right), curr_x + delta_x))
            curr_y = max(int(safe_top), min(int(safe_bottom), curr_y + delta_y))

            page.bring_to_front()
            page.mouse.move(curr_x, curr_y)
            time.sleep(0.1)

        # 点击当前位置
        page.bring_to_front()
        page.mouse.click(curr_x, curr_y)
        if logger:
            logger.debug("click_in_iframe: 成功完成鼠标移动和点击")
        return True
    except Exception as e:
        if logger:
            logger.error(f"在 iframe 内点击失败: {e}")
        return False
