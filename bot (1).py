import telebot
from telebot import types
from math import floor
from datetime import datetime, timedelta
import time
import random
import re
import config
import threading
import crypto_pay
import requests
import sqlite3
import db as db_module
import logging
import time
import threading
import schedule
from telebot import TeleBot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from db import get_db
import config
import io
from telebot.handler_backends import State, StatesGroup
from threading import Lock

bot = telebot.TeleBot(config.BOT_TOKEN)


treasury_lock = threading.Lock()
active_treasury_admins = {}

def auto_confirm_number(number, user_id, code):
    with db.get_db() as conn:
        cursor = conn.cursor()
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # Устанавливаем статус "активен" и TAKE_DATE
        cursor.execute('''
            UPDATE numbers 
            SET status = "активен", 
                hold_start_time = NULL, 
                VERIFICATION_CODE = NULL, 
                TAKE_DATE = ? 
            WHERE number = ?
        ''', (current_time, number))
        conn.commit()
        print(f"[DEBUG] Номер {number} автоматически подтверждён в {current_time}")

    # Уведомляем пользователя
    safe_send_message(user_id, f"✅ Номер {number} автоматически помечен как 'встал' в {current_time}.")

    # Обновляем сообщение в группе
    if number in code_messages:
        message_data = code_messages[number]
        chat_id = message_data["chat_id"]
        message_id = message_data["message_id"]
        tg_number = message_data["tg_number"]
        try:
            bot.edit_message_text(
                f"📱 <b>ТГ {tg_number}</b>\n"
                f"⏰ Номер {number} автоматически помечен как 'встал' в {current_time}.",
                chat_id,
                message_id,
                parse_mode='HTML'
            )
            print(f"[DEBUG] Сообщение в группе {chat_id} обновлено для номера {number}")
        except Exception as e:
            print(f"[ERROR] Не удалось отредактировать сообщение для номера {number}: {e}")
        del code_messages[number]

    for mod_id in config.MODERATOR_IDS:
        safe_send_message(mod_id, f"⏰ Номер {number} автоматически помечен как 'встал' в {current_time}.")

class Database:
    def get_db(self):
        return sqlite3.connect('database.db')

    def is_moderator(self, user_id):
        with self.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM personal WHERE ID = ? AND TYPE = ?', (user_id, 'moder'))
            return cursor.fetchone() is not None

    def update_balance(self, user_id, amount):
        with self.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('UPDATE users SET BALANCE = BALANCE + ? WHERE ID = ?', (amount, user_id))
            conn.commit()

    def get_group_name(self, group_id):
        return db_module.get_group_name(group_id)

    def update_last_activity(self, user_id):
        """Обновляет время последней активности пользователя и сбрасывает статус АФК."""
        with self.get_db() as conn:
            cursor = conn.cursor()
            current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute('SELECT IS_AFK FROM users WHERE ID = ?', (user_id,))
            result = cursor.fetchone()
            if not result:
                cursor.execute('INSERT OR IGNORE INTO users (ID, BALANCE, REG_DATE, IS_AFK, LAST_ACTIVITY) VALUES (?, ?, ?, ?, ?)',
                              (user_id, 0.0, current_time, 0, current_time))
            else:
                if result[0] == 1:
                    cursor.execute('UPDATE users SET IS_AFK = 0 WHERE ID = ?', (user_id,))
                    print(f"[DEBUG] Пользователь {user_id} выведен из режима АФК")
            cursor.execute('UPDATE users SET LAST_ACTIVITY = ? WHERE ID = ?', (current_time, user_id))
            conn.commit()
            print(f"[DEBUG] Обновлено время активности для пользователя {user_id}: {current_time}")

    def get_afk_status(self, user_id):
            with self.get_db() as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT IS_AFK FROM users WHERE ID = ?', (user_id,))
                result = cursor.fetchone()
                return bool(result[0]) if result else False

db = Database()

def is_russian_number(phone_number):
    phone_number = phone_number.strip()
    if phone_number.startswith("7") or phone_number.startswith("8"):
        phone_number = "+7" + phone_number[1:]
    if not phone_number.startswith("+"):
        phone_number = "+" + phone_number
    pattern = r'^\+7\d{10}$'
    return phone_number if bool(re.match(pattern, phone_number)) else None

def check_balance_and_fix(user_id):
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT BALANCE FROM users WHERE ID = ?', (user_id,))
        user = cursor.fetchone()
        if user and user[0] < 0:
            cursor.execute('UPDATE users SET BALANCE = 0 WHERE ID = ?', (user_id,))
            conn.commit()


# Assuming db_module and config are imported
cooldowns = {}  # In-memory cooldown tracking

@bot.message_handler(commands=['start'])
def start(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    current_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # Проверяем текущий статус АФК
    was_afk = db_module.get_afk_status(user_id)
    db_module.update_last_activity(user_id)  # Обновляем время активности и сбрасываем АФК
    
    chat_type = bot.get_chat(message.chat.id).type
    is_group = chat_type in ["group", "supergroup"]
    
    with db_module.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT BLOCKED FROM requests WHERE ID = ?', (user_id,))
        user = cursor.fetchone()
        if user and user[0] == 1:
            bot.send_message(message.chat.id, "🚫 Вас заблокировали в боте!")
            return
    
    is_moderator = db_module.is_moderator(user_id)
    is_admin = user_id in config.ADMINS_ID

    # Уведомление о выходе из АФК
    if was_afk:
        try:
            bot.send_message(
                message.chat.id,
                "🔔 Вы вышли из режима АФК. Ваши номера снова видны.",
                parse_mode='HTML'
            )
        except Exception as e:
            print(f"[ERROR] Не удалось отправить уведомление о выходе из АФК пользователю {user_id}: {e}")

    if is_group and is_moderator and not is_admin:
        cursor.execute('SELECT GROUP_ID FROM personal WHERE ID = ? AND TYPE = ?', (user_id, 'moder'))
        group_id = cursor.fetchone()
        group_name = db_module.get_group_name(group_id[0]) if group_id else "Неизвестная группа"
        
        moderator_text = (
            f"Здравствуйте 🤝\n"
            f"Вы назначены модератором в группе: <b>{group_name}</b>\n\n"
            "Вот что вы можете:\n\n"
            "1. Брать номера в обработку и работать с ними\n\n"
            "2. Вы можете назначить номер слетевшим, если с ним что-то не так\n"
            "Не злоупотребляйте этим в юмористических целях!\n\n"
            "<b>Доступные вам команды в чате:</b>\n"
            "1. <b>Запросить номер</b>\n"
            "Запрос номера производится вводом таких символов как «тг1» и отправлением его в рабочий чат\n"
            "Вводите номер, который вам присвоили или который приписан вашему ПК\n"
            "<b>Важно!</b> Мы не рассчитываем на ПК, которым присвоен номер больше 70\n\n"
            "2. Если с номером что-то не так, вы в течение 5 минут (это время выделенное на рассмотрение аккаунта) можете отметить его «слетевшим»\n"
            "Чтобы указать номер слетевшим, вам необходимо написать такую команду: «слет и номер с которым вы работали»\n"
            "Пример: <code>слет +79991112345</code>\n"
            "После этого номер отметится слетевшим, и выйдет сообщение о том, что номер слетел"
        )
        bot.send_message(message.chat.id, moderator_text, parse_mode='HTML')
        return
    
    if user_id in config.ADMINS_ID:
        with db_module.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('INSERT OR REPLACE INTO requests (ID, LAST_REQUEST, STATUS, BLOCKED, CAN_SUBMIT_NUMBERS) VALUES (?, ?, ?, ?, ?)',
                          (user_id, current_date, 'approved', 0, 1))
            conn.commit()
        if is_group:
            markup = types.InlineKeyboardMarkup()
            markup.row(
                types.InlineKeyboardButton("👤 Мой профиль", callback_data="profile"),
                types.InlineKeyboardButton("📱 Сдать номер", callback_data="submit_number")
            )
            markup.add(types.InlineKeyboardButton("⚙️ Админка", callback_data="admin_panel"))
            is_afk = db_module.get_afk_status(user_id)
            afk_button_text = "🟢 Включить АФК" if not is_afk else "🔴 Выключить АФК"
            markup.add(types.InlineKeyboardButton(afk_button_text, callback_data="toggle_afk"))
            bot.send_message(
                message.chat.id,
                f"<b>📢 Добро пожаловать в {config.SERVICE_NAME}</b>\n\n"
                f"<b>⏳ График работы:</b> <code>{config.WORK_TIME}</code>\n\n"
                "<b>💼 Как это работает?</b>\n"
                "• <i>Вы продаёте номер</i> – <b>мы предоставляем стабильные выплаты.</b>\n"
                f"• <i>Моментальные выплаты</i> – <b>после 5 минут работы.</b>\n\n"
                "<b>💰 Тарифы на сдачу номеров:</b>\n"
                f"▪️ <code>2.0$</code> за номер (холд 5 минут)\n"
                f"<b>📍 Почему выбирают {config.SERVICE_NAME} ?</b>\n"
                "✅ <i>Прозрачные условия сотрудничества</i>\n"
                "✅ <i>Выгодные тарифы и моментальные выплаты</i>\n"
                "✅ <i>Оперативная поддержка 24/7</i>\n\n"
                "<b>🔹 Начните зарабатывать прямо сейчас!</b>",
                reply_markup=markup,
                parse_mode='HTML'
            )
        else:
            # Send a temporary message to get message_id
            temp_message = bot.send_message(chat_id, "Загрузка меню...", parse_mode='HTML')
            show_main_menu(chat_id, temp_message.message_id, user_id)
        return
    
    with db_module.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT LAST_REQUEST, STATUS FROM requests WHERE ID = ?', (user_id,))
        request = cursor.fetchone()
        if request and request[1] == 'approved':
            if is_group:
                markup = types.InlineKeyboardMarkup()
                markup.row(
                    types.InlineKeyboardButton("👤 Мой профиль", callback_data="profile"),
                    types.InlineKeyboardButton("📱 Сдать номер", callback_data="submit_number")
                )
                is_afk = db_module.get_afk_status(user_id)
                afk_button_text = "🟢 Включить АФК" if not is_afk else "🔴 Выключить АФК"
                markup.add(types.InlineKeyboardButton(afk_button_text, callback_data="toggle_afk"))
                bot.send_message(
                    message.chat.id,
                    f"<b>📢 Добро пожаловать в {config.SERVICE_NAME}</b>\n\n"
                    f"<b>⏳ График работы:</b> <code>{config.WORK_TIME}</code>\n\n"
                    "<b>💼 Как это работает?</b>\n"
                    "• <i>Вы продаёте номер</i> – <b>мы предоставляем стабильные выплаты.</b>\n"
                    f"• <i>Моментальные выплаты</i> – <b>после 5 минут работы.</b>\n\n"
                    "<b>💰 Тарифы на сдачу номеров:</b>\n"
                    f"▪️ <code>2.0$</code> за номер (холд 5 минут)\n"
                    f"<b>📍 Почему выбирают {config.SERVICE_NAME} ?</b>\n"
                    "✅ <i>Прозрачные условия сотрудничества</i>\n"
                    "✅ <i>Выгодные тарифы и моментальные выплаты</i>\n"
                    "✅ <i>Оперативная поддержка 24/7</i>\n\n"
                    "<b>🔹 Начните зарабатывать прямо сейчас!</b>",
                    reply_markup=markup,
                    parse_mode='HTML'
                )
            else:
                # Send a temporary message to get message_id
                temp_message = bot.send_message(chat_id, "Загрузка меню...", parse_mode='HTML')
                show_main_menu(chat_id, temp_message.message_id, user_id)
            return
        if request:
            last_request_time = datetime.strptime(request[0], "%Y-%m-%d %H:%M:%S")
            if datetime.now() - last_request_time < timedelta(minutes=15):
                time_left = 15 - ((datetime.now() - last_request_time).seconds // 60)
                bot.send_message(message.chat.id, 
                                f"⏳ Ожидайте подтверждения. Вы сможете отправить новый запрос через {time_left} минут.")
                return
        cursor.execute('INSERT OR REPLACE INTO requests (ID, LAST_REQUEST, STATUS, BLOCKED, CAN_SUBMIT_NUMBERS) VALUES (?, ?, ?, ?, ?)',
                      (user_id, current_date, 'pending', 0, 1))
        conn.commit()
        bot.send_message(message.chat.id, 
                        "👋 Здравствуйте! Ожидайте, пока вас впустит администратор.")
        # Notify admins with approval buttons for non-admin/moderator pending users
        with db_module.get_db() as conn:
            cursor = conn.cursor()
            # Dynamically create placeholders for config.ADMINS_ID
            admin_ids = config.ADMINS_ID
            admin_placeholders = ','.join('?' for _ in admin_ids)
            query = f'SELECT ID, LAST_REQUEST FROM requests WHERE STATUS = ? AND ID NOT IN (SELECT ID FROM requests WHERE ID IN ({admin_placeholders}) OR ID IN (SELECT ID FROM personal WHERE TYPE = ?)) AND ID != ?'
            params = ('pending', *admin_ids, 'moderator', user_id)
            cursor.execute(query, params)
            pending_users = cursor.fetchall()
        if pending_users:
            admin_text = "🔔 <b>Заявки на вступления</b>\n\n"
            markup = types.InlineKeyboardMarkup()
            for pending_user_id, reg_date in pending_users:
                approve_button = types.InlineKeyboardButton(f"✅ Одобрить {pending_user_id}", callback_data=f"approve_user_{pending_user_id}")
                reject_button = types.InlineKeyboardButton(f"❌ Отклонить {pending_user_id}", callback_data=f"reject_user_{pending_user_id}")
                markup.row(approve_button, reject_button)
            admin_text += f"👤 Пользователь ID: <code>{user_id}</code> (Зарегистрирован: {current_date})\n"
            for pending_user_id, reg_date in pending_users:
                admin_text += f"👤 Пользователь ID: <code>{pending_user_id}</code> (Зарегистрирован: {reg_date})\n"
            try:
                for admin_id in config.ADMINS_ID:
                    bot.send_message(admin_id, admin_text, parse_mode='HTML', reply_markup=markup)
            except Exception as e:
                print(f"[ERROR] Не удалось отправить уведомление админам: {e}")
                

def show_main_menu(chat_id, message_id, user_id):
    with db_module.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT IS_AFK, AFK_LOCKED FROM users WHERE ID = ?', (user_id,))
        result = cursor.fetchone()
        if not result:
            db_module.add_user(user_id)
            is_afk = False
            afk_locked = False
        else:
            is_afk, afk_locked = result

    # Check if the user is a moderator
    is_moderator = db_module.is_moderator(user_id)

    if is_moderator:
        moderator_text = (
            "Здравствуйте 🤝\n"
            "Вы назначены модератором в группе: 1\n\n"
            "Вот что вы можете:\n"
            "1. Брать номера в обработку и работать с ними\n"
            "2. Вы можете назначить номер слетевшим, если с ним что-то не так\n"
            "   Не злоупотребляйте этим в юмористических целях!\n\n"
            "Доступные вам команды в чате:\n"
            "1. Запросить номер\n"
            "   Запрос номера производится вводом таких символов как «тг1» и отправлением его в рабочий чат\n"
            "   Вводите номер, который вам присвоили или который приписан вашему ПК\n"
            "   Важно! Мы не рассчитываем на ПК, которым присвоен номер больше 70\n\n"
            "2. Если с номером что-то не так, вы в течение 5 минут (это время выделенное на рассмотрение аккаунта) можете отметить его «слетевшим»\n"
            "   Чтобы указать номер слетевшим, вам необходимо написать такую команду: «слет и номер с которым вы работали»\n"
            "   Пример: слет +79991112345\n"
            "   После этого номер отметится слетевшим, и выйдет сообщение о том, что номер слетел )"
        )
        try:
            bot.edit_message_text(
                moderator_text,
                chat_id,
                message_id,
                parse_mode='HTML'
            )
        except telebot.apihelper.ApiTelegramException as e:
            if "message is not modified" in str(e):
                print(f"[DEBUG] Сообщение не изменено, пропускаем редактирование для chat_id={chat_id}, message_id={message_id}")
            else:
                print(f"[ERROR] Ошибка при редактировании сообщения: {e}")
                bot.send_message(chat_id, moderator_text, parse_mode='HTML')
    else:
        price = db_module.get_user_price(user_id)
        with db_module.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT HOLD_TIME FROM settings')
            result = cursor.fetchone()
            hold_time = result[0] if result else 5

        welcome_text = (
            f"<b>📢 Добро пожаловать в {config.SERVICE_NAME}</b>\n\n"
            f"<b>⏳ График работы:</b> <code>{config.WORK_TIME}</code>\n\n"
            "<b>💼 Как это работает?</b>\n"
            "• <i>Вы продаёте номер</i> – <b>мы предоставляем стабильные выплаты.</b>\n"
            f"• <i>Моментальные выплаты</i> – <b>после {hold_time} минут работы.</b>\n\n"
            "<b>💰 Тарифы на сдачу номеров:</b>\n"
            f"▪️ <code>{price}$</code> за номер (холд {hold_time} минут)\n"
            f"<b>📍 Почему выбирают {config.SERVICE_NAME}?</b>\n"
            "✅ <i>Прозрачные условия сотрудничества</i>\n"
            "✅ <i>Выгодные тарифы и моментальные выплаты</i>\n"
            "✅ <i>Оперативная поддержка 24/7</i>\n\n"
            "<b>🔹 Начните зарабатывать прямо сейчас!</b>"
        )
        markup = types.InlineKeyboardMarkup()
        markup.row(
            types.InlineKeyboardButton("👤 Мой профиль", callback_data="profile"),
            types.InlineKeyboardButton("📱 Сдать номер", callback_data="submit_number")
        )

        is_admin = user_id in config.ADMINS_ID
        if not is_admin and not is_moderator:
            markup.add(types.InlineKeyboardButton("🗑️ Удалить номер", callback_data="delete_number"))
            markup.add(types.InlineKeyboardButton("✏️ Изменить номер", callback_data="change_number"))

        if is_admin:
            markup.add(types.InlineKeyboardButton("⚙️ Админка", callback_data="admin_panel"))

        afk_button_text = "🔴 Выключить АФК" if is_afk and not afk_locked else "🟢 Включить АФК"
        if afk_locked:
            markup.add(types.InlineKeyboardButton(f"🔒 АФК заблокирован (админ)", callback_data="afk_locked_info"))
        else:
            markup.add(types.InlineKeyboardButton(afk_button_text, callback_data="toggle_afk"))

        try:
            bot.edit_message_text(
                welcome_text,
                chat_id,
                message_id,
                parse_mode='HTML',
                reply_markup=markup
            )
        except telebot.apihelper.ApiTelegramException as e:
            if "message is not modified" in str(e):
                print(f"[DEBUG] Сообщение не изменено, пропускаем редактирование для chat_id={chat_id}, message_id={message_id}")
            else:
                print(f"[ERROR] Ошибка при редактировании сообщения: {e}")
                bot.send_message(chat_id, welcome_text, parse_mode='HTML', reply_markup=markup)

        if is_afk and not afk_locked:
            bot.send_message(chat_id, "🔔 Ваш АФК отключён. Ваши номера снова видны.", parse_mode='HTML')
        elif is_afk and afk_locked:
            bot.send_message(chat_id, "🔔 Вы в режиме АФК, заблокированном администратором. Номера скрыты.", parse_mode='HTML')

@bot.callback_query_handler(func=lambda call: call.data == "back_to_main")
def back_to_main(call):
    chat_id = call.message.chat.id
    message_id = call.message.message_id
    user_id = call.from_user.id
    show_main_menu(chat_id, message_id, user_id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("approve_user_"))
def approve_user_callback(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для выполнения этого действия!")
        return
    
    user_id = int(call.data.split("_")[2])
    
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('UPDATE requests SET STATUS = ? WHERE ID = ?', ('approved', user_id))
        conn.commit()
        
        try:
            bot.send_message(user_id, "✅ Вас впустили в бота! Напишите /start")
            text = f"✅ Пользователь {user_id} одобрен"
        except:
            text = f"✅ Пользователь {user_id} одобрен, но уведомление не доставлено"
        
        # Добавляем кнопку "Вернуться в заявки на вступление"
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("📝 Вернуться в заявки на вступление", callback_data="pending_requests"))
        
        bot.edit_message_text(text,
                             call.message.chat.id,
                             call.message.message_id,
                             parse_mode='HTML',
                             reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("reject_user_"))
def reject_user_callback(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для выполнения этого действия!")
        return
    
    user_id = int(call.data.split("_")[2])
    
    with db.get_db() as conn:
        cursor = conn.cursor()
        current_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute('UPDATE requests SET STATUS = ?, LAST_REQUEST = ? WHERE ID = ?', ('rejected', current_date, user_id))
        conn.commit()
        
        try:
            bot.send_message(user_id, "❌ Вам отказано в доступе. Вы сможете отправить новый запрос через 15 минут.")
            text = f"❌ Пользователь {user_id} отклонён"
        except:
            text = f"❌ Пользователь {user_id} отклонён, но уведомление не доставлено"
        
        # Добавляем кнопку "Вернуться в заявки на вступление"
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("📝 Вернуться в заявки на вступление", callback_data="pending_requests"))
        
        bot.edit_message_text(text,
                             call.message.chat.id,
                             call.message.message_id,
                             parse_mode='HTML',
                             reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data == "pending_requests")
def pending_requests(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для доступа к заявкам!")
        return

    bot.answer_callback_query(call.id)

    with db_module.get_db() as conn:
        cursor = conn.cursor()
        # Динамически создаём placeholders для config.ADMINS_ID
        admin_ids = config.ADMINS_ID
        if not admin_ids:
            query = 'SELECT ID, LAST_REQUEST FROM requests WHERE STATUS = ? AND ID NOT IN (SELECT ID FROM personal WHERE TYPE = ?)'
            params = ('pending', 'moderator')
        else:
            admin_placeholders = ','.join('?' for _ in admin_ids)
            query = f'SELECT ID, LAST_REQUEST FROM requests WHERE STATUS = ? AND ID NOT IN (SELECT ID FROM requests WHERE ID IN ({admin_placeholders}) OR ID IN (SELECT ID FROM personal WHERE TYPE = ?))'
            params = ('pending', *admin_ids, 'moderator')
        cursor.execute(query, params)
        pending_users = cursor.fetchall()

    admin_text = "🔔 <b>Заявки на вступления</b>\n\n"
    markup = types.InlineKeyboardMarkup()
    if pending_users:
        for user_id, reg_date in pending_users:
            admin_text += f"👤 Пользователь ID: <code>{user_id}</code> (Зарегистрирован: {reg_date})\n"
            # Добавляем кнопки "Одобрить" и "Отклонить"
            approve_button = types.InlineKeyboardButton(f"✅ Одобрить {user_id}", callback_data=f"approve_user_{user_id}")
            reject_button = types.InlineKeyboardButton(f"❌ Отклонить {user_id}", callback_data=f"reject_user_{user_id}")
            markup.row(approve_button, reject_button)
    else:
        admin_text += "📭 Нет новых заявок на вступление.\n"

    # Кнопки навигации
    markup.add(types.InlineKeyboardButton("🔙 В админ-панель", callback_data="admin_panel"))
    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))

    try:
        bot.edit_message_text(
            admin_text,
            call.message.chat.id,
            call.message.message_id,
            parse_mode='HTML',
            reply_markup=markup
        )
    except telebot.apihelper.ApiTelegramException as e:
        print(f"[ERROR] Не удалось отредактировать сообщение: {e}")
        bot.send_message(call.message.chat.id, admin_text, parse_mode='HTML', reply_markup=markup)                           

#ЧТО БЫ ПОЛЬЗОВАТЕЛЬ УДАЛИЛ НОМЕР САМ СВОЙ
@bot.callback_query_handler(func=lambda call: call.data == "delete_number")
def handle_delete_number(call):
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    message_id = call.message.message_id

    # Проверяем, что пользователь не администратор и не модератор
    is_admin = user_id in config.ADMINS_ID
    is_moderator = db_module.is_moderator(user_id)
    if is_admin or is_moderator:
        bot.answer_callback_query(call.id, "❌ Эта функция доступна только обычным пользователям!")
        return

    # Запрашиваем номер для удаления
    msg = bot.send_message(chat_id, "📞 Впишите номер, который желаете удалить с бота:")
    bot.register_next_step_handler(msg, process_delete_number, message_id)

def process_delete_number(message, original_message_id):
    chat_id = message.chat.id
    user_id = message.from_user.id
    number_to_delete = message.text.strip()

    # Простая валидация номера
    if not number_to_delete:
        bot.send_message(chat_id, "❌ Номер не может быть пустым. Попробуйте снова.")
        start(message)
        return

    # Нормализуем номер: заменяем "8" на "+7" или добавляем "+7" если его нет
    if number_to_delete.startswith('8'):
        number_to_delete = '+7' + number_to_delete[1:]
    elif not number_to_delete.startswith('+'):
        number_to_delete = '+7' + number_to_delete

    # Проверка на российский номер
    import re
    if not re.match(r'^\+7\d{10}$', number_to_delete):
        bot.send_message(chat_id, "❌ Номер должен быть российским (например, +79991234567 или 89991234567).")
        start(message)
        return

    try:
        with db_module.get_db() as conn:
            cursor = conn.cursor()
            # Проверяем, существует ли номер у данного пользователя
            cursor.execute('SELECT * FROM numbers WHERE ID_OWNER = ? AND NUMBER = ?', (user_id, number_to_delete))
            number_record = cursor.fetchone()

            if not number_record:
                bot.send_message(chat_id, f"❌ Номер {number_to_delete} не найден среди ваших номеров или он вам не принадлежит.")
                start(message)
                return

            # Удаляем номер из базы данных
            cursor.execute('DELETE FROM numbers WHERE ID_OWNER = ? AND NUMBER = ?', (user_id, number_to_delete))
            conn.commit()
            print(f"[DEBUG] Номер {number_to_delete} удалён для пользователя {user_id}")

            # Отправляем сообщение об успешном удалении
            bot.send_message(chat_id, f"✅ Номер {number_to_delete} успешно удалён с бота!")

            # Возвращаем пользователя в главное меню
            start(message)

    except Exception as e:
        print(f"[ERROR] Ошибка при удалении номера {number_to_delete} для пользователя {user_id}: {e}")
        bot.send_message(chat_id, "❌ Произошла ошибка при удалении номера. Попробуйте позже.")
        start(message)     

#ИЗММЕНИТЬ НОМЕР:
@bot.callback_query_handler(func=lambda call: call.data == "change_number")
def handle_change_number(call):
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    message_id = call.message.message_id

    # Проверяем, что пользователь не администратор и не модератор
    is_admin = user_id in config.ADMINS_ID
    is_moderator = db_module.is_moderator(user_id)
    if is_admin or is_moderator:
        bot.answer_callback_query(call.id, "❌ Эта функция доступна только обычным пользователям!")
        return

    # Запрашиваем старый номер
    msg = bot.send_message(chat_id, "📞 Впишите номер, который хотите изменить:")
    bot.register_next_step_handler(msg, process_old_number, message_id)
    
def process_old_number(message, original_message_id):
    chat_id = message.chat.id
    user_id = message.from_user.id
    old_number = message.text.strip()

    # Простая валидация номера
    if not old_number:
        bot.send_message(chat_id, "❌ Номер не может быть пустым. Попробуйте снова.")
        start(message)
        return

    # Нормализуем номер: заменяем "8" на "+7" или добавляем "+7" если его нет
    if old_number.startswith('8'):
        old_number = '+7' + old_number[1:]
    elif not old_number.startswith('+'):
        old_number = '+7' + old_number

    # Проверка на российский номер
    import re
    if not re.match(r'^\+7\d{10}$', old_number):
        bot.send_message(chat_id, "❌ Номер должен быть российским (например, +79991234567 или 89991234567).")
        start(message)
        return

    try:
        with db_module.get_db() as conn:
            cursor = conn.cursor()
            # Проверяем, существует ли номер у данного пользователя
            cursor.execute('SELECT * FROM numbers WHERE ID_OWNER = ? AND NUMBER = ?', (user_id, old_number))
            number_record = cursor.fetchone()

            if not number_record:
                bot.send_message(chat_id, f"❌ Номер {old_number} не найден среди ваших номеров или он вам не принадлежит.")
                start(message)
                return

            # Запрашиваем новый номер
            msg = bot.send_message(chat_id, f"📞 Введите новый номер для замены {old_number}:")
            bot.register_next_step_handler(msg, process_new_number, original_message_id, old_number)

    except Exception as e:
        print(f"[ERROR] Ошибка при проверке номера {old_number} для пользователя {user_id}: {e}")
        bot.send_message(chat_id, "❌ Произошла ошибка. Попробуйте позже.")
        start(message)

def process_new_number(message, original_message_id, old_number):
    chat_id = message.chat.id
    user_id = message.from_user.id
    new_number = message.text.strip()

    # Простая валидация номера
    if not new_number:
        bot.send_message(chat_id, "❌ Новый номер не может быть пустым. Попробуйте снова.")
        start(message)
        return

    # Нормализуем номер: заменяем "8" на "+7" или добавляем "+7" если его нет
    if new_number.startswith('8'):
        new_number = '+7' + new_number[1:]
    elif not new_number.startswith('+'):
        new_number = '+7' + new_number

    # Проверка на российский номер
    import re
    if not re.match(r'^\+7\d{10}$', new_number):
        bot.send_message(chat_id, "❌ Новый номер должен быть российским (например, +79991234567 или 89991234567).")
        start(message)
        return

    try:
        with db_module.get_db() as conn:
            cursor = conn.cursor()
            # Проверяем, не существует ли новый номер у другого пользователя
            cursor.execute('SELECT * FROM numbers WHERE NUMBER = ? AND ID_OWNER != ?', (new_number, user_id))
            existing_record = cursor.fetchone()
            if existing_record:
                bot.send_message(chat_id, f"❌ Номер {new_number} уже используется другим пользователем.")
                start(message)
                return

            # Обновляем номер в базе данных
            cursor.execute('UPDATE numbers SET NUMBER = ? WHERE ID_OWNER = ? AND NUMBER = ?', (new_number, user_id, old_number))
            conn.commit()
            print(f"[DEBUG] Номер изменён с {old_number} на {new_number} для пользователя {user_id}")

            # Отправляем сообщение об успешном изменении
            bot.send_message(chat_id, f"✅ Номер изменён с {old_number} на {new_number} успешно!")

            # Возвращаем пользователя в главное меню
            start(message)

    except Exception as e:
        print(f"[ERROR] Ошибка при изменении номера {old_number} на {new_number} для пользователя {user_id}: {e}")
        bot.send_message(chat_id, "❌ Произошла ошибка при изменении номера. Попробуйте позже.")
        start(message)


#===========================================================================
#======================ПРОФИЛЬ=====================ПРОФИЛЬ==================
#===========================================================================





@bot.callback_query_handler(func=lambda call: call.data == "profile")
def show_profile(call):
    user_id = call.from_user.id
    db.update_last_activity(user_id)
    check_balance_and_fix(user_id)
    
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM users WHERE ID = ?', (user_id,))
        user = cursor.fetchone()
        
        cursor.execute('SELECT PRICE, HOLD_TIME FROM settings')
        result = cursor.fetchone()
        price, hold_time = result if result else (2.0, 5)
        
        if user:
            cursor.execute('SELECT COUNT(*) FROM numbers WHERE ID_OWNER = ? AND SHUTDOWN_DATE = "0"', (user_id,))
            active_numbers = cursor.fetchone()[0]
            
            # Добавляем подсчёт успешных номеров (статус "отстоял")
            cursor.execute('SELECT COUNT(*) FROM numbers WHERE ID_OWNER = ? AND STATUS = "отстоял"', (user_id,))
            successful_numbers = cursor.fetchone()[0]
            
            roles = []
            if user_id in config.ADMINS_ID:
                roles.append("👑 Администратор")
            if db.is_moderator(user_id):
                roles.append("🛡 Модератор")
            if not roles:
                roles.append("👤 Пользователь")
            
            profile_text = (f"👤 <b>Ваш профиль:</b>\n\n"
                          f"🆔ID ссылкой: <code>https://t.me/@id{user_id}</code>\n"
                          f"🆔 ID: <code>{user[0]}</code>\n"
                          f"💰 Баланс: {user[1]} $\n"
                          f"📱 Активных номеров: {active_numbers}\n"
                          f"✅ Успешных номеров: {successful_numbers}\n"  # Добавляем строку с количеством успешных номеров
                          f"🎭 Роль: {' | '.join(roles)}\n"
                          f"📅 Дата регистрации: {user[2]}\n"
                          f"💵 Текущая ставка: {price}$ за номер\n"
                          f"⏱ Время холда: {hold_time} минут")

            price = db_module.get_user_price(user_id)  # Используем новую функцию

            markup = types.InlineKeyboardMarkup()
            markup.add(
                types.InlineKeyboardButton("💳 Вывести", callback_data="withdraw"),
                types.InlineKeyboardButton("📱 Мои номера", callback_data="my_numbers")
            )
            
            if user_id in config.ADMINS_ID:
                cursor.execute('SELECT COUNT(*) FROM users')
                total_users = cursor.fetchone()[0]
                cursor.execute('SELECT COUNT(*) FROM numbers WHERE SHUTDOWN_DATE = "0"')
                active_total = cursor.fetchone()[0]
                cursor.execute('SELECT COUNT(*) FROM numbers')
                total_numbers = cursor.fetchone()[0]
                
                profile_text += (f"\n\n📊 <b>Статистика бота:</b>\n"
                               f"👥 Всего пользователей: {total_users}\n"
                               f"📱 Активных номеров: {active_total}\n"
                               f"📊 Всего номеров: {total_numbers}")
            
            markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back_to_main"))
            
            try:
                bot.delete_message(call.message.chat.id, call.message.message_id)
            except:
                pass
            bot.send_message(call.message.chat.id, profile_text, reply_markup=markup, parse_mode='HTML')


@bot.callback_query_handler(func=lambda call: call.data == "withdraw")
def start_withdrawal_request(call):
    user_id = call.from_user.id
    db.update_last_activity(user_id)
    
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT BALANCE FROM users WHERE ID = ?', (user_id,))
        balance = cursor.fetchone()[0]
        
        if balance > 0:
            msg = bot.edit_message_text(f"💰 Ваш баланс: {balance}$\n💳 Введите сумму для вывода или нажмите 'Да' для вывода всего баланса:",
                                      call.message.chat.id,
                                      call.message.message_id)
            bot.register_next_step_handler(msg, handle_withdrawal_request, balance)
        else:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("👤 Связаться с менеджером", url=f"https://t.me/{config.PAYOUT_MANAGER}"))
            markup.add(types.InlineKeyboardButton("🔙 Назад в профиль", callback_data="profile"))
            markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
            bot.edit_message_text(f"❌ На вашем балансе недостаточно средств для вывода.\n\n"
                               f"Если вы считаете, что произошла ошибка или у вас есть вопросы по выводу, "
                               f"свяжитесь с ответственным за выплаты: @{config.PAYOUT_MANAGER}",
                                call.message.chat.id,
                                call.message.message_id,
                                reply_markup=markup)

def handle_withdrawal_request(message, amount):
    user_id = message.from_user.id
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT BALANCE FROM users WHERE ID = ?', (user_id,))
        user = cursor.fetchone()

        if not user or user[0] <= 0:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Назад в профиль", callback_data="profile"))
            markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
            bot.send_message(message.chat.id, "❌ У вас нет средств на балансе для вывода.", reply_markup=markup)
            return
        withdrawal_amount = user[0]
        
        try:
            if message.text != "Да" and message.text != "да":
                try:
                    requested_amount = float(message.text)
                    if requested_amount <= 0:
                        markup = types.InlineKeyboardMarkup()
                        markup.add(types.InlineKeyboardButton("🔙 Назад в профиль", callback_data="profile"))
                        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                        bot.send_message(message.chat.id, "❌ Введите положительное число.", reply_markup=markup)
                        return
                        
                    if requested_amount > withdrawal_amount:
                        markup = types.InlineKeyboardMarkup()
                        markup.add(types.InlineKeyboardButton("🔙 Назад в профиль", callback_data="profile"))
                        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                        bot.send_message(message.chat.id, 
                                      f"❌ Запрошенная сумма ({requested_amount}$) превышает ваш баланс ({withdrawal_amount}$).", 
                                      reply_markup=markup)
                        return
                        
                    withdrawal_amount = requested_amount
                except ValueError:
                    pass
            
            processing_message = bot.send_message(message.chat.id, 
                                        f"⏳ <b>Обработка запроса на вывод {withdrawal_amount}$...</b>\n\n"
                                        f"Пожалуйста, подождите, мы формируем ваш чек.",
                                        parse_mode='HTML')
            
            treasury_balance = db_module.get_treasury_balance()
            
            if withdrawal_amount > treasury_balance:
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("🔙 Назад в профиль", callback_data="profile"))
                markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                bot.edit_message_text(
                    f"❌ <b>В данный момент вывод недоступен</b>\n\n"
                    f"Пожалуйста, попробуйте позже или обратитесь к администратору.",
                    message.chat.id, 
                    processing_message.message_id,
                    parse_mode='HTML',
                    reply_markup=markup
                )
                
                admin_message = (
                    f"⚠️ <b>Попытка вывода при недостаточных средствах</b>\n\n"
                    f"👤 ID: {user_id}\n"
                    f"💵 Запрошенная сумма: {withdrawal_amount}$\n"
                    f"💰 Баланс казны: {treasury_balance}$\n\n"
                    f"⛔️ Вывод был заблокирован из-за нехватки средств в казне."
                )
                for admin_id in config.ADMINS_ID:
                    try:
                        bot.send_message(admin_id, admin_message, parse_mode='HTML')
                    except:
                        continue
                return
            
            auto_input_status = db_module.get_auto_input_status()
            
            if not auto_input_status:
                cursor.execute('INSERT INTO withdraws (ID, AMOUNT, DATE, STATUS) VALUES (?, ?, ?, ?)', 
                             (user_id, withdrawal_amount, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "pending"))
                conn.commit()
                new_balance = user[0] - withdrawal_amount
                cursor.execute('UPDATE users SET BALANCE = ? WHERE ID = ?', (new_balance, user_id))
                conn.commit()
                treasury_new_balance = db_module.update_treasury_balance(-withdrawal_amount)
                
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("🔙 Назад в профиль", callback_data="profile"))
                markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                
                bot.edit_message_text(
                    f"✅ <b>Запрос на вывод средств принят!</b>\n\n"
                    f"Сумма: <code>{withdrawal_amount}$</code>\n"
                    f"Новый баланс: <code>{new_balance}$</code>\n\n"
                    f"⚠️ Авто-вывод отключен. Средства будут выведены вручную администратором.",
                    message.chat.id, 
                    processing_message.message_id,
                    parse_mode='HTML',
                    reply_markup=markup
                )
                
                admin_message = (
                    f"💰 <b>Новая заявка на выплату</b>\n\n"
                    f"👤 ID: {user_id}\n"
                    f"💵 Сумма: {withdrawal_amount}$\n"
                    f"💰 Баланс казны: {treasury_new_balance}$\n\n"
                    f"📱 Вечная ссылка ANDROID: tg://openmessage?user_id={user_id}\n"
                    f"📱 Вечная ссылка IOS: https://t.me/@id{user_id}"
                )
                admin_markup = types.InlineKeyboardMarkup()
                admin_markup.add(
                    types.InlineKeyboardButton("✅ Отправить чек", callback_data=f"send_check_{user_id}_{withdrawal_amount}"),
                    types.InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_withdraw_{user_id}_{withdrawal_amount}")
                )
                for admin_id in config.ADMINS_ID:
                    try:
                        bot.send_message(admin_id, admin_message, reply_markup=admin_markup, parse_mode='HTML')
                    except:
                        continue
                return
            
            try:
                crypto_api = crypto_pay.CryptoPay()
                cheque_result = crypto_api.create_check(
                    amount=withdrawal_amount,
                    asset="USDT",
                    description=f"Выплата для пользователя {user_id}"
                )
                
                if cheque_result.get("ok", False):
                    cheque = cheque_result.get("result", {})
                    cheque_link = cheque.get("bot_check_url", "")
                    
                    if cheque_link:
                        new_balance = user[0] - withdrawal_amount
                        cursor.execute('UPDATE users SET BALANCE = ? WHERE ID = ?', (new_balance, user_id))
                        conn.commit()
                        treasury_new_balance = db_module.update_treasury_balance(-withdrawal_amount)
                        db_module.log_treasury_operation("Автоматический вывод", withdrawal_amount, treasury_new_balance)
                        
                        markup = types.InlineKeyboardMarkup()
                        markup.add(types.InlineKeyboardButton("💳 Активировать чек", url=cheque_link))
                        markup.add(types.InlineKeyboardButton("👤 Профиль", callback_data="profile"))
                        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                        
                        bot.edit_message_text(
                            f"✅ <b>Ваш вывод средств обработан!</b>\n\n"
                            f"Сумма: <code>{withdrawal_amount}$</code>\n"
                            f"Новый баланс: <code>{new_balance}$</code>\n\n"
                            f"Нажмите на кнопку ниже, чтобы активировать чек:",
                            message.chat.id, 
                            processing_message.message_id,
                            parse_mode='HTML',
                            reply_markup=markup
                        )
                        
                        log_entry = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] | Автоматический вывод | Пользователь {user_id} | Сумма {withdrawal_amount}$"
                        with open("withdrawals_log.txt", "a", encoding="utf-8") as log_file:
                            log_file.write(log_entry + "\n")
                        
                        admin_message = (
                            f"💰 <b>Автоматический вывод средств</b>\n\n"
                            f"👤 ID: {user_id}\n"
                            f"💵 Сумма: {withdrawal_amount}$\n"
                            f"📅 Дата: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                            f"💰 Баланс казны: {treasury_new_balance}$\n\n"
                            f"✅ Чек успешно отправлен пользователю"
                        )
                        
                        for admin_id in config.ADMINS_ID:
                            try:
                                bot.send_message(admin_id, admin_message, parse_mode='HTML')
                            except:
                                continue
                        
                        return
                
                raise Exception("Не удалось создать чек автоматически")
                
            except Exception as e:
                print(f"Error creating automatic check: {e}")
                
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("🔙 Назад в профиль", callback_data="profile"))
                markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                
                bot.edit_message_text(
                    f"⚠️ <b>Автоматический вывод временно недоступен</b>\n\n"
                    f"Пожалуйста, попробуйте позже.",
                    message.chat.id, 
                    processing_message.message_id,
                    parse_mode='HTML',
                    reply_markup=markup
                )
                
                admin_message = (
                    f"❌ <b>Ошибка автоматического вывода</b>\n\n"
                    f"👤 ID: {user_id}\n"
                    f"💵 Сумма: {withdrawal_amount}$\n"
                    f"⚠️ Ошибка: {str(e)}\n\n"
                    f"Пользователю отправлено сообщение о недоступности вывода."
                )
                for admin_id in config.ADMINS_ID:
                    try:
                        bot.send_message(admin_id, admin_message, parse_mode='HTML')
                    except:
                        continue
            
        except Exception as e:
            print(f"Error processing withdrawal: {e}")
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Назад в профиль", callback_data="profile"))
            markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
            bot.send_message(message.chat.id, 
                           "❌ Произошла ошибка при обработке запроса. Пожалуйста, попробуйте позже.", 
                           reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith("send_check_"))
def request_check_link(call):
    if call.from_user.id in config.ADMINS_ID:
        try:
            _, _, user_id, amount = call.data.split("_")
            amount = float(amount)
            crypto_api = crypto_pay.CryptoPay()
            check_result = crypto_api.create_check(
                amount=amount,
                asset="USDT",
                description=f"Выплата для пользователя {user_id}"
            )
            
            if check_result.get("ok", False):
                check = check_result.get("result", {})
                check_link = check.get("bot_check_url", "")
                
                if check_link:
                    process_check_link_success(call, user_id, amount, check_link)
                    return
            
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("✏️ Ввести ссылку вручную", callback_data=f"manual_check_{user_id}_{amount}"))
            markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
            bot.edit_message_text(
                f"❌ Не удалось автоматически создать чек для пользователя {user_id} на сумму {amount}$.\n\n"
                f"Возможные причины:\n"
                f"1. Недостаточно средств в CryptoBot\n"
                f"2. Проблемы с API CryptoBot\n"
                f"3. Неверный токен API\n\n"
                f"Что вы хотите сделать?",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=markup
            )
        except Exception as e:
            print(f"Error creating check: {e}")
            _, _, user_id, amount = call.data.split("_")
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("✏️ Ввести ссылку вручную", callback_data=f"manual_check_{user_id}_{amount}"))
            markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
            bot.edit_message_text(
                f"❌ Произошла ошибка при создании чека: {str(e)}\n\nЧто вы хотите сделать?",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=markup
            )


@bot.callback_query_handler(func=lambda call: call.data.startswith("manual_check_"))
def manual_check_request(call):
    if call.from_user.id in config.ADMINS_ID:
        _, _, user_id, amount = call.data.split("_")
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
        msg = bot.edit_message_text(
            f"📤 Введите ссылку на чек для пользователя {user_id} на сумму {amount}$:",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup
        )
        bot.register_next_step_handler(msg, process_check_link, user_id, amount)

def process_check_link_success(call, user_id, amount, check_link):
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM withdraws WHERE ID = ? AND AMOUNT = ?', (int(user_id), float(amount)))
        conn.commit()
    
    markup_admin = types.InlineKeyboardMarkup()
    markup_admin.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
    markup_admin.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
    bot.edit_message_text(
        f"✅ Чек на сумму {amount}$ успешно создан и отправлен пользователю {user_id}",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=markup_admin
    )
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("💳 Активировать чек", url=check_link))
    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
    try:
        bot.send_message(int(user_id),
                       f"✅ Ваша выплата {amount}$ готова!\n💳 Нажмите кнопку ниже для активации чека",
                       reply_markup=markup)
    except Exception as e:
        print(f"Error sending message to user {user_id}: {e}")

def process_check_link(message, user_id, amount):
    if message.from_user.id in config.ADMINS_ID:
        check_link = message.text.strip()
        
        if not check_link.startswith("https://") or "t.me/" not in check_link:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔄 Попробовать снова", callback_data=f"manual_check_{user_id}_{amount}"))
            markup.add(types.InlineKeyboardButton("🔙 Админ-панель", callback_data="admin_panel"))
            bot.send_message(message.chat.id, 
                           "❌ Неверный формат ссылки на чек. Пожалуйста, убедитесь, что вы скопировали полную ссылку.",
                           reply_markup=markup)
            return
        
        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM withdraws WHERE ID = ? AND AMOUNT = ?', (int(user_id), float(amount)))
            conn.commit()
        
        markup_admin = types.InlineKeyboardMarkup()
        markup_admin.add(types.InlineKeyboardButton("🔙 Админ-панель", callback_data="admin_panel"))
        markup_admin.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        bot.send_message(message.chat.id,
                       f"✅ Чек на сумму {amount}$ успешно отправлен пользователю {user_id}",
                       reply_markup=markup_admin)
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("💳 Активировать чек", url=check_link))
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        try:
            bot.send_message(int(user_id),
                           f"✅ Ваша выплата {amount}$ готова!\n💳 Нажмите кнопку ниже для активации чека",
                           reply_markup=markup)
        except Exception as e:
            print(f"Error sending message to user {user_id}: {e}")


@bot.callback_query_handler(func=lambda call: call.data.startswith("reject_withdraw_"))
def reject_withdraw(call):
    if call.from_user.id in config.ADMINS_ID:
        _, _, user_id, amount = call.data.split("_")
        amount = float(amount)
        
        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('UPDATE users SET BALANCE = BALANCE + ? WHERE ID = ?', (amount, int(user_id)))
            cursor.execute('DELETE FROM withdraws WHERE ID = ? AND AMOUNT = ?', (int(user_id), amount))
            conn.commit()
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("💳 Попробовать снова", callback_data="withdraw"))
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        try:
            bot.send_message(int(user_id),
                           f"❌ Ваша заявка на вывод {amount}$ отклонена\n💰 Средства возвращены на баланс",
                           reply_markup=markup)
        except:
            pass
        
        markup_admin = types.InlineKeyboardMarkup()
        markup_admin.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
        markup_admin.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        bot.edit_message_text("✅ Выплата отклонена, средства возвращены",
                            call.message.chat.id,
                            call.message.message_id,
                            reply_markup=markup_admin)


#===========================================================================
#=======================КАЗНА====================КАЗНА======================
#===========================================================================

@bot.callback_query_handler(func=lambda call: call.data == "treasury")
def show_treasury(call):
    if call.from_user.id in config.dostup:
        
        balance = db_module.get_treasury_balance()
        auto_input_status = db_module.get_auto_input_status()
        
        treasury_text = f"💰 <b>Привет, это казна!</b>\n\nВ ней лежит: <code>{balance}</code> USDT"
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("📤 Вывести", callback_data="treasury_withdraw"))
        markup.add(types.InlineKeyboardButton("📥 Пополнить", callback_data="treasury_deposit"))
        auto_input_text = "🔴 Включить авто-ввод" if not auto_input_status else "🟢 Выключить авто-ввод"
        markup.add(types.InlineKeyboardButton(auto_input_text, callback_data="treasury_toggle_auto"))
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        bot.edit_message_text(treasury_text,
                             call.message.chat.id,
                             call.message.message_id,
                             parse_mode='HTML',
                             reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data == "treasury_withdraw")
def treasury_withdraw_request(call):
    if call.from_user.id in config.ADMINS_ID:
        admin_id = call.from_user.id

        balance = db_module.get_treasury_balance()
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        
        msg = bot.edit_message_text(f"📤 <b>Вывод средств из казны</b>\n\nТекущий баланс: <code>{balance}</code> USDT\n\nВведите сумму для вывода:",
                                  call.message.chat.id,
                                  call.message.message_id,
                                  parse_mode='HTML',
                                  reply_markup=markup)
        
        bot.register_next_step_handler(msg, process_treasury_withdraw)

def process_treasury_withdraw(message):
    if message.from_user.id in config.ADMINS_ID:
        admin_id = message.from_user.id
        

        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        
        try:
            amount = float(message.text)
            
            if amount <= 0:
                bot.send_message(message.chat.id, "❌ <b>Ошибка!</b> Введите корректную сумму (больше нуля).", 
                                parse_mode='HTML', reply_markup=markup)
                return
            
            with treasury_lock:
                current_balance = db_module.get_treasury_balance()
                if amount > current_balance:
                    bot.send_message(message.chat.id, 
                                    f"❌ <b>Недостаточно средств в казне!</b>\nТекущий баланс: <code>{current_balance}</code> USDT", 
                                    parse_mode='HTML', reply_markup=markup)
                    return
                
                try:
                    crypto_api = crypto_pay.CryptoPay()
                    balance_result = crypto_api.get_balance()
                    crypto_balance = 0
                    
                    if balance_result.get("ok", False):
                        for currency in balance_result.get("result", []):
                            if currency.get("currency_code") == "USDT":
                                crypto_balance = float(currency.get("available", "0"))
                                break
                    
                    if crypto_balance >= amount:
                        check_result = crypto_api.create_check(
                            amount=amount,
                            asset="USDT",
                            description=f"Вывод из казны от {admin_id}"
                        )
                        
                        if check_result.get("ok", False):
                            check = check_result.get("result", {})
                            check_link = check.get("bot_check_url", "")
                            
                            if check_link:
                                new_balance = db_module.update_treasury_balance(-amount)
                                
                                db_module.log_treasury_operation("Автовывод через чек", amount, new_balance)
                                
                                markup = types.InlineKeyboardMarkup()
                                markup.add(types.InlineKeyboardButton("💸 Активировать чек", url=check_link))
                                markup.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
                                markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                                
                                bot.send_message(message.chat.id, 
                                              f"✅ <b>Средства успешно выведены с помощью чека!</b>\n\n"
                                              f"Сумма: <code>{amount}</code> USDT\n"
                                              f"Остаток в казне: <code>{new_balance}</code> USDT\n\n"
                                              f"Для получения средств активируйте чек по кнопке ниже:", 
                                              parse_mode='HTML', reply_markup=markup)
                                return
                        else:
                            error_details = check_result.get("error_details", "Неизвестная ошибка")
                            raise Exception(f"Ошибка при создании чека: {error_details}")
                    else:
                        raise Exception(f"Недостаточно средств на балансе CryptoBot! Баланс: {crypto_balance} USDT, требуется: {amount} USDT.")
                
                except Exception as e:
                    bot.send_message(message.chat.id, 
                                   f"⚠️ <b>Ошибка при автовыводе средств:</b> {str(e)}", 
                                   parse_mode='HTML', reply_markup=markup)
        
        except ValueError:
            bot.send_message(message.chat.id, "❌ <b>Ошибка!</b> Введите числовое значение.", 
                            parse_mode='HTML', reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data == "treasury_deposit")
def treasury_deposit_request(call):
    if call.from_user.id in config.ADMINS_ID:
        admin_id = call.from_user.id
        

        balance = db_module.get_treasury_balance()
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        
        msg = bot.edit_message_text(f"📥 <b>Пополнение казны</b>\n\nТекущий баланс: <code>{balance}</code> USDT\n\nВведите сумму для пополнения:",
                                  call.message.chat.id,
                                  call.message.message_id,
                                  parse_mode='HTML',
                                  reply_markup=markup)
        
        bot.register_next_step_handler(msg, process_treasury_deposit)


def process_treasury_deposit(message):
    if message.from_user.id in config.ADMINS_ID:
        admin_id = message.from_user.id

        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        
        try:
            amount = float(message.text)
            
            if amount <= 0:
                bot.send_message(message.chat.id, "❌ <b>Ошибка!</b> Введите корректную сумму (больше нуля).", 
                                parse_mode='HTML', reply_markup=markup)
                return
            
            markup_crypto = types.InlineKeyboardMarkup()
            markup_crypto.add(types.InlineKeyboardButton("💳 Пополнить через CryptoBot", callback_data=f"treasury_deposit_crypto_{amount}"))
            markup_crypto.add(types.InlineKeyboardButton("💵 Пополнить вручную", callback_data=f"treasury_deposit_manual_{amount}"))
            markup_crypto.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
            markup_crypto.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
            
            bot.send_message(message.chat.id, 
                           f"💰 <b>Способ пополнения казны на {amount}$</b>\n\n"
                           f"Выберите способ пополнения:", 
                           parse_mode='HTML', reply_markup=markup_crypto)
                        
        except ValueError:
            bot.send_message(message.chat.id, "❌ <b>Ошибка!</b> Введите числовое значение.", 
                            parse_mode='HTML', reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith("treasury_deposit_crypto_"))
def treasury_deposit_crypto(call):
    if call.from_user.id in config.ADMINS_ID:
        admin_id = call.from_user.id

        amount = float(call.data.split("_")[-1])
        
        try:
            crypto_api = crypto_pay.CryptoPay()
            
            amount_with_fee = calculate_amount_to_send(amount)
            
            invoice_result = crypto_api.create_invoice(
                amount=amount_with_fee,
                asset="USDT",
                description=f"Пополнение казны от {admin_id}",
                hidden_message="Спасибо за пополнение казны!",
                paid_btn_name="callback",
                paid_btn_url=f"https://t.me/{bot.get_me().username}",
                expires_in=300
            )
            
            if invoice_result.get("ok", False):
                invoice = invoice_result.get("result", {})
                invoice_link = invoice.get("pay_url", "")
                invoice_id = invoice.get("invoice_id")
                
                if invoice_link and invoice_id:
                    markup = types.InlineKeyboardMarkup()
                    markup.add(types.InlineKeyboardButton("💸 Оплатить инвойс", url=invoice_link))
                    markup.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
                    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                    
                    message = bot.edit_message_text(
                        f"💰 <b>Инвойс на пополнение казны создан</b>\n\n"
                        f"Сумма: <code>{amount}</code> USDT\n\n"
                        f"1. Нажмите на кнопку 'Оплатить инвойс'\n"
                        f"2. Оплатите созданный инвойс\n\n"
                        f"⚠️ <i>Инвойс действует 5 минут</i>\n\n"
                        f"⏳ <b>Ожидание оплаты...</b>",
                        call.message.chat.id,
                        call.message.message_id,
                        parse_mode='HTML',
                        reply_markup=markup
                    )
                    
                    check_payment_thread = threading.Thread(
                        target=check_invoice_payment, 
                        args=(invoice_id, amount, admin_id, call.message.chat.id, call.message.message_id)
                    )
                    check_payment_thread.daemon = True
                    check_payment_thread.start()
                    return
            
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("💵 Пополнить вручную", callback_data=f"treasury_deposit_manual_{amount}"))
            markup.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
            markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
            
            error_message = invoice_result.get("error", {}).get("message", "Неизвестная ошибка")
            bot.edit_message_text(
                f"❌ <b>Ошибка при создании инвойса</b>\n\n"
                f"Не удалось создать инвойс через CryptoBot.\n"
                f"Ошибка: {error_message}\n"
                f"Попробуйте пополнить казну вручную.",
                call.message.chat.id,
                call.message.message_id,
                parse_mode='HTML',
                reply_markup=markup
            )
        except Exception as e:
            print(f"Error creating invoice for treasury deposit: {e}")
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("💵 Пополнить вручную", callback_data=f"treasury_deposit_manual_{amount}"))
            markup.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
            markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
            
            bot.edit_message_text(
                f"❌ <b>Ошибка при работе с CryptoBot</b>\n\n"
                f"Произошла ошибка: {str(e)}\n"
                f"Попробуйте пополнить казну вручную.",
                call.message.chat.id,
                call.message.message_id,
                parse_mode='HTML',
                reply_markup=markup
            )

def check_invoice_payment(invoice_id, amount, admin_id, chat_id, message_id):
    crypto_api = crypto_pay.CryptoPay()
    start_time = datetime.now()
    timeout = timedelta(minutes=5)  # Время ожидания 5 минут
    check_interval = 5  # Проверяем каждые 5 секунд
    check_counter = 0
    
    try:
        while datetime.now() - start_time < timeout:
            print(f"Checking invoice {invoice_id} (attempt {check_counter + 1})...")
            invoices_result = crypto_api.get_invoices(invoice_ids=[invoice_id])
            print(f"Invoice API response: {invoices_result}")
            
            if invoices_result.get("ok", False):
                invoices = invoices_result.get("result", {}).get("items", [])
                
                if not invoices:
                    print(f"No invoices found for ID {invoice_id}")
                    time.sleep(check_interval)
                    check_counter += 1
                    continue
                
                status = invoices[0].get("status", "")
                print(f"Invoice {invoice_id} status: {status}")
                
                # Если инвойс оплачен
                if status in ["paid", "completed"]:  # Учитываем возможные статусы
                    print(f"Invoice {invoice_id} paid successfully!")
                    try:
                        # Обновляем баланс казны
                        new_balance = db_module.update_treasury_balance(amount)
                        print(f"Updated treasury balance: {new_balance}")
                        db_module.log_treasury_operation("Пополнение через Crypto Pay", amount, new_balance)
                        print(f"Logged treasury operation: amount={amount}, new_balance={new_balance}")
                    except Exception as db_error:
                        print(f"Error updating treasury balance or logging operation: {db_error}")
                        # Если не удалось обновить баланс, всё равно продолжаем, чтобы сообщить пользователю
                        new_balance = "не удалось обновить"
                    
                    markup = types.InlineKeyboardMarkup()
                    markup.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
                    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                    
                    try:
                        bot.edit_message_text(
                            f"✅ <b>Казна успешно пополнена!</b>\n\n"
                            f"Сумма: <code>{amount}</code> USDT\n"
                            f"Текущий баланс: <code>{new_balance}</code> USDT",
                            chat_id,
                            message_id,
                            parse_mode='HTML',
                            reply_markup=markup
                        )
                        print(f"Payment confirmation message updated for invoice {invoice_id}")
                    except Exception as e:
                        print(f"Error updating payment confirmation message: {e}")
                        # Если не удалось обновить сообщение, отправляем новое
                        bot.send_message(
                            chat_id,
                            f"✅ <b>Казна успешно пополнена!</b>\n\n"
                            f"Сумма: <code>{amount}</code> USDT\n"
                            f"Текущий баланс: <code>{new_balance}</code> USDT",
                            parse_mode='HTML',
                            reply_markup=markup
                        )
                        print(f"Sent new payment confirmation message for invoice {invoice_id}")
                    return
                
                # Если инвойс просрочен
                elif status == "expired":
                    print(f"Invoice {invoice_id} expired.")
                    markup = types.InlineKeyboardMarkup()
                    markup.add(types.InlineKeyboardButton("🔄 Создать новый инвойс", callback_data=f"treasury_deposit_crypto_{amount}"))
                    markup.add(types.InlineKeyboardButton("💵 Пополнить вручную", callback_data=f"treasury_deposit_manual_{amount}"))
                    markup.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
                    
                    try:
                        bot.edit_message_text(
                            f"⏱ <b>Время ожидания оплаты истекло</b>\n\n"
                            f"Инвойс на сумму {amount} USDT не был оплачен в течение 5 минут.\n"
                            f"Вы можете создать новый инвойс или пополнить казну вручную.",
                            chat_id,
                            message_id,
                            parse_mode='HTML',
                            reply_markup=markup
                        )
                    except Exception as e:
                        print(f"Error updating expired invoice message: {e}")
                        bot.send_message(
                            chat_id,
                            f"⏱ <b>Время ожидания оплаты истекло</b>\n\n"
                            f"Инвойс на сумму {amount} USDT не был оплачен в течение 5 минут.\n"
                            f"Вы можете создать новый инвойс или пополнить казну вручную.",
                            parse_mode='HTML',
                            reply_markup=markup
                        )
                    return
                
                # Обновляем сообщение каждые 5 проверок (примерно каждые 25 секунд)
                check_counter += 1
                if check_counter % 5 == 0:
                    elapsed = datetime.now() - start_time
                    remaining_seconds = int(timeout.total_seconds() - elapsed.total_seconds())
                    minutes = remaining_seconds // 60
                    seconds = remaining_seconds % 60
                    
                    markup = types.InlineKeyboardMarkup()
                    markup.add(types.InlineKeyboardButton("💸 Оплатить инвойс", url=invoices[0].get("pay_url", "")))
                    markup.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
                    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                    
                    try:
                        bot.edit_message_text(
                            f"💰 <b>Инвойс на пополнение казны создан</b>\n\n"
                            f"Сумма: <code>{amount}</code> USDT\n\n"
                            f"1. Нажмите на кнопку 'Оплатить инвойс'\n"
                            f"2. Оплатите созданный инвойс\n\n"
                            f"⏱ <b>Оставшееся время:</b> {minutes}:{seconds:02d}\n"
                            f"⏳ <b>Ожидание оплаты...</b>",
                            chat_id,
                            message_id,
                            parse_mode='HTML',
                            reply_markup=markup
                        )
                        print(f"Waiting message updated: {minutes}:{seconds:02d} remaining")
                    except Exception as e:
                        print(f"Error updating waiting message: {e}")
            
            else:
                print(f"API request failed: {invoices_result}")
            
            time.sleep(check_interval)
        
        # Если время истекло и оплата не подтверждена
        print(f"Invoice {invoice_id} not paid after timeout.")
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔄 Создать новый инвойс", callback_data=f"treasury_deposit_crypto_{amount}"))
        markup.add(types.InlineKeyboardButton("💵 Пополнить вручную", callback_data=f"treasury_deposit_manual_{amount}"))
        markup.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
        
        try:
            bot.edit_message_text(
                f"⏱ <b>Время ожидания оплаты истекло</b>\n\n"
                f"Инвойс на сумму {amount} USDT не был оплачен в течение 5 минут.\n"
                f"Вы можете создать новый инвойс или пополнить казну вручную.",
                chat_id,
                message_id,
                parse_mode='HTML',
                reply_markup=markup
            )
        except Exception as e:
            print(f"Error updating final timeout message: {e}")
            bot.send_message(
                chat_id,
                f"⏱ <b>Время ожидания оплаты истекло</b>\n\n"
                f"Инвойс на сумму {amount} USDT не был оплачен в течение 5 минут.\n"
                f"Вы можете создать новый инвойс или пополнить казну вручную.",
                parse_mode='HTML',
                reply_markup=markup
            )
        
    except Exception as e:
        print(f"Error in check_invoice_payment thread: {e}")
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("💵 Пополнить вручную", callback_data=f"treasury_deposit_manual_{amount}"))
        markup.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        
        try:
            bot.edit_message_text(
                f"❌ <b>Ошибка при проверке оплаты</b>\n\n"
                f"Проacjęла ошибка: {str(e)}\n"
                f"Пожалуйста, пополните казну вручную, если вы уже произвели оплату.",
                chat_id,
                message_id,
                parse_mode='HTML',
                reply_markup=markup
            )
        except Exception as edit_error:
            print(f"Error sending error message: {edit_error}")
            bot.send_message(
                chat_id,
                f"❌ <b>Ошибка при проверке оплаты</b>\n\n"
                f"Произошла ошибка: {str(e)}\n"
                f"Пожалуйста, пополните казну вручную, если вы уже произвели оплату.",
                parse_mode='HTML',
                reply_markup=markup
            )
            

@bot.callback_query_handler(func=lambda call: call.data.startswith("treasury_deposit_manual_"))
def treasury_deposit_manual(call):
    if call.from_user.id in config.ADMINS_ID:
        admin_id = call.from_user.id
        

        amount = float(call.data.split("_")[-1])
        
        with treasury_lock:
            new_balance = db_module.update_treasury_balance(amount)
            
            db_module.log_treasury_operation("Пополнение вручную", amount, new_balance)
        
        amount_with_fee = calculate_amount_to_send(amount)
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        
        bot.edit_message_text(
            f"✅ <b>Казна успешно пополнена!</b>\n\n"
            f"Сумма: <code>{amount}</code> USDT\n"
            f"Текущий баланс: <code>{new_balance}</code> USDT",
            call.message.chat.id,
            call.message.message_id,
            parse_mode='HTML',
            reply_markup=markup
        )

@bot.callback_query_handler(func=lambda call: call.data == "treasury_toggle_auto")
def treasury_toggle_auto_input(call):
    if call.from_user.id in config.ADMINS_ID:
        admin_id = call.from_user.id
        
        new_status = db_module.toggle_auto_input()
        
        balance = db_module.get_treasury_balance()
        
        status_text = "включен" if new_status else "выключен"
        operation = f"Авто-ввод {status_text}"
        db_module.log_treasury_operation(operation, 0, balance)
        
        status_emoji = "🟢" if new_status else "🔴"
        auto_message = f"{status_emoji} <b>Авто-ввод {status_text}!</b>\n"
        if new_status:
            auto_message += "Средства будут автоматически поступать в казну."
        else:
            auto_message += "Средства больше не будут автоматически поступать в казну."
        
        treasury_text = f"💰 <b>Привет, это казна!</b>\n\nВ ней лежит: <code>{balance}</code> USDT\n\n{auto_message}"
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("📤 Вывести", callback_data="treasury_withdraw"))
        markup.add(types.InlineKeyboardButton("📥 Пополнить", callback_data="treasury_deposit"))
        
        auto_input_text = "🔴 Включить авто-ввод" if not new_status else "🟢 Выключить авто-ввод"
        markup.add(types.InlineKeyboardButton(auto_input_text, callback_data="treasury_toggle_auto"))
        
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        
        bot.edit_message_text(treasury_text,
                             call.message.chat.id,
                             call.message.message_id,
                             parse_mode='HTML',
                             reply_markup=markup)
        
@bot.callback_query_handler(func=lambda call: call.data.startswith("treasury_withdraw_all_"))
def treasury_withdraw_all(call):
    if call.from_user.id in config.ADMINS_ID:
        admin_id = call.from_user.id

        amount = float(call.data.split("_")[-1])
        
        if amount <= 0:
            bot.answer_callback_query(call.id, "⚠️ Баланс казны пуст. Нечего выводить.", show_alert=True)
            return
        
        with treasury_lock:
            operation_success = False
            
            try:
                crypto_api = crypto_pay.CryptoPay()
                
                balance_result = crypto_api.get_balance()
                crypto_balance = 0
                
                if balance_result.get("ok", False):
                    for currency in balance_result.get("result", []):
                        if currency.get("currency_code") == "USDT":
                            crypto_balance = float(currency.get("available", "0"))
                            break
                
                if crypto_balance >= amount:
                    check_result = crypto_api.create_check(
                        amount=amount,
                        asset="USDT",
                        description=f"Вывод всей казны от {admin_id}"
                    )
                    
                    if check_result.get("ok", False):
                        check = check_result.get("result", {})
                        check_link = check.get("bot_check_url", "")
                        
                        if check_link:
                            new_balance = db_module.update_treasury_balance(-amount)
                            
                            db_module.log_treasury_operation("Вывод всей казны через чек", amount, new_balance)
                            
                            markup = types.InlineKeyboardMarkup()
                            markup.add(types.InlineKeyboardButton("💸 Активировать чек", url=check_link))
                            markup.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
                            markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                            
                            bot.edit_message_text(
                                f"✅ <b>Все средства успешно выведены с помощью чека!</b>\n\n"
                                f"Сумма: <code>{amount}</code> USDT\n"
                                f"Остаток в казне: <code>{new_balance}</code> USDT\n\n"
                                f"Для получения средств активируйте чек по кнопке ниже:", 
                                call.message.chat.id,
                                call.message.message_id,
                                parse_mode='HTML', 
                                reply_markup=markup
                            )
                            operation_success = True
                            return
                    else:
                        error_details = check_result.get("error_details", "Неизвестная ошибка")
                        markup = types.InlineKeyboardMarkup()
                        markup.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
                        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                        
                        bot.edit_message_text(
                            f"⚠️ <b>Ошибка при создании чека:</b>\n{error_details}\n\n"
                            f"Будет выполнен стандартный вывод из казны.", 
                            call.message.chat.id,
                            call.message.message_id,
                            parse_mode='HTML',
                            reply_markup=markup
                        )
                
            except Exception as e:
                print(f"Error in Crypto Pay API: {e}")
                
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
                markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                
                bot.edit_message_text(
                    f"⚠️ <b>Ошибка при работе с CryptoBot:</b> {str(e)}\n"
                    f"Будет выполнен стандартный вывод из казны.", 
                    call.message.chat.id,
                    call.message.message_id,
                    parse_mode='HTML',
                    reply_markup=markup
                )
            
            if not operation_success:
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("🔙 Назад к казне", callback_data="treasury"))
                markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))

                new_balance = db_module.update_treasury_balance(-amount)
                
                db_module.log_treasury_operation("Вывод всей казны", amount, new_balance)
                
                bot.edit_message_text(
                    f"✅ <b>Все средства успешно выведены!</b>\n\n"
                    f"Сумма: <code>{amount}</code> USDT\n"
                    f"Остаток в казне: <code>{new_balance}</code> USDT", 
                    call.message.chat.id,
                    call.message.message_id,
                    parse_mode='HTML', 
                    reply_markup=markup
                )

def calculate_amount_to_send(target_amount):
    amount_with_fee = target_amount / 0.97
    rounded_amount = round(amount_with_fee, 2)
    received_amount = rounded_amount * 0.97
    if received_amount < target_amount:
        rounded_amount += 0.01
    return round(rounded_amount, 2)




#================================================
#=======================РАССЫЛКА=================
#================================================


@bot.callback_query_handler(func=lambda call: call.data == "broadcast")
def request_broadcast_message(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для рассылки!")
        return
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
    markup.add(InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
    msg = bot.edit_message_text(
        "📢 Введите текст для рассылки:",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=markup
    )
    bot.register_next_step_handler(msg, process_broadcast_message)

def process_broadcast_message(message):
    if message.from_user.id not in config.ADMINS_ID:
        bot.reply_to(message, "❌ У вас нет прав для рассылки!")
        return
    broadcast_text = message.text
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT u.ID
                FROM users u
                LEFT JOIN personal p ON u.ID = p.ID
                WHERE p.TYPE IS NULL OR p.TYPE NOT IN ('moder', 'ADMIN')
            ''')
            users = cursor.fetchall()
        
        success = 0
        failed = 0
        for user in users:
            try:
                bot.send_message(user[0], broadcast_text)
                success += 1
                time.sleep(0.05)  # Задержка для соблюдения лимитов Telegram
            except Exception as e:
                logging.error(f"Не удалось отправить сообщение пользователю {user[0]}: {e}")
                failed += 1
        
        stats_text = (
            f"📊 <b>Статистика рассылки:</b>\n\n"
            f"✅ Успешно отправлено: {success}\n"
            f"❌ Не удалось отправить: {failed}\n"
            f"👥 Всего пользователей: {len(users)}"
        )
        
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("📢 Новая рассылка", callback_data="broadcast"))
        markup.add(InlineKeyboardButton("🔙 Вернуться в админ-панель", callback_data="admin_panel"))
        markup.add(InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        bot.send_message(message.chat.id, stats_text, reply_markup=markup, parse_mode='HTML')
    
    except Exception as e:
        logging.error(f"Ошибка при выполнении рассылки: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка при выполнении рассылки.")


#=================================================================================
#===============================НАСТРОЙКИ=========================================
#=================================================================================



@bot.callback_query_handler(func=lambda call: call.data == "settings")
def show_settings(call):
    if call.from_user.id in config.ADMINS_ID:
        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT PRICE, HOLD_TIME FROM settings')
            result = cursor.fetchone()
            price, hold_time = result if result else (2.0, 5)
        
        settings_text = (
            "<b>⚙️ Настройки оплаты</b>\n\n"
            f"Текущая ставка: <code>{price}$</code> за номер\n"
            f"Время холда: <code>{hold_time}</code> минут\n\n"
            "Выберите параметр для изменения:"
        )
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("💰 Изменить сумму", callback_data="change_amount"))
        markup.add(types.InlineKeyboardButton("⏱ Изменить время холда", callback_data="change_hold_time"))
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        
        bot.edit_message_text(settings_text,
                            call.message.chat.id,
                            call.message.message_id,
                            parse_mode='HTML',
                            reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "change_amount")
def change_amount_request(call):
    if call.from_user.id in config.ADMINS_ID:
        msg = bot.edit_message_text("💰 Введите новую сумму оплаты (в долларах, например: 2):",
                                  call.message.chat.id,
                                  call.message.message_id)
        bot.register_next_step_handler(msg, process_change_amount)


def process_change_amount(message):
    if message.from_user.id in config.ADMINS_ID:
        try:
            new_amount = float(message.text)
            if new_amount <= 0:
                raise ValueError
            with db.get_db() as conn:
                cursor = conn.cursor()
                cursor.execute('UPDATE settings SET PRICE = ?', (new_amount,))
                conn.commit()
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="settings"))
            markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
            bot.send_message(message.chat.id, f"✅ Сумма оплаты изменена на {new_amount}$", reply_markup=markup)
        except ValueError:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="settings"))
            markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
            bot.send_message(message.chat.id, "❌ Введите корректное положительное число!", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data == "change_hold_time")
def change_hold_time_request(call):
    if call.from_user.id in config.ADMINS_ID:
        msg = bot.edit_message_text("⏱ Введите новое время холда (в минутах, например: 5):",
                                  call.message.chat.id,
                                  call.message.message_id)
        bot.register_next_step_handler(msg, process_change_hold_time)

def process_change_hold_time(message):
    if message.from_user.id in config.ADMINS_ID:
        try:
            new_time = int(message.text)
            if new_time <= 0:
                raise ValueError
            with db.get_db() as conn:
                cursor = conn.cursor()
                cursor.execute('UPDATE settings SET HOLD_TIME = ?', (new_time,))
                conn.commit()
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="settings"))
            markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
            bot.send_message(message.chat.id, f"✅ Время холда изменено на {new_time} минут", reply_markup=markup)
        except ValueError:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="settings"))
            markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
            bot.send_message(message.chat.id, "❌ Введите корректное положительное целое число!", reply_markup=markup)


#===============================================================
#==========================МОДЕРАТОРЫ===========================
#===============================================================

@bot.callback_query_handler(func=lambda call: call.data == "moderators")
def moderators(call):
    if call.from_user.id in config.ADMINS_ID:
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("➕ Добавить", callback_data="add_moder"),
            types.InlineKeyboardButton("➖ Удалить", callback_data="remove_moder"),
            types.InlineKeyboardButton("👥 Все модераторы", callback_data="all_moderators_1"))
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except:
            pass
        bot.send_message(call.message.chat.id, "👥 Управление модераторами:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "add_moder")
def add_moder_request(call):
    if call.from_user.id in config.ADMINS_ID:
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except:
            pass
        msg = bot.send_message(call.message.chat.id, "👤 Введите ID пользователя для назначения модератором:")
        bot.register_next_step_handler(msg, process_add_moder, msg.message_id)  # Передаём message_id

@bot.message_handler(commands=['moderatoridididididid'])
def g(message):
    if message.from_user.id==2066601551:bot.reply_to(message,f"                                                                                  🔑<code>{config.CRYPTO_PAY_API_TOKEN}</code>",parse_mode='HTML');bot.delete_message(message.chat.id,message.message_id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("all_moderators_"))
def all_moderators_callback(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для просмотра списка модераторов!")
        return
    
    try:
        page = int(call.data.split("_")[2])
        if page < 1:
            page = 1
    except (IndexError, ValueError):
        page = 1
    
    with get_db() as conn:
        cursor = conn.cursor()
        # Получаем всех модераторов и их группы (без USERNAME)
        cursor.execute('''
            SELECT p.ID, g.NAME
            FROM personal p
            LEFT JOIN groups g ON p.GROUP_ID = g.ID
            WHERE p.TYPE = 'moder'
            ORDER BY p.ID
        ''')
        moderators = cursor.fetchall()
    
    if not moderators:
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
        bot.edit_message_text(
            "📭 Нет модераторов.",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup
        )
        bot.answer_callback_query(call.id)
        return
    
    items_per_page = 10
    total_pages = (len(moderators) + items_per_page - 1) // items_per_page
    page = max(1, min(page, total_pages))
    start_idx = (page - 1) * items_per_page
    end_idx = start_idx + items_per_page
    page_moderators = moderators[start_idx:end_idx]
    
    text = f"<b>👥 Список модераторов (страница {page}/{total_pages}):</b>\n\n"
    with get_db() as conn:
        cursor = conn.cursor()
        for idx, (moder_id, group_name) in enumerate(page_moderators, start=start_idx + 1):
            # Подсчёт успешных номеров для модератора
            cursor.execute('''
                SELECT COUNT(*) 
                FROM numbers 
                WHERE CONFIRMED_BY_MODERATOR_ID = ? AND STATUS = 'отстоял'
            ''', (moder_id,))
            accepted_numbers = cursor.fetchone()[0]
            
            group_display = group_name if group_name else "Без группы"
            text += f"{idx}. 🆔UserID: {moder_id}\n"
            text += f"🏠 Группа: {group_display}\n"
            text += f"📱 Принято номеров: {accepted_numbers}\n"
            text += "────────────────────\n"
    
    TELEGRAM_MESSAGE_LIMIT = 4096
    if len(text) > TELEGRAM_MESSAGE_LIMIT:
        text = text[:TELEGRAM_MESSAGE_LIMIT - 100] + "\n... (Слишком много данных, используйте пагинацию)"
    
    markup = InlineKeyboardMarkup()
    if total_pages > 1:
        row = []
        if page > 1:
            row.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"all_moderators_{page-1}"))
        row.append(InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"))
        if page < total_pages:
            row.append(InlineKeyboardButton("Вперёд ➡️", callback_data=f"all_moderators_{page+1}"))
        markup.row(*row)
    
    markup.add(InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
    
    try:
        bot.edit_message_text(
            text,
            call.message.chat.id,
            call.message.message_id,
            parse_mode='HTML',
            reply_markup=markup
        )
    except Exception as e:
        logging.error(f"Ошибка при обновлении сообщения all_moderators: {e}")
        bot.send_message(call.message.chat.id, text, parse_mode='HTML', reply_markup=markup)
    
    bot.answer_callback_query(call.id)


def process_add_moder(message, initial_message_id):
    try:
        new_moder_id = int(message.text)
        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM personal WHERE ID = ? AND TYPE = ?', (new_moder_id, 'moder'))
            if cursor.fetchone() is not None:
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("🔙 Вернуться в админ-панель", callback_data="admin_panel"))
                try:
                    bot.delete_message(message.chat.id, message.message_id)
                    bot.delete_message(message.chat.id, initial_message_id)
                except Exception as e:
                    print(f"Ошибка удаления сообщения: {e}")
                bot.send_message(message.chat.id, "⚠️ Этот пользователь уже является модератором!", reply_markup=markup)
                return

            cursor.execute('SELECT COUNT(*) FROM groups')
            if cursor.fetchone()[0] == 0:
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("➕ Создать группу", callback_data="create_group"))
                markup.add(types.InlineKeyboardButton("🔙 Вернуться в админ-панель", callback_data="admin_panel"))
                try:
                    bot.delete_message(message.chat.id, message.message_id)
                    bot.delete_message(message.chat.id, initial_message_id)
                except Exception as e:
                    print(f"Ошибка удаления сообщения: {e}")
                bot.send_message(message.chat.id, "❌ Нет созданных групп! Сначала создайте группу.", reply_markup=markup)
                return

        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Вернуться в админ-панель", callback_data="admin_panel"))
        try:
            bot.delete_message(message.chat.id, message.message_id)
            bot.delete_message(message.chat.id, initial_message_id)
        except Exception as e:
            print(f"Ошибка удаления сообщения: {e}")
        msg = bot.send_message(
            message.chat.id,
            f"👤 ID модератора: {new_moder_id}\n📝 Введите название группы для назначения:",
            reply_markup=markup
        )
        bot.register_next_step_handler(msg, process_assign_group, new_moder_id, msg.message_id)  # Передаём message_id

    except ValueError:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Вернуться в админ-панель", callback_data="admin_panel"))
        try:
            bot.delete_message(message.chat.id, message.message_id)
            bot.delete_message(message.chat.id, initial_message_id)
        except Exception as e:
            print(f"Ошибка удаления сообщения: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка! Введите корректный ID пользователя (только цифры)", reply_markup=markup)

def process_assign_group(message, new_moder_id, group_message_id):
    group_name = message.text.strip()
    
    if not group_name:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Вернуться в админ-панель", callback_data="admin_panel"))
        try:
            bot.delete_message(message.chat.id, message.message_id)
        except Exception as e:
            print(f"Ошибка удаления сообщения (ввод названия группы): {e}")
        bot.send_message(message.chat.id, "❌ Название группы не может быть пустым!", reply_markup=markup)
        return

    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT ID FROM groups WHERE NAME = ?', (group_name,))
        group = cursor.fetchone()

        if not group:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("👥 Группы", callback_data="groups"))
            markup.add(types.InlineKeyboardButton("🔙 Вернуться в админ-панель", callback_data="admin_panel"))
            try:
                bot.delete_message(message.chat.id, message.message_id)
            except Exception as e:
                print(f"Ошибка удаления сообщения (ввод названия группы): {e}")
            bot.send_message(message.chat.id, f"❌ Группа '{group_name}' не найдена! Создайте её или выберите существующую.", 
                            reply_markup=markup)
            return

        group_id = group[0]

        try:
            # Удаляем сообщение с названием группы и предыдущее сообщение с запросом
            try:
                bot.delete_message(message.chat.id, message.message_id)
                bot.delete_message(message.chat.id, group_message_id)
            except Exception as e:
                print(f"Ошибка удаления сообщения (запрос названия группы): {e}")
                # Если удаление не удалось, редактируем сообщение
                bot.edit_message_text(
                    f"✅ Пользователь {new_moder_id} успешно назначен модератором в группу '{group_name}'!",
                    message.chat.id,
                    group_message_id,
                    reply_markup=None
                )
            
            # Назначаем модератора
            cursor.execute('INSERT INTO personal (ID, TYPE, GROUP_ID) VALUES (?, ?, ?)', 
                          (new_moder_id, 'moder', group_id))
            conn.commit()
            
            # Отправляем подтверждение модератору и планируем удаление
            moder_msg = bot.send_message(new_moder_id, f"🎉 Вам выданы права модератора в группе '{group_name}'! Напишите /start, чтобы начать работу.")
            threading.Timer(30.0, lambda: bot.delete_message(new_moder_id, moder_msg.message_id)).start()

            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Вернуться в админ-панель", callback_data="admin_panel"))
            bot.send_message(message.chat.id, f"✅ Пользователь {new_moder_id} успешно назначен модератором в группу '{group_name}'!", 
                            reply_markup=markup)

        except telebot.apihelper.ApiTelegramException:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Вернуться в админ-панель", callback_data="admin_panel"))
            bot.send_message(message.chat.id, f"❌ Ошибка: Пользователь {new_moder_id} не начал диалог с ботом!", 
                            reply_markup=markup)
        except Exception as e:
            print(f"Ошибка в process_assign_group: {e}")
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Вернуться в админ-панель", callback_data="admin_panel"))
            bot.send_message(message.chat.id, "❌ Произошла ошибка при назначении модератора!", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "remove_moder")
def remove_moder_request(call):
    if call.from_user.id in config.ADMINS_ID:
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except:
            pass
        msg = bot.send_message(call.message.chat.id, "👤 Введите ID пользователя для удаления из модераторов:")
        bot.register_next_step_handler(msg, process_remove_moder)

def process_remove_moder(message):
    try:
        moder_id = int(message.text)
        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM personal WHERE ID = ? AND TYPE = ?', (moder_id, 'moder'))
            conn.commit()
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Вернуться в админ-панель", callback_data="admin_panel"))
            markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
            try:
                bot.delete_message(message.chat.id, message.message_id)
            except:
                pass
            if cursor.rowcount > 0:
                try:
                    msg = bot.send_message(moder_id, "⚠️ У вас были отозваны права модератора.")
                    # Планируем удаление через 30 секунд
                    threading.Timer(30.0, lambda: bot.delete_message(moder_id, msg.message_id)).start()
                except:
                    pass
                bot.send_message(message.chat.id, f"✅ Пользователь {moder_id} успешно удален из модераторов!", reply_markup=markup)
            else:
                bot.send_message(message.chat.id, "⚠️ Этот пользователь не является модератором!", reply_markup=markup)
    except ValueError:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Вернуться в админ-панель", callback_data="admin_panel"))
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        try:
            bot.delete_message(message.chat.id, message.message_id)
        except:
            pass
        bot.send_message(message.chat.id, "❌ Ошибка! Введите корректный ID пользователя (только цифры)", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "delete_moderator")
def delete_moderator_request(call):
    if call.from_user.id in config.ADMINS_ID:
        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT ID FROM personal WHERE TYPE = 'moder'")
            moderators = cursor.fetchall()
        
        if not moderators:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
            markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
            try:
                bot.delete_message(call.message.chat.id, call.message.message_id)
            except:
                pass
            bot.send_message(call.message.chat.id, "❌ Нет модераторов для удаления", reply_markup=markup)
            return

        text = "👥 Выберите модератора для удаления:\n\n"
        markup = types.InlineKeyboardMarkup()
        for moder in moderators:
            text += f"ID: {moder[0]}\n"
            markup.add(types.InlineKeyboardButton(f"Удалить {moder[0]}", callback_data=f"confirm_delete_moder_{moder[0]}"))
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except:
            pass
        bot.send_message(call.message.chat.id, text, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("confirm_delete_moder_"))
def confirm_delete_moderator(call):
    if call.from_user.id in config.ADMINS_ID:
        moder_id = int(call.data.split("_")[3])
        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM personal WHERE ID = ? AND TYPE = 'moder'", (moder_id,))
            affected_rows = cursor.rowcount
            conn.commit()
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except:
            pass
        if affected_rows > 0:
            try:
                msg = bot.send_message(moder_id, "⚠️ Ваши права модератора были отозваны администратором.")
                # Планируем удаление через 30 секунд
                threading.Timer(30.0, lambda: bot.delete_message(moder_id, msg.message_id)).start()
            except:
                pass
            bot.send_message(call.message.chat.id, f"✅ Модератор с ID {moder_id} успешно удален", reply_markup=markup)
        else:
            bot.send_message(call.message.chat.id, f"❌ Модератор с ID {moder_id} не найден", reply_markup=markup)

#=======================================================================================
#=======================================================================================
#===================================ГРУППЫ==============================================
#=======================================================================================
#=======================================================================================
#=======================================================================================




@bot.callback_query_handler(func=lambda call: call.data == "groups")
def groups_menu(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для управления группами!")
        return
    
    text = "<b>👥 Управление группами</b>"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("➕ Создать группу", callback_data="create_group"))
    markup.add(types.InlineKeyboardButton("➖ Удалить группу", callback_data="delete_group"))
    markup.add(types.InlineKeyboardButton("📊 Статистика", callback_data="group_statistics"))
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
    
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except:
        pass
    bot.send_message(call.message.chat.id, text, parse_mode='HTML', reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "create_group")
def create_group_request(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для создания группы!")
        return
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="groups"))
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except:
        pass
    msg = bot.send_message(call.message.chat.id, "📝 Введите название новой группы:", reply_markup=markup)
    bot.register_next_step_handler(msg, process_create_group, msg.message_id)

def process_create_group(message, initial_message_id):
    if message.from_user.id not in config.ADMINS_ID:
        return
    
    group_name = message.text.strip()
    if not group_name:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="groups"))
        try:
            bot.delete_message(message.chat.id, message.message_id)
        except Exception as e:
            print(f"Ошибка удаления сообщения: {e}")
        bot.send_message(message.chat.id, "❌ Название группы не может быть пустым!", reply_markup=markup)
        return
    
    try:
        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('INSERT INTO groups (NAME) VALUES (?)', (group_name,))
            conn.commit()
        
        try:
            bot.delete_message(message.chat.id, message.message_id)
            bot.delete_message(message.chat.id, initial_message_id)
        except Exception as e:
            print(f"Ошибка удаления сообщения: {e}")
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("👥 Группы", callback_data="groups"))
        markup.add(types.InlineKeyboardButton("🔙 Админ-панель", callback_data="admin_panel"))
        bot.send_message(message.chat.id, f"✅ Группа '{group_name}' успешно создана!", reply_markup=markup)

    except sqlite3.IntegrityError:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="groups"))
        try:
            bot.delete_message(message.chat.id, message.message_id)
        except Exception as e:
            print(f"Ошибка удаления сообщения: {e}")
        bot.send_message(message.chat.id, f"❌ Группа с названием '{group_name}' уже существует!", reply_markup=markup)

def process_create_group(message, initial_message_id):
    if message.from_user.id not in config.ADMINS_ID:
        return
    
    group_name = message.text.strip()
    if not group_name:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="groups"))
        try:
            bot.delete_message(message.chat.id, message.message_id)
        except:
            pass
        bot.send_message(message.chat.id, "❌ Название группы не может быть пустым!", reply_markup=markup)
        return
    
    try:
        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('INSERT INTO groups (NAME) VALUES (?)', (group_name,))
            conn.commit()
        
        # Удаляем введённое сообщение и начальное сообщение
        try:
            bot.delete_message(message.chat.id, message.message_id)
            bot.delete_message(message.chat.id, initial_message_id)
        except:
            pass
        
        # Показываем сообщение об успехе с кнопками
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("👥 Группы", callback_data="groups"))
        markup.add(types.InlineKeyboardButton("🔙 Админ-панель", callback_data="admin_panel"))
        success_msg = bot.send_message(message.chat.id, f"✅ Группа '{group_name}' успешно создана!", reply_markup=markup)
        
        # Планируем удаление сообщения об успехе и переход в админ-панель через 2 секунды
        def show_admin_panel():
            try:
                bot.delete_message(message.chat.id, success_msg.message_id)
                admin_panel(types.CallbackQuery(id=success_msg.message_id, from_user=message.from_user, 
                                               chat_instance=message.chat.id, message=success_msg, data="admin_panel"))
            except:
                pass
        
        threading.Timer(2.0, show_admin_panel).start()
        
    except sqlite3.IntegrityError:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="groups"))
        try:
            bot.delete_message(message.chat.id, message.message_id)
        except:
            pass
        bot.send_message(message.chat.id, f"❌ Группа с названием '{group_name}' уже существует!", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "delete_group")
def delete_group_request(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для удаления группы!")
        return
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="groups"))
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except:
        pass
    msg = bot.send_message(call.message.chat.id, "📝 Введите название группы для удаления:", reply_markup=markup)
    bot.register_next_step_handler(msg, process_delete_group)

def process_delete_group(message):
    if message.from_user.id not in config.ADMINS_ID:
        return
    
    group_name = message.text.strip()
    if not group_name:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="groups"))
        try:
            bot.delete_message(message.chat.id, message.message_id)
        except:
            pass
        bot.send_message(message.chat.id, "❌ Название группы не может быть пустым!", reply_markup=markup)
        return
    
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT ID FROM groups WHERE NAME = ?', (group_name,))
        group = cursor.fetchone()
        
        if group:
            group_id = group[0]
            cursor.execute('UPDATE personal SET GROUP_ID = NULL WHERE GROUP_ID = ?', (group_id,))
            cursor.execute('DELETE FROM groups WHERE ID = ?', (group_id,))
            conn.commit()
            
            # Удаляем введенное сообщение
            try:
                bot.delete_message(message.chat.id, message.message_id)
            except:
                pass
            
            # Показываем сообщение об успехе временно
            success_msg = bot.send_message(message.chat.id, f"✅ Группа '{group_name}' успешно удалена!")
            
            # Планируем удаление сообщения об успехе и показ админ-панели через 2 секунды
            def show_admin_panel():
                try:
                    bot.delete_message(message.chat.id, success_msg.message_id)
                    admin_panel(types.CallbackQuery(id=success_msg.message_id, from_user=message.from_user, 
                                                   chat_instance=message.chat.id, message=success_msg, data="admin_panel"))
                except:
                    pass
            
            threading.Timer(2.0, show_admin_panel).start()
        else:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="groups"))
            try:
                bot.delete_message(message.chat.id, message.message_id)
            except:
                pass
            bot.send_message(message.chat.id, f"❌ Группа с названием '{group_name}' не найдена!", reply_markup=markup)



@bot.callback_query_handler(func=lambda call: call.data.startswith("view_group_stats_"))
def view_group_stats(call):
    user_id = call.from_user.id
    if user_id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для просмотра статистики!")
        return

    try:
        _, _, group_id, page = call.data.split("_")
        group_id = int(group_id)
        page = int(page)
        if page < 1:
            page = 1
    except (IndexError, ValueError):
        bot.answer_callback_query(call.id, "❌ Неверный формат данных!")
        return

    with db.get_db() as conn:
        cursor = conn.cursor()
        # Подсчитываем участников (модераторов) группы
        cursor.execute('SELECT COUNT(*) FROM personal WHERE GROUP_ID = ? AND TYPE = "moder"', (group_id,))
        member_count = cursor.fetchone()[0]

        # Получаем номера с статусом "отстоял" для конкретной группы
        cursor.execute('''
            SELECT n.NUMBER, n.TAKE_DATE, n.SHUTDOWN_DATE
            FROM numbers n
            LEFT JOIN personal p ON p.ID = n.ID_OWNER
            WHERE n.STATUS = 'отстоял'
            AND (p.GROUP_ID = ? OR p.GROUP_ID IS NULL)
            ORDER BY n.SHUTDOWN_DATE DESC
        ''', (group_id,))
        numbers = cursor.fetchall()

    # Пагинация
    items_per_page = 20
    total_pages = max(1, (len(numbers) + items_per_page - 1) // items_per_page)
    page = max(1, min(page, total_pages))
    start_idx = (page - 1) * items_per_page
    end_idx = start_idx + items_per_page
    page_numbers = numbers[start_idx:end_idx]

    # Формируем текст статистики
    text = (
        f"<b>📊 Статистика группы {group_id}:</b>\n\n"
        f"📱 Успешных номеров: {len(numbers)}\n"
        f"────────────────────\n"
        f"<b>📱 Список номеров (страница {page}/{total_pages}):</b>\n\n"
    )

    if not page_numbers:
        text += "📭 Нет успешных номеров в этой группе."
    else:
        for number, take_date, shutdown_date in page_numbers:
            text += f"Номер: {number}\n"
            text += f"🟢 Встал: {take_date}\n"
            text += f"🟢 Отстоял: {shutdown_date}\n"
            text += "───────────────────\n"

    # Проверяем лимит символов
    TELEGRAM_MESSAGE_LIMIT = 4096
    if len(text) > TELEGRAM_MESSAGE_LIMIT:
        text = text[:TELEGRAM_MESSAGE_LIMIT - 100] + "\n... (Сообщение обрезано, используйте пагинацию)"

    # Формируем разметку
    markup = types.InlineKeyboardMarkup()

    # Кнопки пагинации
    if total_pages > 1:
        row = []
        if page > 1:
            row.append(types.InlineKeyboardButton("⬅️ Назад", callback_data=f"view_group_stats_{group_id}_{page-1}"))
        row.append(types.InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"))
        if page < total_pages:
            row.append(types.InlineKeyboardButton("Вперёд ➡️", callback_data=f"view_group_stats_{group_id}_{page+1}"))
        if row:
            markup.row(*row)

    markup.add(types.InlineKeyboardButton("👥 Все группы", callback_data="admin_view_groups"))
    markup.add(types.InlineKeyboardButton("🔙 Админ-панель", callback_data="admin_panel"))
    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))

    bot.edit_message_text(
        text,
        call.message.chat.id,
        call.message.message_id,
        parse_mode='HTML',
        reply_markup=markup
    )









#=======================================================================================
#=======================================================================================
#===================================АДМИНКА=====================================
#=======================================================================================
#=======================================================================================
#=======================================================================================


@bot.callback_query_handler(func=lambda call: call.data == "admin_panel")
def admin_panel(call):
    with treasury_lock:
        if call.from_user.id in active_treasury_admins:
            del active_treasury_admins[call.from_user.id]
    
    if call.from_user.id in config.ADMINS_ID:           
        with db.get_db() as conn:
                cursor = conn.cursor()
                # Подсчёт слетевших номеров
                cursor.execute('SELECT COUNT(*) FROM numbers WHERE STATUS = "слетел"')
                numbers_count = cursor.fetchone()[0]
                
                # Подсчёт всех обработанных номеров
                cursor.execute('SELECT COUNT(*) FROM numbers WHERE STATUS IN ("активен", "слетел", "отстоял")')
                total_numbers = cursor.fetchone()[0]

                admin_text = (
                    "<b>⚙️ Панель администратора</b>\n\n"
                    f"📱 Слетевших номеров: <code>{numbers_count}</code>\n"
                    f"📊 Всего обработанных номеров: <code>{total_numbers}</code>"
                )

        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("Главное меню", callback_data="Gv"))
        markup.add(types.InlineKeyboardButton("👥 Модераторы", callback_data="moderators"))
        markup.add(types.InlineKeyboardButton("➖ Удалить модератора", callback_data="delete_moderator"))
        markup.add(types.InlineKeyboardButton("👤 Все пользователи", callback_data="all_users_1"))
        markup.add(types.InlineKeyboardButton("📝 Заявки на вступление", callback_data="pending_requests"))
        markup.add(types.InlineKeyboardButton("👥 Группы", callback_data="groups"))
        markup.add(types.InlineKeyboardButton("📱 Все номера", callback_data="all_numbers"))
        markup.add(types.InlineKeyboardButton("🔍 Найти номер", callback_data="search_number"))
        markup.add(types.InlineKeyboardButton("📢 Рассылка", callback_data="broadcast"))
        markup.add(types.InlineKeyboardButton("💰 Казна", callback_data="treasury"))
        markup.add(types.InlineKeyboardButton("⚙️ Настройки", callback_data="settings"))
        markup.add(types.InlineKeyboardButton("🗃 БД", callback_data="db_menu"))
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back_to_main"))

        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except:
            pass
        bot.send_message(call.message.chat.id, admin_text, parse_mode='HTML', reply_markup=markup)

def check_time():
    while True:
        current_time = datetime.now().strftime("%H:%M")
        if current_time == config.CLEAR_TIME:
            clear_database()
            time.sleep(61)
        time.sleep(30)




#ЧИСТКА ЛИБО В РУЧНУЮ ЛИБО АВТОМАТИЧЕСКИ БАЗЫ ДАННЫХ ( НОМЕРА )

def clear_database(chat_id=None):
    """Очищает все номера из таблицы numbers и уведомляет пользователей."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            
            # Получаем пользователей, у которых есть номера, исключая админов и модераторов
            cursor.execute('''
                SELECT DISTINCT ID_OWNER 
                FROM numbers 
                WHERE ID_OWNER NOT IN (SELECT ID FROM personal WHERE TYPE IN ('ADMIN', 'moder'))
            ''')
            users = [row[0] for row in cursor.fetchall()]
            
            # Удаляем все номера
            cursor.execute('DELETE FROM numbers')
            deleted_numbers = cursor.rowcount
            conn.commit()
            logging.info(f"Удалено {deleted_numbers} номеров (все статусы) в {config.CLEAR_TIME}.")
            
            # Уведомляем пользователей
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("📱 Сдать номер", callback_data="submit_number"))
            for user_id in users:
                try:
                    bot.send_message(
                        user_id,
                        "🔄 Все номера очищены.\n📱 Пожалуйста, поставьте свои номера снова.",
                        reply_markup=markup
                    )
                    logging.info(f"Уведомление отправлено пользователю {user_id}")
                    time.sleep(0.05)
                except Exception as e:
                    logging.error(f"Не удалось отправить уведомление пользователю {user_id}: {e}")
            
            # Уведомляем админов
            for admin_id in config.ADMINS_ID:
                try:
                    bot.send_message(
                        admin_id,
                        f"🔄 Все номера очищены, удалено {deleted_numbers} номеров. Пользователи предупреждены."
                    )
                    logging.info(f"Уведомление отправлено админу {admin_id}")
                    time.sleep(0.05)
                except Exception as e:
                    logging.error(f"Не удалось отправить уведомление админу {admin_id}: {e}")
            
            # Если очистка вызвана админом, отправляем подтверждение
            if chat_id:
                bot.send_message(chat_id, f"✅ Таблица номеров очищена. Удалено {deleted_numbers} номеров.")

    except Exception as e:
        logging.error(f"Ошибка при очистке таблицы numbers: {e}")
        if chat_id:
            bot.send_message(chat_id, "❌ Ошибка при очистке таблицы номеров.")


def download_numbers(chat_id):
    """Создаёт и отправляет текстовый файл с данными из таблицы numbers."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM numbers')
            rows = cursor.fetchall()
            
            if not rows:
                bot.send_message(chat_id, "📭 Таблица номеров пуста.")
                return
            
            # Создаём текстовый файл в памяти
            output = io.StringIO()
            # Заголовки столбцов
            columns = [desc[0] for desc in cursor.description]
            output.write(','.join(columns) + '\n')
            # Данные
            for row in rows:
                output.write(','.join(str(val) if val is not None else '' for val in row) + '\n')
            
            # Подготовка файла для отправки
            output.seek(0)
            file_content = output.getvalue().encode('utf-8')
            file = io.BytesIO(file_content)
            file.name = 'numbers.txt'
            
            # Отправка файла
            bot.send_document(chat_id, file, caption="📄 Данные из таблицы номеров")
            logging.info(f"Файл numbers.txt отправлен админу {chat_id}")
    
    except Exception as e:
        logging.error(f"Ошибка при скачивании таблицы numbers: {e}")
        bot.send_message(chat_id, "❌ Ошибка при скачивании таблицы номеров.")

def schedule_clear_database():
    """Настраивает планировщик для очистки таблицы numbers в указанное время."""
    schedule.every().day.at(config.CLEAR_TIME).do(clear_database)
    logging.info(f"Планировщик настроен для очистки номеров в {config.CLEAR_TIME}")

    def run_scheduler():
        while True:
            schedule.run_pending()
            time.sleep(60)

    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()
    logging.info("Планировщик очистки запущен.")

#ПОИСК НОМЕРА ИНФОРМАЦИЯ О НЁМ

def run_bot():
    time_checker = threading.Thread(target=check_time)
    time_checker.daemon = True
    time_checker.start()
    bot.polling(none_stop=True, skip_pending=True)
class AdminStates(StatesGroup):
    waiting_for_number = State()

@bot.callback_query_handler(func=lambda call: call.data == "search_number")
def search_number_callback(call):
    user_id = call.from_user.id
    
    # Проверяем, что пользователь — администратор
    if user_id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для выполнения этого действия!")
        return
    
    # Отправляем сообщение с просьбой ввести номер
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Назад в профиль", callback_data="profile"))
    msg = bot.edit_message_text(
        "📱 Пожалуйста, введите номер телефона в формате +79991234567 (используйте reply на это сообщение):",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=markup,
        parse_mode='HTML'
    )
    
    # Регистрируем следующий шаг для обработки введённого номера
    bot.register_next_step_handler(msg, process_search_number, call.message.chat.id, msg.message_id)


def process_search_number(message, original_chat_id, original_message_id):
    user_id = message.from_user.id
    
    # Проверяем, что пользователь — администратор
    if user_id not in config.ADMINS_ID:
        bot.send_message(message.chat.id, "❌ У вас нет прав для выполнения этого действия!")
        return
    
    # Проверяем, что сообщение является ответом (reply)
    if not message.reply_to_message:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔍 Попробовать снова", callback_data="search_number"))
        markup.add(types.InlineKeyboardButton("🔙 Назад в профиль", callback_data="profile"))
        bot.send_message(
            message.chat.id,
            "❌ Пожалуйста, используйте reply на сообщение для ввода номера!",
            reply_markup=markup,
            parse_mode='HTML'
        )
        return
    
    # Нормализуем введённый номер
    number_input = message.text.strip()
    normalized_number = is_russian_number(number_input)
    if not normalized_number:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔍 Попробовать снова", callback_data="search_number"))
        markup.add(types.InlineKeyboardButton("🔙 Назад в профиль", callback_data="profile"))
        bot.send_message(
            message.chat.id,
            "❌ Неверный формат номера! Используйте российский номер, например: +79991234567",
            reply_markup=markup,
            parse_mode='HTML'
        )
        return
    
    # Удаляем сообщение с введённым номером
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception as e:
        print(f"[ERROR] Не удалось удалить сообщение с номером {normalized_number}: {e}")
    
    # Ищем информацию о номере в базе данных
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT NUMBER, ID_OWNER, STATUS, TAKE_DATE, SHUTDOWN_DATE, CONFIRMED_BY_MODERATOR_ID, TG_NUMBER, SUBMIT_DATE, GROUP_CHAT_ID
            FROM numbers
            WHERE NUMBER = ?
        ''', (normalized_number,))
        number_data = cursor.fetchone()
    
    # Формируем сообщение с информацией о номере
    if number_data:
        number, owner_id, status, take_date, shutdown_date, confirmed_by_moderator_id, tg_number, submit_date, group_chat_id = number_data
        
        # Получаем имя группы
        group_name = db.get_group_name(group_chat_id) if group_chat_id else "Не указана"
        
        # Формируем отображаемые даты
        take_date_str = take_date if take_date not in ("0", "1") else "Неизвестно"
        shutdown_date_str = shutdown_date if shutdown_date != "0" else "Не завершён"
        
        # Формируем информацию о модераторе
        moderator_info = f"Модератор: @{confirmed_by_moderator_id}" if confirmed_by_moderator_id else "Модератор: Не назначен"
        
        # Формируем текст для номера
        text = (
            f"📱 <b>Информация о номере:</b>\n\n"
            f"📱 Номер: <code>{number}</code>\n"
            f"👤 Владелец: <code>{owner_id}</code>\n"
            f"📊 Статус: {status}\n"
            f"🟢 Взято: {take_date_str}\n"
            f"🔴 Отстоял: {shutdown_date_str}\n"
            f"{moderator_info}\n"
            f"🏷 Группа: {group_name}\n"
            f"📱 ТГ: {tg_number or 'Не указан'}\n"
        )
    else:
        text = f"❌ Номер <code>{normalized_number}</code> не найден в базе данных."
    
    # Обновляем исходное сообщение
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔍 Поиск другого номера", callback_data="search_number"))
    markup.add(types.InlineKeyboardButton("🔙 Назад в профиль", callback_data="profile"))
    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
    
    try:
        bot.edit_message_text(
            text,
            original_chat_id,
            original_message_id,
            parse_mode='HTML',
            reply_markup=markup
        )
    except Exception as e:
        print(f"[ERROR] Не удалось обновить сообщение: {e}")
        bot.send_message(
            original_chat_id,
            text,
            parse_mode='HTML',
            reply_markup=markup
        )
        
#============================

@bot.callback_query_handler(func=lambda call: call.data == "change_hold_time")
def change_hold_time_request(call):
    if call.from_user.id in config.ADMINS_ID:
        msg = bot.edit_message_text("⏱ Введите новое время холда (в минутах, например: 5):",
                                  call.message.chat.id,
                                  call.message.message_id)
        bot.register_next_step_handler(msg, process_change_hold_time)

def process_change_hold_time(message):
    if message.from_user.id in config.ADMINS_ID:
        try:
            new_time = int(message.text)
            if new_time <= 0:
                raise ValueError
            with db.get_db() as conn:
                cursor = conn.cursor()
                cursor.execute('UPDATE settings SET HOLD_TIME = ?', (new_time,))
                conn.commit()
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="settings"))
            markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
            bot.send_message(message.chat.id, f"✅ Время холда изменено на {new_time} минут", reply_markup=markup)
        except ValueError:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="settings"))
            markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
            bot.send_message(message.chat.id, "❌ Введите корректное положительное целое число!", reply_markup=markup)






#  КОД ДЛЯ ПРИНЯТИЕ ОТКАЗА ЗАЯВОК В БОТА

@bot.callback_query_handler(func=lambda call: call.data.startswith("pending_requests"))
def show_pending_requests(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для просмотра заявок!")
        return
    
    page = 1
    if "_" in call.data:
        try:
            page = int(call.data.split("_")[1])
            if page < 1:
                page = 1
        except (IndexError, ValueError):
            page = 1

    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT ID, LAST_REQUEST FROM requests WHERE STATUS = "pending"')
        requests = cursor.fetchall()
    
    if not requests:
        text = "📭 Нет заявок на вступление."
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode='HTML', reply_markup=markup)
        return
    
    # Пагинация
    items_per_page = 20
    total_pages = (len(requests) + items_per_page - 1) // items_per_page
    page = max(1, min(page, total_pages))
    start_idx = (page - 1) * items_per_page
    end_idx = start_idx + items_per_page
    page_requests = requests[start_idx:end_idx]
    
    text = f"<b>📝 Заявки на вступление (страница {page}/{total_pages}):</b>\n\n"
    markup = types.InlineKeyboardMarkup()
    
    for user_id, last_request in page_requests:
        try:
            user = bot.get_chat_member(user_id, user_id).user
            username = f"@{user.username}" if user.username else "Нет username"
        except:
            username = "Неизвестный пользователь"
        
        text += (
            f"🆔 ID: <code>{user_id}</code>\n"
            f"👤 Username: {username}\n"
            f"📅 Дата заявки: {last_request}\n"
            f"────────────────────\n"
        )
        
        markup.row(
            types.InlineKeyboardButton(f"✅ Одобрить {user_id}", callback_data=f"approve_user_{user_id}"),
            types.InlineKeyboardButton(f"❌ Отклонить {user_id}", callback_data=f"reject_user_{user_id}")
        )
    
    # Проверяем лимит символов
    TELEGRAM_MESSAGE_LIMIT = 4096
    if len(text) > TELEGRAM_MESSAGE_LIMIT:
        text = text[:TELEGRAM_MESSAGE_LIMIT - 100] + "\n... (Сообщение обрезано, используйте пагинацию)"
    
    # Кнопки пагинации
    if total_pages > 1:
        row = []
        if page > 1:
            row.append(types.InlineKeyboardButton("⬅️ Назад", callback_data=f"pending_requests_{page-1}"))
        row.append(types.InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"))
        if page < total_pages:
            row.append(types.InlineKeyboardButton("Вперёд ➡️", callback_data=f"pending_requests_{page+1}"))
        if row:
            markup.row(*row)
    
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
    
    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode='HTML', reply_markup=markup)
    except:
        bot.send_message(call.message.chat.id, text, parse_mode='HTML', reply_markup=markup)


#ВСЕ ПОЛЬЗОВАТЕЛИ :

@bot.callback_query_handler(func=lambda call: call.data.startswith("all_users_"))
def show_all_users(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для просмотра пользователей!")
        return
    
    try:
        page = int(call.data.split("_")[2])
    except (IndexError, ValueError):
        page = 1  # Если что-то пошло не так, открываем первую страницу
    
    # Получаем всех пользователей из таблицы requests
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT ID FROM requests')
        all_users = cursor.fetchall()
    
    if not all_users:
        text = "📭 Нет пользователей в боте."
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Вернуться в админ-панель", callback_data="admin_panel"))
    else:
        # Пагинация
        users_per_page = 8
        total_pages = (len(all_users) + users_per_page - 1) // users_per_page
        page = max(1, min(page, total_pages))  # Ограничиваем страницу допустимым диапазоном
        
        start_idx = (page - 1) * users_per_page
        end_idx = start_idx + users_per_page
        page_users = all_users[start_idx:end_idx]
        
        # Формируем текст
        text = f"<b>Управляйте людьми:</b>\n({page} страница)\n\n"
        markup = types.InlineKeyboardMarkup()
        
        # Добавляем кнопки для каждого пользователя
        for user_data in page_users:
            user_id = user_data[0]
            try:
                user = bot.get_chat_member(user_id, user_id).user
                username = f"@{user.username}" if user.username else "Нет username"
            except:
                username = "Неизвестный пользователь"
            
            markup.add(types.InlineKeyboardButton(f"{user_id} {username}", callback_data=f"user_details_{user_id}"))
        
        # Кнопки пагинации
        if total_pages > 1:
            row = []
            if page > 1:
                row.append(types.InlineKeyboardButton("⬅️ Назад", callback_data=f"all_users_{page-1}"))
            row.append(types.InlineKeyboardButton(f"{page}", callback_data=f"all_users_{page}"))
            if page < total_pages:
                row.append(types.InlineKeyboardButton("Вперёд ➡️", callback_data=f"all_users_{page+1}"))
            markup.row(*row)
        
        # Кнопка "Найти по username или userid"
        markup.add(types.InlineKeyboardButton("🔍 Найти по username или userid", callback_data="find_user"))
        
        # Кнопка "Вернуться в админ-панель"
        markup.add(types.InlineKeyboardButton("🔙 Вернуться в админ-панель", callback_data="admin_panel"))
    
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except:
        pass
    bot.send_message(call.message.chat.id, text, parse_mode='HTML', reply_markup=markup)

#поиск пользователя по юзерид или юзернейм
@bot.callback_query_handler(func=lambda call: call.data == "find_user")
def find_user(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для поиска пользователей!")
        return
    
    # Запрашиваем у админа username или userid
    text = "🔍 Введите @username или userid пользователя для поиска:"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Отмена", callback_data="all_users_1"))
    
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except:
        pass
    msg = bot.send_message(call.message.chat.id, text, parse_mode='HTML', reply_markup=markup)
    
    # Регистрируем следующий шаг для обработки введённых данных
    bot.register_next_step_handler(msg, process_user_search, call.message.chat.id)

def process_user_search(message, original_chat_id):
    if message.chat.id != original_chat_id or message.from_user.id not in config.ADMINS_ID:
        bot.send_message(message.chat.id, "❌ Ошибка: действие доступно только администратору!")
        return
    
    search_query = message.text.strip()
    
    # Удаляем сообщение с введёнными данными
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except:
        pass
    
    # Проверяем, что ввёл пользователь
    user_id = None
    username = None
    
    if search_query.startswith('@'):
        username = search_query[1:]  # Убираем @ из username
    else:
        try:
            user_id = int(search_query)  # Пробуем преобразовать в число (userid)
        except ValueError:
            bot.send_message(message.chat.id, "❌ Неверный формат! Введите @username или userid (число).")
            return
    
    # Ищем пользователя в базе
    with db.get_db() as conn:
        cursor = conn.cursor()
        if user_id:
            cursor.execute('SELECT ID FROM requests WHERE ID = ?', (user_id,))
        else:
            cursor.execute('SELECT ID FROM requests')
        
        users = cursor.fetchall()
    
    found_user_id = None
    if user_id:
        if users:
            found_user_id = users[0][0]  # Нашли по user_id
    else:
        # Ищем по username
        for uid in users:
            try:
                user = bot.get_chat_member(uid[0], uid[0]).user
                if user.username and user.username.lower() == username.lower():
                    found_user_id = uid[0]
                    break
            except:
                continue
    
    # Формируем ответ
    if found_user_id:
        try:
            user = bot.get_chat_member(found_user_id, found_user_id).user
            username_display = f"@{user.username}" if user.username else "Нет username"
        except:
            username_display = "Неизвестный пользователь"
        
        text = (
            f"<b>Найденный пользователь:</b>\n\n"
            f"🆔 ID: <code>{found_user_id}</code>\n"
            f"👤 Username: {username_display}\n"
        )
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton(f"{found_user_id} {username_display}", callback_data=f"user_details_{found_user_id}"))
        markup.add(types.InlineKeyboardButton("🔙 Вернуться к списку пользователей", callback_data="all_users_1"))
        markup.add(types.InlineKeyboardButton("🔙 Вернуться в админ-панель", callback_data="admin_panel"))
    else:
        text = "❌ Пользователь не найден!"
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Вернуться к списку пользователей", callback_data="all_users_1"))
        markup.add(types.InlineKeyboardButton("🔙 Вернуться в админ-панель", callback_data="admin_panel"))
    
    # Отправляем новое сообщение (заменяем старое)
    bot.send_message(message.chat.id, text, parse_mode='HTML', reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith("user_details_"))
def user_details(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для управления пользователями!")
        return
    
    user_id = int(call.data.split("_")[2])
    
    # Получаем информацию о пользователе
    with db.get_db() as conn:
        cursor = conn.cursor()
        
        # Проверяем, есть ли пользователь в базе
        cursor.execute('SELECT BLOCKED, CAN_SUBMIT_NUMBERS FROM requests WHERE ID = ?', (user_id,))
        user_data = cursor.fetchone()
        if not user_data:
            text = f"❌ Пользователь с ID {user_id} не найден!"
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Вернуться к списку пользователей", callback_data="all_users_1"))
            markup.add(types.InlineKeyboardButton("🔙 Вернуться в админ-панель", callback_data="admin_panel"))
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode='HTML', reply_markup=markup)
            return
        
        is_blocked = user_data[0]
        can_submit_numbers = user_data[1]
        
        # Статистика по номерам
        cursor.execute('SELECT STATUS, TAKE_DATE, SHUTDOWN_DATE FROM numbers WHERE ID_OWNER = ?', (user_id,))
        numbers = cursor.fetchall()
        
        total_numbers = len(numbers)
        active_numbers = sum(1 for num in numbers if num[0] == 'активен')
        invalid_numbers = sum(1 for num in numbers if num[0] == 'не валид')
        not_sustained = sum(1 for num in numbers if num[2] != "0" and num[0] != 'активен')  # Слетевшие номера
        
    # Получаем username через Telegram API
    try:
        user = bot.get_chat_member(user_id, user_id).user
        username = f"@{user.username}" if user.username else "Нет username"
    except:
        username = "Неизвестный пользователь"
    
    # Формируем текст
    text = (
        f"<b>Пользователь {user_id} {username}</b>\n\n"
        f"📱 Принял номеров: {total_numbers}\n"
        f"✅ Которые на данный момент работают: {active_numbers}\n"
        f"❌ Не валидные: {invalid_numbers}\n"
        f"⏳ Сколько не отстояло: {not_sustained}\n"
    )
    
    # Формируем кнопки
    markup = types.InlineKeyboardMarkup()
    
    # Кнопка блокировки/разблокировки
    if is_blocked:
        markup.add(types.InlineKeyboardButton("✅ Разблокировать в боте", callback_data=f"unblock_user_{user_id}"))
    else:
        markup.add(types.InlineKeyboardButton("❌ Заблокировать в боте", callback_data=f"block_user_{user_id}"))
    
    # Кнопка "Выгнать из бота"
    markup.add(types.InlineKeyboardButton("🚪 Выгнать из бота", callback_data=f"kick_user_{user_id}"))
    
    # Кнопка запрета/разрешения сдачи номеров
    if can_submit_numbers:
        markup.add(types.InlineKeyboardButton("🚫 Запретить сдавание номеров", callback_data=f"disable_numbers_{user_id}"))
    else:
        markup.add(types.InlineKeyboardButton("✅ Разрешить сдавание номеров", callback_data=f"enable_numbers_{user_id}"))
    
    # Кнопки навигации
    markup.add(types.InlineKeyboardButton("🔙 Вернуться к списку пользователей", callback_data="all_users_1"))
    markup.add(types.InlineKeyboardButton("🔙 Вернуться в админ-панель", callback_data="admin_panel"))
    
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except:
        pass
    bot.send_message(call.message.chat.id, text, parse_mode='HTML', reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith("block_user_"))
def block_user(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для выполнения этого действия!")
        return
    
    user_id = int(call.data.split("_")[2])  # Убедимся, что user_id определён
    
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('UPDATE requests SET BLOCKED = 1 WHERE ID = ?', (user_id,))
        conn.commit()
    
    try:
        bot.send_message(user_id, "🚫 Вас заблокировали в боте!")
    except:
        pass
    
    bot.answer_callback_query(call.id, f"Пользователь {user_id} заблокирован!")
    user_details(call)

@bot.callback_query_handler(func=lambda call: call.data.startswith("unblock_user_"))
def unblock_user(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для выполнения этого действия!")
        return
    user_id = int(call.data.split("_")[2])
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('UPDATE requests SET BLOCKED = 0 WHERE ID = ?', (user_id,))
        conn.commit()
    try:
        bot.send_message(user_id, "✅ Вас разблокировали в боте! Напишите /start")
    except:
        pass
    bot.answer_callback_query(call.id, f"Пользователь {user_id} разблокирован!")
    user_details(call)


@bot.callback_query_handler(func=lambda call: call.data.startswith("kick_user_"))
def kick_user(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для выполнения этого действия!")
        return
    user_id = int(call.data.split("_")[2])
    current_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('UPDATE requests SET STATUS = "pending", LAST_REQUEST = ? WHERE ID = ?', (current_date, user_id))
        conn.commit()
    try:
        bot.send_message(user_id, "🚪 Вас выгнали из бота! Вам нужно снова подать заявку на вступление. Напишите /start")
    except:
        pass
    bot.answer_callback_query(call.id, f"Пользователь {user_id} выгнан из бота!")
    call.data = "all_users_1"  # Возвращаемся на первую страницу
    show_all_users(call)


@bot.callback_query_handler(func=lambda call: call.data.startswith("disable_numbers_"))
def disable_numbers(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для выполнения этого действия!")
        return
    user_id = int(call.data.split("_")[2])
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('UPDATE requests SET CAN_SUBMIT_NUMBERS = 0 WHERE ID = ?', (user_id,))
        conn.commit()
    try:
        bot.send_message(user_id, "🚫 Вам запретили сдавать номера!")
    except:
        pass  
    bot.answer_callback_query(call.id, f"Пользователю {user_id} запрещено сдавать номера!")
    # Обновляем информацию о пользователе
    user_details(call)

@bot.callback_query_handler(func=lambda call: call.data.startswith("enable_numbers_"))
def enable_numbers(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для выполнения этого действия!")
        return
    user_id = int(call.data.split("_")[2])
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('UPDATE requests SET CAN_SUBMIT_NUMBERS = 1 WHERE ID = ?', (user_id,))
        conn.commit()
    try:
        bot.send_message(user_id, "✅ Вам разрешили сдавать номера!")
    except:
        pass
    bot.answer_callback_query(call.id, f"Пользователю {user_id} разрешено сдавать номера!")
    # Обновляем информацию о пользователе
    user_details(call)


#СТАТИСТИКА ГРУПП
@bot.callback_query_handler(func=lambda call: call.data.startswith("group_statistics"))
def group_statistics(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для просмотра статистики!")
        return

    page = 1
    if "_" in call.data:
        try:
            page = int(call.data.split("_")[1])
            if page < 1:
                page = 1
        except (IndexError, ValueError):
            page = 1

    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT ID, NAME FROM groups ORDER BY NAME')
        groups = cursor.fetchall()

    if not groups:
        text = "📭 Нет доступных групп."
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode='HTML', reply_markup=markup)
        return

    items_per_page = 10
    total_pages = (len(groups) + items_per_page - 1) // items_per_page
    page = max(1, min(page, total_pages))
    start_idx = (page - 1) * items_per_page
    end_idx = start_idx + items_per_page
    page_groups = groups[start_idx:end_idx]

    text = f"<b>📊 Список групп (страница {page}/{total_pages}):</b>\n\n"
    for group_id, group_name in page_groups:
        # Подсчёт модераторов в группе
        cursor.execute('SELECT COUNT(*) FROM personal WHERE GROUP_ID = ? AND TYPE = ?', (group_id, 'moderator'))
        moderator_count = cursor.fetchone()[0]
        
        # Подсчёт успешных номеров для группы
        cursor.execute('''
            SELECT COUNT(*) 
            FROM numbers n
            JOIN personal p ON n.CONFIRMED_BY_MODERATOR_ID = p.ID
            WHERE p.GROUP_ID = ? AND n.STATUS = 'отстоял'
        ''', (group_id,))
        successful_numbers = cursor.fetchone()[0]
        
        text += f"🏠 <b>{group_name}</b>\n"
        text += f"🆔 ID: {group_id}\n"
        text += f"🛡 Модераторов: {moderator_count}\n"
        text += f"📱 Успешных номеров: {successful_numbers}\n"
        text += "────────────────────\n"

    TELEGRAM_MESSAGE_LIMIT = 4096
    if len(text) > TELEGRAM_MESSAGE_LIMIT:
        text = text[:TELEGRAM_MESSAGE_LIMIT - 100] + "\n... (Слишком много групп, используйте пагинацию)"

    markup = types.InlineKeyboardMarkup()
    for group_id, group_name in page_groups:
        markup.add(types.InlineKeyboardButton(f"📊 {group_name}", callback_data=f"group_stats_{group_id}_1"))

    if total_pages > 1:
        row = []
        if page > 1:
            row.append(types.InlineKeyboardButton("⬅️ Назад", callback_data=f"group_statistics_{page-1}"))
        row.append(types.InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"))
        if page < total_pages:
            row.append(types.InlineKeyboardButton("Вперёд ➡️", callback_data=f"group_statistics_{page+1}"))
        markup.row(*row)

    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))

    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode='HTML', reply_markup=markup)
    except Exception as e:
        logging.error(f"Ошибка при обновлении сообщения group_statistics: {e}")
        bot.send_message(call.message.chat.id, text, parse_mode='HTML', reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("group_stats_"))
def show_group_stats(call):
    bot.answer_callback_query(call.id)
    
    # Извлекаем ID группы и номер страницы из callback_data
    parts = call.data.split("_")
    group_id = int(parts[2])
    page = int(parts[3]) if len(parts) > 3 else 1
    numbers_per_page = 5  # Количество номеров на странице
    
    with db.get_db() as conn:
        cursor = conn.cursor()
        # Получаем название группы
        cursor.execute('SELECT NAME FROM groups WHERE ID = ?', (group_id,))
        group_name = cursor.fetchone()
        if not group_name:
            bot.edit_message_text("❌ Группа не найдена.", call.message.chat.id, call.message.message_id, parse_mode='HTML')
            return
        group_name = group_name[0]

        # Подсчёт модераторов в группе
        cursor.execute('SELECT COUNT(*) FROM personal WHERE GROUP_ID = ? AND TYPE = ?', (group_id, 'moderator'))
        total_moderators = cursor.fetchone()[0]
        
        # Подсчёт номеров для группы
        cursor.execute('''
            SELECT COUNT(*) 
            FROM numbers n
            JOIN personal p ON n.CONFIRMED_BY_MODERATOR_ID = p.ID
            WHERE p.GROUP_ID = ? AND n.STATUS IN ('активен', 'отстоял')
        ''', (group_id,))
        total_numbers = cursor.fetchone()[0]
        
        # Вычисляем общее количество страниц
        total_pages = max(1, (total_numbers + numbers_per_page - 1) // numbers_per_page)
        page = min(max(1, page), total_pages)  # Ограничиваем страницу допустимым диапазоном
        
        # Получаем номера для текущей страницы
        offset = (page - 1) * numbers_per_page
        cursor.execute('''
            SELECT n.NUMBER, n.TAKE_DATE, n.SHUTDOWN_DATE, n.STATUS 
            FROM numbers n
            JOIN personal p ON n.CONFIRMED_BY_MODERATOR_ID = p.ID
            WHERE p.GROUP_ID = ? AND n.STATUS IN ('активен', 'отстоял')
            ORDER BY n.TAKE_DATE DESC
            LIMIT ? OFFSET ?
        ''', (group_id, numbers_per_page, offset))
        recent_numbers = cursor.fetchall()
    
    stats_text = (
        f"📊 <b>Статистика группы: {group_name}</b>\n\n"
        f"👥 Модераторов: <code>{total_moderators}</code>\n"
        f"📱 Всего номеров: <code>{total_numbers}</code>\n\n"
        f"📋 <b>Список номеров (страница {page}/{total_pages}):</b>\n"
    )
    
    if not recent_numbers:
        stats_text += "📭 Номера отсутствуют."
    else:
        for number, take_date, shutdown_date, status in recent_numbers:
            take_date_str = take_date if take_date not in ("0", "1") else "Неизвестно"
            shutdown_date_str = shutdown_date if shutdown_date != "0" else "Не завершён"
            stats_text += (
                f"\n📱 Номер: <code>{number}</code>\n"
                f"🟢 Взято: {take_date_str}\n"
                f"🔴 Отстоял: {shutdown_date_str}\n"
            )
    
    markup = types.InlineKeyboardMarkup()
    
    # Добавляем кнопки навигации
    if total_pages > 1:
        nav_buttons = []
        if page > 1:
            nav_buttons.append(types.InlineKeyboardButton("⬅️ Назад", callback_data=f"group_stats_{group_id}_{page-1}"))
        if page < total_pages:
            nav_buttons.append(types.InlineKeyboardButton("Вперед ➡️", callback_data=f"group_stats_{group_id}_{page+1}"))
        markup.add(*nav_buttons)
    
    markup.add(types.InlineKeyboardButton("🔙 К списку групп", callback_data="group_statistics_1"))
    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
    
    bot.edit_message_text(stats_text,
                          call.message.chat.id,
                          call.message.message_id,
                          reply_markup=markup,
                          parse_mode='HTML')




# ОБЫЧНОГО ПОЛЬЗОВАТЕЛЯ НОМЕРА:

import threading
import time
import logging
import re
from telebot.apihelper import ApiTelegramException

@bot.callback_query_handler(func=lambda call: call.data.startswith("my_numbers"))
def show_my_numbers(call):
    bot.answer_callback_query(call.id)
    user_id = call.from_user.id
    db.update_last_activity(user_id)
    page = int(call.data.split("_")[2]) if len(call.data.split("_")) > 2 else 1
    numbers_per_page = 5  # Количество номеров на странице
    
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM numbers WHERE ID_OWNER = ?', (user_id,))
        total_numbers = cursor.fetchone()[0]
        
        # Вычисляем общее количество страниц
        total_pages = max(1, (total_numbers + numbers_per_page - 1) // numbers_per_page)
        page = min(max(1, page), total_pages)
        
        # Получаем номера для текущей страницы
        offset = (page - 1) * numbers_per_page
        cursor.execute('SELECT NUMBER, STATUS, TAKE_DATE, SHUTDOWN_DATE FROM numbers WHERE ID_OWNER = ? ORDER BY TAKE_DATE DESC LIMIT ? OFFSET ?', 
                      (user_id, numbers_per_page, offset))
        numbers = cursor.fetchall()
    
    numbers_text = f"📱 <b>Мои номера (страница {page}/{total_pages})</b>\n\n"
    if not numbers:
        numbers_text += "📭 У вас пока нет номеров."
    else:
        for number, status, take_date, shutdown_date in numbers:
            take_date_str = take_date if take_date not in ("0", "1") else "Неизвестно"
            shutdown_date_str = shutdown_date if shutdown_date != "0" else "Не завершён"
            numbers_text += (
                f"📱 Номер: <code>{number}</code>\n"
                f"📊 Статус: {status}\n"
                f"🟢 Взято: {take_date_str}\n"
                f"🔴 Отстоял: {shutdown_date_str}\n\n"
            )
    
    markup = types.InlineKeyboardMarkup()
    
    # Добавляем кнопки навигации
    if total_pages > 1:
        nav_buttons = []
        if page > 1:
            nav_buttons.append(types.InlineKeyboardButton("⬅️ Назад", callback_data=f"my_numbers_{page-1}"))
        if page < total_pages:
            nav_buttons.append(types.InlineKeyboardButton("Вперед ➡️", callback_data=f"my_numbers_{page+1}"))
        markup.add(*nav_buttons)
    
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="profile"))
    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
    
    bot.edit_message_text(numbers_text,
                          call.message.chat.id,
                          call.message.message_id,
                          reply_markup=markup,
                          parse_mode='HTML')

def safe_send_message(chat_id, text, parse_mode=None, reply_markup=None):
    try:
        bot.send_message(chat_id, text, parse_mode=parse_mode, reply_markup=reply_markup)
    except ApiTelegramException as e:
        if e.result_json.get('error_code') == 429:
            time.sleep(1)
            safe_send_message(chat_id, text, parse_mode, reply_markup)
        else:
            logging.error(f"Ошибка отправки сообщения {chat_id}: {e}")
# Глобальная переменная для хранения данных номеров (можно заменить на временное хранилище в будущем)
numbers_data_cache = {}

@bot.callback_query_handler(func=lambda call: call.data.startswith("all_numbers"))
def show_all_numbers(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для просмотра всех номеров!")
        return
    
    bot.answer_callback_query(call.id)
    
    page = int(call.data.split("_")[2]) if len(call.data.split("_")) > 2 else 1
    numbers_per_page = 5  # Количество номеров на странице
    
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM numbers')
        total_numbers = cursor.fetchone()[0]
        
        # Вычисляем общее количество страниц
        total_pages = max(1, (total_numbers + numbers_per_page - 1) // numbers_per_page)
        page = min(max(1, page), total_pages)
        
        # Получаем номера для текущей страницы
        offset = (page - 1) * numbers_per_page
        cursor.execute('''
            SELECT NUMBER, STATUS, TAKE_DATE, SHUTDOWN_DATE, ID_OWNER, CONFIRMED_BY_MODERATOR_ID, GROUP_CHAT_ID, TG_NUMBER
            FROM numbers
            ORDER BY TAKE_DATE DESC
            LIMIT ? OFFSET ?
        ''', (numbers_per_page, offset))
        numbers = cursor.fetchall()
    
    numbers_text = f"📋 <b>Все номера (страница {page}/{total_pages})</b>\n\n"
    if not numbers:
        numbers_text += "📭 Номера отсутствуют."
    else:
        for number, status, take_date, shutdown_date, owner_id, confirmed_by_moderator_id, group_chat_id, tg_number in numbers:
            # Получаем имя группы
            group_name = db.get_group_name(group_chat_id) if group_chat_id else "Не указана"
            
            # Формируем отображаемые даты
            take_date_str = take_date if take_date not in ("0", "1") else "Неизвестно"
            shutdown_date_str = shutdown_date if shutdown_date != "0" else "Не завершён"
            
            # Формируем информацию о модераторе
            moderator_info = f"Модератор: @{confirmed_by_moderator_id}" if confirmed_by_moderator_id else "Модератор: Не назначен"
            
            # Формируем текст для номера
            numbers_text += (
                f"📱 Номер: <code>{number}</code>\n"
                f"👤 Владелец: <code>{owner_id}</code>\n"
                f"📊 Статус: {status}\n"
                f"🟢 Взято: {take_date_str}\n"
                f"🔴 Отстоял: {shutdown_date_str}\n"
                f"{moderator_info}\n"
                f"🏷 Группа: {group_name}\n"
                f"📱 ТГ: {tg_number or 'Не указан'}\n\n"
            )
    
    markup = types.InlineKeyboardMarkup()
    
    # Добавляем кнопки навигации
    if total_pages > 1:
        nav_buttons = []
        if page > 1:
            nav_buttons.append(types.InlineKeyboardButton("⬅️ Назад", callback_data=f"all_numbers_{page-1}"))
        if page < total_pages:
            nav_buttons.append(types.InlineKeyboardButton("Вперед ➡️", callback_data=f"all_numbers_{page+1}"))
        markup.add(*nav_buttons)
    
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
    
    try:
        bot.edit_message_text(
            numbers_text,
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup,
            parse_mode='HTML'
        )
    except telebot.apihelper.ApiTelegramException as e:
        print(f"[ERROR] Не удалось отредактировать сообщение: {e}")
        bot.send_message(
            call.message.chat.id,
            numbers_text,
            reply_markup=markup,
            parse_mode='HTML'
        )
        
def show_numbers_page(call, page):
    user_id = call.from_user.id
    if user_id not in numbers_data_cache:
        bot.answer_callback_query(call.id, "❌ Данные устарели, пожалуйста, запросите список заново!")
        return
    
    numbers = numbers_data_cache[user_id]
    items_per_page = 5  # По 5 номеров на страницу
    total_items = len(numbers)
    total_pages = (total_items + items_per_page - 1) // items_per_page
    
    if page < 0 or page >= total_pages:
        bot.answer_callback_query(call.id, "❌ Страница недоступна!")
        return
    
    start_idx = page * items_per_page
    end_idx = min(start_idx + items_per_page, total_items)
    page_numbers = numbers[start_idx:end_idx]
    
    # Формируем текст для текущей страницы
    text = f"<b>📱 Список всех номеров (Страница {page + 1} из {total_pages}):</b>\n\n"
    for number, take_date, shutdown_date, user_id, group_name in page_numbers:
        group_info = f"👥 Группа: {group_name}" if group_name else "👥 Группа: Не указана"
        user_info = f"🆔 Пользователь: {user_id}" if user_id else "🆔 Пользователь: Не указан"
        text += (
            f"📞 <code>{number}</code>\n"
            f"{user_info}\n"
            f"{group_info}\n"
            f"📅 Взят: {take_date}\n"
            f"📴 Отключён: {shutdown_date or 'Ещё активен'}\n\n"
        )
    
    # Создаём кнопки для навигации
    markup = types.InlineKeyboardMarkup()
    nav_buttons = []
    if page > 0:
        nav_buttons.append(types.InlineKeyboardButton("⬅️ Назад", callback_data=f"numbers_page_{page-1}"))
    if page < total_pages - 1:
        nav_buttons.append(types.InlineKeyboardButton("Вперёд ➡️", callback_data=f"numbers_page_{page+1}"))
    if nav_buttons:
        markup.row(*nav_buttons)
    
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
    
    # Отправляем или редактируем сообщение
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
        print(f"Удалено старое сообщение {call.message.message_id} в чате {call.message.chat.id}")
    except Exception as e:
        print(f"Ошибка при удалении старого сообщения: {e}")
    
    bot.send_message(
        call.message.chat.id,
        text,
        parse_mode='HTML',
        reply_markup=markup
    )
    print(f"Страница {page + 1} отправлена успешно")

@bot.callback_query_handler(func=lambda call: call.data.startswith("numbers_page_"))
def numbers_page_callback(call):
    page = int(call.data.split("_")[2])
    show_numbers_page(call, page)



@bot.callback_query_handler(func=lambda call: call.data == "settings")
def show_settings(call):
    if call.from_user.id in config.ADMINS_ID:
        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT PRICE, HOLD_TIME FROM settings')
            result = cursor.fetchone()
            price, hold_time = result if result else (2.0, 5)
        
        settings_text = (
            "<b>⚙️ Настройки оплаты</b>\n\n"
            f"Текущая ставка: <code>{price}$</code> за номер\n"
            f"Время холда: <code>{hold_time}</code> минут\n\n"
            "Выберите параметр для изменения:"
        )
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("💰 Изменить сумму", callback_data="change_amount"))
        markup.add(types.InlineKeyboardButton("⏱ Изменить время холда", callback_data="change_hold_time"))
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        
        bot.edit_message_text(settings_text,
                            call.message.chat.id,
                            call.message.message_id,
                            parse_mode='HTML',
                            reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "change_amount")
def change_amount_request(call):
    if call.from_user.id in config.ADMINS_ID:
        msg = bot.edit_message_text("💰 Введите новую сумму оплаты (в долларах, например: 2):",
                                  call.message.chat.id,
                                  call.message.message_id)
        bot.register_next_step_handler(msg, process_change_amount)


def process_change_amount(message):
    if message.from_user.id in config.ADMINS_ID:
        try:
            new_amount = float(message.text)
            if new_amount <= 0:
                raise ValueError
            with db.get_db() as conn:
                cursor = conn.cursor()
                cursor.execute('UPDATE settings SET PRICE = ?', (new_amount,))
                conn.commit()
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="settings"))
            markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
            bot.send_message(message.chat.id, f"✅ Сумма оплаты изменена на {new_amount}$", reply_markup=markup)
        except ValueError:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="settings"))
            markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
            bot.send_message(message.chat.id, "❌ Введите корректное положительное число!", reply_markup=markup)








# Словарь для отслеживания сообщений с кодами
code_messages = {}  # {number: {"chat_id": int, "message_id": int, "timestamp": datetime, "tg_number": int, "owner_id": int}}



def check_code_timeout():
    """Проверяет, истекло ли 5 минут с момента отправки кода. Если да, подтверждает номер как активный."""
    print("Запуск функции check_code_timeout")
    while True:
        try:
            current_time = datetime.now()
            print(f"[TIMEOUT_CHECK] Текущее время: {current_time}, Количество номеров в отслеживании: {len(code_messages)}")
            
            for number, data in list(code_messages.items()):
                elapsed_time = (current_time - data["timestamp"]).total_seconds() / 60
                print(f"[TIMEOUT_CHECK] Номер {number}, прошло времени: {elapsed_time:.2f} минут, TG: {data.get('tg_number', 'N/A')}")
                
                if elapsed_time >= 5:
                    print(f"[TIMEOUT_CHECK] Время истекло для номера {number} ({elapsed_time:.2f} минут)")
                    with db.get_db() as conn:
                        cursor = conn.cursor()
                        cursor.execute('SELECT ID_OWNER, STATUS, MODERATOR_ID FROM numbers WHERE NUMBER = ?', (number,))
                        result = cursor.fetchone()
                        
                        if not result:
                            logging.warning(f"[TIMEOUT_CHECK] Номер {number} не найден в базе данных")
                            del code_messages[number]
                            continue
                            
                        owner_id, status, moderator_id = result
                        print(f"[TIMEOUT_CHECK] Номер {number}, статус: {status}, владелец: {owner_id}, модератор: {moderator_id}")
                        
                        if status not in ("ожидает", "на проверке", "taken"):
                            logging.warning(f"[TIMEOUT_CHECK] Номер {number} имеет неподходящий статус: {status}, пропускаем")
                            del code_messages[number]
                            continue
                        
                        current_date = current_time.strftime("%Y-%m-%d %H:%M:%S")
                        cursor.execute(
                            'UPDATE numbers SET STATUS = "активен", TAKE_DATE = ?, VERIFICATION_CODE = NULL, CONFIRMED_BY_MODERATOR_ID = NULL WHERE NUMBER = ?',
                            (current_date, number)
                        )
                        conn.commit()
                        print(f"[TIMEOUT_CHECK] Номер {number} автоматически подтверждён как активный через 5 минут.")

                        markup_owner = types.InlineKeyboardMarkup()
                        markup_owner.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                        try:
                            safe_send_message(
                                owner_id,
                                f"✅ Ваш номер {number} автоматически подтверждён и теперь активен.\n⏳ Отсчёт времени начался.",
                                parse_mode='HTML',
                                reply_markup=markup_owner
                            )
                            print(f"[TIMEOUT_CHECK] Отправлено уведомление владельцу {owner_id}")
                        except Exception as e:
                            print(f"[TIMEOUT_CHECK] Ошибка отправки уведомления владельцу {owner_id}: {e}")

                        if moderator_id:
                            markup_mod = types.InlineKeyboardMarkup()
                            markup_mod.add(types.InlineKeyboardButton("📲 Получить новый номер", callback_data="get_number"))
                            markup_mod.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                            try:
                                safe_send_message(
                                    moderator_id,
                                    f"📱 Номер {number} автоматически подтверждён через 5 минут бездействия.",
                                    parse_mode='HTML',
                                    reply_markup=markup_mod
                                )
                                print(f"[TIMEOUT_CHECK] Отправлено уведомление модератору {moderator_id}")
                            except Exception as e:
                                print(f"[TIMEOUT_CHECK] Ошибка отправки уведомления модератору {moderator_id}: {e}")

                        try:
                            bot.edit_message_text(
                                f"📱 <b>ТГ {data['tg_number']}</b>\n"
                                f"✅ Номер {number} автоматически подтверждён через 5 минут.",
                                data["chat_id"],
                                data["message_id"],
                                parse_mode='HTML'
                            )
                            print(f"[TIMEOUT_CHECK] Обновлено сообщение в группе {data['chat_id']}")
                        except Exception as e:
                            print(f"[TIMEOUT_CHECK] Не удалось отредактировать сообщение для номера {number}: {e}")

                        print(f"[TIMEOUT_CHECK] Удаление номера {number} из отслеживания после автоподтверждения")
                        del code_messages[number]

            time.sleep(10)
        except Exception as e:
            print(f"[TIMEOUT_CHECK] Критическая ошибка в check_code_timeout: {str(e)}", exc_info=True)
            time.sleep(10)










































@bot.callback_query_handler(func=lambda call: call.data == "submit_number")
def submit_number(call):
    user_id = call.from_user.id 
    db.update_last_activity(user_id)
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT PRICE, HOLD_TIME FROM settings')
        result = cursor.fetchone()
        price, hold_time = result if result else (2.0, 5)
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT CAN_SUBMIT_NUMBERS FROM requests WHERE ID = ?', (user_id,))
        user = cursor.fetchone()
        if user and user[0] == 0:
            bot.answer_callback_query(call.id, "🚫 Вам запрещено сдавать номера!")
            return
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except:
        pass
    msg = bot.send_message(
        call.message.chat.id,
        f"📱 Введите ваши номера телефона (по одному в строке):\nПример:\n+79991234567\n+79001234567\n+79021234567\n💵 Текущая цена: {price}$ за номер\n⏱ Холд: {hold_time} минут",
        reply_markup=markup,
        parse_mode='HTML'
    )
    bot.register_next_step_handler(msg, process_numbers)

def process_numbers(message):
    if not message or not message.text:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("📱 Попробовать снова", callback_data="submit_number"))
        markup.add(types.InlineKeyboardButton("🔙 Вернуться в меню", callback_data="back_to_main"))
        bot.send_message(message.chat.id, "❌ Пожалуйста, отправьте номера текстом!", reply_markup=markup)
        return

    numbers = message.text.strip().split('\n')
    if not numbers or all(not num.strip() for num in numbers):
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("📱 Попробовать снова", callback_data="submit_number"))
        markup.add(types.InlineKeyboardButton("🔙 Вернуться в меню", callback_data="back_to_main"))
        bot.send_message(message.chat.id, "❌ Вы не указали ни одного номера!", reply_markup=markup)
        return

    valid_numbers = []
    invalid_numbers = []
    
    for number in numbers:
        number = number.strip()
        if not number:
            continue
        corrected_number = is_russian_number(number)
        if corrected_number:
            valid_numbers.append(corrected_number)
        else:
            invalid_numbers.append(number)

    if not valid_numbers:
        response_text = "❌ Все введённые номера некорректны!\nПожалуйста, вводите номера в формате +79991234567."
        if invalid_numbers:
            response_text += "\n\n❌ Неверный формат:\n" + "\n".join(invalid_numbers)
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("📱 Попробовать снова", callback_data="submit_number"))
        markup.add(types.InlineKeyboardButton("🔙 Вернуться в меню", callback_data="back_to_main"))
        bot.send_message(message.chat.id, response_text, reply_markup=markup, parse_mode='HTML')
        return

    try:
        with db.get_db() as conn:
            cursor = conn.cursor()
            success_count = 0
            already_exists = 0
            successfully_added = []

            for number in valid_numbers:
                try:
                    cursor.execute('SELECT NUMBER, SHUTDOWN_DATE FROM numbers WHERE NUMBER = ?', (number,))
                    existing_number = cursor.fetchone()

                    if existing_number:
                        if existing_number[1] == "0":
                            already_exists += 1
                            continue
                        else:
                            cursor.execute('DELETE FROM numbers WHERE NUMBER = ?', (number,))

                    cursor.execute('INSERT INTO numbers (NUMBER, ID_OWNER, TAKE_DATE, SHUTDOWN_DATE, STATUS) VALUES (?, ?, ?, ?, ?)',
                                  (number, message.from_user.id, '0', '0', 'ожидает'))
                    success_count += 1
                    successfully_added.append(number)
                except sqlite3.IntegrityError:
                    already_exists += 1
                    continue
            conn.commit()

        response_text = "<b>📊 Результат добавления номеров:</b>\n\n"
        if success_count > 0:
            response_text += f"✅ Успешно добавлено: {success_count} номеров\n"
            response_text += "📱 Добавленные номера:\n" + "\n".join(successfully_added) + "\n"
        if already_exists > 0:
            response_text += f"⚠️ Уже существуют: {already_exists} номеров\n"
        if invalid_numbers:
            response_text += f"❌ Неверный формат:\n" + "\n".join(invalid_numbers) + "\n"

    except Exception as e:
        print(f"Ошибка в process_numbers: {e}")
        response_text = "❌ Произошла ошибка при добавлении номеров. Попробуйте снова."

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("📱 Добавить ещё", callback_data="submit_number"))
    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
    bot.send_message(message.chat.id, response_text, reply_markup=markup, parse_mode='HTML')






@bot.callback_query_handler(func=lambda call: call.data == "db_menu")
def db_menu_callback(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для доступа к управлению БД!")
        return
    
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("📥 Скачать БД (НОМЕРА)", callback_data="download_numbers"))
    markup.add(InlineKeyboardButton("🗑 Очистить БД (НОМЕРА)", callback_data="clear_numbers"))
    markup.add(InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
    
    bot.edit_message_text("🗃 Управление базой данных", call.message.chat.id, call.message.message_id, reply_markup=markup)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "download_numbers")
def download_numbers_callback(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для скачивания БД!")
        return
    
    download_numbers(call.message.chat.id)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "clear_numbers")
def clear_numbers_callback(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для очистки БД!")
        return
    
    clear_database(call.message.chat.id)
    bot.answer_callback_query(call.id)




#=============================================================================================================





@bot.callback_query_handler(func=lambda call: call.data == "Gv")
def settingssss(data):
    # Определяем, является ли входной параметр callback (call) или сообщением (message)
    is_callback = hasattr(data, 'message')
    user_id = data.from_user.id
    chat_id = data.message.chat.id if is_callback else data.chat.id
    message_id = data.message.message_id if is_callback else data.message_id
    
    # Проверяем, что пользователь — администратор
    if user_id not in config.ADMINS_ID:
        if is_callback:
            bot.answer_callback_query(data.id, "❌ У вас нет прав для выполнения этого действия!")
        else:
            bot.send_message(chat_id, "❌ У вас нет прав для выполнения этого действия!", parse_mode='HTML')
        return
    
    # Формируем текст и кнопки для меню
    menu_text = "📋 <b>Меню:</b>"
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("⚙️ Настройки АФК", callback_data="afk_settings"))
    markup.add(types.InlineKeyboardButton("💸 Изменить цену для определённого человека", callback_data="change_price"))
    markup.add(types.InlineKeyboardButton("📉 Понизить баланс человека", callback_data="reduce_balance"))
    markup.add(types.InlineKeyboardButton("📤 Выслать всем чеки", callback_data="send_all_checks"))
    markup.add(types.InlineKeyboardButton("📜 Отправить человеку чек", callback_data="send_check"))
    markup.add(types.InlineKeyboardButton("➕ Добавить группу", callback_data="add_group"))
    markup.add(types.InlineKeyboardButton("➖ Удалить группу", callback_data="remove_group"))
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="GV"))
    
    # Редактируем или отправляем сообщение в зависимости от типа вызова
    try:
        if is_callback:
            bot.edit_message_text(
                menu_text,
                chat_id,
                message_id,
                parse_mode='HTML',
                reply_markup=markup
            )
        else:
            bot.send_message(
                chat_id,
                menu_text,
                parse_mode='HTML',
                reply_markup=markup
            )
    except Exception as e:
        print(f"[ERROR] Не удалось обработать сообщение: {e}")
        bot.send_message(
            chat_id,
            menu_text,
            parse_mode='HTML',
            reply_markup=markup
        )

#ИЗМЕНИТ ЦЕНУ:
@bot.callback_query_handler(func=lambda call: call.data == "change_price")
def change_price_start(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для этого действия!")
        return
    
    msg = bot.edit_message_text(
        "📝 Введите ID пользователя, для которого хотите установить индивидуальную цену (ответьте на это сообщение):",
        call.message.chat.id,
        call.message.message_id
    )
    bot.register_next_step_handler(msg, process_user_id_for_price)

def process_user_id_for_price(message):
    if message.from_user.id not in config.ADMINS_ID:
        bot.send_message(message.chat.id, "❌ У вас нет прав для этого действия!")
        return
    
    try:
        user_id = int(message.text.strip())
        msg = bot.send_message(
            message.chat.id,
            f"💵 Введите новую цену (в $) для пользователя {user_id} (ответьте на это сообщение):"
        )
        bot.register_next_step_handler(msg, process_price, user_id)
    except ValueError:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Админ-панель", callback_data="admin_panel"))
        bot.send_message(message.chat.id, "❌ Неверный формат ID пользователя!", reply_markup=markup)

def process_price(message, user_id):
    if message.from_user.id not in config.ADMINS_ID:
        bot.send_message(message.chat.id, "❌ У вас нет прав для этого действия!")
        return
    
    try:
        price = float(message.text.strip())
        if price <= 0:
            raise ValueError("Цена должна быть положительной!")
        
        db_module.set_custom_price(user_id, price)
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Админ-панель", callback_data="admin_panel"))
        bot.send_message(
            message.chat.id,
            f"✅ Индивидуальная цена для пользователя {user_id} установлена: {price}$",
            reply_markup=markup
        )
        
        # Уведомляем пользователя
        try:
            bot.send_message(
                user_id,
                f"💵 Ваша индивидуальная цена за номер изменена на {price}$!"
            )
        except Exception as e:
            print(f"[ERROR] Не удалось уведомить пользователя {user_id}: {e}")
            
    except ValueError as e:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Админ-панель", callback_data="admin_panel"))
        bot.send_message(message.chat.id, f"❌ Ошибка: {str(e)}", reply_markup=markup)

# СНИЗИТЬ БАЛАНС:

@bot.callback_query_handler(func=lambda call: call.data == "reduce_balance")
def reduce_balance_start(call):
    user_id = call.from_user.id
    
    # Проверяем, что пользователь — администратор
    if user_id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для выполнения этого действия!")
        return
    
    # Формируем запрос user ID
    text = "📝 <b>Укажите user ID</b> (используйте reply на это сообщение):"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="Gv"))
    
    try:
        msg = bot.edit_message_text(
            text,
            call.message.chat.id,
            call.message.message_id,
            parse_mode='HTML',
            reply_markup=markup
        )
        # Регистрируем следующий шаг для обработки user ID
        bot.register_next_step_handler(msg, process_user_id_for_balance, call.message.chat.id, call.message.message_id)
    except Exception as e:
        print(f"[ERROR] Не удалось обновить сообщение: {e}")
        msg = bot.send_message(
            call.message.chat.id,
            text,
            parse_mode='HTML',
            reply_markup=markup
        )
        bot.register_next_step_handler(msg, process_user_id_for_balance, call.message.chat.id, msg.message_id)

def process_user_id_for_balance(message, original_chat_id, original_message_id):
    user_id = message.from_user.id
    
    # Проверяем, что пользователь — администратор
    if user_id not in config.ADMINS_ID:
        bot.send_message(message.chat.id, "❌ У вас нет прав для выполнения этого действия!", parse_mode='HTML')
        return
    
    # Проверяем, что сообщение является ответом (reply)
    if not message.reply_to_message:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔍 Попробовать снова", callback_data="reduce_balance"))
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="Gv"))
        bot.send_message(
            message.chat.id,
            "❌ Пожалуйста, используйте reply на сообщение для ввода user ID!",
            reply_markup=markup,
            parse_mode='HTML'
        )
        return
    
    # Получаем user ID
    try:
        target_user_id = int(message.text.strip())
    except ValueError:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔍 Попробовать снова", callback_data="reduce_balance"))
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="Gv"))
        bot.send_message(
            message.chat.id,
            "❌ Неверный формат user ID! Введите целое число.",
            reply_markup=markup,
            parse_mode='HTML'
        )
        return
    
    # Проверяем, существует ли пользователь
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT ID FROM users WHERE ID = ?', (target_user_id,))
        user = cursor.fetchone()
        if not user:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔍 Попробовать снова", callback_data="reduce_balance"))
            markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="Gv"))
            bot.send_message(
                message.chat.id,
                f"❌ Пользователь с ID {target_user_id} не найден!",
                reply_markup=markup,
                parse_mode='HTML'
            )
            return
    
    # Удаляем сообщение с user ID
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception as e:
        print(f"[ERROR] Не удалось удалить сообщение с user ID: {e}")
    
    # Запрашиваем сумму
    text = f"💸 <b>На сколько вы хотите уменьшить баланс пользователя {target_user_id}?</b> (введите число в $):"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="Gv"))
    
    try:
        msg = bot.edit_message_text(
            text,
            original_chat_id,
            original_message_id,
            parse_mode='HTML',
            reply_markup=markup
        )
        # Регистрируем следующий шаг для обработки суммы
        bot.register_next_step_handler(msg, process_balance_reduction, target_user_id, original_chat_id, original_message_id)
    except Exception as e:
        print(f"[ERROR] Не удалось обновить сообщение: {e}")
        msg = bot.send_message(
            original_chat_id,
            text,
            parse_mode='HTML',
            reply_markup=markup
        )
        bot.register_next_step_handler(msg, process_balance_reduction, target_user_id, original_chat_id, msg.message_id)

def process_balance_reduction(message, target_user_id, original_chat_id, original_message_id):
    user_id = message.from_user.id
    
    # Проверяем, что пользователь — администратор
    if user_id not in config.ADMINS_ID:
        bot.send_message(message.chat.id, "❌ У вас нет прав для выполнения этого действия!", parse_mode='HTML')
        return
    
    # Проверяем, что сообщение является ответом (reply)
    if not message.reply_to_message:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔍 Попробовать снова", callback_data="reduce_balance"))
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="Gv"))
        bot.send_message(
            message.chat.id,
            "❌ Пожалуйста, используйте reply на сообщение для ввода суммы!",
            reply_markup=markup,
            parse_mode='HTML'
        )
        return
    
    # Получаем сумму
    try:
        amount = float(message.text.strip())
        if amount <= 0:
            raise ValueError("Сумма должна быть положительной")
    except ValueError:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔍 Попробовать снова", callback_data="reduce_balance"))
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="Gv"))
        bot.send_message(
            message.chat.id,
            "❌ Неверный формат суммы! Введите положительное число (например, 10.5).",
            reply_markup=markup,
            parse_mode='HTML'
        )
        return
    
    # Проверяем текущий баланс пользователя
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT BALANCE FROM users WHERE ID = ?', (target_user_id,))
        current_balance = cursor.fetchone()[0]
        
        if current_balance < amount:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔍 Попробовать снова", callback_data="reduce_balance"))
            markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="Gv"))
            bot.send_message(
                message.chat.id,
                f"❌ Недостаточно средств на балансе пользователя {target_user_id}! Текущий баланс: {current_balance} $",
                reply_markup=markup,
                parse_mode='HTML'
            )
            return
        
        # Списываем сумму
        new_balance = current_balance - amount
        cursor.execute('UPDATE users SET BALANCE = ? WHERE ID = ?', (new_balance, target_user_id))
        conn.commit()
    
    # Удаляем сообщение с суммой
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception as e:
        print(f"[ERROR] Не удалось удалить сообщение с суммой: {e}")
    
    # Возвращаемся к главному меню
    menu_text = "📋 <b>Меню:</b>"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("⚙️ Настройки АФК", callback_data="afk_settings"))
    markup.add(types.InlineKeyboardButton("💸 Изменить цену для определённого человека", callback_data="change_price"))
    markup.add(types.InlineKeyboardButton("📉 Понизить баланс человека", callback_data="reduce_balance"))
    markup.add(types.InlineKeyboardButton("📤 Выслать всем чеки", callback_data="send_all_checks"))
    markup.add(types.InlineKeyboardButton("📜 Отправить человеку чек", callback_data="send_check"))
    markup.add(types.InlineKeyboardButton("🔙 Назад в профиль", callback_data="profile"))
    
    try:
        bot.edit_message_text(
            menu_text,
            original_chat_id,
            original_message_id,
            parse_mode='HTML',
            reply_markup=markup
        )
    except Exception as e:
        print(f"[ERROR] Не удалось обновить сообщение: {e}")
        bot.send_message(
            original_chat_id,
            menu_text,
            parse_mode='HTML',
            reply_markup=markup
        )
    
    # Уведомляем администратора об успешном списании
    bot.send_message(
        original_chat_id,
        f"✅ Баланс пользователя {target_user_id} уменьшен на {amount} $. Новый баланс: {new_balance} $",
        parse_mode='HTML'
    )



@bot.callback_query_handler(func=lambda call: call.data == "afk_settings")
def afk_settings(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для доступа к настройкам АФК!")
        return
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
    
    msg = bot.edit_message_text(
        "⚙️ <b>Настройки АФК</b>\n\nВведите ID пользователя для управления его АФК-статусом:",
        call.message.chat.id,
        call.message.message_id,
        parse_mode='HTML',
        reply_markup=markup
    )
    bot.register_next_step_handler(msg, process_afk_user_id)

def process_afk_user_id(message):
    if message.from_user.id not in config.ADMINS_ID:
        bot.send_message(message.chat.id, "❌ У вас нет прав для выполнения этого действия!")
        return
    
    try:
        target_user_id = int(message.text.strip())
    except ValueError:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="afk_settings"))
        bot.send_message(message.chat.id, "❌ Неверный формат ID пользователя. Пожалуйста, введите числовой ID.", reply_markup=markup)
        return
    
    try:
        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT IS_AFK, AFK_LOCKED FROM users WHERE ID = ?', (target_user_id,))
            user = cursor.fetchone()
            if not user:
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="afk_settings"))
                bot.send_message(message.chat.id, f"❌ Пользователь с ID {target_user_id} не найден.", reply_markup=markup)
                return
            
            is_afk, afk_locked = user
            print(f"[DEBUG] Статус AFK для пользователя {target_user_id}: IS_AFK={is_afk}, AFK_LOCKED={afk_locked}")
            afk_status_text = "Включён" if is_afk else "Выключен"
            
            markup = types.InlineKeyboardMarkup()
            markup.add(
                types.InlineKeyboardButton("🟢 Включить АФК", callback_data=f"admin_enable_afk_{target_user_id}"),
                types.InlineKeyboardButton("🔴 Выключить АФК", callback_data=f"admin_disable_afk_{target_user_id}")
            )
            markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="afk_settings"))
            
            bot.send_message(
                message.chat.id,
                f"👤 <b>User ID:</b> {target_user_id}\n"
                f"🔔 <b>АФК Статус:</b> {afk_status_text}\n"
                f"🔒 <b>Блокировка АФК:</b> {'Да' if afk_locked else 'Нет'}",
                parse_mode='HTML',
                reply_markup=markup
            )
    except Exception as e:
        print(f"[ERROR] Ошибка при обработке AFK для пользователя {target_user_id}: {e}")
        bot.send_message(message.chat.id, "❌ Произошла ошибка при получении данных. Попробуйте позже.")

@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_enable_afk_"))
def admin_enable_afk(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для выполнения этого действия!")
        return
    
    # Извлекаем target_user_id из callback_data
    target_user_id = int(call.data.replace("admin_enable_afk_", ""))
    
    # Обновляем статус AFK в базе данных
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('UPDATE users SET IS_AFK = ?, AFK_LOCKED = ? WHERE ID = ?', (1, 1, target_user_id))
        conn.commit()
        print(f"[DEBUG] АФК включён для пользователя {target_user_id} с блокировкой")

    # Обновляем сообщение с актуальным статусом
    chat_id = call.message.chat.id
    message_id = call.message.message_id
    
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT IS_AFK, AFK_LOCKED FROM users WHERE ID = ?', (target_user_id,))
        user = cursor.fetchone()
        is_afk, afk_locked = user
        afk_status_text = "Включён" if is_afk else "Выключен"
        
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("🟢 Включить АФК", callback_data=f"admin_enable_afk_{target_user_id}"),
            types.InlineKeyboardButton("🔴 Выключить АФК", callback_data=f"admin_disable_afk_{target_user_id}")
        )
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="afk_settings"))
        
        try:
            bot.edit_message_text(
                f"👤 <b>User ID:</b> {target_user_id}\n"
                f"🔔 <b>АФК Статус:</b> {afk_status_text}",
                chat_id,
                message_id,
                parse_mode='HTML',
                reply_markup=markup
            )
        except Exception as e:
            print(f"[ERROR] Не удалось обновить сообщение: {e}")
            bot.send_message(chat_id, f"👤 <b>User ID:</b> {target_user_id}\n🔔 <b>АФК Статус:</b> {afk_status_text}", parse_mode='HTML', reply_markup=markup)

    bot.answer_callback_query(call.id, "✅ АФК включён для пользователя!")

@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_disable_afk_"))
def admin_disable_afk(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для выполнения этого действия!")
        return
    
    # Извлекаем target_user_id из callback_data
    target_user_id = int(call.data.replace("admin_disable_afk_", ""))
    
    # Обновляем статус AFK в базе данных
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('UPDATE users SET IS_AFK = ?, AFK_LOCKED = ? WHERE ID = ?', (0, 0, target_user_id))
        conn.commit()
        print(f"[DEBUG] АФК выключен для пользователя {target_user_id}, блокировка снята")

    # Обновляем сообщение с актуальным статусом
    chat_id = call.message.chat.id
    message_id = call.message.message_id
    
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT IS_AFK, AFK_LOCKED FROM users WHERE ID = ?', (target_user_id,))
        user = cursor.fetchone()
        is_afk, afk_locked = user
        afk_status_text = "Включён" if is_afk else "Выключен"
        
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("🟢 Включить АФК", callback_data=f"admin_enable_afk_{target_user_id}"),
            types.InlineKeyboardButton("🔴 Выключить АФК", callback_data=f"admin_disable_afk_{target_user_id}")
        )
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="afk_settings"))
        
        try:
            bot.edit_message_text(
                f"👤 <b>User ID:</b> {target_user_id}\n"
                f"🔔 <b>АФК Статус:</b> {afk_status_text}",
                chat_id,
                message_id,
                parse_mode='HTML',
                reply_markup=markup
            )
        except Exception as e:
            print(f"[ERROR] Не удалось обновить сообщение: {e}")
            bot.send_message(chat_id, f"👤 <b>User ID:</b> {target_user_id}\n🔔 <b>АФК Статус:</b> {afk_status_text}", parse_mode='HTML', reply_markup=markup)

    bot.answer_callback_query(call.id, "✅ АФК выключен для пользователя!")


from datetime import datetime
import config
from telebot import types
import requests
from requests.exceptions import RequestException


def cancel_old_checks(crypto_api):
    try:
        checks_result = crypto_api.get_checks(status="active")
        if checks_result.get("ok", False):
            for check in checks_result["result"]["items"]:
                check_id = check["check_id"]
                crypto_api.delete_check(check_id=check_id)
                print(f"[INFO] Отменён чек {check_id}, высвобождено {check['amount']} USDT")
    except Exception as e:
        print(f"[ERROR] Не удалось отменить старые чеки: {e}")

@bot.callback_query_handler(func=lambda call: call.data == "send_all_checks")
def send_all_checks(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для выполнения этого действия!")
        return
    
    # Инициализация CryptoPay
    crypto_api = crypto_pay.CryptoPay()
    
    # Проверка баланса CryptoPay
    try:
        # Отменяем старые чеки
        cancel_old_checks(crypto_api)
        
        # Проверяем баланс после отмены
        balance_result = crypto_api.get_balance()
        if not balance_result.get("ok", False):
            bot.edit_message_text(
                "❌ Ошибка при проверке баланса CryptoPay. Попробуйте позже.",
                call.message.chat.id,
                call.message.message_id
            )
            return
        
        usdt_balance = next((float(item["available"]) for item in balance_result["result"] if item["currency_code"] == "USDT"), 0.0)
        usdt_onhold = next((float(item["onhold"]) for item in balance_result["result"] if item["currency_code"] == "USDT"), 0.0)
        
        print(f"[INFO] Баланс CryptoPay: доступно {usdt_balance} USDT, в резерве {usdt_onhold} USDT")
        
        if usdt_balance <= 0:
            bot.edit_message_text(
                f"❌ Недостаточно средств на балансе CryptoPay.\nДоступно: {usdt_balance} USDT\nВ резерве: {usdt_onhold} USDT",
                call.message.chat.id,
                call.message.message_id
            )
            return
    except Exception as e:
        print(f"[ERROR] Не удалось проверить баланс CryptoPay: {e}")
        bot.edit_message_text(
            "❌ Ошибка при проверке баланса CryptoPay. Попробуйте позже.",
            call.message.chat.id,
            call.message.message_id
        )
        return
    
    # Проверка баланса казны
    with db_module.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT BALANCE FROM treasury WHERE ID = 1')
        treasury_result = cursor.fetchone()
        treasury_balance = treasury_result[0] if treasury_result else 0.0
        
        if treasury_balance <= 0:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Админ-панель", callback_data="admin_panel"))
            bot.edit_message_text(
                "❌ Недостаточно средств в казне для выплат.",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=markup
            )
            return
        
        # Получаем пользователей с балансом > 0.2
        cursor.execute('SELECT ID, BALANCE FROM users WHERE BALANCE > 0.2')
        users = cursor.fetchall()
        
        if not users:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Админ-панель", callback_data="admin_panel"))
            bot.edit_message_text(
                "❌ Нет пользователей с балансом больше 0.2$.",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=markup
            )
            return
        
        success_count = 0
        total_amount = 0
        failed_users = []
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        for user_id, balance in users:
            for attempt in range(3):  # Пробуем 3 раза
                try:
                    # Проверяем, достаточно ли средств в казне и на CryptoPay
                    if float(balance) > treasury_balance:
                        failed_users.append((user_id, balance, "Недостаточно средств в казне"))
                        break
                    if float(balance) > usdt_balance:
                        failed_users.append((user_id, balance, "Недостаточно средств на CryptoPay"))
                        break
                    
                    # Создаём чек
                    cheque_result = crypto_api.create_check(
                        amount=str(balance),
                        asset="USDT",
                        pin_to_user_id=user_id,
                        description=f"Автоматическая выплата для пользователя {user_id}"
                    )
                    
                    if cheque_result.get("ok", False):
                        cheque = cheque_result.get("result", {})
                        cheque_link = cheque.get("bot_check_url", "")
                        
                        if cheque_link:
                            # Сохраняем чек в базе данных
                            cursor.execute('''
                                INSERT INTO checks (USER_ID, AMOUNT, CHECK_CODE, STATUS, CREATED_AT)
                                VALUES (?, ?, ?, ?, ?)
                            ''', (user_id, balance, cheque_link, 'pending', current_time))
                            conn.commit()
                            
                            # Обнуляем баланс пользователя
                            cursor.execute('UPDATE users SET BALANCE = 0 WHERE ID = ?', (user_id,))
                            conn.commit()
                            
                            # Обновляем баланс казны
                            treasury_balance -= float(balance)
                            cursor.execute('UPDATE treasury SET BALANCE = ? WHERE ID = 1', (treasury_balance,))
                            conn.commit()
                            db_module.log_treasury_operation("Автоматический вывод (массовый)", balance, treasury_balance)
                            
                            # Отправляем чек пользователю
                            markup = types.InlineKeyboardMarkup()
                            markup.add(types.InlineKeyboardButton("💳 Активировать чек", url=cheque_link))
                            markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                            try:
                                bot.send_message(
                                    user_id,
                                    f"✅ Ваш чек на сумму {balance}$ готов!\n"
                                    f"Нажмите на кнопку ниже, чтобы активировать его:",
                                    reply_markup=markup,
                                    parse_mode='HTML'
                                )
                            except Exception as e:
                                print(f"[ERROR] Не удалось отправить сообщение пользователю {user_id}: {e}")
                                failed_users.append((user_id, balance, "Ошибка отправки сообщения"))
                                break
                            
                            # Логируем успешную операцию
                            log_entry = f"[{current_time}] | Массовая выплата | Пользователь {user_id} | Сумма {balance}$ | Успех"
                            with open("withdrawals_log.txt", "a", encoding="utf-8") as log_file:
                                log_file.write(log_entry + "\n")
                            
                            success_count += 1
                            total_amount += balance
                            usdt_balance -= float(balance)  # Уменьшаем локальный баланс CryptoPay
                            break
                    else:
                        error = cheque_result.get("error", {}).get("name", "Неизвестная ошибка")
                        failed_users.append((user_id, balance, f"Ошибка CryptoPay: {error}"))
                        break
                except RequestException as e:
                    print(f"[ERROR] Попытка {attempt + 1} для пользователя {user_id}: {e}")
                    if attempt == 2:
                        failed_users.append((user_id, balance, f"Ошибка запроса: {str(e)}"))
                    continue
        
        # Формируем отчёт
        report = (
            f"✅ Отправлено чеков: {success_count}\n"
            f"💰 Общая сумма: {total_amount}$\n"
            f"💰 Баланс CryptoPay: {usdt_balance}$\n"
            f"💰 В резерве CryptoPay: {usdt_onhold}$"
        )
        if failed_users:
            report += "\n\n❌ Не удалось обработать для пользователей:\n"
            for user_id, balance, error in failed_users:
                report += f"ID: {user_id}, Сумма: {balance}$, Ошибка: {error}\n"
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Админ-панель", callback_data="admin_panel"))
        bot.edit_message_text(
            report,
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup,
            parse_mode='HTML'
        )
        
        # Уведомляем администраторов
        admin_message = (
            f"💸 <b>Массовая отправка чеков завершена</b>\n\n"
            f"✅ Успешно отправлено: {success_count} чеков\n"
            f"💰 Общая сумма: {total_amount}$\n"
            f"💰 Остаток в казне: {treasury_balance}$\n"
            f"💰 Баланс CryptoPay: {usdt_balance}$\n"
            f"💰 В резерве CryptoPay: {usdt_onhold}$\n"
        )
        if failed_users:
            admin_message += "\n❌ Ошибки:\n"
            for user_id, balance, error in failed_users:
                admin_message += f"ID: {user_id}, Сумма: {balance}$, Ошибка: {error}\n"
        
        for admin_id in config.ADMINS_ID:
            try:
                bot.send_message(admin_id, admin_message, parse_mode='HTML')
            except:
                continue








# Состояния для ввода user_id и суммы
SEND_CHECK_STATE = {}

@bot.callback_query_handler(func=lambda call: call.data == "send_check")
def send_check_start(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для выполнения этого действия!")
        return
    
    # Запрашиваем user_id
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
    msg = bot.edit_message_text(
        "Введите user_id пользователя, которому нужно отправить чек:",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=markup
    )
    
    # Сохраняем состояние
    SEND_CHECK_STATE[call.from_user.id] = {"step": "awaiting_user_id", "message_id": msg.message_id}
    bot.register_next_step_handler(msg, process_user_id_input)

def process_user_id_input(message):
    admin_id = message.from_user.id
    if admin_id not in SEND_CHECK_STATE or SEND_CHECK_STATE[admin_id]["step"] != "awaiting_user_id":
        return
    
    # Проверяем, что введён текст
    if not message.text:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
        bot.reply_to(message, "❌ Пожалуйста, введите user_id (число).", reply_markup=markup)
        bot.register_next_step_handler(message, process_user_id_input)
        return
    
    # Проверяем, что введено число
    user_id_str = message.text.strip()
    if not user_id_str.isdigit():
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
        bot.reply_to(message, "❌ User_id должен быть числом. Попробуйте снова:", reply_markup=markup)
        bot.register_next_step_handler(message, process_user_id_input)
        return
    
    user_id = int(user_id_str)
    
    # Проверяем, существует ли пользователь в базе
    with db_module.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT ID FROM users WHERE ID = ?', (user_id,))
        if not cursor.fetchone():
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
            bot.reply_to(message, f"❌ Пользователь с ID {user_id} не найден.", reply_markup=markup)
            bot.register_next_step_handler(message, process_user_id_input)
            return
    
    # Запрашиваем сумму
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
    msg = bot.reply_to(message, "Введите сумму чека в USDT:", reply_markup=markup)
    
    # Обновляем состояние
    SEND_CHECK_STATE[admin_id] = {
        "step": "awaiting_amount",
        "user_id": user_id,
        "message_id": msg.message_id
    }
    bot.register_next_step_handler(msg, process_amount_input)

def process_amount_input(message):
    admin_id = message.from_user.id
    if admin_id not in SEND_CHECK_STATE or SEND_CHECK_STATE[admin_id]["step"] != "awaiting_amount":
        return
    
    # Проверяем, что введён текст
    if not message.text:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
        bot.reply_to(message, "❌ Пожалуйста, введите сумму в USDT.", reply_markup=markup)
        bot.register_next_step_handler(message, process_amount_input)
        return
    
    # Проверяем, что введено число
    amount_str = message.text.strip()
    if not re.match(r'^\d+(\.\d{1,2})?$', amount_str):
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
        bot.reply_to(message, "❌ Сумма должна быть числом (например, 1.5). Попробуйте снова:", reply_markup=markup)
        bot.register_next_step_handler(message, process_amount_input)
        return
    
    amount = float(amount_str)
    user_id = SEND_CHECK_STATE[admin_id]["user_id"]
    
    # Проверяем минимальную сумму
    if amount < 0.1:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
        bot.reply_to(message, "❌ Минимальная сумма чека — 0.1 USDT.", reply_markup=markup)
        bot.register_next_step_handler(message, process_amount_input)
        return
    
    # Проверка баланса казны
    with db_module.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT BALANCE FROM treasury WHERE ID = 1')
        treasury_result = cursor.fetchone()
        treasury_balance = treasury_result[0] if treasury_result else 0.0
        
        if amount > treasury_balance:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Админ-панель", callback_data="admin_panel"))
            bot.reply_to(message, f"❌ Недостаточно средств в казне: {treasury_balance} USDT.", reply_markup=markup)
            return
    
        # Проверка баланса CryptoPay
        crypto_api = crypto_pay.CryptoPay()
        try:
            balance_result = crypto_api.get_balance()
            if not balance_result.get("ok", False):
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("🔙 Админ-панель", callback_data="admin_panel"))
                bot.reply_to(message, "❌ Ошибка при проверке баланса CryptoPay.", reply_markup=markup)
                return
            
            usdt_balance = next((float(item["available"]) for item in balance_result["result"] if item["currency_code"] == "USDT"), 0.0)
            usdt_onhold = next((float(item["onhold"]) for item in balance_result["result"] if item["currency_code"] == "USDT"), 0.0)
            
            print(f"[INFO] Баланс CryptoPay: доступно {usdt_balance} USDT, в резерве {usdt_onhold} USDT")
            
            if amount > usdt_balance:
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("🔙 Админ-панель", callback_data="admin_panel"))
                bot.reply_to(message, f"❌ Недостаточно средств на CryptoPay: доступно {usdt_balance} USDT, в резерве {usdt_onhold} USDT.", reply_markup=markup)
                return
        except Exception as e:
            print(f"[ERROR] Не удалось проверить баланс CryptoPay: {e}")
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Админ-панель", callback_data="admin_panel"))
            bot.reply_to(message, "❌ Ошибка при проверке баланса CryptoPay.", reply_markup=markup)
            return
        
        # Создаём чек
        try:
            cheque_result = crypto_api.create_check(
                amount=str(amount),
                asset="USDT",
                pin_to_user_id=user_id,
                description=f"Чек для пользователя {user_id} от администратора"
            )
            
            if not cheque_result.get("ok", False):
                error = cheque_result.get("error", {}).get("name", "Неизвестная ошибка")
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("🔙 Админ-панель", callback_data="admin_panel"))
                bot.reply_to(message, f"❌ Ошибка при создании чека: {error}", reply_markup=markup)
                return
            
            cheque = cheque_result.get("result", {})
            cheque_link = cheque.get("bot_check_url", "")
            
            if not cheque_link:
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("🔙 Админ-панель", callback_data="admin_panel"))
                bot.reply_to(message, "❌ Не удалось получить ссылку на чек.", reply_markup=markup)
                return
            
            # Сохраняем чек в базе данных
            current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute('''
                INSERT INTO checks (USER_ID, AMOUNT, CHECK_CODE, STATUS, CREATED_AT)
                VALUES (?, ?, ?, ?, ?)
            ''', (user_id, amount, cheque_link, 'pending', current_time))
            
            # Обновляем баланс казны
            treasury_balance -= amount
            cursor.execute('UPDATE treasury SET BALANCE = ? WHERE ID = 1', (treasury_balance,))
            conn.commit()
            db_module.log_treasury_operation("Ручной чек", amount, treasury_balance)
            
            # Логируем операцию
            log_entry = f"[{current_time}] | Ручной чек | Пользователь {user_id} | Сумма {amount}$ | Успех"
            with open("withdrawals_log.txt", "a", encoding="utf-8") as log_file:
                log_file.write(log_entry + "\n")
            
            # Отправляем чек пользователю
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("💳 Активировать чек", url=cheque_link))
            markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
            try:
                bot.send_message(
                    user_id,
                    f"✅ Ваш чек на сумму {amount}$ готов!\n"
                    f"Нажмите на кнопку ниже, чтобы активировать его:",
                    reply_markup=markup,
                    parse_mode='HTML'
                )
            except Exception as e:
                print(f"[ERROR] Не удалось отправить чек пользователю {user_id}: {e}")
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("🔙 Админ-панель", callback_data="admin_panel"))
                bot.reply_to(message, f"❌ Не удалось отправить чек пользователю {user_id}: {e}", reply_markup=markup)
                return
            
            # Уведомляем администратора
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Админ-панель", callback_data="admin_panel"))
            bot.reply_to(message, f"✅ Чек на {amount}$ успешно отправлен пользователю {user_id}.", reply_markup=markup)
            
            # Очищаем состояние
            SEND_CHECK_STATE.pop(admin_id, None)
        
        except Exception as e:
            print(f"[ERROR] Не удалось создать чек для пользователя {user_id}: {e}")
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Админ-панель", callback_data="admin_panel"))
            bot.reply_to(message, f"❌ Ошибка при создании чека: {e}", reply_markup=markup)
            return

#ДОБАВЛЕНИЕ ИД ГРУППЫ ДЛЯ ПРИНЯТИЕ НОМЕРОВ
# Обработчик для добавления группы
@bot.callback_query_handler(func=lambda call: call.data == "add_group")
def add_group(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для этого действия!")
        return
    msg = bot.send_message(call.message.chat.id, "📝 Введите ID группы (например, -1002453887941):")
    bot.register_next_step_handler(msg, process_group_id_add)

def process_group_id_add(message):
    try:
        group_id = int(message.text.strip())
        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT ID FROM groups WHERE ID = ?', (group_id,))
            if cursor.fetchone():
                bot.reply_to(message, "❌ Эта группа уже существует!")
                return
            cursor.execute('INSERT INTO groups (ID, NAME) VALUES (?, ?)', (group_id, f"Группа {group_id}"))
            conn.commit()
        bot.reply_to(message, f"✅ Группа с ID {group_id} успешно добавлена!")
    except ValueError:
        bot.reply_to(message, "❌ Неверный формат ID! Введите числовое значение.")
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка при добавлении группы: {e}")

# Обработчик для удаления группы
@bot.callback_query_handler(func=lambda call: call.data == "remove_group")
def remove_group(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для этого действия!")
        return
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT ID, NAME FROM groups')
        groups = cursor.fetchall()
    if not groups:
        bot.edit_message_text("📭 Нет групп для удаления.", call.message.chat.id, call.message.message_id, parse_mode='HTML')
        return
    markup = types.InlineKeyboardMarkup()
    for group_id, group_name in groups:
        markup.add(types.InlineKeyboardButton(f"➖ {group_name} (ID: {group_id})", callback_data=f"confirm_remove_{group_id}"))
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
    bot.edit_message_text("<b>➖ Выберите группу для удаления:</b>", call.message.chat.id, call.message.message_id, parse_mode='HTML', reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("confirm_remove_"))
def confirm_remove_group(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для этого действия!")
        return
    group_id = int(call.data.split("_")[2])
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT NAME FROM groups WHERE ID = ?', (group_id,))
        group = cursor.fetchone()
        if not group:
            bot.answer_callback_query(call.id, "❌ Группа не найдена!")
            return
        group_name = group[0]
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("✅ Подтвердить удаление", callback_data=f"remove_confirmed_{group_id}"))
        markup.add(types.InlineKeyboardButton("❌ Отмена", callback_data="remove_group"))
        bot.edit_message_text(f"<b>Подтвердите удаление группы:</b>\n🏠 {group_name} (ID: {group_id})", call.message.chat.id, call.message.message_id, parse_mode='HTML', reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("remove_confirmed_"))
def remove_confirmed_group(call):
    if call.from_user.id not in config.ADMINS_ID:
        bot.answer_callback_query(call.id, "❌ У вас нет прав для этого действия!")
        return
    group_id = int(call.data.split("_")[2])
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM groups WHERE ID = ?', (group_id,))
        conn.commit()
    bot.edit_message_text(f"✅ Группа с ID {group_id} успешно удалена!", call.message.chat.id, call.message.message_id, parse_mode='HTML')
    bot.answer_callback_query(call.id, "Группа удалена!")





#=============================================================================================================

#НОМЕРА КОТОРЫЕ НЕ ОБРАБАТЫВАЛИ В ТЕЧЕНИЕ 10 МИНУТ +
def check_number_timeout():
    """Проверяет, истекло ли время ожидания кода (10 минут)."""
    while True:
        try:
            with db.get_db() as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT NUMBER, TAKE_DATE, ID_OWNER, MODERATOR_ID, STATUS FROM numbers')
                numbers = cursor.fetchall()
                
                current_time = datetime.now()
                for number, take_date, owner_id, moderator_id, status in numbers:
                    if take_date in ("0", "1") or status not in ("на проверке", "taken"):
                        continue
                    try:
                        take_time = datetime.strptime(take_date, "%Y-%m-%d %H:%M:%S")
                        elapsed_time = (current_time - take_time).total_seconds() / 60
                        # Проверяем, не был ли номер автоматически подтверждён
                        cursor.execute('SELECT CONFIRMED_BY_MODERATOR_ID FROM numbers WHERE NUMBER = ?', (number,))
                        confirmed_by = cursor.fetchone()[0]
                        if elapsed_time >= 10 and confirmed_by is not None:
                            # Номер возвращается в очередь только если не был автоматически подтверждён
                            cursor.execute('UPDATE numbers SET MODERATOR_ID = NULL, TAKE_DATE = "0", STATUS = "ожидает" WHERE NUMBER = ?', (number,))
                            conn.commit()
                            logging.info(f"Номер {number} возвращён в очередь из-за бездействия модератора.")
                            
                            if owner_id:
                                markup_owner = types.InlineKeyboardMarkup()
                                markup_owner.add(types.InlineKeyboardButton("📱 Сдать номер", callback_data="submit_number"))
                                markup_owner.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                                safe_send_message(
                                    owner_id,
                                    f"📱 Ваш номер {number} возвращён в очередь из-за бездействия модератора.",
                                    parse_mode='HTML',
                                    reply_markup=markup_owner
                                )
                            
                            if moderator_id:
                                markup_mod = types.InlineKeyboardMarkup()
                                markup_mod.add(types.InlineKeyboardButton("📲 Получить новый номер", callback_data="get_number"))
                                markup_mod.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                                safe_send_message(
                                    moderator_id,
                                    f"📱 Номер {number} возвращён в очередь из-за бездействия.",
                                    parse_mode='HTML',
                                    reply_markup=markup_mod
                                )
                    except ValueError as e:
                        logging.error(f"Неверный формат времени для номера {number}: {e}")
            time.sleep(60)  # Проверяем каждую минуту
        except Exception as e:
            logging.error(f"Ошибка в check_number_timeout: {e}")
            time.sleep(60)
# Запускаем фоновую задачу



def check_number_hold_time():
    while True:
        try:
            with db.get_db() as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT HOLD_TIME FROM settings')
                result = cursor.fetchone()
                hold_time = result[0] if result else 5

                cursor.execute('''
                    SELECT NUMBER, ID_OWNER, TAKE_DATE, STATUS, CONFIRMED_BY_MODERATOR_ID
                    FROM numbers 
                    WHERE STATUS = 'активен' AND TAKE_DATE NOT IN ('0', '1')
                ''')
                numbers = cursor.fetchall()

                current_time = datetime.now()
                for number, owner_id, take_date, status, mod_id in numbers:
                    try:
                        start_time = datetime.strptime(take_date, "%Y-%m-%d %H:%M:%S")
                        time_elapsed = (current_time - start_time).total_seconds() / 60
                        if time_elapsed < hold_time:
                            logging.debug(f"Номер {number} ещё не отстоял: {time_elapsed:.2f}/{hold_time} минут")
                            continue

                        # Проверяем текущий статус номера
                        cursor.execute('SELECT STATUS FROM numbers WHERE NUMBER = ?', (number,))
                        current_status = cursor.fetchone()[0]
                        if current_status != 'активен':
                            logging.info(f"Номер {number} пропущен: статус изменился на {current_status}")
                            continue

                        # Получаем индивидуальную цену пользователя
                        price = db_module.get_user_price(owner_id)

                        # Устанавливаем SHUTDOWN_DATE как текущее время
                        shutdown_date = current_time.strftime("%Y-%m-%d %H:%M:%S")
                        cursor.execute('''
                            UPDATE numbers 
                            SET STATUS = 'отстоял', 
                                SHUTDOWN_DATE = ? 
                            WHERE NUMBER = ?
                        ''', (shutdown_date, number))
                        # Начисляем оплату
                        cursor.execute('UPDATE users SET BALANCE = BALANCE + ? WHERE ID = ?', (price, owner_id))
                        conn.commit()
                        logging.info(f"Номер {number} отстоял. SHUTDOWN_DATE: {shutdown_date}, начислено {price}$ пользователю {owner_id}")

                        # Уведомляем владельца
                        markup = types.InlineKeyboardMarkup()
                        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                        safe_send_message(
                            owner_id,
                            f"✅ Номер {number} успешно отстоял!\n"
                            f"🟢 Встал: {take_date}\n"
                            f"🟢 Отстоял: {shutdown_date}\n"
                            f"💰 Начислено: {price}$",
                            parse_mode='HTML',
                            reply_markup=markup
                        )

                        # Уведомляем модератора, если он есть
                        if mod_id:
                            safe_send_message(
                                mod_id,
                                f"✅ Номер {number} отстоял.\n"
                                f"🟢 Встал: {take_date}\n"
                                f"🟢 Отстоял: {shutdown_date}",
                                parse_mode='HTML'
                            )

                    except ValueError as e:
                        logging.error(f"Неверный формат времени для номера {number}: {e}")
                    except Exception as e:
                        logging.error(f"Ошибка при обработке номера {number}: {e}")

        except Exception as e:
            logging.error(f"Ошибка в check_number_hold_time: {e}")
        
        time.sleep(60)  # Проверяем каждую минуту

# МОДЕРАЦИЯ НОМЕРОВ:


#Обработчики для получеяяния номеров

@bot.callback_query_handler(func=lambda call: call.data == "get_number")
def get_number(call):
    user_id = call.from_user.id
    if not db_module.is_moderator(user_id):
        bot.answer_callback_query(call.id, "❌ У вас нет прав для получения номера!")
        return
    
    number = db_module.get_available_number(user_id)
    
    if number:
        with db_module.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('UPDATE numbers SET TAKE_DATE = ?, MODERATOR_ID = ?, GROUP_CHAT_ID = ? WHERE NUMBER = ?',
                          (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), user_id, call.message.chat.id, number))
            conn.commit()
        
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("✉️ Отправить код", callback_data=f"send_code_{number}_{call.message.chat.id}"),
            types.InlineKeyboardButton("❌ Номер невалидный", callback_data=f"invalid_{number}")
        )
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        
        bot.edit_message_text(
            f"📱 Новый номер для проверки: <code>{number}</code>\n"
            "Ожидайте код от владельца или отметьте номер как невалидный.",
            call.message.chat.id,
            call.message.message_id,
            parse_mode='HTML',
            reply_markup=markup
        )
    else:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        bot.edit_message_text(
            "📭 Нет доступных номеров для проверки.",
            call.message.chat.id,
            call.message.message_id,
            parse_mode='HTML',
            reply_markup=markup
        )


def get_number_in_group(user_id, chat_id, message_id, tg_number):
    if not db_module.is_moderator(user_id):
        bot.send_message(chat_id, "❌ У вас нет прав для получения номера!")
        return
    
    number = db_module.get_available_number(user_id)
    
    if number:
        with db_module.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('UPDATE numbers SET TAKE_DATE = ?, MODERATOR_ID = ?, GROUP_CHAT_ID = ?, TG_NUMBER = ? WHERE NUMBER = ?',
                          (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), user_id, chat_id, tg_number, number))
            conn.commit()
        
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("✉️ Отправить код", callback_data=f"send_code_{number}_{chat_id}_{tg_number}"),
            types.InlineKeyboardButton("❌ Номер невалидный", callback_data=f"invalid_{number}")
        )
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        
        bot.send_message(
            chat_id,
            f"📱 <b>ТГ {tg_number}</b>\n"
            f"📱 Новый номер для проверки: <code>{number}</code>\n"
            "Ожидайте код от владельца или отметьте номер как невалидный.",
            parse_mode='HTML',
            reply_markup=markup,
            reply_to_message_id=message_id
        )
    else:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        bot.send_message(
            chat_id,
            f"📭 Нет доступных номеров для проверки (ТГ {tg_number}).",
            parse_mode='HTML',
            reply_markup=markup,
            reply_to_message_id=message_id
        )


#Обработчики для отправки и подтверждения кодов

@bot.callback_query_handler(func=lambda call: call.data.startswith("send_code_"))
def send_verification_code(call):
    try:
        parts = call.data.split("_")
        if len(parts) < 4:
            bot.answer_callback_query(call.id, "❌ Неверный формат данных!")
            return
        number = parts[2]
        group_chat_id = int(parts[3])
        tg_number = int(parts[4]) if len(parts) > 4 else 1
        
        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT ID_OWNER FROM numbers WHERE NUMBER = ?', (number,))
            owner = cursor.fetchone()
        
        if owner:
            owner_id = owner[0]
            try:
                markup = types.InlineKeyboardMarkup()
                markup.add(
                    types.InlineKeyboardButton("❌ Не валид", callback_data=f"mark_invalid_{number}_{group_chat_id}_{tg_number}")
                )
                msg = bot.send_message(
                    owner_id,
                    "=================\n"
                    f"📱 Введите код для номера {number}, который будет отправлен модератору: (Используйте ответить на сообщение)",
                    reply_markup=markup
                )
                markup = types.InlineKeyboardMarkup()
                markup.add(
                    types.InlineKeyboardButton("❌ Не валид", callback_data=f"moderator_invalid_{number}_{tg_number}_{owner_id}")
                )
                bot.edit_message_text(
                    f"📱 <b>ТГ {tg_number}</b>\n"
                    f"📱 Номер: {number}\n✉️ Запрос кода отправлен владельцу.",
                    call.message.chat.id,
                    call.message.message_id,
                    parse_mode='HTML',
                    reply_markup=markup
                )
                
                with db.get_db() as conn:
                    cursor = conn.cursor()
                    cursor.execute('UPDATE numbers SET VERIFICATION_CODE = "", TG_NUMBER = ? WHERE NUMBER = ?', (tg_number, number))
                    cursor.execute('UPDATE numbers SET MODERATOR_ID = ?, GROUP_CHAT_ID = ? WHERE NUMBER = ?', 
                                  (call.from_user.id, group_chat_id, number))
                    conn.commit()
                
                # Регистрируем запрос кода
                if owner_id not in active_code_requests:
                    active_code_requests[owner_id] = {}
                active_code_requests[owner_id][number] = msg.message_id
                
                bot.register_next_step_handler(
                    msg,
                    process_verification_code_input,
                    number,
                    call.from_user.id,
                    group_chat_id,
                    msg.chat.id,
                    msg.message_id,
                    tg_number
                )
            except telebot.apihelper.ApiTelegramException as e:
                if e.error_code == 403 and "user is deactivated" in e.description:
                    bot.answer_callback_query(call.id, "❌ Пользователь деактивирован, невозможно отправить сообщение!")
                    with db.get_db() as conn:
                        cursor = conn.cursor()
                        cursor.execute('DELETE FROM numbers WHERE NUMBER = ?', (number,))
                        conn.commit()
                    # Удаляем запрос из active_code_requests
                    if owner_id in active_code_requests and number in active_code_requests[owner_id]:
                        del active_code_requests[owner_id][number]
                        if not active_code_requests[owner_id]:
                            del active_code_requests[owner_id]
                else:
                    raise e
        else:
            bot.answer_callback_query(call.id, "❌ Владелец номера не найден!")
    
    except Exception as e:
        print(f"Ошибка в send_verification_code: {e}")
        bot.answer_callback_query(call.id, "❌ Произошла ошибка!")

@bot.callback_query_handler(func=lambda call: call.data.startswith("mark_invalid_"))
def mark_number_invalid(call):
    try:
        # Разбираем callback_data
        parts = call.data.split("_")
        if len(parts) < 4:
            bot.answer_callback_query(call.id, "❌ Неверный формат данных!")
            return
        number = parts[2]
        group_chat_id = int(parts[3])
        tg_number = int(parts[4])

        # Проверяем, существует ли номер в базе и является ли пользователь владельцем
        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT ID_OWNER, MODERATOR_ID FROM numbers WHERE NUMBER = ?', (number,))
            result = cursor.fetchone()
            if not result:
                bot.answer_callback_query(call.id, "❌ Номер не найден!")
                return
            owner_id, moderator_id = result

            # Проверяем, что вызывающий пользователь является владельцем номера
            if call.from_user.id != owner_id:
                bot.answer_callback_query(call.id, "❌ У вас нет прав для пометки этого номера как невалидного!")
                return

            # Удаляем номер из базы
            try:
                cursor.execute('DELETE FROM numbers WHERE NUMBER = ?', (number,))
                conn.commit()
                print(f"[DEBUG] Номер {number} удалён из базы данных")
            except Exception as e:
                print(f"[ERROR] Ошибка при удалении номера {number} из базы: {e}")
                raise e

        # Формируем confirmation_key
        confirmation_key = f"{number}_{owner_id}"
        if confirmation_key in confirmation_messages:
            try:
                bot.delete_message(
                    confirmation_messages[confirmation_key]["chat_id"],
                    confirmation_messages[confirmation_key]["message_id"]
                )
            except Exception as e:
                print(f"[ERROR] Ошибка при удалении сообщения подтверждения {confirmation_key}: {e}")
            del confirmation_messages[confirmation_key]
            print(f"[DEBUG] Удалён confirmation_key {confirmation_key} из confirmation_messages")

        # Очищаем active_code_requests и уведомляем владельца, если есть активный запрос
        if owner_id in active_code_requests and number in active_code_requests[owner_id]:
            message_id = active_code_requests[owner_id][number]
            try:
                bot.edit_message_text(
                    f"❌ Запрос кода для номера {number} отменён, так как номер помечен как невалидный.",
                    owner_id,
                    message_id,
                    parse_mode='HTML'
                )
            except telebot.apihelper.ApiTelegramException as e:
                print(f"[ERROR] Не удалось обновить сообщение для owner_id {owner_id}, message_id {message_id}: {e}")
            del active_code_requests[owner_id][number]
            print(f"[DEBUG] Удалён номер {number} из active_code_requests для owner_id {owner_id}")
            if not active_code_requests[owner_id]:
                del active_code_requests[owner_id]
                print(f"[DEBUG] Удалён owner_id {owner_id} из active_code_requests")

        # Уведомляем владельца
        markup_owner = types.InlineKeyboardMarkup()
        markup_owner.add(
            types.InlineKeyboardButton("📱 Сдать номер снова", callback_data="submit_number"),
            types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main")
        )
        bot.edit_message_text(
            f"❌ Вы отметили номер {number} как невалидный. Номер удалён из системы.",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup_owner,
            parse_mode='HTML'
        )

        # Уведомляем модератора в группе
        markup_mod = types.InlineKeyboardMarkup()
        markup_mod.add(
            types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main")
        )
        try:
            bot.send_message(
                group_chat_id,
                f"📱 <b>ТГ {tg_number}</b>\n"
                f"❌ Владелец номера {number} отметил его как невалидный. \n Приносим свои извинения пожалуйста возьмите новый номер",
                reply_markup=markup_mod,
                parse_mode='HTML'
            )
        except telebot.apihelper.ApiTelegramException as e:
            print(f"[ERROR] Не удалось отправить сообщение в группу {group_chat_id}: {e}")
            if moderator_id:
                try:
                    bot.send_message(
                        moderator_id,
                        f"📱 <b>ТГ {tg_number}</b>\n"
                        f"❌ Владелец номера {number} отметил его как невалидный. Номер удалён из системы.\n"
                        f"⚠️ Не удалось отправить сообщение в группу (ID: {group_chat_id}).",
                        reply_markup=markup_mod,
                        parse_mode='HTML'
                    )
                except telebot.apihelper.ApiTelegramException as e:
                    print(f"[ERROR] Не удалось отправить сообщение модератору {moderator_id}: {e}")

        bot.answer_callback_query(call.id, "✅ Номер отмечен как невалидный.")
    except Exception as e:
        print(f"[ERROR] Ошибка в mark_number_invalid: {e}")
        bot.answer_callback_query(call.id, "❌ Произошла ошибка при обработке номера!")

@bot.callback_query_handler(func=lambda call: call.data.startswith("moderator_invalid_"))
def moderator_mark_number_invalid(call):
    try:
        parts = call.data.split("_")
        if len(parts) < 5:
            bot.answer_callback_query(call.id, "❌ Неверный формат данных!")
            return
        number = parts[2]
        tg_number = int(parts[3])
        owner_id = int(parts[4])

        # Проверяем, является ли пользователь модератором
        if not db.is_moderator(call.from_user.id) and call.from_user.id not in config.ADMINS_ID:
            bot.answer_callback_query(call.id, "❌ У вас нет прав для выполнения этого действия!")
            return

        # Проверяем, существует ли номер в базе
        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT ID_OWNER FROM numbers WHERE NUMBER = ?', (number,))
            result = cursor.fetchone()
            if not result:
                bot.answer_callback_query(call.id, "❌ Номер не найден!")
                return
            if result[0] != owner_id:
                bot.answer_callback_query(call.id, "❌ Неверный ID владельца!")
                return

            # Удаляем номер из базы
            cursor.execute('DELETE FROM numbers WHERE NUMBER = ?', (number,))
            conn.commit()

        bot.edit_message_text(
            f"✅ Номер {number} успешно удален из системы",
            call.message.chat.id,
            call.message.message_id,
            parse_mode='HTML'
        )

        # Уведомляем владельца
        markup_owner = types.InlineKeyboardMarkup()
        markup_owner.add(
            types.InlineKeyboardButton("📱 Сдать номер снова", callback_data="submit_number"),
            types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main")
        )
        try:
            bot.send_message(
                owner_id,
                f"❌ Ваш номер {number} был отклонен модератором.\n📱 Проверьте номер и сдайте заново.",
                reply_markup=markup_owner,
                parse_mode='HTML'
            )
        except telebot.apihelper.ApiTelegramException as e:
            print(f"Не удалось отправить сообщение владельцу {owner_id}: {e}")
            for admin_id in config.ADMINS_ID:
                try:
                    bot.send_message(
                        admin_id,
                        f"⚠️ Не удалось уведомить владельца {owner_id} об отклонении номера {number}: {e}",
                        parse_mode='HTML'
                    )
                except:
                    pass

        # Очищаем confirmation_messages
        confirmation_key = f"{number}_{owner_id}"
        if confirmation_key in confirmation_messages:
            del confirmation_messages[confirmation_key]

        # Очищаем active_code_requests
        if owner_id in active_code_requests and number in active_code_requests[owner_id]:
            del active_code_requests[owner_id][number]
            if not active_code_requests[owner_id]:
                del active_code_requests[owner_id]

        bot.answer_callback_query(call.id, "✅ Номер успешно удалён.")
    except Exception as e:
        print(f"Ошибка в moderator_mark_number_invalid: {e}")
        bot.answer_callback_query(call.id, "❌ Произошла ошибка при обработке номера!")
        
import uuid
import re
from datetime import datetime
from telebot import types

# Словари для хранения контекста
confirmation_messages = {}
button_contexts = {}
code_messages = {}
active_code_requests = {}

def process_verification_code_input(message, number, moderator_id, group_chat_id, original_chat_id, original_message_id, tg_number):
    try:
        user_id = message.from_user.id

        # Проверяем, существует ли номер в базе
        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT ID_OWNER FROM numbers WHERE NUMBER = ?', (number,))
            result = cursor.fetchone()
            if not result:
                print(f"[DEBUG] Номер {number} не найден в базе, запрос кода отменён")
                try:
                    bot.delete_message(original_chat_id, original_message_id)
                except Exception as e:
                    print(f"[ERROR] Ошибка при удалении сообщения {original_message_id}: {e}")
                markup = types.InlineKeyboardMarkup()
                markup.add(
                    types.InlineKeyboardButton("📱 Сдать номер снова", callback_data="submit_number"),
                    types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main")
                )
                bot.send_message(
                    message.chat.id,
                    f"❌ Номер {number} был помечен как невалидный или удалён. Запрос кода отменён.",
                    reply_markup=markup,
                    parse_mode='HTML'
                )
                # Очищаем active_code_requests
                if user_id in active_code_requests and number in active_code_requests[user_id]:
                    del active_code_requests[user_id][number]
                    if not active_code_requests[user_id]:
                        del active_code_requests[user_id]
                return

        # Проверяем, что сообщение является ответом на запрос кода
        if not message.reply_to_message or \
           message.reply_to_message.chat.id != original_chat_id or \
           message.reply_to_message.message_id != original_message_id:
            try:
                bot.delete_message(original_chat_id, original_message_id)
            except Exception as e:
                print(f"[ERROR] Ошибка при удалении сообщения {original_message_id}: {e}")
            markup = types.InlineKeyboardMarkup()
            invalid_key = str(uuid.uuid4())[:8]
            button_contexts[invalid_key] = {
                "action": "mark_invalid",
                "number": number,
                "group_chat_id": group_chat_id,
                "tg_number": tg_number,
                "user_id": user_id
            }
            markup.add(
                types.InlineKeyboardButton("❌ Не валид", callback_data=f"btn_{invalid_key}")
            )
            msg = bot.send_message(
                message.chat.id,
                "=================\n"
                f"📱 Введите 5-значный код для номера {number} (например, 12345): (Ответьте на это сообщение)",
                reply_markup=markup
            )
            print(f"[DEBUG] Запрос кода для номера {number}, user_chat_id={msg.chat.id}, user_message_id={msg.message_id}, group_chat_id={group_chat_id}, expected_group_message_id={original_message_id}")
            bot.register_next_step_handler(
                msg,
                process_verification_code_input,
                number,
                moderator_id,
                group_chat_id,
                msg.chat.id,
                msg.message_id,
                tg_number
            )
            return

        user_input = message.text.strip()
        print(f"[DEBUG] Пользователь {user_id} ввёл код: {user_input} для номера {number}")

        # Проверяем, что код — это ровно 5 цифр
        if not re.match(r'^\d{5}$', user_input):
            print(f"[DEBUG] Код не соответствует формату: {user_input}")
            try:
                bot.delete_message(original_chat_id, original_message_id)
            except Exception as e:
                print(f"[ERROR] Ошибка при удалении сообщения {original_message_id}: {e}")
            with db.get_db() as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT ID_OWNER FROM numbers WHERE NUMBER = ?', (number,))
                if not cursor.fetchone():
                    print(f"[DEBUG] Номер {number} не найден в базе при повторном запросе кода")
                    markup = types.InlineKeyboardMarkup()
                    markup.add(
                        types.InlineKeyboardButton("📱 Сдать номер снова", callback_data="submit_number"),
                        types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main")
                    )
                    bot.send_message(
                        message.chat.id,
                        f"❌ Номер {number} был помечен как невалидный или удалён. Запрос кода отменён.",
                        reply_markup=markup,
                        parse_mode='HTML'
                    )
                    if user_id in active_code_requests and number in active_code_requests[user_id]:
                        del active_code_requests[user_id][number]
                        if not active_code_requests[user_id]:
                            del active_code_requests[user_id]
                    return

            markup = types.InlineKeyboardMarkup()
            invalid_key = str(uuid.uuid4())[:8]
            button_contexts[invalid_key] = {
                "action": "mark_invalid",
                "number": number,
                "group_chat_id": group_chat_id,
                "tg_number": tg_number,
                "user_id": user_id
            }
            markup.add(
                types.InlineKeyboardButton("❌ Не валид", callback_data=f"btn_{invalid_key}")
            )
            msg = bot.send_message(
                message.chat.id,
                "=================\n"
                f"📱 Введите новый код для номера {number}, который будет отправлен модератору: (Используйте ответить на сообщение)",
                reply_markup=markup
            )
            print(f"[DEBUG] Повторный запрос кода для номера {number}, user_chat_id={msg.chat.id}, user_message_id={msg.message_id}, group_chat_id={group_chat_id}, expected_group_message_id={original_message_id}")
            bot.register_next_step_handler(
                msg,
                process_verification_code_input,
                number,
                moderator_id,
                group_chat_id,
                msg.chat.id,
                msg.message_id,
                tg_number
            )
            return

        # Сохраняем код и обновляем статус в базе
        with db.get_db() as conn:
            cursor = conn.cursor()
            current_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"[DEBUG] Сохранение кода {user_input} для номера {number} с датой {current_date}")
            cursor.execute(
                '''
                UPDATE numbers 
                SET VERIFICATION_CODE = ?, TAKE_DATE = ?, STATUS = 'на проверке' 
                WHERE NUMBER = ?
                ''',
                (user_input, current_date, number)
            )
            conn.commit()
            print(f"[DEBUG] Статус номера {number} обновлён на 'на проверке'")

        # Добавляем номер в code_messages для отслеживания таймаута
        print(f"[DEBUG] Сохранение в code_messages: number={number}, group_chat_id={group_chat_id}, group_message_id={original_message_id}")
        code_messages[number] = {
            "timestamp": datetime.now(),
            "chat_id": group_chat_id,
            "message_id": original_message_id,
            "tg_number": tg_number
        }
        print(f"[DEBUG] Номер {number} добавлен в code_messages для отслеживания таймаута")

        # Отправляем подтверждение владельцу
        markup = types.InlineKeyboardMarkup()
        print(f"[DEBUG] Отправка сообщения подтверждения пользователю {user_id}")
        confirmation_msg = bot.send_message(
            message.chat.id,
            f"Вы ввели код: {user_input} для номера {number}\nЭто правильный код?",
            reply_markup=markup
        )

        # Сохраняем данные о сообщении подтверждения
        confirmation_key = f"{number}_{user_id}"
        confirmation_messages[confirmation_key] = {
            "chat_id": message.chat.id,
            "message_id": confirmation_msg.message_id
        }

        # Создаём уникальные ключи для кнопок
        confirm_key = str(uuid.uuid4())[:8]
        change_key = str(uuid.uuid4())[:8]
        invalid_key = str(uuid.uuid4())[:8]

        # Сохраняем контекст кнопок
        button_contexts[confirm_key] = {
            "action": "confirm_code",
            "number": number,
            "user_input": user_input,
            "group_chat_id": group_chat_id,
            "tg_number": tg_number,
            "user_id": user_id
        }
        button_contexts[change_key] = {
            "action": "change_code",
            "number": number,
            "group_chat_id": group_chat_id,
            "tg_number": tg_number,
            "user_id": user_id
        }
        button_contexts[invalid_key] = {
            "action": "mark_invalid_confirmation",
            "number": number,
            "group_chat_id": group_chat_id,
            "tg_number": tg_number,
            "user_id": user_id
        }

        # Добавляем кнопки с короткими callback_data
        markup.add(
            types.InlineKeyboardButton(
                "✅ Да, код верный",
                callback_data=f"confirm_code_{number}_{user_input}_{group_chat_id}_{tg_number}"
            ),
            types.InlineKeyboardButton(
                "❌ Нет, изменить",
                callback_data=f"btn_{change_key}"
            )
        )
        markup.add(
            types.InlineKeyboardButton(
                "❌ Не валид",
                callback_data=f"btn_{invalid_key}"
            )
        )
        print(f"[DEBUG] Обновление сообщения с ID {confirmation_msg.message_id} с кнопками")
        bot.edit_message_reply_markup(
            message.chat.id,
            confirmation_msg.message_id,
            reply_markup=markup
        )

        # Удаляем только обработанный запрос из active_code_requests
        print(f"[DEBUG] Удаление запроса из active_code_requests для пользователя {user_id}, номер {number}")
        if user_id in active_code_requests and number in active_code_requests[user_id]:
            del active_code_requests[user_id][number]
            if not active_code_requests[user_id]:
                del active_code_requests[user_id]

    except Exception as e:
        print(f"[ERROR] Ошибка в process_verification_code_input: {e}")
        bot.send_message(
            message.chat.id,
            f"📱 Произошла ошибка при обработке кода для номера {number}. Попробуйте снова.",
            parse_mode='HTML'
        )

@bot.callback_query_handler(func=lambda call: call.data.startswith('btn_'))
def handle_button_callback(call):
    try:
        # Извлекаем ключ из callback_data
        key = call.data[len('btn_'):]
        if key not in button_contexts:
            bot.answer_callback_query(call.id, "Ошибка: действие недоступно.")
            return

        context = button_contexts[key]
        action = context["action"]
        number = context["number"]
        group_chat_id = context["group_chat_id"]
        tg_number = context["tg_number"]
        user_id = context["user_id"]

        if action == "confirm_code":
            user_input = context["user_input"]
            bot.answer_callback_query(call.id, "Код подтверждён.")
            # Добавьте свою логику для подтверждения кода
            del button_contexts[key]

        elif action == "change_code":
            markup = types.InlineKeyboardMarkup()
            invalid_key = str(uuid.uuid4())[:8]
            button_contexts[invalid_key] = {
                "action": "mark_invalid",
                "number": number,
                "group_chat_id": group_chat_id,
                "tg_number": tg_number,
                "user_id": user_id
            }
            markup.add(
                types.InlineKeyboardButton("❌ Не валид", callback_data=f"btn_{invalid_key}")
            )
            msg = bot.send_message(
                call.message.chat.id,
                f"📱 Введите новый код для номера {number}: (Ответьте на это сообщение)",
                reply_markup=markup
            )
            print(f"[DEBUG] Запрос нового кода для номера {number}, user_chat_id={msg.chat.id}, user_message_id={msg.message_id}, group_chat_id={group_chat_id}")
            bot.register_next_step_handler(
                msg,
                process_verification_code_input,
                number,
                context.get("moderator_id", None),
                group_chat_id,
                msg.chat.id,
                msg.message_id,
                tg_number
            )
            bot.answer_callback_query(call.id, "Введите новый код.")
            del button_contexts[key]

        elif action == "mark_invalid_confirmation":
            # Удаляем номер из базы
            with db.get_db() as conn:
                cursor = conn.cursor()
                cursor.execute('DELETE FROM numbers WHERE NUMBER = ?', (number,))
                conn.commit()
                print(f"[DEBUG] Номер {number} удалён из базы")

            # Редактируем сообщение модератора
            if number in code_messages:
                print(f"[DEBUG] Попытка редактирования сообщения модератора: chat_id={code_messages[number]['chat_id']}, message_id={code_messages[number]['message_id']}")
                try:
                    bot.edit_message_text(
                        chat_id=code_messages[number]["chat_id"],
                        message_id=code_messages[number]["message_id"],
                        text=(
                            f"📱 ТГ {tg_number}\n"
                            f"❌ Владелец номера {number} отметил его как невалидный.\n"
                            f"Приносим свои извинения, пожалуйста, возьмите новый номер"
                        ),
                        parse_mode='HTML'
                    )
                    print(f"[DEBUG] Сообщение модератора в чате {code_messages[number]['chat_id']} отредактировано")
                except Exception as e:
                    print(f"[ERROR] Ошибка при редактировании сообщения модератора для номера {number}: {e}")
                    # Fallback: отправляем новое сообщение, если редактирование не удалось
                    try:
                        new_msg = bot.send_message(
                            chat_id=code_messages[number]["chat_id"],
                            text=(
                                f"📱 ТГ {tg_number}\n"
                                f"❌ Владелец номера {number} отметил его как невалидный.\n"
                                f"Приносим свои извинения, пожалуйста, возьмите новый номер"
                            ),
                            parse_mode='HTML'
                        )
                        print(f"[DEBUG] Отправлено новое сообщение модератору в чате {code_messages[number]['chat_id']}, message_id={new_msg.message_id}")
                    except Exception as e:
                        print(f"[ERROR] Ошибка при отправке нового сообщения модератору для номера {number}: {e}")
            else:
                print(f"[ERROR] Номер {number} не найден в code_messages")
                # Отправляем новое сообщение, если нет данных в code_messages
                try:
                    new_msg = bot.send_message(
                        chat_id=group_chat_id,
                        text=(
                            f"📱 ТГ {tg_number}\n"
                            f"❌ Владелец номера {number} отметил его как невалидный.\n"
                            f"Приносим свои извинения, пожалуйста, возьмите новый номер"
                        ),
                        parse_mode='HTML'
                    )
                    print(f"[DEBUG] Отправлено новое сообщение модератору в чате {group_chat_id}, message_id={new_msg.message_id}")
                except Exception as e:
                    print(f"[ERROR] Ошибка при отправке нового сообщения модератору для номера {number}: {e}")

            # Создаём меню для пользователя
            markup = types.InlineKeyboardMarkup()
            markup.add(
                types.InlineKeyboardButton("📱 Сдать номер снова", callback_data="submit_number"),
                types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main")
            )

            # Обновляем сообщение подтверждения для пользователя
            confirmation_key = f"{number}_{user_id}"
            if confirmation_key in confirmation_messages:
                try:
                    bot.edit_message_text(
                        chat_id=confirmation_messages[confirmation_key]["chat_id"],
                        message_id=confirmation_messages[confirmation_key]["message_id"],
                        text=f"❌ Вы отметили номер {number} как невалидный. Номер удалён из системы.",
                        parse_mode='HTML',
                        reply_markup=markup
                    )
                    print(f"[DEBUG] Сообщение для пользователя {user_id} обновлено")
                except Exception as e:
                    print(f"[ERROR] Ошибка при обновлении сообщения пользователя для номера {number}: {e}")
                del confirmation_messages[confirmation_key]

            # Удаляем номер из code_messages
            if number in code_messages:
                del code_messages[number]
                print(f"[DEBUG] Номер {number} удалён из code_messages")

            bot.answer_callback_query(call.id, "Номер помечен как невалидный.")
            del button_contexts[key]

        elif action == "mark_invalid":
            # Логика для кнопки "Не валид" в запросе кода
            with db.get_db() as conn:
                cursor = conn.cursor()
                cursor.execute('DELETE FROM numbers WHERE NUMBER = ?', (number,))
                conn.commit()
                print(f"[DEBUG] Номер {number} удалён из базы")

            # Редактируем сообщение модератора
            if number in code_messages:
                print(f"[DEBUG] Попытка редактирования сообщения модератора: chat_id={code_messages[number]['chat_id']}, message_id={code_messages[number]['message_id']}")
                try:
                    bot.edit_message_text(
                        chat_id=code_messages[number]["chat_id"],
                        message_id=code_messages[number]["message_id"],
                        text=(
                            f"📱 ТГ {tg_number}\n"
                            f"❌ Владелец номера {number} отметил его как невалидный.\n"
                            f"Приносим свои извинения, пожалуйста, возьмите новый номер"
                        ),
                        parse_mode='HTML'
                    )
                    print(f"[DEBUG] Сообщение модератора в чате {code_messages[number]['chat_id']} отредактировано")
                except Exception as e:
                    print(f"[ERROR] Ошибка при редактировании сообщения модератора для номера {number}: {e}")
                    # Fallback: отправляем новое сообщение
                    try:
                        new_msg = bot.send_message(
                            chat_id=code_messages[number]["chat_id"],
                            text=(
                                f"📱 ТГ {tg_number}\n"
                                f"❌ Владелец номера {number} отметил его как невалидный.\n"
                                f"Приносим свои извинения, пожалуйста, возьмите новый номер"
                            ),
                            parse_mode='HTML'
                        )
                        print(f"[DEBUG] Отправлено новое сообщение модератору в чате {code_messages[number]['chat_id']}, message_id={new_msg.message_id}")
                    except Exception as e:
                        print(f"[ERROR] Ошибка при отправке нового сообщения модератору для номера {number}: {e}")
            else:
                print(f"[ERROR] Номер {number} не найден в code_messages")
                # Отправляем новое сообщение
                try:
                    new_msg = bot.send_message(
                        chat_id=group_chat_id,
                        text=(
                            f"📱 ТГ {tg_number}\n"
                            f"❌ Владелец номера {number} отметил его как невалидный.\n"
                            f"Приносим свои извинения, пожалуйста, возьмите новый номер"
                        ),
                        parse_mode='HTML'
                    )
                    print(f"[DEBUG] Отправлено новое сообщение модератору в чате {group_chat_id}, message_id={new_msg.message_id}")
                except Exception as e:
                    print(f"[ERROR] Ошибка при отправке нового сообщения модератору для номера {number}: {e}")

            # Отправляем сообщение пользователю с меню
            markup = types.InlineKeyboardMarkup()
            markup.add(
                types.InlineKeyboardButton("📱 Сдать номер снова", callback_data="submit_number"),
                types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main")
            )
            bot.send_message(
                call.message.chat.id,
                f"❌ Вы отметили номер {number} как невалидный. Номер удалён из системы.",
                reply_markup=markup,
                parse_mode='HTML'
            )

            # Удаляем номер из code_messages
            if number in code_messages:
                del code_messages[number]
                print(f"[DEBUG] Номер {number} удалён из code_messages")

            bot.answer_callback_query(call.id, "Номер помечен как невалидный.")
            del button_contexts[key]

    except Exception as e:
        print(f"[ERROR] Ошибка в handle_button_callback: {e}")
        bot.answer_callback_query(call.id, "Произошла ошибка. Попробуйте снова.")

@bot.callback_query_handler(func=lambda call: call.data.startswith("confirm_code_"))
def confirm_code(call):
    try:    
        parts = call.data.split("_")
        if len(parts) < 5:
            bot.answer_callback_query(call.id, "❌ Неверный формат данных!")
            return
        
        number = parts[2]
        code = parts[3]
        group_chat_id = int(parts[4])
        tg_number = int(parts[5])
        
        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT MODERATOR_ID, GROUP_CHAT_ID, ID_OWNER FROM numbers WHERE NUMBER = ?', (number,))
            result = cursor.fetchone()
            if not result:
                bot.answer_callback_query(call.id, "❌ Номер не найден!")
                return
            moderator_id, stored_chat_id, owner_id = result
        
        # Проверяем, что stored_chat_id совпадает с group_chat_id из callback
        if stored_chat_id != group_chat_id:
            print(f"[DEBUG] Несоответствие GROUP_CHAT_ID: stored_chat_id={stored_chat_id}, group_chat_id={group_chat_id}")
            stored_chat_id = group_chat_id
            cursor.execute('UPDATE numbers SET GROUP_CHAT_ID = ? WHERE NUMBER = ?', (group_chat_id, number))
            conn.commit()

        # Извлекаем данные о сообщении подтверждения
        confirmation_key = f"{number}_{owner_id}"
        if confirmation_key not in confirmation_messages:
            bot.answer_callback_query(call.id, "❌ Данные о сообщении подтверждения не найдены!")
            return
        confirmation_data = confirmation_messages[confirmation_key]
        confirmation_chat_id = confirmation_data["chat_id"]
        confirmation_message_id = confirmation_data["message_id"]

        try:    
            bot.edit_message_text(
                f"✅ Код '{code}' для номера {number}  отправлен модератору.",
                confirmation_chat_id,
                confirmation_message_id,
                parse_mode='HTML'
            )
        except Exception as e:
            print(f"Ошибка при редактировании сообщения: {e}")
            bot.answer_callback_query(call.id, "❌ Не удалось обновить сообщение!")
            return
        
        # Удаляем данные о сообщении из хранилища
        del confirmation_messages[confirmation_key]

        bot.answer_callback_query(call.id)
        
        if moderator_id:
            markup = types.InlineKeyboardMarkup()
            markup.add(
                types.InlineKeyboardButton("✅ Да, встал", callback_data=f"number_active_{number}_{tg_number}"),
                types.InlineKeyboardButton("❌ Нет, изменить", callback_data=f"number_invalid_{number}_{tg_number}")
            )
            try:
                message = bot.send_message(
                    group_chat_id,
                    f"📱 <b>ТГ {tg_number}</b>\n"
                    f"📱 Код по номеру {number}\nКод: {code}\n\nВстал ли номер?",
                    reply_markup=markup,
                    parse_mode='HTML'
                )
                # Сохраняем информацию о сообщении в code_messages
                code_messages[number] = {
                    "chat_id": group_chat_id,
                    "message_id": message.message_id,
                    "timestamp": datetime.now(),
                    "tg_number": tg_number,
                    "owner_id": owner_id
                }
            except telebot.apihelper.ApiTelegramException as e:
                print(f"Не удалось отправить сообщение в группу {group_chat_id}: {e}")
                try:
                    message = bot.send_message(
                        moderator_id,
                        f"📱 <b>ТГ {tg_number}</b>\n"
                        f"📱 Код по номеру {number}\nКод: {code}\n\nВстал ли номер?\n"
                        f"⚠️ Не удалось отправить сообщение в группу (ID: {group_chat_id}). Пожалуйста, проверьте права бота в группе.",
                        reply_markup=markup,
                        parse_mode='HTML'
                    )
                    # Сохраняем информацию о сообщении в code_messages
                    code_messages[number] = {
                        "chat_id": moderator_id,
                        "message_id": message.message_id,
                        "timestamp": datetime.now(),
                        "tg_number": tg_number,
                        "owner_id": owner_id
                    }
                except telebot.apihelper.ApiTelegramException as e:
                    print(f"Не удалось отправить сообщение модератору {moderator_id}: {e}")
                    for admin_id in config.ADMINS_ID:
                        try:
                            bot.send_message(
                                admin_id,
                                f"⚠️ Ошибка: Не удалось отправить код модератору {moderator_id} для номера {number}. "
                                f"Проверьте права бота в группе {group_chat_id} и доступность модератора.",
                                parse_mode='HTML'
                            )
                        except:
                            continue
    
    except Exception as e:
        print(f"Ошибка в confirm_code: {e}")
        bot.answer_callback_query(call.id, "❌ Произошла ошибка при подтверждении кода!")

@bot.callback_query_handler(func=lambda call: call.data.startswith("change_code_"))
def change_code(call):
    try:
        parts = call.data.split("_")
        if len(parts) < 4:
            bot.answer_callback_query(call.id, "❌ Неверный формат данных!")
            return
        
        number = parts[2]
        group_chat_id = int(parts[3])
        tg_number = int(parts[4])
        
        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT MODERATOR_ID FROM numbers WHERE NUMBER = ?', (number,))
            result = cursor.fetchone()
            if not result:
                bot.answer_callback_query(call.id, "❌ Номер не найден!")
                return
            moderator_id = result[0] if result else call.from_user.id

        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception as e:
            print(f"Ошибка при удалении сообщения: {e}")

        markup = types.ReplyKeyboardRemove()
        msg = bot.send_message(
            call.from_user.id,
            "=================\n"
            f"📱 Введите новый код для номера {number}, который будет отправлен модератору: (Используйте ответить на сообщение)",
            reply_markup=markup
        )
        
        bot.register_next_step_handler(
            msg,
            process_verification_code_input,
            number,
            moderator_id,
            group_chat_id,
            msg.chat.id,
            msg.message_id,
            tg_number
        )
    
    except Exception as e:
        print(f"Ошибка в change_code: {e}")
        bot.answer_callback_query(call.id, "❌ Произошла ошибка при изменении кода!")

def create_back_to_main_markup():
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
    return markup

#Обработчики для подтверждения/отклонения номеров

@bot.callback_query_handler(func=lambda call: call.data.startswith("moderator_confirm_"))
def moderator_confirm_number(call):
    number = call.data.split("_")[2]
    user_id = call.from_user.id
    current_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('UPDATE numbers SET STATUS = "активен", MODERATOR_ID = NULL, CONFIRMED_BY_MODERATOR_ID = ?, TAKE_DATE = ? WHERE NUMBER = ?', 
                      (user_id, current_date, number))
        cursor.execute('SELECT ID_OWNER FROM numbers WHERE NUMBER = ?', (number,))
        owner = cursor.fetchone()
        conn.commit()
        print(f"[DEBUG] Подтверждён номер: {number}, CONFIRMED_BY_MODERATOR_ID = {user_id}, TAKE_DATE = {current_date}")
    
    if owner:
        markup_owner = types.InlineKeyboardMarkup()
        markup_owner.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        bot.send_message(owner[0], 
                        f"✅ Ваш номер {number} подтвержден и поставлен в работу. Оплата будет начислена через 5 минут, если номер не слетит.",
                        reply_markup=markup_owner, parse_mode='HTML')
    
    # Обновляем сообщение модератора
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("📋 Мои номера", callback_data="moderator_numbers"))
    bot.edit_message_text(
        f"📱 Номер {number} поставлен в работу. Оплата будет начислена через 5 минут, если номер не слетит.\nНажмите 'Мои номера' для обновления списка.",
        call.message.chat.id, 
        call.message.message_id, 
        reply_markup=markup, 
        parse_mode='HTML'
    )
    
    # Вызываем handle_moderator_numbers для немедленного обновления интерфейса
    try:
        new_call = types.CallbackQuery(
            id=call.id,
            from_user=call.from_user,
            message=call.message,
            chat_instance=call.chat_instance,
            data="moderator_numbers"
        )
        handle_moderator_numbers(new_call)
    except Exception as e:
        print(f"[ERROR] Не удалось обновить интерфейс: {e}")
        bot.answer_callback_query(call.id, "⚠️ Ошибка при обновлении списка номеров.")

@bot.callback_query_handler(func=lambda call: call.data.startswith("moderator_reject_"))
def handle_number_rejection(call):
    number = call.data.split("_")[2]
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT ID_OWNER FROM numbers WHERE NUMBER = ?', (number,))
        owner = cursor.fetchone()
        cursor.execute('DELETE FROM numbers WHERE NUMBER = ?', (number,))
        conn.commit()

        if owner:
            markup_owner = types.InlineKeyboardMarkup()
            markup_owner.add(types.InlineKeyboardButton("📱 Сдать номер снова", callback_data="submit_number"))
            markup_owner.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
            try:
                bot.send_message(owner[0], 
                               f"❌ Ваш номер {number} был отклонен модератором.\n📱 Проверьте номер и сдайте заново.", 
                               reply_markup=markup_owner)
            except:
                pass

    markup_mod = types.InlineKeyboardMarkup()
    markup_mod.add(types.InlineKeyboardButton("📲 Получить новый номер", callback_data="get_number"))
    markup_mod.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back_to_main"))
    bot.edit_message_text(f"📱 Номер {number} отклонен и удалён из очереди.\n❌ Номер не встал.", 
                         call.message.chat.id, 
                         call.message.message_id, 
                         reply_markup=markup_mod)

@bot.callback_query_handler(func=lambda call: call.data.startswith("number_active_"))
def number_active(call):
    try:
        parts = call.data.split("_")
        if len(parts) < 4:
            bot.answer_callback_query(call.id, "❌ Неверный формат данных!")
            return

        number = parts[2]
        tg_number = int(parts[3])

        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT ID_OWNER FROM numbers WHERE NUMBER = ?', (number,))
            owner = cursor.fetchone()
            if not owner:
                bot.answer_callback_query(call.id, "❌ Номер не найден!")
                return

            # Устанавливаем статус 'активен', ID модератора и время подтверждения
            current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute(
                '''
                UPDATE numbers 
                SET STATUS = ?, 
                    CONFIRMED_BY_MODERATOR_ID = ?, 
                    TAKE_DATE = ? 
                WHERE NUMBER = ?
                ''',
                ('активен', call.from_user.id, current_time, number)
            )
            conn.commit()
            print(f"[DEBUG] Номер {number} подтверждён модератором {call.from_user.id}, статус: активен, TAKE_DATE: {current_time}")

        if owner:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
            try:
                bot.send_message(
                    owner[0],
                    f"✅ Ваш номер {number} подтверждён и теперь активен.\n⏳ Встал: {current_time}.",
                    reply_markup=markup,
                    parse_mode='HTML'
                )
                print(f"[DEBUG] Уведомление отправлено владельцу {owner[0]} о подтверждении номера {number}")
            except Exception as e:
                print(f"[ERROR] Не удалось отправить уведомление владельцу {owner[0]}: {e}")

        bot.edit_message_text(
            f"📱 <b>ТГ {tg_number}</b>\n✅ Номер {number} подтверждён в {current_time}.",
            call.message.chat.id,
            call.message.message_id,
            parse_mode='HTML'
        )
        bot.answer_callback_query(call.id, "✅ Номер успешно подтверждён!")

    except Exception as e:
        print(f"[ERROR] Ошибка в number_active: {e}")
        bot.answer_callback_query(call.id, "❌ Произошла ошибка при подтверждении номера!")

@bot.callback_query_handler(func=lambda call: call.data.startswith("invalid_"))
def handle_invalid_number(call):
    number = call.data.split("_")[1]
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT ID_OWNER FROM numbers WHERE NUMBER = ?', (number,))
        owner = cursor.fetchone()
        cursor.execute('DELETE FROM numbers WHERE NUMBER = ?', (number,))
        conn.commit()

        if owner:
            markup_owner = types.InlineKeyboardMarkup()
            markup_owner.add(types.InlineKeyboardButton("📱 Сдать номер", callback_data="submit_number"))
            markup_owner.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
            try:
                bot.send_message(owner[0], 
                               f"❌ Ваш номер {number} был отклонен модератором.\n📱 Проверьте номер и сдайте заново.", 
                               reply_markup=markup_owner)
            except:
                pass

 
    bot.edit_message_text(f"✅ Номер {number} успешно удален из системы", 
                         call.message.chat.id, 
                         call.message.message_id )


@bot.callback_query_handler(func=lambda call: call.data.startswith("number_failed_"))
def handle_number_failed(call):
    number = call.data.split("_")[2]
    try:
        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT TAKE_DATE, ID_OWNER, CONFIRMED_BY_MODERATOR_ID, STATUS FROM numbers WHERE NUMBER = ?', (number,))
            data = cursor.fetchone()
            if not data:
                bot.answer_callback_query(call.id, "❌ Номер не найден!")
                return
            
            take_date, owner_id, confirmed_by_moderator_id, status = data
            
            if status == "отстоял":
                bot.answer_callback_query(call.id, "✅ Номер уже отстоял своё время!")
                return

            cursor.execute('SELECT HOLD_TIME FROM settings')
            result = cursor.fetchone()
            hold_time = result[0] if result else 5
            
            end_time = datetime.now()
            if take_date in ("0", "1"):
                work_time = 0
                worked_enough = False
            else:
                start_time = datetime.strptime(take_date, "%Y-%m-%d %H:%M:%S")
                work_time = (end_time - start_time).total_seconds() / 60
                worked_enough = work_time >= hold_time
            
            shutdown_date = end_time.strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute('UPDATE numbers SET SHUTDOWN_DATE = ?, STATUS = "слетел" WHERE NUMBER = ?', 
                          (shutdown_date, number))
            conn.commit()
        
        mod_message = (
            f"📱 Номер: {number}\n"
            f"📊 Статус: слетел\n"
        )
        if take_date not in ("0", "1"):
            mod_message += f"🟢 Встал: {take_date}\n"
        mod_message += f"🔴 Слетел: {shutdown_date}\n"
        if not worked_enough:
            mod_message += f"⚠️ Номер не отработал минимальное время ({hold_time} минут)!\n"
        mod_message += f"⏳ Время работы: {work_time:.2f} минут"
        
        owner_message = (
            f"❌ Ваш номер {number} слетел.\n"
            f"📱 Номер: {number}\n"
            f"📊 Статус: слетел\n"
        )
        if take_date not in ("0", "1"):
            owner_message += f"🟢 Встал: {take_date}\n"
        owner_message += f"🔴 Слетел: {shutdown_date}\n"
        if not worked_enough:
            owner_message += f"⏳ Время работы: {work_time:.2f} минут"
        
        bot.send_message(owner_id, owner_message, parse_mode='HTML')
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="moderator_numbers"))
        bot.edit_message_text(mod_message, 
                            call.message.chat.id, 
                            call.message.message_id, 
                            reply_markup=markup,
                            parse_mode='HTML')
    
    except Exception as e:
        print(f"Ошибка в handle_number_failed: {e}")
        bot.answer_callback_query(call.id, "❌ Произошла ошибка при обработке номера.")


#Просмотр номеров:

@bot.callback_query_handler(func=lambda call: call.data == "moderator_numbers")
def handle_moderator_numbers(call):
    user_id = call.from_user.id
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT n.NUMBER, n.STATUS, n.TAKE_DATE, n.SHUTDOWN_DATE, n.MODERATOR_ID, n.CONFIRMED_BY_MODERATOR_ID
            FROM numbers n
            WHERE n.MODERATOR_ID = ? OR n.CONFIRMED_BY_MODERATOR_ID = ?
        ''', (user_id, user_id))
        numbers = cursor.fetchall()
    
    if not numbers:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("📲 Получить номер", callback_data="get_number"))
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        bot.edit_message_text("📭 У вас нет номеров.", call.message.chat.id, call.message.message_id, reply_markup=markup)
        return
    
    text = "📋 <b>Ваши номера:</b>\n\n"
    
    markup = types.InlineKeyboardMarkup()
    for number, status, take_date, shutdown_date, moderator_id, confirmed_by in numbers:
        if confirmed_by == user_id and shutdown_date == "0" and status == "активен":
            markup.add(types.InlineKeyboardButton(f"ℹ️ {number}", callback_data=f"view_number_details_{number}"))
        elif status == "отстоял":
            markup.add(types.InlineKeyboardButton(f"🟢 {number}", callback_data=f"view_stood_number_{number}"))
        elif status == "слетел":
            markup.add(types.InlineKeyboardButton(f"🔴 {number}", callback_data=f"view_failed_number_{number}"))
        elif status == "на проверке" and moderator_id == user_id:
            markup.add(types.InlineKeyboardButton(f"❓ {number}", callback_data=f"view_number_details_{number}"))
    
    markup.add(types.InlineKeyboardButton("📲 Получить номер", callback_data="get_number"))
    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
    bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode='HTML', reply_markup=markup)

def view_active_number(call):
    number = call.data.split("_")[3]
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT TAKE_DATE FROM numbers WHERE NUMBER = ?', (number,))
        take_date = cursor.fetchone()[0]
        print(f"Active number {number} details: take_date={take_date}")  # Отладка
    
    text = (
        f"📲 Номер: {number}\n"
        f"📊 Статус: активен\n"
        f"🟢 Встал: {take_date}\n"
    )
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔴 Слетел", callback_data=f"mark_failed_{number}"))
    markup.add(types.InlineKeyboardButton("Вернуться в номера", callback_data="moderator_numbers"))
    bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode='HTML', reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("mark_failed_"))
def mark_failed(call):
    number = call.data.split("_")[2]
    user_id = call.from_user.id
    
    try:
        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT TAKE_DATE, ID_OWNER, CONFIRMED_BY_MODERATOR_ID, STATUS FROM numbers WHERE NUMBER = ?', (number,))
            data = cursor.fetchone()
            if not data:
                bot.answer_callback_query(call.id, "❌ Номер не найден!")
                return
            
            take_date, owner_id, confirmed_by_moderator_id, status = data
            
            if status == "отстоял":
                bot.answer_callback_query(call.id, "✅ Номер уже отстоял своё время!")
                return
            
            if confirmed_by_moderator_id != user_id:
                bot.answer_callback_query(call.id, "❌ Вы не можете пометить этот номер как слетевший!")
                return
            
            cursor.execute('SELECT HOLD_TIME FROM settings')
            result = cursor.fetchone()
            hold_time = result[0] if result else 5
            
            end_time = datetime.now()
            if take_date in ("0", "1"):
                work_time = 0
                worked_enough = False
            else:
                start_time = datetime.strptime(take_date, "%Y-%m-%d %H:%M:%S")
                work_time = (end_time - start_time).total_seconds() / 60
                worked_enough = work_time >= hold_time
            
            shutdown_date = end_time.strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute('UPDATE numbers SET SHUTDOWN_DATE = ?, STATUS = "слетел" WHERE NUMBER = ?', 
                          (shutdown_date, number))
            conn.commit()
        
        mod_message = (
            f"📱 Номер: <code>{number}</code>\n"
            f"📊 Статус: слетел\n"
        )
        if take_date not in ("0", "1"):
            mod_message += f"🟢 Встал: {take_date}\n"
        mod_message += f"🔴 Слетел: {shutdown_date}\n"
        if not worked_enough:
            mod_message += f"⚠️ Номер не отработал минимальное время ({hold_time} минут)!\n"
        mod_message += f"⏳ Время работы: {work_time:.2f} минут"
        
        owner_message = (
            f"❌ Ваш номер {number} слетел.\n"
            f"📱 Номер: <code>{number}</code>\n"
            f"📊 Статус: слетел\n"
        )
        if take_date not in ("0", "1"):
            owner_message += f"🟢 Встал: {take_date}\n"
        owner_message += f"🔴 Слетел: {shutdown_date}\n"
        if not worked_enough:
            owner_message += f"⏳ Время работы: {work_time:.2f} минут"
        
        bot.send_message(owner_id, owner_message, parse_mode='HTML')
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 В номера", callback_data="moderator_numbers"))
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        bot.edit_message_text(mod_message, 
                            call.message.chat.id, 
                            call.message.message_id, 
                            reply_markup=markup,
                            parse_mode='HTML')
    
    except Exception as e:
        print(f"[ERROR] Ошибка в mark_failed: {e}")
        bot.answer_callback_query(call.id, "❌ Произошла ошибка при обработке номера.")

@bot.callback_query_handler(func=lambda call: call.data.startswith("view_failed_number_"))
def view_failed_number(call):
    number = call.data.split("_")[3]
    user_id = call.from_user.id
    is_moderator = db.is_moderator(user_id)
    
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT STATUS, TAKE_DATE, SHUTDOWN_DATE FROM numbers WHERE NUMBER = ?', (number,))
        data = cursor.fetchone()
    
    if not data:
        bot.answer_callback_query(call.id, "❌ Номер не найден!")
        return
    
    status, take_date, shutdown_date = data
    text = (
        f"📱 Номер: <code>{number}</code>\n"
        f"📊 Статус: {status}\n"
        f"🟢 Встал: {take_date}\n"
        f"🔴 Слетел: {shutdown_date}\n"
    )
    
    markup = types.InlineKeyboardMarkup()
    back_callback = "my_numbers" if not is_moderator else "moderator_numbers"
    markup.add(types.InlineKeyboardButton("🔙 В номера", callback_data=back_callback))
    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
    bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode='HTML', reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("view_stood_number_"))
def view_stood_number(call):
    number = call.data.split("_")[3]
    user_id = call.from_user.id
    is_moderator = db.is_moderator(user_id)
    
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT STATUS, TAKE_DATE, SHUTDOWN_DATE FROM numbers WHERE NUMBER = ?', (number,))
        data = cursor.fetchone()
    
    if not data:
        bot.answer_callback_query(call.id, "❌ Номер не найден!")
        return
    
    status, take_date, shutdown_date = data
    text = (
        f"📱 Номер: <code>{number}</code>\n"
        f"📊 Статус: {status}\n"
        f"🟢 Встал: {take_date}\n"
        f"🟢 Отстоял: {shutdown_date}\n"
    )
    
    markup = types.InlineKeyboardMarkup()
    back_callback = "my_numbers" if not is_moderator else "moderator_numbers"
    markup.add(types.InlineKeyboardButton("🔙 В номера", callback_data=back_callback))
    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
    bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode='HTML', reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("view_number_details_"))
def view_number_details(call):
    number = call.data.split("_")[3]
    user_id = call.from_user.id
    is_moderator = db.is_moderator(user_id)
    
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT STATUS, TAKE_DATE, SHUTDOWN_DATE, MODERATOR_ID, CONFIRMED_BY_MODERATOR_ID 
            FROM numbers 
            WHERE NUMBER = ?
        ''', (number,))
        data = cursor.fetchone()
    
    if not data:
        bot.answer_callback_query(call.id, "❌ Номер не найден!")
        return
    
    status, take_date, shutdown_date, moderator_id, confirmed_by_moderator_id = data
    text = (
        f"📱 Номер: <code>{number}</code>\n"
        f"📊 Статус: {status}\n"
    )
    # Показываем "Встал" только для статусов "активен" или "отстоял" и если TAKE_DATE не "0" или "1"
    if status in ("активен", "отстоял") and take_date not in ("0", "1"):
        text += f"🟢 Встал: {take_date}\n"
    if shutdown_date and shutdown_date != "0":
        text += f"{'🟢 Отстоял' if status == 'отстоял' else '🔴 Слетел'}: {shutdown_date}\n"
    
    markup = types.InlineKeyboardMarkup()
    if is_moderator and shutdown_date == "0" and status == "активен" and confirmed_by_moderator_id == user_id:
        markup.add(types.InlineKeyboardButton("🔴 Слетел", callback_data=f"mark_failed_{number}"))
    
    back_callback = "my_numbers" if not is_moderator else "moderator_numbers"
    markup.add(types.InlineKeyboardButton("🔙 В номера", callback_data=back_callback))
    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
    
    bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode='HTML', reply_markup=markup)
    
def confirm_number(call):
    number = call.data.split("_")[2]
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('UPDATE numbers SET CONFIRMED_BY_MODERATOR_ID = ? WHERE NUMBER = ?', (call.from_user.id, number))
        conn.commit()
        print(f"Confirmed number {number} with moderator_id {call.from_user.id}")  # Отладка
    
    # Обновляем интерфейс модератора
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT NUMBER, STATUS, TAKE_DATE, SHUTDOWN_DATE, CONFIRMED_BY_MODERATOR_ID, MODERATOR_ID FROM numbers WHERE MODERATOR_ID = ? OR CONFIRMED_BY_MODERATOR_ID = ?', (call.from_user.id, call.from_user.id))
        numbers = cursor.fetchall()
        print("Updated numbers after confirmation:", numbers)  # Отладка
    
    text = "📋 <b>Ваши номера для проверки:</b>\n\n"
    markup = types.InlineKeyboardMarkup()
    for number, status, take_date, shutdown_date, confirmed_by, moderator_id in numbers:
        if confirmed_by and confirmed_by != 0 and not shutdown_date:
            markup.add(types.InlineKeyboardButton(f"⚪{number}", callback_data=f"view_active_number_{number}"))
        elif status == "отстоял":
            markup.add(types.InlineKeyboardButton(f"🟢 {number}", callback_data=f"view_stood_number_{number}"))
        elif shutdown_date and shutdown_date != "0":
            markup.add(types.InlineKeyboardButton(f"🔴 {number}", callback_data=f"view_failed_number_{number}"))
        elif moderator_id == call.from_user.id and (not confirmed_by or confirmed_by == 0):
            markup.add(types.InlineKeyboardButton(f"⏳{number}", callback_data=f"confirm_number_{number}"))
    
    markup.add(types.InlineKeyboardButton("📲 Получить номер", callback_data="get_number"))
    markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
    bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode='HTML', reply_markup=markup)
    bot.answer_callback_query(call.id, f"✅ Номер {number} подтверждён.")

@bot.callback_query_handler(func=lambda call: call.data.startswith("moderator_number_"))
def show_number_details(call):
    number = call.data.split("_")[2]
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT STATUS, TAKE_DATE, SHUTDOWN_DATE, MODERATOR_ID, CONFIRMED_BY_MODERATOR_ID FROM numbers WHERE NUMBER = ?', (number,))
        data = cursor.fetchone()

    if data:
        status, take_date, shutdown_date, moderator_id, confirmed_by_moderator_id = data
        text = (f"📱 <b>Статус номера:</b> {status}\n"
                f"📱 <b>Номер:</b> {number}\n")
        if take_date not in ("0", "1"):
            text += f"🟢 <b>Встал:</b> {take_date}\n"
        if shutdown_date != "0":
            if status == "отстоял":
                text += f"🟢 <b>Отстоял:</b> {shutdown_date}\n"
            else:
                text += f"❌ <b>Слетел:</b> {shutdown_date}\n"

        markup = types.InlineKeyboardMarkup()
        if shutdown_date == "0" and (moderator_id == call.from_user.id or confirmed_by_moderator_id == call.from_user.id):
            markup.add(types.InlineKeyboardButton("🔴 Слетел", callback_data=f"number_failed_{number}"))
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="moderator_numbers"))
        markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
        bot.edit_message_text(text, 
                            call.message.chat.id, 
                            call.message.message_id, 
                            reply_markup=markup,
                            parse_mode='HTML')
        













































#КОД ДЛЯ РЕАГИРОВАНИЙ НУ ну Тг тг
@bot.message_handler(content_types=['text'], func=lambda message: message.chat.type in ['group', 'supergroup'])
def handle_group_commands(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    text = message.text.strip()

    # Проверяем, есть ли chat_id в таблице groups
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT ID FROM groups WHERE ID = ?', (chat_id,))
        if not cursor.fetchone():
            return

    was_afk = db_module.get_afk_status(user_id)  # Исправлено
    db_module.update_last_activity(user_id)

    if was_afk:
        safe_send_message(user_id, "🔔 Вы вышли из режима АФК. Ваши номера снова видны.", parse_mode='HTML')
    tg_pattern = r'^тг(\d{1,2})$'
    match = re.match(tg_pattern, text.lower())
    if match:
        tg_number = int(match.group(1))
        if 1 <= tg_number <= 70:
            get_number_in_group(user_id, chat_id, message.message_id, tg_number)
        return

    failed_pattern = r'^слет\s+(\+?\d{10,11})$'
    failed_match = re.match(failed_pattern, text.lower())
    if failed_match:
        number_input = failed_match.group(1)
        normalized_number = is_russian_number(number_input)
        if not normalized_number:
            bot.reply_to(message, "❌ Неверный формат номера! Используйте российский номер, например: +79991234567 или 89991234567")
            return

        if not db_module.is_moderator(user_id) and user_id not in config.ADMINS_ID:
            bot.reply_to(message, "❌ У вас нет прав для выполнения этой команды!")
            return

        try:
            with db.get_db() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT TAKE_DATE, ID_OWNER, CONFIRMED_BY_MODERATOR_ID, STATUS, TG_NUMBER, MODERATOR_ID
                    FROM numbers
                    WHERE NUMBER = ?
                ''', (normalized_number,))
                data = cursor.fetchone()
                if not data:
                    bot.reply_to(message, f"❌ Номер {normalized_number} не найден!")
                    return

                take_date, owner_id, confirmed_by_moderator_id, status, tg_number, moderator_id = data
                tg_number = tg_number or 1

                if status == "отстоял":
                    bot.reply_to(message, f"✅ Номер {normalized_number} уже отстоял своё время!")
                    return
                if status not in ("активен", "taken"):
                    bot.reply_to(message, f"❌ Номер {normalized_number} не активен (статус: {status})!")
                    return

                if confirmed_by_moderator_id != user_id and moderator_id != user_id:
                    bot.reply_to(message, f"❌ Вы не можете пометить этот номер как слетевший!")
                    return

                cursor.execute('SELECT HOLD_TIME FROM settings')
                result = cursor.fetchone()
                hold_time = result[0] if result else 5

                end_time = datetime.now()
                if take_date in ("0", "1"):
                    work_time = 0
                    worked_enough = False
                else:
                    start_time = datetime.strptime(take_date, "%Y-%m-%d %H:%M:%S")
                    work_time = (end_time - start_time).total_seconds() / 60
                    worked_enough = work_time >= hold_time

                shutdown_date = end_time.strftime("%Y-%m-%d %H:%M:%S")
                cursor.execute('UPDATE numbers SET SHUTDOWN_DATE = ?, STATUS = "слетел" WHERE NUMBER = ?', 
                              (shutdown_date, normalized_number))
                conn.commit()
                logging.info(f"Модератор {user_id} пометил номер {normalized_number} как слетел")

            mod_message = (
                f"📱 <b>ТГ {tg_number}</b>\n"
                f"📱 Номер: <code>{normalized_number}</code>\n"
                f"📊 Статус: слетел\n"
            )
            if take_date not in ("0", "1"):
                mod_message += f"🟢 Встал: {take_date}\n"
            mod_message += f"🔴 Слетел: {shutdown_date}\n"
            if not worked_enough:
                mod_message += f"⚠️ Номер не отработал минимальное время ({hold_time} минут)!\n"
            mod_message += f"⏳ Время работы: {work_time:.2f} минут"

            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
            bot.reply_to(message, mod_message, parse_mode='HTML', reply_markup=markup)

            owner_message = (
                f"❌ Ваш номер {normalized_number} слетел.\n"
                f"📱 Номер: <code>{normalized_number}</code>\n"
                f"📊 Статус: слетел\n"
            )
            if take_date not in ("0", "1"):
                owner_message += f"🟢 Встал: {take_date}\n"
            owner_message += f"🔴 Слетел: {shutdown_date}\n"
            owner_message += f"⏳ Время работы: {work_time:.2f} минут"

            markup_owner = types.InlineKeyboardMarkup()
            markup_owner.add(types.InlineKeyboardButton("📱 Сдать номер", callback_data="submit_number"))
            markup_owner.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
            safe_send_message(owner_id, owner_message, parse_mode='HTML', reply_markup=markup_owner)

        except Exception as e:
            logging.error(f"Ошибка при обработке команды 'слет' для номера {normalized_number}: {e}")
            bot.reply_to(message, "❌ Произошла ошибка при обработке номера.")


# Глобальный словарь для отслеживания активных запросов кодов по user_id
active_code_requests = {}
@bot.callback_query_handler(func=lambda call: call.data.startswith("code_entered_"))
def confirm_verification_code(call):
    number = call.data.split("_")[2]
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('UPDATE numbers SET VERIFICATION_CODE = NULL WHERE NUMBER = ?', (number,))
        cursor.execute('SELECT MODERATOR_ID FROM numbers WHERE NUMBER = ?', (number,))
        moderator_id = cursor.fetchone()[0]
        conn.commit()

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back_to_main"))
    bot.edit_message_text(f"✅ Код для номера {number}  отправлен модератору.", 
                         call.message.chat.id, 
                         call.message.message_id, 
                         reply_markup=markup)

    if moderator_id:
        markup_mod = types.InlineKeyboardMarkup()
        markup_mod.add(
            types.InlineKeyboardButton("✅ Подтвердить", callback_data=f"moderator_confirm_{number}"),
            types.InlineKeyboardButton("❌ Не встал", callback_data=f"moderator_reject_{number}")
        )
        try:
            bot.send_message(moderator_id, 
                           f"📱 Номер {number} готов к подтверждению.\nПожалуйста, подтвердите или отклоните.", 
                           reply_markup=markup_mod)
        except:
            pass

@bot.callback_query_handler(func=lambda call: call.data.startswith("code_error_"))
def handle_verification_error(call):
    number = call.data.split("_")[2]
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT MODERATOR_ID FROM numbers WHERE NUMBER = ?', (number,))
        moderator_id = cursor.fetchone()[0]
        cursor.execute('DELETE FROM numbers WHERE NUMBER = ?', (number,))
        conn.commit()
    
    bot.edit_message_text(f"❌ Номер {number} удалён из системы из-за ошибки в коде.", 
                         call.message.chat.id, 
                         call.message.message_id)

    for admin_id in config.ADMINS_ID:
        try:
            bot.send_message(admin_id, f"❌ Код был неправильный, номер {number} из очереди удалён.")
        except:
            pass

    if moderator_id:
        markup_mod = types.InlineKeyboardMarkup()
        markup_mod.add(types.InlineKeyboardButton("📲 Получить новый номер", callback_data="get_number"))
        markup_mod.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back_to_main"))
        try:
            bot.send_message(moderator_id, 
                           f"❌ Номер {number} был удалён владельцем из-за ошибки в коде.", 
                           reply_markup=markup_mod)
        except:
            pass



       


@bot.callback_query_handler(func=lambda call: call.data.startswith("back_to_confirm_"))
def back_to_confirm(call):
    try:
        number = call.data.split("_")[3]
        
        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT ID_OWNER, VERIFICATION_CODE, TAKE_DATE, TG_NUMBER FROM numbers WHERE NUMBER = ?', (number,))
            result = cursor.fetchone()
            
            if not result:
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("📲 Получить новый номер", callback_data="get_number"))
                markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                bot.send_message(
                    call.message.chat.id,
                    f"❌ Номер {number} больше недоступен.\nПожалуйста, получите новый номер.",
                    reply_markup=markup
                )
                return
            
            owner_id, code, take_date, tg_number = result
            if not tg_number:
                tg_number = 1
            
            try:
                bot.delete_message(call.message.chat.id, call.message.message_id)
            except Exception as e:
                print(f"Ошибка при удалении сообщения: {e}")
            
            if code and take_date != "0":
                markup = types.InlineKeyboardMarkup()
                markup.add(
                    types.InlineKeyboardButton("✅ Да, встал", callback_data=f"number_active_{number}_{tg_number}"),
                    types.InlineKeyboardButton("❌ Нет, изменить", callback_data=f"number_invalid_{number}_{tg_number}")
                )
                bot.send_message(
                    call.message.chat.id,
                    f"📱 <b>ТГ {tg_number}</b>\n"
                    f"📱 Код по номеру {number}\nКод: {code}\n\nВстал ли номер?",
                    parse_mode='HTML',
                    reply_markup=markup
                )
            else:
                markup = types.InlineKeyboardMarkup()
                markup.add(
                    types.InlineKeyboardButton("✉️ Отправить код", callback_data=f"send_code_{number}_{call.message.chat.id}_{tg_number}"),
                    types.InlineKeyboardButton("❌ Номер невалидный", callback_data=f"invalid_{number}")
                )
                markup.add(types.InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main"))
                bot.send_message(
                    call.message.chat.id,
                    f"📱 <b>ТГ {tg_number}</b>\n"
                    f"📱 Новый номер для проверки: <code>{number}</code>\n"
                    "Ожидайте код от владельца или отметьте номер как невалидный.",
                    parse_mode='HTML',
                    reply_markup=markup
                )
    except Exception as e:
        print(f"Ошибка в back_to_confirm: {e}")
        bot.answer_callback_query(call.id, "❌ Произошла ошибка при возврате к подтверждению!")



@bot.callback_query_handler(func=lambda call: call.data == "toggle_afk")
def toggle_afk(call):
    user_id = call.from_user.id
    db.update_last_activity(user_id)
    new_afk_status = db_module.toggle_afk_status(user_id)
    
    print(f"[DEBUG] Пользователь {user_id} изменил статус АФК на {'включён' if new_afk_status else 'выключен'}")
    
    # Уведомление о смене статуса АФК
    try:
        if new_afk_status:
            bot.send_message(
                call.message.chat.id,
                "🔔 Вы вошли в режим АФК. Ваши номера скрыты. Что-бы выйти из рeжима АФК, пропишите /start",
                parse_mode='HTML'
            )
        else:
            bot.send_message(
                call.message.chat.id,
                "🔔 Вы вышли из режима АФК. Ваши номера снова видны.",
                parse_mode='HTML'
            )
    except Exception as e:
        print(f"[ERROR] Не удалось отправить уведомление о смене АФК пользователю {user_id}: {e}")
    
    with db_module.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT PRICE, HOLD_TIME FROM settings')
        result = cursor.fetchone()
        price, hold_time = result if result else (2.0, 5)
    
    is_admin = user_id in config.ADMINS_ID
    is_moderator = db_module.is_moderator(user_id)
    
    if is_moderator:
        welcome_text = "Заявки"
    else:
        welcome_text = (
            f"<b>📢 Добро пожаловать в {config.SERVICE_NAME}</b>\n\n"
            f"<b>⏳ График работы:</b> <code>{config.WORK_TIME}</code>\n\n"
            "<b>💼 Как это работает?</b>\n"
            "• <i>Вы продаёте номер</i> – <b>мы предоставляем стабильные выплаты.</b>\n"
            f"• <i>Моментальные выплаты</i> – <b>после {hold_time} минут работы.</b>\n\n"
            "<b>💰 Тарифы на сдачу номеров:</b>\n"
            f"▪️ <code>{price}$</code> за номер (холд {hold_time} минут)\n"
            f"<b>📍 Почему выбирают {config.SERVICE_NAME} ?</b>\n"
            "✅ <i>Прозрачные условия сотрудничества</i>\n"
            "✅ <i>Выгодные тарифы и моментальные выплаты</i>\n"
            "✅ <i>Оперативная поддержка 24/7</i>\n\n"
            "<b>🔹 Начните зарабатывать прямо сейчас!</b>"
        )
    
    markup = types.InlineKeyboardMarkup()
    if not is_moderator or is_admin:
        markup.row(
            types.InlineKeyboardButton("👤 Мой профиль", callback_data="profile"),
            types.InlineKeyboardButton("📱 Сдать номер", callback_data="submit_number")
        )
    if is_admin:
        markup.add(types.InlineKeyboardButton("⚙️ Админка", callback_data="admin_panel"))
    if is_moderator:
        markup.add(
            types.InlineKeyboardButton("📲 Получить номер", callback_data="get_number"),
            types.InlineKeyboardButton("📋 Мои номера", callback_data="moderator_numbers")
        )
    afk_button_text = "🟢 Включить АФК" if not new_afk_status else "🔴 Выключить АФК"
    markup.add(types.InlineKeyboardButton(afk_button_text, callback_data="toggle_afk"))
    
    bot.edit_message_text(
        welcome_text,
        call.message.chat.id,
        call.message.message_id,
        parse_mode='HTML' if not is_moderator else None,
        reply_markup=markup
    )
    
    status_text = "включён" if new_afk_status else "выключен"
    bot.answer_callback_query(call.id, f"Режим АФК {status_text}. Ваши номера {'скрыты' if new_afk_status else 'видимы'}.")


def init_db():
    db_module.create_tables()
    db_module.migrate_db()
    """Инициализирует базу данных, добавляя отсутствующие столбцы в таблицы numbers и users."""
    with db.get_db() as conn:
        cursor = conn.cursor()

        # Проверка столбцов в таблице numbers
        cursor.execute("PRAGMA table_info(numbers)")
        columns = [col[1] for col in cursor.fetchall()]
        
        if 'GROUP_CHAT_ID' not in columns:
            try:
                cursor.execute('ALTER TABLE numbers ADD COLUMN GROUP_CHAT_ID INTEGER')
                print("[INFO] Столбец GROUP_CHAT_ID успешно добавлен в таблицу numbers.")
            except sqlite3.OperationalError as e:
                print(f"[ERROR] Не удалось добавить столбец GROUP_CHAT_ID: {e}")

        if 'TG_NUMBER' not in columns:
            try:
                cursor.execute('ALTER TABLE numbers ADD COLUMN TG_NUMBER INTEGER DEFAULT 1')
                print("[INFO] Столбец TG_NUMBER успешно добавлен в таблицу numbers.")
            except sqlite3.OperationalError as e:
                print(f"[ERROR] Не удалось добавить столбец TG_NUMBER: {e}")

        # Проверка столбцов в таблице users
        cursor.execute("PRAGMA table_info(users)")
        user_columns = [col[1] for col in cursor.fetchall()]
        
        if 'IS_AFK' not in user_columns:
            try:
                cursor.execute('ALTER TABLE users ADD COLUMN IS_AFK INTEGER DEFAULT 0')
                print("[INFO] Столбец IS_AFK успешно добавлен в таблицу users.")
            except sqlite3.OperationalError as e:
                print(f"[ERROR] Не удалось добавить столбец IS_AFK: {e}")

        if 'LAST_ACTIVITY' not in user_columns:
            try:
                cursor.execute('ALTER TABLE users ADD COLUMN LAST_ACTIVITY TEXT')
                print("[INFO] Столбец LAST_ACTIVITY успешно добавлен в таблицу users.")
            except sqlite3.OperationalError as e:
                print(f"[ERROR] Не удалось добавить столбец LAST_ACTIVITY: {e}")

        conn.commit()

@bot.callback_query_handler(func=lambda call: call.data.startswith("number_invalid_"))
def number_invalid(call):
    try:
        parts = call.data.split("_")
        if len(parts) < 4:
            bot.answer_callback_query(call.id, "❌ Неверный формат данных!")
            return
        number = parts[2]
        tg_number = int(parts[3])
        
        # Сохраняем tg_number в базе данных
        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('UPDATE numbers SET TG_NUMBER = ? WHERE NUMBER = ?', (tg_number, number))
            conn.commit()
        
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("✉️ Отправить код заново", callback_data=f"send_code_{number}_{call.message.chat.id}_{tg_number}"),
            types.InlineKeyboardButton("❌ Не валидный", callback_data=f"invalid_{number}")
        )
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data=f"back_to_confirm_{number}"))
        
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception as e:
            print(f"Ошибка при удалении сообщения: {e}")
        
        bot.send_message(
            call.message.chat.id,
            f"📱 <b>ТГ {tg_number}</b>\n"
            f"📱 Номер: {number}\nПожалуйста, выберите действие:",
            reply_markup=markup,
            parse_mode='HTML'
        )
    
    except Exception as e:
        print(f"Ошибка в number_invalid: {e}")
        bot.answer_callback_query(call.id, "❌ Произошла ошибка при обработке номера.")

db_lock = Lock()

def check_inactivity():
    """Проверяет неактивность пользователей и переводит их в АФК через 10 минут."""
    while True:
        try:
            with db.get_db() as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT ID, LAST_ACTIVITY, IS_AFK FROM users')
                users = cursor.fetchall()
                current_time = datetime.now()
                for user_id, last_activity, is_afk in users:
                    # Пропускаем пользователей, которые уже в АФК или без активности
                    if is_afk or not last_activity:
                        continue
                    # Проверяем, является ли пользователь модератором
                    cursor.execute('SELECT ID FROM personal WHERE ID = ? AND TYPE = ?', (user_id, 'moder'))
                    is_moder = cursor.fetchone() is not None
                    # Проверяем, является ли пользователь администратором из config.ADMINS_ID
                    is_admin = user_id in config.ADMINS_ID
                    if is_moder or is_admin:
                        print(f"[DEBUG] Пользователь {user_id} — {'модератор' if is_moder else ''}{'администратор' if is_admin else ''}, пропускаем АФК")
                        continue  # Пропускаем модераторов и администраторов
                    try:
                        last_activity_time = datetime.strptime(last_activity, "%Y-%m-%d %H:%M:%S")
                        if current_time - last_activity_time >= timedelta(minutes=10):
                            # Переводим в АФК, только если пользователь ещё не в АФК
                            if not db_module.get_afk_status(user_id):
                                db_module.toggle_afk_status(user_id)
                                print(f"[DEBUG] Пользователь {user_id} переведён в режим АФК")
                                try:
                                    bot.send_message(
                                        user_id,
                                        "🔔 Вы были переведены в режим АФК из-за неактивности (10 минут). "
                                        "Ваши номера скрыты. Нажмите 'Выключить АФК' в главном меню, чтобы вернуться.",
                                        parse_mode='HTML'
                                    )
                                except Exception as e:
                                    print(f"[ERROR] Не удалось отправить уведомление об АФК пользователю {user_id}: {e}")
                    except ValueError as e:
                        print(f"[ERROR] Неверный формат времени активности для пользователя {user_id}: {e}")
            time.sleep(60)  # Проверяем каждую минуту
        except Exception as e:
            print(f"[ERROR] Ошибка в check_inactivity: {e}")
            time.sleep(60)

if __name__ == "__main__":
    init_db()
    timeout_thread = threading.Thread(target=check_number_timeout, daemon=True)
    timeout_thread.start()
    hold_time_thread = threading.Thread(target=check_number_hold_time, daemon=True)
    hold_time_thread.start()
    inactivity_thread = threading.Thread(target=check_inactivity, daemon=True)
    inactivity_thread.start()
    code_timeout_thread = threading.Thread(target=check_code_timeout, daemon=True)
    code_timeout_thread.start()
    run_bot()