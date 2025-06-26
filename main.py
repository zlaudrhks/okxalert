import requests
import pandas as pd
import time
import ta
from flask import Flask
from datetime import datetime

app = Flask(__name__)

BOT_TOKEN = 'ykyk123'
CHAT_ID = '6786843744'
CHECK_INTERVAL = 60  # 1분마다 검사

proxies = {
    "http": "http://188.166.29.251:3128",
    "https": "http://188.166.29.251:3128"
}

def send_telegram_message(message):
    url = f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage'
    data = {'chat_id': CHAT_ID, 'text': message}
    try:
        res = requests.post(url, data=data)
        if res.status_code != 200:
            print(f'❌ 텔레그램 전송 실패: {res.text}')
    except Exception as e:
        print(f'❌ 텔레그램 오류: {e}')

def get_usdt_swap_symbols():
    url = "https://www.okx.com/api/v5/public/instruments?instType=SWAP"
    try:
        res = requests.get(url, proxies=proxies, timeout=10)
        data = res.json()
        symbols = [item['instId'] for item in data['data'] if item['instId'].endswith("USDT-SWAP")]
        print(f"✅ USDT 종목 수: {len(symbols)}")
        return symbols
    except Exception as e:
        print(f"❌ 심볼 가져오기 실패: {e}")
        return []

def get_candles(symbol, bar="5m", limit=100):
    url = f"https://www.okx.com/api/v5/market/candles?instId={symbol}&bar={bar}&limit={limit}"
    try:
        res = requests.get(url, proxies=proxies, timeout=10)
        data = res.json()
        if 'data' not in data or len(data['data']) == 0:
            return None
        df = pd.DataFrame(data['data'], columns=[
            "timestamp", "open", "high", "low", "close", "volume",
            "volume_ccy", "ts", "confirm", "turnover"
        ])
        df = df.iloc[::-1]
        df["close"] = pd.to_numeric(df["close"])
        df["open"] = pd.to_numeric(df["open"])
        return df
    except Exception as e:
        print(f"❌ {symbol} 캔들 오류: {e}")
        return None

def check_conditions(symbol):
    df_5m = get_candles(symbol, "5m", 100)
    df_15m = get_candles(symbol, "15m", 100)
    if df_5m is None or df_15m is None:
        return

    now_price = df_5m["close"].iloc[-1]

    # ✅ 조건 1: 5분봉
    price_5m_ago = df_5m["close"].iloc[-6]
    change_5m = (now_price - price_5m_ago) / price_5m_ago * 100

    df_5m["rsi"] = ta.momentum.RSIIndicator(df_5m["close"], window=14).rsi()
    bb_5m = ta.volatility.BollingerBands(df_5m["close"], window=20, window_dev=3)
    df_5m["bb_upper"] = bb_5m.bollinger_hband()
    rsi_5m = df_5m["rsi"].iloc[-1]
    bb_upper_5m = df_5m["bb_upper"].iloc[-1]

    if change_5m > 1.5 and rsi_5m > 70 and now_price > bb_upper_5m:
        message = (
            f"⚠️ [강세 신호] {symbol}\n\n"
            f"▷ 5분간 상승률: +{change_5m:.2f}%\n"
            f"▷ RSI: {rsi_5m:.2f}"
        )
        print(message)
        send_telegram_message(message)
        return

    # ✅ 조건 2: 15분봉
    price_15m_ago = df_15m["close"].iloc[-6]
    change_15m = (now_price - price_15m_ago) / price_15m_ago * 100

    df_15m["rsi"] = ta.momentum.RSIIndicator(df_15m["close"], window=14).rsi()
    bb_15m = ta.volatility.BollingerBands(df_15m["close"], window=20, window_dev=3)
    df_15m["bb_upper"] = bb_15m.bollinger_hband()
    rsi_15m = df_15m["rsi"].iloc[-1]
    bb_upper_15m = df_15m["bb_upper"].iloc[-1]

    if rsi_15m > 70 and now_price > bb_upper_15m:
        message = (
            f"🚨 [급등 감지] {symbol}\n\n"
            f"▶ 15분간 상승률: +{change_15m:.2f}%\n"
            f"▶ RSI: {rsi_15m:.2f}"
        )
        print(message)
        send_telegram_message(message)
    else:
        print(f"⏳ {symbol} 조건 불충족")

def run_alert_loop():
    print("🚀 급등 감지 봇 시작됨.")
    while True:
        print("🔄 USDT 종목 검사 중...")
        symbols = get_usdt_swap_symbols()
        if not symbols:
            print("❌ 감시할 종목이 없습니다.")
            time.sleep(CHECK_INTERVAL)
            continue
        for symbol in symbols:
            check_conditions(symbol)
        print(f"⏱️ 대기 중... ({CHECK_INTERVAL}초)")
        time.sleep(CHECK_INTERVAL)

@app.route('/')
def home():
    return f"OKX 급등 감지 봇 작동 중 ({datetime.utcnow()})"

if __name__ == '__main__':
    from threading import Thread
    Thread(target=run_alert_loop).start()
    app.run(host="0.0.0.0", port=10000)
