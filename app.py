import os
from dotenv import load_dotenv
from flask import Flask, jsonify

# 載入 .env 檔案中的環境變數
load_dotenv()

# 建立 Flask 應用程式實例
app = Flask(__name__)

# 取得 GEMINI_API_KEY
gemini_api_key = os.getenv("GEMINI_API_KEY")

@app.route("/")
def index():
    return "Flask 伺服器已啟動！"

@app.route("/api/status")
def api_status():
    if gemini_api_key:
        return jsonify({"status": "success", "message": "成功載入 GEMINI API 金鑰"})
    return jsonify({"status": "error", "message": "錯誤：找不到 GEMINI API 金鑰"})

if __name__ == "__main__":
    # 執行 Flask 應用程式，並在 127.0.0.1 的 5000 port 上運行
    # debug=True 會在程式碼變更時自動重啟伺服器，方便開發
    app.run(host="127.0.0.1", port=5000, debug=True)