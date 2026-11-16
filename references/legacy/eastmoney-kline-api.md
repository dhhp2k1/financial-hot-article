# eastmoney 历史K线API使用参考

## 使用场景
- 查个股历史涨停日期（"上次涨停是什么时候"）
- 查近期涨跌幅趋势
- 验证创新高/新低事件

## API 端点

```
GET https://push2his.eastmoney.com/api/qt/stock/kline/get
```

## 必填参数

| 参数 | 说明 | 示例 |
|------|------|------|
| secid | 交易所+代码 | 深=0.000725, 沪=1.600519 |
| fields1 | 固定填 f1-f6 | f1,f2,f3,f4,f5,f6 |
| fields2 | 返回字段 | f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61 |
| klt | K线周期 | 101=日线 |
| fqt | 复权类型 | 1=前复权, 2=后复权 |
| beg | 开始日期 | 20200101 |
| end | 结束日期 | 20260522 |

## 返回格式

```json
{
  "data": {
    "klines": [
      "2026-05-22,4.70,4.79,4.82,4.47,30638285,14186724404.63,7.46,2.13,0.10,8.43"
    ]
  }
}
```

每条 K 线是一个逗号分隔的字符串，字段索引：

| 索引 | 含义 | 示例 |
|------|------|------|
| 0 | 日期 | 2026-05-22 |
| 1 | 开盘价 | 4.70 |
| 2 | 收盘价 | 4.79 |
| 3 | 最高价 | 4.82 |
| 4 | 最低价 | 4.47 |
| 5 | 前收盘 | (不复权有效) |
| 6 | 成交量 | 30638285 |
| 7 | 成交额 | 14186724404.63 |
| 8 | 振幅% | 7.46 |
| 9 | 涨跌幅% | 2.13 |
| 10 | 涨跌额 | 0.10 |
| 11 | 换手率% | 8.43 |

## 实战示例: 查京东方A历史涨停

```bash
curl -s 'https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=0.000725&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61&klt=101&fqt=1&beg=20200101&end=20260522' \
  -H 'User-Agent: Mozilla/5.0' \
  -H 'Referer: https://quote.eastmoney.com/' \
  | python3 -c "
import sys,json
data=json.load(sys.stdin)
kdata = data.get('data',{}).get('klines',[])
results = []
for line in kdata:
    parts = line.split(',')
    date = parts[0]
    close = float(parts[2])
    chg = float(parts[8])  # 涨跌幅%
    if chg >= 9.5:
        results.append(f'{date} 涨停! 收盘:{close} 涨幅:{chg}%')
print('\\n'.join(results) if results else '无涨停记录')
"
```

输出:
```
2020-06-08 涨停! 收盘:4.27 涨幅:10.34%
2021-01-12 涨停! 收盘:6.51 涨幅:9.6%
2026-05-21 涨停! 收盘:4.69 涨幅:10.09%
```

## 注意事项

- 该 API 跨域使用需要添加 `Referer: https://quote.eastmoney.com/` 请求头
- 返回约 1500 条日线数据（从 2020 年开始），足以覆盖大部分查询
- 条数过多时建议缩短 beg/end 范围以提高响应速度
- 不复权数据(fqt不填或空)的前收盘可能不准，建议用 fqt=1 或 fqt=2
