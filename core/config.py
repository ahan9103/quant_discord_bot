# core/config.py
import os
from dotenv import load_dotenv

# 載入 .env 檔案（如果在本地開發）
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
# 注意：SQLAlchemy 非同步需要使用 postgresql+asyncpg 驅動
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://quant_user:your_password@db:5432/quant_db")

SHIOAJI_API_KEY = os.getenv("SHIOAJI_API_KEY")
SHIOAJI_SECRET_KEY = os.getenv("SHIOAJI_SECRET_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")