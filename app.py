from flask import Flask, render_template, request, redirect, url_for, flash, session, g, Response
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import psycopg
from psycopg.rows import dict_row
import HRISsystem as hris
from io import StringIO
import csv
import os
import random
import requests
import threading
import time
from datetime import datetime

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'global-payments-hris-2026')

# --- TRANSACTION POOLER CONNECTION STRING (IPv4 - Port 6543) ---
DATABASE_URL = os.environ.get(
    'DATABASE_URL',
    'postgresql://postgres.hudetzzomizjnygxkjqu:Keithleyvien0516@aws-0-ap-southeast-1.pooler.supabase.com:6543/postgres?sslmode=require'
)

# --- BREVO REST API CONFIGURATION ---
BREVO_API_KEY = os.environ.get('BREVO_API_KEY', '')
SENDER_EMAIL = os.environ.get('SENDER_EMAIL', 'no-reply@globalpayments.com')

def send_otp_email_worker(receiver_email, otp, intent):
    """Background worker for sending OTP emails via Brevo API without blocking HTTP response."""
    if not BREVO_API_KEY or 'YOUR-BREVO' in BREVO_API_KEY:
        print("API Error: Missing or default BREVO_API_KEY.")
        return

    url = "https://api.brevo.com/v3/smtp/email"
    headers = {
        "accept": "application/json",
        "api-key": BREVO_API_KEY,
        "content-type": "application/json"
    }
    payload = {
        "sender": {
            "name": "Global Payments HRIS",
            "email": SENDER_EMAIL
        },
        "to": [{"email": receiver_email}],
        "subject": f"Global Payments HRIS - {intent} OTP",
        "textContent": f"Your {intent} One-Time Password (OTP) is: {otp}\n\nThis OTP is valid for 5 minutes. Please enter this code to proceed. Do not share this code with anyone."
    }

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=15)
        print(f"Brevo API Email Sent. Status Code: {response.status_code}, Response: {response.text}")
    except Exception as e:
        print(f"Brevo API Request Error: {e}")

UPLOAD_FOLDER = os.path.join('static', 'uploads', 'profile')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

hris.init_system()

def migrate_users_table():
    """Migrates schema to support detailed registration fields, shift assignments, leave balances, and loans."""
    try:
        conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS employee_id TEXT DEFAULT '';")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS job_title TEXT DEFAULT '';")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS department TEXT DEFAULT '';")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS hourly_rate NUMERIC DEFAULT 0;")
            
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS daily_rate NUMERIC DEFAULT 0;")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS personal_number TEXT DEFAULT '';")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS hire_date DATE;")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS country TEXT DEFAULT 'Philippines';")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS personal_address TEXT DEFAULT '';")
            
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS shift_start TIME DEFAULT '09:00:00';")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS shift_end TIME DEFAULT '18:00:00';")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS rest_days TEXT DEFAULT 'Saturday,Sunday';")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS work_location TEXT DEFAULT 'Cubao Quezon City';")
            
            # --- LEAVE BALANCE FIELDS ---
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS vl_balance NUMERIC DEFAULT 15;")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS sl_balance NUMERIC DEFAULT 15;")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS el_balance NUMERIC DEFAULT 5;")
            
            conn.commit()
        conn.close()
    except Exception as e:
        print("Migration Warning:", e)

def create_loans_table():
    try:
        conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS loan_applications (
                    id SERIAL PRIMARY KEY,
                    user_id INT REFERENCES users(id) ON DELETE CASCADE,
                    loan_type TEXT NOT NULL,
                    amount NUMERIC NOT NULL,
                    monthly_amortization NUMERIC NOT NULL,
                    reason TEXT,
                    status TEXT DEFAULT 'Pending',
                    date_applied TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            conn.commit()
        conn.close()
    except Exception as e:
        print("Loan Table Migration Error:", e)

migrate_users_table()
create_loans_table()

def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    return db

@app.teardown_appcontext
def close(exception):
    db = getattr(g, '_database', None)
    if db:
        db.close()

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect('/dashboard')
    return redirect('/login')

@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        db = get_db()
        with db.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE email=%s", (request.form['email'].strip(),))
            user = cur.fetchone()
        if user and check_password_hash(user['password'], request.form['password']):
            session['user_id'] = user['id']
            session['role'] = user['role']
            session['fullname'] = user['fullname']
            session['user_email'] = user['email']
            session['employee_id'] = user.get('employee_id', '')
            return redirect('/dashboard')
        flash('Invalid email or password', 'danger')
    return render_template('login.html')

@app.route('/register', methods=['GET','POST'])
def register():
    if request.method == 'GET':
        return render_template('register.html')

    employee_id = request.form.get('employee_id', '').strip()
    fullname = request.form.get('fullname', '').strip()
    email = request.form.get('email', '').strip()
    password = request.form.get('password', '').strip()
    role = request.form.get('role', 'employee').strip()
    department = request.form.get('department','').strip()
    job_title = request.form.get('job_title','').strip()
    
    daily_rate = request.form.get('daily_rate', 0)
    personal_number = request.form.get('personal_number', '').strip()
    hire_date = request.form.get('hire_date') or None
    country = request.form.get('country', 'Philippines').strip()
    personal_address = request.form.get('personal_address', '').strip()
    
    shift_start = request.form.get('shift_start', '09:00').strip()
    shift_end = request.form.get('shift_end', '18:00').strip()
    work_location = request.form.get('work_location', 'Cubao Quezon City').strip()
    
    rest_days_list = request.form.getlist('rest_days')
    rest_days = ", ".join(rest_days_list) if rest_days_list else "Saturday,Sunday"

    if len(password) < 8:
        flash('Password must be at least 8 characters', 'danger')
        return render_template('register.html')

    if not department or not job_title:
        flash('Please select Department and Job Title', 'danger')
        return render_template('register.html')

    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("""
                INSERT INTO users (
                    employee_id, fullname, email, password, role, 
                    job_title, department, daily_rate, personal_number, 
                    hire_date, country, personal_address, 
                    shift_start, shift_end, rest_days, work_location
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                employee_id, fullname, email,
                generate_password_hash(password), role,
                job_title, department, daily_rate, personal_number,
                hire_date, country, personal_address,
                shift_start, shift_end, rest_days, work_location
            ))
            db.commit()
        
        flash("Employee account successfully created!", "success")
        if 'user_id' in session:
            return redirect(url_for("dashboard"))
        return redirect(url_for("login"))
    except Exception as e:
        print("Registration Error:", e)
        flash("Email or Employee ID already exists in the system.", "danger")
        return render_template("register.html")

@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        db = get_db()
        with db.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE email=%s", (email,))
            user = cur.fetchone()
        
        if user:
            otp = str(random.randint(100000, 999999))
            session['pending_reset'] = {
                'email': email,
                'otp': otp,
                'otp_expiry': time.time() + 300
            }
            threading.Thread(target=send_otp_email_worker, args=(email, otp, "Password Reset")).start()
            flash("We sent a password reset OTP code to your email address.", "info")
            return redirect(url_for("verify_otp", action="reset"))
        else:
            flash("Email address not found in the system.", "danger")
            
    return render_template('forgot.html')

@app.route('/reset-password', methods=['GET', 'POST'])
def reset_password():
    if 'pending_reset_verified' not in session:
        flash("Unauthorized access. Please request a password reset first.", "warning")
        return redirect(url_for('forgot_password'))

    if request.method == 'POST':
        new_password = request.form.get('password', '').strip()
        confirm_password = request.form.get('confirm_password', '').strip()

        if len(new_password) < 8:
            flash('Password must be at least 8 characters long.', 'danger')
            return render_template('reset_password.html')

        if confirm_password and new_password != confirm_password:
            flash('Passwords do not match.', 'danger')
            return render_template('reset_password.html')

        email = session['pending_reset_verified']['email']
        db = get_db()
        try:
            with db.cursor() as cur:
                cur.execute(
                    "UPDATE users SET password = %s WHERE email = %s",
                    (generate_password_hash(new_password), email)
                )
                db.commit()
            session.pop('pending_reset_verified', None)
            flash("Password successfully updated! Please log in with your new password.", "success")
            return redirect(url_for('login'))
        except Exception as e:
            print("Reset Password Error:", e)
            flash("Failed to update password. Please try again.", "danger")

    return render_template('reset_password.html')

@app.route('/verify-otp/<action>', methods=['GET', 'POST'])
def verify_otp(action):
    if action != 'reset':
        return redirect(url_for('login'))

    if 'pending_reset' not in session:
        flash("Session expired or invalid access. Please try again.", "warning")
        return redirect(url_for("forgot_password"))

    if request.method == "POST":
        user_otp = request.form.get("otp_code", "").strip()
        data = session['pending_reset']

        if time.time() > data.get('otp_expiry', 0):
            flash("The OTP code has expired. Please try again.", "danger")
            return render_template("otp_verify.html", action_url=url_for('verify_otp', action=action), action=action)

        if user_otp == data['otp']:
            session['pending_reset_verified'] = session.pop('pending_reset')
            flash("OTP verified successfully. Please enter your new password.", "success")
            return redirect(url_for("reset_password"))
        else:
            flash("Invalid OTP code. Try again.", "danger")

    return render_template("otp_verify.html", action_url=url_for('verify_otp', action=action), action=action)

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')

@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        return redirect('/login')

    db = get_db()
    q = request.args.get('q', '')

    now = datetime.now()
    current_month_year = now.strftime('%B %Y')

    try:
        with db.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE id=%s", (session['user_id'],))
            current_user = cur.fetchone()

            if not current_user:
                current_user = {
                    'fullname': session.get('fullname', 'Employee Profile'),
                    'role': session.get('role', 'employee'),
                    'email': session.get('user_email', '')
                }

            cur.execute("SELECT * FROM users WHERE fullname ILIKE %s ORDER BY id DESC", (f"%{q}%",))
            employees = cur.fetchall() or []

            cur.execute("SELECT COUNT(*) as c FROM users WHERE role='employee'")
            t_res = cur.fetchone()
            total_employees = t_res['c'] if (t_res and 'c' in t_res) else 0

            cur.execute("SELECT COUNT(*) as c FROM leave_applications WHERE status='Pending'")
            l_res = cur.fetchone()
            pending_leaves_count = l_res['c'] if (l_res and 'c' in l_res) else 0

            cur.execute("SELECT SUM(net_pay) as c FROM payroll_records")
            p_res = cur.fetchone()
            total_payroll = p_res['c'] if (p_res and 'c' in p_res and p_res['c'] is not None) else 0.0

            cur.execute("SELECT * FROM leave_applications WHERE user_id=%s ORDER BY date_created DESC", (session['user_id'],))
            my_leaves = cur.fetchall() or []

            cur.execute("SELECT * FROM loan_applications WHERE user_id=%s ORDER BY date_applied DESC", (session['user_id'],))
            my_loans = cur.fetchall() or []

            cur.execute("""
                SELECT l.*, u.fullname, u.department, u.job_title FROM leave_applications l
                JOIN users u ON u.id=l.user_id
                WHERE l.status='Pending' ORDER BY l.date_created DESC
            """)
            pending_leaves_list = cur.fetchall() or []

            cur.execute("""
                SELECT t.*, u.fullname, u.department, u.employee_id, u.work_location as lob, u.shift_start 
                FROM timekeeping t
                JOIN users u ON u.id=t.user_id
                ORDER BY t.clock_in DESC LIMIT 20
            """)
            timekeeping_records = cur.fetchall() or []

        return render_template(
            'dashboard.html', 
            user=current_user,
            employees=employees, 
            my_leaves=my_leaves, 
            my_loans=my_loans,
            pending_leaves=pending_leaves_list, 
            timekeeping_records=timekeeping_records, 
            total_employees=total_employees, 
            pending_leaves_count=pending_leaves_count, 
            total_payroll=total_payroll, 
            search_q=q,
            current_month_year=current_month_year
        )

    except Exception as e:
        print("Dashboard Route Error:", e)
        fallback_user = {
            'fullname': session.get('fullname', 'Employee Profile'),
            'role': session.get('role', 'employee'),
            'email': session.get('user_email', '')
        }
        return render_template(
            'dashboard.html', 
            user=fallback_user,
            employees=[], 
            my_leaves=[], 
            my_loans=[],
            pending_leaves=[], 
            timekeeping_records=[], 
            total_employees=0, 
            pending_leaves_count=0, 
            total_payroll=0.0, 
            search_q=q,
            current_month_year=current_month_year
        )

@app.route('/reports')
def reports():
    if 'user_id' not in session:
        return redirect('/login')
    
    db = get_db()
    timekeeping_records = []
    employees = []
    current_user = None

    try:
        with db.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE id=%s", (session['user_id'],))
            current_user = cur.fetchone()

            if not current_user:
                current_user = {
                    'id': session['user_id'],
                    'fullname': session.get('fullname', 'Employee'),
                    'role': session.get('role', 'employee'),
                    'employee_id': session.get('employee_id', 'GP-2026-001'),
                    'department': 'Operations',
                    'job_title': session.get('role', 'employee').title(),
                    'work_location': 'Cubao Quezon City',
                    'daily_rate': 3500
                }

            try:
                cur.execute("SELECT * FROM users ORDER BY id DESC")
                employees = cur.fetchall() or []
            except Exception:
                employees = [current_user]

            try:
                cur.execute("""
                    SELECT t.*, u.fullname, u.department, u.employee_id, u.work_location as lob, u.shift_start 
                    FROM timekeeping t
                    JOIN users u ON u.id=t.user_id
                    ORDER BY t.clock_in DESC
                """)
                timekeeping_records = cur.fetchall() or []
            except Exception:
                timekeeping_records = []

    except Exception as e:
        print("Reports Route Detailed Error:", e)
        current_user = {
            'id': session.get('user_id', 1),
            'fullname': session.get('fullname', 'Employee'),
            'role': session.get('role', 'employee'),
            'employee_id': session.get('employee_id', 'GP-2026-001'),
            'department': 'Operations',
            'job_title': 'Staff',
            'work_location': 'Cubao Quezon City',
            'daily_rate': 3500
        }
        employees = [current_user]
        timekeeping_records = []
        
    return render_template(
        'reports.html', 
        user=current_user,
        employees=employees,
        timekeeping_records=timekeeping_records
    )

@app.route('/payroll-processing', methods=['GET', 'POST'])
def payroll_processing():
    if 'user_id' not in session:
        return redirect('/login')
    
    if session.get('role') == 'employee':
        flash('Unauthorized access to payroll processing module.', 'danger')
        return redirect('/dashboard')

    db = get_db()
    employees = []
    payroll_results = []
    period_start = request.form.get('period_start', '2026-10-01')
    period_end = request.form.get('period_end', '2026-10-15')

    try:
        with db.cursor() as cur:
            cur.execute("SELECT * FROM users ORDER BY id DESC")
            employees = cur.fetchall() or []

            for emp in employees:
                daily_rate = float(emp.get('daily_rate') or 1000.0)
                hourly_rate = daily_rate / 8.0
                
                try:
                    cur.execute("""
                        SELECT * FROM timekeeping 
                        WHERE user_id = %s AND clock_out IS NOT NULL
                        AND clock_in::date >= %s::date AND clock_in::date <= %s::date
                    """, (emp['id'], period_start, period_end))
                    logs = cur.fetchall() or []
                except Exception:
                    logs = []
                
                days_worked = len(logs)
                basic_pay = days_worked * daily_rate
                night_diff_pay = days_worked * (hourly_rate * 0.10 * 2)
                overtime_pay = 0.00
                
                gross_pay = basic_pay + night_diff_pay + overtime_pay
                est_monthly = daily_rate * 22
                
                pagibig = 100.00 if gross_pay > 0 else 0.00
                philhealth = ((est_monthly * 0.025) / 2) if gross_pay > 0 else 0.00
                sss = ((est_monthly * 0.045) / 2) if gross_pay > 0 else 0.00
                total_deductions = pagibig + philhealth + sss
                
                net_pay = gross_pay - total_deductions if gross_pay > total_deductions else 0.00

                payroll_results.append({
                    'employee_id': emp.get('employee_id', 'GP-2026-001'),
                    'fullname': emp.get('fullname', 'Unnamed'),
                    'department': emp.get('department', 'Operations'),
                    'days_worked': days_worked,
                    'basic_pay': basic_pay,
                    'night_diff': night_diff_pay,
                    'gross_pay': gross_pay,
                    'deductions': total_deductions,
                    'net_pay': net_pay
                })
    except Exception as e:
        print("Payroll Processing Detailed Error:", e)

    return render_template(
        'payroll_processing.html',
        employees=employees,
        payroll_results=payroll_results,
        period_start=period_start,
        period_end=period_end
    )

@app.route('/leave-module', methods=['GET'])
def leave_module():
    if 'user_id' not in session:
        return redirect('/login')
    db = get_db()
    with db.cursor() as cur:
        cur.execute("SELECT * FROM users WHERE id=%s", (session['user_id'],))
        current_user = cur.fetchone()
        cur.execute("SELECT * FROM leave_applications WHERE user_id=%s ORDER BY date_created DESC", (session['user_id'],))
        my_leaves = cur.fetchall() or []
    return render_template('leave_page.html', user=current_user, my_leaves=my_leaves)

@app.route('/loans-module', methods=['GET'])
def loans_module():
    if 'user_id' not in session:
        return redirect('/login')
    db = get_db()
    with db.cursor() as cur:
        cur.execute("SELECT * FROM users WHERE id=%s", (session['user_id'],))
        current_user = cur.fetchone()
        cur.execute("SELECT * FROM loan_applications WHERE user_id=%s ORDER BY date_applied DESC", (session['user_id'],))
        my_loans = cur.fetchall() or []
    return render_template('loans_page.html', user=current_user, my_loans=my_loans)

@app.route('/pending-approvals', methods=['GET'])
def pending_approvals():
    if 'user_id' not in session:
        return redirect('/login')
    if session.get('role') == 'employee':
        flash('Unauthorized access.', 'danger')
        return redirect('/dashboard')
    db = get_db()
    with db.cursor() as cur:
        cur.execute("""
            SELECT l.*, u.fullname, u.department, u.job_title FROM leave_applications l
            JOIN users u ON u.id=l.user_id
            WHERE l.status='Pending' ORDER BY l.date_created DESC
        """)
        pending_leaves = cur.fetchall() or []
    return render_template('approvals_page.html', pending_leaves=pending_leaves)

@app.route('/admin/assign_shift', methods=['POST'])
def assign_shift():
    if session.get('role') not in ['admin', 'hr_manager', 'supervisor', 'hr']:
        flash('Unauthorized access for shift management.', 'danger')
        return redirect('/dashboard')

    user_id = request.form.get('user_id')
    shift_start = request.form.get('shift_start')
    shift_end = request.form.get('shift_end')
    rest_days_list = request.form.getlist('rest_days')
    work_location = request.form.get('work_location', 'Cubao Quezon City')

    rest_days_str = ", ".join(rest_days_list) if rest_days_list else "Saturday,Sunday"

    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("""
                UPDATE users 
                SET shift_start = %s, shift_end = %s, rest_days = %s, work_location = %s
                WHERE id = %s
            """, (shift_start, shift_end, rest_days_str, work_location, user_id))
            db.commit()
        flash('Shift schedule updated successfully!', 'success')
    except Exception as e:
        print("Shift Assignment Error:", e)
        flash('Failed to update shift schedule.', 'danger')

    return redirect('/dashboard')

@app.route('/timekeeping', methods=['GET', 'POST'])
def timekeeping():
    if 'user_id' not in session:
        return redirect('/login')
    if request.method == 'POST':
        action_type = request.form.get('action')
        if action_type == 'time_in':
            hris.record_time_in(session['user_id'])
            flash('Successfully Clocked In!', 'success')
        elif action_type == 'time_out':
            hris.record_time_out(session['user_id'])
            flash('Successfully Clocked Out!', 'success')
        return redirect('/dashboard')

    db = get_db()
    with db.cursor() as cur:
        cur.execute("""
            SELECT t.*, u.fullname, u.department FROM timekeeping t
            JOIN users u ON u.id=t.user_id
            WHERE t.user_id=%s ORDER BY t.clock_in DESC
        """, (session['user_id'],))
        records = cur.fetchall() or []
    return render_template('timekeeping.html', records=records)

@app.route('/leave/apply', methods=['POST'])
def apply_leave():
    if 'user_id' not in session:
        return redirect('/login')
    ok, msg = hris.request_leave(
        session['user_id'], 
        request.form['leave_type'], 
        request.form['start_date'], 
        request.form['end_date'], 
        request.form['reason']
    )
    flash(msg, 'success' if ok else 'danger')
    return redirect('/dashboard')

@app.route('/leave/delete/<int:id>', methods=['POST'])
def delete_leave(id):
    if 'user_id' not in session:
        return redirect('/login')
    
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("DELETE FROM leave_applications WHERE id = %s AND user_id = %s AND status = 'Pending'", (id, session['user_id']))
            db.commit()
        flash('Leave application successfully deleted.', 'success')
    except Exception as e:
        print("Delete Leave Error:", e)
        flash('Failed to delete leave application.', 'danger')
        
    return redirect('/dashboard')

@app.route('/loan/apply', methods=['POST'])
def apply_loan():
    if 'user_id' not in session:
        return redirect('/login')

    loan_type = request.form.get('loan_type')
    amount = request.form.get('amount', 0)
    monthly_amortization = request.form.get('monthly_amortization', 0)
    reason = request.form.get('reason', '')

    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("""
                INSERT INTO loan_applications (user_id, loan_type, amount, monthly_amortization, reason, status)
                VALUES (%s, %s, %s, %s, %s, 'Pending')
            """, (session['user_id'], loan_type, amount, monthly_amortization, reason))
            db.commit()
        flash('Loan application submitted successfully for HR approval.', 'success')
    except Exception as e:
        print("Apply Loan Error:", e)
        flash('Failed to submit loan application.', 'danger')

    return redirect('/dashboard')

@app.route('/admin/approve_leave/<int:id>')
def approve_leave(id):
    if session.get('role') not in ['admin', 'hr_manager', 'supervisor', 'hr']:
        flash('Unauthorized access for leave approval.', 'danger')
        return redirect('/dashboard')
    ok, msg = hris.approve_leave(id, session['user_id'])
    flash(msg, 'success' if ok else 'danger')
    return redirect('/dashboard')

@app.route('/admin/calculate_payroll', methods=['POST'])
def calculate_payroll():
    if session.get('role') not in ['admin', 'hr_manager', 'hr']:
        flash('Unauthorized access for payroll processing.', 'danger')
        return redirect('/dashboard')
    period_start = request.form.get('period_start')
    period_end = request.form.get('period_end')
    hris.process_periodic_payroll(period_start, period_end)
    flash(f"Payroll successfully processed and locked for period {period_start} to {period_end}.", 'success')
    return redirect('/dashboard')

@app.route('/export_payroll_csv')
def export_payroll_csv():
    if 'user_id' not in session:
        return redirect('/login')
    
    db = get_db()
    user_role = session.get('role', 'employee')
    
    with db.cursor() as cur:
        if user_role == 'employee':
            cur.execute("""
                SELECT p.*, u.fullname, u.department, u.employee_id 
                FROM payroll_records p
                JOIN users u ON u.id=p.user_id
                WHERE p.user_id=%s
            """, (session['user_id'],))
        else:
            cur.execute("""
                SELECT p.*, u.fullname, u.department, u.employee_id 
                FROM payroll_records p
                JOIN users u ON u.id=p.user_id
            """)
        payroll_data = cur.fetchall() or []

    si = StringIO()
    w = csv.writer(si)
    w.writerow(['Employee ID', 'Fullname', 'Department', 'Basic Pay', 'Overtime Pay', 'Tardy Deductions', 'Tax Deductions', 'Net Pay'])
    for p in payroll_data:
        w.writerow([p['employee_id'], p['fullname'], p['department'], p['basic_pay'], p['overtime_pay'], p['tardy_deductions'], p['tax_deductions'], p['net_pay']])
    
    filename = "my_payslip.csv" if user_role == 'employee' else "master_payroll_register.csv"
    return Response(si.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment;filename={filename}"})

@app.route('/profile', methods=['GET','POST'])
def profile():
    if 'user_id' not in session:
        return redirect('/login')
    if request.method == 'POST':
        if 'profile_pic' in request.files:
            file = request.files['profile_pic']
            if file and file.filename != '' and allowed_file(file.filename):
                ext = file.filename.rsplit('.', 1)[1].lower()
                filename = f"user_{session['user_id']}.{ext}"
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                file.save(filepath)
                hris.update_profile_pic(session['user_id'], filename)
                flash('Profile photo updated!', 'success')
                return redirect('/profile')
    db = get_db()
    with db.cursor() as cur:
        cur.execute("SELECT * FROM users WHERE id=%s", (session['user_id'],))
        user = cur.fetchone()
        cur.execute("""
            SELECT COUNT(*) as total_leaves, 
                   SUM(CASE WHEN status='Pending' THEN 1 ELSE 0 END) as pending 
            FROM leave_applications WHERE user_id=%s
        """, (session['user_id'],))
        stats = cur.fetchall() or []
    return render_template('profile.html', user=user, stats=stats)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)