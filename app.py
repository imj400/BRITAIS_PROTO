import os
import re
import json
import random
import calendar
import hashlib
from pathlib import Path
from datetime import datetime, timedelta, date
from functools import wraps
import smtplib
from email.message import EmailMessage

from flask import (
    Flask,
    render_template,
    redirect,
    url_for,
    request,
    flash,
    session,
    send_from_directory,
)
from werkzeug.security import check_password_hash, generate_password_hash
    # 
from werkzeug.utils import secure_filename

from openai import OpenAI

# 
from dotenv import load_dotenv
load_dotenv()

from extensions import db, migrate
from models import User, Submission, Result, Feedback, PlannerEntry, PasswordResetToken

# -------------------------------------------------------
# App setup
# -------------------------------------------------------
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret")

app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///britais.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# Uploads 
UPLOAD_DIR = Path(app.root_path) / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # 25MB max upload

db.init_app(app)
migrate.init_app(app, db)

# -------------------------------------------------------
# Email config for password reset
# -------------------------------------------------------
EMAIL_HOST = os.environ.get("EMAIL_HOST", "smtp.gmail.com")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", 587))
EMAIL_USER = os.environ.get("EMAIL_USER")  
EMAIL_PASS = os.environ.get("EMAIL_PASS")  


def send_reset_email(to_email: str, reset_url: str):
    """
    Sends a password reset email using basic SMTP.

    Requires:
      - EMAIL_USER
      - EMAIL_PASS

    If those are missing, it will just print the link to the console.
    """
    if not EMAIL_USER or not EMAIL_PASS:
        # 
        print("Email not configured. Would send reset link to", to_email, "->", reset_url)
        return

    msg = EmailMessage()
    msg["Subject"] = "Reset your password"
    msg["From"] = EMAIL_USER
    msg["To"] = to_email
    msg.set_content(
        f"Hi,\n\n"
        f"You requested a password reset for your BritAIS account.\n\n"
        f"Click this link to set a new password:\n{reset_url}\n\n"
        f"If you didn’t request this, you can safely ignore this email.\n"
    )

    with smtplib.SMTP(EMAIL_HOST, EMAIL_PORT) as server:
        server.starttls()
        server.login(EMAIL_USER, EMAIL_PASS)
        server.send_message(msg)


def get_openai_client():
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None
    return OpenAI(api_key=api_key)


# -------------------------------------------------------
# Google OAuth (optional)
# -------------------------------------------------------
from authlib.integrations.flask_client import OAuth

oauth = OAuth(app)
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")

google = None
if GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET:
    google = oauth.register(
        name="google",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )


# -------------------------------------------------------
# Writing AI
# -------------------------------------------------------
def generate_ai_feedback_writing(text: str) -> dict:
    client = get_openai_client()

    if client is None:
        words = re.findall(r"\b\w+\b", text)
        score = 50 + (10 if len(words) >= 80 else 0)
        return {
            "score": min(100, score),
            "feedback": [
                "AI service is not configured (missing OPENAI_API_KEY).",
                "Fallback evaluation was used.",
                "Set OPENAI_API_KEY and try again for full AI feedback.",
            ],
        }

    json_schema_format = {
        "type": "json_schema",
        "name": "writing_assessment",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "score": {"type": "number", "minimum": 0, "maximum": 100},
                "feedback": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 5,
                    "maxItems": 20,
                },
            },
            "required": ["score", "feedback"],
        },
    }

    prompt = f"""
You are an English writing examiner and coach.

Assess the writing using CEFR-style criteria:
- Task achievement
- Coherence and cohesion
- Vocabulary range and accuracy
- Grammar range and accuracy
- Naturalness

Give:
- ONE overall score from 0 to 100
- Clear, specific, actionable feedback (bullet points)
- Include concrete corrections or rewrites where relevant

Return ONLY valid JSON matching the schema.

Writing:
{text}
""".strip()

    try:
        resp = client.responses.create(
            model="gpt-4o-mini",
            input=prompt,
            text={"format": json_schema_format},
        )

        data = json.loads(resp.output_text)
        return {
            "score": float(data["score"]),
            "feedback": [str(x) for x in data["feedback"]],
        }

    except Exception as e:
        print("OPENAI ERROR:", repr(e))
        return {
            "score": 55,
            "feedback": [
                "AI evaluation failed temporarily.",
                "Please try again later.",
                f"Debug: {type(e).__name__}",
            ],
        }


# -------------------------------------------------------
# Speaking: transcription 
# -------------------------------------------------------
def transcribe_audio_file(filepath: Path) -> str | None:
    client = get_openai_client()
    if client is None:
        return None

    try:
        with open(filepath, "rb") as f:
            t = client.audio.transcriptions.create(
                model="gpt-4o-mini-transcribe",
                file=f,
                response_format="text",
            )
        return getattr(t, "text", str(t)).strip()
    except Exception as e:
        print("OPENAI ERROR (transcription):", repr(e))
        return None


def generate_ai_feedback_speaking(transcript: str) -> dict:
    client = get_openai_client()
    if client is None:
        return {
            "score": 0,
            "feedback": [
                "AI speaking feedback unavailable (missing OPENAI_API_KEY).",
                "Enable OpenAI billing/credits to get transcription + speaking feedback.",
            ],
        }

    json_schema_format = {
        "type": "json_schema",
        "name": "speaking_assessment",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "score": {"type": "number", "minimum": 0, "maximum": 100},
                "feedback": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 5,
                    "maxItems": 15,
                },
            },
            "required": ["score", "feedback"],
        },
    }

    prompt = f"""
You are an English speaking examiner and coach.

You are given a TRANSCRIPT of a learner's spoken answer.
Score 0–100 and give actionable feedback.

Evaluate:
- Task response & coherence
- Grammar accuracy & range
- Vocabulary range & appropriacy
- Fluency signals visible in text (repetition, unfinished sentences, filler words)
- Natural phrasing

Important:
- You cannot judge pronunciation from text. Do not claim you can.
- Rewrite 2–4 sentences to sound more natural.
- Give 5–15 bullet feedback points.

Return ONLY valid JSON.

Transcript:
{transcript}
""".strip()

    try:
        resp = client.responses.create(
            model="gpt-4o-mini",
            input=prompt,
            text={"format": json_schema_format},
            max_output_tokens=450,
            temperature=0.3,
        )
        data = json.loads(resp.output_text)
        return {"score": float(data["score"]), "feedback": [str(x) for x in data["feedback"]]}
    except Exception as e:
        print("OPENAI ERROR (speaking eval):", repr(e))

        if "insufficient_quota" in repr(e):
            return {
                "score": 0,
                "feedback": [
                    "AI speaking evaluation is unavailable because your OpenAI account has no remaining quota.",
                    "Fix: enable billing/credits, restart the app, then try again.",
                    "Debug: insufficient_quota",
                ],
            }

        return {
            "score": 55,
            "feedback": [
                "AI evaluation failed temporarily.",
                "Please try again later.",
                f"Debug: {type(e).__name__}",
            ],
        }


# -------------------------------------------------------
# Speaking topic 
# -------------------------------------------------------
def fallback_speaking_topic():
    topics = [
        {
            "title": "Daily Routine",
            "prompt": "Describe your daily routine and say what you would like to improve.",
            "questions": [
                "What do you usually do in the morning?",
                "What is the hardest part of your day?",
                "What would you change if you had more time?",
            ],
        },
        {
            "title": "A Place You Love",
            "prompt": "Talk about a place you love and explain why it is special to you.",
            "questions": [
                "Where is it and what does it look like?",
                "What do you do there?",
                "Why does it matter to you?",
            ],
        },
        {
            "title": "Technology in Daily Life",
            "prompt": "Do you think technology makes life better or worse? Explain your opinion.",
            "questions": [
                "What technology do you use most?",
                "What are the benefits?",
                "What are the downsides?",
            ],
        },
        {
            "title": "Learning English",
            "prompt": "Talk about your experience learning English and what motivates you.",
            "questions": [
                "What is hardest for you: speaking, listening, writing, or reading?",
                "What helps you improve the most?",
                "What is your goal for the next 3 months?",
            ],
        },
    ]
    return random.choice(topics)


def generate_speaking_topic(level: str = "B1") -> dict:
    client = get_openai_client()
    if client is None:
        return fallback_speaking_topic()

    json_schema_format = {
        "type": "json_schema",
        "name": "speaking_topic",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "title": {"type": "string"},
                "prompt": {"type": "string"},
                "questions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 3,
                    "maxItems": 5,
                },
            },
            "required": ["title", "prompt", "questions"],
        },
    }

    prompt = f"""
You are an English speaking tutor.
Generate ONE speaking topic for CEFR level {level}.
Return:
- title (short)
- prompt (2-3 sentences)
- 3 to 5 guiding questions

Keep it practical and not too abstract.
Return ONLY valid JSON.
""".strip()

    try:
        resp = client.responses.create(
            model="gpt-4o-mini",
            input=prompt,
            text={"format": json_schema_format},
            max_output_tokens=220,
            temperature=0.8,
        )
        return json.loads(resp.output_text)
    except Exception as e:
        print("OPENAI ERROR (speaking topic):", repr(e))
        return fallback_speaking_topic()


# -------------------------------------------------------
# Listening: task generation AI
# -------------------------------------------------------
def _task_id_for(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def fallback_listening_task(level: str) -> dict:
    bank = {
        "A2": {
            "title": "A morning at the café",
            "passage": (
                "Sofia goes to a small café near her home. She orders a coffee and a sandwich. "
                "The café is busy, so she waits for ten minutes. When her food arrives, she sits by the window "
                "and watches people walking in the street."
            ),
            "questions": [
                {
                    "question": "Where does Sofia go?",
                    "options": ["To a café near her home", "To a park", "To a supermarket", "To a library"],
                    "answer": "To a café near her home",
                    "why": "The passage says she goes to a small café near her home."
                },
                {
                    "question": "Why does she wait for ten minutes?",
                    "options": ["Because the café is busy", "Because she forgot her money", "Because the café is closed", "Because she meets a friend"],
                    "answer": "Because the café is busy",
                    "why": "It explicitly says the café is busy."
                },
                {
                    "question": "Where does she sit?",
                    "options": ["By the window", "Outside", "Near the kitchen", "At the bar"],
                    "answer": "By the window",
                    "why": "It says she sits by the window."
                },
            ],
        },
        "B1": {
            "title": "A new hobby",
            "passage": (
                "Last month, Daniel decided to learn photography. At first, he used his phone, but soon he bought a simple camera. "
                "Every Saturday morning, he walks around his city and takes pictures of buildings and markets. "
                "He uploads his best photos online and asks for feedback. Little by little, he is improving."
            ),
            "questions": [
                {
                    "question": "Why did Daniel buy a camera?",
                    "options": ["He wanted better photos than his phone", "His phone was lost", "He needed it for work", "A friend gave him money"],
                    "answer": "He wanted better photos than his phone",
                    "why": "He started with a phone, then bought a camera to improve."
                },
                {
                    "question": "When does he practice?",
                    "options": ["Every Saturday morning", "Every evening", "On Sundays", "Only on holidays"],
                    "answer": "Every Saturday morning",
                    "why": "The passage says every Saturday morning."
                },
                {
                    "question": "How does he improve?",
                    "options": ["He asks for feedback online", "He stops taking photos", "He only reads books", "He changes cities"],
                    "answer": "He asks for feedback online",
                    "why": "He uploads and asks for feedback."
                },
            ],
        },
        "B2": {
            "title": "Remote work changes",
            "passage": (
                "Many companies introduced remote work to reduce costs and offer employees more flexibility. "
                "However, some managers noticed that new employees sometimes felt isolated and learned more slowly. "
                "To solve this, several teams created short daily check-ins and paired newcomers with mentors. "
                "These changes helped people feel connected without removing the benefits of working from home."
            ),
            "questions": [
                {
                    "question": "What problem did some new employees have?",
                    "options": ["They felt isolated", "They were paid less", "They had no internet", "They moved abroad"],
                    "answer": "They felt isolated",
                    "why": "It says new employees sometimes felt isolated."
                },
                {
                    "question": "What solution is mentioned?",
                    "options": ["Daily check-ins and mentors", "Longer meetings every day", "No communication", "Returning to full-time office work"],
                    "answer": "Daily check-ins and mentors",
                    "why": "Those are the two measures described."
                },
                {
                    "question": "What was the goal of the changes?",
                    "options": ["To feel connected while keeping flexibility", "To remove remote work", "To reduce salaries", "To hire fewer people"],
                    "answer": "To feel connected while keeping flexibility",
                    "why": "The final sentence states that balance."
                },
            ],
        },
        "C1": {
            "title": "Attention and multitasking",
            "passage": (
                "Multitasking is often praised as a modern skill, yet research suggests that frequent task switching can reduce accuracy. "
                "When people jump between messages, documents, and meetings, their brains pay a ‘switching cost’—a short period of reorientation. "
                "Over time, this can create the illusion of productivity while quietly lowering the quality of decisions."
            ),
            "questions": [
                {
                    "question": "What does task switching often reduce?",
                    "options": ["Accuracy", "Sleep", "Team size", "Creativity in all cases"],
                    "answer": "Accuracy",
                    "why": "The passage says it can reduce accuracy."
                },
                {
                    "question": "What is the 'switching cost'?",
                    "options": ["A reorientation period", "A financial penalty", "A tax for companies", "A training program"],
                    "answer": "A reorientation period",
                    "why": "It is defined in the passage."
                },
                {
                    "question": "What illusion may multitasking create?",
                    "options": ["An illusion of productivity", "An illusion of boredom", "An illusion of perfect memory", "An illusion of free time"],
                    "answer": "An illusion of productivity",
                    "why": "It says it creates the illusion of productivity."
                },
            ],
        },
        "C2": {
            "title": "Interpretation and nuance",
            "passage": (
                "In complex debates, participants may agree on facts yet disagree profoundly on meaning. "
                "This happens because interpretation is shaped by assumptions, values, and the context people consider relevant. "
                "As a result, language becomes less a tool for transferring information and more a medium for negotiating nuance."
            ),
            "questions": [
                {
                    "question": "Why can people disagree even when they share facts?",
                    "options": ["Because interpretation differs", "Because facts are always false", "Because context is irrelevant", "Because language is fixed"],
                    "answer": "Because interpretation differs",
                    "why": "It says disagreement comes from interpretation shaped by assumptions/values/context."
                },
                {
                    "question": "What shapes interpretation, according to the passage?",
                    "options": ["Assumptions, values, and context", "Only grammar rules", "Only emotions", "Only statistics"],
                    "answer": "Assumptions, values, and context",
                    "why": "Those three are explicitly listed."
                },
                {
                    "question": "How is language described in complex debates?",
                    "options": ["A medium for negotiating nuance", "A perfect calculator", "Only a dictionary", "A barrier to meaning"],
                    "answer": "A medium for negotiating nuance",
                    "why": "That’s the final point."
                },
            ],
        },
    }

    base = bank.get(level, bank["B1"])
    txt = base["title"] + "\n" + base["passage"]
    return {
        "task_id": _task_id_for(txt),
        "level": level,
        "title": base["title"],
        "instructions": "Listen to the audio. Then answer the questions.",
        "passage": base["passage"],
        "questions": base["questions"],
        "audio_url": None,
        "audio_mimetype": None,
        "audio_path": None,
        "source": "fallback",
    }


def generate_listening_task(level: str) -> dict:
    """
    Creates passage + 3 MCQ questions.
    Attempts OpenAI for generation and TTS; falls back safely if unavailable.
    """
    client = get_openai_client()
    if client is None:
        return fallback_listening_task(level)

    json_schema_format = {
        "type": "json_schema",
        "name": "listening_task",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "title": {"type": "string"},
                "passage": {"type": "string"},
                "questions": {
                    "type": "array",
                    "minItems": 3,
                    "maxItems": 3,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "question": {"type": "string"},
                            "options": {
                                "type": "array",
                                "minItems": 4,
                                "maxItems": 4,
                                "items": {"type": "string"},
                            },
                            "answer": {"type": "string"},
                            "why": {"type": "string"},
                        },
                        "required": ["question", "options", "answer", "why"],
                    },
                },
            },
            "required": ["title", "passage", "questions"],
        },
    }

    prompt = f"""
Create an English listening exercise for CEFR level {level}.

Requirements:
- Passage: 90 to 140 words
- Natural spoken style, clear story or explanation
- Exactly 3 multiple-choice questions
- Each question has 4 options
- Provide the correct answer (must match one of the options)
- Provide a short explanation "why"

Return ONLY valid JSON.
""".strip()

    try:
        r = client.responses.create(
            model="gpt-4o-mini",
            input=prompt,
            text={"format": json_schema_format},
            max_output_tokens=700,
            temperature=0.7,
        )
        data = json.loads(r.output_text)

        title = data["title"].strip()
        passage = data["passage"].strip()
        questions = data["questions"]

        task = {
            "task_id": _task_id_for(title + "\n" + passage),
            "level": level,
            "title": title,
            "instructions": "Listen to the audio. Then answer the questions.",
            "passage": passage,
            "questions": questions,
            "audio_url": None,
            "audio_mimetype": None,
            "audio_path": None,
            "source": "openai",
        }

        try:
            tts = client.audio.speech.create(
                model="gpt-4o-mini-tts",
                voice="alloy",
                input=passage,
                format="mp3",
            )
            ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            filename = f"listening_{ts}_{task['task_id']}.mp3"
            save_path = UPLOAD_DIR / filename

            audio_bytes = getattr(tts, "read", None)
            if callable(audio_bytes):
                content = tts.read()
            else:
                content = getattr(tts, "content", None) or bytes(tts)

            save_path.write_bytes(content)

            task["audio_path"] = f"uploads/{filename}"
            task["audio_url"] = url_for("uploaded_file", filename=filename)
            task["audio_mimetype"] = "audio/mpeg"
        except Exception as e:
            print("OPENAI ERROR (listening TTS):", repr(e))

        return task

    except Exception as e:
        print("OPENAI ERROR (listening task gen):", repr(e))
        return fallback_listening_task(level)


def evaluate_listening_answers(task: dict, user_answers: list[str]) -> tuple[float, list[str]]:
    questions = task.get("questions", [])
    total = len(questions)
    correct = 0
    feedback_lines = []

    for i, q in enumerate(questions):
        expected = q.get("answer")
        got = user_answers[i] if i < len(user_answers) else None
        if got == expected:
            correct += 1
        else:
            feedback_lines.append(
                f"Q{i+1}: Correct answer is '{expected}'. {q.get('why', '').strip()}"
            )

    score = (correct / total * 100.0) if total else 0.0

    if score == 100:
        feedback_lines.insert(0, "Excellent — all answers correct. Try a higher level next time.")
    else:
        feedback_lines.insert(0, "Review the audio once more and focus on keywords (names, numbers, cause-effect).")
        if score < 50:
            feedback_lines.append("Tip: Use a lower level for 2–3 days, then step up again.")

    return score, feedback_lines



def current_user():
    email = session.get("user_email")
    return User.query.filter_by(email=email).first() if email else None


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


# -------------------------------------------------------
# Serve uploaded audio
# -------------------------------------------------------
@app.route("/uploads/<path:filename>")
@login_required
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename, as_attachment=False)


# -------------------------------------------------------
# Home
# -------------------------------------------------------
@app.route("/")
def home():
    if current_user():
        return redirect(url_for("dashboard"))
    return render_template("home.html")


# -------------------------------------------------------
# Auth
# -------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user():
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        email = request.form.get("email", "").lower()
        password = request.form.get("password", "")

        user = User.query.filter_by(email=email).first()
        if user and check_password_hash(user.password_hash, password):
            session["user_email"] = user.email
            return redirect(url_for("dashboard"))

        flash("Invalid credentials", "error")

    google_enabled = "auth_google" in app.view_functions
    return render_template("login.html", google_enabled=google_enabled)


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if current_user():
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        email = request.form.get("email", "").lower()
        password = request.form.get("password", "")

        if User.query.filter_by(email=email).first():
            flash("Email already registered.", "error")
            return redirect(url_for("login"))

        user = User(
            email=email,
            password_hash=generate_password_hash(password),
        )
        db.session.add(user)
        db.session.commit()

        session["user_email"] = user.email
        return redirect(url_for("dashboard"))

    google_enabled = "auth_google" in app.view_functions
    return render_template("signup.html", google_enabled=google_enabled)


@app.route("/logout")
def logout():
    session.pop("user_email", None)
    return redirect(url_for("home"))


# -------------------------------------------------------
# Password reset: request link
# -------------------------------------------------------
@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email", "").lower().strip()
        user = User.query.filter_by(email=email).first()

        # 
        if user:
            token = os.urandom(24).hex()
            expires = datetime.utcnow() + timedelta(hours=1)

            reset = PasswordResetToken(
                email=email,
                token=token,
                expires_at=expires,
            )
            db.session.add(reset)
            db.session.commit()

            reset_url = url_for("reset_password", token=token, _external=True)
            # 
            send_reset_email(email, reset_url)

        flash("If that email exists, a reset link has been sent.", "info")
        return redirect(url_for("login"))

    return render_template("forgot_password.html")


# -------------------------------------------------------
# Password reset
# -------------------------------------------------------
@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    reset = PasswordResetToken.query.filter_by(token=token, used=False).first()

    if not reset or reset.expires_at < datetime.utcnow():
        flash("Invalid or expired reset link.", "error")
        return redirect(url_for("login"))

    if request.method == "POST":
        new_password = request.form.get("password", "").strip()
        if not new_password:
            flash("Please enter a new password.", "error")
            return redirect(request.url)

        user = User.query.filter_by(email=reset.email).first()
        if not user:
            flash("Account not found.", "error")
            return redirect(url_for("login"))

        user.password_hash = generate_password_hash(new_password)
        reset.used = True
        db.session.commit()

        flash("Password updated successfully. Please log in.", "success")
        return redirect(url_for("login"))

    return render_template("reset_password.html", token=token)


# -------------------------------------------------------
# Dashboard  
# -------------------------------------------------------
@app.route("/dashboard")
@login_required
def dashboard():
    user = current_user()
    today = date.today()

   
    recent = (
        Submission.query
        .filter(Submission.user_id == user.id)
        .filter(Submission.ai_score.isnot(None))
        .order_by(Submission.created_at.desc())
        .limit(12)
        .all()
    )

    scores = []
    for s in recent:
        try:
            v = float(s.ai_score)
        except Exception:
            continue
        if v > 0:
            scores.append(v)

    overall_score = round(sum(scores) / len(scores)) if scores else None

    def score_to_level(score: float) -> str:
        if score < 50:
            return "A2"
        if score < 65:
            return "B1"
        if score < 78:
            return "B2"
        if score < 90:
            return "C1"
        return "C2"

    level = score_to_level(overall_score) if overall_score is not None else "—"

    # ---- PLANNER: ----
    next_entry = (
        PlannerEntry.query
        .filter(PlannerEntry.user_id == user.id)
        .filter(PlannerEntry.date >= today)
        .order_by(PlannerEntry.date.asc())
        .first()
    )

    next_session_text = None
    if next_entry:
        day_name = next_entry.date.strftime("%a")
        next_session_text = f"{day_name} {next_entry.date.strftime('%Y-%m-%d')} — {next_entry.minutes} min {next_entry.activity}"


    week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=6)

    week_entries = (
        PlannerEntry.query
        .filter(PlannerEntry.user_id == user.id)
        .filter(PlannerEntry.date >= week_start)
        .filter(PlannerEntry.date <= week_end)
        .all()
    )
    weekly_minutes = sum(e.minutes for e in week_entries) if week_entries else 0
    weekly_goal = 60

    # ----  REAL STREAK (back-to-back practice days) ----
    all_subs = (
        Submission.query
        .filter(Submission.user_id == user.id)
        .order_by(Submission.created_at.desc())
        .all()
    )
    practiced_days = {s.created_at.date() for s in all_subs}

    # 
    anchor = today if today in practiced_days else (today - timedelta(days=1))
    streak = 0
    d = anchor
    while d in practiced_days:
        streak += 1
        d -= timedelta(days=1)

    # 
    streak_window = []
    for i in range(6, -1, -1):
        day = today - timedelta(days=i)
        streak_window.append({"date": day, "done": day in practiced_days})

    return render_template(
        "dashboard.html",
        streak=streak,
        streak_window=streak_window,
        level=level,
        overall_score=overall_score,
        next_session_text=next_session_text,
        weekly_minutes=weekly_minutes,
        weekly_goal=weekly_goal,
    )


# -------------------------------------------------------
# Planner 
# -------------------------------------------------------
@app.route("/planner", methods=["GET", "POST"])
@app.route("/planner/<int:year>/<int:month>", methods=["GET", "POST"])
@login_required
def planner(year=None, month=None):
    user = current_user()
    today = date.today()

    year = year or today.year
    month = month or today.month

    month_name = calendar.month_name[month]
    days_in_month = calendar.monthrange(year, month)[1]

    # 
    first_weekday = date(year, month, 1).weekday()

    first = date(year, month, 1)
    prev_month_date = (first - timedelta(days=1)).replace(day=1)
    next_month_date = (first + timedelta(days=days_in_month + 1)).replace(day=1)

    prev_year, prev_month = prev_month_date.year, prev_month_date.month
    next_year, next_month = next_month_date.year, next_month_date.month

    if request.method == "POST":
        date_iso = request.form.get("day", "").strip()
        activity = request.form.get("activity", "").strip()
        minutes_raw = request.form.get("minutes", "").strip()

        if not date_iso or not activity or not minutes_raw:
            flash("Please fill all planner fields.", "error")
            return redirect(url_for("planner", year=year, month=month))

        try:
            minutes = int(minutes_raw)
        except ValueError:
            flash("Minutes must be a number.", "error")
            return redirect(url_for("planner", year=year, month=month))

        try:
            entry_date = datetime.strptime(date_iso, "%Y-%m-%d").date()
        except ValueError:
            flash("Invalid date format.", "error")
            return redirect(url_for("planner", year=year, month=month))

        entry = PlannerEntry(
            user_id=user.id,
            date=entry_date,
            activity=activity,
            minutes=minutes,
        )
        db.session.add(entry)
        db.session.commit()

        flash("Planner entry saved.", "success")
        return redirect(url_for("planner", year=year, month=month))

    start = date(year, month, 1)
    end = date(year, month, days_in_month)

    entries = (
        PlannerEntry.query
        .filter(PlannerEntry.user_id == user.id)
        .filter(PlannerEntry.date >= start)
        .filter(PlannerEntry.date <= end)
        .order_by(PlannerEntry.date.asc())
        .all()
    )

    entries_by_day = {}
    for e in entries:
        entries_by_day.setdefault(e.date.day, []).append(e)

    return render_template(
        "planner.html",
        year=year,
        month=month,
        month_name=month_name,
        days_in_month=days_in_month,
        first_weekday=first_weekday,  
        prev_year=prev_year,
        prev_month=prev_month,
        next_year=next_year,
        next_month=next_month,
        entries_by_day=entries_by_day,
    )


# -------------------------------------------------------
# Writing
# -------------------------------------------------------
@app.route("/writing", methods=["GET", "POST"])
@login_required
def writing():
    if request.method == "POST":
        essay = request.form.get("essay", "").strip()
        if not essay:
            flash("Please write something.", "error")
            return redirect(url_for("writing"))

        ai = generate_ai_feedback_writing(essay)

        submission = Submission(
            user_id=current_user().id,
            type="WRITING",
            content=essay,
            ai_score=ai["score"],
            ai_feedback="\n".join(ai["feedback"]),
        )
        db.session.add(submission)
        db.session.commit()

        return redirect(url_for("stats"))

    return render_template("writing.html")


# -------------------------------------------------------
# Speaking 
# -------------------------------------------------------
@app.route("/speaking", methods=["GET", "POST"])
@login_required
def speaking():
    if request.method == "GET" and request.args.get("new") == "1":
        session.pop("speaking_topic", None)

    topic = session.get("speaking_topic")
    if not topic:
        topic = generate_speaking_topic(level="B1")
        session["speaking_topic"] = topic

    if request.method == "POST":
        audio = request.files.get("audio")
        notes = request.form.get("notes", "").strip()

        if not audio or audio.filename == "":
            flash("Please record and upload an audio answer.", "error")
            return redirect(url_for("speaking"))

        safe_name = secure_filename(audio.filename)
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        filename = f"speaking_{current_user().id}_{ts}_{safe_name}"
        save_path = UPLOAD_DIR / filename
        audio.save(save_path)

        payload = {
            "topic": topic,
            "audio_path": f"uploads/{filename}",
            "audio_url": url_for("uploaded_file", filename=filename),
            "audio_mimetype": audio.mimetype,
            "notes": notes,
        }

        transcript = transcribe_audio_file(save_path)
        payload["transcript"] = transcript

        if transcript:
            ai = generate_ai_feedback_speaking(transcript)
            ai_score = ai["score"]
            ai_feedback = "\n".join(ai["feedback"])
        else:
            ai_score = 0
            ai_feedback = (
                "Speaking feedback unavailable because transcription failed.\n"
                "If you see 'insufficient_quota' in the terminal, enable OpenAI billing/credits."
            )

        submission = Submission(
            user_id=current_user().id,
            type="SPEAKING",
            content=json.dumps(payload, ensure_ascii=False),
            ai_score=ai_score,
            ai_feedback=ai_feedback,
        )
        db.session.add(submission)
        db.session.commit()

        session.pop("speaking_topic", None)
        return redirect(url_for("stats"))

    return render_template("speaking.html", topic=topic)


# -------------------------------------------------------
# Listening 
# -------------------------------------------------------
@app.route("/listening", methods=["GET", "POST"])
@login_required
def listening():
    if request.method == "GET":
        level = (request.args.get("level") or "B1").upper().strip()
        if level not in {"A2", "B1", "B2", "C1", "C2"}:
            level = "B1"

        if request.args.get("new") == "1":
            session.pop("listening_task", None)

        task = session.get("listening_task")
        if not task or task.get("level") != level:
            task = generate_listening_task(level)
            session["listening_task"] = task

        return render_template("listening.html", task=task)

    task = session.get("listening_task")
    if not task:
        flash("No listening task found. Generate a new one first.", "error")
        return redirect(url_for("listening"))

    level = (request.form.get("level") or task.get("level") or "B1").upper().strip()

    user_answers = []
    for i in range(len(task.get("questions", []))):
        user_answers.append(request.form.get(f"q{i}"))

    score, feedback_lines = evaluate_listening_answers(task, user_answers)

    payload = {
        "task_id": task.get("task_id"),
        "level": level,
        "title": task.get("title"),
        "instructions": task.get("instructions"),
        "passage": task.get("passage"),
        "questions": task.get("questions"),
        "audio_url": task.get("audio_url"),
        "audio_mimetype": task.get("audio_mimetype"),
        "audio_path": task.get("audio_path"),
        "user_answers": user_answers,
        "score": score,
        "source": task.get("source"),
    }

    submission = Submission(
        user_id=current_user().id,
        type="LISTENING",
        content=json.dumps(payload, ensure_ascii=False),
        ai_score=score,
        ai_feedback="\n".join(feedback_lines),
    )
    db.session.add(submission)
    db.session.commit()

    session.pop("listening_task", None)
    return redirect(url_for("stats"))


# -------------------------------------------------------
# Stats  
# -------------------------------------------------------
@app.route("/stats")
@login_required
def stats():
    user = current_user()
    subs = Submission.query.filter_by(user_id=user.id).all()

    counts = {"WRITING": 0, "SPEAKING": 0, "LISTENING": 0}
    for s in subs:
        counts[s.type] = counts.get(s.type, 0) + 1

    subs_sorted = sorted(subs, key=lambda s: s.created_at, reverse=True)

    prepared = []
    for s in subs_sorted:
        row = {"s": s, "data": None}
        if s.type in {"SPEAKING", "LISTENING"}:
            try:
                row["data"] = json.loads(s.content)
            except Exception:
                row["data"] = None
        prepared.append(row)

    return render_template("stats.html", submissions=prepared, counts=counts)


# -------------------------------------------------------
if __name__ == "__main__":
    app.run(debug=True)



