import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from cloakbrowser import launch_persistent_context


DEFAULT_PROFILE_URL = "https://www.zhihu.com/people/deng-cheng-chen-17"
DEFAULT_SIGNIN_URL = "https://www.zhihu.com/signin"
DEFAULT_PROFILE_DIR = Path(__file__).with_name("browser_profiles") / "zhihu"
DEFAULT_OUTPUT = Path("/srv/samba/share") / "zhihu_profile_pins_min_test.json"


def normalize_pins_url(profile_url: str) -> str:
    return profile_url.rstrip("/") + "/pins"


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
    text = re.sub(r"<br\\s*/?>", "\n", html)
    text = re.sub(r"</p>|</div>|</li>|</h[1-6]>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&nbsp;", " ")
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&").replace("&quot;", '"')
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def extract_pin_records_from_api_payload(payload: object) -> list[dict[str, object]]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, list):
        return []

    pins: list[dict[str, object]] = []
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            continue
        author = item.get("author") if isinstance(item.get("author"), dict) else {}
        content_html = item.get("content_html") if isinstance(item.get("content_html"), str) else ""
        if not content_html:
            content_list = item.get("content")
            if isinstance(content_list, list):
                html_parts = []
                for part in content_list:
                    if isinstance(part, dict) and isinstance(part.get("content"), str):
                        html_parts.append(part["content"])
                content_html = "\n".join(html_parts)
        created = item.get("created")
        updated = item.get("updated")
        pin_id = item.get("id")
        pins.append(
            {
                "index": index,
                "pin_id": pin_id,
                "author_name": author.get("name", ""),
                "content_html": content_html,
                "content_text": html_to_text(content_html),
                "excerpt_title": item.get("excerpt_title", ""),
                "created": created,
                "updated": updated,
                "created_iso": datetime.fromtimestamp(created).astimezone().isoformat() if isinstance(created, int | float) else "",
                "updated_iso": datetime.fromtimestamp(updated).astimezone().isoformat() if isinstance(updated, int | float) else "",
                "comment_count": item.get("comment_count"),
                "voteup_count": item.get("voteup_count"),
                "like_count": item.get("like_count"),
                "reaction_count": item.get("reaction_count"),
                "web_pin_url": build_web_pin_url(pin_id),
                "raw": item,
            }
        )
    return pins


def build_web_pin_url(pin_id: object) -> str:
    if isinstance(pin_id, int | str):
        return f"https://www.zhihu.com/pin/{pin_id}"
    return ""


def main() -> None:
    parser = argparse.ArgumentParser(description="最小测试：抓取知乎个人主页首批想法")
    parser.add_argument("--url", default=DEFAULT_PROFILE_URL)
    parser.add_argument("--signin-url", default=DEFAULT_SIGNIN_URL)
    parser.add_argument("--profile-dir", type=Path, default=DEFAULT_PROFILE_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--login-wait", type=int, default=300)
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()

    args.profile_dir.mkdir(parents=True, exist_ok=True)
    pins_url = normalize_pins_url(args.url)
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
            if "/api/v4/" not in url:
                return
            if "/pins/" not in url and "/moments/" not in url and "/posts/" not in url:
                return
            try:
                payload = response.json()
            except Exception:
                return
            if not isinstance(payload, dict):
                return
            if not isinstance(payload.get("data"), list):
                return
            api_payloads.append({"url": url, "payload": payload})

        page.on("response", handle_response)
        wait_for_login(page, args.signin_url, args.login_wait)
        page.goto(pins_url, wait_until="domcontentloaded", timeout=60_000)
        if is_login_page(page):
            raise RuntimeError("登录后仍然被重定向到登录页，请确认账号已完成登录")

        page.wait_for_timeout(4_000)

        pins: list[dict[str, object]] = []
        api_source_url = ""
        for item in api_payloads:
            payload_pins = extract_pin_records_from_api_payload(item["payload"])
            if payload_pins:
                pins = payload_pins
                api_source_url = str(item["url"])
                break

        payload = {
            "source_profile_url": args.url,
            "source_pins_url": pins_url,
            "slug": slug,
            "fetched_at": datetime.now().astimezone().isoformat(),
            "api_source_url": api_source_url,
            "pin_count": len(pins),
            "pins": pins,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已保存 {len(pins)} 条想法到 {args.output}", flush=True)
    finally:
        context.close()


if __name__ == "__main__":
    main()
