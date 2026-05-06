# 使用輕量級的 Python 3.11 作為基底
FROM python:3.11-slim

# 設定容器內的工作目錄
WORKDIR /app

# 安裝編譯時需要的系統套件 (處理一些資料庫或科學運算套件會用到)
RUN apt-get update && apt-get install -y gcc libpq-dev && rm -rf /var/lib/apt/lists/*

# 複製 requirements.txt 並安裝 Python 套件
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 將專案內的所有程式碼複製到容器內
COPY . .

# 設定環境變數確保 Python 輸出不會被緩衝，方便在終端機看 log
ENV PYTHONUNBUFFERED=1

# 啟動機器人的指令
CMD ["python", "bot/main.py"]