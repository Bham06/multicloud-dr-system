import os
from datetime import datetime
from flask import Flask, render_template, url_for, redirect, request, flash
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text  
from flask_bcrypt import Bcrypt
from flask_login import UserMixin, login_user, LoginManager, login_required, logout_user, current_user
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

#Environment configuration
ENV = os.getenv('FLASK_ENV', 'development')
IS_PRODUCTION = ENV == 'production'

#App configuration
secret_key = os.environ.get('SECRET_KEY')
if IS_PRODUCTION and not secret_key:
    raise ValueError("SECRET_KEY environment variable is required in production.")

database_url = os.environ.get('DATABASE_URL')
if IS_PRODUCTION and not database_url:
    raise ValueError("DATABASE_URL environment variable is requierd in production")

app.config['SECRET_KEY'] = secret_key
app.config['SQLALCHEMY_DATABASE_URI'] = database_url or 'sqlite:///tododb.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

if IS_PRODUCTION:
    app.config['DEBUG'] = False
else:
    app.config['DEBUG'] = True

db = SQLAlchemy(app)
bcrypt = Bcrypt(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message_category = "info"

#Database Models

#User model 
class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    tasks = db.relationship('Task', backref='owner', lazy=True)
    
#Task model 
class Task(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    completed = db.Column(db.Boolean, default=False)
    title = db.Column(db.String(200), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False) 
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

#Loading user
@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))    

#Initialise database
with app.app_context():
    db.create_all()

#Routes

#Home
@app.route('/')
def home():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

#Registration page route 
@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        
        if User.query.filter_by(username=username).first():
            flash("Username already exists")
            return render_template('register.html')
            
        hashed_password = bcrypt.generate_password_hash(password).decode('utf-8')
        user = User(username=username, password=hashed_password) 
        
        db.session.add(user)
        db.session.commit()
        
        flash("Account created, please login", "success")
        return redirect(url_for('login'))
        
    return render_template('register.html')

#Login route
@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        user = User.query.filter_by(username=username).first()
        
        if user and bcrypt.check_password_hash(user.password, password):
            login_user(user) 
            next_page = request.args.get('next')
            return redirect(next_page) if next_page else redirect(url_for('dashboard'))
        else:
            flash("Login failed!")
    
    return render_template('login.html')

#Dashboard route
@app.route('/dashboard')
@login_required
def dashboard():
    tasks = Task.query.filter_by(user_id=current_user.id).order_by(Task.created_at.desc()).all()
    return render_template('dashboard.html', tasks=tasks)

#Add tasks
@app.route('/add_task', methods=['POST'])
@login_required
def add_task():
    title = request.form['title']
    if title.strip():
        new_task = Task(title=title.strip(), user_id=current_user.id)
        db.session.add(new_task)
        db.session.commit()
        flash('Task added successfully!', 'success')
    else:
        flash('Task cannot be empty!', 'danger')
    
    return redirect(url_for('dashboard'))

#Toggle complete 
@app.route('/complete/<int:task_id>')
@login_required
def complete(task_id):
    task = Task.query.get_or_404(task_id)
    if task.user_id != current_user.id:
        flash('Not authorized!', 'danger')
        return redirect(url_for('dashboard'))
    
    task.completed = not task.completed
    db.session.commit()
    
    status = "completed" if task.completed else "marked incomplete"
    flash(f'Task {status}!', 'success')
    return redirect(url_for('dashboard'))

#Task deletion
@app.route('/delete/<int:task_id>')
@login_required
def delete(task_id):
    task = Task.query.get_or_404(task_id)
    
    if task.user_id != current_user.id:
        flash("Unauthorised delete!")
        return redirect(url_for('dashboard'))
    
    db.session.delete(task)
    db.session.commit()
    
    flash('Task successfully deleted')
    return redirect(url_for('dashboard'))

#Logout
@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash("You have been logged out!")
    return redirect(url_for('login'))

#Health check 
@app.route('/health')
def health_check():
    try:
        db.session.execute(db.text('SELECT 1'))
        return {
               'status': 'healthy',
               'environment': ENV,
               'database' : 'connected',
               'timestamp': datetime.utcnow().isoformat()
        }, 200 
    except Exception as e:
        return {'status': 'unhealthy', 'error' : str(e)}, 500 
    
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=not IS_PRODUCTION)