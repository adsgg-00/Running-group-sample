from flask import Flask, jsonify, request
from flask_cors import CORS
import requests

app = Flask(__name__)
CORS(app)  # 解除跨網域 CORS 限制

# 🚨 安全鎖：將你的真實 Gemini 金鑰安全的鎖在 Python 後端中 🚨
GEMINI_API_KEY = "這裡請精準貼上你申請到以AIzaSy開頭的真實金鑰"


@app.route("/", methods=["GET"])
def home():
    return jsonify({"status": "success", "message": "後端伺服器運作中！"})


# 🚨 新增：Gemini 智慧中轉路由 🚨
@app.route("/api/generate-plan", methods=["POST"])
def generate_plan():
    # 接收來自前端網頁的參數
    req_data = request.get_json()
    goal_race = req_data.get("goalRace")
    goal_time = req_data.get("goalTime")
    selected_days = req_data.get("selectedDays")

    # 構建給 AI 的 Prompt
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
          {{ "day": "週二", "type": "輕鬆跑", "details": "40分鐘，心率 Z2 區間", "icon": "fa-person-running", "iconBg": "rgba(50, 215, 75, 0.2)" }},
          {{ "day": "週三", "type": "間歇跑", "details": "800m x 4趟", "icon": "fa-stopwatch", "iconBg": "rgba(191, 90, 242, 0.2)" }},
          {{ "day": "週四", "type": "休息", "details": "恢復日", "icon": "fa-bed", "iconBg": "rgba(142, 142, 147, 0.2)" }},
          {{ "day": "週五", "type": "輕鬆跑", "details": "30分鐘", "icon": "fa-person-running", "iconBg": "rgba(50, 215, 75, 0.2)" }},
          {{ "day": "週六", "type": "休息", "details": "恢復日", "icon": "fa-bed", "iconBg": "rgba(142, 142, 147, 0.2)" }},
          {{ "day": "週日", "type": "長距離", "details": "LSD 12公里", "icon": "fa-route", "iconBg": "rgba(255, 159, 10, 0.2)" }}
        ]
      }}
    ]
    注意：一週七天都要有，陣列裡要有 2 週。icon與iconBg請依照上面範例的顏色配對。"""

    try:
        # 由 Python 後端發送請求給 Google 伺服器
        gemini_url = f"[https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key=](https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key=){GEMINI_API_KEY}"
        response = requests.post(
            gemini_url,
            json={"contents": [{"parts": [{"text": prompt}]}]},
            headers={"Content-Type": "application/json"},
        )

        if response.status_code != 200:
            return (
                jsonify(
                    {
                        "error": f"Google API 回傳錯誤碼: {response.status_code}"
                    }
                ),
                400,
            )

        res_json = response.json()
        raw_text = res_json["candidates"][0]["content"]["parts"][0]["text"].strip()

        # JSON 強固裁剪器
        json_start = raw_text.indexOf("[") if hasattr(raw_text, "indexOf") else raw_text.find("[")
        json_end = raw_text.lastIndexOf("]") if hasattr(raw_text, "lastIndexOf") else raw_text.rfind("]")
        if json_start != -1 and json_end != -1:
            raw_text = raw_text[json_start : json_end + 1]

        return raw_text, 200, {"Content-Type": "application/json"}

    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5000)