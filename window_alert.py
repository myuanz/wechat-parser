import json
import os
import socket
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.request import Request, urlopen


WINDOW_MISSING_MESSAGE = "没有找到 title=微信 且 tray=False 的 xpra 窗口"
DEFAULT_WINDOW_ALERT_THRESHOLD = 10
DEFAULT_WINDOW_ALERT_COOLDOWN_SECONDS = 4 * 60 * 60
MISSING_COUNT_ENV = "WECHAT_WINDOW_MISSING_COUNT"
LAST_ALERT_AT_ENV = "WECHAT_WINDOW_LAST_ALERT_AT"


def _env_int(name: str) -> int:
    value = os.environ.get(name)
    return int(value) if value else 0


def _env_float(name: str) -> float | None:
    value = os.environ.get(name)
    return float(value) if value else None


@dataclass
class WindowAlert:
    webhook_url: str
    threshold: int = DEFAULT_WINDOW_ALERT_THRESHOLD
    cooldown_seconds: int = DEFAULT_WINDOW_ALERT_COOLDOWN_SECONDS

    @property
    def missing_count(self) -> int:
        return _env_int(MISSING_COUNT_ENV)

    @missing_count.setter
    def missing_count(self, value: int) -> None:
        os.environ[MISSING_COUNT_ENV] = str(value)

    @property
    def last_alert_at(self) -> float | None:
        return _env_float(LAST_ALERT_AT_ENV)

    @last_alert_at.setter
    def last_alert_at(self, value: float) -> None:
        os.environ[LAST_ALERT_AT_ENV] = str(value)

    def clear(self) -> None:
        os.environ.pop(MISSING_COUNT_ENV, None)
        os.environ.pop(LAST_ALERT_AT_ENV, None)

    def is_window_missing_error(self, error: RuntimeError) -> bool:
        return WINDOW_MISSING_MESSAGE in str(error)

    def on_window_missing(self, error: RuntimeError) -> None:
        self.missing_count = self.missing_count + 1
        print(f"微信窗口缺失: count={self.missing_count} error={error}", file=sys.stderr, flush=True)

        now = time.time()
        last_alert_at = self.last_alert_at
        cooled_down = last_alert_at is None or now - last_alert_at >= self.cooldown_seconds
        if self.missing_count < self.threshold or not cooled_down:
            return
        if not self.webhook_url:
            print("微信窗口缺失达到阈值，但未配置 FEISHU_WEBHOOK_URL", file=sys.stderr, flush=True)
            return

        last_alert = "无"
        if last_alert_at is not None:
            last_alert = datetime.fromtimestamp(last_alert_at, UTC).isoformat()
        self.send_feishu_text(
            "微信采集告警\n"
            f"主机: {socket.gethostname()}\n"
            f"事件: 连续 {self.missing_count} 次找不到微信窗口\n"
            f"错误: {error}\n"
            f"上次通知: {last_alert}"
        )
        self.last_alert_at = now

    def on_window_found(self) -> None:
        if self.missing_count == 0 and self.last_alert_at is None:
            return
        print(f"微信窗口已恢复，清空窗口告警状态: previous_count={self.missing_count}")
        self.clear()

    def send_feishu_text(self, text: str) -> None:
        payload = json.dumps(
            {"msg_type": "text", "content": {"text": text}},
            ensure_ascii=False,
        ).encode()
        request = Request(
            self.webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=10) as response:
            body = response.read().decode()
        print(f"飞书通知完成: status={response.status} body={body}")


window_alert = WindowAlert(webhook_url="")


def configure(webhook_url: str, threshold: int, cooldown_seconds: int) -> None:
    window_alert.webhook_url = webhook_url
    window_alert.threshold = threshold
    window_alert.cooldown_seconds = cooldown_seconds
