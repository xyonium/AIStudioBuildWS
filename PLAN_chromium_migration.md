# Chromium 迁移计划(替代 Camoufox)

**背景**:Camoufox 158.0-beta.40 (daijro/camoufox#550) 在编译补丁层炸掉 WebSocket,top-level 都 TIMEOUT state=0,无法恢复。
**验证**:宿主机 stock Chromium 136 + BrowserForge 指纹 + `--disable-blink-features=AutomationControlled`,原生 WS 直接 OPEN,USER_COOKIE_7 cookie 已成功迁移并实测 WS CONNECTED。

**分支**:`feature/chromium-migration`(绝不动 main,等 user 审核后自己 merge)

---

## 阶段 1:不改业务逻辑，替换浏览器 runtime

### 文件改动清单

#### 1. 新增 `browser/fingerprint.py` (~150 行)

职责：
- `load_or_create_fingerprint(profile_dir)`:
  - 如果 `<profile_dir>/fingerprint.json` 存在且是 BrowserForge 格式 → 直接加载复用
  - 否则 → BrowserForge 生成新指纹 + 序列化到该路径（锁定）
  - 返回 `(fingerprint_obj, injection_js)`
- `build_injection_js(fp)`:
  - 把 fp.navigator (UA/platform/hardwareConc/languages/doNotTrack/maxTouchPoints) + fp.screen (width/height/avail/colorDepth/pixelDepth) + webdriver=False 打包成 add_init_script JS
  - 额外加 `window.chrome = { runtime: {} }` (AI Studio 会检查）
- `export_cookies_from_firefox_db(firefox_profile_dir)`:
  - sqlite3 只读打开 `<dir>/cookies.sqlite`(`mode=ro&immutable=1`，绝不写生产 profile)
  - 抓所有 google/youtube/aistudio/run.app domain 的 cookie
  - expiry: firefox 存毫秒 (13位),Playwright 要秒；转换
  - sameSite 0/1/2/3 → None/Lax/Strict/None,None+!secure 则改 Lax(Chromium 限制）
  - 返回 `[{name, value, domain, path, expires, secure, httpOnly, sameSite}, ...]`
- `migrate_profile_dir(legacy_camoufox_dir, new_chromium_dir)`:
  - 创建新 dir
  - 从 legacy 导出 cookie → 序列化为 `<new_dir>/cookies.json`(cookies 本身存入 JSON，后面 main.py 启动时 add_cookies)
  - 如果 legacy 有 `<legacy>/fingerprint.json`,**不**尝试解析（是 Camoufox 的 launch config，不是 BrowserForge schema)，直接忽略让 BrowserForge 新生成

#### 2. 修改 `browser/instance.py`(~50 行改动）

改动点（**不影响 keep-alive / dialogs / provider_name 注入 / park-and-wait**):

a) 顶部 import:
```python
from playwright.sync_api import sync_playwright
from .fingerprint import (
    load_or_create_fingerprint,
    build_injection_js,
    export_cookies_from_firefox_db,
    migrate_profile_dir,
)
```

b) `_build_camoufox_launch_options()` → 改名 `_build_chromium_launch_options()`，返回 playwright kwargs:
```python
{
  "user_data_dir": new_chromium_profile_dir,
  "headless": False,
  "args": [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    f"--window-size={fp.screen.width},{fp.screen.height}",
    # 保留内存压缩的 Chromium 等效
    "--js-flags=--max-old-space-size=512 --max-semi-space-size=16",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-features=TranslateUI,MemoryPurgeOnFreezing",
    "--mute-audio",
    "--disable-sync",
    "--metrics-recording-only",
    "--password-store=basic",
  ],
}
```

c) `run_browser_instance()` 主循环：
   - `Camoufox(**opts)` → `sync_playwright().chromium.launch_persistent_context(**opts)`
   - 拿到 `context` 后：
     ```python
     injection_js = build_injection_js(fp)
     context.add_init_script(injection_js)
     context.add_init_script(f'window.__PROVIDER_NAME__ = {provider_name!r};')
     ```
   - Cookie 加载：
     - 若 `<profile_dir>/cookies.json` 存在 → `context.add_cookies(json.load(...))`
     - 若 USER_COOKIE_N env 里的 cookie 是 Cookie-Editor JSON → 走 `cookie_handler.convert_cookie_editor_to_playwright` 后 add_cookies（原有逻辑）
   - **`use_persistent_context=True` 模式下 `launch_persistent_context` 返回 BrowserContext 直接**，调用方式一样（`ctx.new_page()` / `ctx.add_init_script` / `ctx.add_cookies`)
   - 后面所有 `page.goto(...)`、`dismiss_stacked_dialogs`、`keep-alive loop`、WS state 检测、`iframe[title="Preview"]` 定位、`MANUAL_LOGIN_TIMEOUT` 等待、`PAUSE_AFTER_RETRIES`、`DAILY_RESTART_TIME` — **原样保留**，因为它们都是 Playwright API,Chrom/FF 通用

d) `firefox_user_prefs` 内存压缩块：
   - **整块删除**, Chromium 用 `args` 里的 `--js-flags` + `--disable-features` 等效
   - 保留 `dom.ipc.processCount: 1` 的意图 → Chromium 默认每个 tab 一个 renderer process，可以通过 `--single-process`（不推荐）或 `--renderer-process-limit=1` + `--process-per-site` 来近似，但**默认行为已够**

e) Proxy / fingerprint JSON 传递：
   - 原代码 Tor proxy 用 `firefox_user_prefs["network.proxy..."]`
   - 换 Chromium: `--proxy-server=...`

#### 3. 新增 `browser/cookie_migrator.py` 独立 CLI (~80 行）

```bash
python -m browser.cookie_migrator \
  --legacy /app/camoufox_profiles/USER_COOKIE_7 \
  --out /app/chromium_profiles/USER_COOKIE_7
```

职责：
- 找到 legacy Firefox profile 里的 cookies.sqlite
- 导出 cookies → 存 `<out>/cookies.json`
- 复制一个 stub `fingerprint.json` 占位（下一阶段首次启动时用 BrowserForge 重新生成并锁定）
- 用户可手动执行一次，批量迁移所有 profile

### 阶段 1 测试步骤

1. 本地 `pip install -r requirements.txt` + `playwright install chromium`
2. `python -m browser.cookie_migrator --legacy ...`
3. `python main.py`（启动全部 7 个 profile)
4. 验证 logs/USER_COOKIE_*.log 显示 `WS CONNECTED`
5. 观察 3-5 分钟：看 cpu/memory 是否和 Camoufox 差不多或更低

---

## 阶段 2:Docker 打包 + 环境隔离

### 4. 修改 `Dockerfile`

改动：

```dockerfile
# 删除原来的:
# RUN camoufox fetch
# ENV PATH=/root/.cache/camoufox:$PATH

# 新增:
RUN pip install --no-cache-dir playwright browserforge && \
    playwright install --with-deps chromium

# 保留 Xvfb + fluxbox + x11vnc + noVNC (Chromium 也要)
```

预期 image size: 3.0GB → 1.2GB(说白了：camoufox-bin ~600M + camoufox-profile cache ~150M + Firefox deps 一大堆被去掉，换成 chromium ~150M)

### 5. 修改 `requirements.txt`

```
camoufox[...] → 删
+ browserforge==1.2.3
+ playwright==1.4x
```

### 6. 修改 `start.sh` 或 `main.py` 启动时判断 profile 目录：

```python
# main.py 启动时统一做一次迁移检查
from browser.cookie_migrator import needs_migration, migrate_all
if needs_migration(os.environ.get("FF_LEGACY_ROOT", "/app/camoufox_profiles")):
    migrate_all(...)
```

切换 profile root:
```
CAMOUFOX_PROJECT_ROOT=/new/chromium_profiles  # use new env var
FF_LEGACY_ROOT=/app/camoufox_profiles        # legacy source
```

### 7. `docker-compose` / `.env` 兼容

- 不动：`USER_COOKIE_N` 仍然支持（保留原 CookieManager 接口）
- 新增：`CHROMIUM_PROFILE_ROOT`（默认 `/app/chromium_profiles`)
- 废弃：`CAMOUFOX_PROJECT_ROOT`（替换为上面的）

---

## 审核重点（给你找的 agent 看）

我个人建议你让审核 agent 在这些特定地方多花心思：

1. **`browser/instance.py` 的 `_build_chromium_launch_options()`** — 检查所有 launch 参数
   - `--disable-blink-features=AutomationControlled` 是否真的必要？(Chromium 136 里 navigator.webdriver 由这个 flag 控制）
   - Playwright 默认 args 里有没有坑？需要看 playwright 源码 `chromium.py` 的默认 `chromium_sandbox` 参数
2. **`browser/fingerprint.py` 的 `build_injection_js`** — 检查 add_init_script escape
   - UA 里有反斜杠或单引号会不会 break JS？
   - `!r` Python repr 是否等价于 JS 字符串字面量？（未必，需手写 escape)
3. **指纹一致性**:
   - BrowserForge 每次 `FingerprintGenerator().generate()` 返回的指纹**是随机的**
   - 多实例必须使用 `fp.to_json()`/`fp.from_json()` 固定指纹，否则每次重启都换指纹 → Google 风控
   - 我代码里必须加这个锁定逻辑
4. **cookie 迁移的 domain 匹配**:
   - `.google.com` vs `google.com` vs `.aistudio.google.com` — Chromium 对 domain 前缀 `.` 处理要严格，我加的 filter 是否太宽抓错？
5. **内存压缩**:
   - firefox `firefox_user_prefs` 内存压缩被翻译为 `--js-flags` 是不完全等效的
   - 7 个实例跑起来后 memory/CPU 是否比 Camoufox 高？需要上线后监控
6. **stacked-dialog 四支柱保持**(user 特别强调）:
   - debounce / topmost-only / elementFromPoint hit-testing / 3-strike skip — 不应该被任何 Playwright-Driver 切换影响
7. **cross-process shutdown**:
   - `multiprocessing.Event` + SIGTERM → SIGKILL escalation 链条，切换 Chromium 后 browser.terminate() 时必须 kill 干净
   - Chromium 不像 Camoufox 那样对 SIGTERM 立刻退出，可能需要 `context.close()` + 手动 pkill

---

## 审核通过后 → 切换全部 profile

1. 本地 build test 正常 → push 分支 → PR 给 user 审
2. user merge 后 → 重新 build image → deploy 新容器（并行老容器跑，不要马上 kill)
3. 新容器启动后用**火墙**保留旧 profile 目录，万一需要 rollback 只需要把 `docker compose stop aistudio-websocket-app` 换成老的 image tag
4. 观察 24 小时，所有 7 个 WS CONNECTED 稳定 → kill 老容器 + 清理旧 image
5. 保留 `/mnt/docker/aistdprx/camoufox_profiles/` 至少 30 天（万一被风控，需要 cookie 重新开始登录）

---

## 已知风险（我不是 100% 靠谱的部分）

- Google 风控对 UA 突变（Firefox → Chromium）的敏感度未知，我 test 通过了 USER_COOKIE_7 但只跑了 ~3 min
- BrowserForge 的指纹模板可能比 Camoufox 弱（Camoufox 是专门 anti-detect),Google 可能 detect headless-ish behavior。需要观察长期是否被 limit
- Chromium renderer process 内存比 Firefox 单 process 模式占用高。7 个实例的 docker host 内存可能不够（用户需要观察)
