import requests, time, pandas as pd, ta
from flask import Flask
import threading

# 🔧 텔레그램 설정
TOKEN = '7971519272:AAHjBO9Dnc2e-cc5uqQbalHy3bi0kPSAfNw'
CHAT_ID = '6786843744'

# 🌐 Flask 서버
app = Flask(__name__)
@app.route('/')
def home():
    return '✅ OKX 급등 감지 봇 작동 중입니다!'

# 📬 텔레그램 전송 함수
def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": msg}
    try:
        requests.post(url, data=data)
    except Exception as e:
        print("❌ 텔레그램 전송 실패:", e)

# 📈 OKX USDT 선물 심볼 가져오기
def get_all_usdt_symbols(retries=3):
    url = "https://www.okx.com/api/v5/public/instruments?instType=SWAP"
    for _ in range(retries):
        try:
            res = requests.get(url)
            if res.status_code == 200:
                data = res.json().get('data', [])
                usdt_symbols = [item['instId'] for item in data if item['settleCcy'] == 'USDT']
                if usdt_symbols:
                    return usdt_symbols
        except:
            pass
        time.sleep(1)
    return []

# 🕰️ 캔들 데이터 요청
def get_ohlcv(symbol, interval, limit=100):
    url = f'https://www.okx.com/api/v5/market/candles?instId={symbol}&bar={interval}&limit={limit}'
    try:
        res = requests.get(url)
        if res.status_code != 200:
            return None
        raw = res.json().get('data', [])
        df = pd.DataFrame(raw, columns=[
            'timestamp', 'open', 'high', 'low', 'close', 'volume', 'volumeCcy'
        ])
        df = df.astype(float).iloc[::-1].reset_index(drop=True)
        return df
    except Exception as e:
        print(f"❌ {symbol} 데이터 요청 실패:", e)
        return None

# ✅ 조건 확인
def check_conditions(symbol):
    df = get_ohlcv(symbol, '5m')
    if df is None or len(df) < 30:
        return

    close = df['close']
    price_change_5m = (close.iloc[-1] - close.iloc[-6]) / close.iloc[-6] * 100
    rsi = ta.momentum.RSIIndicator(close=close, window=14).rsi().iloc[-1]
    bb = ta.volatility.BollingerBands(close=close, window=30, window_dev=3)
    upper = bb.bollinger_hband().iloc[-1]
    last_close = close.iloc[-1]

    if price_change_5m >= 1.5 and rsi > 70 and last_close > upper:
        msg = f"📈 {symbol} 급등 감지!\n" \
              f"5분봉: +{price_change_5m:.2f}%\n" \
              f"RSI: {rsi:.2f}\n" \
              f"종가: {last_close:.4f} > 상단: {upper:.4f}"
        send_telegram(msg)
        print(msg)

# ▶️ 봇 실행
def run_bot():
    symbols = get_all_usdt_symbols()
    if not symbols:
        msg = "⚠️ Render 봇 시작됨 - 감시할 USDT 종목이 없습니다."
        print(msg)
        send_telegram(msg)
        return

    start_msg = f"✅ Render 봇 시작됨 ({len(symbols)}개 USDT 종목 감시 중)"
    print(start_msg)
    send_telegram(start_msg)

    while True:
        for symbol in symbols:
            check_conditions(symbol)
            time.sleep(0.5)
        time.sleep(60)

# 🔁 봇 쓰레드 + 웹서버 실행
if __name__ == '__main__':
    threading.Thread(target=run_bot).start()
    app.run(host="0.0.0.0", port=3000)
