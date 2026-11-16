# VT get_market_data 使用指南

## 概述

VT get_market_data 是 Vibe-Trading MCP 提供的 OHLCV 行情数据工具，支持多源（yfinance/akshare/tushare/okx/ccxt）。

## 美股（yfinance）— ★★★★★ 推荐

**这是获取美股隔夜数据的最佳方式，精度远超 web_search。**

### 调用模板

```json
{
  "codes": ["^GSPC.US", "^DJI.US", "^IXIC.US", "JPM.US", "AVGO.US", "NVDA.US"],
  "start_date": "2026-06-02",
  "end_date": "2026-06-05",
  "source": "yfinance"
}
```

### 代码格式

- 指数：`^GSPC.US`（标普500）、`^DJI.US`（道琼斯）、`^IXIC.US`（纳斯达克）
- 个股：`BAC.US`、`JPM.US`、`AVGO.US`、`NVDA.US`
- 中文股：`BABA.US`、`JD.US`

### 返回字段

```json
{
  "trade_date": "2026-06-04T00:00:00",
  "open": 50986.10,
  "high": 51657.89,
  "low": 50986.10,
  "close": 51561.93,
  "volume": 520670000
}
```

### 数据呈现最佳实践

以表格形式整理，一目了然：

```
| 标的 | 收盘 | 涨跌幅 | 关键信息 |
|------|------|--------|---------|
| 道琼斯 | 51,562 | +1.73% | 创历史新高 |
| JPMorgan | $310.89 | +3.34% | 放量突破 |
| 博通AVGO | $418.91 | -12.59% | 崩盘式暴跌 |
```

涨跌幅自行计算：`(close - prev_close) / prev_close * 100`

## A股（akshare）— ★ 不推荐

**几乎每次调用都返回空 `{}`。不要浪费时间反复尝试。**

### 已知问题

- 可能原因：akshare 模块版本、网络限制、交易时段限制
- 所有格式（`.SH`、`.SZ`、不同 source 参数）均无效
- 换用 `source: "auto"` 同样返回空

### 替代方案

A股数据使用：
1. 新浪 hq.sinajs.cn API（最可靠）
2. eastmoney push2 API（备用）
3. VT web_search 搜索当日盘面新闻（补充）

## 港股

未充分测试。如有需要，尝试 `source: "yfinance"` 搭配 `.HK` 后缀。

## 版本记录

- v1.0: 基于 2026-06-05 实战记录。yfinance 美股数据完美可用，akshare A股全空。
