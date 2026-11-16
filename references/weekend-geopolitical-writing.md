> 优先级：本文档与 SKILL.md 冲突时，以 SKILL.md 为准。文中的行情、财报、价格等数字是历史案例，只用来说明方法，不得写进新稿件。
>
> 注：本文中的 VT、mx-search、skill_view、DuckDuckGo 等是 v6.8 旧环境的工具。v7.1 起取数统一用 `scripts/mkt.py`，搜索用运行环境自带联网搜索；方法和结论仍然有效。

# 周末/非交易时段地缘事件文章写作适配

## 适用场景

当地缘政治事件在周末（周六/周日）或市场休市时段爆发，用户要求在当天写公众号文章时使用。此时：
- A股/港股/美股均休市，无实时股票数据
- 原油/黄金期货电子盘交易量极低（CME Globex 周日亚洲时段）
- 主要价格数据依赖暗盘/OTC报价和用户提供的信息

## 与周内地缘事件四段式结构的关键差异

### 01 事件/军事动作 — 不变
- 仍然按时间线展开冲突侧+外交侧
- 网搜信源优先级：DuckDuckGo HTML（中英文各搜一次）> Yahoo/Reuters/AP 原文
- 冲突声明必须标注来源（伊朗方面称/美军声明/第三方核实）

### 02 外交/谈判维度 — 不变
- 谈判框架、各方表态、背景信息
- 与01的呼应关系

### 03 跨资产市场定价 — 需适配

**问题**：yfinance 的 `includePrePost=true` 在周日亚洲时段只返回周五收盘数据，不反映暗盘变动。

**解决方案**：
1. 先用 `mkt.py global` 获取周五收盘价作为基准
2. 暗盘涨跌幅只用权威媒体（Reuters/Bloomberg/CNBC）报道过的数字，注明来源和“非正式交易、流动性较低”
3. 不用“周五收盘 ×（1 + 涨幅）”推算价格写进正文（SKILL.md 8.5）
5. 航运数据（船讯网/Kpler）和保险数据（劳氏）不受市场休市影响，可直接使用

**写入格式**：
> WTI上周五收于71.41美元。据Reuters报道，周日电子盘开盘后油价一度涨超3%，属非正式交易时段报价，流动性较低。

**注意**：油价本身就是文章主角，所以写收盘价不违反"不写个股收盘价"规则。

### 04 下周前瞻 — 需重定向

周内文章的"下周前瞻"列出的是已排期数据（PCE/CPI/财报等）。
周末文章的"下周前瞻"需要改为"周一开盘盯什么"，因为：

- 读者最关心的是"开盘会怎样"
- 周末发生的事件尚未被任何主要市场定价
- 亚太市场（日本/韩国/澳洲）周一最先开盘，是第一个反应窗口

**改写为**：
- 第一：海峡通航实际状态（航运保险数据先行）
- 第二：OPEC+应对 + 即将发布的月度报告日期
- 第三：即将公布的关键宏观数据（CPI/PPI日期）+ 当前FedWatch概率
- 收尾：亚太市场周一开盘关注点（日韩高度依赖中东原油）

**禁用**：
- ❌ "周一高开几乎没有悬念"→ 这是预测，不是盯什么
- ❌ "股市可能面临额外压力"→ 改成"周一开盘反应值得关注"
- ❌ "如果X则Y"的条件预测→ 改成"后续需要关注X数据"

## 搜索方法：DuckDuckGo HTML 端点

当 VT web_search 不可用或 Bing HTML 解析失败时，DuckDuckGo 的 HTML 端点是最可靠的中英文搜索源：

```python
import requests, re

headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}

def ddg_search(query, limit=10):
    """搜索 DuckDuckGo HTML 端点，返回 (标题, 摘要) 列表"""
    url = f'https://html.duckduckgo.com/html/?q={query}'
    resp = requests.get(url, headers=headers, timeout=15)
    results = re.findall(
        r'<a rel="nofollow" class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?'
        r'<a class="result__snippet"[^>]*>(.*?)</a>',
        resp.text, re.DOTALL
    )
    out = []
    for url, title, snippet in results[:limit]:
        title = re.sub(r'<[^>]+>', '', title).strip()
        snippet = re.sub(r'<[^>]+>', '', snippet).strip()
        out.append((title, snippet))
    return out

# 中英文各搜一次
en_results = ddg_search("Iran US strike Hormuz Strait July 12 2026 oil")
zh_results = ddg_search("伊朗 霍尔木兹海峡 关闭 2026年7月12日 油价")
```

**优势**：无需 API key，HTML 结构稳定易于解析，中英文均可用。返回的 URL 可通过 `uddg=` 参数提取真实目标。

## 实战案例

2026-07-12（周日）：伊朗宣布关闭霍尔木兹海峡、打击美中东目标 → 油价暗盘涨超3%

- 用 DuckDuckGo HTML 搜到 Mappr、Fortune、AP News、Reuters 等英文信源 + 船讯网、搜狐等中文信源
- yfinance 拉周五收盘：WTI 71.41、Brent 76.01
- 暗盘涨超3%（按报道原文写，不推算价格）
- Energy Aspects 报告引用（7月9日）：目标价 80-90 美元
- Section 04 改写为"周一开盘盯什么"格式
