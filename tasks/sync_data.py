# tasks/sync_data.py
import pandas as pd
import requests
import asyncio
import logging

from FinMind.data import DataLoader
from sqlalchemy import select
from database.session import AsyncSessionLocal
from database.models import Ticker

# 假設您有一個 DataLoader 模組，這裡先模擬 import
# from your_module import DataLoader

logger = logging.getLogger("SyncTask")


def fetch_us_symbols_sync() -> list[dict]:
    import io
    headers = {"User-Agent": "Mozilla/5.0"}

    # 標準美股代號格式：1-5 個大寫字母，允許 .A/.B 這類股份類別後綴
    SYMBOL_PATTERN = r"^[A-Z]{1,5}(\.[A-Z])?$"

    # NASDAQ 上市股票
    nasdaq_raw = requests.get(
        "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
        headers=headers, timeout=15
    ).text
    nasdaq_df = pd.read_csv(io.StringIO(nasdaq_raw), sep="|")
    nasdaq_df = nasdaq_df[
        (nasdaq_df["Test Issue"] == "N") &
        nasdaq_df["Symbol"].str.match(SYMBOL_PATTERN, na=False)
    ][["Symbol", "Security Name"]].rename(columns={"Security Name": "Name"})

    # NYSE / AMEX / BATS 等其他交易所
    other_raw = requests.get(
        "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",
        headers=headers, timeout=15
    ).text
    other_df = pd.read_csv(io.StringIO(other_raw), sep="|")
    other_df = other_df[
        (other_df["Test Issue"] == "N") &
        other_df["ACT Symbol"].str.match(SYMBOL_PATTERN, na=False)
    ][["ACT Symbol", "Security Name"]].rename(columns={"ACT Symbol": "Symbol", "Security Name": "Name"})

    combined = (
        pd.concat([nasdaq_df, other_df])
        .drop_duplicates(subset="Symbol")
        .reset_index(drop=True)
    )

    logger.info(f"✅ 共取得 {len(combined)} 筆美股標的 (NASDAQ + NYSE/AMEX/BATS)")

    return [
        {"symbol": row["Symbol"].replace(".", "-"), "name": row["Name"], "market": "US"}
        for row in combined.to_dict("records")
    ]


async def sync_us_symbols():
    logger.info("🚀 [US Sync] 開始同步美股標的...")
    try:
        # 將爬蟲丟到背景執行，不阻塞 Discord Bot
        fetched_data = await asyncio.to_thread(fetch_us_symbols_sync)

        async with AsyncSessionLocal() as session:
            # 撈取資料庫現有美股標的
            result = await session.execute(select(Ticker.symbol).where(Ticker.market == 'US'))
            existing_symbols = {row[0] for row in result.all()}

            new_objects = []
            seen = set()

            for item in fetched_data:
                sym = item['symbol']
                if sym in seen: continue
                seen.add(sym)

                if sym not in existing_symbols:
                    new_objects.append(Ticker(
                        symbol=sym,
                        name=item['name'][:100],  # 確保不超過資料表長度
                        market=item['market']
                    ))

            if new_objects:
                session.add_all(new_objects)  # Async 版本的 bulk_save
                await session.commit()
                logger.info(f"✅ 美股新增 {len(new_objects)} 筆")
            else:
                logger.info("ℹ️ 美股已是最新")

    except Exception as e:
        logger.error(f"❌ 美股同步失敗: {e}", exc_info=True)


def fetch_tw_symbols_sync() -> list[dict]:
    try:
        dl = DataLoader()
        tw_stocks = dl.taiwan_stock_info()

        if tw_stocks.empty:
            return []

        tw_stocks = tw_stocks.drop_duplicates(subset=["stock_id"])

        results = []
        for _, row in tw_stocks.iterrows():
            results.append({
                "symbol": f"{row['stock_id']}.TW",
                "name": str(row['stock_name']),
                "market": "TW"
            })
        return results
    except Exception as e:
        logger.error(f"抓取台股資料失敗: {e}")
        return []


# 非同步的資料庫寫入邏輯
async def sync_tw_symbols():
    logger.info("🚀 [TW Sync] 開始同步台股標的...")
    try:
        # 將爬蟲丟到背景執行
        fetched_data = await asyncio.to_thread(fetch_tw_symbols_sync)

        if not fetched_data:
            logger.warning("⚠️ 無法獲取台股數據，跳過同步。")
            return

        async with AsyncSessionLocal() as session:
            # 撈取資料庫現有台股標的
            result = await session.execute(select(Ticker.symbol).where(Ticker.market == 'TW'))
            existing_symbols = {row[0] for row in result.all()}

            new_objects = []

            for item in fetched_data:
                sym = item['symbol']
                if sym not in existing_symbols:
                    new_objects.append(Ticker(
                        symbol=sym,
                        name=item['name'][:100],
                        market=item['market']
                    ))

            if new_objects:
                session.add_all(new_objects)
                await session.commit()
                logger.info(f"✅ 台股新增 {len(new_objects)} 筆")
            else:
                logger.info("ℹ️ 台股已是最新狀態")

    except Exception as e:
        logger.error(f"❌ 台股同步失敗: {e}", exc_info=True)