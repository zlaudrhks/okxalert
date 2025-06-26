import requests
import pandas as pd
import time
import ta
from flask import Flask
from threading import Thread
from datetime import datetime

# ✅ 텔레그램 설정
TOKEN = '7971519272:AAHjBO9Dnc2e-cc5uqQbalHy3bi0kPSAfNw'
CHAT_ID = '6786843744'

# ✅ Flask 앱 설정 (Render 외부 점검용)
app = Flask(__name__)

@app.route('/')
def home():
    return f'✅ OKX 급등 감지 봇 작동 중! ({datetime.utcnow()})'

def run_flask():
    app.run(host='0.0.0.0', port=10000)

# ✅ 텔레그램 메시지 전송 함수
def send_telegram_message(message):
    url = f'https://api.telegram.org/bot{TOKEN}/sendMessage'
    try:
        requests.post(url, data={
            'chat_id': CHAT_ID,
            'text': message,
            'parse_mode': 'MarkdownV2'
        })
    except Exception as e:
        print("❌ 텔레그램 전송 오류:", e)

# ✅ 특수문자 이스케이프 (MarkdownV2용)
def escape_md(text):
    for ch in r'_*[]()~`>#+-=|{}.!':
        text = text.replace(ch, f'\\{ch}')
    return text

# ✅ OKX 전체 선물 종목 리스트 가져오기 (프록시 적용)
def get_all_swap_symbols():
    try:
        print("🔁 종목 목록 가져오는 중...")
        headers = {"User-Agent": "Mozilla/5.0"}
        proxies = {
            "http": "http://8.219.97.248:80",
            "https": "http://8.219.97.248:80"
        }
        res = requests.get(
            "https://www.okx.com/api/v5/public/instruments?instType=SWAP",
            headers=headers,
            proxies=proxies,
            timeout=10
        )
        if res.status_code != 200:
            print("❌ 종목 조회 실패:", res.text)
            return []
        data = res.json().get('data', [])
        print(f"✅ 받은 종목 수: {len(data)}")
        return [d['instId'] for d in data if d['instId'].endswith('USDT-SWAP')]
    except Exception as e:
        print("❌ 예외 발생 in get_all_swap_symbols:", e)
        return []

# ✅ 캔들 데이터 가져오기
def get_candles(symbol, interval, limit=100):
    try:
        url = f"https://www.okx.com/api/v5/market/candles?instId={symbol}&bar={interval}&limit={limit}"
        res = requests.get(url, timeout=10)
        data = res.json().get('data', [])
        if not data or len(data) < 20:
            return None
        df = pd.DataFrame(data)
        df.columns = ['timestamp', 'open', 'high', 'low', 'close', 'volume',
                      'volCcy', 'volQuote', 'confirm']
        df = df.iloc[::-1].reset_index(drop=True)
        df['close'] = df['close'].astype(float)
        return df
    except Exception as e:
        print(f"❌ {symbol} 캔들 불러오기 오류:", e)
        return None

# ✅ 조건 검사
def check_symbol(symbol):
    df_5m = get_candles(symbol, '5m')
    df_15m = get_candles(symbol, '15m')
    if df_5m is None or df_15m is None:
        return

    now_price = df_5m['close'].iloc[-1]

    # ===== 조건 1: 강세 신호 (5분봉) =====
    try:
        change_5m = (now_price - df_5m['close'].iloc[-6]) / df_5m['close'].iloc[-6] * 100
        rsi_5m = ta.momentum.RSIIndicator(df_5m['close'], window=14).rsi().iloc[-1]
        bb_5m = ta.volatility.BollingerBands(df_5m['close'], window=20, window_dev=3)
        bb_upper_5m = bb_5m.bollinger_hband().iloc[-1]

        if change_5m > 1.5 and rsi_5m > 70 and now_price > bb_upper_5m:
            msg = (
                f"⚠️ [강세 신호] {escape_md(symbol)}\n\n"
                f"▸ 5분 상승률: `{change_5m:.2f}%`\n"
                f"▸ RSI: `{rsi_5m:.2f}`"
            )
            send_telegram_message(msg)
            print(msg)
            return
    except Exception as e:
        print(f"❌ 조건1 오류: {symbol}", e)

    # ===== 조건 2: 급등 감지 (15분봉) =====
    try:
        change_15m = (now_price - df_15m['close'].iloc[-6]) / df_15m['close'].iloc[-6] * 100
        rsi_15m = ta.momentum.RSIIndicator(df_15m['close'], window=14).rsi().iloc[-1]
        bb_15m = ta.volatility.BollingerBands(df_15m['close'], window=20, window_dev=3)
        bb_upper_15m = bb_15m.bollinger_hband().iloc[-1]

        if rsi_15m > 70 and now_price > bb_upper_15m:
            msg = (
                f"🚨 [급등 감지] {escape_md(symbol)}\n\n"
                f"▸ 15분 상승률: `{change_15m:.2f}%`\n"
                f"▸ RSI: `{rsi_15m:.2f}`"
            )
            send_telegram_message(msg)
            print(msg)
    except Exception as e:
        print(f"❌ 조건2 오류: {symbol}", e)

# ✅ 감시 루프
def run_monitor():
    send_telegram_message("🚀 급등 감지 봇 시작됨.")
    while True:
        symbols = get_all_swap_symbols()
        for symbol in symbols:
            check_symbol(symbol)
        time.sleep(60)

# ✅ 실행
if __name__ == '__main__':
    Thread(target=run_flask).start()
    run_monitor()
