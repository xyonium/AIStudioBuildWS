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
from browser.window_placer import place_window


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
        # 必须显式传入 screen 和 window 参数！
        # 根因：Docker 中 get_screen_cons() 内部调用 get_monitors() 会静默失败
        # （需要 xrandr，容器没装），导致 screen=None 传给 BrowserForge，
        # 其默认 Windows 桌面分布的众数是 1680x1050，每次新生成的指纹都确定性偏大。
        # screen 约束生成的 screen 尺寸上限，window 精确控制窗口大小。
        fingerprint_opts = generate_launch_options(
            user_data_dir=profile_dir,
            os="windows",
            screen=Screen(max_width=1440, max_height=900),
            window=(1440, 900),
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
        #"browser.tabs.unloadOnLowMemory": False,  # 禁用低内存卸载
        "dom.min_background_timeout_value": 100,  # 维持后台计时器频率
        "network.websocket.timeout": 0,  # 禁用WS超时
        "page_visibility.dont_suspend_inactive": True,  # 防止非活动页面挂起
        "dom.timeout.enable_budget_timer_fallback": False,
        "widget.windows.window_occlusion_tracking.enabled": False,  # 禁用遮挡跟踪（如果窗口被遮挡，原本会挂起渲染）
        #"gfx.webrender.dcomp-win.enabled": False,  # 关闭可能导致黑屏或不渲染的硬件加速遮挡        
        # 1. 限制内存缓存容量（而非禁用）
        # 禁用 browser.cache.memory 会同时关掉 Firefox 的 memory-pressure 回收机制，
        # 反而导致 GC 变懒、RSS 只增不还；保留缓存但限制容量更稳
        "browser.cache.memory.capacity": 65536,  # 内存缓存上限 64MB
        #"browser.cache.disk.enable": False,       # 甚至可以禁止磁盘缓存，防止磁盘I/O导致延迟
        # 2. 砍掉页面历史记录 (Session History)
        # 非常关键！默认浏览器会记住50个页面的状态(为了按后退键能够秒开)，极其占内存
        "browser.sessionhistory.max_entries": 2,  # 仅保留2个前进后退记录
        "browser.sessionstore.max_tabs_undo": 0,  # 关闭"恢复关闭的标签页"功能
        # 3. 强制激进的垃圾回收 (Garbage Collection & JS Memory)
        "javascript.options.mem.max": 102400,     # 限制 JS 引擎最大使用内存阈值 (单位KB)
        "javascript.options.mem.high_water_mark": 32, # 更低的水位线，更早触发GC
        # 4. 优化图片与媒体内存消耗（如果你不需要看高清图片）
        "image.mem.decodeondraw": True,           # 只有真正画出来的时候才解码图片
        "image.mem.discardable": True,            # 允许释放掉未显示的图片内存
        "image.mem.max_decoded_image_kb": 10240,  # 限制单张解码图片的最大内存 (10MB)
        # 5. 严格限制子进程数量（防止它悄悄开多个辅助进程）
        "dom.ipc.processCount": 1,                # 强制将网页内容进程数量限制在1个
        "dom.ipc.processCount.extension": 1,      # 限制插件进程数
        # 6. 禁用不需要的遥测和后台服务 (有效减少闲置内存占用)
        "toolkit.telemetry.enabled": False,
        "browser.ping-centre.telemetry": False,
        "network.prefetch-next": False,           # 禁用链接预读取
        "network.dns.disablePrefetch": True,      # 禁用 DNS 预解析
        # 已回退：media.autoplay.default=5 和 image.animation_mode=none
        # 这两条会让浏览器媒体/动图行为偏离真实用户，Google 反爬的行为指纹
        # 一致性校验可能因此判 403，而 AI Studio 页面并无自动播放媒体，收益≈0
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
            # 注意：正则必须精确匹配完整目录名，否则 USER_COOKIE_1 会误杀
            # USER_COOKIE_10/11（前缀匹配），引发多账户级联重启和 403
            escaped_profile_dir = profile_dir.replace("/", "\\/")
            subprocess.run(
                f"pkill -f 'camoufox-bin.*-profile {escaped_profile_dir}( |$)' || true",
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

                # 防止窗口被其他实例 100% 盖死（会触发 Firefox occlusion sleep）。
                # 根因：persistent profile 的 xulstore.json 会让 Firefox 带着
                # _NET_CURRENT_DESKTOP 标记复活窗口，fluxbox 的 CascadePlacement
                # 对这类窗口直接跳过摆放，所以重启后窗口会全叠在上次的位置。
                # 仅在检测到页面区域被完全遮挡时才挪到本实例的确定性槽位；
                # 无 wmctrl 的环境（本地开发）会优雅跳过，不影响流程。
                place_window(diagnostic_tag, logger)

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

                # 2 & 3. 增强版智能等待：弹窗稳定检测 + 最上层交互 + 点击有效性反馈（轮询模式）
                logger.info("正在进入增强版智能等待：弹窗稳定 + 最上层点击 + 死循环防护 (最长30秒)...")
                target_button_names = [
                    "Continue",
                    "Continue to app",
                    "Continue to the app",
                    "Connect",
                    "确认连接", "连接", "继续",
                    "Dismiss", "Got it", "OK", "Accept", "I agree"
                ]

                # ========== 阶段 1：弹窗稳定期 (Debounce) ==========
                # 连续检测2秒，确认没有新的弹窗/overlay再冒出来
                prev_dialog_count = -1
                stable_ticks = 0
                DEBOUNCE_GOAL = 2  # 目标：连续2秒弹窗数量不变
                for _ in range(10):  # 最多等待10秒
                    curr_count = page.evaluate("""() => {
                        return document.querySelectorAll(
                            'mat-mdc-dialog-container, .cdk-overlay-pane, [role="dialog"], ms-g1-welcome-dialog'
                        ).length;
                    }""")
                    if curr_count == prev_dialog_count:
                        stable_ticks += 1
                    else:
                        stable_ticks = 0
                        prev_dialog_count = curr_count
                        logger.info(f"  弹窗/遮罩层数量变化: {curr_count}，重置稳定计数")
                    if stable_ticks >= DEBOUNCE_GOAL:
                        logger.info(f"  弹窗已稳定（连续{DEBOUNCE_GOAL}秒无变化，当前{prev_dialog_count}层），开始扫描")
                        break
                    page.wait_for_timeout(1000)
                else:
                    logger.warning("  弹窗稳定期未完全达标，将强制继续...")

                # ========== 阶段 2：每秒重新扫描 + 最上层点击 + 点击有效性检测 ==========
                wait_time = 0
                max_wait = 30
                # 记录每个按钮的连续无效点击次数，超过3次则跳过
                click_fail_count = {}  # btn_name -> int
                # 记录上一轮被判定为"无效"的按钮，下一轮优先尝试其他
                skip_set = set()

                while wait_time < max_wait:
                    # ---- 每轮循环开始：强制重新扫描，不缓存任何定位器 ----
                    # 先检测最上层弹窗是否变化
                    top_dialog_info = page.evaluate("""() => {
                        const dialogs = Array.from(document.querySelectorAll(
                            'mat-mdc-dialog-container, .cdk-overlay-pane, [role="dialog"], ms-g1-welcome-dialog'
                        )).filter(el => {
                            const rect = el.getBoundingClientRect();
                            return rect.width > 0 && rect.height > 0;
                        });
                        if (dialogs.length === 0) return null;
                        const top = dialogs[dialogs.length - 1];
                        return {
                            tag: top.tagName,
                            class: top.className,
                            id: top.id,
                            hasContinue: !!top.querySelector('button, [role="button"]'),
                        };
                    }""")

                    clicked_any = False
                    candidate_found = None

                    # 遍历所有候选按钮，但优先尝试非 skip_set 中的
                    search_order = [b for b in target_button_names if b not in skip_set] + \
                                   [b for b in target_button_names if b in skip_set]

                    for btn_name in search_order:
                        # 如果该按钮已连续无效点击3次，本轮跳过
                        if click_fail_count.get(btn_name, 0) >= 3:
                            continue

                        try:
                            # 策略1: get_by_role (标准 button)
                            btn = page.get_by_role("button", name=btn_name, exact=True)
                            count = btn.count()
                            if count > 0:
                                # 遍历所有匹配项，找出最上层的那个
                                for nth in range(count):
                                    handle = btn.nth(nth)
                                    if not handle.is_visible(timeout=100):
                                        continue
                                    box = handle.bounding_box()
                                    if not box or box['width'] <= 0 or box['height'] <= 0:
                                        continue
                                    # 命中测试：检查中心点是否被该元素占据
                                    cx = box['x'] + box['width'] / 2
                                    cy = box['y'] + box['height'] / 2
                                    # 直接把 ElementHandle 传进 JS，避免用浏览器不支持的 :has-text 重新查询
                                    is_on_top = page.evaluate("""({element, x, y}) => {
                                        if (!element) return false;
                                        const top = document.elementFromPoint(x, y);
                                        return top === element || element.contains(top);
                                    }""", {"element": handle.element_handle(), "x": cx, "y": cy})
                                    if is_on_top:
                                        candidate_found = ("role", btn_name, handle, nth)
                                        break
                                if candidate_found:
                                    break

                            # 策略2: 文本底平 (非标准 button，如 div[role="button"])
                            text_btn = page.locator(
                                f'button:has-text("{btn_name}"), '
                                f'[role="button"]:has-text("{btn_name}"), '
                                f'button:text-is("{btn_name}")'
                            )
                            count = text_btn.count()
                            if count > 0:
                                for nth in range(count):
                                    handle = text_btn.nth(nth)
                                    if not handle.is_visible(timeout=100):
                                        continue
                                    box = handle.bounding_box()
                                    if not box or box['width'] <= 0 or box['height'] <= 0:
                                        continue
                                    # 命中测试
                                    cx = box['x'] + box['width'] / 2
                                    cy = box['y'] + box['height'] / 2
                                    # 使用 JS 获取实际元素判断（btn_name 通过 dict 传入，Playwright 只接受单个 arg）
                                    is_on_top = page.evaluate("""({x, y, btnName}) => {
                                        const top = document.elementFromPoint(x, y);
                                        if (!top) return false;
                                        // 向上遍历，看是否在按钮元素内
                                        let el = top;
                                        while (el) {
                                            if (el.getAttribute('role') === 'button' || el.tagName === 'BUTTON') {
                                                return el.textContent.trim().includes(btnName);
                                            }
                                            el = el.parentElement;
                                        }
                                        return false;
                                    }""", {"x": cx, "y": cy, "btnName": btn_name})
                                    if is_on_top:
                                        candidate_found = ("text", btn_name, handle, nth)
                                        break
                                if candidate_found:
                                    break

                        except Exception:
                            continue

                    if candidate_found:
                        strategy, btn_name, btn_handle, nth = candidate_found
                        logger.info(f"发现可见按钮 [{btn_name}] (策略: {strategy}, 第{nth}个，已通过命中测试)")

                        # 执行点击
                        click_success = False
                        try:
                            btn_handle.click(force=True, timeout=2000)
                            click_success = True
                        except Exception as e1:
                            logger.debug(f"标准点击失败: {e1}")
                            try:
                                page.evaluate("(el) => { if(el) el.click(); }", btn_handle.element_handle())
                                click_success = True
                            except Exception as e2:
                                logger.debug(f"JS 点击也失败: {e2}")

                        if click_success:
                            logger.info(f"成功点击按钮 [{btn_name}]")
                            page.wait_for_timeout(1500)

                            # ---- 点击有效性检测：看是否解决了问题 ----
                            # 检查该按钮是否还在页面上（如果是弹窗内的按钮，弹窗关闭后就会消失）
                            try:
                                still_visible = btn_handle.is_visible(timeout=500)
                            except Exception:
                                still_visible = False

                            if still_visible:
                                # 点击了但按钮还在，可能是被遮挡/无效点击
                                click_fail_count[btn_name] = click_fail_count.get(btn_name, 0) + 1
                                logger.warning(
                                    f"点击按钮 [{btn_name}] 后按钮仍然可见 (无效次数: {click_fail_count[btn_name]}/3)"
                                )
                                if click_fail_count[btn_name] >= 3:
                                    logger.warning(f"  按钮 [{btn_name}] 连续3次无效，加入跳过列表，下一轮尝试其他按钮")
                                    skip_set.add(btn_name)
                                # 点击后给予短暂休息，但不立即 continue，让 spinner 检查有机会跳出
                            else:
                                # 点击有效，按钮消失了
                                logger.info(f"  按钮 [{btn_name}] 消失，点击有效，重置其失败计数")
                                click_fail_count[btn_name] = 0
                                if btn_name in skip_set:
                                    skip_set.remove(btn_name)

                            clicked_any = True
                            # 点击后立即进入下一轮循环（检查 spinner 或新弹窗）
                            continue

                    # ---- 如果没有发现任何弹窗按钮，检查 spinner 是否都消失 ----
                    if not clicked_any:
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
                                break
                        except Exception:
                            pass

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
                AUTH_401_THRESHOLD = int(os.getenv("AUTH_FAILURE_THRESHOLD", "5"))  # 连续认证失败阈值（默认5次，可环境变量调整）

                def on_response_post_init(response):
                    """页面完全加载后的 API 认证失败/成功监听"""
                    url = response.url
                    # 只关注真正的 Gemini 推理 API 端点
                    if any(api_pattern in url for api_pattern in [
                        "generativelanguage.googleapis.com",
                        "alkalimakersuite-pa.clients6.google.com",
                    ]):
                        if response.status == 401 or response.status == 403:
                            auth_401_count[0] += 1
                            logger.warning(
                                f"[{diagnostic_tag}] API 认证失败 ({auth_401_count[0]}/{AUTH_401_THRESHOLD}): "
                                f"URL: {url[:100]}... 状态码: {response.status}"
                            )
                            if auth_401_count[0] >= AUTH_401_THRESHOLD:
                                # 不在回调里抛异常（Playwright 事件回调的异常不会传播到主循环），
                                # 仅打日志；由 handle_successful_navigation 的保活循环轮询
                                # auth_401_count 并抛 KeepAliveError 重启自愈
                                logger.error(
                                    f"[{diagnostic_tag}] 连续 {AUTH_401_THRESHOLD} 次 API 认证失败，"
                                    f"等待保活循环重建会话"
                                )
                        elif 200 <= response.status < 300:
                            # 成功的 API call 清零连续失败计数器
                            if auth_401_count[0] > 0:
                                logger.info(f"[{diagnostic_tag}] API 请求成功 ({response.status})，重置 API 认证失败计数器")
                                auth_401_count[0] = 0

                page.on("response", on_response_post_init)
                # ====================================================

                # 5. 所有验证通过，确认成功！
                logger.info("所有验证通过，确认已成功登录并准备就绪")
                handle_successful_navigation(page, logger, diagnostic_tag, shutdown_event, cookie_validator, expected_path, expected_url, auth_401_count, AUTH_401_THRESHOLD)

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
            # 如果是 API 连续认证失败引发的重启，自动清理损坏的 Profile 缓存
            # 注意：保留 fingerprint.json 保持指纹一致，只清理 session/cookies 缓存
            if "API 连续认证失败" in str(e):
                logger.warning(f"检测到 [{diagnostic_tag}] 凭证已失效，准备自动清空 Profile 缓存以重新初始化...")
                try:
                    import shutil
                    for item in os.listdir(profile_dir):
                        if item != "fingerprint.json":
                            item_path = os.path.join(profile_dir, item)
                            if os.path.isdir(item_path):
                                shutil.rmtree(item_path, ignore_errors=True)
                            else:
                                try:
                                    os.remove(item_path)
                                except Exception:
                                    pass
                    logger.info(f"成功清理 [{diagnostic_tag}] Profile 缓存 (指纹特征已保留)")
                except Exception as clean_e:
                    logger.error(f"清理 [{diagnostic_tag}] Profile 缓存失败: {clean_e}")

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
