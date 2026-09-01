"""
سكربت السحب المستقل - يشتغل على GitHub Actions فقط (مو على Render).
يفتح الموقع بمتصفح Chrome حقيقي (متوفر افتراضياً على أجهزة GitHub Actions)،
يسجّل دخول، يسحب جدول الامتحانات وجدول الكلية، ويحفظهم مباشرة في Turso.
يرسل تحديثات الحالة للأدمن عبر تيليجرام مباشرة (بدون الحاجة لتشغيل البوت نفسه).

كل القيم الحساسة تُقرأ من GitHub Actions Secrets (متغيرات بيئة وقت التشغيل):
TURSO_DB_URL, TURSO_AUTH_TOKEN, BOT_TOKEN, ADMIN_CHAT_ID, FERNET_KEY

بيانات الماستر (رقم القيد/الباسورد/الكلية) ما عادت تُقرأ من أسرار منفصلة
(MASTER_USER / MASTER_PASS / MASTER_COLLEGE)؛ بدلها يقرأها السكربت مباشرة
من صف master_data في Turso ويفك تشفير الباسورد بـ FERNET_KEY (نفس المفتاح
المستخدم في jedwel_bot.py). هذا يعني أي تحديث لبيانات الماستر من داخل
البوت (زر "تحديث حساب الماستر") ينعكس تلقائياً هنا بدون تحديث أي سر يدوياً.
"""

import os
import re
import time
import requests
import libsql_client
from cryptography.fernet import Fernet
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options

# --- قراءة الإعدادات من GitHub Secrets ---
TURSO_DB_URL = os.environ["TURSO_DB_URL"].replace("wss://", "https://").replace("libsql://", "https://")
TURSO_AUTH_TOKEN = os.environ["TURSO_AUTH_TOKEN"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_CHAT_ID = os.environ["ADMIN_CHAT_ID"]
FERNET_KEY = os.environ["FERNET_KEY"]


def notify(text: str) -> None:
    """يرسل رسالة تيليجرام للأدمن مباشرة عبر Bot API (بدون تشغيل البوت نفسه).
    يتأكد فعليًا من نجاح الإرسال (كود 200)، ولو فشل بسبب صيغة Markdown مكسورة
    (رموز backtick/asterisk/underscore غير متطابقة داخل نص ديناميكي مثل رسالة خطأ)
    يعيد المحاولة كنص عادي بدون تنسيق حتى لا تضيع الرسالة بصمت."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={"chat_id": ADMIN_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=15,
        )
        if resp.status_code == 200:
            return
        print(f"[Telegram notify] فشل الإرسال بصيغة Markdown (كود {resp.status_code}): {resp.text[:300]}")
        # إعادة المحاولة بدون parse_mode حتى تصل الرسالة حتى لو فيها رموز تكسر التنسيق
        resp2 = requests.post(url, json={"chat_id": ADMIN_CHAT_ID, "text": text}, timeout=15)
        if resp2.status_code != 200:
            print(f"[Telegram notify] فشلت المحاولة البديلة أيضًا (كود {resp2.status_code}): {resp2.text[:300]}")
    except Exception as e:
        print(f"[Telegram notify error] {e}")


def load_master_creds_from_turso():
    """يقرأ بيانات الماستر (username/password/college) مباشرة من جدول master_data
    في Turso، ويفك تشفير الباسورد بمفتاح FERNET_KEY الثابت (المشترك مع Render).
    """
    client = libsql_client.create_client_sync(TURSO_DB_URL, auth_token=TURSO_AUTH_TOKEN)
    try:
        result = client.execute("SELECT username, password, college FROM master_data LIMIT 1")
        if not result.rows:
            raise RuntimeError(
                "لا توجد بيانات ماستر محفوظة في Turso (جدول master_data فارغ). "
                "يرجى إعداد حساب الماستر أولاً من داخل البوت."
            )
        username, encrypted_pass, college = result.rows[0]
        cipher = Fernet(FERNET_KEY.encode())
        try:
            password = cipher.decrypt(encrypted_pass.encode()).decode()
        except Exception as decrypt_err:
            raise RuntimeError(
                f"تعذّر فك تشفير باسورد الماستر بمفتاح FERNET_KEY الحالي: {decrypt_err}"
            )
        return username, password, (college or "it")
    finally:
        client.close()


MASTER_USER, MASTER_PASS, MASTER_COLLEGE = load_master_creds_from_turso()


def build_chrome() -> webdriver.Chrome:
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1366,900")
    # تقليل احتمالية اكتشاف Cloudflare إن السحب آلي
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
    # Selenium 4.6+ يدير chromedriver تلقائياً (Selenium Manager) - ما نحتاج تثبيت يدوي
    return webdriver.Chrome(options=options)


# ---------------- نفس منطق التحليل الموجود بالبوت (بدون أي تغيير) ----------------

def parse_exam_schedule(driver):
    try:
        tbody = driver.find_element(By.TAG_NAME, "tbody")
        rows = tbody.find_elements(By.TAG_NAME, "tr")
        all_days_raw = []
        periods_list = ["الفترة الاولى", "الفترة الثانية", "الفترة الثالثة", "الفترة الرابعة"]

        for row in rows:
            if "اليوم" in row.text and "الفترة" in row.text:
                continue
            cells = row.find_elements(By.TAG_NAME, "td")
            if len(cells) < 2:
                continue

            day_text = cells[0].text.strip()
            day_periods_data = []

            for i in range(1, 5):
                period_exams = []
                if i < len(cells):
                    spans = cells[i].find_elements(By.TAG_NAME, "span")
                    for span in spans:
                        text = span.text.strip()
                        if not text:
                            continue
                        match = re.search(r"^(.*?)\s*\(\s*([\w\d]+)\s*\)$", text)
                        if match:
                            period_exams.append({"code": match.group(2).strip(), "name": match.group(1).strip()})
                day_periods_data.append(period_exams)
            all_days_raw.append({"day_text": day_text, "periods": day_periods_data})

        if not all_days_raw:
            return []

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
                if not all_after_13_empty:
                    break
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
                            "day_index": idx + 1,
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
        if not rows:
            return []

        headers = rows[0].find_elements(By.TAG_NAME, "td")
        time_slots = [h.text.strip() for h in headers[1:] if h.text.strip()]

        faculty_data = []
        seen_lectures = set()
        for row in rows[1:]:
            cells = row.find_elements(By.TAG_NAME, "td")
            if not cells:
                continue

            day = cells[0].text.strip()
            if not day:
                continue

            for i in range(1, len(cells)):
                cell = cells[i]
                time_range = time_slots[i - 1] if (i - 1) < len(time_slots) else f"الفترة {i}"

                try:
                    children = cell.find_elements(By.XPATH, "./*")
                    current_course = None

                    for child in children:
                        tag = child.tag_name.lower()
                        if tag == "p":
                            course_text = child.text.strip()
                            course_full_name = (child.get_attribute("title") or "").strip()

                            if not course_text or "غير معروف" in course_full_name:
                                continue

                            match = re.search(r"([\w\d]+)\s*\(\s*([A-Za-z0-9]+)\s*\)", course_text)
                            code = match.group(1).strip() if match else course_text
                            group = match.group(2).strip() if match else "A"

                            if code == "غير معروف":
                                continue

                            lecture_key = (code, group, day, time_range)
                            if lecture_key not in seen_lectures:
                                current_course = {
                                    "code": code,
                                    "name": course_full_name or code,
                                    "group": group,
                                    "day": day,
                                    "time": time_range,
                                    "instructor": "غير محدد",
                                    "room": "غير محدد",
                                }
                                faculty_data.append(current_course)
                                seen_lectures.add(lecture_key)
                            else:
                                current_course = None

                        elif tag == "div" and current_course:
                            div_text = child.text.strip()
                            if not div_text:
                                continue

                            parts = re.split(r"\(|قاعة|مدرج", div_text)
                            inst_match = parts[0].replace("أستاذ المقرر", "").strip()
                            if inst_match and len(inst_match) > 2:
                                current_course["instructor"] = inst_match

                            try:
                                room_tag = child.find_element(By.TAG_NAME, "a")
                                room_text = (room_tag.text.strip("()") or room_tag.get_attribute("title") or "").strip()
                                if room_text:
                                    current_course["room"] = room_text
                            except Exception:
                                room_search = re.search(r"\((.*?)\)", div_text)
                                if room_search:
                                    current_course["room"] = room_search.group(1).strip()
                                elif len(parts) > 1:
                                    room_text = div_text.replace(inst_match, "").strip("() ").replace("أستاذ المقرر", "").strip()
                                    if room_text:
                                        current_course["room"] = room_text
                except Exception as e:
                    print(f"Error in cell parsing: {e}")

        return faculty_data
    except Exception as e:
        print(f"Error parsing faculty schedule: {e}")
        return []


# ---------------- الحفظ المباشر في Turso ----------------

def save_schedules(client, new_exams, new_faculty):
    client.execute("DELETE FROM exams")
    for ex in new_exams:
        client.execute(
            "INSERT INTO exams (code, name, exam_day, exam_period, day_index) VALUES (?, ?, ?, ?, ?)",
            [ex.get("code"), ex.get("name"), ex.get("exam_day"), ex.get("exam_period"), ex.get("day_index", 0)],
        )

    client.execute("DELETE FROM faculty")
    for f in new_faculty:
        client.execute(
            'INSERT INTO faculty (code, name, "group", day, time, instructor, room) VALUES (?, ?, ?, ?, ?, ?, ?)',
            [f.get("code"), f.get("name"), f.get("group"), f.get("day"), f.get("time"), f.get("instructor"), f.get("room")],
        )


def main():
    notify("🌐 بدأ GitHub Action سحب الجداول الآن (فتح الكروم والدخول للمنظومة)...")
    driver = None
    try:
        driver = build_chrome()
        wait = WebDriverWait(driver, 30)
        driver.get("https://sms.uot.edu.ly/eng/login_ing.php")

        fac_dropdown = wait.until(EC.element_to_be_clickable((By.ID, "fac")))
        Select(fac_dropdown).select_by_visible_text(
            "تقنية المعلومات" if MASTER_COLLEGE == "it" else "الهندسة"
        )
        driver.find_element(By.ID, "email").send_keys(MASTER_USER)
        driver.find_element(By.ID, "login-password").send_keys(MASTER_PASS)
        driver.find_element(By.NAME, "btnlogin").click()

        wait.until(EC.url_contains("student"))
        notify("✅ تم تسجيل الدخول بنجاح! جاري سحب جدول الامتحانات...")

        def open_schedule_menu():
            try:
                item = wait.until(EC.element_to_be_clickable((By.XPATH, "//a[contains(., 'الجداول')]")))
                driver.execute_script("arguments[0].click();", item)
            except Exception:
                item = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "a.nav-link.nav-schedule")))
                driver.execute_script("arguments[0].click();", item)

        open_schedule_menu()
        exam_link = wait.until(EC.element_to_be_clickable((By.XPATH, "//p[contains(text(), 'جدول الامتحانات النهائية')]")))
        driver.execute_script("arguments[0].click();", exam_link)
        wait.until(EC.presence_of_element_located((By.TAG_NAME, "tbody")))
        time.sleep(2)
        exam_data = parse_exam_schedule(driver)
        notify(f"📝 تم سحب {len(exam_data)} مادة من جدول الامتحانات.")

        open_schedule_menu()
        faculty_link = wait.until(EC.element_to_be_clickable((By.XPATH, "//p[contains(text(), 'جدول الكلية')]")))
        driver.execute_script("arguments[0].click();", faculty_link)
        wait.until(EC.presence_of_element_located((By.TAG_NAME, "table")))
        time.sleep(2)
        notify("🏫 جاري سحب جدول الكلية وتنسيق البيانات...")
        faculty_data = parse_faculty_schedule(driver, exam_data)

        client = libsql_client.create_client_sync(TURSO_DB_URL, auth_token=TURSO_AUTH_TOKEN)
        save_schedules(client, exam_data, faculty_data)
        client.close()

        notify(
            "✅ *اكتملت العملية بنجاح!*\n\n"
            f"📝 تم تحديث {len(exam_data)} مادة في جدول الامتحانات\n"
            f"🏫 تم تحديث {len(faculty_data)} محاضرة في جدول الكلية\n\n"
            "📂 الجداول محدثة الآن في Turso وجاهزة لكل الطلاب."
        )
    except Exception as e:
        extra = ""
        try:
            if driver:
                extra = f"\n🔗 الرابط وقت الخطأ: {driver.current_url}\n📄 عنوان الصفحة: {driver.title}"
        except Exception:
            pass
        notify(
            f"❌ فشل سحب الجداول عبر GitHub Actions:\n{type(e).__name__}: {str(e)[:300]}{extra}"
        )
        raise
    finally:
        if driver:
            driver.quit()


if __name__ == "__main__":
    main()
