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


def _slot_position(slot: int, step_x: int, step_y: int,
                   win_w: int, win_h: int) -> tuple:
    """
    槽位 -> 屏幕坐标。沿对角线铺开，到达右/下边界后折回并留 30px 页边，
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


def _screen_point_occluded(px: int, py: int, exclude_win_id: str = None) -> bool:
    """
    用 xdotool 查屏幕上 (px, py) 这一点的最顶层窗口。
    返回 True 表示被别的窗口盖住，False 表示是本窗口或桌面。
    """
    try:
        out = subprocess.run(
            ["xdotool", "getmouselocation", "--shell"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        cur = dict(
            line.split("=", 1) for line in out.strip().splitlines() if "=" in line
        )
        # 临时把鼠标挪到目标点取窗口栈（不影响浏览器逻辑）
        subprocess.run(["xdotool", "mousemove", str(px), str(py)],
                       capture_output=True, timeout=5)
        out = subprocess.run(
            ["xdotool", "getmouselocation", "--shell"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        loc = dict(
            line.split("=", 1) for line in out.strip().splitlines() if "=" in line
        )
        # 还原鼠标位置
        subprocess.run(
            ["xdotool", "mousemove", cur.get("X", "0"), cur.get("Y", "0")],
            capture_output=True, timeout=5,
        )
        top_win = loc.get("WINDOW", "")
        return bool(top_win) and top_win != str(exclude_win_id)
    except Exception:
        return False


def _move_and_verify(win_id: str, x: int, y: int) -> bool:
    """
    移动窗口并确认它真的到了目标位置附近。
    fluxbox 有边框吸附（edge snapping），会把窗口吸到屏幕/窗口边缘，
    所以容差放到 40px：只要不是完全没动，就认为归位成功。
    """
    try:
        subprocess.run(
            ["wmctrl", "-i", "-r", win_id, "-e", f"0,{x},{y},-1,-1"],
            capture_output=True, timeout=10,
        )
        time.sleep(0.5)
        out = subprocess.run(
            ["wmctrl", "-l", "-G"], capture_output=True, text=True, timeout=10
        ).stdout
        for line in out.splitlines():
            parts = line.split(None, 7)
            if len(parts) < 8 or parts[0] != win_id:
                continue
            wx, wy = int(parts[2]), int(parts[3])
            return abs(wx - x) <= 40 and abs(wy - y) <= 40
    except Exception:
        pass
    return False


def _page_fully_covered(win_id: str, wx: int, wy: int,
                        win_w: int, win_h: int) -> bool:
    """
    检查窗口在 (wx, wy) 位置时，其页面区域（去掉标题栏 ~28px）是否
    被其他窗口 100% 盖住。采样窗口中心 + 四角共 5 个点。
    """
    tb = 28  # 标题栏高
    # 页面区域（不含标题栏）
    ix0, iy0 = wx + 10, wy + tb + 10
    ix1, iy1 = wx + win_w - 10, wy + win_h - 10
    samples = [
        ((ix0 + ix1) // 2, (iy0 + iy1) // 2),  # 中心
        (ix0, iy0), (ix1, iy0), (ix0, iy1), (ix1, iy1),  # 四角
    ]
    covered = sum(1 for (px, py) in samples
                  if _screen_point_occluded(px, py, exclude_win_id=win_id))
    return covered == len(samples)


def place_window(source_name: str, logger, browser_pid=None,
                 win_w=_WIN_W, win_h=_WIN_H, attempts=10, interval=2):
    """
    确保当前实例的浏览器窗口不会被其他窗口 100% 盖死（否则触发 occlusion sleep）。

    为什么需要这个函数：
    persistent profile 的 xulstore.json 保存了上次窗口位置，Firefox 会带着
    _NET_CURRENT_DESKTOP 标记复活旧窗口，fluxbox 的 CascadePlacement 对"指定了
    桌面"的窗口直接跳过摆放逻辑（fluxbox Window.cc / Workspace.cc）——所以任何
    实例重启后都会叠在上次的位置，其他实例重启后也叠到同一处，完全遮挡触发
    Firefox occlusion sleep（Linux 上遮挡的是标题栏，页面区域全盖住即判定遮挡）。
    而且 Camoufox 会把 window.screenX/screenY 指纹锁定为指纹文件里的常量（内部
    直接改 window attribute，不走 JS 原型），所以"指纹显示的位置"与"窗口真实
    位置"无关，真实位置必须我们在 WM 层显式管理。

    策略：
    1. 等本实例的浏览器窗口出现（按 PID + 标题匹配）。
    2. 用 xdotool 命中测试判断页面区域是否被完全盖死。
    3. 若被盖死，把它挪到本实例的确定性槽位（slot * step），
       直到找到不被盖死的位置或尝试次数用尽。
    这样首次启动和每次重启后，所有窗口最终都回到互不盖死的级联摆放。
    """
    slot = slot_for(source_name)
    step_x, step_y = _cascade_step()

    for attempt in range(attempts):
        try:
            out = subprocess.run(
                ["wmctrl", "-l", "-p"],
                capture_output=True, text=True, timeout=10,
            ).stdout
            win_id = None
            for line in out.splitlines():
                parts = line.split(None, 4)
                if len(parts) < 5:
                    continue
                wid, _desktop, pid, _host, title = parts
                # 只匹配本实例的浏览器主窗口（有标题栏的顶层窗口），
                # 排除无标题的辅助/隐藏窗口
                if not any(hint in title for hint in _TITLE_HINTS):
                    continue
                # 拿到了 browser_pid 就精确匹配；没拿到就兜底匹配唯一候选
                if browser_pid is not None and int(pid) != browser_pid:
                    continue
                if win_id is not None:
                    # 出现多个候选，放弃以免误动其他实例
                    logger.warning("匹配到多个候选窗口，跳过定位以避免误移动其他实例")
                    return False
                win_id = wid

            if win_id is None:
                time.sleep(interval)
                continue

            # 取窗口当前位置
            out_g = subprocess.run(
                ["wmctrl", "-l", "-G"], capture_output=True, text=True, timeout=10
            ).stdout
            wx = wy = None
            for line in out_g.splitlines():
                parts = line.split(None, 7)
                if len(parts) < 8 or parts[0] != win_id:
                    continue
                wx, wy = int(parts[2]), int(parts[3])
                break
            if wx is None:
                time.sleep(interval)
                continue

            # 若页面区域没被盖死，就保持现状（不再动）
            if not _page_fully_covered(win_id, wx, wy, win_w, win_h):
                logger.debug(f"窗口未被完全遮挡，保持当前位置 ({wx}, {wy})")
                return True

            # 被盖死了：挪到本实例的确定性槽位（沿对角线铺开、边界折回）
            x, y = _slot_position(slot, step_x, step_y, win_w, win_h)
            if _move_and_verify(win_id, x, y):
                logger.info(f"窗口从被完全遮挡处挪到槽位 {slot}: ({x}, {y})")
                return True
            # 挪动失败（fluxbox 夹取/超时），下一轮再试
        except FileNotFoundError:
            logger.warning("未找到 wmctrl/xdotool，跳过窗口定位（交给 fluxbox 自行摆放）")
            return False
        except Exception as e:
            logger.debug(f"窗口定位第 {attempt + 1} 次尝试出错: {e}")

        time.sleep(interval)

    logger.warning(f"窗口定位失败：{attempts} 次尝试后仍未完成")
    return False
