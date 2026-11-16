#!/usr/bin/env python3
"""
mkt.py — 财经热点写作的数据与校验工具（financial-hot-article skill v7.5）

设计原则
- 轻量：核心功能只用 Python 标准库；akshare / yfinance 为可选增强
- 可靠：每类数据至少两个源，自动重试、换源；A股行情新浪+腾讯双源核对
- 可追溯：每条输出带 source、数据日期和抓取时间；默认落盘到 ./fha-runs/日期/，供事实底稿引用
- 不假装：旧数据、盘中数据、扫描不完整、口径未验证，都会在输出里标出来

命令（python mkt.py <命令> -h 查看参数）
  now                      北京时间、星期、今天是否交易日、最近交易日、A股时段、是否走海外复盘
  weekday 2026-07-14       校验某日星期几、是否交易日
  scan                     第一步热点列表用：指数、成交、涨跌家数、板块两端、资金流、涨停梯队、强制覆盖提示
  quote 600519 300308 hk00700 usNVDA   行情；A股双源核对，标注收盘/盘中/旧数据，给出“涨超5%”式写法
  index [--verify]         主要指数、两市成交额（两种口径对照）、涨跌家数（--verify 用全市场计数核对）
  boards / flow            板块涨幅两端 / 主力资金两端（--type industry|concept）
  limit [--date]           涨停/跌停/炸板、封板率、连板梯队、行业聚集
  kline 300308 [--days]    日K线、区间涨跌、涨停日
  reports --kw/--code/--org [--days]   东财研报库检索，报告实际扫描覆盖的日期范围
  news cls [--hours 8] | news stock 300308   财联社电报（翻页覆盖指定时长）/ 个股新闻
  calendar [--date]        宏观数据日历，逐条标“已公布/未公布”（需 akshare）
  global [--symbols ...]   海外指数、美债、美元、商品、比特币；标注收盘/盘中与交易所日期
  ledger init              在 ./fha-runs/日期/ 生成事实底稿模板
  lint 稿件.md [--ledger 底稿.md] [--before 初稿.md]   机械检查 + 底稿对照 + 润色前后比对
  selftest                 逐个检查数据源

通用参数：--json 输出 JSON；--no-save 不落盘；--out DIR 指定落盘目录
"""
from __future__ import annotations

import argparse
import datetime as dt
import functools
import hashlib
import json
import os
import random
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable


def _use_bundled_venv() -> None:
    """skill 自带 .venv 时自动改用它（依赖装在 skill 文件夹内，不污染系统 Python）。"""
    if os.environ.get("MKT_IN_VENV") == "1":
        return
    root = Path(__file__).resolve().parent.parent
    for cand in (root / ".venv" / "Scripts" / "python.exe", root / ".venv" / "bin" / "python"):
        if cand.exists():
            try:
                if Path(sys.prefix).resolve() == (root / ".venv").resolve():
                    return
            except OSError:
                pass
            env = dict(os.environ, MKT_IN_VENV="1", PYTHONUTF8="1")
            sys.exit(subprocess.call([str(cand), str(Path(__file__).resolve()), *sys.argv[1:]], env=env))


_use_bundled_venv()

TZ = dt.timezone(dt.timedelta(hours=8))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0 Safari/537.36")
WEEK = "一二三四五六日"
EM_UT = "7eea3edcaed734bea9cbfc24409ed989"
EM_HOSTS = ["push2.eastmoney.com", "17.push2.eastmoney.com", "29.push2.eastmoney.com", "82.push2.eastmoney.com"]
EM_HIS_HOSTS = ["7.push2his.eastmoney.com", "91.push2his.eastmoney.com", "33.push2his.eastmoney.com",
                "push2his.eastmoney.com"]
EM_HEADERS = {"Referer": "https://quote.eastmoney.com/", "Accept-Language": "zh-CN,zh;q=0.9"}
SKILL_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = SKILL_ROOT / ".cache"

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


# ================================================================ 基础设施
class SourceError(Exception):
    pass


class NetDown(SourceError):
    """网络层失败（超时、连不上、403/429 等）：同一数据源本次运行内不再重试。"""


def now() -> dt.datetime:
    return dt.datetime.now(TZ)


def stamp() -> str:
    return now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 坏源记忆
# 某个主机超时或返回 403/429 后记 10 分钟（.cache/source_health.json），这段时间内所有命令直接跳过它，
# 不再每条命令都等一次超时。selftest 会无视记忆重新检测并刷新。设置环境变量 FHA_NO_HEALTH=1 可关闭。
HEALTH_TTL = 600
_HEALTH_LOCK = threading.Lock()
_HEALTH: dict[str, dict] | None = None
HEALTH_BYPASS = os.environ.get("FHA_NO_HEALTH") == "1"


def _health() -> dict[str, dict]:
    global _HEALTH
    if _HEALTH is None:
        try:
            _HEALTH = json.loads((CACHE_DIR / "source_health.json").read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            _HEALTH = {}
    return _HEALTH


def _health_save() -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (CACHE_DIR / "source_health.json").write_text(json.dumps(_health(), ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def host_down(host: str) -> str | None:
    if HEALTH_BYPASS:
        return None
    with _HEALTH_LOCK:
        e = _health().get(host)
    if e and time.time() - e["t"] < HEALTH_TTL:
        return f"{int(time.time() - e['t'])}秒前判定不可用：{e['why']}"
    return None


def _mark(host: str, why: str | None) -> None:
    with _HEALTH_LOCK:
        h = _health()
        if why is None:
            if host not in h:
                return
            h.pop(host)
        else:
            h[host] = {"t": time.time(), "why": why[:80], "at": stamp()}
        _health_save()


def _is_hard_failure(e: Exception | None) -> bool:
    """超时、403/429 这类“短时间内不会好”的失败才记；对方偶尔断开连接不记。"""
    if isinstance(e, urllib.error.HTTPError):
        return e.code in (401, 403, 407, 429, 451)
    if isinstance(e, (socket.timeout, TimeoutError)):
        return True
    if isinstance(e, urllib.error.URLError):
        return isinstance(e.reason, (socket.timeout, TimeoutError, socket.gaierror)) or "timed out" in str(e.reason)
    return False


def http_get(url: str, params: dict | None = None, headers: dict | None = None,
             encoding: str = "utf-8", timeout: float = 6, retries: int = 1) -> str:
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params, safe=":,+!*")
    host = urllib.parse.urlsplit(url).hostname or ""
    why = host_down(host)
    if why:
        raise NetDown(f"{host} 跳过（{why}）")
    h = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        h.update(headers)
    last: Exception | None = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read().decode(encoding, errors="replace")
            _mark(host, None)
            return body
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 404:  # 品种不存在，不算数据源故障
                raise SourceError(f"{url.split('?')[0]} -> {e}") from e
            if 400 <= e.code < 500:  # 403/429 重试没有用，直接报
                break
        except Exception as e:  # noqa: BLE001
            last = e
        if _is_hard_failure(last):  # 超时/403 重试也没用
            break
        if i < retries:
            time.sleep(0.4 * (i + 1) + random.random() * 0.2)
    if _is_hard_failure(last):
        _mark(host, f"{type(last).__name__}: {last}")
    raise NetDown(f"{url.split('?')[0]} -> {last}")


def parallel(tasks: dict[str, Callable[[], Any]], workers: int = 8) -> dict[str, tuple[bool, Any]]:
    """并发执行互不依赖的取数任务，返回 {名称: (是否成功, 结果或异常)}。"""
    out: dict[str, tuple[bool, Any]] = {}
    if not tasks:
        return out
    trade_status()  # 先在主线程算好交易日状态（带缓存），避免各线程重复取K线
    with ThreadPoolExecutor(max_workers=min(workers, len(tasks))) as ex:
        futs = {k: ex.submit(fn) for k, fn in tasks.items()}
        for k, f in futs.items():
            try:
                out[k] = (True, f.result())
            except Exception as e:  # noqa: BLE001
                out[k] = (False, e)
    return out


def take(res: dict[str, tuple[bool, Any]], key: str) -> Any:
    ok, v = res[key]
    if ok:
        return v
    raise v


def http_json(url: str, params: dict | None = None, headers: dict | None = None, **kw) -> Any:
    txt = http_get(url, params, headers, **kw).strip()
    if txt and txt[0] not in "{[":  # 兼容 jsonp
        txt = txt[txt.find("(") + 1: txt.rfind(")")]
    return json.loads(txt)


def first_ok(*fns: Callable[[], Any]) -> tuple[Any, list[str]]:
    """依次尝试多个数据源，返回第一个成功的结果和失败记录。"""
    errors = []
    for fn in fns:
        try:
            return fn(), errors
        except Exception as e:  # noqa: BLE001
            errors.append(f"{getattr(fn, '__name__', 'src')}: {e}")
    raise SourceError("; ".join(errors))


def em_rotate(hosts: list[str], path: str, params: dict) -> Any:
    """东方财富接口按主机轮换：单个主机断连时换下一个。"""
    errs = []
    for host in random.sample(hosts, len(hosts)):
        try:
            return http_json(f"https://{host}{path}", params, EM_HEADERS, retries=0)  # 换主机就是重试
        except Exception as e:  # noqa: BLE001
            errs.append(f"{host}: {e}")
    raise SourceError("eastmoney " + path + ": " + " | ".join(errs[-2:]))


def em_clist(params: dict) -> list[dict]:
    base = {"pn": 1, "pz": 20, "po": 1, "np": 1, "fltt": 2, "invt": 2, "ut": "bd1d9ddb04089700cf9c27f6f7426281"}
    base.update(params)
    d = em_rotate(EM_HOSTS, "/api/qt/clist/get", base)
    diff = (d.get("data") or {}).get("diff") or []
    return list(diff.values()) if isinstance(diff, dict) else diff


def num(x: Any) -> float | None:
    try:
        if x in (None, "", "-"):
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def yi(x: Any) -> str:
    v = num(x)
    if v is None:
        return "-"
    if abs(v) >= 1e12:
        return f"{v / 1e12:.2f}万亿"
    return f"{v / 1e8:.1f}亿"


def need_akshare():
    try:
        import akshare as ak  # noqa: WPS433
        return ak
    except ImportError as e:
        raise SourceError("需要 akshare：双击 setup.bat 或 pip install -U akshare") from e


# ================================================================ 交易日历
def _cached_calendar() -> set[str] | None:
    """新浪交易日历（经 akshare），本地缓存7天；含当年已公布的未来交易日。"""
    f = CACHE_DIR / "trade_dates.json"
    try:
        if f.exists() and time.time() - f.stat().st_mtime < 7 * 86400:
            return set(json.loads(f.read_text(encoding="utf-8")))
    except Exception:  # noqa: BLE001
        pass
    try:
        ak = need_akshare()
        dates = [str(d) for d in ak.tool_trade_date_hist_sina()["trade_date"]]
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(dates), encoding="utf-8")
        return set(dates)
    except Exception:  # noqa: BLE001
        return None


@functools.lru_cache(maxsize=1)
def recent_trade_days() -> list[str]:
    """从上证指数日K线取最近交易日（交易时段内含当天）。"""
    rows, _ = first_ok(lambda: tencent_kline("sh000001", 12), lambda: em_kline("sh000001", 12))
    return [r["date"] for r in rows]


@functools.lru_cache(maxsize=1)
def trade_status() -> dict:
    t = now()
    today = t.strftime("%Y-%m-%d")
    hm = t.hour * 100 + t.minute
    cal = _cached_calendar()
    info: dict[str, Any] = {"calendar": "sina(akshare)" if cal else None}
    try:
        days = recent_trade_days()
    except Exception as e:  # noqa: BLE001
        days, info["kline_error"] = [], str(e)
    if cal and any(d.startswith(today[:4]) for d in cal):
        is_td: bool | None = today in cal
        info["basis"] = "交易日历"
    elif days and days[-1] == today:
        is_td, info["basis"] = True, "今日K线已出现"
    elif t.weekday() >= 5:
        is_td, info["basis"] = False, "周末"
    elif hm >= 935 and days:
        is_td, info["basis"] = False, "工作日但今日无K线，按休市处理"
    elif days:
        is_td, info["basis"] = None, "盘前且无交易日历，无法确认今天是否交易日"
    else:
        is_td, info["basis"] = None, "取不到K线和交易日历，无法确认今天是否交易日"
    if days:
        last = days[-1]
        if last == today and hm < 925 and len(days) > 1:
            last = days[-2]
    else:
        d = t - dt.timedelta(days=1 if hm < 925 else 0)
        while d.weekday() >= 5:
            d -= dt.timedelta(days=1)
        last, info["last_basis"] = d.strftime("%Y-%m-%d"), "按周末推算，未排除节假日"
    info.update({"today": today, "is_trading_day": is_td, "last_trade_date": last})
    return info


def session_state(t: dt.datetime | None = None) -> str:
    t = t or now()
    st = trade_status()
    if st["is_trading_day"] is False:
        return "休市（周末或节假日）"
    hm = t.hour * 100 + t.minute
    if hm < 915:
        s = "盘前"
    elif hm < 930:
        s = "集合竞价"
    elif hm < 1130:
        s = "上午盘中"
    elif hm < 1300:
        s = "午间休市"
    elif hm < 1430:
        s = "下午盘中"
    elif hm < 1500:
        s = "尾盘（14:30后正式稿建议等收盘数据）"
    else:
        s = "已收盘"
    return s + ("（未确认今日是否交易日）" if st["is_trading_day"] is None else "")


def ltd_compact() -> str:
    return trade_status()["last_trade_date"].replace("-", "")


# ================================================================ 写作辅助
def phrase(pct: float | None, limit_hit: str = "") -> str:
    """把涨跌幅转成公众号自然写法：涨超5%、涨近8%、跌约0.3%。"""
    if limit_hit:
        return limit_hit
    if pct is None:
        return "-"
    word = "涨" if pct >= 0 else "跌"
    f = abs(pct)
    if f < 0.05:
        return "基本收平"
    if f < 1:
        return f"{word}约{f:.1f}%"
    n, frac = int(f), f - int(f)
    if frac < 0.25:
        return f"{word}超{n}%"
    if frac >= 0.7:
        return f"{word}近{n + 1}%"
    return f"{word}{f:.1f}%"


def limit_ratio(code: str, name: str = "") -> float:
    """涨跌停幅度：创业板/科创板20%（含ST），北交所30%（含ST），主板10%，主板ST 5%。"""
    c = code[-6:]
    if c.startswith(("300", "301", "302", "688", "689")):
        return 0.20
    if c.startswith(("8", "4", "92")):
        return 0.30
    if "ST" in name.upper():
        return 0.05
    return 0.10


def limit_state(code: str, name: str, price: float | None, prev: float | None) -> str:
    if not price or not prev or code[:5] in ("sh000", "sz399", "bj899"):
        return ""
    r = limit_ratio(code, name)
    up, dn = round(prev * (1 + r) + 1e-9, 2), round(prev * (1 - r) + 1e-9, 2)
    tag = {0.20: "20CM", 0.30: "30CM"}.get(r, "")
    if price >= up - 0.001:
        return f"{tag}涨停" if tag else "涨停"
    if price <= dn + 0.001:
        return "跌停"
    return ""


def norm_code(raw: str) -> str:
    s = raw.strip().lower().replace(".sh", "").replace(".sz", "").replace(".bj", "")
    if s.startswith(("sh", "sz", "bj", "hk")):
        return s
    if s.startswith("us"):
        return "us" + s[2:].upper()
    if s.isdigit() and len(s) == 5:
        return "hk" + s
    if s.isdigit() and len(s) == 6:
        if s.startswith(("8", "4", "92")):
            return "bj" + s
        if s.startswith(("6", "9", "5")):
            return "sh" + s
        return "sz" + s
    return "us" + raw.strip().upper()


# ================================================================ 行情：新浪 + 腾讯
def sina_quotes(codes: list[str]) -> dict[str, dict]:
    a = [c for c in codes if c[:2] in ("sh", "sz", "bj")]
    if not a:
        return {}
    txt = http_get("https://hq.sinajs.cn/list=" + ",".join(a),
                   headers={"Referer": "https://finance.sina.com.cn/"}, encoding="gbk")
    out = {}
    for line in txt.splitlines():
        if "hq_str_" not in line or '=""' in line:
            continue
        code = line.split("hq_str_")[1].split("=")[0]
        f = line.split('"')[1].split(",")
        if len(f) < 32:
            continue
        price, prev = num(f[3]), num(f[2])
        out[code] = {"code": code, "name": f[0], "price": price, "prev_close": prev,
                     "pct": round((price - prev) / prev * 100, 2) if price and prev else None,
                     "amount": num(f[9]), "data_date": f[30], "data_time": f[31], "source": "sina"}
    return out


def tencent_quotes(codes: list[str]) -> dict[str, dict]:
    txt = http_get("https://qt.gtimg.cn/q=" + ",".join(codes), encoding="gbk")
    out = {}
    for line in txt.splitlines():
        if "=" not in line or '"' not in line:
            continue
        code = line.split("=")[0].replace("v_", "").strip()
        f = line.split('"')[1].split("~")
        if len(f) < 40:
            continue
        ts = f[30]
        ddate = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}" if len(ts) >= 8 and ts[:8].isdigit() else ts[:10]
        dtime = f"{ts[8:10]}:{ts[10:12]}:{ts[12:14]}" if len(ts) >= 14 and ts[:14].isdigit() else ""
        rec = {"code": code, "name": f[1], "price": num(f[3]), "prev_close": num(f[4]),
               "pct": num(f[32]), "data_date": ddate, "data_time": dtime, "source": "tencent"}
        if code[:2] in ("sh", "sz", "bj"):
            amt = num(f[37])
            rec.update({"amount": amt * 1e4 if amt else None, "turnover": num(f[38]),
                        "pe": num(f[39]), "mcap": (num(f[45]) or 0) * 1e8 or None})
        out[code] = rec
    return out


def em_quotes(codes: list[str]) -> dict[str, dict]:
    """东财 A股/指数行情：新浪或腾讯不通时补上第二个源，保住两源核对。"""
    a = [c for c in codes if c[:2] in ("sh", "sz", "bj")]
    if not a:
        return {}
    sec = {("1." if c.startswith("sh") else "0.") + c[2:]: c for c in a}
    d = em_rotate(EM_HOSTS, "/api/qt/ulist.np/get",
                  {"fltt": 2, "secids": ",".join(sec), "fields": "f2,f3,f6,f12,f13,f14,f18,f124", "ut": EM_UT})
    diff = (d.get("data") or {}).get("diff") or []
    out = {}
    for x in (diff.values() if isinstance(diff, dict) else diff):
        c = sec.get(f"{x.get('f13')}.{x.get('f12')}")
        price, prev = num(x.get("f2")), num(x.get("f18"))
        if not c or not price or not prev:
            continue
        ts = int(num(x.get("f124")) or 0)
        t = dt.datetime.fromtimestamp(ts, TZ) if ts else None
        out[c] = {"code": c, "name": x.get("f14"), "price": price, "prev_close": prev,
                  "pct": round((price - prev) / prev * 100, 2), "amount": num(x.get("f6")),
                  "data_date": t.strftime("%Y-%m-%d") if t else "", "data_time": t.strftime("%H:%M:%S") if t else "",
                  "source": "eastmoney"}
    return out


def basis_of(rec: dict) -> str:
    """数据口径：收盘 / 盘中（截至） / 旧数据。"""
    st = trade_status()
    d = rec.get("data_date") or ""
    if d and d < st["last_trade_date"]:
        return f"旧数据（{d}，不是最近交易日{st['last_trade_date']}）"
    if d == st["today"] and st["is_trading_day"] is not False:
        if (rec.get("data_time") or "15:00:00") < "15:00:00":
            return f"盘中（截至{(rec.get('data_time') or '')[:5]}）"
    return f"收盘（{d}）"


def cmd_quote(args) -> list[dict]:
    codes = [norm_code(c) for c in args.codes]
    errs: list[str] = []
    res = parallel({"sina": lambda: sina_quotes(codes), "tencent": lambda: tencent_quotes(codes)})
    s = res["sina"][1] if res["sina"][0] else {}
    t = res["tencent"][1] if res["tencent"][0] else {}
    for k, (ok, v) in res.items():
        if not ok:
            errs.append(f"{k}: {str(v)[:160]}")
    e: dict = {}
    need = [c for c in codes if c[:2] in ("sh", "sz", "bj") and not (s.get(c) and t.get(c))]
    if need:  # 新浪或腾讯缺了，用东财补第二个源
        try:
            e = em_quotes(need)
        except Exception as ex:  # noqa: BLE001
            errs.append(f"eastmoney: {str(ex)[:160]}")
    rows = []
    for c in codes:
        srcs = [x for x in (s.get(c), t.get(c), e.get(c)) if x]
        if not srcs:
            rows.append({"code": c, "error": "所有行情源都没有数据", "errors": errs})
            continue
        a = srcs[0]
        b = srcs[1] if len(srcs) > 1 else None
        r = dict(a)
        if t.get(c):
            for k in ("turnover", "pe", "mcap"):
                if t[c].get(k) is not None:
                    r[k] = t[c][k]
        check = "single-source"
        if a and b and a.get("pct") is not None and b.get("pct") is not None:
            diff = abs(a["pct"] - b["pct"])
            check = "ok" if diff <= 0.3 else f"MISMATCH {a['source']} {a['pct']}% vs {b['source']} {b['pct']}%"
        r["cross_check"] = check
        r["source"] = "+".join(x["source"] for x in (a, b) if x)
        is_a = c[:2] in ("sh", "sz", "bj")
        lim = limit_state(c, r["name"], r.get("price"), r.get("prev_close")) if is_a else ""
        r["limit"] = lim
        r["write_as"] = phrase(r.get("pct"), lim)
        if is_a:
            try:
                r["basis"] = basis_of(r)
            except Exception as e:  # noqa: BLE001
                r["basis"] = f"无法判断（{e}）"
            if r["basis"].startswith("盘中"):
                r["write_as"] += "（盘中）"
            if r["basis"].startswith("旧数据"):
                r["write_as"] = "不要写入：" + r["basis"]
        r["fetched_at"] = stamp()
        rows.append(r)
    return rows


# ================================================================ 指数 / 成交 / 涨跌家数
INDEXES = {"sh000001": "上证指数", "sz399001": "深证成指", "sz399006": "创业板指",
           "sh000688": "科创50", "sh000300": "沪深300", "bj899050": "北证50", "sz399106": "深证综指"}
SHSZ_FS = "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23"


def breadth_ulist() -> dict:
    d = em_rotate(EM_HOSTS, "/api/qt/ulist.np/get",
                  {"fltt": 2, "secids": "1.000001,0.399001", "fields": "f12,f104,f105,f106", "ut": EM_UT})
    diff = (d.get("data") or {}).get("diff") or []
    if not diff:
        raise SourceError("empty")
    return {"up": sum(int(x.get("f104") or 0) for x in diff), "down": sum(int(x.get("f105") or 0) for x in diff),
            "flat": sum(int(x.get("f106") or 0) for x in diff), "scope": "沪深（东财指数统计）",
            "source": "eastmoney ulist"}


def breadth_legu() -> dict:
    """乐咕乐股赚钱效应统计（经 akshare），东财全市场计数不可用时核对涨跌家数。"""
    ak = need_akshare()
    df = ak.stock_market_activity_legu()
    m = {str(r["item"]).strip(): r["value"] for _, r in df.iterrows()}
    up, down, flat = num(m.get("上涨")), num(m.get("下跌")), num(m.get("平盘"))
    if up is None or down is None:
        raise SourceError("no data")
    return {"up": int(up), "down": int(down), "flat": int(flat or 0), "scope": "乐咕乐股统计（可能含北交所）",
            "date": str(m.get("统计日期", "")), "source": "legulegu via akshare"}


def breadth_count(fs: str = SHSZ_FS, scope: str = "沪深A股逐只计数") -> dict:
    base = {"pn": 1, "pz": 100, "po": 1, "np": 1, "fltt": 2, "invt": 2, "fid": "f3", "fs": fs,
            "fields": "f3", "ut": "bd1d9ddb04089700cf9c27f6f7426281"}
    d = em_rotate(EM_HOSTS, "/api/qt/clist/get", base)
    total = int((d.get("data") or {}).get("total") or 0)
    rows = list((d.get("data") or {}).get("diff") or [])
    for pn in range(2, min(80, -(-total // 100)) + 1):  # 东财每页上限100条
        dd = em_rotate(EM_HOSTS, "/api/qt/clist/get", dict(base, pn=pn))
        rows += list((dd.get("data") or {}).get("diff") or [])
        time.sleep(0.05)
    pcts = [p for p in (num(x.get("f3")) for x in rows) if p is not None]
    if len(pcts) < 1000:
        raise SourceError(f"only {len(pcts)} rows")
    return {"up": sum(p > 0 for p in pcts), "down": sum(p < 0 for p in pcts), "flat": sum(p == 0 for p in pcts),
            "scope": scope, "source": "eastmoney clist"}


def cmd_index(args) -> dict:
    q = cmd_quote(argparse.Namespace(codes=list(INDEXES)))
    by = {r["code"]: r for r in q}
    for r in q:
        r["name"] = INDEXES.get(r["code"], r.get("name"))
        r.pop("limit", None)

    def amt(code):
        return (by.get(code) or {}).get("amount")

    a1 = amt("sh000001") + amt("sz399001") if amt("sh000001") and amt("sz399001") else None
    a2 = amt("sh000001") + amt("sz399106") if amt("sh000001") and amt("sz399106") else None
    amount: dict[str, Any] = {"上证+深证成指": yi(a1), "上证+深证综指": yi(a2)}
    if a1 and a2 and abs(a1 - a2) / a2 > 0.03:
        amount["check"] = "两种口径相差超过3%：以当日财联社或交易所公布的两市成交额为准，并在底稿注明口径"
    else:
        amount["check"] = "两种口径接近"
    out: dict[str, Any] = {"indexes": [r for r in q if r["code"] != "sz399106"], "two_market_amount": amount,
                           "session": session_state(), "fetched_at": stamp()}
    try:
        out["breadth"] = breadth_ulist()
    except Exception as e:  # noqa: BLE001
        out["breadth"] = {"error": str(e)}
    if getattr(args, "verify", False) or "error" in out["breadth"]:
        try:
            cnt, cerrs = first_ok(breadth_count, breadth_legu)
            if cerrs:
                cnt["fallback_errors"] = cerrs
            if "error" in out["breadth"]:
                out["breadth"] = cnt
            else:
                u = out["breadth"]
                gap = abs(u["up"] - cnt["up"]) + abs(u["down"] - cnt["down"])
                tol = 100 if cnt["source"] == "eastmoney clist" else 250
                out["breadth_verify"] = {**cnt, "gap": gap,
                                         "check": "一致" if gap <= tol else "两种统计相差较大：正文改用财联社收评数字，或注明口径"}
        except Exception as e:  # noqa: BLE001
            out["breadth_verify"] = {"error": str(e)}
    return out


# ================================================================ 板块 / 资金流
BOARD_FS = {"industry": "m:90 t:2 f:!50", "concept": "m:90 t:3 f:!50"}
# 东财行业板块是一二三级混排（共约496个，如“电子/半导体/半导体设备”同时出现）。
# 2026-11-05 实测：加 s:2 只返回一级（31个），加 s:4 只返回二级（128个）。收评和排行默认用二级。
INDUSTRY_LEVEL_FS = {"1": "m:90 t:2 f:!50 s:2", "2": "m:90 t:2 f:!50 s:4", "all": "m:90 t:2 f:!50"}
LEVEL_NOTE = {"1": "东财一级行业", "2": "东财二级行业", "all": "东财行业一二三级混排（同一行业会重复出现）"}
BOARD_FIELDS = "f12,f14,f3,f62,f104,f105,f128,f136,f140,f20"
LEVEL_CACHE = CACHE_DIR / "industry_levels.json"


def _board_fs(kind: str, level: str) -> str:
    return INDUSTRY_LEVEL_FS[level] if kind == "industry" else BOARD_FS[kind]


def em_clist_all(params: dict, max_pages: int = 6) -> list[dict]:
    """东财 clist 翻页取全（每页上限100条）。"""
    rows: list[dict] = []
    for pn in range(1, max_pages + 1):
        base = {"pn": pn, "pz": 100, "po": 1, "np": 1, "fltt": 2, "invt": 2, "ut": "bd1d9ddb04089700cf9c27f6f7426281"}
        base.update(params)
        base["pn"], base["pz"] = pn, 100
        d = em_rotate(EM_HOSTS, "/api/qt/clist/get", base)
        data = d.get("data") or {}
        diff = data.get("diff") or []
        diff = list(diff.values()) if isinstance(diff, dict) else diff
        rows += diff
        if not diff or len(rows) >= int(data.get("total") or 0):
            break
        time.sleep(0.05)
    return rows


def _save_level_codes(level: str, rows: list[dict]) -> None:
    try:
        d = json.loads(LEVEL_CACHE.read_text(encoding="utf-8")) if LEVEL_CACHE.exists() else {}
        d[level] = {str(x.get("f12")): x.get("f14") for x in rows if x.get("f12")}
        d["updated"] = stamp()
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        LEVEL_CACHE.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _level_codes(level: str) -> dict[str, str] | None:
    """{板块代码: 名称}，东财直连成功时缓存。"""
    try:
        return json.loads(LEVEL_CACHE.read_text(encoding="utf-8"))[level] or None
    except Exception:  # noqa: BLE001
        return None


def _board_row(x: dict) -> dict:
    return {"code": x.get("f12"), "name": x.get("f14"), "pct": num(x.get("f3")),
            "write_as": phrase(num(x.get("f3"))), "main_net_inflow": num(x.get("f62")),
            "main_net_inflow_text": yi(x.get("f62")), "up": x.get("f104"), "down": x.get("f105"),
            "leader": x.get("f128"), "leader_pct": num(x.get("f136"))}


_MEMO: dict[tuple, Any] = {}
_MEMO_LOCKS: dict[tuple, threading.Lock] = {}
_MEMO_GUARD = threading.Lock()


def _memo_boards(fn: Callable) -> Callable:
    """同一次运行里同一板块数据只取一次（recap 的个数和排行共用）；参数 top 不影响取数，不进键。"""
    @functools.wraps(fn)
    def wrap(kind: str, fid: str, top: int = 100, level: str = "2"):
        key = (fn.__name__, kind, fid, level)
        with _MEMO_GUARD:
            lock = _MEMO_LOCKS.setdefault(key, threading.Lock())
        with lock:
            if key not in _MEMO:
                _MEMO[key] = fn(kind, fid, top, level)
            hi, lo = _MEMO[key]
            return list(hi), list(lo)
    return wrap


@_memo_boards
def _boards_em(kind: str, fid: str, top: int, level: str = "2") -> tuple[list[dict], list[dict]]:
    fs = _board_fs(kind, level)
    if kind == "industry":  # 行业板块数量有限，翻页取全后本地排序，涨跌个数也能用同一份数据
        rows = em_clist_all({"fs": fs, "fid": fid, "po": 1, "fields": BOARD_FIELDS})
        if not rows:
            raise SourceError("empty")
        if level != "all":
            _save_level_codes(level, rows)
        out = [_board_row(x) for x in rows]
        key = "pct" if fid == "f3" else "main_net_inflow"
        out = [x for x in out if x.get(key) is not None]
        out.sort(key=lambda x: x[key], reverse=True)
        return out, list(reversed(out))
    hi = em_clist({"fs": fs, "fid": fid, "po": 1, "pz": 100, "fields": BOARD_FIELDS})
    lo = em_clist({"fs": fs, "fid": fid, "po": 0, "pz": 100, "fields": BOARD_FIELDS})
    if not hi:
        raise SourceError("empty")
    return [_board_row(x) for x in hi], [_board_row(x) for x in lo]


@_memo_boards
def _boards_ak(kind: str, fid: str, top: int, level: str = "2") -> tuple[list[dict], list[dict]]:
    """东财数据经 akshare（requests 客户端），东财直连被断开时的第一备用。
    akshare 返回全部层级；有层级缓存（东财直连成功时写入）就按层级过滤，否则在结果里注明混排。"""
    ak = need_akshare()
    rows = []
    if fid == "f3":
        df = ak.stock_board_industry_name_em() if kind == "industry" else ak.stock_board_concept_name_em()
        for _, r in df.iterrows():
            pct = num(r.get("涨跌幅"))
            rows.append({"name": r.get("板块名称"), "code": r.get("板块代码"), "pct": pct, "write_as": phrase(pct),
                         "up": r.get("上涨家数"), "down": r.get("下跌家数"), "leader": r.get("领涨股票"),
                         "leader_pct": num(r.get("领涨股票-涨跌幅"))})
        key = "pct"
    else:
        df = ak.stock_sector_fund_flow_rank(indicator="今日",
                                            sector_type="行业资金流" if kind == "industry" else "概念资金流")
        for _, r in df.iterrows():
            pct, flow = num(r.get("今日涨跌幅")), num(r.get("今日主力净流入-净额"))
            rows.append({"name": r.get("名称"), "pct": pct, "write_as": phrase(pct), "main_net_inflow": flow,
                         "main_net_inflow_text": yi(flow), "leader": r.get("今日主力净流入最大股")})
        key = "main_net_inflow"
    rows = [x for x in rows if x.get(key) is not None]
    lv = _level_codes(level) if kind == "industry" and level != "all" else None
    if lv:  # 有代码按代码对，资金流接口没有板块代码就按名称对
        names = set(lv.values())
        rows = [x for x in rows if ((str(x["code"]) in lv) if x.get("code") else (x.get("name") in names))]
    if not rows:
        raise SourceError("empty")
    rows.sort(key=lambda x: x[key], reverse=True)
    return rows, list(reversed(rows))


@_memo_boards
def _boards_sina(kind: str, fid: str, top: int, level: str = "") -> tuple[list[dict], list[dict]]:
    """新浪行业/概念板块（分类与东财不同，没有资金流）。"""
    if fid != "f3":
        raise SourceError("新浪没有板块资金流")
    url = ("https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php" if kind == "industry"
           else "https://money.finance.sina.com.cn/q/view/newFLJK.php?param=class")
    txt = http_get(url, headers={"Referer": "https://finance.sina.com.cn/"}, encoding="gbk")
    data = json.loads(txt[txt.find("{"): txt.rfind("}") + 1])
    rows = []
    for v in data.values():
        f = v.split(",")
        if len(f) < 13:
            continue
        pct = num(f[5])
        rows.append({"name": f[1], "code": f[0], "pct": pct, "write_as": phrase(pct), "count": f[2],
                     "leader": f[12], "leader_pct": num(f[9])})
    rows = [x for x in rows if x["pct"] is not None]
    if not rows:
        raise SourceError("empty")
    rows.sort(key=lambda x: x["pct"], reverse=True)
    return rows, list(reversed(rows))


def _board_size(x: dict) -> int | None:
    if x.get("count") not in (None, ""):
        return int(num(x["count"]) or 0)
    up, down = num(x.get("up")), num(x.get("down"))
    return int((up or 0) + (down or 0)) if up is not None or down is not None else None


def _clean_boards(rows: list[dict], top: int, min_size: int) -> tuple[list[dict], list[dict]]:
    """同名不同层级（如 白酒Ⅱ / 白酒Ⅲ）只留一个；成分股太少的板块不进排行。"""
    seen, kept, small = set(), [], []
    for x in rows:
        base = re.sub(r"[ⅠⅡⅢⅣ]+$", "", str(x.get("name") or "")).strip()
        if base in seen:
            continue
        seen.add(base)
        size = _board_size(x)
        if size is not None and size < min_size:
            small.append({**x, "size": size})
            continue
        kept.append(x)
        if len(kept) >= top:
            break
    return kept, small[:5]


def boards(kind: str, fid: str, top: int, min_size: int = 5, level: str = "2") -> dict:
    top = min(top, 100)
    srcs = {"eastmoney": lambda: _boards_em(kind, fid, top, level),
            "eastmoney via akshare": lambda: _boards_ak(kind, fid, top, level),
            "sina": lambda: _boards_sina(kind, fid, top)}
    errs, used, hi, lo = [], None, [], []
    for name, fn in srcs.items():
        try:
            hi, lo = fn()
            used = name
            break
        except Exception as e:  # noqa: BLE001
            errs.append(f"{name}: {str(e)[:120]}")
    if not used:
        raise SourceError(" | ".join(errs))
    hi, small_hi = _clean_boards(hi, top, min_size)
    lo, small_lo = _clean_boards(lo, top, min_size)
    out = {"type": kind, "sort_by": {"f3": "涨跌幅", "f62": "主力净流入"}[fid], "top": hi, "bottom": lo,
           "excluded_small_boards": {"note": f"成分股少于{min_size}只，未进排行，仅供参考",
                                     "top_side": small_hi, "bottom_side": small_lo},
           "session": session_state(), "source": used, "fetched_at": stamp()}
    if errs:
        out["fallback_errors"] = errs
    if kind == "industry":
        out["level"], out["level_note"] = _level_tag(used, level)
    if used == "sina":
        out["note"] = "新浪板块分类与东财不同，板块名称以新浪为准写入底稿"
    return out


def _level_tag(used: str, level: str) -> tuple[str, str]:
    """数据实际是哪一层级：东财直连按 fs 取；akshare 有层级缓存才算过滤过；新浪是自己的分类。"""
    if used == "sina":
        return "sina", "新浪行业分类（约49个，与东财不同）"
    if used == "eastmoney via akshare" and level != "all" and not _level_codes(level):
        return "all", LEVEL_NOTE["all"] + "：akshare 备用源，没有层级缓存，排行里同一行业可能重复"
    return level, LEVEL_NOTE[level]


def cmd_boards(args) -> dict:
    return boards(args.type, "f3", args.top, args.min_size, args.level)


def cmd_flow(args) -> dict:
    return boards(args.type, "f62", args.top, args.min_size, args.level)


# ================================================================ 涨停 / 跌停 / 炸板
def em_pool(kind: str, date: str) -> list[dict]:
    path, sort = {"zt": ("getTopicZTPool", "fbt:asc"), "dt": ("getTopicDTPool", "fund:asc"),
                  "zb": ("getTopicZBPool", "fbt:asc")}[kind]
    d = http_json(f"https://push2ex.eastmoney.com/{path}",
                  {"ut": EM_UT, "dpt": "wz.ztzt", "Pageindex": 0, "pagesize": 10000, "sort": sort, "date": date},
                  {"Referer": "https://quote.eastmoney.com/ztb/"})
    return ((d.get("data") or {}).get("pool")) or []


def cmd_limit(args) -> dict:
    date = (args.date or ltd_compact()).replace("-", "")
    zt, dtp, zb = em_pool("zt", date), em_pool("dt", date), em_pool("zb", date)
    ladder: dict[int, list[str]] = {}
    for x in zt:
        ladder.setdefault(int(x.get("lbc") or 1), []).append(x.get("n"))
    inds: Counter = Counter(x.get("hybk") or "未分类" for x in zt)
    seal = round(len(zt) / (len(zt) + len(zb)) * 100, 1) if (zt or zb) else None
    ss = session_state()
    note = "盘中数据，收盘后需重拉" if date == trade_status()["today"].replace("-", "") and not ss.startswith("已收盘") else ""
    return {
        "date": date, "session_note": note, "limit_up": len(zt), "limit_down": len(dtp), "broken": len(zb),
        "seal_rate_pct": seal,
        "ladder": {f"{k}连板": v for k, v in sorted(ladder.items(), reverse=True)},
        "top_industries": inds.most_common(10),
        "limit_up_list": [{"name": x.get("n"), "code": x.get("c"), "pct": num(x.get("zdp")),
                           "boards": x.get("lbc"), "first_seal": str(x.get("fbt", "")).zfill(6),
                           "broken_times": x.get("zbc"), "industry": x.get("hybk"),
                           "seal_fund": yi(x.get("fund")), "mcap": yi(x.get("tshare"))} for x in zt],
        "limit_down_list": [{"name": x.get("n"), "code": x.get("c"), "pct": num(x.get("zdp")),
                             "industry": x.get("hybk")} for x in dtp],
        "source": "eastmoney push2ex", "fetched_at": stamp(),
    }


# ================================================================ K线
def em_kline(code: str, days: int) -> list[dict]:
    c = norm_code(code)
    secid = ("1." if c.startswith("sh") else "0.") + c[2:]
    beg = (now() - dt.timedelta(days=days * 2 + 10)).strftime("%Y%m%d")
    d = em_rotate(EM_HIS_HOSTS, "/api/qt/stock/kline/get",
                  {"secid": secid, "fields1": "f1,f2,f3,f4,f5,f6",
                   "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                   "klt": 101, "fqt": 1, "beg": beg, "end": "20500101", "ut": EM_UT})
    rows = []
    for line in ((d.get("data") or {}).get("klines") or [])[-days:]:
        p = line.split(",")  # 日期,开,收,高,低,量,额,振幅,涨跌幅,涨跌额,换手
        rows.append({"date": p[0], "open": num(p[1]), "close": num(p[2]), "high": num(p[3]),
                     "low": num(p[4]), "amount": num(p[6]), "pct": num(p[8]), "turnover": num(p[10])})
    if not rows:
        raise SourceError("empty")
    return rows


def tencent_kline(code: str, days: int) -> list[dict]:
    c = norm_code(code)
    d = http_json("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
                  {"param": f"{c},day,,,{days + 1},qfq"})
    node = (d.get("data") or {}).get(c) or {}
    k = node.get("qfqday") or node.get("day") or []
    rows, prev = [], None
    for p in k:
        close = num(p[2])
        rows.append({"date": p[0], "open": num(p[1]), "close": close, "high": num(p[3]), "low": num(p[4]),
                     "pct": round((close - prev) / prev * 100, 2) if prev and close else None})
        prev = close
    if not rows:
        raise SourceError("empty")
    return rows[-days:]


def cmd_kline(args) -> dict:
    c = norm_code(args.code)

    def eastmoney():
        return em_kline(c, args.days)

    def tencent():
        return tencent_kline(c, args.days)

    rows, errs = first_ok(eastmoney, tencent)
    name = ""
    try:
        name = tencent_quotes([c]).get(c, {}).get("name", "")
    except Exception:  # noqa: BLE001
        pass
    r = limit_ratio(c, name) * 100
    for x in rows:
        x["limit_up"] = bool(x.get("pct") is not None and x["pct"] >= r - 0.3)
    closes = [x["close"] for x in rows if x.get("close")]
    chg = round((closes[-1] / closes[0] - 1) * 100, 2) if len(closes) > 1 else None
    return {"code": c, "name": name, "limit_ratio_pct": r, "rows": rows, "period_change_pct": chg,
            "period": f"{rows[0]['date']} 至 {rows[-1]['date']}",
            "limit_up_days": [x["date"] for x in rows if x["limit_up"]],
            "source": "eastmoney" if not errs else "tencent", "fallback_errors": errs, "fetched_at": stamp()}


# ================================================================ 研报（核验券商观点）
def cmd_reports(args) -> dict:
    end = now().date()
    beg = end - dt.timedelta(days=args.days)
    out, coverage = [], {}
    qtypes = ["0"] if args.code else ["0", "1"]  # 0 个股研报，1 行业研报
    max_pages = 3 if args.code else args.max_pages
    for qt in qtypes:
        scanned, oldest, complete = 0, None, False
        for page in range(1, max_pages + 1):
            params = {"industryCode": "*", "pageSize": 100, "industry": "*", "rating": "*", "ratingChange": "*",
                      "beginTime": beg.isoformat(), "endTime": (end + dt.timedelta(days=1)).isoformat(),
                      "pageNo": page, "fields": "", "qType": qt, "orgCode": "",
                      "code": norm_code(args.code)[-6:] if args.code else "",
                      "rcode": "", "p": page, "pageNum": page, "pageNumber": page}
            d = http_json("https://reportapi.eastmoney.com/report/list", params,
                          {"Referer": "https://data.eastmoney.com/"})
            data = d.get("data") or []
            scanned += len(data)
            for x in data:
                pdate = (x.get("publishDate") or "")[:10]
                if pdate:
                    oldest = min(oldest, pdate) if oldest else pdate
                title, org = x.get("title") or "", x.get("orgSName") or ""
                hay = title + (x.get("industryName") or "") + (x.get("indvInduName") or "") + (x.get("stockName") or "")
                if args.kw and args.kw not in hay:
                    continue
                if args.org and args.org not in org:
                    continue
                code = x.get("infoCode")
                kind = "stock" if qt == "0" else "industry"
                out.append({"date": pdate, "org": org, "title": title,
                            "stock": x.get("stockName"), "industry": x.get("industryName") or x.get("indvInduName"),
                            "researcher": x.get("researcher"), "rating": x.get("emRatingName"),
                            "page": f"https://data.eastmoney.com/report/zw_{kind}.jshtml?infocode={code}",
                            "pdf": f"https://pdf.dfcfw.com/pdf/H3_{code}_1.pdf"})
            if len(data) < 100 or (oldest and oldest <= beg.isoformat()):
                complete = True
                break
        coverage["个股研报" if qt == "0" else "行业研报"] = {
            "scanned": scanned, "covered_from": oldest, "requested_from": beg.isoformat(), "complete": complete}
    incomplete = [k for k, v in coverage.items() if not v["complete"]]
    out.sort(key=lambda r: r["date"], reverse=True)
    note = "覆盖完整：count=0 说明该时间窗内东财研报库查不到，不得引用。"
    if incomplete:
        note = (f"{'、'.join(incomplete)}只扫到 {min(coverage[k]['covered_from'] or '?' for k in incomplete)}，"
                f"更早的没扫到，查不到不等于不存在。缩短 --days、加 --org，或加大 --max-pages 再查。")
    return {"query": {"code": args.code, "kw": args.kw, "org": args.org, "days": args.days},
            "coverage": coverage, "count": len(out), "reports": out[: args.limit],
            "source": "eastmoney reportapi", "fetched_at": stamp(),
            "note": note + " 检索到只证明报告存在；引用具体观点前要打开 page 或 pdf 核对原文。外资研报不在此库。"}


# ================================================================ 新闻 / 日历
def cls_page(last_time: int) -> list[dict]:
    params = {"app": "CailianpressWeb", "category": "", "last_time": last_time, "os": "web",
              "refresh_type": "1", "rn": "20", "sv": "8.4.6"}
    params["sign"] = hashlib.md5(hashlib.sha1(urllib.parse.urlencode(params).encode("utf-8"))
                                 .hexdigest().encode("utf-8")).hexdigest()
    d = http_json("https://www.cls.cn/v1/roll/get_roll_list", params, {"Referer": "https://www.cls.cn/telegraph"})
    return ((d.get("data") or {}).get("roll_data")) or []


def cls_telegraph(hours: float, important: bool, maxlen: int = 300) -> tuple[list[dict], dict]:
    cutoff = time.time() - hours * 3600
    seen, rows, last, pages = set(), [], int(time.time()), 0
    for _ in range(40):
        items = cls_page(last)
        pages += 1
        new = [x for x in items if x.get("id") not in seen]
        if not new:
            break
        for x in new:
            seen.add(x.get("id"))
            rows.append(x)
        last = min(int(x.get("ctime") or last) for x in new)
        if last <= cutoff:
            break
        time.sleep(0.2)
    oldest_all = min((int(x.get("ctime") or 0) for x in rows), default=None)
    rows = [x for x in rows if int(x.get("ctime") or 0) >= cutoff]
    if important:
        rows = [x for x in rows if x.get("level") in ("A", "B")]
    rows.sort(key=lambda x: int(x.get("ctime") or 0), reverse=True)
    cov = {"pages": pages, "requested_hours": hours,
           "covered_from": dt.datetime.fromtimestamp(oldest_all, TZ).strftime("%m-%d %H:%M") if oldest_all else None,
           "complete": bool(oldest_all and oldest_all <= cutoff)}
    out = [{"time": dt.datetime.fromtimestamp(int(x["ctime"]), TZ).strftime("%m-%d %H:%M"),
            "level": x.get("level"), "title": x.get("title") or "", "content": (x.get("content") or "")[:maxlen]}
           for x in rows]
    return out, cov


def cmd_news(args) -> dict:
    if args.kind == "cls":
        try:
            rows, cov = cls_telegraph(args.hours, args.important)
            src = "财联社电报（直连翻页）"
        except Exception as e:  # noqa: BLE001
            ak = need_akshare()
            df = ak.stock_info_global_cls(symbol="重点" if args.important else "全部")
            rows = [{"time": f"{r['发布日期']} {r['发布时间']}", "title": r["标题"], "content": r["内容"]}
                    for _, r in df.iloc[::-1].iterrows()]
            cov = {"complete": False, "note": f"直连失败（{e}），akshare 只返回最新20条"}
            src = "财联社电报 via akshare"
        return {"kind": "cls", "coverage": cov, "count": len(rows), "rows": rows[: args.limit], "source": src,
                "fetched_at": stamp(),
                "note": "coverage.complete=false 时更早的电报没拿到，用带日期的搜索补齐；核对事件时间与发布时间"}
    ak = need_akshare()
    c = norm_code(args.code)[-6:]
    df = ak.stock_news_em(symbol=c)
    rows = [{"time": r["发布时间"], "media": r["文章来源"], "title": r["新闻标题"],
             "summary": str(r["新闻内容"])[:160], "url": r["新闻链接"]} for _, r in df.head(args.limit).iterrows()]
    return {"kind": "stock", "rows": rows, "source": "东方财富个股新闻 via akshare", "fetched_at": stamp(),
            "note": "只含最近约10条；核对事件时间与新闻发布时间"}


def cmd_calendar(args) -> dict:
    ak = need_akshare()
    date = (args.date or now().strftime("%Y%m%d")).replace("-", "")
    df = ak.news_economic_baidu(date=date)
    rows = []
    for _, r in df.iterrows():
        pub = r.get("公布")
        released = pub not in (None, "", "-") and str(pub) != "nan"
        rows.append({"date": str(r.get("日期")), "time": str(r.get("时间")), "region": r.get("地区"),
                     "event": r.get("事件"), "period": r.get("统计周期"), "importance": r.get("重要性"),
                     "actual": pub if released else None, "forecast": r.get("预期"), "previous": r.get("前值"),
                     "status": "已公布" if released else "未公布（只能写市场预期）"})
    return {"date": date, "rows": rows, "source": "百度股市通 via akshare", "fetched_at": stamp(),
            "note": "海外数据以官方日历（BLS、BEA、统计局）为最终依据"}


# ================================================================ 全球市场
GLOBAL_DEFAULT = {"^GSPC": "标普500", "^IXIC": "纳斯达克", "^DJI": "道琼斯", "^SOX": "费城半导体",
                  "^TNX": "10年期美债收益率", "US2Y": "2年期美债收益率", "DX-Y.NYB": "美元指数", "GC=F": "COMEX黄金",
                  "SI=F": "COMEX白银", "BZ=F": "布伦特原油", "CL=F": "WTI原油", "HG=F": "COMEX铜",
                  "BTC-USD": "比特币", "^HSI": "恒生指数", "^KS11": "韩国KOSPI", "^N225": "日经225"}


# 海外源熔断：某个源第一次出现网络层失败（超时、403 等）后，本次运行的其余品种直接跳过它。
# 2026-11-05 雪球 Linux agent：Yahoo 10秒超时、新浪403，旧逻辑每个品种都重试3次，15个品种要等好几分钟。
_DOWN: dict[str, str] = {}


def _guard(name: str, fn: Callable[[], dict]) -> dict:
    if name in _DOWN:
        raise SourceError(f"本次运行已判定不可用（{_DOWN[name]}）")
    try:
        return fn()
    except NetDown as e:
        _DOWN[name] = str(e)[:100]
        raise


def yahoo_chart(sym: str) -> dict:
    d = http_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}",
                  {"range": "10d", "interval": "1d"}, timeout=6, retries=1)
    res = d["chart"]["result"][0]
    meta = res.get("meta") or {}
    off = int(meta.get("gmtoffset") or 0)
    ts, closes = res.get("timestamp") or [], res["indicators"]["quote"][0].get("close") or []
    pts = [(t, c) for t, c in zip(ts, closes) if c is not None]
    if len(pts) < 2:
        raise SourceError("not enough data")
    (t0, c0), (t1, c1) = pts[-2], pts[-1]

    def ex_date(t: float) -> str:
        return dt.datetime.fromtimestamp(t + off, dt.timezone.utc).strftime("%Y-%m-%d")

    period = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
    nowts = time.time()
    live = bool(period) and period.get("start", 0) <= nowts < period.get("end", 0) \
        and ex_date(t1) == ex_date(period.get("start", 0))
    return {"close": round(c1, 4), "prev_close": round(c0, 4), "pct": round((c1 / c0 - 1) * 100, 2),
            "bar_date": ex_date(t1), "date_basis": f"交易所当地日期（{meta.get('exchangeTimezoneName', '')}）",
            "basis": "盘中（未收盘）" if live else "收盘", "source": "yahoo chart"}


# 新浪海外行情：Yahoo 403 时的备用源（2026-11-05 实测字段，见 references/data-sources.md）。
# 注意 int_dji / int_nasdaq / int_sp500 返回的是过期数据，不要用。
SINA_GLOBAL = {"^GSPC": "gb_$inx", "^IXIC": "gb_ixic", "^DJI": "gb_$dji", "DX-Y.NYB": "DINIW",
               "GC=F": "hf_GC", "SI=F": "hf_SI", "BZ=F": "hf_OIL", "CL=F": "hf_CL", "HG=F": "hf_HG",
               "NG=F": "hf_NG", "^HSI": "rt_hkHSI", "^N225": "b_NKY", "^FTSE": "znb_UKX", "^GDAXI": "znb_DAX"}
_MON = {m: i for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def sina_code(sym: str) -> str:
    if sym in SINA_GLOBAL:
        return SINA_GLOBAL[sym]
    if re.fullmatch(r"[A-Za-z][A-Za-z.]{0,5}", sym):  # 美股个股，如 NVDA → gb_nvda
        return "gb_" + sym.lower().replace(".", "")
    raise SourceError(f"新浪没有对应代码：{sym}")


def parse_sina_global(code: str, raw: str) -> dict:
    """解析 hq.sinajs.cn 一行引号里的内容。返回 close/prev_close/pct/bar_date/basis。"""
    f = raw.split(",")
    if len(f) < 4:
        raise SourceError(f"新浪无数据：{code}")

    def n(i: int) -> float:
        v = num(f[i])
        if v is None or v == 0:
            raise SourceError(f"新浪字段为空：{code}[{i}]")
        return v

    basis_unk = "未判断是否收盘（新浪备用源）"
    if code.startswith("gb_"):
        # 0名称 1最新价 2涨跌幅 3北京时间 4涨跌额 5开 6高 7低 ... 25美东时间“Nov 4 04:51PM EST” 26昨收
        close, prev = n(1), n(26)
        m = re.match(r"(\w{3})\s+(\d{1,2})\s+(\d{1,2}):(\d{2})(AM|PM)", f[25].strip())
        if not m:
            raise SourceError(f"新浪美东时间解析失败：{f[25]}")
        bj = f[3].strip()[:10]
        mon, day = _MON[m.group(1)], int(m.group(2))
        year = int(bj[:4]) - (1 if mon == 12 and bj[5:7] == "01" else 0)
        hh = int(m.group(3)) % 12 + (12 if m.group(5) == "PM" else 0)
        return {"close": close, "prev_close": prev, "bar_date": f"{year}-{mon:02d}-{day:02d}",
                "date_basis": "美东日期", "basis": "收盘" if hh >= 16 else "盘中（未收盘）",
                "sina_time": f[25].strip()}
    if code.startswith("hf_"):
        # 0最新价 2买 3卖 4高 5低 6北京时间 7昨结算 8开盘 12日期 13名称
        return {"close": n(0), "prev_close": n(7), "bar_date": f[12], "date_basis": "北京时间日期",
                "basis": f"期货最新价（北京时间{f[12]} {f[6]}，较前结算价）"}
    if code == "DINIW" or code.startswith("fx_"):
        # 0时间 1最新价 3昨收 9名称 10日期（DINIW）
        return {"close": n(1), "prev_close": n(3), "bar_date": f[10] if code == "DINIW" else f[-1],
                "date_basis": "北京时间日期", "basis": f"最新价（北京时间{f[0]}）"}
    if code.startswith("rt_hk"):
        # 2开 3昨收 4高 5低 6最新价 17日期 18时间
        t = f[18]
        return {"close": n(6), "prev_close": n(3), "bar_date": f[17].replace("/", "-"), "date_basis": "香港日期",
                "basis": "收盘" if t >= "16:00" else f"盘中（截至{t}）"}
    if code.startswith(("b_", "znb_")):
        # 0名称 1最新价 2涨跌额 3涨跌幅 6日期 7时间 8开盘 9昨收
        return {"close": n(1), "prev_close": n(9), "bar_date": f[6], "date_basis": "新浪日期", "basis": basis_unk}
    raise SourceError(f"未知的新浪代码格式：{code}")


def sina_global(sym: str) -> dict:
    code = sina_code(sym)
    txt = http_get("https://hq.sinajs.cn/list=" + code, headers={"Referer": "https://finance.sina.com.cn"},
                   encoding="gbk", timeout=6, retries=1)
    m = re.search(r'="([^"]*)"', txt)
    if not m or not m.group(1).strip():
        raise SourceError(f"新浪无数据：{code}")
    r = parse_sina_global(code, m.group(1))
    r["pct"] = round((r["close"] / r["prev_close"] - 1) * 100, 2)
    r.update({"close": round(r["close"], 4), "prev_close": round(r["prev_close"], 4),
              "source": f"sina {code}"})
    return r


# 东财全球指数和国际期货（push2 ulist）：Yahoo、新浪都不通但东财能通的环境用。
# secid 是东财行情中心的代码；返回名称要包含 keyword 才采用，防止代码含义变了却照写。
# 2026-11-05 雪球 agent 实测全部有数据；100.SOX（费城半导体）无返回，比特币没有。
EM_GLOBAL = {"^DJI": ("100.DJIA", "道琼斯"), "^GSPC": ("100.SPX", "标普"), "^IXIC": ("100.NDX", "纳斯达克"),
             "^TNX": ("171.US10Y", "10年期"), "US2Y": ("171.US2Y", "2年期"), "^HSI": ("100.HSI", "恒生"), "^N225": ("100.N225", "日经"),
             "^KS11": ("100.KS11", "韩国"), "DX-Y.NYB": ("100.UDI", "美元指数"), "^FTSE": ("100.FTSE", "富时"),
             "^GDAXI": ("100.GDAXI", "DAX"), "GC=F": ("101.GC00Y", "黄金"), "SI=F": ("101.SI00Y", "白银"),
             "HG=F": ("101.HG00Y", "铜"), "CL=F": ("102.CL00Y", "原油"), "NG=F": ("102.NG00Y", "天然气"),
             "BZ=F": ("112.B00Y", "布伦特")}
_EM_GLOBAL_CACHE: dict[str, dict] = {}


def _us_eastern(t: dt.datetime) -> dt.datetime:
    """UTC → 美东时间（夏令时：3月第二个周日至11月第一个周日），不依赖 tzdata。"""
    y = t.year
    start = dt.datetime(y, 3, 8 + (6 - dt.date(y, 3, 8).weekday()) % 7, 7, tzinfo=dt.timezone.utc)
    end = dt.datetime(y, 11, 1 + (6 - dt.date(y, 11, 1).weekday()) % 7, 6, tzinfo=dt.timezone.utc)
    return t + dt.timedelta(hours=-4 if start <= t < end else -5)


_EM_GLOBAL_LOCK = threading.Lock()


def _em_global_fill() -> None:
    """一次请求取全部品种，结果缓存到本次运行结束。调用方持有 _EM_GLOBAL_LOCK。"""
    if _EM_GLOBAL_CACHE:
        return
    secids = ",".join(v[0] for v in EM_GLOBAL.values())
    try:
        d = em_rotate(EM_HOSTS, "/api/qt/ulist.np/get",
                      {"fltt": 2, "secids": secids, "fields": "f2,f3,f4,f12,f13,f14,f18,f124", "ut": EM_UT})
    except SourceError as e:
        raise NetDown(str(e)) from e
    diff = (d.get("data") or {}).get("diff") or []
    for x in (diff.values() if isinstance(diff, dict) else diff):
        _EM_GLOBAL_CACHE[f"{x.get('f13')}.{x.get('f12')}"] = x
    if not _EM_GLOBAL_CACHE:
        raise SourceError("东财全球行情无数据")


def em_global(sym: str) -> dict:
    if sym not in EM_GLOBAL:
        raise SourceError(f"东财没有对应代码：{sym}")
    with _EM_GLOBAL_LOCK:
        _em_global_fill()
    secid, kw = EM_GLOBAL[sym]
    x = _EM_GLOBAL_CACHE.get(secid)
    if not x:
        raise SourceError(f"东财无数据：{secid}")
    name = str(x.get("f14") or "")
    if kw not in name:
        raise SourceError(f"东财 {secid} 返回“{name}”，与预期“{kw}”不符，未采用")
    close, prev = num(x.get("f2")), num(x.get("f18"))
    if not close or not prev:
        raise SourceError(f"东财字段为空：{secid}")
    ts = int(num(x.get("f124")) or 0)
    upd = dt.datetime.fromtimestamp(ts, dt.timezone.utc) if ts else None
    upd_txt = f"更新于北京时间{upd.astimezone(TZ):%m-%d %H:%M}" if upd else "更新时间未知"
    basis = f"未判断是否收盘（东财备用源，{upd_txt}）"
    bar_date, date_basis = (upd.astimezone(TZ).strftime("%Y-%m-%d") if upd else ""), "北京时间日期"
    if upd and sym in ("^DJI", "^GSPC", "^IXIC"):
        et = _us_eastern(upd)
        bar_date, date_basis = et.strftime("%Y-%m-%d"), "美东日期"
        # 东财收盘后的更新时间是美东 15:59 或 16:00
        basis = "收盘" if et.hour * 60 + et.minute >= 15 * 60 + 59 else f"盘中（美东{et:%H:%M}）"
    if secid.startswith(("101.", "102.", "112.")):
        basis = f"期货最新价（东财备用源，{upd_txt}，较昨收）"
    return {"close": round(close, 4), "prev_close": round(prev, 4), "pct": round((close / prev - 1) * 100, 2),
            "bar_date": bar_date, "date_basis": date_basis, "basis": basis, "source": f"eastmoney {secid} {name}"}


def yf_quote(sym: str) -> dict:
    if "yahoo" in _DOWN:
        raise SourceError("yfinance 和 Yahoo 是同一后端，Yahoo 不通时跳过")
    import yfinance as yf  # noqa: WPS433
    h = yf.Ticker(sym).history(period="10d", interval="1d", auto_adjust=False, timeout=8)
    h = h.dropna(subset=["Close"])
    if len(h) < 2:
        raise SourceError("not enough data")
    c0, c1 = float(h["Close"].iloc[-2]), float(h["Close"].iloc[-1])
    return {"close": round(c1, 4), "prev_close": round(c0, 4), "pct": round((c1 / c0 - 1) * 100, 2),
            "bar_date": str(h.index[-1].date()), "date_basis": "交易所当地日期",
            "basis": "未判断是否收盘（yfinance 备用源）", "source": "yfinance"}


# 美股关键个股：扫描时一起拉，让“隔夜美股哪条链在涨、哪条链在跌”在第一步就摆出来。
# 走腾讯行情（雪球 agent 上可用；Yahoo、新浪不通也不影响）。分组只是方便看，不代表A股映射关系。
US_WATCH = {"存储": ["MU", "SNDK", "WDC", "STX"],
            "算力芯片": ["NVDA", "AVGO", "AMD", "TSM"],
            "光通信与数据中心": ["COHR", "LITE", "ANET", "VRT"],
            "光伏与清洁能源": ["FSLR", "ENPH", "RUN", "NEE"],
            "中概互联": ["BABA", "PDD", "JD", "BIDU"]}


def us_watch() -> dict:
    codes = ["us" + s for g in US_WATCH.values() for s in g]
    q = tencent_quotes(codes)
    groups: dict[str, Any] = {}
    missing: list[str] = []
    for g, syms in US_WATCH.items():
        rows = []
        for sym in syms:
            r = q.get("us" + sym)
            if not r or r.get("pct") is None:
                missing.append(sym)
                continue
            rows.append({"symbol": sym, "name": r.get("name"), "pct": r["pct"], "write_as": phrase(r["pct"]),
                         "price": r.get("price"), "data_date": r.get("data_date")})
        if rows:
            groups[g] = {"avg_pct": round(sum(x["pct"] for x in rows) / len(rows), 2), "rows": rows}
    out: dict[str, Any] = {"groups": groups, "source": "tencent", "fetched_at": stamp(),
                           "note": "data_date 是美东日期；北京时间白天看到的是前一晚收盘。正文引用前用 quote 再拉一次并记进底稿"}
    if missing:
        out["missing"] = missing
    return out


def soxx_proxy() -> dict:
    """费城半导体指数（^SOX）在 Yahoo 以外没有可用源时，用跟踪它的 SOXX ETF 代替。"""
    r = tencent_quotes(["usSOXX"]).get("usSOXX")
    if not r or r.get("pct") is None or not r.get("price"):
        raise SourceError("腾讯 SOXX 无数据")
    return {"close": r["price"], "prev_close": r.get("prev_close"), "pct": r["pct"],
            "bar_date": r.get("data_date", ""), "date_basis": "美东日期",
            "basis": "以SOXX ETF代替指数，正文写“费城半导体ETF（SOXX）”，不能写成指数", "source": "tencent usSOXX"}


def cmd_global(args) -> dict:
    syms = args.symbols or list(GLOBAL_DEFAULT)
    t0 = time.time()

    def one(s: str) -> dict:
        try:
            def yahoo(s=s): return _guard("yahoo", lambda: yahoo_chart(s))
            def sina(s=s): return _guard("sina", lambda: sina_global(s))
            def eastmoney(s=s): return _guard("eastmoney", lambda: em_global(s))
            def yfinance(s=s): return _guard("yfinance", lambda: yf_quote(s))
            fns = [yahoo, eastmoney, sina, yfinance]
            if s == "^SOX":
                fns.append(lambda: _guard("tencent", soxx_proxy))
            r, errs = first_ok(*fns)
            if errs:
                r["fallback_errors"] = [e[:160] for e in errs]
            r.update({"symbol": s, "name": GLOBAL_DEFAULT.get(s, s), "write_as": phrase(r["pct"])})
            if r.get("source") == "tencent usSOXX":
                r["name"] = "费城半导体ETF（SOXX，代替指数）"
            if s in ("^TNX", "US2Y"):
                r["write_as"] = f"收益率{'升' if r['pct'] >= 0 else '降'}至约{r['close']:.2f}%"
            if not r["basis"].startswith("收盘"):
                r["write_as"] += f"（{r['basis']}）"
            return r
        except Exception as e:  # noqa: BLE001
            return {"symbol": s, "name": GLOBAL_DEFAULT.get(s, s), "error": str(e)[:400]}

    res = parallel({s: (lambda s=s: one(s)) for s in syms}, workers=8)
    rows = [res[s][1] for s in syms]
    out: dict[str, Any] = {"rows": rows, "fetched_at": stamp(), "elapsed_s": round(time.time() - t0, 1)}
    if _DOWN:
        out["sources_down"] = dict(_DOWN)
    missing = [r["name"] for r in rows if "error" in r]
    if missing and len(missing) == len(rows):
        out["error"] = ("海外行情全部取不到（" + "；".join(f"{k}: {v}" for k, v in _DOWN.items()) +
                        "）：模式五和美股联动只能用搜索取数，正文数字逐条在底稿注明来源")
    elif missing:
        out["missing"] = f"{'、'.join(missing)} 没有取到：需要时用搜索补，并在底稿注明来源"
    return {**out,
            "note": "写“隔夜”前确认 bar_date 是前一个交易日；期货周末/假日报价不是正式收盘价；"
                    "新浪备用源的期货涨跌幅是相对前结算价，铜价单位是美分/磅；"
                    "历史极值需另找第二来源；2年期美债收益率请用搜索核对"}



# ================================================================ 收评
AMOUNT_RE = re.compile(r"(沪深京三市|沪深两市)(?:全天|合计)?成交(?:额)?(?:为|达|约)?([\d.]+)(万亿|亿)元?"
                       r"[^。]{0,12}?较上(?:一|个)?(?:个)?交易日(放量|缩量|增加|减少)(?:约)?([\d.]+)(亿|万亿)元?")


def approx_count(n: int | None) -> str:
    """家数的自然写法：876 → 近900只，1220 → 超1200只，49 → 近50只。"""
    if n is None:
        return "-"
    base = 100 if n >= 200 else 10
    r = int(round(n / base) * base)
    if r == 0 or n == r:
        return f"{n}只"
    return f"近{r}只" if n < r else f"超{r}只"


def cls_close_amount() -> dict | None:
    """从财联社当日收评电报里取成交额原话（三市口径、较上一交易日变化）。"""
    t = now()
    hours = max(1.0, (t.hour - 14) + t.minute / 60) if t.hour >= 15 else 1.0
    rows, _ = cls_telegraph(hours, False, maxlen=2000)
    for x in rows:
        txt = (x.get("title") or "") + (x.get("content") or "")
        m = AMOUNT_RE.search(txt)
        if m:
            return {"scope": m.group(1), "amount": m.group(2) + m.group(3), "change_dir": m.group(4),
                    "change": m.group(5) + m.group(6), "time": x["time"],
                    "source": f"财联社电报 {x['time']}《{(x.get('title') or txt[:20]).strip('【】')}》",
                    "quote": m.group(0)}
    return None


def amount_change_from_kline() -> dict:
    """沪深两市成交额及较上一交易日变化（上证指数 + 深证成指日K线成交额）。"""
    today = trade_status()["today"]
    tot: dict[str, float] = {}
    for code in ("sh000001", "sz399001"):
        for r in em_kline(code, 5):
            if r.get("amount"):
                tot[r["date"]] = tot.get(r["date"], 0) + r["amount"]
    days = sorted(tot)
    if len(days) < 2 or days[-1] != today:
        raise SourceError("K线里没有今天或上一交易日的成交额")
    a, b = tot[days[-1]], tot[days[-2]]
    d = a - b
    return {"scope": "沪深两市", "amount": yi(a), "change_dir": "放量" if d >= 0 else "缩量",
            "change": f"{abs(d) / 1e8:.0f}亿", "prev_date": days[-2], "source": "东财日K线（上证指数+深证成指）"}


def board_counts(kind: str = "industry", level: str = "2") -> dict:
    """行业板块整体涨跌个数。数据源顺序与 boards() 一致，结果带上来源和层级，
    以便和板块排行核对口径（2026-11-03 曾出现个数来自新浪、排行来自东财，写成“多数收涨”的错误）。"""
    used = {}

    def from_em():
        used["s"] = "eastmoney"
        return _boards_em(kind, "f3", 100, level)[0]

    def from_ak():
        used["s"] = "eastmoney via akshare"
        return _boards_ak(kind, "f3", 100, level)[0]

    def from_sina():
        used["s"] = "sina"
        return _boards_sina(kind, "f3", 100)[0]

    rows, errs = first_ok(from_em, from_ak, from_sina)
    seen, up, down, flat = set(), 0, 0, 0
    for x in rows:
        base = re.sub(r"[ⅠⅡⅢⅣ]+$", "", str(x.get("name") or "")).strip()
        if base in seen or x.get("pct") is None:
            continue
        seen.add(base)
        up += x["pct"] > 0
        down += x["pct"] < 0
        flat += x["pct"] == 0
    lv, note = _level_tag(used["s"], level)
    return {"up": up, "down": down, "flat": flat, "source": used["s"], "level": lv, "level_note": note,
            "fallback_errors": errs}


def _same_board(full: str, pool_name: str) -> bool:
    """板块排行名和涨停池 hybk 是否同一个二级行业（hybk 超过4个字会被截断）。"""
    p = re.sub(r"[ⅠⅡⅢⅣ]+$", "", str(pool_name or "")).strip()
    return bool(p) and (full == p or (len(p) >= 4 and full.startswith(p)))


def cmd_recap(args) -> dict:
    st = trade_status()
    ss = session_state()
    warn = []
    if st["is_trading_day"] is False:
        warn.append("今天休市，不写A股收评")
    elif not ss.startswith("已收盘"):
        warn.append(f"当前{ss}，收评要等15:00收盘后再取数")
    out: dict[str, Any] = {"date": st["today"], "session": ss, "warnings": warn}
    t0 = time.time()
    # 六项互不依赖的取数并发执行（原来串行，而且板块数据取了两遍）
    pre = parallel({
        "quote": lambda: cmd_quote(argparse.Namespace(codes=["sh000001", "sz399001", "sz399006"])),
        "cls": lambda: cls_close_amount(),
        "counts": lambda: board_counts("industry"),
        "boards": lambda: boards("industry", "f3", 8),
        "breadth": lambda: breadth_ulist(),
        "zt": lambda: em_pool("zt", ltd_compact()),
    })

    q = {r["code"]: r for r in take(pre, "quote")}
    names = {"sh000001": "沪指", "sz399001": "深证成指", "sz399006": "创业板指"}
    idx = []
    for c, n in names.items():
        r = q.get(c) or {}
        idx.append({"name": n, "pct": r.get("pct"), "close": r.get("price"), "basis": r.get("basis"),
                    "cross_check": r.get("cross_check")})
    out["indexes"] = idx
    missing_idx = [i["name"] for i in idx if i["pct"] is None or i["close"] is None]
    blocked = []
    if missing_idx:
        blocked.append(f"指数行情取不到（{'、'.join(missing_idx)}）")

    amt = None
    try:
        amt = take(pre, "cls")
    except Exception as e:  # noqa: BLE001
        warn.append(f"财联社收评电报取不到：{e}")
    if not amt:
        try:
            amt = amount_change_from_kline()
        except Exception as e:  # noqa: BLE001
            warn.append(f"成交额变化取不到：{e}")
    out["amount"] = amt

    try:
        out["industry_counts"] = take(pre, "counts")
    except Exception as e:  # noqa: BLE001
        out["industry_counts"] = {"error": str(e)}
    try:
        b = take(pre, "boards")
        strip = lambda n: re.sub(r"[ⅠⅡⅢⅣ]+$", "", str(n)).strip()  # noqa: E731
        out["industry_top"] = [strip(x["name"]) for x in b["top"]]
        out["industry_bottom"] = [strip(x["name"]) for x in b["bottom"]]
        out["industry_source"] = b["source"]
        out["industry_level"] = b.get("level_note")
        ic0 = out["industry_counts"]
        if isinstance(ic0, dict) and "up" in ic0 and (ic0.get("source"), ic0.get("level")) != (b["source"], b.get("level")):
            ic0["mismatch"] = True
            warn.append(f"板块涨跌个数（{ic0.get('source')}，{ic0.get('level_note')}）与板块排行"
                        f"（{b['source']}，{b.get('level_note')}）口径不同：草稿不写“多数收涨/收跌”，需要时改用财联社收评原话")
    except Exception as e:  # noqa: BLE001
        out["industry_top"], out["industry_bottom"] = [], []
        warn.append(f"板块排行取不到：{e}")

    breadth = None
    for name, fn in (("breadth_ulist", lambda: take(pre, "breadth")), ("breadth_legu", breadth_legu)):
        try:
            breadth = fn()
            break
        except Exception as e:  # noqa: BLE001
            warn.append(f"{name}: {str(e)[:80]}")
    out["breadth"] = breadth

    zt: list[dict] | None = None
    try:
        zt = take(pre, "zt")
    except Exception as e:  # noqa: BLE001
        warn.append(f"涨停池取不到：{e}（草稿不写涨停数，不能写成0只）")
    by_ind: dict[str, list[str]] = {}
    for x in zt or []:
        by_ind.setdefault(x.get("hybk") or "未分类", []).append(x.get("n"))
    out["limit_up_count"] = len(zt) if zt is not None else None
    out["limit_up_by_industry"] = dict(sorted(by_ind.items(), key=lambda kv: -len(kv[1]))[:10])

    # ---- 草稿
    pcts = [i["pct"] for i in idx if i["pct"] is not None]
    trend = "集体上涨" if pcts and all(p > 0 for p in pcts) else "集体回调" if pcts and all(p < 0 for p in pcts) else "涨跌不一"
    parts = []
    for i in idx:
        if i["pct"] is None:
            continue
        parts.append(f"{i['name']}{'涨' if i['pct'] >= 0 else '跌'}{abs(i['pct']):.2f}%，收报{i['close']:.2f}点")
    p1 = f"A股三大指数今日{trend}，截至收盘，" + "；".join(parts) + "。"
    if amt:
        unit_amt = amt["amount"] if amt["amount"].endswith("亿") else amt["amount"]
        p1 += f"{amt['scope']}成交额{unit_amt}元，较上一交易日{amt['change_dir']}{amt['change']}元。"
    p2 = ""
    ic = out["industry_counts"]
    if out["industry_top"] or out["industry_bottom"]:
        maj = ""
        if isinstance(ic, dict) and "up" in ic and not ic.get("mismatch"):
            maj = "多数收涨" if ic["up"] > ic["down"] * 1.2 else "多数收跌" if ic["down"] > ic["up"] * 1.2 else "涨跌互现"
        p2 = (f"行业板块{maj}，" if maj else "行业板块方面，") + \
             f"{'、'.join(out['industry_top'][:4])}板块涨幅居前，{'、'.join(out['industry_bottom'][:5])}板块跌幅居前。"
    p3 = "个股方面，"
    if breadth:
        if breadth["down"] > breadth["up"]:
            p3 += f"下跌股票{approx_count(breadth['down'])}，"
        else:
            p3 += f"上涨股票{approx_count(breadth['up'])}，"
    if zt is not None:
        p3 += f"{approx_count(len(zt))}股票涨停。"
    p3 = p3.rstrip("，") if p3 != "个股方面，" else ""
    if p3 and not p3.endswith("。"):
        p3 += "。"
    lead = None
    for name in out["industry_top"][:3]:
        base = re.sub(r"[ⅠⅡⅢⅣ]+$", "", name)
        # 涨停池的 hybk 是东财二级行业名（超过4个字会被截断，如“汽车零部”）。
        # 只认同名或截断前缀，不再用包含关系（曾把“半导体”大类的涨停股安到“半导体设备”头上）
        hit = next((v for k, v in by_ind.items() if _same_board(base, k)), None)
        if hit and len(hit) >= 2:
            lead = (base, hit)
            break
    if lead:
        p3 += f"{lead[0]}板块{'逆市' if trend == '集体回调' else ''}走强，{'、'.join(lead[1][:4])}涨停。"
    if blocked:
        out["draft"] = ""
        out["draft_blocked"] = "；".join(blocked) + "：不出收评草稿，先排查数据源（selftest）再重跑 recap"
    else:
        out["draft"] = "\n\n".join(x for x in (p1, p2, p3) if x)

    # ---- 底稿行
    rows = []
    src_q = "新浪+腾讯行情（recap 落盘文件）"
    for i in idx:
        if i["pct"] is not None:
            rows.append(("收评", f"{i['name']}{'涨' if i['pct'] >= 0 else '跌'}{abs(i['pct']):.2f}%，收报{i['close']:.2f}点", f"{i['pct']}% / {i['close']}",
                         src_q, "-", i.get("basis") or ""))
    if amt:
        rows.append(("收评", f"{amt['scope']}成交额{amt['amount']}，{amt['change_dir']}{amt['change']}",
                     amt.get("quote", ""), amt["source"], "-", amt.get("time", "")))
    if breadth:
        side = ("下跌", breadth["down"]) if breadth["down"] > breadth["up"] else ("上涨", breadth["up"])
        rows.append(("收评", f"{side[0]}股票{approx_count(side[1])}（实际上涨{breadth['up']}只，下跌{breadth['down']}只）", f"{breadth['up']}/{breadth['down']}/{breadth['flat']}",
                     breadth["source"], breadth.get("scope", ""), ""))
    if zt is not None:
        rows.append(("收评", f"{approx_count(len(zt))}股票涨停（实际{len(zt)}只）", str(len(zt)), "东财涨停池", "-", ltd_compact()))
    if lead:
        rows.append(("收评", f"{lead[0]}板块{'、'.join(lead[1][:4])}涨停", "、".join(lead[1]), "东财涨停池（所属行业）", "-", ltd_compact()))
    if isinstance(ic, dict) and "up" in ic and not ic.get("mismatch"):
        rows.append(("收评", f"行业板块上涨{ic['up']}个 下跌{ic['down']}个", f"{ic['up']}/{ic['down']}",
                     f"{ic.get('source')}（{ic.get('level_note')}）", "-", ""))
    out["ledger_rows"] = rows
    if blocked:
        warn.append("收评数据不全，没有写入事实底稿")
    elif not getattr(args, "no_ledger", False):
        d = run_dir(args)
        f = d / "ledger.md"
        if not f.exists():
            f.write_text(LEDGER_TEMPLATE.replace("{date}", now().strftime("%Y-%m-%d")), encoding="utf-8")
        lines = "".join(f"| 收评-{k + 1} | {r[0]} | {r[1]} | {r[2]} | {r[3]} | {r[4]} | 见 recap 落盘 | {st['today']} 收盘 | {r[5]} | {stamp()[5:16]} | 脚本取数 |\n"
                        for k, r in enumerate(rows))
        txt = f.read_text(encoding="utf-8")
        marker = "## 题目判定"
        txt = txt.replace(marker, lines + "\n" + marker, 1) if marker in txt else txt + lines
        f.write_text(txt, encoding="utf-8")
        out["ledger"] = str(f)
    out["fetched_at"] = stamp()
    out["elapsed_s"] = round(time.time() - t0, 1)
    out["note"] = ("draft 只是按数据拼出的格式草稿：措辞可以改，数字、点位、板块名不能改；"
                   "家数可写成“近/超/不足N只”但方向不能错；成交额优先用财联社原话的口径")
    return out


# ================================================================ 时间
def cmd_now(args) -> dict:
    t = now()
    st = trade_status()
    td = st["is_trading_day"]
    return {"date": t.strftime("%Y-%m-%d"), "weekday": f"星期{WEEK[t.weekday()]}", "time": t.strftime("%H:%M"),
            "timezone": "Asia/Shanghai", "is_trading_day": td, "basis": st.get("basis"),
            "last_trade_date": st["last_trade_date"], "a_share_session": session_state(t),
            "route_global": td is False,
            "say_first": f"今天是{t.year}年{t.month}月{t.day}日，星期{WEEK[t.weekday()]}。",
            "note": "route_global=true（周末或节假日休市）时默认走模式五；is_trading_day=null 时请确认交易所休市安排"}


def cmd_weekday(args) -> dict:
    d = dt.date.fromisoformat(args.date)
    out: dict[str, Any] = {"date": args.date, "weekday": f"星期{WEEK[d.weekday()]}"}
    cal = _cached_calendar()
    if cal:
        out["a_share_trading_day"] = args.date in cal
    return out


# ================================================================ 选题扫描
def cmd_scan(args) -> dict:
    out: dict[str, Any] = {"now": cmd_now(args)}
    tasks: dict[str, Callable[[], Any]] = {
        "index": lambda: cmd_index(argparse.Namespace(verify=False)),
        "industry": lambda: boards("industry", "f3", 8),
        "concept": lambda: boards("concept", "f3", 8),
        "industry_flow": lambda: boards("industry", "f62", 5),
        "limit": lambda: cmd_limit(argparse.Namespace(date=args.date)),
        "global": lambda: cmd_global(argparse.Namespace(symbols=None)),
        "us_watch": us_watch,
    }
    if out["now"]["route_global"]:
        tasks = {"global": lambda: cmd_global(argparse.Namespace(symbols=None))}
    t0 = time.time()
    for k, (ok, v) in parallel(tasks).items():
        out[k] = v if ok else {"error": str(v)}
    out["elapsed_s"] = round(time.time() - t0, 1)
    lim = out.get("limit") or {}
    if isinstance(lim, dict) and "limit_up_list" in lim:
        lim["limit_up_list"] = lim["limit_up_list"][:30]
    flags = []
    idx = out.get("index") or {}
    for r in idx.get("indexes", []):
        if r.get("code") in ("sz399006", "sh000688") and (r.get("pct") or 0) <= -2:
            flags.append(f"{r['name']}{r['write_as']}：必须把领跌方向列入热点列表")
    br = idx.get("breadth") or {}
    sh = next((r for r in idx.get("indexes", []) if r.get("code") == "sh000001"), {})
    if br.get("up") and br.get("down") and sh.get("pct") is not None:
        if (sh["pct"] > 0 and br["down"] > br["up"] * 1.5) or (sh["pct"] < 0 and br["up"] > br["down"] * 1.5):
            flags.append("指数与涨跌家数背离：必须列入热点列表")
    if out["now"]["route_global"]:
        flags.append("今天A股休市：热点列表按模式五（海外市场）准备")
    out["must_cover"] = flags
    out["gaps"] = scan_gaps(out)
    out["next"] = ("先做盘面解读（SKILL.md 第一步）：今天最反常的几件事、它们的共同原因、哪些数据对不上；"
                   "gaps 里的缺口连同对文章的影响，写在热点列表最前面")
    return out


def scan_gaps(out: dict) -> list[str]:
    """把扫描阶段没拿到的数据汇总成一张清单，agent 照抄进热点列表的“缺口”部分。"""
    gaps: list[str] = []
    names = {"index": "大盘指数与涨跌家数", "industry": "行业板块排行", "concept": "概念板块排行",
             "industry_flow": "行业资金流", "limit": "涨停池", "global": "海外与亚太行情", "us_watch": "美股关键个股"}
    for k, label in names.items():
        v = out.get(k)
        if isinstance(v, dict) and v.get("error"):
            gaps.append(f"{label}：没取到（{str(v['error'])[:80]}）")
    g = out.get("global") or {}
    for r in g.get("rows", []):
        if "error" in r:
            gaps.append(f"{r.get('name')}：所有源都失败")
        elif "代替" in str(r.get("basis", "")):
            gaps.append(f"{r.get('name')}：指数本身没取到，只有ETF代替")
    uw = out.get("us_watch") or {}
    if uw.get("missing"):
        gaps.append("美股个股没取到：" + "、".join(uw["missing"]))
    br = (out.get("index") or {}).get("breadth") or {}
    if isinstance(out.get("index"), dict) and not out["index"].get("error") and not br.get("up"):
        gaps.append("涨跌家数：没取到")
    return gaps


# ================================================================ 事实底稿
LEDGER_TEMPLATE = """# 事实底稿 {date}

> 第三步深挖时逐条填写；第四步只能使用本表里的事实。正文里每个数字、日期、机构名都要能在这里找到。
> 信源写到能回查的粒度：机构或媒体名称 + 标题 + 链接或 mkt.py 落盘文件。交付时的核查清单按本表整理。
> 数值列写原始值（如 -4.71%），写法列写正文用的说法（如 跌近5%）。

| 编号 | 题目 | 正文里的写法 | 数值/原文 | 信源（机构/媒体） | 标题 | 链接或落盘文件 | 事件时间 | 发布时间 | 抓取时间 | 核验 |
|---|---|---|---|---|---|---|---|---|---|---|
| 示例1 | 光模块 | 中际旭创跌近5% | -4.71% | 新浪+腾讯行情 | - | 150512-quote.json | 10-30 收盘 | - | 10-30 15:05 | 双源一致 |
| 示例2 | 光模块 | {券商}10月28日研报指出…… | 原文摘句 | {券商} | 《研报标题》 | 东财研报页链接 | - | 10-28 | 10-30 15:20 | 已打开原文核对 |

## 题目判定

| 题目 | 判定（可写/偏弱/撑不起） | 原因 |
|---|---|---|
"""


def runs_base() -> Path:
    """取数记录和稿件的落盘根目录。顺序：环境变量 FHA_RUNS → 当前目录下的 fha-runs；
    Windows 上当前目录在 skill 文件夹里面时改用 ~/Documents/fha-workspace/runs，保持 skill 本体干净。"""
    env = os.environ.get("FHA_RUNS")
    if env:
        return Path(env).expanduser()
    cwd = Path.cwd().resolve()
    # 只在 Windows 本机生效；Linux 上的 agent（如雪球）保持原来的 ./fha-runs，避免写到沙箱外
    if os.name == "nt" and (cwd == SKILL_ROOT.resolve() or SKILL_ROOT.resolve() in cwd.parents):
        return Path.home() / "Documents" / "fha-workspace" / "runs"
    return cwd / "fha-runs"


def run_dir(args) -> Path:
    base = Path(getattr(args, "out", None) or runs_base())
    d = base / now().strftime("%Y-%m-%d")
    d.mkdir(parents=True, exist_ok=True)
    return d


def cmd_ledger(args) -> dict:
    d = run_dir(args)
    f = d / "ledger.md"
    if f.exists() and not args.force:
        return {"ledger": str(f), "status": "已存在，未覆盖（--force 覆盖）"}
    f.write_text(LEDGER_TEMPLATE.replace("{date}", now().strftime("%Y-%m-%d")), encoding="utf-8")
    return {"ledger": str(f), "status": "已创建"}


# ================================================================ lint
HARD_BANS = ["故事还在发酵", "答案就在下周", "答案在下周", "暗线不会", "信号已经非常明显", "信号已经很明显",
             "跃然纸上", "正在酝酿新的机会", "拭目以待", "未来可期", "值得持续关注"]
TAIL_BANS = ["因此", "这也意味着", "从今天盘面来看", "未来仍需关注", "真正决定后续的"]
RESIDUE = ["好的，", "以下是", "希望对您有帮助", "希望对你有帮助", "（已修改）", "(已修改)", "本文由", "这一段补充",
           "作为AI", "改写后", "润色后"]
SOFT = ["值得注意的是", "不难看出", "毫无疑问", "至关重要", "赋能", "标志着", "彰显", "里程碑", "注入强心剂",
        "遥遥领先", "王者归来"]
GENERIC_SUBJ = re.compile(r"(?:(?<=^)|(?<=[，。；：！？、“”\s]))(?:有|部分|多家|一些|不少|某些|多位|有关)?"
                          r"(?:机构|券商|投行|分析师|分析人士|业内人士|市场人士|业内|专家|人士|市场|观点|研究机构)"
                          r"(?:普遍|均|也|则)?(?:认为|指出|表示|预计|称|提醒|分析|判断)")
PLACEHOLDER = re.compile(r"某(?:券商|机构|投行|媒体|公司|研究所|分析师)|\{(?:券商|媒体|投行|公司|机构)[^}]*\}")
MEDIA_CUE = re.compile(r"据.{1,20}?(?:报道|消息|电|披露|援引)|援引|.{1,12}?报道(?:称|指出)?")
QUALIFIERS = ["拟", "计划", "预计", "预告", "快报", "可能", "或将", "据称", "市场预期", "截至", "此前", "近期",
              "尚未", "草案", "初步"]
NUM_RE = re.compile(r"(涨超|涨近|跌超|跌近|涨约|跌约|超过|不足|约|逾|近|超|拟)?(\d+(?:\.\d+)?)"
                    r"(%|个百分点|万亿元|亿元|万元|亿美元|美元|港元|万亿|亿|万|元|点|倍|只|家|艘|吨|连板|CM|年|月|日)?")
ATTR_RE = re.compile(r"([一-龥A-Za-z&]{2,10}?)(?:\d{1,2}月\d{1,2}日)?(?:发布的)?(?:研报|报告|公告)?中?(?:此前|近期|日前)?(?:认为|指出|表示|预计|提醒|援引|称)")


def _norm_num(n: str) -> str:
    return n.rstrip("0").rstrip(".") if "." in n else n


def _numbers(text: str) -> Counter:
    c: Counter = Counter()
    for ln in text.splitlines():
        if re.fullmatch(r"\s*0\d\s*", ln):
            continue
        for m in NUM_RE.finditer(ln):
            c[m.group(0)] += 1
    return c


# 机构名：正文里出现的必须能在底稿里找到；底稿里的机构观点必须有可回查的链接或落盘文件。
ORG_RE = re.compile(r"高盛|摩根士丹利|大摩|摩根大通|小摩|巴克莱|瑞银|花旗|美银|富国|野村|汇丰|德意志银行|杰富瑞|伯恩斯坦"
                    r"|[一-龥]{2,6}(?:证券|期货|基金|资本|研究院|银行)(?!业|板块|股|指数|ETF)")
LINK_RE = re.compile(r"https?://|\.json|\.pdf|\.html?|\.md\b|同上")
VIEW_RE = re.compile(r"研报|报告|观点|认为|指出|预计|策略|评级|目标价")


def _ledger_unlinked(ledger: str) -> list[tuple[str, tuple[str, str]]]:
    out = []
    for ln in ledger.splitlines():
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if len(cells) < 11 or cells[0] in ("编号", "") or set(cells[0]) <= set("-:") or cells[0].startswith("示例"):
            continue
        link = cells[6]
        if LINK_RE.search(link):
            continue
        row_text = " ".join(cells[2:6])
        if ORG_RE.search(cells[4]) or VIEW_RE.search(cells[2] + cells[4] + cells[5]):
            out.append((cells[0], ("error", f"是机构观点，但“链接或落盘文件”一栏没有链接或文件（{link or '空'}）：打开原文补上，补不上就不能写，改列进缺口")))
        else:
            out.append((cells[0], ("warn", f"没有可回查的链接或落盘文件（{link or '空'}）：{row_text[:30]}")))
    return out


def cmd_lint(args) -> dict:
    text = Path(args.file).read_text(encoding="utf-8")
    issues: list[dict] = []

    def add(level, line, msg):
        issues.append({"level": level, "line": line, "msg": msg})

    year = args.year or now().year
    first_sec = next((i for i, l in enumerate(text.splitlines(), 1) if re.fullmatch(r"\s*0\d\s*", l)), 10 ** 9)
    for ln_no, ln in enumerate(text.splitlines(), 1):
        if re.search(r"https?://|www\.", ln):
            add("error", ln_no, "正文含网址")
        if re.search(r"!\[|【图】|\[插图\]", ln) or ln.strip() == "图片":
            add("error", ln_no, "图片占位符")
        for m in re.finditer(r"(?<![\d.])(?:[036]\d{5}|688\d{3}|8[37]\d{4}|4[03]\d{4}|92\d{4})(?:\.(?:SH|SZ|BJ))?"
                             r"(?![\d.%亿万元吨股手只家艘人])", ln):
            add("error", ln_no, f"疑似证券代码：{m.group(0)}")
        for w in HARD_BANS:
            if w in ln:
                add("error", ln_no, f"硬禁用表达：{w}")
        for w in RESIDUE:
            if w in ln:
                add("error", ln_no, f"编辑残留：{w}")
        if "**" in ln or re.search(r"[\U0001F300-\U0001FAFF☀-➿]|━|➡|→", ln):
            add("warn", ln_no, "正文排版装饰（粗体/表情/箭头/分隔线）")
        if re.match(r"\s*[-*•]\s+", ln):
            add("warn", ln_no, "正文出现项目符号")
        for m in re.finditer(r"(涨|跌)(?:幅)?(?:达|至)?(\d+\.\d{2})%", ln if ln_no > first_sec else ""):
            add("warn", ln_no, f"行情数字过精确：{m.group(0)}，按1.3写成约数（核心事件数字除外）")
        for m in re.finditer(r"(\d{1,2})月(\d{1,2})日[（(](?:周|星期)([一二三四五六日天])[）)]", ln):
            try:
                d = dt.date(year, int(m.group(1)), int(m.group(2)))
                real = WEEK[d.weekday()]
                if m.group(3).replace("天", "日") != real:
                    add("error", ln_no, f"星期错误：{m.group(0)} 实为星期{real}（{year}年）")
            except ValueError:
                add("error", ln_no, f"日期不存在：{m.group(0)}")
        for w in SOFT:
            if w in ln:
                add("info", ln_no, f"空泛/拔高用语，按11.3判断是否删除：{w}")
        for sent in re.split(r"(?<=[。！？])", ln):
            for m in GENERIC_SUBJ.finditer(sent):
                if not MEDIA_CUE.search(sent[: m.start()]):
                    add("error", ln_no, f"笼统归因没有点名信源：“{m.group(0)}”，按1.7写出具体机构或媒体")
        for m in PLACEHOLDER.finditer(ln):
            add("error", ln_no, f"信源占位写法：{m.group(0)}")
        if re.search(r"雪球(?:用户|网友|上)?@|雪球用户", ln):
            add("error", ln_no, "正文出现雪球用户发言：雪球讨论只放文后附录 xueqiu.md（10.4）")

    # 节尾检查：编号行把正文切成节，看每节最后一段的最后一句
    sections, cur = [], []
    for ln in text.splitlines():
        if re.fullmatch(r"\s*0\d\s*", ln):
            sections.append(cur)
            cur = []
        else:
            cur.append(ln)
    sections.append(cur)
    for i, sec in enumerate(sections[1:], 1):
        body = "\n".join(sec)
        body = re.split(r"\n\s*(?:风险提示|数据来源)", body)[0]
        paras = [p for p in body.split("\n\n") if p.strip()]
        if not paras:
            continue
        sents = [x for x in re.split(r"(?<=[。！？])", paras[-1].strip()) if x.strip()]
        last_sent = sents[-1].strip() if sents else ""
        for w in TAIL_BANS:
            if last_sent.startswith(w) or w in last_sent[:12]:
                add("error", None, f"第{i}节结尾是总结句：{last_sent[:30]}…")
                break

    report: dict[str, Any] = {"file": args.file}
    if args.ledger:
        ledger = Path(args.ledger).read_text(encoding="utf-8")
        led_nums = {_norm_num(m.group(2)) for m in NUM_RE.finditer(ledger)}
        missing = []
        for tok in _numbers(text):
            m = NUM_RE.fullmatch(tok)
            if not m:
                continue
            n = m.group(2)
            if not m.group(1) and not m.group(3) and float(n) <= 12:
                continue  # 不带单位的小整数（节号、天数等）不查
            if re.fullmatch(r"20\d\d", n) or m.group(3) == "年":
                continue
            if _norm_num(n) not in led_nums:
                missing.append(tok)
        for tok in missing:
            add("error", None, f"底稿里找不到的数字：{tok}")
        report["ledger_missing_numbers"] = missing
        for row_id, why in _ledger_unlinked(ledger):
            add(why[0], None, f"底稿第{row_id}行{why[1]}")
        body_only = "\n".join(l for l in text.splitlines() if not re.match(r"\s*(?:数据来源|风险提示)", l))
        for org in sorted({m.group(0) for m in ORG_RE.finditer(body_only)}):
            if org not in ledger:
                add("error", None, f"正文提到的机构不在底稿里：{org}")

    if args.before:
        before = Path(args.before).read_text(encoding="utf-8")
        if before.strip() == text.strip():
            add("warn", None, "终稿与初稿完全相同：第四步的检查和去AI味没有产生任何修改，确认这一步确实做过；交付时注明“未做修订”")
        nb, na = _numbers(before), _numbers(text)
        for tok in (na - nb).elements():
            add("error", None, f"润色后新增或改变的数字/限定：{tok}")
        for tok in (nb - na).elements():
            add("warn", None, f"润色后消失的数字/限定：{tok}（确认不是删了独立信息或改了限定词）")
        for w in QUALIFIERS:
            cb, ca = before.count(w), text.count(w)
            if cb != ca:
                add("warn", None, f"确定性/状态词“{w}”出现次数变化：{cb} → {ca}")
        sb = {m.group(1) for m in ATTR_RE.finditer(before)}
        sa = {m.group(1) for m in ATTR_RE.finditer(text)}
        for x in sorted(sa - sb):
            add("error", None, f"润色后新增的引述主体：{x}")
        for x in sorted(sb - sa):
            add("warn", None, f"润色后消失的引述主体：{x}")

    order = {"error": 0, "warn": 1, "info": 2}
    issues.sort(key=lambda i: (order[i["level"]], i["line"] or 0))
    report.update({"errors": sum(i["level"] == "error" for i in issues),
                   "warnings": sum(i["level"] == "warn" for i in issues),
                   "issues": issues,
                   "note": "error 必须改完再交付；warn 逐条判断；info 是提醒。lint 只查机械问题，不能替代事实核查。"})
    return report


# ================================================================ 雪球附录核对
XQ_QUOTE = re.compile(r"「([^」]{2,})」")
XQ_HEAD = re.compile(r"^\s*\d+[.、]\s*@(?P<user>[^｜|]+)[｜|](?P<time>[^｜|]+)[｜|](?P<link>.+?)\s*$")


def _flat_strings(x: Any) -> list[str]:
    if isinstance(x, str):
        return [x]
    if isinstance(x, dict):
        return [s for v in x.values() for s in _flat_strings(v)] + [str(k) for k in x.keys()]
    if isinstance(x, list):
        return [s for v in x for s in _flat_strings(v)]
    return [str(x)] if x is not None else []


def _xq_norm(t: str) -> str:
    t = re.sub(r"<[^>]+>", "", t)
    t = t.replace("&nbsp;", "").replace("\\n", "")
    return re.sub(r"[\s\u3000]+", "", t)


def cmd_xqcheck(args) -> dict:
    """逐条核对 xueqiu.md：每段「摘录」、用户名、帖子链接或 id 都要能在 xueqiu-scraper 的原始输出里找到。"""
    path = Path(args.file)
    text = path.read_text(encoding="utf-8")
    src_dir = Path(args.src) if args.src else path.parent
    files = [f for f in sorted(src_dir.rglob("*")) if f.is_file() and f.name.startswith("xq")
             and f.resolve() != path.resolve() and f.suffix.lower() in (".json", ".jsonl", ".txt", ".md", ".csv", ".html")]
    corpus_parts = []
    for f in files:
        raw = f.read_text(encoding="utf-8", errors="replace")
        corpus_parts.append(raw)
        try:
            corpus_parts.extend(_flat_strings(json.loads(raw)))
        except Exception:  # noqa: BLE001
            for ln in raw.splitlines():
                try:
                    corpus_parts.extend(_flat_strings(json.loads(ln)))
                except Exception:  # noqa: BLE001
                    pass
    corpus = _xq_norm("\n".join(corpus_parts))
    issues: list[dict] = []

    def add(level, line, msg):
        issues.append({"level": level, "line": line, "msg": msg})

    if not files:
        add("error", None, f"{src_dir} 下没有 xq 开头的原始输出文件：先把 xueqiu-scraper 的输出原样存进运行目录")
    n_quotes = 0
    for ln_no, ln in enumerate(text.splitlines(), 1):
        h = XQ_HEAD.match(ln)
        if h and files:
            user = h.group("user").strip()
            if user != "缺" and _xq_norm(user) not in corpus:
                add("error", ln_no, f"用户名在原始输出里找不到：@{user}")
            link = h.group("link").strip()
            ids = re.findall(r"\d{6,}", link)
            if link != "缺" and ids and not any(i in corpus for i in ids):
                add("error", ln_no, f"帖子链接或 id 在原始输出里找不到：{link[:60]}")
        for m in XQ_QUOTE.finditer(ln):
            n_quotes += 1
            if not files:
                continue
            segs = [x for x in re.split(r"……|\.\.\.|…", m.group(1)) if _xq_norm(x)]
            for seg in segs:
                ns = _xq_norm(seg)
                if len(ns) >= 4 and ns not in corpus:
                    add("error", ln_no, f"摘录在原始输出里找不到（必须逐字复制）：「{seg[:30]}」")
    if n_quotes == 0 and "没有合格帖子" not in text and "抓取失败" not in text:
        add("warn", None, "附录里没有任何「」摘录，也没有写明“没有合格帖子”或“抓取失败”")
    for w in ("点赞", "热度", "评论数", "转发数", "排名"):
        if w in text:
            add("warn", None, f"附录出现“{w}”：10.4 要求不写热度和互动数")
    order = {"error": 0, "warn": 1, "info": 2}
    issues.sort(key=lambda i: (order[i["level"]], i["line"] or 0))
    return {"file": str(path), "sources": [f.name for f in files], "quotes": n_quotes,
            "errors": sum(i["level"] == "error" for i in issues),
            "warnings": sum(i["level"] == "warn" for i in issues), "issues": issues,
            "note": "error 必须改完再交付。只能证明摘录确实出自抓取结果，观点是否有见地、事实是否属实仍需人工判断"}


# ================================================================ 自检
def _must(v: Any) -> Any:
    if not v:
        raise SourceError("不可用")
    return v


def cmd_selftest(args) -> dict:
    checks = {
        "sina 行情": lambda: sina_quotes(["sh600519"])["sh600519"]["price"],
        "腾讯 行情": lambda: tencent_quotes(["sh600519"])["sh600519"]["price"],
        "交易日（K线推断）": lambda: recent_trade_days()[-1],
        "交易日历（akshare）": lambda: len(_must(_cached_calendar())),
        "东财 板块(直连)": lambda: len(em_clist({"fs": BOARD_FS["industry"], "fid": "f3", "pz": 3, "fields": "f12,f14,f3"})),
        "东财 二级行业(直连)": lambda: len(_must(em_clist_all({"fs": INDUSTRY_LEVEL_FS["2"], "fid": "f3",
                                                         "fields": "f12,f14,f3"}))),
        "东财 板块(akshare)": lambda: len(_boards_ak("industry", "f3", 3)[0]),
        "新浪 板块": lambda: len(_boards_sina("industry", "f3", 3)[0]),
        "东财 板块资金流(akshare)": lambda: len(_boards_ak("industry", "f62", 3)[0]),
        "东财 涨跌家数(ulist)": lambda: breadth_ulist()["up"],
        "乐咕 涨跌家数(akshare)": lambda: breadth_legu()["up"],
        "东财 涨停池": lambda: len(em_pool("zt", ltd_compact())),
        "东财 K线": lambda: len(em_kline("600519", 5)),
        "腾讯 K线": lambda: len(tencent_kline("600519", 5)),
        "东财 研报库": lambda: len(http_json("https://reportapi.eastmoney.com/report/list",
                                         {"pageSize": 5, "pageNo": 1, "qType": 0, "industryCode": "*",
                                          "industry": "*", "rating": "*",
                                          "beginTime": (now() - dt.timedelta(days=7)).date().isoformat(),
                                          "endTime": now().date().isoformat()}).get("data") or []),
        "财联社 电报直连": lambda: len(_must(cls_page(int(time.time())))),
        "Yahoo 直连": lambda: _guard("yahoo", lambda: yahoo_chart("GC=F"))["close"],
        "新浪 海外": lambda: sina_global("^GSPC")["close"],
        "东财 海外": lambda: em_global("^GSPC")["close"],
        "yfinance": lambda: yf_quote("GC=F")["close"],
        "akshare 宏观日历": lambda: len(need_akshare().news_economic_baidu(date=now().strftime("%Y%m%d"))),
    }
    global HEALTH_BYPASS
    HEALTH_BYPASS = True  # 自检无视坏源记忆，重新检测，结果会刷新记忆

    def timed(fn):
        def run():
            t0 = time.time()
            v = fn()
            return v, int((time.time() - t0) * 1000)
        return run

    t_all = time.time()
    out = parallel({k: timed(fn) for k, fn in checks.items()}, workers=10)
    res = []
    for name in checks:
        ok, v = out[name]
        if ok:
            res.append({"source": name, "ok": True, "sample": v[0], "ms": v[1]})
        else:
            res.append({"source": name, "ok": False, "error": str(v)[:160]})
    HEALTH_BYPASS = False
    return {"results": res, "fetched_at": stamp(), "elapsed_s": round(time.time() - t_all, 1),
            "tip": "板块三个源至少一个可用即可；首次使用再跑一次 `index --verify`，确认涨跌家数两种统计一致"}


# ================================================================ 输出
def render(obj: Any, indent: int = 0) -> str:
    pad = "  " * indent
    if isinstance(obj, dict):
        lines = []
        for k, v in obj.items():
            if isinstance(v, (dict, list)) and v:
                lines.append(f"{pad}{k}:")
                lines.append(render(v, indent + 1))
            else:
                lines.append(f"{pad}{k}: {v}")
        return "\n".join(lines)
    if isinstance(obj, list):
        out = []
        for x in obj:
            if isinstance(x, dict):
                out.append(pad + "- " + " | ".join(f"{k}={v}" for k, v in x.items() if v not in (None, "", [])))
            else:
                out.append(f"{pad}- {x}")
        return "\n".join(out)
    return f"{pad}{obj}"


NO_SAVE = {"now", "weekday", "selftest", "lint", "ledger", "xqcheck"}


def main() -> None:
    p = argparse.ArgumentParser(description="财经热点写作数据工具", formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--json", action="store_true", help="输出 JSON")
    p.add_argument("--no-save", action="store_true", help="不把结果落盘")
    p.add_argument("--out", help="落盘目录（默认 ./fha-runs；Windows 上在 skill 文件夹里运行时为 ~/Documents/fha-workspace/runs；可用环境变量 FHA_RUNS 指定）")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("now")
    s = sub.add_parser("weekday"); s.add_argument("date")
    s = sub.add_parser("quote"); s.add_argument("codes", nargs="+")
    s = sub.add_parser("index"); s.add_argument("--verify", action="store_true")
    for name in ("boards", "flow"):
        s = sub.add_parser(name)
        s.add_argument("--type", choices=["industry", "concept"], default="industry")
        s.add_argument("--top", type=int, default=10)
        s.add_argument("--min-size", type=int, default=5, help="成分股少于此数的板块不进排行")
        s.add_argument("--level", choices=["1", "2", "all"], default="2",
                       help="行业层级：1=一级（约31个），2=二级（约128个，默认），all=一二三级混排")
    s = sub.add_parser("limit"); s.add_argument("--date")
    s = sub.add_parser("scan"); s.add_argument("--date")
    s = sub.add_parser("recap"); s.add_argument("--no-ledger", action="store_true", help="不写入事实底稿")
    s = sub.add_parser("kline"); s.add_argument("code"); s.add_argument("--days", type=int, default=30)
    s = sub.add_parser("reports")
    s.add_argument("--code"); s.add_argument("--kw"); s.add_argument("--org")
    s.add_argument("--days", type=int, default=30); s.add_argument("--limit", type=int, default=20)
    s.add_argument("--max-pages", type=int, default=20)
    s = sub.add_parser("news"); s.add_argument("kind", choices=["cls", "stock"]); s.add_argument("code", nargs="?")
    s.add_argument("--important", action="store_true"); s.add_argument("--limit", type=int, default=60)
    s.add_argument("--hours", type=float, default=8)
    s = sub.add_parser("calendar"); s.add_argument("--date")
    s = sub.add_parser("global"); s.add_argument("--symbols", nargs="*")
    s = sub.add_parser("ledger"); s.add_argument("action", choices=["init"]); s.add_argument("--force", action="store_true")
    s = sub.add_parser("lint"); s.add_argument("file"); s.add_argument("--ledger"); s.add_argument("--before")
    s.add_argument("--year", type=int)
    s = sub.add_parser("xqcheck"); s.add_argument("file"); s.add_argument("--src", help="xueqiu-scraper 原始输出所在目录（默认与 xueqiu.md 同目录）")
    sub.add_parser("selftest")
    args = p.parse_args()
    if args.cmd == "news" and args.kind == "stock" and not args.code:
        p.error("news stock 需要股票代码")
    fn = globals()["cmd_" + args.cmd]
    try:
        result = fn(args)
    except Exception as e:  # noqa: BLE001
        result = {"error": str(e), "hint": "运行 selftest 查看哪个数据源不可用"}
    if args.cmd not in NO_SAVE and not args.no_save and not (isinstance(result, dict) and "error" in result):
        try:
            f = run_dir(args) / f"{now().strftime('%H%M%S')}-{args.cmd}.json"
            f.write_text(json.dumps({"argv": sys.argv[1:], "result": result}, ensure_ascii=False, indent=1,
                                    default=str), encoding="utf-8")
            if isinstance(result, dict):
                result["saved_to"] = str(f)
            else:
                result = {"saved_to": str(f), "rows": result}
        except Exception:  # noqa: BLE001
            pass
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1, default=str))
    else:
        print(render(result))
    if args.cmd in ("lint", "xqcheck") and isinstance(result, dict) and result.get("errors"):
        sys.exit(1)


if __name__ == "__main__":
    main()
