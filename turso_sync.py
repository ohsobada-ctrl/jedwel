"""
turso_sync.py - وحدة إدارة التزامن وقاعدة بيانات Turso المشتركة بين بوتي جدوّل وتنزيل
"""

import os
import uuid
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Tuple, Any
import libsql_client
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

logger = logging.getLogger("turso_sync")
logger.setLevel(logging.INFO)

def get_turso_credentials() -> Tuple[str, str]:
    db_url = os.getenv("TURSO_DB_URL", "")
    auth_token = os.getenv("TURSO_AUTH_TOKEN", "")
    if db_url:
        db_url = db_url.replace("wss://", "https://").replace("libsql://", "https://")
    return db_url, auth_token

def get_client() -> libsql_client.Client:
    db_url, auth_token = get_turso_credentials()
    if not db_url or not auth_token:
        raise ValueError("بيانات الاتصال بقاعدة Turso غير متوفرة! يرجى ضبط TURSO_DB_URL و TURSO_AUTH_TOKEN في ملف .env")
    return libsql_client.create_client_sync(db_url, auth_token=auth_token)

def init_sync_tables():
    """تهيئة الجداول المشتركة المطلوبة: user_tokens و download_queue"""
    client = get_client()
    try:
        # 1. جدول توكنات المستخدمين
        client.execute("""
            CREATE TABLE IF NOT EXISTS user_tokens (
                user_id INTEGER PRIMARY KEY,
                token TEXT UNIQUE NOT NULL,
                expires_at DATETIME NOT NULL,
                is_active INTEGER DEFAULT 1
            );
        """)

        # 2. جدول طابور التنزيل
        client.execute("""
            CREATE TABLE IF NOT EXISTS download_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                course_code TEXT NOT NULL,
                course_name TEXT,
                group_no TEXT NOT NULL,
                priority INTEGER NOT NULL,
                status TEXT DEFAULT 'PENDING',
                last_updated DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES user_tokens(user_id)
            );
        """)

        # 3. جدول بيانات دخول المنظومة للطلاب (رقم القيد وكلمة المرور المشتركة)
        client.execute("""
            CREATE TABLE IF NOT EXISTS user_portal_creds (
                user_id INTEGER PRIMARY KEY,
                username TEXT NOT NULL,
                password TEXT NOT NULL,
                college TEXT DEFAULT 'it',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # 4. جدول إعدادات النظام وقفل التنزيل
        client.execute("""
            CREATE TABLE IF NOT EXISTS system_settings (
                setting_key TEXT PRIMARY KEY,
                setting_val TEXT NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        """)
        client.execute("INSERT OR IGNORE INTO system_settings (setting_key, setting_val, updated_at) VALUES ('enrollment_active', '1', CURRENT_TIMESTAMP);")
        client.execute("INSERT OR IGNORE INTO system_settings (setting_key, setting_val, updated_at) VALUES ('maintenance_message', 'نظام التنزيل الآلي مغلق حالياً من قبل الإدارة بانتظار إعلان موعد الفتح.', CURRENT_TIMESTAMP);")

        # فهارس لتسريع البحث والأولوية
        client.execute("CREATE INDEX IF NOT EXISTS idx_tokens_token ON user_tokens (token);")
        client.execute("CREATE INDEX IF NOT EXISTS idx_queue_user_priority ON download_queue (user_id, priority ASC);")
        client.execute("CREATE INDEX IF NOT EXISTS idx_queue_status ON download_queue (status);")
        logger.info("[TursoSync] تم التحقق من إنشاء الجداول والفهارس بنجاح.")
    finally:
        client.close()

# ----------------- عمليات التوكن (user_tokens) -----------------

def generate_user_token(user_id: int, hours: int = 12) -> Tuple[str, str]:
    """
    توليد توكن آمن ومشفر للمستخدم صالح لـ 12 ساعة (أو المدة المحددة).
    يحفظ أو يحدث التوكن للمستخدم في Turso.
    يرجع (token, expires_at_iso).
    """
    token_str = f"TKN-{uuid.uuid4().hex[:16].upper()}"
    now_utc = datetime.now(timezone.utc)
    expires_at = (now_utc + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")

    client = get_client()
    try:
        client.execute(
            """
            INSERT INTO user_tokens (user_id, token, expires_at, is_active)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(user_id) DO UPDATE SET
                token = excluded.token,
                expires_at = excluded.expires_at,
                is_active = 1;
            """,
            [user_id, token_str, expires_at]
        )
        logger.info(f"[TursoSync] تم توليد وحفظ توكن للمستخدم {user_id}: {token_str} (ينتهي في {expires_at} UTC)")
        return token_str, expires_at
    finally:
        client.close()

def verify_user_token(token: str, telegram_user_id: int) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """
    التحقق الصارم من التوكن:
    1. وجود التوكن وكونه فعال (is_active == 1)
    2. عدم انتهاء الصلاحية (expires_at > UTC now)
    3. التطابق الصارم مع معرف المستخدم (Strict Anti-Sharing Guard)
    """
    clean_token = token.strip()
    client = get_client()
    try:
        res = client.execute(
            "SELECT user_id, token, expires_at, is_active FROM user_tokens WHERE token = ?",
            [clean_token]
        )
        if not res.rows:
            return False, "TOKEN_NOT_FOUND", None

        row = res.rows[0]
        token_uid = row[0]
        token_val = row[1]
        expires_at_str = row[2]
        is_active = row[3]

        if not is_active:
            return False, "TOKEN_REVOKED", None

        try:
            exp_time = datetime.strptime(expires_at_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except Exception:
            try:
                exp_time = datetime.fromisoformat(expires_at_str).replace(tzinfo=timezone.utc)
            except Exception:
                exp_time = datetime.now(timezone.utc) - timedelta(seconds=1)

        now_utc = datetime.now(timezone.utc)
        if exp_time <= now_utc:
            return False, "TOKEN_EXPIRED", None

        if int(token_uid) != int(telegram_user_id):
            logger.warning(f"[AntiSharingGuard] محاولة استخدام توكن من مستخدم مختلف! Token owner: {token_uid}, Attempted by: {telegram_user_id}")
            return False, "USER_MISMATCH", None

        return True, "VALID", {
            "user_id": token_uid,
            "token": token_val,
            "expires_at": expires_at_str,
            "is_active": is_active
        }
    finally:
        client.close()

def revoke_user_token(user_id: int) -> bool:
    """إلغاء وإبطال صلاحية توكن المستخدم فوراً عند الحظر"""
    client = get_client()
    try:
        past_date = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        client.execute(
            "UPDATE user_tokens SET is_active = 0, expires_at = ? WHERE user_id = ?",
            [past_date, int(user_id)]
        )
        logger.info(f"[TursoSync] تم إبطال توكن المستخدم {user_id} فوراً.")
        return True
    except Exception as e:
        logger.error(f"[TursoSync] خطأ في إلغاء توكن المستخدم {user_id}: {e}")
        return False
    finally:
        client.close()

def get_active_token(user_id: int) -> Optional[Dict[str, Any]]:
    """جلب بيانات التوكن الفعال لمستخدم إن وجد وكان ساري المفعول"""
    client = get_client()
    try:
        res = client.execute(
            "SELECT user_id, token, expires_at, is_active FROM user_tokens WHERE user_id = ? AND is_active = 1",
            [user_id]
        )
        if not res.rows:
            return None
        row = res.rows[0]
        expires_at_str = row[2]
        try:
            exp_time = datetime.strptime(expires_at_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except Exception:
            exp_time = datetime.fromisoformat(expires_at_str).replace(tzinfo=timezone.utc)
        if exp_time <= datetime.now(timezone.utc):
            return None
        return {
            "user_id": row[0],
            "token": row[1],
            "expires_at": row[2],
            "is_active": row[3]
        }
    except Exception:
        return None
    finally:
        client.close()

# ----------------- عمليات طابور التنزيل (download_queue) -----------------

def get_user_queue(user_id: int) -> List[Dict[str, Any]]:
    """استرجاع قائمة المقررات في الطابور للمستخدم مرتبة بالأولوية تصاعدياً"""
    client = get_client()
    try:
        res = client.execute(
            """
            SELECT id, user_id, course_code, course_name, group_no, priority, status, last_updated
            FROM download_queue
            WHERE user_id = ?
            ORDER BY priority ASC, id ASC;
            """,
            [user_id]
        )
        items = []
        for r in res.rows:
            items.append({
                "id": r[0],
                "user_id": r[1],
                "course_code": r[2],
                "course_name": r[3] or "",
                "group_no": str(r[4]),
                "priority": r[5],
                "status": r[6],
                "last_updated": r[7]
            })
        return items
    finally:
        client.close()

def add_course_to_queue(user_id: int, course_code: str, course_name: str, group_no: str, priority: Optional[int] = None) -> int:
    """إضافة مقرر جديد لطابور المستخدم"""
    client = get_client()
    try:
        if priority is None:
            max_res = client.execute(
                "SELECT COALESCE(MAX(priority), 0) FROM download_queue WHERE user_id = ?",
                [user_id]
            )
            max_p = max_res.rows[0][0] if max_res.rows else 0
            priority = max_p + 1

        exist_res = client.execute(
            "SELECT id FROM download_queue WHERE user_id = ? AND course_code = ?",
            [user_id, course_code.strip()]
        )
        if exist_res.rows:
            q_id = exist_res.rows[0][0]
            client.execute(
                """
                UPDATE download_queue
                SET group_no = ?, course_name = ?, priority = ?, status = 'PENDING', last_updated = CURRENT_TIMESTAMP
                WHERE id = ?;
                """,
                [str(group_no), course_name, priority, q_id]
            )
            return q_id

        client.execute(
            """
            INSERT INTO download_queue (user_id, course_code, course_name, group_no, priority, status, last_updated)
            VALUES (?, ?, ?, ?, ?, 'PENDING', CURRENT_TIMESTAMP);
            """,
            [user_id, course_code.strip(), course_name.strip(), str(group_no).strip(), priority]
        )
        id_res = client.execute("SELECT last_insert_rowid();")
        return id_res.rows[0][0] if id_res.rows else 0
    finally:
        client.close()

def set_user_schedule_queue(user_id: int, courses_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """تعيين جدول كامل للمستخدم في طابور التنزيل مع الحفاظ على المواد المنزلة مسبقاً"""
    client = get_client()
    try:
        # مسح أي مواد سابقة لم تنزل بعد (مع الإبقاء على ENROLLED إن وجدت)
        client.execute(
            "DELETE FROM download_queue WHERE user_id = ? AND status != 'ENROLLED'",
            [user_id]
        )
        for idx, c in enumerate(courses_list, start=1):
            code = str(c.get("code", "")).strip().upper()
            name = str(c.get("name", "")).strip() or code
            group = str(c.get("group", "1")).strip()
            client.execute(
                """
                INSERT INTO download_queue (user_id, course_code, course_name, group_no, priority, status, last_updated)
                VALUES (?, ?, ?, ?, ?, 'READY_TO_START', CURRENT_TIMESTAMP);
                """,
                [user_id, code, name, group, idx]
            )
        return get_user_queue(user_id)
    finally:
        client.close()

def update_course_group(queue_id: int, user_id: int, new_group: str) -> bool:
    """تعديل المجموعة لمقرر في الطابور"""
    client = get_client()
    try:
        client.execute(
            "UPDATE download_queue SET group_no = ?, last_updated = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?",
            [str(new_group).strip(), queue_id, user_id]
        )
        return True
    finally:
        client.close()

def update_course_status(queue_id: int, status: str) -> bool:
    """تحديث حالة المقرر (PENDING, ENROLLED, NO_SEATS, WAITING_PORTAL, CONFLICT, ERROR, PAUSED)"""
    client = get_client()
    try:
        client.execute(
            "UPDATE download_queue SET status = ?, last_updated = CURRENT_TIMESTAMP WHERE id = ?",
            [status.strip(), queue_id]
        )
        return True
    finally:
        client.close()

def update_user_course_status(user_id: int, course_code: str, status: str) -> bool:
    """تحديث حالة مقرر معين لمستخدم عن طريق كود المقرر"""
    client = get_client()
    try:
        client.execute(
            "UPDATE download_queue SET status = ?, last_updated = CURRENT_TIMESTAMP WHERE user_id = ? AND course_code = ?",
            [status.strip(), user_id, course_code.strip()]
        )
        return True
    finally:
        client.close()

def delete_from_queue(queue_id: int, user_id: int) -> bool:
    """حذف مقرر من طابور التنزيل وإعادة ترقيم الأولويات"""
    client = get_client()
    try:
        client.execute("DELETE FROM download_queue WHERE id = ? AND user_id = ?", [queue_id, user_id])
        res = client.execute(
            "SELECT id FROM download_queue WHERE user_id = ? ORDER BY priority ASC, id ASC",
            [user_id]
        )
        for idx, r in enumerate(res.rows, start=1):
            client.execute("UPDATE download_queue SET priority = ? WHERE id = ?", [idx, r[0]])
        return True
    finally:
        client.close()

def move_priority(user_id: int, queue_id: int, direction: str = "UP") -> bool:
    """تحريك أولوية مادة للأعلى أو للأسفل مباشرة مع تبادل الأولوية مع العنصر المجاور"""
    items = get_user_queue(user_id)
    if not items:
        return False

    target_idx = None
    for i, it in enumerate(items):
        if it["id"] == queue_id:
            target_idx = i
            break

    if target_idx is None:
        return False

    swap_idx = target_idx - 1 if direction.upper() == "UP" else target_idx + 1
    if swap_idx < 0 or swap_idx >= len(items):
        return False

    curr_item = items[target_idx]
    other_item = items[swap_idx]

    client = get_client()
    try:
        client.execute("UPDATE download_queue SET priority = ? WHERE id = ?", [other_item["priority"], curr_item["id"]])
        client.execute("UPDATE download_queue SET priority = ? WHERE id = ?", [curr_item["priority"], other_item["id"]])
        return True
    finally:
        client.close()

def toggle_queue_pause(user_id: int, pause: bool) -> int:
    """إيقاف مؤقت أو استئناف للتنزيل"""
    client = get_client()
    try:
        if pause:
            client.execute(
                """
                UPDATE download_queue
                SET status = 'PAUSED', last_updated = CURRENT_TIMESTAMP
                WHERE user_id = ? AND status IN ('PENDING', 'NO_SEATS', 'WAITING_PORTAL');
                """,
                [user_id]
            )
        else:
            client.execute(
                """
                UPDATE download_queue
                SET status = 'PENDING', last_updated = CURRENT_TIMESTAMP
                WHERE user_id = ? AND status = 'PAUSED';
                """,
                [user_id]
            )
        return True
    finally:
        client.close()

def get_unfinished_tasks_for_recovery() -> List[Dict[str, Any]]:
    """جلب كافة المهام غير المكتملة لجميع المستخدمين الذين يملكون توكن ساري المفعول"""
    client = get_client()
    try:
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        res = client.execute(
            """
            SELECT q.id, q.user_id, q.course_code, q.course_name, q.group_no, q.priority, q.status
            FROM download_queue q
            JOIN user_tokens t ON q.user_id = t.user_id
            WHERE t.is_active = 1 AND t.expires_at > ?
              AND q.status IN ('PENDING', 'WAITING_PORTAL', 'NO_SEATS')
            ORDER BY q.user_id ASC, q.priority ASC;
            """,
            [now_str]
        )
        tasks = []
        for r in res.rows:
            tasks.append({
                "id": r[0],
                "user_id": r[1],
                "course_code": r[2],
                "course_name": r[3] or "",
                "group_no": str(r[4]),
                "priority": r[5],
                "status": r[6]
            })
        return tasks
    finally:
        client.close()

# ----------------- بيانات دخول المنظومة للطلاب (رقم القيد وكلمة المرور) -----------------

def save_user_portal_credentials(user_id: int, username: str, password: str, college: str = "it") -> bool:
    """حفظ أو تحديث بيانات دخول الطالب لمنظومة الجامعة في Turso"""
    client = get_client()
    try:
        client.execute("""
            INSERT INTO user_portal_creds (user_id, username, password, college, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                password = excluded.password,
                college = excluded.college,
                updated_at = CURRENT_TIMESTAMP;
        """, [user_id, username.strip(), password.strip(), college.strip()])
        logger.info(f"[TursoSync] تم حفظ بيانات منظومة الجامعة للمستخدم {user_id} بنجاح.")
        return True
    finally:
        client.close()

def get_user_portal_credentials(user_id: int) -> Optional[Dict[str, Any]]:
    """جلب بيانات دخول المنظومة للمستخدم إن وجدت"""
    client = get_client()
    try:
        res = client.execute(
            "SELECT user_id, username, password, college FROM user_portal_creds WHERE user_id = ?",
            [user_id]
        )
        if not res.rows:
            return None
        r = res.rows[0]
        return {
            "user_id": r[0],
            "username": r[1],
            "password": r[2],
            "college": r[3] or "it"
        }
    finally:
        client.close()

def has_user_portal_credentials(user_id: int) -> bool:
    """التحقق السريع مما إذا كان الطالب قد أدخل بيانات دخوله سابقاً"""
    creds = get_user_portal_credentials(user_id)
    return bool(creds and creds.get("username") and creds.get("password"))

# ----------------- إعدادات النظام وقفل التنزيل العام -----------------

def get_system_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    """جلب قيمة إعداد من إعدادات النظام"""
    client = get_client()
    try:
        res = client.execute("SELECT setting_val FROM system_settings WHERE setting_key = ?", [key])
        if res.rows:
            return res.rows[0][0]
        return default
    except Exception:
        return default
    finally:
        client.close()

def set_system_setting(key: str, val: str) -> bool:
    """تعيين أو تحديث إعداد في النظام العام"""
    client = get_client()
    try:
        client.execute("""
            INSERT INTO system_settings (setting_key, setting_val, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(setting_key) DO UPDATE SET
                setting_val = excluded.setting_val,
                updated_at = CURRENT_TIMESTAMP;
        """, [key, str(val)])
        return True
    finally:
        client.close()

def is_enrollment_open() -> Tuple[bool, str]:
    """فحص ما إذا كان نظام التنزيل مفتوحاً من قبل الإدارة، وإرجاع رسالة الإغلاق إن كان مقفلاً"""
    active = get_system_setting("enrollment_active", "1")
    msg = get_system_setting("maintenance_message", "نظام التنزيل الآلي مغلق حالياً من قبل الإدارة بانتظار إعلان موعد الفتح.")
    return (active == "1"), (msg or "نظام التنزيل مغلق حالياً.")

def start_user_queue_enrollment(user_id: int) -> List[Dict[str, Any]]:
    """تفعيل طابور التنزيل للمستخدم بعد موافقته الصريحة (تحويل READY_TO_START إلى PENDING)"""
    client = get_client()
    try:
        client.execute(
            "UPDATE download_queue SET status = 'PENDING', last_updated = CURRENT_TIMESTAMP WHERE user_id = ? AND status = 'READY_TO_START'",
            [user_id]
        )
        return get_user_queue(user_id)
    finally:
        client.close()

def pause_all_active_tasks() -> int:
    """إيقاف طوارئ لكافة المواد النشطة في الطابور لجميع المستخدمين"""
    client = get_client()
    try:
        res = client.execute(
            "UPDATE download_queue SET status = 'PAUSED', last_updated = CURRENT_TIMESTAMP WHERE status IN ('PENDING', 'WAITING_PORTAL', 'NO_SEATS')"
        )
        return res.rows_affected if hasattr(res, 'rows_affected') else 1
    finally:
        client.close()

def resume_all_paused_tasks() -> int:
    """استئناف كافة المواد المتوقفة مؤقتاً في الطابور لجميع المستخدمين"""
    client = get_client()
    try:
        res = client.execute(
            "UPDATE download_queue SET status = 'PENDING', last_updated = CURRENT_TIMESTAMP WHERE status = 'PAUSED'"
        )
        return res.rows_affected if hasattr(res, 'rows_affected') else 1
    finally:
        client.close()
