import os
import signal
import time
import json
import subprocess
import gc
from urllib.parse import urlparse
from playwright.sync_api import (
    sync_playwright,
    TimeoutError,
    Error as PlaywrightError,
)
from utils.logger import setup_logging
from utils.cookie_manager import CookieManager
from browser.navigation import handle_successful_navigation, KeepAliveError
from browser.cookie_validator import CookieValidator
from browser.park_and_wait import park_and_wait
from utils.paths import logs_dir
from utils.common import parse_headless_mode, ensure_dir, clean_env_value
from utils.url_helper import extract_url_path, mask_url_for_logging, mask_path_for_logging
from browser.window_placer import place_window
from browser.fingerprint import (
    load_or_create_fingerprint,
    build_injection_js,
    build_context_headers,
    ensure_profile_migrated,
    load_migrated_cookies,
    has_chromium_cookie_db,
)


class _ChromiumCtxMgr:
    """Context manager wrapping sync_playwright().chromium.launch_persistent_context.

    `Camoufox(**opts).__enter__()` previously returned a BrowserContext. This
    adapter preserves that shape for callers: `with X(**opts) as context:`.

    playwright is entered and owned by us; on exit we close the context and
    then stop playwright, in that order, so that all browser processes are
    terminated deterministically (Chromium child processes do not die on
    parent SIGTERM alone).
    """

    def __init__(self, **kwargs):
        self._kwargs = kwargs
        self._pw = None
        self._ctx = None

    def __enter__(self):
        self._pw = sync_playwright().start()
        # launch_persistent_context always returns a BrowserContext; we don't
        # need `.browser` (it will be None for persistent contexts).
        self._ctx = self._pw.chromium.launch_persistent_context(**self._kwargs)
        return self._ctx

    def __exit__(self, exc_type, exc, tb):
        try:
            if self._ctx is not None:
                self._ctx.close()
        except Exception:
            pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass
        self._ctx = None
        self._pw = None
        return False


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
        # Chromium 也支持 launch_options["proxy"](BrowserContext 级别)。注意:
        # 1. proxy 值应为字符串("http://h:p")或 dict;CookieManager 传的就是 str
        # 2. bypass 域名列表用于跳过部分目标
        launch_options["proxy"] = {"server": proxy, "bypass": "localhost, 127.0.0.1"}

    screenshot_dir = logs_dir()
    ensure_dir(screenshot_dir)

    # ================= [多用户 Profile 和指纹管理 (Chromium migration)] =================
    # Profile 根目录优先使用 CHROMIUM_PROFILE_ROOT；若未设置但已挂载 /app/chromium_profiles
    # 则优先用 /app/chromium_profiles，否则兼容现有的 /app/camoufox_profiles 挂载。
    _env_profile_root = clean_env_value(os.getenv("CHROMIUM_PROFILE_ROOT"))
    if _env_profile_root:
        profiles_base_dir = _env_profile_root
    elif os.path.isdir("/app/chromium_profiles"):
        profiles_base_dir = "/app/chromium_profiles"
    else:
        profiles_base_dir = "/app/camoufox_profiles"
    ensure_dir(profiles_base_dir)

    # 使用 identifier (如 USER_COOKIE_1) 作为文件夹名，实现多用户完全隔离
    profile_dir = os.path.join(profiles_base_dir, diagnostic_tag)
    ensure_dir(profile_dir)

    # 若当前 profile 尚未生成 cookies.json 且存在旧 Camoufox 的 cookies.sqlite，
    # 自动执行一次只读拷贝导出（含 WAL），确保无缝迁移登录态。
    try:
        mig_msg = ensure_profile_migrated(profile_dir, diagnostic_tag)
        if mig_msg:
            logger.info(f"[{diagnostic_tag}] 自动从旧 Camoufox profile 迁移 Cookie: {mig_msg}")
    except Exception as mig_e:
        logger.warning(f"[{diagnostic_tag}] 自动迁移旧 Cookie 时出错（将继续）: {mig_e}")

    # BrowserForge 指纹的第一次生成会被立即写盘锁定 → 后续同 profile 复用同一指纹。
    # 若遇到旧版 Camoufox fingerprint.json 会先备份为 fingerprint.camoufox.bak.json。
    fp = load_or_create_fingerprint(profile_dir)
    logger.info(
        f"[{diagnostic_tag}] fingerprint ready: "
        f"ua={fp['navigator']['userAgent'][:60]}... "
        f"screen={fp['screen']['width']}x{fp['screen']['height']}"
    )

    # ---- Chromium launch options ----
    # Camoufox 的 headless 支持三态(True / False / 'virtual':Xvfb 由外部负责显示),
    # 但纯 Playwright 只接受 bool。'virtual' 实际语义是"非 headless,由 Xvfb 提供显示",
    # 对应到这里就等于 headless=False。
    _hl = parse_headless_mode(headless_setting)
    headless_parsed = (_hl is True)  # 'virtual' 和 False 都映射为 False

    # 合并 Playwright 默认 disabledFeatures，避免被新的 --disable-features 覆盖
    _pw_disabled_features = (
        "AcceptCHFrame,AutoExpandDetailsElement,AvoidUnnecessaryBeforeUnloadCheckSync,"
        "CertificateTransparencyComponentUpdater,DeferRendererTasksAfterInput,"
        "DestroyProfileOnBrowserClose,DialMediaRouteProvider,ExtensionManifestV2Disabled,"
        "GlobalMediaControls,HttpsUpgrades,ImprovedCookieControls,LazyFrameLoading,"
        "LensOverlay,MediaRouter,PaintHolding,ThirdPartyStoragePartitioning,Translate,"
        "TranslateUI,MemoryPurgeOnFreezing,OptimizationGuideModelDownloading"
    )
    chromium_args = [
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
        "--no-default-browser-check",
        f"--window-size={fp['screen']['width']},{fp['screen']['height']}",
        # ---- Camoufox 原本用 firefox_user_prefs 做的事,Chromium 等效: ----
        # v8 内存压缩(代替 javascript.options.mem.max/high_water_mark)
        "--js-flags=--max-old-space-size=384 --max-semi-space-size=16",
        # 不要禁用 renderer background throttling 或 Chromium 会把后台 tab 掉线
        # (AI Studio 如果不过 keep-alive 到 WS 就会 1001 掉线,生产已踩过)
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        # 禁用遥测与后台任务（合并 Playwright 默认禁用项，防止覆盖）
        f"--disable-features={_pw_disabled_features}",
        "--mute-audio",
        "--disable-sync",
        "--disable-component-update",
        "--disable-default-apps",
        "--metrics-recording-only",
        "--password-store=basic",
        "--no-service-autorun",
        # DNS/Network 预解析等价于 network.prefetch-next=False
        "--disable-domain-reliability",
        "--disable-client-side-phishing-detection",
        # 禁用 gpu 硬件加速(避免容器里缺 GPU 触发黑屏/崩)
        "--disable-gpu",
        "--disable-dev-shm-usage",  # /dev/shm 默认仅 64MB,Chromium 需要 fallback
        # 避免 Chromium 尝试检测 occluded windows / 挂起(对应 page_visibility.dont_suspend_inactive)
        "--disable-hang-monitor",
        "--disable-prompt-on-repost",
        "--disable-ipc-flooding-protection",
    ]
    # 注意:proxy 通过 launch_options["proxy"] 传(BrowserContext 层),不写 --proxy-server args
    # 否则 persistent_context 下 args 优先级更高会覆盖 bypass,并且与 Playwright 的 proxy error 处理冲突

    nav_lang = (fp.get("navigator", {}).get("languages") or ["en-US"])[0]

    launch_options.update({
        "headless": headless_parsed,
        "user_data_dir": profile_dir,
        "args": chromium_args,
        # 移除 Playwright 默认注入的 --enable-automation 标志
        "ignore_default_args": ["--enable-automation", "--disable-features"],
        # 严禁在此处传入 user_agent 或 extra_http_headers(sec-ch-ua*)！
        # 根因：Google accounts.google.com 登录时会通过 JS/UACH 综合指纹校验真实二进制版本。
        # 容器内真实 Chromium 是 136，若把 BrowserForge 生成的 Chrome 147 UA 或伪造的
        # Navigator.prototype / WebGL Proxy 注入进去，Google 登录页会立即拦截并报错：
        # "This browser or app may not be secure. Try using a different browser."
        # 保持原生 Chromium UA 与原生 Navigator，仅靠 --disable-blink-features=AutomationControlled
        # 和 --enable-automation 移除即可通过 Google 登录安全检查！
        "viewport": {
            "width": int(fp["screen"]["width"]),
            "height": int(fp["screen"]["height"]),
        },
        "locale": nav_lang,
    })

    # =========================================================================
    # 重启控制变量
    max_retries = int(os.getenv("MAX_RESTART_RETRIES", "5"))
    # 人工干预等待超时（秒），通过环境变量配置，默认 10 分钟
    manual_login_timeout = int(os.getenv("MANUAL_LOGIN_TIMEOUT", "600"))
    retry_count = 0
    base_delay = 3

    # =====================================================================
    # 停车/接管辅助：目标页"已就绪"判定器
    # =====================================================================
    # 判据与启动时的 is_at_target_url 保持一致：host 必须落在 ai.studio 白
    # 名单内（accounts.google.com 的 continue= 参数里含目标路径，纯路径
    # 匹配会把登录页误判成"已到达"），且当前 path 含期望 path。
    # 返回的是闭包，读取的是当前 page.url，因此可反复调用。
    def _build_is_ready(current_page, expected_path_str):
        def _check():
            host = urlparse(current_page.url).hostname or ""
            if not any(host == h or host.endswith("." + h) for h in ("ai.studio", "aistudio.google.com")):
                return False
            current_path = extract_url_path(current_page.url)
            return bool(expected_path_str) and expected_path_str in current_path
        return _check

    # =====================================================================
    # 浏览器已死时的"停车"重建器
    # =====================================================================
    # 三种认输路径里 SystemExit(1) / 重试耗尽 / 未预期异常 都发生在 with
    # Camoufox 已销毁之后——page 是死的，没法就地停车。这里统一用**同一套
    # fingerprint + profile 目录**重新 launch 一个浏览器，只做两件事：
    #   1. 注入 __PROVIDER_NAME__ init script（否则停去的 google.com 页面
    #      不含 provider 名，后续人工导航回 AI Studio 时 WS URL 会缺名字，
    #      CLIProxyAPI 又会注册成随机 aistudio-XXXX）；
    #   2. 打开 page 供调用方 park_and_wait 停车。
    # 重建这一步会碰 Google 一次，这是用户明确接受的；它发生在**已经耗尽
    # 所有自动重试之后**，不会放大反爬信号。
    def _inject_cookies_if_needed(target_context):
        """仅在 Chromium 自身 Cookie 库尚未初始化时注入一次迁移/初始 Cookie。

        关键原因：Google 的 SIDTS / SIDCC 等会话 Cookie 在运行期间高频滚动刷新并
        保存在 Chromium 的 `<profile_dir>/Default/Cookies` 中。如果每次重启（如
        每日 07:33 内存回收重启）都拿静态的 `cookies.json` 或环境变量里的初始
        Cookie 重新调用 `add_cookies()`，会用几天前的旧值覆盖 Chromium 刷新后的
        最新 Cookie，导致 Google 判定会话回放/异常而踢下线。
        """
        already_initialized = os.path.exists(os.path.join(profile_dir, ".chromium_cookies_seeded")) and has_chromium_cookie_db(profile_dir)
        if already_initialized:
            logger.info(f"[{diagnostic_tag}] 检测到 Chromium 持久化 Cookie 库已就绪，直接使用本地最新会话状态")
            return

        seeded_any = False
        # 1. 先加载环境变量/cookies/*.json 中的基础配置
        if cookies:
            try:
                target_context.add_cookies(cookies)
                seeded_any = True
            except Exception as ce:
                logger.warning(f"[{diagnostic_tag}] 注入环境变量/文件初始 Cookie 时部分失败: {ce}")

        # 2. 再加载从旧 Camoufox/Firefox profile 导出的完整登录态（优先级更高，覆盖同名占位符）
        migrated_cookies = load_migrated_cookies(profile_dir)
        if migrated_cookies:
            logger.info(f"[{diagnostic_tag}] 首次初始化：加载 {len(migrated_cookies)} 条从 Firefox profile 迁移的 Cookie")
            try:
                target_context.add_cookies(migrated_cookies)
                seeded_any = True
            except Exception as me:
                logger.warning(f"[{diagnostic_tag}] 批量注入迁移 Cookie 异常，尝试逐条注入: {me}")
                ok_cnt = 0
                for c in migrated_cookies:
                    try:
                        target_context.add_cookies([c])
                        ok_cnt += 1
                    except Exception:
                        pass
                logger.info(f"[{diagnostic_tag}] 逐条注入完成: {ok_cnt}/{len(migrated_cookies)} 条成功")
                if ok_cnt > 0:
                    seeded_any = True

        if seeded_any:
            try:
                with open(os.path.join(profile_dir, ".chromium_cookies_seeded"), "w", encoding="utf-8") as sf:
                    sf.write(str(time.time()))
            except Exception:
                pass

    def _relaunch_browser_for_park(reason_str):
        logger.warning(f"[{diagnostic_tag}] 浏览器已死（{reason_str}），正在重建以便停车等待人工...")
        context_manager = _ChromiumCtxMgr(**launch_options)
        context = context_manager.__enter__()
        _inject_cookies_if_needed(context)

        # 与正常启动保持一致的 provider 注入
        provider_name = cookie_source.display_name
        if provider_name.lower().endswith(".json"):
            provider_name = provider_name[:-5]
        provider_name_override_env = "PROVIDER_NAME_" + "".join(
            c if c.isalnum() else "_" for c in provider_name.upper()
        )
        provider_name = clean_env_value(os.getenv(provider_name_override_env)) or provider_name
        _init_script = f"window.__PROVIDER_NAME__ = {json.dumps(provider_name)};\n" + build_injection_js(fp)
        context.add_init_script(_init_script)

        new_page = context.pages[0] if context.pages else context.new_page()
        return context_manager, context, new_page

    while True:
        # 检查是否收到全局关闭信号
        if shutdown_event and shutdown_event.is_set():
            logger.info("检测到全局关闭事件，浏览器实例不再启动，准备退出")
            return

        # ====== 启动前强制清理当前实例的僵尸进程和回收内存 ======
        try:
            # 强制 Python 垃圾回收，释放上一轮残留的 Playwright 对象
            gc.collect()

            # 【安全修复】只清理属于当前 profile 的孤儿进程（含 crashpad_handler），避免误杀其他用户实例
            # Chromium 主进程/renderer 含 --user-data-dir=<profile_dir>，
            # chrome_crashpad_handler 含 --database=<profile_dir>/Crash Reports
            # 注意：正则必须精确匹配完整目录名边界，否则 USER_COOKIE_1 会误杀 USER_COOKIE_10/11
            escaped_profile_dir = profile_dir.replace("/", "\\/")
            subprocess.run(
                f"pkill -f 'chrome.*(--user-data-dir|--database)={escaped_profile_dir}(/| |$)' || true",
                shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            # 清理 Chromium 可能残留的 SingletonLock（容器重启或异常强杀后会导致无法启动）
            for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
                lock_path = os.path.join(profile_dir, lock_name)
                if os.path.lexists(lock_path):
                    try:
                        os.unlink(lock_path)
                    except Exception:
                        pass
            time.sleep(2)  # 给 OS 一点时间回收内存
            logger.debug(f"已完成启动前清理（垃圾回收 + {diagnostic_tag} 僵尸进程清理）")
        except Exception:
            pass
        # ====================================================

        try:
            # === 持久化上下文:Chromium launch_persistent_context 返回 BrowserContext ===
            with _ChromiumCtxMgr(**launch_options) as context:
                # 获取持久化上下文中已有的默认页面，如果没有则新建
                page = context.pages[0] if context.pages else context.new_page()

                # 首次启动注入迁移/初始 Cookie；后续重启直接复用 Chromium Default/Cookies
                _inject_cookies_if_needed(context)

                # ====== 注入稳定的 provider 名称，供 AI Studio App 注册 CLIProxyAPI 时使用 ======
                # App 代码在 Preview iframe 内通过 window.__PROVIDER_NAME__ 读取该值，
                # 拼到 WS 连接 URL 的 provider_name 参数上；带 ProviderFactory 补丁的
                # CLIProxyAPI 会以该名注册 provider，重连/重启时替换同名旧会话，
                # 而不是每次产生新的随机 aistudio-XXXX，保持用量统计归属稳定。
                # add_init_script 对 context 内所有 frame（含跨域 iframe）的每次导航生效。
                # 默认取 Cookie 来源名（USER_COOKIE_N / 文件名去 .json），与账户一一对应；
                # 可用 PROVIDER_NAME_<来源名大写> 环境变量覆盖（如 PROVIDER_NAME_USER_COOKIE_1=alice）。
                provider_name = cookie_source.display_name
                if provider_name.lower().endswith(".json"):
                    provider_name = provider_name[:-5]
                provider_name_override_env = "PROVIDER_NAME_" + "".join(
                    c if c.isalnum() else "_" for c in provider_name.upper()
                )
                provider_name = clean_env_value(os.getenv(provider_name_override_env)) or provider_name
                _init_script = f"window.__PROVIDER_NAME__ = {json.dumps(provider_name)};\n" + build_injection_js(fp)
                context.add_init_script(_init_script)
                logger.info(f"已注入 provider 名称: {provider_name}（可用环境变量 {provider_name_override_env} 覆盖）")

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
                # 目标页域名白名单：ai.studio 与 aistudio.google.com 互为别名（启动时 ai.studio
                # 会 301 到 aistudio.google.com），到达其中任一域名才算"到了目标页"。
                # 关键：不能只比路径！Google 登录页的 URL 长这样
                #   https://accounts.google.com/v3/signin/identifier?continue=https://aistudio.google.com/apps/<ID>
                # continue 参数（未编码时）原文包含目标路径，纯路径子串匹配会把登录页
                # 误判成"已到达目标页"，跳过人工登录等待，随后 iframe 等待超时 ->
                # 重启循环重建页面，人工根本来不及登录（403 清 Profile 后必现）。
                target_hosts = ("ai.studio", "aistudio.google.com")

                def is_at_target_url():
                    host = urlparse(page.url).hostname or ""
                    if not any(host == h or host.endswith("." + h) for h in target_hosts):
                        return False
                    current_path = extract_url_path(page.url)
                    return bool(expected_path) and expected_path in current_path

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
                        # 不是"放弃"而是"停车等待"：浏览器还活着、cookie/profile 都在，
                        # 把页面导航到 google.com 停止一切自动交互，等人工把地址开回
                        # AI Studio 后接着走完初始化（复用当前这个 page，不起新浏览器）。
                        logger.warning(
                            f"[{diagnostic_tag}] {manual_login_timeout}秒内未到达目标页面，"
                            f"改为停车等待模式（不再自动触碰 Google）"
                        )
                        page.screenshot(path=os.path.join(screenshot_dir, f"FAIL_manual_action_timeout_{diagnostic_tag}.png"))
                        is_ready = _build_is_ready(page, expected_path)
                        if park_and_wait(page, diagnostic_tag, logger, is_ready, shutdown_event):
                            logger.info(f"[{diagnostic_tag}] 人工操作完成，成功到达目标页面！")
                        else:
                            # 只在 PARK_TIMEOUT>0 且超时、或收到全局关闭时走到这里
                            logger.info(f"[{diagnostic_tag}] 停车等待结束（超时或收到关闭信号），退出实例")
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
                auth_401_count = [0]  # 各端点"连续失败数"的最大值，供保活循环轮询
                AUTH_401_THRESHOLD = int(os.getenv("AUTH_FAILURE_THRESHOLD", "3"))  # 同一端点连续认证失败阈值（默认3次，可环境变量调整）
                auth_fail_by_method = {}  # rpc方法名 -> 该方法连续失败次数

                def _rpc_method_name(url):
                    """提取 API 方法名（去掉 query，避免泄露 key/token 等参数）"""
                    tail = url.split('?')[0].rstrip('/').rsplit('/', 1)[-1]
                    # $rpc 全路径较长（如 google.internal...MakerSuiteService.CountTokens），只保留方法名
                    return tail.rsplit('.', 1)[-1] if '.' in tail else tail

                def on_response_post_init(response):
                    """页面完全加载后的 API 认证失败/成功监听"""
                    url = response.url
                    # 只关注真正的 Gemini 推理 API 端点
                    if any(api_pattern in url for api_pattern in [
                        "generativelanguage.googleapis.com",
                        "alkalimakersuite-pa.clients6.google.com",
                    ]):
                        req_method = response.request.method
                        # CORS 预检 OPTIONS 恒返回 200，不代表真实 API 成功，必须忽略——
                        # 否则每次真实请求 403 前都有一个预检 200 把计数器清零，阈值永远到不了
                        if req_method in ("OPTIONS", "HEAD"):
                            return
                        method_name = _rpc_method_name(url)
                        if response.status == 401 or response.status == 403:
                            auth_fail_by_method[method_name] = auth_fail_by_method.get(method_name, 0) + 1
                            count = auth_fail_by_method[method_name]
                            auth_401_count[0] = max(auth_fail_by_method.values())
                            # 401 = 令牌/登录态失效；403 = 权限、配额或来源风控（均非网络故障）
                            hint = "登录态/令牌失效" if response.status == 401 else "权限/配额/来源限制"
                            # 首次失败时抓取响应体（Google 返回 JSON error，含 PERMISSION_DENIED/QUOTA 等具体原因）
                            body_snippet = ""
                            if count == 1:
                                try:
                                    ctype = response.headers.get("content-type", "")
                                    if "json" in ctype or "text" in ctype:
                                        body_snippet = f" | 响应体: {response.text()[:300]}"
                                except Exception:
                                    pass
                            logger.warning(
                                f"[{diagnostic_tag}] API 认证失败 [{method_name}] ({count}/{AUTH_401_THRESHOLD}): "
                                f"{req_method} {url.split('?')[0][:120]} 状态码: {response.status} ({hint}){body_snippet}"
                            )
                            if count >= AUTH_401_THRESHOLD:
                                # 不在回调里抛异常（Playwright 事件回调的异常不会传播到主循环），
                                # 仅打日志；由 handle_successful_navigation 的保活循环轮询
                                # auth_401_count 并抛 KeepAliveError 重启自愈
                                logger.error(
                                    f"[{diagnostic_tag}] 端点 [{method_name}] 连续 {AUTH_401_THRESHOLD} 次 API 认证失败，"
                                    f"等待保活循环重建会话"
                                )
                        elif 200 <= response.status < 300:
                            # 只重置同一方法的连续失败计数——同主机上其他端点的成功
                            # 不能掩盖本端点的持续失败（否则阈值同样永远到不了）
                            if auth_fail_by_method.get(method_name, 0) > 0:
                                logger.info(
                                    f"[{diagnostic_tag}] API 请求成功 [{method_name}] ({response.status})，"
                                    f"重置该方法失败计数"
                                )
                                auth_fail_by_method[method_name] = 0
                                auth_401_count[0] = max(auth_fail_by_method.values())

                page.on("response", on_response_post_init)
                # ====================================================

                # 5. 所有验证通过，确认成功！
                logger.info("所有验证通过，确认已成功登录并准备就绪")

                # 防止窗口被其他实例 100% 盖死（会触发 Firefox occlusion sleep）。
                # 根因：persistent profile 的 xulstore.json 会让 Firefox 带着
                # _NET_CURRENT_DESKTOP 标记复活窗口，fluxbox 的 CascadePlacement
                # 对这类窗口直接跳过摆放，所以重启后窗口会叠在上次的位置。
                # 在页面完全加载后调用（此时窗口已稳定存在，重启瞬间窗口/标题
                # 可能还没就绪，太早调用会找不到窗口而归位失败）。把窗口摆到
                # 本实例的确定性槽位，保证和其它实例错开。
                place_window(diagnostic_tag, logger)

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
            # 注意：绝不清空 Chromium 的 Default/Cookies 持久化目录！
            # 原因：Google 账号在容器内完成一次手动或自动登录后，其有效登录态（SIDTS/OSID）
            # 保存在 Default/Cookies 中；而从旧 Firefox 导出的 cookies.json 里有些账号已经
            # Signed out。如果因为 403 清空了 Default/Cookies，就会把好不容易登录好的账号
            # 强行退回 Signed out 状态！
            if "API 连续认证失败" in str(e):
                logger.warning(f"检测到 [{diagnostic_tag}] API 连续认证失败，将重启浏览器实例刷新会话（保留现有登录态 Profile）...")

            retry_count += 1
            if retry_count > max_retries:
                # 不再"自杀退出"，改为"停车等待人工"。浏览器已被 with 销毁，
                # 必须重建一个（同 fingerprint + 同 profile 目录）再停车。
                logger.error(f"重试次数已达上限 ({max_retries})，不再自动重试以避免反爬误判，转为停车等待人工")
                context_manager = None
                try:
                    context_manager, _context, park_page = _relaunch_browser_for_park("重试耗尽")
                    is_ready = _build_is_ready(park_page, expected_path)
                    if park_and_wait(park_page, diagnostic_tag, logger, is_ready, shutdown_event):
                        logger.info(f"[{diagnostic_tag}] 人工已在停车页把地址开回 AI Studio，重新走初始化流程")
                        retry_count = 0  # 接管成功，重试计数归零
                        continue
                    else:
                        retry_count = 0
                        logger.info(f"[{diagnostic_tag}] 停车等待结束（超时或收到关闭信号），退出实例")
                        return
                except Exception as park_e:
                    logger.error(f"[{diagnostic_tag}] 重试耗尽后停车失败（{park_e}），放弃并退出实例")
                    return
                finally:
                    if context_manager is not None:
                        try:
                            context_manager.__exit__(None, None, None)
                        except Exception:
                            pass
            # 指数退避：3秒、6秒、12秒、24秒...最长60秒
            delay = min(base_delay * (2 ** (retry_count - 1)), 60)
            logger.error(f"浏览器实例出现错误 (重试 {retry_count}/{max_retries})，将在 {delay} 秒后重启浏览器实例: {e}")
            time.sleep(delay)
            continue
        except KeyboardInterrupt:
            logger.info(f"用户中断，正在关闭...")
            return
        except SystemExit as e:
            # cookie 验证 3 次网络超时（cookie_validator.py 抛的 SystemExit(1)）会落到
            # 这里。此刻浏览器已随 with 退出销毁，page 不可用；停车必须重建一个浏览器，
            # 用同一 fingerprint + 同一 profile 目录，再停到 google.com 等人工。
            if e.code == 1:
                logger.error(f"[{diagnostic_tag}] Cookie 验证连续网络超时，浏览器已销毁，重建以便停车等待人工...")
                context_manager = None
                try:
                    context_manager, _context, park_page = _relaunch_browser_for_park("SystemExit(1)")
                    is_ready = _build_is_ready(park_page, expected_path)
                    if park_and_wait(park_page, diagnostic_tag, logger, is_ready, shutdown_event):
                        logger.info(f"[{diagnostic_tag}] 人工已在停车页把地址开回 AI Studio，重新走初始化流程")
                        retry_count = 0  # 接管成功，重试计数归零
                        continue
                    else:
                        retry_count = 0
                        logger.info(f"[{diagnostic_tag}] 停车等待结束（超时或收到关闭信号），退出实例")
                        return
                except Exception as park_e:
                    logger.error(f"[{diagnostic_tag}] SystemExit 后停车失败（{park_e}），放弃并退出实例")
                    return
                finally:
                    if context_manager is not None:
                        try:
                            context_manager.__exit__(None, None, None)
                        except Exception:
                            pass
            else:
                logger.info(f"实例正常退出，退出码: {e.code}")
                return
        except Exception as e:
            # 未预期异常也一律停车等人工，而不是直接死掉
            logger.exception(f"运行 Camoufox 实例时发生未预料的严重错误: {e}")
            context_manager = None
            try:
                context_manager, _context, park_page = _relaunch_browser_for_park("未预期异常")
                is_ready = _build_is_ready(park_page, expected_path)
                if park_and_wait(park_page, diagnostic_tag, logger, is_ready, shutdown_event):
                    logger.info(f"[{diagnostic_tag}] 人工已在停车页把地址开回 AI Studio，重新走初始化流程")
                    retry_count = 0  # 接管成功，重试计数归零
                    continue
                else:
                    retry_count = 0
                    logger.info(f"[{diagnostic_tag}] 停车等待结束（超时或收到关闭信号），退出实例")
                    return
            except Exception as park_e:
                logger.error(f"[{diagnostic_tag}] 未预期异常后停车失败（{park_e}），放弃并退出实例")
                return
            finally:
                if context_manager is not None:
                    try:
                        context_manager.__exit__(None, None, None)
                    except Exception:
                        pass
