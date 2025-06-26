import requests import pandas as pd import time from ta.volatility import BollingerBands from ta.momentum import RSIIndicator from flask import Flask, request from datetime import datetime

app = Flask(name)

BOT_TOKEN = '7971519272:AAHjBO9Dnc2e-cc5uqQbalHy3bi0kPSAfNw' CHAT_ID = '6786843744' INTERVALS = ['5m', '15m']

def send_telegram_message(message): url = f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage' requests.post(url, json={'chat_id': CHAT_ID, 'text': message})

def get_klines(symbol, interval, limit=100): url = f'https://www.okx.com/api/v5/market/candles?instId={symbol}&bar={interval}&limit={limit}' try: res = requests.get(url) data = res.json() if 'data' not in data: raise ValueError(f"No 'data' field in response: {data}") df = pd.DataFrame(data['data'], columns=['ts', 'open', 'high', 'low', 'close', 'volume', 'volCcy', 'volCcyQuote', 'confirm']) df['close'] = df['close'].astype(float) df['high'] = df['high'].astype(float) df['low'] = df['low'].astype(float) df['open'] = df['open'].astype(float) return df.iloc[::-1].reset_index(drop=True) except Exception as e: print(f"{symbol} 캔들 오류: {e}") return None

def analyze(symbol): try: df_5m = get_klines(symbol, '5m') df_15m = get_klines(symbol, '15m') if df_5m is None or df_15m is None: return

# ==== 5분봉 급등 조건 ====
    close_5m = df_5m['close']
    rsi_5m = RSIIndicator(close_5m, window=14).rsi()
    bb_5m = BollingerBands(close_5m, window=20, window_dev=3)
    bb_upper_5m = bb_5m.bollinger_hband()

    price_change_5m = (close_5m.iloc[-1] - close_5m.iloc[-2]) / close_5m.iloc[-2] * 100
    if (
        price_change_5m >= 1.5
        and rsi_5m.iloc[-1] >= 70
        and close_5m.iloc[-1] > bb_upper_5m.iloc[-1]
    ):
        send_telegram_message(f'🚀 [5분봉 급등] {symbol}\n상승률: {price_change_5m:.2f}%\nRSI: {rsi_5m.iloc[-1]:.2f}')

    # ==== 15분봉 급등 조건 ====
    close_15m = df_15m['close']
    rsi_15m = RSIIndicator(close_15m, window=14).rsi()
    bb_15m = BollingerBands(close_15m, window=20, window_dev=3)
    bb_upper_15m = bb_15m.bollinger_hband()

    if (
        rsi_15m.iloc[-1] >= 70
        and close_15m.iloc[-1] > bb_upper_15m.iloc[-1]
    ):
        send_telegram_message(f'🚀 [15분봉 급등] {symbol}\nRSI: {rsi_15m.iloc[-1]:.2f}')

    # ==== 5분봉 급락 조건 ====
    bb_lower_5m = bb_5m.bollinger_lband()
    if (
        price_change_5m <= -1.5
        and rsi_5m.iloc[-1] <= 30
        and close_5m.iloc[-1] < bb_lower_5m.iloc[-1]
    ):
        send_telegram_message(f'⚠️ [5분봉 급락] {symbol}\n하락률: {price_change_5m:.2f}%\nRSI: {rsi_5m.iloc[-1]:.2f}')

    # ==== 15분봉 급락 조건 ====
    bb_lower_15m = bb_15m.bollinger_lband()
    if (
        rsi_15m.iloc[-1] <= 30
        and close_15m.iloc[-1] < bb_lower_15m.iloc[-1]
    ):
        send_telegram_message(f'⚠️ [15분봉 급락] {symbol}\nRSI: {rsi_15m.iloc[-1]:.2f}')

except Exception as e:
    print(f"분석 오류 - {symbol}: {e}")

def get_usdt_symbols(): try: url = 'https://www.okx.com/api/v5/public/instruments?instType=SWAP' res = requests.get(url) data = res.json() symbols = [x['instId'] for x in data['data'] if x['instId'].endswith("-USDT-SWAP")] return symbols except Exception as e: print(f"심볼 조회 오류: {e}") return []

@app.route('/') def home(): return f'✅ OKX 급등 감지 봇 작동 중! ({datetime.utcnow()})'

if name == 'main': symbols = get_usdt_symbols() send_telegram_message(f'✅ OKX 급등 감지 봇 작동 시작됨\n총 감시 종목 수: **{len(symbols)}**개') for symbol in symbols: analyze(symbol)

