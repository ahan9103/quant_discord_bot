# database/models.py
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy import String, Float, DateTime, ForeignKey, BigInteger, Boolean
from sqlalchemy.dialects.postgresql import JSONB  # [面試亮點] 針對 PostgreSQL 的 JSONB 型態
from datetime import datetime
from typing import List, Optional


# 建立宣告式基礎類別
class Base(DeclarativeBase):
    pass


class User(Base):
    """使用者與基礎設定表"""
    __tablename__ = 'users'

    # Discord ID 作為主鍵
    discord_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    is_premium: Mapped[bool] = mapped_column(Boolean, default=False)
    # 儲存使用者偏好 (例如：通知開關、預設市場)
    preferences: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    # 關聯設定 (ORM 雙向對應)
    watchlists: Mapped[List["Watchlist"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    strategies: Mapped[List["UserStrategy"]] = relationship(back_populates="user", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<User(discord_id={self.discord_id})>"


class Ticker(Base):
    """股票標的字典表"""
    __tablename__ = 'tickers'

    # 股票代號作為主鍵 (例如: '2330.TW', 'AAPL')
    symbol: Mapped[str] = mapped_column(String(50), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    # 市場別 (例如: 'TW', 'US')
    market: Mapped[str] = mapped_column(String(10), nullable=False)
    asset_type: Mapped[str] = mapped_column(String(50), default='Equity')

    # 關聯設定
    watchlists: Mapped[List["Watchlist"]] = relationship(back_populates="ticker", cascade="all, delete-orphan")
    strategies: Mapped[List["UserStrategy"]] = relationship(back_populates="ticker", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<Ticker(symbol='{self.symbol}', name='{self.name}', market='{self.market}')>"


class Watchlist(Base):
    """使用者追蹤清單表"""
    __tablename__ = 'user_watchlist'

    # 流水號主鍵
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # 外鍵關聯
    user_id: Mapped[int] = mapped_column(ForeignKey('users.discord_id', ondelete="CASCADE"), nullable=False)
    symbol: Mapped[str] = mapped_column(ForeignKey('tickers.symbol', ondelete="CASCADE"), nullable=False)

    # 價格追蹤設定
    cost_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    target_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 目標停利價
    stop_loss: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 停損價

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # 關聯設定
    user: Mapped["User"] = relationship(back_populates="watchlists")
    ticker: Mapped["Ticker"] = relationship(back_populates="watchlists")

    def __repr__(self) -> str:
        return f"<Watchlist(user_id={self.user_id}, symbol='{self.symbol}', cost={self.cost_price})>"


class UserStrategy(Base):
    """量化策略訂閱表"""
    __tablename__ = 'user_strategies'

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # 外鍵關聯
    user_id: Mapped[int] = mapped_column(ForeignKey('users.discord_id', ondelete="CASCADE"), nullable=False)
    symbol: Mapped[str] = mapped_column(ForeignKey('tickers.symbol', ondelete="CASCADE"), nullable=False)

    # 策略名稱 (例如: 'MACD_Cross', 'RSI_Oversold')
    strategy_name: Mapped[str] = mapped_column(String(50), nullable=False)

    # [面試重點] 策略參數：使用 JSONB 確保未來擴充任何策略都不用修改 Table Schema
    # 例如存入: {"fast_period": 12, "slow_period": 26, "signal_period": 9}
    parameters: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # 關聯設定
    user: Mapped["User"] = relationship(back_populates="strategies")
    ticker: Mapped["Ticker"] = relationship(back_populates="strategies")

    def __repr__(self) -> str:
        return f"<UserStrategy(user_id={self.user_id}, symbol='{self.symbol}', strategy='{self.strategy_name}')>"