from __future__ import annotations

import argparse
import os
import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime
from dataclasses import dataclass
from typing import Literal, NamedTuple

import cv2
import numpy as np
from dclassql import Client

from common import discover_wechat_pids, role_from_cmdline
from fetch_wechat_article import DEFAULT_FETCH_DELAY, ArticleFetcher
from wechat_mem_item_xml_scan import ItemXml, dedupe
from wechat_mem_item_xml_scan import scan_pid as scan_pid_matcher
from wechat_mem_xml_parse_scan import scan_pid as scan_pid_parser
from x11_wechat import Rect, capture_wechat_png, click_wechat


Stage = Literal["init", "flow", "tab"]
Scanner = Literal["parser", "matcher"]


def to_ch4(img: np.ndarray) -> np.ndarray:
    if img.shape[-1] == 3:
        return np.concatenate([img, np.ones((*img.shape[:2], 1), dtype=np.uint8) * 255], axis=2)
    return img


class WechatUi:
    def __init__(self, raw_img: np.ndarray):
        self.raw_img = to_ch4(raw_img)
        self.bin_img = (self.raw_img * 1.0 - 225 > 0).astype(np.uint8) * 255
        self.split_line_idxs: list[int] = []

    def find_split_line(self) -> list[int]:
        gray = self.bin_img.mean(axis=2)
        score = gray.std(axis=0) * gray.mean(axis=0)
        idxs: list[int] = []
        for idx in np.where(score == 0)[0]:
            if idx != 0 and (not idxs or idx - idxs[-1] > 10):
                idxs.append(int(idx))
        self.split_line_idxs = idxs
        return idxs

    def account_list_img(self) -> np.ndarray | None:
        if not self.split_line_idxs:
            self.find_split_line()
        if len(self.split_line_idxs) < 3:
            return None
        return self.raw_img[:, self.split_line_idxs[1] : self.split_line_idxs[2]]

    def find_unread_subs(self) -> list["UnreadSubTarget"]:
        account_list_img = self.account_list_img()
        if account_list_img is None:
            return []

        rgb = account_list_img[..., :3].astype(np.int16)
        red = np.array([250, 81, 81], dtype=np.int16)
        color_diff = np.abs(rgb - red).max(axis=2)
        alpha_ok = self.raw_img[:, self.split_line_idxs[1] : self.split_line_idxs[2], 3] >= 240
        mask = (color_diff <= 12) & alpha_ok
        _, binary = cv2.threshold(mask.astype(np.uint8) * 255, 10, 255, cv2.THRESH_BINARY)
        contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        subs: list[UnreadSubTarget] = []
        account_list_x = self.split_line_idxs[1]
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            mini = account_list_img[y : y + h, x : x + w]
            score = mini[..., :3].std(2).mean() * 3
            area = w * h
            strict_pixels = int(mask[y : y + h, x : x + w].sum())
            is_small_exact_badge = w <= 8 and h <= 8 and strict_pixels >= 8
            if area < 8 or (score <= 200 and not is_small_exact_badge):
                continue

            badge_center_x = x + w // 2 + account_list_x
            badge_center_y = y + h // 2
            click_x = account_list_x + min(max(48, x - 24), account_list_img.shape[1] - 24)
            click_y = y + h // 2
            subs.append(
                UnreadSubTarget(
                    badge_x=badge_center_x,
                    badge_y=badge_center_y,
                    click_x=click_x,
                    click_y=click_y,
                    width=w,
                    height=h,
                )
            )
        return sorted(subs, key=lambda item: (item.click_y, item.click_x))

    def debug_unread_candidates(self) -> list["UnreadCandidateDebug"]:
        account_list_img = self.account_list_img()
        if account_list_img is None:
            return []

        account_list_x = self.split_line_idxs[1]
        rgb = account_list_img[..., :3].astype(np.int16)
        alpha = self.raw_img[:, self.split_line_idxs[1] : self.split_line_idxs[2], 3]
        red = np.array([250, 81, 81], dtype=np.int16)
        color_diff = np.abs(rgb - red).max(axis=2)
        alpha_ok = alpha >= 240
        strict_mask = (color_diff <= 12) & alpha_ok

        r = rgb[..., 0]
        g = rgb[..., 1]
        b = rgb[..., 2]
        relaxed_mask = (r >= 160) & (r - g >= 40) & (r - b >= 40) & alpha_ok
        _, binary = cv2.threshold(relaxed_mask.astype(np.uint8) * 255, 10, 255, cv2.THRESH_BINARY)
        contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        rows: list[UnreadCandidateDebug] = []
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            mini = account_list_img[y : y + h, x : x + w]
            mini_rgb = mini[..., :3].astype(np.int16)
            mini_alpha = mini[..., 3]
            mini_diff = color_diff[y : y + h, x : x + w]
            mini_strict = strict_mask[y : y + h, x : x + w]
            score = float(mini[..., :3].std(2).mean() * 3)
            area = w * h
            strict_pixels = int(mini_strict.sum())
            alpha_min = int(mini_alpha.min())
            alpha_max = int(mini_alpha.max())
            center_rgb = tuple(int(v) for v in account_list_img[y + h // 2, x + w // 2, :3])
            mean_rgb = tuple(float(v) for v in mini_rgb.reshape(-1, 3).mean(axis=0))
            min_rgb = tuple(int(v) for v in mini_rgb.reshape(-1, 3).min(axis=0))
            max_rgb = tuple(int(v) for v in mini_rgb.reshape(-1, 3).max(axis=0))

            reasons: list[str] = []
            is_small_exact_badge = w <= 8 and h <= 8 and strict_pixels >= 8
            if strict_pixels == 0:
                reasons.append("color_diff<=12")
            if area < 8:
                reasons.append("area>=8")
            if score <= 200 and not is_small_exact_badge:
                reasons.append("score>200")
            if alpha_min < 240:
                reasons.append("alpha>=240")

            sample_points: list[PixelSample] = []
            local_points = np.argwhere(relaxed_mask[y : y + h, x : x + w])
            for local_y, local_x in local_points[:12]:
                px = x + int(local_x)
                py = y + int(local_y)
                sample_points.append(
                    PixelSample(
                        x=px + account_list_x,
                        y=py,
                        rgb=tuple(int(v) for v in account_list_img[py, px, :3]),
                        alpha=int(alpha[py, px]),
                        color_diff=int(color_diff[py, px]),
                    )
                )

            rows.append(
                UnreadCandidateDebug(
                    x=x + account_list_x,
                    y=y,
                    width=w,
                    height=h,
                    area=area,
                    score=score,
                    strict_pixels=strict_pixels,
                    relaxed_pixels=int(relaxed_mask[y : y + h, x : x + w].sum()),
                    color_diff_min=int(mini_diff.min()),
                    color_diff_mean=float(mini_diff.mean()),
                    color_diff_max=int(mini_diff.max()),
                    alpha_min=alpha_min,
                    alpha_max=alpha_max,
                    center_rgb=center_rgb,
                    mean_rgb=mean_rgb,
                    min_rgb=min_rgb,
                    max_rgb=max_rgb,
                    failed_rules=reasons,
                    samples=sample_points,
                )
            )
        return sorted(rows, key=lambda item: (item.y, item.x))


@dataclass
class CaptureState:
    account_list_region: Rect | None = None


@dataclass(frozen=True)
class UnreadSubTarget:
    badge_x: int
    badge_y: int
    click_x: int
    click_y: int
    width: int
    height: int


@dataclass(frozen=True)
class PixelSample:
    x: int
    y: int
    rgb: tuple[int, int, int]
    alpha: int
    color_diff: int


@dataclass(frozen=True)
class UnreadCandidateDebug:
    x: int
    y: int
    width: int
    height: int
    area: int
    score: float
    strict_pixels: int
    relaxed_pixels: int
    color_diff_min: int
    color_diff_mean: float
    color_diff_max: int
    alpha_min: int
    alpha_max: int
    center_rgb: tuple[int, int, int]
    mean_rgb: tuple[float, float, float]
    min_rgb: tuple[int, int, int]
    max_rgb: tuple[int, int, int]
    failed_rules: list[str]
    samples: list[PixelSample]


class ScanResult(NamedTuple):
    raw_count: int
    rows: list[ItemXml]


def article_key(row: ItemXml) -> str:
    return f"{row.biz}:{row.mid}:{row.idx}"


def account_key(row: ItemXml) -> str:
    return row.source_username or row.biz or row.source_name


def parse_pub_time(value: str) -> datetime | None:
    if not value:
        return None
    timestamp = int(value)
    if timestamp <= 0:
        return None
    return datetime.fromtimestamp(timestamp, UTC)


def scan_articles(scanner: Scanner, all_regions: bool = False) -> ScanResult:
    rows: list[ItemXml] = []
    scan_pid = scan_pid_parser if scanner == "parser" else scan_pid_matcher
    pids = [pid for pid in discover_wechat_pids() if role_from_cmdline(pid) == "wechat-main"]
    for pid in pids:
        current = scan_pid(pid, all_regions)
        print(f"内存扫描: scanner={scanner} pid={pid} role={role_from_cmdline(pid)} raw_items={len(current)}")
        rows.extend(current)
    return ScanResult(raw_count=len(rows), rows=dedupe(rows))


def save_articles(
    client: Client,
    result: ScanResult,
    reason: str,
    print_scanned: bool,
) -> None:
    observed_at = datetime.now(UTC)

    new_rows: list[ItemXml] = []
    for row in result.rows:
        acct_key = account_key(row)
        account = client.account.upsert(
            where={"key": acct_key},
            update={
                "biz": row.biz,
                "username": row.source_username,
                "name": row.source_name or row.show_name or row.source_username,
                "updated_at": observed_at,
            },
            insert={
                "key": acct_key,
                "biz": row.biz,
                "username": row.source_username,
                "name": row.source_name or row.show_name or row.source_username,
                "created_at": observed_at,
                "updated_at": observed_at,
            },
        )

        key = article_key(row)
        existing = client.article.find_first(where={"key": key})
        if existing is None:
            new_rows.append(row)
            client.article.insert(
                {
                    "key": key,
                    "account_id": account.id,
                    "biz": row.biz,
                    "mid": row.mid,
                    "idx": row.idx,
                    "title": row.title,
                    "url": row.url,
                    "digest": row.digest,
                    "summary": row.summary,
                    "pub_time": parse_pub_time(row.pub_time),
                    "first_seen_at": observed_at,
                    "last_seen_at": observed_at,
                    "content_fetched_at": None,
                    "seen_count": 1,
                }
            )
        else:
            client.article.update(
                where={"id": existing.id},
                data={
                    "account_id": account.id,
                    "title": row.title,
                    "url": row.url,
                    "digest": row.digest,
                    "summary": row.summary,
                    "pub_time": parse_pub_time(row.pub_time),
                    "last_seen_at": observed_at,
                    "seen_count": existing.seen_count + 1,
                },
            )

    print(
        f"保存完成: reason={reason} raw={result.raw_count} items={len(result.rows)} "
        f"new={len(new_rows)}"
    )
    print_rows = result.rows if print_scanned else new_rows
    if not print_rows:
        print("  无新增文章")
    else:
        for row in print_rows:
            print(f"  - {row.source_name or row.show_name or row.source_username}: {row.title}")


def save_click(
    client: Client,
    x: int,
    y: int,
    wait_after_click: float,
    unread_count_before: int,
    order_index: int,
) -> None:
    client.click_event.insert(
        {
            "clicked_at": datetime.now(UTC),
            "x": x,
            "y": y,
            "wait_seconds": int(wait_after_click),
            "unread_count_before": unread_count_before,
            "order_index": order_index,
        }
    )
    print(f"点击记录: order={order_index} pos=({x}, {y})")


def _find_unread_targets_in_account_list(account_list_img: np.ndarray, account_list_x: int) -> list[UnreadSubTarget]:
    raw_img = to_ch4(account_list_img)
    rgb = raw_img[..., :3].astype(np.int16)
    red = np.array([250, 81, 81], dtype=np.int16)
    color_diff = np.abs(rgb - red).max(axis=2)
    alpha_ok = raw_img[..., 3] >= 240
    mask = (color_diff <= 12) & alpha_ok
    _, binary = cv2.threshold(mask.astype(np.uint8) * 255, 10, 255, cv2.THRESH_BINARY)
    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    subs: list[UnreadSubTarget] = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        mini = raw_img[y : y + h, x : x + w]
        score = mini[..., :3].std(2).mean() * 3
        area = w * h
        strict_pixels = int(mask[y : y + h, x : x + w].sum())
        is_small_exact_badge = w <= 8 and h <= 8 and strict_pixels >= 8
        if area < 8 or (score <= 200 and not is_small_exact_badge):
            continue

        badge_center_x = x + w // 2 + account_list_x
        badge_center_y = y + h // 2
        click_x = account_list_x + min(max(48, x - 24), raw_img.shape[1] - 24)
        click_y = y + h // 2
        subs.append(
            UnreadSubTarget(
                badge_x=badge_center_x,
                badge_y=badge_center_y,
                click_x=click_x,
                click_y=click_y,
                width=w,
                height=h,
            )
        )
    return sorted(subs, key=lambda item: (item.click_y, item.click_x))


def capture_unread_points(state: CaptureState) -> list[UnreadSubTarget]:
    if state.account_list_region is not None:
        try:
            account_list_img = capture_wechat_png(region=state.account_list_region)
        except RuntimeError:
            state.account_list_region = None
        else:
            targets = _find_unread_targets_in_account_list(account_list_img, state.account_list_region[0])
            summary = [
                {
                    "badge": (target.badge_x, target.badge_y),
                    "click": (target.click_x, target.click_y),
                    "size": (target.width, target.height),
                }
                for target in targets
            ]
            print(
                f"界面检查: mode=roi region={state.account_list_region} "
                f"unread={len(targets)} targets={summary}"
            )
            return targets

    image = capture_wechat_png(use_cache=True)
    ui = WechatUi(image)
    ui.find_split_line()
    account_list_img = ui.account_list_img()
    if account_list_img is None or len(ui.split_line_idxs) < 3:
        state.account_list_region = None
        print(f"界面检查: split_lines={ui.split_line_idxs} unread=0 targets=[]")
        return []

    left = ui.split_line_idxs[1]
    right = ui.split_line_idxs[2]
    state.account_list_region = (left, 0, right - left, image.shape[0])
    targets = _find_unread_targets_in_account_list(account_list_img, left)
    summary = [
        {
            "badge": (target.badge_x, target.badge_y),
            "click": (target.click_x, target.click_y),
            "size": (target.width, target.height),
        }
        for target in targets
    ]
    print(
        f"界面检查: mode=full split_lines={ui.split_line_idxs} region={state.account_list_region} "
        f"unread={len(targets)} targets={summary}"
    )
    return targets


def log_fetch_queue_error(future: Future[None]) -> None:
    error = future.exception()
    if error is not None:
        print(f"文章内容队列消费失败: {error}", file=sys.stderr, flush=True)


def submit_pending_article_fetch(executor: ThreadPoolExecutor, fetcher: ArticleFetcher) -> None:
    future = executor.submit(fetcher.fetch_pending_article_contents)
    future.add_done_callback(log_fetch_queue_error)


def collect_once(
    client: Client,
    executor: ThreadPoolExecutor,
    wait_after_click: float,
    max_clicks: int,
    all_regions: bool,
    scanner: Scanner,
    print_all: bool,
    fetcher: ArticleFetcher,
) -> None:
    result = scan_articles(scanner=scanner, all_regions=all_regions)
    save_articles(client, result, "启动扫描", print_scanned=print_all)
    submit_pending_article_fetch(executor, fetcher)

    clicked = 0
    attempted_badges: set[tuple[int, int]] = set()
    capture_state = CaptureState()
    while clicked < max_clicks:
        try:
            unread_targets = capture_unread_points(capture_state)
        except RuntimeError as exc:
            print(f"界面检查失败，结束本轮: {exc}")
            return
        if not unread_targets:
            print("没有未读红点，本轮结束")
            return

        target = next(
            (
                item
                for item in unread_targets
                if (item.badge_x, item.badge_y) not in attempted_badges
            ),
            None,
        )
        if target is None:
            print(f"当前可见红点都已尝试过，本轮结束 unread={len(unread_targets)}")
            return

        attempted_badges.add((target.badge_x, target.badge_y))
        click_x = target.click_x
        click_y = target.click_y
        print(
            f"处理红点: index={clicked + 1} badge=({target.badge_x}, {target.badge_y}) "
            f"move=({click_x}, {click_y}) click=left"
        )
        try:
            click_wechat(click_x, click_y)
        except RuntimeError as exc:
            print(f"点击失败，结束本轮: {exc}")
            return
        save_click(
            client,
            x=click_x,
            y=click_y,
            wait_after_click=wait_after_click,
            unread_count_before=len(unread_targets),
            order_index=clicked + 1,
        )

        print(f"等待: {wait_after_click:g}s")
        time.sleep(wait_after_click)

        result = scan_articles(scanner=scanner, all_regions=all_regions)
        save_articles(
            client,
            result,
            f"点击红点 {clicked + 1} 后",
            print_scanned=False,
        )
        submit_pending_article_fetch(executor, fetcher)
        clicked += 1

    print(f"达到最大点击次数 max_clicks={max_clicks}，停止本轮")


def reexec_after_sleep(interval: float) -> None:
    print(f"休眠: {interval:g}s")
    time.sleep(interval)

    argv = [sys.executable, *sys.argv]
    if "--reexeced" not in sys.argv:
        argv.append("--reexeced")
    print(f"重新执行: {' '.join(argv)}")
    os.execv(sys.executable, argv)


def main() -> None:
    parser = argparse.ArgumentParser(description="定期抓取微信内存文章列表，并依次点击未读红点刷新数据")
    parser.add_argument("--interval", type=float, default=0, help="循环间隔秒数；0 表示只跑一轮")
    parser.add_argument("--wait-after-click", type=float, default=5, help="每次点击红点后的等待秒数")
    parser.add_argument("--max-clicks", type=int, default=20, help="单轮最多处理多少个红点")
    parser.add_argument("--fetch-delay", type=float, default=DEFAULT_FETCH_DELAY, help="后台抓取文章内容时每次请求之间的间隔秒数")
    parser.add_argument("--all-regions", action="store_true", help="传给内存扫描，扫描更多内存区域")
    parser.add_argument("--scanner", choices=["parser", "matcher"], default="parser", help="内存扫描实现；parser=严格 XML 解析，matcher=旧文本匹配")
    parser.add_argument("--reexeced", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    client = Client()
    try:
        print("== 本轮开始 ==")
        fetcher = ArticleFetcher(fetch_delay=args.fetch_delay)
        with ThreadPoolExecutor(max_workers=1) as executor:
            collect_once(
                client,
                executor=executor,
                wait_after_click=args.wait_after_click,
                max_clicks=args.max_clicks,
                all_regions=args.all_regions,
                scanner=args.scanner,
                print_all=not args.reexeced,
                fetcher=fetcher,
            )
        print("== 本轮结束 ==")
    finally:
        Client.close_all()

    if args.interval > 0:
        reexec_after_sleep(args.interval)


if __name__ == "__main__":
    main()
