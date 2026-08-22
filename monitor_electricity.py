#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
monitor_electricity.py - 剩余电量采集 + 折线图生成 (双房间版)
============================================================
支持同时监控两个房间(4-267 / 4-265), 每个房间独立会话查询。

用法:
    python monitor_electricity.py                 # 采集一次并更新图表
    python monitor_electricity.py --loop 600     # 每600秒采集一次(电脑/手机常驻时用)
    python monitor_electricity.py --chart        # 只重新生成图表不采集

数据:   追加到 monitor_data.csv
        列: time,买1,补1,合1,买2,补2,合2   (1=房间1, 2=房间2)
图表:   生成 chart.html (网页折线图, 自包含, 手机可看) 和 chart.png (如安装了matplotlib)

部署在 GitHub Actions 时, openid 等不需要保密(本来就是公开抓包得到的),
也可以通过环境变量覆盖:
    MONITOR_OPENID / MONITOR_ROOMDM / MONITOR_ROOM        (房间1)
    MONITOR_ROOMDM2 / MONITOR_ROOM2                       (房间2)
    MONITOR_BASE / MONITOR_ROOM_SHORT / MONITOR_ROOM2_SHORT
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
ROOM2 = os.environ.get("MONITOR_ROOM2", "4栋/2层/4-265")
UA = ("Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/107.0.5304.110 Safari/537.36 Language/zh "
      "ColorScheme/Light wxwork/5.0.10 (MicroMessenger/6.2) WindowsWechat  "
      "MailPlugin_Electron WeMail embeddisk wwmver/3.26.510.632")
CONFIG = {
    "openid": os.environ.get("MONITOR_OPENID", "qw178736651287435066286538242373"),
    "roomdm": os.environ.get("MONITOR_ROOMDM", "060267"),
    "room": ROOM,
}
CONFIG2 = {
    "openid": CONFIG["openid"],
    "roomdm": os.environ.get("MONITOR_ROOMDM2", "060265"),
    "room": ROOM2,
}
ROOM_SHORT = os.environ.get("MONITOR_ROOM_SHORT", ROOM.rsplit("/", 1)[-1])
ROOM2_SHORT = os.environ.get("MONITOR_ROOM2_SHORT", ROOM2.rsplit("/", 1)[-1])
DATA_FILE = os.path.join(BASE_DIR, "monitor_data.csv")
CHART_PNG = os.path.join(BASE_DIR, "chart.png")
CHART_HTML = os.path.join(BASE_DIR, "chart.html")
MIN_INTERVAL_S = 600   # 距上次采集不足10分钟则跳过(防重复)
CSV_HEADER = ["time", "买1(度)", "补1(度)", "合1(度)", "买2(度)", "补2(度)", "合2(度)"]


def _get_session():
    s = requests.Session()
    s.headers["User-Agent"] = UA
    r = s.get(BASE + "/goQw", allow_redirects=True, timeout=15)
    r.raise_for_status()
    return s


def query_room(cfg):
    """用独立会话查询一个房间, 返回 (剩余购电, 剩余补助)"""
    s = _get_session()
    r = s.post(
        BASE + "/about/rebinding",
        data={"openid": cfg["openid"], "roomdm": cfg["roomdm"],
              "room": cfg["room"], "mode": "c"},
        headers={"X-Requested-With": "XMLHttpRequest",
                 "Referer": BASE + "/about/rebinding",
                 "Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    if r.status_code != 200 or cfg["room"] not in r.text:
        raise RuntimeError("绑定房间 %s 失败 HTTP %s: %s" % (
            cfg["room"], r.status_code, r.text[:100]))
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
        raise RuntimeError("页面里没找到剩余购电, 学校可能改版了 (%s)" % cfg["room"])
    return buy, (sub if sub is not None else 0.0)


def read_rows():
    """返回行列表: [dt, b1, s1, t1, b2, s2, t2], 缺失的房间2字段为 None"""
    rows = []
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, newline="", encoding="utf-8") as f:
            for line in csv_reader(f):
                if len(line) >= 2 and line[0] != "time":
                    try:
                        dt = datetime.datetime.fromisoformat(line[0].strip())
                    except Exception:
                        continue

                    def num(x):
                        try:
                            return float(x)
                        except Exception:
                            return None
                    b1 = num(line[1]) if len(line) > 1 else None
                    s1 = num(line[2]) if len(line) > 2 else None
                    t1 = num(line[3]) if len(line) > 3 else None
                    b2 = num(line[4]) if len(line) > 4 else None
                    s2 = num(line[5]) if len(line) > 5 else None
                    t2 = num(line[6]) if len(line) > 6 else None
                    if b1 is None and s1 is None and t1 is None:
                        continue
                    rows.append([dt, b1, s1, t1, b2, s2, t2])
    return rows


def csv_reader(f):
    import csv
    return csv.reader(f)


def _ensure_header():
    """旧文件只有4列(time,买1,补1,合1)时, 迁移成7列并补空房间2字段"""
    if not os.path.exists(DATA_FILE) or os.path.getsize(DATA_FILE) == 0:
        return
    with open(DATA_FILE, newline="", encoding="utf-8") as f:
        first = f.readline()
    if first.strip() and len(first.replace("\r", "").rstrip("\n").split(",")) >= 7:
        return  # 已是新格式
    rows = read_rows()
    import csv
    with open(DATA_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(CSV_HEADER)

        def s(x):
            return '' if x is None else x
        for r in rows:
            w.writerow([r[0].isoformat(timespec="seconds"), s(r[1]), s(r[2]),
                        s(r[3]), s(r[4]), s(r[5]), s(r[6])])


def append_row(now, b1, s1, t1, b2, s2, t2):
    rows = read_rows()
    if rows and (now - rows[-1][0]).total_seconds() < MIN_INTERVAL_S:
        return False
    _ensure_header()
    import csv
    first = not os.path.exists(DATA_FILE) or os.path.getsize(DATA_FILE) == 0
    with open(DATA_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if first:
            w.writerow(CSV_HEADER)

        def s(x):
            return '' if x is None else x
        w.writerow([now.isoformat(timespec="seconds"), s(b1), s(s1), s(t1),
                    s(b2), s(s2), s(t2)])
    return True


def make_charts(rows):
    if not rows:
        return
    # ---------- chart.png ----------
    if HAS_MPL:
        try:
            times1 = [r[0] for r in rows if r[3] is not None]
            v1s = [r[3] for r in rows if r[3] is not None]
            times2 = [r[0] for r in rows if r[6] is not None]
            v2s = [r[6] for r in rows if r[6] is not None]
            fig, ax = plt.subplots(figsize=(11, 5))
            ax.plot(times1, v1s, label="%s(度)" % ROOM_SHORT, lw=2.2,
                    marker="o", ms=3, color="#2563eb")
            if times2:
                ax.plot(times2, v2s, label="%s(度)" % ROOM2_SHORT, lw=2.2,
                        marker="o", ms=3, color="#10b981")
            ax.set_title("宿舍剩余电量监测 %s / %s" % (ROOM_SHORT, ROOM2_SHORT))
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
    data = []
    for r in rows:
        data.append([r[0].astimezone().isoformat(timespec="seconds"),
                     r[1], r[2], r[3], r[4], r[5], r[6]])
    html = TEMPLATE.replace("__DATA__", json.dumps(data, ensure_ascii=False)) \
                   .replace("__ROOM__", ROOM) \
                   .replace("__ROOM2__", ROOM2) \
                   .replace("__ROOM_SHORT__", ROOM_SHORT) \
                   .replace("__ROOM2_SHORT__", ROOM2_SHORT) \
                   .replace("__COUNT__", str(len(data)))
    with open(CHART_HTML, "w", encoding="utf-8") as f:
        f.write(html)


TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
    <meta name="theme-color" content="#f2f4f7">
    <title>宿舍剩余电量</title>
    <style>
        * {
            box-sizing: border-box;
            -webkit-tap-highlight-color: transparent;
        }
        html,
        body {
            margin: 0;
            padding: 0;
            background: #f2f4f7;
            color: #1f2937;
            font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif;
        }
        #app {
            display: flex;
            flex-direction: column;
            height: 100vh;
            height: 100dvh;
            max-width: 640px;
            margin: 0 auto;
            padding: 0 0 env(safe-area-inset-bottom) 0;
        }
        /* ===== 顶部摘要卡片 ===== */
        #summary {
            position: relative;
            overflow: hidden;
            background: #ffffff;
            margin: 12px 12px 10px;
            padding: 18px 20px 14px;
            border-radius: 20px;
            box-shadow: 0 2px 12px rgba(0, 0, 0, 0.05), 0 1px 4px rgba(0, 0, 0, 0.03);
        }
        #summary::before {
            content: '';
            position: absolute;
            left: 0;
            top: 0;
            right: 0;
            height: 3.5px;
            background: linear-gradient(90deg, #2563eb, #10b981, #93bbfc);
            border-radius: 20px 20px 0 0;
        }
        #summary .head {
            font-size: 14px;
            font-weight: 500;
            color: #6b7280;
            letter-spacing: 0.5px;
            margin-bottom: 10px;
        }
        /* 双房间数值块 */
        #rooms {
            display: flex;
            gap: 10px;
        }
        #rooms .room {
            flex: 1;
            min-width: 0;
            background: #f7f9fc;
            border-radius: 14px;
            padding: 10px 12px 12px;
        }
        #rooms .room .rlabel {
            font-size: 12px;
            font-weight: 600;
            color: #8b95a5;
            letter-spacing: 0.3px;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        #rooms .room .row {
            display: flex;
            align-items: baseline;
            gap: 4px;
            margin-top: 3px;
        }
        #rooms .room .val {
            font-size: clamp(28px, 8.5vw, 42px);
            font-weight: 700;
            line-height: 1.15;
            font-variant-numeric: tabular-nums;
            white-space: nowrap;
        }
        #rooms .room.v1 .val {
            color: #2563eb;
        }
        #rooms .room.v2 .val {
            color: #10b981;
        }
        #rooms .room .unit {
            font-size: 13px;
            color: #9ca3af;
        }
        #rooms .room .val.flash {
            animation: flash 0.9s ease;
        }
        @keyframes flash {
            0% {
                opacity: 0.55;
            }
            40% {
                opacity: 1;
            }
            100% {
                opacity: 1;
            }
        }
        /* 元信息行 */
        #meta {
            display: flex;
            flex-wrap: wrap;
            align-items: center;
            gap: 6px 14px;
            font-size: 13px;
            color: #9ca3af;
            margin-top: 12px;
        }
        #meta .count {
            font-weight: 600;
            color: #4b5563;
        }
        #meta .dot-divider {
            color: #d1d5db;
        }
        /* 状态指示器 */
        #status {
            display: flex;
            align-items: center;
            gap: 8px;
            margin-top: 10px;
            font-size: 13px;
            color: #8b95a5;
            border-top: 1px solid #f0f2f5;
            padding-top: 10px;
        }
        #status .dot {
            width: 10px;
            height: 10px;
            border-radius: 50%;
            background: #93c5fd;
            flex: none;
            transition: background 0.3s, transform 0.3s;
        }
        #status.loading .dot {
            background: transparent;
            border: 2.5px solid #dbe4f7;
            border-top-color: #2563eb;
            animation: rot 0.9s linear infinite;
        }
        #status.updated .dot {
            background: #22c55e;
            animation: pop 0.45s ease;
        }
        @keyframes rot {
            to {
                transform: rotate(360deg);
            }
        }
        @keyframes pop {
            0% {
                transform: scale(0.4);
            }
            60% {
                transform: scale(1.25);
            }
            100% {
                transform: scale(1);
            }
        }
        #status.updated .sttxt {
            color: #16a34a;
        }
        /* 范围切换按钮 */
        #ranges {
            display: flex;
            gap: 6px;
            margin-top: 12px;
            background: #f1f4f9;
            border-radius: 999px;
            padding: 4px;
        }
        #ranges button {
            flex: 1;
            border: none;
            background: transparent;
            border-radius: 999px;
            padding: 7px 0 6px;
            font-size: 13px;
            font-weight: 500;
            color: #6b7280;
            cursor: pointer;
            transition: all 0.25s ease;
            letter-spacing: 0.3px;
        }
        #ranges button.on {
            background: #ffffff;
            color: #1f2937;
            box-shadow: 0 2px 8px rgba(37, 99, 235, 0.13), 0 1px 3px rgba(0, 0, 0, 0.04);
            font-weight: 600;
        }
        #ranges button:active {
            transform: scale(0.95);
        }
        /* ===== 图表容器 ===== */
        #chart {
            flex: 1;
            min-height: 240px;
            display: flex;
            flex-direction: column;
            background: #ffffff;
            margin: 0 12px 10px;
            border-radius: 20px;
            box-shadow: 0 2px 12px rgba(0, 0, 0, 0.05), 0 1px 4px rgba(0, 0, 0, 0.03);
            overflow: hidden;
        }
        #legend {
            display: flex;
            gap: 18px;
            padding: 12px 16px 0;
            font-size: 12px;
            color: #8b95a5;
        }
        #legend .li {
            display: flex;
            align-items: center;
            gap: 6px;
        }
        #legend .dot {
            width: 9px;
            height: 9px;
            border-radius: 50%;
            flex: none;
        }
        #legend .dot.c1 {
            background: #2563eb;
        }
        #legend .dot.c2 {
            background: #10b981;
        }
        #cv {
            flex: 1;
            display: block;
            width: 100%;
        }
        /* 底部 */
        #foot {
            padding: 4px 16px 12px;
            font-size: 11px;
            color: #b6bec9;
            text-align: center;
            line-height: 1.7;
        }
        /* ===== 深色模式 ===== */
        @media (prefers-color-scheme: dark) {
            html,
            body {
                background: #111318;
                color: #e5e7eb;
            }
            #summary,
            #chart {
                background: #1c1f26;
                box-shadow: 0 2px 12px rgba(0, 0, 0, 0.35);
            }
            #summary .head {
                color: #9ca3af;
            }
            #rooms .room {
                background: #242832;
            }
            #rooms .room.v1 .val {
                color: #3b82f6;
            }
            #rooms .room.v2 .val {
                color: #34d399;
            }
            #meta {
                color: #6b7280;
            }
            #meta .count {
                color: #b0b8c5;
            }
            #status {
                border-top-color: #2a2f3a;
                color: #7a8496;
            }
            #status.updated .sttxt {
                color: #4ade80;
            }
            #status.updated .dot {
                background: #4ade80;
            }
            #ranges {
                background: #2a2f3a;
            }
            #ranges button {
                color: #9ca3af;
            }
            #ranges button.on {
                background: #374151;
                color: #f0f2f5;
                box-shadow: 0 2px 8px rgba(0, 0, 0, 0.3);
            }
            #legend .dot.c1 {
                background: #3b82f6;
            }
            #legend .dot.c2 {
                background: #34d399;
            }
            #foot {
                color: #4b5563;
            }
            @keyframes flash {
                0% {
                    opacity: 0.4;
                }
                40% {
                    opacity: 1;
                }
                100% {
                    opacity: 1;
                }
            }
        }
        /* ===== 小屏微调 ===== */
        @media (max-width: 420px) {
            #summary {
                padding: 14px 14px 12px;
                margin: 8px 10px 8px;
            }
            #rooms .room {
                padding: 8px 10px 10px;
            }
            #rooms .room .val {
                font-size: clamp(24px, 7vw, 34px);
            }
            #ranges button {
                font-size: 12px;
                padding: 6px 0;
            }
            #meta {
                font-size: 12px;
                gap: 4px 10px;
            }
            #chart {
                min-height: 190px;
                margin: 0 10px 8px;
                border-radius: 16px;
            }
            #foot {
                font-size: 10px;
            }
        }
        @media (max-width: 360px) {
            #rooms {
                gap: 6px;
            }
            #rooms .room {
                padding: 6px 8px 8px;
            }
            #rooms .room .val {
                font-size: clamp(20px, 6vw, 28px);
            }
            #chart {
                min-height: 160px;
            }
        }
    </style>
</head>
<body>
    <div id="app">
        <div id="summary">
            <div class="head">⚡ 当前剩余电量</div>
            <div id="rooms">
                <div class="room v1">
                    <div class="rlabel">__ROOM_SHORT__</div>
                    <div class="row">
                        <span class="val" id="val1">--</span><span class="unit">度</span>
                    </div>
                </div>
                <div class="room v2">
                    <div class="rlabel">__ROOM2_SHORT__</div>
                    <div class="row">
                        <span class="val" id="val2">--</span><span class="unit">度</span>
                    </div>
                </div>
            </div>
            <div id="meta">
                <span>共 <span class="count" id="recCount">0</span> 条记录</span>
                <span class="dot-divider">·</span>
                <span>更新于 <span id="updateTime">--</span></span>
            </div>
            <div id="status">
                <span class="dot"></span>
                <span class="sttxt" id="sttxt">自动更新中</span>
            </div>
            <div id="ranges">
                <button data-r="all" class="on">全部</button>
                <button data-r="7">近7天</button>
                <button data-r="30">近30天</button>
            </div>
        </div>

        <div id="chart">
            <div id="legend">
                <span class="li"><span class="dot c1"></span>__ROOM_SHORT__</span>
                <span class="li"><span class="dot c2"></span>__ROOM2_SHORT__</span>
            </div>
            <canvas id="cv"></canvas>
        </div>

        <div id="foot">
            数据来自南昌工学院智能收费系统（__ROOM__ · __ROOM2__）<br>
            约 10 分钟采集一次 · 自动更新，有变化时数字与曲线会动起来
        </div>
    </div>

    <script>
        // ============================================================
        //  数据: [time, b1, s1, 合1, b2, s2, 合2]  (合2可能为 null)
        // ============================================================
        var RAW = __DATA__;

        function pad(n) { return n < 10 ? '0' + n : '' + n; }

        function fmtDT(ts) {
            return pad(ts.getMonth() + 1) + '-' + pad(ts.getDate()) + ' ' +
                pad(ts.getHours()) + ':' + pad(ts.getMinutes());
        }

        function fmtShort(ts) {
            var now = new Date();
            var diff = (now - ts) / 1000;
            if (diff < 60) return '刚刚';
            if (diff < 3600) return Math.floor(diff / 60) + ' 分钟前';
            if (diff < 86400) return Math.floor(diff / 3600) + ' 小时前';
            return fmtDT(ts);
        }

        // ============================================================
        //  顶部卡片渲染 (双房间)
        // ============================================================
        var prev1 = null,
            prev2 = null,
            numAnim = null;
        var val1El = document.getElementById('val1');
        var val2El = document.getElementById('val2');
        var recCountEl = document.getElementById('recCount');
        var updateTimeEl = document.getElementById('updateTime');
        var stEl = document.getElementById('status');
        var stTxt = document.getElementById('sttxt');

        function rollValue(el, prev, now, done) {
            if (numAnim) cancelAnimationFrame(numAnim);
            if (prev !== null && isFinite(prev) && now !== null && isFinite(now) && prev !== now) {
                var from = prev,
                    to = now,
                    t0 = performance.now();

                function step(t) {
                    var k = Math.min(1, (t - t0) / 520);
                    var e = 1 - Math.pow(1 - k, 3);
                    el.textContent = (from + (to - from) * e).toFixed(2);
                    if (k < 1) {
                        numAnim = requestAnimationFrame(step);
                    } else {
                        el.textContent = to.toFixed(2);
                        if (done) done();
                    }
                }
                numAnim = requestAnimationFrame(step);
            } else {
                el.textContent = (now === null || !isFinite(now)) ? '--' : now.toFixed(2);
            }
        }

        function renderCard(roll) {
            var last = RAW.length ? RAW[RAW.length - 1] : null;
            var t1 = last ? Number(last[3]) : NaN;
            var t2 = (last && last[6] !== undefined && last[6] !== null) ? Number(last[6]) : NaN;
            if (isNaN(t1)) t1 = null;
            if (isNaN(t2)) t2 = null;
            if (roll) {
                var f1 = function() {
                    val1El.classList.remove('flash');
                    void val1El.offsetWidth;
                    val1El.classList.add('flash');
                };
                var f2 = function() {
                    val2El.classList.remove('flash');
                    void val2El.offsetWidth;
                    val2El.classList.add('flash');
                };
                rollValue(val1El, prev1, t1, f1);
                rollValue(val2El, prev2, t2, f2);
            } else {
                val1El.textContent = (t1 === null) ? '--' : t1.toFixed(2);
                val2El.textContent = (t2 === null) ? '--' : t2.toFixed(2);
            }
            prev1 = t1;
            prev2 = t2;
            var ts = last ? new Date(last[0]) : null;
            recCountEl.textContent = RAW.length;
            updateTimeEl.textContent = ts ? fmtShort(ts) : '--';
            updateTimeEl.title = ts ? fmtDT(ts) : '';
        }

        function setStatus(mode) {
            stEl.className = mode;
            if (mode === 'loading') stTxt.textContent = '正在更新…';
            else if (mode === 'updated') stTxt.textContent = '刚刚更新 ✓';
            else stTxt.textContent = '自动更新中';
        }

        // ============================================================
        //  Canvas 折线图 (双房间)
        // ============================================================
        var canvas = document.getElementById('cv');
        var ctx = canvas.getContext('2d');
        var RANGE = 'all';
        var animT = null;

        function palette() {
            if (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) {
                return { c1: '#3b82f6', c2: '#34d399', a1: 'rgba(59,130,246,', a2: 'rgba(52,211,153,',
                         text: '#d1d5db', grid: '#2a2f3a', label: '#6b7280' };
            }
            return { c1: '#2563eb', c2: '#10b981', a1: 'rgba(37,99,235,', a2: 'rgba(16,185,129,',
                     text: '#1f2937', grid: '#f0f2f5', label: '#9ca3af' };
        }

        function buildSeries() {
            var a = [],
                b = [];
            for (var i = 0; i < RAW.length; i++) {
                var t = new Date(RAW[i][0]);
                var v1 = Number(RAW[i][3]);
                if (!isNaN(v1)) a.push({ t: t, v: v1 });
                if (RAW[i][6] !== undefined && RAW[i][6] !== null) {
                    var v2 = Number(RAW[i][6]);
                    if (!isNaN(v2)) b.push({ t: t, v: v2 });
                }
            }
            return [a, b];
        }

        function filterRange(pts) {
            if (!pts.length || RANGE === 'all') return pts;
            var end = pts[pts.length - 1].t.getTime();
            var start = end - parseInt(RANGE, 10) * 86400000;
            return pts.filter(function(p) { return p.t.getTime() >= start; });
        }

        function traceLine(px, py, n) {
            ctx.moveTo(px[0], py[0]);
            if (n === 1) return;
            if (n === 2) { ctx.lineTo(px[1], py[1]); return; }
            for (var i = 0; i < n - 1; i++) {
                var p0x = px[Math.max(i - 1, 0)],
                    p0y = py[Math.max(i - 1, 0)];
                var p1x = px[i],
                    p1y = py[i];
                var p2x = px[i + 1],
                    p2y = py[i + 1];
                var p3x = px[Math.min(i + 2, n - 1)],
                    p3y = py[Math.min(i + 2, n - 1)];
                ctx.bezierCurveTo(
                    p1x + (p2x - p0x) / 6, p1y + (p2y - p0y) / 6,
                    p2x - (p3x - p1x) / 6, p2y - (p3y - p1y) / 6,
                    p2x, p2y
                );
            }
        }

        function draw(pg) {
            var dpr = window.devicePixelRatio || 1;
            var w = canvas.clientWidth,
                h = canvas.clientHeight;
            if (!w || !h) { canvas.width = canvas.height = 0; return; }
            canvas.width = Math.round(w * dpr);
            canvas.height = Math.round(h * dpr);
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            ctx.clearRect(0, 0, w, h);

            var raw = buildSeries();
            var ptsA = filterRange(raw[0]);
            var ptsB = filterRange(raw[1]);
            if (!ptsA.length && !ptsB.length) {
                ctx.fillStyle = '#9ca3af';
                ctx.font = '13px sans-serif';
                ctx.textAlign = 'center';
                ctx.textBaseline = 'middle';
                ctx.fillText('暂无数据', w / 2, h / 2);
                return;
            }
            var P = palette();
            var padL = 48,
                padR = 16,
                padT = 14,
                padB = 30;
            var pw = w - padL - padR,
                ph = h - padT - padB;

            var mn = Infinity,
                mx = -Infinity;
            var all = ptsA.concat(ptsB);
            for (var j = 0; j < all.length; j++) {
                if (all[j].v < mn) mn = all[j].v;
                if (all[j].v > mx) mx = all[j].v;
            }
            var lo = Math.floor((mn - 0.5) * 10) / 10;
            var hi = Math.ceil((mx + 0.5) * 10) / 10;
            if (hi - lo < 1) { hi = lo + 1; }

            var t0 = all[0].t.getTime(),
                t1 = all[all.length - 1].t.getTime();
            var span = (t1 - t0) || 1;

            function X(t) { return padL + (t.getTime() - t0) / span * pw; }

            function Y(v) { return padT + (hi - v) / (hi - lo) * ph; }

            function toXY(pts) {
                var px = [],
                    py = [];
                for (var k = 0; k < pts.length; k++) { px.push(X(pts[k].t)); py.push(Y(pts[k].v)); }
                return [px, py];
            }
            var XYa = toXY(ptsA),
                XYb = toXY(ptsB);

            // 网格 + Y轴
            ctx.font = '10px sans-serif';
            ctx.textBaseline = 'middle';
            var steps = 4;
            for (var s = 0; s <= steps; s++) {
                var vv = lo + (hi - lo) * s / steps;
                var yy = Y(vv);
                ctx.strokeStyle = P.grid;
                ctx.lineWidth = 1;
                ctx.beginPath();
                ctx.moveTo(padL, yy);
                ctx.lineTo(padL + pw, yy);
                ctx.stroke();
                ctx.fillStyle = P.label;
                ctx.textAlign = 'right';
                ctx.fillText(vv.toFixed(1), padL - 6, yy);
            }
            // X轴时间
            ctx.textAlign = 'center';
            ctx.textBaseline = 'top';
            ctx.fillStyle = P.label;
            ctx.fillText(fmtDT(all[0].t), padL, padT + ph + 8);
            ctx.fillText(fmtDT(all[all.length - 1].t), padL + pw, padT + ph + 8);
            if (span > 36 * 3600000) {
                var mid = new Date((t0 + t1) / 2);
                ctx.fillText(
                    pad(mid.getMonth() + 1) + '-' + pad(mid.getDate()) + ' ' + pad(mid.getHours()) + ':00',
                    padL + pw / 2, padT + ph + 8
                );
            }

            var pDone = (pg === undefined || pg === null) ? 1 : pg;
            if (pDone < 1) {
                ctx.save();
                ctx.beginPath();
                ctx.rect(padL, padT - 3, Math.max(1, pw * pDone) + 3, ph + 8);
                ctx.clip();
            }

            // 两条曲线: 面积 + 线 + 末点标签
            function paintSeries(px, py, lineColor, alphaPrefix, labelAbove) {
                if (!px.length) return;
                ctx.beginPath();
                ctx.moveTo(px[0], py[0]);
                traceLine(px, py, px.length);
                ctx.lineTo(px[px.length - 1], padT + ph);
                ctx.lineTo(px[0], padT + ph);
                ctx.closePath();
                var g = ctx.createLinearGradient(0, padT, 0, padT + ph);
                g.addColorStop(0, alphaPrefix + '0.20)');
                g.addColorStop(1, alphaPrefix + '0)');
                ctx.fillStyle = g;
                ctx.fill();

                ctx.beginPath();
                traceLine(px, py, px.length);
                ctx.strokeStyle = lineColor;
                ctx.lineWidth = 2.4;
                ctx.lineJoin = 'round';
                ctx.lineCap = 'round';
                ctx.stroke();

                if (pDone >= 0.999) {
                    var lx = px[px.length - 1],
                        ly = py[py.length - 1];
                    ctx.beginPath();
                    ctx.arc(lx, ly, 5, 0, Math.PI * 2);
                    ctx.fillStyle = '#ffffff';
                    ctx.fill();
                    ctx.lineWidth = 2.6;
                    ctx.strokeStyle = lineColor;
                    ctx.stroke();
                    ctx.font = 'bold 11px sans-serif';
                    ctx.textAlign = 'left';
                    ctx.fillStyle = lineColor;
                    var idx = (lineColor === P.c1) ? 3 : 6;
                    var lastRow = RAW[RAW.length - 1];
                    var lv = (lastRow && lastRow[idx] !== undefined && lastRow[idx] !== null) ? Number(lastRow[idx]) : null;
                    var label = (lv === null || isNaN(lv)) ? '' : lv.toFixed(1) + ' 度';
                    if (label) {
                        var tx = Math.min(lx + 10, padL + pw - 70);
                        if (labelAbove) {
                            ctx.textBaseline = 'bottom';
                            ctx.fillText(label, tx, ly - 7);
                        } else {
                            ctx.textBaseline = 'top';
                            ctx.fillText(label, tx, ly + 9);
                        }
                    }
                }
            }
            paintSeries(XYa[0], XYa[1], P.c1, P.a1, true);
            paintSeries(XYb[0], XYb[1], P.c2, P.a2, false);

            if (pDone < 1) ctx.restore();
        }

        // 动画
        function animate(pgFrom, ms) {
            if (animT) cancelAnimationFrame(animT);
            var t0 = performance.now(),
                len = Math.max(1, ms);

            function step(now) {
                var k = Math.min(1, (now - t0) / len);
                var e = 1 - Math.pow(1 - k, 3);
                draw(pgFrom + (1 - pgFrom) * e);
                if (k < 1) animT = requestAnimationFrame(step);
                else animT = null;
            }
            animT = requestAnimationFrame(step);
        }

        // ============================================================
        //  范围切换
        // ============================================================
        var btns = document.querySelectorAll('#ranges button');
        for (var b = 0; b < btns.length; b++) {
            btns[b].addEventListener('click', function() {
                RANGE = this.getAttribute('data-r');
                for (var q = 0; q < btns.length; q++) btns[q].className = '';
                this.className = 'on';
                animate(0, 400);
            });
        }

        // ============================================================
        //  数据刷新
        // ============================================================
        var useFetch = location.protocol !== 'file:';

        function refresh() {
            if (!useFetch) { location.reload(); return; }
            setStatus('loading');
            fetch('monitor_data.csv', { cache: 'no-store' })
                .then(function(r) {
                    if (!r.ok) throw new Error('http ' + r.status);
                    return r.text();
                })
                .then(function(txt) {
                    var rows = [];
                    var lines = txt.split(/\r?\n/);
                    for (var i = 0; i < lines.length; i++) {
                        if (!lines[i].trim()) continue;
                        var p = lines[i].split(',');
                        if (p.length >= 4 && p[0] !== 'time') {
                            function num(x) {
                                if (x === undefined || x === null) return null;
                                var s = String(x).trim();
                                if (s === '') return null;
                                var n = Number(s);
                                return isNaN(n) ? null : n;
                            }
                            var row = [p[0], num(p[1]), num(p[2]), num(p[3])];
                            if (p.length > 4) row.push(num(p[4]));
                            if (p.length > 5) row.push(num(p[5]));
                            if (p.length > 6) row.push(num(p[6]));
                            if (row[3] === null && (row[6] === undefined || row[6] === null)) continue;
                            rows.push(row);
                        }
                    }
                    if (rows.length && rows.length !== RAW.length) {
                        RAW = rows;
                        renderCard(true);
                        setStatus('updated');
                        animate(0, 680);
                        setTimeout(function() { setStatus('idle'); }, 3200);
                    } else if (rows.length) {
                        setStatus('idle');
                    } else {
                        setStatus('idle');
                    }
                })
                .catch(function() { location.reload(); });
        }
        if (useFetch) {
            setInterval(refresh, 5 * 60 * 1000);
            setTimeout(refresh, 2500);
        } else {
            setTimeout(function() { location.reload(); }, 5 * 60 * 1000);
        }

        // ============================================================
        //  启动
        // ============================================================
        renderCard();
        setStatus('idle');

        function redraw() {
            if (animT) { cancelAnimationFrame(animT);
                animT = null; }
            draw(1);
        }
        var resizeTimer;
        window.addEventListener('resize', function() {
            clearTimeout(resizeTimer);
            resizeTimer = setTimeout(redraw, 120);
        });
        window.addEventListener('orientationchange', function() { setTimeout(redraw, 350); });
        var darkMatch = window.matchMedia('(prefers-color-scheme: dark)');
        if (darkMatch.addEventListener) darkMatch.addEventListener('change', redraw);
        animate(0, 720);
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
        q1 = q2 = None
        try:
            q1 = query_room(CONFIG)
        except Exception as e:
            print("%s 房间1(%s)查询失败: %s" % (now.strftime("%H:%M:%S"), ROOM_SHORT, e))
        try:
            q2 = query_room(CONFIG2)
        except Exception as e:
            print("%s 房间2(%s)查询失败: %s" % (now.strftime("%H:%M:%S"), ROOM2_SHORT, e))
        if q1 is None and q2 is None:
            print("%s 两个房间都查询失败" % now.strftime("%H:%M:%S"))
            if not args.loop:
                sys.exit(1)
        else:
            b1, s1 = q1 if q1 else (None, None)
            b2, s2 = q2 if q2 else (None, None)
            t1 = round(b1 + s1, 2) if (b1 is not None and s1 is not None) else None
            t2 = round(b2 + s2, 2) if (b2 is not None and s2 is not None) else None
            added = append_row(now, b1, s1, t1, b2, s2, t2)
            make_charts(read_rows())
            if added:
                print("%s %s=%.2f %s=%.2f (已记录)" % (
                    now.strftime("%m-%d %H:%M"),
                    ROOM_SHORT, t1 if t1 is not None else 0.0,
                    ROOM2_SHORT, t2 if t2 is not None else 0.0))
            else:
                print("%s 距上次不足%d秒, 跳过记录" % (now.strftime("%H:%M:%S"), MIN_INTERVAL_S))
        if not args.loop:
            break
        time.sleep(args.loop)


if __name__ == "__main__":
    main()