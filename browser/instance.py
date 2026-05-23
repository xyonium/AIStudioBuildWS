import os
import signal
import time
import json
from playwright.sync_api import TimeoutError, Error as PlaywrightError
from utils.logger import setup_logging
from utils.cookie_manager import CookieManager
from browser.navigation import handle_successful_navigation, KeepAliveError
from browser.cookie_validator import CookieValidator
from camoufox.sync_api import Camoufox
from utils.paths import logs_dir
from utils.common import parse_headless_mode, ensure_dir
from utils.url_helper import extract_url_path, mask_url_for_logging, mask_path_for_logging
from camoufox.utils import launch_options as generate_launch_options
from browserforge.fingerprints import Screen


def run_browser_instance(config, shutdown_event=None):
    """
    根据最终合并的配置，启动并管理一个单独的 Camoufox 浏览器实例。
    使用CookieManager统一管理Cookie加载，避免重复的扫描逻辑。
    """
    # 重置信号处理器，确保子进程能响应 SIGTERM
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    # 忽略 SIGINT (Ctrl+C)，让主进程统一处理
    signal.signal(signal.SIGINT, signal.SIG_IGN)

    cookie_source = config.get('cookie_source')
    if not cookie_source:
        # 使用默认logger进行错误报告
        logger = setup_logging(os.path.join(logs_dir(), 'app.log'))
        logger.error("错误: 配置中缺少cookie_source对象")
        return

    instance_label = cookie_source.display_name
    logger = setup_logging(
        os.path.join(logs_dir(), 'app.log'), prefix=instance_label
    )
    diagnostic_tag = instance_label.replace(os.sep, "_")

    expected_url = config.get('url')
    proxy = config.get('proxy')
    headless_setting = config.get('headless', 'virtual')

    # 使用CookieManager加载Cookie
    cookie_manager = CookieManager(logger)
    all_cookies = []

    try:
        # 直接使用CookieSource对象加载Cookie
        cookies = cookie_manager.load_cookies(cookie_source)
        all_cookies.extend(cookies)

    except Exception as e:
        logger.error(f"从Cookie来源加载时出错: {e}")
        # 这里先不立刻 return，如果是Profile模式可能根本不需要加载成功

    # 【关键修改】：放宽限制，允许空 Cookie，因为我们要读本地 Profile
    if not all_cookies:
        logger.info(f"未检测到环境变量传入的 Cookie，将完全依赖本地 Profile ({diagnostic_tag}) 进行免密登录。")
        # return 

    cookies = all_cookies

    headless_mode = parse_headless_mode(headless_setting)
    launch_options = {"headless": headless_mode}
    # launch_options["block_images"] = True  # 禁用图片加载

    if proxy:
        logger.info(f"使用代理: {proxy} 访问")
        launch_options["proxy"] = {"server": proxy, "bypass": "localhost, 127.0.0.1"}
    
    screenshot_dir = logs_dir()
    ensure_dir(screenshot_dir)

    # ================= [新增代码：多用户 Profile 和指纹管理] =================
    profiles_base_dir = "/app/camoufox_profiles"
    ensure_dir(profiles_base_dir)
    
    # 使用 identifier (如 USER_COOKIE_1) 作为文件夹名，实现多用户完全隔离
    profile_dir = os.path.join(profiles_base_dir, diagnostic_tag)
    ensure_dir(profile_dir)
    fingerprint_file = os.path.join(profile_dir, "fingerprint.json")
    if os.path.exists(fingerprint_file):
        with open(fingerprint_file, "r") as f:
            fingerprint_opts = json.load(f)
            logger.info(f"已加载现有的环境指纹和 Profile: {profile_dir}")
    else:
        # ====== 新增/修改代码开始 ======
        # 不要传 screen 参数了，让它自己随机，防止被风控
        fingerprint_opts = generate_launch_options(
            user_data_dir=profile_dir,
            os="windows",
        )
        
        # 强制 Firefox 启动时最大化或指定宽高
        # -width 1440 -height 900 是 Firefox 底层的 CLI 参数
        if "args" not in fingerprint_opts:
            fingerprint_opts["args"] = []
        fingerprint_opts["args"].extend(["-width", "1440", "-height", "900"])
        # ====== 新增/修改代码结束 ======
        
        with open(fingerprint_file, "w") as f:
            json.dump(fingerprint_opts, f, indent=4)
            logger.info(f"已生成并锁定全新环境指纹: {profile_dir}")
    
    # 将指纹和持久化设置合并到 Camoufox 启动选项中
    launch_options["from_options"] = fingerprint_opts
    launch_options["persistent_context"] = True
    launch_options["user_data_dir"] = profile_dir
    # [新增] 强制 Firefox 禁用后台资源冻结、标签页休眠和遮挡跟踪 (Occlusion Tracking)
    # 这对多窗口/多标签页在无头环境下能否持续运行至关重要
    launch_options["firefox_user_prefs"] = {
        "browser.tabs.unloadOnLowMemory": False, # 禁用低内存卸载
        "dom.min_background_timeout_value": 4,   # 维持后台计时器频率
        "network.websocket.timeout": 0,          # 禁用WS超时
        "page_visibility.dont_suspend_inactive": True, # 防止非活动页面挂起
        "dom.timeout.enable_budget_timer_fallback": False,
        "widget.windows.window_occlusion_tracking.enabled": False, # 禁用遮挡跟踪（如果窗口被遮挡，原本会挂起渲染）
        "gfx.webrender.dcomp-win.enabled": False # 关闭可能导致黑屏或不渲染的硬件加速遮挡
    }
    # =========================================================================

    # 重启控制变量
    max_retries = int(os.getenv("MAX_RESTART_RETRIES", "5"))
    retry_count = 0
    base_delay = 3

    while True:
        # 检查是否收到全局关闭信号
        if shutdown_event and shutdown_event.is_set():
            logger.info("检测到全局关闭事件，浏览器实例不再启动，准备退出")
            return

        try:
            # === [修改代码：由于开启了持久化，Camoufox 返回的是 BrowserContext] ===
            with Camoufox(**launch_options) as context:
                # 获取持久化上下文中已有的默认页面，如果没有则新建
                page = context.pages[0] if context.pages else context.new_page()
                
                # 依然兼容你现有的环境变量/JSON导入逻辑（当作初始凭证注入）
                if cookies:
                    context.add_cookies(cookies)

                # 创建Cookie验证器 (原有代码不需要改，context 对象完美兼容)
                cookie_validator = CookieValidator(page, context, logger)

                # =============================================================
                
                # 下方的 page.goto() 等业务逻辑完全不需要改动！！！
                response = None
                try:
                    logger.info(f"正在导航到: {mask_url_for_logging(expected_url)} (超时设置为 90 秒)")
                    # page.goto() 会返回一个 response 对象，我们可以用它来获取状态码等信息
                    response = page.goto(expected_url, wait_until='domcontentloaded', timeout=90000)
                    
                    # 检查HTTP响应状态码
                    if response:
                        logger.info(f"导航初步成功，服务器响应状态码: {response.status} {response.status_text}")
                        if not response.ok: # response.ok 检查状态码是否在 200-299 范围内
                            logger.warning(f"警告：页面加载成功，但HTTP状态码表示错误: {response.status}")
                            # 即使状态码错误，也保存快照以供分析
                            page.screenshot(path=os.path.join(screenshot_dir, f"WARN_http_status_{response.status}_{diagnostic_tag}.png"))
                    else:
                        # 对于非http/https的导航（如 about:blank），response可能为None
                        logger.warning("page.goto 未返回响应对象，可能是一个非HTTP导航")

                except TimeoutError:
                    # 这是最常见的错误：超时
                    logger.error(f"导航到 {mask_url_for_logging(expected_url)} 超时 (超过90秒)")
                    logger.error("可能原因：网络连接缓慢、目标网站服务器无响应、代理问题、或页面资源被阻塞")
                    # 尝试保存诊断信息
                    try:
                        # 截图对于看到页面卡在什么状态非常有帮助（例如，空白页、加载中、Chrome错误页）
                        screenshot_path = os.path.join(screenshot_dir, f"FAIL_timeout_{diagnostic_tag}.png")
                        page.screenshot(path=screenshot_path, full_page=True)
                        logger.info(f"已截取超时时的屏幕快照: {screenshot_path}")

                        # 保存HTML可以帮助分析DOM结构，即使在无头模式下也很有用
                        html_path = os.path.join(screenshot_dir, f"FAIL_timeout_{diagnostic_tag}.html")
                        with open(html_path, 'w', encoding='utf-8') as f:
                            f.write(page.content())
                        logger.info(f"已保存超时时的页面HTML: {html_path}")
                    except Exception as diag_e:
                        logger.error(f"在尝试进行超时诊断（截图/保存HTML）时发生额外错误: {diag_e}")

                    # 不要直接 return 终止进程，抛出 KeepAliveError 交给外部循环重试 (即刷新页面)
                    raise KeepAliveError(f"页面加载超时: {expected_url}")

                except PlaywrightError as e:
                    # 捕获其他Playwright相关的网络错误，例如DNS解析失败、连接被拒绝等
                    error_message = str(e)
                    logger.error(f"导航到 {mask_url_for_logging(expected_url)} 时发生 Playwright 网络错误")
                    logger.error(f"错误详情: {error_message}")

                    # Playwright的错误信息通常很具体，例如 "net::ERR_CONNECTION_REFUSED"
                    if "net::ERR_NAME_NOT_RESOLVED" in error_message:
                        logger.error("排查建议：检查DNS设置或域名是否正确")
                    elif "net::ERR_CONNECTION_REFUSED" in error_message:
                        logger.error("排查建议：目标服务器可能已关闭，或代理/防火墙阻止了连接")
                    elif "net::ERR_INTERNET_DISCONNECTED" in error_message:
                        logger.error("排查建议：检查本机的网络连接")

                    # 同样尝试截图，尽管此时页面可能完全无法访问
                    try:
                        screenshot_path = os.path.join(screenshot_dir, f"FAIL_network_error_{diagnostic_tag}.png")
                        page.screenshot(path=screenshot_path)
                        logger.info(f"已截取网络错误时的屏幕快照: {screenshot_path}")
                    except Exception as diag_e:
                        logger.error(f"在尝试进行网络错误诊断（截图）时发生额外错误: {diag_e}")

                    # 网络错误也应该重试刷新，而不是直接终止
                    raise KeepAliveError(f"网络错误: {error_message}")

                # --- 如果导航没有抛出异常，继续执行后续逻辑 ---
                
                logger.info("页面初步加载完成，正在检查并处理初始弹窗...")
                page.wait_for_timeout(2000)

                expected_path = extract_url_path(expected_url).split('?')[0]
                
                # 1. 目标页面等待逻辑
                def is_at_target_url():
                    current_path = extract_url_path(page.url)
                    return expected_path and expected_path in current_path

                if not is_at_target_url():
                    logger.warning(f"[{diagnostic_tag}] 尚未到达目标页面！当前在: {mask_url_for_logging(page.url)}")
                    logger.warning(f"[{diagnostic_tag}] 可能是遇到了登录、Passkey提示、或安全检查...")
                    logger.warning(f"[{diagnostic_tag}] >>> 请立即前往 VNC 桌面 (http://IP:6080) 手动完成操作！")
                    logger.warning(f"[{diagnostic_tag}] >>> 脚本将在此挂起等待，直到浏览器到达目标页面，最多等待 5 分钟...")
                    
                    wait_time = 0
                    while not is_at_target_url() and wait_time < 300:
                        page.wait_for_timeout(5000)
                        wait_time += 5
                        
                    if not is_at_target_url():
                        logger.error(f"[{diagnostic_tag}] 5分钟内未到达目标页面，退出并放弃该实例。")
                        page.screenshot(path=os.path.join(screenshot_dir, f"FAIL_manual_action_timeout_{diagnostic_tag}.png"))
                        return
                    else:
                        logger.info(f"[{diagnostic_tag}] 人工操作完成，成功到达目标页面！")
                
                logger.info(f"URL验证通过。目标路径: {mask_path_for_logging(expected_path)}")

                # 2. 等待所有的 Spinner (加载圈) 消失
                try:
                    logger.info("正在等待页面上的加载指示器消失... (最长等待30秒)")
                    spinners = page.locator('mat-spinner')
                    count = spinners.count()
                    if count > 0:
                        for i in range(count):
                            spinners.nth(i).wait_for(state='hidden', timeout=30000)
                    logger.info("加载指示器已消失。页面已完成异步加载")
                except TimeoutError:
                    logger.warning("忽略 spinner 超时，尝试继续执行...")

                # 3. 拦截并【自动点击】第三方 App 的“确认连接”弹窗
                logger.info("检查是否有第三方 App 的连接确认弹窗...")
                try:
                    # 我们查找包含 "Connect"、"连接" 或 "确认" 文本的按钮
                    confirm_btn = page.locator('button:has-text("Connect"), button:has-text("连接"), button:has-text("确认连接")')
                    if confirm_btn.first.is_visible(timeout=3000):  # 等待最多3秒看弹窗是否出来
                        logger.info("发现 '确认连接' 提示框，正在自动点击授权！")
                        confirm_btn.first.click()
                        page.wait_for_timeout(2000)  # 点完后等它消失
                except Exception as e:
                    pass  # 如果没有弹窗，就静默忽略，什么也不做

                # 4. 最终鉴权错误检查（防身用）
                auth_error_locator = page.get_by_text("authentication error", exact=False)
                if auth_error_locator.is_visible(timeout=2000):
                    logger.error(f"检测到认证失败错误。Cookie已过期或无效")
                    page.screenshot(path=os.path.join(screenshot_dir, f"FAIL_auth_error_{diagnostic_tag}.png"))
                    return

                # 新增检查：确保 App 的 iframe (Preview) 以及 WS 状态元素已经加载出来
                # 否则说明页面并未真正渲染完毕，过早判定成功会导致保活机制找不到元素
                logger.info("正在验证 App Preview 框架是否加载...")
                try:
                    # 等待 iframe 出现
                    frame_element = page.locator('iframe[title="Preview"]')
                    frame_element.first.wait_for(state='visible', timeout=15000)

                    # 验证 iframe 内容加载
                    # get_ws_status() 内部会等待 3 秒找 WS 文本，这里调用一次确保内容已出
                    from browser.ws_helper import get_ws_status
                    if get_ws_status(page) == "UNKNOWN":
                        logger.warning("警告：iframe 已加载，但未检测到 WS 状态文本，可能是网络延迟或页面白屏")
                except Exception as wait_e:
                    logger.error(f"App Preview 框架未加载完成: {wait_e}")
                    raise KeepAliveError("App Preview 框架未能在预期时间内加载完毕，将重试")

                # 5. 所有验证通过，确认成功！
                logger.info("所有验证通过，确认已成功登录并准备就绪")
                handle_successful_navigation(page, logger, diagnostic_tag, shutdown_event, cookie_validator, expected_path)

                # 如果运行到这里且没有异常，表示实例正常结束（例如收到关闭信号）
                # 正常结束时重置重试计数器
                retry_count = 0
                return

        except KeepAliveError as e:
            retry_count += 1
            if retry_count > max_retries:
                logger.error(f"重试次数已达上限 ({max_retries})，实例不再重启，退出")
                return
            
            # 指数退避：3秒、6秒、12秒、24秒...最长60秒
            delay = min(base_delay * (2 ** (retry_count - 1)), 60)
            logger.error(f"浏览器实例出现错误 (重试 {retry_count}/{max_retries})，将在 {delay} 秒后重启浏览器实例: {e}")
            time.sleep(delay)
            continue
        except KeyboardInterrupt:
            logger.info(f"用户中断，正在关闭...")
            return
        except SystemExit as e:
            # 捕获Cookie验证失败时的系统退出
            if e.code == 1:
                logger.error("Cookie验证失败，关闭进程实例")
            else:
                logger.info(f"实例正常退出，退出码: {e.code}")
            return
        except Exception as e:
            # 这是一个最终的捕获，用于捕获所有未预料到的错误
            logger.exception(f"运行 Camoufox 实例时发生未预料的严重错误: {e}")
            return
