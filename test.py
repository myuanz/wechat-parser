# %%
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class MemoryRegion:
    start: int
    end: int
    perms: str
    offset: int
    dev: str
    inode: int
    pathname: str

    @property
    def size(self) -> int:
        return self.end - self.start

    @property
    def is_readable(self) -> bool:
        return "r" in self.perms

# %%
def parse_maps(pid: int) -> list[MemoryRegion]:
    maps_path = Path(f"/proc/{pid}/maps")
    regions: list[MemoryRegion] = []

    for line in maps_path.read_text().splitlines():
        head, *tail = line.split(maxsplit=5)
        perms = tail[0]
        offset_text = tail[1]
        dev = tail[2]
        inode_text = tail[3]
        pathname = tail[4] if len(tail) == 5 else ""

        start_text, end_text = head.split("-", 1)
        regions.append(
            MemoryRegion(
                start=int(start_text, 16),
                end=int(end_text, 16),
                perms=perms,
                offset=int(offset_text, 16),
                dev=dev,
                inode=int(inode_text),
                pathname=pathname,
            )
        )

    return regions


def dump_region(mem_file, region: MemoryRegion, output_dir: Path, index: int) -> Path:
    mem_file.seek(region.start)
    data = mem_file.read(region.size)

    file_name = (
        f"{index:04d}_"
        f"{region.start:016x}-{region.end:016x}_"
        f"{region.perms}.bin"
    )
    output_path = output_dir / file_name
    output_path.write_bytes(data)
    return output_path



pid = 2220693
regions = parse_maps(pid)
print(regions)

index_lines: list[str] = []
mem_path = Path(f"/proc/{pid}/mem")

import re

ITEM_RE = re.compile(r"<item>(?P<body>.*?)</item>", re.DOTALL)

with mem_path.open("rb", buffering=0) as mem_file:
    readable_regions = [region for region in regions if region.is_readable]
    for region in readable_regions:
        if region.pathname.startswith("/usr/lib/"):
            continue
        if region.pathname.startswith("/usr/share/fonts"):
            continue

        mem_file.seek(region.start)
        try:
            data = mem_file.read(region.size)
        except Exception as e:
            # print(f"Failed to read {region.pathname}: {e}")
            continue
        # print(f"Read {len(data)} bytes from {region.pathname}")

        s = re.findall(ITEM_RE, data.decode(errors='ignore'))
        if not s:
            continue


        print(
            "\t".join(
                [
                    f"{region.start:#x}",
                    f"{region.end:#x}",
                    region.perms,
                    str(region.size),
                    region.pathname,
                ]
            )
        )
        print(s)
        # while (r := data.find(b'CDATA')) != -1:
        #     around = data[r-1000:r+1000]
        #     if b'static const char *mars_boost::detail::core_typeid_' in around:
        #         continue
        #     print(f"\tCDATA found at offset {r} in {region.pathname}")
        #     print(data[r-1000:r+1000].decode('utf-8', errors='replace'))
        #     # break
        
# %%
