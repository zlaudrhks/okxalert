from threading import Lock
from collections import deque, defaultdict

# ==========================================
# 앱 설정
# ==========================================
APP_VERSION = "2.0.0"
APP_ID = 1
TOTAL_SYMBOLS = 210
SYMBOLS_PER_APP = 100

# ==========================================
# 전역 상태
# ==========================================
trading_enabled = True
adx_filter_enabled = True
risk_guard_triggered = False
vol_exhausted_filter_enabled = True

positions = {}
pending_entries = set()
sliding_state = {}
instrument_meta = {}
symbols_cache = []
lock = Lock()

last_entry_time = {}
last_failed_order_time = {}
last_exit_bucket = {}
last_sl_price = {}
last_sl_side = {}
last_alarm_time = {}
adx_blocked = {}
processed_cmd_ids = set()
last_cmd_id = None

ws_connected_once = False
WS_STARTED = False
APP_STARTED = False

recent_prices = defaultdict(lambda: deque(maxlen=20))
recent_tick_volumes = defaultdict(lambda: deque(maxlen=200))

bar_state = defaultdict(lambda: {
    "bucket": None, "o": None, "h": None, "l": None, "c": None, "volCcy": 0.0
})
bar_state_10m = defaultdict(lambda: {
    "bucket": None, "o": None, "c": None
})
bar_state_1h = defaultdict(lambda: {
    "bucket": None, "o": None, "c": None
})

BAR_SEC_10M = 600
BAR_SEC_1H  = 3600
ALARM_COOLDOWN_SEC = 60

closed_closes  = defaultdict(lambda: deque(maxlen=30))
closed_highs   = defaultdict(lambda: deque(maxlen=30))
closed_lows    = defaultdict(lambda: deque(maxlen=30))
closed_volumes = defaultdict(lambda: deque(maxlen=30))
prev_close_map = {}

# ==========================================
# 파라미터
# ==========================================
ADX_PERIOD    = 14
ADX_THRESHOLD = 25

# ✅ RSI 필터
RSI_PERIOD         = 14
RSI_FILTER_ENABLED = True
RSI_LONG_MAX       = 30.0   # LONG 진입: RSI <= 30
RSI_SHORT_MIN      = 70.0   # SHORT 진입: RSI >= 70

EXTRA_DIST_ENABLED   = True
EXTRA_DIST_THRESHOLD = 12.0
EXTRA_DIST_PCT       = 0.5
EXTRA_DIST_RANGES = [
    {"min": 10.0, "max": 12.0,  "pct": 2.0,  "enabled": True},
    {"min": 12.0, "max": 18.0,  "pct": 5.0,  "enabled": True},
    {"min": 18.0, "max": 999.0, "pct": 10.0, "enabled": True},
]

ENTRY_DELAY_SEC     = 1.0
TP_PCT              = 0.012
SL_PCT              = 0.015
usdt_amount         = 1500
total_pnl_usdt      = 0.0

ENTRY_SLIP_ENABLED = True
ENTRY_SLIP_PCT     = 0.015
MAX_LOSS_USDT       = -200
SYMBOL_BLACKLIST    = set()
ENTRY_WATCH_ENABLED = True
ENTRY_WATCH_PCT     = 0.008
ENTRY_WATCH_SEC     = 3

# ==========================================
# [v2 개선 1] 손익비 개선 (1차 분할 익절 / 본절 스탑 / 트레일링)
# ==========================================
PARTIAL_TP_ENABLED        = True
PARTIAL_TP_PCT            = 0.010    # 1차 익절 목표: +1.0% (수수료 제하고 순익 조기 확보)
PARTIAL_TP_RATIO          = 0.50     # 1차 익절 수량: 50%
BEP_STOP_ENABLED          = True     # 1차 익절 후 잔여 수량 스탑로스를 본절가(BEP)로 이동
BEP_BUFFER_PCT            = 0.001    # 본절 스탑 버퍼 (+0.1%로 슬리피지/수수료 완벽 보전)
TP2_EXTEND_MULT           = 1.5      # 잔여 50%의 2차 목표가 배수 (1.5x ~ 2.0x)

# 잔여 포지션 트레일링 익절
TRAIL_ENABLED             = True     # 1차 익절 후 또는 목표 수익 도달 시 트레일링 활성화
TRAIL_ACTIVATE_PCT        = 1.2      # 최고 수익률 +1.2% 도달 시 트레일링 감시 가동
TRAIL_DISTANCE_PCT        = 0.4      # 최고점 대비 0.4% 되돌림 발생 시 전량 시장가 익절

# ==========================================
# [v2 개선 2] 비트코인(BTC) 연동 필터 (시장 서킷 브레이커)
# ==========================================
BTC_CIRCUIT_ENABLED       = True
BTC_SYMBOL                = "BTC-USDT-SWAP"
BTC_5M_SURGE_PCT          = 1.0      # BTC 5분봉 등락률 ±1.0% 이상 급변동 시 알트코인 신규 진입 차단
BTC_COOLDOWN_SEC          = 180      # 서킷브레이커 발동 후 최소 대기 시간 (3분)
last_btc_circuit_time     = 0        # 최근 발동 타임스탬프

# ==========================================
# [v2 개선 4] ATR 기반 동적 TP / SL
# ==========================================
ATR_DYNAMIC_ENABLED       = True
ATR_PERIOD                = 14       # 최근 14개 5분봉 ATR 기준
ATR_TP_MULT               = 1.4      # TP = ATR * 1.4 (종목별 고유 변동폭 맞춤)
ATR_SL_MULT               = 1.8      # SL = ATR * 1.8 (휩쏘 노이즈 회피)
ATR_MIN_TP_PCT            = 0.008    # 최소 TP 상한선: 0.8%
ATR_MAX_TP_PCT            = 0.025    # 최대 TP 상한선: 2.5%
ATR_MIN_SL_PCT            = 0.010    # 최소 SL 상한선: 1.0%
ATR_MAX_SL_PCT            = 0.030    # 최대 SL 상한선: 3.0%

# 거래량 소진 필터 파라미터
VOL_SPLIT      = 0.5   # 전체 구간을 나누는 지점 (0.5 = 50%)
VOL_THRESHOLD  = 0.4   # late/early 비율이 이 미만이면 소진으로 판단

CONTROL_SERVER_URL = "https://server-zlaudrhks.replit.app"

# ==========================================
# 학습된 이격거리
# ==========================================
# key: "MID_5M", "MID_10M+1H", "DEEP_5M", "DEEP_10M+1H"
# 값이 없으면 동적 계산 사용
LEARNED_MIN_DIST  = {}
LEARNED_EXTRA_PCT = {}  # key: 구간 index (0,1,2)

SLIDING_ENABLED = True  # 기본 ON