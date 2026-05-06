# services/ai_analyzer.py
import google.generativeai as genai
import os
import logging
import asyncio
import pandas as pd

from data_sources.shioaji_client import sj_manager

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
        # 🛡️ 防禦性編程：確保 client 存在才執行
        if not self.is_ready or self.client is None:
            logger.error("試圖在 AI 模組未就緒時生成報告")
            return "⚠️ AI 分析模組目前離線，請檢查系統環境變數設定。"
        """
        將盤後數據交給 Gemini 進行族群分類與多頭訊號分析
        """
        if not self.is_ready:
            return "❌ AI 模組尚未初始化，請檢查 API KEY。"

        # 💡 [面試亮點] Prompt Engineering (提示詞工程)
        # 明確定義 LLM 的角色、輸入資料格式與嚴格的輸出格式
        prompt = f"""
                你是一位精通台灣股市「籌碼面分析」的量化交易員。請根據以下數據撰寫晚間總結報告。
                絕對不要輸出問候語，直接從 ## 標題開始。

                【法人籌碼】
                {inst_data}

                【融資動向】
                {margin_data}

                請包含：## 🏢 三大法人資金佈局 (外資/投信)、## ⚠️ 融資劇增警示與觀察、## 💡 明日開盤量化策略沙盤推演
                """

        try:
            # 由於 Gemini API 呼叫是同步的 IO，我們把它丟到背景執行
            response = await asyncio.to_thread(
                self.client.models.generate_content,
                model=self.model_name,
                contents=prompt
            )
            return response.text
        except Exception as e:
            logger.error(f"Gemini API 請求失敗: {e}")
            return f"❌ 報告生成失敗，錯誤訊息: {str(e)}"


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

# 建立單例供外部使用
ai_analyzer = AIAnalyzer()