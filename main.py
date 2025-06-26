import requests
import pandas as pd
import time
import ta
from flask import Flask
from threading import Thread

# ✅ 텔레그램 설정
TOKEN = 'ykyk123'
CHAT_ID = '6786843744'

# ✅ Flask 앱 설정 (Render 외부 점검용)
app = Flask(__name__)

@app.route('/')
def home():
    return 'OKX 급등 감지 봇 작동 중!'

def run_flask():
    app.run(host='0.0.0.0', port=10000)

# ✅ 텔레그램 메시지 전송 함수
def send_telegram_message(message):
    url = f'https://api.telegram.org/bot{TOKEN}/sendMessage'
    try:
        requests.post(url, data={'chat_id': CHAT_ID, 'text': message})
    except Exception as e:
        print("❌ 텔레그램 전송 오류:", e)

# ✅ OKX 전체 선물 종목 리스트 가져오기 (프록시 적용)
def get_all_swap_symbols():
    try:
        print("🔁 종목 목록 가져오는 중...")
        headers = {"User-Agent": "Mozilla/5.0"}
        proxies = {
            "http": "http://8.219.97.248:80",   # ← 필요시 다른 프록시 주소로 교체
            "https": "http://8.219.97.248:80"
        }
        res = requests.get(
            "https://www.okx.com/api/v5/public/instruments?instType=SWAP",
            headers=headers,
            proxies=proxies,
            timeout=10
        )
        print(f"🔍 API 응답코드: {res.status_code}")
        if res.status_code != 200:
            print("❌ 종목 조회 실패:", res.text)
            return []
        data = res.json().get('data', [])
        print(f"✅ 받은 종목 수: {len(data)}")
        return [d['instId'] for d in data]
    except Exception as e:
        print("❌ 예외 발생 in get_all_swap_symbols:", e)
        return []

# ✅ 급등 조건 검사 함수
def check_condition(df):
    try:
        df['rsi'] = ta.momentum.RSIIndicator(df['close'], window=14).rsi()
        df['upper'] = ta.volatility.BollingerBands(df['close'], window=30).bollinger_hband()

        one_min_change = (df['close'].iloc[-1] - df['close'].iloc[-2]) / df['close'].iloc[-2] * 100
        five_min_change = (df['close'].iloc[-1] - df['close'].iloc[-6]) / df['close'].iloc[-6] * 100
        rsi = df['rsi'].iloc[-1]
        bb_upper = df['upper'].iloc[-1]
        price = df['close'].iloc[-1]

        return one_min_change >= 1 and five_min_change >= 1.5 and rsi >= 70 and price > bb_upper
    except Exception as e:
        print("❌ 조건 체크 중 오류:", e)
        return False

# ✅ 종목별로 캔들 데이터를 받아 조건 확인
def check_symbols():
    symbols = get_all_swap_symbols()
    if not symbols:
        send_telegram_message("⚠️ 감시할 종목이 없습니다.")
        return

    for symbol in symbols:
        try:
            url = f"https://www.okx.com/api/v5/market/candles?instId={symbol}&bar=1m&limit=30"
            df = pd.DataFrame(requests.get(url).json()['data'])
            df.columns = ['timestamp', 'open', 'high', 'low', 'close', 'volume', 'volCcy', 'volCcyQuote', 'confirm']
            df = df.iloc[::-1].reset_index(drop=True)
            df['close'] = df['close'].astype(float)

            if len(df) >= 30 and check_condition(df):
                message = f"🚨 급등 감지: {symbol}\n현재가: {df['close'].iloc[-1]}"
                send_telegram_message(message)
                print(message)
        except Exception as e:
            print(f"❌ {symbol} 분석 오류:", e)

# ✅ 감시 루프
def run_monitor():
    send_telegram_message("🚀 급등 감지 봇 시작됨.")
    while True:
        check_symbols()
        time.sleep(60)

# ✅ 실행
if __name__ == '__main__':
    Thread(target=run_flask).start()
    run_monitor()
