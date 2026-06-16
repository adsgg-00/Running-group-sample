import os
from flask import Flask, jsonify, request, g
from flask_cors import CORS
from dotenv import load_dotenv
import sqlite3
import requests
import traceback

# 🚨 引入 Google 最新版的 GenAI 套件
from groq import Groq

# 載入 .env
load_dotenv()
GROQ_API_KEY = os.getenv("GROQ_API_KEY")                   # 👈 改讀 GROQ_API_KEY
STRAVA_CLIENT_ID = os.getenv("STRAVA_CLIENT_ID")           
STRAVA_CLIENT_SECRET = os.getenv("STRAVA_CLIENT_SECRET")

print(f"DEBUG: 系統讀到的 Groq 金鑰是 -> {GROQ_API_KEY}")

app = Flask(__name__)
CORS(app)  # 解除跨網域 CORS 限制

DATABASE = 'running.db'

# ==========================================
# 🚨 資料庫初始化
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
    with app.app_context():
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS activities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT, date TEXT, dist TEXT, time TEXT,
                pace TEXT, hr TEXT, type TEXT, source TEXT, polyline TEXT
            )
        ''')
        conn.commit()

init_db()

# ==========================================
# 🚨 API 路由設定
# ==========================================
@app.route('/api/test-data', methods=['GET'])
def get_test_data():
    return jsonify({"message": "這是一筆來自 Python Flask 後端安全傳遞的測試數據！", "system_status": "OK"})

@app.route("/api/activities", methods=["GET"])
def get_activities():
    cursor = get_db().cursor()
    cursor.execute('SELECT * FROM activities ORDER BY id DESC')
    rows = cursor.fetchall()
    return jsonify([dict(row) for row in rows]), 200

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

# ==========================================
# 🚨 Gemini AI 課表生成中轉站 (升級使用最新版 google-genai)
# ==========================================
@app.route("/api/generate-plan", methods=["POST"])
def generate_plan():
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

# 🚨 使用 Groq 客戶端呼叫 Llama 3 模型
        client = Groq(api_key=GROQ_API_KEY)
        completion = client.chat.completions.create(
            model="llama3-8b-8192",  # 使用 Llama 3 8B 模型，速度極快
            messages=[
                {"role": "user", "content": prompt}
            ],
            temperature=0.2, # 降低隨機性，確保輸出的 JSON 格式穩定
        )

        raw_text = completion.choices[0].message.content.strip()

        # 擷取 JSON (保留原本的完美邏輯)
        json_start = raw_text.find("[")
        json_end = raw_text.rfind("]")
        if json_start != -1 and json_end != -1:
            raw_text = raw_text[json_start : json_end + 1]

        return raw_text, 200, {"Content-Type": "application/json"}

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

# ==========================================
# 🚨 Strava API 授權與同步中轉站
# ==========================================

# 1. 取得 Strava 登入網址
@app.route('/api/strava/auth', methods=['GET'])
def strava_auth():
    # 這裡的 redirect_uri 必須與你在 Strava 後台設定的 Callback Domain 完全一致
    # 假設你是用 Live Server 開啟 challenge.html，Port 通常是 5500
    redirect_uri = "http://127.0.0.1:5500/challenge.html"
    auth_url = f"https://www.strava.com/oauth/mobile/authorize?client_id={STRAVA_CLIENT_ID}&response_type=code&redirect_uri={redirect_uri}&approval_prompt=force&scope=activity:read_all"
    return jsonify({"auth_url": auth_url})

# 2. 拿驗證碼 (Code) 去換取真正的通行證 (Token)
@app.route('/api/strava/callback', methods=['POST'])
def strava_callback():
    data = request.get_json()
    code = data.get('code')
    
    res = requests.post("https://www.strava.com/oauth/token", data={
        'client_id': STRAVA_CLIENT_ID,
        'client_secret': STRAVA_CLIENT_SECRET,
        'code': code,
        'grant_type': 'authorization_code'
    })
    return jsonify(res.json()), res.status_code

# 3. 當通行證過期時，用 Refresh Token 換新的
@app.route('/api/strava/refresh', methods=['POST'])
def strava_refresh():
    data = request.get_json()
    refresh_token = data.get('refresh_token')
    
    res = requests.post("https://www.strava.com/oauth/token", data={
        'client_id': STRAVA_CLIENT_ID,
        'client_secret': STRAVA_CLIENT_SECRET,
        'grant_type': 'refresh_token',
        'refresh_token': refresh_token
    })
    return jsonify(res.json()), res.status_code
if __name__ == "__main__":
    app.run(debug=True, port=5000)