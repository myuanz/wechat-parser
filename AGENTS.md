# wechat-parser 开发约定

- 更新后，不要记录过去如何，不需要留下“不再xx”之类的描述
- 代码越少越好，最少的能解决问题的代码就是最好的代码

## systemd 服务

- `wechat-collector-server.service`：Web/API 服务
- `wechat-collector.service`：微信采集服务

## dclassql / ORM 偏好

1. 业务读写优先使用 dclassql ORM，也就是 `from dclassql import Client` 后通过 `client.article`、`client.article_content` 等表对象操作。
2. 不要在业务路径里手写一堆 `sqlite3` SQL 来绕过 ORM。
3. 允许使用原生 SQL 的场景：
   - ORM 当前还不支持或表达成本明显过高的聚合统计，例如 `api_stats` 里的 count / join count。
   - 一次性诊断、只读排查、临时验证。
4. 修改 `db_model.py` 后，必须重新生成 dclassql client：
   - `uv run dclassql -m db_model.py generate`
5. 修改模型字段后，同步数据库结构用 dclassql 的 schema push，不要在运行时加 `ensure_schema` 之类的常驻迁移兜底：
   - `uv run dclassql -m db_model.py push-db --sync-indexes --confirm-rebuild=auto`
6. dclassql 会按 dataclass 重建表并迁移同名列。新增字段前可手工为旧数据做填充，尽量避免直接兼容
7. 关系 include 使用项目当前生成客户端支持的形式，例如：
   - `include={"content": True}`

## 文章抓取模型

1. `Article` 保存文章元数据，`ArticleContent` 保存内容抓取结果和状态。
2. 内容状态以 `ArticleContent.status` 为准：
   - `pending`：等待抓取
   - `fetched`：已成功抓取
   - `failed`：失败但未来可自动重试
   - `give_up`：达到重试上限，自动流程不再重试
3. 自动重试使用有限退避，不允许无穷重试：
   - 最大重试次数：`MAX_RETRIES = 5`
   - 间隔：10 分钟、1 小时、6 小时、24 小时、72 小时
4. 抓取成功时：
   - `status = "fetched"`
   - `retry_count = 0`
   - `next_retry_at = None`
   - 清空 `fetch_error`
   - 更新 `Article.content_fetched_at`
5. 抓取失败时：
   - `retry_count += 1`
   - 未达上限：`status = "failed"`，设置下一次 `next_retry_at`
   - 达到上限：`status = "give_up"`，`next_retry_at = None`
   - `Article.content_fetched_at = None`
6. 自动抓取只处理：
   - `Article.content_fetched_at is None`
   - 且没有 content、或 `pending`、或 `failed` 且 `next_retry_at <= now`
7. 强制重试 API 不同步抓取，只负责重新入队：
   - `POST /api/articles/{article_id}/retry-fetch`
   - 设置 `status = "pending"`、`retry_count = 0`、`next_retry_at = now`、清空错误和已抓取时间
   - 下一轮 collector 自动抓取
8. `ArticleContent` 是唯一正文抓取队列，collector 只从数据库状态消费待抓任务，不单独维护内存投递队列。

## 微信采集原理

1. 采集分两层：
   - 文章列表元数据来自微信主进程内存扫描。
   - 文章正文 HTML 来自 `mp.weixin.qq.com` 原文 URL 抓取和规范化。
2. 内存扫描只扫描 `role_from_cmdline(pid) == "wechat-main"` 的主进程。不扫所有微信子进程，除非正在调试。
3. 微信进程角色由 `common.py` 按 `/proc/<pid>/cmdline` 判断：
   - `wechat-main`：主进程，当前文章列表 XML 的主要来源。
   - `subscription-disorder-renderer`、`article-renderer`、`renderer`、`web-shell-browser`：辅助识别用，不是默认扫描目标。
4. 内存区域来自 `/proc/<pid>/maps`，实际读取 `/proc/<pid>/mem`。默认只扫匿名、heap、v8、partition_alloc、scudo、blink、malloc 等更可能包含页面数据的区域。
5. `--all-regions` 是调试选项，成本更高、噪声更大，不要默认打开。
6. 扫描器有两个：
   - `parser`：`wechat_mem_xml_parse_scan.py`，严格找 `<mmreader>...</mmreader>` 后用 lxml 解析，是默认实现。
   - `matcher`：`wechat_mem_item_xml_scan.py`，基于文本/正则匹配旧逻辑，可用于 parser 漏数据时对比。
7. `ItemXml` 通过 `biz + mid + idx` 作为文章唯一键，保存时对应 `Article.key = f"{biz}:{mid}:{idx}"`。
8. 每轮 collector 的流程：
   - 启动时先扫描一次内存，保存当前可见文章。
   - 从数据库队列消费待抓正文任务。
   - 截图识别订阅号列表里的未读红点。
   - 依次移动鼠标并点击红点，等待 `--wait-after-click` 后再次扫描内存。
   - 每次保存扫描结果后，再从数据库队列消费待抓正文任务。
9. 已存在文章再次扫描到时只更新元数据和 `last_seen_at` / `seen_count`，正文抓取统一交给数据库队列消费，不要绕过重试模型单篇直投。

## xpra / X11 控制

1. 微信运行在 xpra/X11 环境里，默认连接参数在 `x11_wechat.py`：
   - xpra 目标：`tcp://127.0.0.1:10000`
   - 密码文件：`/etc/xpra-auth/password`
   - X display：`:100`
   - Xauthority：`/home/wechat/.Xauthority`
2. 窗口发现用 `xpra info`，只选择：
   - `title == "微信"`
   - `tray == False`
   - `shown == True`
   - 多个候选时取面积最大的窗口。
3. 截图流程：
   - `xpra info` 找窗口 xid。
   - `xwd -id <xid>` 截 X11 窗口。
   - `xwdtopnm | pnmtopng` 转 PNG。
   - OpenCV 读取为 RGB 数组。
4. 鼠标控制用 `xdotool`，坐标是微信窗口内坐标，不是全屏坐标。
5. 未读红点识别在 `WechatUi.find_unread_subs()`：
   - 先用界面竖向分割线定位公众号列表区域。
   - 在列表区域找颜色 `[250, 81, 81, 255]` 的红点。
   - 用轮廓和局部颜色方差过滤噪声。
6. 如果报 `没有找到 title=微信 且 tray=False 的 xpra 窗口`，优先检查 xpra 里微信窗口是否真正显示，而不是改采集逻辑。
7. 不要随意改 xpra 的默认 display、密码文件、窗口筛选条件；这些和 systemd/xpra 部署强相关。

## 正文抓取

1. 正文抓取不依赖微信 GUI，直接请求 `mp.weixin.qq.com` URL。
2. URL 必须是 `mp.weixin.qq.com`，否则直接报错。
3. 抓取后用 lxml 提取并规范化 `#js_article`，删除广告、二维码、script 等节点，图片 `src` 优先使用 `src` 或 `data-src`。
4. 规范化后的 HTML 用 zstd 压缩后保存到 `ArticleContent.normalized_html_zstd`。
5. `未找到 #js_article` 通常意味着拿到的不是正文页，例如微信验证码/风控页、删除页、异常跳转页，应进入失败重试模型，不要当成解析器必须兜底的正常页面。
6. 抓取有全局节流：
   - 默认 `DEFAULT_FETCH_DELAY = 3.0`
   - 实际每次请求间隔随机在 `fetch_delay` 到 `fetch_delay * 2`
   - 通过 `/tmp/wechat_article_fetch.lock` 和 `/tmp/wechat_article_fetch.last` 跨线程/进程协调。
7. collector 固定只开一个后台线程抓取正文，并通过 `fetch_pending_article_contents()` 消费数据库队列。

## 前端与 API

1. Web 页面是工具界面，不做营销页。
2. 未成功文章应显示明确状态，并允许点击“加入重试”调用强制重试 API。
3. `/api/stats` 可以使用 SQL 聚合统计，因为这是 ORM 当前不擅长的路径。
4. 普通文章列表、文章详情、重试入队等业务接口优先使用 dclassql ORM。

## 排查习惯

1. 先读代码和数据库状态，再改代码。
2. 排查数据库时优先只读连接或 ORM 查询；除非用户明确允许，不要修改数据库。
3. 搜索文件优先使用 `rg` / `rg --files`。
4. 修改前注意 `git status`，不要覆盖用户已有改动。
