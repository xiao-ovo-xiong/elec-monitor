#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
monitor_electricity.py - 宿舍剩余电量监测 (多房间版)
====================================================
- rooms.json 维护要监控的房间列表(可多个)
- 每次采集: 对列表里每个房间独立会话查询, 追加到 data/<短名>.csv
- 生成 chart.html (手机端单页应用: 宿舍列表 -> 点击进入查看该宿舍图表)
- 添加宿舍必须通过 --add-room 且经过真实绑定验证, 防止随意添加

用法:
    python monitor_electricity.py                     # 采集全部房间并更新页面
    python monitor_electricity.py --loop 600          # 每600秒循环
    python monitor_electricity.py --chart             # 只重新生成页面
    python monitor_electricity.py --add-room 4-268    # 添加宿舍(自动识别房间代码并验证)

环境变量(可选):
    MONITOR_BASE / MONITOR_ADD_PASS / MONITOR_BUILDING_CODE /
    MONITOR_OPENID / MONITOR_ROOMDM / MONITOR_ROOM (首次生 rooms.json 时用)
"""
import argparse
import datetime
import json
import os
import re
import sys
import time
import csv

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

BASE = os.environ.get("MONITOR_BASE", "http://sf.ncpu.edu.cn:9090")
UA = ("Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/107.0.5304.110 Safari/537.36 Language/zh "
      "ColorScheme/Light wxwork/5.0.10 (MicroMessenger/6.2) WindowsWechat  "
      "MailPlugin_Electron WeMail embeddisk wwmver/3.26.510.632")

ROOMS_FILE = os.path.join(BASE_DIR, "rooms.json")
DATA_DIR = os.path.join(BASE_DIR, "data")
LEGACY_FILE = os.path.join(BASE_DIR, "monitor_data.csv")
CHART_HTML = os.path.join(BASE_DIR, "chart.html")
INDEX_HTML = os.path.join(BASE_DIR, "index.html")
MIN_INTERVAL_S = 600   # 每个房间距上次采集不足10分钟则跳过
ADD_PASS = os.environ.get("MONITOR_ADD_PASS", "dorm-monitor")
BUILDING_CODE = os.environ.get("MONITOR_BUILDING_CODE", "06")
CSV_HEADER = ["time", "buy(度)", "sub(度)", "total(度)"]
COLOR_WHEEL = [
    ("#2563eb", "#3b82f6"), ("#10b981", "#34d399"),
    ("#f59e0b", "#fbbf24"), ("#8b5cf6", "#a78bfa"),
    ("#ec4899", "#f472b6"), ("#14b8a6", "#2dd4bf"),
]


# ================= 房间注册表 =================
def load_rooms():
    if os.path.exists(ROOMS_FILE):
        with open(ROOMS_FILE, encoding="utf-8") as f:
            return json.load(f)
    # 首次运行: 从环境变量/默认值引导两个房间
    openid = os.environ.get("MONITOR_OPENID", "qw178736651287435066286538242373")
    r1 = os.environ.get("MONITOR_ROOM", "4栋/2层/4-267")
    r2 = os.environ.get("MONITOR_ROOM2", "4栋/2层/4-265")

    def mk(room, dm, i):
        short = room.rsplit("/", 1)[-1]
        return {"short": short, "name": room, "roomdm": dm,
                "openid": openid, "color": COLOR_WHEEL[i][0],
                "dark": COLOR_WHEEL[i][1]}
    rooms = [mk(r1, os.environ.get("MONITOR_ROOMDM", "060267"), 0),
             mk(r2, os.environ.get("MONITOR_ROOMDM2", "060265"), 1)]
    save_rooms(rooms, "4栋", BUILDING_CODE)
    return load_rooms()


def save_rooms(rooms, building="4栋", building_code=BUILDING_CODE):
    with open(ROOMS_FILE, "w", encoding="utf-8") as f:
        json.dump({"version": 2, "building": building,
                   "buildingCode": building_code, "rooms": rooms},
                  f, ensure_ascii=False, indent=2)


def room_csv(short):
    return os.path.join(DATA_DIR, short + ".csv")


# ================= 学校接口 =================
def _get_session():
    s = requests.Session()
    s.headers["User-Agent"] = UA
    r = s.get(BASE + "/goQw", allow_redirects=True, timeout=15)
    r.raise_for_status()
    return s


def query_room(room):
    """room: {openid, roomdm, room|name}; 返回 (剩余购电, 剩余补助)"""
    room_name = room.get("name") or room.get("room")
    s = _get_session()
    r = s.post(
        BASE + "/about/rebinding",
        data={"openid": room["openid"], "roomdm": room["roomdm"],
              "room": room_name, "mode": "c"},
        headers={"X-Requested-With": "XMLHttpRequest",
                 "Referer": BASE + "/about/rebinding",
                 "Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    if r.status_code != 200 or room_name not in r.text:
        raise RuntimeError("绑定房间 %s 失败 HTTP %s: %s" % (
            room_name, r.status_code, r.text[:100]))
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
        raise RuntimeError("页面里没找到剩余购电, 学校可能改版了 (%s)" % room_name)
    return buy, (sub if sub is not None else 0.0)


def _quoted_list(pattern, txt):
    m = re.search(pattern, txt)
    if not m:
        return []
    return re.findall(r'"([^"]*)"', m.group(1))


def find_room_dm(room_short, building_code=BUILDING_CODE):
    """按房间名(如 4-268)搜楼, 返回 (roomdm, 楼层名) 或抛错"""
    s = _get_session()
    r = s.get(BASE + "/about/floors/" + building_code, timeout=10)
    r.raise_for_status()
    txt = r.text
    floor_dms = _quoted_list(r'floordm:\[(.*?)\]', txt)
    floor_names = _quoted_list(r'floorname:\[(.*?)\]', txt)
    for i, dm in enumerate(floor_dms):
        if not dm:
            continue
        try:
            rr = s.get(BASE + "/about/rooms/" + dm, timeout=10)
            rooms_txt = rr.text
        except Exception:
            continue
        names = _quoted_list(r'roomname:\[(.*?)\]', rooms_txt)
        dms = _quoted_list(r'roomdm:\[(.*?)\]', rooms_txt)
        for j, n in enumerate(names):
            if n == room_short and j < len(dms):
                floor_name = floor_names[i] if i < len(floor_names) else "1层"
                return dms[j], floor_name
    raise RuntimeError("在楼栋里找不到房间 %s (检查房号格式, 如 4-268)" % room_short)


# ================= 数据存储 =================
def _read_local_rows(path):
    rows = []
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as f:
            for line in csv.reader(f):
                if len(line) >= 4 and line[0] != "time":
                    try:
                        dt = datetime.datetime.fromisoformat(line[0].strip())
                    except Exception:
                        continue
                    try:
                        b = float(line[1])
                        s = float(line[2])
                        t = float(line[3])
                    except ValueError:
                        continue
                    rows.append([dt, b, s, t])
    return rows


def _num(x):
    try:
        return float(x)
    except Exception:
        return None


def _append_csv(path, dt, b, s, t):
    first = not os.path.exists(path) or os.path.getsize(path) == 0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if first:
            w.writerow(CSV_HEADER)
        w.writerow([dt.isoformat(timespec="seconds"), b, s, t])


def migrate_legacy(rooms):
    """旧的单文件7列CSV拆分成每房间一个文件"""
    if not os.path.exists(LEGACY_FILE):
        return
    with open(LEGACY_FILE, newline="", encoding="utf-8") as f:
        for line in csv.reader(f):
            if len(line) < 4 or line[0] == "time":
                continue
            try:
                dt = datetime.datetime.fromisoformat(line[0].strip())
            except Exception:
                continue
            if len(rooms) > 0 and len(line) > 3 and _num(line[3]) is not None:
                _append_csv(room_csv(rooms[0]["short"]), dt,
                            _num(line[1]), _num(line[2]), _num(line[3]))
            if len(rooms) > 1 and len(line) > 6 and _num(line[6]) is not None:
                _append_csv(room_csv(rooms[1]["short"]), dt,
                            _num(line[4]), _num(line[5]), _num(line[6]))
    os.rename(LEGACY_FILE, LEGACY_FILE + ".legacy")
    print("已把旧版 monitor_data.csv 拆分为各房间独立文件 (原文件改名 .legacy)")


# ================= 渲染 =================
def make_charts(rooms):
    payload = {}
    for rm in rooms:
        rows = _read_local_rows(room_csv(rm["short"]))
        payload[rm["short"]] = {
            "name": rm["name"], "short": rm["short"],
            "color": rm["color"], "dark": rm["dark"],
            "data": [[r[0].astimezone().isoformat(timespec="seconds"),
                      r[1], r[2], r[3]] for r in rows],
        }
    html = TEMPLATE.replace("__ROOMS__", json.dumps(payload, ensure_ascii=False)) \
                   .replace("__ADD_PASS__", ADD_PASS) \
                   .replace("__COUNT__", str(len(rooms)))
    for path in (CHART_HTML, INDEX_HTML):
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
    print("已生成 chart.html (房间数=%d)" % len(rooms))
    # 每房间一张 PNG(可选)
    if HAS_MPL:
        try:
            for rm in rooms:
                rows = _read_local_rows(room_csv(rm["short"]))
                if len(rows) < 2:
                    continue
                fig, ax = plt.subplots(figsize=(11, 5))
                ax.plot([r[0] for r in rows], [r[3] for r in rows],
                        label="%s(度)" % rm["short"], lw=2.2, marker="o",
                        ms=3, color=rm["color"])
                ax.set_title("宿舍剩余电量 %s" % rm["name"])
                ax.set_ylabel("度")
                ax.grid(True, alpha=0.3)
                ax.legend()
                fig.autofmt_xdate()
                fig.tight_layout()
                fig.savefig(os.path.join(BASE_DIR, "chart-%s.png" % rm["short"]), dpi=110)
                plt.close(fig)
        except Exception as e:
            print("生成 PNG 失败(不影响网页):", e)


# ================= 渲染模板 =================
TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
    <meta name="theme-color" content="#f2f4f7">
    <title>宿舍电量</title>
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
            max-width: 640px;
            margin: 0 auto;
            padding: 0 0 env(safe-area-inset-bottom) 0;
        }
        /* ============ 首页 ============ */
        #home {
            padding: 18px 14px 90px;
        }
        #home .hdr {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 4px 6px 14px;
        }
        #home .hdr .t {
            font-size: 21px;
            font-weight: 700;
            letter-spacing: 0.3px;
        }
        #home .hdr .t span {
            color: #2563eb;
        }
        #home .hdr .sub {
            font-size: 12px;
            color: #9ca3af;
            margin-top: 3px;
            font-weight: 400;
        }
        #addTop {
            border: none;
            background: #ffffff;
            color: #2563eb;
            font-size: 14px;
            font-weight: 600;
            border-radius: 999px;
            padding: 9px 16px;
            box-shadow: 0 2px 8px rgba(37, 99, 235, 0.14);
            cursor: pointer;
        }
        #grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 12px;
        }
        .card {
            position: relative;
            background: #ffffff;
            border-radius: 18px;
            padding: 14px 14px 12px;
            box-shadow: 0 2px 10px rgba(0, 0, 0, 0.05);
            cursor: pointer;
            overflow: hidden;
            transition: transform 0.15s ease;
        }
        .card::before {
            content: '';
            position: absolute;
            left: 0;
            top: 0;
            bottom: 0;
            width: 4px;
            background: var(--ac);
        }
        .card:active {
            transform: scale(0.97);
        }
        .card .cname {
            font-size: 14px;
            font-weight: 600;
            color: #4b5563;
        }
        .card .crow {
            display: flex;
            align-items: baseline;
            gap: 4px;
            margin-top: 6px;
        }
        .card .cval {
            font-size: clamp(26px, 8vw, 34px);
            font-weight: 700;
            font-variant-numeric: tabular-nums;
            color: var(--ac);
        }
        .card .cunit {
            font-size: 12px;
            color: #9ca3af;
        }
        .card .cupd {
            position: absolute;
            right: 12px;
            bottom: 12px;
            font-size: 11px;
            color: #b0b8c5;
        }
        .card .carrow {
            position: absolute;
            right: 12px;
            top: 10px;
            color: #d1d5db;
            font-size: 16px;
        }
        .card.nodata .cval {
            color: #d1d5db;
            font-size: 22px;
        }
        /* 添加悬浮按钮 */
        #fab {
            position: fixed;
            right: 18px;
            bottom: calc(22px + env(safe-area-inset-bottom));
            width: 56px;
            height: 56px;
            border-radius: 50%;
            border: none;
            background: #2563eb;
            color: #fff;
            font-size: 26px;
            font-weight: 600;
            box-shadow: 0 6px 18px rgba(37, 99, 235, 0.4);
            cursor: pointer;
            z-index: 50;
        }
        #fab:active {
            transform: scale(0.92);
        }
        /* ============ 详情页 ============ */
        #detail {
            display: none;
            flex-direction: column;
            min-height: 100vh;
            min-height: 100dvh;
        }
        #dTop {
            display: flex;
            align-items: center;
            gap: 10px;
            padding: 14px 14px 6px;
        }
        #back {
            border: none;
            background: #ffffff;
            width: 36px;
            height: 36px;
            border-radius: 50%;
            font-size: 20px;
            color: #4b5563;
            box-shadow: 0 2px 8px rgba(0, 0, 0, 0.08);
            cursor: pointer;
            flex: none;
        }
        #back:active {
            transform: scale(0.92);
        }
        #dTop .t {
            min-width: 0;
        }
        #dRoomName {
            font-size: 16px;
            font-weight: 700;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        #dMeta {
            font-size: 12px;
            color: #9ca3af;
            margin-top: 1px;
        }
        #dCard {
            position: relative;
            overflow: hidden;
            background: #ffffff;
            margin: 8px 12px 10px;
            padding: 16px 18px 12px;
            border-radius: 20px;
            box-shadow: 0 2px 12px rgba(0, 0, 0, 0.05);
        }
        #dCard::before {
            content: '';
            position: absolute;
            left: 0;
            top: 0;
            right: 0;
            height: 3.5px;
            background: var(--ac, #2563eb);
        }
        #dCard .dlabel {
            font-size: 13px;
            font-weight: 500;
            color: #6b7280;
        }
        #dRow {
            display: flex;
            align-items: baseline;
            gap: 6px;
            margin-top: 4px;
        }
        #dVal {
            font-size: clamp(40px, 13vw, 60px);
            font-weight: 700;
            line-height: 1.15;
            font-variant-numeric: tabular-nums;
            color: var(--ac, #2563eb);
        }
        #dRow .unit {
            font-size: 14px;
            color: #9ca3af;
        }
        #dVal.flash {
            animation: flash 0.9s ease;
        }
        /* 统计小标签 */
        #dStats {
            display: flex;
            flex-wrap: wrap;
            gap: 6px;
            margin-top: 12px;
        }
        #dStats .chip {
            background: #f7f9fc;
            border-radius: 9px;
            padding: 5px 9px;
            font-size: 11px;
            color: #6b7280;
            line-height: 1.35;
        }
        #dStats .chip b {
            font-size: 12.5px;
            color: #1f2937;
            font-variant-numeric: tabular-nums;
            margin-left: 3px;
        }
        #dStats .chip.recharge b {
            color: #f59e0b;
        }
        @keyframes flash {
            0% {
                opacity: 0.5;
            }
            40% {
                opacity: 1;
            }
        }
        #dStatus {
            display: flex;
            align-items: center;
            gap: 8px;
            margin-top: 10px;
            padding-top: 10px;
            border-top: 1px solid #f0f2f5;
            font-size: 13px;
            color: #8b95a5;
        }
        #dStatus .dot {
            width: 10px;
            height: 10px;
            border-radius: 50%;
            background: #93c5fd;
            flex: none;
            transition: background 0.3s;
        }
        #dStatus.loading .dot {
            background: transparent;
            border: 2.5px solid #dbe4f7;
            border-top-color: #2563eb;
            animation: rot 0.9s linear infinite;
        }
        #dStatus.updated .dot {
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
        #dStatus.updated .sttxt {
            color: #16a34a;
        }
        #dRanges {
            display: flex;
            gap: 6px;
            margin-top: 12px;
            background: #f1f4f9;
            border-radius: 999px;
            padding: 4px;
        }
        #dRanges button {
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
        }
        #dRanges button.on {
            background: #ffffff;
            color: #1f2937;
            box-shadow: 0 2px 8px rgba(37, 99, 235, 0.13);
            font-weight: 600;
        }
        #dChart {
            flex: 1;
            min-height: 220px;
            display: flex;
            flex-direction: column;
            background: #ffffff;
            margin: 0 12px 10px;
            border-radius: 20px;
            box-shadow: 0 2px 12px rgba(0, 0, 0, 0.05);
            overflow: hidden;
        }
        #dChart hdr {
            padding: 12px 16px 0;
            font-size: 12px;
            color: #8b95a5;
            display: flex;
            gap: 6px;
            align-items: center;
        }
        #dChart hdr i {
            width: 9px;
            height: 9px;
            border-radius: 50%;
            background: var(--ac);
            display: inline-block;
        }
        #cv {
            flex: 1;
            display: block;
            width: 100%;
        }
        #foot {
            padding: 2px 16px 14px;
            font-size: 11px;
            color: #b6bec9;
            text-align: center;
            line-height: 1.7;
        }
        /* ============ 添加弹窗 ============ */
        #overlay {
            display: none;
            position: fixed;
            inset: 0;
            background: rgba(17, 19, 24, 0.45);
            z-index: 100;
            padding: 20px 14px;
            overflow-y: auto;
        }
        #overlay .modal {
            background: #ffffff;
            border-radius: 20px;
            padding: 18px;
            max-width: 420px;
            margin: 8vh auto 0;
            box-shadow: 0 10px 40px rgba(0, 0, 0, 0.25);
        }
        .modal .mh {
            display: flex;
            align-items: center;
            justify-content: space-between;
            font-size: 17px;
            font-weight: 700;
        }
        .modal .mh button {
            border: none;
            background: #f1f4f9;
            width: 30px;
            height: 30px;
            border-radius: 50%;
            font-size: 15px;
            color: #6b7280;
            cursor: pointer;
        }
        .modal .desc {
            font-size: 13px;
            color: #6b7280;
            line-height: 1.7;
            margin: 10px 0 14px;
        }
        .modal label {
            display: block;
            font-size: 12px;
            color: #8b95a5;
            margin: 10px 0 4px;
        }
        .modal input {
            width: 100%;
            border: 1px solid #e5e7eb;
            border-radius: 12px;
            padding: 10px 12px;
            font-size: 15px;
            background: #fafbfc;
            outline: none;
        }
        .modal input:focus {
            border-color: #2563eb;
        }
        .modal .err {
            color: #dc2626;
            font-size: 12px;
            margin-top: 6px;
            display: none;
        }
        .modal .okg {
            display: none;
            background: #f0fdf4;
            border: 1px solid #bbf7d0;
            border-radius: 12px;
            padding: 10px 12px;
            font-size: 13px;
            color: #15803d;
            line-height: 1.7;
            margin-top: 10px;
        }
        .modal .okg a {
            color: #15803d;
            font-weight: 600;
        }
        .modal .steps {
            display: none;
            margin-top: 12px;
            font-size: 13px;
            color: #4b5563;
            line-height: 1.9;
        }
        .modal .steps b {
            color: #1f2937;
        }
        .modal .chips {
            display: flex;
            flex-wrap: wrap;
            gap: 6px;
            margin-top: 12px;
        }
        .modal .chips span {
            background: #f1f4f9;
            border-radius: 999px;
            padding: 4px 10px;
            font-size: 12px;
            color: #4b5563;
        }
        #goBtn {
            width: 100%;
            border: none;
            background: #2563eb;
            color: #fff;
            border-radius: 999px;
            padding: 12px 0;
            font-size: 15px;
            font-weight: 600;
            margin-top: 14px;
            cursor: pointer;
        }
        #goBtn:active {
            transform: scale(0.98);
        }
        .safe {
            font-size: 11px;
            color: #9ca3af;
            margin-top: 10px;
            line-height: 1.6;
        }
        /* ============ 深色模式 ============ */
        @media (prefers-color-scheme: dark) {
            html,
            body {
                background: #111318;
                color: #e5e7eb;
            }
            #home .hdr .t span {
                color: #3b82f6;
            }
            #addTop,
            .card,
            #back,
            #dCard,
            #dChart,
            .modal {
                background: #1c1f26;
                box-shadow: 0 2px 12px rgba(0, 0, 0, 0.35);
            }
            #addTop {
                color: #3b82f6;
            }
            .card .cname {
                color: #d1d5db;
            }
            .card .cupd {
                color: #6b7280;
            }
            .card .carrow {
                color: #374151;
            }
            #fab {
                background: #3b82f6;
                box-shadow: 0 6px 18px rgba(59, 130, 246, 0.4);
            }
            #back {
                color: #b0b8c5;
            }
            #dCard .dlabel {
                color: #9ca3af;
            }
            #dStatus {
                border-top-color: #2a2f3a;
            }
            #dStatus.updated .sttxt {
                color: #4ade80;
            }
            #dStatus.updated .dot {
                background: #4ade80;
            }
            #dRanges {
                background: #2a2f3a;
            }
            #dRanges button {
                color: #9ca3af;
            }
            #dRanges button.on {
                background: #374151;
                color: #f0f2f5;
                box-shadow: 0 2px 8px rgba(0, 0, 0, 0.3);
            }
            .modal .mh button {
                background: #2a2f3a;
                color: #9ca3af;
            }
            .modal input {
                border-color: #2a2f3a;
                background: #242832;
                color: #e5e7eb;
            }
            .modal .chips span {
                background: #2a2f3a;
                color: #b0b8c5;
            }
            #dStats .chip {
                background: #242832;
                color: #9ca3af;
            }
            #dStats .chip b {
                color: #e5e7eb;
            }
            #foot {
                color: #4b5563;
            }
            .modal .steps b {
                color: #e5e7eb;
            }
        }
        /* ============ 小屏 ============ */
        @media (max-width: 380px) {
            #grid {
                gap: 10px;
            }
            .card {
                padding: 12px 12px 10px;
            }
            #dCard {
                padding: 14px 16px 10px;
            }
        }
    </style>
</head>
<body>
    <div id="app">
        <!-- 首页: 宿舍列表 -->
        <div id="home">
            <div class="hdr">
                <div class="t">宿舍电量<span>监测</span>
                    <div class="sub">每 10 分钟自动更新</div>
                </div>
                <button id="addTop">＋ 添加宿舍</button>
            </div>
            <div id="grid"></div>
        </div>

        <!-- 详情: 单个宿舍 -->
        <div id="detail">
            <div id="dTop">
                <button id="back">‹</button>
                <div class="t">
                    <div id="dRoomName">--</div>
                    <div id="dMeta">--</div>
                </div>
            </div>
            <div id="dCard" style="--ac:#2563eb">
                <div class="dlabel">⚡ 当前剩余电量</div>
                <div id="dRow">
                    <span id="dVal">--</span><span class="unit">度</span>
                </div>
                <div id="dStats"></div>
                <div id="dStatus">
                    <span class="dot"></span>
                    <span class="sttxt" id="dStTxt">自动更新中</span>
                </div>
                <div id="dRanges">
                    <button data-r="all" class="on">全部</button>
                    <button data-r="7">近7天</button>
                    <button data-r="30">近30天</button>
                </div>
            </div>
            <div id="dChart">
                <hdr><i></i><span id="dChartName">--</span> 剩余电量走势</hdr>
                <canvas id="cv"></canvas>
            </div>
            <div id="foot">剩余电量 = 剩余购电 + 剩余补助<br>数据来自南昌工学院智能收费系统 · 约 10 分钟采集一次</div>
        </div>

        <button id="fab">＋</button>

        <!-- 添加宿舍弹窗 -->
        <div id="overlay">
            <div class="modal">
                <div class="mh">＋ 添加宿舍
                    <button id="closeModal">✕</button>
                </div>
                <div class="desc">
                    添加新宿舍需要管理员权限（也就是你本人）。<br>
                    填好下面两项后，页面会给你"云端添加"的引导步骤。<br>
                    <span style="color:#9ca3af;font-size:12px">删除宿舍：在 Actions 的 removeRoom 输入框填房号即可（仅管理员）。</span>
                </div>
                <label>宿舍房间号</label>
                <input id="inRoom" placeholder="例如 4-268">
                <label>管理口令</label>
                <input id="inPass" type="password" placeholder="管理员口令（防页面访客随意添加）">
                <div class="err" id="addErr">口令错误，或房间号格式不对</div>
                <div class="okg" id="addOk">口令正确 ✓ 房间号格式没问题，请按下面步骤完成添加：</div>
                <div class="steps" id="addSteps"></div>
                <button id="goBtn">验证并获取添加步骤</button>
                <div class="chips" id="curChips"></div>
                <div class="safe">说明：页面口令只是第一道防线（防止公开页面的访客乱点添加）；真正的添加必须通过 GitHub 管理员权限执行，并在云端真实验证房间存在后才生效。</div>
            </div>
        </div>
    </div>

    <script>
        // ============================================================
        //  数据: ROOMS = { "4-267": {name, short, color, dark, data:[[t,b,s,tot],...]}, ... }
        // ============================================================
        var ROOMS = __ROOMS__;
        var ADD_PASS = '__ADD_PASS__';
        var ORDER = Object.keys(ROOMS);
        var cur = null;
        var RANGE = 'all';
        var prevVal = null,
            numAnim = null,
            animT = null;
        var useFetch = location.protocol !== 'file:';

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

        function isDark() {
            return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
        }

        function roomColor(short) {
            var r = ROOMS[short];
            return isDark() ? (r.dark || r.color) : r.color;
        }

        function hexRgb(hex) {
            var h = hex.replace('#', '');
            if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
            var n = parseInt(h, 16);
            return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
        }

        function lastTotal(short) {
            var d = ROOMS[short] && ROOMS[short].data;
            if (!d || !d.length) return null;
            var t = Number(d[d.length - 1][3]);
            return isNaN(t) ? null : t;
        }

        function lastTs(short) {
            var d = ROOMS[short] && ROOMS[short].data;
            return d && d.length ? new Date(d[d.length - 1][0]) : null;
        }

        // ============ 首页渲染 ============
        var gridEl = document.getElementById('grid');
        var fabEl = document.getElementById('fab');
        var homeEl = document.getElementById('home');
        var detailEl = document.getElementById('detail');
        var overlayEl = document.getElementById('overlay');

        function renderHome() {
            gridEl.innerHTML = '';
            ORDER.forEach(function(s) {
                var v = lastTotal(s);
                var ts = lastTs(s);
                var card = document.createElement('div');
                card.className = 'card' + (v === null ? ' nodata' : '');
                card.style.setProperty('--ac', roomColor(s));
                card.setAttribute('data-s', s);
                card.innerHTML =
                    '<div class="cname">' + (ROOMS[s].short || s) + '</div>' +
                    '<div class="crow"><span class="cval">' + (v !== null ? v.toFixed(2) : '--') + '</span>' +
                    '<span class="cunit">度</span></div>' +
                    '<div class="cupd">' + (ts ? fmtShort(ts) : '暂无数据') + '</div>' +
                    '<div class="carrow">›</div>';
                card.addEventListener('click', function() { openDetail(s); });
                gridEl.appendChild(card);
            });
        }

        // ============ 详情 ============
        var cv = document.getElementById('cv');
        var ctx = cv.getContext('2d');

        function setStatus(mode) {
            var el = document.getElementById('dStatus');
            var tx = document.getElementById('dStTxt');
            el.className = mode;
            if (mode === 'loading') tx.textContent = '正在更新…';
            else if (mode === 'updated') tx.textContent = '刚刚更新 ✓';
            else tx.textContent = '自动更新中';
        }

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
                    if (k < 1) numAnim = requestAnimationFrame(step);
                    else { el.textContent = to.toFixed(2); if (done) done(); }
                }
                numAnim = requestAnimationFrame(step);
            } else {
                el.textContent = (now === null || !isFinite(now)) ? '--' : now.toFixed(2);
            }
        }

        function renderCard(roll) {
            var v = cur ? lastTotal(cur) : null;
            var el = document.getElementById('dVal');
            if (roll) {
                var f = function() {
                    el.classList.remove('flash');
                    void el.offsetWidth;
                    el.classList.add('flash');
                };
                rollValue(el, prevVal, v, f);
            } else {
                el.textContent = (v === null) ? '--' : v.toFixed(2);
            }
            prevVal = v;
            var ts = cur ? lastTs(cur) : null;
            var d = cur ? (ROOMS[cur].data || []) : [];
            document.getElementById('dMeta').textContent =
                '共 ' + d.length + ' 条记录 · 更新于 ' + (ts ? fmtShort(ts) : '--');
            renderStats();
        }

        // ============ 消耗统计标签 ============
        function startOfDay(ts) {
            var d = new Date(ts);
            d.setHours(0, 0, 0, 0);
            return d.getTime();
        }

        function computeStats() {
            if (!cur) return null;
            var d = ROOMS[cur] ? ROOMS[cur].data : [];
            if (d.length < 2) return null;
            var last = Number(d[d.length - 1][3]);
            if (isNaN(last)) return null;
            var lastT = new Date(d[d.length - 1][0]).getTime();

            // 从 lastT 往前推 ms 窗口, 返回 {delta, spanH, full}
            function consumed(ms) {
                var start = lastT - ms;
                var i, t = 0;
                for (i = 0; i < d.length; i++) {
                    t = new Date(d[i][0]).getTime();
                    if (t >= start) break;
                }
                if (i >= d.length) return null;
                var baseV = Number(d[i][3]);
                if (isNaN(baseV)) return null;
                return {
                    delta: baseV - last,                 // 正=期间消耗, 负=期间充值
                    spanH: (lastT - t) / 3600000,        // 实际跨度(小时)
                    full: (t - start) <= 60000           // 窗口起点是否完整覆盖
                };
            }

            function fmtCons(c) {
                if (c === null) return ['--', false, false];
                if (c > 0.005) return [c.toFixed(2) + ' 度', false, false];
                if (c < -0.005) return ['充值 +' + Math.abs(c).toFixed(2) + ' 度', true, false];
                return ['0.00 度', false, true];   // flat: 窗口内读数未变
            }

            // 平均速度: 优先近7天窗口, 其次近24h, 最后按实际跨度; 充值时跳过
            function rate(ms) {
                var st = consumed(ms);
                if (!st || st.delta <= 0.05 || st.spanH < 1) return null;
                return st;   // perH = delta / spanH
            }
            var r7 = rate(7 * 86400000);
            var r24 = rate(86400000);
            var rUse = r7 || r24;
            var perH = rUse ? rUse.delta / rUse.spanH : null;
            var perD = perH !== null ? perH * 24 : null;
            var daysLeft = (perD !== null && perD > 0.1 && last > 0) ? last / perD : null;
            var rateNote = r7 ? '按近7天窗口' : (r24 ? '按近24小时' : '');
            if (rUse && !rUse.full) rateNote += '(不足' + rUse.spanH.toFixed(0) + '时, 按实际跨度)';

            var c24o = consumed(86400000);
            var cTo = consumed(lastT - startOfDay(lastT));
            var c7o = consumed(7 * 86400000);
            return {
                c24: fmtCons(c24o ? c24o.delta : null),
                cToday: fmtCons(cTo ? cTo.delta : null),
                c7d: fmtCons(c7o ? c7o.delta : null),
                perH: perH !== null ? perH.toFixed(2) + ' 度/时' : '--',
                perHLabel: r7 ? '均速(近7天)' : (r24 ? '均速(近24h)' : '均速'),
                perHNote: rateNote,
                daysLeft: daysLeft !== null ? daysLeft.toFixed(1) + ' 天' : '--',
                daysNote: rateNote
            };
        }

        function renderStats() {
            var el = document.getElementById('dStats');
            var s = computeStats();
            if (!s) { el.innerHTML = ''; return; }

            function chip(label, v, rc, tip, flat) {
                var t = tip || '';
                if (flat) t += (t ? '；' : '') + '该时段电表读数没有变化';
                return '<span class="chip' + (rc ? ' recharge' : '') + '"' +
                    (t ? ' title="' + t + '"' : '') + '>' + label +
                    '<b>' + v + '</b></span>';
            }
            el.innerHTML =
                chip('近24h 消耗', s.c24[0], s.c24[1], '最近24小时窗口的电量减少值(部分时段无数据时按实际跨度)', s.c24[2]) +
                chip('今日 消耗', s.cToday[0], s.cToday[1], '从今天0点起的减少值', s.cToday[2]) +
                chip('近7天 消耗', s.c7d[0], s.c7d[1], '最近7天窗口的减少值', s.c7d[2]) +
                chip(s.perHLabel || '均速', s.perH, false, s.perHNote || '按当前均速') +
                chip('预计可用', s.daysLeft, false, (s.daysNote || '按当前均速') + '估算剩余天数');
        }

        function openDetail(s) {
            if (!ROOMS[s]) return;
            cur = s;
            homeEl.style.display = 'none';
            detailEl.style.display = 'flex';
            fabEl.style.display = 'none';
            var ac = roomColor(s);
            document.getElementById('dCard').style.setProperty('--ac', ac);
            document.getElementById('dChart').style.setProperty('--ac', ac);
            document.getElementById('dRoomName').textContent = ROOMS[s].name || s;
            document.getElementById('dChartName').textContent = ROOMS[s].short || s;
            document.getElementById('dChartName').style.color = ac;
            if (location.hash !== '#' + s) { try { history.replaceState(null, '', '#' + s); } catch (e) {} }
            renderCard(false);
            setStatus('idle');
            animate(0, 720);
        }

        function goHome() {
            cur = null;
            homeEl.style.display = '';
            detailEl.style.display = 'none';
            fabEl.style.display = '';
            if (location.hash) { try { history.replaceState(null, '', location.pathname); } catch (e) {} }
            renderHome();
        }

        // ============ 折线图(当前宿舍单选线) ============
        function curData() {
            return cur && ROOMS[cur] ? (ROOMS[cur].data || []) : [];
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
            var w = cv.clientWidth,
                h = cv.clientHeight;
            if (!w || !h) { cv.width = cv.height = 0; return; }
            cv.width = Math.round(w * dpr);
            cv.height = Math.round(h * dpr);
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            ctx.clearRect(0, 0, w, h);

            var raw = curData();
            var pts = [];
            for (var i = 0; i < raw.length; i++) {
                var t = new Date(raw[i][0]);
                var v = Number(raw[i][3]);
                if (!isNaN(v)) pts.push({ t: t, v: v });
            }
            pts = filterRange(pts);
            if (!pts.length) {
                ctx.fillStyle = '#9ca3af';
                ctx.font = '13px sans-serif';
                ctx.textAlign = 'center';
                ctx.textBaseline = 'middle';
                ctx.fillText('暂无数据', w / 2, h / 2);
                return;
            }
            var color = cur ? roomColor(cur) : '#2563eb';
            var rgb = hexRgb(color);
            var P = { line: color, a: 'rgba(' + rgb[0] + ',' + rgb[1] + ',' + rgb[2] + ',',
                      grid: isDark() ? '#2a2f3a' : '#f0f2f5',
                      label: isDark() ? '#6b7280' : '#9ca3af',
                      pt: isDark() ? '#1c1f26' : '#ffffff' };
            var padL = 56,
                padR = 16,
                padT = 20,
                padB = 34;
            var pw = w - padL - padR,
                ph = h - padT - padB;

            var mn = Infinity,
                mx = -Infinity;
            for (var j = 0; j < pts.length; j++) {
                if (pts[j].v < mn) mn = pts[j].v;
                if (pts[j].v > mx) mx = pts[j].v;
            }
            var lo = Math.floor((mn - 0.5) * 10) / 10;
            var hi = Math.ceil((mx + 0.5) * 10) / 10;
            if (hi - lo < 1) { hi = lo + 1; }
            var t0 = pts[0].t.getTime(),
                t1 = pts[pts.length - 1].t.getTime();
            var span = (t1 - t0) || 1;

            function X(t) { return padL + (t.getTime() - t0) / span * pw; }

            function Y(v) { return padT + (hi - v) / (hi - lo) * ph; }

            var px = [],
                py = [];
            for (var k = 0; k < pts.length; k++) { px.push(X(pts[k].t)); py.push(Y(pts[k].v)); }

            // 网格 + Y轴标签(首尾行自适应基线, 数值精度自适应, 不外溢)
            ctx.font = '10px sans-serif';
            var yp = (hi - lo >= 20) ? 0 : 1;
            for (var s = 0; s <= 4; s++) {
                var vv = lo + (hi - lo) * s / 4;
                var yy = Y(vv);
                ctx.strokeStyle = P.grid;
                ctx.lineWidth = 1;
                ctx.beginPath();
                ctx.moveTo(padL, yy);
                ctx.lineTo(padL + pw, yy);
                ctx.stroke();
                ctx.fillStyle = P.label;
                ctx.textAlign = 'right';
                if (s === 0) { ctx.textBaseline = 'bottom'; ctx.fillText(vv.toFixed(yp), padL - 8, yy - 3); }
                else if (s === 4) { ctx.textBaseline = 'top'; ctx.fillText(vv.toFixed(yp), padL - 8, yy + 3); }
                else { ctx.textBaseline = 'middle'; ctx.fillText(vv.toFixed(yp), padL - 8, yy); }
            }
            // X轴: 3 个时间刻度
            var tickN = 3;
            var xts = [];
            for (var ti = 0; ti < tickN; ti++) {
                xts.push(t0 + span * ti / (tickN - 1));
            }
            ctx.textAlign = 'center';
            ctx.textBaseline = 'top';
            ctx.fillStyle = P.label;
            for (var tk = 0; tk < xts.length; tk++) {
                var tx0 = padL + (xts[tk] - t0) / span * pw;
                var lbl = fmtDT(new Date(xts[tk]));
                var lx0 = Math.min(Math.max(tx0, padL + 22), padL + pw - 22);
                ctx.fillText(lbl, lx0, padT + ph + 10);
            }

            var pDone = (pg === undefined || pg === null) ? 1 : pg;
            if (pDone < 1) {
                ctx.save();
                ctx.beginPath();
                ctx.rect(padL, padT - 3, Math.max(1, pw * pDone) + 3, ph + 8);
                ctx.clip();
            }

            // 面积
            ctx.beginPath();
            ctx.moveTo(px[0], py[0]);
            traceLine(px, py, px.length);
            ctx.lineTo(px[px.length - 1], padT + ph);
            ctx.lineTo(px[0], padT + ph);
            ctx.closePath();
            var g = ctx.createLinearGradient(0, padT, 0, padT + ph);
            g.addColorStop(0, P.a + '0.20)');
            g.addColorStop(1, P.a + '0)');
            ctx.fillStyle = g;
            ctx.fill();

            // 线
            ctx.beginPath();
            traceLine(px, py, px.length);
            ctx.strokeStyle = P.line;
            ctx.lineWidth = 2.6;
            ctx.lineJoin = 'round';
            ctx.lineCap = 'round';
            ctx.stroke();

            // 末点
            if (pDone >= 0.999) {
                var lx = px[px.length - 1],
                    ly = py[py.length - 1];
                var glow = ctx.createRadialGradient(lx, ly, 1, lx, ly, 16);
                glow.addColorStop(0, P.a + '0.35)');
                glow.addColorStop(1, P.a + '0)');
                ctx.fillStyle = glow;
                ctx.beginPath();
                ctx.arc(lx, ly, 16, 0, Math.PI * 2);
                ctx.fill();
                ctx.beginPath();
                ctx.arc(lx, ly, 5, 0, Math.PI * 2);
                ctx.fillStyle = P.pt;
                ctx.fill();
                ctx.lineWidth = 2.6;
                ctx.strokeStyle = P.line;
                ctx.stroke();
                ctx.font = 'bold 11px sans-serif';
                ctx.textAlign = 'left';
                ctx.fillStyle = P.line;
                var lbl1 = pts[pts.length - 1].v.toFixed(1) + ' 度';
                var tx1 = Math.min(lx + 10, padL + pw - 70);
                if (ly - 7 < padT + 12) {   // 顶部放不下就放到点下方
                    ctx.textBaseline = 'top';
                    ctx.fillText(lbl1, tx1, ly + 9);
                } else {
                    ctx.textBaseline = 'bottom';
                    ctx.fillText(lbl1, tx1, ly - 7);
                }
            }
            if (pDone < 1) ctx.restore();
        }

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

        function redraw() {
            if (animT) { cancelAnimationFrame(animT);
                animT = null; }
            if (cur) draw(1);
        }

        // ============ 范围切换 ============
        var rbtns = document.querySelectorAll('#dRanges button');
        for (var rb = 0; rb < rbtns.length; rb++) {
            rbtns[rb].addEventListener('click', function() {
                RANGE = this.getAttribute('data-r');
                for (var q = 0; q < rbtns.length; q++) rbtns[q].className = '';
                this.className = 'on';
                animate(0, 400);
            });
        }

        // ============ 添加宿舍弹窗 ============
        var inRoom = document.getElementById('inRoom');
        var inPass = document.getElementById('inPass');
        var addErr = document.getElementById('addErr');
        var addOk = document.getElementById('addOk');
        var addSteps = document.getElementById('addSteps');

        function repoActionsUrl() {
            try {
                var h = location.hostname.split('.');
                if (location.hostname.indexOf('github.io') > -1 && h.length >= 3) {
                    var user = h[0];
                    var repo = location.pathname.split('/')[1] || 'elec-monitor';
                    return 'https://github.com/' + user + '/' + repo +
                        '/actions/workflows/monitor.yml';
                }
            } catch (e) {}
            return '';
        }

        function openModal() {
            overlayEl.style.display = 'block';
            addErr.style.display = 'none';
            addOk.style.display = 'none';
            addSteps.style.display = 'none';
            inRoom.value = '';
            inPass.value = '';
            var chips = document.getElementById('curChips');
            chips.innerHTML = '';
            ORDER.forEach(function(s) { chips.innerHTML += '<span>' + s + '</span>'; });
        }

        function closeModal() { overlayEl.style.display = 'none'; }

        document.getElementById('addTop').addEventListener('click', openModal);
        fabEl.addEventListener('click', openModal);
        document.getElementById('closeModal').addEventListener('click', closeModal);
        overlayEl.addEventListener('click', function(e) {
            if (e.target === overlayEl) closeModal();
        });

        document.getElementById('goBtn').addEventListener('click', function() {
            var room = inRoom.value.trim();
            var pw = inPass.value;
            if (pw !== ADD_PASS || !/^[\w-]{2,16}$/.test(room.split('/').pop())) {
                addErr.style.display = 'block';
                addOk.style.display = 'none';
                addSteps.style.display = 'none';
                return;
            }
            addErr.style.display = 'none';
            addOk.style.display = 'block';
            addSteps.style.display = 'block';
            var url = repoActionsUrl();
            var link = url ? '<a href="' + url + '" target="_blank">与 GitHub 仓库连接的 Actions 页面</a>' : 'GitHub 仓库 → Actions 页面';
            addSteps.innerHTML =
                '<b>1.</b> 打开 ' + link + '（需要仓库管理员权限，只有你自己有）<br>' +
                '<b>2.</b> 点 <b>Run workflow</b>，在 <b>addRoom</b> 输入框填 <b>' + room + '</b>（房间代码可选填）<br>' +
                '<b>3.</b> 点绿色 <b>Run</b>，云端会真实验证房间存在并自动加入监控<br>' +
                '<b>4.</b> 约 1 分钟后本页刷新即可看到新宿舍（加到列表后自动开始每 10 分钟采集）';
        });

        // ============ 数据刷新 ============
        function refresh() {
            var changed = false;
            var jobs = ORDER.filter(function(s) { return useFetch; }).map(function(s) {
                return fetch('data/' + s + '.csv', { cache: 'no-store' })
                    .then(function(r) { return r.ok ? r.text() : null; })
                    .then(function(txt) {
                        if (txt === null) return;
                        var rows = [];
                        var lines = txt.split(/\r?\n/);
                        for (var i = 0; i < lines.length; i++) {
                            if (!lines[i].trim()) continue;
                            var p = lines[i].split(',');
                            if (p.length >= 4 && p[0] !== 'time') {
                                var n3 = Number(p[3]);
                                if (isNaN(n3)) continue;
                                rows.push([p[0], Number(p[1]), Number(p[2]), n3]);
                            }
                        }
                        if (!rows.length) return;
                        if (JSON.stringify(ROOMS[s].data) !== JSON.stringify(rows)) {
                            ROOMS[s].data = rows;
                            changed = true;
                        }
                    });
            });
            Promise.all(jobs).then(function() {
                if (!changed) return;
                renderHome();
                if (cur) {
                    renderCard(true);
                    setStatus('updated');
                    animate(0, 680);
                    setTimeout(function() { setStatus('idle'); }, 3200);
                }
            });
        }

        // ============ 启动 ============
        document.getElementById('back').addEventListener('click', goHome);
        window.addEventListener('hashchange', function() {
            var h = location.hash.slice(1);
            if (h && ROOMS[h]) openDetail(h);
            else goHome();
        });

        renderHome();
        var initial = location.hash.slice(1);
        if (initial && ROOMS[initial]) openDetail(initial);
        else goHome();

        var resizeTimer;
        window.addEventListener('resize', function() {
            clearTimeout(resizeTimer);
            resizeTimer = setTimeout(redraw, 120);
        });
        window.addEventListener('orientationchange', function() { setTimeout(redraw, 350); });
        var darkMatch = window.matchMedia('(prefers-color-scheme: dark)');
        if (darkMatch.addEventListener) darkMatch.addEventListener('change', function() { renderHome(); redraw(); });

        if (useFetch) {
            setInterval(refresh, 5 * 60 * 1000);
            setTimeout(refresh, 2500);
        } else {
            setTimeout(function() { location.reload(); }, 5 * 60 * 1000);
        }
    </script>
</body>
</html>
"""


# ================= 采集 / 添加 =================
def collect(rooms, now):
    ok_any = False
    for rm in rooms:
        try:
            buy, sub = query_room(rm)
            total = round(buy + sub, 2)
            path = room_csv(rm["short"])
            rows = _read_local_rows(path)
            if rows and (now - rows[-1][0]).total_seconds() < MIN_INTERVAL_S:
                print("%s %s: %.2f度 (距上次不足%d秒跳过)" % (
                    now.strftime("%H:%M:%S"), rm["short"], total, MIN_INTERVAL_S))
                ok_any = True
                continue
            _append_csv(path, now, buy, sub, total)
            print("%s %s: %.2f度 (已记录)" % (
                now.strftime("%H:%M:%S"), rm["short"], total))
            ok_any = True
        except Exception as e:
            print("%s %s: 查询失败 - %s" % (
                now.strftime("%H:%M:%S"), rm["short"], e))
    return ok_any


def add_room(rooms, room_input, dm_given=""):
    room_short = room_input.strip().rsplit("/", 1)[-1].strip()
    if not re.match(r"^[\w-]{2,16}$", room_short):
        sys.exit("房号格式不对: %s (示例: 4-268)" % room_input)
    if any(r["short"] == room_short for r in rooms):
        sys.exit("该房间已经在列表里了: %s" % room_short)
    if dm_given:
        dm = dm_given.strip()
        floor_name = ""
        try:
            md, fn = find_room_dm(room_short)
            floor_name = fn
            if md and md != dm:
                print("提示: 自动识别到该房间代码 %s, 用你填的 %s 验证..." % (md, dm))
        except Exception:
            floor_name = "2层"   # 手动给了代码就用默认楼层名
        room_name = "%s/%s/%s" % (rooms[0]["name"].rsplit("/", 2)[0],
                                  floor_name or "2层", room_short)
    else:
        try:
            dm, floor_name = find_room_dm(room_short)
        except Exception as e:
            sys.exit("添加失败: %s" % e)
        building = rooms[0]["name"].rsplit("/", 2)[0] if rooms else "4栋"
        room_name = "%s/%s/%s" % (building, floor_name, room_short)
    # 真实绑定验证
    try:
        buy, sub = query_room({"openid": rooms[0]["openid"],
                               "roomdm": dm, "room": room_name})
    except Exception as e:
        sys.exit("验证失败, 没有添加: %s" % e)
    color, dark = COLOR_WHEEL[len(rooms) % len(COLOR_WHEEL)]
    rooms.append({"short": room_short, "name": room_name, "roomdm": dm,
                  "openid": rooms[0]["openid"], "color": color, "dark": dark})
    save_rooms(rooms)
    print("已添加宿舍 %s (%s = %.2f度)" % (room_name, dm, buy + sub))
    return True


def remove_room(rooms, room_input):
    """按房号从列表删除宿舍, 并删除它的数据文件"""
    short = room_input.strip().rsplit("/", 1)[-1].strip()
    idx = next((i for i, r in enumerate(rooms) if r["short"] == short), -1)
    if idx < 0:
        sys.exit("列表里没有这个宿舍: %s (当前: %s)" % (
            short, ", ".join(r["short"] for r in rooms)))
    removed = rooms.pop(idx)
    save_rooms(rooms)
    p = room_csv(removed["short"])
    if os.path.exists(p):
        os.remove(p)
        print("已删除数据文件:", p)
    print("已删除宿舍 %s (%s)" % (removed["name"], removed["roomdm"]))
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=int, default=0)
    ap.add_argument("--chart", action="store_true")
    ap.add_argument("--add-room", metavar="房间号")
    ap.add_argument("--roomdm", default="")
    ap.add_argument("--remove-room", metavar="房间号")
    ap.add_argument("extra", nargs="*", help=argparse.SUPPRESS)
    args = ap.parse_args()

    rooms_data = load_rooms()
    rooms = rooms_data["rooms"]

    if args.remove_room:
        remove_room(rooms, args.remove_room)
        make_charts(rooms)
        return

    if args.add_room:
        # 兼容两种调用: --roomdm x  或 尾部直接跟房间代码(含旧的空串调用)
        dm = args.roomdm
        if not dm and args.extra:
            dm = args.extra[0]
        add_room(rooms, args.add_room, dm)
        collect(rooms, datetime.datetime.now().astimezone())
        make_charts(rooms)
        return

    if args.chart:
        make_charts(rooms)
        return

    migrate_legacy(rooms)
    while True:
        now = datetime.datetime.now().astimezone()
        collect(rooms, now)
        try:
            make_charts(rooms)
        except Exception as e:
            print("生成页面失败:", e)
        if not args.loop:
            break
        time.sleep(args.loop)


if __name__ == "__main__":
    main()