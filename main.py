import requests, time, pandas as pd, ta
from flask import Flask
import threading

TOKEN = '7971519272:AAHjBO9Dnc2e-cc5uqQbalHy3bi0kPSAfNw'
CHAT_ID = '6786843744'

app = Flask(__name__)
@app.route('/')
def home():
    return '✅ 봇 작동 중입니다.'

def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": msg}
    try: requests.post(url, data=data)
    except: print("텔레그램 실패")

def get_all_symbols():
    try:
        r = requests.get("https://www.okx.com/api/v5/public/instruments?instType=SWAP")
        return [i['instId'] for i in r.json()['data'] if i['settleCcy'] == 'USDT']
    except: return []

def get_ohlcv(symbol, interval, limit=100):
    try:
        r = requests.get(f"https://www.okx.com/api/v5/market/candles?instId={symbol}&bar={interval}&limit={limit}")
        df = pd.DataFrame(r.json()['data'], columns=['ts','open','high','low','close','vol','volccy'])
        return df.astype(float).iloc[::-1].reset_index(drop=True)
    except: return None

def check_conditions(symbol):
    df = get_ohlcv(symbol, '5m')
    if df is None or len(df) < 30: return
    close = df['close']
    change = (close.iloc[-1] - close.iloc[-6]) / close.iloc[-6] * 100
    rsi = ta.momentum.RSIIndicator(close, 14).rsi().iloc[-1]
    bb = ta.volatility.BollingerBands(close, 30, 3)
    upper = bb.bollinger_hband().iloc[-1]
    last = close.iloc[-1]
    if change >= 1.5 and rsi >= 70 and last > upper:
        msg = f"📈 {symbol} 급등 감지!\n5분봉 +{change:.2f}%\nRSI {rsi:.2f}\n종가 {last:.4f} > 상단 {upper:.4f}"
        send_telegram(msg)
        print(msg)

def run_bot():
    symbols = get_all_symbols()
    send_telegram(f"✅ Render 봇 시작됨 ({len(symbols)} 종목)")
    while True:
        for s in symbols:
            check_conditions(s)
            time.sleep(0.5)
        time.sleep(60)

# 쓰레드로 봇 + 웹서버
threading.Thread(target=run_bot).start()
app.run(host="0.0.0.0", port=3000)
