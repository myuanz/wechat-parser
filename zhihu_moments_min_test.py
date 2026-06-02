import argparse
import json
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from cloakbrowser import launch_persistent_context
from playwright.sync_api import Error as PlaywrightError

from zhihu_following_collect import is_login_page, wait_for_login


DEFAULT_FOLLOW_URL = "https://www.zhihu.com/follow"
DEFAULT_SIGNIN_URL = "https://www.zhihu.com/signin"
DEFAULT_PROFILE_DIR = Path(__file__).with_name("browser_profiles") / "zhihu"
DEFAULT_OUTPUT = Path(__file__).with_name("dumps") / "zhihu_moments_min_test.json"
DEFAULT_FORMATTED_OUTPUT = Path(__file__).with_name("dumps") / "zhihu_moments_formatted_items.json"


def first_dict(items: object) -> dict[str, object]:
    if not isinstance(items, list):
        return {}
    for item in items:
        if isinstance(item, dict):
            return item
    return {}


def int_time(value: object) -> int | None:
    return value if isinstance(value, int) else None


def text_value(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def question_url(question_id: object, answer_id: object) -> str:
    if isinstance(question_id, int | str) and isinstance(answer_id, int | str):
        return f"https://www.zhihu.com/question/{question_id}/answer/{answer_id}"
    return ""


def format_moment_item(item: dict[str, object]) -> dict[str, object] | None:
    target = item.get("target") if isinstance(item.get("target"), dict) else {}
    target_type = text_value(target.get("type") or item.get("type"))
    if target_type not in {"pin", "answer", "article"}:
        return None

    author = target.get("author") if isinstance(target.get("author"), dict) else first_dict(item.get("actors"))
    question = target.get("question") if isinstance(target.get("question"), dict) else {}
    target_id = target.get("id")

    if target_type == "pin":
        title = text_value(target.get("excerpt_title"))
        content_html = text_value(target.get("content_html") or target.get("content"))
        excerpt = title
        content_url = text_value(target.get("url"))
        created_time = int_time(target.get("created"))
        updated_time = int_time(target.get("updated"))
    elif target_type == "answer":
        title = text_value(question.get("title"))
        content_html = text_value(target.get("content"))
        excerpt = text_value(target.get("excerpt_new") or target.get("excerpt"))
        content_url = question_url(question.get("id"), target_id) or text_value(target.get("url"))
        created_time = int_time(target.get("created_time"))
        updated_time = int_time(target.get("updated_time"))
    else:
        title = text_value(target.get("title") or target.get("excerpt_title"))
        content_html = text_value(target.get("content"))
        excerpt = text_value(target.get("excerpt_new") or target.get("excerpt"))
        content_url = text_value(target.get("url"))
        created_time = int_time(target.get("created"))
        updated_time = int_time(target.get("updated"))

    return {
        "moment_id": text_value(item.get("id")),
        "moment_offset": item.get("offset"),
        "verb": text_value(item.get("verb")),
        "action_text": text_value(item.get("action_text")),
        "content_type": target_type,
        "content_id": target_id,
        "title": title,
        # "excerpt": excerpt,
        "content_html": content_html,
        "content_url": content_url,
        "created_time": created_time,
        "updated_time": updated_time,
        "author_name": text_value(author.get("name")),
        # "author_slug": text_value(author.get("url_token")),
        "author_url": text_value(author.get("url")),
        # "author_headline": text_value(author.get("headline")),
        # "author_avatar_url": text_value(author.get("avatar_url")),
        "is_following": author.get("is_following"),
        "question_id": question.get("id"),
        "question_title": text_value(question.get("title")),
        # "comment_count": target.get("comment_count"),
        # "like_count": target.get("like_count"),
        # "voteup_count": target.get("voteup_count"),
        # "favorite_count": target.get("favorite_count"),
        # "repin_count": target.get("repin_count"),
        # "page_view_count": target.get("page_view_count"),
        # "is_deleted": target.get("is_deleted"),
        # "state": target.get("state"),
    }


def format_moments_payload(payload: object) -> list[dict[str, object]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return []
    items: list[dict[str, object]] = []
    for item in payload["data"]:
        if not isinstance(item, dict):
            continue
        formatted = format_moment_item(item)
        if formatted is not None:
            items.append(formatted)
    return items


def fetch_moments(page, limit: int, offset: str, moment_start_offset: str, page_num: int) -> dict[str, object]:
    params: dict[str, str | int] = {"limit": limit, "desktop": "true"}
    if offset:
        params["offset"] = offset
    if moment_start_offset:
        params["moment_start_offset"] = moment_start_offset
    if page_num:
        params["page_num"] = page_num
    url = f"https://www.zhihu.com/api/v3/moments?{urlencode(params)}"

    result = page.evaluate(
        """
        async (url) => {
          const response = await fetch(url, {
            method: "GET",
            credentials: "include",
            headers: {
              "accept": "application/json, text/plain, */*"
            }
          });
          return {
            request_url: url,
            status: response.status,
            ok: response.ok,
            status_text: response.statusText,
            payload: await response.json()
          };
        }
        """,
        url,
    )
    if not isinstance(result, dict):
        raise RuntimeError("moments 接口返回格式不对")
    return result


def launch_zhihu_context(profile_dir: Path, headless: bool):
    try:
        return launch_persistent_context(
            profile_dir,
            headless=headless,
            locale="zh-CN",
            timezone="Asia/Shanghai",
            humanize=True,
            viewport={"width": 1280, "height": 900},
        )
    except PlaywrightError as error:
        lock_path = profile_dir / "SingletonLock"
        lock_target = lock_path.readlink() if lock_path.exists() else ""
        raise RuntimeError(
            f"无法打开知乎浏览器登录态目录：{profile_dir}\n"
            f"如果已有 CloakBrowser/Chromium 正在使用这个目录，请先关闭它再运行。\n"
            f"当前 SingletonLock: {lock_target}"
        ) from error


def save_formatted_items_file(input_path: Path = DEFAULT_OUTPUT, output_path: Path = DEFAULT_FORMATTED_OUTPUT) -> int:
    data = json.loads(input_path.read_text(encoding="utf-8"))
    formatted_items = format_moments_payload(data.get("payload"))
    output = {
        "fetched_at": data.get("fetched_at"),
        "request_url": data.get("request_url"),
        "count": len(formatted_items),
        "formatted_items": formatted_items,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(formatted_items)


def main() -> None:
    parser = argparse.ArgumentParser(description="最小测试：抓取知乎关注页最新动态 moments API")
    parser.add_argument("--follow-url", default=DEFAULT_FOLLOW_URL)
    parser.add_argument("--signin-url", default=DEFAULT_SIGNIN_URL)
    parser.add_argument("--profile-dir", type=Path, default=DEFAULT_PROFILE_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--formatted-output", type=Path, default=DEFAULT_FORMATTED_OUTPUT)
    parser.add_argument("--format-only", action="store_true")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--offset", default="")
    parser.add_argument("--moment-start-offset", default="")
    parser.add_argument("--page-num", type=int, default=0)
    parser.add_argument("--login-wait", type=int, default=300)
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()

    args.output.parent.mkdir(parents=True, exist_ok=True)

    if args.format_only:
        formatted_count = save_formatted_items_file(args.output, args.formatted_output)
        print(f"已保存 {args.formatted_output}")
        print(f"formatted_count={formatted_count}")
        return

    args.profile_dir.mkdir(parents=True, exist_ok=True)

    context = launch_zhihu_context(args.profile_dir, args.headless)
    try:
        page = context.new_page()
        wait_for_login(page, args.signin_url, args.login_wait)
        page.goto(args.follow_url, wait_until="domcontentloaded", timeout=60_000)
        if is_login_page(page):
            raise RuntimeError("登录后仍然被重定向到登录页，请确认账号已完成登录")
        page.wait_for_timeout(1500)

        result = fetch_moments(page, args.limit, args.offset, args.moment_start_offset, args.page_num)
        formatted_items = format_moments_payload(result.get("payload"))
        output = {
            "fetched_at": datetime.now().astimezone().isoformat(),
            "page_url": page.url,
            "formatted_items": formatted_items,
            **result,
        }
        args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
        save_formatted_items_file(args.output, args.formatted_output)

        payload = result.get("payload")
        data_count = len(payload.get("data", [])) if isinstance(payload, dict) and isinstance(payload.get("data"), list) else 0
        print(f"已保存 {args.output}")
        print(f"已保存 {args.formatted_output}")
        print(f"status={result.get('status')} data_count={data_count} formatted_count={len(formatted_items)}")
    finally:
        context.close()


if __name__ == "__main__":
    main()
