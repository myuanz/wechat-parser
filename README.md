# wechat-parser

## `wechat_mem_item_xml_scan.py`

- 脚本使用 `mmreader/category/item` XML 扫描微信主进程 `wechat-main`
- 目标是导出主进程内存里能稳定找到的公众号文章 XML，优先保证结果干净

### 用法

```bash
uv run python wechat_mem_item_xml_scan.py --out dumps/wechat_item_xml.json
```

- 不传 PID 时，会自动发现微信主进程并只扫描它
- 如果显式传了 PID，就按传入的 PID 扫
- 也支持离线解析主进程 dump：

```bash
uv run python wechat_mem_item_xml_scan.py --dump-dir dumps/wechat_main_dump \
  --out dumps/wechat_item_xml_from_dump.json
```
