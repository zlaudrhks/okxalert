import requests, time, threading, pandas as pd, ta, os
from flask import Flask

TOKEN = '7971519272:AAHjBO9Dnc2e-cc5uqQbalHy3bi0kPSAfNw'
CHAT_ID = '6786843744'
app = Flask(__name__)

@app.route('/')
def home(): return '✅ OKX 급등 감지 봇 작동 중입니다!', 200

def send_telegram(msg):
    try:
        res = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", data={"chat_id": CHAT_ID, "text": msg})
        if res.status_code != 200: print("❌ 텔레그램 오류:", res.text)
    except Exception as e: print("❌ 전송 실패:", e)

def get_all_swap_symbols():
    try:
        res = requests.get("https://www.okx.com/api/v5/public/instruments?instType=SWAP", headers={"User-Agent": "Mozilla/5.0"})
        if res.status_code != 200: return []
        return [d['instId'] for d in res.json().get('data', [])]  # USDT 조건 제거
    except: return []

def get_ohlcv(symbol, interval):
    try:
        url = f"https://www.okx.com/api/v5/market/candles?instId={symbol}&bar={interval}&limit=100"
        res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"})
        if res.status_code != 200: return None
        raw = res.json()['data']
        df = pd.DataFrame(raw, columns=['ts','open','high','low','close','vol','volCcy']).astype(float).iloc[::-1].reset_index(drop=True)
        return df
    except: return None

def check_conditions(symbol):
    df = get_ohlcv(symbol, '5m')
    if df is None or len(df) < 30: return
    close = df['close']
    rsi = ta.momentum.RSIIndicator(close=close, window=14).rsi().iloc[-1]
    bb = ta.volatility.BollingerBands(close=close, window=30, window_dev=3)
    upper = bb.bollinger_hband().iloc[-1]
    change = (close.iloc[-1] - close.iloc[-6]) / close.iloc[-6] * 100
    if change >= 1.5 and rsi > 70 and close.iloc[-1] > upper:
        msg = f"📈 {symbol} 급등 (K=3)\n5분봉: +{change:.2f}%\nRSI: {rsi:.2f}\n종가: {close.iloc[-1]:.4f} > BB상단: {upper:.4f}"
        send_telegram(msg)
        print(msg)

def run_bot():
    symbols = get_all_swap_symbols()
    if not symbols:
        send_telegram("⚠️ 감시할 종목이 없습니다.")
        return
    send_telegram(f"✅ OKX 선물 감시 시작됨 ({len(symbols)}종목)")
    while True:
        for s in symbols: check_conditions(s); time.sleep(0.3)
        time.sleep(60)

if __name__ == '__main__':
    threading.Thread(target=run_bot).start()
    app.run(host="0.0.0.0", port=int(os.environ.get('PORT', 3000)))
