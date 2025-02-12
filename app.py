#!/usr/bin/env python3
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import requests
from bs4 import BeautifulSoup
from flask import (Flask, render_template, request, redirect, url_for, flash,
                   session, g, jsonify)
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from concurrent.futures import ThreadPoolExecutor, as_completed

# ========== 配置 ==========
DATABASE = 'monitor.db'
CHECK_INTERVAL = 10  # 检测间隔（秒）
NOTIFICATION_ENABLED = False  # 是否启用邮件通知，开启为True，关闭为False

# 邮件配置
MAIL_SERVER = 'smtp.example.com'  #  替换为您的 SMTP 服务器地址，例如 smtp.example.com
MAIL_PORT = 587  #  SMTP 端口，常用端口为 587 (TLS) 或 465 (SSL)
MAIL_USERNAME = 'SMTP Username'  #  SMTP 用户名
MAIL_PASSWORD = 'SMTP Password'  #  SMTP 密码
MAIL_FROM = 'sender@example.com'  #  发件人邮箱地址
MAIL_TO = 'receiver@example.com'  #  接收通知的邮箱地址

app = Flask(__name__, template_folder='public_html')
app.secret_key = 'fiZWLrANSBgtfnQp'  # 请设置为随机字符串
from flask_wtf.csrf import CSRFProtect

csrf = CSRFProtect(app)

app.config['REGISTRATION_ENABLED'] = True  # False是关闭注册功能

# 定义北京时间时区
beijing_tz = timezone(timedelta(hours=8))

# ========== 数据库管理 ==========
@contextmanager
def get_db():
    conn = sqlite3.connect(DATABASE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

def init_db():
    with get_db() as conn:
        cursor = conn.cursor()
        # 用户表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                email TEXT
            )
        ''')
        # 监控规则表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                selector TEXT NOT NULL,
                last_value TEXT,
                last_checked TIMESTAMP,
                enabled INTEGER DEFAULT 1,
                sound_url TEXT,
                use_scraper INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        ''')
        # 日志表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_id INTEGER,
                old_value TEXT,
                new_value TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(rule_id) REFERENCES rules(id)
            )
        ''')
        # 通知邮箱配置表
        cursor.execute('''
                CREATE TABLE IF NOT EXISTS rule_emails (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_id INTEGER,
                email TEXT NOT NULL,
                enabled INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(rule_id) REFERENCES rules(id)
            )
        ''')
        conn.commit()

# ========== 用户认证 ==========
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('请先登录')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

@app.before_request
def load_logged_in_user():
    user_id = session.get('user_id')
    if user_id is None:
        g.user = None
    else:
        with get_db() as conn:
            g.user = conn.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()

# ========== 线程局部 Session 和 Scraper ==========
thread_local = threading.local()

def get_session():
    if not hasattr(thread_local, 'session'):
        thread_local.session = requests.Session()
        thread_local.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
        })
    return thread_local.session

def get_scraper():
    if not hasattr(thread_local, 'scraper'):
        import cloudscraper
        thread_local.scraper = cloudscraper.create_scraper()
        thread_local.scraper.headers.update({
            'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                           'AppleWebKit/537.36 (KHTML, like Gecko) '
                           'Chrome/90.0.4430.93 Safari/537.36')
        })
    return thread_local.scraper

# ========== 网页抓取 ==========
def fetch_content(url, selector, use_scraper):
    if use_scraper:
        try:
            scraper = get_scraper()
            response = scraper.get(url, timeout=20)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')
            element = soup.select_one(selector)
            if element:
                return element.get_text(strip=True)
        except Exception as e:
            print(f"Error fetching {url} using cloudscraper: {e}")
            return None
    else:
        try:
            session_req = get_session()
            resp = session_req.get(url, timeout=20)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, 'html.parser')
            element = soup.select_one(selector)
            if element:
                return element.get_text(strip=True)
        except Exception as e:
            print(f"Error fetching {url} using requests: {e}")
            return None

# ========== 并行监控 ==========
executor = ThreadPoolExecutor(max_workers=5)

def monitor_loop():
    while True:
        with get_db() as conn:
            rules = conn.execute('SELECT * FROM rules').fetchall()
        now_beijing = datetime.now(beijing_tz)
        updates = []
        # 仅监控启用的规则，并根据 use_scraper 决定抓取方式
        future_to_rule = {
            executor.submit(fetch_content, rule['url'], rule['selector'], int(rule['use_scraper']) == 1): rule
            for rule in rules if rule['enabled'] == 1
        }
        for future in as_completed(future_to_rule):
            rule = future_to_rule[future]
            try:
                current_value = future.result()
            except Exception as exc:
                print(f"规则【{rule['name']}】异常: {exc}")
                continue
            if current_value is None:
                continue
            if rule['last_value'] != current_value:
                updates.append(('update', rule, current_value))
                if NOTIFICATION_ENABLED:
                    send_notification(rule, rule['last_value'], current_value)
            else:
                updates.append(('touch', rule, None))
        with get_db() as conn:
            for action, rule, value in updates:
                if action == 'update':
                    conn.execute(
                        'INSERT INTO logs (rule_id, old_value, new_value, timestamp) VALUES (?, ?, ?, ?)',
                        (rule['id'], rule['last_value'], value, now_beijing)
                    )
                    conn.execute(
                        'UPDATE rules SET last_value = ?, last_checked = ? WHERE id = ?',
                        (value, now_beijing, rule['id'])
                    )
                    print(f"[{now_beijing}] 规则【{rule['name']}】更新: {rule['last_value']} -> {value}")
                elif action == 'touch':
                    conn.execute(
                        'UPDATE rules SET last_checked = ? WHERE id = ?',
                        (now_beijing, rule['id'])
                    )
            conn.commit()
        time.sleep(CHECK_INTERVAL)

def start_monitoring():
    thread = threading.Thread(target=monitor_loop, daemon=True)
    thread.start()

# ========== 通知函数（可选） ==========
import smtplib
from email.mime.text import MIMEText
def send_notification(rule, old_value, new_value):
    title = f"规则【{rule['name']}】更新提醒"
    desp = f"规则【{rule['name']}】发生变化：\n旧值：{old_value}\n新值：{new_value}"
    message_body = f"{title}\n\n{desp}\n\n请访问网站查看详情: {rule['url']}"

    msg = MIMEText(message_body, 'plain', 'utf-8')
    msg['Subject'] = title
    msg['From'] = MAIL_FROM

    # 从数据库读取该规则下的通知邮箱配置
    with get_db() as conn:
        rows = conn.execute('SELECT email FROM rule_emails WHERE rule_id = ?', (rule['id'],)).fetchall()
    if rows:
        email_list = [row['email'] for row in rows]
    else:
        email_list = [MAIL_TO]  # 若未配置则使用默认收件人
    msg['To'] = ", ".join(email_list)

    try:
        server = smtplib.SMTP(MAIL_SERVER, MAIL_PORT)
        server.starttls()
        server.login(MAIL_USERNAME, MAIL_PASSWORD)
        server.sendmail(MAIL_FROM, email_list, msg.as_string())
        server.quit()
        print("邮件通知发送成功")
    except Exception as e:
        print(f"邮件通知发送失败: {e}")

# ========== 前端提示音 ==========
@app.before_request
def load_default_sound():
    g.default_sound = url_for('static', filename='sounds/default.mp3')

# ========== Flask 路由 ==========
@app.route('/')
def index():
    with get_db() as conn:
        if g.user:
            rules = conn.execute('SELECT * FROM rules WHERE user_id = ?', (g.user['id'],)).fetchall()
        else:
            rules = conn.execute('SELECT * FROM rules').fetchall()
    return render_template('index.html', rules=rules)

@app.route('/api/rules')
def api_rules():
    with get_db() as conn:
        if g.user:
            rules = conn.execute('SELECT * FROM rules WHERE user_id = ?', (g.user['id'],)).fetchall()
        else:
            rules = conn.execute('SELECT * FROM rules').fetchall()
    rules_list = [dict(rule) for rule in rules]
    return jsonify(rules_list)

@app.route('/api/logs/<int:rule_id>')
@login_required
def api_logs(rule_id):
    with get_db() as conn:
        rule = conn.execute('SELECT * FROM rules WHERE id = ? AND user_id = ?', (rule_id, g.user['id'])).fetchone()
        if not rule:
            return jsonify([]), 403
        logs = conn.execute('SELECT * FROM logs WHERE rule_id = ? ORDER BY timestamp DESC', (rule_id,)).fetchall()
    logs_list = [dict(log) for log in logs]
    return jsonify(logs_list)

# 用户注册
@app.route('/register', methods=['GET', 'POST'])
def register():
    # 检查是否允许注册
    if not app.config.get('REGISTRATION_ENABLED', True):
        flash('注册功能已关闭')
        return redirect(url_for('login'))
    
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        email = request.form.get('email', '')
        if not username or not password:
            flash('用户名和密码不能为空')
            return redirect(url_for('register'))
        password_hash = generate_password_hash(password)
        try:
            with get_db() as conn:
                conn.execute('INSERT INTO users (username, password_hash, email) VALUES (?, ?, ?)',
                             (username, password_hash, email))
                conn.commit()
            flash('注册成功，请登录')
            return redirect(url_for('login'))
        except sqlite3.IntegrityError:
            flash('用户名已存在')
            return redirect(url_for('register'))
    return render_template('register.html')

# 用户登录
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        with get_db() as conn:
            user = conn.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        if user and check_password_hash(user['password_hash'], password):
            session.clear()
            session['user_id'] = user['id']
            flash('登录成功')
            return redirect(url_for('index'))
        flash('用户名或密码错误')
        return redirect(url_for('login'))
    return render_template('login.html')

# 用户注销
@app.route('/logout')
def logout():
    session.clear()
    flash('已注销')
    return redirect(url_for('index'))

# 用户资料及密码修改
@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    if request.method == 'POST':
        email = request.form['email']
        new_password = request.form['new_password']
        with get_db() as conn:
            if new_password:
                new_password_hash = generate_password_hash(new_password)
                conn.execute('UPDATE users SET email = ?, password_hash = ? WHERE id = ?',
                             (email, new_password_hash, g.user['id']))
            else:
                conn.execute('UPDATE users SET email = ? WHERE id = ?', (email, g.user['id']))
            conn.commit()
        flash('资料更新成功')
        return redirect(url_for('profile'))
    return render_template('profile.html', user=g.user)

# 添加监控规则（登录后才能添加）
@app.route('/add', methods=['GET', 'POST'])
@login_required
def add_rule():
    if request.method == 'POST':
        name = request.form['name']
        url_rule = request.form['url']
        selector = request.form['selector']
        sound_url = request.form.get('sound_url')
[
        if not sound_url:
            sound_url = url_for('static', filename='sounds/default.mp3', _external=True)
        # 复选框处理：若勾选，则 use_scraper 为 1，否则 0
        use_scraper = 1 if request.form.get('use_scraper') == '1' else 0
        # 获取通知邮箱，多个邮箱以逗号分隔
        notification_emails = request.form.get('notification_emails', '')
        
        if not name or not url_rule or not selector:
            flash('请填写所有必填字段')
            return redirect(url_for('add_rule'))
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO rules (user_id, name, url, selector, sound_url, use_scraper)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (g.user['id'], name, url_rule, selector, sound_url, use_scraper))
            rule_id = cursor.lastrowid

            if notification_emails:
                email_list = [email.strip() for email in notification_emails.split(',') if email.strip()]
                for email in email_list:
                    cursor.execute(
                        'INSERT INTO rule_emails (rule_id, email, enabled) VALUES (?, ?, ?)',
                        (rule_id, email, 1)
                    )
            conn.commit()
        flash('监控规则添加成功')
        return redirect(url_for('index'))
    return render_template('add_rule.html')

@app.route('/edit/<int:rule_id>', methods=['GET', 'POST'])
@login_required
def edit_rule(rule_id):
    with get_db() as conn:
        rule = conn.execute('SELECT * FROM rules WHERE id = ? AND user_id = ?', (rule_id, g.user['id'])).fetchone()
    if not rule:
        flash('规则不存在或无权限')
        return redirect(url_for('index'))
    if request.method == 'POST':
        name = request.form['name']
        url_rule = request.form['url']
        selector = request.form['selector']
        sound_url = request.form.get('sound_url')
        # 如果提示音 URL 为空，则使用默认提示音（生成绝对 URL）
        if not sound_url:
            sound_url = url_for('static', filename='sounds/default.mp3', _external=True)
        use_scraper = 1 if request.form.get('use_scraper') == '1' else 0
        with get_db() as conn:
            # 更新规则基本信息
            conn.execute('''
                UPDATE rules SET name = ?, url = ?, selector = ?, sound_url = ?, use_scraper = ?
                WHERE id = ?
            ''', (name, url_rule, selector, sound_url, use_scraper, rule_id))
            # 删除当前规则下的所有通知邮箱记录
            conn.execute('DELETE FROM rule_emails WHERE rule_id = ?', (rule_id,))
            # 获取邮箱数据（前端将以多个输入框传递 emails[] 数组）
            emails = request.form.getlist("emails[]")
            for email in emails:
                email = email.strip()
                if email:
                    conn.execute('INSERT INTO rule_emails (rule_id, email, enabled) VALUES (?, ?, ?)',
                                 (rule_id, email, 1))
            conn.commit()
        flash('监控规则更新成功')
        return redirect(url_for('index'))
    # GET 请求时，查询当前规则下的通知邮箱配置
    with get_db() as conn:
        rule_emails = conn.execute('SELECT * FROM rule_emails WHERE rule_id = ?', (rule_id,)).fetchall()
    return render_template('edit_rule.html', rule=rule, rule_emails=rule_emails)

# 删除监控规则
@app.route('/delete/<int:rule_id>', methods=['POST'])
@login_required
def delete_rule(rule_id):
    with get_db() as conn:
        rule = conn.execute('SELECT * FROM rules WHERE id = ? AND user_id = ?', (rule_id, g.user['id'])).fetchone()
        if rule:
            conn.execute('DELETE FROM rules WHERE id = ?', (rule_id,))
            conn.commit()
    flash('监控规则已删除')
    return redirect(url_for('index'))

# 删除日志记录
@app.route('/delete_log/<int:log_id>', methods=['POST'])
@login_required
def delete_log(log_id):
    with get_db() as conn:
        log = conn.execute(
            'SELECT logs.id FROM logs JOIN rules ON logs.rule_id = rules.id WHERE logs.id = ? AND rules.user_id = ?',
            (log_id, g.user['id'])
        ).fetchone()
        if log:
            conn.execute('DELETE FROM logs WHERE id = ?', (log_id,))
            conn.commit()
            flash('日志记录已删除')
        else:
            flash('无法删除日志记录：权限不足或日志不存在')
    return redirect(request.referrer or url_for('index'))

# 查看规则变化日志
@app.route('/logs/<int:rule_id>')
@login_required
def view_logs(rule_id):
    with get_db() as conn:
        rule = conn.execute('SELECT * FROM rules WHERE id = ? AND user_id = ?', (rule_id, g.user['id'])).fetchone()
        if not rule:
            flash('无权限查看日志')
            return redirect(url_for('index'))
        logs = conn.execute('SELECT * FROM logs WHERE rule_id = ? ORDER BY timestamp DESC', (rule_id,)).fetchall()
    return render_template('logs.html', rule=rule, logs=logs)

# 切换规则启用/禁用（所有用户可操作自己的规则）
@app.route('/toggle_rule/<int:rule_id>', methods=['POST'])
@login_required
def toggle_rule(rule_id):
    with get_db() as conn:
        rule = conn.execute('SELECT * FROM rules WHERE id = ? AND user_id = ?', (rule_id, g.user['id'])).fetchone()
        if not rule:
            flash('规则不存在或无权限')
            return redirect(url_for('index'))
        new_status = 0 if rule['enabled'] == 1 else 1
        conn.execute('UPDATE rules SET enabled = ? WHERE id = ?', (new_status, rule_id))
        conn.commit()
    flash('规则状态更新成功')
    return redirect(url_for('index'))

# 新增接口：获取最新更新日志的时间戳
@app.route('/api/latest_update')
def api_latest_update():
    with get_db() as conn:
        latest_log = conn.execute('''
            SELECT logs.timestamp, rules.sound_url 
            FROM logs 
            JOIN rules ON logs.rule_id = rules.id 
            ORDER BY logs.timestamp DESC 
            LIMIT 1
        ''').fetchone()
    if latest_log:
        # 返回最新日志的时间戳和对应的提示音 URL（可能为空）
        return jsonify({'timestamp': latest_log['timestamp'], 'sound_url': latest_log['sound_url']})
    else:
        return jsonify({'timestamp': None, 'sound_url': None})

@csrf.exempt
@app.route('/batch_delete_logs', methods=['POST'])
def batch_delete_logs():
    data = request.get_json()
    if not data or 'ids' not in data:
        return jsonify({'success': False, 'msg': '参数错误'}), 400
    ids = data['ids']
    try:
        with get_db() as conn:
            for log_id in ids:
                conn.execute('DELETE FROM logs WHERE id = ?', (log_id,))
            conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        print("批量删除出错：", e)
        return jsonify({'success': False, 'msg': '删除异常'}), 500

if __name__ == '__main__':
    init_db()
    start_monitoring()
    app.run(host='0.0.0.0', port=8080, debug=False)
