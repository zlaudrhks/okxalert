import requests
import time
import pandas as pd
import ta
import threading
from flask import Flask

# 텔레그램 설정
TOKEN = '7971519272:AAHjBO9Dnc2e-cc5uqQbalHy3bi0kPSAfNw'
CHAT_ID = '6786843744'

# Flask 앱 생성
app = Flask(__name__)

@app.route('/')
def home():
    return '✅ OKX 감시 봇 작동 중!'

# 텔레그램 알림
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": message}
    try:
        res = requests.post(url, data=data)
        if res.status_code == 200:
            print("✅ 텔레그램 전송 성공:", message[:40])
        else:
            print(f"❌ 텔레그램 전송 실패: 상태코드 {res.status_code}")
    except Exception as e:
        print("❌ 텔레그램 전송 예외:", e)

# OKX 선물 심볼 조회
def get_all_swap_symbols():
    url = "https://www.okx.com/api/v5/public/instruments?instType=SWAP"
    try:
        res = requests.get(url)
        print("📡 OKX 심볼 응답코드:", res.status_code)  # ✅ 응답코드 출력 추가
        if res.status_code != 200:
            print("❌ 심볼 응답 실패, 빈 목록 반환")
            return []
        data = res.json().get('data', [])
        symbols = [item['instId'] for item in data if item['settleCcy'] == 'USDT']
        print(f"📈 총 {len(symbols)}개 심볼 조회됨")
        return symbols
    except Exception as e:
        print("❌ 심볼 조회 예외:", e)
        return []

# 캔들 데이터
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
        df = df.astype(float).iloc[::-1].reset_index(drop=True)
        return df
    except Exception as e:
        print(f"❌ {symbol} 캔들 예외:", e)
        return None

# 중복 알림 방지
last_alert_time = {}
ALERT_INTERVAL = 300  # 5분

# 조건 체크
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
            last_alert_time[key_up] = now

    elif rsi <= 30 and last_close < lower:
        if key_low not in last_alert_time or now - last_alert_time[key_low] > ALERT_INTERVAL:
            msg = (
                f"🔽🔽🔽 {symbol}\n"
                f"RSI: {rsi:.2f}\n"
                f"변화율: {change_percent:.2f}%"
            )
            send_telegram(msg)
            last_alert_time[key_low] = now

# 감시 봇 실행
def run_bot():
    print("▶️ run_bot() 감시 봇 시작됨")
    symbols = get_all_swap_symbols()
    if not symbols:
        print("⚠️ 감시할 심볼이 없습니다.")
        return

    print(f"🟢 감시 시작: {len(symbols)}개 종목")
    send_telegram(f"✅ OKX 감시 봇 시작됨 ({len(symbols)}종목)")

    while True:
        start = time.time()
        for symbol in symbols:
            try:
                check_15m_conditions(symbol)
            except Exception as e:
                print(f"⚠️ {symbol} 조건 확인 예외:", e)
            time.sleep(0.2)
        elapsed = time.time() - start
        time.sleep(max(0, 120 - elapsed))

# Flask 웹 실행 시 봇 병렬 실행
def start_background_thread():
    t = threading.Thread(target=run_bot)
    t.daemon = True
    t.start()

if __name__ == '__main__':
    start_background_thread()
    app.run(host='0.0.0.0', port=10000)
