import time
import requests
import pandas as pd
from ta.volatility import BollingerBands
from ta.momentum import RSIIndicator
from flask import Flask
import threading

# === 텔레그램 설정 ===
TOKEN = 'ykyk123'
CHAT_ID = '6786843744'

def send_telegram(msg):
    url = f'https://api.telegram.org/bot{TOKEN}/sendMessage'
    try:
        requests.post(url, json={'chat_id': CHAT_ID, 'text': msg})
    except:
        pass

# === OKX USDT 심볼 가져오기 ===
def get_all_swap_symbols():
    url = "https://www.okx.com/api/v5/public/instruments?instType=SWAP"
    headers = {
        'User-Agent': 'Mozilla/5.0',
        'Accept': 'application/json'
    }
    try:
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code != 200:
            print("❌ 응답 실패:", res.status_code)
            send_telegram("❌ OKX 심볼 API 응답 실패")
            return []

        data = res.json().get('data', [])
        usdt_symbols = [
            item['instId'] for item in data
            if item.get('settleCcy') == 'USDT' and 'SWAP' in item.get('instId', '')
        ]

        print(f"✅ USDT 종목 수: {len(usdt_symbols)}")
        if not usdt_symbols:
            send_telegram("⚠️ 감시할 USDT 종목이 없습니다. OKX API 확인 필요")
        return usdt_symbols

    except Exception as e:
        print("❌ 심볼 요청 중 오류:", e)
        send_telegram("❌ OKX 심볼 요청 중 오류: " + str(e))
        return []

# === 조건 감지 함수 ===
def check_signal(df):
    if len(df) < 30:
        return False

    close = df['close']
    rsi = RSIIndicator(close).rsi()
    bb = BollingerBands(close, window=30, window_dev=3)

    latest = len(df) - 1

    # 조건 확인
    pct_change = (close.iloc[-1] - close.iloc[-6]) / close.iloc[-6] * 100
    condition = (
        pct_change >= 1.5 and
        rsi.iloc[-1] >= 70 and
        close.iloc[-1] > bb.bollinger_hband().iloc[-1]
    )
    return condition

# === 캔들 데이터 가져오기 ===
def get_candles(symbol):
    url = f"https://www.okx.com/api/v5/market/candles?instId={symbol}&bar=5m&limit=50"
    try:
        res = requests.get(url, timeout=10)
        data = res.json().get('data', [])
        if not data:
            return None
        df = pd.DataFrame(data, columns=[
            'timestamp', 'open', 'high', 'low', 'close',
            'volume', 'volumeCcy', 'volumeCcyQuote', 'confirm'
        ])
        df = df.astype({'open': float, 'high': float, 'low': float, 'close': float})
        return df[::-1].reset_index(drop=True)
    except:
        return None

# === 메인 감시 루프 ===
def monitor():
    symbols = get_all_swap_symbols()
    if not symbols:
        print("⚠️ 감시할 USDT 종목이 없습니다.")
        return

    send_telegram(f"✅ Render 봇 시작됨 ({len(symbols)} 종목 감시 중)")

    while True:
        for symbol in symbols:
            df = get_candles(symbol)
            if df is None:
                continue
            if check_signal(df):
                msg = f"🚀 급등 감지!\n종목: {symbol}\n가격: {df['close'].iloc[-1]}"
                send_telegram(msg)
                print(msg)
        time.sleep(60)

# === Flask 서버 (Render 외부 접근용 + 점검용) ===
app = Flask(__name__)

@app.route('/')
def home():
    return 'OKX Alert Bot is running.'

# === 스레드로 모니터링 실행 ===
def run_monitor():
    threading.Thread(target=monitor).start()

if __name__ == '__main__':
    run_monitor()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))
