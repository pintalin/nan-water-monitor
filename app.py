import os
import json
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

import anthropic
import requests
import streamlit as st
import pandas as pd
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
import pytz

load_dotenv()

# ── ค่าคงที่ ──────────────────────────────────────────────────────────────
CAPACITY   = 22 * 35 * 4   # 3,080 ลบ.ม.
TANK_H     = 4
BKK        = pytz.timezone('Asia/Bangkok')
# DATA_DIR — ชี้ไปที่ persistent volume บน cloud หรือโฟลเดอร์ปัจจุบันสำหรับ local
DATA_DIR      = Path(os.getenv('DATA_DIR', '.'))
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE    = DATA_DIR / 'state.json'
SETTINGS_FILE = DATA_DIR / 'settings.json'
DB_FILE       = DATA_DIR / 'water_history.db'

DEFAULT_DEMAND = [
    260.84, 333.84, 304.83, 308.07, 366.84, 440.82, 596.58, 777.68,
    814.18, 800.59, 748.67, 711.21, 699.37, 659.94, 608.37, 651.61,
    687.04, 731.41, 747.02, 728.41, 707.32, 627.25, 513.37, 382.22,
]
LOW_P = {22, 23, 0, 1, 2, 3, 4, 5}
TEMP_BASE = 26.0   # อุณหภูมิฐาน (°C) สำหรับ demand น่าน
TEMP_COEF = 0.015  # demand เพิ่ม 1.5% ต่อ 1°C ที่สูงกว่าฐาน

# ── Weather ───────────────────────────────────────────────────────────────
@st.cache_data(ttl=1800)   # cache 30 นาที
def fetch_weather() -> dict:
    """ดึงอุณหภูมิปัจจุบันจาก OpenWeatherMap สำหรับ อ.เมืองน่าน"""
    api_key = os.getenv('OPENWEATHER_API_KEY', '')
    if not api_key:
        return {'temp': None, 'feels_like': None, 'humidity': None,
                'description': '', 'factor': 1.0, 'error': 'ไม่มี API key'}
    try:
        r = requests.get(
            'https://api.openweathermap.org/data/2.5/weather',
            params={'q': 'Nan,TH', 'appid': api_key, 'units': 'metric', 'lang': 'th'},
            timeout=10,
        )
        d = r.json()
        if d.get('cod') != 200:
            return {'temp': None, 'factor': 1.0, 'error': d.get('message', 'API error')}
        temp      = d['main']['temp']
        feels     = d['main']['feels_like']
        humidity  = d['main']['humidity']
        desc      = d['weather'][0]['description']
        # คำนวณ factor: ทุก 1°C ที่ต่างจากฐาน = ±TEMP_COEF ต่อ demand
        raw_factor = 1.0 + (temp - TEMP_BASE) * TEMP_COEF
        factor     = round(max(0.80, min(1.20, raw_factor)), 4)  # จำกัด ±20%
        return {'temp': temp, 'feels_like': feels, 'humidity': humidity,
                'description': desc, 'factor': factor, 'error': None}
    except Exception as e:
        return {'temp': None, 'factor': 1.0, 'error': str(e)}

def get_demand_with_weather(base_demand: list, weather: dict) -> list:
    """คูณ demand แต่ละชั่วโมงด้วย weather factor"""
    f = weather.get('factor', 1.0)
    return [round(d * f, 2) for d in base_demand]

# ── Settings (แก้ได้จาก UI) ───────────────────────────────────────────────
def load_settings() -> dict:
    defaults = {
        'prod_2_pump':    int(os.getenv('PROD_2_PUMP', 750)),
        'prod_1_pump':    int(os.getenv('PROD_1_PUMP', 350)),
        'demand':         DEFAULT_DEMAND[:],
        'cron_schedule':  os.getenv('CRON_SCHEDULE', '0 * * * *'),
    }
    if SETTINGS_FILE.exists():
        try:
            saved = json.loads(SETTINGS_FILE.read_text(encoding='utf-8'))
            defaults.update(saved)
        except Exception:
            pass
    return defaults

def save_settings(cfg: dict):
    SETTINGS_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding='utf-8')

# ── SQLite ────────────────────────────────────────────────────────────────
def init_db():
    con = sqlite3.connect(DB_FILE)
    con.execute("""
        CREATE TABLE IF NOT EXISTS water_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp   TEXT    NOT NULL,
            hour        INTEGER NOT NULL,
            volume      REAL    NOT NULL,
            pct         REAL    NOT NULL,
            depth       REAL    NOT NULL,
            pumps       INTEGER NOT NULL,
            production  REAL    NOT NULL,
            demand      REAL    NOT NULL,
            net         REAL    NOT NULL
        )
    """)
    con.commit()
    con.close()

def record_to_db(ts: str, hour: int, vol: float, pct: float, depth: float,
                 pumps: int, prod: float, dem: float, net: float):
    con = sqlite3.connect(DB_FILE)
    con.execute(
        "INSERT INTO water_history (timestamp,hour,volume,pct,depth,pumps,production,demand,net) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (ts, hour, vol, pct, depth, pumps, prod, dem, net),
    )
    con.commit()
    con.close()

def get_history(days: int = 7) -> pd.DataFrame:
    if not DB_FILE.exists():
        return pd.DataFrame()
    con = sqlite3.connect(DB_FILE)
    since = (datetime.now(BKK) - timedelta(days=days)).isoformat()
    df = pd.read_sql_query(
        "SELECT timestamp, hour, pct, volume, pumps, production, demand, net "
        "FROM water_history WHERE timestamp >= ? ORDER BY timestamp",
        con, params=(since,),
    )
    con.close()
    return df

# ── State ─────────────────────────────────────────────────────────────────
def load_state() -> dict:
    cfg = load_settings()
    try:
        if STATE_FILE.exists():
            s = json.loads(STATE_FILE.read_text(encoding='utf-8'))
            if s.get('lastUpdated'):
                diff_h = int((time.time() - datetime.fromisoformat(s['lastUpdated']).timestamp()) / 3600)
                if 0 < diff_h <= 24:
                    s['volume'] = sim_forward(s['volume'], s['lastHour'], diff_h, cfg)
            return s
    except Exception:
        pass
    init_pct = float(os.getenv('INIT_PCT', 80))
    return {'volume': CAPACITY * init_pct / 100, 'lastHour': -1, 'lastUpdated': None}

def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding='utf-8')

# ── Simulation ────────────────────────────────────────────────────────────
def sim_forward(vol: float, start_hour: int, steps: int = 1,
                cfg: dict = None, weather: dict = None) -> float:
    if cfg is None:
        cfg = load_settings()
    demand = get_demand_with_weather(cfg['demand'], weather) if weather else cfg['demand']
    prod2  = cfg['prod_2_pump']
    prod1  = cfg['prod_1_pump']
    for i in range(steps):
        h    = (start_hour + i) % 24
        prod = prod1 if vol >= CAPACITY else prod2
        vol  = max(0.0, min(float(CAPACITY), vol + prod - demand[h]))
    return vol

def get_status(pct: float):
    if pct >= 60: return ('ปกติ',  '🟢', '#28a745')
    if pct >= 50: return ('ระวัง', '🟡', '#ffc107')
    return              ('วิกฤต', '🔴', '#dc3545')

# ── Telegram ──────────────────────────────────────────────────────────────
def build_telegram_msg(vol: float, hour: int, cfg: dict, ai_text: str = '') -> str:
    pct   = vol / CAPACITY * 100
    depth = vol / CAPACITY * TANK_H
    label, emoji, _ = get_status(pct)
    ts = datetime.now(BKK).strftime('%d/%m/%Y %H:%M น.')

    msg  = f'<b>รายงานถังน้ำใส กปภ. น่าน</b>\n'
    msg += f'{ts}\n'
    msg += '-' * 28 + '\n'
    msg += f'ระดับน้ำ : <b>{pct:.1f}%</b>  {emoji} {label}\n'
    msg += f'ความลึก  : <b>{depth:.2f} ม.</b> / {TANK_H} ม.\n'
    msg += f'ปริมาตร  : <b>{vol:,.0f} ลบ.ม.</b> / {CAPACITY:,} ลบ.ม.\n'
    msg += '<i>กปภ. สาขาน่าน</i>'
    return msg

def send_telegram(text: str):
    token   = os.getenv('TELEGRAM_BOT_TOKEN', '')
    chat_id = os.getenv('TELEGRAM_CHAT_ID', '')
    if not token or not chat_id:
        raise ValueError('TELEGRAM_BOT_TOKEN หรือ TELEGRAM_CHAT_ID ไม่ได้ตั้งค่า')
    r    = requests.post(
        f'https://api.telegram.org/bot{token}/sendMessage',
        json={'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML'},
        timeout=15,
    )
    data = r.json()
    if not data.get('ok'):
        raise ValueError(data.get('description', 'Unknown error'))

# ── AI วิเคราะห์ ──────────────────────────────────────────────────────────
def get_ai_analysis(vol: float, hour: int, cfg: dict) -> str:
    api_key = os.getenv('ANTHROPIC_API_KEY', '')
    if not api_key:
        return ''
    try:
        demand = cfg['demand']
        prod2  = cfg['prod_2_pump']
        prod1  = cfg['prod_1_pump']
        pct    = vol / CAPACITY * 100
        prod   = prod1 if vol >= CAPACITY else prod2
        pumps  = 1     if vol >= CAPACITY else 2
        out    = demand[hour]
        net    = prod - out
        min_vol = vol
        for i in range(1, 9):
            min_vol = min(min_vol, sim_forward(vol, hour, i, cfg))
        min_pct = min_vol / CAPACITY * 100

        client = anthropic.Anthropic(api_key=api_key)
        res = client.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=300,
            system=[{
                'type': 'text',
                'text': 'คุณเป็น AI วิเคราะห์ระบบประปา กปภ. ตอบภาษาไทย กระชับ 3 บรรทัด มีคำแนะนำปฏิบัติชัดเจน',
                'cache_control': {'type': 'ephemeral'},
            }],
            messages=[{'role': 'user', 'content':
                f'ถัง {CAPACITY} ลบ.ม. ระดับ {pct:.1f}% ชม.{hour}:00 '
                f'ปั๊ม {pumps} เครื่อง ผลิต {prod} จ่าย {out:.0f} net {net:.0f} '
                f'ต่ำสุด 8 ชม. {min_pct:.1f}%'
                f'{" ต่ำกว่า 50%" if pct < 50 else ""}'
                f'{" ช่วงลดแรงดัน" if hour in LOW_P else ""} วิเคราะห์และแนะนำ'
            }],
        )
        return next((b.text for b in res.content if b.type == 'text'), '')
    except Exception as e:
        return f'(AI error: {e})'

# ── งานรายชั่วโมง ─────────────────────────────────────────────────────────
def run_report():
    cfg   = load_settings()
    now   = datetime.now(BKK)
    hour  = now.hour
    state = load_state()

    weather = fetch_weather()
    if state['lastHour'] not in (-1, hour):
        steps = (hour - state['lastHour']) % 24 or 1
        state['volume'] = sim_forward(state['volume'], state['lastHour'], steps, cfg, weather)

    vol         = state['volume']
    pct         = vol / CAPACITY * 100
    depth       = vol / CAPACITY * TANK_H
    prod        = cfg['prod_1_pump'] if vol >= CAPACITY else cfg['prod_2_pump']
    pumps       = 1 if vol >= CAPACITY else 2
    adj_demand  = get_demand_with_weather(cfg['demand'], weather)
    dem         = adj_demand[hour]
    net         = prod - dem

    # บันทึกลง DB
    record_to_db(now.isoformat(), hour, vol, pct, depth, pumps, prod, dem, net)

    try:
        ai_text = get_ai_analysis(vol, hour, cfg)
        msg     = build_telegram_msg(vol, hour, cfg, ai_text)
        send_telegram(msg)
        state['lastHour']    = hour
        state['lastUpdated'] = now.isoformat()
        save_state(state)
        return True, 'ส่งสำเร็จ'
    except Exception as e:
        try:
            send_telegram(f'<b>ระบบรายงานขัดข้อง</b>\n{e}')
        except Exception:
            pass
        return False, str(e)

# ── Scheduler ─────────────────────────────────────────────────────────────
@st.cache_resource
def get_scheduler():
    init_db()
    sched = BackgroundScheduler(timezone=BKK)
    cfg   = load_settings()
    sched.add_job(run_report, CronTrigger.from_crontab(cfg['cron_schedule'], timezone=BKK),
                  id='water_report')
    sched.start()
    return sched

def update_scheduler_cron(sched, new_cron: str):
    try:
        sched.reschedule_job('water_report',
                             trigger=CronTrigger.from_crontab(new_cron, timezone=BKK))
        return True
    except Exception:
        return False

# ══════════════════════════════════════════════════════════════════════════
# UI
# ══════════════════════════════════════════════════════════════════════════
st.set_page_config(page_title='ถังน้ำใส กปภ. น่าน', page_icon='💧', layout='wide')
st.title('💧 ระบบรายงานถังน้ำใส — กปภ. สาขาน่าน')

scheduler = get_scheduler()
cfg       = load_settings()

# โหลด weather + state
weather = fetch_weather()
state   = load_state()
now     = datetime.now(BKK)
hour    = now.hour
if state['lastHour'] not in (-1, hour):
    steps = (hour - state['lastHour']) % 24 or 1
    state['volume'] = sim_forward(state['volume'], state['lastHour'], steps, cfg, weather)

vol        = state['volume']
pct        = vol / CAPACITY * 100
depth      = vol / CAPACITY * TANK_H
prod       = cfg['prod_1_pump'] if vol >= CAPACITY else cfg['prod_2_pump']
pumps      = 1 if vol >= CAPACITY else 2
adj_demand = get_demand_with_weather(cfg['demand'], weather)
out        = adj_demand[hour]
net        = prod - out
label, emoji, color = get_status(pct)

# ── Tabs ──────────────────────────────────────────────────────────────────
tab_dash, tab_hist, tab_settings = st.tabs(['📊 Dashboard', '📈 ประวัติ', '⚙️ ตั้งค่า'])

# ══════════════════════════════════
# Tab 1: Dashboard
# ══════════════════════════════════
with tab_dash:
    badge_bg  = '#1a7f3c' if pct >= 60 else ('#a16207' if pct >= 50 else '#9f1239')
    fill_top  = '#00e5ff' if pct >= 60 else ('#facc15' if pct >= 50 else '#f87171')
    fill_bot  = '#0077b6' if pct >= 60 else ('#b45309' if pct >= 50 else '#dc2626')
    net_color = '#4ade80' if net >= 0 else '#f87171'
    ts_str    = now.strftime('%d/%m/%Y %H:%M น.')
    fill_pct  = min(pct, 100)

    st.markdown(f"""
<style>
.db-header{{margin-bottom:18px}}
.db-title{{font-size:1.5rem;font-weight:700;color:#e6edf3;margin:0 0 6px 0}}
.db-tags{{display:flex;gap:8px;align-items:center;flex-wrap:wrap}}
.db-tag{{background:#21262d;border:1px solid #30363d;border-radius:6px;
         padding:3px 10px;font-size:.8rem;color:#8b949e}}
.db-badge{{background:{badge_bg};border-radius:6px;padding:3px 12px;
           font-size:.8rem;font-weight:600;color:#fff}}
.db-layout{{display:flex;gap:24px;align-items:flex-start;margin-top:16px}}
.db-tank-wrap{{display:flex;flex-direction:column;align-items:center;min-width:100px}}
.db-tank{{position:relative;width:72px;height:260px;
          border:2px solid #30363d;border-radius:6px 6px 10px 10px;
          background:#0d1117;overflow:hidden}}
.db-water{{position:absolute;bottom:0;width:100%;height:{fill_pct:.1f}%;
           background:linear-gradient(to top,{fill_bot},{fill_top});
           transition:height .8s ease}}
.db-marker{{position:absolute;width:100%;display:flex;align-items:center}}
.db-marker-line{{flex:1;border-top:1px dashed rgba(255,255,255,.25)}}
.db-marker-lbl{{font-size:10px;color:rgba(255,255,255,.45);padding:0 4px;white-space:nowrap}}
.db-pct-overlay{{position:absolute;top:8px;width:100%;text-align:center;
                 font-size:1rem;font-weight:700;color:#fff;text-shadow:0 1px 4px rgba(0,0,0,.8)}}
.db-tank-base{{width:36px;height:10px;background:#30363d;border-radius:0 0 4px 4px;margin-top:-1px}}
.db-cards{{display:grid;grid-template-columns:1fr 1fr;gap:14px;flex:1}}
.db-card{{background:#161b22;border:1px solid #30363d;border-radius:10px;
          padding:18px 20px}}
.db-card-lbl{{font-size:.8rem;color:#8b949e;margin-bottom:6px}}
.db-card-val{{font-size:1.65rem;font-weight:700;color:#e6edf3;line-height:1}}
.db-card-sub{{font-size:.75rem;color:#8b949e;margin-top:4px}}
.db-net-val{{font-size:1.65rem;font-weight:700;color:{net_color};line-height:1}}
.db-ts{{font-size:.75rem;color:#8b949e;margin-top:14px}}
</style>

<div class="db-header">
  <div class="db-title">💧 ระบบบริหารถังน้ำใส กปภ. น่าน</div>
  <div class="db-tags">
    <span class="db-tag">22×35×4 ม.</span>
    <span class="db-tag">{CAPACITY:,} ลบ.ม.</span>
    <span class="db-badge">{label}</span>
  </div>
</div>

<div class="db-layout">
  <div class="db-tank-wrap">
    <div class="db-tank">
      <div class="db-water"></div>
      <div class="db-pct-overlay">{pct:.1f}%</div>
      <div class="db-marker" style="bottom:75%">
        <div class="db-marker-lbl">75%</div><div class="db-marker-line"></div>
      </div>
      <div class="db-marker" style="bottom:50%">
        <div class="db-marker-lbl">50%</div><div class="db-marker-line"></div>
      </div>
      <div class="db-marker" style="bottom:25%">
        <div class="db-marker-lbl">25%</div><div class="db-marker-line"></div>
      </div>
    </div>
    <div class="db-tank-base"></div>
  </div>

  <div class="db-cards">
    <div class="db-card">
      <div class="db-card-lbl">ระดับน้ำ</div>
      <div class="db-card-val">{pct:.1f} %</div>
    </div>
    <div class="db-card">
      <div class="db-card-lbl">ปริมาตร</div>
      <div class="db-card-val">{vol:,.0f}</div>
      <div class="db-card-sub">ลบ.ม.</div>
    </div>
    <div class="db-card">
      <div class="db-card-lbl">ความลึก</div>
      <div class="db-card-val">{depth:.2f} ม.</div>
    </div>
    <div class="db-card">
      <div class="db-card-lbl">เน็ต ชม.นี้</div>
      <div class="db-net-val">{net:+,.0f} ลบ.ม.</div>
    </div>
  </div>
</div>
<div class="db-ts">อัปเดต {ts_str}</div>
""", unsafe_allow_html=True)

    # weather strip
    temp_val   = weather.get('temp')
    w_factor   = weather.get('factor', 1.0)
    w_err      = weather.get('error')
    if temp_val is not None:
        delta_pct  = (w_factor - 1.0) * 100
        sign       = '+' if delta_pct >= 0 else ''
        hum        = weather.get('humidity', '-')
        desc       = weather.get('description', '')
        feels      = weather.get('feels_like', temp_val)
        adj_color  = '#f87171' if delta_pct > 0 else ('#4ade80' if delta_pct < 0 else '#8b949e')
        st.markdown(f"""
<div style="background:#161b22;border:1px solid #30363d;border-radius:10px;
            padding:12px 20px;margin:12px 0;display:flex;gap:32px;align-items:center;flex-wrap:wrap">
  <div>
    <div style="font-size:.75rem;color:#8b949e">อุณหภูมิ อ.น่าน</div>
    <div style="font-size:1.4rem;font-weight:700;color:#e6edf3">{temp_val:.1f}°C
      <span style="font-size:.85rem;color:#8b949e">รู้สึก {feels:.0f}°C</span>
    </div>
    <div style="font-size:.78rem;color:#8b949e">{desc} · ความชื้น {hum}%</div>
  </div>
  <div>
    <div style="font-size:.75rem;color:#8b949e">ปรับ demand จากอุณหภูมิ</div>
    <div style="font-size:1.2rem;font-weight:700;color:{adj_color}">{sign}{delta_pct:.1f}%</div>
    <div style="font-size:.75rem;color:#8b949e">ฐาน {TEMP_BASE}°C · factor {w_factor:.3f}</div>
  </div>
</div>
""", unsafe_allow_html=True)
    elif w_err and 'ไม่มี API key' not in w_err:
        st.warning(f'Weather API: {w_err}')
    elif 'ไม่มี API key' in (w_err or ''):
        st.caption('🌡️ ยังไม่ได้ตั้งค่า OPENWEATHER_API_KEY — demand ใช้ค่า baseline')

    if pct < 50:
        st.error('⚠️ ระดับน้ำต่ำกว่า 50% — ต้องเดิน 2 เครื่องทันที!')
    elif pct < 60:
        st.warning('ระดับน้ำอยู่ในเกณฑ์ระวัง (50–60%)')

    st.divider()
    st.subheader('🔮 พยากรณ์ 8 ชั่วโมงข้างหน้า')
    rows, v = [], vol
    for i in range(9):
        h = (hour + i) % 24
        rows.append({'ชั่วโมง': f'{h:02d}:00', 'ระดับน้ำ (%)': round(v / CAPACITY * 100, 1)})
        v = sim_forward(v, h, 1, cfg, weather)
    st.line_chart(pd.DataFrame(rows).set_index('ชั่วโมง'), height=220)

# ══════════════════════════════════
# Tab 2: ประวัติ
# ══════════════════════════════════
with tab_hist:
    days_opt = st.selectbox('แสดงข้อมูลย้อนหลัง', [1, 3, 7, 14, 30], index=2,
                            format_func=lambda d: f'{d} วัน')
    df_hist = get_history(days_opt)

    if df_hist.empty:
        st.info('ยังไม่มีข้อมูลในฐานข้อมูล — ระบบจะบันทึกอัตโนมัติทุกชั่วโมง')
    else:
        df_hist['เวลา'] = pd.to_datetime(df_hist['timestamp']).dt.strftime('%d/%m %H:%M')

        st.subheader('ระดับน้ำ (%)')
        st.line_chart(df_hist.set_index('เวลา')[['pct']].rename(columns={'pct': 'ระดับน้ำ (%)'}),
                      height=250)

        st.subheader('การผลิต vs จ่ายน้ำ (ลบ.ม./ชม.)')
        st.line_chart(
            df_hist.set_index('เวลา')[['production', 'demand']].rename(
                columns={'production': 'ผลิต', 'demand': 'จ่าย'}),
            height=200,
        )

        st.subheader('ตารางข้อมูลดิบ')
        show_df = df_hist[['เวลา', 'pct', 'volume', 'pumps', 'production', 'demand', 'net']].copy()
        show_df.columns = ['เวลา', 'ระดับ (%)', 'ปริมาตร', 'เครื่องสูบ', 'ผลิต', 'จ่าย', 'Net']
        st.dataframe(show_df, width='stretch')

        csv = show_df.to_csv(index=False).encode('utf-8-sig')
        st.download_button('ดาวน์โหลด CSV', csv,
                           file_name=f'water_history_{days_opt}d.csv', mime='text/csv')

# ══════════════════════════════════
# Tab 3: ตั้งค่า
# ══════════════════════════════════
with tab_settings:
    st.subheader('⚙️ กำลังผลิตปั๊ม')
    col1, col2 = st.columns(2)
    with col1:
        new_prod2 = st.number_input('กำลังผลิต 2 ปั๊ม (ลบ.ม./ชม.)',
                                    min_value=100, max_value=2000,
                                    value=cfg['prod_2_pump'], step=10)
    with col2:
        new_prod1 = st.number_input('กำลังผลิต 1 ปั๊ม (ลบ.ม./ชม.)',
                                    min_value=100, max_value=2000,
                                    value=cfg['prod_1_pump'], step=10)

    st.divider()
    st.subheader('📋 Baseline น้ำจ่ายรายชั่วโมง (ลบ.ม./ชม.)')
    st.caption('แก้ไขตัวเลขในตารางได้โดยตรง แล้วกดบันทึก')

    demand_df = pd.DataFrame({
        'ชั่วโมง': [f'{h:02d}:00' for h in range(24)],
        'จ่ายน้ำ (ลบ.ม./ชม.)': cfg['demand'],
    })
    edited_df = st.data_editor(demand_df, width='stretch',
                               disabled=['ชั่วโมง'],
                               column_config={
                                   'จ่ายน้ำ (ลบ.ม./ชม.)': st.column_config.NumberColumn(
                                       min_value=0, max_value=2000, step=1, format='%.2f')
                               })

    st.divider()
    st.subheader('⏰ กำหนดเวลาส่งรายงาน')
    cron_presets = {
        'ทุกชั่วโมง 24 ชม. (แนะนำ)': '0 * * * *',
        '17:00–23:00 เท่านั้น':        '0 17-23 * * *',
        'ทุก 30 นาที':                  '*/30 * * * *',
        'กำหนดเอง':                     '__custom__',
    }
    preset_label = next(
        (k for k, v in cron_presets.items() if v == cfg['cron_schedule']),
        'กำหนดเอง'
    )
    selected = st.selectbox('รูปแบบ', list(cron_presets.keys()),
                            index=list(cron_presets.keys()).index(preset_label)
                            if preset_label in cron_presets else 3)
    if cron_presets[selected] == '__custom__':
        new_cron = st.text_input('Cron expression', value=cfg['cron_schedule'])
    else:
        new_cron = cron_presets[selected]
    st.caption(f'Cron ปัจจุบัน: `{cfg["cron_schedule"]}`')

    st.divider()
    if st.button('💾 บันทึกการตั้งค่าทั้งหมด', type='primary', use_container_width=True):
        new_demand = edited_df['จ่ายน้ำ (ลบ.ม./ชม.)'].tolist()
        new_cfg = {
            'prod_2_pump':   int(new_prod2),
            'prod_1_pump':   int(new_prod1),
            'demand':        new_demand,
            'cron_schedule': new_cron,
        }
        save_settings(new_cfg)
        if new_cron != cfg['cron_schedule']:
            update_scheduler_cron(scheduler, new_cron)
        st.success('✅ บันทึกเรียบร้อย — การตั้งค่าใหม่มีผลทันที')
        st.rerun()

# ── Sidebar ───────────────────────────────────────────────────────────────
with st.sidebar:
    st.header('⚙️ ควบคุมระบบ')

    st.subheader('📤 ส่งรายงาน Telegram')
    if st.button('ส่งทันที', use_container_width=True, type='primary'):
        with st.spinner('กำลังส่ง...'):
            ok, result = run_report()
        if ok:
            st.success(f'✅ {result}')
        else:
            st.error(f'❌ {result}')

    st.divider()

    st.subheader('📊 อัปเดตระดับน้ำจริง')
    new_pct = st.number_input('ระดับน้ำ (%)', min_value=0.0, max_value=100.0,
                               value=round(pct, 1), step=0.5,
                               help='วัดจากมาตรวัดจริง')
    if st.button('บันทึก', use_container_width=True):
        save_state({'volume': CAPACITY * new_pct / 100,
                    'lastHour': hour, 'lastUpdated': now.isoformat()})
        st.success(f'✅ บันทึก {new_pct:.1f}%')
        st.rerun()

    st.divider()

    st.subheader('⏰ Auto-Send')
    job = scheduler.get_job('water_report')
    if job and job.next_run_time:
        next_dt   = job.next_run_time.astimezone(BKK)
        mins_left = max(0, int((next_dt - datetime.now(BKK)).total_seconds() // 60))
        st.info(f'ถัดไป: **{next_dt.strftime("%H:%M น.")}**\n\nอีก {mins_left} นาที')
    else:
        st.caption('ไม่มีงานในตาราง')
    st.caption(f'Cron: `{cfg["cron_schedule"]}`')

    st.divider()
    st.caption(f'2 ปั๊ม: {cfg["prod_2_pump"]:,} ลบ.ม./ชม.')
    st.caption(f'1 ปั๊ม: {cfg["prod_1_pump"]:,} ลบ.ม./ชม.')
    st.caption(f'ถัง: {CAPACITY:,} ลบ.ม.')
    if os.getenv('ANTHROPIC_API_KEY'):
        st.caption('🤖 AI: เปิดใช้งาน')
    else:
        st.caption('🤖 AI: ปิด')

st.markdown('<meta http-equiv="refresh" content="300">', unsafe_allow_html=True)
