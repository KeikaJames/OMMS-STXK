import http.server
import socketserver
import json
import sqlite3
import urllib.parse
import os
import random
import string
from datetime import datetime
import csv
import threading
import time

# 数据库连接池实现
class DatabaseConnectionPool:
    def __init__(self, max_connections=5):
        self.max_connections = max_connections
        self.connections = []
        self.lock = threading.RLock()
        self.connection_count = 0
        
    def get_connection(self):
        with self.lock:
            # 尝试获取空闲连接
            while self.connections:
                conn = self.connections.pop()
                try:
                    # 测试连接是否可用
                    cursor = conn.cursor()
                    cursor.execute('SELECT 1')
                    return conn
                except sqlite3.Error:
                    # 连接已失效，关闭它
                    try:
                        DB_POOL.return_connection(conn)
                    except:
                        pass
                    self.connection_count -= 1
            
            # 如果没有空闲连接且未达到最大连接数，创建新连接
            if self.connection_count < self.max_connections:
                conn = sqlite3.connect('club_system.db', check_same_thread=False)
                self.connection_count += 1
                return conn
            
            # 如果达到最大连接数，等待一小段时间后重试
            time.sleep(0.01)
            return self.get_connection()
    
    def return_connection(self, conn):
        if conn:
            with self.lock:
                self.connections.append(conn)
    
    def close_all(self):
        with self.lock:
            while self.connections:
                conn = self.connections.pop()
                try:
                    conn.close()
                except:
                    pass
            self.connection_count = 0

# 创建数据库连接池
DB_POOL = DatabaseConnectionPool()

# 尝试导入pypinyin库来进行中文转拼音
# 如果没有安装，可以使用pip install pypinyin
# 如果导入失败，使用备用方案
try:
    from pypinyin import lazy_pinyin
    HAS_PYPINYIN = True
except ImportError:
    print("警告: pypinyin库未安装，将使用简化的拼音转换方案")
    HAS_PYPINYIN = False

# 创建数据库和表
conn = sqlite3.connect('club_system.db')
cursor = conn.cursor()

# 创建学生表
cursor.execute('''
CREATE TABLE IF NOT EXISTS students (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    class TEXT NOT NULL,
    student_id TEXT NOT NULL UNIQUE,
    username TEXT NOT NULL UNIQUE,
    password TEXT NOT NULL
)
''')

# 创建社团表
cursor.execute('''
CREATE TABLE IF NOT EXISTS clubs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    max_students INTEGER NOT NULL,
    current_students INTEGER DEFAULT 0
)
''')

# 创建报名表
cursor.execute('''
CREATE TABLE IF NOT EXISTS registrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL,
    club_id INTEGER NOT NULL,
    registration_time TEXT NOT NULL,
    FOREIGN KEY (student_id) REFERENCES students (id),
    FOREIGN KEY (club_id) REFERENCES clubs (id),
    UNIQUE (student_id)
)
''')

# 创建系统设置表
cursor.execute('''
CREATE TABLE IF NOT EXISTS settings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    registration_start_time TEXT,
    admin_username TEXT DEFAULT 'admin',
    admin_password TEXT DEFAULT 'admin123'
)
''')

# 初始化设置
cursor.execute('SELECT * FROM settings')
if not cursor.fetchone():
    cursor.execute('INSERT INTO settings (registration_start_time) VALUES (?)', 
                  (datetime.now().strftime('%Y-%m-%d %H:%M:%S'),))

conn.commit()
conn.close()

# 辅助函数：生成用户名（使用学生姓名，重复时加数字）
def generate_username(name):
    # 直接使用学生姓名作为用户名基础
    # 例如：张三 -> 张三，如有重复则为张三1、张三2等
    username = name
    
    # 移除空格，保留中文字符和其他有效字符
    username = ''.join(c for c in username if c.strip())
    
    # 检查是否重复
    conn = DB_POOL.get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM students WHERE username = ?', (username,))
    count = cursor.fetchone()[0]
    DB_POOL.return_connection(conn)
    
    # 如果有重复，添加数字后缀
    if count > 0:
        return f"{username}{count}"
    return username

# 辅助函数：生成简单随机密码（更简化的数字加字母组合）
def generate_password():
    # 根据需求修改为固定密码
    return '123456'

# 自定义请求处理器
class ClubSystemHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        # 处理带有查询参数的根路径请求
        path_without_query = self.path.split('?')[0]
        
        # 静态文件处理 - 优化版本
        # 检查请求的文件是否存在于当前目录
        if path_without_query != '/' and os.path.isfile(path_without_query[1:]):
            try:
                # 确定文件类型
                file_ext = os.path.splitext(path_without_query)[1].lower()
                content_types = {
                    '.mp4': 'video/mp4',
                    '.jpg': 'image/jpeg',
                    '.jpeg': 'image/jpeg',
                    '.png': 'image/png',
                    '.gif': 'image/gif',
                    '.css': 'text/css',
                    '.js': 'application/javascript',
                    '.html': 'text/html',
                    '.txt': 'text/plain'
                }
                
                content_type = content_types.get(file_ext, 'application/octet-stream')
                file_path = path_without_query[1:]
                
                # 优化：对于HTML文件，不添加缓存头，确保获取最新版本
                # 对于其他静态资源，添加缓存控制头
                cache_headers = {}
                if file_ext in ['.mp4', '.jpg', '.jpeg', '.png', '.gif', '.css', '.js']:
                    cache_headers['Cache-Control'] = 'public, max-age=3600'  # 1小时缓存
                
                file_size = os.path.getsize(file_path)
                
                # 支持Range请求（断点续传）
                range_header = self.headers.get('Range', '')
                if range_header:
                    # 处理部分内容请求
                    start, end = 0, file_size - 1
                    if range_header.startswith('bytes='):
                        range_val = range_header[6:]
                        if '-' in range_val:
                            start_str, end_str = range_val.split('-', 1)
                            if start_str:
                                start = int(start_str)
                            if end_str:
                                end = int(end_str)
                    
                    self.send_response(206)  # Partial Content
                    self.send_header('Content-Type', content_type)
                    self.send_header('Content-Range', f'bytes {start}-{end}/{file_size}')
                    self.send_header('Content-Length', str(end - start + 1))
                    # 添加缓存头
                    for key, value in cache_headers.items():
                        self.send_header(key, value)
                    self.end_headers()
                    
                    # 优化：使用更大的块大小提高性能
                    chunk_size = 65536  # 64KB chunks
                    with open(file_path, 'rb') as file:
                        file.seek(start)
                        remaining = end - start + 1
                        while remaining > 0:
                            chunk = file.read(min(chunk_size, remaining))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            remaining -= len(chunk)
                else:
                    # 常规请求，分块传输大文件
                    self.send_response(200)
                    self.send_header('Content-Type', content_type)
                    self.send_header('Content-Length', str(file_size))
                    # 添加缓存头
                    for key, value in cache_headers.items():
                        self.send_header(key, value)
                    self.end_headers()
                    
                    # 优化：使用更大的块大小提高性能
                    chunk_size = 65536  # 64KB chunks
                    with open(file_path, 'rb') as file:
                        while True:
                            chunk = file.read(chunk_size)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                return
            except ConnectionAbortedError:
                # 客户端中止连接是正常现象，特别是对于视频文件
                print(f"客户端中止了文件 {path_without_query} 的连接")
                return
            except Exception as e:
                print(f"提供静态文件失败: {str(e)}")
                # 避免在连接已关闭时尝试发送错误响应
                try:
                    self.send_error(500, f"提供文件失败: {str(e)}")
                except:
                    pass
                return
        
        # 原有路径处理逻辑
        if path_without_query == '/':
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            with open('login.html', 'rb') as file:
                self.wfile.write(file.read())
        elif path_without_query == '/student/dashboard':
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            with open('student_dashboard.html', 'rb') as file:
                self.wfile.write(file.read())
        elif path_without_query == '/student/profile':
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            with open('student_profile.html', 'rb') as file:
                self.wfile.write(file.read())
        elif path_without_query == '/admin/dashboard':
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            with open('admin_dashboard.html', 'rb') as file:
                self.wfile.write(file.read())
        elif path_without_query == '/api/check_registration_time':
            self._handle_check_registration_time()
        elif path_without_query == '/api/get_clubs':
            self._handle_get_clubs()
        elif path_without_query == '/api/get_student_info':
            self._handle_get_student_info()
        elif path_without_query == '/api/get_registrations':
            self._handle_get_registrations()
        elif path_without_query == '/api/get_all_students':
            self._handle_get_all_students()
        elif path_without_query == '/api/export_students_csv':
            self._handle_export_students_csv()
        elif path_without_query == '/api/export_all_data':
            self._handle_export_all_data()
        elif path_without_query == '/api/export_unregistered':
            self._handle_export_unregistered()
        elif path_without_query.startswith('/api/export_club_data'):
            # 解析club_id参数
            parsed_url = urllib.parse.urlparse(self.path)
            query_params = urllib.parse.parse_qs(parsed_url.query)
            club_id = query_params.get('club_id', [None])[0]
            if club_id:
                self._handle_export_club_data(club_id)
            else:
                self.send_error(400, "缺少club_id参数")
        else:
            self.send_error(404)
    
    def _get_request_data(self):
        content_length = int(self.headers['Content-Length'])
        post_data = self.rfile.read(content_length)
        return json.loads(post_data)
        
    def _handle_export_all_data(self):
        try:
            conn = DB_POOL.get_connection()
            cursor = conn.cursor()
            
            # 获取所有学生的报名情况
            cursor.execute('''
                SELECT s.name, s.class, s.student_id, COALESCE(c.name, '未报名') as club_name
                FROM students s
                LEFT JOIN registrations r ON s.id = r.student_id
                LEFT JOIN clubs c ON r.club_id = c.id
            ''')
            registrations = cursor.fetchall()
            
            conn.close()
            
            # 创建CSV内容（兼容Excel）
            import io
            output = io.StringIO()
            writer = csv.writer(output)
            
            # 写入表头
            writer.writerow(['姓名', '班级', '学号', '报名社团'])
            
            # 写入数据
            for reg in registrations:
                writer.writerow(reg)
            
            # 设置响应头
            self.send_response(200)
            self.send_header('Content-Type', 'application/vnd.ms-excel; charset=utf-8-sig')
            filename = f'registrations_{datetime.now().strftime("%Y%m%d_%H%M%S")}.xlsx'
            self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
            self.end_headers()
            
            # 写入响应内容
            self.wfile.write(output.getvalue().encode('utf-8-sig'))
            
        except Exception as e:
            print(f"导出报名数据失败: {str(e)}")
            self.send_error(500, f"导出失败: {str(e)}")
    
    def _handle_export_unregistered(self):
        try:
            conn = DB_POOL.get_connection()
            cursor = conn.cursor()
            
            # 获取未报名学生信息
            cursor.execute('''
                SELECT s.name, s.class, s.student_id
                FROM students s
                LEFT JOIN registrations r ON s.id = r.student_id
                WHERE r.id IS NULL
            ''')
            unregistered = cursor.fetchall()
            
            conn.close()
            
            # 创建CSV内容（兼容Excel）
            import io
            output = io.StringIO()
            writer = csv.writer(output)
            
            # 写入表头
            writer.writerow(['姓名', '班级', '学号'])
            
            # 写入数据
            for student in unregistered:
                writer.writerow(student)
            
            # 设置响应头
            self.send_response(200)
            self.send_header('Content-Type', 'application/vnd.ms-excel; charset=utf-8-sig')
            filename = f'unregistered_{datetime.now().strftime("%Y%m%d_%H%M%S")}.xlsx'
            self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
            self.end_headers()
            
            # 写入响应内容
            self.wfile.write(output.getvalue().encode('utf-8-sig'))
            
        except Exception as e:
            print(f"导出未报名学生数据失败: {str(e)}")
            self.send_error(500, f"导出失败: {str(e)}")
    
    def _handle_export_club_data(self, club_id):
        conn = None
        try:
            # 验证club_id参数
            if not club_id or not club_id.isdigit():
                self.send_error(400, "无效的club_id参数")
                return
            
            club_id = int(club_id)
            
            # 连接数据库
            conn = DB_POOL.get_connection()
            cursor = conn.cursor()
            
            # 获取社团名称
            cursor.execute('SELECT name FROM clubs WHERE id = ?', (club_id,))
            club = cursor.fetchone()
            if not club:
                if conn:
                    conn.close()
                self.send_error(404, "社团不存在")
                return
            
            club_name = club[0]
            
            # 获取该社团的学生信息
            cursor.execute('''
                SELECT s.name, s.class, s.student_id
                FROM students s
                JOIN registrations r ON s.id = r.student_id
                WHERE r.club_id = ?
            ''', (club_id,))
            students = cursor.fetchall()
            
            if conn:
                conn.close()
            
            # 创建CSV内容（兼容Excel）
            import io
            output = io.StringIO()
            writer = csv.writer(output)
            
            # 写入表头
            writer.writerow(['姓名', '班级', '学号'])
            
            # 写入数据
            for student in students:
                writer.writerow(student)
            
            # 安全处理文件名，移除或替换不适合的字符
            # 使用ASCII字符替换中文字符，避免Content-Disposition编码问题
            safe_filename = f'club_{club_id}'
            
            # 设置响应头
            self.send_response(200)
            self.send_header('Content-Type', 'application/vnd.ms-excel; charset=utf-8-sig')
            filename = f'{safe_filename}_{datetime.now().strftime("%Y%m%d_%H%M%S")}.xlsx'
            
            # 使用URL编码或ASCII文件名避免编码问题
            self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
            self.end_headers()
            
            # 确保响应已经发送
            self.wfile.flush()
            
            # 写入响应内容
            csv_content = output.getvalue()
            self.wfile.write(csv_content.encode('utf-8-sig'))
            
        except Exception as e:
            # 确保数据库连接已关闭
            if conn:
                try:
                    conn.close()
                except:
                    pass
            
            print(f"导出社团数据失败: {str(e)}")
            # 确保在异常情况下也发送错误响应
            try:
                self.send_error(500, f"导出失败: {str(e)}")
            except:
                pass  # 忽略响应已发送的错误
    
    def do_POST(self):
        # 获取请求路径，去除查询参数
        path_without_query = self.path.split('?')[0]
        
        # 处理API请求
        if path_without_query == '/api/login':
            data = self._get_request_data()
            self._handle_login(data)
        elif path_without_query == '/api/register_club':
            data = self._get_request_data()
            self._handle_register_club(data)
        elif path_without_query == '/api/cancel_registration':
            data = self._get_request_data()
            self._handle_cancel_registration(data)
        elif path_without_query == '/api/admin_login':
            data = self._get_request_data()
            self._handle_admin_login(data)
        elif path_without_query == '/api/import_students':
            data = self._get_request_data()
            self._handle_import_students(data)
        elif path_without_query == '/api/import_clubs':
            data = self._get_request_data()
            self._handle_import_clubs(data)
        elif path_without_query == '/api/update_registration_time':
            data = self._get_request_data()
            self._handle_update_registration_time(data)
        elif path_without_query == '/api/delete_student':
            data = self._get_request_data()
            self._handle_delete_student(data)
        elif path_without_query == '/api/delete_all_students':
            data = self._get_request_data()
            self._handle_delete_all_students(data)
        elif path_without_query == '/api/delete_club':
            data = self._get_request_data()
            self._handle_delete_club(data)
        elif path_without_query == '/api/delete_all_clubs':
            data = self._get_request_data()
            self._handle_delete_all_clubs(data)
        else:
            self.send_error(404)
    
    def _handle_login(self, data):
        # 优化登录处理，提高响应速度
        username = data.get('username')
        password = data.get('password')
        
        if not username or not password:
            self.send_response(400)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'success': False, 'message': '用户名和密码不能为空'}).encode())
            return
        
        conn = None
        try:
            conn = DB_POOL.get_connection()
            cursor = conn.cursor()
            # 优化查询：只选择必要的字段而不是所有字段
            cursor.execute('SELECT id, name, class, student_id FROM students WHERE username = ? AND password = ?', 
                         (username, password))
            student = cursor.fetchone()
            
            if student:
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                # 添加响应头以优化传输
                self.send_header('Cache-Control', 'no-cache')
                self.end_headers()
                self.wfile.write(json.dumps({
                    'success': True,
                    'student_id': student[0],
                    'name': student[1],
                    'class': student[2],
                    'student_no': student[3]
                }).encode())
            else:
                self.send_response(401)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'success': False, 'message': '用户名或密码错误'}).encode())
        except Exception as e:
            print(f"登录错误: {str(e)}")
            self.send_response(500)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'success': False, 'message': '服务器错误'}).encode())
        finally:
            # 确保连接总是被归还到池中
            if conn:
                DB_POOL.return_connection(conn)
    
    def _handle_check_registration_time(self):
        conn = DB_POOL.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT registration_start_time FROM settings ORDER BY id DESC LIMIT 1')
        setting = cursor.fetchone()
        DB_POOL.return_connection(conn)
        
        if setting:
            start_time = datetime.strptime(setting[0], '%Y-%m-%d %H:%M:%S')
            now = datetime.now()
            can_register = now >= start_time
            
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({
                'can_register': can_register,
                'start_time': setting[0]
            }).encode())
    
    def _handle_get_clubs(self):
        conn = DB_POOL.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM clubs')
        clubs = cursor.fetchall()
        DB_POOL.return_connection(conn)
        
        clubs_data = []
        for club in clubs:
            clubs_data.append({
                'id': club[0],
                'name': club[1],
                'max_students': club[2],
                'current_students': club[3]
            })
        
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(clubs_data).encode())
    
    def _handle_register_club(self, data):
        # 优化社团报名处理，添加参数验证和错误处理
        student_id = data.get('student_id')
        club_id = data.get('club_id')
        
        if not student_id or not club_id:
            self.send_response(400)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'success': False, 'message': '缺少必要参数'}).encode())
            return
        
        conn = None
        try:
            conn = DB_POOL.get_connection()
            cursor = conn.cursor()
            
            # 检查社团是否还有名额和存在性
            cursor.execute('SELECT current_students, max_students FROM clubs WHERE id = ?', (club_id,))
            club = cursor.fetchone()
            
            if not club:
                self.send_response(404)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'success': False, 'message': '社团不存在'}).encode())
                return
                
            if club[0] >= club[1]:
                self.send_response(400)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'success': False, 'message': '该社团已满员'}).encode())
                return
            
            # 检查学生是否已报名其他社团（使用更高效的查询）
            cursor.execute('SELECT 1 FROM registrations WHERE student_id = ?', (student_id,))
            if cursor.fetchone():
                self.send_response(400)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'success': False, 'message': '您已报名其他社团'}).encode())
                return
            
            # 开始事务
            conn.execute('BEGIN TRANSACTION')
            try:
                # 更新社团人数
                cursor.execute('UPDATE clubs SET current_students = current_students + 1 WHERE id = ?', (club_id,))
                # 插入报名记录
                cursor.execute('INSERT INTO registrations (student_id, club_id, registration_time) VALUES (?, ?, ?)', 
                              (student_id, club_id, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
                conn.commit()
                
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'success': True, 'message': '报名成功'}).encode())
            except sqlite3.Error as e:
                print(f"报名事务错误: {str(e)}")
                conn.rollback()
                self.send_response(500)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'success': False, 'message': '报名失败，请重试'}).encode())
        except Exception as e:
            print(f"报名错误: {str(e)}")
            self.send_response(500)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'success': False, 'message': '服务器错误'}).encode())
        finally:
            # 确保连接总是被归还
            if conn:
                DB_POOL.return_connection(conn)
    
    def _handle_cancel_registration(self, data):
        student_id = data.get('student_id')
        
        if not student_id:
            self.send_response(400)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'success': False, 'message': '缺少学生ID'}).encode())
            return
        
        conn = None
        try:
            conn = DB_POOL.get_connection()
            cursor = conn.cursor()
            
            # 获取报名的社团ID
            cursor.execute('SELECT club_id FROM registrations WHERE student_id = ?', (student_id,))
            registration = cursor.fetchone()
            
            if not registration:
                self.send_response(400)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'success': False, 'message': '您还未报名任何社团'}).encode())
                return
            
            club_id = registration[0]
            
            # 开始事务
            conn.execute('BEGIN TRANSACTION')
            try:
                # 删除报名记录
                cursor.execute('DELETE FROM registrations WHERE student_id = ?', (student_id,))
                # 更新社团人数
                cursor.execute('UPDATE clubs SET current_students = current_students - 1 WHERE id = ?', (club_id,))
                conn.commit()
                
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'success': True, 'message': '取消报名成功'}).encode())
            except sqlite3.Error as e:
                conn.rollback()
                print(f"取消报名事务错误: {str(e)}")
                self.send_response(500)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'success': False, 'message': '取消报名失败，请重试'}).encode())
        except Exception as e:
            print(f"取消报名错误: {str(e)}")
            self.send_response(500)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'success': False, 'message': '服务器错误'}).encode())
        finally:
            # 使用连接池的归还方法，而不是直接关闭
            if conn:
                DB_POOL.return_connection(conn)
    
    def _handle_get_student_info(self):
        # 优化学生信息获取，提高响应速度
        student_id = int(self.headers.get('X-Student-ID', 0))
        
        if not student_id:
            self.send_error(400, '缺少学生ID')
            return
        
        conn = None
        try:
            conn = DB_POOL.get_connection()
            cursor = conn.cursor()
            
            # 优化查询：只选择需要的字段
            cursor.execute('SELECT name, class, student_id, username FROM students WHERE id = ?', (student_id,))
            student = cursor.fetchone()
            
            if not student:
                self.send_error(404)
                return
            
            # 获取报名信息
            cursor.execute('''
                SELECT c.name, r.registration_time 
                FROM registrations r
                JOIN clubs c ON r.club_id = c.id
                WHERE r.student_id = ?
            ''', (student_id,))
            registration = cursor.fetchone()
            
            info = {
                'name': student[0],
                'class': student[1],
                'student_id': student[2],
                'username': student[3],
                'registered_club': registration[0] if registration else None,
                'registration_time': registration[1] if registration else None
            }
            
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            # 添加缓存控制头，避免缓存敏感数据
            self.send_header('Cache-Control', 'no-store, no-cache')
            self.end_headers()
            self.wfile.write(json.dumps(info).encode())
        except Exception as e:
            print(f"获取学生信息错误: {str(e)}")
            self.send_error(500)
        finally:
            # 使用连接池的归还方法
            if conn:
                DB_POOL.return_connection(conn)
    
    def _handle_admin_login(self, data):
        # 优化管理员登录处理
        username = data.get('username')
        password = data.get('password')
        
        if not username or not password:
            self.send_response(400)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'success': False, 'message': '用户名和密码不能为空'}).encode())
            return
        
        conn = None
        try:
            conn = DB_POOL.get_connection()
            cursor = conn.cursor()
            # 优化查询：只检查存在性，不需要返回所有字段
            cursor.execute('SELECT 1 FROM settings WHERE admin_username = ? AND admin_password = ?', (username, password))
            admin = cursor.fetchone()
            
            if admin:
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                # 添加响应头以优化传输
                self.send_header('Cache-Control', 'no-cache')
                self.end_headers()
                self.wfile.write(json.dumps({'success': True}).encode())
            else:
                self.send_response(401)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'success': False, 'message': '用户名或密码错误'}).encode())
        except Exception as e:
            print(f"管理员登录错误: {str(e)}")
            self.send_response(500)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'success': False, 'message': '服务器错误'}).encode())
        finally:
            # 确保连接总是被归还到池中
            if conn:
                DB_POOL.return_connection(conn)
    
    def _handle_get_registrations(self):
        # 优化获取所有注册信息，提高大量数据时的性能
        conn = None
        try:
            conn = DB_POOL.get_connection()
            cursor = conn.cursor()
            
            # 获取所有学生的报名情况
            cursor.execute('''
                SELECT s.name, s.class, s.student_id, c.name as club_name
                FROM students s
                LEFT JOIN registrations r ON s.id = r.student_id
                LEFT JOIN clubs c ON r.club_id = c.id
                ORDER BY s.class, s.name
            ''')
            registrations = cursor.fetchall()
            
            # 批量处理数据，避免大量小操作
            data = [
                {
                    'name': reg[0],
                    'class': reg[1],
                    'student_id': reg[2],
                    'club_name': reg[3] if reg[3] else '未报名'
                }
                for reg in registrations
            ]
            
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            # 添加缓存控制头
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            # 直接返回JSON数据，减少中间处理步骤
            self.wfile.write(json.dumps(data).encode())
        except Exception as e:
            print(f"获取注册信息错误: {str(e)}")
            self.send_error(500)
        finally:
            # 使用连接池的归还方法
            if conn:
                DB_POOL.return_connection(conn)
    
    def _handle_import_students(self, data):
        # 优化学生导入处理，使用事务批量操作
        students_data = data.get('students', [])
        
        if not students_data:
            self.send_response(400)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'success': 0, 'failed': 0, 'message': '没有学生数据'}).encode())
            return
        
        conn = None
        try:
            conn = DB_POOL.get_connection()
            cursor = conn.cursor()
            
            # 开始事务，减少磁盘I/O
            conn.execute('BEGIN TRANSACTION')
            
            results = {'success': 0, 'failed': 0}
            
            # 准备批量插入数据
            insert_data = []
            for student in students_data:
                name = student.get('name')
                class_name = student.get('class')
                student_id = student.get('student_id')
                
                if not name or not class_name or not student_id:
                    results['failed'] += 1
                    continue
                
                # 生成用户名和密码
                username = generate_username(name)
                password = generate_password()
                
                insert_data.append((name, class_name, student_id, username, password))
            
            # 批量插入，每批最多处理100条记录
            batch_size = 100
            for i in range(0, len(insert_data), batch_size):
                batch = insert_data[i:i+batch_size]
                try:
                    cursor.executemany(
                        'INSERT OR IGNORE INTO students (name, class, student_id, username, password) VALUES (?, ?, ?, ?, ?)',
                        batch
                    )
                    results['success'] += cursor.rowcount
                    results['failed'] += len(batch) - cursor.rowcount
                except Exception as e:
                    print(f"批量插入错误: {str(e)}")
                    results['failed'] += len(batch)
            
            # 提交事务
            conn.commit()
            
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(results).encode())
        except Exception as e:
            print(f"导入学生错误: {str(e)}")
            if conn:
                try:
                    conn.rollback()
                except:
                    pass
            self.send_response(500)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'success': 0, 'failed': len(students_data), 'message': '导入失败'}).encode())
        finally:
            if conn:
                DB_POOL.return_connection(conn)
    
    def _handle_import_clubs(self, data):
        # 优化社团导入处理，使用连接池和批量操作
        print("开始处理社团导入请求")
        clubs_data = data.get('clubs', [])
        
        if not clubs_data:
            self.send_response(400)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'success': 0, 'failed': 0, 'message': '没有社团数据'}).encode())
            return
        
        print(f"收到 {len(clubs_data)} 个社团数据")
        
        conn = None
        try:
            conn = DB_POOL.get_connection()
            cursor = conn.cursor()
            
            # 确保表结构正确
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS clubs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL,
                    max_students INTEGER NOT NULL,
                    current_students INTEGER DEFAULT 0
                )
            ''')
            print("确保clubs表存在")
            
            # 获取现有社团列表以避免重复检查
            cursor.execute("SELECT name FROM clubs")
            existing_clubs = {row[0] for row in cursor.fetchall()}  # 使用集合提高查找效率
            print(f"现有社团数量: {len(existing_clubs)}")
            
            # 开始事务
            conn.execute('BEGIN TRANSACTION')
            
            results = {'success': 0, 'failed': 0}
            
            # 准备批量插入数据
            insert_data = []
            for club in clubs_data:
                name = club.get('name', '').strip()
                max_students = club.get('max_students')
                
                # 更严格的验证
                if not name or max_students is None or max_students <= 0:
                    results['failed'] += 1
                    continue
                    
                # 检查是否已存在
                if name in existing_clubs:
                    results['failed'] += 1
                    continue
                    
                insert_data.append((name, max_students))
            
            # 批量插入社团数据
            if insert_data:
                try:
                    cursor.executemany(
                        'INSERT OR IGNORE INTO clubs (name, max_students) VALUES (?, ?)',
                        insert_data
                    )
                    results['success'] = cursor.rowcount
                    results['failed'] += len(insert_data) - cursor.rowcount
                    conn.commit()
                except Exception as e:
                    print(f"批量插入社团错误: {str(e)}")
                    conn.rollback()
                    results['failed'] += len(insert_data)
            
            print(f"社团导入完成: 成功 {results['success']}, 失败 {results['failed']}")
            
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(results).encode())
        except Exception as e:
            print(f"导入社团错误: {str(e)}")
            if conn:
                try:
                    conn.rollback()
                except:
                    pass
            self.send_response(500)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'success': 0, 'failed': len(clubs_data), 'message': '导入失败'}).encode())
        finally:
            if conn:
                DB_POOL.return_connection(conn)
        
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(results).encode())
    
    def _handle_update_registration_time(self, data):
        start_time = data.get('start_time')
        
        conn = sqlite3.connect('club_system.db')
        cursor = conn.cursor()
        
        try:
            cursor.execute('UPDATE settings SET registration_start_time = ?', (start_time,))
            conn.commit()
            
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'success': True}).encode())
        except sqlite3.Error:
            conn.rollback()
            self.send_response(500)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'success': False, 'message': '更新失败'}).encode())
        finally:
            conn.close()
    
    def _handle_get_all_students(self):
        conn = sqlite3.connect('club_system.db')
        cursor = conn.cursor()
        
        # 获取所有学生信息，包括用户名和密码
        cursor.execute('SELECT * FROM students')
        students = cursor.fetchall()
        conn.close()
        
        data = []
        for student in students:
            data.append({
                'id': student[0],
                'name': student[1],
                'class': student[2],
                'student_id': student[3],
                'username': student[4],
                'password': student[5]
            })
        
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())
    
    def _handle_export_students_csv(self):
        try:
            conn = sqlite3.connect('club_system.db')
            cursor = conn.cursor()
            
            # 查询所有学生信息
            cursor.execute('SELECT name, class, student_id, username, password FROM students')
            students = cursor.fetchall()
            
            conn.close()
            
            # 创建CSV内容（兼容Excel）
            import io
            output = io.StringIO()
            writer = csv.writer(output)
            
            # 写入表头
            writer.writerow(['姓名', '班级', '学号', '用户名', '密码'])
            
            # 写入数据
            for student in students:
                writer.writerow(student)
            
            # 设置响应头
            self.send_response(200)
            self.send_header('Content-Type', 'application/vnd.ms-excel; charset=utf-8-sig')
            filename = f'students_{datetime.now().strftime("%Y%m%d_%H%M%S")}.xlsx'
            self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
            self.end_headers()
            
            # 写入响应内容
            self.wfile.write(output.getvalue().encode('utf-8-sig'))
            
        except Exception as e:
            print(f"导出学生数据失败: {str(e)}")
            self.send_error(500, f"导出失败: {str(e)}")
    
    def _handle_delete_student(self, data):
        student_id = data.get('student_id')
        
        if not student_id:
            self.send_response(400)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'success': False, 'message': '缺少学生ID'}).encode())
            return
        
        conn = sqlite3.connect('club_system.db')
        cursor = conn.cursor()
        
        try:
            # 先删除相关的报名记录
            cursor.execute('DELETE FROM registrations WHERE student_id = ?', (student_id,))
            
            # 然后删除学生记录
            cursor.execute('DELETE FROM students WHERE id = ?', (student_id,))
            
            if cursor.rowcount == 0:
                conn.close()
                self.send_response(404)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'success': False, 'message': '学生不存在'}).encode())
                return
            
            conn.commit()
            conn.close()
            
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'success': True, 'message': '学生删除成功'}).encode())
            
        except sqlite3.Error as e:
            conn.rollback()
            conn.close()
            print(f"删除学生失败: {str(e)}")
            self.send_response(500)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'success': False, 'message': '删除失败: ' + str(e)}).encode())
    
    def _handle_delete_club(self, data):
        club_id = data.get('club_id')
        
        if not club_id:
            self.send_response(400)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'success': False, 'message': '缺少社团ID'}).encode())
            return
        
        conn = sqlite3.connect('club_system.db')
        cursor = conn.cursor()
        
        try:
            # 检查是否有学生正在报名该社团
            cursor.execute('SELECT COUNT(*) FROM registrations WHERE club_id = ?', (club_id,))
            count = cursor.fetchone()[0]
            
            if count > 0:
                conn.close()
                self.send_response(400)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'success': False, 'message': f'该社团还有{count}名学生报名，无法删除'}).encode())
                return
            
            # 删除社团记录
            cursor.execute('DELETE FROM clubs WHERE id = ?', (club_id,))
            
            if cursor.rowcount == 0:
                conn.close()
                self.send_response(404)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'success': False, 'message': '社团不存在'}).encode())
                return
            
            conn.commit()
            conn.close()
            
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'success': True, 'message': '社团删除成功'}).encode())
            
        except sqlite3.Error as e:
            conn.rollback()
            conn.close()
            print(f"删除社团失败: {str(e)}")
            self.send_response(500)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'success': False, 'message': '删除失败: ' + str(e)}).encode())
    
    def _handle_delete_all_students(self, data):
        # 验证是否有确认标志
        if not data.get('confirm'):
            self.send_response(400)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'success': False, 'message': '缺少确认标志'}).encode())
            return
        
        conn = sqlite3.connect('club_system.db')
        cursor = conn.cursor()
        
        try:
            # 先删除所有报名记录
            cursor.execute('DELETE FROM registrations')
            
            # 然后删除所有学生记录
            cursor.execute('DELETE FROM students')
            
            conn.commit()
            conn.close()
            
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'success': True, 'message': '所有学生数据已删除'}).encode())
            
        except sqlite3.Error as e:
            conn.rollback()
            conn.close()
            print(f"删除所有学生失败: {str(e)}")
            self.send_response(500)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'success': False, 'message': '删除失败: ' + str(e)}).encode())
    
    def _handle_delete_all_clubs(self, data):
        # 验证是否有确认标志
        if not data.get('confirm'):
            self.send_response(400)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'success': False, 'message': '缺少确认标志'}).encode())
            return
        
        conn = sqlite3.connect('club_system.db')
        cursor = conn.cursor()
        
        try:
            # 先删除所有报名记录
            cursor.execute('DELETE FROM registrations')
            
            # 然后删除所有社团记录
            cursor.execute('DELETE FROM clubs')
            
            conn.commit()
            conn.close()
            
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'success': True, 'message': '所有社团数据已删除'}).encode())
            
        except sqlite3.Error as e:
            conn.rollback()
            conn.close()
            print(f"删除所有社团失败: {str(e)}")
            self.send_response(500)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'success': False, 'message': '删除失败: ' + str(e)}).encode())

# ==============================================
# 服务器配置与启动部分
# 以下配置可以根据实际需求进行修改
# ==============================================
if __name__ == '__main__':
    # --------------------------------------------------
    # 服务器IP地址配置说明：
    # --------------------------------------------------
    # 1. "0.0.0.0" - 监听所有可用的网络接口
    #    - 这意味着服务器可以通过服务器的任何IP地址访问
    #    - 适用于部署在正式服务器上，允许其他设备访问
    #    - 请确保防火墙设置允许该端口的外部访问
    host = "0.0.0.0"
    
    # 2. "127.0.0.1" - 仅监听本地回环接口
    #    - 这意味着服务器只能在本机访问，无法从其他设备访问
    #    - 适用于开发和测试阶段，提高安全性
    # host = "127.0.0.1"
    
    # --------------------------------------------------
    # 服务器端口配置说明：
    # --------------------------------------------------
    # 常用的端口选择：
    # - 8000, 8080, 8888 - 常用的非特权端口，无需管理员权限
    # - 80 - HTTP标准端口，需要管理员权限（Linux/Mac）或管理员运行（Windows）
    # - 443 - HTTPS标准端口，需要管理员权限
    # 注意：确保所选端口未被其他程序占用
    port = 2001
    
    # 设置请求处理器
    handler = ClubSystemHandler
    
    # 优化：设置允许地址重用，避免端口占用问题
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    
    try:
        # 创建线程化TCP服务器实例以提高并发性能
        print("正在启动服务器...")
        print(f"服务器配置: IP={host}, 端口={port}")
        
        with socketserver.ThreadingTCPServer((host, port), handler) as httpd:
            # 根据配置输出不同的访问信息
            if host == "0.0.0.0":
                print(f"\n✅ 服务器启动成功!")
                print(f"📌 本地访问地址: http://localhost:{port}")
                print(f"🌐 网络访问地址: http://[服务器IP地址]:{port}")
                print("   (请将[服务器IP地址]替换为实际的服务器IP)")
                
                print("\n===========================================")
                print("🔧 详细部署说明:")
                print("===========================================")
                print("1. 环境准备:")
                print("   - 确保服务器已安装Python 3.6或更高版本")
                print("   - 检查方法: 在服务器命令行输入 'python --version'")
                print("\n2. 文件部署:")
                print("   - 将以下文件复制到服务器的同一目录:")
                print("     * main.py (主程序)")
                print("     * club_system.db (数据库文件)")
                print("     * login.html, admin_dashboard.html, student_dashboard.html, student_profile.html")
                print("\n3. 配置修改:")
                print("   - 编辑main.py文件中的host和port变量:")
                print(f"     * host = '0.0.0.0'  # 允许所有网络访问")
                print(f"     * port = {port}  # 可根据需要修改端口号")
                print("\n4. 防火墙设置:")
                print("   - Windows服务器: 在Windows防火墙中添加入站规则允许端口访问")
                print("   - Linux服务器: 使用ufw或iptables开放端口，例如:")
                print(f"     'sudo ufw allow {port}/tcp'")
                print("\n5. 启动服务:")
                print("   - 前台运行(适用于测试):")
                print("     'python main.py'")
                print("   - 后台运行(适用于正式部署):")
                print("     Windows: 使用'nssm'工具或创建系统服务")
                print("     Linux: 'nohup python main.py > server.log 2>&1 &'")
                print("\n6. 验证服务:")
                print("   - 在浏览器中访问: http://服务器IP地址:端口号")
                print("   - 检查日志确认服务正常运行")
                print("===========================================")
                
            else:
                print(f"\n✅ 服务器启动成功!")
                print(f"📌 仅本地访问地址: http://localhost:{port}")
                print("   (注意: 当前配置下，其他设备无法访问此服务器)")
            
            print(f"\n🟢 服务器正在运行中，按 Ctrl+C 停止服务")
            # 启动服务器，持续监听请求
            httpd.serve_forever()
            
    except KeyboardInterrupt:
        print("\n⏹️  服务器已被用户停止")
        # 关闭所有数据库连接
        DB_POOL.close_all()
    except OSError as e:
        print(f"❌ 服务器启动失败: 端口 {port} 可能已被占用")
        print(f"   错误信息: {str(e)}")
        print(f"   请尝试修改端口号或停止占用该端口的程序")
        # 关闭所有数据库连接
        DB_POOL.close_all()
    except Exception as e:
        print(f"❌ 服务器启动时发生错误: {str(e)}")
        # 关闭所有数据库连接
        DB_POOL.close_all()