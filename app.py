"""
Versicor Customer Forecast Portal
---------------------------------
Public-facing Flask app. Customers log in via email magic-link and submit
weekly forecasts against their own parts only. Holds NO sensitive shop data.

The shop machine (THREADRIPPER-K1) syncs to this app outbound over HTTPS:
  - POST /api/sync/parts      (shop pushes each customer's allowed parts)
  - GET  /api/sync/forecasts  (shop pulls submitted forecasts)
Both protected by a shared API key in the X-API-Key header.

Env vars:
  SECRET_KEY        Flask session secret (required in prod)
  DATABASE_URL      Postgres URL on Render; falls back to local SQLite
  SYNC_API_KEY      shared secret for shop<->portal sync (required in prod)
  RESEND_API_KEY    Resend API key for magic-link email (optional; logs if unset)
  MAIL_FROM         From address, e.g. "Versicor Portal <portal@goversicor.com>"
  PORTAL_BASE_URL   Public base URL, e.g. https://versicor-portal.onrender.com
  MAGIC_LINK_TTL    Minutes a magic link stays valid (default 15)
  WEEKS_AHEAD       How many rolling weeks to show in the grid (default 12)
"""
import os
import secrets
import datetime as dt
from functools import wraps

from flask import (
    Flask, request, render_template, redirect, url_for,
    session, flash, jsonify, abort
)
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from sqlalchemy import (
    create_engine, Column, Integer, String, Boolean, Date, DateTime,
    ForeignKey, UniqueConstraint, func, select
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
SECRET_KEY     = os.environ.get("SECRET_KEY", "dev-insecure-change-me")
DATABASE_URL   = os.environ.get("DATABASE_URL", "sqlite:///portal.db")
SYNC_API_KEY   = os.environ.get("SYNC_API_KEY", "dev-sync-key-change-me")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
MAIL_FROM      = os.environ.get("MAIL_FROM", "Versicor Portal <onboarding@resend.dev>")
PORTAL_BASE_URL = os.environ.get("PORTAL_BASE_URL", "http://localhost:5000")
MAGIC_LINK_TTL = int(os.environ.get("MAGIC_LINK_TTL", "15"))   # minutes
WEEKS_AHEAD    = int(os.environ.get("WEEKS_AHEAD", "12"))

# Render gives postgres:// ; SQLAlchemy wants postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Secure cookies in prod (https). Off for local http dev.
    SESSION_COOKIE_SECURE=PORTAL_BASE_URL.startswith("https://"),
)

signer = URLSafeTimedSerializer(SECRET_KEY, salt="magic-login")

# --------------------------------------------------------------------------
# Database models
# --------------------------------------------------------------------------
Base = declarative_base()
engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, future=True)


class Customer(Base):
    __tablename__ = "customers"
    id          = Column(Integer, primary_key=True)
    # external_id maps to your internal SQL Server CustomerID
    external_id = Column(String(64), unique=True, nullable=False)
    name        = Column(String(200), nullable=False)
    active      = Column(Boolean, default=True, nullable=False)
    users       = relationship("CustomerUser", back_populates="customer")
    parts       = relationship("Part", back_populates="customer")


class CustomerUser(Base):
    __tablename__ = "customer_users"
    id          = Column(Integer, primary_key=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=False)
    email       = Column(String(255), unique=True, nullable=False)
    active      = Column(Boolean, default=True, nullable=False)
    last_login  = Column(DateTime)
    customer    = relationship("Customer", back_populates="users")


class Part(Base):
    __tablename__ = "parts"
    id          = Column(Integer, primary_key=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=False)
    # external_id maps to your internal PartID
    external_id = Column(String(64), nullable=False)
    part_number = Column(String(120), nullable=False)
    description = Column(String(300))
    active      = Column(Boolean, default=True, nullable=False)
    customer    = relationship("Customer", back_populates="parts")
    __table_args__ = (UniqueConstraint("customer_id", "external_id"),)


class Forecast(Base):
    """Append-only. Each submit writes a new row per (part, week) with a
    fresh submitted_at, so history/drift is preserved. Latest-by-submit wins
    when reading 'current'."""
    __tablename__ = "forecasts"
    id           = Column(Integer, primary_key=True)
    customer_id  = Column(Integer, ForeignKey("customers.id"), nullable=False)
    part_id      = Column(Integer, ForeignKey("parts.id"), nullable=False)
    week_start   = Column(Date, nullable=False)        # Monday of the ISO week
    qty          = Column(Integer, nullable=False, default=0)
    submitted_by = Column(String(255))
    submitted_at = Column(DateTime, default=dt.datetime.utcnow, nullable=False)
    synced       = Column(Boolean, default=False, nullable=False)  # pulled by shop?


Base.metadata.create_all(engine)

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def monday_of(d: dt.date) -> dt.date:
    return d - dt.timedelta(days=d.weekday())


def upcoming_weeks(n: int):
    start = monday_of(dt.date.today())
    return [start + dt.timedelta(weeks=i) for i in range(n)]


def send_magic_link(email: str, link: str):
    """Send via Resend if configured; otherwise log to console for dev."""
    if not RESEND_API_KEY:
        app.logger.warning("=== MAGIC LINK (no RESEND_API_KEY set) ===")
        app.logger.warning("To: %s", email)
        app.logger.warning("Link: %s", link)
        app.logger.warning("=========================================")
        return
    import requests
    resp = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
        json={
            "from": MAIL_FROM,
            "to": [email],
            "subject": "Your Versicor portal sign-in link",
            "html": (
                f"<p>Click to sign in to the Versicor forecast portal:</p>"
                f"<p><a href='{link}'>Sign in</a></p>"
                f"<p>This link expires in {MAGIC_LINK_TTL} minutes. "
                f"If you didn't request it, ignore this email.</p>"
            ),
        },
        timeout=15,
    )
    resp.raise_for_status()


def current_user():
    uid = session.get("uid")
    if not uid:
        return None
    db = SessionLocal()
    try:
        return db.get(CustomerUser, uid)
    finally:
        db.close()


def login_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not session.get("uid"):
            return redirect(url_for("login"))
        return f(*a, **kw)
    return wrapper


def require_api_key(f):
    @wraps(f)
    def wrapper(*a, **kw):
        key = request.headers.get("X-API-Key", "")
        if not SYNC_API_KEY or not secrets.compare_digest(key, SYNC_API_KEY):
            abort(401)
        return f(*a, **kw)
    return wrapper

# --------------------------------------------------------------------------
# Auth routes (magic-link)
# --------------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        db = SessionLocal()
        try:
            user = db.execute(
                select(CustomerUser).where(
                    func.lower(CustomerUser.email) == email,
                    CustomerUser.active.is_(True),
                )
            ).scalar_one_or_none()
        finally:
            db.close()
        # Always show the same message — don't reveal whether the email exists.
        if user:
            token = signer.dumps({"uid": user.id})
            link = f"{PORTAL_BASE_URL}{url_for('magic', token=token)}"
            try:
                send_magic_link(user.email, link)
            except Exception:
                app.logger.exception("Failed to send magic link")
        flash("If that email is registered, a sign-in link is on its way.", "info")
        return redirect(url_for("login"))
    return render_template("login.html")


@app.route("/magic/<token>")
def magic(token):
    try:
        data = signer.loads(token, max_age=MAGIC_LINK_TTL * 60)
    except SignatureExpired:
        flash("That link has expired. Request a new one.", "error")
        return redirect(url_for("login"))
    except BadSignature:
        flash("Invalid sign-in link.", "error")
        return redirect(url_for("login"))

    db = SessionLocal()
    try:
        user = db.get(CustomerUser, data["uid"])
        if not user or not user.active:
            flash("Account not available.", "error")
            return redirect(url_for("login"))
        user.last_login = dt.datetime.utcnow()
        db.commit()
        session.clear()
        session["uid"] = user.id
    finally:
        db.close()
    return redirect(url_for("forecast"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# --------------------------------------------------------------------------
# Forecast grid
# --------------------------------------------------------------------------
@app.route("/")
@login_required
def home():
    return redirect(url_for("forecast"))


@app.route("/forecast", methods=["GET"])
@login_required
def forecast():
    user = current_user()
    db = SessionLocal()
    try:
        parts = db.execute(
            select(Part).where(
                Part.customer_id == user.customer_id,
                Part.active.is_(True),
            ).order_by(Part.part_number)
        ).scalars().all()

        weeks = upcoming_weeks(WEEKS_AHEAD)

        # Latest submitted qty per (part, week) for prefill
        existing = {}
        rows = db.execute(
            select(Forecast).where(
                Forecast.customer_id == user.customer_id,
                Forecast.week_start.in_(weeks),
            ).order_by(Forecast.submitted_at)
        ).scalars().all()
        for r in rows:                      # later submits overwrite earlier
            existing[(r.part_id, r.week_start)] = r.qty

        customer = db.get(Customer, user.customer_id)
        grid = []
        for p in parts:
            grid.append({
                "part_id": p.id,
                "part_number": p.part_number,
                "description": p.description or "",
                "cells": [existing.get((p.id, w), "") for w in weeks],
            })
    finally:
        db.close()

    return render_template(
        "forecast.html",
        customer=customer, user=user, weeks=weeks, grid=grid,
    )


@app.route("/forecast", methods=["POST"])
@login_required
def forecast_submit():
    user = current_user()
    db = SessionLocal()
    try:
        # Re-fetch the user's own parts; never trust part ids from the form
        # without confirming they belong to this customer.
        allowed = {
            p.id for p in db.execute(
                select(Part).where(
                    Part.customer_id == user.customer_id,
                    Part.active.is_(True),
                )
            ).scalars().all()
        }
        weeks = upcoming_weeks(WEEKS_AHEAD)
        now = dt.datetime.utcnow()
        written = 0
        for key, val in request.form.items():
            # field name: qty-<part_id>-<week_index>
            if not key.startswith("qty-"):
                continue
            try:
                _, pid_s, widx_s = key.split("-")
                pid = int(pid_s); widx = int(widx_s)
            except ValueError:
                continue
            if pid not in allowed or not (0 <= widx < len(weeks)):
                continue
            val = (val or "").strip()
            if val == "":
                continue
            try:
                qty = max(0, int(val))
            except ValueError:
                continue
            db.add(Forecast(
                customer_id=user.customer_id,
                part_id=pid,
                week_start=weeks[widx],
                qty=qty,
                submitted_by=user.email,
                submitted_at=now,
                synced=False,
            ))
            written += 1
        db.commit()
    finally:
        db.close()
    flash(f"Forecast submitted ({written} entries saved).", "info")
    return redirect(url_for("forecast"))

# --------------------------------------------------------------------------
# Sync API (shop <-> portal). API-key protected, shop-initiated only.
# --------------------------------------------------------------------------
@app.route("/api/sync/parts", methods=["POST"])
@require_api_key
def sync_parts():
    """Shop pushes the authoritative customer + parts list.
    Body: {"customers":[{"external_id","name","active",
            "users":["a@x.com"],
            "parts":[{"external_id","part_number","description","active"}]}]}
    Upserts customers/users/parts; deactivates parts not present."""
    payload = request.get_json(force=True, silent=True) or {}
    db = SessionLocal()
    try:
        for c in payload.get("customers", []):
            cust = db.execute(
                select(Customer).where(Customer.external_id == str(c["external_id"]))
            ).scalar_one_or_none()
            if not cust:
                cust = Customer(external_id=str(c["external_id"]), name=c.get("name", ""))
                db.add(cust); db.flush()
            cust.name = c.get("name", cust.name)
            cust.active = bool(c.get("active", True))

            # Users — add any new emails (don't delete; deactivate via active flag)
            for email in c.get("users", []):
                email = email.strip().lower()
                if not email:
                    continue
                existing = db.execute(
                    select(CustomerUser).where(func.lower(CustomerUser.email) == email)
                ).scalar_one_or_none()
                if not existing:
                    db.add(CustomerUser(customer_id=cust.id, email=email, active=True))

            # Parts — upsert, then deactivate any not in this push
            seen = set()
            for p in c.get("parts", []):
                ext = str(p["external_id"]); seen.add(ext)
                part = db.execute(
                    select(Part).where(
                        Part.customer_id == cust.id, Part.external_id == ext)
                ).scalar_one_or_none()
                if not part:
                    part = Part(customer_id=cust.id, external_id=ext,
                                part_number=p.get("part_number", ext))
                    db.add(part)
                part.part_number = p.get("part_number", part.part_number)
                part.description = p.get("description")
                part.active = bool(p.get("active", True))
            # deactivate parts the shop no longer lists
            for part in db.execute(
                select(Part).where(Part.customer_id == cust.id)
            ).scalars().all():
                if part.external_id not in seen:
                    part.active = False
        db.commit()
    finally:
        db.close()
    return jsonify({"ok": True})


@app.route("/api/sync/forecasts", methods=["GET"])
@require_api_key
def sync_forecasts():
    """Shop pulls submitted forecasts. ?unsynced=1 returns only un-pulled rows
    and marks them synced. Otherwise returns everything since ?since=ISO date."""
    only_unsynced = request.args.get("unsynced") == "1"
    since = request.args.get("since")
    db = SessionLocal()
    try:
        q = select(Forecast)
        if only_unsynced:
            q = q.where(Forecast.synced.is_(False))
        elif since:
            try:
                since_dt = dt.datetime.fromisoformat(since)
                q = q.where(Forecast.submitted_at >= since_dt)
            except ValueError:
                pass
        rows = db.execute(q.order_by(Forecast.submitted_at)).scalars().all()

        # join to external ids so shop maps to its own SQL Server keys
        out = []
        cust_ext = {c.id: c.external_id for c in db.execute(select(Customer)).scalars()}
        part_ext = {p.id: p.external_id for p in db.execute(select(Part)).scalars()}
        for r in rows:
            out.append({
                "forecast_id": r.id,
                "customer_external_id": cust_ext.get(r.customer_id),
                "part_external_id": part_ext.get(r.part_id),
                "week_start": r.week_start.isoformat(),
                "qty": r.qty,
                "submitted_by": r.submitted_by,
                "submitted_at": r.submitted_at.isoformat(),
            })
        if only_unsynced:
            for r in rows:
                r.synced = True
            db.commit()
    finally:
        db.close()
    return jsonify({"forecasts": out})


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
