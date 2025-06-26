import requests
import time
import pandas as pd
import ta
from datetime import datetime
from flask import Flask
import threading

# 텔레그램 설정
TELEGRAM_TOKEN = '7971519272:AAHjBO9Dnc2e-cc5uqQbalHy3bi0kPSAfNw'
TELEGRAM_CHAT_ID = '6786843744'

# Flask 앱 (Render용)
app = Flask(__name__)

@app.route('/')
def home():
    return f'✅ OKX 급등 감지 봇 작동 중! ({datetime.utcnow()})'

# 종목 목록 가져오기 (USDT-SWAP만)
def get_usdt_swap_symbols():
    url = "https://www.okx.com/api/v5/public/instruments?instType=SWAP"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()

        if data.get("code") != "0":
            print("❌ OKX API 에러 코드:", data.get("code"))
            return []

        symbols = [item['instId'] for item in data['data'] if item['instId'].endswith("USDT-SWAP")]
        print(f"✅ 받은 USDT 종목 수: {len(symbols)}")
        return symbols

    except Exception as e:
        print(f"❌ 종목 목록 가져오기 오류: {e}")
        return []

# 캔들 데이터 가져오기
def get_candles(symbol, interval, limit=100):
    url = f"https://www.okx.com/api/v5/market/candles?instId={symbol}&bar={interval}&limit={limit}"
    try:
        response = requests.get(url, timeout=10)
        data = response.json()
        if data.get("code") != "0":
            print(f"❌ {symbol} 캔들 데이터 오류 코드: {data.get('code')}")
            return None
        df = pd.DataFrame(data['data'], columns=[
            'timestamp', 'open', 'high', 'low', 'close', 'volume', 'volume_currency',
            'buy_volume', 'sell_volume', 'candlesign'
        ])
        df = df.iloc[::-1].copy()
        df['close'] = df['close'].astype(float)
        return df
    except Exception as e:
        print(f"❌ {symbol} 캔들 오류: {e}")
        return None

# 텔레그램 메시지 전송
def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {'chat_id': TELEGRAM_CHAT_ID, 'text': text}
    try:
        requests.post(url, data=payload, timeout=10)
    except Exception as e:
        print(f"❌ 텔레그램 전송 실패: {e}")

# 조건 확인 함수
def check_conditions(df, interval):
    if df is None or len(df) < 30:
        return None

    close = df['close']
    rsi = ta.momentum.RSIIndicator(close, window=14).rsi()
    bb = ta.volatility.BollingerBands(close, window=20, window_dev=3)
    upper = bb.bollinger_hband()
    lower = bb.bollinger_lband()

    latest_close = close.iloc[-1]
    previous_close = close.iloc[-2]
    latest_rsi = rsi.iloc[-1]
    latest_upper = upper.iloc[-1]
    latest_lower = lower.iloc[-1]

    change_pct = ((latest_close - previous_close) / previous_close) * 100

    # 급등 조건
    if interval == '5m':
        if change_pct >= 1.5 and latest_rsi >= 70 and latest_close > latest_upper:
            return "📈 5분봉 급등 감지"
    elif interval == '15m':
        if latest_rsi >= 70 and latest_close > latest_upper:
            return "📈 15분봉 급등 감지"

    # 급락 조건
    if interval == '5m':
        if change_pct <= -1.5 and latest_rsi <= 30 and latest_close < latest_lower:
            return "📉 5분봉 급락 감지"
    elif interval == '15m':
        if latest_rsi <= 30 and latest_close < latest_lower:
            return "📉 15분봉 급락 감지"

    return None

# 메인 분석 루프
def monitor():
    symbols = get_usdt_swap_symbols()
    send_telegram_message(f"✅ OKX 급등 감지 봇 작동 시작됨\n총 감시 종목 수: {len(symbols)}개")

    while True:
        for symbol in symbols:
            for interval in ['5m', '15m']:
                df = get_candles(symbol, interval)
                result = check_conditions(df, interval)
                if result:
                    msg = f"{result} 발생!\n종목: {symbol}\n봉 기준: {interval}"
                    print(msg)
                    send_telegram_message(msg)
        time.sleep(30)

# 백그라운드 실행
def start_monitoring():
    thread = threading.Thread(target=monitor)
    thread.daemon = True
    thread.start()

# Render 실행
if __name__ == '__main__':
    start_monitoring()
    app.run(host='0.0.0.0', port=10000)
