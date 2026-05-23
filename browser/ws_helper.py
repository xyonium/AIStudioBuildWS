import time
import random
from playwright.sync_api import Page, FrameLocator

def get_context(page: Page, logger=None):
    """
    获取元素查找上下文。如果存在 iframe[title="Preview"]，则返回其 FrameLocator，
    否则直接返回 page。这样兼容本地直连和 HuggingFace Spaces 嵌套。
    """
    try:
        if page.locator('iframe[title="Preview"]').count() > 0:
            return page.frame_locator('iframe[title="Preview"]')
    except Exception as e:
        if logger:
            logger.debug(f"获取上下文出错: {e}")
    return page

def get_ws_status(page: Page, logger=None) -> str:
    """
    获取页面中WS连接状态。
    返回: CONNECTED, IDLE, CONNECTING 或 UNKNOWN
    """
    try:
        context = get_context(page, logger)
        
        status_element = context.locator('text=/WS:\\s*(CONNECTED|IDLE|CONNECTING)/i').first
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
    try:
        context = get_context(page, logger)

        disconnect_btn = context.locator('button:has-text("Disconnect")')
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
                page.evaluate("""
                    () => {
                        let doc = document;
                        const iframe = document.querySelector('iframe[title="Preview"]');
                        if (iframe && iframe.contentDocument) {
                            doc = iframe.contentDocument;
                        }
                        const btn = Array.from(doc.querySelectorAll('button'))
                            .find(b => b.textContent.includes('Disconnect'));
                        if (btn) btn.click();
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
    try:
        context = get_context(page, logger)

        connect_btn = context.locator('button:has-text("Connect")')
        if connect_btn.count() == 0:
            if logger:
                logger.warning("未找到 Connect 按钮")
            return False

        if not connect_btn.first.is_visible(timeout=3000):
            if logger:
                logger.warning("Connect 按钮不可见")
            return False

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
                    let doc = document;
                    const iframe = document.querySelector('iframe[title="Preview"]');
                    if (iframe && iframe.contentDocument) {
                        doc = iframe.contentDocument;
                    }
                    const btn = Array.from(doc.querySelectorAll('button'))
                        .find(b => b.textContent.includes('Connect'));
                    if (btn && !btn.disabled) btn.click();
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
    start_time = time.time()
    while time.time() - start_time < timeout:
        status = get_ws_status(page, logger)
        if status == "CONNECTED":
            return True
        time.sleep(1)
    return False

def reconnect_ws(page: Page, logger=None) -> str:
    if logger:
        logger.info("开始执行WS重连流程...")

    dismiss_interaction_modal(page, logger)

    current_status = get_ws_status(page, logger)
    if logger:
        logger.info(f"重连前WS状态: {current_status}")

    if current_status in ("UNKNOWN", "CONNECTING"):
        if logger:
            logger.info(f"检测到过渡态 {current_status}，等待3秒观察是否自行恢复...")
        time.sleep(3)
        current_status = get_ws_status(page, logger)
        if logger:
            logger.info(f"等待后WS状态: {current_status}")
        if current_status == "CONNECTED":
            if logger:
                logger.info("WS状态已自行恢复为CONNECTED，跳过重连")
            return current_status

    if current_status == "IDLE":
        if logger:
            logger.info("当前已是IDLE状态，跳过Disconnect，直接Connect...")
        click_connect(page, logger)
        time.sleep(2)
    else:
        disconnected = click_disconnect(page, logger)
        if not disconnected:
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
                curr_x = iframe_box['x'] + random.randint(50, int(iframe_box['width']) - 50)
                curr_y = iframe_box['y'] + random.randint(50, int(iframe_box['height']) - 50)

                for i in range(30):
                    delta_x = random.randint(-30, 30)
                    delta_y = random.randint(-20, 20)
                    curr_x = max(iframe_box['x'] + 20, min(iframe_box['x'] + iframe_box['width'] - 20, curr_x + delta_x))
                    curr_y = max(iframe_box['y'] + 20, min(iframe_box['y'] + iframe_box['height'] - 20, curr_y + delta_y))

                    page.bring_to_front()
                    page.mouse.move(curr_x, curr_y)
                    time.sleep(0.05)

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
    try:
        if logger:
            logger.debug("开始执行 click_in_iframe 保活检测...")

        # ================= 新增：父页面保活点击 =================
        try:
            page.bring_to_front()

            # 方案 A: 尝试寻找左下角的输入框进行点击 (更真实的交互)
            # 根据您说的 "左下角有个文本输入框"，我们尝试用通用的选择器去找
            input_box = page.locator('textarea[placeholder*="ask" i], textarea, input[type="text"]').last
            if input_box.count() > 0 and input_box.is_visible(timeout=500):
                input_box.click()
                if logger:
                    logger.debug("已在父页面点击左下角文本框，防止云端容器休眠")
            else:
                # 方案 B: 兜底方案，在屏幕特定位置点击
                viewport = page.viewport_size
                if viewport:
                    # 点击左下角区域 (X: 100, Y: 底部往上 50)
                    click_y = viewport['height'] - 50
                    page.mouse.move(100, click_y)
                    page.mouse.click(100, click_y)
                    if logger:
                        logger.debug(f"已在父页面左下角({100}, {click_y})点击，防止云端容器休眠")
        except Exception as e:
            if logger:
                logger.debug(f"父页面点击失败: {e}")
        # =======================================================

        iframe = page.locator('iframe[title="Preview"]')
        
        if iframe.count() == 0:
            # 没有 iframe，本地直连模式
            viewport = page.viewport_size
            if not viewport:
                return False
            safe_left = 50
            safe_right = viewport['width'] - 200
            safe_top = 120
            safe_bottom = viewport['height'] - 50
        else:
            iframe_box = iframe.first.bounding_box()
            if not iframe_box:
                if logger:
                    logger.warning("click_in_iframe: 失败，iframe 存在但 bounding_box 返回 None")
                return False
            safe_left = iframe_box['x'] + 50
            safe_right = iframe_box['x'] + iframe_box['width'] - 200
            safe_top = iframe_box['y'] + 120
            safe_bottom = iframe_box['y'] + iframe_box['height'] - 50

        if safe_right <= safe_left or safe_bottom <= safe_top:
            if logger:
                logger.warning(f"click_in_iframe: 安全区域计算无效 (left:{safe_left}, right:{safe_right}, top:{safe_top}, bottom:{safe_bottom})")
            return False

        curr_x = random.randint(int(safe_left), int(safe_right))
        curr_y = random.randint(int(safe_top), int(safe_bottom))

        for _ in range(random.randint(2, 4)):
            delta_x = random.randint(-30, 30)
            delta_y = random.randint(-20, 20)
            curr_x = max(int(safe_left), min(int(safe_right), curr_x + delta_x))
            curr_y = max(int(safe_top), min(int(safe_bottom), curr_y + delta_y))

            page.bring_to_front()
            page.mouse.move(curr_x, curr_y)
            time.sleep(0.1)

        page.bring_to_front()
        page.mouse.click(curr_x, curr_y)
        if logger:
            logger.debug("click_in_iframe: 成功完成鼠标移动和点击")
        return True
    except Exception as e:
        if logger:
            logger.error(f"在页面/iframe内点击失败: {e}")
        return False
