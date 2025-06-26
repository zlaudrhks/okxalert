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

# ✅ Flask 서버 (Render 점검용)
app = Flask(__name__)
@app.route('/')
def home():
    return f'✅ OKX 급등 감지 봇 작동 중! ({datetime.utcnow()})'
def run_flask():
    app.run(host='0.0.0.0', port=10000)

# ✅ 텔레그램 전송 함수
def send_telegram_message(message):
    url = f'https://api.telegram.org/bot{TOKEN}/sendMessage'
    try:
        res = requests.post(url, data={
            'chat_id': CHAT_ID,
            'text': message,
            'parse_mode': 'MarkdownV2'
        })
        if res.status_code != 200:
            print(f"❌ 텔레그램 전송 실패: {res.status_code} - {res.text}")
        else:
            print("✅ 텔레그램 전송 성공")
    except Exception as e:
        print("❌ 텔레그램 예외 오류:", e)

# ✅ Markdown 이스케이프 처리
def escape_md(text):
    for ch in r'\_*[]()~`>#+-=|{}.!':
        text = text.replace(ch, f'\\{ch}')
    return text

# ✅ 전체 USDT 종목 목록
def get_all_swap_symbols():
    try:
        proxies = {
            "http": "http://8.219.97.248:80",
            "https": "http://8.219.97.248:80"
        }
        url = "https://www.okx.com/api/v5/public/instruments?instType=SWAP"
        res = requests.get(url, proxies=proxies, timeout=10)
        data = res.json().get('data', [])
        return [d['instId'] for d in data if d['instId'].endswith("USDT-SWAP")]
    except Exception as e:
        print("❌ 심볼 불러오기 오류:", e)
        return []

# ✅ 캔들 데이터 가져오기
def get_candles(symbol, bar, limit):
    try:
        url = f"https://www.okx.com/api/v5/market/candles?instId={symbol}&bar={bar}&limit={limit}"
        res = requests.get(url, timeout=10)
        data = res.json().get('data', [])
        if not data:
            return None
        df = pd.DataFrame(data, columns=[
            "timestamp", "open", "high", "low", "close", "volume", "volCcy", "volCcyQuote", "confirm", "ts"
        ])
        df = df.iloc[::-1]
        df['close'] = pd.to_numeric(df['close'])
        return df
    except Exception as e:
        print(f"❌ {symbol} 캔들 오류:", e)
        return None

# ✅ 조건 검사
def check_symbol(symbol):
    df_5m = get_candles(symbol, "5m", 100)
    df_15m = get_candles(symbol, "15m", 100)
    if df_5m is None or df_15m is None:
        return

    try:
        # 5분봉 지표
        rsi_5 = ta.momentum.RSIIndicator(df_5m['close'], window=14).rsi().iloc[-1]
        bb_5 = ta.volatility.BollingerBands(df_5m['close'], window=20, window_dev=3.0)
        bb_upper_5 = bb_5.bollinger_hband().iloc[-1]
        close_5 = df_5m['close'].iloc[-1]
        change_5 = (close_5 - df_5m['close'].iloc[-6]) / df_5m['close'].iloc[-6] * 100

        # 조건 1: 강세 신호
        if change_5 > 1.5 and rsi_5 > 70 and close_5 > bb_upper_5:
            msg = (
                f"⚠️ *강세 신호*\n\n"
                f"• 종목: `{escape_md(symbol)}`\n"
                f"• 5분 상승률: `{change_5:.2f}%`\n"
                f"• RSI: `{rsi_5:.2f}`"
            )
            send_telegram_message(msg)

        # 15분봉 지표
        rsi_15 = ta.momentum.RSIIndicator(df_15m['close'], window=14).rsi().iloc[-1]
        bb_15 = ta.volatility.BollingerBands(df_15m['close'], window=20, window_dev=3.0)
        bb_upper_15 = bb_15.bollinger_hband().iloc[-1]
        close_15 = df_15m['close'].iloc[-1]
        change_15 = (close_15 - df_15m['close'].iloc[-16]) / df_15m['close'].iloc[-16] * 100

        # 조건 2: 급등 감지
        if rsi_15 > 70 and close_15 > bb_upper_15:
            msg = (
                f"🚨 *급등 감지*\n\n"
                f"• 종목: `{escape_md(symbol)}`\n"
                f"• 15분 상승률: `{change_15:.2f}%`\n"
                f"• RSI: `{rsi_15:.2f}`"
            )
            send_telegram_message(msg)

    except Exception as e:
        print(f"❌ {symbol} 조건 검사 오류:", e)

# ✅ 모니터링 루프
def run_monitor():
    send_telegram_message("🚀 *OKX 급등 감지 봇이 시작되었습니다.*")
    while True:
        symbols = get_all_swap_symbols()
        for symbol in symbols:
            check_symbol(symbol)
        time.sleep(60)

# ✅ 실행 시작
if __name__ == '__main__':
    Thread(target=run_flask).start()
    run_monitor()
