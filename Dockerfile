FROM python:3.11-slim

WORKDIR /app

# システム依存関係（PyMuPDF の mupdf バックエンド用）
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1-mesa-glx \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Python 依存関係
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# アプリケーションコード
COPY app.py .

# Streamlit 設定
ENV STREAMLIT_SERVER_PORT=8501
ENV STREAMLIT_SERVER_HEADLESS=true
ENV STREAMLIT_SERVER_ENABLE_CORS=false

EXPOSE 8501

CMD ["streamlit", "run", "app.py"]
