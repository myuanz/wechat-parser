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
DEFAULT_OUTPUT = Path("/srv/samba/share") / "zhihu_profile_activities_min_test.json"
DEFAULT_LIMIT = 20


def profile_slug(profile_url: str) -> str:
    parts = [part for part in urlparse(profile_url).path.split("/") if part]
    if len(parts) >= 2 and parts[0] == "people":
        return parts[1]
    raise ValueError(f"不是有效的知乎个人主页: {profile_url}")


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


def target_text(target: dict[str, object]) -> str:
    content = target.get("content")
    if isinstance(content, str) and content.strip():
        return html_to_text(content)
    excerpt = target.get("excerpt")
    if isinstance(excerpt, str) and excerpt.strip():
        return excerpt.strip()
    content_html = target.get("content_html")
    if isinstance(content_html, str) and content_html.strip():
        return html_to_text(content_html)
    content_list = target.get("content")
    if isinstance(content_list, list):
        parts: list[str] = []
        for item in content_list:
            if isinstance(item, dict) and isinstance(item.get("content"), str):
                parts.append(item["content"])
        if parts:
            return html_to_text("\n".join(parts))
    title = target.get("title")
    if isinstance(title, str):
        return title.strip()
    return ""


def extract_activity_records(payload: object) -> list[dict[str, object]]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, list):
        return []

    items: list[dict[str, object]] = []
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            continue
        target = item.get("target") if isinstance(item.get("target"), dict) else {}
        author = target.get("author") if isinstance(target.get("author"), dict) else {}
        question = target.get("question") if isinstance(target.get("question"), dict) else {}
        action_text = item.get("action_text")
        verb = item.get("verb")
        item_type = str(item.get("type") or "")
        target_type = str(target.get("type") or item_type or "")
        target_id = target.get("id")
        created_time = item.get("created_time")

        if target_type == "answer":
            web_url = build_answer_url(question.get("id"), target_id)
            title = str(question.get("title") or "")
        elif target_type == "article":
            web_url = build_article_url(target_id)
            title = str(target.get("title") or "")
        elif target_type == "pin":
            web_url = build_pin_url(target_id)
            title = str(target.get("excerpt_title") or "")
        else:
            web_url = str(target.get("url") or "")
            title = str(target.get("title") or question.get("title") or "")

        items.append(
            {
                "index": index,
                "activity_type": item_type,
                "verb": verb,
                "action_text": action_text,
                "target_type": target_type,
                "target_id": target_id,
                "title": title,
                "summary": target_text(target),
                "author_name": str(author.get("name") or ""),
                "author_url_token": str(author.get("url_token") or ""),
                "question_id": question.get("id"),
                "question_title": str(question.get("title") or ""),
                "created_time": created_time,
                "created_time_iso": datetime.fromtimestamp(created_time).astimezone().isoformat() if isinstance(created_time, int | float) else "",
                "comment_count": target.get("comment_count"),
                "voteup_count": target.get("voteup_count"),
                "reaction_count": target.get("reaction_count"),
                "url": web_url,
                "raw": item,
            }
        )
    return items


def extract_dom_activities(page, limit: int) -> list[dict[str, object]]:
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

          return Array.from(document.querySelectorAll('.List-item')).slice(0, limit).map((item, index) => {
            const actionNode = item.querySelector('.ActivityItem-metaTitle');
            const timeNode = item.querySelector('.ActivityItem-meta span:last-child');
            const contentRoot = item.querySelector('.ContentItem');
            const authorNode = item.querySelector('.AuthorInfo-name, .UserLink-link');
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
              author_name: clean(authorNode?.textContent),
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


def fetch_activities_payload(page, slug: str, limit: int) -> dict[str, object]:
    payload = page.evaluate(
        """
        async ({ slug, limit }) => {
          const url = `https://www.zhihu.com/api/v4/members/${slug}/activities?limit=${limit}&desktop=True`;
          const response = await fetch(url, {
            credentials: "include",
            headers: {
              "accept": "application/json, text/plain, */*"
            }
          });
          if (!response.ok) {
            throw new Error(`抓取动态列表失败: ${response.status} ${response.statusText}`);
          }
          return {
            request_url: url,
            payload: await response.json(),
          };
        }
        """,
        {"slug": slug, "limit": limit},
    )
    if not isinstance(payload, dict):
        raise RuntimeError("动态接口返回格式不对")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="最小测试：抓取知乎个人主页默认动态流")
    parser.add_argument("url", nargs="?", default=DEFAULT_PROFILE_URL)
    parser.add_argument("--signin-url", default=DEFAULT_SIGNIN_URL)
    parser.add_argument("--profile-dir", type=Path, default=DEFAULT_PROFILE_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--login-wait", type=int, default=300)
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()

    args.profile_dir.mkdir(parents=True, exist_ok=True)
    slug = profile_slug(args.url)

    context = launch_persistent_context(
        args.profile_dir,
        headless=args.headless,
        locale="zh-CN",
        timezone="Asia/Shanghai",
        humanize=True,
        viewport={"width": 1280, "height": 900},
    )
    try:
        page = context.new_page()
        observed_api_urls: list[str] = []
        captured_payloads: list[dict[str, object]] = []

        def handle_response(response) -> None:
            url = response.url
            if "/api/v4/" not in url:
                return
            observed_api_urls.append(url)
            try:
                payload = response.json()
            except Exception:
                return
            if isinstance(payload, dict) and isinstance(payload.get("data"), list):
                captured_payloads.append({"url": url, "payload": payload})

        page.on("response", handle_response)
        wait_for_login(page, args.signin_url, args.login_wait)
        page.goto(args.url, wait_until="domcontentloaded", timeout=60_000)
        if is_login_page(page):
            raise RuntimeError("登录后仍然被重定向到登录页，请确认账号已完成登录")
        page.wait_for_timeout(3_000)

        result = fetch_activities_payload(page, slug, args.limit)
        request_url = str(result.get("request_url") or "")
        payload = result.get("payload")
        activities = extract_activity_records(payload)
        if not activities:
            for item in captured_payloads:
                parsed = extract_activity_records(item["payload"])
                if parsed:
                    activities = parsed[: args.limit]
                    if not request_url:
                        request_url = str(item["url"])
                    break
        dom_activities = extract_dom_activities(page, args.limit)
        if dom_activities:
            activities = dom_activities

        output = {
            "source_profile_url": args.url,
            "slug": slug,
            "fetched_at": datetime.now().astimezone().isoformat(),
            "api_source_url": request_url,
            "activity_count": len(activities),
            "observed_api_urls": observed_api_urls,
            "activities": activities,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已保存 {len(activities)} 条动态到 {args.output}", flush=True)
    finally:
        context.close()


if __name__ == "__main__":
    main()
