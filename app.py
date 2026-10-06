import os
import json
import sqlite3
import traceback
from datetime import date, datetime, timedelta

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
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
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
                route_json  TEXT                   -- 軌跡快取，第一次點開才抓
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
            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
        """)
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
        "load": round(r["load"]) if r["load"] else None,
        "type": r["type"] or "Run",
        "source": r["source"],
        "has_route": r["source"] == "garmin",
    }


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
    oldest = (date.today() - timedelta(days=days)).isoformat()
    newest = date.today().isoformat()
    ath = INTERVALS_ATHLETE_ID
    conn = get_db()

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
                                  distance_m, moving_s, avg_hr, max_hr, load)
            VALUES (?, 'garmin', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                start_local=excluded.start_local, title=excluded.title, type=excluded.type,
                distance_m=excluded.distance_m, moving_s=excluded.moving_s,
                avg_hr=excluded.avg_hr, max_hr=excluded.max_hr, load=excluded.load
        """, (
            f"icu_{a['id']}", str(a["id"]), a["start_date_local"], a.get("name"), a.get("type"),
            a.get("distance"), a.get("moving_time"), a.get("average_heartrate"),
            a.get("max_heartrate"), a.get("icu_training_load"),
        ))
        saved += 1

    # 2) 每日恢復數據
    wl = icu_get(f"/athlete/{ath}/wellness", {"oldest": oldest, "newest": newest})
    for w in wl:
        if not w.get("id"):
            continue
        conn.execute("""
            INSERT INTO wellness (date, ctl, atl, resting_hr, hrv, sleep_s, sleep_score, weight)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                ctl=excluded.ctl, atl=excluded.atl, resting_hr=excluded.resting_hr,
                hrv=excluded.hrv, sleep_s=excluded.sleep_s,
                sleep_score=excluded.sleep_score, weight=excluded.weight
        """, (
            w["id"], w.get("ctl"), w.get("atl"), w.get("restingHR"), w.get("hrv"),
            w.get("sleepSecs"), w.get("sleepScore"), w.get("weight"),
        ))

    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('last_sync', ?)",
                 (datetime.now().isoformat(timespec="seconds"),))
    conn.commit()
    return jsonify({"activities": saved, "wellness": len(wl), "skipped_strava": skipped_strava})


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


@app.route("/api/workouts/<wid>/route", methods=["GET"])
def workout_route(wid):
    conn = get_db()
    row = conn.execute("SELECT source, icu_id, route_json FROM workouts WHERE id = ?", (wid,)).fetchone()
    if not row:
        raise UserFacingError("找不到這筆活動")
    if row["route_json"]:
        return jsonify(json.loads(row["route_json"]))
    if row["source"] != "garmin" or not row["icu_id"]:
        return jsonify([])

    try:
        data = icu_get(f"/activity/{row['icu_id']}/map")
    except requests.HTTPError:
        data = None  # 跑步機等沒有 GPS 的活動
    raw = (data or {}).get("latlngs") if isinstance(data, dict) else None
    points = [[p[0], p[1]] for p in (raw or [])
              if isinstance(p, (list, tuple)) and len(p) >= 2 and p[0] is not None and p[1] is not None]
    step = max(1, len(points) // 1500)  # 降採樣，地圖畫起來比較快
    points = points[::step]
    conn.execute("UPDATE workouts SET route_json = ? WHERE id = ?", (json.dumps(points), wid))
    conn.commit()
    return jsonify(points)


# ==========================================
# 統計與今日狀態
# ==========================================
RUN_FILTER = f"type IN ({','.join('?' * len(RUN_TYPES))})"


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
    })


def build_snapshot():
    """把資料庫整理成「今天的身體與訓練狀態」，給前端顯示也給 AI 判斷"""
    conn = get_db()
    today = date.today()
    t = today.isoformat()

    load_row = conn.execute(
        "SELECT date, ctl, atl FROM wellness WHERE ctl IS NOT NULL AND date <= ? ORDER BY date DESC LIMIT 1",
        (t,)).fetchone()
    rhr_row = conn.execute(
        "SELECT date, resting_hr FROM wellness WHERE resting_hr IS NOT NULL AND date <= ? ORDER BY date DESC LIMIT 1",
        (t,)).fetchone()
    rhr_avg = None
    if rhr_row:
        rhr_avg = conn.execute(
            "SELECT AVG(resting_hr) AS a FROM wellness WHERE resting_hr IS NOT NULL AND date < ? AND date >= ?",
            (rhr_row["date"], (date.fromisoformat(rhr_row["date"]) - timedelta(days=28)).isoformat())
        ).fetchone()["a"]
    sleep_row = conn.execute(
        "SELECT date, sleep_s, sleep_score FROM wellness WHERE sleep_s IS NOT NULL AND date <= ? ORDER BY date DESC LIMIT 1",
        (t,)).fetchone()

    def run_sum(days_from, days_to):
        r = conn.execute(
            f"""SELECT COALESCE(SUM(distance_m), 0) AS km, COUNT(*) AS n, MAX(distance_m) AS longest
                FROM workouts WHERE {RUN_FILTER} AND date(start_local) > ? AND date(start_local) <= ?""",
            (*RUN_TYPES, (today - timedelta(days=days_from)).isoformat(),
             (today - timedelta(days=days_to)).isoformat())).fetchone()
        return r["km"] / 1000, r["n"], (r["longest"] or 0) / 1000

    km7, runs7, _ = run_sum(7, 0)
    km_prev28, _, longest28 = run_sum(35, 7)  # 近 7 天之前的 4 週，當作「平常」的基準

    checkin = conn.execute("SELECT * FROM checkins WHERE date = ?", (t,)).fetchone()
    last_sync = conn.execute("SELECT value FROM meta WHERE key = 'last_sync'").fetchone()

    ctl = load_row["ctl"] if load_row else None
    atl = load_row["atl"] if load_row else None
    return {
        "date": t,
        "ctl": round(ctl, 1) if ctl is not None else None,
        "atl": round(atl, 1) if atl is not None else None,
        "tsb": round(ctl - atl, 1) if ctl is not None and atl is not None else None,
        "resting_hr": round(rhr_row["resting_hr"]) if rhr_row else None,
        "resting_hr_date": rhr_row["date"] if rhr_row else None,
        "resting_hr_avg": round(rhr_avg, 1) if rhr_avg else None,
        "sleep_h": round(sleep_row["sleep_s"] / 3600, 1) if sleep_row else None,
        "sleep_date": sleep_row["date"] if sleep_row else None,
        "km7": round(km7, 1),
        "runs7": runs7,
        "weekly_avg_4w": round(km_prev28 / 4, 1),
        "longest_4w": round(longest28, 1),
        "checkin": dict(checkin) if checkin else None,
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
    conn = get_db()
    lines = [f"今天日期：{s['date']}"]

    if s["ctl"] is not None:
        lines.append(f"[訓練負荷] 體能 CTL {s['ctl']}、疲勞 ATL {s['atl']}、狀態 TSB {s['tsb']}"
                     "（TSB 越負代表累積疲勞越多；-10~-30 為一般訓練區間，低於 -30 疲勞偏高）")
    else:
        lines.append("[訓練負荷] 無資料")

    lines.append(f"[跑量] 近 7 天 {s['km7']} km（{s['runs7']} 次）；"
                 f"之前 4 週平均每週 {s['weekly_avg_4w']} km；這 4 週最長單次 {s['longest_4w']} km")

    if s["resting_hr"]:
        diff = f"，比平均{'+' if s['resting_hr'] >= s['resting_hr_avg'] else ''}{round(s['resting_hr'] - s['resting_hr_avg'], 1)}" \
            if s["resting_hr_avg"] else ""
        lines.append(f"[靜止心率] {s['resting_hr']} bpm（{s['resting_hr_date']}），"
                     f"前 28 天平均 {s['resting_hr_avg'] or '無'}{diff}")
    else:
        lines.append("[靜止心率] 無資料")

    lines.append(f"[睡眠] {s['sleep_h']} 小時（{s['sleep_date']}）" if s["sleep_h"] else "[睡眠] 無資料")

    c = s["checkin"]
    if c:
        lines.append(f"[今日主觀感受 1–5 分] 疲勞 {c['fatigue']}（5=很累）、痠痛 {c['soreness']}（5=很痠）、"
                     f"睡眠品質 {c['sleep_quality']}（5=很好）、心情 {c['mood']}（5=很好）"
                     + (f"；備註：{c['note']}" if c.get("note") else ""))
    else:
        lines.append("[今日主觀感受] 未填寫")

    recent = conn.execute(
        "SELECT * FROM workouts WHERE date(start_local) >= ? ORDER BY start_local DESC LIMIT 15",
        ((date.today() - timedelta(days=21)).isoformat(),)).fetchall()
    if recent:
        lines.append("[近 3 週活動]")
        for r in recent:
            a = row_to_activity(r)
            lines.append(f"- {a['date'][:10]} {a['type']} 「{a['title']}」 {a['dist']} km，"
                         f"配速 {a['pace']}/km，平均心率 {a['hr']}"
                         + (f"，負荷 {a['load']}" if a["load"] else ""))
    else:
        lines.append("[近 3 週活動] 無紀錄")
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
    kwargs = dict(
        model=GROQ_MODEL,
        messages=[{"role": "system", "content": system_prompt},
                  {"role": "user", "content": user_prompt}],
        temperature=temperature,
    )
    try:
        completion = client.chat.completions.create(response_format={"type": "json_object"}, **kwargs)
    except Exception:
        # 部分模型不支援 JSON mode，退回一般模式再自己擷取
        completion = client.chat.completions.create(**kwargs)
    return extract_json(completion.choices[0].message.content)


COACH_RULES = """判斷原則：
- 恢復警訊：靜止心率比平均高 5 以上、睡眠少於 6 小時、主觀疲勞或痠痛 ≥ 4、TSB 低於 -30。出現 2 項以上建議休息或只做輕鬆恢復跑；1 項則降低強度。
- 跑量增加要循序：新的一週總量原則上不超過前 4 週平均的 110%；長距離不超過前 4 週最長單次 +2~3 km。
- 資料缺少時不要編造數字，直接說明依據不足並給保守建議。
- 這是訓練建議，不是醫療診斷；若使用者描述疼痛持續或異常症狀，建議停止訓練並就醫。
- 使用者的手錶是 Garmin Forerunner 235，沒有 HRV，恢復判斷以靜止心率、睡眠與主觀感受為主。"""


@app.route("/api/coach/today", methods=["POST"])
def coach_today():
    body = request.get_json(silent=True) or {}
    planned = (body.get("planned") or "").strip()
    system = f"""你是經驗豐富、說話直接的跑步教練，用繁體中文回答。你會根據跑者的實際數據判斷今天該怎麼練。
{COACH_RULES}
只回傳 JSON 物件，格式：
{{"verdict": "go" | "easy" | "rest",
  "headline": "一句話結論，20 字內",
  "workout": "今天具體要做的內容（距離或時間、配速或心率區間）",
  "reasons": ["根據哪些數據做出判斷，2~4 點，要引用實際數字"],
  "tip": "一個今天可以做的恢復或注意事項"}}
verdict：go=照原計畫、easy=降低強度、rest=休息或恢復"""
    user = build_context_text() + "\n\n今天原本預計的訓練：" + (planned or "未指定，請你建議")
    return jsonify(ask_llm(system, user, temperature=0.3))


@app.route("/api/generate-plan", methods=["POST"])
def generate_plan():
    req = request.get_json() or {}
    goal_race = req.get("goalRace")
    goal_time = req.get("goalTime")
    selected_days = req.get("selectedDays") or []

    system = f"""你是專業的馬拉松教練，用繁體中文回答。你會根據跑者「目前真實的訓練狀態」排課表，而不是套用通用模板。
{COACH_RULES}
只回傳 JSON 物件，格式：
{{"weeks": [
  {{"week": 1, "totalKm": 35, "workouts": [
    {{"day": "週一", "type": "休息", "details": "恢復日", "icon": "fa-bed", "iconBg": "rgba(142, 142, 147, 0.2)"}},
    {{"day": "週二", "type": "輕鬆跑", "details": "8 km @ 6'20\\"", "icon": "fa-person-running", "iconBg": "rgba(50, 215, 75, 0.2)"}},
    {{"day": "週三", "type": "間歇", "details": "800m x 6，恢復 90 秒", "icon": "fa-stopwatch", "iconBg": "rgba(191, 90, 242, 0.2)"}},
    {{"day": "週日", "type": "長距離", "details": "LSD 16 km", "icon": "fa-route", "iconBg": "rgba(255, 159, 10, 0.2)"}}
  ]}}
]}}
規則：共 2 週、每週七天都要列出、未選的訓練日一律「休息」；icon 與 iconBg 依範例配對（休息 fa-bed、輕鬆跑 fa-person-running、間歇/節奏 fa-stopwatch、長距離 fa-route）。
details 要具體，配速要依據跑者近期實際配速推算。"""
    user = f"""目標賽事：{goal_race}
期望：{goal_time}
可訓練日：{', '.join(selected_days)}

以下是跑者目前的數據：
{build_context_text()}"""

    result = ask_llm(system, user, temperature=0.2)
    plan = result.get("weeks") if isinstance(result, dict) else result
    if not isinstance(plan, list):
        raise UserFacingError("AI 回傳的課表格式不正確，請再試一次")
    return jsonify(plan)


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok",
                    "intervals_configured": bool(INTERVALS_API_KEY),
                    "groq_configured": bool(GROQ_API_KEY)})


if __name__ == "__main__":
    app.run(debug=True, port=5000)
