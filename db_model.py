from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


__datasource__ = {
    "provider": "sqlite",
    "url": "sqlite:///wechat_articles.db",
}


@dataclass
class Account:
    id: int
    key: str
    biz: str
    username: str
    name: str
    created_at: datetime
    updated_at: datetime

    articles: list[Article]

    def index(self):
        yield self.name
        yield self.biz
        yield self.username
        yield self.updated_at

    def unique_index(self):
        yield self.key


@dataclass
class Article:
    id: int
    key: str
    account_id: int
    account: Account

    biz: str
    mid: str
    idx: str
    title: str
    url: str
    digest: str
    summary: str
    pub_time: datetime | None
    first_seen_at: datetime
    last_seen_at: datetime
    seen_count: int

    observations: list[ArticleObservation]

    def index(self):
        yield self.account_id
        yield self.pub_time
        yield self.last_seen_at
        yield self.mid

    def unique_index(self):
        yield self.key
        yield self.biz, self.mid, self.idx

    def foreign_key(self):
        yield self.account.id == self.account_id, Account.articles


@dataclass
class ScanRun:
    id: int
    started_at: datetime
    reason: str
    raw_count: int
    item_count: int

    observations: list[ArticleObservation]
    clicks: list[ClickEvent]

    def index(self):
        yield self.started_at
        yield self.reason


@dataclass
class ArticleObservation:
    id: int
    scan_id: int
    article_id: int
    scan: ScanRun
    article: Article

    pid: int
    addr: str
    source_name: str
    show_name: str
    source_username: str
    total_count: int
    observed_title: str
    observed_url: str
    observed_at: datetime

    def index(self):
        yield self.scan_id
        yield self.article_id
        yield self.observed_at
        yield self.pid

    def unique_index(self):
        yield self.scan_id, self.article_id

    def foreign_key(self):
        yield self.scan.id == self.scan_id, ScanRun.observations
        yield self.article.id == self.article_id, Article.observations


@dataclass
class ClickEvent:
    id: int
    scan_id: int
    scan: ScanRun
    clicked_at: datetime
    x: int
    y: int
    wait_seconds: int
    unread_count_before: int
    order_index: int

    def index(self):
        yield self.scan_id
        yield self.clicked_at

    def foreign_key(self):
        yield self.scan.id == self.scan_id, ScanRun.clicks
