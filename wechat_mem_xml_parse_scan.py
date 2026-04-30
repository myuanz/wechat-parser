import argparse
import json
import time
from dataclasses import asdict
from pathlib import Path

from lxml import etree

from common import discover_wechat_pids, read_maps, region_wanted, role_from_cmdline
from wechat_mem_item_xml_scan import ItemXml, clean_url, dedupe, parse_link


MMREADER_START = b"<mmreader>"
MMREADER_END = b"</mmreader>"


def text_of(node: etree._Element | None, path: str) -> str:
    return node.findtext(path)


def is_clean_xml_text(data: bytes) -> bool:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return all(ch in "\t\n\r" or ord(ch) >= 32 for ch in text)


def parse_mmreader(pid: int, role: str, base: int, offset: int, data: bytes) -> list[ItemXml]:
    if not is_clean_xml_text(data):
        return []

    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=True)
    try:
        root = etree.fromstring(data, parser)
    except etree.XMLSyntaxError:
        return []

    rows: list[ItemXml] = []
    publisher = root.find("publisher")
    source_username = text_of(publisher, "username")
    for category in root.xpath(".//category[@type='20']"):
        total_count = int(category.get("count"))
        category_name = text_of(category, "name")

        for index, item in enumerate(category.findall("item"), start=1):
            title = text_of(item, "title")
            url = clean_url(text_of(item, "url"))
            mid, idx, biz = parse_link(url) if url else ("", "", "")
            if not (title and url and mid and idx and biz):
                continue

            title_bytes = title.encode()
            title_pos = data.find(title_bytes)
            addr = hex(base + offset + (title_pos if title_pos >= 0 else 0))
            rows.append(
                ItemXml(
                    pid=pid,
                    role=role,
                    addr=addr,
                    encoding="utf-8",
                    title=title,
                    url=url,
                    source_name=category_name,
                    summary=text_of(item, "summary"),
                    digest=text_of(item, "digest"),
                    pub_time=text_of(item, "pub_time"),
                    mid=mid,
                    idx=idx,
                    biz=biz,
                    source_username=source_username,
                    total_count=total_count,
                    show_name=category_name,
                    source="mmreader-lxml",
                )
            )
    return rows


def load_mmreader_xml_blocks(data: bytes) -> list[tuple[int, bytes]]:
    blocks: list[tuple[int, bytes]] = []
    pos = data.find(MMREADER_START)
    while pos >= 0:
        end = data.find(MMREADER_END, pos)
        if end > pos:
            end += len(MMREADER_END)
            blocks.append((pos, data[pos:end]))
        pos = data.find(MMREADER_START, pos + 1)
    return blocks


def scan_memory_block(pid: int, role: str, base: int, block: bytes) -> list[ItemXml]:
    rows: list[ItemXml] = []
    for offset, xml_bytes in load_mmreader_xml_blocks(block):
        rows.extend(parse_mmreader(pid, role, base, offset, xml_bytes))
    return rows


def scan_pid(pid: int, all_regions: bool) -> list[ItemXml]:
    role = role_from_cmdline(pid)
    rows: list[ItemXml] = []
    chunk_size = 8 * 1024 * 1024
    overlap = 8 * 1024 * 1024
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
                rows.extend(scan_memory_block(pid, role, pos - len(carry), block))
                carry = block[-overlap:]
                pos += size
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="用 lxml 解析微信主进程内存里的 mmreader XML")
    parser.add_argument("pids", nargs="*", type=int)
    parser.add_argument("--all-regions", action="store_true")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    rows: list[ItemXml] = []
    pids = args.pids or [pid for pid in discover_wechat_pids() if role_from_cmdline(pid) == "wechat-main"]
    for pid in pids:
        if Path(f"/proc/{pid}/mem").exists():
            current = scan_pid(pid, args.all_regions)
            print(f"pid={pid} role={role_from_cmdline(pid)} xml_items={len(current)}")
            rows.extend(current)

    result = dedupe(rows)
    out = Path(args.out or f"dumps/wechat_item_xml_lxml_{time.strftime('%Y%m%d_%H%M%S')}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps([asdict(row) for row in result], ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"out={out}")
    print(f"item_xml={len(result)}")
    for row in result:
        print(
            f"pid={row.pid} mid={row.mid} idx={row.idx} total={row.total_count} "
            f"source={row.source_name} title={row.title}"
        )
        print(f"  {row.url}")


if __name__ == "__main__":
    main()
