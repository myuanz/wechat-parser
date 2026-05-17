import argparse
from pathlib import Path

import cv2
import numpy as np

from wechat_article_auto_collect import WechatUi
from x11_wechat import capture_wechat_png


def draw_debug_overlay(image: np.ndarray, ui: WechatUi) -> np.ndarray:
    canvas = ui.raw_img[..., :3].copy()

    for x in ui.split_line_idxs:
        cv2.line(canvas, (x, 0), (x, canvas.shape[0] - 1), (0, 255, 255), 1)

    for candidate in ui.debug_unread_candidates():
        if not candidate.failed_rules:
            color = (0, 0, 255)
        elif "color_diff<=12" in candidate.failed_rules:
            color = (255, 0, 255)
        else:
            color = (255, 128, 0)
        cv2.rectangle(
            canvas,
            (candidate.x, candidate.y),
            (candidate.x + candidate.width - 1, candidate.y + candidate.height - 1),
            color,
            1,
        )

    for index, target in enumerate(ui.find_unread_subs(), start=1):
        cv2.circle(canvas, (target.badge_x, target.badge_y), 8, (0, 0, 255), 2)
        cv2.circle(canvas, (target.click_x, target.click_y), 6, (0, 255, 0), -1)
        cv2.line(
            canvas,
            (target.badge_x, target.badge_y),
            (target.click_x, target.click_y),
            (255, 255, 0),
            1,
        )
        cv2.putText(
            canvas,
            str(index),
            (target.click_x + 8, target.click_y - 8),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            (0, 200, 0),
            1,
            cv2.LINE_AA,
        )

    return canvas


def main() -> None:
    parser = argparse.ArgumentParser(description="调试公众号红点识别和自动点击坐标")
    parser.add_argument("--input", type=Path, help="已有截图路径；不传则实时抓取微信窗口")
    parser.add_argument("--output", type=Path, default=Path("tmp_unread_debug.png"), help="标注图输出路径")
    args = parser.parse_args()

    if args.input is None:
        image = capture_wechat_png()
    else:
        bgr = cv2.imread(str(args.input), cv2.IMREAD_COLOR)
        if bgr is None:
            raise RuntimeError(f"OpenCV 无法读取图片: {args.input}")
        image = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)

    ui = WechatUi(image)
    split_lines = ui.find_split_line()
    targets = ui.find_unread_subs()

    print(f"split_lines={split_lines}")
    if not targets:
        print("unread_targets=[]")
    else:
        for index, target in enumerate(targets, start=1):
            print(
                f"{index}. badge=({target.badge_x}, {target.badge_y}) "
                f"click=({target.click_x}, {target.click_y}) size=({target.width}, {target.height})"
            )

    candidates = ui.debug_unread_candidates()
    print(f"candidate_contours={len(candidates)}")
    for index, candidate in enumerate(candidates, start=1):
        status = "pass" if not candidate.failed_rules else "fail:" + ",".join(candidate.failed_rules)
        print(
            f"{index}. box=({candidate.x}, {candidate.y}, {candidate.width}, {candidate.height}) "
            f"area={candidate.area} score={candidate.score:.1f} "
            f"strict_pixels={candidate.strict_pixels} relaxed_pixels={candidate.relaxed_pixels} "
            f"color_diff=min/mean/max({candidate.color_diff_min}/{candidate.color_diff_mean:.1f}/{candidate.color_diff_max}) "
            f"alpha=min/max({candidate.alpha_min}/{candidate.alpha_max}) "
            f"center_rgb={candidate.center_rgb} mean_rgb=({candidate.mean_rgb[0]:.1f}, {candidate.mean_rgb[1]:.1f}, {candidate.mean_rgb[2]:.1f}) "
            f"min_rgb={candidate.min_rgb} max_rgb={candidate.max_rgb} {status}"
        )
        for sample in candidate.samples:
            print(
                f"    pixel=({sample.x}, {sample.y}) rgb={sample.rgb} "
                f"alpha={sample.alpha} color_diff={sample.color_diff}"
            )

    overlay = draw_debug_overlay(image, ui)
    ok = cv2.imwrite(str(args.output), cv2.cvtColor(overlay, cv2.COLOR_RGB2BGR))
    if not ok:
        raise RuntimeError(f"写出调试图失败: {args.output}")
    print(f"debug_image={args.output.resolve()}")


if __name__ == "__main__":
    main()
