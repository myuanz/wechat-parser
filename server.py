import os
import sqlite3
import json
from datetime import time as dt_time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import html_to_markdown
from compression import zstd
from dclassql import Client
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, PlainTextResponse

from db_model import Article, DB_PATH
from zhihu_sync import read_sync_result, sync_single_author, write_sync_request

app = FastAPI(title="wechat-parser")
DEFAULT_FOLLOWING_SNAPSHOT = Path(__file__).with_name("dumps") / "zhihu_following_latest.json"
DEFAULT_ZHIHU_TODAY_DIR = Path(__file__).with_name("dumps") / "zhihu_today"


def _db():
    return sqlite3.connect(DB_PATH, timeout=30)


def _decompress(html_zstd: bytes | None) -> str | None:
    if html_zstd is None:
        return None
    return zstd.decompress(html_zstd).decode("utf-8")


def _normalize_dt(value: str) -> str:
    return datetime.fromisoformat(value).isoformat()


def _load_following_snapshot() -> list[dict[str, str]]:
    if not DEFAULT_FOLLOWING_SNAPSHOT.exists():
        return []
    payload = json.loads(DEFAULT_FOLLOWING_SNAPSHOT.read_text(encoding="utf-8"))
    users = payload.get("users")
    if not isinstance(users, list):
        return []
    result: list[dict[str, str]] = []
    for item in users:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        url = str(item.get("url") or "")
        slug = str(item.get("slug") or "")
        headline = str(item.get("headline") or "")
        avatar = str(item.get("avatar") or "")
        if not name or not url or not slug:
            continue
        result.append(
            {
                "slug": slug,
                "name": name,
                "profile_url": url,
                "headline": headline,
                "avatar_url": avatar,
            }
        )
    return result


def _zhihu_profile_slug(profile_url: str) -> str:
    parts = [part for part in urlparse(profile_url).path.split("/") if part]
    if len(parts) >= 2 and parts[0] in {"people", "org"}:
        return parts[1]
    raise HTTPException(status_code=400, detail=f"invalid zhihu profile url: {profile_url}")


def _zhihu_today_output_path(profile_url: str) -> Path:
    slug = _zhihu_profile_slug(profile_url)
    DEFAULT_ZHIHU_TODAY_DIR.mkdir(parents=True, exist_ok=True)
    return DEFAULT_ZHIHU_TODAY_DIR / f"{slug}.json"


def _read_zhihu_today_output(profile_url: str) -> dict[str, object]:
    output_path = _zhihu_today_output_path(profile_url)
    if not output_path.exists():
        raise HTTPException(status_code=404, detail="zhihu today output not found")
    return json.loads(output_path.read_text(encoding="utf-8"))


def _zhihu_latest_publish_sort_key(item: dict[str, object]) -> tuple[int, float]:
    today_latest_publish_time = str(item.get("today_latest_publish_time") or "")
    if today_latest_publish_time:
        return (0, -datetime.fromisoformat(today_latest_publish_time).timestamp())
    profile_url = str(item.get("profile_url") or "")
    latest_publish = ""
    if profile_url:
        try:
            payload = _read_zhihu_today_output(profile_url)
            items = payload.get("items")
            if isinstance(items, list) and items:
                first = items[0]
                if isinstance(first, dict):
                    latest_publish = str(first.get("publish_time_iso") or "")
        except HTTPException:
            latest_publish = ""
    if latest_publish:
        return (0, -datetime.fromisoformat(latest_publish).timestamp())
    last_seen_pub_time = str(item.get("last_seen_pub_time") or "")
    if last_seen_pub_time:
        return (1, -datetime.fromisoformat(last_seen_pub_time).timestamp())
    return (2, 0.0)


def _row_to_zhihu_item(row: tuple[object, ...]) -> dict[str, object]:
    return {
        "content_type": row[0],
        "publish_time_iso": row[1],
        "updated_time_iso": row[2],
        "url": row[3],
        "title": row[4],
        "content_html": row[5] or "",
        "content_text": row[6] or "",
        "author_name": row[7] or "",
    }


def _db_zhihu_contents_by_slug(slug: str) -> list[dict[str, object]]:
    db = _db()
    try:
        rows = db.execute(
            """
            SELECT
                'answer' AS content_type,
                a.created_time AS publish_time_iso,
                a.updated_time AS updated_time_iso,
                a.answer_url AS url,
                a.question_title AS title,
                a.content_html AS content_html,
                a.content_text AS content_text,
                au.name AS author_name
            FROM ZhihuAnswer a
            JOIN ZhihuAuthor au ON au.id = a.author_id
            WHERE au.slug = ?
            UNION ALL
            SELECT
                'pin' AS content_type,
                p.created_time AS publish_time_iso,
                p.updated_time AS updated_time_iso,
                p.pin_url AS url,
                CASE
                    WHEN COALESCE(NULLIF(p.excerpt_title, ''), '') != '' THEN p.excerpt_title
                    ELSE '想法'
                END AS title,
                p.content_html AS content_html,
                p.content_text AS content_text,
                au.name AS author_name
            FROM ZhihuPin p
            JOIN ZhihuAuthor au ON au.id = p.author_id
            WHERE au.slug = ?
            ORDER BY publish_time_iso DESC
            """,
            [slug, slug],
        ).fetchall()
        return [_row_to_zhihu_item(row) for row in rows]
    finally:
        db.close()


def _db_zhihu_today_latest_map() -> dict[str, str]:
    db = _db()
    try:
        rows = db.execute(
            """
            SELECT slug, MAX(publish_time_iso)
            FROM (
                SELECT au.slug AS slug, a.created_time AS publish_time_iso
                FROM ZhihuAnswer a
                JOIN ZhihuAuthor au ON au.id = a.author_id
                WHERE date(a.first_seen_at) = date('now', 'localtime')
                UNION ALL
                SELECT au.slug AS slug, p.created_time AS publish_time_iso
                FROM ZhihuPin p
                JOIN ZhihuAuthor au ON au.id = p.author_id
                WHERE date(p.first_seen_at) = date('now', 'localtime')
            )
            GROUP BY slug
            """
        ).fetchall()
        return {str(row[0]): str(row[1]) for row in rows if row[0] and row[1]}
    finally:
        db.close()


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


@app.get("/zhihu.txt", response_class=PlainTextResponse)
def zhihu_txt():
    return Path("templates_web/zhihu.txt").read_text(encoding="utf-8")


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


@app.get("/api/zhihu/stats")
def api_zhihu_stats():
    snapshot = _load_following_snapshot()
    db = _db()
    try:
        total_following = len(snapshot) if snapshot else db.execute("SELECT COUNT(*) FROM ZhihuAuthor WHERE is_following = 1").fetchone()[0]
        answer_authors = db.execute("SELECT COUNT(DISTINCT author_id) FROM ZhihuAnswer").fetchone()[0]
        total_contents = db.execute("SELECT COUNT(*) FROM ZhihuAnswer").fetchone()[0]
        today_new = db.execute(
            """
            SELECT COUNT(*)
            FROM ZhihuAnswer
            WHERE date(first_seen_at) = date('now', 'localtime')
            """
        ).fetchone()[0]
        last_check = db.execute(
            """
            SELECT MAX(last_seen_at)
            FROM (
                SELECT last_seen_at FROM ZhihuAuthor
                UNION ALL
                SELECT last_seen_at FROM ZhihuAnswer
                UNION ALL
                SELECT last_seen_at FROM ZhihuPin
            )
            """
        ).fetchone()[0]
        return {
            "total_following": total_following,
            "answer_authors": answer_authors,
            "total_contents": total_contents,
            "today_new": today_new,
            "last_check": last_check,
        }
    finally:
        db.close()


@app.get("/api/zhihu/today")
def api_zhihu_today():
    db = _db()
    try:
        rows = db.execute(
            """
            SELECT
                'answer' AS kind,
                a.answer_id AS content_id,
                au.name AS author_name,
                au.profile_url AS author_url,
                a.question_title AS title,
                a.content_html AS content,
                a.content_text AS content_text,
                a.answer_url AS url,
                a.created_time AS created_time,
                a.updated_time AS updated_time,
                a.first_seen_at AS first_seen_at
            FROM ZhihuAnswer a
            JOIN ZhihuAuthor au ON au.id = a.author_id
            WHERE date(a.first_seen_at) = date('now', 'localtime')
            ORDER BY first_seen_at DESC
            """
        ).fetchall()
        return [
            {
                "kind": row[0],
                "content_id": row[1],
                "author_name": row[2],
                "author_url": row[3],
                "title": row[4],
                "content": row[5],
                "content_html": row[5],
                "content_text": row[6],
                "url": row[7],
                "created_time": row[8],
                "updated_time": row[9],
                "first_seen_at": row[10],
            }
            for row in rows
        ]
    finally:
        db.close()


@app.get("/api/zhihu/following-contents")
def api_zhihu_following_contents(slug: str = Query(...)):
    db = _db()
    try:
        row = db.execute(
            """
            SELECT profile_url, name
            FROM ZhihuAuthor
            WHERE slug = ?
            LIMIT 1
            """,
            [slug],
        ).fetchone()
    finally:
        db.close()
    if row is None:
        snapshot = _load_following_snapshot()
        for item in snapshot:
            if item["slug"] == slug:
                row = (item["profile_url"], item["name"])
                break
    if row is None:
        raise HTTPException(status_code=404, detail="zhihu following not found")

    return {
        "fetched_at": None,
        "items": _db_zhihu_contents_by_slug(slug),
    }


@app.get("/api/zhihu/following")
def api_zhihu_following():
    snapshot = _load_following_snapshot()
    today_latest_map = _db_zhihu_today_latest_map()
    db = _db()
    try:
        rows = db.execute(
            """
            SELECT slug, name, profile_url, headline, avatar_url, is_following, last_seen_content_id, last_seen_pub_time
            FROM ZhihuAuthor
            """
        ).fetchall()
        by_slug = {
            row[0]: {
                "slug": row[0],
                "name": row[1],
                "profile_url": row[2],
                "headline": row[3],
                "avatar_url": row[4],
                "is_following": bool(row[5]),
                "last_seen_content_id": row[6],
                "last_seen_pub_time": row[7],
            }
            for row in rows
            if row[0]
        }
    finally:
        db.close()

    if snapshot:
        result = []
        for user in snapshot:
            item = by_slug.get(user["slug"], {})
            merged = {**item, **user, "is_following": True}
            merged["today_latest_publish_time"] = today_latest_map.get(user["slug"])
            result.append(merged)
        return sorted(result, key=_zhihu_latest_publish_sort_key)
    result = []
    for item in by_slug.values():
        item = dict(item)
        item["today_latest_publish_time"] = today_latest_map.get(item["slug"])
        result.append(item)
    return sorted(result, key=_zhihu_latest_publish_sort_key)


@app.post("/api/zhihu/following-refresh")
def api_zhihu_following_refresh(slug: str = Query(...)):
    db = _db()
    try:
        row = db.execute(
            """
            SELECT profile_url
            FROM ZhihuAuthor
            WHERE slug = ?
            LIMIT 1
            """,
            [slug],
        ).fetchone()
    finally:
        db.close()
    if row is None:
        snapshot = _load_following_snapshot()
        for item in snapshot:
            if item["slug"] == slug:
                row = (item["profile_url"],)
                break
    if row is None:
        raise HTTPException(status_code=404, detail="zhihu following not found")
    profile_url = str(row[0])
    try:
        result = sync_single_author(slug, profile_url)
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error
    return {
        "ok": True,
        "slug": slug,
        "profile_url": profile_url,
        "new_items_count": int(result.get("new_answers", 0)) + int(result.get("new_pins", 0)),
        "new_answers": int(result.get("new_answers", 0)),
        "new_pins": int(result.get("new_pins", 0)),
    }


@app.post("/api/zhihu/refresh-following")
def api_zhihu_refresh_following():
    payload = write_sync_request("refresh-following")
    return {"ok": True, "queued": True, **payload}


@app.post("/api/zhihu/check-new")
def api_zhihu_check_new():
    now_local = datetime.now().astimezone().time()
    if now_local < dt_time(8, 0) or now_local >= dt_time(23, 0):
        return {"ok": True, "skipped": True}
    payload = write_sync_request("check-new")
    return {"ok": True, "queued": True, **payload}


@app.get("/api/zhihu/sync-status")
def api_zhihu_sync_status():
    result = read_sync_result()
    if result is None:
        return {"ok": True, "status": "idle"}
    return {"ok": True, **result}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8199)
