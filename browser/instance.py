import os
import signal
import time
import json
import subprocess
import gc
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
        os.path.join(logs_dir(), 'app.log'),
        prefix=instance_label
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
        # 显式传入 Screen 对象，固定 screen 尺寸为 1440x900
        # 防止 Camoufox 反指纹机制用随机 screen 覆盖 CLI 的 -width/-height
        # （不传 screen 时，生成的 fingerprint.json 中 screen.width/height 是随机的，
        #  可能为 1680x1050 等，导致窗口尺寸偏大甚至超出 Xvfb 桌面）
        fingerprint_opts = generate_launch_options(
            user_data_dir=profile_dir,
            os="windows",
            screen=Screen(width=1440, height=900),
        )
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
        "browser.tabs.unloadOnLowMemory": False,  # 禁用低内存卸载
        "dom.min_background_timeout_value": 4,  # 维持后台计时器频率
        "network.websocket.timeout": 0,  # 禁用WS超时
        "page_visibility.dont_suspend_inactive": True,  # 防止非活动页面挂起
        "dom.timeout.enable_budget_timer_fallback": False,
        "widget.windows.window_occlusion_tracking.enabled": False,  # 禁用遮挡跟踪（如果窗口被遮挡，原本会挂起渲染）
        "gfx.webrender.dcomp-win.enabled": False  # 关闭可能导致黑屏或不渲染的硬件加速遮挡
    }

    # =========================================================================
    # 重启控制变量
    max_retries = int(os.getenv("MAX_RESTART_RETRIES", "5"))
    # 人工干预等待超时（秒），通过环境变量配置，默认 10 分钟
    manual_login_timeout = int(os.getenv("MANUAL_LOGIN_TIMEOUT", "600"))
    retry_count = 0
    base_delay = 3

    while True:
        # 检查是否收到全局关闭信号
        if shutdown_event and shutdown_event.is_set():
            logger.info("检测到全局关闭事件，浏览器实例不再启动，准备退出")
            return

        # ====== 启动前强制清理当前实例的僵尸进程和回收内存 ======
        try:
            # 强制 Python 垃圾回收，释放上一轮残留的 Playwright 对象
            gc.collect()

            # 【安全修复】只清理属于当前 profile 的孤儿进程，避免误杀其他用户实例
            # camoufox 启动命令中包含 -profile /app/camoufox_profiles/USER_COOKIE_X
            escaped_profile_dir = profile_dir.replace("/", "\\/")
            subprocess.run(
                f"pkill -f 'camoufox-bin.*-profile.*{escaped_profile_dir}' || true",
                shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            time.sleep(2)  # 给 OS 一点时间回收内存
            logger.debug(f"已完成启动前清理（垃圾回收 + {diagnostic_tag} 僵尸进程清理）")
        except Exception:
            pass
        # ====================================================

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
                        if not response.ok:  # response.ok 检查状态码是否在 200-299 范围内
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

                # 1. 目标页面等待逻辑（支持通过环境变量 MANUAL_LOGIN_TIMEOUT 配置超时时间）
                def is_at_target_url():
                    current_path = extract_url_path(page.url)
                    return expected_path and expected_path in current_path

                if not is_at_target_url():
                    logger.warning(f"[{diagnostic_tag}] 尚未到达目标页面！当前在: {mask_url_for_logging(page.url)}")
                    logger.warning(f"[{diagnostic_tag}] 可能是遇到了登录、Passkey提示、或安全检查...")
                    logger.warning(f"[{diagnostic_tag}] >>> 请立即前往 VNC 桌面 (http://IP:6080) 手动完成操作！")
                    logger.warning(f"[{diagnostic_tag}] >>> 脚本将在此挂起等待，最多等待 {manual_login_timeout} 秒 (可通过 MANUAL_LOGIN_TIMEOUT 环境变量调整)...")
                    wait_time = 0
                    while not is_at_target_url() and wait_time < manual_login_timeout:
                        page.wait_for_timeout(5000)
                        wait_time += 5
                    if not is_at_target_url():
                        logger.error(f"[{diagnostic_tag}] {manual_login_timeout}秒内未到达目标页面，退出并放弃该实例。")
                        page.screenshot(path=os.path.join(screenshot_dir, f"FAIL_manual_action_timeout_{diagnostic_tag}.png"))
                        return
                    else:
                        logger.info(f"[{diagnostic_tag}] 人工操作完成，成功到达目标页面！")

                logger.info(f"URL验证通过。目标路径: {mask_path_for_logging(expected_path)}")

                # 2 & 3. 智能等待：边等 Spinner 消失，边侦测并点击真正的弹窗按钮（轮询模式）
                logger.info("正在进入智能等待：监控加载状态及处理突发弹窗 (最长30秒)...")
                # 定义要查找的精确按钮名称（优先处理最常见的新版警告）
                target_button_names = [
                    "Continue",  # 对应 Unlock more possibilities / 协议更新
                    "Continue to app",  # 对应 This app is from another developer
                    "Continue to the app",
                    "Connect",  # 对应旧版授权
                    "确认连接", "连接", "继续",
                    "Dismiss", "Got it", "OK", "Accept", "I agree"
                ]

                wait_time = 0
                max_wait = 30
                last_clicked_index = -1  # 轮询指针，记录上次点击的按钮索引

                while wait_time < max_wait:
                    # 第一步：轮询遍历所有候选按钮，找到第一个真正可点击的
                    clicked_any = False
                    found_visible_button = None

                    for idx, btn_name in enumerate(target_button_names):
                        try:
                            # 策略1：使用 get_by_role（最贴近人类行为）
                            btn = page.get_by_role("button", name=btn_name, exact=True)
                            if btn.count() > 0 and btn.first.is_visible(timeout=100):
                                # 额外检查：确保按钮在视口内且未被禁用
                                box = btn.first.bounding_box()
                                if box and box['width'] > 0 and box['height'] > 0:
                                    found_visible_button = ("role", btn_name, btn.first, idx)
                                    break

                            # 策略2：兜底方案 - 直接文本匹配（处理非标准 button 元素）
                            # 匹配 button、div[role="button"]、span[role="button"] 等
                            text_btn = page.locator(
                                f"button:has-text('{btn_name}'), "
                                f"[role='button']:has-text('{btn_name}'), "
                                f"button:text-is('{btn_name}')"
                            )
                            if text_btn.count() > 0 and text_btn.first.is_visible(timeout=100):
                                box = text_btn.first.bounding_box()
                                if box and box['width'] > 0 and box['height'] > 0:
                                    found_visible_button = ("text", btn_name, text_btn.first, idx)
                                    break

                        except Exception:
                            continue  # 找不到这个按钮或报错，静默尝试下一个名字

                    if found_visible_button:
                        strategy, btn_name, btn_locator, idx = found_visible_button
                        logger.info(f"发现可见按钮 [{btn_name}]（策略: {strategy}），尝试点击...")

                        try:
                            # 尝试多种点击策略
                            click_success = False

                            # 策略A：标准点击 + force=True
                            try:
                                btn_locator.click(force=True, timeout=2000)
                                click_success = True
                            except Exception as e1:
                                logger.debug(f"标准点击失败: {e1}")

                            # 策略B：JavaScript 直接触发 click 事件（绕过所有遮挡检测）
                            if not click_success:
                                try:
                                    page.evaluate("""
                                        (element) => {
                                            if (element) {
                                                element.click();
                                                return true;
                                            }
                                            return false;
                                        }
                                    """, btn_locator.element_handle())
                                    click_success = True
                                except Exception as e2:
                                    logger.debug(f"JavaScript 点击失败: {e2}")

                            if click_success:
                                logger.info(f"成功点击按钮 [{btn_name}]")
                                clicked_any = True
                                last_clicked_index = idx
                                page.wait_for_timeout(1500)  # 点击后给予 1.5 秒让弹窗动画消失

                        except Exception as click_e:
                            logger.warning(f"点击按钮 [{btn_name}] 时出错: {click_e}")

                    if clicked_any:
                        continue  # 如果刚才发生了点击，立刻进入下一轮 while 循环，因为可能还有连环弹窗

                    # 第二步：如果一圈下来没发现弹窗，我们来检查加载圈是否都消失了
                    try:
                        spinners = page.locator('mat-spinner')
                        count = spinners.count()
                        all_hidden = True
                        if count > 0:
                            for i in range(count):
                                if spinners.nth(i).is_visible():
                                    all_hidden = False
                                    break
                        if all_hidden:
                            logger.info("所有加载指示器已消失。页面已完成初步加载且无拦截弹窗。")
                            break  # 没有任何弹窗，且圈圈消失，大功告成，跳出 while 循环
                    except Exception:
                        pass  # DOM变化导致获取元素报错，忽略

                    # 休息 1 秒后进行下一秒的轮询探测
                    page.wait_for_timeout(1000)
                    wait_time += 1

                if wait_time >= max_wait:
                    logger.warning("30秒智能等待结束，页面可能仍有后台加载项，将强制执行后续流程...")

                # 4. 最终鉴权错误检查（防身用）— 检查页面上是否有可见的认证错误文本
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

                # ====== 401 致命错误拦截：仅在页面完全加载后才开始监听 ======
                # 页面初始化阶段 (goto + 弹窗处理 + iframe加载) 会有大量正常的 401
                # (alkalimakersuite-pa 等 API 在 token 建立前会返回 401，这是正常行为)
                # 所以我们只在所有初始化完成后才注册监听器，用于后续的保活阶段
                auth_401_count = [0]  # 计数器而非布尔值，避免偶发 401 误杀
                AUTH_401_THRESHOLD = 3  # 连续 3 次以上才判定为真正的认证失败

                def on_response_post_init(response):
                    """页面完全加载后的 API 认证失败监听"""
                    if response.status == 401 or response.status == 403:
                        url = response.url
                        # 只关注真正的 Gemini 推理 API 端点
                        # 排除初始化相关的 API (alkalimakersuite-pa, firebaseinstallations 等)
                        if any(api_pattern in url for api_pattern in [
                            "generativelanguage.googleapis.com",
                            "alkalimakersuite-pa.clients6.google.com",
                        ]):
                            auth_401_count[0] += 1
                            logger.warning(
                                f"[{diagnostic_tag}] API 认证失败 ({auth_401_count[0]}/{AUTH_401_THRESHOLD}): "
                                f"URL: {url[:100]}... 状态码: {response.status}"
                            )

                page.on("response", on_response_post_init)
                # ====================================================

                # 5. 所有验证通过，确认成功！
                logger.info("所有验证通过，确认已成功登录并准备就绪")
                handle_successful_navigation(page, logger, diagnostic_tag, shutdown_event, cookie_validator, expected_path, expected_url)

                # 移除 response 监听器
                try:
                    page.remove_listener("response", on_response_post_init)
                except Exception:
                    pass

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
