import os
import uuid
from datetime import datetime, timedelta, timezone

from flask import (Flask, render_template, request, redirect,
                   url_for, session, flash, abort)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-change-me")
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///bookrecs.db"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=365)

db = SQLAlchemy(app)

GENRES = [
    "Fantasy", "Science Fiction", "Mystery", "Thriller", "Romance",
    "Historical Fiction", "Horror", "Classics", "Young Adult",
    "Non-Fiction", "Biography", "Self-Help",
]

# ---------------------------------------------------------------- models


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
    user_token = db.Column(db.String(64), nullable=False, index=True)
    rating = db.Column(db.Integer, nullable=False)          # 1..5
    text = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime,
                           default=lambda: datetime.now(timezone.utc))


with app.app_context():
    db.create_all()

# --------------------------------------------------------------- helpers


def current_user():
    """Anonymous identity stored in a cookie, so no login is needed.
    'Your reviews' and personal recommendations are tied to this token."""
    if "uid" not in session:
        session["uid"] = str(uuid.uuid4())
        session.permanent = True
    return session["uid"]


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
def index():
    """Add a review (home page)."""
    uid = current_user()
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
                db.session.flush()          # get book.id before commit

            # one review per person per book: update if it already exists
            review = Review.query.filter_by(book_id=book.id, user_token=uid).first()
            if review:
                review.rating, review.text = rating, text
            else:
                db.session.add(Review(book_id=book.id, user_token=uid,
                                      rating=rating, text=text))
            db.session.commit()
            flash("Thanks! Your review has been saved.", "ok")
            return redirect(url_for("my_reviews"))

    titles = [b.title for b in Book.query.order_by(Book.title).all()]
    return render_template("index.html", genres=GENRES, titles=titles, form=form)


@app.route("/top")
def top():
    """Top recommendations: personalised picks + overall top list."""
    uid = current_user()
    mine = Review.query.filter_by(user_token=uid).all()
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
def my_reviews():
    """Your reviews."""
    reviews = (Review.query.filter_by(user_token=current_user())
               .order_by(Review.created_at.desc()).all())
    return render_template("reviews.html", reviews=reviews)


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
            db.session.add(Review(book_id=book.id, user_token=f"seed-{i}",
                                  rating=r, text="Seeded review."))
    db.session.commit()
    print("Seeded.")


if __name__ == "__main__":
    app.run(debug=True)