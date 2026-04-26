from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile

import cv2
import numpy as np


DEFAULT_XPRA_TARGET = "tcp://127.0.0.1:10000"
DEFAULT_PASSWORD_FILE = "/etc/xpra-auth/password"
DEFAULT_X_DISPLAY = ":100"
DEFAULT_XAUTHORITY = "/home/wechat/.Xauthority"


@dataclass(frozen=True)
class X11Window:
    wid: int
    xid: str
    title: str
    size: tuple[int, int]
    client_geometry: tuple[int, int, int, int] | None


def _run(command: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, capture_output=True, text=True, env=env)
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


def find_wechat_window(
    xpra_target: str = DEFAULT_XPRA_TARGET,
    password_file: str = DEFAULT_PASSWORD_FILE,
) -> X11Window:
    command = ["xpra", "info", xpra_target, f"--password-file={password_file}"]
    info = _run(command).stdout

    candidates: list[X11Window] = []
    for window in _parse_xpra_windows(info):
        if window.get("title") != "微信":
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
            )
        )

    if not candidates:
        raise RuntimeError("没有找到 title=微信 且 tray=False 的 xpra 窗口")
    return max(candidates, key=lambda item: item.size[0] * item.size[1])


def capture_window_png(
    window: X11Window,
    output: str | Path | None = None,
    display: str = DEFAULT_X_DISPLAY,
    xauthority: str = DEFAULT_XAUTHORITY,
) -> np.ndarray:
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
    if output is not None:
        ok = cv2.imwrite(str(output), image)
        if not ok:
            raise RuntimeError(f"写出 PNG 失败: {output}")
    return cv2.cvtColor(image, cv2.COLOR_BGR2RGB)


def capture_wechat_png(
    output: str | Path | None = None,
    xpra_target: str = DEFAULT_XPRA_TARGET,
    password_file: str = DEFAULT_PASSWORD_FILE,
    display: str = DEFAULT_X_DISPLAY,
    xauthority: str = DEFAULT_XAUTHORITY,
) -> np.ndarray:
    window = find_wechat_window(xpra_target=xpra_target, password_file=password_file)
    return capture_window_png(window, output=output, display=display, xauthority=xauthority)


def click_window(
    window: X11Window,
    x: int,
    y: int,
    button: int = 1,
    display: str = DEFAULT_X_DISPLAY,
    xauthority: str = DEFAULT_XAUTHORITY,
) -> None:
    env = _x11_env(display, xauthority)
    _run(["xdotool", "mousemove", "--window", window.xid, str(x), str(y), "click", str(button)], env=env)


def move_window_mouse(
    window: X11Window,
    x: int,
    y: int,
    display: str = DEFAULT_X_DISPLAY,
    xauthority: str = DEFAULT_XAUTHORITY,
) -> None:
    env = _x11_env(display, xauthority)
    _run(["xdotool", "mousemove", "--window", window.xid, str(x), str(y)], env=env)


def click_wechat(
    x: int,
    y: int,
    button: int = 1,
    xpra_target: str = DEFAULT_XPRA_TARGET,
    password_file: str = DEFAULT_PASSWORD_FILE,
    display: str = DEFAULT_X_DISPLAY,
    xauthority: str = DEFAULT_XAUTHORITY,
) -> None:
    window = find_wechat_window(xpra_target=xpra_target, password_file=password_file)
    click_window(window, x, y, button=button, display=display, xauthority=xauthority)


def move_wechat_mouse(
    x: int,
    y: int,
    xpra_target: str = DEFAULT_XPRA_TARGET,
    password_file: str = DEFAULT_PASSWORD_FILE,
    display: str = DEFAULT_X_DISPLAY,
    xauthority: str = DEFAULT_XAUTHORITY,
) -> None:
    window = find_wechat_window(xpra_target=xpra_target, password_file=password_file)
    move_window_mouse(window, x, y, display=display, xauthority=xauthority)
