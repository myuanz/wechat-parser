import argparse
from pathlib import Path

import cv2
import numpy as np

import x11_wechat
from x11_wechat import capture_wechat_png, click_wechat, find_wechat_window


def save_rgb(path: Path, image: np.ndarray) -> None:
    ok = cv2.imwrite(str(path), cv2.cvtColor(image, cv2.COLOR_RGB2BGR))
    if not ok:
        raise RuntimeError(f"写图片失败: {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="最小化测试微信窗口模拟点击是否生效")
    parser.add_argument("--x", type=int, default=320, help="微信窗口内相对 x 坐标")
    parser.add_argument("--y", type=int, default=80, help="微信窗口内相对 y 坐标")
    parser.add_argument("--roi-x1", type=int, default=272, help="对比区域左上角 x")
    parser.add_argument("--roi-y1", type=int, default=40, help="对比区域左上角 y")
    parser.add_argument("--roi-x2", type=int, default=483, help="对比区域右下角 x")
    parser.add_argument("--roi-y2", type=int, default=120, help="对比区域右下角 y")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path.cwd(),
        help="输出目录，默认当前目录",
    )
    args = parser.parse_args()

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    window = find_wechat_window()
    print(f"window_title={window.title}")
    print(f"window_xid={window.xid}")
    print(f"window_client_geometry={window.client_geometry}")
    print(f"session_type={x11_wechat.DEFAULT_XDG_SESSION_TYPE}")
    print(f"display={x11_wechat.DEFAULT_X_DISPLAY}")
    print(f"wayland_display={x11_wechat.DEFAULT_WAYLAND_DISPLAY}")
    print(f"xauthority={x11_wechat.DEFAULT_XAUTHORITY}")

    before = capture_wechat_png()
    click_wechat(args.x, args.y)
    after = capture_wechat_png()

    roi = (args.roi_x1, args.roi_y1, args.roi_x2, args.roi_y2)
    x1, y1, x2, y2 = roi
    before_roi = before[y1:y2, x1:x2]
    after_roi = after[y1:y2, x1:x2]
    diff = cv2.absdiff(before_roi, after_roi)

    changed_mask = diff.max(axis=2) > 10
    changed_pixels = int(np.count_nonzero(changed_mask))
    mean_diff = float(diff.mean())
    max_diff = int(diff.max())

    before_path = output_dir / "min_test_click_before_roi.png"
    after_path = output_dir / "min_test_click_after_roi.png"
    diff_path = output_dir / "min_test_click_diff_roi.png"
    save_rgb(before_path, before_roi)
    save_rgb(after_path, after_roi)
    save_rgb(diff_path, diff)

    print(f"click_relative=({args.x}, {args.y})")
    if window.client_geometry is not None:
        left, top, _, _ = window.client_geometry
        print(f"click_absolute=({left + args.x}, {top + args.y})")
    print(f"roi={roi}")
    print(f"changed_pixels={changed_pixels}")
    print(f"mean_diff={mean_diff}")
    print(f"max_diff={max_diff}")
    print(f"before_image={before_path}")
    print(f"after_image={after_path}")
    print(f"diff_image={diff_path}")


if __name__ == "__main__":
    main()
