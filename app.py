import os
import cv2
import face_recognition
from flask import Flask, render_template, request, redirect, url_for, session, send_file, flash
import pymysql
from werkzeug.security import generate_password_hash, check_password_hash
from io import BytesIO
import csv
from datetime import datetime, date
import threading
import numpy as np
import shutil
from urllib.parse import urlencode
from flask import make_response

# ---------------- Session / current run control ----------------
# These were missing in your file and are required because the
# teacher dashboard reads `current_session` while the camera thread runs.
_current_session_lock = threading.Lock()
current_session = None

# ---------------- Camera Control ----------------
camera_active = False
camera_thread = None

def attendance_camera(teacher_id, in_out_status):
    """
    Background camera thread: loads encodings from dataset/student_id and marks attendance.
    """
    global camera_active
    camera_active = True

    dataset_dir = "dataset"
    known_encodings = []
    known_ids = []

    if not os.path.exists(dataset_dir):
        os.makedirs(dataset_dir, exist_ok=True)

    # load encodings from dataset/{student_id} folder (single folder)
    for sid in os.listdir(dataset_dir):
        student_folder = os.path.join(dataset_dir, sid)
        if not os.path.isdir(student_folder):
            continue
        for img_file in os.listdir(student_folder):
            img_path = os.path.join(student_folder, img_file)
            try:
                img = face_recognition.load_image_file(img_path)
                encs = face_recognition.face_encodings(img)
                if encs:
                    known_encodings.append(encs[0])
                    known_ids.append(int(sid))
            except Exception as e:
                print("Skipping bad image:", img_path, e)

    # DB connection
    conn = get_db_connection()
    cursor = conn.cursor()

    # set to avoid multiple entries during a run
    marked_set = set()

    cap = cv2.VideoCapture(0)
    while camera_active:
        ret, frame = cap.read()
        if not ret:
            break

        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        faces = face_recognition.face_locations(rgb)
        encodings = face_recognition.face_encodings(rgb, faces)

        for encoding, face_loc in zip(encodings, faces):
            if len(known_encodings) == 0:
                continue
            matches = face_recognition.compare_faces(known_encodings, encoding, tolerance=0.5)
            if True in matches:
                idx = matches.index(True)
                student_id = known_ids[idx]
                now = datetime.now()

                # unique key for run + day
                key = (student_id, in_out_status, now.date())
                if key in marked_set:
                    pass
                else:
                    # check DB to avoid duplicates across runs
                    try:
                        cursor.execute("SELECT id FROM attendance WHERE student_id=%s AND teacher_id=%s AND date=%s AND in_out_status=%s",
                                       (student_id, teacher_id, now.date(), in_out_status))
                        if not cursor.fetchone():
                            cursor.execute("INSERT INTO attendance (student_id, teacher_id, date, time, status, in_out_status) VALUES (%s,%s,%s,%s,%s,%s)",
                                           (student_id, teacher_id, now.date(), now.time(), "Present", in_out_status))
                            conn.commit()
                            marked_set.add(key)
                    except Exception as e:
                        print("DB insert failed:", e)

                # draw rectangle and label
                top, right, bottom, left = face_loc
                cv2.rectangle(frame, (left, top), (right, bottom), (0, 255, 0), 2)
                cv2.putText(frame, f"ID:{student_id}", (left, top - 10),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 255, 0), 2)

        cv2.imshow("Attendance - Press Q to stop", frame)
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break

    cap.release()
    cv2.destroyAllWindows()
    try:
        cursor.close()
        conn.close()
    except:
        pass
    camera_active = False
    # Clear current_session when thread stops (for safety)
    global current_session
    with _current_session_lock:
        current_session = None

# ---------------- Flask Setup ----------------
DB_CONFIG = {
    'host': 'localhost',
    'user': 'root',
    'password': 'root',   # change to your MySQL root password
    'db': 'face_attendance',
    'cursorclass': pymysql.cursors.DictCursor
}

SECRET_KEY = os.environ.get('SECRET_KEY','change_this_secret')

app = Flask(__name__)
app.secret_key = SECRET_KEY

# ---------- DB ----------
def get_db_connection():
    conn = pymysql.connect(host=DB_CONFIG['host'],
                           user=DB_CONFIG['user'],
                           password=DB_CONFIG['password'],
                           db=DB_CONFIG['db'],
                           cursorclass=pymysql.cursors.DictCursor)
    return conn

# ---------- Routes ----------
@app.route('/')
def index():
    return redirect(url_for('login'))

@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        role = request.form.get('role')
        username = request.form.get('username')
        password = request.form.get('password')

        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM users WHERE username=%s AND role=%s", (username, role))
                user = cur.fetchone()
                if user and check_password_hash(user['password_hash'], password):
                    session['user_id'] = user['id']
                    session['username'] = user['username']
                    session['role'] = user['role']
                    session['name'] = user['name']
                    if user['role'] == 'admin':
                        return redirect(url_for('admin_dashboard'))
                    elif user['role'] == 'teacher':
                        return redirect(url_for('teacher_dashboard'))
                    else:
                        return redirect(url_for('student_dashboard'))
                else:
                    flash("Invalid credentials or role. Please try again.")
        finally:
            conn.close()

    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# ---------- Admin ----------
@app.route('/admin')
def admin_dashboard():
    if session.get('role') != 'admin':
        return redirect(url_for('login'))
    return render_template('admin_dashboard.html', name=session.get('name'))

@app.route('/admin/manage_roles')
def manage_roles():
    if session.get('role') != 'admin':
        return redirect(url_for('login'))
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, username, name FROM users WHERE role='teacher' ORDER BY name")
            teachers = cur.fetchall()
            cur.execute("SELECT id, username, name, roll_no FROM users WHERE role='student' ORDER BY name")
            students = cur.fetchall()
    finally:
        conn.close()
    return render_template('manage_roles.html', teachers=teachers, students=students)

@app.route('/admin/add_teacher', methods=['GET','POST'])
def add_teacher():
    if session.get('role') != 'admin':
        return redirect(url_for('login'))
    if request.method == 'POST':
        username = request.form.get('username')
        name = request.form.get('name')
        password = request.form.get('password')
        
        if not password:
            flash("Password is required!")
            return render_template('add_teacher.html')
        
        password_hash = generate_password_hash(password)
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO users (username,password_hash,role,name) VALUES (%s,%s,%s,%s)",
                            (username, password_hash, 'teacher', name))
                conn.commit()
                flash("Teacher added successfully!")
                return redirect(url_for('manage_roles'))
        except pymysql.err.IntegrityError:
            flash("Username already exists. Choose another username.")
        finally:
            conn.close()
    return render_template('add_teacher.html')

@app.route('/admin/add_student', methods=['GET','POST'])
def add_student():
    if session.get('role') != 'admin':
        return redirect(url_for('login'))
    if request.method == 'POST':
        username = request.form.get('username')
        name = request.form.get('name')
        roll_no = request.form.get('roll_no')
        password = request.form.get('password')
        
        if not password:
            flash("Password is required!")
            return render_template('add_student.html')
        
        password_hash = generate_password_hash(password)
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO users (username,password_hash,role,name,roll_no) VALUES (%s,%s,%s,%s,%s)",
                            (username, password_hash, 'student', name, roll_no))
                conn.commit()
                flash("Student added successfully!")
                return redirect(url_for('manage_roles'))
        except pymysql.err.IntegrityError:
            flash("Username already exists. Choose another username.")
        finally:
            conn.close()
    return render_template('add_student.html')

@app.route('/admin/edit_teacher/<int:teacher_id>', methods=['GET','POST'])
def edit_teacher(teacher_id):
    if session.get('role') != 'admin':
        return redirect(url_for('login'))
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            if request.method == 'POST':
                name = request.form.get('name')
                username = request.form.get('username')
                password = request.form.get('password')
                try:
                    if password:
                        pwd_hash = generate_password_hash(password)
                        cur.execute("UPDATE users SET name=%s, username=%s, password_hash=%s WHERE id=%s AND role='teacher'",
                                    (name, username, pwd_hash, teacher_id))
                    else:
                        cur.execute("UPDATE users SET name=%s, username=%s WHERE id=%s AND role='teacher'",
                                    (name, username, teacher_id))
                    conn.commit()
                    flash("Teacher updated.")
                    return redirect(url_for('manage_roles'))
                except pymysql.err.IntegrityError:
                    flash("Username already exists.")
            cur.execute("SELECT id, username, name FROM users WHERE id=%s AND role='teacher'", (teacher_id,))
            teacher = cur.fetchone()
    finally:
        conn.close()
    if not teacher:
        flash("Teacher not found.")
        return redirect(url_for('manage_roles'))
    return render_template('edit_teacher.html', teacher=teacher)

@app.route('/admin/edit_student/<int:student_id>', methods=['GET','POST'])
def edit_student(student_id):
    if session.get('role') != 'admin':
        return redirect(url_for('login'))
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            if request.method == 'POST':
                name = request.form.get('name')
                username = request.form.get('username')
                roll_no = request.form.get('roll_no')
                password = request.form.get('password')
                try:
                    if password:
                        pwd_hash = generate_password_hash(password)
                        cur.execute("UPDATE users SET name=%s, username=%s, roll_no=%s, password_hash=%s WHERE id=%s AND role='student'",
                                    (name, username, roll_no, pwd_hash, student_id))
                    else:
                        cur.execute("UPDATE users SET name=%s, username=%s, roll_no=%s WHERE id=%s AND role='student'",
                                    (name, username, roll_no, student_id))
                    conn.commit()
                    flash("Student updated.")
                    return redirect(url_for('manage_roles'))
                except pymysql.err.IntegrityError:
                    flash("Username already exists.")
            cur.execute("SELECT id, username, name, roll_no FROM users WHERE id=%s AND role='student'", (student_id,))
            student = cur.fetchone()
    finally:
        conn.close()
    if not student:
        flash("Student not found.")
        return redirect(url_for('manage_roles'))
    return render_template('edit_student.html', student=student)

@app.route('/admin/delete_user/<int:user_id>', methods=['POST'])
def delete_user(user_id):
    if session.get('role') != 'admin':
        return redirect(url_for('login'))
    # remove dataset folder as well for students
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT role FROM users WHERE id=%s", (user_id,))
            u = cur.fetchone()
            cur.execute("DELETE FROM users WHERE id=%s", (user_id,))
            conn.commit()
    finally:
        conn.close()
    # delete dataset folder if existed
    ds = os.path.join("dataset", str(user_id))
    if os.path.isdir(ds):
        try:
            shutil.rmtree(ds)
        except Exception as e:
            print("Failed remove dataset:", e)
    flash("User deleted.")
    return redirect(url_for('manage_roles'))

# ---------- Capture Student Faces ----------
@app.route('/capture_face/<int:student_id>')
def capture_face(student_id):
    # Only admin/teacher allowed to capture
    if session.get('role') not in ('admin', 'teacher'):
        flash("Not authorized to capture faces.")
        return redirect(url_for('login'))

    recapture = request.args.get('recapture', '0') == '1'
    dataset_dir = "dataset"
    student_folder = os.path.join(dataset_dir, str(student_id))

    if recapture and os.path.isdir(student_folder):
        try:
            shutil.rmtree(student_folder)
        except Exception as e:
            print("Failed to remove old dataset:", e)

    os.makedirs(student_folder, exist_ok=True)

    cam = cv2.VideoCapture(0)
    count = 0
    saved = 0
    while True:
        ret, frame = cam.read()
        if not ret:
            break
        cv2.imshow("Capture Face - Press Q to stop", frame)
        # save every 6th frame to reduce near-duplicates
        if count % 6 == 0:
            img_path = os.path.join(student_folder, f"{count}.jpg")
            try:
                cv2.imwrite(img_path, frame)
                saved += 1
            except Exception as e:
                print("Save failed:", e)
        count += 1
        if cv2.waitKey(1) & 0xFF == ord('q') or saved >= 30:
            break
    cam.release()
    cv2.destroyAllWindows()
    flash(f"Captured {saved} images for Student ID {student_id}")
    return redirect(url_for('manage_roles'))

# ---------- Teacher Start/Stop Attendance ----------
@app.route('/teacher/start_attendance', methods=['POST'])
def start_attendance():
    global camera_thread, camera_active, current_session
    if session.get('role') != 'teacher':
        return redirect(url_for('login'))
    in_out_status = request.form.get('in_out_status', 'IN').upper()
    if in_out_status not in ('IN', 'OUT'):
        in_out_status = 'IN'
    if not camera_active:
        teacher_id = session.get('user_id')
        # set current session (thread-safe) so teacher_dashboard can display it
        with _current_session_lock:
            current_session = in_out_status
        camera_thread = threading.Thread(target=attendance_camera, args=(teacher_id, in_out_status), daemon=True)
        camera_thread.start()
        flash(f"Attendance started ({in_out_status}).")
    else:
        flash("Camera already running.")
    return redirect(url_for('teacher_dashboard'))

@app.route('/teacher/stop_attendance', methods=['POST'])
def stop_attendance():
    global camera_active, current_session
    if session.get('role') != 'teacher':
        return redirect(url_for('login'))
    if camera_active:
        camera_active = False
        # clear current session
        with _current_session_lock:
            current_session = None
        flash("Attendance stopped.")
    else:
        flash("Camera not running.")
    return redirect(url_for('teacher_dashboard'))

# ---------- Teacher Dashboard ----------
@app.route('/teacher', methods=['GET'])
def teacher_dashboard():
    """
    Teacher dashboard with optional filters:
    - start_date, end_date (YYYY-MM-DD)
    - session: IN / OUT / ALL
    - search: partial student name or roll_no
    Uses GET query parameters so the same view can generate download links.
    """
    if session.get('role') != 'teacher':
        return redirect(url_for('login'))

    # Read filters from query string
    start_date = request.args.get('start_date') or ''
    end_date = request.args.get('end_date') or ''
    session_filter = request.args.get('session', 'ALL').upper()  # IN, OUT, ALL
    search = request.args.get('search', '').strip()

    teacher_id = session.get('user_id')

    # Build SQL with parameters
    query = """
        SELECT a.id, u.name as student_name, u.roll_no, a.date, a.time, a.status, a.in_out_status
        FROM attendance a
        JOIN users u ON a.student_id = u.id
        WHERE a.teacher_id = %s
    """
    params = [teacher_id]

    # Apply session filter
    if session_filter in ('IN', 'OUT'):
        query += " AND a.in_out_status = %s"
        params.append(session_filter)

    # Apply date filters
    if start_date:
        query += " AND a.date >= %s"
        params.append(start_date)
    if end_date:
        query += " AND a.date <= %s"
        params.append(end_date)

    # Apply search filter (searches student name or roll_no)
    if search:
        query += " AND (u.name LIKE %s OR u.roll_no LIKE %s)"
        like_term = f"%{search}%"
        params.extend([like_term, like_term])

    # Order results
    query += " ORDER BY a.date DESC, a.time DESC"

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(query, tuple(params))
            records = cur.fetchall()
    finally:
        conn.close()

    # Pass current query params so template can build download links / keep form values
    current_filters = {
        'start_date': start_date,
        'end_date': end_date,
        'session': session_filter,
        'search': search
    }

    with _current_session_lock:
        cs = current_session

    return render_template('teacher_dashboard.html',
                           records=records,
                           camera_active=camera_active,
                           current_session=cs,
                           filters=current_filters)


# ---------- Student Dashboard ----------
@app.route('/student', methods=['GET'])
def student_dashboard():
    """
    Student dashboard supports optional GET filters:
    - start_date, end_date
    - session: IN / OUT / ALL
    """
    if session.get('role') != 'student':
        return redirect(url_for('login'))

    start_date = request.args.get('start_date') or ''
    end_date = request.args.get('end_date') or ''
    session_filter = request.args.get('session', 'ALL').upper()
    student_id = session.get('user_id')

    # Build query
    query = """
        SELECT date, time, status, in_out_status FROM attendance
        WHERE student_id = %s
    """
    params = [student_id]
    if session_filter in ('IN', 'OUT'):
        query += " AND in_out_status = %s"
        params.append(session_filter)
    if start_date:
        query += " AND date >= %s"
        params.append(start_date)
    if end_date:
        query += " AND date <= %s"
        params.append(end_date)

    query += " ORDER BY date DESC, time DESC"

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(query, tuple(params))
            records = cur.fetchall()
    finally:
        conn.close()

    current_filters = {'start_date': start_date, 'end_date': end_date, 'session': session_filter}

    return render_template('student_dashboard.html', records=records, filters=current_filters)

# ---------- Reports ----------
@app.route('/admin/generate_report', methods=['GET', 'POST'])
def generate_report():
    if session.get('role') != 'admin':
        return redirect(url_for('login'))
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, username FROM users WHERE role='teacher' ORDER BY name")
            teachers = cur.fetchall()
    finally:
        conn.close()

    records = []
    selected_teacher = None
    start_date = None
    end_date = None

    if request.method == 'POST':
        teacher_id = request.form.get('teacher_id')
        start_date = request.form.get('start_date') or str(date.today())
        end_date = request.form.get('end_date') or str(date.today())
        selected_teacher = teacher_id
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT a.id, u.name as student_name, u.roll_no, a.date, a.time, a.status, a.in_out_status
                    FROM attendance a
                    JOIN users u ON a.student_id = u.id
                    WHERE a.teacher_id = %s AND a.date BETWEEN %s AND %s
                    ORDER BY a.date DESC, u.name
                """, (teacher_id, start_date, end_date))
                records = cur.fetchall()
        finally:
            conn.close()

    return render_template('admin_generate_report.html', teachers=teachers, records=records, selected_teacher=selected_teacher, start_date=start_date, end_date=end_date)

@app.route('/admin/download_report', methods=['POST'])
def admin_download_report():
    if session.get('role') != 'admin':
        return redirect(url_for('login'))
    
    teacher_id = request.form.get('teacher_id')
    start_date = request.form.get('start_date') or str(date.today())
    end_date = request.form.get('end_date') or str(date.today())
    
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT a.id, u.name as student_name, u.roll_no, a.date, a.time, a.status, a.in_out_status
                FROM attendance a
                JOIN users u ON a.student_id = u.id
                WHERE a.teacher_id = %s AND a.date BETWEEN %s AND %s
                ORDER BY a.date DESC, u.name
            """, (teacher_id, start_date, end_date))
            rows = cur.fetchall()
    finally:
        conn.close()

    # Create CSV content as string
    csv_data = "Student Name,Roll No,Date,Time,Status,IN/OUT\n"
    
    for r in rows:
        date_val = r.get('date', '')
        time_val = r.get('time', '')
        
        # Convert date and time to string
        if hasattr(date_val, 'strftime'):
            date_str = date_val.strftime('%Y-%m-%d')
        else:
            date_str = str(date_val) if date_val else ''
            
        if hasattr(time_val, 'strftime'):
            time_str = time_val.strftime('%H:%M:%S')
        else:
            time_str = str(time_val) if time_val else ''
        
        student_name = str(r.get('student_name', '')).replace(',', ' ')
        roll_no = str(r.get('roll_no', ''))
        status = str(r.get('status', ''))
        in_out = str(r.get('in_out_status', 'IN'))
        
        csv_data += f"{student_name},{roll_no},{date_str},{time_str},{status},{in_out}\n"
    
    # Convert to BytesIO
    output = BytesIO()
    output.write(csv_data.encode('utf-8'))
    output.seek(0)
    
    return send_file(
        output,
        mimetype='text/csv',
        as_attachment=True,
        download_name=f'attendance_report_{start_date}_to_{end_date}.csv'
    )


@app.route('/teacher/download_csv', methods=['GET'])
def teacher_download_csv():
    """Return a CSV for the teacher with the same filter semantics as teacher_dashboard."""
    if session.get('role') != 'teacher':
        return redirect(url_for('login'))

    # Read same filters as dashboard
    start_date = request.args.get('start_date') or ''
    end_date = request.args.get('end_date') or ''
    session_filter = request.args.get('session', 'ALL').upper()
    search = request.args.get('search', '').strip()
    teacher_id = session.get('user_id')

    # Build SQL same as above
    query = """
        SELECT u.name as student_name, u.roll_no, a.date, a.time, a.status, a.in_out_status
        FROM attendance a
        JOIN users u ON a.student_id = u.id
        WHERE a.teacher_id = %s
    """
    params = [teacher_id]

    if session_filter in ('IN', 'OUT'):
        query += " AND a.in_out_status = %s"
        params.append(session_filter)
    if start_date:
        query += " AND a.date >= %s"
        params.append(start_date)
    if end_date:
        query += " AND a.date <= %s"
        params.append(end_date)
    if search:
        query += " AND (u.name LIKE %s OR u.roll_no LIKE %s)"
        like_term = f"%{search}%"
        params.extend([like_term, like_term])

    query += " ORDER BY a.date DESC, a.time DESC"

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(query, tuple(params))
            rows = cur.fetchall()
    finally:
        conn.close()

    # Build CSV content
    csv_data = "Student Name,Roll No,Date,Time,Status,IN/OUT\n"
    for r in rows:
        date_val = r.get('date', '')
        time_val = r.get('time', '')
        if hasattr(date_val, 'strftime'):
            date_str = date_val.strftime('%Y-%m-%d')
        else:
            date_str = str(date_val) if date_val else ''
        if hasattr(time_val, 'strftime'):
            time_str = time_val.strftime('%H:%M:%S')
        else:
            time_str = str(time_val) if time_val else ''
        student_name = str(r.get('student_name', '')).replace(',', ' ')
        roll_no = str(r.get('roll_no', ''))
        status = str(r.get('status', ''))
        in_out = str(r.get('in_out_status', 'IN'))
        csv_data += f"{student_name},{roll_no},{date_str},{time_str},{status},{in_out}\n"

    # Send CSV as file response
    response = make_response(csv_data)
    disposition_name = f"teacher_attendance_{start_date or 'all'}_to_{end_date or 'all'}.csv"

    response.headers['Content-Disposition'] = f'attachment; filename={disposition_name}'
    response.headers['Content-Type'] = 'text/csv'
    return response

@app.route('/student/download_csv', methods=['GET'])
def student_download_csv():
    """Allow logged-in student to download their own attendance CSV with optional filters."""
    if session.get('role') != 'student':
        return redirect(url_for('login'))

    start_date = request.args.get('start_date') or ''
    end_date = request.args.get('end_date') or ''
    session_filter = request.args.get('session', 'ALL').upper()
    student_id = session.get('user_id')

    query = """
        SELECT date, time, status, in_out_status FROM attendance
        WHERE student_id = %s
    """
    params = [student_id]
    if session_filter in ('IN', 'OUT'):
        query += " AND in_out_status = %s"
        params.append(session_filter)
    if start_date:
        query += " AND date >= %s"
        params.append(start_date)
    if end_date:
        query += " AND date <= %s"
        params.append(end_date)

    query += " ORDER BY date DESC, time DESC"

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(query, tuple(params))
            rows = cur.fetchall()
    finally:
        conn.close()

    csv_data = "Date,Time,Status,IN/OUT\n"
    for r in rows:
        date_val = r.get('date', '')
        time_val = r.get('time', '')
        if hasattr(date_val, 'strftime'):
            date_str = date_val.strftime('%Y-%m-%d')
        else:
            date_str = str(date_val) if date_val else ''
        if hasattr(time_val, 'strftime'):
            time_str = time_val.strftime('%H:%M:%S')
        else:
            time_str = str(time_val) if time_val else ''
        status = str(r.get('status', ''))
        in_out = str(r.get('in_out_status', 'IN'))
        csv_data += f"{date_str},{time_str},{status},{in_out}\n"

    response = make_response(csv_data)
    disposition_name = f"my_attendance_{start_date or 'all'}_to_{end_date or 'all'}.csv"

    response.headers['Content-Disposition'] = f'attachment; filename={disposition_name}'
    response.headers['Content-Type'] = 'text/csv'
    return response

# helper route to add sample attendance (optional)
@app.route('/add_sample_attendance', methods=['POST'])
def add_sample_attendance():
    if session.get('role') not in ('admin','teacher'):
        return "Unauthorized", 403
    student_id = request.form.get('student_id')
    teacher_id = request.form.get('teacher_id') or session.get('user_id')
    status = request.form.get('status') or 'Present'
    in_out_status = request.form.get('in_out_status') or 'IN'
    now = datetime.now()
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT id FROM attendance WHERE student_id=%s AND teacher_id=%s AND date=%s AND in_out_status=%s""",
                        (student_id, teacher_id, now.date(), in_out_status))
            if not cur.fetchone():
                cur.execute("""INSERT INTO attendance (student_id, teacher_id, date, time, status, in_out_status) VALUES (%s,%s,%s,%s,%s,%s)""",
                            (student_id, teacher_id, now.date(), now.time(), status, in_out_status))
                conn.commit()
                return "Attendance added", 200
            else:
                return "Already marked", 200
    finally:
        conn.close()

if __name__ == '__main__':
    os.makedirs('dataset', exist_ok=True)
    app.run(host='0.0.0.0', port=5000)
