import yfinance as yf
import logging

logger = logging.getLogger("YFinanceClient")


def fetch_stock_info(symbol: str) -> dict | None:
    try:
        info = yf.Ticker(symbol).info
        if 'shortName' not in info and 'longName' not in info:
            return None
        name = info.get('shortName') or info.get('longName')
        market = 'TW' if symbol.endswith(('.TW', '.TWO')) else 'US'
        return {"name": name, "market": market}
    except Exception as e:
        logger.error(f"yfinance 查詢錯誤 ({symbol}): {e}")
        return None


def fetch_current_prices(symbols: list[str]) -> dict:
    if not symbols:
        return {}
    try:
        data = yf.download(symbols, period="1d", progress=False)
        if len(symbols) == 1:
            price = data['Close'].iloc[-1].item()
            return {symbols[0]: price}
        prices = data['Close'].iloc[-1].to_dict()
        return prices
    except Exception as e:
        logger.error(f"批次報價抓取失敗: {e}")
        return {}
