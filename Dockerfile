FROM node:24-alpine AS admin-build
WORKDIR /admin
COPY admin-ui/package*.json ./
RUN npm ci
COPY admin-ui ./
RUN npm run build

# ---- Base Image ----
# 使用輕量的 Python 3.11 映像檔
FROM python:3.11-slim

# ---- 系統套件安裝 ----
# 安裝 Chromium (headless browser) + ChromeDriver + FFmpeg
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    chromium \
    chromium-driver \
    && rm -rf /var/lib/apt/lists/*

# ---- 容器預設設定 ----
# docker run 只需提供端口和持久化目錄；仍可用 -e 覆蓋任意值。
ENV CHROME_BIN=/usr/bin/chromium \
    CHROMEDRIVER_PATH=/usr/bin/chromedriver \
    PYTHONUNBUFFERED=1 \
    JABLE_TIMEOUT=30 \
    PUBLIC_BASE_URL=http://localhost:8000 \
    DOWNLOAD_DIR=/downloads

# ---- 工作目錄 ----
WORKDIR /app

# ---- 安裝 Python 依賴 ----
# 先複製 requirements.txt，利用 Docker 快取機制加速重建
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---- 複製專案程式碼 ----
COPY . .
COPY --from=admin-build /admin/dist /app/admin-ui/dist

# ---- 掛載點 ----
# 下载文件、上传文件和 tasks.sqlite3 使用同一个持久化目录
VOLUME ["/downloads"]
EXPOSE 8000

# 預設工作目錄切到下載資料夾
WORKDIR /downloads

# 預設啟動 API 和管理後台；命令行下載模式可顯式執行 python /app/main.py
CMD ["python", "-m", "uvicorn", "api:app", "--app-dir", "/app", "--host", "0.0.0.0", "--port", "8000"]
