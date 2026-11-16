> 优先级：本文档与 SKILL.md 冲突时，以 SKILL.md 为准。文中的行情、财报、价格等数字是历史案例，只用来说明方法，不得写进新稿件。
>
> 注：本文中的 VT、mx-search、skill_view、DuckDuckGo 等是 v6.8 旧环境的工具。v7.1 起取数统一用 `scripts/mkt.py`，搜索用运行环境自带联网搜索；方法和结论仍然有效。

# 金十数据文章抓取方法

金十数据文章（`go.jin10.com` 短链、`xnews.jin10.com` 详情页）使用 Nuxt.js SPA 渲染。不同路径返回内容差异巨大。

## 路径对照

| 路径 | 渲染方式 | 正文 | 备注 |
|------|---------|------|------|
| `/webapp/details.html?id=222392` | 纯客户端渲染 | ❌ 空壳 | VT read_url 和 curl 均无内容 |
| `/details/222392` | Nuxt SSR | ✅ 正文在 HTML 中 | curl 秒级返回全文 |
| 任何 `/api/` 前缀路径 | Nuxt SSR（route） | ❌ 返回 404 SPA 壳 | 不是真正的 REST API |

## 抓取步骤

1. 短链（`go.jin10.com/-s02ne`）先 curl -sL 跟踪重定向获取真实 URL
2. 真实 URL 通常是 `https://xnews.jin10.com/webapp/details.html?id=XXXXX`
3. 将路径改写为 `https://xnews.jin10.com/details/XXXXX`（去 `/webapp`，去 `.html`）
4. curl 该 SSR 路径，正文在 `<div class="jin10-news-cdetails-content">` 内

## 实操命令

```bash
# 第一步：跟踪短链
REAL_URL=$(curl -sL -o /dev/null -w '%{url_effective}' --max-time 10 '短链URL')
# 第二步：提取 ID 并构造 SSR 路径
ID=$(echo "$REAL_URL" | grep -oP 'id=\K\d+')
# 第三步：抓取
curl -sL --max-time 10 "https://xnews.jin10.com/details/$ID"
```

## 已知限制

- VT read_url 和 browser_navigate 均会超时（SPA 加载过重）
- 金十页面有 `Published Time` meta 标签，检查文章日期是否与当前写作日期匹配
