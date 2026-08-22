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
  #summary{background:#fff;margin:10px 12px 8px;padding:16px 20px 12px;border-radius:16px;
    box-shadow:0 1px 4px rgba(0,0,0,.06)}
  #summary .label{font-size:13px;color:#9ca3af;letter-spacing:1px}
  #summary .row{display:flex;align-items:baseline;margin-top:4px}
  #summary .val{font-size:clamp(34px,10vw,48px);font-weight:700;line-height:1.15;font-variant-numeric:tabular-nums}
  #summary .unit{font-size:15px;color:#9ca3af;margin-left:6px}
  #meta{margin-top:8px;font-size:12px;color:#9ca3af;display:flex;gap:12px;flex-wrap:wrap}
  #ranges{display:flex;gap:8px;margin-top:12px}
  #ranges button{flex:1;border:1px solid #e5e7eb;background:#f9fafb;border-radius:999px;
    padding:6px 0;font-size:12px;color:#6b7280;cursor:pointer}
  #ranges button.on{background:#2563eb;color:#fff;border-color:#2563eb}
  #chart{flex:1;min-height:240px;background:#fff;margin:0 12px 8px;border-radius:16px;
    box-shadow:0 1px 4px rgba(0,0,0,.06);overflow:hidden}
  #cv{display:block;width:100%;height:100%}
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
    <div id="ranges">
      <button data-r="all" class="on">全部</button>
      <button data-r="7">近7天</button>
      <button data-r="30">近30天</button>
    </div>
  </div>
  <div id="chart"><canvas id="cv"></canvas></div>
  <div id="foot">数据来自南昌工学院智能收费系统（__ROOM__）<br>约 30 分钟采集一次 · 本页静默自动更新，无需手动刷新</div>
</div>
<script>
var RAW = __DATA__;
function pad(n){ return n < 10 ? '0' + n : '' + n; }
function fmtDT(ts){
  return pad(ts.getMonth()+1) + '-' + pad(ts.getDate()) + ' ' +
         pad(ts.getHours()) + ':' + pad(ts.getMinutes());
}
function renderCard(){
  var last = RAW.length ? RAW[RAW.length - 1] : null;
  var total = last ? Number(last[3]) : null;
  document.getElementById('val').textContent =
      (total === null || isNaN(total)) ? '--' : total.toFixed(2);
  var ts = last ? new Date(last[0]) : null;
  var tstr = ts ? fmtDT(ts) : '--';
  document.getElementById('meta').innerHTML =
      '共 <b>' + RAW.length + '</b> 条记录<span style="margin:0 6px">·</span>更新于 ' + tstr;
}
/* ============ 纯 Canvas 折线图 (零外部依赖, 秒开) ============ */
var canvas = document.getElementById('cv');
var ctx = canvas.getContext('2d');
var RANGE = 'all';
function draw(){
  var dpr = window.devicePixelRatio || 1;
  var w = canvas.clientWidth, h = canvas.clientHeight;
  if (!w || !h){ canvas.width = canvas.height = 0; return; }
  canvas.width = Math.round(w * dpr);
  canvas.height = Math.round(h * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  var pts = [];
  for (var i = 0; i < RAW.length; i++){
    var v = Number(RAW[i][3]);
    if (isNaN(v)) continue;
    pts.push({t: new Date(RAW[i][0]), v: v});
  }
  if (RANGE !== 'all' && pts.length){
    var end = pts[pts.length - 1].t.getTime();
    var start = end - parseInt(RANGE, 10) * 86400000;
    pts = pts.filter(function(p){ return p.t.getTime() >= start; });
  }
  if (!pts.length){
    ctx.fillStyle = '#9ca3af'; ctx.font = '13px sans-serif';
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.fillText('暂无数据', w / 2, h / 2);
    return;
  }
  var padL = 46, padR = 14, padT = 12, padB = 26;
  var pw = w - padL - padR, ph = h - padT - padB;
  var mn = Infinity, mx = -Infinity;
  for (var j = 0; j < pts.length; j++){
    if (pts[j].v < mn) mn = pts[j].v;
    if (pts[j].v > mx) mx = pts[j].v;
  }
  var lo = Math.floor((mn - 0.5) * 10) / 10;
  var hi = Math.ceil((mx + 0.5) * 10) / 10;
  if (hi - lo < 1){ hi = lo + 1; }
  var t0 = pts[0].t.getTime(), t1 = pts[pts.length - 1].t.getTime();
  var span = (t1 - t0) || 1;
  function X(t){ return padL + (t.getTime() - t0) / span * pw; }
  function Y(v){ return padT + (hi - v) / (hi - lo) * ph; }
  ctx.font = '10px sans-serif'; ctx.textBaseline = 'middle';
  var steps = 4;
  for (var s = 0; s <= steps; s++){
    var vv = lo + (hi - lo) * s / steps;
    var yy = Y(vv);
    ctx.strokeStyle = '#f0f2f5'; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(padL, yy); ctx.lineTo(padL + pw, yy); ctx.stroke();
    ctx.fillStyle = '#9ca3af'; ctx.textAlign = 'right';
    ctx.fillText(vv.toFixed(1), padL - 6, yy);
  }
  ctx.textAlign = 'center'; ctx.textBaseline = 'top';
  ctx.fillStyle = '#9ca3af';
  ctx.fillText(fmtDT(pts[0].t), padL, padT + ph + 6);
  ctx.fillText(fmtDT(pts[pts.length - 1].t), padL + pw, padT + ph + 6);
  if (span > 36 * 3600000){
    var mid = new Date((t0 + t1) / 2);
    ctx.fillText(pad(mid.getMonth()+1) + '-' + pad(mid.getDate()) + ' ' +
                 pad(mid.getHours()) + ':00', padL + pw / 2, padT + ph + 6);
  }
  /* 渐变面积 */
  ctx.beginPath();
  ctx.moveTo(X(pts[0].t), Y(pts[0].v));
  for (var k = 1; k < pts.length; k++) ctx.lineTo(X(pts[k].t), Y(pts[k].v));
  ctx.lineTo(X(pts[pts.length - 1].t), padT + ph);
  ctx.lineTo(X(pts[0].t), padT + ph);
  ctx.closePath();
  var g = ctx.createLinearGradient(0, padT, 0, padT + ph);
  g.addColorStop(0, 'rgba(37,99,235,.22)');
  g.addColorStop(1, 'rgba(37,99,235,0)');
  ctx.fillStyle = g; ctx.fill();
  /* 折线 */
  ctx.beginPath();
  ctx.moveTo(X(pts[0].t), Y(pts[0].v));
  for (var m = 1; m < pts.length; m++) ctx.lineTo(X(pts[m].t), Y(pts[m].v));
  ctx.strokeStyle = '#2563eb'; ctx.lineWidth = 2.2;
  ctx.lineJoin = 'round'; ctx.lineCap = 'round'; ctx.stroke();
  /* 最后一点高亮 + 数值 */
  var lp = pts[pts.length - 1];
  ctx.beginPath(); ctx.arc(X(lp.t), Y(lp.v), 4.5, 0, Math.PI * 2);
  ctx.fillStyle = '#fff'; ctx.fill();
  ctx.lineWidth = 2.5; ctx.strokeStyle = '#2563eb'; ctx.stroke();
  ctx.font = 'bold 11px sans-serif'; ctx.textAlign = 'left'; ctx.textBaseline = 'bottom';
  ctx.fillStyle = '#2563eb';
  ctx.fillText(lp.v.toFixed(1) + ' 度', Math.min(X(lp.t) + 8, padL + pw - 64), Y(lp.v) - 6);
}
/* ============ 范围切换按钮 ============ */
var btns = document.querySelectorAll('#ranges button');
for (var b = 0; b < btns.length; b++){
  btns[b].addEventListener('click', function(){
    RANGE = this.getAttribute('data-r');
    for (var q = 0; q < btns.length; q++) btns[q].className = '';
    this.className = 'on';
    draw();
  });
}
/* ============ 静默原地刷新 (免整页重载; 本地 file:// 时退回整页刷新) ============ */
var useFetch = location.protocol !== 'file:';
function refresh(){
  if (!useFetch){ location.reload(); return; }
  fetch('monitor_data.csv', {cache: 'no-store'})
    .then(function(r){ if (!r.ok) throw new Error('http ' + r.status); return r.text(); })
    .then(function(txt){
      var rows = [];
      var lines = txt.split(/\\r?\\n/);
      for (var i = 0; i < lines.length; i++){
        if (!lines[i].trim()) continue;
        var p = lines[i].split(',');
        if (p.length >= 4 && p[0] !== 'time'){
          var v1 = Number(p[1]), v3 = Number(p[3]);
          if (!isNaN(v1) && !isNaN(v3)) rows.push([p[0], v1, Number(p[2]), v3]);
        }
      }
      if (rows.length){ RAW = rows; renderCard(); draw(); }
    })
    .catch(function(){ location.reload(); });
}
if (useFetch){
  setInterval(refresh, 5 * 60 * 1000);
  setTimeout(refresh, 2500);
} else {
  setTimeout(function(){ location.reload(); }, 5 * 60 * 1000);
}
/* ============ 启动 ============ */
renderCard();
draw();
window.addEventListener('resize', draw);
window.addEventListener('orientationchange', function(){ setTimeout(draw, 300); });
requestAnimationFrame(draw);
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