import os
from flask_cors import CORS
from flask import Flask, jsonify, request, g
from dotenv import load_dotenv
import requests
import sqlite3
import traceback

# 🚨 載入 .env
load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# 🔍 加上這兩行來抓蟲
print(f"DEBUG: 系統讀到的金鑰是 -> {GEMINI_API_KEY}")
if not GEMINI_API_KEY:
    print("❌ 警告：沒讀到金鑰！請檢查 .env 檔案是否在 app.py 同一層目錄")
else:
    print(f"✅ 金鑰長度: {len(GEMINI_API_KEY)}")

app = Flask(__name__)
CORS(app)  # 解除跨網域 CORS 限制

DATABASE = 'running.db'

# ==========================================
# 🚨 資料庫初始化 (建立 SQLite 檔案與資料表)
# ==========================================
def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def init_db():
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS activities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT, date TEXT, dist TEXT, time TEXT,
            pace TEXT, hr TEXT, type TEXT, source TEXT, polyline TEXT
        )
    ''')
    conn.commit()
    conn.close()

init_db() # 啟動時執行

# ==========================================
# 🚨 API 路由設定
# ==========================================

# 1. 測試前端與後端連線的路由 (讓前端藍色標籤亮起來)
@app.route('/api/test-data', methods=['GET'])
def get_test_data():
    return jsonify({
        "message": "這是一筆來自 Python Flask 後端安全傳遞的測試數據！",
        "system_status": "OK"
    })

# 2. 讀取所有資料庫裡的活動紀錄
@app.route("/api/activities", methods=["GET"])
def get_activities():
    cursor = get_db().cursor()
    cursor.execute('SELECT * FROM activities ORDER BY id DESC')
    rows = cursor.fetchall()
    return jsonify([dict(row) for row in rows]), 200

# 3. 將新的活動紀錄存進資料庫
@app.route("/api/activities", methods=["POST"])
def save_activity():
    data = request.get_json()
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO activities (title, date, dist, time, pace, hr, type, source, polyline)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        data.get('title'), data.get('date'), data.get('dist'),
        data.get('time'), data.get('pace'), data.get('hr'),
        data.get('type'), data.get('source'), data.get('polyline')
    ))
    conn.commit()
    return jsonify({"status": "success", "message": "活動已成功存入資料庫！"}), 201

# 4. Gemini AI 課表生成中轉站
@app.route("/api/generate-plan", methods=["POST"])
def generate_plan():
    # 🚨 新增偵錯代碼，它會把金鑰的開頭印出來給你看
    print(f"正在使用的金鑰開頭: {GEMINI_API_KEY[:6]}...")

    try:
        req_data = request.get_json()
        goal_race = req_data.get("goalRace")
        goal_time = req_data.get("goalTime")
        selected_days = req_data.get("selectedDays")

        prompt = f"""你是一位專業的馬拉松國家級教練。請幫一位跑者量身打造一份「2 週」的馬拉松訓練計畫。
        目標賽事: {goal_race}
        跑者期望: {goal_time}
        每週固定的訓練天數: {', '.join(selected_days)}。其餘沒選的天數一律安排為「休息」。

        請嚴格按照以下 JSON 格式回傳，不要包含任何 markdown 標記（如 ```json），只能有 JSON 陣列本身：
        [
          {{
            "week": 1,
            "totalKm": 35,
            "workouts": [
              {{ "day": "週一", "type": "休息", "details": "恢復日", "icon": "fa-bed", "iconBg": "rgba(142, 142, 147, 0.2)" }},
              {{ "day": "週二", "type": "輕鬆跑", "details": "40分鐘", "icon": "fa-person-running", "iconBg": "rgba(50, 215, 75, 0.2)" }},
              {{ "day": "週日", "type": "長距離", "details": "LSD 12公里", "icon": "fa-route", "iconBg": "rgba(255, 159, 10, 0.2)" }}
            ]
          }}
        ]
        注意：一週七天都要有，陣列裡要有 2 週。icon與iconBg請依照上面範例的顏色配對。"""
        # ✅ 正確的網址
        gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"

        response = requests.post(
            gemini_url,
            json={"contents": [{"parts": [{"text": prompt}]}]},
            headers={"Content-Type": "application/json"},
        )

        if response.status_code != 200:
            return jsonify({"error": f"Google API 錯誤: {response.text}"}), 400

        res_json = response.json()
        raw_text = res_json["candidates"][0]["content"]["parts"][0]["text"].strip()

        json_start = raw_text.find("[")
        json_end = raw_text.rfind("]")
        if json_start != -1 and json_end != -1:
            raw_text = raw_text[json_start : json_end + 1]

        return raw_text, 200, {"Content-Type": "application/json"}

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(debug=True, port=5000)