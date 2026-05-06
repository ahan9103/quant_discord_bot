# core/database.py
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import declarative_base
from core.config import DATABASE_URL

# 建立非同步引擎 (echo=False 避免在終端機印出過多 SQL 語句)
engine = create_async_engine(DATABASE_URL, echo=False)

# 建立 Session 工廠
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False
)

# 定義 ORM 的基礎類別，後續的 Table 都會繼承它
Base = declarative_base()

# 取得資料庫 Session 的依賴函數
async def get_session() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session