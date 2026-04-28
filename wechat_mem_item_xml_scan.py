import argparse
import html
import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import parse_qs

from common import discover_wechat_pids, read_maps, region_wanted, role_from_cmdline


MMREADER_ANCHOR = '<category type="20" count="'
COUNT_RE = re.compile(r'<category type="20" count="(?P<count>\d+)">')
PUBLISHER_RE = re.compile(
    r'<publisher>\s*<username><!\[CDATA\[(?P<username>.*?)\]\]></username>\s*<nickname><!\[CDATA\[(?P<nickname>.*?)\]\]></nickname>',
    re.S,
)
ITEM_RE = re.compile(r"<item>(?P<body>.*?)</item>", re.S)
CDATA_FIELD_TEMPLATES = {
    "title": re.compile(r"<title><!\[CDATA\[(.*?)\]\]></title>", re.S),
    "url": re.compile(r"<url><!\[CDATA\[(.*?)\]\]></url>", re.S),
    "summary": re.compile(r"<summary><!\[CDATA\[(.*?)\]\]></summary>", re.S),
    "digest": re.compile(r"<digest><!\[CDATA\[(.*?)\]\]></digest>", re.S),
}
PUB_TIME_RE = re.compile(r"<pub_time>(.*?)</pub_time>")


@dataclass
class ItemXml:
    pid: int
    role: str
    addr: str
    encoding: str
    title: str
    url: str
    source_name: str
    summary: str
    digest: str
    pub_time: str
    mid: str
    idx: str
    biz: str
    source_username: str
    total_count: int
    show_name: str
    source: str


def clean_text(value: str) -> str:
    value = html.unescape(value).replace("\x00", "")
    return " ".join(value.split())


def clean_url(url: str) -> str:
    url = html.unescape(url).replace(r"\/", "/")
    if "#rd" in url:
        return url[: url.index("#rd") + 3]
    return url


def parse_link(url: str) -> tuple[str, str, str]:
    query_text = url.partition("?")[2].partition("#")[0]
    query = parse_qs(query_text)
    return query.get("mid", [""])[0], query.get("idx", [""])[0], query.get("__biz", [""])[0]


def xml_block_end(text: str, start: int) -> int:
    mmreader_end = text.find("</mmreader>", start)
    if mmreader_end < 0:
        return -1
    return mmreader_end + len("</mmreader>")


def xml_block_end_bytes(data: bytes, start: int) -> int:
    end_tag = b"</mmreader>"
    mmreader_end = data.find(end_tag, start)
    if mmreader_end < 0:
        return -1
    return mmreader_end + len(end_tag)


def extract_xml_cdata(pattern: re.Pattern[str], text: str) -> str:
    match = pattern.search(text)
    return clean_text(match.group(1)) if match else ""


def load_mmreader_blocks(text: str) -> list[tuple[int, str]]:
    blocks: list[tuple[int, str]] = []
    pos = text.find(MMREADER_ANCHOR)
    while pos >= 0:
        end = xml_block_end(text, pos)
        if end > pos:
            blocks.append((pos, text[pos:end]))
        pos = text.find(MMREADER_ANCHOR, pos + 1)
    return blocks


def load_mmreader_blocks_bytes(data: bytes) -> list[tuple[int, bytes]]:
    blocks: list[tuple[int, bytes]] = []
    anchor = MMREADER_ANCHOR.encode()
    pos = data.find(anchor)
    while pos >= 0:
        end = xml_block_end_bytes(data, pos)
        if end > pos:
            blocks.append((pos, data[pos:end]))
        pos = data.find(anchor, pos + 1)
    return blocks


def title_addr_in_body(body: str, body_bytes: bytes) -> int | None:
    title = extract_xml_cdata(CDATA_FIELD_TEMPLATES["title"], body)
    if not title:
        return None
    prefix = b"<title><![CDATA["
    title_bytes = title.encode()
    pos = body_bytes.find(prefix + title_bytes)
    if pos < 0:
        return None
    return pos + len(prefix)


def scan_mmreader_text(pid: int, role: str, base: int, data: bytes) -> list[ItemXml]:
    rows: list[ItemXml] = []
    for offset, block_bytes in load_mmreader_blocks_bytes(data):
        block = block_bytes.decode("utf-8", errors="ignore")
        count_match = COUNT_RE.search(block)
        publisher_match = PUBLISHER_RE.search(block)
        if not count_match or not publisher_match:
            continue
        total_count = int(count_match.group("count"))
        source_username = clean_text(publisher_match.group("username"))
        show_name = clean_text(publisher_match.group("nickname"))
        search_from = 0
        for index, match in enumerate(ITEM_RE.finditer(block), start=1):
            body = match.group("body")
            body_bytes = body.encode()
            body_pos = block_bytes.find(body_bytes, search_from)
            if body_pos < 0:
                body_pos = block_bytes.find(body_bytes)
            url = clean_url(extract_xml_cdata(CDATA_FIELD_TEMPLATES["url"], body))
            mid, idx, biz = parse_link(url) if url else ("", "", "")
            title = extract_xml_cdata(CDATA_FIELD_TEMPLATES["title"], body)
            if not (title and mid):
                continue
            summary = extract_xml_cdata(CDATA_FIELD_TEMPLATES["summary"], body)
            digest = extract_xml_cdata(CDATA_FIELD_TEMPLATES["digest"], body)
            pub_time_match = PUB_TIME_RE.search(body)
            pub_time = clean_text(pub_time_match.group(1)) if pub_time_match else ""
            title_rel = title_addr_in_body(body, body_bytes)
            title_bytes = title.encode()
            if body_pos >= 0:
                search_from = body_pos + len(body_bytes)
            if body_pos >= 0 and title_rel is not None:
                addr = hex(base + offset + body_pos + title_rel)
            elif body_pos >= 0:
                title_pos = block_bytes.find(title_bytes, body_pos)
                addr = hex(base + offset + (title_pos if title_pos >= 0 else body_pos))
            else:
                title_pos = block_bytes.find(title_bytes, search_from)
                if title_pos < 0:
                    title_pos = block_bytes.find(title_bytes)
                addr = hex(base + offset + (title_pos if title_pos >= 0 else 0))
            rows.append(
                ItemXml(
                    pid=pid,
                    role=role,
                    addr=addr,
                    encoding="utf-8",
                    title=title,
                    url=url,
                    source_name=show_name or source_username,
                    summary=summary,
                    digest=digest,
                    pub_time=pub_time,
                    mid=mid,
                    idx=idx or str(index),
                    biz=biz,
                    source_username=source_username,
                    total_count=total_count,
                    show_name=show_name,
                    source="mmreader-xml",
                )
            )
    return rows


def scan_memory_block(pid: int, role: str, base: int, block: bytes) -> list[ItemXml]:
    if MMREADER_ANCHOR.encode() not in block:
        return []
    return scan_mmreader_text(pid, role, base, block)


def scan_pid(pid: int, all_regions: bool) -> list[ItemXml]:
    role = role_from_cmdline(pid)
    rows: list[ItemXml] = []
    chunk_size = 8 * 1024 * 1024
    overlap = chunk_size
    with open(f"/proc/{pid}/mem", "rb", buffering=0) as mem:
        for region in read_maps(pid):
            if not region_wanted(region, all_regions):
                continue
            pos = region.start
            carry = b""
            while pos < region.end:
                size = min(chunk_size, region.end - pos)
                try:
                    mem.seek(pos)
                    data = mem.read(size)
                except OSError:
                    carry = b""
                    pos += size
                    continue
                block = carry + data
                base = pos - len(carry)
                rows.extend(scan_memory_block(pid, role, base, block))
                carry = block[-overlap:]
                pos += size
    return rows


def dedupe(rows: list[ItemXml]) -> list[ItemXml]:
    best: dict[tuple[str, str, str], ItemXml] = {}
    for row in rows:
        key = (row.biz, row.mid, row.idx)
        old = best.get(key)
        if old is None:
            best[key] = row
            continue
        old_score = (bool(old.show_name), bool(old.source_username), old.total_count)
        new_score = (bool(row.show_name), bool(row.source_username), row.total_count)
        if new_score > old_score:
            best[key] = row

    return sorted(best.values(), key=lambda row: (-int(row.pub_time or 0), row.mid, row.idx))


def main() -> None:
    parser = argparse.ArgumentParser(description="扫描微信主进程内存里的公众号文章，默认导出所有能找到的 XML 文章")
    parser.add_argument("pids", nargs="*", type=int)
    parser.add_argument("--all-regions", action="store_true")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    rows: list[ItemXml] = []
    pids = args.pids or [pid for pid in discover_wechat_pids() if role_from_cmdline(pid) == "wechat-main"]
    for pid in pids:
        if Path(f"/proc/{pid}/mem").exists():
            current = scan_pid(pid, args.all_regions)
            print(f"pid={pid} role={role_from_cmdline(pid)} item_xml={len(current)}")
            rows.extend(current)

    result = dedupe(rows)
    out = Path(args.out or f"dumps/wechat_item_xml_{time.strftime('%Y%m%d_%H%M%S')}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps([asdict(row) for row in result], ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"out={out}")
    print(f"item_xml={len(result)}")
    for row in result:
        print(
            f"pid={row.pid} mid={row.mid} idx={row.idx} total={row.total_count} "
            f"source={row.show_name or row.source_name or row.source_username} title={row.title}"
        )
        print(f"  {row.url}")


if __name__ == "__main__":
    main()
