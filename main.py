import requests
import pandas as pd
from ta.volatility import BollingerBands
from ta.momentum import RSIIndicator
from flask import Flask
import time
from datetime import datetime

# 텔레그램 설정
BOT_TOKEN = '7971519272:AAHjBO9Dnc2e-cc5uqQbalHy3bi0kPSAfNw'
CHAT_ID = '6786843744'

app = Flask(__name__)

def send_telegram_message(message):
    url = f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage'
    try:
        response = requests.post(url, data={'chat_id': CHAT_ID, 'text': message})
        if response.status_code != 200:
            print(f"❌ 텔레그램 전송 실패: {response.text}")
    except Exception as e:
        print(f"❌ 텔레그램 예외 발생: {e}")

# USDT-SWAP 종목 가져오기
def get_usdt_swap_symbols():
    url = "https://www.okx.com/api/v5/public/instruments?instType=SWAP"
    try:
        response = requests.get(url, timeout=10)
        print("📦 OKX API 응답 상태 코드:", response.status_code)
        print("📦 응답 본문 일부:", response.text[:300])

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
def get_candles(symbol, bar):
    url = f"https://www.okx.com/api/v5/market/candles?instId={symbol}&bar={bar}&limit=30"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()

        if data.get("code") != "0":
            raise Exception(f"API 오류: {data.get('msg')}")

        df = pd.DataFrame(data['data'], columns=[
            'timestamp', 'open', 'high', 'low', 'close', 'volume',
            'volumeCcy', 'volumeCcyQuote', 'confirm'
        ])
        df = df.astype({
            'open': 'float', 'high': 'float', 'low': 'float',
            'close': 'float', 'volume': 'float'
        })
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.sort_values('timestamp', inplace=True)
        return df.reset_index(drop=True)
    except Exception as e:
        print(f"{symbol} 캔들 오류: {e}")
        return None

def analyze_symbol(symbol):
    result = []

    df_5 = get_candles(symbol, '5m')
    df_15 = get_candles(symbol, '15m')
    if df_5 is None or df_15 is None:
        return result

    # ===== 5분봉 급등 조건 =====
    close_now = df_5['close'].iloc[-1]
    close_prev = df_5['close'].iloc[-2]
    change_5m = (close_now - close_prev) / close_prev * 100

    rsi_5 = RSIIndicator(df_5['close'], window=14).rsi().iloc[-1]
    bb_5 = BollingerBands(close=df_5['close'], window=20, window_dev=3)
    bb_upper_5 = bb_5.bollinger_hband().iloc[-1]

    if (
        change_5m >= 1.5 and
        rsi_5 > 70 and
        close_now > bb_upper_5
    ):
        result.append(f"🚀 [{symbol}] 5분봉 기준 급등!\n📈 등락률: {change_5m:.2f}%\n📊 RSI: {rsi_5:.2f}")

    # ===== 5분봉 급락 조건 =====
    if (
        change_5m <= -1.5 and
        rsi_5 < 30 and
        close_now < bb_5.bollinger_lband().iloc[-1]
    ):
        result.append(f"📉 [{symbol}] 5분봉 기준 급락!\n📉 등락률: {change_5m:.2f}%\n📊 RSI: {rsi_5:.2f}")

    # ===== 15분봉 급등 조건 =====
    rsi_15 = RSIIndicator(df_15['close'], window=14).rsi().iloc[-1]
    bb_15 = BollingerBands(close=df_15['close'], window=20, window_dev=3)
    close_15 = df_15['close'].iloc[-1]
    if (
        rsi_15 > 70 and
        close_15 > bb_15.bollinger_hband().iloc[-1]
    ):
        result.append(f"🚀 [{symbol}] 15분봉 RSI+BB 상단 돌파 급등!\n📊 RSI: {rsi_15:.2f}")

    # ===== 15분봉 급락 조건 =====
    if (
        rsi_15 < 30 and
        close_15 < bb_15.bollinger_lband().iloc[-1]
    ):
        result.append(f"📉 [{symbol}] 15분봉 RSI+BB 하단 돌파 급락!\n📊 RSI: {rsi_15:.2f}")

    return result

@app.route('/')
def home():
    return f'✅ OKX 급등 감지 봇 작동 중! ({datetime.utcnow()})'

def main():
    symbols = get_usdt_swap_symbols()
    send_telegram_message(f"✅ OKX 급등 감지 봇 작동 시작됨\n총 감시 종목 수: {len(symbols)}개")

    for symbol in symbols:
        messages = analyze_symbol(symbol)
        for msg in messages:
            send_telegram_message(msg)
        time.sleep(0.2)

if __name__ == '__main__':
    main()
    app.run(host='0.0.0.0', port=10000)
