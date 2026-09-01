import telebot
import json
import os
import threading
import http.server
import socketserver
import sqlite3  # يبقى مستوردًا لأجل sqlite3.Row
import subprocess
import time
import base64
import io
import re
import shutil
import requests
from datetime import datetime, timedelta

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
    raise ValueError("لم يتم العثور على التوكن (TOKEN)! يرجى إضافته في قائمة Environment Variables على Render.")
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
    # تحويل الرابط تلقائياً ليعمل عبر HTTP/REST الهادئ بدون مشاكل الـ WebSockets
    TURSO_DB_URL = TURSO_DB_URL.replace("wss://", "https://").replace("libsql://", "https://")

if not TURSO_DB_URL or not TURSO_AUTH_TOKEN:
    raise ValueError(
        "لم يتم العثور على TURSO_DB_URL أو TURSO_AUTH_TOKEN! "
        "يرجى إضافتهم في Environment Variables على Render (شوف لوحة Turso تبويب Connect)."
    )

file_lock = threading.Lock()
db_lock = threading.Lock()

# =========================================================================
# طبقة توافق (Shim) تحاكي واجهة sqlite3 لكن تتصل فعليًا بقاعدة Turso البعيدة
# =========================================================================

class _TursoRow:
    """يحاكي سلوك sqlite3.Row: يدعم row[0] ، row['col'] ، dict(row) ، وفك التغليف (a, b = row)."""
    __slots__ = ("_cols", "_vals")

    def __init__(self, cols, vals):
        self._cols = cols
        self._vals = list(vals)

    def keys(self):
        return list(self._cols)

    def __getitem__(self, key):
        if isinstance(key, str):
            return self._vals[self._cols.index(key)]
        return self._vals[key]

    def __iter__(self):
        return iter(self._vals)

    def __len__(self):
        return len(self._vals)

    def __repr__(self):
        return f"<TursoRow {dict(zip(self._cols, self._vals))}>"


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
        if self._pos >= len(self._rows):
            return None
        row = _TursoRow(self._cols, self._rows[self._pos])
        self._pos += 1
        return row

    def fetchall(self):
        rows = [_TursoRow(self._cols, r) for r in self._rows[self._pos:]]
        self._pos = len(self._rows)
        return rows

    def close(self):
        pass


class _TursoConnection:
    """يحاكي sqlite3.Connection."""

    def __init__(self):
        self._client = libsql_client.create_client_sync(TURSO_DB_URL, auth_token=TURSO_AUTH_TOKEN)
        self.row_factory = None

    def cursor(self):
        return _TursoCursor(self._client)

    def execute(self, sql, params=None):
        return self.cursor().execute(sql, params)

    def commit(self):
        pass

    def batch(self, stmts):
        """تنفيذ مجموعة استعلامات دفعة واحدة في طلب واحد إلى Turso"""
        return self._client.batch(stmts)

    def close(self):
        try:
            self._client.close()
        except Exception:
            pass


def get_conn():
    """بديل مباشر لـ sqlite3.connect(DB_FILE)"""
    return _TursoConnection()


# --- Database Initialization ---
def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS master_data 
                 (id INTEGER PRIMARY KEY, username TEXT, password TEXT, college TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS exams 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, name TEXT, exam_day TEXT, exam_period TEXT, day_index INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS faculty 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, name TEXT, "group" TEXT, day TEXT, time TEXT, instructor TEXT, room TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS user_schedules 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, schedule_json TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)''')
    
    # قيود منع التكرار على مستوى الحقول المفتاحية
    try:
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_faculty_unique ON faculty (code, \"group\", day, time)")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_exams_unique ON exams (code, exam_day, exam_period)")
    except Exception:
        pass

    try:
        c.execute("ALTER TABLE exams ADD COLUMN day_index INTEGER DEFAULT 0")
    except Exception:
        pass
        
    conn.close()

init_db()

# --- Cryptography Setup ---
def get_cipher():
    if not os.path.exists(KEY_FILE):
        key = Fernet.generate_key()
        with open(KEY_FILE, "wb") as f:
            f.write(key)
    else:
        with open(KEY_FILE, "rb") as f:
            key = f.read()
    return Fernet(key)

# --- Database & Credentials Helpers ---
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
                try:
                    decrypted_pass = cipher.decrypt(encrypted_pass.encode()).decode()
                except:
                    decrypted_pass = encrypted_pass
                return {"master_user": username, "master_pass": decrypted_pass, "college": college}
        except:
            pass
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
    """
    يقوم بحذف الجدول القديم بالكامل وإعادة إدخال التحديثات دفعة واحدة (Batch Execution)
    لتجنب التكرار وضمان السرعة وعدم استهلاك الاتصالات.
    """
    print(f"[Database Sync] بداية حفظ البيانات: {len(new_faculty)} محاضرة | {len(new_exams)} امتحان")
    with db_lock:
        try:
            conn = get_conn()
            stmts = []

            # 1. مسح الترتيب السابق كاملاً لتفادي التكرار نهائياً
            stmts.append("DELETE FROM exams")
            stmts.append("DELETE FROM faculty")

            # 2. بناء استعلامات الإدخال الجديدة لجدول الامتحانات
            for ex in new_exams:
                code = ex.get("code")
                name = ex.get("name")
                exam_day = ex.get("exam_day")
                exam_period = ex.get("exam_period")
                day_index = ex.get("day_index", 0)
                stmts.append((
                    "INSERT INTO exams (code, name, exam_day, exam_period, day_index) VALUES (?, ?, ?, ?, ?)",
                    [code, name, exam_day, exam_period, day_index]
                ))

            # 3. بناء استعلامات الإدخال الجديدة لجدول المحاضرات
            for f in new_faculty:
                code = f.get("code")
                name = f.get("name")
                group_val = f.get("group")
                day = f.get("day")
                time_val = f.get("time")
                instructor = f.get("instructor")
                room = f.get("room")
                stmts.append((
                    "INSERT INTO faculty (code, name, [group], day, time, instructor, room) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [code, name, group_val, day, time_val, instructor, room]
                ))

            # 4. التنفيذ دفعة واحدة على Turso
            conn.batch(stmts)
            conn.close()
            print("[Database Sync] ✅ تم إحلال وتحديث كامل البيانات بنجاح بدون تكرار.")

        except Exception as e:
            print(f"[Database Sync Error] فشل تحديث قاعدة البيانات: {e}")
            return

    with file_lock:
        try:
            os.makedirs(os.path.dirname(EXAMS_FILE), exist_ok=True)
            with open(EXAMS_FILE, "w", encoding="utf-8") as f:
                json.dump(new_exams, f, ensure_ascii=False, indent=4)
            with open(FACULTY_FILE, "w", encoding="utf-8") as f:
                json.dump(new_faculty, f, ensure_ascii=False, indent=4)
            build_static_webapp(new_exams, new_faculty)
        except Exception as file_err:
            print(f"[File Error] فشل كتابة ملفات JSON المحلية: {file_err}")

def build_static_webapp(exams, faculty):
    try:
        template_path = os.path.join(BASE_DIR, "webapp", "index.html")
        output_path = os.path.join(BASE_DIR, "webapp", "index_final.html")
        if not os.path.exists(template_path): return
        
        with open(template_path, "r", encoding="utf-8") as f:
            html = f.read()
        
        dates_map = {}
        try:
            conn = get_conn()
            c = conn.cursor()
            c.execute("SELECT value FROM settings WHERE key = 'exam_dates_map'")
            row = c.fetchone()
            conn.close()
            if row:
                dates_map = json.loads(row[0])
        except:
            pass
        
        data_script = f"""
        <script>
            window.allCourses = {json.dumps(faculty, ensure_ascii=False)};
            window.allExams = {json.dumps(exams, ensure_ascii=False)};
            window.datesMap = {json.dumps(dates_map, ensure_ascii=False)};
            console.log("Data Injected Successfully! Courses:", window.allCourses.length, "Exams:", window.allExams.length);
        </script>
        """
        target_script = '<script src="https://telegram.org/js/telegram-web-app.js"></script>'
        if target_script in html:
            final_html = html.replace(target_script, data_script + '\n' + target_script)
        else:
            final_html = html.replace('</head>', data_script + '\n</head>')

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(final_html)
        print("[Info] Created index_final.html with embedded data.")
    except Exception as e:
        print(f"[Error] Error building static webapp: {e}")

def save_user_schedule_to_db(user_id, selected_courses):
    try:
        with db_lock:
            conn = get_conn()
            c = conn.cursor()
            c.execute("INSERT INTO user_schedules (user_id, schedule_json) VALUES (?, ?)",
                      (user_id, json.dumps(selected_courses)))
            conn.close()
    except Exception as db_err:
        print(f"Error saving user schedule: {db_err}")

# --- Database Export Helper ---
def export_full_backup_json():
    conn = get_conn()
    c = conn.cursor()
    dump = {}
    for table in ["master_data", "exams", "faculty", "user_schedules", "settings"]:
        c.execute(f"SELECT * FROM {table}")
        dump[table] = [dict(row) for row in c.fetchall()]
    conn.close()

    backup_path = os.path.join(BASE_DIR, f"jedwel_backup_{int(time.time())}.json")
    with open(backup_path, "w", encoding="utf-8") as f:
        json.dump(dump, f, ensure_ascii=False, indent=2)
    return backup_path


# --- Automatic Database Backup Background Thread ---
def auto_backup_loop():
    while True:
        try:
            time.sleep(86400) # كل 24 ساعة
            backup_path = export_full_backup_json()

            if os.path.exists(backup_path):
                with open(backup_path, "rb") as doc:
                    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
                    caption = f"📦 **نسخة احتياطية تلقائية من قاعدة بيانات Turso**\n📅 التوقيت: `{timestamp}`"
                    bot.send_document(ADMIN_ID, doc, caption=caption, parse_mode="Markdown")
                os.remove(backup_path)
                print(f"[Backup] Automatic backup sent to admin at {timestamp}")
        except Exception as e:
            print(f"[Backup Error] Failed automatic backup: {e}")

# --- Telegram Command Handlers ---
@bot.message_handler(commands=['start', 'help'])
def start(message):
    user_id = message.from_user.id
    target_url = WEBAPP_URL if WEBAPP_URL else "https://trycloudflare.com"
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
             markup.add(InlineKeyboardButton("📦 أخذ نسخة احتياطية الآن", callback_data="admin_manual_backup"))
        else:
             markup.add(InlineKeyboardButton("🔑 إعداد حساب الماستر", callback_data="setup_master"))
        
        bot.send_message(message.chat.id, "👋 أهلاً بك يا أدمن في نظام الجدولة الذكي!\n\nهنا نقدر نسحب الجداول ونصمم جداول بدون تعارض.", reply_markup=markup)
        
        notice = (
            "💡 **كيف تصمم جدولك؟**\n\n"
            "اضغط على زر **Mini App** بالأسفل لفتح الواجهة الذكية واختيار موادك بدون تعارضات."
        )
        bot.send_message(message.chat.id, notice, reply_markup=reply_markup, parse_mode="Markdown")
    else:
        welcome_text = (
            "👋 **أهلاً بك في نظام الجدولة الذكي!**\n\n"
            "🚀 اضغط على زر **Mini App** بالأسفل لفتح واجهة تصميم جدولك الدراسي وتفادي التعارضات تلقائياً بكبسة زر."
        )
        bot.send_message(message.chat.id, welcome_text, reply_markup=reply_markup, parse_mode="Markdown")

# --- Telegram Inline Query Search ---
@bot.inline_handler(func=lambda query: len(query.query.strip()) > 0)
def inline_search_courses(inline_query):
    query_text = inline_query.query.strip().lower()
    
    faculty_data = get_db_data("faculty")
    exams_data = get_db_data("exams")
    
    matches = [f for f in faculty_data if query_text in f.get('code','').lower() or query_text in f.get('name','').lower()]
    
    results = []
    seen_codes = set()
    
    for m in matches:
        code = m.get('code')
        if code in seen_codes: continue
        seen_codes.add(code)
        
        name = m.get('name', code)
        lecs = [l for l in faculty_data if l.get('code') == code]
        
        ex = next((e for e in exams_data if e.get('code') == code), None)
        exam_info = f"📅 **الامتحان النهائي:** {ex['exam_day']} ({ex['exam_period']})" if ex else "📝 **الامتحان النهائي:** غير محدد"
        
        lecs_text = ""
        groups_list = []
        for l in lecs:
            group = l.get('group', 'A')
            day = l.get('day', '')
            time_slot = l.get('time', '')
            room = l.get('room', 'غير محددة')
            instructor = l.get('instructor', 'غير محدد')
            
            groups_list.append(f"م{group}")
            lecs_text += f"🔹 **مجموعة ({group}):** {day} {time_slot}\n"
            lecs_text += f"    📍 القاعة: `{room}` | 👤 الأستاذ: {instructor}\n\n"
            
        groups_str = " ، ".join(sorted(list(set(groups_list))))
        
        content = (
            f"🎓 **تفاصيل مادة: {name}** (`{code}`)\n"
            f"👥 **المجموعات المتاحة:** {groups_str}\n"
            f"─────────────────\n\n"
            f"{lecs_text}"
            f"─────────────────\n"
            f"{exam_info}\n\n"
            f"⚡ _تمت المشاركة فوراً عبر بوت جدولي الذكي_"
        )
        
        description_snippet = f"المجموعات: {groups_str} | {exam_info.replace('**','').replace('📅 ','').replace('📝 ','')}"
        
        result_article = InlineQueryResultArticle(
            id=code,
            title=f"📘 {name} ({code})",
            description=description_snippet,
            input_message_content=InputTextMessageContent(content, parse_mode="Markdown")
        )
        results.append(result_article)
        if len(results) >= 15: break
        
    bot.answer_inline_query(inline_query.id, results, cache_time=10)

# --- Manual Backup Button for Admin ---
@bot.callback_query_handler(func=lambda call: call.data == "admin_manual_backup")
def admin_manual_backup(call):
    bot.answer_callback_query(call.id)
    if call.from_user.id != ADMIN_ID: return
    try:
        backup_path = export_full_backup_json()

        if os.path.exists(backup_path):
            with open(backup_path, "rb") as doc:
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
                caption = f"📦 **نسخة احتياطية يدوية من قاعدة بيانات Turso**\n📅 التوقيت: `{timestamp}`"
                bot.send_document(call.message.chat.id, doc, caption=caption, parse_mode="Markdown")
            os.remove(backup_path)
    except Exception as e:
        bot.send_message(call.message.chat.id, f"❌ حدث خطأ أثناء النسخ الاحتياطي: {e}")

# --- Admin Setup & Manage Handlers ---
@bot.callback_query_handler(func=lambda call: call.data == "setup_master")
def setup_master(call):
    bot.answer_callback_query(call.id)
    if call.from_user.id != ADMIN_ID: return
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("💻 تقنية المعلومات", callback_data="master_college_it"))
    markup.add(InlineKeyboardButton("🛠️ الهندسة", callback_data="master_college_eng"))
    bot.edit_message_text("🏫 اختر الكلية للحساب الماستر:", call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("master_college_"))
def set_master_college(call):
    bot.answer_callback_query(call.id)
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
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except:
        pass
        
    saved = save_master_creds(username, password, college)
    if saved:
        bot.send_message(message.chat.id, "✅ تم حفظ بيانات الماستر بنجاح!\n\nتوا تقدر تضغط على 'سحب الجداول الآن'.")
    else:
        bot.send_message(message.chat.id, "❌ حدث خطأ أثناء حفظ البيانات.")

@bot.callback_query_handler(func=lambda call: call.data == "admin_manage_data")
def admin_manage_data(call):
    bot.answer_callback_query(call.id)
    if call.from_user.id != ADMIN_ID: return
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("📅 تحديث تواريخ الامتحانات", callback_data="admin_update_exam_dates"))
    markup.add(InlineKeyboardButton("✍️ تعديل مادة في جدول المحاضرات", callback_data="admin_edit_faculty"))
    markup.add(InlineKeyboardButton("✍️ تعديل مادة في جدول الامتحانات", callback_data="admin_edit_exams"))
    markup.add(InlineKeyboardButton("🔙 العودة", callback_data="admin_main_menu"))
    bot.edit_message_text("🛠️ واجهة إدارة البيانات يدوياً:", call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "admin_main_menu")
def admin_return_main(call):
    bot.answer_callback_query(call.id)
    start(call.message)

@bot.callback_query_handler(func=lambda call: call.data == "admin_update_exam_dates")
def admin_ask_exam_start_date(call):
    bot.answer_callback_query(call.id)
    msg = bot.send_message(call.message.chat.id, "📅 أرسل تاريخ أول يوم في الامتحانات (بصيغة YYYY-MM-DD):\nمثال: 2024-05-12")
    bot.register_next_step_handler(msg, process_exam_start_date)

def process_exam_start_date(message):
    if message.from_user.id != ADMIN_ID: return
    date_str = message.text.strip()
    try:
        start_date = None
        for fmt in ("%Y-%m-%d", "%d-%m-%Y"):
            try:
                start_date = datetime.strptime(date_str, fmt)
                break
            except:
                continue
        
        if not start_date:
            raise ValueError("صيغة التاريخ غير مدعومة")
        
        with db_lock:
            conn = get_conn()
            c = conn.cursor()
            c.execute("SELECT exam_day, MIN(id) as first_id FROM exams GROUP BY exam_day ORDER BY first_id ASC")
            rows = c.fetchall()
            if not rows:
                bot.send_message(message.chat.id, "⚠️ لا توجد بيانات امتحانات لتحديثها.")
                return
            for i, (d_name, _) in enumerate(rows):
                c.execute("UPDATE exams SET day_index = ? WHERE exam_day = ?", (i + 1, d_name))
            
            c.execute("SELECT DISTINCT day_index FROM exams ORDER BY day_index ASC")
            day_indices = [row[0] for row in c.fetchall()]
            arabic_days = ["الإثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]
            
            current_date = start_date
            index_to_date = {}
            for idx in day_indices:
                while current_date.weekday() == 4: # Friday
                    current_date += timedelta(days=1)
                day_name = arabic_days[current_date.weekday()]
                formatted_date = f"({idx}) {day_name} {current_date.strftime('%Y-%m-%d')}"
                index_to_date[idx] = formatted_date
                current_date += timedelta(days=1)
            
            for idx, date_text in index_to_date.items():
                c.execute("UPDATE exams SET exam_day = ? WHERE day_index = ?", (date_text, idx))
                
            c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", ("exam_dates_map", json.dumps(index_to_date)))
            conn.close()
            
        with file_lock:
            with open("webapp/dates.json", "w", encoding="utf-8") as f:
                json.dump(index_to_date, f, ensure_ascii=False, indent=4)
                
            bot.send_message(message.chat.id, f"✅ تم تحديث تواريخ {len(index_to_date)} يوماً بنجاح!")
            exams = get_db_data("exams")
            faculty = get_db_data("faculty")
            with open(EXAMS_FILE, "w", encoding="utf-8") as f:
                json.dump(exams, f, ensure_ascii=False, indent=4)
            with open(FACULTY_FILE, "w", encoding="utf-8") as f:
                json.dump(faculty, f, ensure_ascii=False, indent=4)
            build_static_webapp(exams, faculty)
        
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ خطأ: تأكد من كتابة التاريخ بشكل صحيح (2024-05-12)\nالتفاصيل: {e}")

@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_edit_"))
def admin_edit_search(call):
    bot.answer_callback_query(call.id)
    table = "faculty" if "faculty" in call.data else "exams"
    msg = bot.send_message(call.message.chat.id, f"🔍 أرسل رمز المادة (Code) المراد تعديلها في جدول {'المحاضرات' if table=='faculty' else 'الامتحانات'}:")
    bot.register_next_step_handler(msg, process_edit_search, table)

def process_edit_search(message, table):
    if message.from_user.id != ADMIN_ID: return
    code = message.text.strip().upper()
    
    with db_lock:
        conn = get_conn()
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute(f"SELECT * FROM {table} WHERE code = ?", (code,))
        rows = c.fetchall()
        
        if not rows and table == "exams":
            c.execute("SELECT * FROM faculty WHERE code = ? LIMIT 1", (code,))
            f_row = c.fetchone()
            conn.close()
            if f_row:
                item = dict(f_row)
                markup = InlineKeyboardMarkup()
                markup.add(InlineKeyboardButton(f"➕ إضافة {code} لجدول الامتحانات", callback_data=f"admin_add_exam_{code}"))
                bot.send_message(message.chat.id, f"🔍 المادة ({item['name']}) موجودة في جدول المحاضرات فقط.\nهل تريد إضافتها لجدول الامتحانات؟", reply_markup=markup)
            else:
                bot.send_message(message.chat.id, f"❌ لم يتم العثور على المادة {code} في أي جدول.")
            return
            
        conn.close()
        
    if not rows:
        bot.send_message(message.chat.id, f"❌ لم يتم العثور على المادة {code} في جدول {table}.")
        return
        
    for row in rows:
        item = dict(row)
        markup = InlineKeyboardMarkup()
        if table == "faculty":
            text = f"📍 مادة: {item['name']} ({item['code']})\n👥 المجموعة: {item['group']}\n📅 اليوم: {item['day']}\n⏰ الوقت: {item['time']}\n👤 الأستاذ: {item['instructor']}\n🏢 القاعة: {item['room']}"
            markup.add(InlineKeyboardButton("تعديل الوقت", callback_data=f"editdb_faculty_time_{item['id']}"))
            markup.add(InlineKeyboardButton("تعديل القاعة", callback_data=f"editdb_faculty_room_{item['id']}"))
        else:
            text = f"📝 مادة: {item['name']} ({item['code']})\n📅 اليوم: {item['exam_day']}\n⏰ الفترة: {item['exam_period']}"
            markup.add(InlineKeyboardButton("تعديل يوم الامتحان", callback_data=f"editdb_exams_exam_day_{item['id']}"))
            markup.add(InlineKeyboardButton("تعديل الفترة", callback_data=f"editdb_exams_exam_period_{item['id']}"))
            
        bot.send_message(message.chat.id, text, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("editdb_"))
def admin_edit_field_step1(call):
    bot.answer_callback_query(call.id)
    parts = call.data.split("_")
    table, field, row_id = parts[1], parts[2], parts[3]
    msg = bot.send_message(call.message.chat.id, f"📝 أرسل القيمة الجديدة لـ ({field}):")
    bot.register_next_step_handler(msg, process_edit_save, table, field, row_id)

def process_edit_save(message, table, field, row_id):
    if message.from_user.id != ADMIN_ID: return
    new_val = message.text.strip()
    try:
        with db_lock:
            conn = get_conn()
            c = conn.cursor()
            c.execute(f"UPDATE {table} SET {field} = ? WHERE id = ?", (new_val, row_id))
            conn.close()
            
        bot.send_message(message.chat.id, "✅ تم التعديل بنجاح!")
        
        with file_lock:
            exams = get_db_data("exams")
            faculty = get_db_data("faculty")
            with open(EXAMS_FILE, "w", encoding="utf-8") as f:
                json.dump(exams, f, ensure_ascii=False, indent=4)
            with open(FACULTY_FILE, "w", encoding="utf-8") as f:
                json.dump(faculty, f, ensure_ascii=False, indent=4)
            build_static_webapp(exams, faculty)
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ فشل التعديل: {e}")

@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_add_exam_"))
def admin_add_exam_step1(call):
    bot.answer_callback_query(call.id)
    code = call.data.replace("admin_add_exam_", "")
    msg = bot.send_message(call.message.chat.id, f"➕ إضافة مادة {code}:\nأرسل (رقم اليوم) في جدول الامتحانات:\nمثال: إذا كان امتحانها في اليوم 13، أرسل 13")
    bot.register_next_step_handler(msg, admin_add_exam_step2, code)

def admin_add_exam_step2(message, code):
    if message.from_user.id != ADMIN_ID: return
    try:
        day_idx = int(message.text.strip())
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("الفترة الأولى", callback_data=f"save_new_exam_{code}_{day_idx}_الفترة الاولى"))
        markup.add(InlineKeyboardButton("الفترة الثانية", callback_data=f"save_new_exam_{code}_{day_idx}_الفترة الثانية"))
        markup.add(InlineKeyboardButton("الفترة الثالثة", callback_data=f"save_new_exam_{code}_{day_idx}_الفترة الثالثة"))
        bot.send_message(message.chat.id, f"📅 اختر الفترة لليوم {day_idx}:", reply_markup=markup)
    except:
        bot.send_message(message.chat.id, "❌ يرجى إرسال رقم اليوم بشكل صحيح (عدد فقط).")

@bot.callback_query_handler(func=lambda call: call.data.startswith("save_new_exam_"))
def admin_add_exam_step3(call):
    bot.answer_callback_query(call.id)
    parts = call.data.split("_")
    code = parts[3]
    day_idx = int(parts[4])
    period = parts[5]

    try:
        with db_lock:
            conn = get_conn()
            c = conn.cursor()
            c.execute("SELECT name FROM faculty WHERE code = ? LIMIT 1", (code,))
            row = c.fetchone()
            name = row[0] if row else code
            
            c.execute("INSERT INTO exams (code, name, exam_day, exam_period, day_index) VALUES (?, ?, ?, ?, ?)",
                      (code, name, f"اليوم {day_idx}", period, day_idx))
            conn.close()

        bot.send_message(call.message.chat.id, f"✅ تم إدراج المادة {name} ({code}) في جدول الامتحانات!")
        
        with file_lock:
            exams = get_db_data("exams")
            faculty = get_db_data("faculty")
            build_static_webapp(exams, faculty)
    except Exception as e:
        bot.send_message(call.message.chat.id, f"❌ فشل الحفظ: {e}")


# =========================================================================
# تشغيل المهام الجانبية والسيرفر الوهمي الآمن (Safe HTTP Server)
# =========================================================================

class SafeHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Bot is running successfully.")

    def log_message(self, format, *args):
        # تعطيل طباعة لوجات الطلبات اليومية للحفاظ على نظافة الـ Logs
        pass

def run_dummy_server():
    port = int(os.getenv("PORT", 8080))
    try:
        with socketserver.TCPServer(("", port), SafeHandler) as httpd:
            print(f"[HTTP] Safe dummy server running on port {port}")
            httpd.serve_forever()
    except Exception as e:
        print(f"[HTTP Error] Failed to start dummy server: {e}")

# 1. تشغيل سيرفر الويب الوهمي لـ Render
threading.Thread(target=run_dummy_server, daemon=True).start()

# 2. تشغيل التنسيق التلقائي للنسخ الاحتياطي
threading.Thread(target=auto_backup_loop, daemon=True).start()


# =========================================================================
# بدء تشغيل البوت والـ Polling
# =========================================================================

if __name__ == "__main__":
    print("[Bot] Cleaning up old sessions...")

    # تنظيف الـ Webhook وأي جلسات معلقة على سيرفرات تلجرام
    try:
        requests.get(f"https://api.telegram.org/bot{TOKEN}/deleteWebhook?drop_pending_updates=True", timeout=5)
    except Exception as e:
        print(f"Warning clearing webhook: {e}")

    # مهلة زمنية لإغلاق النسخة القديمة تماماً على Render أثناء الـ Deploy
    time.sleep(5)

    print("[Bot] Starting polling successfully...")
    bot.infinity_polling(timeout=10, long_polling_timeout=5, skip_pending=True)
