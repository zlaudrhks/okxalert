import requests
import pandas as pd
import time
from datetime import datetime, timezone
from ta.momentum import RSIIndicator
from ta.trend import MACD
from flask import Flask
import threading

# 텔레그램 정보
TOKEN = '7971519272:AAHjBO9Dnc2e-cc5uqQbalHy3bi0kPSAfNw'
CHAT_ID = '6786843744'

# 텔레그램 전송 함수
def send_telegram(message):
    url = f'https://api.telegram.org/bot{TOKEN}/sendMessage'
    data = {
        'chat_id': CHAT_ID,
        'text': message,
        'parse_mode': 'HTML'
    }
    try:
        response = requests.post(url, data=data)
        print('텔레그램 전송:', response.text)
    except Exception as e:
        print('텔레그램 오류:', e)

# 감시할 USDT 종목 가져오기
def get_usdt_symbols():
    try:
        url = 'https://www.okx.com/api/v5/market/tickers?instType=SWAP'
        res = requests.get(url).json()
        return [i['instId'] for i in res['data'] if i['instId'].endswith('USDT-SWAP')]
    except Exception as e:
        print('종목 가져오기 오류:', e)
        return []

# 조건 체크
def check_conditions(symbol):
    try:
        url = f'https://www.okx.com/api/v5/market/candles?instId={symbol}&bar=1m&limit=100'
        df = pd.DataFrame(requests.get(url).json()['data'],
                          columns=["time", "open", "high", "low", "close", "volume", "volumeCcy", "volumeCcyQuote", "confirm"])
        df = df.iloc[::-1].reset_index(drop=True)
        df["close"] = pd.to_numeric(df["close"])

        rsi = RSIIndicator(df["close"], window=14).rsi()
        macd = MACD(df["close"]).macd_diff()

        if rsi.iloc[-1] > 70 and macd.iloc[-1] > 0 and macd.iloc[-2] < 0:
            msg = (
                f"🚨 <b>{symbol}</b>\n"
                f"RSI: {rsi.iloc[-1]:.2f}, MACD DIFF: {macd.iloc[-1]:.4f}\n"
                f"조건 충족 시각: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC"
            )
            send_telegram(msg)

    except Exception as e:
        print(f'{symbol} 캔들 오류:', e)

# 메인 루프
def run_monitor():
    symbols = get_usdt_symbols()
    send_telegram(f"✅ <b>OKX 급등 감지 봇 작동 시작됨</b>\n총 감시 종목 수: <b>{len(symbols)}</b>개")
    for symbol in symbols:
        check_conditions(symbol)
    while True:
        for symbol in symbols:
            check_conditions(symbol)
        time.sleep(60)

# Flask 헬스체크
app = Flask(__name__)
@app.route('/')
def index():
    return f'✅ OKX 급등 감지 봇 작동 중! ({datetime.now(timezone.utc)})'

# 병렬 실행
if __name__ == '__main__':
    threading.Thread(target=run_monitor).start()
    app.run(host='0.0.0.0', port=10000)
