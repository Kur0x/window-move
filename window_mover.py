"""
window_mover.py — 自动移动前台窗口，减少视觉疲劳

支持两种模式（config.ini 可配置）：
  smooth_drift : 窗口持续平滑漂移，碰边反弹
  timed_jump   : 每隔 N 秒随机跳跃到新位置

系统托盘图标提供暂停/恢复、切换模式、调速、退出功能。
热键（默认 Ctrl+Shift+P）可随时暂停/恢复。

速度/距离参数说明：
  speed       : 每秒移动像素数（smooth_drift）
  step_distance : 每次实际移动像素数，越大越不平滑（smooth_drift）
  min_travel  : 每段方向最少移动像素数，越大转向越少（smooth_drift）
  interval    : 跳跃间隔秒数（timed_jump）
  max_jump_distance : 单次跳跃最大像素距离，0 = 不限（timed_jump）
"""

import configparser
import math
import os
import random
import threading
import time

import keyboard
import win32api
import win32con
import win32gui
from PIL import Image, ImageDraw
import pystray

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.ini")

# 速度预设（每秒像素数）
SPEED_PRESETS = [10, 20, 40, 60, 100, 150, 200]
# 单步移动距离预设（像素）
STEP_PRESETS = [2, 4, 6, 10, 20, 30, 40]
# min_travel 预设（像素）
TRAVEL_PRESETS = [50, 100, 150, 250, 400, 600]


def load_config(path: str = CONFIG_PATH) -> dict:
    cp = configparser.ConfigParser()
    cp.read(path, encoding="utf-8")
    return {
        "mode":              cp.get("general",      "mode",               fallback="smooth_drift"),
        "padding":           cp.getint("general",   "padding",            fallback=50),
        "hotkey":            cp.get("general",      "hotkey",             fallback="ctrl+shift+p"),
        "speed":             cp.getfloat("smooth_drift", "speed",         fallback=40.0),
        "step_distance":     cp.getfloat("smooth_drift", "step_distance", fallback=6.0),
        "min_travel":        cp.getfloat("smooth_drift", "min_travel",    fallback=150.0),
        "tick_interval":     cp.getfloat("smooth_drift", "tick_interval", fallback=0.05),
        "jump_interval":     cp.getint("timed_jump", "interval",          fallback=30),
        "max_jump_distance": cp.getint("timed_jump", "max_jump_distance", fallback=0),
    }


def save_config(cfg: dict, path: str = CONFIG_PATH):
    """把当前运行时配置写回 config.ini。"""
    cp = configparser.ConfigParser()
    cp.read(path, encoding="utf-8")
    for section in ("general", "smooth_drift", "timed_jump"):
        if not cp.has_section(section):
            cp.add_section(section)
    cp.set("general",      "mode",               cfg["mode"])
    cp.set("general",      "padding",            str(cfg["padding"]))
    cp.set("general",      "hotkey",             cfg["hotkey"])
    cp.set("smooth_drift", "speed",              str(cfg["speed"]))
    cp.set("smooth_drift", "step_distance",      str(cfg["step_distance"]))
    cp.set("smooth_drift", "min_travel",         str(cfg["min_travel"]))
    cp.set("smooth_drift", "tick_interval",      str(cfg["tick_interval"]))
    cp.set("timed_jump",   "interval",           str(cfg["jump_interval"]))
    cp.set("timed_jump",   "max_jump_distance",  str(cfg["max_jump_distance"]))
    with open(path, "w", encoding="utf-8") as f:
        cp.write(f)


# ---------------------------------------------------------------------------
# 屏幕 & 窗口工具
# ---------------------------------------------------------------------------

def get_screen_size() -> tuple[int, int]:
    return (win32api.GetSystemMetrics(win32con.SM_CXSCREEN),
            win32api.GetSystemMetrics(win32con.SM_CYSCREEN))


def should_skip_window(hwnd: int, screen_w: int, screen_h: int) -> bool:
    if not hwnd:
        return True
    if not win32gui.IsWindowVisible(hwnd):
        return True
    try:
        show_cmd = win32gui.GetWindowPlacement(hwnd)[1]
    except Exception:
        return True
    if show_cmd in (win32con.SW_SHOWMAXIMIZED, win32con.SW_SHOWMINIMIZED):
        return True
    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    except Exception:
        return True
    if right - left <= 0 or bottom - top <= 0:
        return True
    if left <= 0 and top <= 0 and right >= screen_w and bottom >= screen_h:
        return True
    return False


def get_window_rect(hwnd: int):
    try:
        l, t, r, b = win32gui.GetWindowRect(hwnd)
        return l, t, r - l, b - t
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 平滑漂移状态
# ---------------------------------------------------------------------------

class DriftState:
    def __init__(self, x: float, y: float, step_distance: float):
        angle = random.uniform(0, 2 * math.pi)
        self.x = float(x)
        self.y = float(y)
        self.dx = math.cos(angle) * step_distance
        self.dy = math.sin(angle) * step_distance
        self.traveled = 0.0
        self.target_travel = 0.0

    def reset_target(self, min_travel: float):
        self.target_travel = random.uniform(min_travel, min_travel * 2)
        self.traveled = 0.0


# ---------------------------------------------------------------------------
# 移动循环
# ---------------------------------------------------------------------------

def movement_loop(cfg_holder: list, pause_event: threading.Event,
                  mode_holder: list, stop_event: threading.Event):
    screen_w, screen_h = get_screen_size()

    drift: DriftState | None = None
    last_jump_time = time.time()
    last_hwnd = None

    while not stop_event.is_set():
        if pause_event.is_set():
            time.sleep(0.1)
            continue

        cfg = cfg_holder[0]
        padding = cfg["padding"]
        mode = mode_holder[0]

        hwnd = win32gui.GetForegroundWindow()
        if should_skip_window(hwnd, screen_w, screen_h):
            drift = None
            last_hwnd = None
            time.sleep(0.1)
            continue

        rect = get_window_rect(hwnd)
        if rect is None:
            drift = None
            last_hwnd = None
            time.sleep(0.1)
            continue

        x, y, w, h = rect

        if mode == "smooth_drift":
            tick = cfg["tick_interval"]
            speed = max(1.0, float(cfg["speed"]))
            step_distance = max(1.0, float(cfg["step_distance"]))
            min_travel = cfg["min_travel"]
            sleep_time = max(0.01, step_distance / speed)

            if drift is None or hwnd != last_hwnd:
                drift = DriftState(x, y, step_distance)
                drift.reset_target(min_travel)
                last_hwnd = hwnd

            mag = math.hypot(drift.dx, drift.dy) or 1
            drift.dx = drift.dx / mag * step_distance
            drift.dy = drift.dy / mag * step_distance

            nx = drift.x + drift.dx
            ny = drift.y + drift.dy

            min_x, max_x = padding, screen_w - w - padding
            min_y, max_y = padding, screen_h - h - padding
            bounced = False
            if nx < min_x or nx > max_x:
                drift.dx = -drift.dx
                nx = max(min_x, min(nx, max_x))
                bounced = True
            if ny < min_y or ny > max_y:
                drift.dy = -drift.dy
                ny = max(min_y, min(ny, max_y))
                bounced = True
            if bounced:
                drift.reset_target(min_travel)

            drift.traveled += step_distance
            drift.x, drift.y = nx, ny

            if drift.traveled >= drift.target_travel:
                angle = math.atan2(drift.dy, drift.dx)
                angle += random.uniform(-math.pi * 0.6, math.pi * 0.6)
                drift.dx = math.cos(angle) * step_distance
                drift.dy = math.sin(angle) * step_distance
                drift.reset_target(min_travel)

            try:
                win32gui.MoveWindow(hwnd, int(nx), int(ny), w, h, True)
            except Exception:
                pass

            time.sleep(max(tick, sleep_time))

        else:
            drift = None
            last_hwnd = None
            now = time.time()
            if now - last_jump_time >= cfg["jump_interval"]:
                min_x = padding
                max_x = screen_w - w - padding
                min_y = padding
                max_y = screen_h - h - padding
                max_d = cfg["max_jump_distance"]

                if max_x > min_x and max_y > min_y:
                    if max_d > 0:
                        for _ in range(20):
                            angle = random.uniform(0, 2 * math.pi)
                            dist = random.uniform(max_d * 0.3, max_d)
                            nx = int(x + math.cos(angle) * dist)
                            ny = int(y + math.sin(angle) * dist)
                            nx = max(min_x, min(nx, max_x))
                            ny = max(min_y, min(ny, max_y))
                            if nx != x or ny != y:
                                break
                    else:
                        nx = random.randint(min_x, max_x)
                        ny = random.randint(min_y, max_y)
                    try:
                        win32gui.MoveWindow(hwnd, nx, ny, w, h, True)
                    except Exception:
                        pass
                last_jump_time = now
            time.sleep(0.5)


# ---------------------------------------------------------------------------
# 托盘图标
# ---------------------------------------------------------------------------

def make_icon_image(color: str = "#4A90D9") -> Image.Image:
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([4, 4, size - 4, size - 4], fill=color)
    mid = size // 2
    draw.polygon(
        [(mid, 10), (mid + 12, 28), (mid + 5, 28), (mid + 5, 54),
         (mid - 5, 54), (mid - 5, 28), (mid - 12, 28)],
        fill="white",
    )
    return img


def build_tray_icon(cfg_holder: list, pause_event: threading.Event,
                    mode_holder: list, stop_event: threading.Event) -> pystray.Icon:

    def cfg():
        return cfg_holder[0]

    def on_toggle_pause(icon, item):
        if pause_event.is_set():
            pause_event.clear()
            icon.icon = make_icon_image("#4A90D9")
        else:
            pause_event.set()
            icon.icon = make_icon_image("#999999")
        icon.update_menu()

    def on_toggle_mode(icon, item):
        new_mode = "timed_jump" if mode_holder[0] == "smooth_drift" else "smooth_drift"
        mode_holder[0] = new_mode
        new_cfg = dict(cfg())
        new_cfg["mode"] = new_mode
        cfg_holder[0] = new_cfg
        save_config(new_cfg)
        icon.update_menu()

    def speed_step(delta_idx: int):
        c = dict(cfg())
        idx = min(range(len(SPEED_PRESETS)), key=lambda i: abs(SPEED_PRESETS[i] - c["speed"]))
        idx = max(0, min(len(SPEED_PRESETS) - 1, idx + delta_idx))
        c["speed"] = float(SPEED_PRESETS[idx])
        cfg_holder[0] = c
        save_config(c)

    def on_speed_up(icon, item):
        speed_step(+1)
        icon.update_menu()

    def on_speed_down(icon, item):
        speed_step(-1)
        icon.update_menu()

    def step_step(delta_idx: int):
        c = dict(cfg())
        idx = min(range(len(STEP_PRESETS)), key=lambda i: abs(STEP_PRESETS[i] - c["step_distance"]))
        idx = max(0, min(len(STEP_PRESETS) - 1, idx + delta_idx))
        c["step_distance"] = float(STEP_PRESETS[idx])
        cfg_holder[0] = c
        save_config(c)

    def on_step_up(icon, item):
        step_step(+1)
        icon.update_menu()

    def on_step_down(icon, item):
        step_step(-1)
        icon.update_menu()

    def travel_step(delta_idx: int):
        c = dict(cfg())
        idx = min(range(len(TRAVEL_PRESETS)), key=lambda i: abs(TRAVEL_PRESETS[i] - c["min_travel"]))
        idx = max(0, min(len(TRAVEL_PRESETS) - 1, idx + delta_idx))
        c["min_travel"] = float(TRAVEL_PRESETS[idx])
        cfg_holder[0] = c
        save_config(c)

    def on_travel_up(icon, item):
        travel_step(+1)
        icon.update_menu()

    def on_travel_down(icon, item):
        travel_step(-1)
        icon.update_menu()

    INTERVAL_PRESETS = [5, 10, 15, 30, 60, 120, 300]

    def interval_step(delta_idx: int):
        c = dict(cfg())
        idx = min(range(len(INTERVAL_PRESETS)), key=lambda i: abs(INTERVAL_PRESETS[i] - c["jump_interval"]))
        idx = max(0, min(len(INTERVAL_PRESETS) - 1, idx + delta_idx))
        c["jump_interval"] = INTERVAL_PRESETS[idx]
        cfg_holder[0] = c
        save_config(c)

    def on_interval_up(icon, item):
        interval_step(+1)
        icon.update_menu()

    def on_interval_down(icon, item):
        interval_step(-1)
        icon.update_menu()

    def pause_label(item):
        return "恢复 (Resume)" if pause_event.is_set() else "暂停 (Pause)"

    def mode_label(item):
        other = "定时跳跃" if mode_holder[0] == "smooth_drift" else "平滑漂移"
        return f"切换到{other}"

    def speed_label(item):
        return f"速度: {int(cfg()['speed'])} px/s"

    def step_label(item):
        return f"单步距离: {int(cfg()['step_distance'])} px"

    def travel_label(item):
        return f"每段距离: {int(cfg()['min_travel'])} px"

    def interval_label(item):
        v = cfg()["jump_interval"]
        s = f"{v}s" if v < 60 else f"{v//60}min"
        return f"跳跃间隔: {s}"

    menu = pystray.Menu(
        pystray.MenuItem(pause_label, on_toggle_pause, default=True),
        pystray.MenuItem(mode_label, on_toggle_mode),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(speed_label, pystray.Menu(
            pystray.MenuItem("加快速度 (+)", on_speed_up),
            pystray.MenuItem("降低速度 (-)", on_speed_down),
        )),
        pystray.MenuItem(step_label, pystray.Menu(
            pystray.MenuItem("增大单步距离 (+)", on_step_up),
            pystray.MenuItem("减小单步距离 (-)", on_step_down),
        )),
        pystray.MenuItem(travel_label, pystray.Menu(
            pystray.MenuItem("增大每段距离 (+)", on_travel_up),
            pystray.MenuItem("减小每段距离 (-)", on_travel_down),
        )),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(interval_label, pystray.Menu(
            pystray.MenuItem("增加间隔 (+)", on_interval_up),
            pystray.MenuItem("减少间隔 (-)", on_interval_down),
        )),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出 (Quit)", lambda icon, item: (stop_event.set(), icon.stop())),
    )

    return pystray.Icon("window_mover", make_icon_image("#4A90D9"), "Window Mover", menu)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main():
    cfg = load_config()
    cfg_holder = [cfg]
    mode_holder = [cfg["mode"]]
    pause_event = threading.Event()
    stop_event = threading.Event()

    def toggle_pause():
        if pause_event.is_set():
            pause_event.clear()
        else:
            pause_event.set()

    try:
        keyboard.add_hotkey(cfg["hotkey"], toggle_pause)
    except Exception as e:
        print(f"[警告] 无法注册热键 {cfg['hotkey']}: {e}")

    t = threading.Thread(
        target=movement_loop,
        args=(cfg_holder, pause_event, mode_holder, stop_event),
        daemon=True,
    )
    t.start()

    icon = build_tray_icon(cfg_holder, pause_event, mode_holder, stop_event)
    icon.run()


if __name__ == "__main__":
    main()
