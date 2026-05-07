# services/news_service.py
import logging
import asyncio
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET

logger = logging.getLogger("NewsService")

class NewsService:
    @staticmethod
    async def fetch_watchlist_news(symbols: list) -> str:

        if not symbols:
            return "目前自選股清單為空。"

        logger.info(f"📡 開始從 Google News 抓取自選股動態: {symbols}")
        news_summary = ""

        try:
            for sym in symbols:
                clean_sym = sym.replace('.TW', '').replace('.TWO', '')

                query = urllib.parse.quote(f"{clean_sym} 股票 OR 營收 OR 法說會")
                url = f"https://news.google.com/rss/search?q={query}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"

                # 使用 asyncio.to_thread 避免網路請求卡死機器人
                def fetch_rss():
                    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                    with urllib.request.urlopen(req, timeout=5) as response:
                        return response.read()

                xml_data = await asyncio.to_thread(fetch_rss)
                root = ET.fromstring(xml_data)

                news_summary += f"\n【🎯 標的 {clean_sym} 近期動態追蹤】\n"

                # 抓取前 3 則最新新聞標題與發布時間
                items = root.findall('./channel/item')[:3]
                if items:
                    for item in items:
                        title = item.find('title').text
                        pub_date = item.find('pubDate').text
                        # 簡單清理時間格式，保留日期即可
                        short_date = pub_date[5:16] if pub_date else ""
                        news_summary += f"- ({short_date}) {title}\n"
                else:
                    news_summary += "- 近日市場無特定新聞，請關注技術面價量變化。\n"

            return news_summary

        except Exception as e:
            logger.error(f"Google 新聞抓取失敗: {e}")
            return "抓取新聞資料時發生網路錯誤。"