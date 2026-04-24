import docker
import os
import pty
import subprocess
import select
import time
import threading
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash
from flask_socketio import SocketIO, disconnect
from werkzeug.utils import secure_filename
from werkzeug.security import check_password_hash
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-very-secret-key-change-this'

# --- SHARED POSTGRESQL DATABASE CONFIGURATION ---
app.config['SQLALCHEMY_DATABASE_URI'] = 'postgresql://lab_user:supersecret@localhost:5432/linux_lab_db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

socketio = SocketIO(app, async_mode='threading', cors_allowed_origins="*")
client = docker.from_env()

sessions = {}

# --- DATABASE MODEL ---
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(150), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    last_login = db.Column(db.DateTime, default=datetime.utcnow)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

with app.app_context():
    db.create_all()

# --- 1-WEEK INACTIVITY CLEANUP TASK ---
def cleanup_inactive_containers():
    while True:
        with app.app_context():
            cutoff_date = datetime.utcnow() - timedelta(days=7)
            inactive_users = User.query.filter(User.last_login < cutoff_date).all()
            
            for user in inactive_users:
                container_name = f"student_{user.username}"
                try:
                    container = client.containers.get(container_name)
                    container.remove(force=True)
                    print(f"[CLEANUP] Deleted inactive container for user: {user.username}")
                except docker.errors.NotFound:
                    pass 
                except Exception as e:
                    print(f"[CLEANUP ERROR] Failed to remove {container_name}: {e}")
            
        time.sleep(86400) # Wait 24 hours before checking again

# --- AUTHENTICATION ROUTES ---
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = User.query.filter_by(username=username).first()
        
        if user and user.check_password(password):
            user.last_login = datetime.utcnow()
            db.session.commit()
            
            login_user(user)
            return redirect(url_for('index'))
        else:
            flash("Invalid username or password.")
            
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

# --- PROTECTED APP ROUTES ---
@app.route('/')
@login_required
def index():
    return render_template('terminal.html')

@socketio.on("connect")
def on_connect():
    if not current_user.is_authenticated:
        print("Unauthorized socket connection attempt.")
        return False 

    sid = request.sid
    print(f"[{sid}] Student {current_user.username} connecting...")
    
    container_name = f"student_{current_user.username}"
    
    try:
        try:
            container = client.containers.get(container_name)
            if container.status != 'running':
                print(f"[{sid}] Waking up existing container for {current_user.username}...")
                container.start()
            else:
                print(f"[{sid}] Container for {current_user.username} is already running.")
                
        except docker.errors.NotFound:
            print(f"[{sid}] Creating fresh Ubuntu container for {current_user.username}...")
            container = client.containers.run(
                "ubuntu:latest",
                detach=True,
                tty=True,
                stdin_open=True,
                command="/bin/bash",
                mem_limit="512m",
                name=container_name,
                extra_hosts={"host.docker.internal": "host-gateway"},
                ports={'8000/tcp': ('127.0.0.1', None)}
            )
        
        master_fd, slave_fd = pty.openpty()
        
        process = subprocess.Popen(
            ["docker", "exec", "-it", container.name, "bash"],
            preexec_fn=os.setsid,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            universal_newlines=True,
            env={**os.environ, "TERM": "xterm-256color"}
        )
        
        sessions[sid] = {
            "container": container,
            "fd": master_fd,
            "process": process,
            "user": current_user.username
        }

        socketio.start_background_task(stream_output, sid, master_fd)
        
    except Exception as e:
        print(f"[{sid}] Error: {e}")
        socketio.emit("output", f"\r\nError handling container: {str(e)}\r\n", room=sid)

def stream_output(sid, fd):
    while sid in sessions:
        r, _, _ = select.select([fd], [], [], 0.1)
        if r:
            try:
                output = os.read(fd, 4096).decode(errors='ignore')
                socketio.emit("output", output, room=sid)
            except OSError:
                break

@socketio.on("input")
def on_input(data):
    if request.sid in sessions:
        fd = sessions[request.sid]["fd"]
        try:
            os.write(fd, data.encode())
        except OSError:
            pass

# --- FILE UPLOAD ---
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024

@app.errorhandler(413)
def request_entity_too_large(error):
    return jsonify({"msg": "Upload Failed: File exceeds the 500MB limit."}), 413

@app.route('/upload', methods=['POST'])
@login_required
def upload_to_container():
    if 'file' not in request.files: return jsonify({"msg": "No file"}), 400
    
    sid = request.form.get('sid')
    file = request.files['file']
    filename = secure_filename(file.filename)
    temp_path = os.path.join("/tmp", filename)
    file.save(temp_path)

    if sid in sessions and sessions[sid].get("user") == current_user.username:
        container_name = sessions[sid]['container'].name
        # Changed destination to /root/ for standard Ubuntu
        subprocess.run(["docker", "cp", temp_path, f"{container_name}:/root/{filename}"])
        os.remove(temp_path)
        return jsonify({"msg": f"Success! File uploaded to /root/{filename}"})
    else:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        return jsonify({"msg": "Upload Failed: Unauthorized or Session not found."}), 403

@socketio.on("disconnect")
def on_disconnect():
    sid = request.sid
    data = sessions.pop(sid, None)
    if data:
        print(f"[{sid}] Student {data.get('user')} disconnected terminal.")
        try:
            data["process"].terminate()
        except Exception as e:
            print(f"Cleanup error: {e}")

if __name__ == '__main__':
    cleanup_thread = threading.Thread(target=cleanup_inactive_containers, daemon=True)
    cleanup_thread.start()
    
    socketio.run(app, host='0.0.0.0', port=8501, debug=True)