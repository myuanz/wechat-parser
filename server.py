import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import html_to_markdown
from compression import zstd
from dclassql import Client
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, PlainTextResponse

from db_model import Article

app = FastAPI(title="wechat-parser")


def _db():
    return sqlite3.connect("wechat_articles.db")


def _decompress(html_zstd: bytes | None) -> str | None:
    if html_zstd is None:
        return None
    return zstd.decompress(html_zstd).decode("utf-8")


def _normalize_dt(value: str) -> str:
    return datetime.fromisoformat(value).isoformat()


def _article_json(art: Article, content: str | None = None) -> dict:
    content_row = art.content
    return {
        "id": art.id,
        "key": art.key,
        "account_id": art.account_id,
        "biz": art.biz,
        "mid": art.mid,
        "idx": art.idx,
        "title": art.title,
        "url": art.url,
        "digest": art.digest,
        "summary": art.summary,
        "pub_time": art.pub_time.isoformat() if art.pub_time else None,
        "first_seen_at": art.first_seen_at.isoformat(),
        "last_seen_at": art.last_seen_at.isoformat(),
        "content_fetched_at": art.content_fetched_at.isoformat() if art.content_fetched_at else None,
        "seen_count": art.seen_count,
        "fetch_status": content_row.status if content_row else "pending",
        "retry_count": content_row.retry_count if content_row else 0,
        "next_retry_at": content_row.next_retry_at.isoformat() if content_row and content_row.next_retry_at else None,
        "fetch_error": content_row.fetch_error if content_row else None,
        "content": content,
    }


def _article_list_json(art: Article) -> dict:
    data = _article_json(art)
    data.pop("content")
    data["has_content"] = art.content_fetched_at is not None and data["fetch_status"] == "fetched"
    return data


def _article_where(account_id: int | None, pub_time_from: str | None, pub_time_to: str | None) -> dict:
    where: dict = {}
    if account_id is not None:
        where["account_id"] = account_id
    if pub_time_from or pub_time_to:
        pub_time_filter: dict[str, str] = {}
        if pub_time_from:
            pub_time_filter["gte"] = _normalize_dt(pub_time_from)
        if pub_time_to:
            pub_time_filter["lte"] = _normalize_dt(pub_time_to)
        where["pub_time"] = pub_time_filter
    return where


def _article_total(client: Client, account_id: int | None, pub_time_from: str | None, pub_time_to: str | None) -> int:
    return len(client.article.find_many(where=_article_where(account_id, pub_time_from, pub_time_to)))


def _article_content(art: Article, format: str | None) -> str | None:
    content_html = _decompress(art.content.normalized_html_zstd) if art.content else None
    if content_html is None:
        return None
    return html_to_markdown.convert(content_html).content if format == "markdown" else content_html


# ── page ──────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def index():
    html = Path("templates_web/index.html").read_text(encoding="utf-8")
    return HTMLResponse(content=html)


@app.get("/llm.txt", response_class=PlainTextResponse)
def llm_txt():
    return Path("templates_web/llm.txt").read_text(encoding="utf-8")


# ── API ───────────────────────────────────────────────────

@app.get("/api/accounts")
def api_accounts():
    client = Client()
    try:
        accounts = client.account.find_many(order_by={"name": "asc"})
        return [
            {
                "id": a.id,
                "name": a.name,
                "username": a.username,
                "biz": a.biz,
                "updated_at": a.updated_at.isoformat(),
            }
            for a in accounts
        ]
    finally:
        Client.close_all()


@app.get("/api/articles")
def api_articles(
    pub_time_from: str | None = Query(None, description="ISO datetime, eg 2025-01-01 or 2025-01-01T00:00:00"),
    pub_time_to: str | None = Query(None, description="ISO datetime"),
    account_id: int | None = Query(None),
    limit: int = Query(50, ge=1),
    offset: int = Query(0, ge=0),
    format: str | None = Query("html", description="'html'（默认）或 'markdown'"),
):
    client = Client()
    try:
        articles = client.article.find_many(
            where=_article_where(account_id, pub_time_from, pub_time_to),
            include={"content": True},
            order_by={"pub_time": "desc"},
            take=limit,
            skip=offset,
        )

        result: list[dict] = []
        for art in articles:
            result.append(_article_json(art, _article_content(art, format)))

        return {
            "items": result,
            "total": _article_total(client, account_id, pub_time_from, pub_time_to),
            "limit": limit,
            "offset": offset,
        }
    finally:
        Client.close_all()


@app.get("/api/article-list")
def api_article_list(
    pub_time_from: str | None = Query(None, description="ISO datetime, eg 2025-01-01 or 2025-01-01T00:00:00"),
    pub_time_to: str | None = Query(None, description="ISO datetime"),
    account_id: int | None = Query(None),
    limit: int = Query(50, ge=1),
    offset: int = Query(0, ge=0),
):
    client = Client()
    try:
        articles = client.article.find_many(
            where=_article_where(account_id, pub_time_from, pub_time_to),
            include={"content": True},
            order_by={"pub_time": "desc"},
            take=limit,
            skip=offset,
        )
        return {
            "items": [_article_list_json(art) for art in articles],
            "total": _article_total(client, account_id, pub_time_from, pub_time_to),
            "limit": limit,
            "offset": offset,
        }
    finally:
        Client.close_all()


@app.get("/api/articles/{article_id}")
def api_article(article_id: int, format: str | None = Query("html", description="'html'（默认）或 'markdown'")):
    client = Client()
    try:
        art = client.article.find_first(where={"id": article_id}, include={"content": True})
        if art is None:
            raise HTTPException(status_code=404, detail="article not found")
        return _article_json(art, _article_content(art, format))
    finally:
        Client.close_all()


@app.post("/api/articles/{article_id}/retry-fetch")
def api_retry_fetch(article_id: int):
    client = Client()
    try:
        article = client.article.find_first(where={"id": article_id})
        if article is None:
            raise HTTPException(status_code=404, detail="article not found")

        now = datetime.now(UTC)
        client.article_content.upsert(
            where={"article_id": article_id},
            update={
                "url": article.url,
                "normalized_html_zstd": None,
                "status": "pending",
                "retry_count": 0,
                "next_retry_at": now,
                "fetch_error": None,
                "fetched_at": None,
                "updated_at": now,
            },
            insert={
                "article_id": article_id,
                "url": article.url,
                "normalized_html_zstd": None,
                "status": "pending",
                "retry_count": 0,
                "next_retry_at": now,
                "fetch_error": None,
                "fetched_at": None,
                "updated_at": now,
            },
        )
        client.article.update(where={"id": article_id}, data={"content_fetched_at": None})
        return {
            "article_id": article_id,
            "status": "pending",
            "retry_count": 0,
            "next_retry_at": now.isoformat(),
        }
    finally:
        Client.close_all()


@app.get("/api/stats")
def api_stats():
    client = Client()
    try:
        db = _db()
        try:
            now = datetime.now(UTC).isoformat()
            total_accounts = db.execute("SELECT COUNT(*) FROM Account").fetchone()[0]
            total_articles = db.execute("SELECT COUNT(*) FROM Article").fetchone()[0]
            fetched = db.execute("SELECT COUNT(*) FROM ArticleContent WHERE status = 'fetched'").fetchone()[0]
            failed = db.execute("SELECT COUNT(*) FROM ArticleContent WHERE status = 'failed'").fetchone()[0]
            give_up = db.execute("SELECT COUNT(*) FROM ArticleContent WHERE status = 'give_up'").fetchone()[0]
            pending = db.execute(
                """
                SELECT COUNT(*)
                FROM Article a
                LEFT JOIN ArticleContent c ON c.article_id = a.id
                WHERE a.content_fetched_at IS NULL
                AND (c.article_id IS NULL OR c.status = 'pending')
                """
            ).fetchone()[0]
            retryable = db.execute(
                """
                SELECT COUNT(*)
                FROM Article a
                LEFT JOIN ArticleContent c ON c.article_id = a.id
                WHERE a.content_fetched_at IS NULL
                AND (
                    c.article_id IS NULL
                    OR c.status = 'pending'
                    OR (c.status = 'failed' AND c.next_retry_at <= ?)
                )
                """,
                [now],
            ).fetchone()[0]
        finally:
            db.close()

        latest = client.article.find_first(order_by={"first_seen_at": "desc"})
        last = client.article.find_first(order_by={"last_seen_at": "desc"})
        return {
            "total_accounts": total_accounts,
            "total_articles": total_articles,
            "fetched_articles": fetched,
            "pending_articles": pending,
            "failed_articles": failed,
            "give_up_articles": give_up,
            "retryable_articles": retryable,
            "latest_article_at": latest.first_seen_at.isoformat() if latest else None,
            "last_collect_at": last.last_seen_at.isoformat() if last else None,
        }
    finally:
        Client.close_all()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8199)
