from flask import Flask
import requests
import time
import pandas as pd
import ta
import threading

# 텔레그램 설정
TOKEN = '7971519272:AAHjBO9Dnc2e-cc5uqQbalHy3bi0kPSAfNw'
CHAT_ID = '6786843744'

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": message}
    try:
        requests.post(url, data=data)
    except Exception as e:
        print("❌ 텔레그램 전송 실패:", e)

def get_all_swap_symbols():
    url = "https://www.okx.com/api/v5/public/instruments?instType=SWAP"
    try:
        res = requests.get(url)
        if res.status_code != 200:
            print("❌ 심볼 조회 실패", res.status_code)
            return []
        data = res.json().get('data', [])
        symbols = [item['instId'] for item in data if item['settleCcy'] == 'USDT']
        print(f"✅ 총 {len(symbols)}개의 심볼 조회됨")
        return symbols
    except Exception as e:
        print("❌ 심볼 조회 예외:", e)
        return []

def get_ohlcv(symbol, interval='15m', limit=100):
    url = f'https://www.okx.com/api/v5/market/candles?instId={symbol}&bar={interval}&limit={limit}'
    try:
        res = requests.get(url)
        if res.status_code != 200:
            print(f"❌ {symbol} 캔들 조회 실패 {res.status_code}")
            return None
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
        df = df.iloc[::-1].reset_index(drop=True)  # 시간순 정렬
        return df
    except Exception as e:
        print(f"❌ {symbol} 캔들 조회 예외:", e)
        return None

# 알림 쿨타임 (중복방지용)
last_alert_time = {}
ALERT_INTERVAL = 300  # 5분

def check_15m_conditions(symbol):
    df = get_ohlcv(symbol)
    if df is None or len(df) < 30:
        return

    close = df['close']
    rsi = ta.momentum.RSIIndicator(close, window=14).rsi().iloc[-1]
    bb = ta.volatility.BollingerBands(close=close, window=20, window_dev=3)
    upper = bb.bollinger_hband().iloc[-1]
    lower = bb.bollinger_lband().iloc[-1]
    last_close = close.iloc[-1]
    prev_close = close.iloc[-2]
    change_percent = (last_close - prev_close) / prev_close * 100

    now = time.time()
    key_up = (symbol, 'upper')
    key_low = (symbol, 'lower')

    # 🚀 급등 조건 (RSI 70 이상 + 볼린저밴드 상단 돌파)
    if rsi >= 70 and last_close > upper:
        if key_up not in last_alert_time or now - last_alert_time[key_up] > ALERT_INTERVAL:
            msg = (
                f"🚀 급등 감지: {symbol}\n"
                f"🔸 RSI: {rsi:.2f}\n"
                f"🔸 변화율: +{change_percent:.2f}%\n"
                f"🔸 종가: {last_close:.4f}\n"
                f"(15분봉 기준)"
            )
            send_telegram(msg)
            print(msg)
            last_alert_time[key_up] = now

    # 📉 급락 조건 (RSI 30 이하 + 볼린저밴드 하단 이탈)
    elif rsi <= 30 and last_close < lower:
        if key_low not in last_alert_time or now - last_alert_time[key_low] > ALERT_INTERVAL:
            msg = (
                f"📉 급락 감지: {symbol}\n"
                f"🔹 RSI: {rsi:.2f}\n"
                f"🔹 변화율: {change_percent:.2f}%\n"
                f"🔹 종가: {last_close:.4f}\n"
                f"(15분봉 기준)"
            )
            send_telegram(msg)
            print(msg)
            last_alert_time[key_low] = now

def run_bot():
    symbols = get_all_swap_symbols()
    if not symbols:
        print("🚫 감시할 심볼 없음")
        return

    send_telegram(f"✅ OKX 감지 봇 시작됨 ({len(symbols)}종목)")
    print(f"▶️ 감시 시작: {len(symbols)}개 심볼")

    while True:
        start = time.time()
        for symbol in symbols:
            try:
                check_15m_conditions(symbol)
            except Exception as e:
                print(f"⚠️ {symbol} 분석 중 오류:", e)
            time.sleep(0.2)
        elapsed = time.time() - start
        sleep_time = max(0, 120 - elapsed)
        time.sleep(sleep_time)

# Flask 서버 (Render에서 PORT 바인딩)
app = Flask(__name__)

@app.route('/')
def home():
    return '✅ OKX 감지 서버 작동 중'

# 백그라운드에서 run_bot 실행
threading.Thread(target=run_bot).start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
