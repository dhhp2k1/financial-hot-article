"""v7.5 升级后一键核对。放在 skill 的 scripts/ 目录，运行：python3 scripts/check.py
结果打印并写到当前目录的 check-out.txt，整个发给维护人。只读取数，不写事实底稿。"""
import datetime as dt
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if not (ROOT / "SKILL.md").exists():  # 放在 skill 外面（如 fha-workspace\tools）时，指向默认安装位置
    ROOT = Path(r"C:\Users\50331\.codex\skills\financial-hot-article")
MKT = ROOT / "scripts" / "mkt.py"
OUT = open("check-out.txt", "w", encoding="utf-8")
EXPECT = {"SKILL.md": "72c8dd1768d10925d29bb7f4843adffff078e7840c4c91167cc6294df2e9d3c5",
          "scripts/mkt.py": "96aaf915ae213ba1f869b6e65ce87dc56a0673e4799609789f744226791bf537",
          "references/data-sources.md": "47847d007e840c1a21039a819852ee86304b86ed7a80ee8d188bce11d2aec941"}


def w(s=""):
    print(s, flush=True)
    OUT.write(s + "\n")
    OUT.flush()


def run(*args, timeout=300):
    t = time.time()
    try:
        p = subprocess.run([sys.executable, str(MKT), "--json", "--no-save", *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        took = time.time() - t
        try:
            return json.loads(p.stdout), took, p.stderr[-500:]
        except Exception:  # noqa: BLE001
            return {"raw": p.stdout[-3000:]}, took, p.stderr[-1500:]
    except subprocess.TimeoutExpired:
        return {"error": f"超过{timeout}秒未结束"}, time.time() - t, ""


w(f"check at {dt.datetime.now():%Y-%m-%d %H:%M:%S} | python {sys.version.split()[0]}")
w("\n## 文件版本")
for f, h in EXPECT.items():
    got = hashlib.sha256((ROOT / f).read_bytes()).hexdigest() if (ROOT / f).exists() else "缺失"
    w(f"{'OK  ' if got == h else 'DIFF'} {f} {got[:12]}")

w("\n## selftest")
r, took, err = run("selftest")
w(f"耗时 {took:.1f}s")
for x in r.get("results", []):
    w(f"  {'OK  ' if x.get('ok') else 'FAIL'} {x['source']} {x.get('ms', '')}ms {str(x.get('sample', x.get('error', '')))[:100]}")
if "results" not in r:
    w(json.dumps(r, ensure_ascii=False)[:2000]); w(err)

for i in (1, 2):
    w(f"\n## global 第{i}次")
    r, took, err = run("global")
    w(f"耗时 {took:.1f}s | sources_down {r.get('sources_down')} | missing {r.get('missing', r.get('error', ''))}")
    for x in r.get("rows", []):
        w(f"  {x.get('symbol'):9s} {x.get('name', '')} | {x.get('source', 'ERR')} | {x.get('close', '')} | {x.get('pct', '')}% | "
          f"{x.get('bar_date', '')} | {x.get('basis', x.get('error', ''))[:80]}")
    if "rows" not in r:
        w(json.dumps(r, ensure_ascii=False)[:2000]); w(err)

w("\n## scan（v7.5：海外、美股个股、缺口）")
r, took, err = run("scan")
w(f"耗时 {took:.1f}s")
uw = r.get("us_watch") or {}
for g, v in (uw.get("groups") or {}).items():
    w(f"  {g} 平均{v['avg_pct']}% | " + "，".join(f"{x['symbol']}{x['pct']}%({x['data_date']})" for x in v["rows"]))
if not uw.get("groups"):
    w(f"  us_watch 没取到：{json.dumps(uw, ensure_ascii=False)[:300]}")
w("  gaps: " + json.dumps(r.get("gaps"), ensure_ascii=False))
if "gaps" not in r:
    w(json.dumps(r, ensure_ascii=False)[:2000]); w(err)

w("\n## quote")
r, took, err = run("quote", "600519", "300750", "sh000001")
rows = r if isinstance(r, list) else r.get("rows", [])
w(f"耗时 {took:.1f}s")
for x in rows:
    w(f"  {x.get('code')} {x.get('name', '')} | {x.get('source', '')} | {x.get('cross_check', x.get('error', ''))} | "
      f"{x.get('pct', '')}% | {x.get('basis', '')}")
if not rows:
    w(json.dumps(r, ensure_ascii=False)[:2000]); w(err)

w("\n## recap")
now = dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))
if now.hour * 100 + now.minute < 1505:
    w("北京时间15:05前不跑 recap（数据不是收盘口径）。收盘后再运行一次本脚本即可。")
else:
    r, took, err = run("recap", "--no-ledger")
    w(f"耗时 {took:.1f}s | warnings {r.get('warnings')}")
    w(f"板块层级 {r.get('industry_level')} | 个数 {r.get('industry_counts')}")
    w(f"居前 {r.get('industry_top')} | 居后 {r.get('industry_bottom')}")
    w("草稿：\n" + (r.get("draft") or r.get("draft_blocked") or json.dumps(r, ensure_ascii=False)[:2000]))

w("\nDONE")
OUT.close()
