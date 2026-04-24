import os
import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
from flask import Flask, render_template, request, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash
from flask_login import UserMixin
from datetime import datetime 

# --- DATABASE CONNECTION DETAILS ---
DB_USER = 'lab_user'
DB_PASS = 'supersecret'
DB_HOST = 'localhost'
DB_PORT = '5432'
DB_NAME = 'linux_lab_db'

def create_database_if_not_exists():
    """Checks if the database exists in PostgreSQL and creates it if it doesn't."""
    try:
        # Connect to the default 'postgres' database to issue server-level commands
        conn = psycopg2.connect(
            dbname='postgres', 
            user=DB_USER, 
            password=DB_PASS, 
            host=DB_HOST, 
            port=DB_PORT
        )
        # Postgres requires autocommit to be True to create a database
        conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
        cursor = conn.cursor()

        # Check if our target database exists
        cursor.execute(f"SELECT 1 FROM pg_catalog.pg_database WHERE datname = '{DB_NAME}'")
        exists = cursor.fetchone()

        if not exists:
            print(f"[INIT] Database '{DB_NAME}' not found. Creating it now...")
            cursor.execute(f"CREATE DATABASE {DB_NAME}")
            print(f"[INIT] Database '{DB_NAME}' created successfully.")
        else:
            print(f"[INIT] Database '{DB_NAME}' already exists.")

        cursor.close()
        conn.close()
    except Exception as e:
        print(f"[ERROR] Failed to check or create database: {e}")

# 1. Run the database check/creation FIRST
create_database_if_not_exists()

# 2. Initialize Flask and SQLAlchemy SECOND
app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-very-secret-key-change-this'

# --- SHARED POSTGRESQL DATABASE CONFIGURATION ---
app.config['SQLALCHEMY_DATABASE_URI'] = f'postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# --- DATABASE MODEL ---
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(150), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    last_login = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

# 3. Create the Tables THIRD (inside the newly confirmed database)
with app.app_context():
    db.create_all()
    print(f"[INIT] Tables verified/created in '{DB_NAME}'.")

@app.route('/')
def home():
    return redirect(url_for('register'))

# --- REGISTRATION ROUTE ---
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        if User.query.filter_by(username=username).first():
            flash("Username already exists. Please choose a different one.")
            return redirect(url_for('register'))
            
        new_user = User(username=username)
        new_user.set_password(password)
        
        db.session.add(new_user)
        db.session.commit()
        
        flash("Registration successful. You can now log in on the main app.")
        return redirect(url_for('register')) 

    return render_template('register.html')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5006)