import requests
import pandas as pd
import ta
import time
from datetime import datetime
from flask import Flask
from threading import Thread

# ✅ Telegram 설정
TOKEN = '7971519272:AAHjBO9Dnc2e-cc5uqQbalHy3bi0kPSAfNw'
CHAT_ID = '6786843744'

# ✅ Flask 설정
app = Flask(__name__)

@app.route('/')
def home():
    return f'✅ OKX 급등 감지 봇 작동 중! ({datetime.utcnow()})'

def run_flask():
    app.run(host='0.0.0.0', port=10000)

# ✅ 텔레그램 알림 함수
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    try:
        requests.post(url, data=data)
    except Exception as e:
        print("❌ 텔레그램 오류:", e)

# ✅ 심볼 목록 가져오기
def get_usdt_symbols():
    url = "https://www.okx.com/api/v5/public/instruments?instType=SWAP"
    try:
        res = requests.get(url, timeout=10)
        data = res.json()
        symbols = [s['instId'] for s in data['data'] if s['instId'].endswith("USDT-SWAP")]
        print(f"✅ USDT 종목 수: {len(symbols)}")
        return symbols
    except Exception as e:
        print("❌ 심볼 목록 오류:", e)
        return []

# ✅ 캔들 데이터 가져오기 (JSON 오류 대응)
def get_candles(symbol, bar='5m', limit=100):
    url = f"https://www.okx.com/api/v5/market/candles?instId={symbol}&bar={bar}&limit={limit}"
    try:
        res = requests.get(url, timeout=10)
        if res.status_code != 200:
            print(f"❌ {symbol} API 오류: HTTP {res.status_code}")
            return None
        try:
            raw = res.json()
        except ValueError:
            print(f"❌ {symbol} 응답 JSON 오류: {res.text[:100]}")
            return None
        if 'data' not in raw or not raw['data']:
            print(f"⚠️ {symbol} 캔들 없음")
            return None
        df = pd.DataFrame(raw['data'], columns=[
            "ts", "open", "high", "low", "close", "vol", "vol_ccy", "vol_quote", "confirm"
        ])
        df = df.iloc[::-1].reset_index(drop=True)
        df["close"] = pd.to_numeric(df["close"])
        return df
    except Exception as e:
        print(f"❌ {symbol} 캔들 오류:", e)
        return None

# ✅ 조건 검사
def check_conditions(symbol):
    df_5m = get_candles(symbol, "5m", 100)
    df_15m = get_candles(symbol, "15m", 100)
    if df_5m is None or df_15m is None:
        return

    # RSI + BB (공통)
    df_5m['rsi'] = ta.momentum.RSIIndicator(df_5m['close'], window=14).rsi()
    bb_5m = ta.volatility.BollingerBands(df_5m['close'], window=20, window_dev=3)
    df_5m['bb_upper'] = bb_5m.bollinger_hband()

    df_15m['rsi'] = ta.momentum.RSIIndicator(df_15m['close'], window=14).rsi()
    bb_15m = ta.volatility.BollingerBands(df_15m['close'], window=20, window_dev=3)
    df_15m['bb_upper'] = bb_15m.bollinger_hband()

    # 최신값
    price = df_5m['close'].iloc[-1]
    rsi_5m = df_5m['rsi'].iloc[-1]
    bb_5m_upper = df_5m['bb_upper'].iloc[-1]

    rsi_15m = df_15m['rsi'].iloc[-1]
    bb_15m_upper = df_15m['bb_upper'].iloc[-1]

    # 상승률
    price_5m_ago = df_5m['close'].iloc[-6]
    change_5m = (price - price_5m_ago) / price_5m_ago * 100

    price_15m_ago = df_15m['close'].iloc[-16]
    change_15m = (price - price_15m_ago) / price_15m_ago * 100

    # 조건1: 📡 강세 신호
    if change_5m > 1.5 and rsi_5m >= 70 and price > bb_5m_upper:
        msg = (
            f"<b>📡 <u>[강세 신호]</u></b>\n"
            f"<b>종목:</b> {symbol}\n"
            f"<b>5분 상승률:</b> <span style='color:white'>{change_5m:.2f}%</span>\n"
            f"<b>RSI:</b> <span style='color:white'>{rsi_5m:.2f}</span>"
        )
        print(msg)
        send_telegram(msg)

    # 조건2: 🚨 급등 감지
    if rsi_15m >= 70 and price > bb_15m_upper:
        msg = (
            f"<b>🚨 <u>[급등 감지]</u></b>\n"
            f"<b>종목:</b> {symbol}\n"
            f"<b>15분 상승률:</b> <span style='color:white'>{change_15m:.2f}%</span>\n"
            f"<b>RSI:</b> <span style='color:white'>{rsi_15m:.2f}</span>"
        )
        print(msg)
        send_telegram(msg)

# ✅ 감시 루프
def run_monitor():
    send_telegram("✅ <b>OKX 급등 감지 봇 작동 시작됨</b>")
    while True:
        symbols = get_usdt_symbols()
        for symbol in symbols:
            check_conditions(symbol)
        time.sleep(60)

# ✅ 실행
if __name__ == '__main__':
    Thread(target=run_flask).start()
    run_monitor()
