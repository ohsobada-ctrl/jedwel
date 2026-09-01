import telebot
import json
import os
import threading
import http.server
import socketserver
import sqlite3
import subprocess
import time
import base64
import io
import re
import shutil
import requests
from datetime import datetime, timedelta

# Selenium Imports
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import Select

# pyrefly: ignore [missing-import]
import libsql_client

from telebot.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo,
    ReplyKeyboardMarkup, KeyboardButton,
    InlineQueryResultArticle, InputTextMessageContent
)
from dotenv import load_dotenv
# pyrefly: ignore [missing-import]
from cryptography.fernet import Fernet

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise ValueError("لم يتم العثور على التوكن (TOKEN)! يرجى إضافته في قائمة Environment Variables.")
ADMIN_ID = 1084115596

bot = telebot.TeleBot(TOKEN)

EXAMS_FILE = os.path.join(BASE_DIR, "webapp", "exams.json")
FACULTY_FILE = os.path.join(BASE_DIR, "webapp", "faculty.json")
KEY_FILE = os.path.join(BASE_DIR, "secret.key")

WEBAPP_URL = os.getenv("WEBAPP_URL")

# --- Turso (libSQL) Connection Settings ---
TURSO_DB_URL = os.getenv("TURSO_DB_URL")
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN")

if TURSO_DB_URL:
    TURSO_DB_URL = TURSO_DB_URL.replace("wss://", "https://").replace("libsql://", "https://")

if not TURSO_DB_URL or not TURSO_AUTH_TOKEN:
    raise ValueError("لم يتم العثور على TURSO_DB_URL أو TURSO_AUTH_TOKEN!")

file_lock = threading.Lock()
db_lock = threading.Lock()

# =========================================================================
# Turso Database Compatibility Shim
# =========================================================================

class _TursoRow:
    __slots__ = ("_cols", "_vals")
    def __init__(self, cols, vals):
        self._cols = cols
        self._vals = list(vals)
    def keys(self): return list(self._cols)
    def __getitem__(self, key):
        if isinstance(key, str): return self._vals[self._cols.index(key)]
        return self._vals[key]
    def __iter__(self): return iter(self._vals)
    def __len__(self): return len(self._vals)

class _TursoCursor:
    def __init__(self, client):
        self._client = client
        self._rows = []
        self._cols = []
        self._pos = 0
        self.lastrowid = None
        self.rowcount = -1

    def execute(self, sql, params=None):
        params = list(params) if params else []
        result = self._client.execute(sql, params)
        self._cols = list(result.columns or [])
        self._rows = [list(r) for r in result.rows]
        self._pos = 0
        self.rowcount = len(self._rows)

        if sql.strip().lower().startswith("insert"):
            try:
                lr = self._client.execute("SELECT last_insert_rowid()")
                self.lastrowid = lr.rows[0][0] if lr.rows else None
            except Exception:
                self.lastrowid = None
        return self

    def executemany(self, sql, seq_of_params):
        for params in seq_of_params:
            self.execute(sql, params)
        return self

    def fetchone(self):
        if self._pos >= len(self._rows): return None
        row = _TursoRow(self._cols, self._rows[self._pos])
        self._pos += 1
        return row

    def fetchall(self):
        rows = [_TursoRow(self._cols, r) for r in self._rows[self._pos:]]
        self._pos = len(self._rows)
        return rows

    def close(self): pass

class _TursoConnection:
    def __init__(self):
        self._client = libsql_client.create_client_sync(TURSO_DB_URL, auth_token=TURSO_AUTH_TOKEN)
        self.row_factory = None

    def cursor(self): return _TursoCursor(self._client)
    def execute(self, sql, params=None): return self.cursor().execute(sql, params)
    def commit(self): pass
    def batch(self, stmts): return self._client.batch(stmts)
    def close(self):
        try: self._client.close()
        except Exception: pass

def get_conn(): return _TursoConnection()

# --- Database Initialization ---
def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS master_data (id INTEGER PRIMARY KEY, username TEXT, password TEXT, college TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS exams (id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, name TEXT, exam_day TEXT, exam_period TEXT, day_index INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS faculty (id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, name TEXT, "group" TEXT, day TEXT, time TEXT, instructor TEXT, room TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS user_schedules (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, schedule_json TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)''')
    
    try:
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_faculty_unique ON faculty (code, \"group\", day, time)")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_exams_unique ON exams (code, exam_day, exam_period)")
        c.execute("ALTER TABLE exams ADD COLUMN day_index INTEGER DEFAULT 0")
    except Exception: pass
    conn.close()

init_db()

# --- Cryptography & Credential Helpers ---
def get_cipher():
    if not os.path.exists(KEY_FILE):
        key = Fernet.generate_key()
        with open(KEY_FILE, "wb") as f: f.write(key)
    else:
        with open(KEY_FILE, "rb") as f: key = f.read()
    return Fernet(key)

def save_master_creds(username, password, college):
    cipher = get_cipher()
    encrypted_pass = cipher.encrypt(password.encode()).decode()
    with db_lock:
        try:
            conn = get_conn()
            c = conn.cursor()
            c.execute("DELETE FROM master_data")
            c.execute("INSERT INTO master_data (username, password, college) VALUES (?, ?, ?)", (username, encrypted_pass, college))
            conn.close()
            return True
        except Exception as e:
            print(f"Error saving credentials: {e}")
            return False

def load_master_creds():
    with db_lock:
        try:
            conn = get_conn()
            c = conn.cursor()
            c.execute("SELECT username, password, college FROM master_data LIMIT 1")
            row = c.fetchone()
            conn.close()
            if row:
                username, encrypted_pass, college = row
                cipher = get_cipher()
                try: decrypted_pass = cipher.decrypt(encrypted_pass.encode()).decode()
                except: decrypted_pass = encrypted_pass
                return {"master_user": username, "master_pass": decrypted_pass, "college": college}
        except: pass
        return None

def get_db_data(table):
    if table not in ["exams", "faculty"]: return []
    with db_lock:
        try:
            conn = get_conn()
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute(f"SELECT * FROM {table}")
            rows = [dict(row) for row in c.fetchall()]
            conn.close()
            return rows
        except Exception as e:
            print(f"Error reading from {table}: {e}")
            return []

def save_schedules(new_exams, new_faculty):
    print(f"[Database Sync] بداية حفظ البيانات: {len(new_faculty)} محاضرة | {len(new_exams)} امتحان")
    with db_lock:
        try:
            conn = get_conn()
            stmts = ["DELETE FROM exams", "DELETE FROM faculty"]

            for ex in new_exams:
                stmts.append((
                    "INSERT INTO exams (code, name, exam_day, exam_period, day_index) VALUES (?, ?, ?, ?, ?)",
                    [ex.get("code"), ex.get("name"), ex.get("exam_day"), ex.get("exam_period"), ex.get("day_index", 0)]
                ))

            for f in new_faculty:
                stmts.append((
                    "INSERT INTO faculty (code, name, [group], day, time, instructor, room) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [f.get("code"), f.get("name"), f.get("group"), f.get("day"), f.get("time"), f.get("instructor"), f.get("room")]
                ))

            conn.batch(stmts)
            conn.close()
            print("[Database Sync] ✅ تم تحديث قاعدة البيانات بنجاح.")
        except Exception as e:
            print(f"[Database Sync Error] فشل تحديث قاعدة البيانات: {e}")
            return

    with file_lock:
        try:
            os.makedirs(os.path.dirname(EXAMS_FILE), exist_ok=True)
            with open(EXAMS_FILE, "w", encoding="utf-8") as f: json.dump(new_exams, f, ensure_ascii=False, indent=4)
            with open(FACULTY_FILE, "w", encoding="utf-8") as f: json.dump(new_faculty, f, ensure_ascii=False, indent=4)
            build_static_webapp(new_exams, new_faculty)
        except Exception as file_err:
            print(f"[File Error] فشل كتابة الملفات المحلية: {file_err}")

def build_static_webapp(exams, faculty):
    try:
        html_path = os.path.join(BASE_DIR, "webapp", "index.html")
        if not os.path.exists(html_path): return
        with open(html_path, "r", encoding="utf-8") as f: html = f.read()
        html = re.sub(r'<script id="injected-data">\s*window\.allCourses[\s\S]*?</script>', '', html)
        
        dates_map = {}
        try:
            conn = get_conn()
            c = conn.cursor()
            c.execute("SELECT value FROM settings WHERE key = 'exam_dates_map'")
            row = c.fetchone()
            conn.close()
            if row: dates_map = json.loads(row[0])
        except: pass
        
        data_script = f"""
        <script id="injected-data">
            window.allCourses = {json.dumps(faculty, ensure_ascii=False)};
            window.allExams = {json.dumps(exams, ensure_ascii=False)};
            window.datesMap = {json.dumps(dates_map, ensure_ascii=False)};
        </script>
        """
        target_script = '<script src="https://telegram.org/js/telegram-web-app.js"></script>'
        final_html = html.replace(target_script, target_script + '\n' + data_script) if target_script in html else html.replace('</head>', data_script + '\n</head>')

        with open(html_path, "w", encoding="utf-8") as f: f.write(final_html)
    except Exception as e:
        print(f"[Error] Error building static webapp: {e}")

def save_user_schedule_to_db(user_id, selected_courses):
    try:
        with db_lock:
            conn = get_conn()
            c = conn.cursor()
            c.execute("INSERT INTO user_schedules (user_id, schedule_json) VALUES (?, ?)", (user_id, json.dumps(selected_courses)))
            conn.close()
    except Exception as db_err:
        print(f"Error saving user schedule: {db_err}")

# =========================================================================
# Scraping Logic (Selenium)
# =========================================================================

def parse_exam_schedule(driver):
    try:
        tbody = driver.find_element(By.TAG_NAME, "tbody")
        rows = tbody.find_elements(By.TAG_NAME, "tr")
        all_days_raw = []
        periods_list = ["الفترة الاولى", "الفترة الثانية", "الفترة الثالثة", "الفترة الرابعة"]

        for row in rows:
            if "اليوم" in row.text and "الفترة" in row.text: continue
            cells = row.find_elements(By.TAG_NAME, "td")
            if len(cells) < 2: continue
            
            day_text = cells[0].text.strip()
            day_periods_data = []
            
            for i in range(1, 5):
                period_exams = []
                if i < len(cells):
                    spans = cells[i].find_elements(By.TAG_NAME, "span")
                    for span in spans:
                        text = span.text.strip()
                        if not text: continue
                        match = re.search(r"^(.*?)\s*\(\s*([\w\d]+)\s*\)$", text)
                        if match:
                            period_exams.append({"code": match.group(2).strip(), "name": match.group(1).strip()})
                day_periods_data.append(period_exams)
            all_days_raw.append({"day_text": day_text, "periods": day_periods_data})

        if not all_days_raw: return []

        is_p4_empty_everywhere = True
        for day in all_days_raw:
            if day["periods"][3]:
                is_p4_empty_everywhere = False
                break
        
        day_13_idx = -1
        for idx, day in enumerate(all_days_raw):
            if "(13)" in day["day_text"]:
                day_13_idx = idx
                break
        
        if day_13_idx != -1:
            all_after_13_empty = True
            for i in range(day_13_idx, len(all_days_raw)):
                for p in all_days_raw[i]["periods"]:
                    if p:
                        all_after_13_empty = False
                        break
                if not all_after_13_empty: break
            
            if all_after_13_empty:
                all_days_raw = all_days_raw[:day_13_idx]

        final_exams = []
        max_periods = 3 if is_p4_empty_everywhere else 4
        
        for idx, day in enumerate(all_days_raw):
            for p_idx in range(max_periods):
                p_name = periods_list[p_idx]
                exams_in_p = day["periods"][p_idx]
                
                if exams_in_p:
                    for ex in exams_in_p:
                        if not ex.get("code") or not ex.get("name") or ex["name"] in ["غير معروف", "فارغ", ""]:
                            continue
                            
                        final_exams.append({
                            "code": ex["code"].strip(),
                            "name": ex["name"].strip(),
                            "exam_day": day["day_text"].strip(),
                            "exam_period": p_name.strip(),
                            "day_index": idx + 1
                        })
        
        unique_exams = []
        seen_exams = set()
        for ex in final_exams:
            key = (ex["code"], ex["exam_day"], ex["exam_period"])
            if key not in seen_exams:
                unique_exams.append(ex)
                seen_exams.add(key)
                
        return unique_exams
    except Exception as e:
        print(f"Error parsing exams: {e}")
        return []

def parse_faculty_schedule(driver, exam_data):
    try:
        table = driver.find_element(By.TAG_NAME, "table")
        rows = table.find_elements(By.TAG_NAME, "tr")
        if not rows: return []
        
        headers = rows[0].find_elements(By.TAG_NAME, "td")
        time_slots = [h.text.strip() for h in headers[1:] if h.text.strip()]
        
        faculty_data = []
        seen_lectures = set()
        for row in rows[1:]:
            cells = row.find_elements(By.TAG_NAME, "td")
            if not cells: continue
            
            day = cells[0].text.strip()
            if not day: continue
            
            for i in range(1, len(cells)):
                cell = cells[i]
                time_range = time_slots[i-1] if (i-1) < len(time_slots) else f"الفترة {i}"
                
                try:
                    children = cell.find_elements(By.XPATH, "./*")
                    current_course = None
                    
                    for child in children:
                        tag = child.tag_name.lower()
                        if tag == "p":
                            course_text = child.text.strip()
                            course_full_name = (child.get_attribute("title") or "").strip()
                            
                            if not course_text or "غير معروف" in course_full_name: continue
                                
                            match = re.search(r"([\w\d]+)\s*\(\s*([A-Za-z0-9]+)\s*\)", course_text)
                            code = match.group(1).strip() if match else course_text
                            group = match.group(2).strip() if match else "A"
                            
                            if code == "غير معروف": continue

                            lecture_key = (code, group, day, time_range)
                            if lecture_key not in seen_lectures:
                                current_course = {
                                    "code": code,
                                    "name": course_full_name or code,
                                    "group": group,
                                    "day": day,
                                    "time": time_range,
                                    "instructor": "غير محدد",
                                    "room": "غير محدد"
                                }
                                faculty_data.append(current_course)
                                seen_lectures.add(lecture_key)
                            else: current_course = None
                                
                        elif tag == "div" and current_course:
                            div_text = child.text.strip()
                            if not div_text: continue
                            
                            parts = re.split(r'\(|قاعة|مدرج', div_text)
                            inst_match = parts[0].replace("أستاذ المقرر", "").strip()
                            if inst_match and len(inst_match) > 2:
                                current_course["instructor"] = inst_match
                            
                            try:
                                room_tag = child.find_element(By.TAG_NAME, "a")
                                room_text = (room_tag.text.strip("()") or room_tag.get_attribute("title") or "").strip()
                                if room_text: current_course["room"] = room_text
                            except:
                                room_search = re.search(r'\((.*?)\)', div_text)
                                if room_search: current_course["room"] = room_search.group(1).strip()
                                elif len(parts) > 1:
                                    room_text = div_text.replace(inst_match, "").strip("() ").replace("أستاذ المقرر", "").strip()
                                    if room_text: current_course["room"] = room_text
                except Exception as e:
                    print(f"Error in cell parsing: {e}")
                    
        return faculty_data
    except Exception as e:
        print(f"Error parsing faculty schedule: {e}")
        return []

def scrape_process(chat_id, creds):
    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")
    
    driver = None
    try:
        try:
            driver = webdriver.Chrome(options=chrome_options)
        except Exception:
            bot.send_message(
                chat_id, 
                "⚠️ **تنبيه:** خادم الاستضافة المجاني (Render) لا يحتوي على متصفح Chrome لتشغيل السحب السحابي المباشر.\n\n"
                "💡 **الحل البسيط جداً:**\n"
                "قم بتشغيل البوت من حاسوبك محلياً واضغط على **'📊 سحب الجداول الآن'** مرة واحدة عند بداية الفصل، وسيقوم البوت بسحب الجداول وتحديثها فوراً لجميع الطلاب على السيرفر!",
                parse_mode="Markdown"
            )
            return

        wait = WebDriverWait(driver, 30)
        bot.send_message(chat_id, "🌐 جاري فتح الكروم والدخول للمنظومة...")
        driver.get("https://sms.uot.edu.ly/eng/login_ing.php")
        
        fac_dropdown = wait.until(EC.element_to_be_clickable((By.ID, "fac")))
        select = Select(fac_dropdown)
        target_text = "تقنية المعلومات" if creds['college'] == 'it' else "الهندسة"
        select.select_by_visible_text(target_text)
        
        driver.find_element(By.ID, "email").send_keys(creds['master_user'])
        driver.find_element(By.ID, "login-password").send_keys(creds['master_pass'])
        driver.find_element(By.NAME, "btnlogin").click()
        
        wait.until(EC.url_contains("student"))
        bot.send_message(chat_id, "✅ تم الدخول بنجاح! جاري سحب جدول الامتحانات...")
        
        def open_schedule_menu():
            try:
                item = wait.until(EC.element_to_be_clickable((By.XPATH, "//a[contains(., 'الجداول')]")))
                driver.execute_script("arguments[0].click();", item)
            except:
                item = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "a.nav-link.nav-schedule")))
                driver.execute_script("arguments[0].click();", item)

        open_schedule_menu()
        exam_link = wait.until(EC.element_to_be_clickable((By.XPATH, "//p[contains(text(), 'جدول الامتحانات النهائية')]")))
        driver.execute_script("arguments[0].click();", exam_link)
        wait.until(EC.presence_of_element_located((By.TAG_NAME, "tbody")))
        time.sleep(2)
        exam_data = parse_exam_schedule(driver)
        bot.send_message(chat_id, f"📝 تم سحب {len(exam_data)} مادة من جدول الامتحانات.")
        
        open_schedule_menu()
        faculty_link = wait.until(EC.element_to_be_clickable((By.XPATH, "//p[contains(text(), 'جدول الكلية')]")))
        driver.execute_script("arguments[0].click();", faculty_link)
        wait.until(EC.presence_of_element_located((By.TAG_NAME, "table")))
        time.sleep(2)
        
        bot.send_message(chat_id, "🏫 جاري سحب جدول الكلية وتنسيق البيانات...")
        faculty_data = parse_faculty_schedule(driver, exam_data)
        
        save_schedules(exam_data, faculty_data)
        
        status_msg = (
            "✅ اكتملت العملية بنجاح!\n\n"
            f"📝 تم تحديث {len(exam_data)} مادة في جدول الامتحانات\n"
            f"🏫 تم تحديث {len(faculty_data)} محاضرة في جدول الكلية\n\n"
            "📂 الجداول الآن محدثة وجاهزة."
        )
        bot.send_message(chat_id, status_msg)
        
    except Exception as e:
        bot.send_message(chat_id, f"❌ حدث خطأ أثناء السحب: {str(e)}")
    finally:
        time.sleep(5)
        if driver: driver.quit()

# =========================================================================
# Telegram Handlers & Unified HTTP Web Server
# =========================================================================

@bot.message_handler(commands=['start', 'help'])
def start(message):
    user_id = message.from_user.id
    target_url = WEBAPP_URL if WEBAPP_URL else "https://jedwel.onrender.com"
    user_url = f"{target_url}/?uid={message.chat.id}"
    
    reply_markup = ReplyKeyboardMarkup(resize_keyboard=True)
    web_app_btn = KeyboardButton("🎓 صانع الجداول الذكي (Mini App)", web_app=WebAppInfo(url=user_url))
    reply_markup.add(web_app_btn)
    
    if user_id == ADMIN_ID:
        markup = InlineKeyboardMarkup()
        creds = load_master_creds()
        if creds:
             markup.add(InlineKeyboardButton("✅ تحديث حساب الماستر", callback_data="setup_master"))
             markup.add(InlineKeyboardButton("📊 سحب الجداول الآن", callback_data="scrape_schedule"))
             markup.add(InlineKeyboardButton("🛠️ إدارة البيانات يدوياً", callback_data="admin_manage_data"))
        else:
             markup.add(InlineKeyboardButton("🔑 إعداد حساب الماستر", callback_data="setup_master"))
        
        bot.send_message(message.chat.id, "👋 أهلاً بك يا أدمن في نظام الجدولة الذكي!", reply_markup=markup)
        bot.send_message(message.chat.id, "💡 اضغط على زر **Mini App** بالأسفل لفتح الواجهة الذكية.", reply_markup=reply_markup, parse_mode="Markdown")
    else:
        welcome_text = "👋 **أهلاً بك في نظام الجدولة الذكي!**\n\n🚀 اضغط على زر **Mini App** بالأسفل لفتح الواجهة."
        bot.send_message(message.chat.id, welcome_text, reply_markup=reply_markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data == "scrape_schedule")
def handle_scrape(call):
    if call.from_user.id != ADMIN_ID: return
    creds = load_master_creds()
    if not creds:
        bot.send_message(call.message.chat.id, "❌ يرجى إعداد بيانات الماستر أولاً.")
        return
    bot.answer_callback_query(call.id, "⏳ بدأت العملية...")
    threading.Thread(target=scrape_process, args=(call.message.chat.id, creds)).start()

@bot.callback_query_handler(func=lambda call: call.data == "setup_master")
def setup_master(call):
    if call.from_user.id != ADMIN_ID: return
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("💻 تقنية المعلومات", callback_data="master_college_it"))
    markup.add(InlineKeyboardButton("🛠️ الهندسة", callback_data="master_college_eng"))
    bot.edit_message_text("🏫 اختر الكلية للحساب الماستر:", call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("master_college_"))
def set_master_college(call):
    if call.from_user.id != ADMIN_ID: return
    college = call.data.split("_")[-1]
    msg = bot.send_message(call.message.chat.id, f"👤 أرسل رقم القيد للكلية المختارة ({'تقنية المعلومات' if college=='it' else 'الهندسة'}):")
    bot.register_next_step_handler(msg, get_master_user, college)

def get_master_user(message, college):
    if message.from_user.id != ADMIN_ID: return
    username = message.text.strip()
    msg = bot.send_message(message.chat.id, "🔐 توا أرسل الباسورد (Password):")
    bot.register_next_step_handler(msg, get_master_pass, username, college)

def get_master_pass(message, username, college):
    if message.from_user.id != ADMIN_ID: return
    password = message.text.strip()
    if save_master_creds(username, password, college):
        bot.send_message(message.chat.id, "✅ تم حفظ بيانات الماستر بنجاح!")
    else:
        bot.send_message(message.chat.id, "❌ حدث خطأ أثناء حفظ البيانات.")

@bot.message_handler(content_types=['web_app_data'])
def handle_web_app_data(message):
    try:
        user_id = message.from_user.id
        raw_data = json.loads(message.web_app_data.data)

        if isinstance(raw_data, dict) and raw_data.get('type') == 'schedule_image':
            image_data = raw_data.get('image', '')
            if ',' in image_data: image_data = image_data.split(',', 1)[1]
            image_file = io.BytesIO(base64.b64decode(image_data))
            image_file.name = 'schedule.png'
            bot.send_photo(message.chat.id, image_file, caption="📅 جدول المحاضرات المختار")
            return

        selected_courses = raw_data if isinstance(raw_data, list) else []
        if selected_courses:
            save_user_schedule_to_db(user_id, selected_courses)
            bot.send_message(message.chat.id, "✅ تم حفظ جدولك بنجاح!")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ حدث خطأ في معالجة الجدول: {str(e)}")

# Unified HTTP Handler
class UnifiedHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args): pass

    def do_GET(self):
        req_path = self.path.split('?')[0]
        if req_path == '/api/faculty':
            self.send_json_response(get_db_data("faculty"))
        elif req_path == '/api/exams':
            self.send_json_response(get_db_data("exams"))
        else:
            webapp_dir = os.path.join(BASE_DIR, "webapp")
            file_path = os.path.join(webapp_dir, "index.html" if req_path in ["/", ""] else req_path.lstrip('/'))
            if os.path.exists(file_path) and os.path.isfile(file_path):
                self.send_response(200)
                self.end_headers()
                with open(file_path, "rb") as f: self.wfile.write(f.read())
            else:
                self.send_error(404, "File Not Found")

    def do_POST(self):
        if self.path.split('?')[0] == '/api/save_schedule':
            content_length = int(self.headers.get('Content-Length', 0))
            data = json.loads(self.rfile.read(content_length).decode('utf-8'))
            if data.get("user_id") and data.get("selected_courses"):
                save_user_schedule_to_db(data["user_id"], data["selected_courses"])
                self.send_json_response({"status": "success"})
            else: self.send_error(400, "Invalid Parameters")
        else: self.send_error(404)

    def send_json_response(self, data):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))

def run_http_server():
    port = int(os.getenv("PORT", 8080))
    server = socketserver.TCPServer(("", port), UnifiedHandler)
    print(f"[Web Server] HTTP server started on port {port}")
    server.serve_forever()

if __name__ == "__main__":
    exams_init = get_db_data("exams")
    faculty_init = get_db_data("faculty")
    build_static_webapp(exams_init, faculty_init)

    threading.Thread(target=run_http_server, daemon=True).start()

    print("[Bot] Removing webhooks and starting bot polling...")
    try:
        bot.remove_webhook()
    except Exception:
        pass
        
    bot.infinity_polling(skip_pending=True, timeout=60, long_polling_timeout=60)
