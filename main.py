import requests
import time
import pandas as pd
import ta
import threading
from flask import Flask

# 텔레그램 설정
TOKEN = '7971519272:AAHjBO9Dnc2e-cc5uqQbalHy3bi0kPSAfNw'
CHAT_ID = '6786843744'

# 웹 서버 생성
app = Flask(__name__)

@app.route('/')
def home():
    return '✅ OKX 감시 봇 작동 중!'

# 텔레그램 알림 함수
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": message}
    try:
        requests.post(url, data=data)
    except Exception as e:
        print("❌ 텔레그램 전송 실패:", e)

# 심볼 가져오기
def get_all_swap_symbols():
    url = "https://www.okx.com/api/v5/public/instruments?instType=SWAP"
    try:
        res = requests.get(url)
        if res.status_code != 200:
            print("심볼 조회 실패", res.status_code)
            return []
        data = res.json().get('data', [])
        symbols = [item['instId'] for item in data if item['settleCcy'] == 'USDT']
        print(f"총 {len(symbols)}개 심볼 조회됨")
        return symbols
    except Exception as e:
        print("❌ 심볼 조회 예외:", e)
        return []

# 캔들 데이터 가져오기
def get_ohlcv(symbol, interval='15m', limit=100):
    url = f'https://www.okx.com/api/v5/market/candles?instId={symbol}&bar={interval}&limit={limit}'
    try:
        res = requests.get(url)
        raw = res.json().get('data', [])
        if not raw:
            return None
        df = pd.DataFrame(raw, columns=[
            'timestamp', 'open', 'high', 'low', 'close', 'volume', 'volumeCcy'
        ])
        df = df.astype({
            'open': float, 'high': float, 'low': float,
            'close': float, 'volume': float, 'volumeCcy': float
        })
        df = df.iloc[::-1].reset_index(drop=True)
        return df
    except Exception as e:
        print(f"{symbol} 캔들 예외:", e)
        return None

# 중복 알림 제어
last_alert_time = {}
ALERT_INTERVAL = 300  # 5분

def check_15m_conditions(symbol):
    df = get_ohlcv(symbol)
    if df is None or len(df) < 30:
        return

    close = df['close']
    rsi = ta.momentum.RSIIndicator(close, window=14).rsi().iloc[-1]
    bb = ta.volatility.BollingerBands(close=close, window=30, window_dev=3)
    upper = bb.bollinger_hband().iloc[-1]
    lower = bb.bollinger_lband().iloc[-1]
    last_close = close.iloc[-1]
    prev_close = close.iloc[-2]
    change_percent = (last_close - prev_close) / prev_close * 100

    now = time.time()
    key_up = (symbol, 'upper')
    key_low = (symbol, 'lower')

    if rsi >= 70 and last_close > upper:
        if key_up not in last_alert_time or now - last_alert_time[key_up] > ALERT_INTERVAL:
            msg = (
                f"🔺🔺🔺 {symbol}\n"
                f"RSI: {rsi:.2f}\n"
                f"변화율: +{change_percent:.2f}%"
            )
            send_telegram(msg)
            print(msg)
            last_alert_time[key_up] = now

    elif rsi <= 30 and last_close < lower:
        if key_low not in last_alert_time or now - last_alert_time[key_low] > ALERT_INTERVAL:
            msg = (
                f"🔽🔽🔽 {symbol}\n"
                f"RSI: {rsi:.2f}\n"
                f"변화율: {change_percent:.2f}%"
            )
            send_telegram(msg)
            print(msg)
            last_alert_time[key_low] = now

# 봇 루프
def run_bot():
    symbols = get_all_swap_symbols()
    if not symbols:
        print("감시할 심볼이 없습니다.")
        return

    send_telegram(f"✅ OKX 감시 봇 시작됨 ({len(symbols)}종목)")
    print(f"감시 시작: {len(symbols)}개 종목")

    while True:
        start = time.time()
        for symbol in symbols:
            try:
                check_15m_conditions(symbol)
            except Exception as e:
                print(f"{symbol} 조건 체크 오류:", e)
            time.sleep(0.2)
        elapsed = time.time() - start
        sleep_time = max(0, 120 - elapsed)
        time.sleep(sleep_time)

# Flask 시작 시 백그라운드에서 봇 실행
def start_background_thread():
    t = threading.Thread(target=run_bot)
    t.daemon = True
    t.start()

if __name__ == '__main__':
    start_background_thread()
    app.run(host='0.0.0.0', port=10000)
