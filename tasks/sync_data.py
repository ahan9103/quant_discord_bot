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


# 將會阻塞的爬蟲與 Pandas 處理獨立為一般函式
def fetch_us_symbols_sync() -> list[dict]:
    headers = {"User-Agent": "Mozilla/5.0"}
    symbols = set()

    # 1. S&P 500
    sp500_url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    res = requests.get(sp500_url, headers=headers)
    sp500 = pd.read_html(res.text)[0]
    sp500 = sp500[sp500["Symbol"].notna()]
    for _, row in sp500.iterrows():
        symbols.add((row["Symbol"], row["Security"]))

    # 2. NASDAQ
    nasdaq_url = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
    nasdaq_df = pd.read_csv(nasdaq_url, sep="|")
    nasdaq_df = nasdaq_df[nasdaq_df["Symbol"].notna()]
    nasdaq_df = nasdaq_df[nasdaq_df["Test Issue"] == "N"]
    for _, row in nasdaq_df.iterrows():
        symbols.add((row["Symbol"], row["Security Name"]))

    return [{"symbol": sym.replace(".", "-"), "name": name, "market": "US"} for sym, name in symbols]


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
        print(f"抓取台股資料失敗: {e}")
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