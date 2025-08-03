#!/usr/bin/env python3
"""
SSH Terminal Telegram Bot with PostgreSQL and Premium System
Telegram Stars Payment Integration
"""
import logging
import paramiko
import psycopg2
import threading
import os
import re
import json
import nest_asyncio
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler, PreCheckoutQueryHandler
from cryptography.fernet import Fernet

# Enable nested event loops
nest_asyncio.apply()

# Konfiguratsiya
BOT_TOKEN = "8238940495:AAFdcm-MWKQKzNw8qXxeerdFpNPU01AAsec"  # Bot token

# Admin ID - BU YERNI O'ZGARTIRING
ADMIN_IDS = [8162058247]  # O'zingizning Telegram ID raqamingizni kiriting

# PostgreSQL konfiguratsiyasi - BU YERNI O'ZGARTIRING
DB_CONFIG = {
    'host': 'postgresql-komp.alwaysdata.net',
    'database': 'komp_sshbot',
    'user': 'komp',
    'password': 'komp@#12',
    'port': 5432
}

SESSION_TIMEOUT = 3600

# Shifrlash
ENCRYPTION_KEY = os.environ.get('ENCRYPTION_KEY', Fernet.generate_key().decode())
fernet = Fernet(ENCRYPTION_KEY.encode())

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

ssh_sessions = {}
session_timers = {}
processed_messages = set()  # To prevent duplicate message processing
processed_commands = set()  # To prevent duplicate command processing

def deduplicate_command(func):
    """Decorator to prevent duplicate command processing"""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        message_id = update.message.message_id
        user_id = update.effective_user.id
        command_key = f"{user_id}_{message_id}"
        if command_key in processed_commands:
            return
        processed_commands.add(command_key)
        # Clean up old processed commands (keep only last 1000)
        if len(processed_commands) > 1000:
            processed_commands.clear()
        return await func(update, context)
    return wrapper

def parse_tariff_arguments(args):
    """
    Parse tariff arguments from command line arguments.
    Handles both quoted and unquoted multi-word arguments.
    Expected format: name description price duration max_servers max_commands
    Where name and description can be multi-word (quoted or unquoted)
    """
    if len(args) < 6:
        return None, "Not enough arguments"
    # Try to find the last 4 numeric arguments from the end
    numeric_args = []
    text_args = []
    # Start from the end and work backwards to find the 4 numeric arguments
    for i in range(len(args) - 1, -1, -1):
        try:
            num = int(args[i])
            numeric_args.insert(0, num)
            if len(numeric_args) == 4:
                # Found all 4 numeric arguments, everything before this is text
                text_args = args[:i]
                break
        except ValueError:
            # Not a number, continue searching
            continue
    if len(numeric_args) != 4:
        return None, f"Could not find 4 numeric arguments. Found: {numeric_args}"
    if len(text_args) < 2:
        return None, "Need at least name and description"
    # The first text argument is the name, the rest is the description
    name = text_args[0]
    description = " ".join(text_args[1:])
    price_stars, duration_days, max_servers, max_commands = numeric_args
    return {
        'name': name,
        'description': description,
        'price_stars': price_stars,
        'duration_days': duration_days,
        'max_servers': max_servers,
        'max_commands': max_commands
    }, None

class DatabaseManager:
    def __init__(self):
        self.init_database()

    def get_connection(self):
        """PostgreSQL ga ulanish"""
        return psycopg2.connect(**DB_CONFIG)

    def init_database(self):
        """Ma'lumotlar bazasini yaratish"""
        conn = None
        cursor = None
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            # Create users table if not exists
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    username VARCHAR(255),
                    first_name VARCHAR(255),
                    last_name VARCHAR(255),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            conn.commit()
            # Add premium columns if they don't exist - use separate connections to avoid transaction issues
            try:
                temp_conn = self.get_connection()
                temp_cursor = temp_conn.cursor()
                temp_cursor.execute('ALTER TABLE users ADD COLUMN is_premium BOOLEAN DEFAULT FALSE')
                temp_conn.commit()
                temp_cursor.close()
                temp_conn.close()
            except Exception as e:
                # Column already exists or other error
                logger.info(f"is_premium column check: {e}")
                if temp_conn:
                    temp_conn.rollback()
                    temp_conn.close()
            try:
                temp_conn = self.get_connection()
                temp_cursor = temp_conn.cursor()
                temp_cursor.execute('ALTER TABLE users ADD COLUMN premium_expires TIMESTAMP')
                temp_conn.commit()
                temp_cursor.close()
                temp_conn.close()
            except Exception as e:
                # Column already exists or other error
                logger.info(f"premium_expires column check: {e}")
                if temp_conn:
                    temp_conn.rollback()
                    temp_conn.close()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS ssh_hosts (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    name VARCHAR(255),
                    hostname VARCHAR(255),
                    port INTEGER DEFAULT 22,
                    username VARCHAR(255),
                    password TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users (user_id)
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS premium_tariffs (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    description TEXT,
                    price_stars INTEGER NOT NULL,
                    duration_days INTEGER NOT NULL,
                    max_servers INTEGER DEFAULT 5,
                    max_commands_per_day INTEGER DEFAULT 100,
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS user_payments (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    tariff_id INTEGER,
                    amount_stars INTEGER,
                    payment_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    status VARCHAR(50) DEFAULT 'pending',
                    FOREIGN KEY (user_id) REFERENCES users (user_id),
                    FOREIGN KEY (tariff_id) REFERENCES premium_tariffs (id)
                )
            ''')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_ssh_hosts_user_id ON ssh_hosts (user_id)
            ''')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_user_payments_user_id ON user_payments (user_id)
            ''')
            conn.commit()
            logger.info("Ma'lumotlar bazasi muvaffaqiyatli sozlandi")
        except Exception as e:
            logger.error(f"Ma'lumotlar bazasini sozlashda xatolik: {e}")
            if conn:
                try:
                    conn.rollback()
                except:
                    pass
            raise
        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()

    def save_user(self, user_id, username, first_name, last_name):
        """Foydalanuvchini saqlash"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO users (user_id, username, first_name, last_name)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id) 
                DO UPDATE SET 
                    username = EXCLUDED.username,
                    first_name = EXCLUDED.first_name,
                    last_name = EXCLUDED.last_name
            ''', (user_id, username, first_name, last_name))
            conn.commit()
            cursor.close()
            conn.close()
        except Exception as e:
            logger.error(f"Foydalanuvchini saqlashda xatolik: {e}")
            raise

    def add_ssh_host(self, user_id, name, hostname, port, username, password):
        """SSH host qo'shish (parolni shifrlab)"""
        try:
            # Premium tekshirish
            if not self.is_premium_user(user_id):
                server_count = self.get_user_server_count(user_id)
                if server_count >= 3:  # Free foydalanuvchilar uchun 3 ta server
                    raise Exception("Free foydalanuvchilar uchun maksimal 3 ta server ruxsat etilgan. Premium tarifga o'ting.")
            encrypted_password = fernet.encrypt(password.encode()).decode()
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO ssh_hosts (user_id, name, hostname, port, username, password)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
            ''', (user_id, name, hostname, port, username, encrypted_password))
            host_id = cursor.fetchone()[0]
            conn.commit()
            cursor.close()
            conn.close()
            return host_id
        except Exception as e:
            logger.error(f"SSH host qo'shishda xatolik: {e}")
            raise

    def get_ssh_hosts(self, user_id):
        """Foydalanuvchining SSH hostlarini olish"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                SELECT id, name, hostname, port, username FROM ssh_hosts
                WHERE user_id = %s
                ORDER BY created_at DESC
            ''', (user_id,))
            hosts = cursor.fetchall()
            cursor.close()
            conn.close()
            return hosts
        except Exception as e:
            logger.error(f"SSH hostlarni olishda xatolik: {e}")
            raise

    def get_ssh_host(self, user_id, host_id):
        """Muayyan SSH hostni olish (parolni deshifrlab)"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                SELECT hostname, port, username, password FROM ssh_hosts
                WHERE user_id = %s AND id = %s
            ''', (user_id, host_id))
            result = cursor.fetchone()
            cursor.close()
            conn.close()
            if result:
                hostname, port, username, encrypted_password = result
                password = fernet.decrypt(encrypted_password.encode()).decode()
                return hostname, port, username, password
            return None
        except Exception as e:
            logger.error(f"SSH hostni olishda xatolik: {e}")
            raise

    def is_premium_user(self, user_id):
        """Foydalanuvchining premium statusini tekshirish"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                SELECT is_premium, premium_expires FROM users
                WHERE user_id = %s
            ''', (user_id,))
            result = cursor.fetchone()
            cursor.close()
            conn.close()
            if result:
                is_premium, expires = result
                if is_premium and expires:
                    from datetime import datetime
                    return datetime.now() < expires
            return False
        except Exception as e:
            logger.error(f"Premium statusni tekshirishda xatolik: {e}")
            return False

    def get_user_server_count(self, user_id):
        """Foydalanuvchining serverlar sonini olish"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                SELECT COUNT(*) FROM ssh_hosts WHERE user_id = %s
            ''', (user_id,))
            count = cursor.fetchone()[0]
            cursor.close()
            conn.close()
            return count
        except Exception as e:
            logger.error(f"Serverlar sonini olishda xatolik: {e}")
            return 0

    def get_premium_tariffs(self):
        """Faol premium tariflarni olish"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                SELECT id, name, description, price_stars, duration_days, max_servers, max_commands_per_day
                FROM premium_tariffs
                WHERE is_active = TRUE
                ORDER BY price_stars ASC
            ''')
            tariffs = cursor.fetchall()
            cursor.close()
            conn.close()
            return tariffs
        except Exception as e:
            logger.error(f"Premium tariflarni olishda xatolik: {e}")
            return []

    def get_tariff_by_id(self, tariff_id):
        """Tarifni ID bo'yicha olish"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                SELECT id, name, description, price_stars, duration_days, max_servers, max_commands_per_day
                FROM premium_tariffs
                WHERE id = %s AND is_active = TRUE
            ''', (tariff_id,))
            tariff = cursor.fetchone()
            cursor.close()
            conn.close()
            return tariff
        except Exception as e:
            logger.error(f"Tarifni olishda xatolik: {e}")
            return None

    def add_premium_tariff(self, name, description, price_stars, duration_days, max_servers, max_commands_per_day):
        """Yangi premium tarif qo'shish"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO premium_tariffs (name, description, price_stars, duration_days, max_servers, max_commands_per_day)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
            ''', (name, description, price_stars, duration_days, max_servers, max_commands_per_day))
            tariff_id = cursor.fetchone()[0]
            conn.commit()
            cursor.close()
            conn.close()
            return tariff_id
        except Exception as e:
            logger.error(f"Premium tarif qo'shishda xatolik: {e}")
            raise

    def update_premium_tariff(self, tariff_id, name, description, price_stars, duration_days, max_servers, max_commands_per_day):
        """Premium tarifni yangilash"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE premium_tariffs
                SET name = %s, description = %s, price_stars = %s, duration_days = %s, 
                    max_servers = %s, max_commands_per_day = %s
                WHERE id = %s
            ''', (name, description, price_stars, duration_days, max_servers, max_commands_per_day, tariff_id))
            conn.commit()
            cursor.close()
            conn.close()
        except Exception as e:
            logger.error(f"Premium tarifni yangilashda xatolik: {e}")
            raise

    def delete_premium_tariff(self, tariff_id):
        """Premium tarifni o'chirish"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE premium_tariffs SET is_active = FALSE WHERE id = %s
            ''', (tariff_id,))
            conn.commit()
            cursor.close()
            conn.close()
        except Exception as e:
            logger.error(f"Premium tarifni o'chirishda xatolik: {e}")
            raise

    def activate_premium(self, user_id, tariff_id):
        """Foydalanuvchiga premium aktivlashtirish"""
        try:
            tariff = self.get_tariff_by_id(tariff_id)
            if not tariff:
                raise Exception("Tarif topilmadi")
            from datetime import datetime, timedelta
            expires = datetime.now() + timedelta(days=tariff[4])  # duration_days
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE users
                SET is_premium = TRUE, premium_expires = %s
                WHERE user_id = %s
            ''', (expires, user_id))
            conn.commit()
            cursor.close()
            conn.close()
        except Exception as e:
            logger.error(f"Premium aktivlashtirishda xatolik: {e}")
            raise

class SSHClient:
    def __init__(self, hostname, port, username, password):
        self.hostname = hostname
        self.port = port
        self.username = username
        self.password = password
        self.client = None
        self.connected = False

    def connect(self):
        """SSH ga ulanish"""
        try:
            self.client = paramiko.SSHClient()
            self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            self.client.connect(
                hostname=self.hostname,
                port=self.port,
                username=self.username,
                password=self.password,
                timeout=10,
                look_for_keys=False,
                allow_agent=False
            )
            self.connected = True
            return True, "✅ Ulanish muvaffaqiyatli amalga oshdi"
        except Exception as e:
            return False, f"❌ Ulanishda xatolik: {str(e)}"

    def execute_command(self, command):
        """Buyruq bajarish"""
        if not self.connected or not self.client:
            return False, "⚠️ SSH ulanish mavjud emas"
        try:
            stdin, stdout, stderr = self.client.exec_command(command, timeout=30)
            output = stdout.read().decode('utf-8', errors='ignore')
            error = stderr.read().decode('utf-8', errors='ignore')
            if error:
                return True, f"📤 STDOUT:\n{output}\n⚠️ STDERR:\n{error}"
            else:
                return True, output if output else "✅ Buyruq bajarildi (chiqish yo'q)"
        except Exception as e:
            return False, f"❌ Buyruq bajarishda xatolik: {str(e)}"

    def disconnect(self):
        """Ulanishni uzish"""
        if self.client:
            self.client.close()
        self.connected = False

def parse_ssh_string(ssh_string):
    """SSH manzilni parse qilish: komp@ssh-komp.alwaysdata.net"""
    pattern = r'^([^@]+)@([^:]+)(?::(\d+))?$'
    match = re.match(pattern, ssh_string)
    if match:
        username = match.group(1)
        hostname = match.group(2)
        port = int(match.group(3)) if match.group(3) else 22
        return username, hostname, port
    return None

@deduplicate_command
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start komandasi"""
    user = update.effective_user
    try:
        db_manager.save_user(user.id, user.username, user.first_name, user.last_name)
    except Exception as e:
        logger.error(f"Foydalanuvchini saqlashda xatolik: {e}")
    # Premium statusni tekshirish
    is_premium = db_manager.is_premium_user(user.id)
    premium_status = "⭐ Premium" if is_premium else "🆓 Free"
    keyboard = [
        [KeyboardButton("➕ Server qo'shish"), KeyboardButton("📋 Serverlar")],
        [KeyboardButton("⭐ Premium"), KeyboardButton("❓ Yordam")],
        [KeyboardButton("ℹ️ Bot haqida")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    welcome_text = f'''
👋 Assalomu alaykum, {user.first_name}!
🔐 SSH Terminal Botga xush kelibsiz!
Bu bot orqali siz SSH orqali serverlaringizni Telegramdan chiqmasdan boshqarishingiz mumkin.
👤 Status: {premium_status}
📋 Mavjud tugmalar:
• ➕ Server qo'shish - Yangi SSH server qo'shish
• 📋 Serverlar - Saqlangan serverlar ro'yxati
• ⭐ Premium - Premium tariflar va xususiyatlar
• ❓ Yordam - Batafsil yordam
• ℹ️ Bot haqida - Loyiha haqida ma'lumot
'''
    await update.message.reply_text(welcome_text, reply_markup=reply_markup)

@deduplicate_command
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Yordam komandasi"""
    help_text = '''
🤖 SSH Terminal Bot Yordami
📌 Asosiy buyruqlar:
/addhost - Yangi SSH server qo'shish
/listhosts - Saqlangan serverlar ro'yxati
/connect [id] - Serverga ulanish
/terminal [buyruq] - Buyruq bajarish
/disconnect - Ulanishni uzish
/premium - Premium tariflar
/help - Ushbu yordam
/about - Bot haqida
🔧 Foydalanish tartibi:
1. /addhost orqali server qo'shing
2. /listhosts orqali serverlarni ko'ring
3. /connect [id] orqali ulaning
4. /terminal [buyruq] orqali buyruq bajarib turing
5. /disconnect orqali uziling
📄 SSH manzil formatlari:
• oddiy: komp@ssh-komp.alwaysdata.net
• port bilan: komp@ssh-komp.alwaysdata.net:2222
• batafsil: nom|host|port|foydalanuvchi|parol
⭐ Premium xususiyatlar:
• Cheksiz serverlar qo'shish
• Cheksiz buyruqlar bajarish
• Yuqori tezlik
• Maxsus yordam
'''
    await update.message.reply_text(help_text)

@deduplicate_command
async def about_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bot haqida ma'lumot"""
    about_text = '''
🚀 SSH Terminal Telegram Bot
🎯 Maqsad:
Telegram orqali SSH serverlarni boshqarish
👨‍💻 Ishlab chiquvchi: Siz
🔧 Texnologiyalar:
• Python 3
• Telegram Bot API
• Paramiko (SSH)
• PostgreSQL
• Cryptography
📦 Xususiyatlar:
• Bir nechta serverlarni boshqarish
• Xavfsiz parol saqlash
• Real-time buyruq bajarish
• Avtomatik sessiya timeout
• Premium tizim
'''
    await update.message.reply_text(about_text)

@deduplicate_command
async def premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Premium tariflar"""
    user = update.effective_user
    is_premium = db_manager.is_premium_user(user.id)
    if is_premium:
        # Premium foydalanuvchi uchun ma'lumot
        premium_text = '''
⭐ Siz Premium foydalanuvchisiz!
🎉 Premium xususiyatlar:
• Cheksiz serverlar qo'shish
• Cheksiz buyruqlar bajarish
• Yuqori tezlik
• Maxsus yordam
• Yangi xususiyatlar birinchi navbatda
📅 Premium muddati: Faol
'''
        await update.message.reply_text(premium_text)
    else:
        # Free foydalanuvchi uchun tariflar
        tariffs = db_manager.get_premium_tariffs()
        if not tariffs:
            await update.message.reply_text("❌ Hozirda mavjud tariflar yo'q.")
            return
        keyboard = []
        for tariff in tariffs:
            tariff_id, name, description, price_stars, duration_days, max_servers, max_commands = tariff
            button_text = f"⭐ {name} - {price_stars} ⭐"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=f"tariff_{tariff_id}")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        premium_text = '''
⭐ Premium Tariflar
🚀 Premium xususiyatlar:
• Cheksiz serverlar qo'shish
• Cheksiz buyruqlar bajarish
• Yuqori tezlik
• Maxsus yordam
• Yangi xususiyatlar birinchi navbatda
📋 Mavjud tariflar:
'''
        await update.message.reply_text(premium_text, reply_markup=reply_markup)

async def handle_tariff_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tarif tanlash"""
    query = update.callback_query
    await query.answer()
    tariff_id = int(query.data.split('_')[1])
    tariff = db_manager.get_tariff_by_id(tariff_id)
    if not tariff:
        await query.edit_message_text("❌ Tarif topilmadi.")
        return
    tariff_id, name, description, price_stars, duration_days, max_servers, max_commands = tariff
    tariff_info = f'''
⭐ {name}
📄 Tavsif:
{description}
💰 Narxi: {price_stars} ⭐
⏱ Muddati: {duration_days} kun
🖥️ Maksimal serverlar: {max_servers} ta
⚡ Kunlik buyruqlar: {max_commands} ta
🎁 Xususiyatlar:
• Cheksiz serverlar qo'shish
• Cheksiz buyruqlar bajarish
• Yuqori tezlik
• Maxsus yordam
'''
    
    # To'lov uchun noyob payload yaratamiz
    payload_data = {
        'user_id': query.from_user.id,
        'tariff_id': tariff_id
    }
    payload = json.dumps(payload_data) # payload string bo'lishi kerak

    # Invoice yuborish
    try:
        await context.bot.send_invoice(
            chat_id=query.from_user.id,
            title=name,
            description=description,
            payload=payload, # Bu to'lovni aniqlash uchun
            provider_token="", # Telegram Stars uchun bo'sh
            currency="XTR", # Telegram Stars
            prices=[LabeledPrice(label=name, amount=price_stars)], # amount Starsda bo'lishi kerak
            start_parameter=f"premium_tariff_{tariff_id}" # Deep linking uchun
        )
        await query.edit_message_text("💳 To'lov interfeysi ochilmoqda...")
    except Exception as e:
        logger.error(f"Invoice yuborishda xatolik: {e}")
        await query.edit_message_text("❌ To'lov interfeysi ochishda xatolik yuz berdi. Iltimos, qaytadan urinib ko'ring.")

async def handle_precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """To'lovdan oldin tekshirish"""
    query = update.pre_checkout_query
    # Oddiy holatda to'lovni tasdiqlaymiz
    # Murakkabroq logika (masalan, foydalanuvchi cheklangan bo'lsa) ham qo'shilishi mumkin
    await query.answer(ok=True)

async def handle_successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """To'lov muvaffaqiyatli amalga oshgach"""
    user_id = update.effective_user.id
    payment = update.message.successful_payment
    payload_str = payment.invoice_payload

    try:
        # payload ni JSON ga aylantiramiz
        payload_data = json.loads(payload_str)
        tariff_id = payload_data['tariff_id']
        paid_user_id = payload_data['user_id']

        # Xavfsizlik uchun, to'lov qilgan user_id biz kutganimizdirmi tekshiramiz
        if paid_user_id != user_id:
             logger.warning(f"User ID mismatch in payment. Paid by {user_id}, expected {paid_user_id}")
             await update.message.reply_text("To'lovda xatolik yuz berdi. Iltimos, qaytadan urinib ko'ring.")
             return

        # Premiumni faollashtirish
        db_manager.activate_premium(user_id, tariff_id)

        # Foydalanuvchiga xabar berish
        await update.message.reply_text("✅ To'lov qabul qilindi! Sizning Premium tarifingiz faollashtirildi!")

        # (Ixtiyoriy) Adminlarga xabar yuborish
        # for admin_id in ADMIN_IDS:
        #     try:
        #         await context.bot.send_message(chat_id=admin_id, text=f"Foydalanuvchi {user_id} premium sotib oldi (Tarif ID: {tariff_id})")
        #     except Exception as e:
        #         logger.error(f"Admin xabarini yuborishda xatolik: {e}")

    except Exception as e:
        logger.error(f"To'lovni qayta ishlashda xatolik: {e}")
        await update.message.reply_text("❌ To'lovni qayta ishlashda xatolik yuz berdi. Iltimos, admin bilan bog'laning.")

async def handle_back_to_tariffs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tariflar ro'yxatiga qaytish"""
    query = update.callback_query
    await query.answer()
    tariffs = db_manager.get_premium_tariffs()
    if not tariffs:
        await query.edit_message_text("❌ Hozirda mavjud tariflar yo'q.")
        return
    keyboard = []
    for tariff in tariffs:
        tariff_id, name, description, price_stars, duration_days, max_servers, max_commands = tariff
        button_text = f"⭐ {name} - {price_stars} ⭐"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"tariff_{tariff_id}")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    premium_text = '''
⭐ Premium Tariflar
🚀 Premium xususiyatlar:
• Cheksiz serverlar qo'shish
• Cheksiz buyruqlar bajarish
• Yuqori tezlik
• Maxsus yordam
• Yangi xususiyatlar birinchi navbatda
📋 Mavjud tariflar:
'''
    await query.edit_message_text(premium_text, reply_markup=reply_markup)

# Admin buyruqlari
@deduplicate_command
async def admin_add_tariff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: Yangi tarif qo'shish"""
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Bu buyruq faqat admin uchun.")
        return
    if len(context.args) < 6:
        await update.message.reply_text('''
📄 Yangi tarif qo'shish
Format: /addtariff nomi tavsif narx kunlar serverlar buyruqlar
Misol: /addtariff "Premium" "Eng yaxshi tarif" 100 30 10 500
Yoki: /addtariff Premium super tarif 100 30 10 500
''')
        return
    try:
        # Debug: Print received arguments
        logger.info(f"Received args: {context.args}")
        # Use the new parsing function
        parsed_args, error = parse_tariff_arguments(context.args)
        if error:
            await update.message.reply_text(f"❌ Xatolik: {error}")
            return
        # Debug: Print parsed values
        logger.info(f"Parsed args: {parsed_args}")
        tariff_id = db_manager.add_premium_tariff(
            parsed_args['name'], 
            parsed_args['description'], 
            parsed_args['price_stars'], 
            parsed_args['duration_days'], 
            parsed_args['max_servers'], 
            parsed_args['max_commands']
        )
        success_text = f'''
✅ Yangi tarif qo'shildi!
ID: {tariff_id}
Nomi: {parsed_args['name']}
Tavsif: {parsed_args['description']}
Narxi: {parsed_args['price_stars']} ⭐
Muddati: {parsed_args['duration_days']} kun
Serverlar: {parsed_args['max_servers']} ta
Buyruqlar: {parsed_args['max_commands']} ta/kun
'''
        await update.message.reply_text(success_text)
    except Exception as e:
        logger.error(f"Exception in admin_add_tariff: {e}")
        await update.message.reply_text(f"❌ Xatolik: {str(e)}")

@deduplicate_command
async def admin_edit_tariff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: Tarifni tahrirlash"""
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Bu buyruq faqat admin uchun.")
        return
    if len(context.args) < 7:
        await update.message.reply_text('''
📄 Tarifni tahrirlash
Format: /edittariff ID nomi tavsif narx kunlar serverlar buyruqlar
Misol: /edittariff 1 "Premium+" "Yaxshilangan tarif" 150 30 15 750
Yoki: /edittariff 1 Premium+ Yaxshilangan tarif 150 30 15 750
''')
        return
    try:
        tariff_id = int(context.args[0])
        # Parse the remaining arguments using the same function
        remaining_args = context.args[1:]
        parsed_args, error = parse_tariff_arguments(remaining_args)
        if error:
            await update.message.reply_text(f"❌ Xatolik: {error}")
            return
        db_manager.update_premium_tariff(
            tariff_id, 
            parsed_args['name'], 
            parsed_args['description'], 
            parsed_args['price_stars'], 
            parsed_args['duration_days'], 
            parsed_args['max_servers'], 
            parsed_args['max_commands']
        )
        success_text = f'''
✅ Tarif yangilandi!
ID: {tariff_id}
Nomi: {parsed_args['name']}
Tavsif: {parsed_args['description']}
Narxi: {parsed_args['price_stars']} ⭐
Muddati: {parsed_args['duration_days']} kun
Serverlar: {parsed_args['max_servers']} ta
Buyruqlar: {parsed_args['max_commands']} ta/kun
'''
        await update.message.reply_text(success_text)
    except ValueError:
        await update.message.reply_text("❌ Noto'g'ri format. Tarif ID raqam bo'lishi kerak.")
    except Exception as e:
        await update.message.reply_text(f"❌ Xatolik: {str(e)}")

@deduplicate_command
async def admin_delete_tariff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: Tarifni o'chirish"""
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Bu buyruq faqat admin uchun.")
        return
    if not context.args:
        await update.message.reply_text("❌ Tarif ID sini kiriting.\nMisol: /deletetariff 1")
        return
    try:
        tariff_id = int(context.args[0])
        db_manager.delete_premium_tariff(tariff_id)
        await update.message.reply_text(f"✅ Tarif ID {tariff_id} o'chirildi.")
    except ValueError:
        await update.message.reply_text("❌ ID raqam bo'lishi kerak.")
    except Exception as e:
        await update.message.reply_text(f"❌ Xatolik: {str(e)}")

@deduplicate_command
async def admin_list_tariffs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: Barcha tariflarni ko'rish"""
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Bu buyruq faqat admin uchun.")
        return
    try:
        tariffs = db_manager.get_premium_tariffs()
        if not tariffs:
            await update.message.reply_text("📭 Hozirda mavjud tariflar yo'q.")
            return
        tariffs_text = "📋 Barcha faol tariflar:\n"
        for tariff in tariffs:
            tariff_id, name, description, price_stars, duration_days, max_servers, max_commands = tariff
            tariffs_text += f"ID: {tariff_id}\n"
            tariffs_text += f"Nomi: {name}\n"
            tariffs_text += f"Tavsif: {description}\n"
            tariffs_text += f"Narxi: {price_stars} ⭐\n"
            tariffs_text += f"Muddati: {duration_days} kun\n"
            tariffs_text += f"Serverlar: {max_servers} ta\n"
            tariffs_text += f"Buyruqlar: {max_commands} ta/kun\n"
            tariffs_text += "─" * 30 + "\n"
        await update.message.reply_text(tariffs_text)
    except Exception as e:
        await update.message.reply_text(f"❌ Xatolik: {str(e)}")

@deduplicate_command
async def admin_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin yordam"""
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Bu buyruq faqat admin uchun.")
        return
    admin_help_text = '''
👨‍💼 Admin Buyruqlari
📄 Tariflar boshqaruvi:
/addtariff "Nomi" "Tavsif" narx kunlar serverlar buyruqlar
/edittariff ID "Nomi" "Tavsif" narx kunlar serverlar buyruqlar
/deletetariff ID
/listtariffs
💰 Misollar:
/addtariff "Premium" "Eng yaxshi tarif" 100 30 10 500
/edittariff 1 "Premium+" "Yaxshilangan" 150 30 15 750
'''
    await update.message.reply_text(admin_help_text)

@deduplicate_command
async def add_host(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """SSH host qo'shish"""
    instruction_text = '''
➕ Yangi SSH server qo'shish
📌 2 xil formatda kiritishingiz mumkin:
1️⃣ Oddiy format (SSH manzil):
<foydalanuvchi@host[:port]>
Misol: komp@ssh-komp.alwaysdata.net
2️⃣ Batafsil format:
<nom|host|port|foydalanuvchi|parol>
Misol: MeningServer|192.168.1.100|22|root|mypassword
ℹ️ Portni o'tkazib yuborishingiz mumkin (standart 22):
server1|192.168.1.100||root|mypassword
'''
    await update.message.reply_text(instruction_text)
    context.user_data['awaiting_host_data'] = True

async def handle_host_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """SSH host ma'lumotlarini qabul qilish"""
    if not context.user_data.get('awaiting_host_data'):
        return
    try:
        text = update.message.text.strip()
        # 1. Oddiy SSH manzil formatini tekshirish
        ssh_parsed = parse_ssh_string(text)
        if ssh_parsed:
            username, hostname, port = ssh_parsed
            # Foydalanuvchidan parolni so'rash
            context.user_data['temp_ssh_data'] = {
                'name': f"{username}@{hostname}",
                'hostname': hostname,
                'port': port,
                'username': username
            }
            await update.message.reply_text(f'''
SSH manzil aniqlandi:
Host: {hostname}
Port: {port}
Foydalanuvchi: {username}
Endi parolni kiriting:
''')
            context.user_data['awaiting_password'] = True
            return
        # 2. Batafsil formatni tekshirish
        data = text.split('|')
        if len(data) < 5:
            await update.message.reply_text("❌ Noto'g'ri format. Iltimos, to'g'ri formatda kiriting.")
            return
        name, hostname, port_str, username, password = data
        port = int(port_str) if port_str and port_str.isdigit() else 22
        if not name or not hostname or not username or not password:
            await update.message.reply_text("❌ Barcha maydonlar to'ldirilishi shart.")
            return
        host_id = db_manager.add_ssh_host(
            update.effective_user.id, name, hostname, port, username, password
        )
        success_text = f'''
✅ SSH server muvaffaqiyatli qo'shildi!
ID: {host_id}
Nomi: {name}
Host: {hostname}
Port: {port}
Foydalanuvchi: {username}
Endi serverga ulanish uchun:
/connect {host_id}
'''
        await update.message.reply_text(success_text)
        context.user_data['awaiting_host_data'] = False
        context.user_data.pop('temp_ssh_data', None)
    except ValueError:
        await update.message.reply_text("❌ Port raqam bo'lishi kerak.")
    except Exception as e:
        await update.message.reply_text(f"❌ Xatolik: {str(e)}")

async def handle_password_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Parolni qabul qilish"""
    if not context.user_data.get('awaiting_password'):
        return
    try:
        password = update.message.text.strip()
        temp_data = context.user_data.get('temp_ssh_data', {})
        if not temp_data:
            await update.message.reply_text("❌ Xatolik yuz berdi. Qaytadan urinib ko'ring.")
            return
        name = temp_data['name']
        hostname = temp_data['hostname']
        port = temp_data['port']
        username = temp_data['username']
        host_id = db_manager.add_ssh_host(
            update.effective_user.id, name, hostname, port, username, password
        )
        success_text = f'''
✅ SSH server muvaffaqiyatli qo'shildi!
ID: {host_id}
Nomi: {name}
Host: {hostname}
Port: {port}
Foydalanuvchi: {username}
Endi serverga ulanish uchun:
/connect {host_id}
'''
        await update.message.reply_text(success_text)
        context.user_data['awaiting_host_data'] = False
        context.user_data['awaiting_password'] = False
        context.user_data.pop('temp_ssh_data', None)
    except Exception as e:
        await update.message.reply_text(f"❌ Xatolik: {str(e)}")

@deduplicate_command
async def list_hosts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """SSH hostlar ro'yxatini ko'rsatish"""
    try:
        hosts = db_manager.get_ssh_hosts(update.effective_user.id)
        is_premium = db_manager.is_premium_user(update.effective_user.id)
        server_count = len(hosts)
    except Exception as e:
        await update.message.reply_text(f"❌ Ma'lumotlar bazasidan olishda xatolik: {str(e)}")
        return
    if not hosts:
        await update.message.reply_text("📭 Sizda hali saqlangan SSH serverlar yo'q.")
        return
    # Premium status
    status_text = "⭐ Premium" if is_premium else f"🆓 Free ({server_count}/3 server)"
    hosts_text = f"📋 Sizning SSH serverlaringiz:\n👤 Status: {status_text}\n"
    for host in hosts:
        host_id, name, hostname, port, username = host
        hosts_text += f"ID: {host_id} | {name}\n"
        hosts_text += f"Host: {hostname}:{port}\n"
        hosts_text += f"Foydalanuvchi: {username}\n"
        hosts_text += f"Ulanish: /connect_{host_id}\n"
        hosts_text += "─" * 30 + "\n"
    hosts_text += "\nUlanish uchun ID ustiga bosing yoki quyidagicha kiriting:\n/connect [ID]"
    if not is_premium and server_count >= 3:
        hosts_text += "\n⚠️ Free foydalanuvchilar uchun maksimal 3 ta server. Premium tarifga o'ting!"
    await update.message.reply_text(hosts_text)

@deduplicate_command
async def connect_host(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """SSH hostga ulanish"""
    if not context.args:
        await update.message.reply_text("Iltimos, server ID sini kiriting.\nMisol: /connect 1")
        return
    try:
        host_id = int(context.args[0])
        host_data = db_manager.get_ssh_host(update.effective_user.id, host_id)
        if not host_data:
            await update.message.reply_text("❌ Bunday ID li server topilmadi.")
            return
        hostname, port, username, password = host_data
        if update.effective_user.id in ssh_sessions:
            ssh_sessions[update.effective_user.id].disconnect()
            await update.message.reply_text("⚠️ Oldingi sessiya uzildi.")
        ssh_client = SSHClient(hostname, port, username, password)
        success, message = ssh_client.connect()
        if success:
            ssh_sessions[update.effective_user.id] = ssh_client
            if update.effective_user.id in session_timers:
                session_timers[update.effective_user.id].cancel()
            timer = threading.Timer(SESSION_TIMEOUT, lambda: disconnect_session(update.effective_user.id))
            timer.start()
            session_timers[update.effective_user.id] = timer
            connect_text = f'''
✅ Serverga muvaffaqiyatli ulandingiz!
Host: {hostname}:{port}
Foydalanuvchi: {username}
Buyruq bajarish:
/terminal ls -la
Sessiya avtomatik ravishda {SESSION_TIMEOUT//60} daqiqadan keyin uziladi.
'''
            await update.message.reply_text(connect_text)
        else:
            await update.message.reply_text(message)
    except ValueError:
        await update.message.reply_text("❌ ID raqam bo'lishi kerak.")
    except Exception as e:
        await update.message.reply_text(f"❌ Xatolik: {str(e)}")

def disconnect_session(user_id):
    """Sessiyani avtomatik uzish"""
    if user_id in ssh_sessions:
        ssh_sessions[user_id].disconnect()
        del ssh_sessions[user_id]
        if user_id in session_timers:
            del session_timers[user_id]

@deduplicate_command
async def disconnect_host(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """SSH hostdan uzilish"""
    user_id = update.effective_user.id
    if user_id in ssh_sessions:
        ssh_sessions[user_id].disconnect()
        del ssh_sessions[user_id]
        if user_id in session_timers:
            session_timers[user_id].cancel()
            del session_timers[user_id]
        await update.message.reply_text("✅ SSH sessiya uzildi.")
    else:
        await update.message.reply_text("📭 Faol SSH sessiya mavjud emas.")

@deduplicate_command
async def execute_terminal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Terminal buyruq bajarish"""
    user_id = update.effective_user.id
    if user_id not in ssh_sessions:
        await update.message.reply_text("❌ SSH sessiya mavjud emas. Avval /connect dan foydalaning.")
        return
    if not context.args:
        await update.message.reply_text("Iltimos, bajarish uchun buyruq kiriting.\nMisol: /terminal ls -la")
        return
    command = ' '.join(context.args)
    await update.message.reply_text(f"🔄 Bajarilmoqda: {command}")
    ssh_client = ssh_sessions[user_id]
    success, result = ssh_client.execute_command(command)
    if success:
        if len(result) > 4000:
            await update.message.reply_text(f"📤 Buyruq natijasi:\n```\n{result[:4000]}\n```", parse_mode='Markdown')
            for i in range(4000, len(result), 4000):
                await update.message.reply_text(f"```\n{result[i:i+4000]}\n```", parse_mode='Markdown')
        else:
            await update.message.reply_text(f"📤 Buyruq natijasi:\n```\n{result}\n```", parse_mode='Markdown')
    else:
        await update.message.reply_text(result)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Oddiy xabarlarni qayta ishlash"""
    # Prevent duplicate message processing
    message_id = update.message.message_id
    user_id = update.effective_user.id
    if (user_id, message_id) in processed_messages:
        return
    processed_messages.add((user_id, message_id))
    # Clean up old processed messages (keep only last 1000)
    if len(processed_messages) > 1000:
        processed_messages.clear()
    text = update.message.text
    if text == "➕ Server qo'shish":
        await add_host(update, context)
    elif text == "📋 Serverlar":
        await list_hosts(update, context)
    elif text == "⭐ Premium":
        await premium_command(update, context)
    elif text == "❓ Yordam":
        await help_command(update, context)
    elif text == "ℹ️ Bot haqida":
        await about_command(update, context)
    elif text.startswith('/connect_'):
        host_id = text.split('_')[1]
        context.args = [host_id]
        await connect_host(update, context)
    elif context.user_data.get('awaiting_password'):
        await handle_password_input(update, context)
    elif context.user_data.get('awaiting_host_data'):
        await handle_host_data(update, context)
    else:
        await update.message.reply_text("❓ Noma'lum buyruq. Yordam uchun /help ni bosing.")

async def main():
    """Asosiy funksiya"""
    global db_manager
    # Database initialization
    db_manager = DatabaseManager()
    logger.info("Ma'lumotlar bazasi muvaffaqiyatli ulandi")
    # Application initialization
    application = Application.builder().token(BOT_TOKEN).build()
    logger.info("Telegram bot muvaffaqiyatli yaratildi")
    # Asosiy buyruqlar
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("about", about_command))
    application.add_handler(CommandHandler("premium", premium_command))
    application.add_handler(CommandHandler("addhost", add_host))
    application.add_handler(CommandHandler("listhosts", list_hosts))
    application.add_handler(CommandHandler("connect", connect_host))
    application.add_handler(CommandHandler("disconnect", disconnect_host))
    application.add_handler(CommandHandler("terminal", execute_terminal))
    # Admin buyruqlari
    application.add_handler(CommandHandler("addtariff", admin_add_tariff))
    application.add_handler(CommandHandler("edittariff", admin_edit_tariff))
    application.add_handler(CommandHandler("deletetariff", admin_delete_tariff))
    application.add_handler(CommandHandler("listtariffs", admin_list_tariffs))
    application.add_handler(CommandHandler("adminhelp", admin_help))
    # Callback query handlerlar
    application.add_handler(CallbackQueryHandler(handle_tariff_selection, pattern="^tariff_"))
    application.add_handler(CallbackQueryHandler(handle_back_to_tariffs, pattern="^back_to_tariffs$"))
    # To'lov handlerlari
    application.add_handler(PreCheckoutQueryHandler(handle_precheckout))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, handle_successful_payment))
    # Oddiy xabarlar
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("SSH Terminal Bot Premium tizimi bilan ishga tushirilmoqda...")
    await application.run_polling()

if __name__ == '__main__':
    import asyncio
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBot to'xtatildi.")
    except Exception as e:
        print(f"Xatolik: {e}")
        import sys
        sys.exit(1)