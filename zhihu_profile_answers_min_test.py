import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from cloakbrowser import launch_persistent_context


DEFAULT_PROFILE_URL = "https://www.zhihu.com/people/shen-chen-7-10"
DEFAULT_SIGNIN_URL = "https://www.zhihu.com/signin"
DEFAULT_PROFILE_DIR = Path(__file__).with_name("browser_profiles") / "zhihu"
DEFAULT_OUTPUT = Path(__file__).with_name("dumps") / "zhihu_profile_answers_min_test.json"


def normalize_answers_url(profile_url: str) -> str:
    return profile_url.rstrip("/") + "/answers"


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


def extract_initial_answers(page) -> list[dict[str, object]]:
    return page.evaluate(
        """
        () => {
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

          const cards = Array.from(document.querySelectorAll('.List-item')).filter((item) => {
            return item.querySelector('.ContentItem.AnswerItem');
          });

          return cards.map((item, index) => {
            const answer = item.querySelector('.ContentItem.AnswerItem');
            const titleLink =
              answer?.querySelector('h2 a[data-za-detail-view-element_name="Title"]') ||
              answer?.querySelector('h2 a') ||
              item.querySelector('h2 a');
            const metaLink =
              answer?.querySelector('meta[itemprop="url"]') ||
              answer?.querySelector('meta[itemprop="mainEntityOfPage"]');
            const answerLink = metaLink?.getAttribute('content') || titleLink?.getAttribute('href') || '';
            const contentRoot =
              answer?.querySelector('.RichContent-inner') ||
              answer?.querySelector('.RichText') ||
              answer;
            const voteButton = Array.from(answer?.querySelectorAll('button') || []).find((btn) => {
              const text = clean(btn.textContent);
              return text.includes('赞同');
            });
            const commentButton = Array.from(answer?.querySelectorAll('button') || []).find((btn) => {
              const text = clean(btn.textContent);
              return text.includes('评论');
            });
            const timeNode =
              answer?.querySelector('time') ||
              answer?.querySelector('.ContentItem-time a span') ||
              answer?.querySelector('.ContentItem-time span');
            const authorNode = item.querySelector('.AuthorInfo-name, .UserLink-link');

            return {
              index,
              question_title: clean(titleLink?.textContent),
              question_url: toAbsUrl(titleLink?.getAttribute('href') || ''),
              answer_url: toAbsUrl(answerLink),
              author_name: clean(authorNode?.textContent),
              answer_excerpt: clean(contentRoot?.innerText || contentRoot?.textContent),
              published_time_text: clean(timeNode?.textContent),
              published_time_datetime: timeNode?.getAttribute('datetime') || '',
              voteup_text: clean(voteButton?.textContent),
              voteup_count: parseCount(voteButton?.textContent),
              comment_text: clean(commentButton?.textContent),
              comment_count: parseCount(commentButton?.textContent),
            };
          }).filter((item) => item.question_title || item.answer_excerpt);
        }
        """
    )


def extract_answer_records_from_api_payload(payload: object) -> list[dict[str, object]]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, list):
        return []

    answers: list[dict[str, object]] = []
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            continue
        question = item.get("question") if isinstance(item.get("question"), dict) else {}
        author = item.get("author") if isinstance(item.get("author"), dict) else {}
        excerpt = item.get("excerpt") or item.get("excerpt_new") or item.get("content") or ""
        created_time = item.get("created_time")
        updated_time = item.get("updated_time")
        question_url = question.get("url") or ""
        answer_url = item.get("url") or ""
        question_id = question.get("id")
        answer_id = item.get("id")
        answers.append(
            {
                "index": index,
                "question_id": question_id,
                "answer_id": answer_id,
                "question_title": question.get("title", ""),
                "question_url": question_url,
                "answer_url": answer_url,
                "web_answer_url": build_web_answer_url(question_id, answer_id),
                "author_name": author.get("name", ""),
                "answer_excerpt": excerpt,
                "created_time": created_time,
                "updated_time": updated_time,
                "created_time_iso": datetime.fromtimestamp(created_time).astimezone().isoformat() if isinstance(created_time, int | float) else "",
                "updated_time_iso": datetime.fromtimestamp(updated_time).astimezone().isoformat() if isinstance(updated_time, int | float) else "",
                "voteup_count": item.get("voteup_count"),
                "comment_count": item.get("comment_count"),
                "thanks_count": item.get("thanks_count"),
            }
        )
    return answers


def build_web_answer_url(question_id: object, answer_id: object) -> str:
    if isinstance(question_id, int | str) and isinstance(answer_id, int | str):
        return f"https://www.zhihu.com/question/{question_id}/answer/{answer_id}"
    return ""


def html_to_text(html: str) -> str:
    text = re.sub(r"<br\\s*/?>", "\n", html)
    text = re.sub(r"</p>|</div>|</li>|</h[1-6]>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&nbsp;", " ")
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&").replace("&quot;", '"')
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def fetch_answer_detail(page, answer_id: int | str) -> dict[str, object]:
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
            "is_normal",
            "author",
            "question"
          ].join(",");
          const url = `https://www.zhihu.com/api/v4/answers/${answerId}?include=${include}`;
          const response = await fetch(url, {
            credentials: "include",
            headers: {
              "accept": "application/json, text/plain, */*"
            }
          });
          if (!response.ok) {
            throw new Error(`抓取回答详情失败: ${response.status} ${response.statusText}`);
          }
          return await response.json();
        }
        """,
        answer_id,
    )
    if not isinstance(payload, dict):
        raise RuntimeError(f"回答详情格式不对: {answer_id}")
    return payload


def enrich_answers_with_detail(page, answers: list[dict[str, object]]) -> list[dict[str, object]]:
    enriched: list[dict[str, object]] = []
    for item in answers:
        answer_id = item.get("answer_id")
        if not isinstance(answer_id, int | str):
            enriched.append(item)
            continue
        detail = fetch_answer_detail(page, answer_id)
        content_html = detail.get("content") if isinstance(detail.get("content"), str) else ""
        merged = dict(item)
        merged["question_id"] = detail.get("question", {}).get("id", item.get("question_id")) if isinstance(detail.get("question"), dict) else item.get("question_id")
        merged["question_title"] = detail.get("question", {}).get("title", item.get("question_title")) if isinstance(detail.get("question"), dict) else item.get("question_title")
        merged["author_name"] = detail.get("author", {}).get("name", item.get("author_name")) if isinstance(detail.get("author"), dict) else item.get("author_name")
        merged["voteup_count"] = detail.get("voteup_count", item.get("voteup_count"))
        merged["comment_count"] = detail.get("comment_count", item.get("comment_count"))
        merged["thanks_count"] = detail.get("thanks_count", item.get("thanks_count"))
        merged["created_time"] = detail.get("created_time", item.get("created_time"))
        merged["updated_time"] = detail.get("updated_time", item.get("updated_time"))
        created_time = merged.get("created_time")
        updated_time = merged.get("updated_time")
        merged["created_time_iso"] = datetime.fromtimestamp(created_time).astimezone().isoformat() if isinstance(created_time, int | float) else ""
        merged["updated_time_iso"] = datetime.fromtimestamp(updated_time).astimezone().isoformat() if isinstance(updated_time, int | float) else ""
        merged["full_content_html"] = content_html
        merged["full_content_text"] = html_to_text(content_html)
        merged["web_answer_url"] = build_web_answer_url(merged.get("question_id"), answer_id)
        enriched.append(merged)
    return enriched


def save_payload(payload: dict[str, object], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="最小测试：抓取知乎个人主页首屏回答")
    parser.add_argument("--url", default=DEFAULT_PROFILE_URL)
    parser.add_argument("--signin-url", default=DEFAULT_SIGNIN_URL)
    parser.add_argument("--profile-dir", type=Path, default=DEFAULT_PROFILE_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--login-wait", type=int, default=300)
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()

    args.profile_dir.mkdir(parents=True, exist_ok=True)
    answers_url = normalize_answers_url(args.url)
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
        api_payloads: list[dict[str, object]] = []

        def handle_response(response) -> None:
            url = response.url
            if "/api/v4/" not in url or "/answers" not in url:
                return
            try:
                payload = response.json()
            except Exception:
                return
            if not isinstance(payload, dict):
                return
            if not isinstance(payload.get("data"), list):
                return
            api_payloads.append(
                {
                    "url": url,
                    "payload": payload,
                }
            )

        page.on("response", handle_response)
        wait_for_login(page, args.signin_url, args.login_wait)
        page.goto(answers_url, wait_until="domcontentloaded", timeout=60_000)
        if is_login_page(page):
            raise RuntimeError("登录后仍然被重定向到登录页，请确认账号已完成登录")

        page.wait_for_selector(".List-item .ContentItem.AnswerItem", timeout=30_000)
        page.wait_for_timeout(2_000)
        answers: list[dict[str, object]] = []
        api_source_url = ""
        for item in api_payloads:
            payload_answers = extract_answer_records_from_api_payload(item["payload"])
            if payload_answers:
                answers = payload_answers
                api_source_url = str(item["url"])
                break
        if not answers:
            answers = extract_initial_answers(page)
        else:
            answers = enrich_answers_with_detail(page, answers)

        payload = {
            "source_profile_url": args.url,
            "source_answers_url": answers_url,
            "slug": slug,
            "fetched_at": datetime.now().astimezone().isoformat(),
            "api_source_url": api_source_url,
            "answer_count": len(answers),
            "answers": answers,
        }
        save_payload(payload, args.output)
        print(f"已保存 {len(answers)} 条回答到 {args.output}", flush=True)
    finally:
        context.close()


if __name__ == "__main__":
    main()
