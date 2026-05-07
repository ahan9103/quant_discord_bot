# services/ai_analyzer.py
from google import genai
import os
import logging
import asyncio

logger = logging.getLogger("AIAnalyzer")

class AIAnalyzer:
    def __init__(self):
        api_key = os.getenv("GEMINI_API_KEY")
        self.is_ready = False
        self.client = None
        self.model_name = 'gemini-2.5-flash'

        if not api_key:
            logger.error("❌ 未設定 GEMINI_API_KEY")
            return

        try:
            # 建立 Client 實例
            self.client = genai.Client(api_key=api_key)
            self.is_ready = True
            logger.info(f"✅ AI 模組初始化成功，模型設定為: {self.model_name}")
        except Exception as e:
            logger.error(f"❌ Gemini SDK 初始化過程發生異常: {e}")

    async def generate_evening_report(self, inst_data: str, margin_data: str) -> str:
        """生成 21:30 的籌碼與法人晚報"""
        if not self.is_ready:
            return "❌ AI 模組尚未初始化。"

        # 💡 [面試亮點] 針對籌碼面的專屬 Prompt
        prompt = f"""
        你是一位精通台灣股市「籌碼面分析」的量化交易員。請根據我提供的盤後籌碼數據，撰寫一份「台股晚間籌碼總結報告」。

        【嚴格輸出要求】
        1. 絕對不要輸出任何問候語、開場白或結語。
        2. 請直接以 `## 🏢 三大法人資金佈局 (外資/投信)` 作為第一行開始。
        3. 語氣嚴肅專業，點出籌碼集中的族群。

        【提供的籌碼數據】
        1. 法人同步買超標的 (外資與投信皆買超)：
        {inst_data}

        2. 融資異常劇增標的 (散戶動能或主力鎖碼)：
        {margin_data}

        【請根據以上數據，依序輸出以下三個章節】
        ## 🏢 三大法人資金佈局 (外資/投信)
        (分析法人資金主要進駐哪些產業？是防禦型還是攻擊型佈局？)

        ## ⚠️ 融資劇增警示與觀察
        (分析融資大增的標的。如果該標的法人也買，可能是主力利用融資鎖碼；如果法人賣，則可能是散戶接刀。請給出專業點評。)

        ## 💡 明日開盤量化策略沙盤推演
        (綜合法人與融資數據，給出明天早盤的操作建議，例如：避開哪類股、留意哪類股的拉回買點。)
        """

        try:
            response = await asyncio.to_thread(
                self.client.models.generate_content,
                model=self.model_name,
                contents=prompt
            )
            return response.text
        except Exception as e:
            logger.error(f"Gemini API 籌碼晚報生成失敗: {e}")
            return f"籌碼報告生成失敗: {e}"

    async def generate_market_report(self, top_value_data: str, surge_volume_data: str) -> str:
        if not self.is_ready:
            return "❌ AI 模組尚未初始化，請檢查 API KEY。"

        prompt = f"""
        你是一位專業的台灣股市量化分析師。請根據我提供的今日盤後數據，撰寫一份「今日台股收盤總結報告」。

        【提供的數據】
        1. 交易值前 50 大標的：
        {top_value_data}

        2. 交易量突然大增的標的：
        {surge_volume_data}

        【請根據以上數據，輸出以下格式的報告】
        ## 📊 今日資金流向與主流族群
        ## 🚀 爆量轉強潛力股觀察
        ## 💡 量化多頭訊號總結

        請使用繁體中文，語氣專業但易於閱讀，適合在 Discord 頻道中推播。
        """

        try:
            # 新版 SDK 的呼叫方式
            response = await asyncio.to_thread(
                self.client.models.generate_content,
                model=self.model_name,
                contents=prompt
            )
            return response.text
        except Exception as e:
            logger.error(f"Gemini API 呼叫失敗: {e}")
            return f"生成報告時發生錯誤: {e}"

    async def analyze_news_sentiment(self, news_text: str) -> str:
        """針對自選股新聞進行多空情緒分析"""
        if not self.is_ready or self.client is None:
            return "⚠️ AI 分析模組目前離線。"

        # 💡 [面試亮點] 嚴格規範 LLM 輸出結構，強制進行多空量化評分
        prompt = f"""
        你是一位專業的金融 NLP (自然語言處理) 分析師。請閱讀以下關於使用者「自選股」的最新新聞與事件。

        【任務要求】
        1. 排除雜訊：如果新聞內容與該公司營運無關（例如單純大盤分析提到名字），請忽略。
        2. 多空判定：請為每一檔有重大新聞的股票，給出明確的多空判定（🟢 偏多、🔴 偏空、⚪ 中性）。
        3. 簡短摘要：用一句話總結該新聞對股價的潛在影響（例如：營收創高、法說會下修展望、除息日將近等）。
        4. 絕對不要輸出任何問候語，直接從 `## 📰 自選股晨間新聞解析` 開始。

        【自選股新聞資料】
        {news_text}

        【請依照以下格式輸出】
        ## 📰 自選股晨間新聞與事件解析
        - **[股票代號/名稱]** (🟢偏多/🔴偏空/⚪中性) : [一句話影響摘要]
        """

        try:
            import asyncio
            response = await asyncio.to_thread(
                self.client.models.generate_content,
                model=self.model_name,
                contents=prompt
            )
            return response.text
        except Exception as e:
            logger.error(f"新聞語意分析失敗: {e}")
            return f"❌ 新聞分析生成失敗: {str(e)}"

# 建立單例供外部使用
ai_analyzer = AIAnalyzer()