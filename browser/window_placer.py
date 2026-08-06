import os
import re
import subprocess
import time
import zlib

# Xvfb 虚拟屏幕尺寸（与 start.sh 中 Xvfb 的 -screen 0 1920x1080x24 对应）
_SCREEN_W = 1920
_SCREEN_H = 1080

# 浏览器外窗实际尺寸（含标题栏）。
# 注意：指纹里自报的 1440x900 只是伪装值，Camoufox 真实外窗是 1280x772，
# 从运行中的容器 wmctrl -l -G 实测得到。布局必须按真实尺寸算，否则会误判遮挡。
_WIN_W = 1280
_WIN_H = 772

# 匹配浏览器主窗口标题的片段（Camoufox 的标题后缀是 "— Camoufox"，
# 普通 Firefox 是 "— Mozilla Firefox"，两者都兼容）。
_TITLE_HINTS = ("— Camoufox", "— Mozilla Firefox")


def _cascade_step() -> tuple:
    """
    相邻槽位的错开步长：fluxbox 原生 cascade 步长是标题栏高（约 28px），
    这里用 CASCADE_SCALE（默认 1.5，可用环境变量调）按标题栏放大，
    并限制在"任何窗口都不会被 100% 盖死"的安全范围内。
    """
    try:
        scale = float(os.getenv("CASCADE_SCALE", "1.5"))
        if scale <= 0:
            raise ValueError
    except (ValueError, TypeError):
        scale = 1.5
    titlebar = 28  # fluxbox 标题栏高（cascade 原生步长）
    # 露出页边：保证每个窗口右缘/下缘至少露出这么多像素不被盖住
    margin_x, margin_y = 90, 45
    step_x = int(titlebar * scale)
    step_y = int(titlebar * scale)
    step_x = max(1, min(step_x, _SCREEN_W - _WIN_W - margin_x))
    step_y = max(1, min(step_y, _SCREEN_H - _WIN_H - margin_y))
    return step_x, step_y


def slot_for(source_name: str, max_slots: int = 10) -> int:
    """
    从实例名（USER_COOKIE_12 / xxx.json）提取末尾序号作为布局槽位。
    没有序号的来源按名字 crc32 映射。槽位即 (x=slot*STEP_X, y=slot*STEP_Y)，
    与启动顺序无关，每个实例每次重启都回到同一个固定位置。
    """
    m = re.search(r'(\d+)', source_name)
    if m:
        return (int(m.group(1)) - 1) % max_slots
    return zlib.crc32(source_name.encode()) % max_slots


def _slot_position(slot: int, step_x: int, step_y: int,
                   win_w: int, win_h: int) -> tuple:
    """
    槽位 -> 屏幕坐标。沿对角线铺开，到达右/下边界后折回并留页边，
    保证任何槽位的页面区域都不会被其它槽位 100% 盖死。
    """
    max_x = max(_SCREEN_W - win_w, 0)
    max_y = max(_SCREEN_H - win_h, 0)
    x = slot * step_x
    y = slot * step_y
    if x > max_x:
        x = max_x - (x - max_x) % 60  # 从右缘向左折回
    if y > max_y:
        y = max_y - (y - max_y) % 60  # 从下缘向上折回
    return max(0, x), max(0, y)


def _run(cmd, timeout=10):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout


def _pid_matches_profile(pid: str, source_name: str) -> bool:
    """
    判断窗口所属进程是不是本实例：读 /proc/<pid>/cmdline，
    匹配 camoufox 启动参数里的 -profile .../<source_name>。
    source_name 即 profile 目录名（USER_COOKIE_N 或 json 文件名）。
    用路径结尾边界匹配，避免 USER_COOKIE_1 误配 USER_COOKIE_10。
    """
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmdline = f.read().replace(b"\0", b" ").decode("utf-8", "ignore")
        return re.search(
            r'-profile\s+\S*camoufox_profiles/' + re.escape(source_name) + r'(?:\s|$)',
            cmdline,
        ) is not None
    except Exception:
        return False


def _find_own_window(source_name: str):
    """
    在顶层窗口里找到属于本实例的浏览器主窗口 ID。
    按 PID 的 cmdline 精确匹配 profile 路径，不再依赖标题唯一性
    （所有 Camoufox 实例标题相同，靠标题无法区分）。
    """
    out = _run(["wmctrl", "-l", "-p"])
    for line in out.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        win_id, _desktop, pid, _host, title = parts
        # 必须是带标题栏的浏览器主窗口
        if not any(hint in title for hint in _TITLE_HINTS):
            continue
        if _pid_matches_profile(pid, source_name):
            return win_id
    return None


def _window_geometry(win_id: str):
    """返回 (x, y, w, h)，找不到返回 None。"""
    out = _run(["wmctrl", "-l", "-G"])
    for line in out.splitlines():
        parts = line.split(None, 7)
        if len(parts) >= 8 and parts[0] == win_id:
            return int(parts[2]), int(parts[3]), int(parts[4]), int(parts[5])
    return None


def _screen_point_window(px: int, py: int):
    """返回屏幕 (px, py) 处最顶层窗口的 ID（十六进制字符串），失败返回 None。"""
    try:
        out = _run(["xdotool", "getmouselocation", "--shell"], timeout=5)
        cur = dict(l.split("=", 1) for l in out.strip().splitlines() if "=" in l)
        _run(["xdotool", "mousemove", str(px), str(py)], timeout=5)
        out = _run(["xdotool", "getmouselocation", "--shell"], timeout=5)
        loc = dict(l.split("=", 1) for l in out.strip().splitlines() if "=" in l)
        _run(["xdotool", "mousemove", cur.get("X", "0"), cur.get("Y", "0")], timeout=5)
        win = loc.get("WINDOW")
        return f"0x{int(win):08x}" if win else None
    except Exception:
        return None


def _page_fully_covered(win_id: str, wx: int, wy: int,
                        win_w: int, win_h: int) -> bool:
    """
    检查窗口在 (wx, wy) 位置时，其页面区域（去掉标题栏 ~28px）是否
    被其他窗口 100% 盖住。采样中心 + 四角共 5 个点。
    """
    tb = 28
    ix0, iy0 = wx + 10, wy + tb + 10
    ix1, iy1 = wx + win_w - 10, wy + win_h - 10
    samples = [
        ((ix0 + ix1) // 2, (iy0 + iy1) // 2),
        (ix0, iy0), (ix1, iy0), (ix0, iy1), (ix1, iy1),
    ]
    covered = sum(
        1 for (px, py) in samples
        if (lambda top: top and top != win_id)(_screen_point_window(px, py))
    )
    return covered == len(samples)


def place_window(source_name: str, logger, win_w=_WIN_W, win_h=_WIN_H,
                 attempts=10, interval=2):
    """
    确保当前实例的浏览器窗口不会被其他窗口 100% 盖死（否则触发 occlusion sleep）。

    为什么需要这个函数：
    persistent profile 的 xulstore.json 保存了上次窗口位置，Firefox 复活窗口时
    带 _NET_CURRENT_DESKTOP 标记，fluxbox 的 CascadePlacement 对这类窗口直接
    跳过摆放逻辑——所以实例重启后会叠在上次的位置，多个实例重启后叠到同一处，
    完全遮挡触发 Firefox occlusion sleep。而 Camoufox 把 window.screenX/screenY
    指纹锁成常量，真实位置必须我们在 WM 层显式管理。

    策略：
    1. 用 PID cmdline 里的 profile 路径找到本实例的窗口（标题大家都一样，靠
       标题无法区分）。
    2. 用 xdotool 命中测试判断页面区域是否被完全盖死。
    3. 只有盖死时才挪到本实例的确定性槽位；没被盖死则不动，避免与 fluxbox
       首次级联摆放打架。
    """
    slot = slot_for(source_name)
    step_x, step_y = _cascade_step()

    for attempt in range(attempts):
        try:
            win_id = _find_own_window(source_name)
            if win_id is None:
                time.sleep(interval)
                continue

            geo = _window_geometry(win_id)
            if geo is None:
                time.sleep(interval)
                continue
            wx, wy, ww, wh = geo

            # 没被盖死就保持现状
            if not _page_fully_covered(win_id, wx, wy, ww, wh):
                logger.debug(f"窗口未被完全遮挡，保持当前位置 ({wx}, {wy})")
                return True

            # 盖死了：挪到本实例的确定性槽位
            x, y = _slot_position(slot, step_x, step_y, ww, wh)
            _run(["wmctrl", "-i", "-r", win_id, "-e", f"0,{x},{y},-1,-1"])
            time.sleep(0.5)

            new_geo = _window_geometry(win_id)
            if new_geo and abs(new_geo[0] - x) <= 40 and abs(new_geo[1] - y) <= 40:
                logger.info(f"窗口从被完全遮挡处挪到槽位 {slot}: ({new_geo[0]}, {new_geo[1]})")
                return True
            # fluxbox 吸附/夹取导致没到位，下一轮重试
        except FileNotFoundError:
            logger.warning("未找到 wmctrl/xdotool，跳过窗口定位（交给 fluxbox 自行摆放）")
            return False
        except Exception as e:
            logger.debug(f"窗口定位第 {attempt + 1} 次尝试出错: {e}")

        time.sleep(interval)

    logger.warning(f"窗口定位失败：{attempts} 次尝试后仍未完成")
    return False
