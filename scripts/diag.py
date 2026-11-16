"""网络诊断：逐个测试 mkt.py 用到的数据源，只用 Python 标准库，不改任何文件（只写 diag-out.txt）。

用法：python scripts/diag.py        结果同时打印并写到当前目录的 diag-out.txt
把 diag-out.txt 整个发给维护人判断。每个请求只试一次、8秒超时，全部跑完约1至3分钟。
"""
from __future__ import annotations

import json
import os
import platform
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
OUT = open("diag-out.txt", "w", encoding="utf-8")


def w(*a):
    s = " ".join(str(x) for x in a)
    print(s, flush=True)
    OUT.write(s + "\n")
    OUT.flush()


def probe(label, url, ref=None, enc="utf-8", show=300):
    h = {"User-Agent": UA, "Accept": "*/*"}
    if ref:
        h["Referer"] = ref
    t = time.time()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=8) as r:
            body = r.read().decode(enc, errors="replace")
            w(f"[OK  ] {label} | http={r.status} | {time.time() - t:.1f}s | {len(body)}B | {body[:show]!r}")
            return body
    except urllib.error.HTTPError as e:
        w(f"[FAIL] {label} | http={e.code} | {time.time() - t:.1f}s | {str(e)[:150]}")
    except Exception as e:  # noqa: BLE001
        w(f"[FAIL] {label} | {type(e).__name__} | {time.time() - t:.1f}s | {str(e)[:200]}")
    return None


w("time", time.strftime("%Y-%m-%d %H:%M:%S %z"))
w("python", sys.version.split()[0], "|", platform.platform(), "|", ssl.OPENSSL_VERSION)
w("proxy env", {k: v for k, v in os.environ.items() if "proxy" in k.lower()} or "none")
w("urllib proxies", urllib.request.getproxies() or "none")

w("\n## DNS")
for host in ["hq.sinajs.cn", "qt.gtimg.cn", "push2.eastmoney.com", "push2his.eastmoney.com", "push2ex.eastmoney.com",
             "reportapi.eastmoney.com", "www.cls.cn", "query1.finance.yahoo.com", "vip.stock.finance.sina.com.cn"]:
    t = time.time()
    try:
        ips = sorted({x[4][0] for x in socket.getaddrinfo(host, 443)})
        w(f"[OK  ] {host} -> {', '.join(ips[:4])} ({time.time() - t:.1f}s)")
    except Exception as e:  # noqa: BLE001
        w(f"[FAIL] {host} -> {e}")

w("\n## A股行情")
probe("新浪 A股", "https://hq.sinajs.cn/list=sh000001", "https://finance.sina.com.cn", "gbk")
probe("新浪 A股 http", "http://hq.sinajs.cn/list=sh000001", "https://finance.sina.com.cn", "gbk")
probe("腾讯 A股", "https://qt.gtimg.cn/q=sh000001", enc="gbk")
probe("东财 涨跌家数", "https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&secids=1.000001,0.399001&fields=f12,f104,f105,f106")
probe("东财 二级行业", "https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=3&po=1&np=1&fltt=2&invt=2&fid=f3"
      "&fs=m:90+t:2+f:!50+s:4&fields=f12,f14,f3&ut=bd1d9ddb04089700cf9c27f6f7426281", "https://quote.eastmoney.com/")
probe("东财 K线", "https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.000001&fields1=f1,f2,f3&fields2=f51,f52,f53"
      "&klt=101&fqt=1&lmt=3&end=20500101")
probe("东财 涨停池", "https://push2ex.eastmoney.com/getTopicZTPool?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt"
      f"&Pageindex=0&pagesize=3&sort=fbt:asc&date={time.strftime('%Y%m%d')}", "https://quote.eastmoney.com/ztb/")
probe("东财 研报库", "https://reportapi.eastmoney.com/report/list?pageSize=2&pageNo=1&qType=0")
probe("财联社 电报", "https://www.cls.cn/nodeapi/telegraphList?rn=2")
probe("新浪 行业板块", "https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php", "https://finance.sina.com.cn/", "gbk", 120)

w("\n## 海外行情")
probe("Yahoo chart", "https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC?range=5d&interval=1d", show=150)
probe("Yahoo chart query2", "https://query2.finance.yahoo.com/v8/finance/chart/%5EGSPC?range=5d&interval=1d", show=150)
probe("新浪 海外", "https://hq.sinajs.cn/list=gb_$inx,hf_GC,DINIW", "https://finance.sina.com.cn", "gbk")
probe("腾讯 美股", "https://qt.gtimg.cn/q=usDJI,usINX,usIXIC", enc="gbk")

w("\n## 东财全球行情（逐个代码核对名称）")
cands = ["100.DJIA", "100.SPX", "100.NDX", "100.SOX", "100.HSI", "100.N225", "100.KS11", "100.UDI", "100.FTSE",
         "100.GDAXI", "101.GC00Y", "101.SI00Y", "101.HG00Y", "102.CL00Y", "102.NG00Y", "112.B00Y",
         "171.US10Y", "171.US2Y", "100.IXIC", "100.NDX100"]
body = probe("东财 全球 ulist", "https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&ut=7eea3edcaed734bea9cbfc24409ed989"
             "&fields=f2,f3,f12,f13,f14,f18,f124&secids=" + ",".join(cands), show=0)
if body:
    try:
        diff = (json.loads(body).get("data") or {}).get("diff") or []
        got = {f"{x.get('f13')}.{x.get('f12')}": x for x in (diff.values() if isinstance(diff, dict) else diff)}
        for c in cands:
            x = got.get(c)
            if x:
                ts = time.strftime("%m-%d %H:%M", time.localtime(int(x.get("f124") or 0)))
                w(f"  {c:12s} {x.get('f14')} | 最新 {x.get('f2')} | 昨收 {x.get('f18')} | 涨跌幅 {x.get('f3')} | 更新 {ts}")
            else:
                w(f"  {c:12s} （无返回）")
    except Exception as e:  # noqa: BLE001
        w("解析失败", e, body[:300])

w("\nDONE")
OUT.close()
