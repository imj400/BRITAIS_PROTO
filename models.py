from datetime import datetime, date
from extensions import db

# =========================
# USERS
# =========================
class User(db.Model):
    __tablename__ = "tb_users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    name = db.Column(db.String(80), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    profile = db.relationship("Profile", back_populates="user", uselist=False)
    submissions = db.relationship("Submission", back_populates="user")
    planner_entries = db.relationship("PlannerEntry", back_populates="user")
    progress_snapshots = db.relationship("ProgressSnapshot", back_populates="user")


# =========================
# PROFILES
# =========================
class Profile(db.Model):
    __tablename__ = "tb_profiles"

    user_id = db.Column(db.Integer, db.ForeignKey("tb_users.id"), primary_key=True)
    cefr_level = db.Column(db.String(2), nullable=True)
    streak = db.Column(db.Integer, default=0)

    user = db.relationship("User", back_populates="profile")


# =========================
# TASKS
# =========================
class Task(db.Model):
    __tablename__ = "tb_tasks"

    id = db.Column(db.Integer, primary_key=True)
    type = db.Column(db.String(20), nullable=False)  # WRITING, SPEAKING, LISTENING
    title = db.Column(db.String(255), nullable=False)
    prompt = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    submissions = db.relationship("Submission", back_populates="task")


# =========================
# SUBMISSIONS
# =========================
class Submission(db.Model):
    __tablename__ = "tb_submissions"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("tb_users.id"), nullable=False)
    task_id = db.Column(db.Integer, db.ForeignKey("tb_tasks.id"), nullable=True)
    type = db.Column(db.String(20), nullable=False)
    content = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # AI-related fields
    ai_score = db.Column(db.Float, nullable=True)  # AI score
    ai_feedback = db.Column(db.Text, nullable=True)  # AI feedback text

    user = db.relationship("User", back_populates="submissions")
    task = db.relationship("Task", back_populates="submissions")

    feedback = db.relationship("Feedback", back_populates="submission", uselist=False)
    result = db.relationship("Result", back_populates="submission", uselist=False)


# =========================
# RESULTS
# =========================
class Result(db.Model):
    __tablename__ = "tb_results"

    id = db.Column(db.Integer, primary_key=True)
    submission_id = db.Column(db.Integer, db.ForeignKey("tb_submissions.id"), unique=True)
    score = db.Column(db.Float, nullable=True)

    submission = db.relationship("Submission", back_populates="result")


# =========================
# FEEDBACK
# =========================
class Feedback(db.Model):
    __tablename__ = "tb_feedback"

    id = db.Column(db.Integer, primary_key=True)
    submission_id = db.Column(db.Integer, db.ForeignKey("tb_submissions.id"), unique=True)

    score = db.Column(db.Float, nullable=True)
    suggestions = db.Column(db.Text, nullable=True)

    submission = db.relationship("Submission", back_populates="feedback")


# =========================
# PLANNER (Updated to include activity)
# =========================
class PlannerEntry(db.Model):
    __tablename__ = "tb_planner_entries"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("tb_users.id"), nullable=False)

    date = db.Column(db.Date, nullable=False)
    activity = db.Column(db.String(50), nullable=False)  # Activity type (Writing, Speaking, Listening)
    minutes = db.Column(db.Integer, default=30)

    user = db.relationship("User", back_populates="planner_entries")

    def __repr__(self):
        return f'<PlannerEntry {self.activity} on {self.date}>'


# =========================
# PROGRESS SNAPSHOTS
# =========================
class ProgressSnapshot(db.Model):
    __tablename__ = "tb_progress_snapshots"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("tb_users.id"), nullable=False)

    date = db.Column(db.Date, default=date.today)
    writing_score = db.Column(db.Float, nullable=True)
    speaking_score = db.Column(db.Float, nullable=True)
    listening_score = db.Column(db.Float, nullable=True)

    user = db.relationship("User", back_populates="progress_snapshots")


# =========================
# RESETING PASSWORD
# =========================

class PasswordResetToken(db.Model):
    __tablename__ = "tb_password_reset_tokens"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), nullable=False)
    token = db.Column(db.String(128), unique=True, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    used = db.Column(db.Boolean, default=False)
