import os
from dotenv import load_dotenv
from google import genai

# 載入 .env 裡的金鑰
load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# 建立連線
client = genai.Client(api_key=GEMINI_API_KEY)

print("🔍 正在向 Google 查詢您專屬的可用模型清單...\n")

try:
    # 列出所有你可以用的模型
    for model in client.models.list():
        # 我們只印出跟 gemini 有關的模型，過濾掉不重要的
        if "gemini" in model.name:
            print(f"✅ 可用模型名稱: {model.name}")
            
    print("\n💡 請從上方挑選一個名稱（例如 gemini-2.0-flash 或 gemini-pro），貼回 app.py 中！")
    
except Exception as e:
    print(f"查詢失敗: {e}")