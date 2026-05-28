import argparse
import json
import random
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

from cloakbrowser import launch_persistent_context


DEFAULT_FOLLOWING_URL = "https://www.zhihu.com/people/bu-ye-cheng-76/following"
DEFAULT_SIGNIN_URL = "https://www.zhihu.com/signin"
DEFAULT_PROFILE_DIR = Path(__file__).with_name("browser_profiles") / "zhihu"
DEFAULT_OUTPUT = Path("/srv/samba/share") / "zhihu_following_latest.json"


def normalize_people_url(href: str) -> str | None:
    url = urljoin("https://www.zhihu.com", href)
    parsed = urlparse(url)
    if parsed.netloc != "www.zhihu.com":
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2 or parts[0] != "people":
        return None
    return f"https://www.zhihu.com/people/{parts[1]}"


def extract_following(page) -> list[dict[str, str]]:
    rows = page.evaluate(
        """
        () => Array.from(document.querySelectorAll('a[href*="/people/"]')).map((a) => {
          const card = a.closest('.List-item, .ContentItem, .UserItem, [class*="List-item"]') || a.parentElement;
          const name = (a.textContent || '').trim();
          const headlineEl = card && card.querySelector('.ContentItem-headline, .UserItem-headline, [class*="headline"]');
          const avatarEl = card && card.querySelector('img.Avatar, img[class*="Avatar"]');
          return {
            name,
            href: a.getAttribute('href') || '',
            headline: headlineEl ? (headlineEl.textContent || '').trim() : '',
            avatar: avatarEl ? (avatarEl.getAttribute('src') || '') : '',
          };
        })
        """
    )

    users: dict[str, dict[str, str]] = {}
    for row in rows:
        url = normalize_people_url(row["href"])
        if url is None:
            continue
        name = row["name"].strip()
        slug = url.rstrip("/").split("/")[-1]
        if not name or name in {"关注", "取消关注"}:
            name = slug
        users[url] = {
            "name": name,
            "url": url,
            "slug": slug,
            "headline": row["headline"].strip(),
            "avatar": row["avatar"].strip(),
        }
    return sorted(users.values(), key=lambda item: item["url"])


def is_login_page(page) -> bool:
    url = page.url.lower()
    if "signin" in url or "login" in url:
        return True
    return page.locator('input[type="tel"], input[name="phone"], input[placeholder*="手机号"], input[placeholder*="密码"]').count() > 0


def wait_for_login(page, signin_url: str, seconds: int) -> None:
    page.goto(signin_url, wait_until="domcontentloaded", timeout=60_000)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not is_login_page(page):
            return
        page.wait_for_timeout(1000)
    raise RuntimeError("知乎仍在登录页，请先在打开的浏览器里完成登录")


def scroll_to_end(page, max_idle_scrolls: int) -> list[dict[str, str]]:
    seen_count = 0
    idle_count = 0
    while idle_count < max_idle_scrolls:
        users = extract_following(page)
        if len(users) > seen_count:
            seen_count = len(users)
            idle_count = 0
            print(f"已发现 {seen_count} 个关注用户", flush=True)
        else:
            idle_count += 1

        page.mouse.wheel(0, random.randint(900, 1500))
        page.wait_for_timeout(random.randint(900, 1500))

    return extract_following(page)


def find_next_page_control(page):
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(800)
    candidates = [
        'button:has-text("下一页")',
        'a:has-text("下一页")',
        '[aria-label*="下一页"]',
        '[aria-label*="next"]',
    ]
    for selector in candidates:
        loc = page.locator(selector)
        if loc.count() == 0:
            continue
        for idx in range(loc.count()):
            item = loc.nth(idx)
            if item.is_visible():
                return item
    return None


def click_control(control) -> None:
    control.evaluate("(el) => el.click()")


def page_signature(page) -> str:
    urls = [user["url"] for user in extract_following(page)]
    return page.url + "|" + "|".join(urls)


def is_disabled(control) -> bool:
    if control.is_disabled():
        return True
    aria_disabled = control.get_attribute("aria-disabled")
    class_name = control.get_attribute("class") or ""
    return aria_disabled == "true" or "disabled" in class_name.lower()


def collect_all_following(page) -> list[dict[str, str]]:
    users: dict[str, dict[str, str]] = {}
    page_no = 1
    while True:
        page.wait_for_timeout(1500)
        page_users = extract_following(page)
        for user in page_users:
            users[user["url"]] = user
        print(f"第 {page_no} 页，累计 {len(users)} 个关注用户", flush=True)

        next_control = find_next_page_control(page)
        if next_control is None:
            break
        if is_disabled(next_control):
            break

        current_signature = page_signature(page)
        click_control(next_control)
        page.wait_for_timeout(1500)
        page.wait_for_function(
            """
            (prev) => {
              const urls = Array.from(document.querySelectorAll('a[href*="/people/"]'))
                .map((a) => new URL(a.getAttribute('href'), location.href).href)
                .join('|');
              return location.href + '|' + urls !== prev;
            }
            """,
            arg=current_signature,
            timeout=30_000,
        )
        page_no += 1

    return sorted(users.values(), key=lambda item: item["url"])


def save_users(users: list[dict[str, str]], output: Path, source_url: str) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "source_url": source_url,
        "fetched_at": datetime.now().astimezone().isoformat(),
        "total": len(users),
        "users": users,
    }
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    snapshot = output.with_name(f"zhihu_following_{stamp}.json")
    snapshot.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已保存 {len(users)} 个关注用户到 {output}", flush=True)
    print(f"快照文件: {snapshot}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="抓取知乎关注列表")
    parser.add_argument("--url", default=DEFAULT_FOLLOWING_URL)
    parser.add_argument("--signin-url", default=DEFAULT_SIGNIN_URL)
    parser.add_argument("--profile-dir", type=Path, default=DEFAULT_PROFILE_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--max-idle-scrolls", type=int, default=8)
    parser.add_argument("--login-wait", type=int, default=300)
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()

    args.profile_dir.mkdir(parents=True, exist_ok=True)
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
        wait_for_login(page, args.signin_url, args.login_wait)
        page.goto(args.url, wait_until="domcontentloaded", timeout=60_000)
        if is_login_page(page):
            raise RuntimeError("登录后仍然被重定向到登录页，请确认账号已完成登录")
        page.wait_for_timeout(2_000)
        users = collect_all_following(page)
        save_users(users, args.output, args.url)
    finally:
        context.close()


if __name__ == "__main__":
    main()
