import json
import random
import argparse
import sys
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from cloakbrowser import launch_persistent_context
from dclassql import Client
from x11_wechat import DEFAULT_XAUTHORITY, DEFAULT_X_DISPLAY
from zhihu_following_collect import collect_all_following, is_login_page, wait_for_login
from zhihu_profile_today_updates import collect_today_updates


DEFAULT_PROFILE_DIR = Path(__file__).with_name("browser_profiles") / "zhihu"
DEFAULT_SIGNIN_URL = "https://www.zhihu.com/signin"
DEFAULT_PROFILE_URL = "https://www.zhihu.com/people/bu-ye-cheng-76"
DEFAULT_RESULT_PATH = Path(__file__).with_name("dumps") / "zhihu_sync_result.json"
DEFAULT_REQUEST_PATH = Path(__file__).with_name("dumps") / "zhihu_sync_request.json"
DEFAULT_FOLLOWING_DEBUG_PATH = Path(__file__).with_name("dumps") / "zhihu_following_latest.json"
DEFAULT_LIMIT = 20
DEFAULT_CONTENT_LIMIT = 1
DEFAULT_REQUEST_PROFILE_URL = "https://www.zhihu.com/people/bu-ye-cheng-76"
INVALID_AUTHOR_SLUGS = {
    "following",
    "answers",
    "columns",
    "collections",
    "followers",
    "questions",
    "pins",
    "topics",
    "lineComments",
}
INVALID_AUTHOR_NAMES = {
    "动态",
    "回答",
    "想法",
    "文章",
    "关注者",
}


def now() -> datetime:
    return datetime.now(UTC)


def today_start_utc() -> datetime:
    now_local = datetime.now().astimezone()
    return now_local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


def to_dt(value: object) -> datetime | None:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, UTC)
    if isinstance(value, str) and value:
        return datetime.fromisoformat(value)
    return None


def sleep_random(min_seconds: float = 2, max_seconds: float = 8) -> None:
    time.sleep(random.uniform(min_seconds, max_seconds))


def profile_slug(profile_url: str) -> str:
    parts = [part for part in profile_url.rstrip("/").split("/") if part]
    return parts[-1]


def run_today_updates(profile_url: str, profile_dir: Path = DEFAULT_PROFILE_DIR) -> dict[str, object]:
    payload = collect_today_updates(
        profile_url=profile_url,
        profile_dir=profile_dir,
        headless=True,
    )
    import_stats = import_today_payload(profile_url, payload)
    return {
        "fetched_at": payload.get("fetched_at"),
        "total_count": payload.get("total_count"),
        **import_stats,
    }


def content_id_from_item(item: dict[str, object]) -> str:
    content_id = str(item.get("content_id") or "")
    if content_id:
        return content_id
    content_type = str(item.get("content_type") or "")
    url = str(item.get("url") or "")
    if not url:
        return ""
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if content_type == "pin" and parts and parts[0] == "pin":
        return parts[-1]
    if content_type == "article" and parts:
        return parts[-1]
    if content_type == "answer" and "answer" in parts:
        return parts[-1]
    return ""


def should_skip_following_user(user: dict[str, str]) -> bool:
    slug = user["slug"]
    name = user["name"].strip()
    if slug in INVALID_AUTHOR_SLUGS:
        return True
    if name in INVALID_AUTHOR_NAMES:
        return True
    if name.startswith("关注了") or name.startswith("关注的") or name.startswith("关注者"):
        return True
    return False


def fetch_following(page, profile_url: str, signin_url: str, login_wait: int) -> list[dict[str, str]]:
    wait_for_login(page, signin_url, login_wait)
    page.goto(profile_url.rstrip("/") + "/following", wait_until="domcontentloaded", timeout=60_000)
    if is_login_page(page):
        raise RuntimeError("登录后仍然被重定向到登录页，请确认账号已完成登录")
    page.wait_for_timeout(2000)
    users = collect_all_following(page)
    return [user for user in users if not should_skip_following_user(user)]


def save_following_debug_dump(users: list[dict[str, str]]) -> None:
    DEFAULT_FOLLOWING_DEBUG_PATH.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_FOLLOWING_DEBUG_PATH.write_text(
        json.dumps(
            {
                "updated_at": datetime.now().astimezone().isoformat(),
                "users": users,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def html_to_text(html: str) -> str:
    text = html.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    for tag in ("</p>", "</div>", "</li>", "</h1>", "</h2>", "</h3>", "</h4>", "</h5>", "</h6>"):
        text = text.replace(tag, "\n")
    import re
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&nbsp;", " ")
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&").replace("&quot;", '"')
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def extract_answer_records(payload: object) -> list[dict[str, object]]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    answers: list[dict[str, object]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        question = item.get("question") if isinstance(item.get("question"), dict) else {}
        author = item.get("author") if isinstance(item.get("author"), dict) else {}
        answer_id = item.get("id")
        created_time = to_dt(item.get("created_time"))
        updated_time = to_dt(item.get("updated_time"))
        answers.append(
            {
                "answer_id": str(answer_id) if answer_id is not None else "",
                "question_id": str(question.get("id", "")),
                "question_title": str(question.get("title", "")),
                "question_api_url": str(question.get("url", "")),
                "answer_api_url": str(item.get("url", "")),
                "answer_url": f"https://www.zhihu.com/question/{question.get('id')}/answer/{answer_id}" if question.get("id") and answer_id else "",
                "excerpt": str(item.get("excerpt") or item.get("excerpt_new") or item.get("content") or ""),
                "created_time": created_time,
                "updated_time": updated_time,
                "voteup_count": int(item.get("voteup_count") or 0),
                "comment_count": int(item.get("comment_count") or 0),
                "thanks_count": item.get("thanks_count"),
                "author_id": str(author.get("id", "")),
                "author_name": str(author.get("name", "")),
                "author_url_token": str(author.get("url_token", "")),
            }
        )
    return answers


def fetch_answer_detail(page, answer_id: str) -> dict[str, object]:
    return page.evaluate(
        """
        async (answerId) => {
          const include = [
            "content",
            "voteup_count",
            "comment_count",
            "thanks_count",
            "created_time",
            "updated_time",
            "excerpt",
            "author",
            "question"
          ].join(",");
          const url = `https://www.zhihu.com/api/v4/answers/${answerId}?include=${include}`;
          const response = await fetch(url, { credentials: "include", headers: { "accept": "application/json, text/plain, */*" } });
          if (!response.ok) throw new Error(`抓取回答详情失败: ${response.status} ${response.statusText}`);
          return await response.json();
        }
        """,
        answer_id,
    )


def fetch_answer_candidates(page, slug: str) -> list[dict[str, object]]:
    payload = page.evaluate(
        """
        async ({ slug, limit }) => {
          const include = [
            "data[*].is_normal",
            "admin_closed_comment",
            "reward_info",
            "is_collapsed",
            "annotation_action",
            "annotation_detail",
            "collapse_reason",
            "collapsed_by",
            "suggest_edit",
            "comment_count",
            "can_comment",
            "content",
            "editable_content",
            "attachment",
            "voteup_count",
            "reshipment_settings",
            "comment_permission",
            "created_time",
            "updated_time",
            "review_info",
            "excerpt",
            "paid_info",
            "reaction_instruction",
            "is_labeled",
            "label_info",
            "relationship.is_authorized",
            "voting",
            "is_author",
            "is_thanked",
            "is_nothelp",
            "reaction",
            "vessay_info",
            "data[*].author.badge[?(type=best_answerer)].topics",
            "data[*].author.kvip_info",
            "data[*].author.vip_info",
            "data[*].question.has_publishing_draft",
            "relationship"
          ].join(",");
          const url = `https://www.zhihu.com/api/v4/members/${slug}/answers?include=${include}&offset=0&limit=${limit}&sort_by=created&ws_qiangzhisafe=0`;
          const response = await fetch(url, {
            credentials: "include",
            headers: { "accept": "application/json, text/plain, */*" }
          });
          if (!response.ok) throw new Error(`抓取回答列表失败: ${response.status} ${response.statusText}`);
          return await response.json();
        }
        """,
        {"slug": slug, "limit": DEFAULT_LIMIT},
    )
    return extract_answer_records(payload)[:DEFAULT_LIMIT]


def answer_created_dt(item: dict[str, object]) -> datetime | None:
    created_time = item.get("created_time")
    if isinstance(created_time, datetime):
        return created_time
    return to_dt(created_time)


def fetch_pin_candidates(page, slug: str) -> list[dict[str, object]]:
    payload = page.evaluate(
        """
        async ({ slug, limit }) => {
          const url = `https://www.zhihu.com/api/v4/v2/members/${slug}/pins?offset=0&limit=${limit}`;
          const response = await fetch(url, { credentials: "include", headers: { "accept": "application/json, text/plain, */*" } });
          if (!response.ok) throw new Error(`抓取想法列表失败: ${response.status} ${response.statusText}`);
          return await response.json();
        }
        """,
        {"slug": slug, "limit": DEFAULT_LIMIT},
    )
    data = payload.get("data") if isinstance(payload, dict) else None
    return data[:DEFAULT_LIMIT] if isinstance(data, list) else []


def sync_following(client: Client, page, profile_url: str, signin_url: str, login_wait: int) -> list[dict[str, str]]:
    users = fetch_following(page, profile_url, signin_url, login_wait)
    save_following_debug_dump(users)
    now_value = now()
    authors = client.zhihu_author.find_many()
    existing_by_url = {author.profile_url: author for author in authors}
    existing_by_slug = {author.slug: author for author in authors}
    seen: set[str] = set()
    for user in users:
        seen.add(user["url"])
        author = existing_by_url.get(user["url"]) or existing_by_slug.get(user["slug"])
        if author is None:
            client.zhihu_author.upsert(
                where={"slug": user["slug"]},
                update={
                    "profile_url": user["url"],
                    "name": user["name"],
                    "headline": user["headline"],
                    "avatar_url": user["avatar"],
                    "is_following": True,
                    "last_seen_at": now_value,
                    "updated_at": now_value,
                },
                insert={
                    "slug": user["slug"],
                    "name": user["name"],
                    "profile_url": user["url"],
                    "headline": user["headline"],
                    "avatar_url": user["avatar"],
                    "is_following": True,
                    "last_seen_content_id": None,
                    "last_seen_pub_time": None,
                    "first_seen_at": now_value,
                    "last_seen_at": now_value,
                    "created_at": now_value,
                    "updated_at": now_value,
                },
            )
            continue
        client.zhihu_author.update(
            where={"id": author.id},
            data={
                "slug": user["slug"],
                "profile_url": user["url"],
                "name": user["name"],
                "headline": user["headline"],
                "avatar_url": user["avatar"],
                "is_following": True,
                "last_seen_at": now_value,
                "updated_at": now_value,
            },
        )
    for author in authors:
        if author.profile_url not in seen and author.is_following:
            client.zhihu_author.update(where={"id": author.id}, data={"is_following": False, "updated_at": now_value})
    return users


def answer_text(content_html: str, excerpt: str) -> str:
    if content_html:
        return html_to_text(content_html)
    return excerpt


def fetch_answer_detail_or_none(page, answer_id: str) -> dict[str, object] | None:
    try:
        payload = fetch_answer_detail(page, answer_id)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def fetch_complete_answer_detail(page, answer_id: str, retries: int = 3) -> dict[str, object] | None:
    for _ in range(retries):
        detail = fetch_answer_detail_or_none(page, answer_id)
        if detail is None:
            sleep_random(1, 2)
            continue
        content_html = str(detail.get("content") or "").strip()
        if content_html:
            return detail
        sleep_random(1, 2)
    return None


def detail_author_slug(detail: dict[str, object], item: dict[str, object]) -> str:
    author = detail.get("author")
    if isinstance(author, dict) and isinstance(author.get("url_token"), str):
        return str(author.get("url_token") or "")
    author_url_token = item.get("author_url_token")
    if isinstance(author_url_token, str):
        return author_url_token
    return ""


def ensure_author_record(client: Client, slug: str, name: str, now_value: datetime):
    existing = client.zhihu_author.find_first(where={"slug": slug})
    profile_url = f"https://www.zhihu.com/people/{slug}"
    if existing is not None:
        if existing.profile_url != profile_url or existing.name != name:
            client.zhihu_author.update(
                where={"id": existing.id},
                data={
                    "profile_url": profile_url,
                    "name": name or existing.name,
                    "updated_at": now_value,
                },
            )
            return client.zhihu_author.find_first(where={"id": existing.id})
        return existing
    client.zhihu_author.upsert(
        where={"slug": slug},
        update={"profile_url": profile_url, "name": name or slug, "updated_at": now_value},
        insert={
            "slug": slug,
            "name": name or slug,
            "profile_url": profile_url,
            "headline": "",
            "avatar_url": "",
            "is_following": False,
            "last_seen_content_id": None,
            "last_seen_pub_time": None,
            "first_seen_at": now_value,
            "last_seen_at": now_value,
            "created_at": now_value,
            "updated_at": now_value,
        },
    )
    return client.zhihu_author.find_first(where={"slug": slug})


def upsert_article_from_today_item(client: Client, author, item: dict[str, object], now_value: datetime) -> bool:
    article_id = content_id_from_item(item)
    if not article_id:
        return False
    created_dt = to_dt(item.get("publish_time_iso"))
    updated_dt = to_dt(item.get("updated_time_iso")) or created_dt
    if created_dt is None or updated_dt is None:
        raise RuntimeError(f"文章时间字段缺失: {article_id}")
    content_html = str(item.get("content_html") or "")
    content_text = str(item.get("content_text") or "")
    title = str(item.get("title") or "")
    existing = client.zhihu_article.find_first(where={"article_id": article_id})
    payload = {
        "author_id": author.id,
        "article_url": str(item.get("url") or f"https://zhuanlan.zhihu.com/p/{article_id}"),
        "title": title,
        "excerpt": content_text,
        "content_html": content_html,
        "content_text": content_text,
        "created_time": created_dt,
        "updated_time": updated_dt,
        "last_seen_at": now_value,
        "voteup_count": int(item.get("voteup_count") or 0),
        "comment_count": int(item.get("comment_count") or 0),
    }
    if existing is None:
        client.zhihu_article.upsert(
            where={"article_id": article_id},
            update={"last_seen_at": now_value},
            insert={"article_id": article_id, "first_seen_at": now_value, **payload},
        )
        client.zhihu_author.update(
            where={"id": author.id},
            data={"last_seen_content_id": article_id, "last_seen_pub_time": created_dt, "updated_at": now_value},
        )
        return True
    if existing.updated_time < updated_dt or existing.created_time < created_dt or not existing.content_html.strip():
        client.zhihu_article.update(where={"id": existing.id}, data=payload)
        client.zhihu_author.update(
            where={"id": author.id},
            data={"last_seen_content_id": article_id, "last_seen_pub_time": created_dt, "updated_at": now_value},
        )
    else:
        client.zhihu_article.update(where={"id": existing.id}, data={"last_seen_at": now_value})
    return False


def upsert_pin_from_today_item(client: Client, author, item: dict[str, object], now_value: datetime) -> bool:
    pin_id = content_id_from_item(item)
    if not pin_id:
        return False
    created_dt = to_dt(item.get("publish_time_iso"))
    updated_dt = to_dt(item.get("updated_time_iso")) or created_dt
    if created_dt is None or updated_dt is None:
        raise RuntimeError(f"想法时间字段缺失: {pin_id}")
    title = str(item.get("title") or "")
    content_html = str(item.get("content_html") or "")
    content_text = str(item.get("content_text") or "")
    existing = client.zhihu_pin.find_first(where={"pin_id": pin_id})
    payload = {
        "author_id": author.id,
        "pin_url": str(item.get("url") or f"https://www.zhihu.com/pin/{pin_id}"),
        "excerpt_title": title,
        "content_html": content_html,
        "content_text": content_text,
        "created_time": created_dt,
        "updated_time": updated_dt,
        "last_seen_at": now_value,
        "like_count": int(item.get("voteup_count") or 0),
        "comment_count": int(item.get("comment_count") or 0),
        "reaction_count": int(item.get("voteup_count") or 0),
    }
    if existing is None:
        client.zhihu_pin.upsert(
            where={"pin_id": pin_id},
            update={"last_seen_at": now_value},
            insert={"pin_id": pin_id, "first_seen_at": now_value, **payload},
        )
        client.zhihu_author.update(
            where={"id": author.id},
            data={"last_seen_content_id": pin_id, "last_seen_pub_time": created_dt, "updated_at": now_value},
        )
        return True
    if existing.updated_time < updated_dt or existing.created_time < created_dt or not existing.content_html.strip():
        client.zhihu_pin.update(where={"id": existing.id}, data=payload)
        client.zhihu_author.update(
            where={"id": author.id},
            data={"last_seen_content_id": pin_id, "last_seen_pub_time": created_dt, "updated_at": now_value},
        )
    else:
        client.zhihu_pin.update(where={"id": existing.id}, data={"last_seen_at": now_value})
    return False


def upsert_answer_from_today_item(client: Client, author, item: dict[str, object], now_value: datetime) -> bool:
    answer_id = content_id_from_item(item)
    if not answer_id:
        return False
    created_dt = to_dt(item.get("publish_time_iso"))
    updated_dt = to_dt(item.get("updated_time_iso")) or created_dt
    if created_dt is None or updated_dt is None:
        raise RuntimeError(f"回答时间字段缺失: {answer_id}")
    content_html = str(item.get("content_html") or "")
    content_text = str(item.get("content_text") or "")
    title = str(item.get("title") or "")
    url = str(item.get("url") or "")
    question_id = ""
    if "/question/" in url and "/answer/" in url:
        parts = url.split("/")
        try:
            question_id = parts[parts.index("question") + 1]
        except (ValueError, IndexError):
            question_id = ""
    existing = client.zhihu_answer.find_first(where={"answer_id": answer_id})
    payload = {
        "author_id": author.id,
        "question_id": question_id,
        "question_title": title,
        "question_api_url": "",
        "answer_api_url": "",
        "answer_url": url,
        "excerpt": content_text,
        "content_html": content_html,
        "content_text": content_text,
        "created_time": created_dt,
        "updated_time": updated_dt,
        "last_seen_at": now_value,
        "voteup_count": int(item.get("voteup_count") or 0),
        "comment_count": int(item.get("comment_count") or 0),
        "thanks_count": None,
    }
    if existing is None:
        client.zhihu_answer.upsert(
            where={"answer_id": answer_id},
            update={"last_seen_at": now_value},
            insert={"answer_id": answer_id, "first_seen_at": now_value, **payload},
        )
        client.zhihu_author.update(
            where={"id": author.id},
            data={"last_seen_content_id": answer_id, "last_seen_pub_time": created_dt, "updated_at": now_value},
        )
        return True
    if existing.updated_time < updated_dt or existing.created_time < created_dt or not existing.content_html.strip():
        client.zhihu_answer.update(where={"id": existing.id}, data=payload)
        client.zhihu_author.update(
            where={"id": author.id},
            data={"last_seen_content_id": answer_id, "last_seen_pub_time": created_dt, "updated_at": now_value},
        )
    else:
        client.zhihu_answer.update(where={"id": existing.id}, data={"last_seen_at": now_value})
    return False


def import_today_payload(profile_url: str, payload: dict[str, object]) -> dict[str, int]:
    slug = profile_slug(profile_url)
    items = payload.get("items")
    if not isinstance(items, list):
        raise RuntimeError("今日更新输出格式不对: items 缺失")
    client = Client()
    now_value = now()
    try:
        author = client.zhihu_author.find_first(where={"slug": slug})
        author_name = ""
        for raw_item in items:
            if isinstance(raw_item, dict) and str(raw_item.get("author_name") or "").strip():
                author_name = str(raw_item.get("author_name") or "").strip()
                break
        if author is None:
            author = ensure_author_record(client, slug, author_name or slug, now_value)
        elif author_name and author.name != author_name:
            client.zhihu_author.update(where={"id": author.id}, data={"name": author_name, "updated_at": now_value})
            author = client.zhihu_author.find_first(where={"id": author.id})
        if author is None:
            raise RuntimeError(f"知乎作者不存在: {slug}")

        stats = {"new_answers": 0, "new_articles": 0, "new_pins": 0}
        latest_pub_time: datetime | None = author.last_seen_pub_time
        latest_content_id = author.last_seen_content_id
        for raw_item in items:
            if not isinstance(raw_item, dict):
                continue
            content_type = str(raw_item.get("content_type") or "")
            if content_type == "answer":
                is_new = upsert_answer_from_today_item(client, author, raw_item, now_value)
                stats["new_answers"] += int(is_new)
            elif content_type == "article":
                is_new = upsert_article_from_today_item(client, author, raw_item, now_value)
                stats["new_articles"] += int(is_new)
            elif content_type == "pin":
                is_new = upsert_pin_from_today_item(client, author, raw_item, now_value)
                stats["new_pins"] += int(is_new)
            else:
                continue
            item_pub_time = to_dt(raw_item.get("publish_time_iso"))
            item_content_id = content_id_from_item(raw_item)
            if item_pub_time is not None and (latest_pub_time is None or item_pub_time >= latest_pub_time):
                latest_pub_time = item_pub_time
                latest_content_id = item_content_id or latest_content_id
        client.zhihu_author.update(
            where={"id": author.id},
            data={
                "profile_url": profile_url,
                "last_seen_content_id": latest_content_id,
                "last_seen_pub_time": latest_pub_time,
                "last_seen_at": now_value,
                "updated_at": now_value,
            },
        )
        return stats
    finally:
        Client.close_all()


def upsert_answer(client: Client, author, item: dict[str, object], detail: dict[str, object] | None = None) -> bool:
    answer_id = str(item.get("answer_id") or "")
    if not answer_id:
        return False
    created_time = item.get("created_time")
    updated_time = item.get("updated_time")
    if detail is not None:
        created_time = to_dt(detail.get("created_time")) or created_time
        updated_time = to_dt(detail.get("updated_time")) or updated_time
        content_html = str(detail.get("content") or "")
        excerpt = str(detail.get("excerpt") or item.get("excerpt") or "")
        voteup_count = int(detail.get("voteup_count") or item.get("voteup_count") or 0)
        comment_count = int(detail.get("comment_count") or item.get("comment_count") or 0)
        thanks_count = detail.get("thanks_count", item.get("thanks_count"))
        question = detail.get("question") if isinstance(detail.get("question"), dict) else {}
    else:
        content_html = ""
        excerpt = str(item.get("excerpt") or "")
        voteup_count = int(item.get("voteup_count") or 0)
        comment_count = int(item.get("comment_count") or 0)
        thanks_count = item.get("thanks_count")
        question = {}
    created_dt = created_time if isinstance(created_time, datetime) else to_dt(created_time)
    updated_dt = updated_time if isinstance(updated_time, datetime) else to_dt(updated_time)
    if created_dt is None or updated_dt is None:
        raise RuntimeError(f"回答时间字段缺失: {answer_id}")
    if not content_html.strip():
        raise RuntimeError(f"回答正文缺失: {answer_id}")
    question_id = str(question.get("id") or item.get("question_id") or "")
    question_title = str(question.get("title") or item.get("question_title") or "")
    question_api_url = str(question.get("url") or item.get("question_api_url") or "")
    answer_url = str(item.get("answer_url") or "")
    now_value = now()
    existing = client.zhihu_answer.find_first(where={"answer_id": answer_id})
    if existing is None:
        client.zhihu_answer.upsert(
            where={"answer_id": answer_id},
            update={
                "last_seen_at": now_value,
            },
            insert={
                "answer_id": answer_id,
                "author_id": author.id,
                "question_id": question_id,
                "question_title": question_title,
                "question_api_url": question_api_url,
                "answer_api_url": str(item.get("answer_api_url") or ""),
                "answer_url": answer_url,
                "excerpt": excerpt,
                "content_html": content_html,
                "content_text": answer_text(content_html, excerpt),
                "created_time": created_dt,
                "updated_time": updated_dt,
                "first_seen_at": now_value,
                "last_seen_at": now_value,
                "voteup_count": voteup_count,
                "comment_count": comment_count,
                "thanks_count": thanks_count if isinstance(thanks_count, int) else None,
            },
        )
        client.zhihu_author.update(
            where={"id": author.id},
            data={"last_seen_content_id": answer_id, "last_seen_pub_time": created_dt, "updated_at": now_value},
        )
        return True
    if existing.updated_time < updated_dt or existing.created_time < created_dt or not existing.content_html.strip():
        client.zhihu_answer.update(
            where={"id": existing.id},
            data={
                "question_id": question_id or existing.question_id,
                "question_title": question_title or existing.question_title,
                "question_api_url": question_api_url or existing.question_api_url,
                "answer_api_url": str(item.get("answer_api_url") or existing.answer_api_url),
                "answer_url": answer_url or existing.answer_url,
                "excerpt": excerpt,
                "content_html": content_html,
                "content_text": answer_text(content_html, excerpt),
                "created_time": created_dt,
                "updated_time": updated_dt,
                "last_seen_at": now_value,
                "voteup_count": voteup_count,
                "comment_count": comment_count,
                "thanks_count": thanks_count if isinstance(thanks_count, int) else existing.thanks_count,
            },
        )
        client.zhihu_author.update(
            where={"id": author.id},
            data={"last_seen_content_id": answer_id, "last_seen_pub_time": created_dt, "updated_at": now_value},
        )
    else:
        client.zhihu_answer.update(where={"id": existing.id}, data={"last_seen_at": now_value})
    return False


def pin_content_text(item: dict[str, object]) -> str:
    html = str(item.get("content_html") or "")
    if html:
        return html
    content = item.get("content")
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("content"), str):
                parts.append(part["content"])
        return "\n".join(parts)
    return ""


def upsert_pin(client: Client, author, item: dict[str, object]) -> bool:
    pin_id = str(item.get("id") or item.get("pin_id") or "")
    if not pin_id:
        return False
    created_dt = to_dt(item.get("created"))
    updated_dt = to_dt(item.get("updated"))
    if created_dt is None or updated_dt is None:
        raise RuntimeError(f"想法时间字段缺失: {pin_id}")
    now_value = now()
    existing = client.zhihu_pin.find_first(where={"pin_id": pin_id})
    payload = {
        "pin_id": pin_id,
        "author_id": author.id,
        "pin_url": str(item.get("pin_url") or f"https://www.zhihu.com/pin/{pin_id}"),
        "excerpt_title": str(item.get("excerpt_title") or ""),
        "content_html": str(item.get("content_html") or ""),
        "content_text": pin_content_text(item),
        "created_time": created_dt,
        "updated_time": updated_dt,
        "last_seen_at": now_value,
        "like_count": int(item.get("like_count") or 0),
        "comment_count": int(item.get("comment_count") or 0),
        "reaction_count": int(item.get("reaction_count") or 0),
    }
    if existing is None:
        client.zhihu_pin.upsert(
            where={"pin_id": pin_id},
            update={"last_seen_at": now_value},
            insert={**payload, "first_seen_at": now_value},
        )
        client.zhihu_author.update(
            where={"id": author.id},
            data={"last_seen_content_id": pin_id, "last_seen_pub_time": created_dt, "updated_at": now_value},
        )
        return True
    if existing.updated_time < updated_dt or existing.created_time < created_dt:
        client.zhihu_pin.update(where={"id": existing.id}, data=payload)
        client.zhihu_author.update(
            where={"id": author.id},
            data={"last_seen_content_id": pin_id, "last_seen_pub_time": created_dt, "updated_at": now_value},
        )
    else:
        client.zhihu_pin.update(where={"id": existing.id}, data={"last_seen_at": now_value})
    return False


def sync_author_contents(client: Client, page, author, first_run: bool) -> dict[str, int]:
    new_answers = 0
    last_seen_pub_time = author.last_seen_pub_time
    candidates = fetch_answer_candidates(page, author.slug)
    if not candidates:
        return {"new_answers": 0, "new_pins": 0}

    if first_run:
        start_dt = today_start_utc()
        candidates = [item for item in candidates if (created := answer_created_dt(item)) is not None and created >= start_dt]
    elif isinstance(last_seen_pub_time, datetime):
        candidates = [item for item in candidates if (created := answer_created_dt(item)) is not None and created > last_seen_pub_time]

    for item in candidates:
        answer_id = str(item.get("answer_id") or "")
        if not answer_id:
            continue
        detail = fetch_complete_answer_detail(page, answer_id)
        if detail is None:
            print(f"跳过回答: answer_id={answer_id} reason=missing_content_html", file=sys.stderr, flush=True)
            continue
        actual_slug = detail_author_slug(detail, item)
        if actual_slug and actual_slug != author.slug:
            print(
                f"跳过回答: answer_id={answer_id} expected_slug={author.slug} actual_slug={actual_slug}",
                file=sys.stderr,
                flush=True,
            )
            continue
        if upsert_answer(client, author, item, detail):
            new_answers += 1
        sleep_random()
    return {"new_answers": new_answers, "new_pins": 0}


def backfill_empty_answer_contents(
    profile_url: str = DEFAULT_PROFILE_URL,
    profile_dir: Path = DEFAULT_PROFILE_DIR,
    signin_url: str = DEFAULT_SIGNIN_URL,
    login_wait: int = 300,
    limit: int = 100,
) -> dict[str, int]:
    profile_dir.mkdir(parents=True, exist_ok=True)
    previous_display = subprocess.os.environ.get("DISPLAY")
    previous_xauthority = subprocess.os.environ.get("XAUTHORITY")
    subprocess.os.environ["DISPLAY"] = DEFAULT_X_DISPLAY
    subprocess.os.environ["XAUTHORITY"] = DEFAULT_XAUTHORITY
    context = launch_persistent_context(profile_dir, headless=False, locale="zh-CN", timezone="Asia/Shanghai", humanize=True, viewport={"width": 1280, "height": 900})
    client = Client()
    try:
        page = context.new_page()
        wait_for_login(page, signin_url, login_wait)
        answers = client.zhihu_answer.find_many(where={"content_html": ""}, order_by={"first_seen_at": "desc"})
        fixed = 0
        skipped = 0
        for answer in answers[:limit]:
            detail = fetch_complete_answer_detail(page, answer.answer_id)
            if detail is None:
                skipped += 1
                print(f"跳过回填: answer_id={answer.answer_id} reason=missing_content_html", file=sys.stderr, flush=True)
                continue
            content_html = str(detail.get("content") or "")
            excerpt = str(detail.get("excerpt") or answer.excerpt or "")
            question = detail.get("question") if isinstance(detail.get("question"), dict) else {}
            created_dt = to_dt(detail.get("created_time")) or answer.created_time
            updated_dt = to_dt(detail.get("updated_time")) or answer.updated_time
            client.zhihu_answer.update(
                where={"id": answer.id},
                data={
                    "question_id": str(question.get("id") or answer.question_id),
                    "question_title": str(question.get("title") or answer.question_title),
                    "question_api_url": str(question.get("url") or answer.question_api_url),
                    "excerpt": excerpt,
                    "content_html": content_html,
                    "content_text": answer_text(content_html, excerpt),
                    "created_time": created_dt,
                    "updated_time": updated_dt,
                    "last_seen_at": now(),
                    "voteup_count": int(detail.get("voteup_count") or answer.voteup_count or 0),
                    "comment_count": int(detail.get("comment_count") or answer.comment_count or 0),
                    "thanks_count": detail.get("thanks_count") if isinstance(detail.get("thanks_count"), int) else answer.thanks_count,
                },
            )
            fixed += 1
            sleep_random()
        return {"fixed_answers": fixed, "skipped_answers": skipped}
    finally:
        Client.close_all()
        context.close()
        if previous_display is None:
            subprocess.os.environ.pop("DISPLAY", None)
        else:
            subprocess.os.environ["DISPLAY"] = previous_display
        if previous_xauthority is None:
            subprocess.os.environ.pop("XAUTHORITY", None)
        else:
            subprocess.os.environ["XAUTHORITY"] = previous_xauthority


def repair_answer_authors(
    profile_url: str = DEFAULT_PROFILE_URL,
    profile_dir: Path = DEFAULT_PROFILE_DIR,
    signin_url: str = DEFAULT_SIGNIN_URL,
    login_wait: int = 300,
    limit: int = 100,
    author_slug: str | None = None,
) -> dict[str, int]:
    profile_dir.mkdir(parents=True, exist_ok=True)
    previous_display = subprocess.os.environ.get("DISPLAY")
    previous_xauthority = subprocess.os.environ.get("XAUTHORITY")
    subprocess.os.environ["DISPLAY"] = DEFAULT_X_DISPLAY
    subprocess.os.environ["XAUTHORITY"] = DEFAULT_XAUTHORITY
    context = launch_persistent_context(profile_dir, headless=False, locale="zh-CN", timezone="Asia/Shanghai", humanize=True, viewport={"width": 1280, "height": 900})
    client = Client()
    try:
        page = context.new_page()
        wait_for_login(page, signin_url, login_wait)
        answers = client.zhihu_answer.find_many(order_by={"created_time": "desc"})
        checked = 0
        fixed = 0
        skipped = 0
        for answer in answers:
            current_author = client.zhihu_author.find_first(where={"id": answer.author_id})
            if current_author is None:
                skipped += 1
                continue
            if author_slug is not None and current_author.slug != author_slug:
                continue
            if checked >= limit:
                break
            checked += 1
            detail = fetch_complete_answer_detail(page, answer.answer_id)
            if detail is None:
                skipped += 1
                print(f"跳过修复: answer_id={answer.answer_id} reason=missing_detail", file=sys.stderr, flush=True)
                continue
            actual_slug = detail_author_slug(detail, {})
            if not actual_slug or actual_slug == current_author.slug:
                continue
            actual_author = detail.get("author") if isinstance(detail.get("author"), dict) else {}
            actual_name = str(actual_author.get("name") or actual_slug)
            target_author = ensure_author_record(client, actual_slug, actual_name, now())
            if target_author is None:
                skipped += 1
                continue
            client.zhihu_answer.update(
                where={"id": answer.id},
                data={
                    "author_id": target_author.id,
                    "last_seen_at": now(),
                },
            )
            fixed += 1
            print(
                f"修复回答作者: answer_id={answer.answer_id} from={current_author.slug} to={actual_slug}",
                file=sys.stderr,
                flush=True,
            )
            sleep_random(1, 2)
        return {"checked_answers": checked, "fixed_answers": fixed, "skipped_answers": skipped}
    finally:
        Client.close_all()
        context.close()
        if previous_display is None:
            subprocess.os.environ.pop("DISPLAY", None)
        else:
            subprocess.os.environ["DISPLAY"] = previous_display
        if previous_xauthority is None:
            subprocess.os.environ.pop("XAUTHORITY", None)
        else:
            subprocess.os.environ["XAUTHORITY"] = previous_xauthority


def sync_zhihu(profile_url: str = DEFAULT_PROFILE_URL, profile_dir: Path = DEFAULT_PROFILE_DIR, signin_url: str = DEFAULT_SIGNIN_URL, login_wait: int = 300) -> dict[str, int]:
    profile_dir.mkdir(parents=True, exist_ok=True)
    refresh_following(profile_url=profile_url, profile_dir=profile_dir, signin_url=signin_url, login_wait=login_wait)
    client = Client()
    try:
        authors = client.zhihu_author.find_many(where={"is_following": True}, order_by={"updated_at": "desc"})
        stats = {"new_answers": 0, "new_articles": 0, "new_pins": 0}
        for author in authors:
            result = run_today_updates(author.profile_url, profile_dir=profile_dir)
            stats["new_answers"] += int(result.get("new_answers", 0))
            stats["new_articles"] += int(result.get("new_articles", 0))
            stats["new_pins"] += int(result.get("new_pins", 0))
            sleep_random()
        return stats
    finally:
        Client.close_all()


def refresh_following(profile_url: str = DEFAULT_PROFILE_URL, profile_dir: Path = DEFAULT_PROFILE_DIR, signin_url: str = DEFAULT_SIGNIN_URL, login_wait: int = 300) -> dict[str, int]:
    profile_dir.mkdir(parents=True, exist_ok=True)
    previous_display = subprocess.os.environ.get("DISPLAY")
    previous_xauthority = subprocess.os.environ.get("XAUTHORITY")
    subprocess.os.environ["DISPLAY"] = DEFAULT_X_DISPLAY
    subprocess.os.environ["XAUTHORITY"] = DEFAULT_XAUTHORITY
    context = launch_persistent_context(profile_dir, headless=False, locale="zh-CN", timezone="Asia/Shanghai", humanize=True, viewport={"width": 1280, "height": 900})
    client = Client()
    try:
        authors_before = client.zhihu_author.find_many()
        following_before = {author.slug for author in authors_before if author.is_following}
        page = context.new_page()
        users = sync_following(client, page, profile_url, signin_url, login_wait)
        following_after = {user["slug"] for user in users}
        return {
            "following_count": len(users),
            "added_count": len(following_after - following_before),
            "removed_count": len(following_before - following_after),
            "reactivated_count": len(
                {
                    user["slug"]
                    for user in users
                    if user["slug"] not in following_before
                    and any(author.slug == user["slug"] for author in authors_before)
                }
            ),
        }
    finally:
        Client.close_all()
        context.close()
        if previous_display is None:
            subprocess.os.environ.pop("DISPLAY", None)
        else:
            subprocess.os.environ["DISPLAY"] = previous_display
        if previous_xauthority is None:
            subprocess.os.environ.pop("XAUTHORITY", None)
        else:
            subprocess.os.environ["XAUTHORITY"] = previous_xauthority


def sync_single_author(
    author_slug: str,
    profile_url: str,
    profile_dir: Path = DEFAULT_PROFILE_DIR,
    signin_url: str = DEFAULT_SIGNIN_URL,
    login_wait: int = 300,
) -> dict[str, int]:
    del signin_url, login_wait
    profile_dir.mkdir(parents=True, exist_ok=True)
    client = Client()
    try:
        author = client.zhihu_author.find_first(where={"slug": author_slug})
        now_value = now()
        if author is None:
            author = ensure_author_record(client, author_slug, author_slug, now_value)
        if author is None:
            raise RuntimeError(f"知乎作者不存在: {author_slug}")
        update_payload = {
            "profile_url": profile_url,
            "is_following": True,
            "updated_at": now_value,
        }
        client.zhihu_author.update(where={"id": author.id}, data=update_payload)
        author = client.zhihu_author.find_first(where={"id": author.id})
        if author is None:
            raise RuntimeError(f"知乎作者不存在: {author_slug}")
        result = run_today_updates(profile_url, profile_dir=profile_dir)
        return {
            "new_answers": int(result.get("new_answers", 0)),
            "new_articles": int(result.get("new_articles", 0)),
            "new_pins": int(result.get("new_pins", 0)),
        }
    finally:
        Client.close_all()


def write_sync_request(action: str, profile_url: str = DEFAULT_REQUEST_PROFILE_URL, **extra: object) -> dict[str, object]:
    DEFAULT_REQUEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "action": action,
        "profile_url": profile_url,
        "requested_at": datetime.now().astimezone().isoformat(),
        "status": "pending",
        **extra,
    }
    DEFAULT_REQUEST_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def read_sync_request() -> dict[str, object] | None:
    if not DEFAULT_REQUEST_PATH.exists():
        return None
    return json.loads(DEFAULT_REQUEST_PATH.read_text(encoding="utf-8"))


def clear_sync_request() -> None:
    if DEFAULT_REQUEST_PATH.exists():
        DEFAULT_REQUEST_PATH.unlink()


def write_sync_result(payload: dict[str, object]) -> dict[str, object]:
    DEFAULT_RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_RESULT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def read_sync_result() -> dict[str, object] | None:
    if not DEFAULT_RESULT_PATH.exists():
        return None
    return json.loads(DEFAULT_RESULT_PATH.read_text(encoding="utf-8"))


def cli_main() -> None:
    parser = argparse.ArgumentParser(description="知乎同步")
    parser.add_argument("--run-once", action="store_true")
    parser.add_argument("--action", default="refresh-following")
    parser.add_argument("--profile-url", default=DEFAULT_PROFILE_URL)
    parser.add_argument("--profile-dir", type=Path, default=DEFAULT_PROFILE_DIR)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--author-slug")
    args = parser.parse_args()
    if not args.run_once:
        raise SystemExit("目前只支持 --run-once")
    if args.action == "backfill-empty-answer-content":
        result = backfill_empty_answer_contents(profile_url=args.profile_url, profile_dir=args.profile_dir, limit=args.limit)
    elif args.action == "repair-answer-authors":
        result = repair_answer_authors(profile_url=args.profile_url, profile_dir=args.profile_dir, limit=args.limit, author_slug=args.author_slug)
    else:
        result = sync_zhihu(profile_url=args.profile_url, profile_dir=args.profile_dir)
    write_sync_result(
        {
            "action": args.action,
            "profile_url": args.profile_url,
            "finished_at": datetime.now().astimezone().isoformat(),
            "status": "done",
            **result,
        }
    )


if __name__ == "__main__":
    cli_main()
