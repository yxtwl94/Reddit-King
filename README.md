# Reddit King

一个精简的 Reddit 关键词数据采集器，只负责采集，不包含情感分析、Dashboard、
媒体下载、REST API、告警或可视化。

## 为什么不需要 Reddit API Key

本项目没有调用需要 OAuth 的 Reddit 官方开发者 API，也不会启动无头浏览器。
它把采集拆成两条请求链路：

| 数据 | 实际请求目标 | 工作方式 |
|---|---|---|
| 帖子搜索与帖子字段 | `old.reddit.com` | 读取公开、服务端渲染的旧版 HTML 搜索结果 |
| 评论树 | `arctic-shift.photon-reddit.com` | 查询 Arctic Shift 已归档的 Reddit 评论数据库 |

这里的“免 API Key”并不表示程序完全不发 HTTP 请求。准确含义是：不需要创建
Reddit 开发者应用，不需要 Client ID、Client Secret、账号 Cookie 或 OAuth Token。
默认采集仍然完全匿名；只有用户主动使用界面的“预登录”功能时，才会加载其自行
登录后保存的 Cookie。

```text
BAT / WinForms GUI
        |
        v
uv run main.py
        |
        +--> old.reddit.com HTML 搜索 --> 帖子 ID、标题、作者、正文、points、评论计数
        |
        +--> Arctic Shift 评论接口 --> 按帖子 ID 获取评论树
        |
        +--> posts.csv + comments.csv + run.log（逐帖实时落盘）
```

## 帖子是怎么请求的

全站搜索的实际请求类似：

```text
GET https://old.reddit.com/search
    ?q=starlink
    &include_over_18=on
    &limit=100
    &sort=relevance
    &t=all
    &type=link
```

指定 Subreddit 时使用：

```text
GET https://old.reddit.com/r/Starlink/search
    ?q=starlink
    &restrict_sr=on
    &include_over_18=on
    &limit=100
    &sort=relevance
    &t=all
    &type=link
```

主要参数：

- `q`：关键词或表达式。
- `limit=100`：Reddit 单页最多返回 100 条，不是整个任务只能保存 100 条。
- `sort`：默认 `relevance`，GUI 可选 `new`、`comments`、`top`、`hot`。
- `restrict_sr=on`：只搜索指定 Subreddit；全站搜索时不传。
- `after=t3_xxx`：Reddit 返回的下一页游标。程序读取游标后继续请求下一页。

请求使用普通 `requests.Session`，发送常规浏览器 `User-Agent`、`Accept`、
`Accept-Language` 和 `over18=1` Cookie。每次只做 GET，请求失败会针对 429、
500、502、503、504 最多重试 5 次，采用退避等待并遵守 `Retry-After`。
搜索页之间默认等待 0.75 秒。

响应回来后，程序解析：

```text
div.search-result-link[data-fullname]
```

从每张结果卡片提取帖子 ID、Subreddit、标题、作者、正文、points、发布时间、
帖子链接和 Reddit 评论计数。搜索结果以 Reddit 返回为准，程序不再做关键词二次过滤；
只应用用户设置的日期范围，并按帖子 ID 去重。

## 为什么这种搜索比无头浏览器稳定

新版 Reddit 主要依赖 JavaScript、动态接口、Cookie 和浏览器指纹，无头浏览器
更容易进入登录墙、验证码或挑战页。old Reddit 搜索页是公开的服务端 HTML，
不需要执行 JavaScript，所以程序只下载文档并解析，不模拟点击，也没有浏览器
指纹环境。

这不是破解或无条件绕过 Reddit。它只是选择了当前仍可公开读取、结构更简单的
旧版页面，并通过低并发、间隔和退避降低触发限流的概率。Reddit 仍可能随时返回
403、429 或挑战页，也可能限制可翻页的历史范围。

## 评论 API 为什么可以获取大量评论

[Arctic Shift](https://github.com/ArthurHeitmann/arctic_shift) 是独立的 Reddit
公共数据存档项目。它提前归档帖子和评论，并把存档数据库通过无需账号和 Key 的
公开查询接口提供出来。本项目请求评论时访问的是 Arctic Shift 服务器，而不是
让当前电脑再次向 Reddit 请求评论页面，因此不会触发 Reddit 对当前 IP 的评论
接口限制或浏览器指纹检查。

单帖评论请求类似：

```text
GET https://arctic-shift.photon-reddit.com/api/comments/tree
    ?link_id=t3_1uth14x
    &limit=1000
    &start_depth=20
    &start_breadth=100
```

参数含义：

- `link_id=t3_<帖子ID>`：用 Reddit 帖子 ID 定位评论树。
- `limit`：本次允许返回的最大评论容量，接口上限 25000。
- `start_depth`：评论递归深度。
- `start_breadth`：每层展开宽度；项目限制为 100，降低大型评论树返回 422 的概率。

默认“尽量抓全”时，请求容量按下面规则计算：

```text
请求容量 = min(max(Reddit 评论计数, GUI 最低容量), 25000)
```

例如页面显示 5 条、GUI 容量为 1000，仍会请求 `limit=1000`，但数据源只会返回
实际存在的 5 条；页面显示 1804 条时会请求 1804。大型树若因深度 20 返回 422，
程序会自动用深度 10 重试。

返回的树会递归展开，只保存 `kind=t1` 的真实评论，并按 `comment_id` 去重。
日志只显示评论数量和一条可点击示例链接；作者、points、完整正文、深度和全部
评论链接都写入 `comments.csv`，避免数千条评论明细撑大日志并拖慢 GUI。
每篇帖子在日志中使用带帖子序号的分割线单独成段，便于长任务快速浏览。

## “绕过 Reddit”到底绕过了什么

- 帖子搜索仍然直接访问 Reddit，但使用公开 old Reddit HTML，不走被当前网络
  403 拦截的 `.json` 搜索接口，也不运行容易被识别的无头浏览器。
- 评论请求访问 Arctic Shift 的存档数据库，因此 Reddit 不会收到这些评论请求。
- 没有绕过登录、私有社区、权限控制、验证码或付费内容。
- 不保证获得已删除、被移除、未归档或刚发布但尚未同步的数据。
- 这不是匿名代理或 IP 轮换工具；能否访问 old Reddit 仍取决于当前网络和 Reddit。

这种组合的价值是：old Reddit 提供实时关键词检索和当前评论计数，Arctic Shift
提供更大的历史评论树，两者通过帖子 ID 关联，同时避免把所有请求都压到 Reddit。

## 双击运行

Windows 用户直接双击：

```text
Reddit-King.bat
```

BAT 会打开原生 Windows 图形界面，可设置：

BAT 只负责异步启动 uv 虚拟环境中的 `pythonw.exe` 和 Tkinter GUI，随后立即退出，
不会在界面后面保留黑色命令行窗口，也不再依赖任何 PS1/VBS 启动脚本。从源码
运行时，实际采集任务由 GUI 通过 `uv run --frozen python -u main.py` 启动；打包后
则由 EXE 自身启动内置采集 worker，不再依赖目标电脑上的 Python、uv 或项目源码。

- 可选 Subreddit（留空为全局）与必填关键词/搜索表达式
- 搜索排序默认关联性，可选最新、评论最多、最高得分或热门
- 帖子数量默认无限，也可手动限制；起始时间可通过日期时间选择器设置
- 评论默认按 Reddit 显示的评论数尽量抓全，单帖最高 25000 条；也可关闭评论采集
- 标题搜索或标题+正文搜索
- 每帖最低评论容量，默认 1000
- 按 Reddit 评论计数尽量抓全评论，单帖最高 25000
- 评论深度、最大搜索页和输出目录
- 实时日志、可点击的帖子/评论链接、停止任务、打开输出目录
- 可选“预登录 / 更新 Cookie”；登录结果由 Windows 当前用户加密保存，每次任务
  可独立选择是否使用，取消使用不会删除已保存内容

GUI 日志只保留最近约 0.5MB 文本，防止长任务占用过多界面内存；输出目录中的
`run.log` 不会截断，仍完整记录整个任务。帖子和评论 CSV 也始终完整落盘。

预登录会打开独立的 Chrome 配置目录，密码始终由用户直接输入 Chrome。程序只在
检测到 Reddit 登录成功后覆盖已保存 Cookie；关闭窗口、超时或登录失败都保留原有
Cookie。Cookie 不写入运行日志、CSV、输出目录或命令行。保存时间不等于 Reddit
会话有效期，服务端使会话过期后需要再次点击预登录更新。

## 构建 Windows EXE

构建电脑需先安装 `uv`，然后双击 `build.bat`，或在命令行运行：

```bat
build.bat
```

脚本会严格按 `uv.lock` 安装构建及运行依赖，并使用 PyInstaller 生成单文件、无
控制台窗口的 `dist\Reddit-King.exe`。生成的 EXE 已包含 Tkinter、Requests、
Beautiful Soup、证书文件和采集代码，可单独复制到其他 Windows 电脑运行。

## 构建 macOS APP 和 DMG

macOS 产物必须在 macOS 上构建，不能由 Windows 版 PyInstaller 交叉生成。仓库已
提供 `build-mac.sh`，在安装 `uv` 的 Mac 上运行：

```bash
bash build-mac.sh
```

构建完成后会生成：

```text
dist/Reddit-King.app
dist/Reddit-King-macOS.zip
dist/Reddit-King.dmg
```

也可以在 GitHub 仓库的 **Actions → Build macOS → Run workflow** 手动触发云端
构建，然后从该次任务底部的 Artifacts 下载 `Reddit-King-macOS`。未进行 Apple
签名和公证的应用首次运行时，需要在 Finder 中右键应用并选择“打开”。当前匿名
采集支持 macOS；预登录 Cookie 的长期系统加密保存目前仅支持 Windows。

打包后的 macOS 应用默认把结果保存到 `~/Documents/Reddit-King/output`，不会写入
`.app` 应用包内部。“打开输出目录”会直接在 Finder 中显示最近一次任务目录。

## uv 环境

```powershell
uv sync
uv run --frozen python -m pytest -q
```

## 命令行

```powershell
# 采集 Reddit 返回的搜索结果，保存 100 个帖子及其评论
uv run --frozen python main.py `
  --subreddit python `
  --keyword "local AI" `
  --sort relevance `
  --limit 100

# 支持简单 OR 搜索表达式
uv run --frozen python main.py `
  -s AskReddit `
  -k 'career OR "job interview"' `
  --limit 500 `
  --after 2024-01-01 `
  --before 2025-01-01

# 固定每帖最多 1000 条，不按 Reddit 评论计数提高
uv run --frozen python main.py `
  -s python -k asyncio `
  --comment-limit 1000 `
  --fixed-comment-limit
```

## 输出数据

每次任务只有两张 UTF-8 BOM CSV 表，方便 Excel 直接打开。

`posts.csv`：

```text
post_id, subreddit, search_keyword, title, author, score, body, created_utc,
permalink, url, reported_comment_count, collected_comment_count, comments_complete
```

`comments.csv`：

```text
post_id, comment_id, comment_url, parent_id, author, score, body, created_utc, depth
```

`post_id` 用于关联两张表，`parent_id` 和 `depth` 用于还原评论树；
`posts.csv` 的 `permalink` 是帖子链接，`comments.csv` 的 `comment_url` 是评论直达链接。
这些查看链接使用新版 `www.reddit.com`，但帖子搜索采集仍使用 old Reddit HTML。

`score` 就是 Reddit 显示的 points，即当前净得分，不等于原始点赞总数。Reddit
会对票数做模糊处理，分数也会随投票变化，因此它是采集时刻的近似快照。

默认每次按启动时间创建独立输出目录：

```text
output/<YYYYMMDD_HHMMSS>_<subreddit或all>_<关键词>/
```

例如：`output/20260711_184530_all_asyncio/`。GUI 选择的是输出根目录，
每次开始任务仍会在其下面创建新的时间子目录。

## 评论完整性

- 默认最低请求容量为每帖 1000 条。
- 开启“尽量抓全”时，若 Reddit 评论计数为 2014，就请求 2014 条。
- Arctic Shift 单帖接口上限为 25000 条。
- 删除、缺失、未归档评论可能导致采集数量少于 Reddit 评论计数；此时
  `comments_complete` 为 `False`。
- 私有、隔离、未被归档或已响应删除请求的数据无法保证获得。

请控制采集频率并遵守数据源条款和隐私删除要求。

## 搜索限制与反爬

- old Reddit HTML 当前可直接搜索，但 Reddit 随时可能返回 403、429 或挑战页。
- 默认 `sort=relevance` 按关联性排序；可通过 GUI 或 `--sort` 调整。
- 程序使用浏览器请求头、低并发、请求间隔和自动退避，不使用无头浏览器。
- 每页的 `limit=100` 是 Reddit 单次响应上限，不是整个任务的帖子上限；程序通过
  `after` 游标跨页，直到达到帖子数量、最大页数或 Reddit 不再返回下一页。
- Reddit 搜索本身可能限制可翻页的历史范围；“无限”表示持续读取 Reddit 实际
  提供的全部结果，不代表能绕过 Reddit 服务端的结果上限。
