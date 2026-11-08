"""
setup_gsheets.py — ตั้งค่า Google Sheets template สำหรับ Work Time Tracker

วิธีใช้:
  python setup_gsheets.py

ต้องมีไฟล์ credentials.json หรือ GOOGLE_CREDENTIALS_JSON ใน .env
และ SPREADSHEET_ID ใน .env (หรือใส่เป็น argument)
"""
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ─── สี / style constants ──────────────────────────────────────────────────────
COLOR_HEADER_LOG = {"red": 0.235, "green": 0.329, "blue": 0.608}   # navy
COLOR_HEADER_PROJ = {"red": 0.192, "green": 0.49, "blue": 0.302}   # dark green
COLOR_WHITE = {"red": 1, "green": 1, "blue": 1}
COLOR_LIGHT_BLUE = {"red": 0.898, "green": 0.922, "blue": 0.996}
COLOR_LIGHT_GREEN = {"red": 0.898, "green": 0.98, "blue": 0.898}


def load_credentials():
    creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON", "")
    if not creds_json:
        cf = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
        if Path(cf).exists():
            creds_json = Path(cf).read_text()
    if not creds_json:
        print("❌ ไม่พบ credentials")
        print("   ตั้งค่า GOOGLE_CREDENTIALS_JSON ใน .env หรือวางไฟล์ credentials.json")
        sys.exit(1)
    return json.loads(creds_json)


def get_client():
    import gspread
    from google.oauth2.service_account import Credentials

    creds_data = load_credentials()
    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/spreadsheets",
    ]
    creds = Credentials.from_service_account_info(creds_data, scopes=scopes)
    return gspread.authorize(creds), creds_data.get("client_email", "")


def cell_format(bold=False, bg_color=None, text_color=None, font_size=10,
                h_align="LEFT", wrap=False):
    fmt = {
        "textFormat": {
            "bold": bold,
            "fontSize": font_size,
        },
        "horizontalAlignment": h_align,
        "wrapStrategy": "WRAP" if wrap else "OVERFLOW_CELL",
    }
    if bg_color:
        fmt["backgroundColor"] = bg_color
    if text_color:
        fmt["textFormat"]["foregroundColor"] = text_color
    return fmt


def setup_log_sheet(ss, existing_titles):
    """สร้างและตั้งค่า sheet บันทึกงาน"""
    title = "บันทึกงาน"
    print(f"\n📝 ตั้งค่า sheet '{title}'...")

    if title not in existing_titles:
        ws = ss.add_worksheet(title=title, rows=1000, cols=10)
        print(f"   สร้าง sheet ใหม่")
    else:
        ws = ss.worksheet(title)
        print(f"   sheet มีอยู่แล้ว — ข้ามการสร้าง header ถ้ามีข้อมูลแล้ว")
        # ถ้ามีข้อมูลแล้ว ไม่ต้อง init
        if ws.row_count > 1 and ws.cell(2, 1).value:
            print(f"   พบข้อมูลอยู่แล้ว — ข้าม")
            return ws

    # Headers
    headers = ["วันที่", "วัน", "เริ่ม", "จบ", "ชม.", "งาน / รายละเอียด",
               "โปรเจ็ค", "ประเภท", "หมายเหตุ"]
    ws.update("A1:I1", [headers])

    # Format header row
    ws.format("A1:I1", cell_format(
        bold=True, bg_color=COLOR_HEADER_LOG,
        text_color=COLOR_WHITE, font_size=11, h_align="CENTER"
    ))

    # Column widths (approximate via column metadata)
    requests = [
        {"updateDimensionProperties": {
            "range": {"sheetId": ws.id, "dimension": "COLUMNS",
                      "startIndex": i, "endIndex": i + 1},
            "properties": {"pixelSize": w},
            "fields": "pixelSize"
        }}
        for i, w in enumerate([90, 40, 60, 60, 50, 280, 150, 110, 150])
    ]

    # Freeze header row
    requests.append({"updateSheetProperties": {
        "properties": {"sheetId": ws.id, "gridProperties": {"frozenRowCount": 1}},
        "fields": "gridProperties.frozenRowCount"
    }})

    # Alternating row color (rows 2–100)
    for row in range(2, 101):
        bg = COLOR_LIGHT_BLUE if row % 2 == 0 else COLOR_WHITE
        requests.append({"repeatCell": {
            "range": {"sheetId": ws.id, "startRowIndex": row - 1,
                      "endRowIndex": row, "startColumnIndex": 0, "endColumnIndex": 9},
            "cell": {"userEnteredFormat": {"backgroundColor": bg}},
            "fields": "userEnteredFormat.backgroundColor"
        }})

    ss.batch_update({"requests": requests})

    # Sample data
    today = date.today()
    mon = today - timedelta(days=today.weekday())
    day_th = {0: "จ", 1: "อ", 2: "พ", 3: "พฤ", 4: "ศ", 5: "ส", 6: "อา"}

    samples = []
    sample_tasks = [
        ("09:00", "10:30", 1.5, "ประชุม Stand-up + วางแผนงาน", "Launch BuildFine", "ประชุม"),
        ("10:30", "12:00", 1.5, "พัฒนา Feature ใหม่ — ระบบ Login", "Launch BuildFine", "โปรเจ็ค"),
        ("13:00", "14:00", 1.0, "ตอบ chat ลูกค้า / แก้ปัญหาด่วน", "", "Adhoc"),
        ("14:00", "17:00", 3.0, "พัฒนา Dashboard หน้าหลัก", "Launch BuildFine", "โปรเจ็ค"),
        ("17:00", "18:00", 1.0, "สรุปงานและเขียน report", "Launch BuildFine", "โปรเจ็ค"),
    ]

    for i, (st, en, hrs, task, proj, typ) in enumerate(sample_tasks):
        d = mon + timedelta(days=min(i, 4))
        samples.append([
            str(d), day_th[d.weekday()], st, en, hrs, task, proj, typ, ""
        ])

    if samples:
        ws.update(f"A2:I{len(samples)+1}", samples)
        print(f"   เพิ่มข้อมูลตัวอย่าง {len(samples)} แถว")

    # Number format for column E (ชม.)
    ss.batch_update({"requests": [{
        "repeatCell": {
            "range": {"sheetId": ws.id, "startRowIndex": 1, "endRowIndex": 1000,
                      "startColumnIndex": 4, "endColumnIndex": 5},
            "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": "0.00"}}},
            "fields": "userEnteredFormat.numberFormat"
        }
    }]})

    print(f"   ✅ เสร็จสิ้น")
    return ws


def setup_project_sheet(ss, existing_titles):
    """สร้างและตั้งค่า sheet โปรเจ็ค"""
    title = "โปรเจ็ค"
    print(f"\n📋 ตั้งค่า sheet '{title}'...")

    if title not in existing_titles:
        ws = ss.add_worksheet(title=title, rows=200, cols=9)
        print(f"   สร้าง sheet ใหม่")
    else:
        ws = ss.worksheet(title)
        if ws.row_count > 1 and ws.cell(2, 1).value:
            print(f"   พบข้อมูลอยู่แล้ว — ข้าม")
            return ws

    headers = ["โปรเจ็ค", "งานย่อย / milestone", "Owner",
               "Due date", "ความสำคัญ", "สถานะ", "% เสร็จ", "หมายเหตุ"]
    ws.update("A1:H1", [headers])

    ws.format("A1:H1", cell_format(
        bold=True, bg_color=COLOR_HEADER_PROJ,
        text_color=COLOR_WHITE, font_size=11, h_align="CENTER"
    ))

    requests = [
        {"updateDimensionProperties": {
            "range": {"sheetId": ws.id, "dimension": "COLUMNS",
                      "startIndex": i, "endIndex": i + 1},
            "properties": {"pixelSize": w},
            "fields": "pixelSize"
        }}
        for i, w in enumerate([180, 220, 100, 100, 90, 100, 70, 200])
    ]
    requests.append({"updateSheetProperties": {
        "properties": {"sheetId": ws.id, "gridProperties": {"frozenRowCount": 1}},
        "fields": "gridProperties.frozenRowCount"
    }})

    ss.batch_update({"requests": requests})

    today = date.today()
    samples = [
        ["Launch BuildFine", "ระบบ User Login + Auth", "นนท์", str(today + timedelta(days=14)),
         "High", "กำลังทำ", 40, "OAuth2 + JWT"],
        ["Launch BuildFine", "Dashboard หน้าหลัก", "นนท์", str(today + timedelta(days=21)),
         "High", "กำลังทำ", 25, ""],
        ["Launch BuildFine", "ระบบ Payment", "แนน", str(today + timedelta(days=30)),
         "High", "รอ", 0, "รอ design จาก UX"],
        ["แผน Q1 2027", "วางแผน roadmap Q1", "นนท์", str(today + timedelta(days=7)),
         "Medium", "กำลังทำ", 60, ""],
        ["แผน Q1 2027", "ประชุม Stakeholder", "นนท์", str(today + timedelta(days=5)),
         "Medium", "รอ", 0, "นัด 15 Nov"],
        ["งาน Internal", "อัปเดต Documentation", "แนน", str(today + timedelta(days=45)),
         "Low", "รอ", 0, ""],
    ]

    ws.update(f"A2:H{len(samples)+1}", samples)
    print(f"   เพิ่มข้อมูลตัวอย่าง {len(samples)} แถว")

    # Color-code priority column (E)
    priority_colors = {
        "High":   {"red": 1.0, "green": 0.9, "blue": 0.9},
        "Medium": {"red": 1.0, "green": 0.98, "blue": 0.88},
        "Low":    {"red": 0.9, "green": 1.0, "blue": 0.9},
    }
    color_requests = []
    for row_i, row in enumerate(samples, start=2):
        prio = row[4]
        bg = priority_colors.get(prio, COLOR_WHITE)
        color_requests.append({"repeatCell": {
            "range": {"sheetId": ws.id,
                      "startRowIndex": row_i - 1, "endRowIndex": row_i,
                      "startColumnIndex": 0, "endColumnIndex": 8},
            "cell": {"userEnteredFormat": {"backgroundColor": bg}},
            "fields": "userEnteredFormat.backgroundColor"
        }})
    if color_requests:
        ss.batch_update({"requests": color_requests})

    print(f"   ✅ เสร็จสิ้น")
    return ws


def setup_summary_sheet(ss, log_ws, proj_ws, existing_titles):
    """สร้าง sheet สรุปรายสัปดาห์ด้วย formula"""
    title = "สรุปรายสัปดาห์"
    print(f"\n📊 ตั้งค่า sheet '{title}'...")

    if title not in existing_titles:
        ws = ss.add_worksheet(title=title, rows=50, cols=8)
    else:
        ws = ss.worksheet(title)

    log = f"'{log_ws.title}'!A:I"

    ws.update("A1:B1", [["สรุปรายสัปดาห์", ""]])
    ws.format("A1", cell_format(bold=True, font_size=14))

    ws.update("A3:B3", [["ชม. ทั้งหมด (สัปดาห์นี้)", ""]])
    ws.update("A4:B4", [["ชม. โปรเจ็ค", ""]])
    ws.update("A5:B5", [["ชม. Adhoc / นอกแผน", ""]])
    ws.update("A6:B6", [["% Adhoc", ""]])
    ws.update("A7:B7", [["ชม. เกินเวลา (เกิน 40 ชม.)", ""]])

    # Formulas using SUMIF on column H (ประเภท)
    # สัปดาห์นี้ = วันจันทร์ถึงวันอาทิตย์
    ws.update("B3", [["=SUMPRODUCT((WEEKNUM(INDIRECT(\"'บันทึกงาน'!A2:A1000\"),2)=WEEKNUM(TODAY(),2))*(YEAR(INDIRECT(\"'บันทึกงาน'!A2:A1000\"))=YEAR(TODAY()))*ISNUMBER(INDIRECT(\"'บันทึกงาน'!A2:A1000\"))*INDIRECT(\"'บันทึกงาน'!E2:E1000\"))"]])
    ws.update("B4", [['=SUMPRODUCT((WEEKNUM(INDIRECT("\'บันทึกงาน\'!A2:A1000"),2)=WEEKNUM(TODAY(),2))*(YEAR(INDIRECT("\'บันทึกงาน\'!A2:A1000\"))=YEAR(TODAY()))*(INDIRECT("\'บันทึกงาน\'!H2:H1000")="โปรเจ็ค")*INDIRECT("\'บันทึกงาน\'!E2:E1000")']])
    ws.update("B5", [["=B3-B4"]])
    ws.update("B6", [['=IF(B3>0,B5/B3,0)']])
    ws.update("B7", [["=MAX(0,B3-40)"]])

    ws.format("B3:B5,B7", {"numberFormat": {"type": "NUMBER", "pattern": "0.00"}})
    ws.format("B6", {"numberFormat": {"type": "PERCENT", "pattern": "0.0%"}})
    ws.format("A3:A7", cell_format(bold=True))

    print(f"   ✅ เสร็จสิ้น")
    return ws


def print_banner(spreadsheet_id, service_email):
    url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"
    print("\n" + "="*60)
    print("✅  ตั้งค่า Google Sheets เสร็จสิ้น!")
    print("="*60)
    print(f"\n🔗 เปิด Spreadsheet:")
    print(f"   {url}")
    print(f"\n📧 Service Account email (แชร์ให้ด้วย):")
    print(f"   {service_email}")
    print(f"\n🚀 รันแอป:")
    print(f"   streamlit run work_tracker.py")
    print("\n" + "="*60)


def main():
    spreadsheet_id = os.getenv("SPREADSHEET_ID", "")
    if len(sys.argv) > 1:
        spreadsheet_id = sys.argv[1]

    if not spreadsheet_id:
        print("❌ ไม่พบ SPREADSHEET_ID")
        print("   ใส่ใน .env หรือส่งเป็น argument:")
        print("   python setup_gsheets.py <spreadsheet_id>")
        sys.exit(1)

    print("🔑 กำลังโหลด credentials...")
    gc, service_email = get_client()

    print(f"📄 กำลังเปิด Spreadsheet: {spreadsheet_id}")
    try:
        ss = gc.open_by_key(spreadsheet_id)
        print(f"   ชื่อ: '{ss.title}'")
    except Exception as e:
        print(f"❌ เปิดไม่ได้: {e}")
        print(f"   ตรวจสอบว่าได้แชร์ sheet ให้ {service_email} (Editor) แล้ว")
        sys.exit(1)

    existing_titles = {ws.title for ws in ss.worksheets()}
    print(f"   sheets ที่มีอยู่: {existing_titles}")

    log_ws = setup_log_sheet(ss, existing_titles)
    proj_ws = setup_project_sheet(ss, existing_titles)
    setup_summary_sheet(ss, log_ws, proj_ws, existing_titles)

    print_banner(spreadsheet_id, service_email)


if __name__ == "__main__":
    main()
