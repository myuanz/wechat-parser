import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from cloakbrowser import launch_persistent_context


DEFAULT_PROFILE_URL = "https://www.zhihu.com/people/xiao-peng-61-47"
DEFAULT_SIGNIN_URL = "https://www.zhihu.com/signin"
DEFAULT_PROFILE_DIR = Path(__file__).with_name("browser_profiles") / "zhihu"
DEFAULT_OUTPUT = Path("/srv/samba/share") / "zhihu_profile_today_updates.json"
DEFAULT_LIMIT = 30


def profile_slug(profile_url: str) -> str:
    parts = [part for part in urlparse(profile_url).path.split("/") if part]
    if len(parts) >= 2 and parts[0] in {"people", "org"}:
        return parts[1]
    raise ValueError(f"不是有效的知乎主页: {profile_url}")


def is_login_page(page) -> bool:
    url = page.url.lower()
    if "signin" in url or "login" in url:
        return True
    return page.locator('input[type="tel"], input[name="phone"], input[placeholder*="手机号"], input[placeholder*="密码"]').count() > 0


def wait_for_login(page, signin_url: str, seconds: int) -> None:
    page.goto(signin_url, wait_until="domcontentloaded", timeout=60_000)
    deadline = datetime.now().timestamp() + seconds
    while datetime.now().timestamp() < deadline:
        if not is_login_page(page):
            return
        page.wait_for_timeout(1000)
    raise RuntimeError("知乎仍在登录页，请先在打开的浏览器里完成登录")


def html_to_text(html: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", html)
    text = re.sub(r"</p>|</div>|</li>|</h[1-6]>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&nbsp;", " ")
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&").replace("&quot;", '"')
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def build_answer_url(question_id: object, answer_id: object) -> str:
    if isinstance(question_id, int | str) and isinstance(answer_id, int | str):
        return f"https://www.zhihu.com/question/{question_id}/answer/{answer_id}"
    return ""


def build_article_url(article_id: object) -> str:
    if isinstance(article_id, int | str):
        return f"https://zhuanlan.zhihu.com/p/{article_id}"
    return ""


def build_pin_url(pin_id: object) -> str:
    if isinstance(pin_id, int | str):
        return f"https://www.zhihu.com/pin/{pin_id}"
    return ""


def extract_pin_title_and_html(item: dict[str, object]) -> tuple[str, str]:
    content_list = item.get("content")
    if not isinstance(content_list, list):
        return "想法", ""

    title = ""
    html_parts: list[str] = []
    for part in content_list:
        if not isinstance(part, dict):
            continue
        if not title and isinstance(part.get("title"), str) and part.get("title", "").strip():
            title = part["title"].strip()
        if isinstance(part.get("content"), str) and part.get("content", "").strip():
            html_parts.append(part["content"])
    return title or "想法", "\n".join(html_parts)


def start_of_today_local() -> datetime:
    now = datetime.now().astimezone()
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def to_iso(timestamp: object) -> str:
    if isinstance(timestamp, int | float):
        return datetime.fromtimestamp(timestamp).astimezone().isoformat()
    return ""


def is_today_timestamp(timestamp: object, start_dt: datetime) -> bool:
    if not isinstance(timestamp, int | float):
        return False
    return datetime.fromtimestamp(timestamp).astimezone() >= start_dt


def fetch_answers_payload(page, slug: str, limit: int) -> dict[str, object]:
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
          return { url, payload: await response.json() };
        }
        """,
        {"slug": slug, "limit": limit},
    )
    if not isinstance(payload, dict):
        raise RuntimeError("回答接口返回格式不对")
    return payload


def fetch_answer_detail(page, answer_id: str) -> dict[str, object]:
    payload = page.evaluate(
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
          const response = await fetch(url, {
            credentials: "include",
            headers: { "accept": "application/json, text/plain, */*" }
          });
          if (!response.ok) throw new Error(`抓取回答详情失败: ${response.status} ${response.statusText}`);
          return await response.json();
        }
        """,
        answer_id,
    )
    if not isinstance(payload, dict):
        raise RuntimeError(f"回答详情格式不对: {answer_id}")
    return payload


def fetch_articles_payload(page, slug: str, limit: int) -> dict[str, object]:
    payload = page.evaluate(
        """
        async ({ slug, limit }) => {
          const include = [
            "data[*].comment_count",
            "suggest_edit",
            "is_normal",
            "thumbnail_extra_info",
            "thumbnail",
            "can_comment",
            "comment_permission",
            "admin_closed_comment",
            "content",
            "voteup_count",
            "created",
            "updated",
            "upvoted_followees",
            "voting",
            "review_info",
            "reaction_instruction",
            "is_labeled",
            "label_info",
            "reaction",
            "vessay_info",
            "data[*].author.badge[?(type=best_answerer)].topics",
            "data[*].author.kvip_info",
            "data[*].author.vip_info"
          ].join(",");
          const url = `https://www.zhihu.com/api/v4/members/${slug}/articles?include=${include}&offset=0&limit=${limit}&sort_by=created&ws_qiangzhisafe=0`;
          const response = await fetch(url, {
            credentials: "include",
            headers: { "accept": "application/json, text/plain, */*" }
          });
          if (!response.ok) throw new Error(`抓取文章列表失败: ${response.status} ${response.statusText}`);
          return { url, payload: await response.json() };
        }
        """,
        {"slug": slug, "limit": limit},
    )
    if not isinstance(payload, dict):
        raise RuntimeError("文章接口返回格式不对")
    return payload


def fetch_pins_payload(page, slug: str, limit: int) -> dict[str, object]:
    payload = page.evaluate(
        """
        async ({ slug, limit }) => {
          const url = `https://www.zhihu.com/api/v4/v2/members/${slug}/pins?offset=0&limit=${limit}`;
          const response = await fetch(url, {
            credentials: "include",
            headers: { "accept": "application/json, text/plain, */*" }
          });
          if (!response.ok) throw new Error(`抓取想法列表失败: ${response.status} ${response.statusText}`);
          return { url, payload: await response.json() };
        }
        """,
        {"slug": slug, "limit": limit},
    )
    if not isinstance(payload, dict):
        raise RuntimeError("想法接口返回格式不对")
    return payload


def capture_api_payload_on_page(page, page_url: str, api_keyword: str, wait_ms: int = 4_000) -> dict[str, object]:
    payloads: list[dict[str, object]] = []

    def handle_response(response) -> None:
        url = response.url
        if api_keyword not in url:
            return
        try:
            payload = response.json()
        except Exception:
            return
        if isinstance(payload, dict) and isinstance(payload.get("data"), list):
            payloads.append({"url": url, "payload": payload})

    page.on("response", handle_response)
    try:
        page.goto(page_url, wait_until="domcontentloaded", timeout=60_000)
        if is_login_page(page):
            raise RuntimeError("访问内容页时被重定向到登录页")
        page.wait_for_timeout(wait_ms)
    finally:
        page.remove_listener("response", handle_response)

    if not payloads:
        raise RuntimeError(f"页面未捕获到接口响应: {api_keyword}")
    return payloads[0]


def extract_today_answers(payload: object, start_dt: datetime) -> list[dict[str, object]]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, list):
        return []

    items: list[dict[str, object]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        created_time = item.get("created_time")
        if not is_today_timestamp(created_time, start_dt):
            continue
        question = item.get("question") if isinstance(item.get("question"), dict) else {}
        author = item.get("author") if isinstance(item.get("author"), dict) else {}
        answer_id = item.get("id")
        question_id = question.get("id")
        content_html = item.get("content") if isinstance(item.get("content"), str) else ""
        excerpt = item.get("excerpt") or item.get("excerpt_new") or content_html or ""
        items.append(
            {
                "content_type": "answer",
                "content_id": str(answer_id or ""),
                "publish_time": created_time,
                "publish_time_iso": to_iso(created_time),
                "updated_time": item.get("updated_time"),
                "updated_time_iso": to_iso(item.get("updated_time")),
                "url": build_answer_url(question_id, answer_id),
                "title": str(question.get("title") or ""),
                "content_html": content_html,
                "content_text": html_to_text(content_html) if content_html else str(excerpt),
                "author_name": str(author.get("name") or ""),
                "voteup_count": item.get("voteup_count"),
                "comment_count": item.get("comment_count"),
                "source_mode": "api",
                "source_list": "answers",
            }
        )
    return items


def enrich_today_answers(page, items: list[dict[str, object]]) -> list[dict[str, object]]:
    enriched: list[dict[str, object]] = []
    for item in items:
        answer_id = str(item.get("content_id") or "")
        if not answer_id:
            enriched.append(item)
            continue
        detail = fetch_answer_detail(page, answer_id)
        content_html = str(detail.get("content") or "")
        question = detail.get("question") if isinstance(detail.get("question"), dict) else {}
        author = detail.get("author") if isinstance(detail.get("author"), dict) else {}
        merged = dict(item)
        merged["url"] = build_answer_url(question.get("id") or "", answer_id) or str(item.get("url") or "")
        merged["title"] = str(question.get("title") or item.get("title") or "")
        merged["content_html"] = content_html
        merged["content_text"] = html_to_text(content_html) if content_html else str(detail.get("excerpt") or item.get("content_text") or "")
        merged["author_name"] = str(author.get("name") or item.get("author_name") or "")
        merged["publish_time"] = detail.get("created_time", item.get("publish_time"))
        merged["publish_time_iso"] = to_iso(merged["publish_time"])
        merged["updated_time"] = detail.get("updated_time", item.get("updated_time"))
        merged["updated_time_iso"] = to_iso(merged["updated_time"])
        merged["voteup_count"] = detail.get("voteup_count", item.get("voteup_count"))
        merged["comment_count"] = detail.get("comment_count", item.get("comment_count"))
        enriched.append(merged)
    return enriched


def extract_today_articles(payload: object, start_dt: datetime) -> list[dict[str, object]]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, list):
        return []

    items: list[dict[str, object]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        created = item.get("created")
        if not is_today_timestamp(created, start_dt):
            continue
        author = item.get("author") if isinstance(item.get("author"), dict) else {}
        article_id = item.get("id")
        content_html = item.get("content") if isinstance(item.get("content"), str) else ""
        excerpt = item.get("excerpt") if isinstance(item.get("excerpt"), str) else ""
        items.append(
            {
                "content_type": "article",
                "content_id": str(article_id or ""),
                "publish_time": created,
                "publish_time_iso": to_iso(created),
                "updated_time": item.get("updated"),
                "updated_time_iso": to_iso(item.get("updated")),
                "url": build_article_url(article_id),
                "title": str(item.get("title") or ""),
                "content_html": content_html,
                "content_text": html_to_text(content_html) if content_html else excerpt,
                "author_name": str(author.get("name") or ""),
                "voteup_count": item.get("voteup_count"),
                "comment_count": item.get("comment_count"),
                "source_mode": "api",
                "source_list": "articles",
            }
        )
    return items


def extract_today_pins(payload: object, start_dt: datetime) -> list[dict[str, object]]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, list):
        return []

    items: list[dict[str, object]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        created = item.get("created")
        if not is_today_timestamp(created, start_dt):
            continue
        author = item.get("author") if isinstance(item.get("author"), dict) else {}
        pin_id = item.get("id")
        title, content_html = extract_pin_title_and_html(item)
        if not content_html and isinstance(item.get("content_html"), str):
            content_html = str(item.get("content_html") or "")
        items.append(
            {
                "content_type": "pin",
                "content_id": str(pin_id or ""),
                "publish_time": created,
                "publish_time_iso": to_iso(created),
                "updated_time": item.get("updated"),
                "updated_time_iso": to_iso(item.get("updated")),
                "url": build_pin_url(pin_id),
                "title": title,
                "content_html": content_html,
                "content_text": html_to_text(content_html),
                "author_name": str(author.get("name") or ""),
                "voteup_count": item.get("voteup_count"),
                "comment_count": item.get("comment_count"),
                "source_mode": "api",
                "source_list": "pins",
            }
        )
    return items


def extract_dom_default_items(page, limit: int) -> list[dict[str, object]]:
    return page.evaluate(
        """
        ({ limit }) => {
          const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
          const toAbsUrl = (href) => {
            if (!href) return '';
            return new URL(href, location.href).toString();
          };
          const parseCount = (text) => {
            const raw = clean(text);
            if (!raw) return null;
            const match = raw.match(/([0-9]+(?:\\.[0-9]+)?)(万)?/);
            if (!match) return null;
            const value = Number(match[1]);
            if (Number.isNaN(value)) return null;
            return match[2] ? Math.round(value * 10000) : Math.round(value);
          };
          const getAuthorName = (item) => {
            const meta = item.querySelector('meta[itemprop="name"]');
            if (meta?.content) return clean(meta.content);
            const link = item.querySelector('.AuthorInfo-name a, .UserLink-link');
            return clean(link?.textContent);
          };

          return Array.from(document.querySelectorAll('.List-item')).slice(0, limit).map((item, index) => {
            const actionNode = item.querySelector('.ActivityItem-metaTitle');
            const timeNode = item.querySelector('.ActivityItem-meta span:last-child');
            const contentRoot = item.querySelector('.ContentItem');
            const titleLink =
              item.querySelector('.ContentItem-title a') ||
              item.querySelector('a[href*="/question/"][href*="/answer/"]') ||
              item.querySelector('a[href*="zhuanlan.zhihu.com/p/"]') ||
              item.querySelector('a[href*="/pin/"]');
            const pinLink = item.querySelector('a[href*="/pin/"]');
            const answerLink = item.querySelector('a[href*="/question/"][href*="/answer/"]');
            const articleLink = item.querySelector('a[href*="zhuanlan.zhihu.com/p/"]');
            const voteButton = Array.from(item.querySelectorAll('button, a')).find((el) => clean(el.textContent).includes('赞同'));
            const commentButton = Array.from(item.querySelectorAll('button, a')).find((el) => clean(el.textContent).includes('评论'));
            const dataZop = contentRoot?.getAttribute('data-zop') || '';

            let targetType = '';
            if (contentRoot?.classList.contains('PinItem') || pinLink) targetType = 'pin';
            else if (contentRoot?.classList.contains('ArticleItem') || articleLink) targetType = 'article';
            else if (contentRoot?.classList.contains('AnswerItem') || answerLink) targetType = 'answer';

            let targetId = '';
            try {
              if (dataZop) {
                const parsed = JSON.parse(dataZop);
                if (parsed?.type && !targetType) targetType = String(parsed.type);
                if (parsed?.itemId) targetId = String(parsed.itemId);
              }
            } catch (_) {}

            return {
              index,
              action_text: clean(actionNode?.textContent),
              published_time_text: clean(timeNode?.textContent),
              target_type: targetType,
              target_id: targetId,
              title: clean(titleLink?.textContent),
              author_name: getAuthorName(item),
              summary: clean(item.querySelector('.RichContent-inner, .RichText, .ContentItem')?.innerText || item.innerText),
              url: toAbsUrl(titleLink?.getAttribute('href') || pinLink?.getAttribute('href') || answerLink?.getAttribute('href') || articleLink?.getAttribute('href') || ''),
              voteup_count: parseCount(voteButton?.textContent),
              comment_count: parseCount(commentButton?.textContent),
            };
          });
        }
        """,
        {"limit": limit},
    )


def extract_today_dom_fallback(page, start_dt: datetime, limit: int) -> list[dict[str, object]]:
    today_prefix = start_dt.strftime("%Y-%m-%d")
    items = extract_dom_default_items(page, limit)
    result: list[dict[str, object]] = []
    allowed_actions = ("回答了问题", "发表了文章", "发布了想法")
    for item in items:
        if not isinstance(item, dict):
            continue
        target_type = str(item.get("target_type") or "")
        content_id = str(item.get("target_id") or "")
        action_text = str(item.get("action_text") or "")
        published_time_text = str(item.get("published_time_text") or "")
        if target_type not in {"answer", "article", "pin"}:
            continue
        if not any(action in action_text for action in allowed_actions):
            continue
        if today_prefix not in published_time_text:
            continue
        result.append(
            {
                "content_type": target_type,
                "content_id": content_id,
                "publish_time": None,
                "publish_time_iso": published_time_text,
                "updated_time": None,
                "updated_time_iso": "",
                "url": str(item.get("url") or ""),
                "title": str(item.get("title") or ("想法" if target_type == "pin" else "")),
                "content_html": "",
                "content_text": str(item.get("summary") or ""),
                "author_name": str(item.get("author_name") or ""),
                "voteup_count": item.get("voteup_count"),
                "comment_count": item.get("comment_count"),
                "source_mode": "dom_fallback",
                "source_list": "default_feed",
            }
        )
    return result


def merge_items(*groups: list[dict[str, object]]) -> list[dict[str, object]]:
    merged: dict[str, dict[str, object]] = {}
    for group in groups:
        for item in group:
            content_type = str(item.get("content_type") or "")
            content_id = str(item.get("content_id") or "")
            if not content_type or not content_id:
                continue
            key = f"{content_type}:{content_id}"
            merged.setdefault(key, item)
    return sorted(
        merged.values(),
        key=lambda item: (item.get("publish_time") if isinstance(item.get("publish_time"), int | float) else -1),
        reverse=True,
    )


def ids_by_type(items: list[dict[str, object]]) -> dict[str, list[str]]:
    result = {"answer": [], "article": [], "pin": []}
    for item in items:
        content_type = str(item.get("content_type") or "")
        content_id = str(item.get("content_id") or "")
        if content_type in result and content_id:
            result[content_type].append(content_id)
    return result


def slim_item(item: dict[str, object]) -> dict[str, object]:
    return {
        "content_type": item.get("content_type"),
        "content_id": item.get("content_id"),
        "publish_time_iso": item.get("publish_time_iso"),
        "updated_time_iso": item.get("updated_time_iso"),
        "url": item.get("url"),
        "title": item.get("title"),
        "content_html": item.get("content_html"),
        "content_text": item.get("content_text"),
        "author_name": item.get("author_name"),
        "voteup_count": item.get("voteup_count"),
        "comment_count": item.get("comment_count"),
    }


def collect_today_updates(
    profile_url: str,
    signin_url: str = DEFAULT_SIGNIN_URL,
    profile_dir: Path = DEFAULT_PROFILE_DIR,
    limit: int = DEFAULT_LIMIT,
    dom_limit: int = 20,
    login_wait: int = 300,
    headless: bool = False,
) -> dict[str, object]:
    profile_dir.mkdir(parents=True, exist_ok=True)
    slug = profile_slug(profile_url)
    start_dt = start_of_today_local()

    context = launch_persistent_context(
        profile_dir,
        headless=headless,
        locale="zh-CN",
        timezone="Asia/Shanghai",
        humanize=True,
        viewport={"width": 1280, "height": 900},
    )
    try:
        page = context.new_page()
        wait_for_login(page, signin_url, login_wait)
        page.goto(profile_url, wait_until="domcontentloaded", timeout=60_000)
        if is_login_page(page):
            raise RuntimeError("登录后仍然被重定向到登录页，请确认账号已完成登录")
        page.wait_for_timeout(3_000)

        api_urls: dict[str, str] = {}
        errors: dict[str, str] = {}

        answers: list[dict[str, object]] = []
        try:
            result = fetch_answers_payload(page, slug, limit)
            api_urls["answers"] = str(result.get("url") or "")
            answers = extract_today_answers(result.get("payload"), start_dt)
            if answers:
                answers = enrich_today_answers(page, answers)
        except Exception as error:
            errors["answers"] = str(error)

        articles: list[dict[str, object]] = []
        try:
            result = fetch_articles_payload(page, slug, limit)
            api_urls["articles"] = str(result.get("url") or "")
            articles = extract_today_articles(result.get("payload"), start_dt)
        except Exception as error:
            errors["articles_direct"] = str(error)
            try:
                result = capture_api_payload_on_page(page, profile_url.rstrip("/") + "/posts", f"/api/v4/members/{slug}/articles")
                api_urls["articles"] = str(result.get("url") or "")
                articles = extract_today_articles(result.get("payload"), start_dt)
            except Exception as page_error:
                errors["articles"] = str(page_error)

        pins: list[dict[str, object]] = []
        try:
            result = fetch_pins_payload(page, slug, limit)
            api_urls["pins"] = str(result.get("url") or "")
            pins = extract_today_pins(result.get("payload"), start_dt)
        except Exception as error:
            errors["pins_direct"] = str(error)
            try:
                result = capture_api_payload_on_page(page, profile_url.rstrip("/") + "/pins", "/api/v4/v2/pins/")
                api_urls["pins"] = str(result.get("url") or "")
                pins = extract_today_pins(result.get("payload"), start_dt)
            except Exception as page_error:
                errors["pins"] = str(page_error)

        page.goto(profile_url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(3_000)
        dom_today_items = extract_today_dom_fallback(page, start_dt, dom_limit)
        api_items = merge_items(answers, articles, pins)
        api_ids = ids_by_type(api_items)

        dom_only_items = [
            item
            for item in dom_today_items
            if str(item.get("content_id") or "") not in api_ids.get(str(item.get("content_type") or ""), [])
        ]

        final_items = merge_items(api_items, dom_only_items)

        return {
            "source_profile_url": profile_url,
            "slug": slug,
            "fetched_at": datetime.now().astimezone().isoformat(),
            "today_start_iso": start_dt.isoformat(),
            "errors": errors,
            "total_count": len(final_items),
            "items": [slim_item(item) for item in final_items],
        }
    finally:
        context.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="抓取知乎主页今日更新（回答/文章/想法，多接口混合，DOM 兜底）")
    parser.add_argument("--url", default=DEFAULT_PROFILE_URL)
    parser.add_argument("--signin-url", default=DEFAULT_SIGNIN_URL)
    parser.add_argument("--profile-dir", type=Path, default=DEFAULT_PROFILE_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--dom-limit", type=int, default=20)
    parser.add_argument("--login-wait", type=int, default=300)
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()

    payload = collect_today_updates(
        profile_url=args.url,
        signin_url=args.signin_url,
        profile_dir=args.profile_dir,
        limit=args.limit,
        dom_limit=args.dom_limit,
        login_wait=args.login_wait,
        headless=args.headless,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已保存 {int(payload.get('total_count') or 0)} 条今日更新到 {args.output}", flush=True)


if __name__ == "__main__":
    main()
