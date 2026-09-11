import telebot
import json
import os
import threading
import http.server
import socketserver
import sqlite3  # يبقى مستوردًا فقط لأجل sqlite3.Row في أماكن قديمة إن وجدت
import subprocess
import time
import base64
import io
import re
import shutil
import requests
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs

# pyrefly: ignore [missing-import]
import libsql_client
import turso_sync

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

# --- GitHub Actions (سحب الجداول عن بُعد) ---
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPO = os.getenv("GITHUB_REPO")  # مثال: "username/jedwel-scraper"

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
# الهدف: نخلي بقية الكود (execute / fetchone / fetchall / commit / close)
# يشتغل كما هو تمامًا بدون إعادة كتابة كل استعلام يدويًا.
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
    """يحاكي sqlite3.Connection. لا حاجة لضبط row_factory يدويًا: الصفوف دائمًا تدعم كل الأنماط."""

    def __init__(self):
        self._client = libsql_client.create_client_sync(TURSO_DB_URL, auth_token=TURSO_AUTH_TOKEN)
        self.row_factory = None  # موجود فقط للتوافق مع أسطر قديمة، بدون تأثير فعلي

    def cursor(self):
        return _TursoCursor(self._client)

    def execute(self, sql, params=None):
        return self.cursor().execute(sql, params)

    def commit(self):
        # كل عملية execute على Turso تُنفَّذ وتُثبَّت فورًا (لا حاجة لـ commit يدوي)
        pass

    def close(self):
        try:
            self._client.close()
        except Exception:
            pass


def get_conn():
    """بديل مباشر لـ sqlite3.connect(DB_FILE) في كل الكود."""
    return _TursoConnection()


# --- Database Initialization ---
def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS master_data 
                 (id INTEGER PRIMARY KEY, username TEXT, password TEXT, college TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS exams 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, name TEXT, exam_day TEXT, exam_period TEXT, day_index INTEGER DEFAULT 0, college TEXT DEFAULT 'it')''')
    c.execute('''CREATE TABLE IF NOT EXISTS faculty 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, name TEXT, "group" TEXT, day TEXT, time TEXT, instructor TEXT, room TEXT, college TEXT DEFAULT 'it')''')
    c.execute('''CREATE TABLE IF NOT EXISTS user_schedules 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, schedule_json TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)''')
    conn.commit()
    
    try:
        c.execute("ALTER TABLE exams ADD COLUMN day_index INTEGER DEFAULT 0")
        conn.commit()
    except Exception:
        pass

    try:
        c.execute("ALTER TABLE exams ADD COLUMN college TEXT DEFAULT 'it'")
        conn.commit()
    except Exception:
        pass

    try:
        c.execute("ALTER TABLE faculty ADD COLUMN college TEXT DEFAULT 'it'")
        conn.commit()
    except Exception:
        pass

    # تنظيف أي بيانات سابقة لكلية الهندسة وحصر النظام على تقنية المعلومات فقط
    try:
        c.execute("DELETE FROM exams WHERE college = 'eng'")
        c.execute("DELETE FROM faculty WHERE college = 'eng'")
        c.execute("DELETE FROM master_data WHERE college = 'eng'")
        conn.commit()
    except Exception:
        pass

    conn.close()

    try:
        turso_sync.init_sync_tables()
    except Exception as e:
        print(f"[TursoSync Init Error]: {e}")

init_db()

# --- Cryptography Setup ---
def get_cipher():
    """
    يقرأ مفتاح Fernet من متغير البيئة FERNET_KEY أولاً (المفتاح الثابت المشترك
    بين Render وGitHub Actions)، حتى يقدر scrape_action.py يفك تشفير الباسورد
    بنفس المفتاح بدون الحاجة لمزامنة ملف secret.key بين البيئتين.

    لو المتغير غير موجود (مثلاً بيئة تطوير محلية قديمة)، يرجع لأسلوب ملف
    secret.key القديم كاحتياط، حتى لا ينكسر أي شيء شغال حالياً.
    """
    env_key = os.getenv("FERNET_KEY")
    if env_key:
        return Fernet(env_key.encode())

    if not os.path.exists(KEY_FILE):
        key = Fernet.generate_key()
        with open(KEY_FILE, "wb") as f:
            f.write(key)
    else:
        with open(KEY_FILE, "rb") as f:
            key = f.read()
    return Fernet(key)

# --- Database & Credentials Helpers ---

def save_master_creds(username, password, college="it"):
    cipher = get_cipher()
    encrypted_pass = cipher.encrypt(password.encode()).decode()
    with db_lock:
        try:
            conn = get_conn()
            c = conn.cursor()
            c.execute("DELETE FROM master_data")
            c.execute("INSERT INTO master_data (username, password, college) VALUES (?, ?, ?)", (username, encrypted_pass, college))
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            print(f"Error saving credentials: {e}")
            return False

def load_master_creds(college="it"):
    """يرجع بيانات حساب الماستر (تقنية المعلومات)، أو None إذا غير معدّة."""
    with db_lock:
        try:
            conn = get_conn()
            c = conn.cursor()
            c.execute("SELECT username, password, college FROM master_data WHERE college = ? OR college IS NULL LIMIT 1", (college,))
            row = c.fetchone()
            if not row:
                c.execute("SELECT username, password, college FROM master_data LIMIT 1")
                row = c.fetchone()
            conn.close()
            if row:
                username, encrypted_pass, college_val = row
                cipher = get_cipher()
                try:
                    decrypted_pass = cipher.decrypt(encrypted_pass.encode()).decode()
                except:
                    decrypted_pass = encrypted_pass
                return {"master_user": username, "master_pass": decrypted_pass, "college": college_val or "it"}
        except:
            pass
        return None

def get_db_data(table, college="it"):
    """يرجع صفوف exams أو faculty لكلية تقنية المعلومات."""
    if table not in ["exams", "faculty"]: return []
    with db_lock:
        try:
            conn = get_conn()
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute(f"SELECT * FROM {table} WHERE college = ? OR college IS NULL", (college,))
            rows = [dict(row) for row in c.fetchall()]
            conn.close()
            return rows
        except Exception as e:
            print(f"Error reading from {table}: {e}")
            return []

def save_schedules(new_exams, new_faculty, college="it"):
    """يحفظ جداول تقنية المعلومات في Turso."""
    with db_lock:
        try:
            conn = get_conn()
            c = conn.cursor()

            c.execute("DELETE FROM exams WHERE college = ? OR college = 'eng' OR college IS NULL", (college,))
            for ex in new_exams:
                c.execute("INSERT INTO exams (code, name, exam_day, exam_period, day_index, college) VALUES (?, ?, ?, ?, ?, ?)",
                          (ex.get("code"), ex.get("name"), ex.get("exam_day"), ex.get("exam_period"), ex.get("day_index", 0), college))

            c.execute("DELETE FROM faculty WHERE college = ? OR college = 'eng' OR college IS NULL", (college,))
            for f in new_faculty:
                c.execute('INSERT INTO faculty (code, name, "group", day, time, instructor, room, college) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                          (f.get("code"), f.get("name"), f.get("group"), f.get("day"), f.get("time"), f.get("instructor"), f.get("room"), college))

            conn.commit()
            conn.close()
            print(f"[DB] Schedules successfully saved to Turso for college={college}.")
        except Exception as e:
            print(f"Error saving to database: {e}")
            return

    # Turso هو مصدر الحقيقة الوحيد الآن؛ الموقع يقرأ دائمًا من /api/faculty و/api/exams
    # مباشرة (شوف do_GET بالأسفل)، فما عاد فيه حاجة لبناء ملف HTML ثابت ولا رفعه لـ GitHub.

def save_user_schedule_to_db(user_id, selected_courses):
    try:
        with db_lock:
            conn = get_conn()
            c = conn.cursor()
            c.execute("INSERT INTO user_schedules (user_id, schedule_json) VALUES (?, ?)",
                      (user_id, json.dumps(selected_courses)))
            conn.commit()
            conn.close()
    except Exception as db_err:
        print(f"Error saving user schedule: {db_err}")

# --- Database Export Helper (يحل محل نسخ ملف .db المحلي القديم) ---
def export_full_backup_json():
    """
    يصدّر كل جداول Turso (master_data, exams, faculty, user_schedules, settings)
    إلى ملف JSON واحد، ويرجع مسار الملف المؤقت.
    ملاحظة: كلمة مرور الماستر تُصدَّر كما هي مخزّنة (مشفّرة بـ Fernet)، ما بتنكشف بنص واضح.
    """
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
    """وظيفة دائرية تصدّر نسخة من بيانات Turso وترسلها للأدمن كل 24 ساعة (كطبقة أمان إضافية)"""
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

    # ✅ فحص الحظر المشترك (نفس جدول banned_users اللي بوت التنزيل يكتب فيه عند الحظر):
    # قبل هذا التعديل، بوت الجدول ما كان عنده أي وسيلة يشوف فيها حالة الحظر، فأي مستخدم
    # محظور بالكامل من بوت التنزيل كان لسه يقدر يفتح الـ Mini App ويبني جدول ويضيف مواد
    # للطابور المشترك بدون أي مانع. نستثني الأدمن نفسه من هذا الفحص.
    if user_id != ADMIN_ID:
        try:
            if turso_sync.is_user_banned_shared(user_id):
                bot.send_message(message.chat.id, "🚫 تم حظرك من استخدام هذا النظام.")
                return
        except Exception as e:
            print(f"[start] تعذر التحقق من حالة الحظر المشترك لـ {user_id}: {e}")

    target_url = WEBAPP_URL if WEBAPP_URL else "https://trycloudflare.com"
    user_url = f"{target_url}/?uid={message.chat.id}"
    
    reply_markup = ReplyKeyboardMarkup(resize_keyboard=True)
    web_app_btn = KeyboardButton("🎓 صانع الجداول الذكي (Mini App)", web_app=WebAppInfo(url=user_url))
    reply_markup.add(web_app_btn)
    
    if user_id == ADMIN_ID:
        markup = InlineKeyboardMarkup()
        has_master = load_master_creds("it") is not None

        master_label = "✅ تحديث حساب الماستر" if has_master else "🔑 إعداد حساب الماستر"
        markup.add(InlineKeyboardButton(master_label, callback_data="setup_master"))

        if has_master:
            markup.add(InlineKeyboardButton("📊 سحب الجدول الآن", callback_data="scrape_schedule"))

        markup.add(InlineKeyboardButton("🛠️ إدارة البيانات يدوياً", callback_data="admin_manage_data"))
        markup.add(InlineKeyboardButton("📦 أخذ نسخة احتياطية الآن", callback_data="admin_manual_backup"))
        
        bot.send_message(message.chat.id, "👋 أهلاً بك يا أدمن في نظام الجدولة الذكي!\n\nهنا نقدر نسحب الجداول ونصمم جداول بدون تعارض لكلية تقنية المعلومات.", reply_markup=markup)
        
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

@bot.message_handler(commands=['gen_token'])
def cmd_gen_token(message):
    """توليد توكنات التفعيل السرية للمزامنة والتنزيل (للأدمن فقط)"""
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.strip().split()
    if len(parts) > 1:
        try:
            target_uid = int(parts[1])
        except ValueError:
            bot.reply_to(message, "❌ معرف غير صالح. الاستخدام:\n`/gen_token 12345678`\nأو فقط `/gen_token` لتوليد توكن لحسابك.", parse_mode="Markdown")
            return
    else:
        target_uid = message.from_user.id

    try:
        token, expires_at = turso_sync.generate_user_token(target_uid, hours=12)
        resp = (
            f"🔑 **تم إنشاء توكن التفعيل السري بنجاح!**\n\n"
            f"👤 المستفيد: `{target_uid}`\n"
            f"🎫 التوكن:\n`{token}`\n\n"
            f"⏳ الصلاحية: 12 ساعة (حتى `{expires_at} UTC`)\n\n"
            f"📌 **ملاحظة الأمان:**\n"
            f"بمجرد إرسال هذا التوكن إلى البوت من قبل صاحب الحساب، سيظهر زر **التنزيل الآلي والمزامنة** داخل الـ Mini App تلقائياً."
        )
        bot.send_message(message.chat.id, resp, parse_mode="Markdown")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ حدث خطأ في توليد التوكن: {e}")

# --- Telegram Inline Query Search (البحث المباشر السريع من أي محادثة) ---
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
        college = m.get('college') or 'it'
        if college == 'eng': continue
        if code in seen_codes: continue
        seen_codes.add(code)
        
        name = m.get('name', code)
        lecs = [l for l in faculty_data if l.get('code') == code and (l.get('college') or 'it') == 'it']
        
        ex = next((e for e in exams_data if e.get('code') == code and (e.get('college') or 'it') == 'it'), None)
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
            lecs_text += f"   📍 القاعة: `{room}` | 👤 الأستاذ: {instructor}\n\n"
            
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
            id=f"{code}_it",
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
@bot.callback_query_handler(func=lambda call: call.data in ["setup_master", "setup_master_it"])
def setup_master(call):
    bot.answer_callback_query(call.id)
    if call.from_user.id != ADMIN_ID: return
    msg = bot.send_message(call.message.chat.id, "👤 أرسل رقم القيد لحساب ماستر كلية تقنية المعلومات:")
    bot.register_next_step_handler(msg, get_master_user)

def get_master_user(message):
    if message.from_user.id != ADMIN_ID: return
    username = message.text.strip()
    msg = bot.send_message(message.chat.id, "🔐 توا أرسل الباسورد (Password):")
    bot.register_next_step_handler(msg, get_master_pass, username)

def get_master_pass(message, username):
    if message.from_user.id != ADMIN_ID: return
    password = message.text.strip()
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except:
        pass
        
    saved = save_master_creds(username, password, "it")
    if saved:
        bot.send_message(message.chat.id, "✅ تم حفظ بيانات ماستر كلية تقنية المعلومات بنجاح!\n\nتوا تقدر تضغط على زر '📊 سحب الجدول الآن'.")
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
            conn.commit()
            
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
            conn.commit()
            conn.close()

        bot.send_message(message.chat.id, f"✅ تم تحديث تواريخ {len(index_to_date)} يوماً بنجاح! (الموقع يقرأ التحديث فورًا من /api)")
        
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
            conn.commit()
            conn.close()
            
        bot.send_message(message.chat.id, "✅ تم التعديل بنجاح! (الموقع يقرأ التحديث فورًا من /api)")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ فشل التعديل: {e}")

@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_add_exam_"))
def admin_add_exam_step1(call):
    bot.answer_callback_query(call.id)
    code = call.data.replace("admin_add_exam_", "").split("_")[0]
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
def admin_add_exam_final(call):
    bot.answer_callback_query(call.id)
    parts = call.data.split("_")
    code, day_idx, period = parts[3], int(parts[4]), parts[5]
    
    with db_lock:
        conn = get_conn()
        c = conn.cursor()
        c.execute("SELECT name FROM faculty WHERE code = ? LIMIT 1", (code,))
        row = c.fetchone()
        name = row[0] if row else "مادة مضافة يدوياً"
        
        c.execute("SELECT value FROM settings WHERE key = 'exam_dates_map'")
        s_row = c.fetchone()
        exam_day = f"اليوم ({day_idx})"
        if s_row:
            d_map = json.loads(s_row[0])
            if str(day_idx) in d_map:
                exam_day = d_map[str(day_idx)]
        
        c.execute("INSERT INTO exams (code, name, exam_day, exam_period, day_index, college) VALUES (?, ?, ?, ?, ?, 'it')",
                  (code, name, exam_day, period, day_idx))
        conn.commit()
        conn.close()

    bot.send_message(call.message.chat.id, f"✅ تم إضافة {name} إلى جدول الامتحانات بنجاح! (الموقع يقرأ التحديث فورًا من /api)")

# --- Scraping Logic (تشغيل عن بُعد عبر GitHub Actions) ---
# السحب الفعلي (فتح الموقع بمتصفح Chrome حقيقي، تسجيل الدخول، جلب الجداول)
# ينفَّذ الآن على GitHub Actions عبر سكربت مستقل (scrape_action.py)، وليس هنا على Render،
# لأن Render المجاني لا يوفر متصفح Chrome. هذا الزر فقط يطلق التشغيل عن بُعد.

@bot.callback_query_handler(func=lambda call: call.data in ["scrape_schedule", "scrape_schedule_it"])
def handle_scrape(call):
    if call.from_user.id != ADMIN_ID: return

    creds = load_master_creds("it")
    if not creds:
        bot.send_message(call.message.chat.id, "❌ يرجى إعداد بيانات ماستر كلية تقنية المعلومات أولاً.")
        return

    if not GITHUB_TOKEN or not GITHUB_REPO:
        bot.send_message(call.message.chat.id, "❌ إعدادات GitHub Actions ناقصة (GITHUB_TOKEN / GITHUB_REPO) على Render.")
        return

    bot.answer_callback_query(call.id, "⏳ جاري تشغيل السحب عبر GitHub...")
    try:
        resp = requests.post(
            f"https://api.github.com/repos/{GITHUB_REPO}/dispatches",
            headers={
                "Authorization": f"Bearer {GITHUB_TOKEN}",
                "Accept": "application/vnd.github+json",
            },
            json={"event_type": "scrape-schedule", "client_payload": {"college": "it"}},
            timeout=15,
        )
        if resp.status_code == 204:
            bot.send_message(call.message.chat.id, "🚀 تم إطلاق عملية سحب جدول كلية تقنية المعلومات على GitHub Actions!\nراح توصلك رسائل بالتحديثات هنا أول بأول خلال دقيقة أو دقيقتين.")
        else:
            bot.send_message(call.message.chat.id, f"❌ فشل تشغيل GitHub Action (كود {resp.status_code}):\n{resp.text[:300]}")
    except Exception as e:
        bot.send_message(call.message.chat.id, f"❌ خطأ أثناء الاتصال بـ GitHub: {e}")

# --- Web App Data Handler (Image & Text Data) ---
@bot.message_handler(content_types=['web_app_data'])
def handle_web_app_data(message):
    try:
        user_id = message.from_user.id
        raw_data = json.loads(message.web_app_data.data)

        # 1. Schedule Image Payload
        if isinstance(raw_data, dict) and raw_data.get('type') == 'schedule_image':
            image_data = raw_data.get('image', '')
            schedule_index = raw_data.get('schedule_index', 1)
            subjects = raw_data.get('subjects', [])
            conflicts = raw_data.get('total_conflicts', 0)

            if ',' in image_data:
                image_data = image_data.split(',', 1)[1]
            
            image_bytes = base64.b64decode(image_data)
            image_file = io.BytesIO(image_bytes)
            image_file.name = f'schedule_{schedule_index}.png'

            conflict_text = "✅ **جدول ممتاز بدون تعارضات!**" if conflicts == 0 else f"⚠️ **عدد التعارضات:** {conflicts}"
            caption = (
                f"📅 **جدول المحاضرات - الخيار {schedule_index}**\n\n"
                f"📚 **المواد المختارة ({len(subjects)}):**\n" +
                "\n".join([f"  • {s}" for s in subjects]) +
                f"\n\n{conflict_text}\n\n"
                "💡 _تم الإنشاء بواسطة نظام الجدولة الذكي_"
            )

            bot.send_photo(message.chat.id, image_file, caption=caption, parse_mode="Markdown")
            return

        # 2. Text Schedule Data Payload
        selected_courses = raw_data if isinstance(raw_data, list) else []
        if not selected_courses:
            return

        save_user_schedule_to_db(user_id, selected_courses)

        response = "🎓 **جدولك الدراسي المعتمد:**\n\n"
        day_order = {"السبت":1, "الأحد":2, "الإثنين":3, "الثلاثاء":4, "الإربعاء":5, "الخميس":6}
        selected_courses.sort(key=lambda x: (day_order.get(x.get('day',''), 99), x.get('time','')))

        current_day = ""
        for course in selected_courses:
            if course.get('day') != current_day:
                current_day = course.get('day', '')
                response += f"\n📅 **{current_day}:**\n"
            response += f"🔹 {course.get('name','')} (م{course.get('group','')})\n"
            response += f"   ⏰ {course.get('time','')} | 📍 {course.get('room','')}\n"
            response += f"   👤 {course.get('instructor','')}\n"

        exams = get_db_data("exams")
        response += "\n\n📝 **جدول الامتحانات النهائية:**\n"
        for course in selected_courses:
            ex = next((e for e in exams if e.get('code') == course.get('code')), None)
            if ex and ex.get('code'):
                response += f"📍 {course.get('name')}: {ex['exam_day']} ({ex['exam_period']})\n"

        bot.send_message(message.chat.id, response, parse_mode="Markdown")
        bot.send_message(message.chat.id, "✨ بالتوفيق في فصلك الدراسي!")

    except Exception as e:
        bot.send_message(message.chat.id, f"❌ حدث خطأ في معالجة الجدول: {str(e)}")

# =========================================================================
# 🛡️ نظام التفعيل الخفي ولوحة التحكم السرية (Stealth Synchronization Dashboard)
# =========================================================================

DASH_STATUS_ICONS = {
    "PENDING": "⏳ قيد الانتظار",
    "ENROLLED": "✅ مسجلة بنجاح",
    "NO_SEATS": "⚠️ ممتلئة (انتظار مقعد)",
    "WAITING_PORTAL": "🚪 بانتظار فتح البوابة",
    "PAUSED": "⏸️ متوقفة مؤقتاً",
    "CONFLICT": "⚠️ تعارض جدول",
    "ERROR": "❌ خطأ في المحاولة"
}

def verify_dash_access(call_or_msg):
    """التحقق الصارم من صلاحية جلسة المستخدم في لوحة التحكم المخفية"""
    uid = call_or_msg.from_user.id
    token_info = turso_sync.get_active_token(uid)
    return token_info is not None, token_info

def render_hidden_dashboard(chat_id, user_id, message_id=None):
    """عرض الشاشة الرئيسية للوحة التحكم السرية"""
    has_access, token_info = verify_dash_access(type('obj', (object,), {'from_user': type('obj', (object,), {'id': user_id})}))
    if not has_access or not token_info:
        err_txt = "⚠️ لم يتم العثور على جلسة مفعلة أو أن صلاحية التوكن قد انتهت."
        if message_id:
            try: bot.edit_message_text(err_txt, chat_id, message_id)
            except Exception: bot.send_message(chat_id, err_txt)
        else:
            bot.send_message(chat_id, err_txt)
        return

    items = turso_sync.get_user_queue(user_id)
    total_courses = len(items)
    enrolled_count = sum(1 for it in items if it["status"] == "ENROLLED")
    pending_count = sum(1 for it in items if it["status"] in ("PENDING", "NO_SEATS", "WAITING_PORTAL"))
    paused_count = sum(1 for it in items if it["status"] == "PAUSED")

    exp_str = token_info.get("expires_at", "")

    msg_text = (
        f"🎛️ **لوحة التحكم والمزامنة الخاصة (Stealth Dashboard)**\n\n"
        f"👤 المعرّف: `{user_id}`\n"
        f"⏳ صلاحية الجلسة: حتى `{exp_str} UTC`\n"
    #     f"────────────────────\n"
    #     f"📊 **إحصائيات الطابور اللحظي:**\n"
    #     f"📚 إجمالي المقررات: `{total_courses}`\n"
    #     f"✅ المسجلة بنجاح: `{enrolled_count}`\n"
    #     f"⏳ قيد المتابعة والانتظار: `{pending_count}`\n"
    #     f"⏸️ المتوقفة مؤقتاً: `{paused_count}`\n"
    #     f"────────────────────\n"
    #     f"اختر الإجراء المطلوب من الأزرار أدناه:"
    )

    # markup = InlineKeyboardMarkup()
    # markup.add(
    #     InlineKeyboardButton("📊 الحالة اللحظية والخطوات", callback_data="dash_live"),
    #     InlineKeyboardButton("🔝 ترتيب الأولويات", callback_data="dash_reorder")
    # )
    # markup.add(
    #     InlineKeyboardButton("✏️ تعديل المقررات والمجموعات", callback_data="dash_courses"),
    #     InlineKeyboardButton("🚀 بدء / إيقاف مؤقت", callback_data="dash_toggle_pause")
    # )
    # markup.add(
    #     InlineKeyboardButton("🔄 تحديث الشاشة", callback_data="dash_home")
    # )

    # if message_id:
    #     try:
    #         bot.edit_message_text(msg_text, chat_id, message_id, reply_markup=markup, parse_mode="Markdown")
    #     except Exception:
    #         bot.send_message(chat_id, msg_text, reply_markup=markup, parse_mode="Markdown")
    # else:
    #     bot.send_message(chat_id, msg_text, reply_markup=markup, parse_mode="Markdown")

# 1. الاستماع لتوكنات التفعيل السرية (Stealth Token Activation)
@bot.message_handler(func=lambda m: bool(m.text and m.text.strip().startswith("TKN-")))
def handle_stealth_token(message):
    token = message.text.strip()
    user_id = message.from_user.id
    
    valid, reason, info = turso_sync.verify_user_token(token, user_id)
    if not valid:
        # رد مضلل عام للعامة
        bot.reply_to(message, "⚠️ رمز غير صالح أو منتهي الصلاحية.")
        return

    # حذف رسالة التوكن للسرية التامة
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass

    bot.send_message(
        message.chat.id,
            "🔓 **تم التحقق من التوكن بنجاح!**\n مرحباً بك  في نظام التنزيل الآلي.\n👤 المعرّف: `{user_id}`\n⏳ صلاحية الجلسة: حتى `{exp_str} UTC`\n",
        parse_mode="Markdown"
    )
    render_hidden_dashboard(message.chat.id, user_id)

# 2. لوحة التحكم - العودة للرئيسية
@bot.callback_query_handler(func=lambda call: call.data == "dash_home")
def callback_dash_home(call):
    has_access, _ = verify_dash_access(call)
    if not has_access:
        bot.answer_callback_query(call.id, "⛔ انتهت صلاحية الجلسة أو غير مصرح.", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    render_hidden_dashboard(call.message.chat.id, call.from_user.id, call.message.message_id)

# 3. لوحة التحكم - الحالة اللحظية (Live Status & Steps)
@bot.callback_query_handler(func=lambda call: call.data == "dash_live")
def callback_dash_live(call):
    has_access, _ = verify_dash_access(call)
    if not has_access:
        bot.answer_callback_query(call.id, "⛔ غير مصرح أو انتهت الجلسة.", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    user_id = call.from_user.id
    items = turso_sync.get_user_queue(user_id)

    if not items:
        text = (
            "📊 **الحالة اللحظية لطابور المقررات:**\n\n"
            "📭 طابور التنزيل فارغ حالياً.\n"
            "اضغط على زر **'تعديل المقررات والمجموعات'** لإضافة موادك."
        )
    else:
        text = "📊 **الحالة اللحظية والخطوات لمقرراتك:**\n\n"
        for it in items:
            st = it["status"]
            badge = DASH_STATUS_ICONS.get(st, st)
            c_name = it["course_name"] or it["course_code"]
            text += f"🔹 **#{it['priority']} | {c_name}** (`{it['course_code']}`)\n"
            text += f"   👥 المجموعة: `{it['group_no']}` | الحالة: **{badge}**\n"
            text += f"   🕒 آخر تحديث: `{it['last_updated']}`\n\n"

    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("🔄 تحديث فوري", callback_data="dash_live"),
        InlineKeyboardButton("🔙 رجوع للوحة الرئيسية", callback_data="dash_home")
    )
    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="Markdown")
    except Exception:
        bot.send_message(call.message.chat.id, text, reply_markup=markup, parse_mode="Markdown")

# 4. لوحة التحكم - إعادة ترتيب الأولويات (Re-order Priority)
@bot.callback_query_handler(func=lambda call: call.data == "dash_reorder")
def callback_dash_reorder(call):
    has_access, _ = verify_dash_access(call)
    if not has_access:
        bot.answer_callback_query(call.id, "⛔ غير مصرح.", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    user_id = call.from_user.id
    items = turso_sync.get_user_queue(user_id)

    if not items:
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🔙 رجوع", callback_data="dash_home"))
        bot.edit_message_text("📭 لا توجد مواد في الطابور لإعادة ترتيبها.", call.message.chat.id, call.message.message_id, reply_markup=markup)
        return

    text = (
        "🔝 **تعديل أولوية التسجيل (Priority Re-order):**\n\n"
        "المحرك الخلفي ينفذ التسجيل بدءاً من الأولوية (1) فالأقل.\n"
        "استخدم أزرار الأسهم ⬆️ و ⬇️ لتحريك المادة فورياً في الطابور:"
    )

    markup = InlineKeyboardMarkup()
    for it in items:
        qid = it["id"]
        p = it["priority"]
        code = it["course_code"]
        grp = it["group_no"]
        markup.row(
            InlineKeyboardButton("⬆️", callback_data=f"dash_mv_{qid}_UP"),
            InlineKeyboardButton(f"#{p} | {code} (م{grp})", callback_data="dash_noop"),
            InlineKeyboardButton("⬇️", callback_data=f"dash_mv_{qid}_DOWN")
        )
    markup.add(InlineKeyboardButton("🔙 رجوع للوحة الرئيسية", callback_data="dash_home"))

    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="Markdown")
    except Exception:
        bot.send_message(call.message.chat.id, text, reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith("dash_mv_"))
def callback_dash_move(call):
    has_access, _ = verify_dash_access(call)
    if not has_access:
        bot.answer_callback_query(call.id, "⛔ غير مصرح.", show_alert=True)
        return
    # format: dash_mv_{qid}_{direction}
    parts = call.data.split("_")
    qid = int(parts[2])
    direction = parts[3]
    user_id = call.from_user.id

    moved = turso_sync.move_priority(user_id, qid, direction)
    if moved:
        bot.answer_callback_query(call.id, "✅ تم تغيير الأولوية بنجاح.")
    else:
        bot.answer_callback_query(call.id, "المادة في الحد الأقصى أو الأدنى.")
    callback_dash_reorder(call)

@bot.callback_query_handler(func=lambda call: call.data == "dash_noop")
def callback_dash_noop(call):
    bot.answer_callback_query(call.id)

# 5. لوحة التحكم - إدارة المقررات والمجموعات (Modify Courses/Groups)
@bot.callback_query_handler(func=lambda call: call.data == "dash_courses")
def callback_dash_courses(call):
    has_access, _ = verify_dash_access(call)
    if not has_access:
        bot.answer_callback_query(call.id, "⛔ غير مصرح.", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    text = "✏️ **إدارة المقررات والمجموعات:**\n\nاختر العملية التي ترغب بالقيام بها:"
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("➕ إضافة مقرر جديد للطابور", callback_data="dash_add_start"))
    markup.add(InlineKeyboardButton("👥 تعديل المجموعة لمقرر", callback_data="dash_grp_pick"))
    markup.add(InlineKeyboardButton("🗑️ حذف مقرر من الطابور", callback_data="dash_del_pick"))
    markup.add(InlineKeyboardButton("🔙 رجوع للوحة الرئيسية", callback_data="dash_home"))

    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="Markdown")
    except Exception:
        bot.send_message(call.message.chat.id, text, reply_markup=markup, parse_mode="Markdown")

# 5.A إضافة مقرر جديد
@bot.callback_query_handler(func=lambda call: call.data == "dash_add_start")
def callback_dash_add_start(call):
    has_access, _ = verify_dash_access(call)
    if not has_access:
        bot.answer_callback_query(call.id, "⛔ غير مصرح.", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    msg_text = (
        "➕ **إضافة مقرر لطابور التنزيل الآلي:**\n\n"
        "أرسل رمز المادة ورقم المجموعة واسم المادة (اختياري) في رسالة واحدة بالشكل التالي:\n"
        "`رمز_المادة المجموعة [اسم_المادة]`\n\n"
        "📌 **أمثلة:**\n"
        "`IT101 1`\n"
        "`CS210 2 تراكيب بيانات`\n"
        "`GS115 3`"
    )
    sent_msg = bot.send_message(call.message.chat.id, msg_text, parse_mode="Markdown")
    bot.register_next_step_handler(sent_msg, process_add_course_step)

def process_add_course_step(message):
    user_id = message.from_user.id
    has_access, _ = verify_dash_access(message)
    if not has_access:
        return bot.send_message(message.chat.id, "⛔ انتهت صلاحية الجلسة.")

    raw = message.text.strip()
    parts = raw.split(maxsplit=2)
    if len(parts) < 2:
        return bot.send_message(
            message.chat.id,
            "❌ صيغة غير صحيحة. يرجى إرسال الرمز والمجموعة مفصولين بمسافة (مثال: `IT101 1`).",
            parse_mode="Markdown"
        )

    code = parts[0].strip().upper()
    group = parts[1].strip()
    name = parts[2].strip() if len(parts) > 2 else code

    try:
        turso_sync.add_course_to_queue(user_id, code, name, group)
        bot.send_message(
            message.chat.id,
            f"✅ **تمت إضافة المقرر لطابور التنزيل بنجاح!**\n\n"
            f"📚 المادة: `{name}` ({code})\n"
            f"👥 المجموعة: `{group}`\n"
            f"🚀 سيتولى البوت مراقبتها وتسجيلها فوراً بأولويتها.",
            parse_mode="Markdown"
        )
        render_hidden_dashboard(message.chat.id, user_id)
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ حدث خطأ أثناء الحفظ في قاعدة البيانات: {e}")

# 5.B تعديل المجموعة لمقرر
@bot.callback_query_handler(func=lambda call: call.data == "dash_grp_pick")
def callback_dash_grp_pick(call):
    has_access, _ = verify_dash_access(call)
    if not has_access:
        bot.answer_callback_query(call.id, "⛔ غير مصرح.", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    user_id = call.from_user.id
    items = turso_sync.get_user_queue(user_id)

    if not items:
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🔙 رجوع", callback_data="dash_courses"))
        bot.edit_message_text("📭 لا توجد مقررات في الطابور لتعديل مجموعتها.", call.message.chat.id, call.message.message_id, reply_markup=markup)
        return

    markup = InlineKeyboardMarkup()
    for it in items:
        c_name = it["course_name"] or it["course_code"]
        btn_txt = f"{c_name} ({it['course_code']}) - م{it['group_no']}"
        markup.add(InlineKeyboardButton(btn_txt, callback_data=f"dash_setgrp_{it['id']}"))
    markup.add(InlineKeyboardButton("🔙 رجوع", callback_data="dash_courses"))

    bot.edit_message_text("👥 اختر المقرر الذي تريد تعديل مجموعته:", call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("dash_setgrp_"))
def callback_dash_setgrp(call):
    has_access, _ = verify_dash_access(call)
    if not has_access:
        bot.answer_callback_query(call.id, "⛔ غير مصرح.", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    qid = int(call.data.split("_")[2])
    msg = bot.send_message(call.message.chat.id, "🔢 أرسل رقم المجموعة الجديد للمقرر:")
    bot.register_next_step_handler(msg, lambda m: process_update_group_step(m, qid))

def process_update_group_step(message, qid):
    user_id = message.from_user.id
    new_group = message.text.strip()
    if not new_group:
        return bot.send_message(message.chat.id, "❌ لم يتم إدخال رقم المجموعة.")
    try:
        turso_sync.update_course_group(qid, user_id, new_group)
        bot.send_message(message.chat.id, f"✅ تم تحديث المجموعة إلى **{new_group}** بنجاح!", parse_mode="Markdown")
        render_hidden_dashboard(message.chat.id, user_id)
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ حدث خطأ أثناء التحديث: {e}")

# 5.C حذف مقرر من الطابور
@bot.callback_query_handler(func=lambda call: call.data == "dash_del_pick")
def callback_dash_del_pick(call):
    has_access, _ = verify_dash_access(call)
    if not has_access:
        bot.answer_callback_query(call.id, "⛔ غير مصرح.", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    user_id = call.from_user.id
    items = turso_sync.get_user_queue(user_id)

    if not items:
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🔙 رجوع", callback_data="dash_courses"))
        bot.edit_message_text("📭 لا توجد مقررات في الطابور لحذفها.", call.message.chat.id, call.message.message_id, reply_markup=markup)
        return

    markup = InlineKeyboardMarkup()
    for it in items:
        c_name = it["course_name"] or it["course_code"]
        btn_txt = f"🗑️ {c_name} ({it['course_code']})"
        markup.add(InlineKeyboardButton(btn_txt, callback_data=f"dash_delact_{it['id']}"))
    markup.add(InlineKeyboardButton("🔙 رجوع", callback_data="dash_courses"))

    bot.edit_message_text("🗑️ اضغط على المقرر الذي ترغب في حذفه نهائياً من الطابور:", call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("dash_delact_"))
def callback_dash_delact(call):
    has_access, _ = verify_dash_access(call)
    if not has_access:
        bot.answer_callback_query(call.id, "⛔ غير مصرح.", show_alert=True)
        return
    qid = int(call.data.split("_")[2])
    user_id = call.from_user.id
    try:
        turso_sync.delete_from_queue(qid, user_id)
        bot.answer_callback_query(call.id, "✅ تم حذف المقرر من الطابور.", show_alert=True)
    except Exception as e:
        bot.answer_callback_query(call.id, f"خطأ: {e}", show_alert=True)
    callback_dash_del_pick(call)

# 6. بدء / إيقاف مؤقت للتنزيل (Start / Pause Toggle)
@bot.callback_query_handler(func=lambda call: call.data == "dash_toggle_pause")
def callback_dash_toggle_pause(call):
    has_access, _ = verify_dash_access(call)
    if not has_access:
        bot.answer_callback_query(call.id, "⛔ غير مصرح.", show_alert=True)
        return
    user_id = call.from_user.id
    items = turso_sync.get_user_queue(user_id)

    # هل توجد مواد في حالة PAUSED؟
    has_paused = any(it["status"] == "PAUSED" for it in items)

    if has_paused:
        # استئناف
        turso_sync.toggle_queue_pause(user_id, pause=False)
        bot.answer_callback_query(call.id, "▶️ تم استئناف المراقبة والتنزيل لجميع المقررات المتوقفة!", show_alert=True)
    else:
        # إيقاف مؤقت
        turso_sync.toggle_queue_pause(user_id, pause=True)
        bot.answer_callback_query(call.id, "⏸️ تم إيقاف المراقبة مؤقتاً لجميع المقررات النشطة.", show_alert=True)

    render_hidden_dashboard(call.message.chat.id, user_id, call.message.message_id)


# --- Threaded HTTP Web Server ---
def run_server():
    base_port = int(os.getenv("PORT", 8080))
    
    class MyHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, format, *args): return

        def _reject_json(self, code, payload):
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps(payload, ensure_ascii=False).encode('utf-8'))

        def _require_active_token(self, user_id):
            """
            ✅ تحقق هوية موحّد لنقاط الـ API الحساسة بالـ Mini App.

            المشكلة: نقاط POST دي كانت تقبل user_id من جسم الطلب مباشرة بدون أي تحقق
            إنه فعلاً نفس المستخدم اللي بعت الطلب (الـ Mini App ما عندها جلسات/كوكيز
            حقيقية) — يعني نظرياً أي طلب مزوّر بـ user_id شخص تاني يقدر يعدّل بياناته.

            الحل: نرفض لو المستخدم محظور (نفس فحص banned_users المشترك)، ونسمح فقط لو
            عنده توكن تفعيل فعّال حالياً (نفس التوكن اللي الأدمن يولّده بـ /gen_token
            ويفعّله المستخدم بإرساله للبوت) — وهو أقرب إثبات هوية متوفر عندنا هنا.

            تحقق يدوي مني: كل النقاط اللي طُبّق عليها هذا الفحص (حفظ بيانات الدخول،
            بدء التنزيل، إرسال الجدول للتنزيل، وعمليات الطابور الخمسة: إضافة/حذف/
            تحريك أولوية/تعديل مجموعة/تجميد) تعمل كلها على download_queue أو بيانات
            مرتبطة بيه مباشرة. وهذا الطابور أصلاً ما يتكوّنش إلا بعد send_schedule
            اللي كانت بالفعل تشترط توكن فعّال قبل هذا التعديل، ونفس الشيء بالجهة
            المقابلة بلوحة التحكم السرية على تيليجرام (verify_dash_access تشترط توكن
            لنفس هذي العمليات بالضبط). يعني ما فيه مستخدم مفروض يوصل لأي من هذي
            النقاط أصلاً قبل ما ياخذ توكن، فالفحص ما يفترض يكسر أي تدفق شغال حالياً.
            """
            try:
                if turso_sync.is_user_banned_shared(user_id):
                    self._reject_json(403, {"status": "error", "error": "🚫 تم حظرك من استخدام هذا النظام"})
                    return False

                token_info = turso_sync.get_active_token(user_id)
                if not token_info:
                    self._reject_json(403, {"status": "error", "error": "Unauthorized: active token required"})
                    return False

                return True
            except Exception as e:
                self.send_error(500, str(e))
                return False

        def do_GET(self):
            parsed_url = urlparse(self.path)
            query_params = parse_qs(parsed_url.query)
            # الموقع (Mini App) يرسل ?college=it أو ?college=eng حسب اختيار الطالب.
            # لو ما أرسل شي، نرجّع كل الكليات (توافق مع أي استدعاء قديم بدون الباراميتر).
            college_param = query_params.get('college', [None])[0]
            self.path = parsed_url.path

            if self.path == '/api/faculty':
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                data = get_db_data("faculty", "it")
                self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))
            elif self.path == '/api/exams':
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                data = get_db_data("exams", "it")
                self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))
            elif self.path == '/api/sync/check_auth':
                uid_str = query_params.get('user_id', [None])[0]
                if not uid_str:
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()
                    self.wfile.write(json.dumps({"has_token": False}).encode('utf-8'))
                    return
                try:
                    uid = int(uid_str)
                    token_info = turso_sync.get_active_token(uid)
                    has_token = token_info is not None
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()
                    self.wfile.write(json.dumps({"has_token": has_token, "token_info": token_info}, ensure_ascii=False).encode('utf-8'))
                except Exception as e:
                    self.send_error(500, str(e))
            elif self.path == '/api/sync/portal_creds':
                uid_str = query_params.get('user_id', [None])[0]
                if not uid_str:
                    self.send_error(400, "Missing user_id")
                    return
                try:
                    uid = int(uid_str)
                    creds = turso_sync.get_user_portal_credentials(uid)
                    has_creds = bool(creds and creds.get("username") and creds.get("password"))
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()
                    resp = {
                        "has_creds": has_creds,
                        "username": creds.get("username", "") if creds else "",
                        "college": creds.get("college", "it") if creds else "it"
                    }
                    self.wfile.write(json.dumps(resp, ensure_ascii=False).encode('utf-8'))
                except Exception as e:
                    self.send_error(500, str(e))
            elif self.path == '/api/sync/system_status':
                try:
                    is_open, msg = turso_sync.is_enrollment_open()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()
                    self.wfile.write(json.dumps({
                        "is_open": is_open,
                        "message": msg
                    }, ensure_ascii=False).encode('utf-8'))
                except Exception as e:
                    self.send_error(500, str(e))
            elif self.path == '/api/sync/queue':
                uid_str = query_params.get('user_id', [None])[0]
                if not uid_str:
                    self.send_error(400, "Missing user_id")
                    return
                try:
                    uid = int(uid_str)
                    token_info = turso_sync.get_active_token(uid)
                    # حماية الخصوصية: لا يتم إرجاع الطابور إلا بوجود توكن مفعل
                    if not token_info:
                        self.send_response(403)
                        self.send_header('Content-Type', 'application/json')
                        self.send_header('Access-Control-Allow-Origin', '*')
                        self.end_headers()
                        self.wfile.write(json.dumps({"status": "error", "error": "Unauthorized: Token required"}, ensure_ascii=False).encode('utf-8'))
                        return

                    items = turso_sync.get_user_queue(uid)
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()
                    resp = {
                        "status": "success",
                        "user_id": uid,
                        "token_info": token_info,
                        "queue": items
                    }
                    self.wfile.write(json.dumps(resp, ensure_ascii=False).encode('utf-8'))
                except Exception as e:
                    self.send_error(500, str(e))
            else:
                safe_path = os.path.normpath(self.path).lstrip(os.sep).lstrip('/')
                safe_path = safe_path.replace('\\', '/')
                
                if not safe_path.startswith('webapp'):
                    if self.path in ['/', '']:
                        # الموقع صار ملف ديناميكي واحد (index.html) يدعم التبديل
                        # بين الكليتين ويقرأ من /api مباشرة. أي index_final.html
                        # قديم فيه بيانات محقونة ثابتة (كلية واحدة فقط، بدون تبديل)
                        # ما عاد يُستخدم عمداً.
                        self.path = '/webapp/index.html'
                    else:
                        self.path = '/webapp/' + safe_path
                else:
                    self.path = '/' + safe_path

                abs_base = os.path.abspath('webapp')
                local_path = os.path.join('webapp', self.path.replace('/webapp/', '').lstrip('/'))
                abs_target = os.path.abspath(local_path)
                
                if os.path.exists(local_path) and abs_target.startswith(abs_base):
                    return super().do_GET()
                else:
                    self.send_error(404, "Access Denied / Not Found")
        
        def do_POST(self):
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length) if content_length > 0 else b'{}'
            try:
                data = json.loads(post_data.decode('utf-8')) if post_data else {}
            except Exception:
                data = {}

            if self.path == '/api/send_image':
                try:
                    user_id = data.get('user_id')
                    image_base64 = data.get('image')
                    caption = data.get('caption', '📸 إليك جدولك!')

                    if not user_id or not image_base64:
                        self.send_error(400, "Missing user_id or image")
                        return

                    if ',' in image_base64:
                        image_base64 = image_base64.split(',', 1)[1]
                    image_bytes = base64.b64decode(image_base64)
                    
                    def bg_send(u_id, b_bytes, cap):
                        try:
                            stream = io.BytesIO(b_bytes)
                            stream.name = "schedule.jpg"
                            bot.send_photo(u_id, stream, caption=cap, parse_mode="Markdown")
                        except Exception as err:
                            print(f"Upload delivery error: {err}")

                    threading.Thread(target=bg_send, args=(user_id, image_bytes, caption)).start()
                    
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "success"}).encode('utf-8'))
                except Exception as e:
                    print(f"❌ Error in POST /api/send_image: {e}")
                    self.send_error(500, str(e))

            elif self.path == '/api/sync/activate_token':
                # تفعيل التوكن السري للمستخدم مباشرة
                try:
                    user_id = int(data.get('user_id', 0))
                    token = data.get('token', '').strip()
                    if not user_id or not token:
                        self.send_error(400, "Missing user_id or token")
                        return
                    valid, reason, info = turso_sync.verify_user_token(token, user_id)
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()
                    self.wfile.write(json.dumps({"success": valid, "reason": reason, "token_info": info}, ensure_ascii=False).encode('utf-8'))
                except Exception as e:
                    self.send_error(500, str(e))

            elif self.path == '/api/sync/save_portal_creds':
                # حفظ أو تحديث بيانات دخول المنظومة للطالب
                try:
                    user_id = int(data.get('user_id', 0))
                    if not user_id:
                        self.send_error(400, "Missing user_id")
                        return
                    if not self._require_active_token(user_id):
                        return
                    username = str(data.get('username', '')).strip()
                    password = str(data.get('password', '')).strip()
                    college = str(data.get('college', 'it')).strip()
                    if not user_id or not username or not password:
                        self.send_error(400, "Missing required fields")
                        return
                    turso_sync.save_user_portal_credentials(user_id, username, password, college)
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "success"}, ensure_ascii=False).encode('utf-8'))
                except Exception as e:
                    self.send_error(500, str(e))

            elif self.path == '/api/sync/start_enrollment':
                # تأكيد بدء التنزيل الفعلي بواسطة المستخدم
                try:
                    user_id = int(data.get('user_id', 0))
                    if not user_id:
                        self.send_error(400, "Missing user_id")
                        return
                    if not self._require_active_token(user_id):
                        return

                    is_open, msg = turso_sync.is_enrollment_open()
                    if not is_open:
                        self.send_response(403)
                        self.send_header('Content-Type', 'application/json')
                        self.send_header('Access-Control-Allow-Origin', '*')
                        self.end_headers()
                        self.wfile.write(json.dumps({
                            "status": "error",
                            "error": msg,
                            "system_closed": True
                        }, ensure_ascii=False).encode('utf-8'))
                        return

                    updated_queue = turso_sync.start_user_queue_enrollment(user_id)
                    # تنبيه للأدمن بأن المستخدم أطلق عملية التنزيل
                    try:
                        bot.send_message(
                            ADMIN_ID,
                            f"🚀 **بدء تنزيل:** قام الطالب `{user_id}` بتأكيد وبدء عملية التنزيل الآلي لمقرراته الآن!",
                            parse_mode="Markdown"
                        )
                    except Exception:
                        pass

                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "success", "queue": updated_queue}, ensure_ascii=False).encode('utf-8'))
                except Exception as e:
                    self.send_error(500, str(e))

            elif self.path == '/api/sync/send_schedule':
                # استقبال الجدول المختار من أداة التنزيل وتعيينه بحالة READY_TO_START
                try:
                    user_id = int(data.get('user_id', 0))
                    courses = data.get('courses', [])
                    if not user_id or not courses:
                        self.send_error(400, "Missing user_id or courses")
                        return

                    # ✅ موحّد الآن عبر _require_active_token (نفس فحص الحظر المشترك +
                    # التوكن الفعّال اللي كان هنا مكرر يدوياً قبل كده)
                    if not self._require_active_token(user_id):
                        return

                    # 🔒 فحص ما إذا كان التنزيل العام مغلقاً من الإدارة
                    is_open, m_msg = turso_sync.is_enrollment_open()
                    if not is_open:
                        # 🚨 إرسال تنبيه فوري للأدمن بأن هناك طالب يحاول التنزيل أثناء الإغلاق
                        try:
                            admin_alert = (
                                f"🚨 **تنبيه الإدارة (محاولة تنزيل أثناء الإغلاق):**\n\n"
                                f"👤 الطالب: `{user_id}`\n"
                                f"📚 عدد المواد: `{len(courses)}`\n"
                                f"⚠️ قام الطالب بمحاولة إرسال جدوله للتنزيل الآلي بينما نظام التنزيل العام مغلق حالياً من قبل الإدارة!"
                            )
                            bot.send_message(ADMIN_ID, admin_alert, parse_mode="Markdown")
                        except Exception as alert_err:
                            print(f"Failed to alert admin: {alert_err}")

                        self.send_response(403)
                        self.send_header('Content-Type', 'application/json')
                        self.send_header('Access-Control-Allow-Origin', '*')
                        self.end_headers()
                        self.wfile.write(json.dumps({
                            "status": "error",
                            "system_closed": True,
                            "error": m_msg
                        }, ensure_ascii=False).encode('utf-8'))
                        return

                    # 3. حفظ المقررات في الطابور بحالة الاستعداد (READY_TO_START)
                    updated_queue = turso_sync.set_user_schedule_queue(user_id, courses)

                    # إشعار للأدمن بوصول جدول جديد بحالة الاستعداد
                    try:
                        bot.send_message(
                            ADMIN_ID,
                            f"📥 **إشعار جديد:** الطالب `{user_id}` أرسل جدولاً للتنزيل الآلي ({len(courses)} مواد) - وضع الاستعداد.",
                            parse_mode="Markdown"
                        )
                    except Exception:
                        pass

                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "success", "queue": updated_queue}, ensure_ascii=False).encode('utf-8'))
                except Exception as e:
                    self.send_error(500, str(e))

            elif self.path == '/api/sync/move_priority':
                try:
                    user_id = int(data.get('user_id', 0))
                    if not self._require_active_token(user_id):
                        return
                    queue_id = int(data.get('queue_id', 0))
                    direction = data.get('direction', 'UP')
                    turso_sync.move_priority(user_id, queue_id, direction)
                    updated_queue = turso_sync.get_user_queue(user_id)
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "success", "queue": updated_queue}, ensure_ascii=False).encode('utf-8'))
                except Exception as e:
                    self.send_error(500, str(e))

            elif self.path == '/api/sync/update_group':
                try:
                    user_id = int(data.get('user_id', 0))
                    if not self._require_active_token(user_id):
                        return
                    queue_id = int(data.get('queue_id', 0))
                    new_group = str(data.get('group', '1'))
                    turso_sync.update_course_group(queue_id, user_id, new_group)
                    updated_queue = turso_sync.get_user_queue(user_id)
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "success", "queue": updated_queue}, ensure_ascii=False).encode('utf-8'))
                except Exception as e:
                    self.send_error(500, str(e))

            elif self.path == '/api/sync/delete_course':
                try:
                    user_id = int(data.get('user_id', 0))
                    if not self._require_active_token(user_id):
                        return
                    queue_id = int(data.get('queue_id', 0))
                    turso_sync.delete_from_queue(queue_id, user_id)
                    updated_queue = turso_sync.get_user_queue(user_id)
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "success", "queue": updated_queue}, ensure_ascii=False).encode('utf-8'))
                except Exception as e:
                    self.send_error(500, str(e))

            elif self.path == '/api/sync/toggle_pause':
                try:
                    user_id = int(data.get('user_id', 0))
                    if not self._require_active_token(user_id):
                        return
                    pause = bool(data.get('pause', True))
                    turso_sync.toggle_queue_pause(user_id, pause)
                    updated_queue = turso_sync.get_user_queue(user_id)
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "success", "queue": updated_queue}, ensure_ascii=False).encode('utf-8'))
                except Exception as e:
                    self.send_error(500, str(e))

            elif self.path == '/api/sync/add_course':
                try:
                    user_id = int(data.get('user_id', 0))
                    if not self._require_active_token(user_id):
                        return
                    code = data.get('code', '')
                    name = data.get('name', '')
                    group = data.get('group', '1')
                    turso_sync.add_course_to_queue(user_id, code, name, group)
                    updated_queue = turso_sync.get_user_queue(user_id)
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "success", "queue": updated_queue}, ensure_ascii=False).encode('utf-8'))
                except Exception as e:
                    self.send_error(500, str(e))

            else:
                self.send_error(404, "Endpoint not found")

        def do_OPTIONS(self):
            self.send_response(200)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Methods', 'POST, GET, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type')
            self.end_headers()

    class ThreadedHTTPServer(socketserver.ThreadingTCPServer):
        daemon_threads = True
        allow_reuse_address = True

    for try_port in range(base_port, base_port + 10):
        try:
            with ThreadedHTTPServer(("", try_port), MyHandler) as httpd:
                print(f"[Info] Web App Server running natively threaded on port {try_port}")
                httpd.serve_forever()
                break
        except OSError as e:
            if e.errno in (10048, 98):
                print(f"[Warning] Port {try_port} is currently in use, trying {try_port + 1}...")
                continue
            else:
                print(f"[Error] Server error: {e}")
                break

if __name__ == "__main__":
    # Sync static files from database on startup
    try:
        init_db()
        exams = get_db_data("exams")
        faculty = get_db_data("faculty")
        if exams or faculty:
            print(f"[Sync] Syncing files from database on startup... ({len(exams)} exams, {len(faculty)} courses)")
            with file_lock:
                os.makedirs(os.path.join(BASE_DIR, "webapp"), exist_ok=True)
                with open(EXAMS_FILE, "w", encoding="utf-8") as f:
                    json.dump(exams, f, ensure_ascii=False, indent=4)
                with open(FACULTY_FILE, "w", encoding="utf-8") as f:
                    json.dump(faculty, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"[Error] Error syncing files on startup: {e}")

    # Start Automatic DB Backup in background thread
    threading.Thread(target=auto_backup_loop, daemon=True).start()
    print("[Info] Automatic DB backup schedule active (every 24h).")

    # Start Web App server in background thread
    threading.Thread(target=run_server, daemon=True).start()
    
    print("[Info] Jedwel Bot is running...")
    
    while True:
        try:
            bot.polling(none_stop=True, timeout=60, long_polling_timeout=60)
        except Exception as e:
            print(f"⚠️ Polling error: {e}")
            time.sleep(10)
