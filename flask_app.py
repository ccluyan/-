import os
import csv
import io
import json
import time
import requests
from functools import wraps
from datetime import datetime
from flask import Flask, render_template_string, request, redirect, url_for, flash, jsonify, session, make_response
from flask_sqlalchemy import SQLAlchemy

app = Flask(__name__)
# 生产环境建议修改密钥
app.config['SECRET_KEY'] = 'my_super_secret_key_v4'
app.config['PASSWORD'] = '1234560' 

# --- 数据库配置 (升级到 v4) ---
basedir = os.path.abspath(os.path.dirname(__file__))
db_path = os.path.join(basedir, 'domains_v4.db')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + db_path
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# --- 模型定义 ---
class Domain(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    domain_name = db.Column(db.String(100), unique=True, nullable=False)
    registration_date = db.Column(db.String(50), default="")
    expiration_date = db.Column(db.String(50), default="")
    days_to_expire = db.Column(db.Integer, default=0)
    remark = db.Column(db.String(200), default="")
    
    # 状态
    is_online = db.Column(db.Boolean, default=False)
    status_code = db.Column(db.String(50), default="N/A") 
    response_time = db.Column(db.Integer, default=0)
    last_checked = db.Column(db.DateTime, default=datetime.utcnow)
    position = db.Column(db.Integer, default=0)

class Config(db.Model):
    """存储用户的配置信息 (单行表)"""
    id = db.Column(db.Integer, primary_key=True)
    # GitHub Gist
    gist_token = db.Column(db.String(200), default="")
    gist_id = db.Column(db.String(100), default="") # 自动记录第一次创建的ID
    # WebDAV
    webdav_url = db.Column(db.String(200), default="")
    webdav_user = db.Column(db.String(100), default="")
    webdav_pass = db.Column(db.String(100), default="")

# --- 辅助函数 ---
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def get_config():
    """获取配置，如果没有则创建默认"""
    conf = Config.query.first()
    if not conf:
        conf = Config()
        db.session.add(conf)
        db.session.commit()
    return conf

def check_website_detailed(domain):
    url = domain
    if not url.startswith('http'): url = f'http://{url}'
    headers = {'User-Agent': 'Mozilla/5.0 (DomainMonitor/1.0)'}
    try:
        start_time = time.time()
        r = requests.get(url, timeout=5, headers=headers, allow_redirects=True)
        duration = int((time.time() - start_time) * 1000)
        return True, str(r.status_code), duration
    except Exception as e:
        return False, "Error", 0

def calc_days(exp_date_str):
    if not exp_date_str: return 0
    try:
        exp = datetime.strptime(exp_date_str, '%Y-%m-%d')
        return (exp - datetime.now()).days
    except: return 0

# --- 路由 ---

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form['password'] == app.config['PASSWORD']:
            session['logged_in'] = True
            return redirect(url_for('index'))
        flash('密码错误')
    return render_template_string(LOGIN_TEMPLATE)

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))

@app.route('/')
@login_required
def index():
    domains = Domain.query.order_by(Domain.position.asc()).all()
    conf = get_config()
    
    stats = {
        'total': len(domains),
        'online': sum(1 for d in domains if d.is_online),
        'issue': sum(1 for d in domains if not d.is_online and d.status_code != 'N/A'),
        'soon': sum(1 for d in domains if d.days_to_expire < 30)
    }
    return render_template_string(HTML_TEMPLATE, domains=domains, stats=stats, config=conf)

# --- API: 域名操作 ---

@app.route('/api/add_bulk', methods=['POST'])
@login_required
def api_add_bulk():
    raw_text = request.form.get('domains', '')
    lines = raw_text.split('\n')
    count = 0
    max_pos = db.session.query(db.func.max(Domain.position)).scalar() or 0
    
    for line in lines:
        clean = line.strip().replace('http://', '').replace('https://', '').split('/')[0]
        if clean and '.' in clean:
            if not Domain.query.filter_by(domain_name=clean).first():
                max_pos += 1
                db.session.add(Domain(domain_name=clean, position=max_pos))
                count += 1
    db.session.commit()
    return jsonify({'status': 'success', 'count': count})

@app.route('/api/refresh/<int:id>', methods=['POST'])
@login_required
def api_refresh(id):
    d = Domain.query.get(id)
    if not d: return jsonify({'status':'error'})
    online, code, ms = check_website_detailed(d.domain_name)
    d.is_online = online
    d.status_code = code
    d.response_time = ms
    d.last_checked = datetime.utcnow()
    d.days_to_expire = calc_days(d.expiration_date)
    db.session.commit()
    return jsonify({'status': 'success', 'online': online, 'code': code, 'ms': ms})

@app.route('/api/delete/<int:id>', methods=['POST'])
@login_required
def api_delete(id):
    d = Domain.query.get(id)
    if d:
        db.session.delete(d)
        db.session.commit()
        return jsonify({'status':'success'})
    return jsonify({'status':'error'})

@app.route('/api/edit', methods=['POST'])
@login_required
def api_edit():
    d = Domain.query.get(request.form.get('id'))
    if d:
        d.domain_name = request.form.get('domain_name')
        d.remark = request.form.get('remark')
        d.registration_date = request.form.get('reg_date')
        d.expiration_date = request.form.get('exp_date')
        d.days_to_expire = calc_days(d.expiration_date)
        db.session.commit()
    return jsonify({'status':'success'})

@app.route('/api/reorder', methods=['POST'])
@login_required
def api_reorder():
    order_data = request.json.get('order', [])
    for idx, did in enumerate(order_data):
        d = Domain.query.get(did)
        if d: d.position = idx
    db.session.commit()
    return jsonify({'status':'success'})

# --- API: 配置与远程备份 (Gist/WebDAV) ---

@app.route('/api/save_config', methods=['POST'])
@login_required
def save_config():
    conf = get_config()
    conf.gist_token = request.form.get('gist_token', '')
    conf.webdav_url = request.form.get('webdav_url', '')
    conf.webdav_user = request.form.get('webdav_user', '')
    conf.webdav_pass = request.form.get('webdav_pass', '')
    db.session.commit()
    return jsonify({'status':'success', 'msg':'配置已保存'})

def get_backup_json():
    domains = Domain.query.all()
    return json.dumps([
        {'domain': d.domain_name, 'reg': d.registration_date, 'exp': d.expiration_date, 'remark': d.remark}
        for d in domains
    ], indent=2, ensure_ascii=False)

@app.route('/api/gist/<action>', methods=['POST'])
@login_required
def gist_action(action):
    conf = get_config()
    if not conf.gist_token:
        return jsonify({'status':'error', 'msg':'请先点击⚙️配置 Gist Token'})
    
    headers = {
        'Authorization': f'token {conf.gist_token}',
        'Accept': 'application/vnd.github.v3+json'
    }
    
    if action == 'export':
        content = get_backup_json()
        payload = {
            "description": "Domain Monitor Backup",
            "public": False,
            "files": {"domains_backup.json": {"content": content}}
        }
        
        try:
            # 如果已有ID，尝试更新 (PATCH)
            if conf.gist_id:
                r = requests.patch(f"https://api.github.com/gists/{conf.gist_id}", json=payload, headers=headers)
                if r.status_code == 404: # ID失效，转为新建
                    conf.gist_id = "" 
                else:
                    return jsonify({'status':'success', 'msg':'Gist 更新成功'})
            
            # 如果没有ID或更新失败，新建 (POST)
            if not conf.gist_id:
                r = requests.post("https://api.github.com/gists", json=payload, headers=headers)
                if r.status_code == 201:
                    conf.gist_id = r.json()['id']
                    db.session.commit()
                    return jsonify({'status':'success', 'msg':'新 Gist 创建成功'})
                
            return jsonify({'status':'error', 'msg': f'GitHub API Error: {r.status_code}'})
        except Exception as e:
            return jsonify({'status':'error', 'msg': str(e)})

    elif action == 'import':
        if not conf.gist_id: return jsonify({'status':'error', 'msg':'未找到绑定的 Gist ID，请先执行一次导出'})
        try:
            r = requests.get(f"https://api.github.com/gists/{conf.gist_id}", headers=headers)
            if r.status_code == 200:
                files = r.json()['files']
                if 'domains_backup.json' in files:
                    data = json.loads(files['domains_backup.json']['content'])
                    import_data_logic(data)
                    return jsonify({'status':'success', 'msg':'从 Gist 恢复成功'})
            return jsonify({'status':'error', 'msg':'获取 Gist 失败'})
        except Exception as e:
            return jsonify({'status':'error', 'msg': str(e)})

@app.route('/api/webdav/<action>', methods=['POST'])
@login_required
def webdav_action(action):
    conf = get_config()
    if not conf.webdav_url:
        return jsonify({'status':'error', 'msg':'请先点击⚙️配置 WebDAV 信息'})
    
    url = conf.webdav_url.rstrip('/') + '/domains_backup.json'
    auth = (conf.webdav_user, conf.webdav_pass)
    
    try:
        if action == 'export':
            data = get_backup_json()
            r = requests.put(url, data=data.encode('utf-8'), auth=auth)
            if r.status_code in [200, 201, 204]:
                return jsonify({'status':'success', 'msg':'WebDAV 上传成功'})
            return jsonify({'status':'error', 'msg':f'WebDAV Error: {r.status_code}'})
            
        elif action == 'import':
            r = requests.get(url, auth=auth)
            if r.status_code == 200:
                import_data_logic(r.json())
                return jsonify({'status':'success', 'msg':'从 WebDAV 恢复成功'})
            return jsonify({'status':'error', 'msg':f'WebDAV Error: {r.status_code}'})
    except Exception as e:
        return jsonify({'status':'error', 'msg':str(e)})

def import_data_logic(data_list):
    """通用导入逻辑"""
    count = 0
    for item in data_list:
        if 'domain' in item and not Domain.query.filter_by(domain_name=item['domain']).first():
            db.session.add(Domain(
                domain_name=item['domain'],
                remark=item.get('remark',''),
                registration_date=item.get('reg',''),
                expiration_date=item.get('exp',''),
                position=9999
            ))
            count += 1
    db.session.commit()

# --- 文件导入导出 ---

@app.route('/export/<fmt>')
@login_required
def export_file(fmt):
    domains = Domain.query.all()
    fname = f"backup_{datetime.now().strftime('%Y%m%d')}"
    if fmt == 'json':
        resp = make_response(get_backup_json())
        resp.headers['Content-Disposition'] = f'attachment; filename={fname}.json'
        resp.headers['Content-type'] = 'application/json'
        return resp
    # TXT/CSV逻辑略，保持旧版即可
    return "Format not supported", 400

@app.route('/import_file', methods=['POST'])
@login_required
def import_file():
    if 'file' not in request.files: return jsonify({'status':'error'})
    f = request.files['file']
    try:
        content = f.stream.read().decode("UTF8", errors='ignore')
        if f.filename.endswith('.json'):
            import_data_logic(json.loads(content))
        else: # TXT
            lines = content.splitlines()
            count = 0
            for line in lines:
                d = line.strip()
                if d and not Domain.query.filter_by(domain_name=d).first():
                    db.session.add(Domain(domain_name=d, position=9999))
                    count += 1
            db.session.commit()
        return jsonify({'status':'success', 'msg':'导入完成'})
    except Exception as e:
        return jsonify({'status':'error', 'msg':str(e)})

# 初始化
with app.app_context():
    db.create_all()

# --- 模板 ---

LOGIN_TEMPLATE = """
<!DOCTYPE html>
<html>
<head><meta name="viewport" content="width=device-width"><title>登录</title></head>
<body style="background:#121212;color:white;display:flex;justify-content:center;align-items:center;height:100vh;font-family:sans-serif;">
<form method="POST" style="background:#1e1e1e;padding:40px;border-radius:10px;text-align:center;">
<h3><i class="fas fa-shield-alt"></i> 域名监控</h3>
<input type="password" name="password" placeholder="请输入密码" style="padding:10px;margin:10px 0;width:200px;" required><br>
<button type="submit" style="padding:10px 20px;background:#6c5ce7;color:white;border:none;border-radius:5px;cursor:pointer;">进入</button>
</form></body></html>
"""

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="zh">
<head>
    <meta charset="UTF-8">
    <title>Domain Monitor Pro</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" rel="stylesheet">
    <script src="https://cdnjs.cloudflare.com/ajax/libs/Sortable/1.15.0/Sortable.min.js"></script>
    <style>
        :root { --bg:#121212; --card:#1e1e1e; --text:#e0e0e0; --accent:#6c5ce7; --danger:#d63031; --success:#00b894; }
        [data-theme="light"] { --bg:#f5f6fa; --card:#ffffff; --text:#2d3436; --accent:#0984e3; }
        [data-theme="cyber"] { --bg:#000; --card:#0a0a0a; --text:#0ff; --accent:#f0f; --success:#0f0; }

        body { background:var(--bg); color:var(--text); font-family:'Segoe UI', sans-serif; margin:0; padding:20px; min-height:100vh; transition:0.3s; }
        .container { max-width:1200px; margin:0 auto; }
        .navbar { display:flex; justify-content:space-between; align-items:center; background:var(--card); padding:15px; border-radius:15px; margin-bottom:30px; box-shadow:0 4px 10px rgba(0,0,0,0.1); }
        .btn { padding:8px 15px; border:none; border-radius:6px; cursor:pointer; color:white; display:inline-flex; align-items:center; gap:5px; text-decoration:none; font-size:14px; }
        .btn:hover { opacity:0.9; }
        .btn-primary { background:var(--accent); }
        .btn-danger { background:var(--danger); }
        .btn-success { background:var(--success); }
        .btn-grey { background:#636e72; }
        
        /* 设置面板 */
        .settings-grid { display:none; grid-template-columns:repeat(auto-fit, minmax(300px, 1fr)); gap:20px; margin-bottom:20px; }
        .config-card { background:rgba(255,255,255,0.05); padding:20px; border-radius:15px; border:1px solid rgba(255,255,255,0.1); }
        .config-header { display:flex; justify-content:space-between; align-items:center; margin-bottom:15px; font-weight:bold; font-size:1.1em; }
        .group { margin-bottom:15px; }
        .group-label { font-size:0.85em; color:#888; margin-bottom:8px; }
        .btn-group { display:flex; gap:8px; flex-wrap:wrap; }

        /* 表格 */
        .d-table { width:100%; border-collapse:collapse; background:var(--card); border-radius:12px; overflow:hidden; }
        th, td { padding:12px 15px; text-align:left; border-bottom:1px solid rgba(128,128,128,0.2); }
        .status-badge { padding:3px 8px; border-radius:4px; font-size:0.8em; }
        .badge-ok { background:rgba(0,184,148,0.2); color:var(--success); border:1px solid var(--success); }
        .badge-err { background:rgba(214,48,49,0.2); color:var(--danger); border:1px solid var(--danger); }
        
        /* 模态框 */
        .modal { display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.7); z-index:999; backdrop-filter:blur(3px); }
        .modal-content { background:var(--card); width:90%; max-width:450px; margin:10% auto; padding:25px; border-radius:15px; border:1px solid #444; }
        .modal input, .modal textarea { width:100%; padding:10px; margin:5px 0 15px 0; background:rgba(0,0,0,0.2); border:1px solid #555; color:var(--text); box-sizing:border-box; border-radius:5px; }
        
        @media(max-width:768px) { .hide-mobile { display:none; } }
    </style>
</head>
<body>

<div class="container">
    <div class="navbar">
        <div style="font-weight:bold; font-size:1.2em;"><i class="fas fa-server"></i> DomainMonitor</div>
        <div style="display:flex; gap:10px; align-items:center;">
            <select onchange="setTheme(this.value)" style="padding:5px; border-radius:5px;">
                <option value="default">🌑 深色</option>
                <option value="light">☀️ 浅色</option>
                <option value="cyber">🤖 赛博</option>
            </select>
            <button class="btn btn-grey" onclick="toggleSettings()">数据管理</button>
            <a href="/logout" class="btn btn-danger"><i class="fas fa-power-off"></i></a>
        </div>
    </div>

    <!-- 数据管理区域 -->
    <div id="settingsPanel" class="settings-grid">
        <!-- 本地导入导出 -->
        <div class="config-card">
            <div class="config-header">📁 本地数据</div>
            <div class="group">
                <div class="group-label">导出格式:</div>
                <div class="btn-group">
                    <a href="/export/json" class="btn btn-primary btn-sm">JSON</a>
                    <a href="/export/txt" class="btn btn-primary btn-sm">TXT</a>
                </div>
            </div>
            <div class="group">
                <div class="group-label">导入文件 (JSON/TXT):</div>
                <div class="btn-group">
                    <button onclick="document.getElementById('fileIn').click()" class="btn btn-success">选择文件导入</button>
                    <input type="file" id="fileIn" hidden onchange="uploadFile(this)">
                </div>
            </div>
        </div>

        <!-- GitHub Gist -->
        <div class="config-card">
            <div class="config-header">
                <span><i class="fab fa-github"></i> GitHub Gist</span>
                <button onclick="openConfigModal('gist')" class="btn btn-grey" style="font-size:0.8em">⚙️ 配置</button>
            </div>
            <div class="group-label">云端备份与恢复:</div>
            <div class="btn-group">
                <button onclick="cloudAction('gist','export')" class="btn btn-success" style="background:#2ecc71">备份到 Gist</button>
                <button onclick="cloudAction('gist','import')" class="btn btn-success" style="background:#2ecc71">从 Gist 恢复</button>
            </div>
            <div style="font-size:0.7em; margin-top:10px; color:#888;">ID: {{ config.gist_id or '未绑定' }}</div>
        </div>

        <!-- WebDAV -->
        <div class="config-card">
            <div class="config-header">
                <span><i class="fas fa-cloud"></i> WebDAV</span>
                <button onclick="openConfigModal('webdav')" class="btn btn-grey" style="font-size:0.8em">⚙️ 配置</button>
            </div>
            <div class="group-label">坚果云 / Nextcloud:</div>
            <div class="btn-group">
                <button onclick="cloudAction('webdav','export')" class="btn btn-primary" style="background:#0984e3">备份到 WebDAV</button>
                <button onclick="cloudAction('webdav','import')" class="btn btn-danger" style="background:#e17055">从 WebDAV 恢复</button>
            </div>
        </div>
    </div>

    <!-- 统计栏 -->
    <div style="display:grid; grid-template-columns:repeat(4,1fr); gap:15px; margin-bottom:20px; text-align:center;">
        <div class="config-card">
            <div style="color:#888">总数</div><div style="font-size:1.5em; font-weight:bold;">{{ stats.total }}</div>
        </div>
        <div class="config-card">
            <div style="color:#888">在线</div><div style="font-size:1.5em; color:var(--success); font-weight:bold;">{{ stats.online }}</div>
        </div>
        <div class="config-card">
            <div style="color:#888">异常</div><div style="font-size:1.5em; color:var(--danger); font-weight:bold;">{{ stats.issue }}</div>
        </div>
        <div class="config-card">
            <div style="color:#888">即将过期</div><div style="font-size:1.5em; color:#fdcb6e; font-weight:bold;">{{ stats.soon }}</div>
        </div>
    </div>

    <!-- 操作栏 -->
    <div style="margin-bottom:15px; display:flex; justify-content:space-between;">
        <div style="display:flex; gap:10px;">
            <button onclick="document.getElementById('addModal').style.display='block'" class="btn btn-primary"><i class="fas fa-plus"></i> 添加域名</button>
            <button onclick="batchRefresh()" class="btn btn-success" style="background:#0984e3"><i class="fas fa-sync"></i> 刷新状态</button>
        </div>
        <button onclick="batchDelete()" class="btn btn-danger"><i class="fas fa-trash"></i> 批量删除</button>
    </div>

    <!-- 列表 -->
    <div style="overflow-x:auto;">
        <table class="d-table">
            <thead>
                <tr>
                    <th width="30"><input type="checkbox" id="selectAll" onclick="toggleAll()"></th>
                    <th width="30"></th>
                    <th>域名 / 备注</th>
                    <th>状态</th>
                    <th class="hide-mobile">到期</th>
                    <th style="text-align:right">操作</th>
                </tr>
            </thead>
            <tbody id="domainList">
                {% for d in domains %}
                <tr data-id="{{ d.id }}">
                    <td><input type="checkbox" class="chk" value="{{ d.id }}"></td>
                    <td class="drag-handle" style="cursor:grab; color:#666;"><i class="fas fa-grip-lines"></i></td>
                    <td>
                        <div style="font-weight:bold; font-size:1.1em;">{{ d.domain_name }}</div>
                        <div style="font-size:0.8em; color:var(--accent);">{{ d.remark }}</div>
                    </td>
                    <td id="status-{{ d.id }}">
                        {% if d.is_online %}
                            <span class="status-badge badge-ok">200 OK</span> <small>{{ d.response_time }}ms</small>
                        {% elif d.status_code != 'N/A' %}
                            <span class="status-badge badge-err">{{ d.status_code }}</span>
                        {% else %}
                            <span style="color:#666">-</span>
                        {% endif %}
                    </td>
                    <td class="hide-mobile">
                        {% if d.days_to_expire < 30 %}
                            <span style="color:var(--danger)">{{ d.days_to_expire }} 天</span>
                        {% else %}
                            <span style="color:var(--success)">{{ d.days_to_expire }} 天</span>
                        {% endif %}
                        <div style="font-size:0.75em; color:#888;">{{ d.expiration_date }}</div>
                    </td>
                    <td style="text-align:right;">
                        <button class="btn btn-grey" style="padding:4px 8px;" onclick="safeCopy('{{ d.domain_name }}')"><i class="fas fa-copy"></i></button>
                        <button class="btn btn-primary" style="padding:4px 8px;" onclick="openEdit('{{ d.id }}', '{{ d.domain_name }}')"><i class="fas fa-edit"></i></button>
                        <button class="btn btn-danger" style="padding:4px 8px;" onclick="delOne({{ d.id }})"><i class="fas fa-trash"></i></button>
                    </td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>

</div>

<!-- 弹窗: 添加 -->
<div id="addModal" class="modal">
    <div class="modal-content">
        <h3>批量添加</h3>
        <p style="font-size:0.8em; color:#888;">一行一个，支持粘贴</p>
        <textarea id="bulkInput" rows="8"></textarea>
        <div style="text-align:right;">
            <button onclick="document.getElementById('addModal').style.display='none'" class="btn btn-grey">取消</button>
            <button onclick="submitAdd()" class="btn btn-primary">确定</button>
        </div>
    </div>
</div>

<!-- 弹窗: 编辑 -->
<div id="editModal" class="modal">
    <div class="modal-content">
        <h3>编辑域名</h3>
        <input type="hidden" id="editId">
        <label>域名</label><input type="text" id="editDomain">
        <label>备注</label><input type="text" id="editRemark">
        <label>注册日期</label><input type="text" id="editReg">
        <label>到期日期</label><input type="text" id="editExp">
        <div style="text-align:right;">
            <button onclick="document.getElementById('editModal').style.display='none'" class="btn btn-grey">取消</button>
            <button onclick="submitEdit()" class="btn btn-primary">保存</button>
        </div>
    </div>
</div>

<!-- 弹窗: 配置 (核心更新) -->
<div id="configModal" class="modal">
    <div class="modal-content">
        <h3>配置云端参数</h3>
        <form id="configForm">
            <div id="gistFields" style="display:none;">
                <label>GitHub Personal Access Token (Gist权限)</label>
                <input type="password" name="gist_token" value="{{ config.gist_token }}" placeholder="ghp_xxxxxxxx">
            </div>
            <div id="webdavFields" style="display:none;">
                <label>WebDAV 地址 (如 https://dav.jianguoyun.com/dav/)</label>
                <input type="text" name="webdav_url" value="{{ config.webdav_url }}">
                <label>账号</label>
                <input type="text" name="webdav_user" value="{{ config.webdav_user }}">
                <label>密码 (或应用密码)</label>
                <input type="password" name="webdav_pass" value="{{ config.webdav_pass }}">
            </div>
        </form>
        <div style="text-align:right; margin-top:15px;">
            <button onclick="document.getElementById('configModal').style.display='none'" class="btn btn-grey">取消</button>
            <button onclick="saveConfig()" class="btn btn-success">保存配置</button>
        </div>
    </div>
</div>

<script>
    // --- 修复后的复制功能 (核心更新) ---
    function safeCopy(text) {
        // 优先尝试现代API
        if (navigator.clipboard && window.isSecureContext) {
            navigator.clipboard.writeText(text).then(() => alert('已复制: ' + text));
        } else {
            // 兼容性后备方案：创建隐藏文本域
            let textArea = document.createElement("textarea");
            textArea.value = text;
            textArea.style.position = "fixed"; // 避免滚动到底部
            textArea.style.left = "-9999px";
            document.body.appendChild(textArea);
            textArea.focus();
            textArea.select();
            try {
                document.execCommand('copy');
                alert('已复制: ' + text);
            } catch (err) {
                alert('复制失败，请手动复制');
            }
            document.body.removeChild(textArea);
        }
    }

    // --- 配置与云端备份逻辑 ---
    function openConfigModal(type) {
        document.getElementById('configModal').style.display = 'block';
        document.getElementById('gistFields').style.display = (type==='gist'?'block':'none');
        document.getElementById('webdavFields').style.display = (type==='webdav'?'block':'none');
    }

    function saveConfig() {
        const form = document.getElementById('configForm');
        const fd = new FormData(form);
        fetch('/api/save_config', {method:'POST', body:fd})
        .then(r=>r.json())
        .then(res => {
            alert(res.msg);
            location.reload();
        });
    }

    function cloudAction(service, action) {
        const btn = event.target;
        const oldTxt = btn.innerText;
        btn.innerText = '执行中...';
        btn.disabled = true;

        fetch(`/api/${service}/${action}`, {method:'POST'})
        .then(r=>r.json())
        .then(res => {
            alert(res.msg);
            if(res.status === 'success' && action === 'import') location.reload();
        })
        .finally(() => {
            btn.innerText = oldTxt;
            btn.disabled = false;
        });
    }

    // --- 基础功能 ---
    function toggleSettings() {
        const p = document.getElementById('settingsPanel');
        p.style.display = (p.style.display==='grid'?'none':'grid');
    }
    
    function setTheme(t) {
        document.body.setAttribute('data-theme', t);
        localStorage.setItem('theme', t);
    }
    document.body.setAttribute('data-theme', localStorage.getItem('theme')||'default');

    function submitAdd() {
        const fd = new FormData();
        fd.append('domains', document.getElementById('bulkInput').value);
        fetch('/api/add_bulk', {method:'POST', body:fd}).then(r=>r.json()).then(res=>{
            alert('添加了 '+res.count+' 个'); location.reload();
        });
    }
    
    function batchRefresh() {
        if(!confirm('确定刷新所有选中的域名状态?')) return;
        const checks = document.querySelectorAll('.chk:checked');
        const list = checks.length ? checks : document.querySelectorAll('.chk');
        list.forEach((c, idx) => {
            setTimeout(() => {
                document.getElementById('status-'+c.value).innerHTML = '...';
                fetch('/api/refresh/'+c.value, {method:'POST'}).then(r=>r.json()).then(d=>{
                    const cls = d.online ? 'badge-ok' : 'badge-err';
                    const txt = d.online ? '200 OK' : d.code;
                    document.getElementById('status-'+c.value).innerHTML = `<span class="status-badge ${cls}">${txt}</span> <small>${d.ms}ms</small>`;
                });
            }, idx * 200);
        });
    }

    function delOne(id) { if(confirm('删除?')) fetch('/api/delete/'+id, {method:'POST'}).then(()=>location.reload()); }
    function batchDelete() {
        const checks = document.querySelectorAll('.chk:checked');
        if(!checks.length) return alert('未选择');
        if(confirm('删除选中的?')) {
            checks.forEach(c => fetch('/api/delete/'+c.value, {method:'POST'}));
            setTimeout(()=>location.reload(), 1000);
        }
    }
    
    function uploadFile(input) {
        const fd = new FormData(); fd.append('file', input.files[0]);
        fetch('/import_file', {method:'POST', body:fd}).then(r=>r.json()).then(res=>{
            alert(res.msg); location.reload();
        });
    }

    function toggleAll() {
        const val = document.getElementById('selectAll').checked;
        document.querySelectorAll('.chk').forEach(c=>c.checked=val);
    }
    
    // 拖拽排序
    new Sortable(document.getElementById('domainList'), {
        handle: '.drag-handle', animation: 150,
        onEnd: function() {
            const ids = [];
            document.querySelectorAll('tr[data-id]').forEach(tr=>ids.push(tr.getAttribute('data-id')));
            fetch('/api/reorder', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({order:ids})});
        }
    });

    // 简单的编辑弹窗逻辑 (实际使用需回显数据)
    function openEdit(id, name) {
        document.getElementById('editModal').style.display='block';
        document.getElementById('editId').value = id;
        document.getElementById('editDomain').value = name;
    }
    function submitEdit() {
        const fd = new FormData();
        fd.append('id', document.getElementById('editId').value);
        fd.append('domain_name', document.getElementById('editDomain').value);
        fd.append('remark', document.getElementById('editRemark').value);
        fd.append('reg_date', document.getElementById('editReg').value);
        fd.append('exp_date', document.getElementById('editExp').value);
        fetch('/api/edit', {method:'POST', body:fd}).then(()=>location.reload());
    }
    
    // 点击外部关闭弹窗
    window.onclick = function(e) {
        if(e.target.classList.contains('modal')) e.target.style.display='none';
    }
</script>

</body>
</html>
"""
