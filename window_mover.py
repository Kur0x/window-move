"""
window_mover.py — 自动移动前台窗口，减少视觉疲劳

支持两种模式（config.ini 可配置）：
  smooth_drift : 窗口持续平滑漂移，碰边反弹
  timed_jump   : 每隔 N 秒随机跳跃到新位置

系统托盘图标提供暂停/恢复、切换模式、调速、退出功能。
热键（默认 Ctrl+Shift+P）可随时暂停/恢复。

速度/距离参数说明：
  speed           : 每秒移动像素数（smooth_drift）
  step_distance   : 每次实际移动像素数，越大越不平滑（smooth_drift）
  min_travel      : 每段方向最少移动像素数，越大转向越少（smooth_drift）
  interval        : 跳跃间隔秒数（timed_jump）
  max_jump_distance : 单次跳跃最大像素距离，0 = 不限（timed_jump）
  monitor_mode    : current_monitor | switch_monitor
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

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.ini")

SPEED_PRESETS = [10, 20, 40, 60, 100, 150, 200]
STEP_PRESETS = [2, 4, 6, 10, 20, 30, 40]
TRAVEL_PRESETS = [50, 100, 150, 250, 400, 600]
BOTTOM_PADDING_PRESETS = [8, 12, 18, 24, 32, 40]
MONITOR_MODES = ["current_monitor", "switch_monitor"]


def load_config(path: str = CONFIG_PATH) -> dict:
    cp = configparser.ConfigParser()
    cp.read(path, encoding="utf-8")
    return {
        "mode": cp.get("general", "mode", fallback="smooth_drift"),
        "monitor_mode": cp.get("general", "monitor_mode", fallback="current_monitor"),
        "padding": cp.getint("general", "padding", fallback=50),
        "bottom_padding": cp.getint("general", "bottom_padding", fallback=18),
        "hotkey": cp.get("general", "hotkey", fallback="ctrl+shift+p"),
        "speed": cp.getfloat("smooth_drift", "speed", fallback=40.0),
        "step_distance": cp.getfloat("smooth_drift", "step_distance", fallback=6.0),
        "min_travel": cp.getfloat("smooth_drift", "min_travel", fallback=150.0),
        "tick_interval": cp.getfloat("smooth_drift", "tick_interval", fallback=0.05),
        "jump_interval": cp.getint("timed_jump", "interval", fallback=30),
        "max_jump_distance": cp.getint("timed_jump", "max_jump_distance", fallback=0),
    }


def save_config(cfg: dict, path: str = CONFIG_PATH):
    cp = configparser.ConfigParser()
    cp.read(path, encoding="utf-8")
    for section in ("general", "smooth_drift", "timed_jump"):
        if not cp.has_section(section):
            cp.add_section(section)
    cp.set("general", "mode", cfg["mode"])
    cp.set("general", "monitor_mode", cfg["monitor_mode"])
    cp.set("general", "padding", str(cfg["padding"]))
    cp.set("general", "bottom_padding", str(cfg["bottom_padding"]))
    cp.set("general", "hotkey", cfg["hotkey"])
    cp.set("smooth_drift", "speed", str(cfg["speed"]))
    cp.set("smooth_drift", "step_distance", str(cfg["step_distance"]))
    cp.set("smooth_drift", "min_travel", str(cfg["min_travel"]))
    cp.set("smooth_drift", "tick_interval", str(cfg["tick_interval"]))
    cp.set("timed_jump", "interval", str(cfg["jump_interval"]))
    cp.set("timed_jump", "max_jump_distance", str(cfg["max_jump_distance"]))
    with open(path, "w", encoding="utf-8") as f:
        cp.write(f)


def get_window_rect(hwnd: int):
    try:
        l, t, r, b = win32gui.GetWindowRect(hwnd)
        return l, t, r - l, b - t
    except Exception:
        return None


def get_monitors() -> list[dict]:
    monitors = []
    for handle, _, _ in win32api.EnumDisplayMonitors():
        info = win32api.GetMonitorInfo(handle)
        ml, mt, mr, mb = info["Monitor"]
        wl, wt, wr, wb = info["Work"]
        monitors.append({
            "handle": handle,
            "monitor": (ml, mt, mr, mb),
            "work": (wl, wt, wr, wb),
            "primary": bool(info.get("Flags", 0) & 1),
        })
    monitors.sort(key=lambda m: (m["monitor"][0], m["monitor"][1]))
    return monitors


def rect_intersection_area(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> int:
    left = max(a[0], b[0])
    top = max(a[1], b[1])
    right = min(a[2], b[2])
    bottom = min(a[3], b[3])
    if right <= left or bottom <= top:
        return 0
    return (right - left) * (bottom - top)


def find_monitor_for_rect(x: int, y: int, w: int, h: int, monitors: list[dict]) -> dict | None:
    if not monitors:
        return None
    rect = (x, y, x + w, y + h)
    best = None
    best_area = -1
    for monitor in monitors:
        area = rect_intersection_area(rect, monitor["monitor"])
        if area > best_area:
            best_area = area
            best = monitor
    return best or monitors[0]


def get_monitor_bounds(monitor: dict, cfg: dict, w: int, h: int) -> tuple[int, int, int, int]:
    wl, wt, wr, wb = monitor["work"]
    padding = int(cfg["padding"])
    bottom_padding = int(cfg["bottom_padding"])
    min_x = wl + padding
    max_x = wr - w - padding
    min_y = wt + padding
    max_y = wb - h - bottom_padding
    if max_x < min_x:
        mid_x = wl + max(0, (wr - wl - w) // 2)
        min_x = max_x = mid_x
    if max_y < min_y:
        mid_y = wt + max(0, (wb - wt - h) // 2)
        min_y = max_y = mid_y
    return min_x, max_x, min_y, max_y


def clamp_to_bounds(x: int, y: int, bounds: tuple[int, int, int, int]) -> tuple[int, int]:
    min_x, max_x, min_y, max_y = bounds
    return max(min_x, min(x, max_x)), max(min_y, min(y, max_y))


def should_skip_window(hwnd: int, monitors: list[dict]) -> bool:
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
    rect = get_window_rect(hwnd)
    if rect is None:
        return True
    x, y, w, h = rect
    if w <= 0 or h <= 0:
        return True
    monitor = find_monitor_for_rect(x, y, w, h, monitors)
    if monitor is None:
        return True
    ml, mt, mr, mb = monitor["monitor"]
    if x <= ml and y <= mt and x + w >= mr and y + h >= mb:
        return True
    return False


def get_other_monitor(current: dict | None, monitors: list[dict]) -> dict | None:
    if current is None or len(monitors) < 2:
        return current
    for monitor in monitors:
        if monitor["handle"] != current["handle"]:
            return monitor
    return current


class DriftState:
    def __init__(self, x: float, y: float, step_distance: float, dx: float | None = None, dy: float | None = None):
        self.x = float(x)
        self.y = float(y)
        if dx is None or dy is None:
            angle = random.uniform(0, 2 * math.pi)
            dx = math.cos(angle)
            dy = math.sin(angle)
        mag = math.hypot(dx, dy) or 1.0
        self.dx = dx / mag * step_distance
        self.dy = dy / mag * step_distance
        self.traveled = 0.0
        self.target_travel = 0.0

    def reset_target(self, min_travel: float):
        self.target_travel = random.uniform(min_travel, min_travel * 2)
        self.traveled = 0.0


def movement_loop(cfg_holder: list, pause_event: threading.Event,
                  mode_holder: list, stop_event: threading.Event):
    drift: DriftState | None = None
    last_jump_time = time.time()
    last_hwnd = None

    while not stop_event.is_set():
        if pause_event.is_set():
            time.sleep(0.1)
            continue

        cfg = cfg_holder[0]
        mode = mode_holder[0]
        monitors = get_monitors()

        hwnd = win32gui.GetForegroundWindow()
        if should_skip_window(hwnd, monitors):
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
        current_monitor = find_monitor_for_rect(x, y, w, h, monitors)
        if current_monitor is None:
            drift = None
            last_hwnd = None
            time.sleep(0.1)
            continue

        if mode == "smooth_drift":
            tick = cfg["tick_interval"]
            speed = max(1.0, float(cfg["speed"]))
            step_distance = max(1.0, float(cfg["step_distance"]))
            min_travel = float(cfg["min_travel"])
            sleep_time = max(0.01, step_distance / speed)
            bounds = get_monitor_bounds(current_monitor, cfg, w, h)

            if drift is None or hwnd != last_hwnd:
                drift = DriftState(x, y, step_distance)
                drift.reset_target(min_travel)
                last_hwnd = hwnd
            else:
                desync_threshold = max(12.0, step_distance * 2)
                if abs(x - drift.x) > desync_threshold or abs(y - drift.y) > desync_threshold:
                    drift = DriftState(x, y, step_distance, drift.dx, drift.dy)
                    drift.reset_target(min_travel)

            mag = math.hypot(drift.dx, drift.dy) or 1.0
            drift.dx = drift.dx / mag * step_distance
            drift.dy = drift.dy / mag * step_distance

            nx = drift.x + drift.dx
            ny = drift.y + drift.dy
            min_x, max_x, min_y, max_y = bounds
            bounced = False

            if cfg["monitor_mode"] == "switch_monitor" and len(monitors) > 1:
                switched = False
                if nx < min_x or nx > max_x:
                    target_monitor = get_other_monitor(current_monitor, monitors)
                    if target_monitor is not None and target_monitor["handle"] != current_monitor["handle"]:
                        target_bounds = get_monitor_bounds(target_monitor, cfg, w, h)
                        target_min_x, target_max_x, target_min_y, target_max_y = target_bounds
                        source_height = max(1, max_y - min_y)
                        target_height = max(1, target_max_y - target_min_y)
                        relative_y = 0.0 if source_height == 0 else (ny - min_y) / source_height
                        ny = target_min_y + int(relative_y * target_height)
                        ny = max(target_min_y, min(ny, target_max_y))
                        if nx < min_x:
                            nx = target_max_x
                        else:
                            nx = target_min_x
                        current_monitor = target_monitor
                        bounds = target_bounds
                        min_x, max_x, min_y, max_y = bounds
                        switched = True
                if not switched:
                    if nx < min_x or nx > max_x:
                        drift.dx = -drift.dx
                        nx = max(min_x, min(nx, max_x))
                        bounced = True
            else:
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
                bounds = get_monitor_bounds(current_monitor, cfg, w, h)
                min_x, max_x, min_y, max_y = bounds
                max_d = int(cfg["max_jump_distance"])
                if max_x >= min_x and max_y >= min_y:
                    if max_d > 0:
                        nx, ny = x, y
                        for _ in range(20):
                            angle = random.uniform(0, 2 * math.pi)
                            dist = random.uniform(max_d * 0.3, max_d)
                            nx = int(x + math.cos(angle) * dist)
                            ny = int(y + math.sin(angle) * dist)
                            nx, ny = clamp_to_bounds(nx, ny, bounds)
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

    def update_cfg(mutator):
        c = dict(cfg())
        mutator(c)
        cfg_holder[0] = c
        mode_holder[0] = c["mode"]
        save_config(c)

    def on_toggle_pause(icon, item):
        if pause_event.is_set():
            pause_event.clear()
            icon.icon = make_icon_image("#4A90D9")
        else:
            pause_event.set()
            icon.icon = make_icon_image("#999999")
        icon.update_menu()

    def on_toggle_mode(icon, item):
        def mut(c):
            c["mode"] = "timed_jump" if c["mode"] == "smooth_drift" else "smooth_drift"
        update_cfg(mut)
        icon.update_menu()

    def on_toggle_monitor_mode(icon, item):
        def mut(c):
            c["monitor_mode"] = "switch_monitor" if c["monitor_mode"] == "current_monitor" else "current_monitor"
        update_cfg(mut)
        icon.update_menu()

    def speed_step(delta_idx: int):
        def mut(c):
            idx = min(range(len(SPEED_PRESETS)), key=lambda i: abs(SPEED_PRESETS[i] - c["speed"]))
            idx = max(0, min(len(SPEED_PRESETS) - 1, idx + delta_idx))
            c["speed"] = float(SPEED_PRESETS[idx])
        update_cfg(mut)

    def step_step(delta_idx: int):
        def mut(c):
            idx = min(range(len(STEP_PRESETS)), key=lambda i: abs(STEP_PRESETS[i] - c["step_distance"]))
            idx = max(0, min(len(STEP_PRESETS) - 1, idx + delta_idx))
            c["step_distance"] = float(STEP_PRESETS[idx])
        update_cfg(mut)

    def travel_step(delta_idx: int):
        def mut(c):
            idx = min(range(len(TRAVEL_PRESETS)), key=lambda i: abs(TRAVEL_PRESETS[i] - c["min_travel"]))
            idx = max(0, min(len(TRAVEL_PRESETS) - 1, idx + delta_idx))
            c["min_travel"] = float(TRAVEL_PRESETS[idx])
        update_cfg(mut)

    def bottom_padding_step(delta_idx: int):
        def mut(c):
            idx = min(range(len(BOTTOM_PADDING_PRESETS)), key=lambda i: abs(BOTTOM_PADDING_PRESETS[i] - c["bottom_padding"]))
            idx = max(0, min(len(BOTTOM_PADDING_PRESETS) - 1, idx + delta_idx))
            c["bottom_padding"] = int(BOTTOM_PADDING_PRESETS[idx])
        update_cfg(mut)

    INTERVAL_PRESETS = [5, 10, 15, 30, 60, 120, 300]

    def interval_step(delta_idx: int):
        def mut(c):
            idx = min(range(len(INTERVAL_PRESETS)), key=lambda i: abs(INTERVAL_PRESETS[i] - c["jump_interval"]))
            idx = max(0, min(len(INTERVAL_PRESETS) - 1, idx + delta_idx))
            c["jump_interval"] = INTERVAL_PRESETS[idx]
        update_cfg(mut)

    def pause_label(item):
        return "恢复 (Resume)" if pause_event.is_set() else "暂停 (Pause)"

    def mode_label(item):
        other = "定时跳跃" if mode_holder[0] == "smooth_drift" else "平滑漂移"
        return f"切换到{other}"

    def monitor_mode_label(item):
        return "屏幕模式: 单屏限制" if cfg()["monitor_mode"] == "current_monitor" else "屏幕模式: 边缘切换另一屏"

    def monitor_mode_toggle_label(item):
        return "切换到边缘跨屏" if cfg()["monitor_mode"] == "current_monitor" else "切换到单屏限制"

    def speed_label(item):
        return f"速度: {int(cfg()['speed'])} px/s"

    def step_label(item):
        return f"单步距离: {int(cfg()['step_distance'])} px"

    def travel_label(item):
        return f"每段距离: {int(cfg()['min_travel'])} px"

    def bottom_padding_label(item):
        return f"底部边距: {int(cfg()['bottom_padding'])} px"

    def interval_label(item):
        v = cfg()["jump_interval"]
        s = f"{v}s" if v < 60 else f"{v // 60}min"
        return f"跳跃间隔: {s}"

    menu = pystray.Menu(
        pystray.MenuItem(pause_label, on_toggle_pause, default=True),
        pystray.MenuItem(mode_label, on_toggle_mode),
        pystray.MenuItem(monitor_mode_label, on_toggle_monitor_mode),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(speed_label, pystray.Menu(
            pystray.MenuItem("加快速度 (+)", lambda icon, item: (speed_step(+1), icon.update_menu())),
            pystray.MenuItem("降低速度 (-)", lambda icon, item: (speed_step(-1), icon.update_menu())),
        )),
        pystray.MenuItem(step_label, pystray.Menu(
            pystray.MenuItem("增大单步距离 (+)", lambda icon, item: (step_step(+1), icon.update_menu())),
            pystray.MenuItem("减小单步距离 (-)", lambda icon, item: (step_step(-1), icon.update_menu())),
        )),
        pystray.MenuItem(travel_label, pystray.Menu(
            pystray.MenuItem("增大每段距离 (+)", lambda icon, item: (travel_step(+1), icon.update_menu())),
            pystray.MenuItem("减小每段距离 (-)", lambda icon, item: (travel_step(-1), icon.update_menu())),
        )),
        pystray.MenuItem(bottom_padding_label, pystray.Menu(
            pystray.MenuItem("增大底部边距 (+)", lambda icon, item: (bottom_padding_step(+1), icon.update_menu())),
            pystray.MenuItem("减小底部边距 (-)", lambda icon, item: (bottom_padding_step(-1), icon.update_menu())),
        )),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(interval_label, pystray.Menu(
            pystray.MenuItem("增加间隔 (+)", lambda icon, item: (interval_step(+1), icon.update_menu())),
            pystray.MenuItem("减少间隔 (-)", lambda icon, item: (interval_step(-1), icon.update_menu())),
        )),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出 (Quit)", lambda icon, item: (stop_event.set(), icon.stop())),
    )


    return pystray.Icon("window_mover", make_icon_image("#4A90D9"), "Window Mover", menu)


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
