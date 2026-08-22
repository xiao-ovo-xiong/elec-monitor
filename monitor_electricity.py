#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
monitor_electricity.py - 剩余电量采集 + 折线图生成
============================================================
用法:
    python monitor_electricity.py                 # 采集一次并更新图表
    python monitor_electricity.py --loop 1800     # 每1800秒采集一次(电脑/手机常驻时用)
    python monitor_electricity.py --once --chart  # 只重新生成图表不采集

数据:   追加到 monitor_data.csv (time,剩余购电,剩余补助,合计)
图表:   生成 chart.html (网页折线图, 自包含, 手机可看) 和 chart.png (如安装了matplotlib)

部署在 GitHub Actions 时, openid 等不需要保密(本来就是公开抓包得到的),
也可以通过环境变量 MONITOR_OPENID / MONITOR_ROOMDM / MONITOR_ROOM 覆盖。
"""
import argparse
import datetime
import json
import os
import re
import sys
import time

try:
    import requests
except ImportError:
    sys.exit("缺少 requests, 请执行: pip install requests")

try:
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib import font_manager
    import matplotlib.pyplot as plt
    for name in ("Microsoft YaHei", "SimHei", "Noto Sans CJK SC",
                 "WenQuanYi Zen Hei", "PingFang SC", "Arial Unicode MS"):
        try:
            font_manager.findfont(name, fallback_to_default=False)
            plt.rcParams["font.sans-serif"] = [name]
            break
        except Exception:
            continue
    plt.rcParams["axes.unicode_minus"] = False
    HAS_MPL = True
except Exception:
    HAS_MPL = False

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ================= 配置(与 query_api.py 相同) =================
BASE = os.environ.get("MONITOR_BASE", "http://sf.ncpu.edu.cn:9090")
ROOM = os.environ.get("MONITOR_ROOM", "4栋/2层/4-267")
UA = ("Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/107.0.5304.110 Safari/537.36 Language/zh "
      "ColorScheme/Light wxwork/5.0.10 (MicroMessenger/6.2) WindowsWechat  "
      "MailPlugin_Electron WeMail embeddisk wwmver/3.26.510.632")
CONFIG = {
    "openid": os.environ.get("MONITOR_OPENID", "qw178736651287435066286538242373"),
    "roomdm": os.environ.get("MONITOR_ROOMDM", "060267"),
    "room": ROOM,
}
DATA_FILE = os.path.join(BASE_DIR, "monitor_data.csv")
CHART_PNG = os.path.join(BASE_DIR, "chart.png")
CHART_HTML = os.path.join(BASE_DIR, "chart.html")
MIN_INTERVAL_S = 600   # 距上次采集不足10分钟则跳过(防重复)


def query():
    """查询当前剩余电量, 返回 (剩余购电, 剩余补助)"""
    s = requests.Session()
    s.headers["User-Agent"] = UA
    r = s.get(BASE + "/goQw", allow_redirects=True, timeout=15)
    r.raise_for_status()
    r = s.post(
        BASE + "/about/rebinding",
        data={"openid": CONFIG["openid"], "roomdm": CONFIG["roomdm"],
              "room": CONFIG["room"], "mode": "c"},
        headers={"X-Requested-With": "XMLHttpRequest",
                 "Referer": BASE + "/about/rebinding",
                 "Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    if r.status_code != 200:
        raise RuntimeError("绑定房间失败 HTTP %s" % r.status_code)
    r = s.get(BASE + "/use/record",
              headers={"Referer": BASE + "/about/rebinding"}, timeout=15)
    r.raise_for_status()
    html = r.text

    def grab(label):
        m = re.search(
            r'<div class="item-title">%s</div>\s*<div class="item-after">([\d.]+)度</div>' % label,
            html)
        return float(m.group(1)) if m else None

    buy = grab("剩余购电")
    sub = grab("剩余补助")
    if buy is None:
        raise RuntimeError("页面里没找到剩余购电, 学校可能改版了")
    return buy, (sub if sub is not None else 0.0)


def read_rows():
    rows = []
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, newline="", encoding="utf-8") as f:
            for line in csv_reader(f):
                if len(line) >= 4 and line[0] != "time":
                    try:
                        rows.append(datetime.datetime.fromisoformat(line[0]))
                        rows[-1] = [rows[-1], float(line[1]), float(line[2]), float(line[3])]
                    except Exception:
                        pass
    return rows


def csv_reader(f):
    import csv
    return csv.reader(f)


def append_row(now, buy, sub):
    rows = read_rows()
    if rows and (now - rows[-1][0]).total_seconds() < MIN_INTERVAL_S:
        return False
    import csv
    first = not os.path.exists(DATA_FILE) or os.path.getsize(DATA_FILE) == 0
    with open(DATA_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if first:
            w.writerow(["time", "剩余购电(度)", "剩余补助(度)", "合计(度)"])
        w.writerow([now.isoformat(timespec="seconds"), buy, sub, round(buy + sub, 2)])
    return True


def make_charts(rows):
    if not rows:
        return
    # ---------- chart.png ----------
    if HAS_MPL:
        try:
            times = [r[0] for r in rows]
            fig, ax = plt.subplots(figsize=(11, 5))
            ax.plot(times, [r[3] for r in rows], label="剩余电量(度)", lw=2.2,
                    marker="o", ms=3, color="#2563eb")
            ax.set_title("宿舍剩余电量监测 %s" % ROOM)
            ax.set_ylabel("度")
            ax.grid(True, alpha=0.3)
            ax.legend()
            fig.autofmt_xdate()
            fig.tight_layout()
            fig.savefig(CHART_PNG, dpi=110)
            plt.close(fig)
        except Exception as e:
            print("生成 chart.png 失败(不影响网页版):", e)
    # ---------- chart.html ----------
    data = [[r[0].astimezone().isoformat(timespec="seconds"), r[1], r[2], r[3]] for r in rows]
    html = TEMPLATE.replace("__DATA__", json.dumps(data, ensure_ascii=False)) \
                   .replace("__ROOM__", ROOM) \
                   .replace("__COUNT__", str(len(data)))
    with open(CHART_HTML, "w", encoding="utf-8") as f:
        f.write(html)


TEMPLATE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>宿舍剩余电量</title>
<style>
  *{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
  html,body{margin:0;padding:0;background:#f2f4f7;color:#1f2937;
    font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}
  #app{display:flex;flex-direction:column;height:100vh;height:100dvh;max-width:640px;margin:0 auto}
  #summary{background:#fff;margin:10px 12px 8px;padding:18px 20px;border-radius:16px;
    box-shadow:0 1px 4px rgba(0,0,0,.06)}
  #summary .label{font-size:13px;color:#9ca3af;letter-spacing:1px}
  #summary .row{display:flex;align-items:baseline;margin-top:4px}
  #summary .val{font-size:clamp(34px,10vw,48px);font-weight:700;line-height:1.15;font-variant-numeric:tabular-nums}
  #summary .unit{font-size:15px;color:#9ca3af;margin-left:6px}
  #meta{margin-top:8px;font-size:12px;color:#9ca3af;display:flex;gap:12px;flex-wrap:wrap}
  #chart{flex:1;min-height:240px;background:#fff;margin:0 12px 8px;border-radius:16px;
    box-shadow:0 1px 4px rgba(0,0,0,.06);position:relative}
  #empty{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
    color:#9ca3af;font-size:13px}
  #foot{padding:0 16px calc(12px + env(safe-area-inset-bottom));font-size:11px;
    color:#b6bec9;text-align:center;line-height:1.6}
</style>
</head>
<body>
<div id="app">
  <div id="summary">
    <div class="label">当前剩余电量</div>
    <div class="row"><span class="val" id="val">--</span><span class="unit">度</span></div>
    <div class="meta" id="meta"></div>
  </div>
  <div id="chart"><div id="empty" style="display:none">暂无数据</div></div>
  <div id="foot">数据来自南昌工学院智能收费系统（__ROOM__）<br>约 30 分钟采集一次 · 页面每 5 分钟自动刷新</div>
</div>
<script>
var RAW = __DATA__;
var CDNS = [
  'https://cdn.bootcdn.net/ajax/libs/echarts/5.4.3/echarts.min.js',
  'https://cdn.staticfile.org/echarts/5.4.3/echarts.min.js',
  'https://cdn.jsdelivr.net/npm/echarts@5.4.3/dist/echarts.min.js',
  'https://unpkg.com/echarts@5.4.3/dist/echarts.min.js'
];
function boot(i){
  if (i >= CDNS.length) {
    document.getElementById('empty').style.display = 'flex';
    document.getElementById('empty').textContent = '图表组件加载失败, 请检查网络后刷新';
    return;
  }
  var s = document.createElement('script');
  s.src = CDNS[i];
  s.onload = function(){ window.echarts ? initChart() : boot(i + 1); };
  s.onerror = function(){ boot(i + 1); };
  document.head.appendChild(s);
}
/* ---- 顶部数字卡片: 不依赖图表库, 立即渲染 ---- */
(function(){
  var last = RAW.length ? RAW[RAW.length - 1] : null;
  var total = last ? Number(last[3]) : null;
  document.getElementById('val').textContent =
      (total === null || isNaN(total)) ? '--' : total.toFixed(2);
  var meta = document.getElementById('meta');
  var ts = last ? new Date(last[0]) : null;
  function pad(n){ return n < 10 ? '0' + n : '' + n; }
  var tstr = ts ? pad(ts.getMonth()+1) + '-' + pad(ts.getDate()) + ' ' +
                   pad(ts.getHours()) + ':' + pad(ts.getMinutes()) : '--';
  meta.innerHTML = '共 <b>' + RAW.length + '</b> 条记录<span style="margin:0 6px">·</span>更新于 ' + tstr;
})();
/* ---- 折线图: 图表库加载完成后才初始化 ---- */
function initChart(){
  var chartEl = document.getElementById('chart');
  if (!RAW.length) {
    document.getElementById('empty').style.display = 'flex';
    return;
  }
  var isMobile = window.innerWidth < 768;
  var pts = RAW.map(function(r){ return [r[0], Number(r[3])]; });
  var chart = echarts.init(chartEl);
  var dz = [{type: 'inside'}];
  if (!isMobile) {
    dz.push({type: 'slider', height: 16, bottom: 10});
  }
  chart.setOption({
    tooltip: {trigger: 'axis', backgroundColor:'rgba(17,24,39,.92)', borderWidth:0,
      textStyle:{color:'#fff', fontSize:12},
      valueFormatter: function(v){ return Number(v).toFixed(2) + ' 度'; }},
    grid: {left: 14, right: 14, top: 18, bottom: 26, containLabel: true},
    xAxis: {type: 'time', axisLabel:{fontSize:10, color:'#9ca3af'},
      axisLine:{lineStyle:{color:'#e5e7eb'}}, axisTick:{show:false},
      splitLine:{show:false}},
    yAxis: {type: 'value', name: '度', nameTextStyle:{color:'#9ca3af', fontSize:10},
      axisLabel:{fontSize:10, color:'#9ca3af'},
      axisLine:{show:false}, axisTick:{show:false}, splitLine:{lineStyle:{color:'#f1f2f4', type:'dashed'}}},
    dataZoom: dz,
    series: [{
      name: '剩余电量', type: 'line', smooth: 0.3, symbol: 'circle',
      symbolSize: 4, showSymbol: RAW.length < 40, data: pts,
      lineStyle:{width: 2.5, color:'#2563eb'},
      itemStyle:{color:'#2563eb'},
      areaStyle:{color:{type:'linear', x:0, y:0, x2:0, y2:1,
        colorStops:[{offset:0, color:'rgba(37,99,235,.22)'},{offset:1, color:'rgba(37,99,235,0)'}]}}
    }]
  });
  function rs(){ chart.resize(); }
  window.addEventListener('resize', rs);
  window.addEventListener('orientationchange', function(){ setTimeout(rs, 300); });
  setTimeout(function(){ location.reload(); }, 5 * 60 * 1000);
}
boot(0);
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=int, default=0, help="循环采集间隔秒数(0=只跑一次)")
    ap.add_argument("--chart", action="store_true", help="只重新生成图表, 不采集")
    args = ap.parse_args()

    if args.chart:
        make_charts(read_rows())
        print("已重新生成图表:", CHART_HTML)
        return

    while True:
        now = datetime.datetime.now().astimezone()
        try:
            buy, sub = query()
            added = append_row(now, buy, sub)
            make_charts(read_rows())
            if added:
                print("%s 剩余购电=%.2f 剩余补助=%.2f 合计=%.2f (已记录)" % (
                    now.strftime("%m-%d %H:%M"), buy, sub, buy + sub))
            else:
                print("%s 距上次不足%d秒, 跳过记录" % (now.strftime("%H:%M:%S"), MIN_INTERVAL_S))
        except Exception as e:
            print("%s 采集失败: %s" % (now.strftime("%H:%M:%S"), e))
            if not args.loop:
                sys.exit(1)
        if not args.loop:
            break
        time.sleep(args.loop)


if __name__ == "__main__":
    main()