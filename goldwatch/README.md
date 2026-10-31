# 枫币行情 · Maple Gold Watch

面向 **Linux＋MySQL** 的独立价格监控网页。公开报价来自：

独立文件包：[maple-goldwatch.zip](https://github.com/btc2060/mxd/raw/refs/heads/gold-price-monitor/downloads/maple-goldwatch.zip)。解压后只需其中 `maple-goldwatch` 目录，无需任何挂机程序。

<https://search.7881.com/G6344-100001-G6344P002-G6344P002005-0.html?pageNum=1>

**商家回收价与玩家挂售价完全分开保存和显示。** 每条同时保留 `1 元 = X 万金`、`1 万金 = Y 元`；金额使用数据库 Decimal，倒数重新计算，避免原页面显示位数不同造成偏差。

![界面预览：明确标记的演示数据，不代表真实行情](docs/preview-desktop.png)

## 已提供的功能

- 默认每 10 分钟采集，可在网页设置 60～86400 秒任意间隔、暂停／恢复定时、手动采集、采集前 1～3 页。
- 商家回收取当前样本的最高收购单价，玩家挂售取当前样本的最低挂售单价；支持中位数指标。
- 双线对比、单市场折线／K 线；周期 5 分钟、30 分钟、1 小时、日、周，范围 24 小时／7 天／30 天／90 天。
- 切换“元／万金”和“万金／元”，K 线倒数转换时自动交换高低价。周线按北京时间周一开始。
- 原始报价明细、采集日志、24 小时涨跌和 CSV 导出。缺少对比样本不显示虚构涨跌，空白时段不补值。
- 数据库锁防止重复采集；任务超时、失败退避、过期报价标识；失败不以 0 覆盖上一笔价格。两个区域不同步时不计算价差。
- 管理员口令控制改设置、抓取和导入；只读看板不需要口令。网页没有数据库密码。
- HTML 优先、必要时 Chromium 渲染；不绕过登录、人机验证或访问限制。可手动导入保存的公开 HTML，记录明确标记为“手动导入”。

这记录的是**挂牌报价，不是成交价**。只抓取前几页时，“最优”仅指已采集样本，不代表全站最优，也没有把采样条数称为成交量。

## 来源验证的实际限制

本次开发环境的出站域名白名单不含 `search.7881.com`，因此**没有在云端完成该网站实时页面的在线核验**。价格解析测试根据用户截图中的 2 条商家和 5 条玩家报价建立 HTML 测试页，网站真实 HTML 结构仍需在你的部署服务器首次验证。

采集器有自动识别与两个独立 CSS 选择器。如果来源改版、动态区域未加载、robots 禁止访问、HTTP 403／429 或出现验证页面，看板会显示实际失败原因，不会伪造实时价格。浏览器渲染只用于正常加载公开页面，不解验证。演示模式的所有历史都是独立模拟数据，页面始终带明显标记。

## 方案 A：Docker Compose 自带 MySQL（最容易部署）

服务器建议 Linux x86_64、2 核 2GB 起，已安装 Docker 和 Compose 插件。浏览器仅按需运行，默认 10 分钟一次；实际资源占用与来源页面有关。

下载代码后进入包含 `compose.yml` 的 `goldwatch` 目录：

```bash
python3 tools/init_env.py
docker compose up -d --build
docker compose ps
```

`init_env.py` 自动生成随机管理员口令、MySQL 用户密码和 root 密码，只写入本机 `.env`，不覆盖已有文件。MySQL 的 3306 端口不对外发布，网页默认对外端口 8080。

浏览器打开：`http://服务器IP:8080`。服务器安全组／防火墙放行该端口。初次启动会建立表，后台自动开始第一轮采集；没有历史时先显示空白，随着采集积累曲线。

管理员口令是 `.env` 内的 `ADMIN_TOKEN`。在网页左侧“采集设置”中输入，即可改频率、手动采集等。不要把 `.env` 放进网页静态目录。对公网使用时配域名和 HTTPS，反向代理示例见 `deploy/nginx.conf.example`；通过反向代理可把 `.env` 的 `BIND_IP` 改为 `127.0.0.1`。

查看启动或采集错误：

```bash
docker compose logs --tail=100 web
docker compose logs --tail=100 mysql
docker compose exec web python -m goldwatch.cli collect
```

最后一条手动执行一轮实际采集，并打印商家／玩家数量；已有任务运行时不会再开第二个。第一次部署请确认网页“采集记录”有实际成功记录、明细两类区域与原站一致，再长期使用。不能把演示预览当作已经成功抓取来源。

## 方案 B：连接你已有的在线 MySQL

在 MySQL 创建单独数据库 `goldwatch`，字符集 `utf8mb4`。给专用应用用户这个数据库的建表、索引、读写权限；首次会自动建表，不要求全局管理员权限。

先执行 `python3 tools/init_env.py`，再在生成的 `.env` 增加：

```dotenv
DATABASE_URL=mysql+pymysql://应用用户:URL编码后的密码@MySQL主机:3306/goldwatch?charset=utf8mb4
```

密码中 `@ : / # %` 等字符必须做 URL 编码。数据库访问白名单允许该 Linux 服务器。托管 MySQL 要求 TLS 时按提供方配置证书／连接参数，例如在容器挂载 CA 文件并使用 PyMySQL `ssl_ca` 参数，不关闭证书校验。

只启动网页，不启动自带数据库：

```bash
docker compose -f compose.external.yml up -d --build
docker compose -f compose.external.yml logs --tail=100 web
```

两种 Compose 选一种。不要同时启动，否则会争用同一个网页端口。

## 不使用 Docker／本地查看

Python 3.11／3.12：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install --with-deps chromium
```

真实模式连接 MySQL：在进程环境配置 `DATABASE_URL`、`ADMIN_TOKEN`，然后：

```bash
.venv/bin/python -m uvicorn goldwatch.app:create_app --factory --host 0.0.0.0 --port 8080 --workers 1
```

不设置 `DATABASE_URL` 时会自动使用本地 SQLite 的 `data/goldwatch.db`，方便独立试用。也兼容 `postgresql+psycopg://...`。**Docker Compose 主方案使用 MySQL。**

若系统已安装 Chromium，也可配置 `CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium`，无需下载 Playwright 的浏览器。Docker 镜像已安装 Chromium。

只看界面演示，使用独立 SQLite 演示库，不抓取真实网站：

```bash
MODE=demo ADMIN_TOKEN=demo-password-change-me .venv/bin/python -m uvicorn goldwatch.app:create_app --factory --host 127.0.0.1 --port 8080
```

打开 `http://127.0.0.1:8080`。演示库与真实库分开；即使手动指定同一个数据库，也通过 dataset 字段隔离读取。演示模式不会发起真实采集或导入真实报价。

## 采集方式与适配

1. 默认自动模式先请求 robots.txt，再读取公开报价页。
2. 两个报价区域未都解析出来时，正常启动 Chromium 等待公开页面渲染，合并相应区域，避免双计数。
3. 基于“立即出售／立即购买”和带单位价格定位报价卡片；不会把商家区与玩家区混合。多页的同一明细链接会去重。
4. 如果改版，设置“商家回收卡片选择器”和“玩家挂售卡片选择器”，**各自必须选中单张报价卡片**，不要选择整个列表或页面。留空恢复自动识别。
5. 可在你自己的浏览器手动保存公开页面，再在设置“页面适配与手动导入”选择 HTML。导入时间按实际操作记录，不伪造过去时间；未解析到报价或只有验证页面时拒绝入库。

推荐间隔 5～10 分钟，默认只抓第一页以降低访问量。开启 5 分钟 K 线不会改变抓取频率；10 分钟抓取意味着有些 5 分钟时段没有价格，留空是正常行为。没有实际历史的数据无法恢复，后台从启动后开始累计。

## 数据库与备份

主要表：

| 表 | 内容 |
| --- | --- |
| `crawl_runs` | 每次采集的开始、结束、成功／失败、来源方式和错误 |
| `quotes` | 原始单条报价、两种单位、区域、来源链接和截取的文本证据 |
| `snapshots` | 每次采集、每个区域的最优／中位／最差和报价条数 |
| `preferences` | 保存的采集频率、方式、选择器和下次运行时间 |
| `collector_lease` | 防止两个采集任务并发 |

`docker compose down` 保留数据库卷；**不要使用 `down -v`，它会删除数据卷**。建议每日数据库备份。自带 MySQL 示例：

```bash
mkdir -p backups
umask 077
docker compose exec -T mysql sh -c 'MYSQL_PWD="$MYSQL_PASSWORD" exec mysqldump -u "$MYSQL_USER" --single-transaction --no-tablespaces "$MYSQL_DATABASE"' | gzip > "backups/goldwatch-$(date +%Y%m%d-%H%M%S).sql.gz"
```

已有在线 MySQL 使用其托管备份／快照或你现有的 mysqldump 流程。CSV 是行情数据导出，不能代替完整数据库备份。

升级先备份，再 `docker compose up -d --build`，保留 `.env` 和 MySQL 数据卷。当前为初始 schema；未来结构升级应增加迁移，而不是删表重建。

## 测试与已验证范围

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

测试涵盖用户截图价格、倒数计算、双市场隔离、最佳报价方向、缺测不补值、K 线高低倒数、数据库锁、失败退避、管理认证、CSV、选择器和来源限制。浏览器检查覆盖桌面／手机、单位和 K 线切换、设置保存。

可选的临时 MySQL 8.4＋应用容器测试：先构建 `goldwatch:local` 并拉取 `mysql:8.4`，再运行 `python3 tools/smoke_mysql.py`。它只创建带随机名称的本地测试容器，使用随机密码，测试结束会移除测试容器和匿名卷，不访问 7881 或已有数据库。

ECharts 已随项目本地提供，运行时不依赖第三方图表 CDN；许可证见 `static/vendor/`。应用源码位于 `goldwatch/`，界面位于 `static/`。部署只需当前目录，不需要仓库中的任何挂机代码。
