import os
import hmac
import hashlib
import base64
import json
import math
import re
import time
import requests
from datetime import datetime

import state
from telegram import send_telegram

API_KEY    = os.getenv("OKX_API_KEY", "")
SECRET_KEY = os.getenv("OKX_SECRET_KEY", "")
PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")
BASE_URL   = "https://www.okx.com"

def okx_request(method, path, body="", retries=2):
    ts   = datetime.utcnow().isoformat("T", "milliseconds") + "Z"
    msg  = f"{ts}{method}{path}{body}"
    sign = (
        base64.b64encode(
            hmac.new(SECRET_KEY.encode(), msg.encode(), hashlib.sha256).digest()
        ).decode()
        if SECRET_KEY else ""
    )
    headers = {
        "OK-ACCESS-KEY":        API_KEY or "",
        "OK-ACCESS-SIGN":       sign,
        "OK-ACCESS-TIMESTAMP":  ts,
        "OK-ACCESS-PASSPHRASE": PASSPHRASE or "",
        "Content-Type":         "application/json",
    }
    last_err = None
    for attempt in range(retries + 1):
        try:
            if method == "GET":
                r = requests.get(BASE_URL + path, headers=headers, timeout=10)
            else:
                r = requests.post(BASE_URL + path, headers=headers, data=body, timeout=10)
            try:
                return r.json()
            except Exception:
                return json.loads(r.text) if r.text.startswith("{") else {"msg": r.text}
        except Exception as e:
            last_err = e
            if attempt < retries:
                print(f"[okx_request] 재시도 {attempt+1}/{retries} ({path}): {e}")
                time.sleep(0.5)
                continue
            print("okx_request 예외:", e)
    return {"error": str(last_err)}

def round_to_tick(symbol, price):
    try:
        tick = state.instrument_meta.get(symbol, {}).get("tickSz", 0.001)
        return math.floor(price / tick) * tick
    except Exception:
        return price

def adjust_size(size, lot_size, min_size):
    if lot_size <= 0:
        return max(size, min_size)
    steps = math.floor(size / lot_size) * lot_size
    if steps < min_size:
        steps = math.ceil(min_size / lot_size) * lot_size
    return round(steps, 8)

def _save_blacklist():
    try:
        with open("blacklist.json", "w") as f:
            json.dump(list(state.SYMBOL_BLACKLIST), f)
    except Exception as e:
        print(f"블랙리스트 저장 실패: {e}")

def get_rest_last_price(symbol):
    try:
        res = okx_request("GET", f"/api/v5/market/ticker?instId={symbol}")
        if res and "data" in res and res["data"]:
            return float(res["data"][0]["last"])
    except Exception as e:
        print(f"[get_rest_last_price] error: {e}")
    return None

def get_positions():
    res = okx_request("GET", "/api/v5/account/positions?instType=SWAP")
    if res.get("code") == "0" and res.get("data"):
        return [p for p in res["data"] if abs(float(p.get("pos", 0))) > 0]
    return []

def get_open_algo_orders():
    res = okx_request("GET", "/api/v5/trade/orders-algo-pending?instType=SWAP")
    if res.get("code") == "0" and res.get("data"):
        return [d for d in res["data"] if d.get("ordType") == "oco"]
    return []

def get_algo_order(symbol, algo_id):
    try:
        path = f"/api/v5/trade/order-algo?instType=SWAP&algoId={algo_id}"
        res  = okx_request("GET", path)
        if res.get("code") == "0" and res.get("data"):
            return res["data"][0]
        path2 = f"/api/v5/trade/orders-algo-history?instType=SWAP&algoId={algo_id}&instId={symbol}"
        res2  = okx_request("GET", path2)
        if res2.get("code") == "0" and res2.get("data"):
            return res2["data"][0]
        return None
    except Exception as e:
        print(f"get_algo_order 예외: {e}")
        return None

def get_real_position_size(symbol):
    res = okx_request("GET", f"/api/v5/account/positions?instId={symbol}")
    if not res or res.get("code") != "0":
        return 0.0
    for p in res.get("data", []):
        pos = float(p.get("pos", 0))
        if abs(pos) > 0:
            return abs(pos)
    return 0.0

def get_real_open_orders(symbol):
    try:
        res = okx_request("GET", f"/api/v5/trade/orders-pending?instType=SWAP&instId={symbol}")
        if res.get("code") == "0" and res.get("data"):
            return len(res["data"]) > 0
    except Exception as e:
        print(f"[get_real_open_orders] error: {e}")
    return False

def wait_position_visible(symbol, side, timeout=4.0, poll=0.3):
    start = time.time()
    while time.time() - start < timeout:
        try:
            for p in get_positions():
                if p.get("instId") != symbol:
                    continue
                pos = float(p.get("pos", 0))
                if side == "buy"  and pos > 0: return abs(pos)
                if side == "sell" and pos < 0: return abs(pos)
        except Exception as e:
            print(f"[wait_position_visible] {symbol} error:", e)
        time.sleep(poll)
    return 0.0

def check_order_status(symbol, order_id):
    res = okx_request("GET", f"/api/v5/trade/order?instId={symbol}&ordId={order_id}")
    if res.get("code") == "0" and res.get("data"):
        return res["data"][0].get("state")
    return None

def get_filled_info(symbol, order_id, retries=10, delay=0.25):
    for _ in range(retries):
        res = okx_request("GET", f"/api/v5/trade/order?instId={symbol}&ordId={order_id}")
        if res.get("code") == "0" and res.get("data"):
            d         = res["data"][0]
            state_str = (d.get("state") or "").lower()
            acc       = float(d.get("accFillSz") or 0)
            px        = float(d.get("avgPx") or d.get("fillPx") or 0)
            if state_str in ("filled", "partially_filled") and acc > 0 and px > 0:
                return acc, px
            if acc > 0 and px > 0:
                return acc, px
        time.sleep(delay)
    return 0, 0

def _get_max_leverage_from_info(symbol):
    try:
        res = okx_request("GET", f"/api/v5/account/max-leverage?instId={symbol}&mgnMode=isolated")
        if res.get("code") == "0" and res.get("data"):
            return float(res["data"][0].get("maxLever", 1))
    except Exception as e:
        print(f"_get_max_leverage_from_info 예외: {e}")
    return None

def _post_set_leverage(symbol, lever):
    body = json.dumps({"instId": symbol, "lever": str(int(lever)), "mgnMode": "isolated"})
    return okx_request("POST", "/api/v5/account/set-leverage", body)

def set_leverage(symbol, target_lever=10):
    try:
        target_lever = int(target_lever)
    except Exception:
        target_lever = 10
    res = _post_set_leverage(symbol, target_lever)
    if res.get("code") == "0":
        return True
    send_telegram(f"⚠️ 레버리지 설정 실패 ({target_lever}x) - {symbol}\n{res}")
    maxlev = _get_max_leverage_from_info(symbol)
    if not maxlev:
        return False
    for lv in range(min(int(maxlev), target_lever), 0, -1):
        if _post_set_leverage(symbol, lv).get("code") == "0":
            return True
        time.sleep(0.2)
    send_telegram(f"❌ 레버리지 자동조정 실패 - {symbol}")
    return False

def cancel_order(symbol, order_id):
    try:
        okx_request(
            "POST", "/api/v5/trade/cancel-order",
            json.dumps({"instId": symbol, "ordId": order_id})
        )
    except Exception:
        pass

def cancel_algo_order(symbol, algo_id):
    try:
        body = json.dumps([{"instId": symbol, "algoId": algo_id}])
        res  = okx_request("POST", "/api/v5/trade/cancel-algos", body)
        if not res or res.get("code") != "0":
            return False
        data = res.get("data", [])
        return bool(data and data[0].get("sCode") == "0")
    except Exception as e:
        print("cancel_algo_order 예외:", e)
        return False

def place_limit_order(symbol, side, target_price, order_usdt):
    if not state.trading_enabled:
        return None

    meta  = state.instrument_meta.get(symbol, {"ctVal": 1, "lotSz": 1, "tickSz": 1, "minSz": 1})
    ctVal = float(meta["ctVal"])
    lotSz = float(meta["lotSz"])
    minSz = float(meta["minSz"])

    px = round_to_tick(symbol, target_price)
    if px <= 0:
        return None

    contracts = order_usdt / (px * ctVal)
    sz        = adjust_size(contracts, lotSz, minSz)
    if sz <= 0:
        return None

    actual_usdt = sz * px * ctVal
    ratio       = actual_usdt / order_usdt
    if ratio > 1.02:
        sz = adjust_size(sz - lotSz, lotSz, minSz)
    elif ratio < 0.98:
        sz = adjust_size(sz + lotSz, lotSz, minSz)
    if sz <= 0:
        return None

    def _do_order(order_px, order_sz):
        return okx_request(
            "POST", "/api/v5/trade/order",
            json.dumps({
                "instId":  symbol,
                "tdMode":  "isolated",
                "side":    side,
                "ordType": "limit",
                "px":      f"{order_px:.12f}",
                "sz":      str(order_sz),
            })
        )

    res    = _do_order(px, sz)
    s_code = ""
    s_msg  = ""
    try:
        s_code = res.get("data", [{}])[0].get("sCode", "")
        s_msg  = res.get("data", [{}])[0].get("sMsg", "")
    except Exception:
        pass

    if res.get("code") == "0":
        return res["data"][0]["ordId"]

    # ✅ 51155: 지역 제한 → 블랙리스트 저장 후 포기
    if s_code == "51155":
        state.SYMBOL_BLACKLIST.add(symbol)
        _save_blacklist()
        print(f"[place_limit_order] {symbol} 지역 제한 → 블랙리스트 추가")
        send_telegram(f"⛔ {symbol} 지역 제한 → 블랙리스트 추가\n이후 신호 무시됩니다")
        return None

    # ✅ 51004: 최대 수량 초과 → 에러 메시지에서 최대값 파싱 후 재시도
    if s_code == "51004":
        match = re.search(r"more than ([\d,]+)\(contracts\)", s_msg)
        if match:
            max_contracts = int(match.group(1).replace(",", ""))
            sz_capped     = adjust_size(max_contracts, lotSz, minSz)
            if sz_capped > 0:
                res2 = _do_order(px, sz_capped)
                if res2.get("code") == "0":
                    print(f"[place_limit_order] {symbol} 51004 재시도 성공: {sz_capped}계약")
                    return res2["data"][0]["ordId"]
                s_code = res2.get("data", [{}])[0].get("sCode", "")
                s_msg  = res2.get("data", [{}])[0].get("sMsg", "")
                res    = res2

    # ✅ 51006: 가격 범위 초과 → 에러 메시지에서 허용 가격 파싱 후 재시도
    if s_code == "51006":
        if side == "buy":
            match = re.search(r"max buy price:\s*([\d.]+)", s_msg)
        else:
            match = re.search(r"min sell price:\s*([\d.]+)", s_msg)
        if match:
            adjusted_px = round_to_tick(symbol, float(match.group(1)))
            if adjusted_px > 0:
                res2 = _do_order(adjusted_px, sz)
                if res2.get("code") == "0":
                    print(f"[place_limit_order] {symbol} 51006 재시도 성공: px={adjusted_px}")
                    return res2["data"][0]["ordId"]
                res = res2

    # 최종 실패 처리
    state.last_failed_order_time[symbol] = time.time()
    if s_code == "51008":
        state.last_failed_order_time[symbol] = time.time() + 300
    send_telegram(f"❌ ENTRY 주문 실패 - {symbol}\n{res}")
    return None

def _place_oco_order(symbol, side, sz, tp_price, sl_price):
    if not state.trading_enabled:
        return None
    body = {
        "instId":      symbol,
        "tdMode":      "isolated",
        "side":        side,
        "ordType":     "oco",
        "sz":          str(sz),
        "tpTriggerPx": f"{tp_price:.12f}",
        "tpOrdPx":     "-1",
        "slTriggerPx": f"{sl_price:.12f}",
        "slOrdPx":     "-1",
        "reduceOnly":  True,
    }
    res = okx_request("POST", "/api/v5/trade/order-algo", json.dumps(body, separators=(",", ":")))
    return res if (res and res.get("code") == "0") else None

def close_position_oco_checked(symbol, side, pos_sz, tp_price, sl_price):
    if not state.trading_enabled:
        return None
    try:
        res = _place_oco_order(symbol, side, pos_sz, tp_price, sl_price)
        if not res or res.get("code") != "0":
            return None
        algo_id = res["data"][0].get("algoId")
        if not algo_id:
            return None
        with state.lock:
            state.positions.setdefault(symbol, {}).setdefault("main", {})["oco_id"] = algo_id
        return algo_id
    except Exception as e:
        print(f"[close_position_oco_checked] ERROR: {e}")
        return None