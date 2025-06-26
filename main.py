import requests, time, pandas as pd, ta
from flask import Flask
import threading

# ✅ 텔레그램 설정
TOKEN = '7971519272:AAHjBO9Dnc2e-cc5uqQbalHy3bi0kPSAfNw'
CHAT_ID = '6786843744'

# ✅ Flask 웹 서버
app = Flask(__name__)
@app.route('/')
def home():
    return '✅ OKX 급등 감지 봇 작동 중입니다!'

# ✅ 텔레그램 전송 함수
def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": CHAT_ID, "text": msg})
    except Exception as e:
        print("❌ 텔레그램 전송 실패:", e)

# ✅ OKX 선물 USDT 종목 가져오기 (디버그용 출력 포함)
def get_all_usdt_symbols(retries=3):
    url = "https://www.okx.com/api/v5/public/instruments?instType=SWAP"
    headers = {"User-Agent": "Mozilla/5.0"}
    
    for i in range(retries):
        try:
            res = requests.get(url, headers=headers)
            print(f"[시도 {i+1}] 응답 코드:", res.status_code)
            
            if res.status_code == 200:
                data = res.json()
                sample = data.get("data", [])[:1]
                print("📦 데이터 샘플:", sample)

                usdt_symbols = [
                    item['instId'] for item in data.get('data', [])
                    if item.get('settleCcy') == 'USDT'
                ]
                if usdt_symbols:
                    return usdt_symbols
            else:
                print("❌ API 호출 실패. 응답코드:", res.status_code)
        except Exception as e:
            print("❌ 예외 발생:", e)
        time.sleep(1)
    
    return []

# ✅ OHLCV 가져오기
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

# ✅ 조건 체크
def check_conditions(symbol):
    df = get_ohlcv(symbol, '5m')
    if df is None or len(df) < 30:
        return

    close = df['close']
    change_5m = (close.iloc[-1] - close.iloc[-6]) / close.iloc[-6] * 100
    rsi = ta.momentum.RSIIndicator(close=close, window=14).rsi().iloc[-1]
    bb = ta.volatility.BollingerBands(close=close, window=30, window_dev=3)
    upper = bb.bollinger_hband().iloc[-1]
    last_close = close.iloc[-1]

    if change_5m >= 1.5 and rsi > 70 and last_close > upper:
        msg = f"📈 {symbol} 급등 감지!\n" \
              f"5분봉: +{change_5m:.2f}%\nRSI: {rsi:.2f}\n" \
              f"종가: {last_close:.4f} > 상단: {upper:.4f}"
        send_telegram(msg)
        print(msg)

# ✅ 봇 실행 루프
def run_bot():
    symbols = get_all_usdt_symbols()
    if not symbols:
        msg = "⚠️ Render 봇 시작됨 - 감시할 USDT 종목이 없습니다."
        print(msg)
        send_telegram(msg)
        return

    msg = f"✅ Render 봇 시작됨 ({len(symbols)}개 USDT 종목 감시 중)"
    print(msg)
    send_telegram(msg)

    while True:
        for symbol in symbols:
            check_conditions(symbol)
            time.sleep(0.5)
        time.sleep(60)

# ✅ 메인 실행
if __name__ == '__main__':
    threading.Thread(target=run_bot).start()
    app.run(host="0.0.0.0", port=10000)
