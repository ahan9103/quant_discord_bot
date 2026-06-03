# database/models.py
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy import String, Float, DateTime, Date, ForeignKey, BigInteger, Boolean, Column, Integer
from sqlalchemy.dialects.postgresql import JSONB
from datetime import datetime, date
from typing import List, Optional
from sqlalchemy.sql import func

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

class AlphaScan(Base):
    """每次 Alpha 因子掃描的快照紀錄"""
    __tablename__ = 'alpha_scans'

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    scan_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    scan_time: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    factors_hit: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)   # {factor: count}
    pool_size: Mapped[int] = mapped_column(Integer, default=0)

    entries: Mapped[List["AlphaPoolEntry"]] = relationship(
        back_populates="scan", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<AlphaScan(date={self.scan_date}, pool={self.pool_size})>"


class AlphaPoolEntry(Base):
    """Alpha 標的池中每檔個股的歷史紀錄（供走勢估算）"""
    __tablename__ = 'alpha_pool_entries'

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    scan_id: Mapped[int] = mapped_column(
        ForeignKey('alpha_scans.id', ondelete="CASCADE"), nullable=False
    )
    scan_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    code: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    factors: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    change_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    volume: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    close_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    scan: Mapped["AlphaScan"] = relationship(back_populates="entries")

    def __repr__(self) -> str:
        return f"<AlphaPoolEntry(date={self.scan_date}, code={self.code}, score={self.score})>"


class Channel(Base):
    __tablename__ = "monitored_channels"

    id = Column(Integer, primary_key=True, autoincrement=True)
    channel_url = Column(String, unique=True, nullable=False)
    channel_name = Column(String, nullable=True)
    category = Column(String, default="MARKET") # MARKET 或 STOCK
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())



    def __repr__(self) -> str:
        return f"<UserStrategy(user_id={self.user_id}, symbol='{self.symbol}', strategy='{self.strategy_name}')>"
    

