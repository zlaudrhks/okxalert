from threading import Lock
from collections import deque, defaultdict

# ==========================================
# 앱 설정
# ==========================================
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
TRAIL_ACTIVATE_PCT  = 0.8
TRAIL_DISTANCE_PCT  = 0.4
TRAIL_ENABLED       = False

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