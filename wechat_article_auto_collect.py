from __future__ import annotations

import argparse
import os
import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Callable, Literal, NamedTuple

import cv2
import numpy as np
from dclassql import Client

from common import discover_wechat_pids, role_from_cmdline
from fetch_wechat_article import DEFAULT_FETCH_DELAY, ArticleFetcher
from wechat_mem_item_xml_scan import ItemXml, dedupe
from wechat_mem_item_xml_scan import scan_pid as scan_pid_matcher
from wechat_mem_xml_parse_scan import scan_pid as scan_pid_parser
from x11_wechat import capture_wechat_png, click_wechat, move_wechat_mouse


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

    def find_unread_subs(self) -> list[tuple[int, int]]:
        account_list_img = self.account_list_img()
        if account_list_img is None:
            return []

        mask = (account_list_img == np.array([250, 81, 81, 255])).all(axis=2)
        _, binary = cv2.threshold(mask.astype(np.uint8) * 255, 10, 255, cv2.THRESH_BINARY)
        contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        subs: list[tuple[int, int]] = []
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            mini = account_list_img[y : y + h, x : x + w]
            score = mini[..., :3].std(2).mean() * 3
            if score > 200:
                subs.append((x + self.split_line_idxs[1], y))
        return sorted(subs, key=lambda item: (item[1], item[0]))


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
    on_new_article: Callable[[int], None] | None = None,
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
            article = client.article.insert(
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
            if on_new_article is not None:
                on_new_article(article.id)
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


def capture_unread_points() -> list[tuple[int, int]]:
    image = capture_wechat_png()
    ui = WechatUi(image)
    ui.find_split_line()
    points = ui.find_unread_subs()
    print(f"界面检查: split_lines={ui.split_line_idxs} unread={len(points)} points={points}")
    return points


def log_fetch_error(future: Future[None]) -> None:
    error = future.exception()
    if error is not None:
        print(f"文章内容抓取失败: {error}", file=sys.stderr)


def submit_article_fetch(executor: ThreadPoolExecutor, fetcher: ArticleFetcher, article_id: int) -> None:
    future = executor.submit(fetcher.fetch_article_content_by_id, article_id)
    future.add_done_callback(log_fetch_error)


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
    on_new_article = lambda article_id: submit_article_fetch(executor, fetcher, article_id)
    result = scan_articles(scanner=scanner, all_regions=all_regions)
    save_articles(client, result, "启动扫描", print_scanned=print_all, on_new_article=on_new_article)

    clicked = 0
    while clicked < max_clicks:
        unread_points = capture_unread_points()
        if not unread_points:
            print("没有未读红点，本轮结束")
            return

        x, y = unread_points[0]
        click_x = x + 10
        click_y = y + 10
        print(f"处理红点: index={clicked + 1} move=({click_x}, {click_y}) click=left")
        move_wechat_mouse(click_x, click_y)
        click_wechat(click_x, click_y)
        save_click(
            client,
            x=click_x,
            y=click_y,
            wait_after_click=wait_after_click,
            unread_count_before=len(unread_points),
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
            on_new_article=on_new_article,
        )
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
