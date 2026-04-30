import sqlite3
from datetime import datetime
from pathlib import Path

import html_to_markdown
from compression import zstd
from dclassql import Client
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, PlainTextResponse

from db_model import Account, Article, ArticleContent

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
        "content": content,
    }


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

        articles = client.article.find_many(
            where=where,
            include={"content": ArticleContent},
            order_by={"pub_time": "desc"},
            take=limit,
            skip=offset,
        )

        result: list[dict] = []
        for art in articles:
            content_html = _decompress(art.content.normalized_html_zstd) if art.content else None
            content = None
            if content_html:
                content = html_to_markdown.convert(content_html).content if format == "markdown" else content_html
            result.append(_article_json(art, content))

        # get total count with correct filters
        db = _db()
        try:
            count_sql = "SELECT COUNT(*) FROM Article WHERE 1=1"
            count_params: list = []
            if account_id is not None:
                count_sql += " AND account_id = ?"
                count_params.append(account_id)
            if pub_time_from:
                count_sql += " AND pub_time >= ?"
                count_params.append(_normalize_dt(pub_time_from))
            if pub_time_to:
                count_sql += " AND pub_time <= ?"
                count_params.append(_normalize_dt(pub_time_to))
            total = db.execute(count_sql, count_params).fetchone()[0]
        finally:
            db.close()

        return {"items": result, "total": total, "limit": limit, "offset": offset}
    finally:
        Client.close_all()


@app.get("/api/stats")
def api_stats():
    client = Client()
    try:
        db = _db()
        try:
            total_accounts = db.execute("SELECT COUNT(*) FROM Account").fetchone()[0]
            total_articles = db.execute("SELECT COUNT(*) FROM Article").fetchone()[0]
            fetched = db.execute("SELECT COUNT(*) FROM ArticleContent WHERE status='fetched'").fetchone()[0]
        finally:
            db.close()

        latest = client.article.find_first(order_by={"first_seen_at": "desc"})
        last = client.article.find_first(order_by={"last_seen_at": "desc"})
        return {
            "total_accounts": total_accounts,
            "total_articles": total_articles,
            "fetched_articles": fetched,
            "latest_article_at": latest.first_seen_at.isoformat() if latest else None,
            "last_collect_at": last.last_seen_at.isoformat() if last else None,
        }
    finally:
        Client.close_all()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8199)
