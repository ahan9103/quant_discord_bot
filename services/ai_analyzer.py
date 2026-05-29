from google import genai
import httpx
import os
import logging
import asyncio

logger = logging.getLogger("AIAnalyzer")

_QUOTA_KEYWORDS = ("quota", "429", "resource exhausted", "rate limit", "too many requests")

# OpenRouter API endpoint（相容 OpenAI 格式）
_OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
# 預設備援模型（可於 .env 設定 OPENROUTER_MODEL 覆蓋）
_OPENROUTER_DEFAULT_MODEL = "deepseek/deepseek-v4-flash:free"


class AIAnalyzer:
    def __init__(self):
        api_key = os.getenv("GEMINI_API_KEY")
        self.is_ready = False
        self.client = None
        self.primary_model = "gemini-2.5-flash"
        self.fallback_model = "gemini-2.0-flash"

        # OpenRouter 設定
        self.openrouter_api_key = os.getenv("OPENROUTER_API_KEY")
        self.openrouter_model = os.getenv("OPENROUTER_MODEL", _OPENROUTER_DEFAULT_MODEL)
        self.openrouter_ready = bool(self.openrouter_api_key)

        if not api_key:
            logger.error("❌ 未設定 GEMINI_API_KEY")
        else:
            try:
                self.client = genai.Client(api_key=api_key)
                self.is_ready = True
                logger.info(
                    f"✅ AI 模組初始化成功，主要模型: {self.primary_model}，"
                    f"Gemini 備援: {self.fallback_model}"
                )
            except Exception as e:
                logger.error(f"❌ Gemini SDK 初始化過程發生異常: {e}")

        if self.openrouter_ready:
            logger.info(f"✅ OpenRouter 備援已就緒，模型: {self.openrouter_model}")
        else:
            logger.warning("⚠️ 未設定 OPENROUTER_API_KEY，OpenRouter 備援不可用")

    # ------------------------------------------------------------------
    # 內部：OpenRouter fallback（async，httpx）
    # ------------------------------------------------------------------
    async def _generate_openrouter(self, prompt: str) -> str:
        """透過 OpenRouter API（OpenAI 相容格式）生成文字。"""
        headers = {
            "Authorization": f"Bearer {self.openrouter_api_key}",
            "Content-Type": "application/json",
            "X-Title": "Quant Discord Bot",
        }
        payload = {
            "model": self.openrouter_model,
            "messages": [{"role": "user", "content": prompt}],
        }
        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(_OPENROUTER_API_URL, headers=headers, json=payload)
            if resp.is_error:
                # 把 OpenRouter 回傳的錯誤訊息一起記錄，方便診斷 404/模型不存在等問題
                logger.error(
                    f"❌ OpenRouter HTTP {resp.status_code}，"
                    f"模型: {self.openrouter_model}，回應: {resp.text}"
                )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]

    # ------------------------------------------------------------------
    # 內部：統一生成入口（Gemini primary → Gemini fallback → OpenRouter）
    # ------------------------------------------------------------------
    async def _generate(self, prompt: str) -> str:
        """
        呼叫順序：
          1. gemini-2.5-flash
          2. gemini-2.0-flash（Gemini quota 錯誤時）
          3. OpenRouter（兩個 Gemini 均失敗時）
        """
        # --- Gemini 嘗試 ---
        if self.client:
            for model in [self.primary_model, self.fallback_model]:
                try:
                    response = await asyncio.to_thread(
                        self.client.models.generate_content,
                        model=model,
                        contents=prompt,
                    )
                    if model == self.fallback_model:
                        logger.warning(f"⚠️ 已切換至 Gemini 備援模型 {model} 完成生成")
                    return response.text
                except Exception as e:
                    if any(k in str(e).lower() for k in _QUOTA_KEYWORDS):
                        logger.warning(f"⚠️ Gemini 模型 {model} 用量超限：{e}，嘗試下一個備援...")
                        continue
                    raise  # 非 quota 錯誤直接往上拋

        # --- OpenRouter fallback ---
        if self.openrouter_ready:
            try:
                logger.warning(
                    f"⚠️ Gemini 全部模型額度不足，切換至 OpenRouter ({self.openrouter_model})"
                )
                result = await self._generate_openrouter(prompt)
                logger.info("✅ OpenRouter 備援生成成功")
                return result
            except Exception as e:
                logger.error(f"❌ OpenRouter 備援也失敗：{e}")
                raise RuntimeError(f"所有 AI 提供者均失敗，最後錯誤：{e}") from e

        raise RuntimeError("Gemini 主／備援模型均無法使用，且未設定 OpenRouter 備援")

    # ------------------------------------------------------------------
    # 公開方法
    # ------------------------------------------------------------------
    async def generate_evening_report(self, inst_data: str, margin_data: str) -> str:
        if not self.is_ready and not self.openrouter_ready:
            return "❌ AI 模組尚未初始化。"

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
            return await self._generate(prompt)
        except Exception as e:
            logger.error(f"籌碼晚報生成失敗: {e}")
            return f"籌碼報告生成失敗: {e}"

    async def generate_market_report(self, top_value_data: str, surge_volume_data: str) -> str:
        if not self.is_ready and not self.openrouter_ready:
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
            return await self._generate(prompt)
        except Exception as e:
            logger.error(f"市場報告生成失敗: {e}")
            return f"生成報告時發生錯誤: {e}"

    async def analyze_news_sentiment(self, news_text: str) -> str:
        if not self.is_ready and not self.openrouter_ready:
            return "⚠️ AI 分析模組目前離線。"

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
            return await self._generate(prompt)
        except Exception as e:
            logger.error(f"新聞語意分析失敗: {e}")
            return f"❌ 新聞分析生成失敗: {str(e)}"


ai_analyzer = AIAnalyzer()
