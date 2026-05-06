# database/session.py
import os
from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from database.models import Base

load_dotenv()

# 讀取 .env 中的資料庫連線字串
DATABASE_URL = os.getenv("DATABASE_URL")

# 建立非同步資料庫引擎 (echo=True 可以在終端機印出 SQL 語句，方便開發期除錯)
engine = create_async_engine(DATABASE_URL, echo=True)

# 建立 Session 工廠，供後續操作資料庫時產生 session
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    expire_on_commit=False
)

async def init_db():
    """初始化資料庫，如果資料表不存在則自動建立"""
    async with engine.begin() as conn:
        # 執行建立資料表的 SQL 指令
        await conn.run_sync(Base.metadata.create_all)