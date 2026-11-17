from flask import Flask, request, jsonify
import requests
import time
import threading
import json
import os
import logging
import psycopg2
from psycopg2.extras import Json
from collections import defaultdict

logging.getLogger('werkzeug').setLevel(logging.ERROR)

app = Flask(__name__)

TOKEN   = os.getenv("TG_TOKEN", "")
CHAT_ID = os.getenv("TG_CHAT_ID", "")
DB_URL  = os.getenv("DATABASE_URL", "")

commands       = []
last_update_id = 0
cmd_counter    = 0
symbol_list    = []
trade_log      = []
trade_log_lock = threading.Lock()

EXTRA_DIST_RANGES = [
    {"min": 10.0, "max": 12.0,  "pct": 2.0},
    {"min": 12.0, "max": 18.0,  "pct": 5.0},
    {"min": 18.0, "max": 999.0, "pct": 10.0},
]

VALID_CMDS = {
    "켜", "꺼", "청산", "상태", "초기화",
    "adx켜", "adx꺼", "사이즈", "tp", "sl",
    "vol켜", "vol꺼",
    "rsi켜", "rsi꺼", "rsi롱", "rsi숏",
    "추가이격켜", "추가이격꺼", "변화율합", "이격비율",
    "청산켜", "청산꺼", "청산비율",
    "슬라이딩켜", "슬라이딩꺼", "adx",
    "이격1", "이격2", "이격3",
    "구간0", "구간1", "구간2",
    "구간1켜", "구간1꺼", "구간2켜", "구간2꺼", "구간3켜", "구간3꺼",
    "대기시간", "명령어",
    "분석", "이격적용", "이격초기화", "추가이격초기화",
    "slip켜", "slip꺼", "slip비율",
    "손실한도",
    "이격mid5m", "이격mid10m", "이격deep5m", "이격deep10m",
    "btc서킷켜", "btc서킷꺼", "atr켜", "atr꺼",
    "분할익절켜", "분할익절꺼", "트레일켜", "트레일꺼",
}

VALUE_CMDS = {
    "사이즈", "tp", "sl", "변화율합", "이격비율",
    "이격1", "이격2", "이격3",
    "rsi롱", "rsi숏", "adx",
    "slip비율",
    "손실한도",
    "구간0", "구간1", "구간2", "대기시간",
    "이격mid5m", "이격mid10m", "이격deep5m", "이격deep10m",
    "청산비율",
}

MISS_PCT_MAX          = 1.5
WATCH_RISK_THRESHOLD  = 0.6

# ==========================================
# Postgres
# ==========================================
def _get_conn():
    return psycopg2.connect(DB_URL)

def _init_db():
    try:
        conn = _get_conn()
        cur  = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS trade_log (
                record_id TEXT PRIMARY KEY,
                data JSONB NOT NULL,
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)
        conn.commit()
        cur.close()
        conn.close()
        print("✅ Postgres 테이블 준비 완료")
    except Exception as e:
        print(f"❌ DB 초기화 실패: {e}")

def _load_trade_log():
    global trade_log
    try:
        conn = _get_conn()
        cur  = conn.cursor()
        cur.execute("SELECT data FROM trade_log ORDER BY updated_at ASC")
        rows = cur.fetchall()
        trade_log = [r[0] for r in rows]
        cur.close()
        conn.close()
        print(f"✅ trade_log 로드(Postgres): {len(trade_log)}건")
    except Exception as e:
        print(f"❌ trade_log 로드 실패: {e}")
        trade_log = []

def _save_one_trade_log(record_id, data):
    try:
        conn = _get_conn()
        cur  = conn.cursor()
        cur.execute("""
            INSERT INTO trade_log (record_id, data, updated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (record_id)
            DO UPDATE SET data = %s, updated_at = NOW()
        """, (record_id, Json(data), Json(data)))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"❌ trade_log 단건 저장 실패: {e}")

# ==========================================
# 심볼 조회
# ==========================================
def fetch_symbols():
    global symbol_list
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
            vol  = float(ticker.get("volCcy24h", 0) or 0)
            last = float(ticker.get("last", 0) or 0)
            metas.append({"instId": inst_id, "turnover": vol * last})

        symbol_list = [
            m["instId"]
            for m in sorted(metas, key=lambda x: x["turnover"], reverse=True)[:200]
        ]
        print(f"✅ 심볼 로드 완료: {len(symbol_list)}개")
    except Exception as e:
        print("심볼 조회 실패:", e)

def send_telegram(msg):
    try:
        res = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": msg},
            timeout=5
        )
        if res.status_code != 200:
            print(f"텔레그램 발송 실패: {res.status_code} {res.text}")
    except Exception as e:
        print(f"텔레그램 전송 예외: {e}")

# ==========================================
# 심볼 자동 갱신
# ==========================================
def symbol_refresh_loop():
    REFRESH_INTERVAL = 6 * 3600  # 6시간

    while True:
        time.sleep(REFRESH_INTERVAL)

        try:
            print("🔄 심볼 자동 갱신 시작...")
            fetch_symbols()
            print(f"✅ 심볼 자동 갱신 완료: {len(symbol_list)}개")
        except Exception as e:
            print(f"❌ 심볼 자동 갱신 실패: {e}")

# ==========================================
# 분석 함수
# ==========================================
def run_analysis():
    try:
        with trade_log_lock:
            all_logs = list(trade_log)

        total_all = len(all_logs)
        print(f"[분석] 전체: {total_all}")

        if total_all == 0:
            return (
                "📭 데이터 없음\n"
                "봇에서 신호가 아직 감지되지 않았거나\n"
                "trade_log 전송이 실패하고 있습니다"
            )

        miss_logs  = []
        loss_logs  = []
        watch_logs = []

        for l in all_logs:
            result      = l.get("result")
            exit_reason = l.get("exit_reason", "")
            pnl         = l.get("pnl")

            if result == "MISS":
                miss_logs.append(l)
            elif result == "HIT" and exit_reason in ("WATCH_CLOSE", "SLIP_CLOSE"):
                watch_logs.append(l)
            elif (result == "HIT" and pnl is not None
                    and pnl < 0
                    and exit_reason == "OCO_FILLED"):
                loss_logs.append(l)

        pending_count = sum(1 for l in all_logs if l.get("result") == "PENDING")
        cancel_count  = sum(1 for l in all_logs if l.get("result") == "CANCEL_CANDLE")
        hit_count     = sum(1 for l in all_logs if l.get("result") == "HIT")
        miss_count    = len(miss_logs)
        watch_count   = len(watch_logs)
        loss_count    = len(loss_logs)

        if not miss_logs and not loss_logs and not watch_logs:
            return (
                f"📭 분석 대상 없음\n\n"
                f"전체: {total_all}건\n"
                f"체결: {hit_count} | 미체결: {miss_count} | "
                f"캔들취소: {cancel_count} | 미확정: {pending_count}"
            )

        stats      = defaultdict(lambda: defaultdict(lambda: {
            "miss": [], "miss_no_fill": [], "loss": [], "hit_dist": []
        }))
        app_counts = defaultdict(lambda: {"miss": 0, "loss": 0, "watch": 0})

        miss_a_count = 0
        miss_b_count = 0
        miss_c_count = 0

        for l in miss_logs:
            zone        = l.get("zone", "UNKNOWN")
            stype       = l.get("signal_type", "5M")
            side        = l.get("side")
            candle_open = l.get("candle_open") or 0
            candle_low  = l.get("candle_low")  or 0
            candle_high = l.get("candle_high") or 0
            final_tgt   = l.get("final_target") or 0
            min_dist    = l.get("min_dist", 0)
            extra_pct   = l.get("extra_pct", 0)
            total_chg   = l.get("total_chg", 0)
            app_id      = l.get("app_id", "?")

            if candle_open > 0 and final_tgt > 0:
                if side == "buy":
                    miss_pct = (final_tgt - candle_low) / candle_open * 100
                else:
                    miss_pct = (candle_high - final_tgt) / candle_open * 100

                if miss_pct < -2.0:
                    miss_b_count += 1
                    continue

                if miss_pct < 0:
                    miss_c_count += 1
                    stats[zone][stype]["miss_no_fill"].append({
                        "miss_pct":        miss_pct,
                        "min_dist":        min_dist,
                        "extra_pct":       extra_pct,
                        "total_chg":       total_chg,
                        "extra_range_idx": l.get("extra_range_idx", -1),
                    })
                    continue

                if miss_pct > MISS_PCT_MAX:
                    miss_b_count += 1
                    continue

                miss_a_count += 1
                stats[zone][stype]["miss"].append({
                    "miss_pct":        miss_pct,
                    "min_dist":        min_dist,
                    "extra_pct":       extra_pct,
                    "total_chg":       total_chg,
                    "extra_range_idx": l.get("extra_range_idx", -1),
                })
                app_counts[app_id]["miss"] += 1

        for l in loss_logs:
            zone      = l.get("zone", "UNKNOWN")
            stype     = l.get("signal_type", "5M")
            min_dist  = l.get("min_dist", 0)
            extra_pct = l.get("extra_pct", 0)
            total_chg = l.get("total_chg", 0)
            pnl       = l.get("pnl", 0)
            app_id    = l.get("app_id", "?")

            stats[zone][stype]["loss"].append({
                "min_dist":        min_dist,
                "extra_pct":       extra_pct,
                "total_chg":       total_chg,
                "pnl":             pnl,
                "extra_range_idx": l.get("extra_range_idx", -1),
                "adx":             l.get("adx"),
            })
            app_counts[app_id]["loss"] += 1

        for l in watch_logs:
            app_id = l.get("app_id", "?")
            app_counts[app_id]["watch"] += 1

        for l in all_logs:
            if l.get("result") == "HIT" and (l.get("pnl") or 0) >= 0:
                zone  = l.get("zone", "UNKNOWN")
                stype = l.get("signal_type", "5M")
                stats[zone][stype]["hit_dist"].append(l.get("min_dist", 0))

        lines = ["📊 FastBand 이격거리 분석\n"]

        for aid, cnt in sorted(app_counts.items()):
            lines.append(
                f"App{aid}: 미체결(이격) {cnt['miss']} | "
                f"OCO손실 {cnt['loss']} | "
                f"3초청산 {cnt['watch']}"
            )

        lines.append(
            f"\n[미체결 분류]\n"
            f"  이격 문제(A): {miss_a_count}건 → 이격 조정 대상\n"
            f"  스파이크 약함(B): {miss_b_count}건 → 제외\n"
            f"  체결력 문제(C): {miss_c_count}건 → 타겟가는 닿았으나 미체결, 이격과 무관"
        )

        total_miss_adj = 0
        total_loss_adj = 0

        for zone in ["MID", "DEEP"]:
            for stype in ["5M", "10M+1H"]:
                d           = stats[zone][stype]
                misses      = d["miss"]
                misses_nofl = d["miss_no_fill"]
                losses      = d["loss"]
                hit_dist    = d["hit_dist"]

                if not misses and not losses and not misses_nofl:
                    continue

                total_miss_adj += len(misses)
                total_loss_adj += len(losses)

                lines.append(f"\n▶ {zone}/{stype}")
                lines.append(
                    f"  미체결(이격A): {len(misses)} | "
                    f"체결력문제(C): {len(misses_nofl)} | "
                    f"OCO손실: {len(losses)} | 수익: {len(hit_dist)}"
                )

                rec      = None
                rec_loss = None

                if misses:
                    avg_miss = sum(m["miss_pct"] for m in misses) / len(misses)
                    avg_dist = sum(m["min_dist"] for m in misses) / len(misses)
                    rec      = max(0.1, avg_dist - avg_miss * 0.7)
                    lines.append(f"  [미체결A] 평균미달: {avg_miss:.3f}%")
                    lines.append(f"  현재이격: {avg_dist:.3f}% → 추천: {rec:.3f}%")

                if misses_nofl:
                    avg_over = sum(abs(m["miss_pct"]) for m in misses_nofl) / len(misses_nofl)
                    lines.append(
                        f"  [체결력문제C] 평균 초과도달: {avg_over:.3f}% "
                        f"→ 슬라이딩/ENTRY_WAIT 점검 필요 (이격 조정 대상 아님)"
                    )

                if losses:
                    avg_loss   = sum(l["pnl"] for l in losses) / len(losses)
                    avg_dist_l = sum(l["min_dist"] for l in losses) / len(losses)
                    rec_loss   = avg_dist_l * 1.3
                    lines.append(f"  [OCO손실] 평균PnL: {avg_loss:+.2f} USDT")
                    lines.append(f"  현재이격: {avg_dist_l:.3f}% → 추천: {rec_loss:.3f}%")

                    adx_vals = [l["adx"] for l in losses if l.get("adx") is not None]
                    if adx_vals:
                        avg_adx = sum(adx_vals) / len(adx_vals)
                        lines.append(f"  [손실패턴] 평균ADX: {avg_adx:.1f}")

                if rec is not None and rec_loss is not None:
                    rec_combined = (
                        rec * len(misses) + rec_loss * len(losses)
                    ) / (len(misses) + len(losses))
                    lines.append(f"  ✅ 종합 추천 min_dist: {rec_combined:.3f}%")
                elif rec is not None:
                    lines.append(f"  ✅ 추천 min_dist: {rec:.3f}%")
                elif rec_loss is not None:
                    lines.append(f"  ✅ 추천 min_dist: {rec_loss:.3f}%")

                if stype == "10M+1H":
                    all_problems = misses + [
                        {**l, "miss_pct": None} for l in losses
                    ]
                    range_stats = defaultdict(list)
                    for m in all_problems:
                        idx = m.get("extra_range_idx")
                        if idx is not None and idx >= 0:
                            range_stats[idx].append(m)

                    for i, rm in range_stats.items():
                        if not rm:
                            continue
                        r         = EXTRA_DIST_RANGES[i]
                        avg_extra = sum(x["extra_pct"] for x in rm) / len(rm)

                        miss_in = [x for x in rm if x.get("miss_pct") is not None]
                        loss_in = [x for x in rm if x.get("pnl") is not None]

                        if miss_in and loss_in:
                            avg_m     = sum(x["miss_pct"] for x in miss_in) / len(miss_in)
                            rec_m     = max(0, avg_extra - avg_m * 0.7)
                            rec_l     = avg_extra * 1.3
                            rec_extra = (
                                rec_m * len(miss_in) + rec_l * len(loss_in)
                            ) / len(rm)
                        elif miss_in:
                            avg_m     = sum(x["miss_pct"] for x in miss_in) / len(miss_in)
                            rec_extra = max(0, avg_extra - avg_m * 0.7)
                        else:
                            rec_extra = avg_extra * 1.3

                        lines.append(
                            f"  구간{i+1}({r['min']}~{r['max']}%): "
                            f"현재 {avg_extra:.1f}% → 추천 {rec_extra:.2f}%"
                        )

                        chg_vals = [x["total_chg"] for x in rm if x.get("total_chg")]
                        if chg_vals:
                            band = (r["max"] - r["min"]) * 0.2 if r["max"] < 900 else 3.0
                            near_upper = sum(1 for c in chg_vals if c > r["max"] - band)
                            near_lower = sum(1 for c in chg_vals if c < r["min"] + band)

                            boundary_note = []
                            if miss_in and near_upper / len(rm) > 0.4:
                                new_max = round(r["max"] - band * 0.7, 1)
                                boundary_note.append(f"상한 {r['max']}→{new_max}%")
                            if loss_in and near_lower / len(rm) > 0.4:
                                new_min = round(r["min"] + band * 0.7, 1)
                                boundary_note.append(f"하한 {r['min']}→{new_min}%")

                            if boundary_note:
                                lines.append(
                                    f"    📏 경계 조정 권장: " + " / ".join(boundary_note)
                                )

        # ✅ 캔들변경 취소 판단 분석
        cancel_logs = [l for l in all_logs if l.get("result") == "CANCEL_CANDLE"]
        cancel_with_outcome = [l for l in cancel_logs if l.get("cancel_would_have_filled") is not None]

        if cancel_with_outcome:
            would_fill_count = sum(1 for l in cancel_with_outcome if l.get("cancel_would_have_filled"))
            no_fill_count    = len(cancel_with_outcome) - would_fill_count
            fill_rate        = would_fill_count / len(cancel_with_outcome) * 100

            avg_closest = sum(
                abs(l.get("cancel_closest_dist_pct") or 0) for l in cancel_with_outcome
            ) / len(cancel_with_outcome)

            lines.append(f"\n🕯️ 캔들변경 취소 판단 분석 ({len(cancel_with_outcome)}건 추적완료)")
            lines.append(
                f"  취소 후 60초 내 타겟가 도달: {would_fill_count}건 ({fill_rate:.1f}%)\n"
                f"  끝까지 미도달: {no_fill_count}건\n"
                f"  평균 최근접 거리: {avg_closest:.3f}%"
            )

            if fill_rate > 40:
                lines.append(
                    "  ⚠️ 취소 후에도 타겟가에 자주 도달함 → "
                    "캔들변경 즉시취소 기준이 너무 엄격할 가능성, 유예 로직 검토 권장"
                )
            else:
                lines.append(
                    "  ✅ 취소 후 대부분 타겟가 미도달 → 현재 취소 기준이 합리적"
                )

            pending_cancel = len(cancel_logs) - len(cancel_with_outcome)
            if pending_cancel > 0:
                lines.append(f"  (추적 진행중 또는 데이터 누락: {pending_cancel}건)")

        lines.append(f"\n⏱ 대기시간(ENTRY_DELAY_SEC) 정밀 분석")

        timeline_logs = [l for l in all_logs if l.get("signal_timeline")]

        if not timeline_logs:
            lines.append("  데이터 없음 (signal_timeline 미수집, 누적 대기 필요)")
        else:
            bucket_breach = defaultdict(int)
            bucket_total  = defaultdict(int)

            for l in timeline_logs:
                tl = l.get("signal_timeline", [])
                for point in tl:
                    bucket = round(point["t"], 1)
                    bucket_total[bucket] += 1
                    if point["slip_pct"] > WATCH_RISK_THRESHOLD:
                        bucket_breach[bucket] += 1

            lines.append(f"  샘플: {len(timeline_logs)}건 (MISS+HIT 전체)")

            grouped = defaultdict(lambda: {"breach": 0, "total": 0})
            for bucket, total in bucket_total.items():
                group = round(bucket * 2) / 2
                grouped[group]["total"]  += total
                grouped[group]["breach"] += bucket_breach.get(bucket, 0)

            best_delay = None

            for t in sorted(grouped.keys()):
                g    = grouped[t]
                rate = g["breach"] / g["total"] * 100 if g["total"] else 0
                lines.append(f"  {t:.1f}초 시점: 위험신호 {g['breach']}/{g['total']}건 ({rate:.1f}%)")

                if rate < 15 and best_delay is None:
                    best_delay = t

            if best_delay is not None:
                lines.append(
                    f"\n  ✅ 추천 ENTRY_DELAY_SEC: {best_delay:.1f}초\n"
                    f"     (이 시점부터 위험신호 15% 미만으로 안정화)"
                )
            else:
                lines.append(
                    f"\n  ⚠️ 4초 동안에도 위험신호가 충분히 낮아지지 않음\n"
                    f"     스파이크 자체가 강해서 대기시간 조정만으론 부족할 수 있음"
                )

        if watch_logs:
            avg_watch_pnl = sum(l.get("pnl", 0) for l in watch_logs) / len(watch_logs)
            lines.append(
                f"\n  참고: 3초감시청산 {watch_count}건 | "
                f"평균손실 {avg_watch_pnl:+.2f} USDT"
            )

        lines.append(f"\n총 이격조정 대상: 미체결 {total_miss_adj} | OCO손실 {total_loss_adj}")
        lines.append("※ '이격적용' 명령으로 추천값을 봇에 반영하세요")
        lines.append("※ 구간경계/대기시간은 수동으로 적용해주세요")
        return "\n".join(lines)

    except Exception as e:
        print(f"[run_analysis] 예외: {e}")
        return f"❌ 분석 오류: {e}"

# ==========================================
# 텔레그램 수신
# ==========================================
def listen_telegram():
    global last_update_id, cmd_counter
    session = requests.Session()

    while True:
        try:
            url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
            params = {"offset": last_update_id + 1, "timeout": 5}
            res = session.get(url, params=params, timeout=15).json()

            for u in res.get("result", []):
                uid = u["update_id"]
                if uid <= last_update_id:
                    continue
                last_update_id = uid

                msg_obj = u.get("message", {})
                chat_id = str(msg_obj.get("chat", {}).get("id"))
                if chat_id != CHAT_ID:
                    continue

                msg = msg_obj.get("text", "")
                if not msg:
                    continue

                parts = msg.split()
                cmd   = parts[0]

                if cmd not in VALID_CMDS:
                    print("❌ 잘못된 명령:", cmd)
                    continue

                if cmd == "분석":
                    try:
                        result = run_analysis()
                        send_telegram(result)
                        print(f"📊 분석 발송 완료 ({len(result)}자)")
                    except Exception as e:
                        err = f"❌ 분석 오류: {e}"
                        print(err)
                        send_telegram(err)
                    continue

                if cmd in VALUE_CMDS:
                    if len(parts) < 2:
                        print(f"❌ {cmd} 값 없음")
                        continue
                    try:
                        value = float(parts[1])
                    except Exception:
                        print(f"❌ {cmd} 파싱 실패: {parts[1]}")
                        continue

                    target = "all"
                    if len(parts) > 2:
                        if parts[2] == "all":
                            target = "all"
                        else:
                            try:
                                target = int(parts[2])
                            except Exception:
                                print("❌ target 파싱 실패:", parts[2])
                                continue

                    cmd_counter += 1
                    commands.append({
                        "id":     cmd_counter,
                        "cmd":    cmd,
                        "value":  value,
                        "target": target,
                        "time":   time.time(),
                    })
                    print(f"📥 {cmd} {value} | target={target}")
                    continue

                target = "all"
                if len(parts) > 1:
                    if parts[1] == "all":
                        target = "all"
                    else:
                        try:
                            target = int(parts[1])
                        except Exception:
                            print("❌ target 파싱 실패:", parts[1])
                            continue

                cmd_counter += 1
                commands.append({
                    "id":     cmd_counter,
                    "cmd":    cmd,
                    "target": target,
                    "time":   time.time(),
                })
                print(f"📥 명령 수신: {cmd} | target={target}")

        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as e:
            print("⚠️ 텔레그램 연결 오류 (재시도):", e)
            time.sleep(3)
            continue
        except Exception as e:
            print("⚠️ 텔레그램 오류:", e)

        time.sleep(2)

# ==========================================
# 엔드포인트
# ==========================================
@app.route("/get/<int:app_id>")
def get_cmd(app_id):
    matched = []
    now     = time.time()
    for c in commands:
        if now - c["time"] > 60:
            continue
        if c["target"] == app_id or c["target"] == "all":
            matched.append(c)
    commands[:] = [c for c in commands if now - c["time"] <= 60]
    return jsonify(matched)

@app.route("/symbols/<int:app_id>")
def get_symbols(app_id):
    start = (app_id - 1) * 100
    end   = start + 100
    return jsonify(symbol_list[start:end])

@app.route("/trade_log", methods=["POST"])
def post_trade_log():
    try:
        data      = request.json
        record_id = data.get("record_id")
        if not record_id:
            return jsonify({"ok": False}), 400

        with trade_log_lock:
            idx = next(
                (i for i, l in enumerate(trade_log)
                 if l.get("record_id") == record_id),
                None
            )
            if idx is not None:
                trade_log[idx].update(data)
                merged = trade_log[idx]
            else:
                trade_log.append(data)
                merged = data
                print(
                    f"[trade_log] 신규: {record_id} | "
                    f"result={data.get('result')} | 총 {len(trade_log)}건"
                )

        _save_one_trade_log(record_id, merged)

        return jsonify({"ok": True})
    except Exception as e:
        print(f"trade_log POST 오류: {e}")
        return jsonify({"ok": False}), 500

@app.route("/trade_log/analyze")
def get_trade_log():
    with trade_log_lock:
        return jsonify({"data": list(trade_log)})

@app.route("/trade_log/status")
def trade_log_status():
    with trade_log_lock:
        total    = len(trade_log)
        hits     = sum(1 for l in trade_log if l.get("result") == "HIT")
        misses   = sum(1 for l in trade_log if l.get("result") == "MISS")
        pending  = sum(1 for l in trade_log if l.get("result") == "PENDING")
        cancels  = sum(1 for l in trade_log if l.get("result") == "CANCEL_CANDLE")
        oco_loss = sum(
            1 for l in trade_log
            if l.get("result") == "HIT"
            and (l.get("pnl") or 0) < 0
            and l.get("exit_reason") == "OCO_FILLED"
        )
        watch = sum(
            1 for l in trade_log
            if l.get("result") == "HIT"
            and l.get("exit_reason") in ("WATCH_CLOSE", "SLIP_CLOSE")
        )
        timeline = sum(1 for l in trade_log if l.get("signal_timeline"))
        cancel_tracked = sum(1 for l in trade_log if l.get("cancel_would_have_filled") is not None)
    return jsonify({
        "total":          total,
        "hit":            hits,
        "miss":           misses,
        "pending":        pending,
        "cancel_candle":  cancels,
        "oco_loss":       oco_loss,
        "watch_close":    watch,
        "with_timeline":  timeline,
        "cancel_tracked": cancel_tracked,
    })

@app.route("/")
def index():
    return "FastBand Control Server Running"

@app.route("/run_analysis", methods=["POST"])
def trigger_analysis():
    try:
        result = run_analysis()
        send_telegram(result)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ==========================================
# 실행
# ==========================================
if __name__ == "__main__":
    print("🚀 Control Server Started")
    fetch_symbols()
    _init_db()
    _load_trade_log()
    threading.Thread(target=listen_telegram, daemon=True).start()
    threading.Thread(target=symbol_refresh_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=5000)