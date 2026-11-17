import time
import json
from threading import Thread

import state
import config as cfg
from telegram import send_telegram
from okx_api import (
    okx_request, round_to_tick, get_rest_last_price,
    get_real_position_size, get_real_open_orders,
    set_leverage, place_limit_order, cancel_order,
    check_order_status, get_filled_info,
    close_position_oco_checked,
    wait_position_visible,
    adjust_size, place_market_order, cancel_algo_order,
)
from indicators import (
    get_bollinger, get_volume_ratio, get_adx, get_rsi,
    estimate_strength, classify_zone, calc_target_price,
    get_sliding_params_by_zone, check_vol_exhausted,
)
from exit import force_market_close_safe, _close_with_fallback
from trade_log import _upsert_trade_log

# =====================================================
# [v2.0.0 전략 개선 모듈] ATR / BTC 서킷브레이커 / 분할익절
# =====================================================

def calc_atr_pct(symbol, period=14):
    """
    최근 N개 5분봉 캔들의 고가/저가/종가를 바탕으로 ATR을 산출하고,
    현재가 대비 ATR 비율(%)을 반환합니다.
    """
    try:
        highs = list(state.closed_highs.get(symbol, []))
        lows = list(state.closed_lows.get(symbol, []))
        closes = list(state.closed_closes.get(symbol, []))

        if len(closes) < period + 1 or len(highs) < period + 1 or len(lows) < period + 1:
            return None

        tr_list = []
        sub_h = highs[-period:]
        sub_l = lows[-period:]
        sub_c_prev = closes[-period-1:-1]

        for h, l, c_prev in zip(sub_h, sub_l, sub_c_prev):
            tr = max(h - l, abs(h - c_prev), abs(l - c_prev))
            tr_list.append(tr)

        if not tr_list:
            return None

        atr = sum(tr_list) / len(tr_list)
        curr_px = closes[-1] if closes else 1.0
        if curr_px <= 0:
            return None
        return (atr / curr_px) * 100
    except Exception as e:
        print(f"[calc_atr_pct] {symbol} 예외: {e}")
        return None

def _is_btc_circuit_breaker_active():
    """
    BTC 5분봉 등락률 또는 변동폭이 임계치(±1.0%)를 초과할 경우
    알트코인의 신규 역추세 진입을 차단하는 시장 서킷 브레이커입니다.
    """
    if not getattr(state, "BTC_CIRCUIT_ENABLED", True):
        return False

    now = time.time()
    if now - state.last_btc_circuit_time < state.BTC_COOLDOWN_SEC:
        return True

    btc_bar = state.bar_state.get(state.BTC_SYMBOL)
    if not btc_bar or not btc_bar.get("o") or not btc_bar.get("c"):
        return False

    btc_open = btc_bar["o"]
    btc_close = btc_bar["c"]
    if btc_open <= 0:
        return False

    btc_chg_pct = abs((btc_close - btc_open) / btc_open) * 100
    btc_high = btc_bar.get("h", btc_close)
    btc_low = btc_bar.get("l", btc_close)
    btc_range_pct = ((btc_high - btc_low) / btc_open) * 100

    if btc_chg_pct >= state.BTC_5M_SURGE_PCT or btc_range_pct >= (state.BTC_5M_SURGE_PCT * 1.3):
        state.last_btc_circuit_time = now
        send_telegram(
            f"⚡ [BTC 서킷 브레이커 발동]\n"
            f"비트코인 5분 등락: {btc_chg_pct:.2f}% (진폭: {btc_range_pct:.2f}%)\n"
            f"기준({state.BTC_5M_SURGE_PCT}%) 초과로 {state.BTC_COOLDOWN_SEC}초간 모든 알트코인 신규 진입을 정지합니다."
        )
        return True

    return False

def _execute_partial_tp_and_bep(symbol, pos, current_price):
    """
    1차 목표가 도달 시 50% 분할 익절을 시장가로 체결하고,
    남은 50%의 스탑로스를 본절가(BEP)로 이동시키며 트레일링 익절을 시작합니다.
    """
    try:
        side = pos["side"]
        entry = pos["entry"]
        total_sz = pos["sz"]
        old_oco_id = pos.get("oco_id")

        meta = state.instrument_meta.get(symbol, {"lotSz": 1, "minSz": 1})
        lot_sz = float(meta.get("lotSz", 1))
        min_sz = float(meta.get("minSz", 1))

        close_sz = adjust_size(total_sz * state.PARTIAL_TP_RATIO, lot_sz, min_sz)
        remain_sz = adjust_size(total_sz - close_sz, lot_sz, min_sz)

        if close_sz <= 0 or remain_sz < min_sz:
            print(f"[_execute_partial_tp_and_bep] {symbol} 수량 분할 불가 (총: {total_sz}, 50%: {close_sz}, 잔여: {remain_sz})")
            return

        close_side = "sell" if side == "buy" else "buy"

        # 1. 기존 OCO 취소
        if old_oco_id:
            cancel_algo_order(symbol, old_oco_id)
            time.sleep(0.2)

        # 2. 50% 시장가 분할 익절
        ord_id = place_market_order(symbol, close_side, close_sz, reduce_only=True)
        if not ord_id:
            send_telegram(f"⚠️ {symbol} 1차 분할 익절 시장가 주문 실패")
            return

        # 3. 본절 스탑(BEP) 및 2차 연장 TP 계산
        tp_pct = pos.get("tp_pct", state.TP_PCT)
        tp2_pct = tp_pct * state.TP2_EXTEND_MULT

        if side == "buy":
            bep_sl = round_to_tick(symbol, entry * (1 + state.BEP_BUFFER_PCT))
            tp2 = round_to_tick(symbol, entry * (1 + tp2_pct))
        else:
            bep_sl = round_to_tick(symbol, entry * (1 - state.BEP_BUFFER_PCT))
            tp2 = round_to_tick(symbol, entry * (1 - tp2_pct))

        # 4. 잔여 50%에 대한 신규 OCO 설정 (TP2 / 본절 SL)
        new_oco_id = close_position_oco_checked(symbol, close_side, remain_sz, tp2, bep_sl)

        with state.lock:
            pos["sz"] = remain_sz
            pos["oco_id"] = new_oco_id
            pos["bep_sl"] = bep_sl
            pos["partial_tp_done"] = True

        pnl_pct = (current_price - entry) / entry * 100 if side == "buy" else (entry - current_price) / entry * 100
        send_telegram(
            f"🎯 1차 분할 익절 완료 (50%) - {symbol}\n"
            f"익절수량: {close_sz} | 잔여수량: {remain_sz}\n"
            f"실현 PnL: +{pnl_pct:.2f}%\n"
            f"스탑로스: 본절가({bep_sl}) 상향 이동\n"
            f"2차 TP: {tp2} (+{tp2_pct*100:.2f}%) & 트레일링 익절 가동"
        )
    except Exception as e:
        print(f"[_execute_partial_tp_and_bep] {symbol} 오류: {e}")

def _risk_guard():
    if state.risk_guard_triggered:
        return False
    if state.total_pnl_usdt > state.MAX_LOSS_USDT:
        return True
    state.risk_guard_triggered = True
    state.trading_enabled      = False
    send_telegram(
        f"🚨 RISK GUARD 발동\n"
        f"누적손실: {state.total_pnl_usdt:.2f} USDT\n"
        f"기준: {state.MAX_LOSS_USDT:.2f} USDT\n"
        f"▶ 신규 진입 차단"
    )
    from exit import _save_pnl_state
    _save_pnl_state()
    return False

def _track_signal_timeline(symbol, signal_id, initial_side, signal_price, duration=4.0, interval=0.2):
    timeline = []
    start = time.time()
    try:
        while time.time() - start < duration:
            prices = state.recent_prices.get(symbol)
            px = prices[-1] if prices else None
            if px:
                elapsed = round(time.time() - start, 2)
                if initial_side == "buy":
                    slip_pct = (signal_price - px) / signal_price * 100
                else:
                    slip_pct = (px - signal_price) / signal_price * 100
                timeline.append({"t": elapsed, "slip_pct": round(slip_pct, 3)})
            time.sleep(interval)
    except Exception as e:
        print(f"[_track_signal_timeline] {symbol} 예외: {e}")
    finally:
        if timeline:
            _upsert_trade_log(signal_id, {"signal_timeline": timeline})

def _check_filled_despite_cancel(symbol, order_id, initial_side):
    time.sleep(0.3)
    sz, entry = get_filled_info(symbol, order_id, retries=2, delay=0.2)
    return sz, entry

def _track_cancel_outcome(symbol, signal_id, initial_side, cancel_price, target, duration=60.0, interval=1.0):
    would_have_filled = False
    closest_dist_pct  = None
    start = time.time()
    try:
        while time.time() - start < duration:
            prices = state.recent_prices.get(symbol)
            px = prices[-1] if prices else None
            if px:
                if initial_side == "buy":
                    hit = px <= target
                    dist_pct = (px - target) / target * 100
                else:
                    hit = px >= target
                    dist_pct = (target - px) / target * 100
                if closest_dist_pct is None or dist_pct < closest_dist_pct:
                    closest_dist_pct = dist_pct
                if hit:
                    would_have_filled = True
                    break
            time.sleep(interval)
    except Exception as e:
        print(f"[_track_cancel_outcome] {symbol} 예외: {e}")
    finally:
        _upsert_trade_log(signal_id, {
            "cancel_would_have_filled": would_have_filled,
            "cancel_closest_dist_pct":  round(closest_dist_pct, 4) if closest_dist_pct is not None else None,
            "cancel_price":             cancel_price,
            "cancel_target":            target,
        })

def _track_blocked_vol_outcome(symbol, signal_id, initial_side, target_price, duration=60.0, interval=1.0):
    reached_target   = False
    closest_dist_pct = None
    start = time.time()
    try:
        while time.time() - start < duration:
            prices = state.recent_prices.get(symbol)
            px = prices[-1] if prices else None
            if px:
                if initial_side == "buy":
                    hit = px <= target_price
                    dist_pct = (px - target_price) / target_price * 100
                else:
                    hit = px >= target_price
                    dist_pct = (target_price - px) / target_price * 100
                if closest_dist_pct is None or dist_pct < closest_dist_pct:
                    closest_dist_pct = dist_pct
                if hit:
                    reached_target = True
                    break
            time.sleep(interval)
    except Exception as e:
        print(f"[_track_blocked_vol_outcome] {symbol} 예외: {e}")
    finally:
        _upsert_trade_log(signal_id, {
            "blocked_vol_reached_target":   reached_target,
            "blocked_vol_closest_dist_pct": round(closest_dist_pct, 4) if closest_dist_pct is not None else None,
        })

def _delayed_confirm_and_enter(
    signal_id, symbol, initial_price, initial_side,
    base, entry_bucket, strength, zone,
    target, main_usdt,
    signal_type="5M", extra_pct=0.0, total_chg=0.0, extra_range_idx=-1,
):
    try:
        _, SLIDING_TOTAL, ENTRY_WAIT, _ = get_sliding_params_by_zone(zone)

        if get_real_position_size(symbol) > 0:
            return
        if get_real_open_orders(symbol):
            return
        if not set_leverage(symbol, cfg.LEVERAGE):
            send_telegram(f"❌ 레버리지 설정 실패 - {symbol}")
            return

        time.sleep(state.ENTRY_DELAY_SEC)

        if state.bar_state[symbol]["bucket"] != entry_bucket:
            return

        if state.vol_exhausted_filter_enabled:
            is_exhausted, vol_ratio_val, tick_count, early, late = check_vol_exhausted(symbol)
            if not is_exhausted:
                _upsert_trade_log(signal_id, {
                    "result":               "BLOCKED_VOL",
                    "vol_late_early_ratio": vol_ratio_val,
                    "vol_tick_count":       tick_count,
                    "vol_early":            early,
                    "vol_late":             late,
                    "vol_threshold":        state.VOL_THRESHOLD,
                    "vol_split":            state.VOL_SPLIT,
                })
                send_telegram(
                    f"⛔ 거래량 미소진 → 진입 취소\n{symbol}\n"
                    f"late/early: {vol_ratio_val} (기준: {state.VOL_THRESHOLD})"
                )
                Thread(
                    target=_track_blocked_vol_outcome,
                    args=(symbol, signal_id, initial_side, target),
                    daemon=True,
                ).start()
                return
            else:
                _upsert_trade_log(signal_id, {
                    "vol_late_early_ratio": vol_ratio_val,
                    "vol_tick_count":       tick_count,
                    "vol_early":            early,
                    "vol_late":             late,
                    "vol_threshold":        state.VOL_THRESHOLD,
                    "vol_split":            state.VOL_SPLIT,
                })

        vol_ratio = get_volume_ratio(symbol)
        s         = state.bar_state.get(symbol)

        slide_now = state.sliding_state.get(symbol)
        if slide_now and s and s.get("o"):
            best_2sec    = slide_now["best_price"]
            chg_pct_new  = abs((best_2sec - s["o"]) / s["o"]) * 100
            new_strength = estimate_strength(symbol, chg_pct_new, vol_ratio)
            new_zone     = classify_zone(new_strength)
            new_target, _, _ = calc_target_price(
                symbol=symbol, side=initial_side, base=base,
                price=best_2sec, vol_ratio=vol_ratio,
                strength=new_strength, zone=new_zone,
                chg_pct=chg_pct_new, signal_type=signal_type,
            )
            if initial_side == "buy" and new_target < target:
                target = new_target; zone = new_zone; strength = new_strength
            elif initial_side == "sell" and new_target > target:
                target = new_target; zone = new_zone; strength = new_strength

        main_id = place_limit_order(symbol, initial_side, target, main_usdt)
        if not main_id:
            send_telegram(f"❌ 주문 실패 - {symbol}")
            return

        sliding_occurred = False
        last_target      = target
        start_time       = time.time()
        last_slide_price = target
        check_interval   = 0
        candle_cancelled = False
        sz, entry        = 0, None

        while time.time() - start_time < SLIDING_TOTAL:
            if state.bar_state[symbol]["bucket"] != entry_bucket:
                cancel_order(symbol, main_id)
                sz_check, entry_check = _check_filled_despite_cancel(symbol, main_id, initial_side)
                if sz_check > 0:
                    send_telegram(
                        f"⚠️ 캔들변경 취소 시도했지만 이미 체결됨 → 정상 진입 처리\n{symbol}"
                    )
                    sz, entry = sz_check, entry_check
                    break
                else:
                    cancel_price = state.bar_state[symbol].get("c") or 0
                    _upsert_trade_log(signal_id, {"result": "CANCEL_CANDLE"})
                    send_telegram(f"🕯️ 캔들 변경 → 진입 취소 (슬라이딩 중)\n{symbol}")
                    candle_cancelled = True
                    Thread(
                        target=_track_cancel_outcome,
                        args=(symbol, signal_id, initial_side, cancel_price, last_target),
                        daemon=True,
                    ).start()
                    return

            check_interval += 1
            if check_interval % 5 == 0:
                st = check_order_status(symbol, main_id)
                if st in ("filled", "partially_filled"):
                    break

            slide = state.sliding_state.get(symbol)
            if not slide:
                time.sleep(0.3)
                continue

            best_price = slide["best_price"]
            slide_ok   = (
                (initial_side == "buy"  and best_price < last_slide_price) or
                (initial_side == "sell" and best_price > last_slide_price)
            )

            if slide_ok:
                st = check_order_status(symbol, main_id)
                if st in ("filled", "partially_filled"):
                    break
                cancel_order(symbol, main_id)
                time.sleep(0.2)
                st = check_order_status(symbol, main_id)
                if st in ("filled", "partially_filled"):
                    break
                sliding_occurred = True
                last_slide_price = best_price

                vol_ratio    = get_volume_ratio(symbol)
                s            = state.bar_state.get(symbol)
                if not s or not s.get("o"):
                    break

                chg_pct_new  = abs((best_price - s["o"]) / s["o"]) * 100
                new_strength = estimate_strength(symbol, chg_pct_new, vol_ratio)
                new_zone     = classify_zone(new_strength)
                new_target, _, _ = calc_target_price(
                    symbol=symbol, side=initial_side, base=base,
                    price=best_price, vol_ratio=vol_ratio,
                    strength=new_strength, zone=new_zone,
                    chg_pct=chg_pct_new, signal_type=signal_type,
                )

                if initial_side == "buy":
                    last_target = min(last_target, new_target)
                else:
                    last_target = max(last_target, new_target)

                last_target = round_to_tick(symbol, last_target)
                zone        = new_zone
                strength    = new_strength

                _upsert_trade_log(signal_id, {
                    "final_target": last_target,
                    "sliding":      True,
                })

                main_id = place_limit_order(symbol, initial_side, last_target, main_usdt)
                if not main_id:
                    return

            time.sleep(0.3)

        if sz <= 0:
            wait_start = time.time()
            while time.time() - wait_start < ENTRY_WAIT:
                st = check_order_status(symbol, main_id)
                if st in ("filled", "partially_filled"):
                    break
                if state.bar_state[symbol]["bucket"] != entry_bucket:
                    cancel_order(symbol, main_id)
                    sz_check, entry_check = _check_filled_despite_cancel(symbol, main_id, initial_side)
                    if sz_check > 0:
                        send_telegram(
                            f"⚠️ 캔들변경 취소 시도했지만 이미 체결됨 → 정상 진입 처리\n{symbol}"
                        )
                        sz, entry = sz_check, entry_check
                        break
                    else:
                        cancel_price = state.bar_state[symbol].get("c") or 0
                        _upsert_trade_log(signal_id, {"result": "CANCEL_CANDLE"})
                        send_telegram(f"🕯️ 캔들 변경 → 진입 취소 (대기 중)\n{symbol}")
                        candle_cancelled = True
                        Thread(
                            target=_track_cancel_outcome,
                            args=(symbol, signal_id, initial_side, cancel_price, last_target),
                            daemon=True,
                        ).start()
                        break
                time.sleep(0.5)

            if sz <= 0 and not candle_cancelled:
                cancel_order(symbol, main_id)
                sz_check, entry_check = _check_filled_despite_cancel(symbol, main_id, initial_side)
                if sz_check > 0:
                    send_telegram(
                        f"⚠️ ENTRY_WAIT 만료 후 취소했지만 이미 체결됨 → 정상 진입 처리\n{symbol}"
                    )
                    sz, entry = sz_check, entry_check
                else:
                    sz, entry = get_filled_info(symbol, main_id, retries=5, delay=0.4)
            elif sz <= 0:
                sz, entry = get_filled_info(symbol, main_id, retries=5, delay=0.4)

        cancel_order(symbol, main_id)

        if sz <= 0:
            if candle_cancelled:
                send_telegram(f"⌛ 캔들변경으로 미체결 종료 - {symbol}")
                return

            if state.bar_state[symbol]["bucket"] != entry_bucket:
                _upsert_trade_log(signal_id, {"result": "CANCEL_CANDLE"})
            else:
                s_now       = state.bar_state[symbol]
                candle_high = s_now.get("h")
                candle_low  = s_now.get("l")

                if initial_side == "buy":
                    target_hit = (candle_high or 0) >= last_target
                    mfe_pct    = ((candle_high - initial_price) / initial_price * 100
                                  if candle_high else 0)
                    mae_pct    = ((candle_low  - initial_price) / initial_price * 100
                                  if candle_low  else 0)
                else:
                    target_hit = (candle_low or 999999) <= last_target
                    mfe_pct    = ((initial_price - candle_low)  / initial_price * 100
                                  if candle_low  else 0)
                    mae_pct    = ((initial_price - candle_high) / initial_price * 100
                                  if candle_high else 0)

                _upsert_trade_log(signal_id, {
                    "result":              "MISS",
                    "final_target":        last_target,
                    "candle_high":         candle_high,
                    "candle_low":          candle_low,
                    "target_hit":          target_hit,
                    "max_favorable_price": candle_high if initial_side == "buy" else candle_low,
                    "max_adverse_price":   candle_low  if initial_side == "buy" else candle_high,
                    "mfe_pct":             round(mfe_pct, 2),
                    "mae_pct":             round(mae_pct, 2),
                })
            send_telegram(f"⌛ 미체결 종료 (ENTRY_WAIT 타임아웃) - {symbol}")
            return

        current_px = get_rest_last_price(symbol)
        if current_px:
            slip_pct = (
                (entry - current_px) / entry * 100 if initial_side == "buy"
                else (current_px - entry) / entry * 100
            )
            if state.ENTRY_SLIP_ENABLED and slip_pct > state.ENTRY_SLIP_PCT * 100:
                send_telegram(
                    f"⚡ 체결 직후 급락 감지 → 즉시 청산\n{symbol}\n"
                    f"체결가: {entry:.6f}\n현재가: {current_px:.6f}\n"
                    f"슬리피지: {slip_pct:.2f}%"
                )
                close_side = "sell" if initial_side == "buy" else "buy"
                success, method = _close_with_fallback(symbol, close_side, sz)
                if not success:
                    send_telegram(f"🚨 즉시 청산 실패 → 강제청산\n{symbol}")
                    with state.lock:
                        state.positions.setdefault(symbol, {})
                        state.positions[symbol]["main"] = {
                            "side":           initial_side,
                            "entry":          entry,
                            "sz":             sz,
                            "final_target":   last_target,
                            "oco_id":         None,
                            "signal_id":      signal_id,
                            "zone":           zone,
                            "entry_time":     time.time(),
                            "max_profit_pct": 0.0,
                            "position_high":  entry,
                            "position_low":   entry,
                            "alive":          True,
                            "exited":         False,
                            "entry_bucket":   entry_bucket,
                            "exit_intent":    "SLIP_CLOSE_FAILED",
                        }
                    force_market_close_safe(symbol)
                else:
                    current_px2 = get_rest_last_price(symbol) or current_px
                    meta  = state.instrument_meta.get(symbol, {"ctVal": 1})
                    ctVal = float(meta["ctVal"])
                    pnl   = (
                        (current_px2 - entry) * sz * ctVal if initial_side == "buy"
                        else (entry - current_px2) * sz * ctVal
                    )
                    pnl     = round(pnl, 2)
                    pnl_pct = pnl / max(entry * sz * ctVal, 1e-12) * 100
                    state.total_pnl_usdt += pnl
                    from exit import _save_pnl_state
                    _save_pnl_state()
                    _upsert_trade_log(signal_id, {
                        "result":       "HIT",
                        "entry":        entry,
                        "final_target": last_target,
                        "exit_price":   current_px2,
                        "exit_reason":  "SLIP_CLOSE",
                        "pnl":          pnl,
                        "pnl_pct":      pnl_pct,
                    })
                    send_telegram(
                        f"📊 슬리피지청산 PnL - {symbol}\n"
                        f"Entry: {entry:.6f}\nExit: {current_px2:.6f}\n"
                        f"PnL: {pnl:+.2f} USDT\n누적: {state.total_pnl_usdt:+.2f} USDT"
                    )
                return

        if state.ENTRY_WATCH_ENABLED:
            watch_triggered = False
            for _ in range(state.ENTRY_WATCH_SEC):
                time.sleep(1.0)
                watch_px = get_rest_last_price(symbol)
                if not watch_px:
                    continue
                watch_slip = (
                    (entry - watch_px) / entry * 100 if initial_side == "buy"
                    else (watch_px - entry) / entry * 100
                )
                if watch_slip > state.ENTRY_WATCH_PCT * 100:
                    send_telegram(
                        f"⚡ 진입 후 {state.ENTRY_WATCH_SEC}초 감시 청산\n{symbol}\n"
                        f"체결가: {entry:.6f}\n현재가: {watch_px:.6f}\n"
                        f"역방향: {watch_slip:.2f}%"
                    )
                    close_side = "sell" if initial_side == "buy" else "buy"
                    success, method = _close_with_fallback(symbol, close_side, sz)
                    if not success:
                        send_telegram(f"🚨 감시청산 실패 → 강제청산\n{symbol}")
                        with state.lock:
                            state.positions.setdefault(symbol, {})
                            state.positions[symbol]["main"] = {
                                "side":           initial_side,
                                "entry":          entry,
                                "sz":             sz,
                                "final_target":   last_target,
                                "oco_id":         None,
                                "signal_id":      signal_id,
                                "zone":           zone,
                                "entry_time":     time.time(),
                                "max_profit_pct": 0.0,
                                "position_high":  entry,
                                "position_low":   entry,
                                "alive":          True,
                                "exited":         False,
                                "entry_bucket":   entry_bucket,
                                "exit_intent":    "WATCH_CLOSE_FAILED",
                            }
                        force_market_close_safe(symbol)
                    else:
                        watch_px2 = get_rest_last_price(symbol) or watch_px
                        meta  = state.instrument_meta.get(symbol, {"ctVal": 1})
                        ctVal = float(meta["ctVal"])
                        pnl   = (
                            (watch_px2 - entry) * sz * ctVal if initial_side == "buy"
                            else (entry - watch_px2) * sz * ctVal
                        )
                        pnl     = round(pnl, 2)
                        pnl_pct = pnl / max(entry * sz * ctVal, 1e-12) * 100
                        state.total_pnl_usdt += pnl
                        from exit import _save_pnl_state
                        _save_pnl_state()
                        _upsert_trade_log(signal_id, {
                            "result":       "HIT",
                            "entry":        entry,
                            "final_target": last_target,
                            "exit_price":   watch_px2,
                            "exit_reason":  "WATCH_CLOSE",
                            "pnl":          pnl,
                            "pnl_pct":      pnl_pct,
                        })
                        send_telegram(
                            f"📊 감시청산 PnL - {symbol}\n"
                            f"Entry: {entry:.6f}\nExit: {watch_px2:.6f}\n"
                            f"PnL: {pnl:+.2f} USDT\n누적: {state.total_pnl_usdt:+.2f} USDT"
                        )
                    watch_triggered = True
                    break
            if watch_triggered:
                return

        with state.lock:
            state.last_entry_time[symbol] = time.time()
            state.positions.setdefault(symbol, {})
            state.positions[symbol]["main"] = {
                "side":           initial_side,
                "entry":          entry,
                "sz":             sz,
                "final_target":   last_target,
                "oco_id":         None,
                "signal_id":      signal_id,
                "zone":           zone,
                "entry_time":     time.time(),
                "max_profit_pct": 0.0,
                "position_high":  entry,
                "position_low":   entry,
                "alive":          True,
                "exited":         False,
                "entry_bucket":   entry_bucket,
                "exit_intent":    None,
            }

        _upsert_trade_log(signal_id, {
            "result":         "HIT",
            "entry":          entry,
            "signal_price":   initial_price,
            "final_target":   last_target,
            "target_hit":     False,
            "mfe_pct":        0.0,
            "mae_pct":        0.0,
            "max_profit_pct": 0.0,
        })

        time.sleep(0.5)
        if sliding_occurred:
            send_telegram(f"🔁 슬라이딩 발생 - {symbol}\n최종 진입가: {entry}")

        close_side = "sell" if initial_side == "buy" else "buy"
        oco_id     = None

        if get_rest_last_price(symbol) is None:
            send_telegram(f"⚠️ {symbol} REST 가격 조회 실패 → OCO 스킵")
            return

        pos_sz = wait_position_visible(symbol, initial_side, timeout=3.0)
        if pos_sz <= 0:
            send_telegram(f"⚠️ 포지션 인식 실패 → OCO 중단\n{symbol}")
            return

        # ✅ [v2 개선 4] ATR 기반 동적 TP/SL 계산
        tp_pct = state.TP_PCT
        sl_pct = state.SL_PCT
        atr_applied = False
        if getattr(state, "ATR_DYNAMIC_ENABLED", False):
            atr_val = calc_atr_pct(symbol, period=state.ATR_PERIOD)
            if atr_val is not None and atr_val > 0:
                atr_ratio = atr_val / 100.0
                tp_pct = max(state.ATR_MIN_TP_PCT, min(state.ATR_MAX_TP_PCT, atr_ratio * state.ATR_TP_MULT))
                sl_pct = max(state.ATR_MIN_SL_PCT, min(state.ATR_MAX_SL_PCT, atr_ratio * state.ATR_SL_MULT))
                atr_applied = True

        with state.lock:
            m = state.positions.get(symbol, {}).get("main")
            if m:
                m["tp_pct"] = tp_pct
                m["sl_pct"] = sl_pct

        for retry in range(3):
            relax = 1 + 0.25 * retry
            if initial_side == "buy":
                tp = round_to_tick(symbol, entry * (1 + tp_pct * relax))
                sl = round_to_tick(symbol, entry * (1 - sl_pct * relax))
            else:
                tp = round_to_tick(symbol, entry * (1 - tp_pct * relax))
                sl = round_to_tick(symbol, entry * (1 + sl_pct * relax))

            oco_id = close_position_oco_checked(symbol, close_side, pos_sz, tp, sl)
            if oco_id:
                atr_msg = f" (ATR동적: TP {tp_pct*100:.2f}% / SL {sl_pct*100:.2f}%)" if atr_applied else ""
                send_telegram(f"🟢 OCO 설정 성공 ({retry+1}/3)\n{symbol}{atr_msg}")
                break

        if not oco_id:
            send_telegram(f"🚨 OCO 3회 실패 → 강제청산\n{symbol}")
            with state.lock:
                m = state.positions.get(symbol, {}).get("main")
                if m:
                    m["exit_intent"] = "OCO_FAILED_RETRY_EXCEEDED"
                    m["sz"]          = sz
            force_market_close_safe(symbol)
            return

        with state.lock:
            state.positions[symbol]["main"]["oco_id"] = oco_id
            state.positions[symbol]["main"]["sz"]     = pos_sz

        send_telegram(f"✅ 진입 완료 - {symbol}\nEntry: {entry}")

    except Exception as e:
        send_telegram(f"❌ 진입 로직 오류: {e}")
    finally:
        with state.lock:
            state.pending_entries.discard((symbol, "main"))
        state.sliding_state.pop(symbol, None)

def try_enter_on_price(symbol, price):
    if symbol in state.SYMBOL_BLACKLIST:
        return

    if not _risk_guard():
        return

    slide = state.sliding_state.get(symbol)
    if slide:
        if slide["side"] == "buy"  and price < slide["best_price"]:
            slide["best_price"] = price
        elif slide["side"] == "sell" and price > slide["best_price"]:
            slide["best_price"] = price
        return

    with state.lock:
        pos = state.positions.get(symbol, {}).get("main")

    if pos and pos.get("alive"):
        entry = pos["entry"]
        side  = pos["side"]
        pnl_pct = (
            (price - entry) / entry * 100 if side == "buy"
            else (entry - price) / entry * 100
        )

        with state.lock:
            if pnl_pct > pos.get("max_profit_pct", 0):
                pos["max_profit_pct"] = pnl_pct
            pos["position_high"] = max(pos.get("position_high", price), price)
            pos["position_low"]  = min(pos.get("position_low",  price), price)

        if not state.trading_enabled:
            return

        elapsed_min = (time.time() - pos["entry_time"]) / 60
        max_pct     = pos.get("max_profit_pct", 0)

        # ✅ [v2 개선 1] 1차 분할 익절(50%) 및 본절 스탑(BEP) 이동 트리거
        tp1_trigger = pos.get("tp_pct", state.PARTIAL_TP_PCT) * 100 if state.ATR_DYNAMIC_ENABLED else state.PARTIAL_TP_PCT * 100
        if state.PARTIAL_TP_ENABLED and not pos.get("partial_tp_done") and pnl_pct >= tp1_trigger:
            with state.lock:
                pos["partial_tp_done"] = True
            Thread(
                target=_execute_partial_tp_and_bep,
                args=(symbol, pos, price),
                daemon=True,
            ).start()

        if state.TRAIL_ENABLED and max_pct >= state.TRAIL_ACTIVATE_PCT:
            if max_pct - pnl_pct >= state.TRAIL_DISTANCE_PCT:
                with state.lock:
                    cur = state.positions.get(symbol, {}).get("main")
                    if cur and cur.get("alive") and cur.get("exit_intent") is None:
                        cur["exit_intent"] = "TRAILING_STOP"
                send_telegram(f"🏹 트레일링 익절 발동 - {symbol}\n최고수익: {max_pct:.2f}% | 청산수익: {pnl_pct:.2f}%")
                force_market_close_safe(symbol)
                return

        if not pos.get("restored") and not pos.get("skip_time_exit"):
            if elapsed_min >= 10 and pos.get("alive"):
                with state.lock:
                    cur = state.positions.get(symbol, {}).get("main")
                    if cur and cur.get("alive") and cur.get("exit_intent") is None:
                        cur["exit_intent"] = "TIME_LIMIT_10M"
                force_market_close_safe(symbol)
                return
            if elapsed_min >= 5 and pos.get("alive") and max_pct < 0.5:
                with state.lock:
                    cur = state.positions.get(symbol, {}).get("main")
                    if cur and cur.get("alive") and cur.get("exit_intent") is None:
                        cur["exit_intent"] = "NO_PROFIT_5M"
                force_market_close_safe(symbol)
                return

    # ✅ [v2 개선 2] BTC 서킷 브레이커 (비트코인 급변동 시 알트코인 신규 진입 차단)
    if state.BTC_CIRCUIT_ENABLED and symbol != state.BTC_SYMBOL:
        if _is_btc_circuit_breaker_active():
            return

    if state.last_failed_order_time.get(symbol):
        failed_time = state.last_failed_order_time[symbol]
        if failed_time > time.time():
            return
        if time.time() - failed_time < cfg.FAILED_ORDER_COOLDOWN:
            return

    if (state.last_entry_time.get(symbol) and
            time.time() - state.last_entry_time[symbol] < cfg.ENTRY_COOLDOWN_SEC):
        return

    open_now = state.bar_state[symbol].get("o")
    if not open_now:
        return

    with state.lock:
        main = state.positions.get(symbol, {}).get("main")
        if main and main.get("alive"):
            return
        if (symbol, "main") in state.pending_entries:
            return

    chg_pct = abs((price - open_now) / open_now) * 100
    s10     = state.bar_state_10m[symbol]
    s1h     = state.bar_state_1h[symbol]
    chg_10m = None
    chg_1h  = None

    if s10.get("o") and s10["o"] > 0:
        chg_10m = (price - s10["o"]) / s10["o"] * 100
    if s1h.get("o") and s1h["o"] > 0:
        chg_1h  = (price - s1h["o"]) / s1h["o"] * 100

    lo5, me5, up5 = get_bollinger(symbol)
    if lo5 is None:
        return

    initial_side = None
    base         = None

    if price <= lo5 and 1.5 <= chg_pct < 5.5:
        initial_side = "buy";  base = lo5
    elif price >= up5 and 1.5 <= chg_pct < 5.5:
        initial_side = "sell"; base = up5

    if initial_side is None:
        return

    rsi_val = get_rsi(symbol, period=state.RSI_PERIOD)
    if state.RSI_FILTER_ENABLED and rsi_val is not None:
        if initial_side == "buy" and rsi_val > state.RSI_LONG_MAX:
            return
        if initial_side == "sell" and rsi_val < state.RSI_SHORT_MIN:
            return

    signal_type = "5M"
    if chg_10m is not None and chg_1h is not None:
        if initial_side == "buy"  and chg_10m <= -1.5 and chg_1h <= -1.5:
            signal_type = "10M+1H"
        elif initial_side == "sell" and chg_10m >= 1.5 and chg_1h >= 1.5:
            signal_type = "10M+1H"

    if state.last_exit_bucket.get(symbol) == state.bar_state[symbol]["bucket"]:
        return

    sl_px = state.last_sl_price.get(symbol)
    if sl_px and abs(price - sl_px) / sl_px < 0.002:
        return

    vol_ratio = get_volume_ratio(symbol)
    if vol_ratio < 1.5:
        return

    strength = estimate_strength(symbol, chg_pct, vol_ratio)
    zone     = classify_zone(strength)
    if zone == "SHALLOW":
        return

    target_price, min_dist, ratio = calc_target_price(
        symbol=symbol, side=initial_side, base=base, price=price,
        vol_ratio=vol_ratio, strength=strength, zone=zone,
        chg_pct=chg_pct, signal_type=signal_type,
    )

    extra_pct       = 0.0
    total_chg       = 0.0
    extra_range_idx = -1

    if (state.EXTRA_DIST_ENABLED and signal_type == "10M+1H"
            and chg_10m is not None and chg_1h is not None):
        total_chg = chg_pct + abs(chg_10m) + abs(chg_1h) * 0.5
        for i, r in enumerate(state.EXTRA_DIST_RANGES):
            if r["enabled"] and r["min"] <= total_chg < r["max"]:
                extra_pct       = state.LEARNED_EXTRA_PCT.get(i, r["pct"])
                extra_range_idx = i
                break
        extra_pct = min(extra_pct, 7.0)
        if extra_pct > 0:
            if initial_side == "buy":
                target_price = round_to_tick(symbol, target_price / (1 + extra_pct / 100))
            else:
                target_price = round_to_tick(symbol, target_price * (1 + extra_pct / 100))

    s            = state.bar_state[symbol]
    entry_bucket = s["bucket"]
    signal_id    = f"{symbol}-{entry_bucket}-{initial_side}"

    adx_val       = get_adx(symbol)
    adx_str       = f"{adx_val:.1f}" if adx_val is not None else "N/A"
    direction_str = "LONG" if initial_side == "buy" else "SHORT"

    if adx_val is not None and adx_val > state.ADX_THRESHOLD:
        current_bucket = state.bar_state[symbol]["bucket"]
        if state.adx_blocked.get(symbol) != current_bucket:
            state.adx_blocked[symbol] = current_bucket
            send_telegram(
                f"⛔ [{state.APP_ID} | {direction_str}]\n"
                f"{symbol}\n타겟가: {target_price:.6f}\n"
                f"ADX: {adx_str} > {state.ADX_THRESHOLD}\n"
            )
        if state.adx_filter_enabled:
            return

    status_msg = "실매매 가동" if state.trading_enabled else "알람 전용(OFF)"
    now        = time.time()
    last_alarm = state.last_alarm_time.get(symbol, 0)
    if not state.trading_enabled and now - last_alarm < state.ALARM_COOLDOWN_SEC:
        return
    state.last_alarm_time[symbol] = now

    rsi_str = f"{rsi_val:.1f}" if rsi_val is not None else "N/A"

    send_telegram(
        f"🌪 [{state.APP_ID} | {status_msg} | {direction_str}] {symbol}\n"
        f"현재가: {price:.6f}\n"
        f"Base: {base:.6f} ({min_dist:.2f}% 이탈)\n"
        f"타겟가: {target_price:.6f}\n"
        f"Strength: {strength:.2f} ({zone})\n"
        f"ADX: {adx_str} | RSI: {rsi_str}\n"
        f"변화율(5M): {chg_pct:.2f}% | Vol: {vol_ratio:.2f}x"
        + (f"\n변화율(10M): {abs(chg_10m):.2f}%"
           f" | 변화율(1H): {abs(chg_1h):.2f}%"
           f" | total_chg: {total_chg:.2f}%"
           f" | extra: {extra_pct:.1f}%"
           if signal_type == "10M+1H" else "")
    )

    if not state.trading_enabled:
        return

    state.sliding_state[symbol] = {
        "signal_id":    signal_id,
        "side":         initial_side,
        "base":         base,
        "zone":         zone,
        "strength":     strength,
        "start_ts":     time.time(),
        "entry_bucket": entry_bucket,
        "best_price":   price,
    }

    size_mult = cfg.ZONE_SIZE_MULT.get(zone, 1.0)
    main_usdt = state.usdt_amount * size_mult

    _upsert_trade_log(signal_id, {
        "symbol":              symbol,
        "side":                initial_side,
        "signal_price":        price,
        "signal_target":       target_price,
        "zone":                zone,
        "signal_type":         signal_type,
        "strength":            strength,
        "adx":                 adx_val,
        "rsi":                 rsi_val,
        "adx_filter_on":       state.adx_filter_enabled,
        "vol_filter_on":       state.vol_exhausted_filter_enabled,
        "rsi_filter_on":       state.RSI_FILTER_ENABLED,
        "initial_target":      target_price,
        "final_target":        target_price,
        "sliding":             False,
        "min_dist":            min_dist,
        "extra_pct":           extra_pct,
        "extra_range_idx":     extra_range_idx,
        "total_chg":           total_chg,
        "candle_open":         s.get("o"),
        "candle_high":         s.get("h"),
        "candle_low":          s.get("l"),
        "target_hit":          False,
        "max_favorable_price": None,
        "max_adverse_price":   None,
        "mfe_pct":             None,
        "mae_pct":             None,
        "max_profit_pct":      None,
        "result":              "PENDING",
        "entry":               None,
        "exit_price":          None,
        "exit_reason":         None,
        "pnl":                 None,
        "pnl_pct":             None,
        "timestamp":           time.time(),
    })

    with state.lock:
        state.pending_entries.add((symbol, "main"))
        state.last_entry_time[symbol] = time.time()

    Thread(
        target=_track_signal_timeline,
        args=(symbol, signal_id, initial_side, price),
        kwargs={"duration": 4.0, "interval": 0.2},
        daemon=True,
    ).start()

    Thread(
        target=_delayed_confirm_and_enter,
        args=(
            signal_id, symbol, price, initial_side,
            base, entry_bucket, strength, zone,
            target_price, main_usdt,
            signal_type, extra_pct, total_chg, extra_range_idx,
        ),
        daemon=True,
    ).start()