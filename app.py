import os
import io
import csv
import json
import re
import sqlite3
import traceback
import uuid
from datetime import date, datetime, timedelta

import groq
import requests
from flask import Flask, jsonify, request, g
from flask_cors import CORS
from werkzeug.exceptions import HTTPException
from dotenv import load_dotenv
from groq import Groq

# ==========================================
# 設定（全部從 .env 讀取，不要寫死在程式碼裡）
# ==========================================
load_dotenv()
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
# GROQ_MODEL 被 Groq 下架時依序改用這些
GROQ_FALLBACK_MODELS = ("openai/gpt-oss-120b", "qwen/qwen3.8-27b", "openai/gpt-oss-20b")
INTERVALS_API_KEY = os.getenv("INTERVALS_API_KEY")
# "0" 代表「API key 本人」，也可以填 intervals.icu 網址上的 i123456
INTERVALS_ATHLETE_ID = os.getenv("INTERVALS_ATHLETE_ID", "0")
ICU_BASE = "https://intervals.icu/api/v1"

RUN_TYPES = ("Run", "TrailRun", "VirtualRun", "TreadmillRun")

app = Flask(__name__)
CORS(app)

DATABASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "running.db")


# ==========================================
# 資料庫
# ==========================================
def get_db():
    db = getattr(g, "_database", None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
    return db


@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, "_database", None)
    if db is not None:
        db.close()


def init_db():
    with app.app_context():
        conn = get_db()
        conn.executescript("""
            -- 活動：數值用數字存，之後才能做統計（舊版存成 "5'40\"" 這種字串無法計算）
            CREATE TABLE IF NOT EXISTS workouts (
                id          TEXT PRIMARY KEY,      -- icu_i12345 / manual_20261006...
                source      TEXT NOT NULL,         -- garmin / manual
                icu_id      TEXT,
                start_local TEXT NOT NULL,         -- 2026-10-05T06:30:00
                title       TEXT,
                type        TEXT,
                distance_m  REAL,
                moving_s    INTEGER,
                avg_hr      REAL,
                max_hr      REAL,
                load        REAL,                  -- intervals.icu 算的訓練負荷
                route_json  TEXT                   -- 舊版軌跡快取，已改用 detail_json
            );
            -- 每日恢復數據（來自 Garmin → intervals.icu）
            CREATE TABLE IF NOT EXISTS wellness (
                date        TEXT PRIMARY KEY,
                ctl         REAL,                  -- 體能 Fitness
                atl         REAL,                  -- 疲勞 Fatigue
                resting_hr  REAL,
                hrv         REAL,
                sleep_s     REAL,
                sleep_score REAL,
                weight      REAL
            );
            -- 每天自己填的主觀感受（1–5 分）
            CREATE TABLE IF NOT EXISTS checkins (
                date          TEXT PRIMARY KEY,
                fatigue       INTEGER,             -- 1 很輕鬆 … 5 很累
                soreness      INTEGER,             -- 1 不痠 … 5 很痠
                sleep_quality INTEGER,             -- 1 很差 … 5 很好
                mood          INTEGER,             -- 1 很差 … 5 很好
                note          TEXT
            );
            -- 課表：每天一筆，AI 產生或手動輸入
            CREATE TABLE IF NOT EXISTS planned_workouts (
                id           TEXT PRIMARY KEY,
                date         TEXT NOT NULL UNIQUE,
                kind         TEXT NOT NULL,          -- rest / easy / recovery / long / tempo / interval / race / cross / other
                title        TEXT,
                distance_km  REAL,
                duration_min REAL,
                details      TEXT,
                steps_json   TEXT,                   -- 結構化步驟，之後傳到手錶用
                source       TEXT NOT NULL,          -- ai / manual
                plan_id      TEXT,                   -- 同一次套用的 AI 課表
                created_at   TEXT,
                icu_event_id TEXT                    -- 傳到 intervals.icu 後的行事曆編號
            );
            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
        """)
        # 舊資料庫補上新欄位
        new_cols = {
            "workouts": (("avg_cadence", "REAL"),     # 每分鐘步數 spm
                         ("elev_gain", "REAL"),       # 總爬升 m
                         ("calories", "REAL"),
                         ("intensity", "REAL"),       # intervals.icu 強度 %（相對乳酸閾值心率）
                         ("hr_zones_json", "TEXT"),   # {"times": 各區間秒數, "bounds": 區間上限心率}
                         ("detail_json", "TEXT")),    # 軌跡、心率/步頻/配速曲線快取，第一次點開才抓
            "wellness": (("steps", "INTEGER"),),
        }
        for table, cols in new_cols.items():
            existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            for col, typ in cols:
                if col not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        conn.commit()


init_db()


class UserFacingError(Exception):
    """可以直接顯示給使用者看的錯誤訊息"""


@app.errorhandler(UserFacingError)
def handle_user_error(e):
    return jsonify({"error": str(e)}), 400


@app.errorhandler(Exception)
def handle_unexpected(e):
    if isinstance(e, HTTPException):
        return jsonify({"error": e.description}), e.code
    traceback.print_exc()
    return jsonify({"error": f"伺服器錯誤：{e}"}), 500


# ==========================================
# 格式化小工具
# ==========================================
def fmt_duration(seconds):
    if not seconds:
        return "--"
    seconds = int(seconds)
    h, m, s = seconds // 3600, (seconds % 3600) // 60, seconds % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def fmt_pace(distance_m, seconds):
    if not distance_m or not seconds or distance_m < 50:
        return "--"
    sec_per_km = seconds / (distance_m / 1000)
    m, s = int(sec_per_km // 60), int(round(sec_per_km % 60))
    if s == 60:
        m, s = m + 1, 0
    return f"{m}'{s:02d}\""


def row_to_activity(r):
    start = r["start_local"] or ""
    try:
        dt = datetime.fromisoformat(start)
        date_str = dt.strftime("%Y/%m/%d %H:%M")
    except ValueError:
        date_str = start
    return {
        "id": r["id"],
        "title": r["title"] or "未命名活動",
        "date": date_str,
        "dist": f"{(r['distance_m'] or 0) / 1000:.2f}",
        "time": fmt_duration(r["moving_s"]),
        "pace": fmt_pace(r["distance_m"], r["moving_s"]),
        "hr": str(round(r["avg_hr"])) if r["avg_hr"] else "--",
        "max_hr": round(r["max_hr"]) if r["max_hr"] else None,
        "cadence": round(r["avg_cadence"]) if r["avg_cadence"] else None,
        "elev": round(r["elev_gain"]) if r["elev_gain"] else None,
        "calories": round(r["calories"]) if r["calories"] else None,
        "load": round(r["load"]) if r["load"] else None,
        "type": r["type"] or "Run",
        "source": r["source"],
        "has_detail": r["source"] == "garmin",
    }


def run_cadence(activity_type, cadence):
    """intervals.icu 的跑步步頻是單腳（rpm），乘 2 才是一般說的每分鐘步數（spm）"""
    if not cadence:
        return None
    return cadence * 2 if activity_type in RUN_TYPES else cadence


def remove_csv_duplicates(conn):
    """intervals.icu 收到同一筆活動後，刪掉先前用 CSV 匯入的那筆"""
    conn.execute("""
        DELETE FROM workouts WHERE source = 'csv'
          AND start_local IN (SELECT start_local FROM workouts WHERE source = 'garmin')
    """)


# ==========================================
# intervals.icu 串接
# ==========================================
def icu_get(path, params=None):
    if not INTERVALS_API_KEY:
        raise UserFacingError("尚未設定 INTERVALS_API_KEY，請在 .env 填入 intervals.icu 的 API key")
    res = requests.get(
        f"{ICU_BASE}{path}",
        params=params,
        auth=("API_KEY", INTERVALS_API_KEY),  # intervals.icu 規定帳號固定填 API_KEY
        timeout=30,
    )
    if res.status_code in (401, 403):
        raise UserFacingError("intervals.icu 驗證失敗，請確認 API key 與 athlete ID")
    res.raise_for_status()
    return res.json()


@app.route("/api/sync", methods=["POST"])
def sync():
    body = request.get_json(silent=True) or {}
    days = max(1, min(int(body.get("days", 30)), 3650))
    conn = get_db()
    # 新增心率區間、步數等欄位後，第一次同步重抓一年，舊活動才有這些資料
    backfilled = conn.execute("SELECT 1 FROM meta WHERE key = 'backfill_v2'").fetchone()
    if not backfilled:
        days = max(days, 365)
    oldest = (date.today() - timedelta(days=days)).isoformat()
    newest = date.today().isoformat()
    ath = INTERVALS_ATHLETE_ID

    # 1) 活動
    acts = icu_get(f"/athlete/{ath}/activities", {"oldest": oldest, "newest": newest})
    saved, skipped_strava = 0, 0
    for a in acts:
        # 從 Strava 匯入 intervals.icu 的活動，API 不會給完整資料（Strava 條款限制）
        if a.get("source") == "STRAVA":
            skipped_strava += 1
            continue
        if not a.get("start_date_local"):
            continue
        conn.execute("""
            INSERT INTO workouts (id, source, icu_id, start_local, title, type,
                                  distance_m, moving_s, avg_hr, max_hr, load,
                                  avg_cadence, elev_gain, calories, intensity, hr_zones_json)
            VALUES (?, 'garmin', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                start_local=excluded.start_local, title=excluded.title, type=excluded.type,
                distance_m=excluded.distance_m, moving_s=excluded.moving_s,
                avg_hr=excluded.avg_hr, max_hr=excluded.max_hr, load=excluded.load,
                avg_cadence=COALESCE(excluded.avg_cadence, avg_cadence),
                elev_gain=COALESCE(excluded.elev_gain, elev_gain),
                calories=COALESCE(excluded.calories, calories),
                intensity=excluded.intensity, hr_zones_json=excluded.hr_zones_json
        """, (
            f"icu_{a['id']}", str(a["id"]), a["start_date_local"], a.get("name"), a.get("type"),
            a.get("distance"), a.get("moving_time"), a.get("average_heartrate"),
            a.get("max_heartrate"), a.get("icu_training_load"),
            run_cadence(a.get("type"), a.get("average_cadence")),
            a.get("total_elevation_gain"), a.get("calories"), a.get("icu_intensity"),
            json.dumps({"times": a["icu_hr_zone_times"], "bounds": a["icu_hr_zones"]})
            if a.get("icu_hr_zone_times") and a.get("icu_hr_zones") else None,
        ))
        saved += 1

    # 2) 每日恢復數據
    wl = icu_get(f"/athlete/{ath}/wellness", {"oldest": oldest, "newest": newest})
    for w in wl:
        if not w.get("id"):
            continue
        conn.execute("""
            INSERT INTO wellness (date, ctl, atl, resting_hr, hrv, sleep_s, sleep_score, weight, steps)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                ctl=excluded.ctl, atl=excluded.atl, resting_hr=excluded.resting_hr,
                hrv=excluded.hrv, sleep_s=excluded.sleep_s,
                sleep_score=excluded.sleep_score, weight=excluded.weight, steps=excluded.steps
        """, (
            w["id"], w.get("ctl"), w.get("atl"), w.get("restingHR"), w.get("hrv"),
            w.get("sleepSecs"), w.get("sleepScore"), w.get("weight"), w.get("steps"),
        ))

    remove_csv_duplicates(conn)
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('backfill_v2', '1')")
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('last_sync', ?)",
                 (datetime.now().isoformat(timespec="seconds"),))
    conn.commit()
    return jsonify({"activities": saved, "wellness": len(wl), "skipped_strava": skipped_strava})


# ==========================================
# 匯入 Garmin Connect 活動列表 CSV（intervals.icu 沒收到資料時的備案）
# ==========================================
CSV_TYPE_MAP = {
    "跑步": "Run", "跑步機": "TreadmillRun", "越野跑": "TrailRun", "虛擬跑步": "VirtualRun",
    "田徑跑道跑步": "Run", "室內跑步": "TreadmillRun",
    "騎自行車": "Ride", "室內騎自行車": "VirtualRide", "步行": "Walk", "健行": "Hike",
    "泳池游泳": "Swim", "開放水域游泳": "Swim", "有氧運動": "Workout", "肌力訓練": "WeightTraining",
}


def csv_num(v):
    v = (v or "").replace(",", "").strip()
    try:
        return float(v)
    except ValueError:
        return None  # "--" 之類代表沒資料


def csv_seconds(v):
    """'00:04:48.0' / '02:04:46' / '27:36' → 秒數"""
    v = (v or "").strip()
    if not v or v == "--":
        return None
    try:
        parts = [float(p) for p in v.split(":")]
    except ValueError:
        return None
    sec = 0.0
    for p in parts:
        sec = sec * 60 + p
    return int(round(sec))


@app.route("/api/import/garmin-csv", methods=["POST"])
def import_garmin_csv():
    f = request.files.get("file")
    if not f:
        raise UserFacingError("沒有收到檔案")
    raw = f.read()
    for enc in ("utf-8-sig", "big5", "cp950"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise UserFacingError("無法讀取檔案編碼")

    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows or "日期" not in rows[0] or "活動類型" not in rows[0]:
        raise UserFacingError("這不是 Garmin Connect 匯出的活動列表 CSV（需要中文欄位：活動類型、日期）")

    conn = get_db()
    garmin_starts = {r["start_local"] for r in conn.execute(
        "SELECT start_local FROM workouts WHERE source = 'garmin'")}
    added, skipped = 0, 0
    for r in rows:
        try:
            start = datetime.strptime(r["日期"].strip(), "%Y-%m-%d %H:%M:%S").isoformat()
        except ValueError:
            continue
        if start in garmin_starts:
            skipped += 1  # intervals.icu 已經有了，資料比較完整
            continue
        moving = csv_seconds(r.get("移動時間")) or csv_seconds(r.get("時間"))
        dist_km = csv_num(r.get("距離"))
        conn.execute("""
            INSERT INTO workouts (id, source, start_local, title, type, distance_m, moving_s, avg_hr, max_hr,
                                  avg_cadence, elev_gain, calories)
            VALUES (?, 'csv', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title=excluded.title, type=excluded.type, distance_m=excluded.distance_m,
                moving_s=excluded.moving_s, avg_hr=excluded.avg_hr, max_hr=excluded.max_hr,
                avg_cadence=excluded.avg_cadence, elev_gain=excluded.elev_gain, calories=excluded.calories
        """, (
            f"csv_{start}", start, r.get("標題") or r["活動類型"],
            CSV_TYPE_MAP.get(r["活動類型"].strip(), r["活動類型"].strip()),
            dist_km * 1000 if dist_km is not None else None, moving,
            csv_num(r.get("平均心率")), csv_num(r.get("最大心率")),
            csv_num(r.get("平均步頻")),  # Garmin CSV 已經是 spm
            csv_num(r.get("總爬升")), csv_num(r.get("卡路里")),
        ))
        added += 1
    conn.commit()
    return jsonify({"imported": added, "skipped": skipped})


# ==========================================
# 活動 CRUD
# ==========================================
@app.route("/api/workouts", methods=["GET"])
def list_workouts():
    limit = max(1, min(int(request.args.get("limit", 100)), 1000))
    rows = get_db().execute(
        "SELECT * FROM workouts ORDER BY start_local DESC LIMIT ?", (limit,)).fetchall()
    return jsonify([row_to_activity(r) for r in rows])


@app.route("/api/workouts", methods=["POST"])
def add_manual_workout():
    data = request.get_json() or {}
    try:
        dist_km = float(data.get("dist"))
        minutes = float(data.get("minutes"))
    except (TypeError, ValueError):
        raise UserFacingError("請填寫距離與時間")
    now = datetime.now()
    wid = f"manual_{now.strftime('%Y%m%d%H%M%S%f')}"
    conn = get_db()
    conn.execute("""
        INSERT INTO workouts (id, source, start_local, title, type, distance_m, moving_s)
        VALUES (?, 'manual', ?, ?, 'Run', ?, ?)
    """, (wid, data.get("start_local") or now.isoformat(timespec="seconds"),
          data.get("title") or "手動紀錄", dist_km * 1000, int(minutes * 60)))
    conn.commit()
    return jsonify({"id": wid}), 201


@app.route("/api/workouts/<wid>", methods=["DELETE"])
def delete_workout(wid):
    conn = get_db()
    conn.execute("DELETE FROM workouts WHERE id = ?", (wid,))
    conn.commit()
    return jsonify({"deleted": wid})


def icu_get_optional(path, params=None):
    """抓不到時回傳 None（例如跑步機沒有 GPS），驗證錯誤仍然往上丟"""
    try:
        return icu_get(path, params)
    except requests.HTTPError:
        return None


def fetch_activity_detail(row):
    """從 intervals.icu 抓軌跡、逐秒曲線與心率區間，並補齊摘要欄位"""
    icu_id = row["icu_id"]

    data = icu_get_optional(f"/activity/{icu_id}/map")
    raw = data.get("latlngs") if isinstance(data, dict) else None
    points = [[p[0], p[1]] for p in (raw or [])
              if isinstance(p, (list, tuple)) and len(p) >= 2 and p[0] is not None and p[1] is not None]
    points = points[::max(1, len(points) // 1500)]  # 降採樣，地圖畫起來比較快

    streams = None
    raw_streams = icu_get_optional(f"/activity/{icu_id}/streams",
                                   {"types": "time,distance,heartrate,cadence,velocity_smooth,altitude"})
    if isinstance(raw_streams, list):
        s = {st.get("type"): st.get("data") or [] for st in raw_streams if isinstance(st, dict)}
        n = len(s.get("time") or [])
        if n:
            idx = range(0, n, max(1, n // 600))  # 圖表最多約 600 點
            is_run = row["type"] in RUN_TYPES

            def col(key, fn=lambda v: v):
                arr = s.get(key) or []
                if len(arr) != n:
                    return None
                return [fn(arr[i]) if arr[i] is not None else None for i in idx]

            streams = {
                "time": col("time"),
                "distance_km": col("distance", lambda v: round(v / 1000, 3)),
                "heartrate": col("heartrate"),
                "cadence": col("cadence", lambda v: v * 2 if is_run else v),
                "pace": col("velocity_smooth", lambda v: round(1000 / v) if v > 0.5 else None),  # 秒/公里
                "altitude": col("altitude", lambda v: round(v, 1)),
            }

    act = icu_get_optional(f"/activity/{icu_id}")
    hr_zones = None
    if isinstance(act, dict):
        if act.get("icu_hr_zone_times") and act.get("icu_hr_zones"):
            hr_zones = {"times": act["icu_hr_zone_times"], "bounds": act["icu_hr_zones"]}
        get_db().execute("""
            UPDATE workouts SET avg_cadence = COALESCE(?, avg_cadence), elev_gain = COALESCE(?, elev_gain),
                                calories = COALESCE(?, calories), max_hr = COALESCE(?, max_hr)
            WHERE id = ?
        """, (run_cadence(row["type"], act.get("average_cadence")), act.get("total_elevation_gain"),
              act.get("calories"), act.get("max_heartrate"), row["id"]))
    return {"route": points, "streams": streams, "hr_zones": hr_zones}, isinstance(act, dict)


@app.route("/api/workouts/<wid>/detail", methods=["GET"])
def workout_detail(wid):
    conn = get_db()
    row = conn.execute("SELECT * FROM workouts WHERE id = ?", (wid,)).fetchone()
    if not row:
        raise UserFacingError("找不到這筆活動")

    if row["detail_json"]:
        detail = json.loads(row["detail_json"])
    elif row["source"] == "garmin" and row["icu_id"]:
        detail, complete = fetch_activity_detail(row)
        if complete:  # intervals.icu 暫時出錯就不快取，下次點開再試
            conn.execute("UPDATE workouts SET detail_json = ? WHERE id = ?", (json.dumps(detail), wid))
        conn.commit()
        row = conn.execute("SELECT * FROM workouts WHERE id = ?", (wid,)).fetchone()
    else:
        detail = {"route": [], "streams": None, "hr_zones": None}
    return jsonify({**detail, "activity": row_to_activity(row)})


# ==========================================
# 統計與今日狀態
# ==========================================
RUN_FILTER = f"type IN ({','.join('?' * len(RUN_TYPES))})"


# 本週訓練：每天的訓練類型、週跑量與警訊
TRAINED_KINDS = ("easy", "moderate", "hard", "long", "run", "cross")
LONG_RUN_MIN_KM = 8  # 長距離至少 8 km，且達到前 4 週平均週跑量的 25%


def week_summary(conn, today):
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    first = monday - timedelta(days=28)
    rows = conn.execute(
        "SELECT date(start_local) AS d, type, title, distance_m, hr_zones_json FROM workouts "
        "WHERE date(start_local) >= ? AND date(start_local) <= ?",
        (first.isoformat(), sunday.isoformat())).fetchall()
    by_day = {}
    for r in rows:
        by_day.setdefault(r["d"], []).append(r)

    plans = {p["date"]: p for p in plans_between(conn, monday.isoformat(), sunday.isoformat())}
    prev_km = sum((r["distance_m"] or 0) for r in rows
                  if r["type"] in RUN_TYPES and r["d"] < monday.isoformat()) / 1000
    avg_4w = prev_km / 4
    long_km = max(LONG_RUN_MIN_KM, avg_4w * 0.25)

    def day_info(d):
        items = by_day.get(d.isoformat(), [])
        runs = [r for r in items if r["type"] in RUN_TYPES]
        real_runs = [r for r in runs if (r["distance_m"] or 0) >= 500]  # 0.5 km 以下是手錶切出來的片段
        km = sum((r["distance_m"] or 0) for r in runs) / 1000
        longest = max(((r["distance_m"] or 0) for r in real_runs), default=0) / 1000
        if real_runs:
            # 和 AI 教練相同的標準：高強度 Z4+ ≥ 30%、輕鬆 Z1–Z2 ≥ 70%，其餘算中強度
            found = set()
            for r in real_runs:
                split = zone_split(r["hr_zones_json"]) if r["hr_zones_json"] else None
                if split and sum(split):
                    total = sum(split)
                    found.add("hard" if split[2] / total >= 0.3 else "easy" if split[0] / total >= 0.7 else "moderate")
            if "hard" in found:
                kind = "hard"
            elif longest >= long_km:
                kind = "long"
            elif "moderate" in found:
                kind = "moderate"
            elif "easy" in found:
                kind = "easy"
            else:
                kind = "run"  # 沒有心率區間（手動、CSV 或沒戴心率）
        elif items:
            kind = "cross" if any(r["type"] not in RUN_TYPES for r in items) else "run"
        elif d > today:
            kind = "future"
        elif d == today:
            kind = "today"
        else:
            kind = "rest"
        return {"date": d.isoformat(), "label": f"{d.month}/{d.day}", "weekday": "一二三四五六日"[d.weekday()],
                "kind": kind, "km": round(km, 1), "longest_km": round(longest, 1),
                "titles": [r["title"] for r in items if r["title"] and not (r["type"] in RUN_TYPES and (r["distance_m"] or 0) < 500)],
                "is_today": d == today, "plan": plans.get(d.isoformat())}

    days = [day_info(monday + timedelta(days=i)) for i in range(7)]
    total_km = round(sum(d["km"] for d in days), 1)

    flags = []
    if avg_4w > 0 and total_km > avg_4w * 1.1:
        flags.append(f"本週跑量已達前 4 週平均的 {round(total_km / avg_4w * 100)}%，建議不超過 110%")
    prev = day_info(monday - timedelta(days=1))
    for a, b in zip([prev] + days[:-1], days):
        if a["kind"] == "hard" and b["kind"] == "hard":
            flags.append(f"週{a['weekday']}、週{b['weekday']}連續兩天高強度，中間最好安排輕鬆跑或休息")
    streak, d = 0, today if day_info(today)["kind"] in TRAINED_KINDS else today - timedelta(days=1)
    while d >= first and day_info(d)["kind"] in TRAINED_KINDS:
        streak, d = streak + 1, d - timedelta(days=1)
    if streak >= 7:
        flags.append(f"已連續 {streak} 天訓練，沒有休息日")

    return {
        "start_label": f"{monday.month}/{monday.day}", "end_label": f"{sunday.month}/{sunday.day}",
        "days": days, "total_km": total_km, "avg_4w": round(avg_4w, 1),
        "over": bool(avg_4w > 0 and total_km > avg_4w * 1.1),
        "hard_sessions": sum(d["kind"] == "hard" for d in days),
        "longest_km": max(d["longest_km"] for d in days),
        "rest_days": sum(d["kind"] == "rest" for d in days),
        "planned_km": round(sum(p["distance_km"] or 0 for p in plans.values()), 1),
        "flags": flags,
    }


@app.route("/api/stats", methods=["GET"])
def stats():
    weeks = 12
    today = date.today()
    this_monday = today - timedelta(days=today.weekday())
    start = this_monday - timedelta(weeks=weeks - 1)
    rows = get_db().execute(
        f"SELECT date(start_local) AS d, distance_m FROM workouts WHERE {RUN_FILTER} AND date(start_local) >= ?",
        (*RUN_TYPES, start.isoformat())).fetchall()

    weekly = [0.0] * weeks
    daily = [0.0] * 7
    for r in rows:
        d = date.fromisoformat(r["d"])
        km = (r["distance_m"] or 0) / 1000
        wi = (d - start).days // 7
        if 0 <= wi < weeks:
            weekly[wi] += km
        di = (today - d).days
        if 0 <= di < 7:
            daily[6 - di] += km

    weekday = "一二三四五六日"
    day_labels = [f"{(today - timedelta(days=6 - i)).month}/{(today - timedelta(days=6 - i)).day}"
                  f"({weekday[(today - timedelta(days=6 - i)).weekday()]})" for i in range(7)]
    week_labels = [(start + timedelta(weeks=i)).strftime("%m/%d") for i in range(weeks)]
    week_labels[-1] = "本週"

    total = get_db().execute(
        f"SELECT COALESCE(SUM(distance_m), 0) AS t FROM workouts WHERE {RUN_FILTER}", RUN_TYPES).fetchone()["t"]

    return jsonify({
        "daily": {"labels": day_labels, "data": [round(x, 2) for x in daily]},
        "weekly": {"labels": week_labels, "data": [round(x, 2) for x in weekly]},
        "this_week_km": round(weekly[-1], 2),
        "total_km": round(total / 1000, 1),
        "week": week_summary(get_db(), today),
    })


def zone_split(hr_zones_json):
    """把心率區間秒數分成輕鬆（Z1–Z2）、中強度（Z3）、高強度（Z4 以上）"""
    try:
        times = json.loads(hr_zones_json)["times"]
    except (TypeError, ValueError, KeyError):
        return None
    return sum(times[:2]), sum(times[2:3]), sum(times[3:])


def training_snapshot(conn, today):
    """A. 課表執行能力：只看訓練數據（負荷、跑量、強度分配、有氧效率）"""
    t = today.isoformat()

    load_row = conn.execute(
        "SELECT date, ctl, atl FROM wellness WHERE ctl IS NOT NULL AND date <= ? ORDER BY date DESC LIMIT 1",
        (t,)).fetchone()
    week_ago = conn.execute(
        "SELECT ctl FROM wellness WHERE ctl IS NOT NULL AND date <= ? ORDER BY date DESC LIMIT 1",
        ((today - timedelta(days=7)).isoformat(),)).fetchone()

    def run_sum(days_from, days_to):
        r = conn.execute(
            # 手錶常把一次訓練切成好幾段，0.5 km 以下的片段不算一次跑步
            f"""SELECT COALESCE(SUM(distance_m), 0) AS km, SUM(distance_m >= 500) AS n, MAX(distance_m) AS longest
                FROM workouts WHERE {RUN_FILTER} AND date(start_local) > ? AND date(start_local) <= ?""",
            (*RUN_TYPES, (today - timedelta(days=days_from)).isoformat(),
             (today - timedelta(days=days_to)).isoformat())).fetchone()
        return r["km"] / 1000, r["n"] or 0, (r["longest"] or 0) / 1000

    km7, runs7, _ = run_sum(7, 0)
    km_prev28, runs_prev28, longest28 = run_sum(35, 7)  # 近 7 天之前的 4 週，當作「平常」的基準

    # 近 28 天跑步的心率區間分配，順便分出輕鬆跑和高強度課
    rows = conn.execute(
        f"""SELECT start_local, type, distance_m, moving_s, avg_hr, hr_zones_json FROM workouts
            WHERE {RUN_FILTER} AND date(start_local) > ? AND hr_zones_json IS NOT NULL""",
        (*RUN_TYPES, (today - timedelta(days=42)).isoformat())).fetchall()
    easy_s = mod_s = hard_s = 0
    hard_sessions_14 = 0
    easy_runs = []  # (日期, 每次心跳前進公尺, 配速秒/公里, 平均心率)
    for r in rows:
        split = zone_split(r["hr_zones_json"])
        if not split or not sum(split):
            continue
        d = date.fromisoformat(r["start_local"][:10])
        total = sum(split)
        if (today - d).days < 28:
            easy_s, mod_s, hard_s = easy_s + split[0], mod_s + split[1], hard_s + split[2]
        if (today - d).days < 14 and split[2] / total >= 0.3:
            hard_sessions_14 += 1
        # 有氧效率只看戶外跑，跑步機的配速靠估算不準
        if r["type"] in ("Run", "TrailRun") and split[0] / total >= 0.7 and r["avg_hr"] and r["moving_s"] and (r["distance_m"] or 0) > 2000:
            speed = r["distance_m"] / (r["moving_s"] / 60)  # 公尺/分鐘
            easy_runs.append((d, speed / r["avg_hr"], r["moving_s"] / (r["distance_m"] / 1000), r["avg_hr"]))

    def easy_avg(days_from, days_to):
        sel = [e for e in easy_runs if days_to <= (today - e[0]).days < days_from]
        if not sel:
            return None
        return {"runs": len(sel),
                "m_per_beat": round(sum(e[1] for e in sel) / len(sel), 2),
                "pace": fmt_pace(1000, sum(e[2] for e in sel) / len(sel)),
                "avg_hr": round(sum(e[3] for e in sel) / len(sel))}

    zone_total = easy_s + mod_s + hard_s
    ctl = load_row["ctl"] if load_row else None
    atl = load_row["atl"] if load_row else None
    return {
        "ctl": round(ctl, 1) if ctl is not None else None,
        "atl": round(atl, 1) if atl is not None else None,
        "tsb": round(ctl - atl, 1) if ctl is not None and atl is not None else None,
        "ctl_ramp_7d": round(ctl - week_ago["ctl"], 1) if ctl is not None and week_ago else None,
        "km7": round(km7, 1),
        "runs7": runs7,
        "weekly_avg_4w": round(km_prev28 / 4, 1),
        "runs_per_week_4w": round(runs_prev28 / 4, 1),
        "longest_4w": round(longest28, 1),
        "intensity_28d": {
            "hours": round(zone_total / 3600, 1),
            "easy_pct": round(easy_s / zone_total * 100),
            "moderate_pct": round(mod_s / zone_total * 100),
            "hard_pct": round(hard_s / zone_total * 100),
        } if zone_total else None,
        "hard_sessions_14d": hard_sessions_14,
        "easy_recent": easy_avg(14, 0),     # 近 14 天輕鬆跑
        "easy_before": easy_avg(42, 14),    # 再往前 4 週，用來看有氧效率趨勢
    }


def body_snapshot(conn, today):
    """B. 身體日常狀態：靜止心率、睡眠、日常活動量、主觀感受"""
    t = today.isoformat()
    yesterday = (today - timedelta(days=1)).isoformat()
    today_w = conn.execute("SELECT * FROM wellness WHERE date = ?", (t,)).fetchone()

    # Garmin 的靜止心率主要靠睡眠時段計算；沒戴錶睡覺那天只能拿白天的心率估，常常偏高很多
    # 所以只採用「當天有睡眠紀錄」的靜止心率
    reliable = conn.execute(
        """SELECT date, resting_hr FROM wellness
           WHERE resting_hr IS NOT NULL AND sleep_s IS NOT NULL AND date <= ?
           ORDER BY date DESC LIMIT 1""", (t,)).fetchone()
    baseline = None
    if reliable:
        baseline = conn.execute(
            """SELECT AVG(resting_hr) AS a, COUNT(*) AS n FROM wellness
               WHERE resting_hr IS NOT NULL AND sleep_s IS NOT NULL AND date < ? AND date >= ?""",
            (reliable["date"], (date.fromisoformat(reliable["date"]) - timedelta(days=28)).isoformat())).fetchone()

    nights_7 = conn.execute(
        "SELECT COUNT(*) AS n FROM wellness WHERE sleep_s IS NOT NULL AND date > ? AND date <= ?",
        ((today - timedelta(days=7)).isoformat(), t)).fetchone()["n"]
    steps_y = conn.execute("SELECT steps FROM wellness WHERE date = ?", (yesterday,)).fetchone()
    steps_avg = conn.execute(
        "SELECT AVG(steps) AS a FROM wellness WHERE steps > 0 AND date >= ? AND date < ?",
        ((today - timedelta(days=8)).isoformat(), yesterday)).fetchone()["a"]
    checkin = conn.execute("SELECT * FROM checkins WHERE date = ?", (t,)).fetchone()

    worn_last_night = bool(today_w and today_w["sleep_s"])
    raw_today = today_w["resting_hr"] if today_w else None
    return {
        "worn_last_night": worn_last_night,
        "sleep_h": round(today_w["sleep_s"] / 3600, 1) if worn_last_night else None,
        "sleep_score": today_w["sleep_score"] if worn_last_night else None,
        "nights_worn_7d": nights_7,
        "resting_hr": round(reliable["resting_hr"]) if reliable else None,
        "resting_hr_date": reliable["date"] if reliable else None,
        "resting_hr_days_ago": (today - date.fromisoformat(reliable["date"])).days if reliable else None,
        "resting_hr_baseline": round(baseline["a"], 1) if baseline and baseline["a"] else None,
        "resting_hr_baseline_days": baseline["n"] if baseline else 0,
        # 今天 Garmin 給的數字但沒戴錶睡覺，不可靠，只顯示不判斷
        "resting_hr_unreliable_today": round(raw_today) if raw_today and not worn_last_night else None,
        "steps_yesterday": steps_y["steps"] if steps_y else None,
        "steps_avg_7d": round(steps_avg) if steps_avg else None,
        "checkin": dict(checkin) if checkin else None,
    }


def build_snapshot():
    """今天的狀態，分成「課表執行能力」和「身體日常狀態」兩部分，給前端顯示也給 AI 判斷"""
    conn = get_db()
    today = date.today()
    last_sync = conn.execute("SELECT value FROM meta WHERE key = 'last_sync'").fetchone()
    return {
        "date": today.isoformat(),
        "training": training_snapshot(conn, today),
        "body": body_snapshot(conn, today),
        "last_sync": last_sync["value"] if last_sync else None,
    }


@app.route("/api/status", methods=["GET"])
def status():
    return jsonify(build_snapshot())


@app.route("/api/checkin", methods=["POST"])
def save_checkin():
    d = request.get_json() or {}

    def score(key):
        v = d.get(key)
        return int(v) if v not in (None, "") and 1 <= int(v) <= 5 else None

    conn = get_db()
    conn.execute("""
        INSERT OR REPLACE INTO checkins (date, fatigue, soreness, sleep_quality, mood, note)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (d.get("date") or date.today().isoformat(), score("fatigue"), score("soreness"),
          score("sleep_quality"), score("mood"), d.get("note")))
    conn.commit()
    return jsonify({"status": "ok"})


# ==========================================
# AI 教練
# ==========================================
def build_context_text():
    s = build_snapshot()
    tr, bd = s["training"], s["body"]
    conn = get_db()
    lines = [f"今天日期：{s['date']}", "", "【A. 課表執行能力】（訓練數據）"]

    if tr["ctl"] is not None:
        ramp = f"，CTL 近 7 天變化 {tr['ctl_ramp_7d']:+}" if tr["ctl_ramp_7d"] is not None else ""
        lines.append(f"[訓練負荷] 體能 CTL {tr['ctl']}、疲勞 ATL {tr['atl']}、狀態 TSB {tr['tsb']}{ramp}"
                     "（TSB 越負代表累積疲勞越多；-10~-30 為一般訓練區間，低於 -30 疲勞偏高）")
    else:
        lines.append("[訓練負荷] 無資料")

    lines.append(f"[跑量] 近 7 天 {tr['km7']} km（{tr['runs7']} 次）；之前 4 週平均每週 {tr['weekly_avg_4w']} km、"
                 f"{tr['runs_per_week_4w']} 次；這 4 週最長單次 {tr['longest_4w']} km")

    it = tr["intensity_28d"]
    lines.append(f"[強度分配 近 28 天，共 {it['hours']} 小時] 輕鬆 Z1–Z2 {it['easy_pct']}%、"
                 f"中強度 Z3 {it['moderate_pct']}%、高強度 Z4+ {it['hard_pct']}%；"
                 f"近 14 天高強度課 {tr['hard_sessions_14d']} 次" if it else "[強度分配] 無心率區間資料")

    er, eb = tr["easy_recent"], tr["easy_before"]
    if er or eb:
        def fmt(e):
            return f"{e['runs']} 次，平均配速 {e['pace']}/km、心率 {e['avg_hr']}、每次心跳前進 {e['m_per_beat']} m" if e else "無"
        lines.append(f"[有氧效率（輕鬆跑）] 近 14 天：{fmt(er)}；之前 4 週：{fmt(eb)}")
    else:
        lines.append("[有氧效率] 沒有足夠的輕鬆跑資料")

    recent = conn.execute(
        "SELECT * FROM workouts WHERE date(start_local) >= ? ORDER BY start_local DESC LIMIT 15",
        ((date.today() - timedelta(days=21)).isoformat(),)).fetchall()
    if recent:
        lines.append("[近 3 週活動]")
        for r in recent:
            if r["type"] in RUN_TYPES and (r["distance_m"] or 0) < 200:
                continue  # 手錶切出來的零碎片段
            a = row_to_activity(r)
            split = zone_split(r["hr_zones_json"]) if r["hr_zones_json"] else None
            kind = ""
            if split and sum(split):
                kind = "（高強度）" if split[2] / sum(split) >= 0.3 else "（輕鬆）" if split[0] / sum(split) >= 0.7 else "（中強度）"
            lines.append(f"- {a['date'][:10]} {a['type']}{kind} 「{a['title']}」 {a['dist']} km，"
                         f"配速 {a['pace']}/km，平均心率 {a['hr']}"
                         + (f"，步頻 {a['cadence']} spm" if a["cadence"] else "")
                         + (f"，負荷 {a['load']}" if a["load"] else ""))
    else:
        lines.append("[近 3 週活動] 無紀錄")

    lines += ["", "【B. 身體日常狀態】（恢復數據）"]
    lines.append(f"[戴錶睡覺] 昨晚{'有' if bd['worn_last_night'] else '沒有'}；近 7 晚有 {bd['nights_worn_7d']} 晚有睡眠紀錄")
    lines.append(f"[睡眠] 昨晚 {bd['sleep_h']} 小時" if bd["sleep_h"] else "[睡眠] 昨晚無資料")

    if bd["resting_hr"]:
        base = bd["resting_hr_baseline"]
        diff = f"，比基準 {bd['resting_hr'] - base:+.1f}" if base else ""
        ago = bd["resting_hr_days_ago"]
        stale = f"；這是 {ago} 天前的數字，不能代表今天的狀態" if ago > 2 else ""
        lines.append(f"[靜止心率（只採用有戴錶睡覺的日子）] {bd['resting_hr']} bpm（{bd['resting_hr_date']}），"
                     f"基準 {base or '不足'}（{bd['resting_hr_baseline_days']} 天）{diff}{stale}")
    else:
        lines.append("[靜止心率] 沒有可靠資料（最近沒有戴錶睡覺）")
    if bd["resting_hr_unreliable_today"]:
        lines.append(f"（今天 Garmin 顯示 {bd['resting_hr_unreliable_today']}，但昨晚沒戴錶睡覺，此數字不可靠，不要用來判斷）")

    if bd["steps_yesterday"] is not None:
        lines.append(f"[日常活動量] 昨天 {bd['steps_yesterday']} 步，前 7 天平均 {bd['steps_avg_7d'] or '無'} 步")

    c = bd["checkin"]
    if c:
        lines.append(f"[今日主觀感受 1–5 分] 疲勞 {c['fatigue']}（5=很累）、痠痛 {c['soreness']}（5=很痠）、"
                     f"睡眠品質 {c['sleep_quality']}（5=很好）、心情 {c['mood']}（5=很好）"
                     + (f"；備註：{c['note']}" if c.get("note") else ""))
    else:
        lines.append("[今日主觀感受] 未填寫")
    return "\n".join(lines)


def extract_json(text):
    text = (text or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for open_c, close_c in (("{", "}"), ("[", "]")):
        start, end = text.find(open_c), text.rfind(close_c)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                continue
    raise UserFacingError("AI 回傳的格式無法解析，請再試一次")


def ask_llm(system_prompt, user_prompt, temperature=0.3):
    if not GROQ_API_KEY:
        raise UserFacingError("尚未設定 GROQ_API_KEY")
    client = Groq(api_key=GROQ_API_KEY)
    models = list(dict.fromkeys([GROQ_MODEL, *GROQ_FALLBACK_MODELS]))
    for model in models:
        kwargs = dict(
            model=model,
            messages=[{"role": "system", "content": system_prompt},
                      {"role": "user", "content": user_prompt}],
            temperature=temperature,
        )
        try:
            try:
                completion = client.chat.completions.create(response_format={"type": "json_object"}, **kwargs)
            except groq.BadRequestError:
                # 部分模型不支援 JSON mode，退回一般模式再自己擷取
                completion = client.chat.completions.create(**kwargs)
        except groq.NotFoundError:
            print(f"Groq 模型 {model} 無法使用，改試下一個")
            continue
        return extract_json(completion.choices[0].message.content)
    raise UserFacingError(f"Groq 上找不到可用的模型（試過：{'、'.join(models)}），請在 .env 修改 GROQ_MODEL")


COACH_RULES = """判斷原則分成兩部分，請分開評估：
A. 課表執行能力（只看訓練數據）
- TSB 低於 -30 代表累積疲勞偏高；CTL 一週上升超過 5~8 代表加量太快。
- 跑量增加要循序：新的一週總量原則上不超過前 4 週平均的 110%；長距離不超過前 4 週最長單次 +2~3 km。
- 強度分配：約 80% 時間在輕鬆區（Z1–Z2）較理想；高強度比例過高或近 14 天高強度課太多，應該減少高強度。
- 有氧效率：輕鬆跑「每次心跳前進的距離」上升代表有氧能力進步，下降可能是疲勞或天氣影響。
B. 身體日常狀態（只看恢復數據）
- 靜止心率只採用「有戴錶睡覺」那天的數值；比基準高 5 以上是警訊。超過 2 天前的數字不能代表今天，不要當作今天正常的依據。沒戴錶睡覺那天的靜止心率不可靠，絕對不能拿來判斷。
- 睡眠少於 6 小時、主觀疲勞或痠痛 ≥ 4 是警訊。
- 沒有可靠的客觀數據時，以主觀感受為主，並提醒戴錶睡覺。
綜合結論：身體狀態警訊 2 項以上 → rest；身體警訊 1 項、或訓練負荷/加量過高 → easy；其餘 → go。
其他：
- 資料缺少時不要編造數字，直接說明依據不足並給保守建議。
- 這是訓練建議，不是醫療診斷；若使用者描述疼痛持續或異常症狀，建議停止訓練並就醫。
- 使用者的手錶是 Garmin Forerunner 235，沒有 HRV。"""


@app.route("/api/coach/today", methods=["POST"])
def coach_today():
    body = request.get_json(silent=True) or {}
    planned = (body.get("planned") or "").strip()
    system = f"""你是經驗豐富、說話直接的跑步教練，用繁體中文回答。你會根據跑者的實際數據判斷今天該怎麼練。
{COACH_RULES}
只回傳 JSON 物件，格式：
{{"training": {{"rating": "good" | "ok" | "caution", "summary": "課表執行能力的一句評估，25 字內",
               "points": ["2~3 點，只談訓練數據，要引用實際數字"]}},
  "body": {{"rating": "good" | "ok" | "caution", "summary": "身體日常狀態的一句評估，25 字內",
           "points": ["2~3 點，只談恢復數據與主觀感受，要引用實際數字"]}},
  "verdict": "go" | "easy" | "rest",
  "headline": "綜合兩者的一句話結論，20 字內",
  "workout": "今天具體要做的內容（距離或時間、配速或心率區間）",
  "tip": "一個今天可以做的恢復或注意事項"}}
rating：good=狀態好、ok=普通、caution=要注意。verdict：go=照原計畫、easy=降低強度、rest=休息或恢復"""
    if not planned:
        today_plan = plans_between(get_db(), date.today().isoformat(), date.today().isoformat())
        if today_plan:
            planned = "課表安排：" + plan_text(today_plan[0])
    user = build_context_text() + "\n\n今天原本預計的訓練：" + (planned or "未指定，請你建議")
    return jsonify(ask_llm(system, user, temperature=0.3))


# ==========================================
# 課表：AI 產生或手動輸入，每天一筆
# ==========================================
PLAN_KINDS = {"rest": "休息", "easy": "輕鬆跑", "recovery": "恢復跑", "long": "長距離", "tempo": "節奏跑",
              "interval": "間歇", "race": "比賽", "cross": "交叉訓練", "other": "其他"}
PLAN_DAYS = 14
PACE_RE = re.compile(r"^\d{1,2}:[0-5]\d(-\d{1,2}:[0-5]\d)?$")
WEEKDAY_CHARS = "一二三四五六日"


def positive(value, upper):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if 0 < value < upper else None


def normalize_steps(raw, depth=0):
    """整理 AI 給的結構化步驟；格式不對的步驟直接略過"""
    if not isinstance(raw, list):
        return None
    out = []
    for s in raw[:20]:
        if not isinstance(s, dict):
            continue
        if "repeat" in s:
            try:
                reps = int(s["repeat"])
            except (TypeError, ValueError):
                continue
            inner = normalize_steps(s.get("steps"), depth + 1) if depth == 0 else None  # 只允許一層重複
            if inner and 2 <= reps <= 30:
                out.append({"repeat": reps, "steps": inner})
            continue
        dist, dur = positive(s.get("distance_km"), 100), positive(s.get("duration_min"), 600)
        if not dist and not dur:
            continue
        step = {"label": str(s.get("label") or "跑步").strip()[:12]}
        if dist:
            step["distance_km"] = round(dist, 2)
        else:
            step["duration_min"] = round(dur, 1)
        pace = str(s.get("pace") or "").replace(" ", "")
        if PACE_RE.match(pace):
            step["pace"] = pace
        else:
            try:
                zone = int(s.get("hr_zone"))
            except (TypeError, ValueError):
                zone = None
            if zone and 1 <= zone <= 5:
                step["hr_zone"] = zone
        out.append(step)
    return out or None


def normalize_plan_item(item, source):
    if not isinstance(item, dict):
        return None
    try:
        day = date.fromisoformat(str(item.get("date") or "")[:10])
    except ValueError:
        return None
    kind = item.get("kind") if item.get("kind") in PLAN_KINDS else "other"
    rest = kind == "rest"
    dist = None if rest else positive(item.get("distance_km"), 100)
    dur = None if rest else positive(item.get("duration_min"), 600)
    return {
        "date": day.isoformat(), "kind": kind,
        "title": str(item.get("title") or "").strip()[:40] or PLAN_KINDS[kind],
        "distance_km": round(dist, 1) if dist else None,
        "duration_min": round(dur) if dur else None,
        "details": str(item.get("details") or "").strip()[:300],
        "steps": None if rest else normalize_steps(item.get("steps")),
        "source": source,
    }


def save_plan_items(conn, items, plan_id=None):
    """同一天只保留一筆，已有的就更新（編號不變，之後同步到手錶時才能更新同一筆）"""
    now = datetime.now().isoformat(timespec="seconds")
    for it in items:
        conn.execute("""
            INSERT INTO planned_workouts (id, date, kind, title, distance_km, duration_min, details, steps_json,
                                          source, plan_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                kind=excluded.kind, title=excluded.title, distance_km=excluded.distance_km,
                duration_min=excluded.duration_min, details=excluded.details, steps_json=excluded.steps_json,
                source=excluded.source, plan_id=excluded.plan_id, created_at=excluded.created_at
        """, (f"plan_{uuid.uuid4().hex[:12]}", it["date"], it["kind"], it["title"], it["distance_km"],
              it["duration_min"], it["details"],
              json.dumps(it["steps"], ensure_ascii=False) if it["steps"] else None,
              it["source"], plan_id, now))


def plan_row(r):
    return {"id": r["id"], "date": r["date"], "kind": r["kind"], "title": r["title"],
            "distance_km": r["distance_km"], "duration_min": r["duration_min"], "details": r["details"] or "",
            "steps": json.loads(r["steps_json"]) if r["steps_json"] else None, "source": r["source"]}


def plans_between(conn, oldest, newest):
    rows = conn.execute("SELECT * FROM planned_workouts WHERE date >= ? AND date <= ? ORDER BY date",
                        (oldest, newest)).fetchall()
    return [plan_row(r) for r in rows]


def plan_text(p):
    parts = [p["title"]]
    if p["distance_km"]:
        parts.append(f"{p['distance_km']} km")
    if p["duration_min"]:
        parts.append(f"{round(p['duration_min'])} 分鐘")
    return "，".join(parts) + (f"：{p['details']}" if p["details"] else "")


@app.route("/api/plan", methods=["GET"])
def list_plan():
    today = date.today()
    oldest = request.args.get("oldest") or today.isoformat()
    newest = request.args.get("newest") or (today + timedelta(days=PLAN_DAYS - 1)).isoformat()
    try:
        date.fromisoformat(oldest), date.fromisoformat(newest)
    except ValueError:
        raise UserFacingError("日期格式錯誤")
    return jsonify(plans_between(get_db(), oldest, newest))


@app.route("/api/plan/workouts", methods=["POST"])
def save_manual_plan():
    item = normalize_plan_item(request.get_json(silent=True) or {}, "manual")
    if not item:
        raise UserFacingError("請選擇課表日期")
    conn = get_db()
    save_plan_items(conn, [item])
    conn.commit()
    return jsonify(plans_between(conn, item["date"], item["date"])[0]), 201


@app.route("/api/plan/workouts/<pid>", methods=["DELETE"])
def delete_plan(pid):
    conn = get_db()
    conn.execute("DELETE FROM planned_workouts WHERE id = ?", (pid,))
    conn.commit()
    return jsonify({"deleted": pid})


@app.route("/api/plan/apply", methods=["POST"])
def apply_plan():
    raw = (request.get_json(silent=True) or {}).get("days")
    if not isinstance(raw, list) or not raw or len(raw) > 31:
        raise UserFacingError("沒有可套用的課表")
    items = [n for n in (normalize_plan_item(x, "ai") for x in raw) if n]
    if not items:
        raise UserFacingError("課表格式不正確")
    conn = get_db()
    save_plan_items(conn, items, plan_id=f"ai_{uuid.uuid4().hex[:8]}")
    conn.commit()
    return jsonify({"saved": len(items), "start": min(i["date"] for i in items), "end": max(i["date"] for i in items)})


@app.route("/api/generate-plan", methods=["POST"])
def generate_plan():
    """只產生預覽，使用者按「套用」才會存進課表"""
    req = request.get_json(silent=True) or {}
    goal_race = req.get("goalRace") or "強身健體"
    goal_time = req.get("goalTime") or "未指定"
    selected = set(req.get("selectedDays") or [])
    if not selected:
        raise UserFacingError("請至少選擇一個訓練日")
    try:
        start = date.fromisoformat(str(req.get("startDate") or ""))
    except ValueError:
        start = date.today() + timedelta(days=1)
    dates = [start + timedelta(days=i) for i in range(PLAN_DAYS)]

    def trainable(d):
        return f"週{WEEKDAY_CHARS[d.weekday()]}" in selected

    calendar = "\n".join(f"{d.isoformat()}（週{WEEKDAY_CHARS[d.weekday()]}）：{'可訓練' if trainable(d) else '休息日'}"
                         for d in dates)

    system = f"""你是專業的跑步教練，用繁體中文回答。你會根據跑者「目前真實的訓練狀態」排課表，而不是套用通用模板。
{COACH_RULES}
請為使用者列出的每一個日期各排一筆課表，只回傳 JSON 物件，格式：
{{"days": [
  {{"date": "2026-11-20", "kind": "easy", "title": "輕鬆跑", "distance_km": 8, "duration_min": 52,
    "details": "8 km 輕鬆跑，配速 6:20-6:40/km，心率維持 Z2",
    "steps": [{{"label": "輕鬆跑", "distance_km": 8, "hr_zone": 2}}]}},
  {{"date": "2026-11-21", "kind": "interval", "title": "800m 間歇", "distance_km": 8.6, "duration_min": 50,
    "details": "暖身 2 km、800m × 6 配速 4:25-4:35、每趟間慢跑 400m、緩和 2 km",
    "steps": [{{"label": "暖身", "distance_km": 2, "hr_zone": 2}},
              {{"repeat": 6, "steps": [{{"label": "快跑", "distance_km": 0.8, "pace": "4:25-4:35"}},
                                       {{"label": "恢復", "distance_km": 0.4, "hr_zone": 1}}]}},
              {{"label": "緩和", "distance_km": 2, "hr_zone": 1}}]}},
  {{"date": "2026-11-22", "kind": "rest", "title": "休息", "details": "完全休息或輕度伸展", "steps": []}}
]}}
規則：
- kind 只能是 rest、easy、recovery、long、tempo、interval、race、cross 其中之一。
- 每一個列出的日期都要剛好一筆；標示「休息日」的日期一律 kind 為 rest。
- 每一步要有 distance_km 或 duration_min；強度用 pace（每公里配速，寫成 "分:秒-分:秒"）或 hr_zone（1–5）擇一。
- repeat 只能用在最外層，裡面放每一趟的步驟。
- distance_km、duration_min 是整堂課的總量；details 用一句話寫清楚內容。
- 配速要依據跑者近期實際配速推算，跑量循序漸進，遵守上面的判斷原則。"""
    user = f"""目標：{goal_race}
期望：{goal_time}

要排的日期：
{calendar}

以下是跑者目前的數據：
{build_context_text()}"""

    result = ask_llm(system, user, temperature=0.2)
    raw_days = result.get("days") if isinstance(result, dict) else None
    if not isinstance(raw_days, list):
        raise UserFacingError("AI 回傳的課表格式不正確，請再試一次")
    by_date = {}
    for item in raw_days:
        n = normalize_plan_item(item, "ai")
        if n:
            by_date.setdefault(n["date"], n)

    days = []
    for d in dates:
        item = by_date.get(d.isoformat())
        if not trainable(d):  # 使用者說不能練的日子，一律休息
            item = normalize_plan_item({"date": d.isoformat(), "kind": "rest", "details": "休息日"}, "ai")
        elif not item:
            item = normalize_plan_item({"date": d.isoformat(), "kind": "rest", "details": "AI 沒有安排，可自行補上"}, "ai")
        days.append(item)
    return jsonify({"start": dates[0].isoformat(), "end": dates[-1].isoformat(), "days": days})


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok",
                    "intervals_configured": bool(INTERVALS_API_KEY),
                    "groq_configured": bool(GROQ_API_KEY)})


if __name__ == "__main__":
    app.run(debug=True, port=5000)
