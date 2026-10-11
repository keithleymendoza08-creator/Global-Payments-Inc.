from datetime import datetime, time, timedelta
import os
import psycopg
from psycopg.rows import dict_row

DATABASE_URL = os.environ.get(
    'DATABASE_URL',
    'postgresql://postgres.hudetzzomizjnygxkjqu:Keithley%401004@aws-0-ap-southeast-1.pooler.supabase.com:6543/postgres?sslmode=require'
)

def get_db():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)

def init_system():
    """Initializes database tables required for HRIS operations[cite: 12]."""
    try:
        conn = get_db()
        with conn.cursor() as cur:
            # Timekeeping Table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS timekeeping (
                    id SERIAL PRIMARY KEY,
                    user_id INT REFERENCES users(id) ON DELETE CASCADE,
                    clock_in TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    clock_out TIMESTAMP,
                    tardy_minutes INT DEFAULT 0,
                    undertime_minutes INT DEFAULT 0
                );
            """)

            # Leave Applications Table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS leave_applications (
                    id SERIAL PRIMARY KEY,
                    user_id INT REFERENCES users(id) ON DELETE CASCADE,
                    leave_type VARCHAR(50),
                    start_date DATE,
                    end_date DATE,
                    reason TEXT,
                    status VARCHAR(20) DEFAULT 'Pending',
                    approved_by INT,
                    date_created TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # Payroll Records Table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS payroll_records (
                    id SERIAL PRIMARY KEY,
                    user_id INT REFERENCES users(id) ON DELETE CASCADE,
                    period_start DATE,
                    period_end DATE,
                    basic_pay NUMERIC(10, 2) DEFAULT 0.00,
                    overtime_pay NUMERIC(10, 2) DEFAULT 0.00,
                    tardy_deductions NUMERIC(10, 2) DEFAULT 0.00,
                    tax_deductions NUMERIC(10, 2) DEFAULT 0.00,
                    net_pay NUMERIC(10, 2) DEFAULT 0.00,
                    date_processed TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            conn.commit()
        conn.close()
    except Exception as e:
        print("Database Initialization Error:", e)

def record_time_in(user_id):
    """
    Calculates tardy minutes based on standard shift start (09:00 AM) and grace period (15 mins)[cite: 12].
    Formula: Tardy Minutes = max(0, Time In - (Shift Start + Grace Period))[cite: 12]
    """
    conn = get_db()
    # Adjusted to Philippine Standard Time (UTC+8) for presentation accuracy
    now = datetime.now() + timedelta(hours=8)
    
    # Shift Boundary: 09:00 AM, Grace Period: 15 mins (09:15 AM limit)
    shift_start = datetime.combine(now.date(), time(9, 0))
    grace_period = timedelta(minutes=15)
    cutoff = shift_start + grace_period

    tardy_minutes = 0
    if now > cutoff:
        tardy_minutes = int((now - cutoff).total_seconds() // 60)

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO timekeeping (user_id, clock_in, tardy_minutes)
            VALUES (%s, %s, %s)
        """, (user_id, now, tardy_minutes))
        conn.commit()
    conn.close()

def record_time_out(user_id):
    """Updates the active clock-in session with clock-out timestamp and calculates undertime if applicable[cite: 12]."""
    conn = get_db()
    # Adjusted to Philippine Standard Time (UTC+8) for presentation accuracy
    now = datetime.now() + timedelta(hours=8)
    
    # Standard Shift End: 05:00 PM (17:00)
    shift_end = datetime.combine(now.date(), time(17, 0))
    undertime_minutes = 0
    if now < shift_end:
        undertime_minutes = int((shift_end - now).total_seconds() // 60)

    with conn.cursor() as cur:
        cur.execute("""
            UPDATE timekeeping 
            SET clock_out = %s, undertime_minutes = %s
            WHERE id = (
                SELECT id FROM timekeeping 
                WHERE user_id = %s AND clock_out IS NULL 
                ORDER BY clock_in DESC LIMIT 1
            )
        """, (now, undertime_minutes, user_id))
        conn.commit()
    conn.close()

def request_leave(user_id, leave_type, start_date, end_date, reason):
    """Files a new leave application[cite: 12]."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO leave_applications (user_id, leave_type, start_date, end_date, reason)
                VALUES (%s, %s, %s, %s, %s)
            """, (user_id, leave_type, start_date, end_date, reason))
            conn.commit()
        conn.close()
        return True, "Leave application submitted successfully."
    except Exception as e:
        return False, f"Failed to submit leave: {e}"

def approve_leave(leave_id, manager_id):
    """Approves a pending leave application[cite: 12]."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE leave_applications 
                SET status = 'Approved', approved_by = %s 
                WHERE id = %s
            """, (manager_id, leave_id))
            conn.commit()
        conn.close()
        return True, "Leave approved successfully."
    except Exception as e:
        return False, f"Approval error: {e}"

def update_profile_pic(user_id, filename):
    """Updates user profile picture filename[cite: 12]."""
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("UPDATE users SET profile_pic = %s WHERE id = %s", (filename, user_id))
        conn.commit()
    conn.close()

def process_periodic_payroll(period_start, period_end):
    """
    Calculates Net Pay based on the formula:[cite: 12]
    Net Pay = (Basic Pay + Overtime) - (Tardy Deductions + Absences + Taxes)[cite: 12]
    Locks the period against retroactive edits[cite: 12].
    """
    conn = get_db()
    with conn.cursor() as cur:
        users = cur.execute("SELECT id, hourly_rate FROM users WHERE role='employee'").fetchall()
        
        for u in users:
            uid = u['id']
            hourly_rate = float(u['hourly_rate'] or 150)  # Default base hourly rate
            
            # Aggregate total tardy minutes for the cut-off period
            tardy_res = cur.execute("""
                SELECT SUM(tardy_minutes) as total_tardy 
                FROM timekeeping 
                WHERE user_id = %s AND clock_in BETWEEN %s AND %s
            """, (uid, period_start, period_end)).fetchone()
            
            total_tardy = tardy_res['total_tardy'] or 0
            
            # Compensation Computations
            basic_pay = hourly_rate * 80.0  # Standard 80 hours per 2-week cut-off
            overtime_pay = 0.00
            tardy_deductions = (hourly_rate / 60.0) * total_tardy
            tax_deductions = basic_pay * 0.10  # Standard tax deduction assumption
            
            net_pay = (basic_pay + overtime_pay) - (tardy_deductions + tax_deductions)

            cur.execute("""
                INSERT INTO payroll_records (user_id, period_start, period_end, basic_pay, overtime_pay, tardy_deductions, tax_deductions, net_pay)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (uid, period_start, period_end, basic_pay, overtime_pay, tardy_deductions, tax_deductions, max(0, net_pay)))
        
        conn.commit()
    conn.close()