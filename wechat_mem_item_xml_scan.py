from __future__ import annotations

import argparse
import html
import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from common import discover_wechat_pids, read_maps, region_wanted, role_from_cmdline


DIRECT_OBJECT_ANCHOR = '{"AppMsgId":'
ESCAPED_OBJECT_ANCHOR = r'{\"AppMsgId\":'
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
FIELD_RE = re.compile(
    r'"(?P<key>AppMsgId|DateTime|ContentUrl|Digest|Title|sourceUsername|showName|name|totalCnt|ItemIndex|pub_time)"\s*:\s*(?:"(?P<str>(?:\\.|[^"\\])*)"|(?P<num>-?\d+))'
)


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


def decode_json_string(value: str) -> str:
    try:
        return json.loads(f'"{value}"')
    except json.JSONDecodeError:
        return value.replace(r"\/", "/")


def clean_text(value: str) -> str:
    value = html.unescape(value).replace("\x00", "")
    return " ".join(value.split())


def clean_url(url: str) -> str:
    url = html.unescape(url).replace(r"\/", "/")
    if "#rd" in url:
        return url[: url.index("#rd") + 3]
    return url


def parse_link(url: str) -> tuple[str, str, str]:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    return query.get("mid", [""])[0], query.get("idx", [""])[0], query.get("__biz", [""])[0]


def object_end(text: str, start: int) -> int:
    depth = 0
    in_string = False
    escaped = False
    for pos in range(start, len(text)):
        char = text[pos]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return pos + 1
    return -1


def xml_block_end(text: str, start: int) -> int:
    mmreader_end = text.find("</mmreader>", start)
    if mmreader_end < 0:
        return -1
    return mmreader_end + len("</mmreader>")


def extract_xml_cdata(pattern: re.Pattern[str], text: str) -> str:
    match = pattern.search(text)
    return clean_text(match.group(1)) if match else ""


def load_direct_objects(text: str) -> list[tuple[int, str]]:
    objects: list[tuple[int, str]] = []
    pos = text.find(DIRECT_OBJECT_ANCHOR)
    while pos >= 0:
        end = object_end(text, pos)
        if end > pos:
            objects.append((pos, text[pos:end]))
        pos = text.find(DIRECT_OBJECT_ANCHOR, pos + 1)
    return objects


def load_mmreader_blocks(text: str) -> list[tuple[int, str]]:
    blocks: list[tuple[int, str]] = []
    pos = text.find(MMREADER_ANCHOR)
    while pos >= 0:
        end = xml_block_end(text, pos)
        if end > pos:
            blocks.append((pos, text[pos:end]))
        pos = text.find(MMREADER_ANCHOR, pos + 1)
    return blocks


def load_escaped_objects(text: str) -> list[tuple[int, str]]:
    objects: list[tuple[int, str]] = []
    pos = text.find(ESCAPED_OBJECT_ANCHOR)
    while pos >= 0:
        end = text.find(r'","reportInfo"', pos)
        if end < 0:
            end = min(len(text), pos + 24000)
        escaped = text[pos:end]
        try:
            decoded = json.loads(f'"{escaped}"')
        except json.JSONDecodeError:
            decoded = escaped.replace(r"\"", '"').replace(r"\/", "/")
        objects.append((pos, decoded))
        pos = text.find(ESCAPED_OBJECT_ANCHOR, pos + 1)
    return objects


def fields_from_segment(segment: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for match in FIELD_RE.finditer(segment):
        raw = match.group("str")
        value = decode_json_string(raw) if raw is not None else match.group("num") or ""
        fields.setdefault(match.group("key"), value)
    return fields


def row_from_fields(pid: int, role: str, addr: int, encoding: str, source: str, fields: dict[str, str]) -> ItemXml | None:
    url = clean_url(fields.get("ContentUrl", ""))
    mid, idx, biz = parse_link(url) if url else ("", "", "")
    title = clean_text(fields.get("Title", ""))
    if not (title and mid):
        return None
    return ItemXml(
        pid=pid,
        role=role,
        addr=hex(addr),
        encoding=encoding,
        title=title,
        url=url,
        source_name=clean_text(fields.get("name", "") or fields.get("showName", "")),
        summary="",
        digest=clean_text(fields.get("Digest", "")),
        pub_time=fields.get("pub_time", "") or fields.get("DateTime", ""),
        mid=mid,
        idx=idx or fields.get("ItemIndex", "") or "1",
        biz=biz,
        source_username=fields.get("sourceUsername", ""),
        total_count=int(fields.get("totalCnt") or 0),
        show_name=clean_text(fields.get("showName", "")),
        source=source,
    )


def scan_text(pid: int, role: str, base: int, encoding: str, text: str) -> list[ItemXml]:
    rows: list[ItemXml] = []
    for source, loader in (("direct-detail", load_direct_objects), ("escaped-detail", load_escaped_objects)):
        for offset, segment in loader(text):
            try:
                item = json.loads(segment)
            except json.JSONDecodeError:
                item = None
            if isinstance(item, dict):
                detail = item.get("detailInfo") or item.get("DetailInfo") or {}
                fields = {
                    "AppMsgId": str(item.get("AppMsgId") or ""),
                    "DateTime": str(item.get("DateTime") or ""),
                    "sourceUsername": str(item.get("sourceUsername") or ""),
                    "showName": str(item.get("showName") or ""),
                    "name": str(item.get("name") or ""),
                    "totalCnt": str(item.get("totalCnt") or ""),
                    "ContentUrl": str(detail.get("ContentUrl") or item.get("ContentUrl") or ""),
                    "Digest": str(detail.get("Digest") or item.get("Digest") or ""),
                    "Title": str(detail.get("Title") or item.get("Title") or ""),
                    "ItemIndex": str(detail.get("ItemIndex") or item.get("ItemIndex") or ""),
                }
            else:
                fields = fields_from_segment(segment)
            row = row_from_fields(pid, role, base + offset * (2 if encoding == "utf-16le" else 1), encoding, source, fields)
            if row:
                rows.append(row)
    return rows


def scan_mmreader_text(pid: int, role: str, base: int, encoding: str, text: str) -> list[ItemXml]:
    rows: list[ItemXml] = []
    for offset, block in load_mmreader_blocks(text):
        count_match = COUNT_RE.search(block)
        publisher_match = PUBLISHER_RE.search(block)
        if not count_match or not publisher_match:
            continue
        total_count = int(count_match.group("count"))
        source_username = clean_text(publisher_match.group("username"))
        show_name = clean_text(publisher_match.group("nickname"))
        for index, match in enumerate(ITEM_RE.finditer(block), start=1):
            body = match.group("body")
            url = clean_url(extract_xml_cdata(CDATA_FIELD_TEMPLATES["url"], body))
            mid, idx, biz = parse_link(url) if url else ("", "", "")
            title = extract_xml_cdata(CDATA_FIELD_TEMPLATES["title"], body)
            if not (title and mid):
                continue
            summary = extract_xml_cdata(CDATA_FIELD_TEMPLATES["summary"], body)
            digest = extract_xml_cdata(CDATA_FIELD_TEMPLATES["digest"], body)
            pub_time_match = PUB_TIME_RE.search(body)
            pub_time = clean_text(pub_time_match.group(1)) if pub_time_match else ""
            rows.append(
                ItemXml(
                    pid=pid,
                    role=role,
                    addr=hex(base + (offset + match.start()) * (2 if encoding == "utf-16le" else 1)),
                    encoding=encoding,
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




def scan_pid(pid: int, all_regions: bool) -> list[ItemXml]:
    role = role_from_cmdline(pid)
    rows: list[ItemXml] = []
    chunk_size = 8 * 1024 * 1024
    overlap = 1024 * 1024
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
                if b'AppMsgId' in block and b'ContentUrl' in block:
                    text = block.decode("utf-8", errors="ignore")
                    rows.extend(scan_text(pid, role, base, "utf-8", text))
                    rows.extend(scan_mmreader_text(pid, role, base, "utf-8", text))
                elif MMREADER_ANCHOR.encode() in block:
                    text = block.decode("utf-8", errors="ignore")
                    rows.extend(scan_mmreader_text(pid, role, base, "utf-8", text))
                if "AppMsgId".encode("utf-16le") in block and "ContentUrl".encode("utf-16le") in block:
                    text = block.decode("utf-16le", errors="ignore")
                    rows.extend(scan_text(pid, role, base, "utf-16le", text))
                carry = block[-overlap:]
                pos += size
    return rows


def dedupe(rows: list[ItemXml]) -> list[ItemXml]:
    best: dict[tuple[str, str, str], ItemXml] = {}
    for row in rows:
        key = (row.mid, row.idx, row.title)
        old = best.get(key)
        if old is None:
            best[key] = row
            continue
        old_score = (bool(old.show_name), bool(old.source_username), old.total_count, old.encoding == "utf-16le")
        new_score = (bool(row.show_name), bool(row.source_username), row.total_count, row.encoding == "utf-16le")
        if new_score > old_score:
            best[key] = row

    return sorted(best.values(), key=lambda row: (-int(row.pub_time or 0), row.mid, row.idx))


def main() -> None:
    parser = argparse.ArgumentParser(description="扫描微信内存里的公众号文章，默认导出所有能找到的文章")
    parser.add_argument("pids", nargs="*", type=int)
    parser.add_argument("--all-regions", action="store_true")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    pids = args.pids or discover_wechat_pids()

    rows: list[ItemXml] = []
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
    for row in result[:50]:
        print(
            f"pid={row.pid} mid={row.mid} idx={row.idx} total={row.total_count} "
            f"source={row.show_name or row.source_name or row.source_username} title={row.title}"
        )
        print(f"  {row.url}")


if __name__ == "__main__":
    main()
