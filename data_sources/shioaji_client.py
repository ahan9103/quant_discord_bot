# data_sources/shioaji_client.py
import shioaji as sj
import os
import logging
from datetime import datetime
import asyncio

logger = logging.getLogger("ShioajiClient")


class ShioajiManager:
    def __init__(self):
        # 建議開發與測試期間使用 simulation=True 避免誤觸實單
        self.api = sj.Shioaji(simulation=True)
        self.is_logged_in = False

        try:
            self.loop = asyncio.get_running_loop()
        except RuntimeError:
            self.loop = None

        self.quote_queue = asyncio.Queue()

    def login(self):
        if self.is_logged_in:
            return

        api_key = os.getenv("SHIOAJI_API_KEY")
        secret_key = os.getenv("SHIOAJI_SECRET_KEY")

        if not api_key or not secret_key:
            logger.error("❌ 找不到 Shioaji API 金鑰！")
            return

        try:
            logger.info("⏳ 正在登入 Shioaji API...")
            self.api.login(
                api_key=api_key,
                secret_key=secret_key,
                contracts_cb=lambda security_type: logger.info(f"✅ {security_type} 契約下載完成")
            )
            self.is_logged_in = True
            logger.info("✅ Shioaji 登入成功！")
        except Exception as e:
            logger.error(f"❌ Shioaji 登入失敗: {e}")

    def get_stock_snapshot(self, symbol: str) -> dict:
        """
        獲取單檔股票的最新快照 (Snapshot)
        包含開高低收、成交量等資訊，盤中為即時，盤後為收盤資料
        """
        if not self.is_logged_in:
            return {"error": "API 未登入"}
        sj_symbol = symbol.replace(".TW", "").replace(".TWO", "")

        try:
            # 取得合約物件
            contract = self.api.Contracts.Stocks[sj_symbol]
            if not contract:
                return {"error": f"找不到合約: {symbol}"}

            # 抓取快照
            snapshots = self.api.snapshots([contract])
            if not snapshots:
                return {"error": "無法獲取快照資料"}

            snap = snapshots[0]

            raw_time = getattr(snap, 'update_time', getattr(snap, 'time', getattr(snap, 'ts', None)))

            if isinstance(raw_time, int):
                # 如果位數大於 15，判定為奈秒 (Nanoseconds) 或微秒
                if raw_time > 1e15:
                    dt_obj = datetime.fromtimestamp(raw_time / 1e9)
                # 否則判定為一般的秒數或毫秒
                elif raw_time > 1e11:
                    dt_obj = datetime.fromtimestamp(raw_time / 1e3)
                else:
                    dt_obj = datetime.fromtimestamp(raw_time)

                update_time_str = dt_obj.strftime('%Y-%m-%d %H:%M:%S')

            elif isinstance(raw_time, datetime):
                # 如果 API 已經佛心地轉好 datetime 物件，直接格式化
                update_time_str = raw_time.strftime('%Y-%m-%d %H:%M:%S')

            else:
                # 都抓不到的最後防線
                update_time_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

            # 將資料整理成乾淨的字典回傳
            return {
                "symbol": symbol,
                "name": contract.name,
                "open": snap.open,
                "high": snap.high,
                "low": snap.low,
                "close": snap.close,
                "volume": snap.total_volume,
                "change_price": snap.change_price,
                "change_rate": snap.change_rate,
                "update_time": update_time_str
            }
        except Exception as e:
            logger.error(f"快照抓取發生錯誤 [{symbol}]: {e}")
            return {"error": str(e)}

    def quote_callback(self, exchange: sj.Exchange, tick: sj.TickSTKv1):

        quote_data = {
            "symbol": tick.code,
            "price": tick.close,
            "volume": tick.volume,
            "time": tick.datetime
        }

        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.quote_queue.put_nowait, quote_data)

    def subscribe_quotes(self, symbols: list[str]):
        """訂閱指定的股票代號"""
        if not self.is_logged_in:
            logger.error("API 未登入，無法訂閱行情")
            return

        # 設定回呼函式
        self.api.quote.set_on_tick_stk_v1_callback(self.quote_callback)

        for sym in symbols:
            sj_symbol = sym.replace(".TW", "").replace(".TWO", "")
            contract = self.api.Contracts.Stocks[sj_symbol]
            if contract:
                logger.info(f"📡 正在訂閱 {sym} 的即時 Tick...")
                self.api.quote.subscribe(
                    contract,
                    quote_type=sj.constant.QuoteType.Tick,
                    version=sj.constant.QuoteVersion.v1
                )

# 建立一個全域實例供整個專案使用 (Singleton 概念)
sj_manager = ShioajiManager()