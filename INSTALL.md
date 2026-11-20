# 安装说明（给安装本 skill 的 agent）

本文件写给负责安装的 agent。按顺序执行，每一步完成后再进行下一步。

## 1. 确认前提

- Python 3.10 或以上：运行 `python --version` 或 `python3 --version` 确认
- 运行环境能执行命令并访问网络（Codex 等沙盒环境需开启命令联网，否则数据源全部失败）

## 2. 放到正确的目录

解压后的文件夹名必须是 `financial-hot-article`，整个文件夹放进所用 agent 的 skills 目录：

| Agent | 目录 |
|---|---|
| Codex | `~/.codex/skills/financial-hot-article/`（Windows：`C:\Users\<用户名>\.codex\skills\financial-hot-article\`） |
| Claude Code | `~/.claude/skills/financial-hot-article/`（Windows：`C:\Users\<用户名>\.claude\skills\financial-hot-article\`） |
| 其他 agent | 该 agent 读取 skill 的目录；要求能读到根目录的 `SKILL.md` |

放好后确认结构：

```text
financial-hot-article/
├── SKILL.md
├── INSTALL.md
├── requirements.txt
├── setup.bat / setup.ps1 / setup.sh
├── scripts/mkt.py
└── references/
```

如果目标位置已有同名文件夹，先改名备份（如 `financial-hot-article.bak`），不要覆盖。

## GitHub 自动更新

每次启动任务时会尝试运行 `python scripts/update.py`，从 `dhhp2k1/financial-hot-article` 的 main 分支检查并自动快进。需要 Git、网络和该私有仓库的读取权限；Git 使用现有本机凭据，不交互索取令牌。脚本只更新以该仓库为 origin 的独立 Git 安装，当前分支须为 main 且工作区干净；有本地改动、历史分叉、认证失败或连接超时时保留现有版本并提示。更新成功后重新读取 SKILL.md。

解压包没有 Git 仓库信息，无法自行检查更新。要启用此功能，先备份已有目录，再用有权限的账户在所用 agent 的 skills 目录中执行：

```bash
git clone https://github.com/dhhp2k1/financial-hot-article.git
```

已有 Git 克隆可确认 origin 指向该仓库，且工作区已妥善保存。升级到包含本更新器的版本需要先手动同步一次；原本不含更新器的旧版不会自动发现它。可用 `python scripts/update.py` 单独测试；失败不影响已有功能。

## 3. 安装依赖

依赖装在 skill 文件夹自带的 `.venv` 里，不改动系统 Python。

- Windows：在 skill 文件夹里运行 `setup.bat`（或 `powershell -NoProfile -ExecutionPolicy Bypass -File setup.ps1`）
- macOS / Linux：在 skill 文件夹里运行 `bash setup.sh`

脚本会建 `.venv`、安装 `requirements.txt`（PyPI 失败时自动改用清华镜像），最后自动运行自检。

依赖安装失败也能用：行情、指数、板块、涨停池、K线、研报、财联社电报、海外行情只用 Python 标准库。缺依赖时只少交易日历、个股新闻、宏观日历三项。

## 4. 验收

在 skill 文件夹里运行：

```bash
python scripts/mkt.py selftest
```

（macOS / Linux 用 `python3`。脚本会自动改用 `.venv`。）

通过标准：

- 必须 `ok=True`：sina 行情、腾讯 行情、交易日（K线推断）、腾讯 K线、东财 研报库、财联社 电报直连、Yahoo 直连
- 装了依赖时还应 `ok=True`：交易日历（akshare）、akshare 宏观日历
- 东财板块、东财涨跌家数会间歇性被断开连接，属于正常，只要同组里有一个备用源 `ok=True` 即可（板块：东财直连 / akshare / 新浪；涨跌家数：ulist / 乐咕）

再运行一次 `python scripts/mkt.py now`，确认输出了当天日期和星期。

## 5. 让 agent 加载

重启 agent（或开启新会话），使其重新读取 skills 目录。

## 6. 向用户汇报

用一两句话告诉用户：装在哪个目录、依赖是否装好、自检里哪些源不可用。不要修改 skill 内的任何文件。

## 卸载

删除整个 `financial-hot-article` 文件夹即可；依赖都在其中的 `.venv` 里。

## 运行时产生的文件

- `.venv/`：依赖环境，绑定本机，换电脑需重新运行安装脚本，不要复制
- `.cache/`：交易日历缓存
- `fha-runs/`：每次取数的落盘结果和事实底稿，生成在运行命令时的当前目录下
