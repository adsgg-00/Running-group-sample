from flask import Flask, jsonify, request
from flask_cors import CORS
import requests

app = Flask(__name__)
CORS(app) # 解除前端與後端溝通的跨網域 CORS 限制

# 測試路由：確認伺服器有活著
@app.route('/', methods=['GET'])
def home():
    return jsonify({
        "status": "success",
        "message": "歡迎來到 R - 跑者儀表板的後端伺服器！",
        "version": "1.0"
    })

# 未來用來接收前端請求並向外串接的中轉 API
@app.route('/api/test-data', methods=['GET'])
def get_test_data():
    return jsonify({
        "message": "這是一筆來自 Python Flask 後端安全傳遞的測試數據！",
        "system_status": "OK"
    })

if __name__ == '__main__':
    # debug=True 讓你在改 Python 程式碼時，伺服器會自動重啟
    app.run(debug=True, port=5000)