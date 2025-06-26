import requests
import time
import threading
import pandas as pd
import ta
from flask import Flask
import os

# 텔레그램 설정
TOKEN = '7971519272:AAHjBO9Dnc2e-cc5uqQbalHy3bi0kPSAfNw'
CHAT_ID = '6786843744'

app = Flask(__name__)

@app.route('/')
def home():
    return '✅ OKX 급등 감지 봇 작동 중입니다!', 200

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": message}
    try:
        res = requests.post(url, data=data)
        if res.status_code != 200:
            print(f"❌ 텔레그램 응답 오류: {res.status_code} / {res.text}")
    except Exception as e:
        print("❌ 텔레그램 전송 실패:", e)

def get_all_swap_symbols():
    url = "https://www.okx.com/api/v5/public/instruments?instType=SWAP"
    try:
        res = requests.get(url)
        if res.status_code != 200:
            print("❌ 선물 심볼 불러오기 실패")
            return []
        data = res.json().get('data', [])
        usdt_symbols = [item['instId'] for item in data if item['settleCcy'] == 'USDT']
        return usdt_symbols
    except Exception as e:
        print("❌ 선물 심볼 요청 오류:", e)
        return []

def get_ohlcv(symbol, interval, limit=100):
    url = f'https://www.okx.com/api/v5/market/candles?instId={symbol}&bar={interval}&limit={limit}'
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        res = requests.get(url, headers=headers)
        if res.status_code != 200:
            print(f"❌ {symbol} OHLCV 요청 실패: {res.status_code}")
            return None
        raw = res.json().get('data', [])
        df = pd.DataFrame(raw, columns=[
            'timestamp', 'open', 'high', 'low', 'close', 'volume', 'volumeCcy'
        ])
        df = df.astype(float)
        df = df.iloc[::-1].reset_index(drop=True)
        return df
    except Exception as e:
        print(f"❌ {symbol} 데이터 처리 실패:", e)
        return None

def check_conditions(symbol):
    df_5m = get_ohlcv(symbol, '5m')
    if df_5m is None or len(df_5m) < 30:
        return

    close_5m = df_5m['close']
    price_change_5m = (close_5m.iloc[-1] - close_5m.iloc[-6]) / close_5m.iloc[-6] * 100
    rsi = ta.momentum.RSIIndicator(close=close_5m, window=14).rsi().iloc[-1]

    bb = ta.volatility.BollingerBands(close=close_5m, window=30, window_dev=3)
    bb_upper = bb.bollinger_hband().iloc[-1]
    last_close = close_5m.iloc[-1]

    if price_change_5m >= 1.5 and rsi > 70 and last_close > bb_upper:
        msg = f"📈 {symbol} 급등 감지 (K=3)\n" \
              f"5분봉 상승률: +{price_change_5m:.2f}%\n" \
              f"RSI: {rsi:.2f}\n" \
              f"종가: {last_close:.4f} > 볼린저 상단: {bb_upper:.4f}"
        send_telegram(msg)
        print(msg)

def run_bot():
    symbols = get_all_swap_symbols()
    if not symbols:
        send_telegram("⚠️ 감시할 USDT 종목이 없습니다.")
        return

    start_msg = f"✅ OKX USDT 선물 감시 봇 시작됨 ({len(symbols)}종목)"
    send_telegram(start_msg)
    print(start_msg)

    while True:
        for symbol in symbols:
            check_conditions(symbol)
            time.sleep(0.3)
        time.sleep(60)

if __name__ == '__main__':
    # 감지 봇 실행
    bot_thread = threading.Thread(target=run_bot)
    bot_thread.start()

    # Flask 서버 실행 (PORT 환경변수 자동 감지)
    port = int(os.environ.get('PORT', 3000))
    app.run(host="0.0.0.0", port=port)
