import fcntl
from datetime import datetime, timedelta
from pathlib import Path

from zhihu_sync import (
    DEFAULT_PROFILE_DIR,
    DEFAULT_REQUEST_PROFILE_URL,
    claim_next_pending_zhihu_task,
    cleanup_stale_running_zhihu_tasks,
    create_zhihu_task,
    read_latest_auto_check_task,
    refresh_following,
    run_today_updates,
    sync_zhihu,
    update_zhihu_task,
)


DAY_START_HOUR = 8
DAY_END_HOUR = 24
AUTO_CHECK_INTERVAL = timedelta(hours=4)
WORKER_LOCK_PATH = Path("/tmp/zhihu-worker.lock")


def now_local() -> datetime:
    return datetime.now().astimezone()


def in_auto_window(dt: datetime) -> bool:
    return DAY_START_HOUR <= dt.hour < DAY_END_HOUR


def auto_check_anchor_at(task: dict[str, object]) -> datetime | None:
    for key in ("started_at", "requested_at", "finished_at"):
        value = task.get(key)
        if isinstance(value, str) and value:
            return datetime.fromisoformat(value)
    return None


def try_acquire_worker_lock():
    lock_file = WORKER_LOCK_PATH.open("w")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        return None
    lock_file.write(str(now_local().timestamp()))
    lock_file.flush()
    return lock_file


def handle_request(request: dict[str, object]) -> dict[str, object]:
    task_type = str(request.get("task_type") or "")
    profile_url = str(request.get("profile_url") or DEFAULT_REQUEST_PROFILE_URL)
    requested_at = str(request.get("requested_at") or "")
    started_at = str(request.get("started_at") or now_local().isoformat())

    if task_type == "refresh_following":
        stats = refresh_following(profile_url=profile_url)
        return {
            "task_type": task_type,
            "profile_url": profile_url,
            "requested_at": requested_at,
            "started_at": started_at,
            "finished_at": now_local().isoformat(),
            "status": "done",
            **stats,
        }

    if task_type == "check_new":
        stats = sync_zhihu(profile_url=profile_url)
        return {
            "task_type": task_type,
            "profile_url": profile_url,
            "requested_at": requested_at,
            "started_at": started_at,
            "finished_at": now_local().isoformat(),
            "status": "done",
            **stats,
        }

    if task_type == "refresh_profile":
        script_result = run_today_updates(profile_url)
        return {
            "task_type": task_type,
            "profile_url": profile_url,
            "requested_at": requested_at,
            "started_at": started_at,
            "finished_at": now_local().isoformat(),
            "status": "done",
            **script_result,
        }

    raise RuntimeError(f"不支持的知乎任务类型: {task_type}")


def run_auto_check(last_auto_check: dict[str, object] | None) -> datetime | None:
    current = now_local()
    if not in_auto_window(current):
        return auto_check_anchor_at(last_auto_check) if last_auto_check is not None else None

    if last_auto_check is not None:
        last_status = str(last_auto_check.get("status") or "")
        last_auto_check_at = auto_check_anchor_at(last_auto_check)
        if last_status == "running":
            return last_auto_check_at
        if last_auto_check_at is not None and current - last_auto_check_at < AUTO_CHECK_INTERVAL:
            return last_auto_check_at

    task = create_zhihu_task(
        "auto_check_new",
        profile_url=DEFAULT_REQUEST_PROFILE_URL,
        status="running",
        started_at=current,
    )
    try:
        stats = sync_zhihu(profile_url=DEFAULT_REQUEST_PROFILE_URL)
        update_zhihu_task(
            int(task["id"]),
            status="done",
            finished_at=now_local(),
            result_payload=stats,
        )
        return current
    except Exception as error:
        update_zhihu_task(
            int(task["id"]),
            status="failed",
            finished_at=now_local(),
            error=str(error),
        )
        return current


def process_pending_request() -> bool:
    request = claim_next_pending_zhihu_task()
    if request is None:
        return False
    task_id = int(request["id"])
    try:
        result = handle_request(request)
        update_zhihu_task(
            task_id,
            status="done",
            finished_at=now_local(),
            result_payload={k: v for k, v in result.items() if k not in {"id", "task_type", "profile_url", "requested_at", "started_at", "finished_at", "status", "error"}},
        )
    except Exception as error:
        update_zhihu_task(task_id, status="failed", finished_at=now_local(), error=str(error))
    return True


def main() -> None:
    lock_file = try_acquire_worker_lock()
    if lock_file is None:
        return
    cleanup_stale_running_zhihu_tasks(task_types=("check_new", "auto_check_new"))
    if process_pending_request():
        return
    run_auto_check(read_latest_auto_check_task())


if __name__ == "__main__":
    main()
