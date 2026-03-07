import telebot
import json
import os
import threading
import http.server
import socketserver
import sqlite3
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove

# Basic configuration
TOKEN = '8417503660:AAHMH3Byey2qhhDZETzzWfI3m8L2XHI23tk'
ADMIN_ID = 1084115596 

bot = telebot.TeleBot(TOKEN)
DATA_FILE = "master_data.json"
EXAMS_FILE = "webapp/exams.json"
FACULTY_FILE = "webapp/faculty.json"

# --- Web App Configuration ---
WEBAPP_URL = "https://87422f5k-8080.euw.devtunnels.ms/" 

# --- Database Configuration ---
DB_FILE = "jedwel.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    # Table for master credentials
    c.execute('''CREATE TABLE IF NOT EXISTS master_data 
                 (id INTEGER PRIMARY KEY, username TEXT, password TEXT, college TEXT)''')
    # Table for exam schedules
    c.execute('''CREATE TABLE IF NOT EXISTS exams 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, name TEXT, exam_day TEXT, exam_period TEXT)''')
    # Table for faculty schedules
    c.execute('''CREATE TABLE IF NOT EXISTS faculty 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, name TEXT, "group" TEXT, day TEXT, time TEXT, instructor TEXT, room TEXT)''')
    # Table for student saved schedules
    c.execute('''CREATE TABLE IF NOT EXISTS user_schedules 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, schedule_json TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    conn.close()

init_db()

# --- User State Management ---
user_states = {} # {user_id: {'mode': 'idle', 'selected_codes': []}}

# --- Credentials Management ---
def save_master_creds(username, password, college):
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("DELETE FROM master_data") # Keep only one master
        c.execute("INSERT INTO master_data (username, password, college) VALUES (?, ?, ?)", (username, password, college))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Error saving credentials: {e}")
        return False

def load_master_creds():
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT username, password, college FROM master_data LIMIT 1")
        row = c.fetchone()
        conn.close()
        if row:
            return {"master_user": row[0], "master_pass": row[1], "college": row[2]}
    except:
        pass
    return None

def load_json(filename):
    # This is still used for reading files if needed, but we'll prefer DB
    if os.path.exists(filename):
        try:
            with open(filename, "r", encoding="utf-8") as f:
                content = f.read().strip()
                return json.loads(content) if content else []
        except: return []
    return []

# Helper to get all data from DB
def get_db_data(table):
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute(f"SELECT * FROM {table}")
        rows = [dict(row) for row in c.fetchall()]
        conn.close()
        return rows
    except Exception as e:
        print(f"Error reading from {table}: {e}")
        return []

# --- Schedule Data Management ---
def save_schedules(new_exams, new_faculty):
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()

        # 1. Update Exam Schedule
        c.execute("DELETE FROM exams")
        for ex in new_exams:
            c.execute("INSERT INTO exams (code, name, exam_day, exam_period) VALUES (?, ?, ?, ?)",
                      (ex.get("code"), ex.get("name"), ex.get("exam_day"), ex.get("exam_period")))

        # 2. Update Faculty Schedule
        c.execute("DELETE FROM faculty")
        for f in new_faculty:
            c.execute("INSERT INTO faculty (code, name, [group], day, time, instructor, room) VALUES (?, ?, ?, ?, ?, ?, ?)",
                      (f.get("code"), f.get("name"), f.get("group"), f.get("day"), f.get("time"), f.get("instructor"), f.get("room")))

        conn.commit()
        conn.close()
        
        # --- Update legacy JSON files for compatibility ---
        with open(EXAMS_FILE, "w", encoding="utf-8") as f:
            json.dump(new_exams, f, ensure_ascii=False, indent=4)
        with open(FACULTY_FILE, "w", encoding="utf-8") as f:
            json.dump(new_faculty, f, ensure_ascii=False, indent=4)

        # --- توليد نسخة الويب المدمجة ---
        build_static_webapp(new_exams, new_faculty)
    except Exception as e:
        print(f"Error saving to database: {e}")

def build_static_webapp(exams, faculty):
    """دمج البيانات داخل ملف الـ HTML ليعمل بدون سيرفر محلي"""
    try:
        template_path = os.path.join("webapp", "index.html")
        output_path = os.path.join("webapp", "index_final.html")
        
        if not os.path.exists(template_path): return
        
        with open(template_path, "r", encoding="utf-8") as f:
            html = f.read()
        
        # حقن البيانات في مكانها الصحيح
        data_script = f"""
        <script>
            window.allCourses = {json.dumps(faculty, ensure_ascii=False)};
            window.allExams = {json.dumps(exams, ensure_ascii=False)};
            console.log("Data Injected Successfully!");
        </script>
        """
        # نضع البيانات قبل وسم الـ script الأصلي
        final_html = html.replace('<script src="https://telegram.org/js/telegram-web-app.js"></script>', 
                                 data_script + '<script src="https://telegram.org/js/telegram-web-app.js"></script>')
        
        # تعديل وظيفة loadData لتستخدم البيانات المدمجة
        final_html = final_html.replace('loadData();', 'updateUI();')

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(final_html)
        print("✅ Created index_final.html with embedded data.")
    except Exception as e:
        print(f"Error building static webapp: {e}")

@bot.message_handler(commands=['start'])
def start(message):
    user_id = message.from_user.id
    if user_id == ADMIN_ID:
        # Inline Markup for Admin Controls
        markup = InlineKeyboardMarkup()
        creds = load_master_creds()
        if creds:
             markup.add(InlineKeyboardButton("✅ تحديث حساب الماستر", callback_data="setup_master"))
             markup.add(InlineKeyboardButton("📊 سحب الجداول الآن", callback_data="scrape_schedule"))
        else:
             markup.add(InlineKeyboardButton("🔑 إعداد حساب الماستر", callback_data="setup_master"))
        
        # Reply Keyboard for Web App
        reply_markup = ReplyKeyboardMarkup(resize_keyboard=True)
        web_app_btn = KeyboardButton("🎓 صانع الجداول الذكي (Mini App)", web_app = WebAppInfo(url=WEBAPP_URL))
        reply_markup.add(web_app_btn)

        bot.send_message(message.chat.id, "👋 أهلاً بك يا أدمن في نظام الجدولة الذكي!\n\nهنا نقدر نسحب الجداول ونصمم جداول بدون تعارض.", reply_markup=markup)
        
        notice = (
            "💡 **كيف تصمم جدولك؟**\n\n"
            "اضغط على زر **Mini App** بالأسفل لفتح الواجهة الذكية واختيار موادك بدون تعارضات."
        )
        bot.send_message(message.chat.id, notice, reply_markup=reply_markup, parse_mode="Markdown")
    else:
        reply_markup = ReplyKeyboardMarkup(resize_keyboard=True)
        web_app_btn = KeyboardButton("🎓 صانع الجداول الذكي (Mini App)", web_app = WebAppInfo(url=WEBAPP_URL))
        reply_markup.add(web_app_btn)
        bot.send_message(message.chat.id, "👋 أهلاً بك! اضغط على الزر أدناه لفتح واجهة تصميم الجدول الدراسي:", reply_markup=reply_markup)

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
        bot.send_message(message.chat.id, "✅ تم حفظ بيانات الماستر بنجاح!\n\nتوا تقدر تضغط على 'سحب الجداول الآن'.")
    else:
        bot.send_message(message.chat.id, "❌ حدث خطأ أثناء حفظ البيانات.")

# --- Scraping Logic ---
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import Select
import time

import re

def parse_exam_schedule(driver):
    """تحليل جدول الامتحانات مع تطبيق قواعد الفلترة (الفترة 4 واليوم 13+)"""
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
            
            for i in range(1, 5): # الفترات 1 إلى 4
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

        # القاعدة 1: فحص إذا كانت الفترة الرابعة فارغة تماماً في كل الجدول
        is_p4_empty_everywhere = True
        for day in all_days_raw:
            if day["periods"][3]: # إذا وجد مادة في الفترة 4 لأي يوم
                is_p4_empty_everywhere = False
                break
        
        # القاعدة 2: فحص الأيام من (13) فما فوق
        day_13_idx = -1
        for idx, day in enumerate(all_days_raw):
            if "(13)" in day["day_text"]:
                day_13_idx = idx
                break
        
        if day_13_idx != -1:
            all_after_13_empty = True
            for i in range(day_13_idx, len(all_days_raw)):
                for p in all_days_raw[i]["periods"]:
                    if p: # إذا وجد مادة واحدة فقط
                        all_after_13_empty = False
                        break
                if not all_after_13_empty: break
            
            if all_after_13_empty:
                # حذف كل الأيام من 13 لنهاية الجدول لأنها فارغة تماماً
                all_days_raw = all_days_raw[:day_13_idx]

        # تحويل البيانات للشكل النهائي مع حفظ الخانات الفارغة للأيام الباقية
        final_exams = []
        max_periods = 3 if is_p4_empty_everywhere else 4
        
        for day in all_days_raw:
            for p_idx in range(max_periods):
                p_name = periods_list[p_idx]
                exams_in_p = day["periods"][p_idx]
                
                if exams_in_p:
                    for ex in exams_in_p:
                        final_exams.append({
                            "code": ex["code"],
                            "name": ex["name"],
                            "exam_day": day["day_text"],
                            "exam_period": p_name
                        })
                else:
                    # تخزين الخانة كفارغة (حتى لو فاضية خزنها كما طلبت)
                    final_exams.append({
                        "code": None,
                        "name": "فارغ",
                        "exam_day": day["day_text"],
                        "exam_period": p_name
                    })
        return final_exams
    except Exception as e:
        print(f"Error parsing exams: {e}")
        return []

def parse_faculty_schedule(driver, exam_data):
    """تحليل جدول الكلية المعقد واستخراج كافة التفاصيل بدقة"""
    try:
        table = driver.find_element(By.TAG_NAME, "table")
        rows = table.find_elements(By.TAG_NAME, "tr")
        if not rows: return []
        
        # 1. استخراج الفترات الزمنية من الصف الأول
        headers = rows[0].find_elements(By.TAG_NAME, "td")
        time_slots = [h.text.strip() for h in headers[1:] if h.text.strip()]
        
        faculty_data = []
        # 2. المرور على أيام الأسبوع
        for row in rows[1:]:
            cells = row.find_elements(By.TAG_NAME, "td")
            if not cells: continue
            
            day = cells[0].text.strip()
            if not day: continue
            
            # 3. المرور على كل فترة زمنية في اليوم
            for i in range(1, len(cells)):
                cell = cells[i]
                time_range = time_slots[i-1] if (i-1) < len(time_slots) else f"الفترة {i}"
                
                # البحث عن كل مادة داخل الخلية (قد تكون هناك عدة مواد)
                # المواد تأتي في وسوم <p> والمعلومات في وسوم <div>
                try:
                    course_elements = cell.find_elements(By.TAG_NAME, "p")
                    info_elements = cell.find_elements(By.TAG_NAME, "div")
                    
                    for idx, p_tag in enumerate(course_elements):
                        # اسم المادة من خاصية title
                        course_full_name = p_tag.get_attribute("title") or "غير معروف"
                        course_text = p_tag.text.strip()
                        
                        # استخراج الرمز والمجموعة (مثلاً: ITGS240 (A))
                        match = re.search(r"([\w\d]+)\s*\(\s*([A-Za-z0-9]+)\s*\)", course_text)
                        code = match.group(1) if match else course_text
                        group = match.group(2) if match else "1"
                        
                        # معلومات الدكتور والقاعة من الـ div المقابل
                        instructor = "غير محدد"
                        room = "غير محدد"
                        if idx < len(info_elements):
                            div_tag = info_elements[idx]
                            instructor = div_tag.text.split('(')[0].replace("أستاذ المقرر", "").strip()
                            try:
                                room_tag = div_tag.find_element(By.TAG_NAME, "a")
                                room = room_tag.text.strip("()") or room_tag.get_attribute("title")
                            except:
                                pass
                        
                        # فحص إذا كانت كل المعلومات "فارغة" أو "غير معروفة" لتجنب تخزين محاضرة لا قيمة لها
                        if course_full_name == "غير معروف" and instructor == "غير محدد" and room == "غير محدد":
                            continue

                        faculty_data.append({
                            "code": code,
                            "name": course_full_name,
                            "group": group,
                            "day": day,
                            "time": time_range,
                            "instructor": instructor,
                            "room": room
                        })
                except Exception as e:
                    print(f"Error in cell parsing: {e}")
                    
        return faculty_data
    except Exception as e:
        print(f"Error parsing faculty schedule: {e}")
        return []

def scrape_process(chat_id, creds):
    chrome_options = Options()
    # chrome_options.add_argument("--headless")
    chrome_options.add_argument("--window-size=1200,800")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--no-sandbox")
    
    driver = None
    try:
        driver = webdriver.Chrome(options=chrome_options)
        wait = WebDriverWait(driver, 30)
        
        bot.send_message(chat_id, "🌐 جاري فتح الكروم والدخول للمنظومة...")
        driver.get("https://sms.uot.edu.ly/eng/login_ing.php")
        
        # اختيار الكلية
        fac_dropdown = wait.until(EC.element_to_be_clickable((By.ID, "fac")))
        select = Select(fac_dropdown)
        target_text = "تقنية المعلومات" if creds['college'] == 'it' else "الهندسة"
        select.select_by_visible_text(target_text)
        
        # إدخال البيانات
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

        # 1. جدول الامتحانات
        open_schedule_menu()
        exam_link = wait.until(EC.element_to_be_clickable((By.XPATH, "//p[contains(text(), 'جدول الامتحانات النهائية')]")))
        driver.execute_script("arguments[0].click();", exam_link)
        time.sleep(5)
        exam_data = parse_exam_schedule(driver)
        bot.send_message(chat_id, f"📝 تم سحب {len(exam_data)} مادة من جدول الامتحانات.")
        
        # 2. جدول الكلية
        open_schedule_menu()
        faculty_link = wait.until(EC.element_to_be_clickable((By.XPATH, "//p[contains(text(), 'جدول الكلية')]")))
        driver.execute_script("arguments[0].click();", faculty_link)
        time.sleep(5)
        
        bot.send_message(chat_id, "🏫 جاري سحب جدول الكلية وتنسيق البيانات...")
        faculty_data = parse_faculty_schedule(driver, exam_data)
        
        # حفظ كل البيانات (فصل وتحديث)
        save_schedules(exam_data, faculty_data)
        
        status_msg = (
            "✅ اكتملت العملية بنجاح!\n\n"
            f"📝 تم تحديث {len(exam_data)} مادة في {EXAMS_FILE}\n"
            f"🏫 تم تحديث {len(faculty_data)} محاضرة في {FACULTY_FILE}\n\n"
            "📂 الجداول الآن محدثة وجاهزة."
        )
        bot.send_message(chat_id, status_msg)
        
    except Exception as e:
        bot.send_message(chat_id, f"❌ حدث خطأ أثناء السحب: {str(e)}")
    finally:
        time.sleep(10)
        if driver: driver.quit()

@bot.callback_query_handler(func=lambda call: call.data == "scrape_schedule")
def handle_scrape(call):
    if call.from_user.id != ADMIN_ID: return
    creds = load_master_creds()
    if not creds:
        bot.send_message(call.message.chat.id, "❌ يرجى إعداد بيانات الماستر أولاً.")
        return
    
    bot.answer_callback_query(call.id, "⏳ بدأت العملية...")
    threading.Thread(target=scrape_process, args=(call.message.chat.id, creds)).start()

# --- Web App Data Handler ---
@bot.message_handler(content_types=['web_app_data'])
def handle_web_app_data(message):
    try:
        user_id = message.from_user.id
        selected_courses = json.loads(message.web_app_data.data)
        
        # Save to Database
        try:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("INSERT INTO user_schedules (user_id, schedule_json) VALUES (?, ?)", (user_id, json.dumps(selected_courses)))
            conn.commit()
            conn.close()
        except Exception as db_err:
            print(f"Error saving user schedule: {db_err}")

        response = "🎓 **جدولك الدراسي النهائي الخالي من التعارضات:**\n\n"
        
        # ترتيب حسب الأيام
        day_order = {"السبت":1, "الأحد":2, "الإثنين":3, "الثلاثاء":4, "الإربعاء":5, "الخميس":6}
        selected_courses.sort(key=lambda x: (day_order.get(x['day'], 99), x['time']))

        current_day = ""
        for course in selected_courses:
            if course['day'] != current_day:
                current_day = course['day']
                response += f"\n📅 **{current_day}:**\n"
            
            response += f"🔹 {course['name']} ({course['group']})\n"
            response += f"   ⏰ {course['time']} | 📍 {course['room']}\n"
            response += f"   👤 د. {course['instructor']}\n"

        # إضافة معلومات الامتحانات
        exams = load_json(EXAMS_FILE)
        response += "\n\n📝 **جدول الامتحانات النهائية لموادك:**\n"
        
        for course in selected_courses:
            ex = next((e for e in exams if e.get('code') == course['code']), None)
            if ex and ex.get('code'):
                response += f"📍 {course['name']}: {ex['exam_day']} ({ex['exam_period']})\n"

        bot.send_message(message.chat.id, response, parse_mode="Markdown")
        bot.send_message(message.chat.id, "✨ بالتوفيق في فصلك الدراسي!")

    except Exception as e:
        bot.send_message(message.chat.id, f"❌ حدث خطأ في معالجة الجدول: {str(e)}")

# --- Threaded HTTP Server ---
def run_server():
    PORT = 8080
    class MyHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, format, *args): return # Silent log
        
        def do_GET(self):
            # API Endpoints
            if self.path == '/api/faculty':
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                data = get_db_data("faculty")
                self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))
            elif self.path == '/api/exams':
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                data = get_db_data("exams")
                self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))
            
            # Static Files
            else:
                # If root, serve index.html from webapp folder
                if self.path == '/':
                    self.path = '/webapp/index.html'
                # If webapp path not specified, prepend it
                elif not self.path.startswith('/webapp/'):
                    self.path = '/webapp' + self.path
                
                # Check if file exists
                file_path = self.path.lstrip('/')
                if os.path.exists(file_path):
                    return super().do_GET()
                else:
                    self.send_error(404, "File not found")
        
    try:
        socketserver.TCPServer.allow_reuse_address = True
        with socketserver.TCPServer(("", PORT), MyHandler) as httpd:
            print(f"🌍 Web App Server running on port {PORT}")
            httpd.serve_forever()
    except Exception as e:
        print(f"❌ Server error: {e}")

if __name__ == "__main__":
    # Start Web App server in background
    threading.Thread(target=run_server, daemon=True).start()
    
    print("✅ Jedwel Bot is running...")
    bot.polling(none_stop=True)
