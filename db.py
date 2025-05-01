import sqlite3
from datetime import datetime

def get_db():
    """Создаёт и возвращает подключение к базе данных."""
    return sqlite3.connect('database.db', check_same_thread=False)

def create_tables():
    """Создаёт все необходимые таблицы в базе данных."""
    with get_db() as conn:
        cursor = conn.cursor()
        # Таблица requests
        cursor.execute('''CREATE TABLE IF NOT EXISTS requests (
            ID INTEGER PRIMARY KEY,
            LAST_REQUEST TIMESTAMP,
            STATUS TEXT DEFAULT 'pending',
            BLOCKED INTEGER DEFAULT 0,
            CAN_SUBMIT_NUMBERS INTEGER DEFAULT 1
        )''')
        # Таблица withdraws
        cursor.execute('''CREATE TABLE IF NOT EXISTS withdraws (
            ID INTEGER,
            AMOUNT REAL,
            DATE TEXT,
            STATUS TEXT DEFAULT 'pending'
        )''')
        # Таблица users
        cursor.execute('''CREATE TABLE IF NOT EXISTS users (
                ID INTEGER PRIMARY KEY,
                BALANCE REAL DEFAULT 0,
                REG_DATE TEXT,
                IS_AFK INTEGER DEFAULT 0,
                LAST_ACTIVITY TEXT
            )''')
        # Таблица numbers (включаем SUBMIT_DATE)
        cursor.execute('''
                CREATE TABLE IF NOT EXISTS numbers (
                    NUMBER TEXT PRIMARY KEY,
                    ID_OWNER INTEGER,
                    STATUS TEXT,
                    TAKE_DATE TEXT,
                    SHUTDOWN_DATE TEXT,
                    CONFIRMED_BY_MODERATOR_ID INTEGER,
                    TG_NUMBER INTEGER,
                    SUBMIT_DATE TEXT,
                    VERIFICATION_CODE TEXT
                )
            ''')
                # Проверка и добавление столбца MESSAGE_I
        # Остальные таблицы без изменений
        cursor.execute('''CREATE TABLE IF NOT EXISTS settings (
            PRICE REAL DEFAULT 2.0,
            HOLD_TIME INTEGER DEFAULT 5
        )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS personal (
            ID INTEGER PRIMARY KEY,
            TYPE TEXT NOT NULL,
            GROUP_ID INTEGER
        )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS groups (
            ID INTEGER PRIMARY KEY AUTOINCREMENT,
            NAME TEXT UNIQUE NOT NULL
        )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS treasury (
            ID INTEGER PRIMARY KEY CHECK (ID = 1),
            BALANCE REAL DEFAULT 0,
            AUTO_INPUT INTEGER DEFAULT 0,
            CURRENCY TEXT DEFAULT 'USDT'
        )''')
        # Инициализация settings
        cursor.execute('SELECT COUNT(*) FROM settings')
        if cursor.fetchone()[0] == 0:
            cursor.execute('INSERT INTO settings (PRICE, HOLD_TIME) VALUES (?, ?)', (2.0, 5))
        # Инициализация treasury
        cursor.execute('SELECT COUNT(*) FROM treasury')
        if cursor.fetchone()[0] == 0:
            cursor.execute('INSERT INTO treasury (ID, BALANCE, AUTO_INPUT, CURRENCY) VALUES (?, ?, ?, ?)',
                          (1, 0, 0, 'USDT'))
        conn.commit()

def migrate_db():
    """Миграция базы данных: добавляет новые столбцы, если их нет."""
    with get_db() as conn:
        cursor = conn.cursor()
        # Проверка таблицы requests
        cursor.execute('PRAGMA table_info(requests)')
        columns = [col[1] for col in cursor.fetchall()]
        if not columns:
            cursor.execute('''CREATE TABLE requests (
                ID INTEGER PRIMARY KEY,
                LAST_REQUEST TIMESTAMP,
                STATUS TEXT DEFAULT 'pending',
                BLOCKED INTEGER DEFAULT 0,
                CAN_SUBMIT_NUMBERS INTEGER DEFAULT 1
            )''')
        else:
            if 'BLOCKED' not in columns:
                cursor.execute('ALTER TABLE requests ADD COLUMN BLOCKED INTEGER DEFAULT 0')
            if 'CAN_SUBMIT_NUMBERS' not in columns:
                cursor.execute('ALTER TABLE requests ADD COLUMN CAN_SUBMIT_NUMBERS INTEGER DEFAULT 1')
        
        # Проверка таблицы numbers
        cursor.execute('PRAGMA table_info(numbers)')
        columns = [col[1] for col in cursor.fetchall()]
        if 'MODERATOR_ID' not in columns:
            cursor.execute('ALTER TABLE numbers ADD COLUMN MODERATOR_ID INTEGER')
        if 'CONFIRMED_BY_MODERATOR_ID' not in columns:
            cursor.execute('ALTER TABLE numbers ADD COLUMN CONFIRMED_BY_MODERATOR_ID INTEGER')
        if 'STATUS' not in columns:
            cursor.execute('ALTER TABLE numbers ADD COLUMN STATUS TEXT DEFAULT "ожидает"')
        if 'GROUP_CHAT_ID' not in columns:
            cursor.execute('ALTER TABLE numbers ADD COLUMN GROUP_CHAT_ID INTEGER')
        if 'TG_GROUP' not in columns:
            cursor.execute('ALTER TABLE numbers ADD COLUMN TG_GROUP TEXT')
        if 'SUBMIT_DATE' not in columns:
            cursor.execute('ALTER TABLE numbers ADD COLUMN SUBMIT_DATE TEXT')
        if 'TG_NUMBER' not in columns:
            cursor.execute('ALTER TABLE numbers ADD COLUMN TG_NUMBER INTEGER DEFAULT 1')
        if 'STATUS' in columns:
            cursor.execute('UPDATE numbers SET STATUS = "ожидает" WHERE STATUS = "активен" AND TAKE_DATE = "0"')
        
        # Проверка таблицы personal
        cursor.execute('PRAGMA table_info(personal)')
        columns = [col[1] for col in cursor.fetchall()]
        if 'GROUP_ID' not in columns:
            cursor.execute('ALTER TABLE personal ADD COLUMN GROUP_ID INTEGER')
        
        # Проверка таблицы users
        cursor.execute('PRAGMA table_info(users)')
        columns = [col[1] for col in cursor.fetchall()]
        if 'BALANCE' in columns and 'IS_AFK' not in columns:
            cursor.execute('ALTER TABLE users ADD COLUMN IS_AFK INTEGER DEFAULT 0')
        
        # Проверка таблицы settings
        cursor.execute('PRAGMA table_info(settings)')
        columns = [col[1] for col in cursor.fetchall()]
        if 'PRICE' in columns and 'HOLD_TIME' not in columns:
            cursor.execute('ALTER TABLE settings RENAME TO settings_old')
            cursor.execute('''CREATE TABLE settings (
                PRICE REAL DEFAULT 2.0,
                HOLD_TIME INTEGER DEFAULT 5
            )''')
            cursor.execute('INSERT INTO settings (PRICE, HOLD_TIME) SELECT CAST(PRICE AS REAL), MIN_TIME FROM settings_old')
            cursor.execute('DROP TABLE settings_old')
        
        # Проверка таблицы groups
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS groups (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                NAME TEXT UNIQUE NOT NULL
            )
        ''')
        # Проверка таблицы treasury
        cursor.execute('PRAGMA table_info(treasury)')
        columns = [col[1] for col in cursor.fetchall()]
        if not columns:
            cursor.execute('''CREATE TABLE treasury (
                ID INTEGER PRIMARY KEY CHECK (ID = 1),
                BALANCE REAL DEFAULT 0,
                AUTO_INPUT INTEGER DEFAULT 0,
                CURRENCY TEXT DEFAULT 'USDT'
            )''')
            cursor.execute('INSERT INTO treasury (ID, BALANCE, AUTO_INPUT, CURRENCY) VALUES (?, ?, ?, ?)',
                          (1, 0, 0, 'USDT'))
        
        conn.commit()
        
def add_user(user_id, balance=0.0, reg_date=None):
    """Добавляет нового пользователя в таблицу users."""
    if reg_date is None:
        reg_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('INSERT OR IGNORE INTO users (ID, BALANCE, REG_DATE, IS_AFK) VALUES (?, ?, ?, ?)', 
                       (user_id, balance, reg_date, 0))
        conn.commit()

def update_balance(user_id, amount):
    """Обновляет баланс пользователя."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('UPDATE users SET BALANCE = BALANCE + ? WHERE ID = ?', (amount, user_id))
        conn.commit()

def add_number(number, user_id, tg_group="1"):
    """Добавляет номер, связанный с пользователем."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT ID FROM users WHERE ID = ?', (user_id,))
        if not cursor.fetchone():
            add_user(user_id)
        cursor.execute('''
            INSERT INTO numbers (NUMBER, ID_OWNER, TAKE_DATE, SHUTDOWN_DATE, STATUS, TG_GROUP, SUBMIT_DATE) 
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (number, user_id, '0', '0', 'ожидает', tg_group, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
        print(f"[DEBUG] Добавлен номер: {number}, ID_OWNER: {user_id}, STATUS: ожидает, TG_GROUP: {tg_group}, SUBMIT_DATE: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

def update_number_status(number, status, moderator_id=None):
    """Обновляет статус номера."""
    with get_db() as conn:
        cursor = conn.cursor()
        shutdown_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S") if status != 'ожидает' else '0'
        cursor.execute('''
            UPDATE numbers 
            SET STATUS = ?, MODERATOR_ID = ?, SHUTDOWN_DATE = ? 
            WHERE NUMBER = ?
        ''', (status, moderator_id, shutdown_date, number))
        conn.commit()
        print(f"[DEBUG] Обновлён номер: {number}, STATUS: {status}, MODERATOR_ID: {moderator_id}")

def get_available_number(moderator_id):
    """Возвращает следующий доступный номер из очереди, беря по одному номеру от каждого пользователя."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT GROUP_ID FROM personal WHERE ID = ? AND TYPE = ?', (moderator_id, 'moder'))
        group_id = cursor.fetchone()
        group_id = group_id[0] if group_id else None
        
        # Получаем список пользователей с доступными номерами (не в АФК)
        query = '''
            SELECT DISTINCT n.ID_OWNER
            FROM numbers n
            LEFT JOIN users u ON n.ID_OWNER = u.ID
            LEFT JOIN personal p ON n.ID_OWNER = p.ID
            WHERE n.TAKE_DATE = "0" 
            AND n.STATUS = "ожидает"
            AND (u.IS_AFK = 0 OR u.IS_AFK IS NULL)
        '''
        params = []
        if group_id:
            query += ' AND (p.GROUP_ID = ? OR p.GROUP_ID IS NULL)'
            params.append(group_id)
        
        cursor.execute(query, params)
        users = [row[0] for row in cursor.fetchall()]
        
        if not users:
            print(f"[DEBUG] Нет доступных номеров для модератора {moderator_id} (GROUP_ID={group_id})")
            return None
        
        # Получаем последнего пользователя, чей номер был взят
        cursor.execute('''
            SELECT ID_OWNER 
            FROM numbers 
            WHERE MODERATOR_ID IS NOT NULL 
            AND TAKE_DATE != "0"
            ORDER BY TAKE_DATE DESC 
            LIMIT 1
        ''')
        last_user = cursor.fetchone()
        last_user_id = last_user[0] if last_user else None
        
        # Определяем следующего пользователя в очереди
        if last_user_id and last_user_id in users:
            current_index = users.index(last_user_id)
            next_index = (current_index + 1) % len(users)
        else:
            next_index = 0
        
        next_user_id = users[next_index]
        
        # Получаем самый старый номер от следующего пользователя
        query = '''
            SELECT n.NUMBER
            FROM numbers n
            LEFT JOIN users u ON n.ID_OWNER = u.ID
            LEFT JOIN personal p ON n.ID_OWNER = p.ID
            WHERE n.ID_OWNER = ?
            AND n.TAKE_DATE = "0" 
            AND n.STATUS = "ожидает"
            AND (u.IS_AFK = 0 OR u.IS_AFK IS NULL)
        '''
        params = [next_user_id]
        if group_id:
            query += ' AND (p.GROUP_ID = ? OR p.GROUP_ID IS NULL)'
            params.append(group_id)
        
        query += ' ORDER BY n.SUBMIT_DATE ASC LIMIT 1'
        cursor.execute(query, params)
        number = cursor.fetchone()
        
        # Логируем все доступные номера для отладки
        cursor.execute('''
            SELECT NUMBER, ID_OWNER, STATUS, TAKE_DATE, TG_GROUP, SUBMIT_DATE 
            FROM numbers 
            WHERE TAKE_DATE = "0" AND STATUS = "ожидает"
        ''')
        all_available = cursor.fetchall()
        print(f"[DEBUG] Модератор {moderator_id} (GROUP_ID={group_id}) запросил номер. Выбран пользователь {next_user_id}. Доступные номера: {all_available}")
        
        return number[0] if number else None

def get_group_name(group_id):
    """Возвращает имя группы по её ID."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT NAME FROM groups WHERE ID = ?', (group_id,))
        result = cursor.fetchone()
        return result[0] if result else None

def get_user_numbers(user_id):
    """Возвращает все номера пользователя."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''SELECT NUMBER, TAKE_DATE, SHUTDOWN_DATE, TG_GROUP FROM numbers WHERE ID_OWNER = ?''', (user_id,))
        numbers = cursor.fetchall()
    return numbers

def is_moderator(user_id):
    """Проверяет, является ли пользователь модератором."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT ID FROM personal WHERE ID = ? AND TYPE = 'moder'", (user_id,))
        return cursor.fetchone() is not None

def get_treasury_balance():
    """Возвращает баланс казны."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT BALANCE FROM treasury WHERE ID = 1")
        result = cursor.fetchone()
        return result[0] if result else 0

def update_treasury_balance(amount):
    """Обновляет баланс казны."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE treasury SET BALANCE = BALANCE + ? WHERE ID = 1", (amount,))
        conn.commit()
        cursor.execute("SELECT BALANCE FROM treasury WHERE ID = 1")
        return cursor.fetchone()[0]

def set_treasury_balance(amount):
    """Устанавливает новый баланс казны."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE treasury SET BALANCE = ? WHERE ID = 1", (amount,))
        conn.commit()
        return amount

def get_auto_input_status():
    """Возвращает статус автоматического ввода."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT AUTO_INPUT FROM treasury WHERE ID = 1")
        result = cursor.fetchone()
        return bool(result[0]) if result else False
    
from datetime import datetime

def update_last_activity(self, user_id):
    """Обновляет время последней активности пользователя и сбрасывает статус АФК."""
    with self.get_db() as conn:
        cursor = conn.cursor()
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # Проверяем, существует ли пользователь
        cursor.execute('SELECT IS_AFK FROM users WHERE ID = ?', (user_id,))
        result = cursor.fetchone()
        if not result:
            # Если пользователь не существует, создаём его
            cursor.execute('INSERT OR IGNORE INTO users (ID, BALANCE, REG_DATE, IS_AFK, LAST_ACTIVITY) VALUES (?, ?, ?, ?, ?)',
                          (user_id, 0.0, current_time, 0, current_time))
        else:
            # Сбрасываем АФК, если он включён
            if result[0] == 1:
                cursor.execute('UPDATE users SET IS_AFK = 0 WHERE ID = ?', (user_id,))
                print(f"[DEBUG] Пользователь {user_id} выведен из режима АФК")
        # Обновляем время активности
        cursor.execute('UPDATE users SET LAST_ACTIVITY = ? WHERE ID = ?', (current_time, user_id))
        conn.commit()
        print(f"[DEBUG] Обновлено время активности для пользователя {user_id}: {current_time}")

def toggle_auto_input():
    """Переключает статус автоматического ввода."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT AUTO_INPUT FROM treasury WHERE ID = 1")
        current_status = cursor.fetchone()[0]
        new_status = 1 if current_status == 0 else 0
        cursor.execute("UPDATE treasury SET AUTO_INPUT = ? WHERE ID = 1", (new_status,))
        conn.commit()
        return bool(new_status)


def log_treasury_operation(operation, amount, balance):
    """Логирует операции с казной."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"[{timestamp}] | Операция {operation} | Сумма {amount} | Остаток {balance}"
    with open("treasury_log.txt", "a", encoding="utf-8") as log_file:
        log_file.write(log_entry + "\n")

def get_afk_status(user_id):
    """Возвращает статус АФК пользователя."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT IS_AFK FROM users WHERE ID = ?', (user_id,))
        result = cursor.fetchone()
        return bool(result[0]) if result else False


def toggle_afk_status(user_id):
    """Переключает статус АФК пользователя."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT IS_AFK FROM users WHERE ID = ?', (user_id,))
        current_status = cursor.fetchone()
        if not current_status:
            add_user(user_id)
            current_status = [0]
        new_status = 1 if current_status[0] == 0 else 0
        cursor.execute('UPDATE users SET IS_AFK = ? WHERE ID = ?', (new_status, user_id))
        conn.commit()
        return bool(new_status)

# Вызов функций создания таблиц и миграции
create_tables()
migrate_db()