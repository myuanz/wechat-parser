import json
from datetime import datetime, timedelta
from pathlib import Path

from zhihu_sync import (
    DEFAULT_PROFILE_DIR,
    DEFAULT_REQUEST_PROFILE_URL,
    clear_sync_request,
    read_sync_request,
    refresh_following,
    run_today_updates,
    sync_zhihu,
    write_sync_result,
)


DAY_START_HOUR = 8
DAY_END_HOUR = 23
AUTO_CHECK_INTERVAL = timedelta(hours=4)
DEFAULT_AUTO_STATE_PATH = Path(__file__).with_name("dumps") / "zhihu_auto_check_state.json"


def now_local() -> datetime:
    return datetime.now().astimezone()


def in_auto_window(dt: datetime) -> bool:
    return DAY_START_HOUR <= dt.hour < DAY_END_HOUR


def read_auto_check_state() -> datetime | None:
    if not DEFAULT_AUTO_STATE_PATH.exists():
        return None
    payload = json.loads(DEFAULT_AUTO_STATE_PATH.read_text(encoding="utf-8"))
    last_run_at = payload.get("last_auto_check_at")
    if not isinstance(last_run_at, str) or not last_run_at:
        return None
    return datetime.fromisoformat(last_run_at)


def write_auto_check_state(last_auto_check_at: datetime) -> None:
    DEFAULT_AUTO_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_AUTO_STATE_PATH.write_text(
        json.dumps({"last_auto_check_at": last_auto_check_at.isoformat()}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def handle_request(request: dict[str, object]) -> dict[str, object]:
    action = str(request.get("action") or "")
    profile_url = str(request.get("profile_url") or DEFAULT_REQUEST_PROFILE_URL)
    requested_at = str(request.get("requested_at") or "")
    started_at = now_local().isoformat()

    if action == "refresh-following":
        stats = refresh_following(profile_url=profile_url)
        return {
            "action": action,
            "profile_url": profile_url,
            "requested_at": requested_at,
            "started_at": started_at,
            "finished_at": now_local().isoformat(),
            "status": "done",
            **stats,
        }

    if action == "check-new":
        stats = sync_zhihu(profile_url=profile_url)
        return {
            "action": action,
            "profile_url": profile_url,
            "requested_at": requested_at,
            "started_at": started_at,
            "finished_at": now_local().isoformat(),
            "status": "done",
            **stats,
        }

    if action == "refresh-profile":
        script_result = run_today_updates(profile_url)
        return {
            "action": action,
            "profile_url": profile_url,
            "requested_at": requested_at,
            "started_at": started_at,
            "finished_at": now_local().isoformat(),
            "status": "done",
            **script_result,
        }

    raise RuntimeError(f"不支持的知乎请求动作: {action}")


def run_auto_check(last_auto_check_at: datetime | None) -> datetime | None:
    current = now_local()
    if not in_auto_window(current):
        return last_auto_check_at
    if last_auto_check_at is not None and current - last_auto_check_at < AUTO_CHECK_INTERVAL:
        return last_auto_check_at

    result = {
        "action": "auto-check-new",
        "profile_url": DEFAULT_REQUEST_PROFILE_URL,
        "started_at": current.isoformat(),
        "status": "running",
    }
    write_sync_result(result)
    try:
        stats = sync_zhihu(profile_url=DEFAULT_REQUEST_PROFILE_URL)
        write_sync_result(
            {
                "action": "auto-check-new",
                "profile_url": DEFAULT_REQUEST_PROFILE_URL,
                "started_at": current.isoformat(),
                "finished_at": now_local().isoformat(),
                "status": "done",
                **stats,
            }
        )
        return current
    except Exception as error:
        write_sync_result(
            {
                "action": "auto-check-new",
                "profile_url": DEFAULT_REQUEST_PROFILE_URL,
                "started_at": current.isoformat(),
                "finished_at": now_local().isoformat(),
                "status": "failed",
                "error": str(error),
            }
        )
        return last_auto_check_at


def process_pending_request() -> bool:
    request = read_sync_request()
    if request is None or str(request.get("status") or "") != "pending":
        return False
    write_sync_result(
        {
            "action": request.get("action"),
            "profile_url": request.get("profile_url"),
            "requested_at": request.get("requested_at"),
            "started_at": now_local().isoformat(),
            "status": "running",
        }
    )
    try:
        result = handle_request(request)
        write_sync_result(result)
    except Exception as error:
        write_sync_result(
            {
                "action": request.get("action"),
                "profile_url": request.get("profile_url"),
                "requested_at": request.get("requested_at"),
                "started_at": now_local().isoformat(),
                "finished_at": now_local().isoformat(),
                "status": "failed",
                "error": str(error),
            }
        )
    finally:
        clear_sync_request()
    return True


def main() -> None:
    if process_pending_request():
        return
    last_auto_check_at = read_auto_check_state()
    updated_last_auto_check_at = run_auto_check(last_auto_check_at)
    if updated_last_auto_check_at is not None and updated_last_auto_check_at != last_auto_check_at:
        write_auto_check_state(updated_last_auto_check_at)


if __name__ == "__main__":
    main()
