import os
import json
import time
import signal
import requests
import websocket
from flask import Flask
from threading import Thread
from datetime import datetime

import state
import config as cfg
from telegram import send_telegram
from okx_api import get_positions, get_open_algo_orders
from indicators import bootstrap_closed_20
from entry import try_enter_on_price
from exit import monitor_oco_orders, _save_pnl_state
from trade_log import _upsert_trade_log, apply_recommendations

app = Flask('')

# ✅ WebSocket 인스턴스 참조 (심볼 갱신 시 subscribe/unsubscribe 사용)
_ws_instance = None

def _apply_tick_to_bar(symbol, price, ts_sec, trade_volCcy):
    now      = int(ts_sec)
    bucket_5 = (now // cfg.BAR_SEC) * cfg.BAR_SEC
    s5       = state.bar_state[symbol]

    if s5["bucket"] is None:
        s5["bucket"] = bucket_5
        s5["o"] = s5["h"] = s5["l"] = s5["c"] = float(price)
        s5["volCcy"] = trade_volCcy
        return

    if bucket_5 > s5["bucket"]:
        state.closed_closes[symbol].append(float(s5["c"] if s5["c"] is not None else s5["o"]))
        state.closed_highs[symbol].append(float(s5["h"]))
        state.closed_lows[symbol].append(float(s5["l"]))
        state.closed_volumes[symbol].append(float(s5["volCcy"]))
        s5["bucket"] = bucket_5
        s5["o"] = s5["h"] = s5["l"] = s5["c"] = float(price)
        s5["volCcy"] = trade_volCcy
    else:
        c       = float(price)
        s5["c"] = c
        s5["h"] = max(s5["h"], c)
        s5["l"] = min(s5["l"], c)
        s5["volCcy"] += trade_volCcy

def _apply_tick_to_bar_10m_1h(symbol, price, ts_sec):
    now        = int(ts_sec)
    bucket_10m = (now // state.BAR_SEC_10M) * state.BAR_SEC_10M
    s10        = state.bar_state_10m[symbol]
    if s10["bucket"] is None:
        s10["bucket"] = bucket_10m; s10["o"] = s10["c"] = float(price)
    elif bucket_10m > s10["bucket"]:
        s10["bucket"] = bucket_10m; s10["o"] = s10["c"] = float(price)
    else:
        s10["c"] = float(price)

    bucket_1h = (now // state.BAR_SEC_1H) * state.BAR_SEC_1H
    s1h       = state.bar_state_1h[symbol]
    if s1h["bucket"] is None:
        s1h["bucket"] = bucket_1h; s1h["o"] = s1h["c"] = float(price)
    elif bucket_1h > s1h["bucket"]:
        s1h["bucket"] = bucket_1h; s1h["o"] = s1h["c"] = float(price)
    else:
        s1h["c"] = float(price)

def get_all_symbols(top_n=100):
    try:
        inst = requests.get(
            "https://www.okx.com/api/v5/public/instruments?instType=SWAP", timeout=10
        ).json()
        tick = requests.get(
            "https://www.okx.com/api/v5/market/tickers?instType=SWAP", timeout=10
        ).json()
        tick_data = [
            t for t in tick.get("data", [])
            if t.get("instId", "").endswith("USDT-SWAP")
        ]
        metas = []
        for i in inst.get("data", []):
            inst_id = i.get("instId", "")
            if i.get("settleCcy") != "USDT" or not inst_id.endswith("USDT-SWAP"):
                continue
            if i.get("state") != "live":
                continue
            ticker = next((t for t in tick_data if t["instId"] == inst_id), None)
            if not ticker:
                continue
            vol      = float(ticker.get("volCcy24h", 0) or 0)
            last     = float(ticker.get("last", 0) or 0)
            turnover = vol * last
            metas.append({
                "instId":   inst_id,
                "ctVal":    float(i.get("ctVal", 1)),
                "lotSz":    float(i.get("lotSz", 1)),
                "tickSz":   float(i.get("tickSz", 0.001)),
                "minSz":    float(i.get("minSz", 1)),
                "turnover": turnover,
            })
        top = sorted(metas, key=lambda x: x["turnover"], reverse=True)[:top_n]
        state.instrument_meta.update({m["instId"]: m for m in top})
        return [m["instId"] for m in top]
    except Exception as e:
        print("get_all_symbols 오류:", e)
        return []

# ✅ 6시간마다 심볼 갱신
def _refresh_symbols():
    REFRESH_INTERVAL = 6 * 3600  # 6시간
    while True:
        time.sleep(REFRESH_INTERVAL)
        try:
            global _ws_instance
            if _ws_instance is None:
                continue

            print(f"🔄 App {state.APP_ID} 심볼 갱신 시작...")

            res = requests.get(
                f"{state.CONTROL_SERVER_URL}/symbols/{state.APP_ID}", timeout=10
            ).json()
            new_symbols = set(res)

            get_all_symbols(top_n=state.TOTAL_SYMBOLS)

            old_symbols = set(state.symbols_cache)

            with state.lock:
                active_symbols = set(
                    sym for sym, pos in state.positions.items()
                    if pos.get("main") and pos["main"].get("alive")
                )

            to_remove = old_symbols - new_symbols - active_symbols
            to_add    = new_symbols - old_symbols

            if to_remove:
                args = [{"channel": "trades", "instId": s} for s in to_remove]
                _ws_instance.send(json.dumps({"op": "unsubscribe", "args": args}))
                print(f"[심볼갱신] 제거: {len(to_remove)}개")

            if to_add:
                batch = 80
                to_add_list = list(to_add)
                for i in range(0, len(to_add_list), batch):
                    args = [{"channel": "trades", "instId": s} for s in to_add_list[i:i+batch]]
                    _ws_instance.send(json.dumps({"op": "subscribe", "args": args}))
                    time.sleep(0.25)
                for s in to_add:
                    bootstrap_closed_20(s)
                    time.sleep(0.05)
                print(f"[심볼갱신] 추가: {len(to_add)}개")

            state.symbols_cache[:] = list(new_symbols | active_symbols)

            send_telegram(
                f"🔄 App {state.APP_ID} 심볼 갱신 완료\n"
                f"총 {len(state.symbols_cache)}개\n"
                f"추가: {len(to_add)} | 제거: {len(to_remove)}"
            )

        except Exception as e:
            print(f"[_refresh_symbols] 오류: {e}")
            send_telegram(f"⚠️ 심볼 갱신 실패 (App {state.APP_ID})\n{e}")

def load_initial_state():
    try:
        if os.path.exists("blacklist.json"):
            with open("blacklist.json", "r") as f:
                state.SYMBOL_BLACKLIST = set(json.load(f))
            print(f"✅ 블랙리스트 로드: {len(state.SYMBOL_BLACKLIST)}개")
            if state.SYMBOL_BLACKLIST:
                send_telegram(
                    f"⛔ 블랙리스트 로드: {len(state.SYMBOL_BLACKLIST)}개\n"
                    f"{', '.join(list(state.SYMBOL_BLACKLIST)[:10])}"
                )
    except Exception as e:
        print(f"블랙리스트 로드 실패: {e}")

    try:
        if os.path.exists("pnl_state.json"):
            with open("pnl_state.json", "r") as f:
                saved = json.load(f)
                today = datetime.utcnow().strftime("%Y-%m-%d")
                if saved.get("date") == today:
                    state.total_pnl_usdt = saved.get("total_pnl_usdt", 0.0)
                    send_telegram(f"ℹ️ 이전 PnL 로드: {state.total_pnl_usdt:+.2f} USDT")
                else:
                    state.total_pnl_usdt = 0.0
                    send_telegram("ℹ️ 새 날짜 감지 → PnL 초기화")
    except Exception as e:
        print(f"PnL 상태 로드 실패: {e}")

    all_open_pos  = get_positions()
    current_algos = get_open_algo_orders()

    with state.lock:
        for pos in all_open_pos:
            symbol = pos["instId"]
            pos_sz = abs(float(pos.get("pos", 0)))
            if pos_sz <= 0:
                continue
            pos_info = {
                "side":           "buy" if float(pos.get("pos", 0)) > 0 else "sell",
                "entry":          float(pos.get("avgPx", 0)),
                "sz":             pos_sz,
                "oco_id":         None,
                "time":           time.time(),
                "alive":          True,
                "exited":         False,
                "exit_intent":    None,
                "zone":           "RESTORED",
                "signal_id":      f"RESTORE-{symbol}",
                "entry_time":     time.time(),
                "max_profit_pct": 0.0,
                "entry_bucket":   None,
                "restored":       True,
            }
            matching = next(
                (o for o in current_algos
                 if o["instId"] == symbol and abs(float(o["sz"]) - pos_sz) < 1e-8),
                None
            )
            if matching:
                pos_info["oco_id"] = matching["algoId"]
            state.positions.setdefault(symbol, {})
            state.positions[symbol]["main"] = pos_info

    if state.positions:
        send_telegram(f"✅ 포지션 로드 완료: {len(state.positions)}개")
    else:
        send_telegram("✅ 활성 포지션 없음.")

def on_message(ws, message):
    try:
        if message == "ping":
            ws.send("pong"); return
        if message == "pong":
            return
        data    = json.loads(message)
        arg     = data.get("arg", {})
        channel = arg.get("channel")
        symbol  = arg.get("instId")

        if channel == "trades":
            for trade in data.get("data", []):
                price        = float(trade["px"])
                ts_sec       = int(int(trade["ts"]) / 1000)
                sz           = float(trade["sz"])
                trade_volCcy = price * sz
                state.recent_prices[symbol].append(price)
                state.recent_tick_volumes[symbol].append((ts_sec, trade_volCcy))
                _apply_tick_to_bar(symbol, price, ts_sec, trade_volCcy)
                _apply_tick_to_bar_10m_1h(symbol, price, ts_sec)
                try_enter_on_price(symbol, price)
    except Exception as e:
        print("on_message 예외:", e)

def _send_ws_ping(ws):
    while True:
        try:
            time.sleep(25)
            ws.send("ping")
        except Exception as e:
            print(f"WS ping 실패: {e}"); break

def _bootstrap_all():
    print(f"🔄 App {state.APP_ID} 데이터 로딩 시작...")
    for s in state.symbols_cache:
        bootstrap_closed_20(s)
        time.sleep(0.05)
    print(f"✅ App {state.APP_ID} 데이터 로드 완료")

def on_open(ws):
    global _ws_instance
    _ws_instance = ws

    get_all_symbols(top_n=state.TOTAL_SYMBOLS)
    res = requests.get(
        f"{state.CONTROL_SERVER_URL}/symbols/{state.APP_ID}", timeout=10
    ).json()
    state.symbols_cache[:] = res
    if state.BTC_SYMBOL not in state.symbols_cache:
        state.symbols_cache.insert(0, state.BTC_SYMBOL)

    batch = 80
    for i in range(0, len(state.symbols_cache), batch):
        args = [{"channel": "trades", "instId": s} for s in state.symbols_cache[i:i+batch]]
        ws.send(json.dumps({"op": "subscribe", "args": args}))
        time.sleep(0.25)

    Thread(target=_bootstrap_all, daemon=True).start()
    Thread(target=_send_ws_ping, args=(ws,), daemon=True).start()

    if not state.ws_connected_once:
        send_telegram(f"🟢 App {state.APP_ID} 구독 완료: {len(state.symbols_cache)} symbols")
        state.ws_connected_once = True

def on_close(ws, code, msg):
    global _ws_instance
    _ws_instance = None
    print("WebSocket 종료:", code, msg)

def on_error(ws, error):
    print(f"⚠️ WS 에러: {error}")

def start_ws_thread():
    if state.WS_STARTED:
        return
    state.WS_STARTED = True
    def _run_ws():
        while True:
            try:
                ws = websocket.WebSocketApp(
                    "wss://ws.okx.com/ws/v5/public",
                    on_open=on_open, on_message=on_message,
                    on_close=on_close, on_error=on_error,
                )
                ws.run_forever(ping_interval=0)
            except Exception as e:
                print("WS thread error:", e)
            time.sleep(3)
    Thread(target=_run_ws, daemon=True).start()

def fetch_control_commands():
    try:
        url = f"{state.CONTROL_SERVER_URL}/get/{state.APP_ID}"
        res = requests.get(url, timeout=3).json()

        for cmd in res:
            cmd_id = cmd.get("id")
            if cmd_id in state.processed_cmd_ids:
                continue
            state.processed_cmd_ids.add(cmd_id)
            c = cmd.get("cmd")

            if c == "켜":
                state.trading_enabled      = True
                state.risk_guard_triggered = False
                with state.lock:
                    for sym, pos in list(state.positions.items()):
                        main = pos.get("main")
                        if main and main.get("alive"):
                            main["skip_time_exit"] = True
                send_telegram(f"🟢 거래 활성화 (APP {state.APP_ID})")

            elif c == "꺼":
                state.trading_enabled = False
                send_telegram(f"🔴 거래 중지 (APP {state.APP_ID})")

            elif c == "청산":
                send_telegram(f"⚠️ 전체 포지션 청산 실행 (APP {state.APP_ID})")

            elif c == "상태":
                with state.lock:
                    pos_count = sum(
                        1 for p in state.positions.values()
                        if p.get("main") and p["main"].get("alive")
                    )
                learned_lines = []
                for k, v in state.LEARNED_MIN_DIST.items():
                    learned_lines.append(f"  {k}: {v:.3f}%")
                learned_str = "\n".join(learned_lines) if learned_lines else "  없음(동적계산)"

                send_telegram(
                    f"📊 FastBand v{getattr(state, 'APP_VERSION', '2.0.0')} 상태 (APP {state.APP_ID})\n\n"
                    f"🟢 거래상태\n"
                    f"거래활성화 : {'ON' if state.trading_enabled else 'OFF'}\n"
                    f"활성포지션 : {pos_count}개\n"
                    f"누적PnL : {state.total_pnl_usdt:.2f} USDT\n\n"
                    f"💰 진입 및 익절/손절 (v2)\n"
                    f"사이즈 : {state.usdt_amount} USDT\n"
                    f"기본TP/SL : {state.TP_PCT*100:.2f}% | {state.SL_PCT*100:.2f}%\n"
                    f"ATR 동적 TP/SL : {'ON' if state.ATR_DYNAMIC_ENABLED else 'OFF'} (TP {state.ATR_TP_MULT}x / SL {state.ATR_SL_MULT}x)\n"
                    f"1차 분할익절 : {'ON' if state.PARTIAL_TP_ENABLED else 'OFF'} (수량 {int(state.PARTIAL_TP_RATIO*100)}% / 목표 {state.PARTIAL_TP_PCT*100:.1f}%)\n"
                    f"본절 스탑(BEP) : {'ON' if state.BEP_STOP_ENABLED else 'OFF'} (+{state.BEP_BUFFER_PCT*100:.2f}%)\n"
                    f"트레일링 익절 : {'ON' if state.TRAIL_ENABLED else 'OFF'} (발동 {state.TRAIL_ACTIVATE_PCT}% / 감시 {state.TRAIL_DISTANCE_PCT}%)\n\n"
                    f"🛡️ 안전 필터 (v2)\n"
                    f"BTC 서킷브레이커 : {'ON' if state.BTC_CIRCUIT_ENABLED else 'OFF'} (±{state.BTC_5M_SURGE_PCT}% / {state.BTC_COOLDOWN_SEC}초)\n"
                    f"ADX : {'ON' if state.adx_filter_enabled else 'OFF'} ({state.ADX_THRESHOLD}) | "
                    f"거래량소진 : {'ON' if state.vol_exhausted_filter_enabled else 'OFF'}\n"
                    f"RSI : {'ON' if state.RSI_FILTER_ENABLED else 'OFF'} "
                    f"(LONG≤{state.RSI_LONG_MAX}/SHORT≥{state.RSI_SHORT_MIN})\n"
                    f"추가이격 : {'ON' if state.EXTRA_DIST_ENABLED else 'OFF'}\n"
                    f"슬라이딩 : {'ON' if state.SLIDING_ENABLED else 'OFF'}\n"
                    f"슬리피지청산 : {'ON' if state.ENTRY_SLIP_ENABLED else 'OFF'} "
                    f"({state.ENTRY_SLIP_PCT*100:.1f}%)\n\n"
                    f"📐 추가이격 구간\n"
                    f"구간1: {state.EXTRA_DIST_RANGES[0]['min']}~{state.EXTRA_DIST_RANGES[0]['max']}% "
                    f"({state.EXTRA_DIST_RANGES[0]['pct']}%)\n"
                    f"구간2: {state.EXTRA_DIST_RANGES[1]['min']}~{state.EXTRA_DIST_RANGES[1]['max']}% "
                    f"({state.EXTRA_DIST_RANGES[1]['pct']}%)\n"
                    f"구간3: {state.EXTRA_DIST_RANGES[2]['min']}%~ "
                    f"({state.EXTRA_DIST_RANGES[2]['pct']}%)\n\n"
                    f"⚠️ 일일 손실한도 : {state.MAX_LOSS_USDT} USDT\n"
                    f"⛔ 블랙리스트: {len(state.SYMBOL_BLACKLIST)}개\n\n"
                    f"🧠 학습된 이격거리\n{learned_str}"
                )

            elif c == "초기화":
                state.total_pnl_usdt      = 0.0
                state.risk_guard_triggered = False
                state.trading_enabled      = True
                _save_pnl_state()
                send_telegram(f"🔄 PnL 초기화 완료 (APP {state.APP_ID})")

            elif c == "사이즈":
                new_size = cmd.get("value")
                if new_size and new_size > 0:
                    state.usdt_amount = new_size
                    send_telegram(f"💰 사이즈 변경 (APP {state.APP_ID})\n{state.usdt_amount} USDT")

            elif c == "tp":
                new_val = cmd.get("value")
                if new_val and new_val > 0:
                    state.TP_PCT = new_val / 100
                    send_telegram(f"🎯 TP 변경 (APP {state.APP_ID})\n{new_val}%")

            elif c == "sl":
                new_val = cmd.get("value")
                if new_val and new_val > 0:
                    state.SL_PCT = new_val / 100
                    send_telegram(f"🛑 SL 변경 (APP {state.APP_ID})\n{new_val}%")

            elif c == "대기시간":
                new_val = cmd.get("value")
                if new_val is not None and new_val >= 0:
                    state.ENTRY_DELAY_SEC = new_val
                    send_telegram(f"⏱ 대기시간 변경 (APP {state.APP_ID})\n{new_val}초")

            elif c == "adx켜":
                state.adx_filter_enabled = True
                send_telegram(f"✅ ADX 필터 활성화 (APP {state.APP_ID})")
            elif c == "adx꺼":
                state.adx_filter_enabled = False
                send_telegram(f"🔓 ADX 필터 비활성화 (APP {state.APP_ID})")
            elif c == "adx":
                new_val = cmd.get("value")
                if new_val is not None and new_val > 0:
                    state.ADX_THRESHOLD = new_val
                    send_telegram(f"📐 ADX 기준값 변경 (APP {state.APP_ID})\n{new_val}")

            elif c == "vol켜":
                state.vol_exhausted_filter_enabled = True
                send_telegram(f"✅ 거래량 소진 필터 활성화 (APP {state.APP_ID})")
            elif c == "vol꺼":
                state.vol_exhausted_filter_enabled = False
                send_telegram(f"🔓 거래량 소진 필터 비활성화 (APP {state.APP_ID})")

            elif c == "btc서킷켜":
                state.BTC_CIRCUIT_ENABLED = True
                send_telegram(f"✅ BTC 서킷 브레이커 활성화 (APP {state.APP_ID})")
            elif c == "btc서킷꺼":
                state.BTC_CIRCUIT_ENABLED = False
                send_telegram(f"🔓 BTC 서킷 브레이커 비활성화 (APP {state.APP_ID})")

            elif c == "atr켜":
                state.ATR_DYNAMIC_ENABLED = True
                send_telegram(f"✅ ATR 동적 TP/SL 활성화 (APP {state.APP_ID})")
            elif c == "atr꺼":
                state.ATR_DYNAMIC_ENABLED = False
                send_telegram(f"🔓 ATR 동적 TP/SL 비활성화 (고정 TP/SL 모드)")

            elif c == "분할익절켜":
                state.PARTIAL_TP_ENABLED = True
                send_telegram(f"✅ 1차 분할 익절(50%) 활성화 (APP {state.APP_ID})")
            elif c == "분할익절꺼":
                state.PARTIAL_TP_ENABLED = False
                send_telegram(f"🔓 1차 분할 익절 비활성화 (APP {state.APP_ID})")

            elif c == "트레일켜":
                state.TRAIL_ENABLED = True
                send_telegram(f"✅ 트레일링 익절 활성화 (APP {state.APP_ID})")
            elif c == "트레일꺼":
                state.TRAIL_ENABLED = False
                send_telegram(f"🔓 트레일링 익절 비활성화 (APP {state.APP_ID})")

            elif c == "rsi켜":
                state.RSI_FILTER_ENABLED = True
                send_telegram(
                    f"✅ RSI 필터 활성화 (APP {state.APP_ID})\n"
                    f"LONG≤{state.RSI_LONG_MAX} / SHORT≥{state.RSI_SHORT_MIN}"
                )
            elif c == "rsi꺼":
                state.RSI_FILTER_ENABLED = False
                send_telegram(f"🔓 RSI 필터 비활성화 (APP {state.APP_ID})")
            elif c == "rsi롱":
                new_val = cmd.get("value")
                if new_val is not None and 0 < new_val < 100:
                    state.RSI_LONG_MAX = new_val
                    send_telegram(f"📐 RSI LONG 기준 변경 (APP {state.APP_ID})\n≤{new_val}")
            elif c == "rsi숏":
                new_val = cmd.get("value")
                if new_val is not None and 0 < new_val < 100:
                    state.RSI_SHORT_MIN = new_val
                    send_telegram(f"📐 RSI SHORT 기준 변경 (APP {state.APP_ID})\n≥{new_val}")

            elif c == "추가이격켜":
                state.EXTRA_DIST_ENABLED = True
                send_telegram(f"✅ 추가 이격 활성화 (APP {state.APP_ID})")
            elif c == "추가이격꺼":
                state.EXTRA_DIST_ENABLED = False
                send_telegram(f"🔓 추가 이격 비활성화 (APP {state.APP_ID})")
            elif c == "변화율합":
                new_val = cmd.get("value")
                if new_val and new_val > 0:
                    state.EXTRA_DIST_THRESHOLD = new_val
                    send_telegram(f"📐 변화율합 기준 변경 (APP {state.APP_ID})\n{new_val}%")
            elif c == "이격비율":
                new_val = cmd.get("value")
                if new_val and new_val > 0:
                    state.EXTRA_DIST_PCT = new_val
                    send_telegram(f"📐 이격비율 변경 (APP {state.APP_ID})\n{new_val}%")
            elif c == "이격1":
                new_val = cmd.get("value")
                if new_val is not None and new_val >= 0:
                    state.EXTRA_DIST_RANGES[0]["pct"] = new_val
                    send_telegram(f"📐 구간1 이격비율 변경 (APP {state.APP_ID})\n{new_val}%")
            elif c == "이격2":
                new_val = cmd.get("value")
                if new_val is not None and new_val >= 0:
                    state.EXTRA_DIST_RANGES[1]["pct"] = new_val
                    send_telegram(f"📐 구간2 이격비율 변경 (APP {state.APP_ID})\n{new_val}%")
            elif c == "이격3":
                new_val = cmd.get("value")
                if new_val is not None and new_val >= 0:
                    state.EXTRA_DIST_RANGES[2]["pct"] = new_val
                    send_telegram(f"📐 구간3 이격비율 변경 (APP {state.APP_ID})\n{new_val}%")

            elif c == "구간0":
                new_val = cmd.get("value")
                if new_val is not None and new_val >= 0:
                    if new_val < state.EXTRA_DIST_RANGES[0]["max"]:
                        state.EXTRA_DIST_RANGES[0]["min"] = new_val
                        send_telegram(
                            f"📐 구간1 시작값 변경 (APP {state.APP_ID})\n"
                            f"{new_val}% ~ {state.EXTRA_DIST_RANGES[0]['max']}%"
                        )
                    else:
                        send_telegram(f"⚠️ 구간1 시작값이 끝값 이상입니다")
            elif c == "구간1":
                new_val = cmd.get("value")
                if new_val and new_val > state.EXTRA_DIST_RANGES[0]["min"]:
                    state.EXTRA_DIST_RANGES[0]["max"] = new_val
                    state.EXTRA_DIST_RANGES[1]["min"] = new_val
                    send_telegram(f"📐 구간1/2 경계 변경 (APP {state.APP_ID})\n{new_val}%")
            elif c == "구간2":
                new_val = cmd.get("value")
                if new_val and new_val > state.EXTRA_DIST_RANGES[1]["min"]:
                    state.EXTRA_DIST_RANGES[1]["max"] = new_val
                    state.EXTRA_DIST_RANGES[2]["min"] = new_val
                    send_telegram(f"📐 구간2/3 경계 변경 (APP {state.APP_ID})\n{new_val}%")
            elif c == "구간1켜":
                state.EXTRA_DIST_RANGES[0]["enabled"] = True
                send_telegram(f"✅ 구간1 활성화 (APP {state.APP_ID})")
            elif c == "구간1꺼":
                state.EXTRA_DIST_RANGES[0]["enabled"] = False
                send_telegram(f"🔓 구간1 비활성화 (APP {state.APP_ID})")
            elif c == "구간2켜":
                state.EXTRA_DIST_RANGES[1]["enabled"] = True
                send_telegram(f"✅ 구간2 활성화 (APP {state.APP_ID})")
            elif c == "구간2꺼":
                state.EXTRA_DIST_RANGES[1]["enabled"] = False
                send_telegram(f"🔓 구간2 비활성화 (APP {state.APP_ID})")
            elif c == "구간3켜":
                state.EXTRA_DIST_RANGES[2]["enabled"] = True
                send_telegram(f"✅ 구간3 활성화 (APP {state.APP_ID})")
            elif c == "구간3꺼":
                state.EXTRA_DIST_RANGES[2]["enabled"] = False
                send_telegram(f"🔓 구간3 비활성화 (APP {state.APP_ID})")

            elif c == "이격mid5m":
                new_val = cmd.get("value")
                if new_val is not None and new_val >= 0:
                    state.LEARNED_MIN_DIST["MID_5M"] = round(new_val, 4)
                    send_telegram(f"🧠 MID/5M 이격 설정 (APP {state.APP_ID})\n{new_val}%")
            elif c == "이격mid10m":
                new_val = cmd.get("value")
                if new_val is not None and new_val >= 0:
                    state.LEARNED_MIN_DIST["MID_10M+1H"] = round(new_val, 4)
                    send_telegram(f"🧠 MID/10M+1H 이격 설정 (APP {state.APP_ID})\n{new_val}%")
            elif c == "이격deep5m":
                new_val = cmd.get("value")
                if new_val is not None and new_val >= 0:
                    state.LEARNED_MIN_DIST["DEEP_5M"] = round(new_val, 4)
                    send_telegram(f"🧠 DEEP/5M 이격 설정 (APP {state.APP_ID})\n{new_val}%")
            elif c == "이격deep10m":
                new_val = cmd.get("value")
                if new_val is not None and new_val >= 0:
                    state.LEARNED_MIN_DIST["DEEP_10M+1H"] = round(new_val, 4)
                    send_telegram(f"🧠 DEEP/10M+1H 이격 설정 (APP {state.APP_ID})\n{new_val}%")

            elif c == "분석":
                try:
                    res = requests.post(
                        f"{state.CONTROL_SERVER_URL}/run_analysis",
                        timeout=10
                    )
                    if res.status_code != 200:
                        send_telegram(f"❌ 분석 요청 실패: {res.status_code}")
                except Exception as e:
                    send_telegram(f"❌ 분석 오류: {e}")

            elif c == "이격적용":
                try:
                    res  = requests.get(
                        f"{state.CONTROL_SERVER_URL}/trade_log/analyze",
                        timeout=5
                    ).json()
                    logs = res.get("data", [])
                    send_telegram(apply_recommendations(logs))
                except Exception as e:
                    send_telegram(f"❌ 이격적용 실패: {e}")

            elif c == "이격초기화":
                state.LEARNED_MIN_DIST  = {}
                state.LEARNED_EXTRA_PCT = {}
                send_telegram(f"🔄 학습 이격거리 초기화 → 동적계산 복귀 (APP {state.APP_ID})")

            elif c == "추가이격초기화":
                state.EXTRA_DIST_RANGES = [
                    {"min": 10.0, "max": 12.0,  "pct": 2.0,  "enabled": True},
                    {"min": 12.0, "max": 18.0,  "pct": 5.0,  "enabled": True},
                    {"min": 18.0, "max": 999.0, "pct": 10.0, "enabled": True},
                ]
                state.LEARNED_EXTRA_PCT = {}
                send_telegram(
                    f"🔄 추가이격 구간 초기화 완료 (APP {state.APP_ID})\n"
                    f"구간1: 10.0~12.0% (2.0%)\n"
                    f"구간2: 12.0~18.0% (5.0%)\n"
                    f"구간3: 18.0%~ (10.0%)"
                )

            # ── 슬리피지청산 (청산/slip 두 이름 모두 지원) ──
            elif c in ("청산켜", "slip켜"):
                state.ENTRY_SLIP_ENABLED = True
                send_telegram(
                    f"✅ 슬리피지청산 활성화 (APP {state.APP_ID})\n"
                    f"기준: {state.ENTRY_SLIP_PCT*100:.1f}%"
                )
            elif c in ("청산꺼", "slip꺼"):
                state.ENTRY_SLIP_ENABLED = False
                send_telegram(f"🔓 슬리피지청산 비활성화 (APP {state.APP_ID})")
            elif c in ("청산비율", "slip비율"):
                new_val = cmd.get("value")
                if new_val is not None and 0 < new_val < 100:
                    state.ENTRY_SLIP_PCT = new_val / 100
                    send_telegram(f"📐 슬리피지청산 비율 변경 (APP {state.APP_ID})\n{new_val}%")

            elif c == "슬라이딩켜":
                state.SLIDING_ENABLED = True
                send_telegram(f"✅ 슬라이딩 활성화 (APP {state.APP_ID})")
            elif c == "슬라이딩꺼":
                state.SLIDING_ENABLED = False
                send_telegram(f"🔓 슬라이딩 비활성화 (APP {state.APP_ID})\n최초 타겟가 고정 대기")

            elif c == "손실한도":
                new_val = cmd.get("value")
                if new_val is not None and new_val < 0:
                    state.MAX_LOSS_USDT = new_val
                    send_telegram(f"📐 일일 손실 한도 변경 (APP {state.APP_ID})\n{new_val} USDT")

            elif c == "명령어":
                send_telegram(
                    f"📋 FastBand 명령어 (APP {state.APP_ID})\n\n"
                    f"🟢 거래제어\n켜 / 꺼 / 청산 / 상태 / 초기화\n\n"
                    f"💰 포지션\n사이즈 100 / tp 1.5 / sl 2.0 / 대기시간 2\n\n"
                    f"📈 필터\nadx켜 / adx꺼 / adx 25 / vol켜 / vol꺼\n"
                    f"rsi켜 / rsi꺼 / rsi롱 30 / rsi숏 70\n\n"
                    f"⚡ 슬리피지·슬라이딩\n"
                    f"slip켜 / slip꺼 / slip비율 1.5\n"
                    f"슬라이딩켜 / 슬라이딩꺼\n\n"
                    f"📐 추가이격\n추가이격켜 / 추가이격꺼\n"
                    f"변화율합 10 / 이격비율 1.0\n"
                    f"이격1 0.5 / 이격2 1.0 / 이격3 1.5\n\n"
                    f"📏 구간경계\n구간0 8 / 구간1 10 / 구간2 20\n"
                    f"구간1켜~구간3꺼\n\n"
                    f"🧠 학습이격 직접설정\n"
                    f"이격mid5m 0.8 / 이격mid10m 1.2\n"
                    f"이격deep5m 1.0 / 이격deep10m 1.5\n\n"
                    f"⚠️ 리스크\n손실한도 -300\n\n"
                    f"📊 분석\n분석 / 이격적용 / 이격초기화 / 추가이격초기화"
                )

    except Exception as e:
        print("컨트롤 서버 오류:", e)

def control_loop():
    while True:
        fetch_control_commands()
        time.sleep(2)

def shutdown_handler(signum, frame):
    print(f"🛑 종료 신호 ({signum})")
    try:
        send_telegram("🛑 FastBand 종료됨")
    except Exception:
        pass
    os._exit(0)

signal.signal(signal.SIGTERM, shutdown_handler)
signal.signal(signal.SIGINT,  shutdown_handler)

def start_app():
    if state.APP_STARTED:
        return
    state.APP_STARTED = True
    load_initial_state()
    Thread(target=monitor_oco_orders,  daemon=True).start()
    Thread(target=control_loop,        daemon=True).start()
    Thread(target=_refresh_symbols,    daemon=True).start()
    start_ws_thread()

@app.route("/")
def index():
    return f"FastBand Bot Running - App {state.APP_ID}"

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    send_telegram(f"🌪 FastBand Bot Started - App {state.APP_ID} (PORT {port})")
    start_app()
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)