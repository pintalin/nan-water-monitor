"""
Work Time Tracker — แอปติดตามเวลาทำงาน
Backend: Google Sheets (หรือ SQLite สำรอง)
"""
import os
import json
import sqlite3
import time
from datetime import datetime, date, timedelta
from pathlib import Path

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
from dotenv import load_dotenv
import pytz

load_dotenv()

# ─── Constants ────────────────────────────────────────────────────────────────
BKK = pytz.timezone("Asia/Bangkok")
WORK_HRS = 8  # ชม./วัน ปกติ

LOG_SHEET = "บันทึกงาน"
PROJECT_SHEET = "โปรเจ็ค"

TASK_TYPES = ["โปรเจ็ค", "Adhoc", "งานแทน/ช่วย", "ประชุม", "เรียน/พัฒนา", "อื่นๆ"]
NON_PROJECT_TYPES = {"Adhoc", "งานแทน/ช่วย", "ประชุม", "เรียน/พัฒนา", "อื่นๆ"}

PRIORITY_LIST = ["High", "Medium", "Low"]
STATUS_LIST = ["กำลังทำ", "รอ", "เสร็จแล้ว", "ยกเลิก"]

WARN_ADHOC_PCT = 50
DANGER_ADHOC_PCT = 60
WARN_OT_HRS = 1.0
DANGER_OT_HRS = 2.0

# ─── Page config ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Work Time Tracker",
    page_icon="⏱️",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
.timer-display {
    font-size: 2.4rem; font-weight: bold; text-align: center;
    padding: 0.5rem; border-radius: 8px;
}
.kpi-box { text-align: center; padding: 0.5rem; }
.kpi-label { font-size: 0.85rem; color: #6b7280; }
.kpi-value { font-size: 1.9rem; font-weight: 800; }
.c-normal { color: #374151; }
.c-warn   { color: #f97316; }
.c-danger { color: #ef4444; }
div[data-testid="metric-container"] {
    background: #f9fafb; border: 1px solid #e5e7eb;
    border-radius: 8px; padding: 10px;
}
</style>
""", unsafe_allow_html=True)

# ─── Local SQLite (always available) ──────────────────────────────────────────
@st.cache_resource
def _db():
    db_path = Path(os.getenv("DATA_DIR", ".")) / "work_tracker.db"
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS work_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            log_date    TEXT NOT NULL,
            day_name    TEXT,
            start_time  TEXT,
            end_time    TEXT,
            hours       REAL DEFAULT 0,
            task        TEXT,
            project     TEXT,
            task_type   TEXT,
            note        TEXT,
            created_at  TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS projects (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            project     TEXT NOT NULL,
            subtask     TEXT,
            owner       TEXT,
            due_date    TEXT,
            priority    TEXT DEFAULT 'Medium',
            status      TEXT DEFAULT 'กำลังทำ',
            pct_done    REAL DEFAULT 0,
            note        TEXT,
            created_at  TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.commit()
    return conn

# ─── Google Sheets client (optional) ──────────────────────────────────────────
@st.cache_resource
def _gsheets_client():
    try:
        import gspread  # noqa: PLC0415
        from google.oauth2.service_account import Credentials  # noqa: PLC0415

        creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON", "")
        if not creds_json:
            cf = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
            if Path(cf).exists():
                creds_json = Path(cf).read_text()
        if not creds_json:
            return None
        scopes = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = Credentials.from_service_account_info(json.loads(creds_json), scopes=scopes)
        return gspread.authorize(creds)
    except Exception:
        return None

def _spreadsheet():
    sid = os.getenv("SPREADSHEET_ID", st.session_state.get("spreadsheet_id", ""))
    if not sid:
        return None
    gc = _gsheets_client()
    if not gc:
        return None
    try:
        ss = gc.open_by_key(sid)
        # Ensure required sheets exist
        existing = {ws.title for ws in ss.worksheets()}
        if LOG_SHEET not in existing:
            ws = ss.add_worksheet(LOG_SHEET, rows=1000, cols=10)
            ws.append_row(["วันที่", "วัน", "เริ่ม", "จบ", "ชม.", "งาน", "โปรเจ็ค", "ประเภท", "หมายเหตุ"])
        if PROJECT_SHEET not in existing:
            ws = ss.add_worksheet(PROJECT_SHEET, rows=200, cols=9)
            ws.append_row(["โปรเจ็ค", "งานย่อย", "Owner", "Due date", "ความสำคัญ", "สถานะ", "% เสร็จ", "หมายเหตุ"])
        return ss
    except Exception as e:
        st.warning(f"Google Sheets: {e}")
        return None

def _using_gsheets() -> bool:
    return bool(os.getenv("SPREADSHEET_ID") or st.session_state.get("spreadsheet_id"))

# ─── Helpers ──────────────────────────────────────────────────────────────────
_DAY_TH = {0: "จ", 1: "อ", 2: "พ", 3: "พฤ", 4: "ศ", 5: "ส", 6: "อา"}


def _level(adhoc_pct: float, ot_hrs: float) -> str:
    if adhoc_pct >= DANGER_ADHOC_PCT or ot_hrs >= DANGER_OT_HRS:
        return "danger"
    if adhoc_pct >= WARN_ADHOC_PCT or ot_hrs >= WARN_OT_HRS:
        return "warn"
    return "normal"


def _kpi(label: str, value: str, level: str = "normal") -> None:
    css = {"normal": "c-normal", "warn": "c-warn", "danger": "c-danger"}.get(level, "c-normal")
    st.markdown(
        f'<div class="kpi-box"><div class="kpi-label">{label}</div>'
        f'<div class="kpi-value {css}">{value}</div></div>',
        unsafe_allow_html=True,
    )


def _this_week():
    today = datetime.now(BKK).date()
    mon = today - timedelta(days=today.weekday())
    return mon, mon + timedelta(days=6)


def _norm_hours(df: pd.DataFrame) -> pd.DataFrame:
    """Make sure the hours column is numeric."""
    for c in ("hours", "ชม."):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    return df


def _hcol(df):
    return "hours" if "hours" in df.columns else "ชม."


def _tcol(df):
    return "task_type" if "task_type" in df.columns else "ประเภท"


def _pcol(df):
    return "project" if "project" in df.columns else "โปรเจ็ค"


def _dcol(df):
    return "log_date" if "log_date" in df.columns else "วันที่"

# ─── Data: Work Log ───────────────────────────────────────────────────────────
def get_log_df(start: date = None, end: date = None) -> pd.DataFrame:
    if _using_gsheets():
        ss = _spreadsheet()
        if ss:
            try:
                recs = ss.worksheet(LOG_SHEET).get_all_records()
                df = pd.DataFrame(recs)
                if not df.empty:
                    df["log_date_obj"] = pd.to_datetime(df["วันที่"], errors="coerce").dt.date
                    if start:
                        df = df[df["log_date_obj"] >= start]
                    if end:
                        df = df[df["log_date_obj"] <= end]
                    df = df.drop(columns=["log_date_obj"])
                    df["_row"] = range(2, len(df) + 2)  # 1-based row incl header
                    return df
                return pd.DataFrame(columns=["วันที่","วัน","เริ่ม","จบ","ชม.","งาน","โปรเจ็ค","ประเภท","หมายเหตุ","_row"])
            except Exception as e:
                st.warning(f"GSheets read error: {e}")

    db = _db()
    q = "SELECT rowid AS id, * FROM work_log WHERE 1=1"
    p: list = []
    if start:
        q += " AND log_date >= ?"
        p.append(str(start))
    if end:
        q += " AND log_date <= ?"
        p.append(str(end))
    q += " ORDER BY log_date, start_time"
    rows = db.execute(q, p).fetchall()
    if not rows:
        return pd.DataFrame(columns=["id","log_date","day_name","start_time","end_time","hours","task","project","task_type","note"])
    return pd.DataFrame([dict(r) for r in rows])


def add_log(entry: dict) -> None:
    d: date = entry["log_date"]
    day = _DAY_TH.get(d.weekday(), "")
    if _using_gsheets():
        ss = _spreadsheet()
        if ss:
            try:
                ss.worksheet(LOG_SHEET).append_row([
                    str(d), day,
                    entry.get("start_time", ""), entry.get("end_time", ""),
                    round(entry.get("hours", 0), 2),
                    entry.get("task", ""), entry.get("project", ""),
                    entry.get("task_type", ""), entry.get("note", ""),
                ])
                return
            except Exception as e:
                st.warning(f"GSheets write error: {e} — บันทึกใน local")

    db = _db()
    db.execute(
        "INSERT INTO work_log (log_date,day_name,start_time,end_time,hours,task,project,task_type,note) VALUES (?,?,?,?,?,?,?,?,?)",
        (str(d), day, entry.get("start_time",""), entry.get("end_time",""),
         entry.get("hours", 0), entry.get("task",""), entry.get("project",""),
         entry.get("task_type",""), entry.get("note","")),
    )
    db.commit()


def delete_log(row_id) -> None:
    if _using_gsheets():
        ss = _spreadsheet()
        if ss:
            try:
                ss.worksheet(LOG_SHEET).delete_rows(int(row_id))
                return
            except Exception as e:
                st.warning(f"GSheets delete error: {e}")
    db = _db()
    db.execute("DELETE FROM work_log WHERE rowid = ?", (int(row_id),))
    db.commit()

# ─── Data: Projects ───────────────────────────────────────────────────────────
def get_projects_df() -> pd.DataFrame:
    if _using_gsheets():
        ss = _spreadsheet()
        if ss:
            try:
                recs = ss.worksheet(PROJECT_SHEET).get_all_records()
                df = pd.DataFrame(recs)
                if not df.empty:
                    df["_row"] = range(2, len(df) + 2)
                    return df
                return pd.DataFrame(columns=["โปรเจ็ค","งานย่อย","Owner","Due date","ความสำคัญ","สถานะ","% เสร็จ","หมายเหตุ","_row"])
            except Exception as e:
                st.warning(f"GSheets read error: {e}")

    db = _db()
    rows = db.execute("SELECT rowid AS id, * FROM projects ORDER BY project, priority").fetchall()
    if not rows:
        return pd.DataFrame(columns=["id","project","subtask","owner","due_date","priority","status","pct_done","note"])
    return pd.DataFrame([dict(r) for r in rows])


def add_project(proj: dict) -> None:
    if _using_gsheets():
        ss = _spreadsheet()
        if ss:
            try:
                ss.worksheet(PROJECT_SHEET).append_row([
                    proj.get("project",""), proj.get("subtask",""),
                    proj.get("owner",""), proj.get("due_date",""),
                    proj.get("priority","Medium"), proj.get("status","กำลังทำ"),
                    proj.get("pct_done", 0), proj.get("note",""),
                ])
                return
            except Exception as e:
                st.warning(f"GSheets write error: {e}")

    db = _db()
    db.execute(
        "INSERT INTO projects (project,subtask,owner,due_date,priority,status,pct_done,note) VALUES (?,?,?,?,?,?,?,?)",
        (proj.get("project",""), proj.get("subtask",""), proj.get("owner",""),
         proj.get("due_date",""), proj.get("priority","Medium"),
         proj.get("status","กำลังทำ"), proj.get("pct_done",0), proj.get("note","")),
    )
    db.commit()


def update_project_status(row_id, status: str, pct: float) -> None:
    if _using_gsheets():
        ss = _spreadsheet()
        if ss:
            try:
                ws = ss.worksheet(PROJECT_SHEET)
                # สถานะ = col F (6), % เสร็จ = col G (7)
                ws.update_cell(int(row_id), 6, status)
                ws.update_cell(int(row_id), 7, pct)
                return
            except Exception as e:
                st.warning(f"GSheets update error: {e}")
    db = _db()
    db.execute("UPDATE projects SET status=?, pct_done=? WHERE rowid=?", (status, pct, int(row_id)))
    db.commit()

# ─── Page: Daily Log ──────────────────────────────────────────────────────────
def _init_timer():
    defaults = dict(running=False, start_ts=None, task="", project="", task_type=TASK_TYPES[0])
    for k, v in defaults.items():
        if f"timer_{k}" not in st.session_state:
            st.session_state[f"timer_{k}"] = v


def page_daily_log():
    _init_timer()
    today = datetime.now(BKK).date()
    st.title("📝 บันทึกงาน")
    st.caption("กดเริ่ม–จบ จับเวลาให้ หรือกรอกเวลาเอง")

    # ── Timer ────────────────────────────────────────────────────────────────
    with st.container(border=True):
        st.markdown("#### ⏱️ จับเวลา")
        col_form, col_clock = st.columns([3, 1])

        with col_form:
            t_task = st.text_input("งานที่กำลังทำ", placeholder="กำลังทำอะไรอยู่?", key="ti_task")
            c1, c2 = st.columns(2)
            with c1:
                proj_df = get_projects_df()
                proj_names = [""] + sorted(
                    proj_df["project"].dropna().unique().tolist()
                    if "project" in proj_df.columns
                    else proj_df["โปรเจ็ค"].dropna().unique().tolist()
                    if "โปรเจ็ค" in proj_df.columns else []
                )
                t_proj = st.selectbox("โปรเจ็ค", proj_names, key="ti_proj")
            with c2:
                t_type = st.selectbox("ประเภท", TASK_TYPES, key="ti_type")

        with col_clock:
            if st.session_state.timer_running:
                elapsed = int(time.time() - st.session_state.timer_start_ts)
                h, r = divmod(elapsed, 3600)
                m, s = divmod(r, 60)
                st.markdown(
                    f'<div class="timer-display c-danger">🔴 {h:02d}:{m:02d}:{s:02d}</div>',
                    unsafe_allow_html=True,
                )
                if st.button("⏹ จบงาน", use_container_width=True, type="primary"):
                    end_dt = datetime.now(BKK)
                    start_dt = datetime.fromtimestamp(st.session_state.timer_start_ts, tz=BKK)
                    hrs = (end_dt - start_dt).total_seconds() / 3600
                    add_log({
                        "log_date": today,
                        "start_time": start_dt.strftime("%H:%M"),
                        "end_time": end_dt.strftime("%H:%M"),
                        "hours": round(hrs, 2),
                        "task": t_task,
                        "project": t_proj,
                        "task_type": t_type,
                        "note": "",
                    })
                    st.session_state.timer_running = False
                    st.session_state.timer_start_ts = None
                    st.success(f"บันทึกแล้ว {round(hrs, 2)} ชม.")
                    st.rerun()
            else:
                st.markdown(
                    '<div class="timer-display" style="color:#9ca3af">⏸ 00:00:00</div>',
                    unsafe_allow_html=True,
                )
                if st.button("▶ เริ่มงาน", use_container_width=True, type="primary"):
                    if not t_task:
                        st.warning("กรุณากรอกชื่องานก่อน")
                    else:
                        st.session_state.timer_running = True
                        st.session_state.timer_start_ts = time.time()
                        st.rerun()

    # ── Manual entry ──────────────────────────────────────────────────────────
    with st.expander("✏️ กรอกเวลาเอง"):
        with st.form("manual_log_form"):
            mc1, mc2, mc3 = st.columns(3)
            with mc1:
                m_date = st.date_input("วันที่", value=today)
                m_task = st.text_input("งาน/รายละเอียด*", placeholder="ชื่องาน")
            with mc2:
                m_start = st.time_input("เวลาเริ่ม", value=None)
                proj_df2 = get_projects_df()
                pnames2 = [""] + sorted(
                    proj_df2["project"].dropna().unique().tolist()
                    if "project" in proj_df2.columns
                    else proj_df2.get("โปรเจ็ค", pd.Series()).dropna().unique().tolist()
                )
                m_proj = st.selectbox("โปรเจ็ค", pnames2, key="m_proj")
            with mc3:
                m_end = st.time_input("เวลาจบ", value=None)
                m_type = st.selectbox("ประเภท", TASK_TYPES, key="m_type")
            m_note = st.text_input("หมายเหตุ")
            if st.form_submit_button("บันทึก", type="primary"):
                if not m_task:
                    st.error("กรุณากรอกชื่องาน")
                else:
                    if m_start and m_end:
                        hrs = (datetime.combine(m_date, m_end) - datetime.combine(m_date, m_start)).total_seconds() / 3600
                    else:
                        hrs = 0.0
                    add_log({
                        "log_date": m_date,
                        "start_time": m_start.strftime("%H:%M") if m_start else "",
                        "end_time": m_end.strftime("%H:%M") if m_end else "",
                        "hours": round(hrs, 2),
                        "task": m_task,
                        "project": m_proj,
                        "task_type": m_type,
                        "note": m_note,
                    })
                    st.success("บันทึกเรียบร้อย")
                    st.rerun()

    # ── Today summary ─────────────────────────────────────────────────────────
    st.markdown(f"#### 📋 งานวันนี้ — {today.strftime('%d %b %Y')}")
    df = _norm_hours(get_log_df(start=today, end=today))

    if df.empty:
        st.info("ยังไม่มีข้อมูลวันนี้")
    else:
        h = _hcol(df)
        t = _tcol(df)
        total = df[h].sum()
        is_non_proj = df[t].isin(NON_PROJECT_TYPES) if t in df.columns else pd.Series([False] * len(df))
        adhoc_hrs = df[is_non_proj][h].sum()
        proj_hrs = total - adhoc_hrs
        adhoc_pct = (adhoc_hrs / total * 100) if total > 0 else 0
        ot = max(0, total - WORK_HRS)
        lv = _level(adhoc_pct, ot)

        c1, c2, c3, c4 = st.columns(4)
        with c1:
            _kpi("ชม. ทั้งหมด", f"{total:.1f} ชม.")
        with c2:
            _kpi("โปรเจ็ค", f"{proj_hrs:.1f} ชม.")
        with c3:
            _kpi("Adhoc / นอกแผน", f"{adhoc_pct:.0f}%", lv)
        with c4:
            ot_lv = "danger" if ot >= DANGER_OT_HRS else "warn" if ot >= WARN_OT_HRS else "normal"
            _kpi("เกินเวลา", f"+{ot:.1f} ชม.", ot_lv)

        st.divider()

        # Table
        if "hours" in df.columns:
            show = df[["start_time","end_time","hours","task","project","task_type","note"]].copy()
            show.columns = ["เริ่ม","จบ","ชม.","งาน","โปรเจ็ค","ประเภท","หมายเหตุ"]
        else:
            cols = [c for c in ["เริ่ม","จบ","ชม.","งาน","โปรเจ็ค","ประเภท","หมายเหตุ"] if c in df.columns]
            show = df[cols].copy()

        st.dataframe(show, use_container_width=True, hide_index=True)

        # Delete entry
        with st.expander("🗑️ ลบรายการ"):
            id_col = "_row" if "_row" in df.columns else "id"
            if id_col in df.columns:
                del_idx = st.number_input("ลบแถวที่ (จากตารางด้านบน)", min_value=1, max_value=len(df), step=1)
                if st.button("ลบ", type="secondary"):
                    row_id = df.iloc[del_idx - 1][id_col]
                    delete_log(row_id)
                    st.success("ลบแล้ว")
                    st.rerun()

# ─── Page: Project Plan ───────────────────────────────────────────────────────
def page_project_plan():
    st.title("📋 แผนโปรเจ็ค")
    st.caption("ใส่โปรเจ็คและงานย่อย กำหนดความสำคัญ แล้วรู้ว่าวันนี้ต้องทำอะไร")

    with st.expander("➕ เพิ่มงาน/โปรเจ็ค"):
        with st.form("add_project_form"):
            pc1, pc2 = st.columns(2)
            with pc1:
                p_proj = st.text_input("โปรเจ็ค*", placeholder="ชื่อโปรเจ็ค")
                p_sub = st.text_input("งานย่อย / milestone")
                p_owner = st.text_input("Owner")
            with pc2:
                p_due = st.date_input("Due date", value=None)
                p_prio = st.selectbox("ความสำคัญ", PRIORITY_LIST)
                p_status = st.selectbox("สถานะ", STATUS_LIST)
                p_pct = st.slider("% เสร็จ", 0, 100, 0)
            p_note = st.text_input("หมายเหตุ")
            if st.form_submit_button("เพิ่ม", type="primary"):
                if p_proj:
                    add_project({
                        "project": p_proj, "subtask": p_sub, "owner": p_owner,
                        "due_date": str(p_due) if p_due else "",
                        "priority": p_prio, "status": p_status,
                        "pct_done": p_pct, "note": p_note,
                    })
                    st.success("เพิ่มเรียบร้อย")
                    st.rerun()
                else:
                    st.error("กรุณากรอกชื่อโปรเจ็ค")

    df = get_projects_df()
    if df.empty:
        st.info("ยังไม่มีโปรเจ็ค — กดปุ่ม 'เพิ่มงาน/โปรเจ็ค' ด้านบน")
        return

    # Normalize col names
    pj_col  = "project"   if "project"  in df.columns else "โปรเจ็ค"
    sub_col = "subtask"   if "subtask"  in df.columns else "งานย่อย"
    pr_col  = "priority"  if "priority" in df.columns else "ความสำคัญ"
    st_col  = "status"    if "status"   in df.columns else "สถานะ"
    pc_col  = "pct_done"  if "pct_done" in df.columns else "% เสร็จ"
    ow_col  = "owner"     if "owner"    in df.columns else "Owner"
    due_col = "due_date"  if "due_date" in df.columns else "Due date"
    id_col  = "_row"      if "_row"     in df.columns else "id"

    # Filters
    fc1, fc2 = st.columns(2)
    with fc1:
        f_prio = st.multiselect("ความสำคัญ", PRIORITY_LIST, default=PRIORITY_LIST)
    with fc2:
        f_status = st.multiselect("สถานะ", STATUS_LIST, default=["กำลังทำ", "รอ"])

    if pr_col in df.columns and f_prio:
        df = df[df[pr_col].isin(f_prio)]
    if st_col in df.columns and f_status:
        df = df[df[st_col].isin(f_status)]

    prio_order = {"High": 0, "Medium": 1, "Low": 2}
    if pr_col in df.columns:
        df = df.sort_values(pr_col, key=lambda x: x.map(prio_order).fillna(9))

    prio_icon = {"High": "🔴", "Medium": "🟡", "Low": "🟢"}

    for _, row in df.iterrows():
        prio  = row.get(pr_col, "Medium")
        icon  = prio_icon.get(prio, "⚪")
        pname = row.get(pj_col, "")
        sub   = row.get(sub_col, "")
        sts   = row.get(st_col, "")
        pct   = float(row.get(pc_col, 0) or 0)
        due   = row.get(due_col, "")
        owner = row.get(ow_col, "")
        rid   = row.get(id_col)

        with st.container(border=True):
            bc1, bc2, bc3, bc4 = st.columns([4, 2, 1, 1])
            with bc1:
                st.markdown(f"{icon} **{pname}**")
                if sub:
                    st.caption(sub)
            with bc2:
                if due:
                    st.caption(f"📅 {due}")
                if owner:
                    st.caption(f"👤 {owner}")
            with bc3:
                new_st = st.selectbox("", STATUS_LIST, index=STATUS_LIST.index(sts) if sts in STATUS_LIST else 0, key=f"st_{rid}", label_visibility="collapsed")
            with bc4:
                new_pct = st.number_input("", 0.0, 100.0, pct, 5.0, key=f"pc_{rid}", label_visibility="collapsed", format="%.0f")

            if new_st != sts or new_pct != pct:
                update_project_status(rid, new_st, new_pct)
                st.rerun()

            st.progress(int(pct) / 100)

# ─── Page: Weekly Summary ─────────────────────────────────────────────────────
def page_weekly_summary():
    st.title("📊 สรุปรายสัปดาห์")

    mon, sun = _this_week()
    c1, c2 = st.columns(2)
    with c1:
        sel_start = st.date_input("ตั้งแต่", value=mon)
    with c2:
        sel_end = st.date_input("ถึง", value=sun)

    df = _norm_hours(get_log_df(start=sel_start, end=sel_end))
    if df.empty:
        st.info("ไม่มีข้อมูลในช่วงเวลาที่เลือก")
        return

    h = _hcol(df)
    t = _tcol(df)
    p = _pcol(df)
    d = _dcol(df)

    total  = df[h].sum()
    days   = max((sel_end - sel_start).days + 1, 1)
    exp    = sum(1 for i in range(days) if (sel_start + timedelta(i)).weekday() < 5) * WORK_HRS
    ot     = max(0.0, total - exp)

    is_np    = df[t].isin(NON_PROJECT_TYPES) if t in df.columns else pd.Series([False]*len(df))
    adhoc_h  = df[is_np][h].sum()
    proj_h   = total - adhoc_h
    adhoc_pct = (adhoc_h / total * 100) if total > 0 else 0
    lv = _level(adhoc_pct, ot)

    k1, k2, k3, k4 = st.columns(4)
    with k1:
        st.metric("ชม. ทั้งหมด", f"{total:.1f} ชม.", f"เป้า {exp:.0f} ชม.")
    with k2:
        st.metric("โปรเจ็ค", f"{proj_h:.1f} ชม.", f"{proj_h/total*100:.0f}%" if total else "")
    with k3:
        _kpi("Adhoc / นอกโปรเจ็ค", f"{adhoc_pct:.0f}%", lv)
    with k4:
        ot_lv = "danger" if ot >= DANGER_OT_HRS else "warn" if ot >= WARN_OT_HRS else "normal"
        _kpi("เกินเวลา", f"+{ot:.1f} ชม.", ot_lv)

    # Colour legend
    st.markdown("""
<div style="display:flex;gap:1.5rem;padding:0.5rem 0;font-size:0.85rem">
  <span>⚪ <b>ปกติ</b>: Adhoc &lt;50% และเกินเวลา &lt;1 ชม.</span>
  <span style="color:#f97316">🟠 <b>เยอะแล้ว</b>: Adhoc ≥50% หรือเกินเวลา ≥1 ชม.</span>
  <span style="color:#ef4444">🔴 <b>อันตราย</b>: Adhoc ≥60% หรือเกินเวลา ≥2 ชม.</span>
</div>""", unsafe_allow_html=True)

    st.divider()

    # Daily bar chart
    if d in df.columns and t in df.columns:
        daily = df.groupby([d, t])[h].sum().reset_index()
        fig = px.bar(daily, x=d, y=h, color=t, title="ชม. ทำงานแต่ละวัน (แยกประเภท)",
                     labels={h: "ชั่วโมง", d: "วันที่"},
                     color_discrete_sequence=px.colors.qualitative.Pastel)
        fig.add_hline(y=WORK_HRS, line_dash="dash", line_color="#9ca3af",
                      annotation_text=f"เป้าหมาย {WORK_HRS} ชม./วัน")
        st.plotly_chart(fig, use_container_width=True)

    col_a, col_b = st.columns(2)
    with col_a:
        if p in df.columns:
            by_p = df.groupby(p)[h].sum().sort_values(ascending=True).tail(10)
            fig2 = px.bar(by_p, orientation="h", title="ชม. แยกตามโปรเจ็ค",
                          labels={"value": "ชั่วโมง", "index": "โปรเจ็ค"},
                          color_discrete_sequence=["#4f46e5"])
            st.plotly_chart(fig2, use_container_width=True)
    with col_b:
        if t in df.columns:
            by_t = df.groupby(t)[h].sum()
            fig3 = go.Figure(go.Pie(labels=by_t.index.tolist(), values=by_t.values,
                                    hole=0.4, marker_colors=px.colors.qualitative.Pastel))
            fig3.update_layout(title="สัดส่วนตามประเภทงาน", height=350)
            st.plotly_chart(fig3, use_container_width=True)

    with st.expander("ดูข้อมูลทั้งหมด"):
        show_cols = [c for c in [d,"day_name","start_time","end_time",h,"task",p,t,"note"] if c in df.columns]
        if not show_cols:
            show_cols = [c for c in ["วันที่","วัน","เริ่ม","จบ","ชม.","งาน","โปรเจ็ค","ประเภท","หมายเหตุ"] if c in df.columns]
        st.dataframe(df[show_cols], use_container_width=True, hide_index=True)

# ─── Page: Dashboard ──────────────────────────────────────────────────────────
def page_dashboard():
    st.title("📈 แดชบอร์ด")

    c1, c2 = st.columns(2)
    with c1:
        sel_start = st.date_input("ตั้งแต่", value=date.today() - timedelta(days=30))
    with c2:
        sel_end = st.date_input("ถึง", value=date.today())

    df = _norm_hours(get_log_df(start=sel_start, end=sel_end))
    if df.empty:
        st.info("ไม่มีข้อมูลในช่วงเวลาที่เลือก")
        return

    h = _hcol(df)
    t = _tcol(df)
    p = _pcol(df)
    d = _dcol(df)

    total = df[h].sum()
    days = max((sel_end - sel_start).days + 1, 1)
    avg_day = total / days

    is_np    = df[t].isin(NON_PROJECT_TYPES) if t in df.columns else pd.Series([False]*len(df))
    adhoc_h  = df[is_np][h].sum()
    proj_h   = total - adhoc_h
    adhoc_pct = (adhoc_h / total * 100) if total else 0
    proj_pct  = 100 - adhoc_pct
    lv = _level(adhoc_pct, 0)
    p_lv = "normal" if proj_pct >= 50 else "warn" if proj_pct >= 40 else "danger"

    k1, k2, k3, k4 = st.columns(4)
    with k1:
        _kpi("ชม. ทั้งหมด", f"{total:.1f}")
    with k2:
        _kpi("เฉลี่ย/วัน", f"{avg_day:.1f} ชม.")
    with k3:
        _kpi("Adhoc %", f"{adhoc_pct:.0f}%", lv)
    with k4:
        _kpi("โปรเจ็ค %", f"{proj_pct:.0f}%", p_lv)

    st.divider()

    col_a, col_b = st.columns(2)
    with col_a:
        fig_d = go.Figure(go.Pie(
            labels=["โปรเจ็ค", "Adhoc / นอกแผน"],
            values=[proj_h, adhoc_h],
            hole=0.5,
            marker_colors=["#4f46e5", "#f97316"],
        ))
        fig_d.update_layout(title="Adhoc vs โปรเจ็ค", height=300)
        st.plotly_chart(fig_d, use_container_width=True)

    with col_b:
        if d in df.columns:
            df["_week"] = pd.to_datetime(df[d], errors="coerce").dt.to_period("W").astype(str)
            wk = df.groupby("_week")[h].sum().reset_index()
            fig_w = px.line(wk, x="_week", y=h, markers=True, title="แนวโน้มรายสัปดาห์",
                            labels={"_week": "สัปดาห์", h: "ชั่วโมง"},
                            color_discrete_sequence=["#4f46e5"])
            fig_w.update_layout(height=300)
            st.plotly_chart(fig_w, use_container_width=True)

    if p in df.columns:
        by_p = df.groupby(p)[h].sum().sort_values(ascending=False).head(10).reset_index()
        by_p.columns = ["โปรเจ็ค", "ชั่วโมง"]
        fig_p = px.bar(by_p, x="โปรเจ็ค", y="ชั่วโมง",
                       title="Top 10 โปรเจ็คตามชั่วโมง",
                       color="ชั่วโมง", color_continuous_scale="Blues")
        st.plotly_chart(fig_p, use_container_width=True)

    if d in df.columns:
        df["_dow"] = pd.to_datetime(df[d], errors="coerce").dt.day_name()
        df["_wk"]  = pd.to_datetime(df[d], errors="coerce").dt.isocalendar().week.astype(int)
        pivot = df.pivot_table(values=h, index="_dow", columns="_wk", aggfunc="sum", fill_value=0)
        order = [x for x in ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"] if x in pivot.index]
        pivot = pivot.reindex(order)
        if not pivot.empty:
            fig_hm = px.imshow(pivot, title="Heatmap ชม. ทำงาน (วัน × สัปดาห์)",
                               color_continuous_scale="Blues",
                               labels={"x": "สัปดาห์", "y": "วัน", "color": "ชม."})
            st.plotly_chart(fig_hm, use_container_width=True)

# ─── Page: Settings ───────────────────────────────────────────────────────────
def page_settings():
    st.title("⚙️ ตั้งค่า")

    with st.container(border=True):
        st.markdown("#### 🔗 เชื่อมต่อ Google Sheets")
        cur_id = os.getenv("SPREADSHEET_ID", st.session_state.get("spreadsheet_id", ""))
        if cur_id:
            st.success(f"เชื่อมต่อแล้ว: `{cur_id}`")
        else:
            st.info("ยังไม่ได้เชื่อมต่อ — ใช้ local SQLite (ข้อมูลอยู่ในเครื่อง)")

        new_id = st.text_input("Spreadsheet ID", value=cur_id,
                               placeholder="1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms")
        if st.button("บันทึก ID"):
            st.session_state.spreadsheet_id = new_id
            st.success("บันทึกแล้ว — reload หน้าเพื่อเชื่อมต่อ")
            st.rerun()

        with st.expander("📖 วิธีตั้งค่า Google Sheets"):
            st.markdown("""
**ขั้นตอน:**
1. สร้าง Google Sheet ใหม่ → คัดลอก **Spreadsheet ID** จาก URL
   `https://docs.google.com/spreadsheets/d/`**<ID>**`/edit`
2. ไปที่ [Google Cloud Console](https://console.cloud.google.com)
   → เปิด **Google Sheets API** + **Google Drive API**
3. สร้าง **Service Account** → ดาวน์โหลด JSON key
4. แชร์ Spreadsheet ให้ email ของ Service Account (Editor)
5. ตั้งค่า `.env`:
```
SPREADSHEET_ID=<your-sheet-id>
GOOGLE_CREDENTIALS_JSON=<json-content-single-line>
# หรือ
GOOGLE_CREDENTIALS_FILE=credentials.json
```
6. รีสตาร์ทแอป

> ถ้าไม่ตั้งค่า Google Sheets แอปจะใช้ SQLite เก็บในเครื่องโดยอัตโนมัติ
            """)

    with st.container(border=True):
        st.markdown("#### 🎚️ เกณฑ์สี (Color Thresholds)")
        c1, c2, c3 = st.columns(3)
        with c1:
            st.markdown("**⚪ ปกติ**")
            st.caption(f"Adhoc <{WARN_ADHOC_PCT}%  เกินเวลา <{WARN_OT_HRS} ชม.")
        with c2:
            st.markdown("**🟠 เยอะแล้ว**")
            st.caption(f"Adhoc ≥{WARN_ADHOC_PCT}%  หรือ เกินเวลา ≥{WARN_OT_HRS} ชม.")
        with c3:
            st.markdown("**🔴 อันตราย**")
            st.caption(f"Adhoc ≥{DANGER_ADHOC_PCT}%  หรือ เกินเวลา ≥{DANGER_OT_HRS} ชม.")

    with st.container(border=True):
        st.markdown("#### 💾 Export ข้อมูล Local")
        if st.button("Export CSV"):
            db = _db()
            df_export = pd.read_sql("SELECT * FROM work_log ORDER BY log_date, start_time", db)
            st.download_button(
                "⬇️ ดาวน์โหลด work_log.csv",
                data=df_export.to_csv(index=False),
                file_name=f"work_log_{date.today()}.csv",
                mime="text/csv",
            )

# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    with st.sidebar:
        st.markdown("## ⏱️ Work Time Tracker")
        st.caption("GOOGLE SHEETS TEMPLATE · สำหรับคนทำงาน")
        st.divider()

        page = st.radio(
            "nav",
            ["📝 บันทึกงาน", "📋 แผนโปรเจ็ค", "📊 สรุปรายสัปดาห์", "📈 แดชบอร์ด", "⚙️ ตั้งค่า"],
            label_visibility="collapsed",
        )

        st.divider()

        # Quick today stats
        today = datetime.now(BKK).date()
        df_t = _norm_hours(get_log_df(start=today, end=today))
        if not df_t.empty:
            hc = _hcol(df_t)
            total_t = df_t[hc].sum()
            st.metric("วันนี้", f"{total_t:.1f} ชม.")

        storage = "Google Sheets ✓" if _using_gsheets() else "Local (SQLite)"
        st.caption(f"💾 {storage}")
        st.caption(f"🕐 {datetime.now(BKK).strftime('%H:%M · %d %b %Y')}")

    if page == "📝 บันทึกงาน":
        page_daily_log()
    elif page == "📋 แผนโปรเจ็ค":
        page_project_plan()
    elif page == "📊 สรุปรายสัปดาห์":
        page_weekly_summary()
    elif page == "📈 แดชบอร์ด":
        page_dashboard()
    else:
        page_settings()


if __name__ == "__main__":
    main()
