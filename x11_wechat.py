import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import uuid4

import cv2
import mss
import numpy as np

from common import discover_wechat_pids, role_from_cmdline


WINDOW_LINE_RE = re.compile(r"^\s*(?P<xid>0x[0-9a-f]+)\s+\"(?P<title>[^\"]*)\"")
XWININFO_INT_FIELDS = {
    "Absolute upper-left X": "abs_x",
    "Absolute upper-left Y": "abs_y",
    "Width": "width",
    "Height": "height",
}


@dataclass(frozen=True)
class X11Window:
    wid: int
    xid: str
    title: str
    size: tuple[int, int]
    client_geometry: tuple[int, int, int, int] | None
    backend: str


Rect = tuple[int, int, int, int]
_WINDOW_CACHE: dict[tuple[str, str, str], X11Window] = {}


def _read_proc_environ(pid: int) -> dict[str, str]:
    data = Path(f"/proc/{pid}/environ").read_bytes()
    env: dict[str, str] = {}
    for item in data.split(b"\x00"):
        if not item or b"=" not in item:
            continue
        key, value = item.split(b"=", 1)
        env[key.decode(errors="ignore")] = value.decode(errors="ignore")
    return env


def _detect_wechat_session_env() -> dict[str, str]:
    main_pids = [pid for pid in discover_wechat_pids() if role_from_cmdline(pid) == "wechat-main"]
    for pid in main_pids:
        try:
            env = _read_proc_environ(pid)
        except OSError:
            continue
        if "DISPLAY" in env or "XAUTHORITY" in env:
            return env
    return {}


_WECHAT_SESSION_ENV = _detect_wechat_session_env()
DEFAULT_XPRA_TARGET = "tcp://127.0.0.1:10000"
DEFAULT_PASSWORD_FILE = "/etc/xpra-auth/password"
DEFAULT_X_DISPLAY = os.environ.get("DISPLAY") or _WECHAT_SESSION_ENV.get("DISPLAY") or ":0"
DEFAULT_XAUTHORITY = os.environ.get("XAUTHORITY") or _WECHAT_SESSION_ENV.get("XAUTHORITY") or str(Path.home() / ".Xauthority")
DEFAULT_BACKEND = os.environ.get("WECHAT_X11_BACKEND", "local-x11")
DEFAULT_WINDOW_TITLE = os.environ.get("WECHAT_WINDOW_TITLE", "微信")
DEFAULT_WAYLAND_DISPLAY = os.environ.get("WAYLAND_DISPLAY") or _WECHAT_SESSION_ENV.get("WAYLAND_DISPLAY") or ""
DEFAULT_XDG_SESSION_TYPE = os.environ.get("XDG_SESSION_TYPE") or _WECHAT_SESSION_ENV.get("XDG_SESSION_TYPE") or ""
DEFAULT_XDG_CURRENT_DESKTOP = os.environ.get("XDG_CURRENT_DESKTOP") or _WECHAT_SESSION_ENV.get("XDG_CURRENT_DESKTOP") or ""
DEFAULT_DBUS_SESSION_BUS_ADDRESS = (
    os.environ.get("DBUS_SESSION_BUS_ADDRESS")
    or _WECHAT_SESSION_ENV.get("DBUS_SESSION_BUS_ADDRESS")
    or ""
)
DEFAULT_XDG_RUNTIME_DIR = (
    os.environ.get("XDG_RUNTIME_DIR")
    or _WECHAT_SESSION_ENV.get("XDG_RUNTIME_DIR")
    or f"/run/user/{os.getuid()}"
)


def _run(
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    timeout: float = 30,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(command, capture_output=True, text=True, env=env, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"命令执行超时 {timeout:g}s: {' '.join(command)}") from exc
    if result.returncode != 0:
        raise RuntimeError(
            f"命令执行失败，退出码 {result.returncode}\n"
            f"命令: {' '.join(command)}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result


def _x11_env(display: str, xauthority: str) -> dict[str, str]:
    env = os.environ.copy()
    env["DISPLAY"] = display
    env["XAUTHORITY"] = xauthority
    return env


def _wayland_env() -> dict[str, str]:
    env = os.environ.copy()
    env["XDG_RUNTIME_DIR"] = DEFAULT_XDG_RUNTIME_DIR
    if DEFAULT_WAYLAND_DISPLAY:
        env["WAYLAND_DISPLAY"] = DEFAULT_WAYLAND_DISPLAY
    if DEFAULT_DBUS_SESSION_BUS_ADDRESS:
        env["DBUS_SESSION_BUS_ADDRESS"] = DEFAULT_DBUS_SESSION_BUS_ADDRESS
    if DEFAULT_XDG_CURRENT_DESKTOP:
        env["XDG_CURRENT_DESKTOP"] = DEFAULT_XDG_CURRENT_DESKTOP
    if DEFAULT_XDG_SESSION_TYPE:
        env["XDG_SESSION_TYPE"] = DEFAULT_XDG_SESSION_TYPE
    return env


def _command_exists(command: str) -> bool:
    result = subprocess.run(["which", command], capture_output=True, text=True)
    return result.returncode == 0


def _window_absolute_point(window: X11Window, x: int, y: int) -> tuple[int, int]:
    if window.client_geometry is None:
        raise RuntimeError("Wayland 点击缺少窗口几何信息")
    left, top, _, _ = window.client_geometry
    return left + x, top + y


def _click_with_wdotool(window: X11Window, x: int, y: int, button: int) -> None:
    abs_x, abs_y = _window_absolute_point(window, x, y)
    env = _wayland_env()
    _run(["wdotool", "--backend", "libei", "mousemove", str(abs_x), str(abs_y)], env=env, timeout=5)
    _run(["wdotool", "--backend", "libei", "click", str(button)], env=env, timeout=5)


def _move_with_wdotool(window: X11Window, x: int, y: int) -> None:
    abs_x, abs_y = _window_absolute_point(window, x, y)
    _run(
        ["wdotool", "--backend", "libei", "mousemove", str(abs_x), str(abs_y)],
        env=_wayland_env(),
        timeout=5,
    )


def _activate_with_wdotool(window: X11Window) -> None:
    env = _wayland_env()
    result = _run(["wdotool", "search", "--name", window.title], env=env, timeout=5)
    for line in result.stdout.splitlines():
        window_id = line.split("\t", 1)[0].strip()
        if window_id:
            _run(["wdotool", "windowactivate", window_id], env=env, timeout=5)
            time.sleep(0.2)
            return
    raise RuntimeError(f"wdotool 未找到窗口: {window.title}")


def _has_xwd_pipeline() -> bool:
    return all(_command_exists(command) for command in ("xwd", "xwdtopnm", "pnmtopng"))


def _has_wayland_session() -> bool:
    return DEFAULT_XDG_SESSION_TYPE == "wayland" or bool(DEFAULT_WAYLAND_DISPLAY)


def _parse_bool(value: str) -> bool:
    if value == "True":
        return True
    if value == "False":
        return False
    raise ValueError(f"无法解析布尔值: {value}")


def _parse_int_tuple(value: str) -> tuple[int, ...]:
    return tuple(int(item.strip()) for item in value.strip("()").split(",") if item.strip())


def _parse_xpra_windows(info: str) -> list[dict[str, str]]:
    windows: dict[int, dict[str, str]] = {}
    prefix = "windows."
    for line in info.splitlines():
        if not line.startswith(prefix) or "=" not in line:
            continue
        key, value = line.split("=", 1)
        parts = key.split(".", 2)
        if len(parts) != 3:
            continue
        _, wid_text, attr = parts
        if not wid_text.isdigit():
            continue
        wid = int(wid_text)
        windows.setdefault(wid, {"wid": str(wid)})[attr] = value.strip().strip("'")
    return list(windows.values())


def _parse_xwininfo_window(info: str) -> X11Window | None:
    title_match = re.search(r'xwininfo: Window id: (0x[0-9a-f]+) "([^"]*)"', info)
    if title_match is None:
        return None

    fields: dict[str, int] = {}
    for line in info.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        if key not in XWININFO_INT_FIELDS:
            continue
        fields[XWININFO_INT_FIELDS[key]] = int(value.strip())

    if "Map State: IsViewable" not in info:
        return None
    if fields.get("width", 0) <= 0 or fields.get("height", 0) <= 0:
        return None

    return X11Window(
        wid=int(title_match.group(1), 16),
        xid=title_match.group(1),
        title=title_match.group(2),
        size=(fields["width"], fields["height"]),
        client_geometry=(fields["abs_x"], fields["abs_y"], fields["width"], fields["height"]),
        backend="local-x11",
    )


def _list_local_x11_window_ids(display: str, xauthority: str, title: str) -> list[str]:
    env = _x11_env(display, xauthority)
    info = _run(["xwininfo", "-root", "-tree"], env=env).stdout
    xids: list[str] = []
    for line in info.splitlines():
        match = WINDOW_LINE_RE.match(line)
        if match is None:
            continue
        current_title = match.group("title").strip()
        if current_title == title or title in current_title:
            xids.append(match.group("xid"))
    return xids


def _lookup_local_x11_window_by_xid(
    xid: str,
    display: str,
    xauthority: str,
    title: str,
) -> X11Window | None:
    env = _x11_env(display, xauthority)
    try:
        info = _run(["xwininfo", "-id", xid], env=env).stdout
    except RuntimeError:
        return None
    window = _parse_xwininfo_window(info)
    if window is None:
        return None
    current_title = window.title.strip()
    if current_title != title and title not in current_title:
        return None
    return window


def find_wechat_window_local_x11(
    display: str = DEFAULT_X_DISPLAY,
    xauthority: str = DEFAULT_XAUTHORITY,
    title: str = DEFAULT_WINDOW_TITLE,
    preferred_xid: str | None = None,
) -> X11Window:
    if not _command_exists("xwininfo"):
        raise RuntimeError("local-x11 backend 需要 xwininfo，但系统里没有这个命令")

    if preferred_xid is not None:
        cached = _lookup_local_x11_window_by_xid(preferred_xid, display, xauthority, title)
        if cached is not None:
            return cached

    candidates: list[X11Window] = []
    env = _x11_env(display, xauthority)
    for xid in _list_local_x11_window_ids(display, xauthority, title):
        info = _run(["xwininfo", "-id", xid], env=env).stdout
        window = _parse_xwininfo_window(info)
        if window is not None:
            candidates.append(window)

    if not candidates:
        raise RuntimeError(f"没有找到标题匹配 {title!r} 的本地 X11 微信窗口")
    return max(candidates, key=lambda item: item.size[0] * item.size[1])


def find_wechat_window_xpra(
    xpra_target: str = DEFAULT_XPRA_TARGET,
    password_file: str = DEFAULT_PASSWORD_FILE,
    title: str = DEFAULT_WINDOW_TITLE,
) -> X11Window:
    if not _command_exists("xpra"):
        raise RuntimeError("xpra backend 不可用：系统里没有 xpra 命令")

    command = ["xpra", "info", xpra_target, f"--password-file={password_file}"]
    info = _run(command).stdout

    candidates: list[X11Window] = []
    for window in _parse_xpra_windows(info):
        if window.get("title") != title:
            continue
        if _parse_bool(window.get("tray", "False")):
            continue
        if not _parse_bool(window.get("shown", "True")):
            continue

        size = _parse_int_tuple(window["size"])
        geometry = window.get("client-geometry")
        candidates.append(
            X11Window(
                wid=int(window["wid"]),
                xid=window["xid"],
                title=window["title"],
                size=(size[0], size[1]),
                client_geometry=_parse_int_tuple(geometry) if geometry else None,  # type: ignore[arg-type]
                backend="xpra",
            )
        )

    if not candidates:
        raise RuntimeError(f"没有找到 title={title} 且 tray=False 的 xpra 窗口")
    return max(candidates, key=lambda item: item.size[0] * item.size[1])


def find_wechat_window(
    backend: str = DEFAULT_BACKEND,
    xpra_target: str = DEFAULT_XPRA_TARGET,
    password_file: str = DEFAULT_PASSWORD_FILE,
    display: str = DEFAULT_X_DISPLAY,
    xauthority: str = DEFAULT_XAUTHORITY,
    title: str = DEFAULT_WINDOW_TITLE,
    use_cache: bool = True,
) -> X11Window:
    errors: list[str] = []
    backends = ["xpra", "local-x11"] if backend == "auto" else [backend]
    cache_key = (backend, display, title)
    cached_window = _WINDOW_CACHE.get(cache_key) if use_cache else None
    for current_backend in backends:
        try:
            if current_backend == "xpra":
                window = find_wechat_window_xpra(xpra_target=xpra_target, password_file=password_file, title=title)
            elif current_backend == "local-x11":
                preferred_xid = cached_window.xid if cached_window and cached_window.backend == "local-x11" else None
                window = find_wechat_window_local_x11(
                    display=display,
                    xauthority=xauthority,
                    title=title,
                    preferred_xid=preferred_xid,
                )
            else:
                raise RuntimeError(f"不支持的 backend: {current_backend}")
            if use_cache:
                _WINDOW_CACHE[cache_key] = window
            return window
        except RuntimeError as exc:
            errors.append(f"{current_backend}: {exc}")
    raise RuntimeError("查找微信窗口失败\n" + "\n".join(errors))

def _save_rgb_image(output: str | Path | None, image: np.ndarray) -> None:
    if output is None:
        return
    ok = cv2.imwrite(str(output), cv2.cvtColor(image, cv2.COLOR_RGB2BGR))
    if not ok:
        raise RuntimeError(f"写出 PNG 失败: {output}")


def _load_rgb_image(path: str | Path) -> np.ndarray:
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None:
        raise RuntimeError(f"OpenCV 无法读取图片: {path}")
    return cv2.cvtColor(image, cv2.COLOR_BGR2RGB)


def _normalize_capture_region(window: X11Window, region: Rect | None) -> Rect | None:
    if region is None:
        return None
    if window.client_geometry is None:
        raise RuntimeError("窗口缺少几何信息，无法使用局部截图")
    rel_x, rel_y, width, height = region
    window_width, window_height = window.size
    if width <= 0 or height <= 0:
        raise RuntimeError(f"无效截图区域: {region}")
    left = max(0, rel_x)
    top = max(0, rel_y)
    right = min(window_width, rel_x + width)
    bottom = min(window_height, rel_y + height)
    if right <= left or bottom <= top:
        raise RuntimeError(f"截图区域越界: region={region} window_size={window.size}")
    return (left, top, right - left, bottom - top)


def _crop_relative_region(image: np.ndarray, region: Rect | None) -> np.ndarray:
    if region is None:
        return image
    left, top, width, height = region
    bottom = top + height
    right = left + width
    if top < 0 or left < 0 or bottom > image.shape[0] or right > image.shape[1]:
        raise RuntimeError(f"局部截图区域越界: region={region} image_shape={image.shape[:2]}")
    return image[top:bottom, left:right].copy()


def _capture_window_with_mss(
    window: X11Window,
    display: str,
    xauthority: str,
    region: Rect | None = None,
) -> np.ndarray:
    if window.client_geometry is None:
        raise RuntimeError("local-x11 截图缺少窗口几何信息")
    left, top, width, height = window.client_geometry
    current_region = _normalize_capture_region(window, region)
    if current_region is not None:
        rel_x, rel_y, width, height = current_region
        left += rel_x
        top += rel_y
    previous_display = os.environ.get("DISPLAY")
    previous_xauthority = os.environ.get("XAUTHORITY")
    os.environ["DISPLAY"] = display
    os.environ["XAUTHORITY"] = xauthority
    try:
        try:
            sct = mss.mss(display=display)
        except TypeError:
            sct = mss.mss()
        with sct:
            raw = sct.grab({"left": left, "top": top, "width": width, "height": height})
    finally:
        if previous_display is None:
            os.environ.pop("DISPLAY", None)
        else:
            os.environ["DISPLAY"] = previous_display
        if previous_xauthority is None:
            os.environ.pop("XAUTHORITY", None)
        else:
            os.environ["XAUTHORITY"] = previous_xauthority
    image = np.asarray(raw)
    if image.size == 0:
        raise RuntimeError("mss 返回了空截图")
    return cv2.cvtColor(image, cv2.COLOR_BGRA2RGB)


def _capture_window_with_xwd(window: X11Window, display: str, xauthority: str) -> np.ndarray:
    env = _x11_env(display, xauthority)
    with NamedTemporaryFile(suffix=".xwd") as xwd_file:
        _run(["xwd", "-silent", "-id", window.xid, "-out", xwd_file.name], env=env)

        pnm = subprocess.Popen(
            ["xwdtopnm", xwd_file.name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        png = subprocess.run(
            ["pnmtopng"],
            stdin=pnm.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if pnm.stdout is not None:
            pnm.stdout.close()
        _, pnm_stderr = pnm.communicate()
        if pnm.returncode != 0 or png.returncode != 0:
            raise RuntimeError(
                f"XWD 转 PNG 失败\n"
                f"xwdtopnm stderr:\n{pnm_stderr.decode()}\n"
                f"pnmtopng stderr:\n{png.stderr.decode()}"
            )

    data = np.frombuffer(png.stdout, dtype=np.uint8)
    image = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if image is None:
        raise RuntimeError("OpenCV 无法读取 X11 截图 PNG")
    return cv2.cvtColor(image, cv2.COLOR_BGR2RGB)


def _capture_with_gnome_screenshot() -> np.ndarray:
    if not _command_exists("gdbus"):
        raise RuntimeError("Wayland 截图 fallback 需要 gdbus，但系统里没有这个命令")
    if "GNOME" not in DEFAULT_XDG_CURRENT_DESKTOP.upper():
        raise RuntimeError(
            "Wayland 截图 fallback 当前只支持 GNOME Shell。"
            f" current_desktop={DEFAULT_XDG_CURRENT_DESKTOP!r}"
        )
    env = _wayland_env()
    output = Path("/tmp") / f"wechat-wayland-{uuid4().hex}.png"
    try:
        _run(
            [
                "gdbus",
                "call",
                "--session",
                "--dest",
                "org.gnome.Shell.Screenshot",
                "--object-path",
                "/org/gnome/Shell/Screenshot",
                "--method",
                "org.gnome.Shell.Screenshot.Screenshot",
                "false",
                "false",
                str(output),
            ],
            env=env,
        )
        return _load_rgb_image(output)
    finally:
        output.unlink(missing_ok=True)


def _crop_window_image(window: X11Window, image: np.ndarray) -> np.ndarray:
    if window.client_geometry is None:
        raise RuntimeError("窗口缺少几何信息，无法裁剪截图")
    left, top, width, height = window.client_geometry
    bottom = top + height
    right = left + width
    if top < 0 or left < 0 or bottom > image.shape[0] or right > image.shape[1]:
        raise RuntimeError(
            f"窗口裁剪区域越界: geometry={window.client_geometry} image_shape={image.shape[:2]}"
        )
    return image[top:bottom, left:right].copy()


def capture_window_png(
    window: X11Window,
    output: str | Path | None = None,
    display: str = DEFAULT_X_DISPLAY,
    xauthority: str = DEFAULT_XAUTHORITY,
    region: Rect | None = None,
) -> np.ndarray:
    current_region = _normalize_capture_region(window, region)
    if window.backend == "local-x11":
        try:
            image = _capture_window_with_mss(window, display, xauthority, region=current_region)
        except Exception as exc:
            if _has_xwd_pipeline():
                image = _capture_window_with_xwd(window, display, xauthority)
                image = _crop_relative_region(image, current_region)
            elif _has_wayland_session():
                try:
                    image = _crop_window_image(window, _capture_with_gnome_screenshot())
                    image = _crop_relative_region(image, current_region)
                except Exception as wayland_exc:
                    raise RuntimeError(
                        "local-x11 截图失败。mss 在当前 Wayland/XWayland 会话下不可用，"
                        "且 X11 / Wayland fallback 也失败了。"
                    ) from wayland_exc
            else:
                raise RuntimeError(
                    "local-x11 截图失败。mss 截图不可用，且系统没有可用的 xwd 截图流水线。\n"
                    "建议安装 x11-apps 和 netpbm 后重试。"
                ) from exc
        _save_rgb_image(output, image)
        return image

    rgb_image = _capture_window_with_xwd(window, display, xauthority)
    rgb_image = _crop_relative_region(rgb_image, current_region)
    _save_rgb_image(output, rgb_image)
    return rgb_image

def capture_wechat_png(
    output: str | Path | None = None,
    backend: str = DEFAULT_BACKEND,
    xpra_target: str = DEFAULT_XPRA_TARGET,
    password_file: str = DEFAULT_PASSWORD_FILE,
    display: str = DEFAULT_X_DISPLAY,
    xauthority: str = DEFAULT_XAUTHORITY,
    title: str = DEFAULT_WINDOW_TITLE,
    region: Rect | None = None,
    use_cache: bool = True,
) -> np.ndarray:
    window = find_wechat_window(
        backend=backend,
        xpra_target=xpra_target,
        password_file=password_file,
        display=display,
        xauthority=xauthority,
        title=title,
        use_cache=use_cache,
    )
    return capture_window_png(window, output=output, display=display, xauthority=xauthority, region=region)


def click_window(
    window: X11Window,
    x: int,
    y: int,
    button: int = 1,
    display: str = DEFAULT_X_DISPLAY,
    xauthority: str = DEFAULT_XAUTHORITY,
) -> None:
    if _has_wayland_session() and _command_exists("wdotool"):
        _activate_with_wdotool(window)
        _click_with_wdotool(window, x, y, button)
        return

    env = _x11_env(display, xauthority)
    if _command_exists("xdotool"):
        _run(["xdotool", "mousemove", "--window", window.xid, str(x), str(y), "click", str(button)], env=env)
        return
    if _command_exists("ydotool"):
        abs_x, abs_y = _window_absolute_point(window, x, y)
        button_code = {1: "0xC0", 2: "0xC1", 3: "0xC2"}.get(button, str(button))
        _run(["ydotool", "mousemove", "--delay", "0", str(abs_x), str(abs_y)], timeout=3)
        _run(["ydotool", "click", button_code], timeout=3)
        return
    raise RuntimeError("没有可用的点击工具。请安装 wdotool（Wayland）、xdotool（XWayland）或 ydotool")


def move_window_mouse(
    window: X11Window,
    x: int,
    y: int,
    display: str = DEFAULT_X_DISPLAY,
    xauthority: str = DEFAULT_XAUTHORITY,
) -> None:
    if _has_wayland_session() and _command_exists("wdotool"):
        _move_with_wdotool(window, x, y)
        return

    env = _x11_env(display, xauthority)
    if _command_exists("xdotool"):
        _run(["xdotool", "mousemove", "--window", window.xid, str(x), str(y)], env=env)
        return
    if _command_exists("ydotool"):
        abs_x, abs_y = _window_absolute_point(window, x, y)
        _run(["ydotool", "mousemove", "--delay", "0", str(abs_x), str(abs_y)], timeout=3)
        return
    raise RuntimeError("没有可用的鼠标移动工具。请安装 wdotool（Wayland）、xdotool（XWayland）或 ydotool")


def click_wechat(
    x: int,
    y: int,
    button: int = 1,
    backend: str = DEFAULT_BACKEND,
    xpra_target: str = DEFAULT_XPRA_TARGET,
    password_file: str = DEFAULT_PASSWORD_FILE,
    display: str = DEFAULT_X_DISPLAY,
    xauthority: str = DEFAULT_XAUTHORITY,
    title: str = DEFAULT_WINDOW_TITLE,
) -> None:
    window = find_wechat_window(
        backend=backend,
        xpra_target=xpra_target,
        password_file=password_file,
        display=display,
        xauthority=xauthority,
        title=title,
    )
    click_window(window, x, y, button=button, display=display, xauthority=xauthority)


def move_wechat_mouse(
    x: int,
    y: int,
    backend: str = DEFAULT_BACKEND,
    xpra_target: str = DEFAULT_XPRA_TARGET,
    password_file: str = DEFAULT_PASSWORD_FILE,
    display: str = DEFAULT_X_DISPLAY,
    xauthority: str = DEFAULT_XAUTHORITY,
    title: str = DEFAULT_WINDOW_TITLE,
) -> None:
    window = find_wechat_window(
        backend=backend,
        xpra_target=xpra_target,
        password_file=password_file,
        display=display,
        xauthority=xauthority,
        title=title,
    )
    move_window_mouse(window, x, y, display=display, xauthority=xauthority)
