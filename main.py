import requests import pandas as pd import time import datetime from ta.volatility import BollingerBands from ta.momentum import RSIIndicator from flask import Flask

텔레그램 설정

TELEGRAM_TOKEN = '7971519272:AAHjBO9Dnc2e-cc5uqQbalHy3bi0kPSAfNw' CHAT_ID = '6786843744'

def send_telegram_message(message): url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage" data = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"} try: response = requests.post(url, data=data) if response.status_code != 200: print("❌ 텔레그램 전송 실패:", response.text) except Exception as e: print("❌ 텔레그램 예외:", e)

OKX 프록시 서버 주소

BASE_URL = 'https://okxrelay.onrender.com/okx'

종목 리스트 가져오기 (USDT 무기한 선물만)

def get_usdt_swaps(): try: url = f"{BASE_URL}/public/instruments?instType=SWAP" response = requests.get(url, timeout=10) if response.status_code != 200: print(f"📦 OKX API 응답 상태 코드: {response.status_code}") print("📦 응답 본문 일부:", response.text[:300]) return [] data = response.json() return [x['instId'] for x in data['data'] if x['ctValCcy'] == 'USDT'] except Exception as e: print("❌ 종목 목록 가져오기 오류:", e) return []

캔들 데이터 가져오기 (proxy 통해)

def get_candles(symbol, bar): try: url = f"{BASE_URL}/market/candles?instId={symbol}&bar={bar}&limit=100" response = requests.get(url, timeout=10) if response.status_code != 200: return None data = response.json()['data'] df = pd.DataFrame(data, columns=["timestamp", "open", "high", "low", "close", "volume", "volumeCcy"]) df = df.iloc[::-1].copy() df['close'] = df['close'].astype(float) return df except: return None

급등/급락 조건 검사

def analyze_symbol(symbol): try: result = []

# 5분봉 (급등/급락)
    df5 = get_candles(symbol, '5m')
    if df5 is not None and len(df5) > 30:
        df5['rsi'] = RSIIndicator(df5['close'], window=14).rsi()
        bb5 = BollingerBands(df5['close'], window=20, window_dev=3)
        df5['bb_upper'] = bb5.bollinger_hband()
        df5['bb_lower'] = bb5.bollinger_lband()
        c0, c1 = df5['close'].iloc[-1], df5['close'].iloc[-2]
        pct_change_5m = (c0 - c1) / c1 * 100

        if pct_change_5m >= 1.5 and df5['rsi'].iloc[-1] > 70 and c0 > df5['bb_upper'].iloc[-1]:
            result.append("🚀 5분봉 급등 조건 충족")
        if pct_change_5m <= -1.5 and df5['rsi'].iloc[-1] < 30 and c0 < df5['bb_lower'].iloc[-1]:
            result.append("⚠️ 5분봉 급락 조건 충족")

    # 15분봉 (급등/급락)
    df15 = get_candles(symbol, '15m')
    if df15 is not None and len(df15) > 30:
        df15['rsi'] = RSIIndicator(df15['close'], window=14).rsi()
        bb15 = BollingerBands(df15['close'], window=20, window_dev=3)
        df15['bb_lower'] = bb15.bollinger_lband()
        df15['bb_upper'] = bb15.bollinger_hband()
        c15 = df15['close'].iloc[-1]

        if df15['rsi'].iloc[-1] > 70 and c15 > df15['bb_upper'].iloc[-1]:
            result.append("🚀 15분봉 RSI+BB 상단 급등 조건 충족")
        if df15['rsi'].iloc[-1] < 30 and c15 < df15['bb_lower'].iloc[-1]:
            result.append("⚠️ 15분봉 RSI+BB 하단 급락 조건 충족")

    return result

except Exception as e:
    print(f"❌ {symbol} 분석 오류:", e)
    return []

메인 실행 함수

def run_bot(): symbols = get_usdt_swaps() send_telegram_message(f"✅ OKX 급등 감지 봇 작동 시작됨\n총 감시 종목 수: **{len(symbols)}**개")

for symbol in symbols:
    conditions = analyze_symbol(symbol)
    if conditions:
        message = f"📊 *{symbol}* 감지됨\n" + "\n".join(conditions)
        print(message)
        send_telegram_message(message)
    time.sleep(0.3)

Flask 헬스체크 서버

app = Flask(name)

@app.route("/") def index(): return f'✅ OKX 급등 감지 봇 작동 중! ({datetime.datetime.utcnow()})'

if name == 'main': run_bot() app.run(host='0.0.0.0', port=10000)

