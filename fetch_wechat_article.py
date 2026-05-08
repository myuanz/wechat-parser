from __future__ import annotations

import argparse
from compression import zstd
from datetime import UTC, datetime, timedelta
from pathlib import Path
import random
import sys
import tempfile
import time
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

from dclassql import Client
from filelock import FileLock
from lxml import html as lxml_html
from lxml.html import HtmlElement, tostring


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/117.0.0.0 Safari/537.36 WAE/1.0"
)
DEFAULT_FETCH_DELAY = 3.0
FETCH_STATE_DIR = Path(tempfile.gettempdir())
FETCH_LOCK_PATH = FETCH_STATE_DIR / "wechat_article_fetch.lock"
FETCH_STAMP_PATH = FETCH_STATE_DIR / "wechat_article_fetch.last"
MAX_RETRIES = 5
RETRY_DELAYS = [
    timedelta(minutes=10),
    timedelta(hours=1),
    timedelta(hours=6),
    timedelta(hours=24),
    timedelta(hours=72),
]


def next_retry_at(retry_count: int, now: datetime) -> datetime | None:
    if retry_count >= MAX_RETRIES:
        return None
    return now + RETRY_DELAYS[retry_count - 1]


def should_fetch_article(article) -> bool:
    if article.content_fetched_at is not None:
        return False
    if article.content is None:
        return True
    if article.content.status == "pending":
        return True
    if article.content.status != "failed":
        return False
    return article.content.next_retry_at is not None and article.content.next_retry_at <= datetime.now(UTC)


def is_valid_mp_article_url(url: str) -> bool:
    return urlparse(url).hostname == "mp.weixin.qq.com"


def fetch_raw_html(url: str) -> str:
    url = unquote(url.strip())
    if not is_valid_mp_article_url(url):
        raise ValueError("url 不合法，hostname 必须是 mp.weixin.qq.com")

    request = Request(
        url,
        headers={
            "Referer": "https://mp.weixin.qq.com/",
            "Origin": "https://mp.weixin.qq.com",
            "User-Agent": USER_AGENT,
        },
    )
    with urlopen(request, timeout=30) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def find_by_id(root: HtmlElement, element_id: str) -> HtmlElement | None:
    nodes = root.xpath(f'//*[@id="{element_id}"]')
    return nodes[0] if nodes else None


def remove_nodes(parent: HtmlElement, xpath: str) -> None:
    for node in parent.xpath(xpath):
        node_parent = node.getparent()
        if node_parent is not None:
            node_parent.remove(node)


def normalize_html(raw_html: str) -> str:
    root = lxml_html.fromstring(raw_html)
    js_article = find_by_id(root, "js_article")
    if js_article is None:
        raise ValueError("未找到 #js_article")

    js_content = find_by_id(js_article, "js_content")
    if js_content is not None:
        js_content.attrib.pop("style", None)

    for element_id in [
        "js_top_ad_area",
        "js_tags_preview_toast",
        "content_bottom_area",
        "js_pc_qr_code",
        "wx_stream_article_slide_tip",
    ]:
        remove_nodes(js_article, f'.//*[@id="{element_id}"]')

    remove_nodes(js_article, ".//script")

    for img in root.xpath(".//img"):
        img_url = img.get("src") or img.get("data-src")
        if img_url:
            img.set("src", img_url)

    body = root.find("body")
    body_class = body.get("class") if body is not None else ""
    page_content_html = tostring(js_article, encoding="unicode", method="html")

    return f"""<!DOCTYPE html>
  <html lang="zh_CN">
  <head>
      <meta charset="utf-8">
      <meta http-equiv="Content-Type" content="text/html; charset=utf-8">
      <meta http-equiv="X-UA-Compatible" content="IE=edge">
      <meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0,user-scalable=0,viewport-fit=cover">
      <meta name="referrer" content="no-referrer">
      <style>
          #js_row_immersive_stream_wrap {{
              max-width: 667px;
              margin: 0 auto;
          }}
          #js_row_immersive_stream_wrap .wx_follow_avatar_pic {{
            display: block;
            margin: 0 auto;
          }}
          #page-content,
          #js_article_bottom_bar,
          .__page_content__ {{
              max-width: 667px;
              margin: 0 auto;
          }}
          img {{
              max-width: 100%;
          }}
          .sns_opr_btn::before {{
              width: 16px;
              height: 16px;
              margin-right: 3px;
          }}
      </style>
  </head>
  <body class="{body_class}">
  {page_content_html}
  </body>
  </html>
    """


def fetch_normalized_html(url: str) -> str:
    return normalize_html(fetch_raw_html(url))


def compress_html(html: str) -> bytes:
    return zstd.compress(html.encode("utf-8"))


class ArticleFetcher:
    def __init__(
        self,
        fetch_delay: float = DEFAULT_FETCH_DELAY,
        lock_path: Path = FETCH_LOCK_PATH,
        stamp_path: Path = FETCH_STAMP_PATH,
    ):
        self.fetch_delay = fetch_delay
        self.lock_path = lock_path
        self.stamp_path = stamp_path

    def wait_fetch_slot(self) -> None:
        if self.fetch_delay <= 0:
            return

        with FileLock(str(self.lock_path)):
            if self.stamp_path.exists():
                text = self.stamp_path.read_text().strip()
                last_fetch_started_at = float(text) if text else 0
            else:
                last_fetch_started_at = 0

            interval = random.uniform(self.fetch_delay, self.fetch_delay * 2)
            current = time.monotonic()
            wait_seconds = interval - (current - last_fetch_started_at)
            if wait_seconds > 0:
                time.sleep(wait_seconds)

            self.stamp_path.write_text(str(time.monotonic()))

    def fetch_normalized_html(self, url: str) -> str:
        self.wait_fetch_slot()
        return fetch_normalized_html(url)

    def fetch_and_save_article_content(self, client: Client, article_id: int, url: str) -> None:
        updated_at = datetime.now(UTC)
        try:
            normalized_html = self.fetch_normalized_html(url)
            compressed_html = compress_html(normalized_html)
            client.article_content.upsert(
                where={"article_id": article_id},
                update={
                    "url": url,
                    "normalized_html_zstd": compressed_html,
                    "status": "fetched",
                    "retry_count": 0,
                    "next_retry_at": None,
                    "fetch_error": None,
                    "fetched_at": updated_at,
                    "updated_at": updated_at,
                },
                insert={
                    "article_id": article_id,
                    "url": url,
                    "normalized_html_zstd": compressed_html,
                    "status": "fetched",
                    "retry_count": 0,
                    "next_retry_at": None,
                    "fetch_error": None,
                    "fetched_at": updated_at,
                    "updated_at": updated_at,
                },
            )
            client.article.update(
                where={"id": article_id},
                data={"content_fetched_at": updated_at},
            )
        except Exception as error:
            existing = client.article_content.find_first(where={"article_id": article_id})
            retry_count = (existing.retry_count if existing else 0) + 1
            status = "give_up" if retry_count >= MAX_RETRIES else "failed"
            client.article_content.upsert(
                where={"article_id": article_id},
                update={
                    "url": url,
                    "normalized_html_zstd": None,
                    "status": status,
                    "retry_count": retry_count,
                    "next_retry_at": next_retry_at(retry_count, updated_at),
                    "fetch_error": str(error),
                    "fetched_at": None,
                    "updated_at": updated_at,
                },
                insert={
                    "article_id": article_id,
                    "url": url,
                    "normalized_html_zstd": None,
                    "status": status,
                    "retry_count": retry_count,
                    "next_retry_at": next_retry_at(retry_count, updated_at),
                    "fetch_error": str(error),
                    "fetched_at": None,
                    "updated_at": updated_at,
                },
            )
            client.article.update(
                where={"id": article_id},
                data={"content_fetched_at": None},
            )
            raise

    def fetch_article_content_by_id(self, article_id: int) -> None:
        client = Client()
        try:
            article = client.article.find_first(where={"id": article_id})
            if article is None:
                raise ValueError(f"文章不存在: article_id={article_id}")
            print(f"文章内容抓取开始: article_id={article.id} title={article.title}", flush=True)
            self.fetch_and_save_article_content(client, article.id, article.url)
            print(f"文章内容抓取成功: article_id={article.id} title={article.title}", flush=True)
        finally:
            Client.close_all()

    def fetch_pending_article_contents(self, limit: int | None = None) -> None:
        client = Client()
        fetched = 0
        try:
            articles = client.article.find_many(
                where={"content_fetched_at": None},
                include={"content": True},
                order_by={"first_seen_at": "asc"},
            )
            for article in articles:
                if limit is not None and fetched >= limit:
                    return
                if not should_fetch_article(article):
                    continue

                print(f"抓取文章: article_id={article.id} title={article.title}", flush=True)
                try:
                    self.fetch_and_save_article_content(client, article.id, article.url)
                    print(f"抓取成功: article_id={article.id} title={article.title}", flush=True)
                except Exception as error:
                    print(f"抓取失败: article_id={article.id} error={error}", file=sys.stderr, flush=True)
                fetched += 1
        finally:
            Client.close_all()


def main() -> None:
    parser = argparse.ArgumentParser(description="抓取微信公众号文章 HTML")
    parser.add_argument("url", nargs="?", help="微信公众号文章链接；不传则抓取数据库中未抓取的文章")
    parser.add_argument("--input", help="读取本地 raw HTML 文件并输出 normalize_html 结果")
    parser.add_argument("--limit", type=int, help="不传 URL 时，最多抓取多少篇数据库文章")
    parser.add_argument("--fetch-delay", type=float, default=DEFAULT_FETCH_DELAY, help="批量抓取时每次请求之间的间隔秒数")
    parser.add_argument("-o", "--output", help="传 URL 或 --input 时写入文件；不传则输出到 stdout")
    args = parser.parse_args()

    if args.input:
        with open(args.input, encoding="utf-8") as file:
            html = normalize_html(file.read())
    elif args.url:
        html = fetch_normalized_html(args.url)
    else:
        if args.output:
            parser.error("不传 URL 时不能使用 --output")
        ArticleFetcher(fetch_delay=args.fetch_delay).fetch_pending_article_contents(limit=args.limit)
        return

    if args.output:
        with open(args.output, "w", encoding="utf-8") as file:
            file.write(html)
        return

    sys.stdout.write(html)


if __name__ == "__main__":
    main()
