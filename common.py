from dataclasses import dataclass
from pathlib import Path


WECHAT_MAIN_MARKERS = (
    "/usr/bin/wechat",
    "/opt/wechat/wechat",
    "/.local/wechat-pkg/opt/wechat/wechat",
)


@dataclass
class Region:
    start: int
    end: int
    perms: str
    path: str


def read_cmdline(pid: int) -> str:
    data = Path(f"/proc/{pid}/cmdline").read_bytes()
    return data.replace(b"\x00", b" ").decode(errors="replace").strip()


def role_from_cmdline(pid: int) -> str:
    cmdline = read_cmdline(pid)
    if "--is-subscription-disorder" in cmdline:
        return "subscription-disorder-renderer"
    if "--type=renderer" in cmdline and "--wmpf-render-type=6" in cmdline:
        return "article-renderer"
    if "--type=renderer" in cmdline:
        return "renderer"
    if "WeChatAppEx" in cmdline and "--type=" not in cmdline:
        return "web-shell-browser"
    if any(cmdline.endswith(marker) or cmdline == marker for marker in WECHAT_MAIN_MARKERS):
        return "wechat-main"
    return "other"


def discover_wechat_pids() -> list[int]:
    pids: list[int] = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            cmdline = read_cmdline(int(proc.name))
        except OSError:
            continue
        if any(marker in cmdline for marker in WECHAT_MAIN_MARKERS) or "WeChatAppEx" in cmdline:
            if "crashpad_handler" not in cmdline and "--type=zygote" not in cmdline:
                pids.append(int(proc.name))
    return sorted(pids)


def read_maps(pid: int) -> list[Region]:
    regions: list[Region] = []
    for line in Path(f"/proc/{pid}/maps").read_text(errors="replace").splitlines():
        parts = line.split(maxsplit=5)
        if len(parts) < 5 or "r" not in parts[1]:
            continue
        start_s, end_s = parts[0].split("-")
        regions.append(Region(int(start_s, 16), int(end_s, 16), parts[1], parts[5] if len(parts) == 6 else ""))
    return regions


def region_wanted(region: Region, all_regions: bool) -> bool:
    if all_regions:
        return True
    path = region.path
    if not path:
        return True
    if path.startswith("[heap]"):
        return True
    if path.startswith("[anon:"):
        return any(name in path for name in ("v8", "partition_alloc", "scudo", "blink", "malloc"))
    return False
