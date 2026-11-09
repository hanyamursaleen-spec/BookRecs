import os
from datetime import datetime, timedelta, timezone

from flask import (Flask, render_template, request, redirect,
                   url_for, flash, abort, session)
from flask_sqlalchemy import SQLAlchemy
from flask_login import (LoginManager, UserMixin, login_user, logout_user,
                         login_required, current_user)
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import func

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-change-me")

# Hosted DB in production (DATABASE_URL), SQLite file only as a local fallback
db_url = os.environ.get("DATABASE_URL", "sqlite:///bookrecs.db")
if db_url.startswith("postgres://"):          # some hosts still use the old prefix
    db_url = db_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True}
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

db = SQLAlchemy(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Please log in to continue."


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

GENRES = [
    "Fantasy", "Science Fiction", "Mystery", "Thriller", "Romance",
    "Historical Fiction", "Horror", "Classics", "Young Adult",
    "Non-Fiction", "Biography", "Self-Help",
]

# ---------------------------------------------------------------- models

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(40), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)

class Book(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    title_key = db.Column(db.String(200), unique=True, nullable=False)  # lowercase, prevents duplicates
    genre = db.Column(db.String(50), nullable=False)
    reviews = db.relationship("Review", backref="book",
                              cascade="all, delete-orphan")


class Review(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    book_id = db.Column(db.Integer, db.ForeignKey("book.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    user = db.relationship("User", backref="reviews")
    rating = db.Column(db.Integer, nullable=False)          # 1..5
    text = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime,
                           default=lambda: datetime.now(timezone.utc))


with app.app_context():
    db.create_all()

# --------------------------------------------------------------- helpers
MIN_VOTES = 2  # how many reviews a book needs before its own average dominates


def ranked_books(genre=None, genres=None, exclude_ids=None, limit=None):
    """Return books sorted by a weighted rating.

    score = (n / (n + m)) * avg + (m / (n + m)) * overall_avg

    A book with a single 5-star review therefore doesn't outrank a book
    with fifty 4.8-star reviews.
    """
    q = (db.session.query(Book,
                          func.avg(Review.rating).label("avg"),
                          func.count(Review.id).label("n"))
         .join(Review, Review.book_id == Book.id)
         .group_by(Book.id))
    if genre:
        q = q.filter(Book.genre == genre)
    if genres:
        q = q.filter(Book.genre.in_(genres))
    if exclude_ids:
        q = q.filter(Book.id.notin_(exclude_ids))

    rows = q.all()
    if not rows:
        return []

    overall = db.session.query(func.avg(Review.rating)).scalar() or 3.0
    items = []
    for book, avg, n in rows:
        score = (n / (n + MIN_VOTES)) * avg + (MIN_VOTES / (n + MIN_VOTES)) * overall
        items.append({"book": book, "avg": float(avg), "n": n, "score": score})

    items.sort(key=lambda x: (x["score"], x["n"]), reverse=True)
    return items[:limit] if limit else items

# ---------------------------------------------------------------- routes


@app.route("/", methods=["GET", "POST"])
@login_required
def index():
    """Add a review (home page)."""
    form = request.form if request.method == "POST" else {}

    if request.method == "POST":
        title = form.get("book_name", "").strip()
        genre = form.get("genre", "").strip()
        text = form.get("review", "").strip()
        try:
            rating = int(form.get("rating", ""))
        except ValueError:
            rating = 0

        if not title or not text or genre not in GENRES or not 1 <= rating <= 5:
            flash("Please fill in the book name, genre, rating and review.", "error")
        else:
            key = title.lower()
            book = Book.query.filter_by(title_key=key).first()
            if book is None:
                book = Book(title=title, title_key=key, genre=genre)
                db.session.add(book)
                db.session.flush()

            review = Review.query.filter_by(book_id=book.id, user_id=current_user.id).first()
            if review:
                review.rating, review.text = rating, text
            else:
                db.session.add(Review(book_id=book.id, user_id=current_user.id,
                                      rating=rating, text=text))
            db.session.commit()
            flash("Thanks! Your review has been saved.", "ok")
            return redirect(url_for("my_reviews"))

    titles = [b.title for b in Book.query.order_by(Book.title).all()]
    return render_template("index.html", genres=GENRES, titles=titles, form=form)


@app.route("/top")
def top():
    """Top recommendations (public; personalised when logged in)."""
    mine = (Review.query.filter_by(user_id=current_user.id).all()
            if current_user.is_authenticated else [])
    reviewed_ids = [r.book_id for r in mine]
    liked_genres = sorted({r.book.genre for r in mine if r.rating >= 4})

    recommended = []
    if liked_genres:
        recommended = ranked_books(genres=liked_genres,
                                   exclude_ids=reviewed_ids, limit=5)

    return render_template("top.html",
                           recommended=recommended,
                           liked_genres=liked_genres,
                           has_reviews=bool(mine),
                           overall=ranked_books(limit=10))


@app.route("/my-reviews")
@login_required
def my_reviews():
    reviews = (Review.query.filter_by(user_id=current_user.id)
               .order_by(Review.created_at.desc()).all())
    return render_template("reviews.html", reviews=reviews)


@app.post("/review/<int:review_id>/delete")
@login_required
def delete_review(review_id):
    review = db.get_or_404(Review, review_id)
    if review.user_id != current_user.id:
        abort(403)
    db.session.delete(review)
    db.session.commit()
    flash("Review deleted.", "ok")
    return redirect(url_for("my_reviews"))

@app.post("/review/<int:review_id>/delete")
def delete_review(review_id):
    review = db.get_or_404(Review, review_id)
    if review.user_token != current_user():
        abort(403)
    db.session.delete(review)
    db.session.commit()
    flash("Review deleted.", "ok")
    return redirect(url_for("my_reviews"))


@app.route("/genre")
def genre_list():
    """Genre tab: list of genres."""
    counts = dict(db.session.query(Book.genre, func.count(Book.id))
                  .group_by(Book.genre).all())
    return render_template("genre.html", genres=GENRES, counts=counts,
                           selected=None, items=[])


@app.route("/genre/<name>")
def genre_detail(name):
    """Books in one genre, best rated first."""
    if name not in GENRES:
        abort(404)
    counts = dict(db.session.query(Book.genre, func.count(Book.id))
                  .group_by(Book.genre).all())
    return render_template("genre.html", genres=GENRES, counts=counts,
                           selected=name, items=ranked_books(genre=name))


# Optional: `flask --app main seed` fills the DB so you can see recommendations
@app.cli.command("seed")
def seed():
    db.create_all()
    users = []
    for i in range(3):
        u = User.query.filter_by(username=f"seed{i}").first()
        if not u:
            u = User(username=f"seed{i}", email=f"seed{i}@example.com",
                     password_hash=generate_password_hash("not-a-real-password"))
            db.session.add(u)
        users.append(u)
    db.session.flush()

    data = [
        ("Dune", "Science Fiction", [5, 5, 4]),
        ("Project Hail Mary", "Science Fiction", [5, 5]),
        ("Neuromancer", "Science Fiction", [4, 3, 4]),
        ("The Hobbit", "Fantasy", [5, 4, 4]),
        ("The Name of the Wind", "Fantasy", [5, 4]),
        ("Gone Girl", "Thriller", [4, 3]),
        ("Pride and Prejudice", "Romance", [5, 4]),
    ]
    for title, genre, ratings in data:
        if Book.query.filter_by(title_key=title.lower()).first():
            continue
        book = Book(title=title, title_key=title.lower(), genre=genre)
        db.session.add(book)
        db.session.flush()
        for i, r in enumerate(ratings):
            db.session.add(Review(book_id=book.id, user_id=users[i].id,
                                  rating=r, text="Seeded review."))
    db.session.commit()
    print("Seeded.")

@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not username or not email or len(password) < 8:
            flash("Enter a username, email and a password of at least 8 characters.", "error")
        elif User.query.filter((User.username == username) | (User.email == email)).first():
            flash("That username or email is already registered.", "error")
        else:
            user = User(username=username, email=email,
                        password_hash=generate_password_hash(password))
            db.session.add(user)
            db.session.commit()
            login_user(user, remember=True)
            return redirect(url_for("index"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = User.query.filter_by(email=email).first()
        if user and check_password_hash(user.password_hash, password):
            login_user(user, remember=True)
            nxt = request.args.get("next", "")
            if nxt.startswith("/") and not nxt.startswith("//"):   # only local redirects
                return redirect(nxt)
            return redirect(url_for("index"))
        flash("Incorrect email or password.", "error")
    return render_template("login.html")


@app.post("/logout")
@login_required
def logout():
    logout_user()
    flash("You've been logged out.", "ok")
    return redirect(url_for("login"))

if __name__ == "__main__":
    app.run(debug=True)