from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


DB_PATH = Path(__file__).with_name("wechat_articles.db").resolve()


__datasource__ = {
    "provider": "sqlite",
    "url": f"sqlite:///{DB_PATH}",
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

    articles: list["Article"]

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
    content_fetched_at: datetime | None
    seen_count: int

    content: "ArticleContent | None"

    def index(self):
        yield self.account_id
        yield self.pub_time
        yield self.last_seen_at
        yield self.content_fetched_at
        yield self.mid

    def unique_index(self):
        yield self.key
        yield self.biz, self.mid, self.idx

    def foreign_key(self):
        yield self.account.id == self.account_id, Account.articles


@dataclass
class ArticleContent:
    id: int
    article_id: int
    article: Article
    url: str
    normalized_html_zstd: bytes | None
    status: str
    fetch_error: str | None
    fetched_at: datetime | None
    updated_at: datetime
    retry_count: int = 0
    next_retry_at: datetime | None = None

    def index(self):
        yield self.article_id
        yield self.status
        yield self.next_retry_at
        yield self.updated_at

    def unique_index(self):
        yield self.article_id

    def foreign_key(self):
        yield self.article.id == self.article_id, Article.content


@dataclass
class ClickEvent:
    id: int
    clicked_at: datetime
    x: int
    y: int
    wait_seconds: int
    unread_count_before: int
    order_index: int

    def index(self):
        yield self.clicked_at


@dataclass
class ZhihuAuthor:
    id: int
    slug: str
    name: str
    profile_url: str
    headline: str
    avatar_url: str
    is_following: bool
    last_seen_content_id: str | None
    last_seen_pub_time: datetime | None
    first_seen_at: datetime
    last_seen_at: datetime
    created_at: datetime
    updated_at: datetime

    answers: list["ZhihuAnswer"]
    articles: list["ZhihuArticle"]
    pins: list["ZhihuPin"]

    def index(self):
        yield self.name
        yield self.is_following
        yield self.last_seen_at
        yield self.updated_at

    def unique_index(self):
        yield self.slug
        yield self.profile_url


@dataclass
class ZhihuAnswer:
    id: int
    answer_id: str
    author_id: int
    author: ZhihuAuthor

    question_id: str
    question_title: str
    question_api_url: str
    answer_api_url: str
    answer_url: str
    excerpt: str
    content_html: str
    content_text: str
    created_time: datetime
    updated_time: datetime
    first_seen_at: datetime
    last_seen_at: datetime
    voteup_count: int
    comment_count: int
    thanks_count: int | None

    def index(self):
        yield self.author_id
        yield self.question_id
        yield self.created_time
        yield self.updated_time
        yield self.last_seen_at

    def unique_index(self):
        yield self.answer_id

    def foreign_key(self):
        yield self.author.id == self.author_id, ZhihuAuthor.answers


@dataclass
class ZhihuPin:
    id: int
    pin_id: str
    author_id: int
    author: ZhihuAuthor

    pin_url: str
    excerpt_title: str
    content_html: str
    content_text: str
    created_time: datetime
    updated_time: datetime
    first_seen_at: datetime
    last_seen_at: datetime
    like_count: int
    comment_count: int
    reaction_count: int

    def index(self):
        yield self.author_id
        yield self.created_time
        yield self.updated_time
        yield self.last_seen_at

    def unique_index(self):
        yield self.pin_id

    def foreign_key(self):
        yield self.author.id == self.author_id, ZhihuAuthor.pins


@dataclass
class ZhihuArticle:
    id: int
    article_id: str
    author_id: int
    author: ZhihuAuthor

    article_url: str
    title: str
    excerpt: str
    content_html: str
    content_text: str
    created_time: datetime
    updated_time: datetime
    first_seen_at: datetime
    last_seen_at: datetime
    voteup_count: int
    comment_count: int

    def index(self):
        yield self.author_id
        yield self.created_time
        yield self.updated_time
        yield self.last_seen_at

    def unique_index(self):
        yield self.article_id

    def foreign_key(self):
        yield self.author.id == self.author_id, ZhihuAuthor.articles


@dataclass
class ZhihuTask:
    id: int
    task_type: str
    profile_url: str
    status: str
    requested_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    request_payload_json: str | None
    result_payload_json: str | None
    error: str | None
    created_at: datetime
    updated_at: datetime

    def index(self):
        yield self.task_type
        yield self.status
        yield self.requested_at
        yield self.finished_at
        yield self.updated_at
