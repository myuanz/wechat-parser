import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import html_to_markdown
from compression import zstd
from dclassql import Client
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, PlainTextResponse

from db_model import Article, DB_PATH
from zhihu_sync import (
    DEFAULT_REQUEST_PROFILE_URL,
    cleanup_stale_running_zhihu_tasks,
    create_zhihu_task,
    refresh_following,
    run_today_updates,
    sync_zhihu,
    update_zhihu_task,
)

app = FastAPI(title="wechat-parser")


def _db():
    return sqlite3.connect(DB_PATH, timeout=30)


def _decompress(html_zstd: bytes | None) -> str | None:
    if html_zstd is None:
        return None
    return zstd.decompress(html_zstd).decode("utf-8")


def _normalize_dt(value: str) -> str:
    return datetime.fromisoformat(value).isoformat()


def _zhihu_task_payload_json(payload_json: str | None) -> dict[str, object]:
    if not payload_json:
        return {}
    payload = json.loads(payload_json)
    return payload if isinstance(payload, dict) else {}


def _zhihu_task_to_dict(task) -> dict[str, object]:
    payload = {
        "id": task.id,
        "task_type": task.task_type,
        "profile_url": task.profile_url,
        "status": task.status,
        "requested_at": task.requested_at.isoformat(),
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "finished_at": task.finished_at.isoformat() if task.finished_at else None,
        "error": task.error,
    }
    payload.update(_zhihu_task_payload_json(task.request_payload_json))
    payload.update(_zhihu_task_payload_json(task.result_payload_json))
    return payload


def _latest_zhihu_task_by_type(client: Client, task_type: str) -> dict[str, object] | None:
    task = client.zhihu_task.find_first(where={"task_type": task_type}, order_by={"id": "desc"})
    return None if task is None else _zhihu_task_to_dict(task)


def _zhihu_task_sort_key(task: dict[str, object] | None) -> tuple[float, int]:
    if task is None:
        return (0.0, 0)
    for key in ("started_at", "requested_at", "finished_at"):
        value = task.get(key)
        if isinstance(value, str) and value:
            return (datetime.fromisoformat(value).timestamp(), int(task.get("id") or 0))
    return (0.0, int(task.get("id") or 0))


def _latest_zhihu_check_task(client: Client) -> dict[str, object] | None:
    tasks = [
        _latest_zhihu_task_by_type(client, "check_new"),
        _latest_zhihu_task_by_type(client, "auto_check_new"),
    ]
    return max(tasks, key=_zhihu_task_sort_key)


def _zhihu_latest_publish_sort_key(item: dict[str, object]) -> tuple[int, float]:
    today_latest_publish_time = str(item.get("today_latest_publish_time") or "")
    if today_latest_publish_time:
        return (0, -datetime.fromisoformat(today_latest_publish_time).timestamp())
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
            UNION ALL
            SELECT
                'article' AS content_type,
                ar.created_time AS publish_time_iso,
                ar.updated_time AS updated_time_iso,
                ar.article_url AS url,
                ar.title AS title,
                ar.content_html AS content_html,
                ar.content_text AS content_text,
                au.name AS author_name
            FROM ZhihuArticle ar
            JOIN ZhihuAuthor au ON au.id = ar.author_id
            WHERE au.slug = ?
            ORDER BY publish_time_iso DESC
            """,
            [slug, slug, slug],
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
                UNION ALL
                SELECT au.slug AS slug, ar.created_time AS publish_time_iso
                FROM ZhihuArticle ar
                JOIN ZhihuAuthor au ON au.id = ar.author_id
                WHERE date(ar.first_seen_at) = date('now', 'localtime')
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
    cleanup_stale_running_zhihu_tasks(task_types=("check_new", "auto_check_new"))
    db = _db()
    client = Client()
    try:
        total_following = db.execute("SELECT COUNT(*) FROM ZhihuAuthor WHERE is_following = 1").fetchone()[0]
        answer_authors = db.execute("SELECT COUNT(DISTINCT author_id) FROM ZhihuAnswer").fetchone()[0]
        article_authors = db.execute("SELECT COUNT(DISTINCT author_id) FROM ZhihuArticle").fetchone()[0]
        pin_authors = db.execute("SELECT COUNT(DISTINCT author_id) FROM ZhihuPin").fetchone()[0]
        total_contents = db.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM ZhihuAnswer) +
                (SELECT COUNT(*) FROM ZhihuArticle) +
                (SELECT COUNT(*) FROM ZhihuPin)
            """
        ).fetchone()[0]
        today_new = db.execute(
            """
            SELECT COUNT(*)
            FROM (
                SELECT first_seen_at FROM ZhihuAnswer
                UNION ALL
                SELECT first_seen_at FROM ZhihuArticle
                UNION ALL
                SELECT first_seen_at FROM ZhihuPin
            )
            WHERE date(first_seen_at) = date('now', 'localtime')
            """
        ).fetchone()[0]
        last_check_task = _latest_zhihu_check_task(client)
        last_check = None
        if last_check_task is not None:
            last_check = (
                last_check_task.get("started_at")
                or last_check_task.get("requested_at")
                or last_check_task.get("finished_at")
            )
        return {
            "total_following": total_following,
            "answer_authors": answer_authors,
            "article_authors": article_authors,
            "pin_authors": pin_authors,
            "total_contents": total_contents,
            "today_new": today_new,
            "last_check": last_check,
            "last_check_task": last_check_task,
        }
    finally:
        db.close()
        Client.close_all()


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
            UNION ALL
            SELECT
                'article' AS kind,
                ar.article_id AS content_id,
                au.name AS author_name,
                au.profile_url AS author_url,
                ar.title AS title,
                ar.content_html AS content,
                ar.content_text AS content_text,
                ar.article_url AS url,
                ar.created_time AS created_time,
                ar.updated_time AS updated_time,
                ar.first_seen_at AS first_seen_at
            FROM ZhihuArticle ar
            JOIN ZhihuAuthor au ON au.id = ar.author_id
            WHERE date(ar.first_seen_at) = date('now', 'localtime')
            UNION ALL
            SELECT
                'pin' AS kind,
                p.pin_id AS content_id,
                au.name AS author_name,
                au.profile_url AS author_url,
                CASE
                    WHEN COALESCE(NULLIF(p.excerpt_title, ''), '') != '' THEN p.excerpt_title
                    ELSE '想法'
                END AS title,
                p.content_html AS content,
                p.content_text AS content_text,
                p.pin_url AS url,
                p.created_time AS created_time,
                p.updated_time AS updated_time,
                p.first_seen_at AS first_seen_at
            FROM ZhihuPin p
            JOIN ZhihuAuthor au ON au.id = p.author_id
            WHERE date(p.first_seen_at) = date('now', 'localtime')
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
        raise HTTPException(status_code=404, detail="zhihu following not found")

    return {
        "fetched_at": None,
        "items": _db_zhihu_contents_by_slug(slug),
    }


@app.get("/api/zhihu/following")
def api_zhihu_following():
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

    active_by_slug = {slug: item for slug, item in by_slug.items() if item["is_following"]}
    result = []
    for item in active_by_slug.values():
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
        raise HTTPException(status_code=404, detail="zhihu following not found")
    profile_url = str(row[0])
    task = create_zhihu_task("refresh_profile", profile_url=profile_url, status="running", started_at=datetime.now().astimezone())
    try:
        result = run_today_updates(profile_url)
        update_zhihu_task(
            int(task["id"]),
            status="done",
            finished_at=datetime.now().astimezone(),
            result_payload=result,
        )
    except Exception as error:
        update_zhihu_task(int(task["id"]), status="failed", finished_at=datetime.now().astimezone(), error=str(error))
        raise HTTPException(status_code=500, detail=str(error)) from error
    return {
        "ok": True,
        "slug": slug,
        "profile_url": profile_url,
        "new_items_count": int(result.get("new_answers", 0)) + int(result.get("new_articles", 0)) + int(result.get("new_pins", 0)),
        "new_answers": int(result.get("new_answers", 0)),
        "new_articles": int(result.get("new_articles", 0)),
        "new_pins": int(result.get("new_pins", 0)),
    }


@app.post("/api/zhihu/refresh-following")
def api_zhihu_refresh_following():
    task = create_zhihu_task("refresh_following", status="running", started_at=datetime.now().astimezone())
    try:
        result = refresh_following()
        update_zhihu_task(
            int(task["id"]),
            status="done",
            finished_at=datetime.now().astimezone(),
            result_payload=result,
        )
    except Exception as error:
        update_zhihu_task(int(task["id"]), status="failed", finished_at=datetime.now().astimezone(), error=str(error))
        raise HTTPException(status_code=500, detail=str(error)) from error
    return {"ok": True, **result}


@app.post("/api/zhihu/check-new")
def api_zhihu_check_new():
    cleanup_stale_running_zhihu_tasks(task_types=("check_new", "auto_check_new"))
    task = create_zhihu_task("check_new", profile_url=DEFAULT_REQUEST_PROFILE_URL, status="running", started_at=datetime.now().astimezone())
    try:
        result = sync_zhihu(profile_url=DEFAULT_REQUEST_PROFILE_URL)
        update_zhihu_task(
            int(task["id"]),
            status="done",
            finished_at=datetime.now().astimezone(),
            result_payload=result,
        )
    except Exception as error:
        update_zhihu_task(int(task["id"]), status="failed", finished_at=datetime.now().astimezone(), error=str(error))
        raise HTTPException(status_code=500, detail=str(error)) from error
    return {
        "ok": True,
        "new_items_count": int(result.get("new_answers", 0)) + int(result.get("new_articles", 0)) + int(result.get("new_pins", 0)),
        "new_answers": int(result.get("new_answers", 0)),
        "new_articles": int(result.get("new_articles", 0)),
        "new_pins": int(result.get("new_pins", 0)),
        **result,
    }


@app.get("/api/zhihu/sync-status")
def api_zhihu_sync_status():
    cleanup_stale_running_zhihu_tasks(task_types=("check_new", "auto_check_new"))
    client = Client()
    try:
        latest_task = client.zhihu_task.find_first(order_by={"id": "desc"})
        if latest_task is None:
            return {"ok": True, "status": "idle"}
        return {
            "ok": True,
            "status": latest_task.status,
            "latest_task": _zhihu_task_to_dict(latest_task),
            "latest_check_new": _latest_zhihu_task_by_type(client, "check_new"),
            "latest_auto_check_new": _latest_zhihu_task_by_type(client, "auto_check_new"),
            "latest_check_task": _latest_zhihu_check_task(client),
            "latest_refresh_following": _latest_zhihu_task_by_type(client, "refresh_following"),
            "latest_refresh_profile": _latest_zhihu_task_by_type(client, "refresh_profile"),
        }
    finally:
        Client.close_all()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8199)
