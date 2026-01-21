from werkzeug.security import generate_password_hash

from app import app
from extensions import db
from models import User, Profile


def run_seed():
    with app.app_context():
        user = User.query.filter_by(email="user@demo.com").first()

        if user:
            print("Demo user already exists.")
            return

        user = User(
            email="user@demo.com",
            name="Demo User",
            password_hash=generate_password_hash("123456"),
        )
        db.session.add(user)
        db.session.flush()

        profile = Profile(
            user_id=user.id,
            cefr_level="B1",
            streak=0,
        )
        db.session.add(profile)

        db.session.commit()
        print("Created demo user: user@demo.com / 123456")


if __name__ == "__main__":
    run_seed()

