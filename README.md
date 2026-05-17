# wechat-parser

## 局域网部署

目标是让连接同一路由器 Wi‑Fi 的设备都能通过 `http://news.lan/` 访问这个项目的 Web 页面。

### 1. 启动 Web 服务

项目自带 Web 服务，监听 `127.0.0.1:8199`：

```bash
uv run python server.py
```

如果要长期后台运行，直接用仓库里的 systemd 服务：

```bash
sudo cp wechat-collector-server.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wechat-collector-server
```

如需同时启动采集服务：

```bash
sudo cp wechat-collector.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wechat-collector
```

### 2. 查询本机局域网 IP

先在部署这套服务的机器上执行：

```bash
ip a
```

找到连接路由器那个网卡对应的局域网 IP，例如 `10.1.1.23`。

### 3. 在路由器里添加主机名映射

打开路由器后台：

```text
https://10.1.1.1/cgi-bin/luci/admin/network/dns
```

在页面里操作：

1. 选择 `DNS记录`。
2. 找到 `主机名映射`。
3. 点击 `添加`。
4. 域名填 `news.lan`。
5. IP 地址填上一步通过 `ip a` 查到的局域网 IP，例如 `10.1.1.23`。
6. 点击 `保存并应用`。

这样连到这台路由器 Wi‑Fi 的设备，就能把 `news.lan` 解析到这台机器。

### 4. 配置 Caddy

在 Caddy 配置里加入：

```caddyfile
http://news.lan {
    reverse_proxy 127.0.0.1:8199
}
```

然后重载 Caddy。比如系统使用 systemd 时：

```bash
sudo systemctl reload caddy
```

### 5. 验证访问

现在连接该路由器 Wi‑Fi 的设备都可以直接打开：

```text
http://news.lan/
```

如果打不开，优先检查这几项：

- `uv run python server.py` 或 `wechat-collector-server` 是否正常监听 `127.0.0.1:8199`
- Caddy 是否已经重载成功
- 路由器里的 `news.lan -> 局域网 IP` 是否填对
- 访问设备是否真的连接在这台路由器的 Wi‑Fi / 局域网内

## `wechat_mem_item_xml_scan.py`

- 脚本使用 `mmreader/category/item` XML 扫描微信主进程 `wechat-main`
- 目标是导出主进程内存里能稳定找到的公众号文章 XML，优先保证结果干净

### 用法

```bash
uv run python wechat_mem_item_xml_scan.py --out dumps/wechat_item_xml.json
```

- 不传 PID 时，会自动发现微信主进程并只扫描它
- 如果显式传了 PID，就按传入的 PID 扫

### 当前机器的运行前提

- 当前仓库已经兼容两种微信主程序路径：
  - `/opt/wechat/wechat`
  - `~/.local/wechat-pkg/opt/wechat/wechat`
- 如果脚本报 `/proc/<pid>/mem` 权限错误，通常不是代码问题，而是 Ubuntu 默认 `kernel.yama.ptrace_scope=1` 拦截了跨进程内存读取。
- 这时需要先执行：

```bash
sudo sysctl kernel.yama.ptrace_scope=0
```

- 然后再运行：

```bash
uv run python wechat_mem_item_xml_scan.py --out dumps/wechat_item_xml.json
```

## 非 xpra 截图

- `x11_wechat.py` 现在支持 `xpra` 和 `local-x11` 两种 backend，默认 `auto`。
- 非 `xpra` 场景的截图走 `local-x11`，通过 `xwininfo` 找窗口，再优先尝试 `xwd` 流水线；如果系统里没有这些命令，再退回到 Python 依赖 `mss`。
- 如果系统已经安装了 `xwd`、`xwdtopnm`、`pnmtopng`，`local-x11` 会优先走这条截图流水线；在 KDE Wayland + XWayland 下，这通常比 `mss` 更稳。
- 推荐补齐这几个系统包：

```bash
sudo apt install x11-apps netpbm xdotool wmctrl
```
- 默认会优先读取当前 shell 的 `DISPLAY` / `XAUTHORITY`；如果当前 shell 没有这些变量，会退回到微信主进程环境里的值。
- 如果要显式指定 backend，可以这样跑：

```bash
WECHAT_X11_BACKEND=local-x11 uv run python test.py
```

- 如果窗口标题不是“微信”，可以覆盖：

```bash
WECHAT_WINDOW_TITLE='微信测试版' WECHAT_X11_BACKEND=local-x11 uv run python test.py
```

- 鼠标点击仍然依赖 `xdotool`。这部分如果要在本机完整跑自动点击，需要先装上面的系统包。
