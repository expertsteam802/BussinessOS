from flask import Flask, render_template, request, jsonify, session
from pathlib import Path
from datetime import datetime, timedelta
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash
from email.message import EmailMessage
from urllib.parse import quote
import sqlite3, os, json, urllib.request, urllib.error, secrets, hashlib, smtplib, logging
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
DATABASE = Path(os.environ.get("BUSINESS_OS_DB", DATA_DIR / "business_os.db"))
app = Flask(__name__)
app.secret_key = os.environ.get("BUSINESS_OS_SECRET", "change-this-local-secret-before-sharing")
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("COOKIE_SECURE", "0") == "1"
logging.basicConfig(level=logging.INFO)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

CLOUD_SYNC_STATE = {"configured": False, "ok": None, "last_synced": None, "error": None}

BUSINESS_TYPES = ["Supermarket / Grocery", "Retail shop", "Wholesale", "Restaurant / Cafe",
                  "Pharmacy", "Clothing store", "Hardware store", "Salon / Beauty",
                  "Services", "Other"]

def connect_db():
    db = sqlite3.connect(DATABASE, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db

def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")

def init_db():
    with connect_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS users(
          id INTEGER PRIMARY KEY AUTOINCREMENT, owner_name TEXT NOT NULL,
          business_name TEXT NOT NULL, business_type TEXT NOT NULL, currency TEXT NOT NULL DEFAULT 'RWF',
          email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS products(
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
          name TEXT NOT NULL, sku TEXT DEFAULT '', category TEXT DEFAULT 'General',
          quantity INTEGER NOT NULL DEFAULT 0, cost_price REAL NOT NULL DEFAULT 0,
          selling_price REAL NOT NULL DEFAULT 0, min_stock INTEGER NOT NULL DEFAULT 5,
          created_at TEXT NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS sales(
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
          product_id INTEGER, product_name TEXT NOT NULL, quantity INTEGER NOT NULL,
          unit_price REAL NOT NULL, unit_cost REAL NOT NULL DEFAULT 0, total REAL NOT NULL,
          created_at TEXT NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS expenses(
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
          category TEXT NOT NULL, description TEXT DEFAULT '', amount REAL NOT NULL,
          created_at TEXT NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_products_user ON products(user_id);
        CREATE INDEX IF NOT EXISTS idx_sales_user ON sales(user_id);
        CREATE INDEX IF NOT EXISTS idx_expenses_user ON expenses(user_id);
        CREATE TABLE IF NOT EXISTS password_reset_tokens(
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, token_hash TEXT NOT NULL UNIQUE,
          expires_at TEXT NOT NULL, used_at TEXT DEFAULT NULL, created_at TEXT NOT NULL,
          FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        """)
        # Migrate existing local databases safely without deleting user data.
        user_columns = {row[1] for row in db.execute("PRAGMA table_info(users)").fetchall()}
        if "currency" not in user_columns:
            db.execute("ALTER TABLE users ADD COLUMN currency TEXT NOT NULL DEFAULT 'RWF'")

def cloud_enabled():
    return bool(os.environ.get("SUPABASE_URL", "").strip() and os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip())

def cloud_request(method, path, body=None):
    base = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not base or not key:
        raise RuntimeError("Cloud sync is not configured.")
    headers = {"apikey": key, "Authorization": "Bearer " + key, "Content-Type": "application/json"}
    if method == "POST":
        headers["Prefer"] = "resolution=merge-duplicates,return=minimal"
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(base + "/rest/v1/" + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=12) as response:
        raw = response.read().decode("utf-8", errors="replace")
        return json.loads(raw) if raw.strip() else None

def cloud_fetch(email):
    if not cloud_enabled():
        return None
    try:
        query = "business_os_accounts?email=eq." + quote(email, safe="") + "&select=snapshot,updated_at&limit=1"
        rows = cloud_request("GET", query)
        return rows[0].get("snapshot") if rows else None
    except Exception as exc:
        logging.warning("Cloud account lookup failed: %s", exc)
        return None

def make_snapshot(user_id):
    with connect_db() as db:
        user = db.execute("SELECT owner_name,business_name,business_type,currency,email,password_hash,created_at FROM users WHERE id=?", (user_id,)).fetchone()
        if not user:
            return None
        products = [dict(r) for r in db.execute("SELECT name,sku,category,quantity,cost_price,selling_price,min_stock,created_at FROM products WHERE user_id=? ORDER BY id", (user_id,))]
        sales = [dict(r) for r in db.execute("SELECT product_name,quantity,unit_price,unit_cost,total,created_at FROM sales WHERE user_id=? ORDER BY id", (user_id,))]
        expenses = [dict(r) for r in db.execute("SELECT category,description,amount,created_at FROM expenses WHERE user_id=? ORDER BY id", (user_id,))]
    return {"version": 1, "account": dict(user), "products": products, "sales": sales, "expenses": expenses, "synced_at": now()}

def sync_user_to_cloud(user_id):
    CLOUD_SYNC_STATE["configured"] = cloud_enabled()
    if not cloud_enabled():
        CLOUD_SYNC_STATE.update({"ok": None, "last_synced": None, "error": None})
        return False
    snapshot = make_snapshot(user_id)
    if not snapshot:
        return False
    email = snapshot["account"]["email"]
    try:
        stamp = now()
        cloud_request("POST", "business_os_accounts?on_conflict=email", [{"email": email, "snapshot": snapshot, "updated_at": stamp}])
        CLOUD_SYNC_STATE.update({"configured": True, "ok": True, "last_synced": stamp, "error": None})
        return True
    except Exception as exc:
        CLOUD_SYNC_STATE.update({"configured": True, "ok": False, "error": str(exc)[:220]})
        logging.warning("Cloud sync failed (local save remains safe): %s", exc)
        return False

def restore_snapshot(snapshot):
    """Restore an account snapshot into this PC when this email is not yet local."""
    account = (snapshot or {}).get("account") or {}
    email = str(account.get("email", "")).strip().lower()
    if not email or not account.get("password_hash"):
        return None
    with connect_db() as db:
        existing = db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        if existing:
            return existing["id"]
        cur = db.execute("INSERT INTO users(owner_name,business_name,business_type,currency,email,password_hash,created_at) VALUES(?,?,?,?,?,?,?)",
          (account.get("owner_name", "Owner"), account.get("business_name", "My business"), account.get("business_type", "Other"), account.get("currency", "RWF"), email, account["password_hash"], account.get("created_at", now())))
        uid = cur.lastrowid
        for p in snapshot.get("products", []):
            db.execute("INSERT INTO products(user_id,name,sku,category,quantity,cost_price,selling_price,min_stock,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
              (uid,p.get("name", "Unnamed item"),p.get("sku", ""),p.get("category", "General"),int(p.get("quantity",0)),float(p.get("cost_price",0)),float(p.get("selling_price",0)),int(p.get("min_stock",5)),p.get("created_at",now())))
        for sale in snapshot.get("sales", []):
            db.execute("INSERT INTO sales(user_id,product_id,product_name,quantity,unit_price,unit_cost,total,created_at) VALUES(?,NULL,?,?,?,?,?,?)",
              (uid,sale.get("product_name", "Item"),int(sale.get("quantity",0)),float(sale.get("unit_price",0)),float(sale.get("unit_cost",0)),float(sale.get("total",0)),sale.get("created_at",now())))
        for expense in snapshot.get("expenses", []):
            db.execute("INSERT INTO expenses(user_id,category,description,amount,created_at) VALUES(?,?,?,?,?)",
              (uid,expense.get("category", "General"),expense.get("description", ""),float(expense.get("amount",0)),expense.get("created_at",now())))
    return uid

def send_reset_email(email, reset_link):
    host = os.environ.get("SMTP_HOST", "").strip()
    sender = os.environ.get("SMTP_FROM", "").strip()
    if not host or not sender:
        raise RuntimeError("Email reset is not configured yet. Add SMTP_HOST and SMTP_FROM to .env.")
    port = int(os.environ.get("SMTP_PORT", "587"))
    username = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASSWORD", "")
    msg = EmailMessage()
    msg["Subject"] = "Reset your Business OS password"
    msg["From"] = sender
    msg["To"] = email
    msg.set_content("A password reset was requested for your Business OS account. The link expires in 60 minutes:\n\n" + reset_link + "\n\nIf you did not request this, ignore this email.")
    with smtplib.SMTP(host, port, timeout=20) as server:
        if os.environ.get("SMTP_USE_TLS", "1") == "1":
            server.starttls()
        if username:
            server.login(username, password)
        server.send_message(msg)

def err(message, status=400):
    return jsonify({"error": message}), status

def current_user():
    return session.get("user_id")

def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user():
            return err("Please log in to continue.", 401)
        return fn(*args, **kwargs)
    return wrapper

@app.get("/")
def home():
    return render_template("index.html")

@app.get("/api/session")
def get_session():
    if not current_user():
        return jsonify({"authenticated": False})
    with connect_db() as db:
        row = db.execute("SELECT id, owner_name, business_name, business_type, currency, email FROM users WHERE id=?",
                         (current_user(),)).fetchone()
    if not row:
        session.clear()
        return jsonify({"authenticated": False})
    return jsonify({"authenticated": True, "user": dict(row), "business_types": BUSINESS_TYPES})

@app.post("/api/register")
def register():
    data = request.get_json(silent=True) or {}
    owner = str(data.get("owner_name", "")).strip()
    business = str(data.get("business_name", "")).strip()
    kind = str(data.get("business_type", "")).strip()
    currency = str(data.get("currency", "RWF")).strip().upper()[:3] or "RWF"
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))
    if not all([owner, business, kind, email, password]):
        return err("Complete all required fields.")
    if kind not in BUSINESS_TYPES:
        return err("Choose a business type from the list.")
    if len(password) < 8:
        return err("Use a password with at least 8 characters.")
    if len(email) > 254 or "@" not in email:
        return err("Enter a valid email address.")
    try:
        with connect_db() as db:
            cur = db.execute("""INSERT INTO users(owner_name,business_name,business_type,currency,email,password_hash,created_at)
                                VALUES(?,?,?,?,?,?,?)""",
                             (owner, business, kind, currency, email, generate_password_hash(password), now()))
            user_id = cur.lastrowid
        session.clear()
        session["user_id"] = user_id
        sync_user_to_cloud(user_id)
        return jsonify({"message":"Account created.", "user":{"id":user_id,"owner_name":owner,
            "business_name":business,"business_type":kind,"currency":currency,"email":email}}), 201
    except sqlite3.IntegrityError:
        return err("An account with that email already exists. Log in instead.", 409)

@app.post("/api/login")
def login():
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))
    with connect_db() as db:
        row = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not row and cloud_enabled():
        snapshot = cloud_fetch(email)
        account = (snapshot or {}).get("account", {})
        if account and check_password_hash(account.get("password_hash", ""), password):
            try:
                restore_snapshot(snapshot)
            except Exception as exc:
                logging.warning("Could not restore cloud account locally: %s", exc)
            with connect_db() as db:
                row = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not row or not check_password_hash(row["password_hash"], password):
        return err("Email or password is incorrect, or this account is not available on this device. Check internet/cloud sync if you use another PC.", 401)
    session.clear()
    session["user_id"] = row["id"]
    sync_user_to_cloud(row["id"])
    return jsonify({"message":"Logged in.", "user":{"id":row["id"],"owner_name":row["owner_name"],
        "business_name":row["business_name"],"business_type":row["business_type"],"currency":row["currency"],"email":row["email"]}})

@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify({"message":"Logged out."})

@app.get("/api/dashboard")
@login_required
def dashboard():
    uid=current_user()
    with connect_db() as db:
        revenue=db.execute("SELECT COALESCE(SUM(total),0) FROM sales WHERE user_id=?",(uid,)).fetchone()[0]
        cogs=db.execute("SELECT COALESCE(SUM(unit_cost*quantity),0) FROM sales WHERE user_id=?",(uid,)).fetchone()[0]
        expenses=db.execute("SELECT COALESCE(SUM(amount),0) FROM expenses WHERE user_id=?",(uid,)).fetchone()[0]
        count=db.execute("SELECT COUNT(*) FROM products WHERE user_id=?",(uid,)).fetchone()[0]
        stock=db.execute("SELECT COALESCE(SUM(quantity),0) FROM products WHERE user_id=?",(uid,)).fetchone()[0]
        low=db.execute("SELECT COUNT(*) FROM products WHERE user_id=? AND quantity<=min_stock",(uid,)).fetchone()[0]
        recent=db.execute("SELECT product_name,quantity,total,created_at FROM sales WHERE user_id=? ORDER BY id DESC LIMIT 8",(uid,)).fetchall()
        products=db.execute("SELECT id,name,sku,category,quantity,cost_price,selling_price,min_stock FROM products WHERE user_id=? ORDER BY name",(uid,)).fetchall()
        expenses_recent=db.execute("SELECT category,description,amount,created_at FROM expenses WHERE user_id=? ORDER BY id DESC LIMIT 8",(uid,)).fetchall()
        daily=db.execute("""SELECT substr(created_at,1,10) day, SUM(total) revenue,
                           SUM(unit_cost*quantity) cogs FROM sales WHERE user_id=?
                           GROUP BY substr(created_at,1,10) ORDER BY day DESC LIMIT 7""",(uid,)).fetchall()
    return jsonify({"revenue":revenue,"cogs":cogs,"gross_profit":revenue-cogs,
      "expenses":expenses,"net_profit":revenue-cogs-expenses,"sales_count":sum(1 for _ in recent),
      "products_count":count,"stock_units":stock,"low_stock_count":low,
      "recent_sales":[dict(x) for x in recent],"recent_expenses":[dict(x) for x in expenses_recent],
      "products":[dict(x) for x in products],"daily":[dict(x) for x in reversed(daily)]})

@app.get("/api/products")
@login_required
def get_products():
    with connect_db() as db:
        rows=db.execute("SELECT * FROM products WHERE user_id=? ORDER BY name",(current_user(),)).fetchall()
    return jsonify([dict(r) for r in rows])

@app.post("/api/products")
@login_required
def add_product():
    d=request.get_json(silent=True) or {}
    name=str(d.get("name","")).strip()
    if not name: return err("Product name is required.")
    try:
        qty=int(d.get("quantity",0)); minimum=int(d.get("min_stock",5))
        cost=float(d.get("cost_price",0)); price=float(d.get("selling_price",0))
    except (TypeError,ValueError): return err("Enter valid stock and price values.")
    if qty<0 or minimum<0 or cost<0 or price<0: return err("Stock and prices cannot be negative.")
    sku = str(d.get("sku", "")).strip()
    with connect_db() as db:
        if sku:
            duplicate = db.execute("SELECT id FROM products WHERE user_id=? AND lower(sku)=lower(?)", (current_user(), sku)).fetchone()
            if duplicate: return err("That barcode/item code is already registered. Scan it to find the existing item.", 409)
        cur=db.execute("""INSERT INTO products(user_id,name,sku,category,quantity,cost_price,selling_price,min_stock,created_at)
          VALUES(?,?,?,?,?,?,?,?,?)""",(current_user(),name,sku,
          str(d.get("category","General")).strip() or "General",qty,cost,price,minimum,now()))
        product_id = cur.lastrowid
        if not sku:
            sku = f"ITEM-{product_id:06d}"
            db.execute("UPDATE products SET sku=? WHERE id=? AND user_id=?", (sku, product_id, current_user()))
    sync_user_to_cloud(current_user())
    return jsonify({"message":"Product added.","id":product_id,"sku":sku}),201

@app.patch("/api/products/<int:pid>")
@login_required
def update_product(pid):
    d=request.get_json(silent=True) or {}
    allowed={"name","sku","category","quantity","cost_price","selling_price","min_stock"}
    fields={k:v for k,v in d.items() if k in allowed}
    if not fields: return err("No valid fields supplied.")
    if "sku" in fields:
        fields["sku"] = str(fields["sku"] or "").strip() or f"ITEM-{pid:06d}"
        if fields["sku"]:
            with connect_db() as db:
                duplicate = db.execute("SELECT id FROM products WHERE user_id=? AND lower(sku)=lower(?) AND id<>?", (current_user(), fields["sku"], pid)).fetchone()
            if duplicate: return err("That barcode/item code is already assigned to another item.", 409)
    for k in ("quantity","min_stock","cost_price","selling_price"):
        if k in fields:
            try: fields[k]=float(fields[k]) if k in ("cost_price","selling_price") else int(fields[k])
            except (TypeError,ValueError): return err(f"Invalid {k}.")
            if fields[k]<0: return err("Values cannot be negative.")
    sql=", ".join(f"{k}=?" for k in fields)
    with connect_db() as db:
        cur=db.execute(f"UPDATE products SET {sql} WHERE id=? AND user_id=?",(*fields.values(),pid,current_user()))
    if not cur.rowcount: return err("Product not found.",404)
    sync_user_to_cloud(current_user())
    return jsonify({"message":"Product updated."})

@app.get("/api/products/lookup")
@login_required
def lookup_product():
    code = str(request.args.get("code", "")).strip()
    if not code: return err("Enter an item name or barcode.")
    with connect_db() as db:
        row = db.execute("SELECT * FROM products WHERE user_id=? AND (lower(sku)=lower(?) OR lower(name)=lower(?)) ORDER BY CASE WHEN lower(sku)=lower(?) THEN 0 ELSE 1 END LIMIT 1", (current_user(), code, code, code)).fetchone()
    if not row: return err("No item found for that barcode or exact name.", 404)
    return jsonify(dict(row))

@app.delete("/api/products/<int:pid>")
@login_required
def delete_product(pid):
    with connect_db() as db:
        row = db.execute("SELECT id,name FROM products WHERE id=? AND user_id=?", (pid,current_user())).fetchone()
        if not row: return err("Item not found.",404)
        # Historical sales retain their saved item name and price even after catalog deletion.
        db.execute("DELETE FROM products WHERE id=? AND user_id=?", (pid,current_user()))
    sync_user_to_cloud(current_user())
    return jsonify({"message": f"{row['name']} deleted from inventory."})

@app.post("/api/sales")
@login_required
def record_sale():
    d=request.get_json(silent=True) or {}
    try: pid=int(d.get("product_id")); qty=int(d.get("quantity"))
    except (TypeError,ValueError): return err("Choose a product and valid quantity.")
    if qty<=0: return err("Quantity must be greater than zero.")
    uid=current_user()
    with connect_db() as db:
        product=db.execute("SELECT * FROM products WHERE id=? AND user_id=?",(pid,uid)).fetchone()
        if not product: return err("Product not found.",404)
        if product["quantity"]<qty: return err(f"Only {product['quantity']} units are in stock.")
        total=qty*product["selling_price"]
        db.execute("UPDATE products SET quantity=quantity-? WHERE id=? AND user_id=?",(qty,pid,uid))
        db.execute("""INSERT INTO sales(user_id,product_id,product_name,quantity,unit_price,unit_cost,total,created_at)
          VALUES(?,?,?,?,?,?,?,?)""",(uid,pid,product["name"],qty,product["selling_price"],product["cost_price"],total,now()))
    sync_user_to_cloud(uid)
    return jsonify({"message":"Sale recorded.","total":total,"gross_profit":qty*(product["selling_price"]-product["cost_price"])}),201

@app.get("/api/sales")
@login_required
def get_sales():
    with connect_db() as db:
        rows=db.execute("SELECT * FROM sales WHERE user_id=? ORDER BY id DESC LIMIT 500",(current_user(),)).fetchall()
    return jsonify([dict(r) for r in rows])

@app.get("/api/expenses")
@login_required
def get_expenses():
    with connect_db() as db:
        rows=db.execute("SELECT * FROM expenses WHERE user_id=? ORDER BY id DESC",(current_user(),)).fetchall()
    return jsonify([dict(r) for r in rows])

@app.post("/api/expenses")
@login_required
def add_expense():
    d=request.get_json(silent=True) or {}
    category=str(d.get("category","General")).strip() or "General"
    description=str(d.get("description","")).strip()
    try: amount=float(d.get("amount",0))
    except (TypeError,ValueError): return err("Enter a valid expense amount.")
    if not 0<amount<1e12: return err("Expense amount must be greater than zero.")
    with connect_db() as db:
        cur=db.execute("INSERT INTO expenses(user_id,category,description,amount,created_at) VALUES(?,?,?,?,?)",
          (current_user(),category,description,amount,now()))
    sync_user_to_cloud(current_user())
    return jsonify({"message":"Expense saved.","id":cur.lastrowid}),201

@app.patch("/api/profile")
@login_required
def update_profile():
    d = request.get_json(silent=True) or {}
    owner = str(d.get("owner_name", "")).strip()
    business = str(d.get("business_name", "")).strip()
    kind = str(d.get("business_type", "")).strip()
    currency = str(d.get("currency", "RWF")).strip().upper()[:3]
    if not owner or not business: return err("Owner name and business name are required.")
    if kind and kind not in BUSINESS_TYPES: return err("Choose a valid business type.")
    if len(currency) != 3 or not currency.isalpha(): return err("Choose a valid 3-letter currency code.")
    with connect_db() as db:
        db.execute("UPDATE users SET owner_name=?,business_name=?,business_type=?,currency=? WHERE id=?", (owner,business,kind or "Other",currency,current_user()))
        row = db.execute("SELECT id,owner_name,business_name,business_type,currency,email FROM users WHERE id=?", (current_user(),)).fetchone()
    sync_user_to_cloud(current_user())
    return jsonify({"message":"Business details updated.","user":dict(row)})

@app.post("/api/password/change")
@login_required
def change_password():
    d = request.get_json(silent=True) or {}
    current = str(d.get("current_password", ""))
    new_password = str(d.get("new_password", ""))
    if len(new_password) < 8:
        return err("Use a new password with at least 8 characters.")
    with connect_db() as db:
        user = db.execute("SELECT id,password_hash FROM users WHERE id=?", (current_user(),)).fetchone()
        if not user or not check_password_hash(user["password_hash"], current):
            return err("Your current password is incorrect.", 401)
        db.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(new_password), current_user()))
        db.execute("UPDATE password_reset_tokens SET used_at=? WHERE user_id=? AND used_at IS NULL", (now(), current_user()))
    sync_user_to_cloud(current_user())
    return jsonify({"message": "Password updated successfully."})

@app.post("/api/password/forgot")
def forgot_password():
    d = request.get_json(silent=True) or {}
    email = str(d.get("email", "")).strip().lower()
    if not email or "@" not in email:
        return err("Enter the email address used for your account.")
    with connect_db() as db:
        user = db.execute("SELECT id,email FROM users WHERE email=?", (email,)).fetchone()
    if not user and cloud_enabled():
        snapshot = cloud_fetch(email)
        if snapshot:
            try:
                restore_snapshot(snapshot)
            except Exception as exc:
                logging.warning("Could not restore cloud account for reset: %s", exc)
            with connect_db() as db:
                user = db.execute("SELECT id,email FROM users WHERE email=?", (email,)).fetchone()
    # Use the same public response whether or not an account exists.
    if not user:
        return jsonify({"message": "If an account exists for that email, reset instructions will be sent."})
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    expires = (datetime.now().astimezone() + timedelta(minutes=60)).isoformat(timespec="seconds")
    with connect_db() as db:
        db.execute("UPDATE password_reset_tokens SET used_at=? WHERE user_id=? AND used_at IS NULL", (now(), user["id"]))
        db.execute("INSERT INTO password_reset_tokens(user_id,token_hash,expires_at,created_at) VALUES(?,?,?,?)", (user["id"],token_hash,expires,now()))
    base_url = os.environ.get("APP_BASE_URL", request.host_url.rstrip("/" )).rstrip("/")
    link = base_url + "/?reset_token=" + quote(token, safe="")
    try:
        send_reset_email(email, link)
    except Exception as exc:
        logging.warning("Password reset email could not be sent: %s", exc)
        return err(str(exc), 503)
    return jsonify({"message": "If an account exists for that email, reset instructions will be sent."})

@app.post("/api/password/reset")
def reset_password():
    d = request.get_json(silent=True) or {}
    token = str(d.get("token", ""))
    password = str(d.get("password", ""))
    if len(password) < 8:
        return err("Use a password with at least 8 characters.")
    if not token:
        return err("This reset link is invalid or expired.", 400)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with connect_db() as db:
        record = db.execute("SELECT id,user_id,expires_at,used_at FROM password_reset_tokens WHERE token_hash=?", (token_hash,)).fetchone()
        if not record or record["used_at"] or datetime.fromisoformat(record["expires_at"]) < datetime.now().astimezone():
            return err("This reset link is invalid or expired. Request a new one.", 400)
        db.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(password), record["user_id"]))
        db.execute("UPDATE password_reset_tokens SET used_at=? WHERE id=?", (now(), record["id"]))
    sync_user_to_cloud(record["user_id"])
    return jsonify({"message": "Password reset successfully. You can now log in."})

@app.get("/api/sync/status")
@login_required
def sync_status():
    return jsonify({"local": True, "cloud_configured": cloud_enabled(), "last_sync_ok": CLOUD_SYNC_STATE.get("ok"), "last_synced": CLOUD_SYNC_STATE.get("last_synced"), "last_sync_error": CLOUD_SYNC_STATE.get("error"), "message": "Cloud sync configured." if cloud_enabled() else "Local database active. Configure SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY for online sync."})

def call_ai(prompt, context):
    provider=os.environ.get("AI_PROVIDER","gemini").lower()
    if provider=="openai":
        key=os.environ.get("OPENAI_API_KEY","")
        if not key: raise RuntimeError("Add OPENAI_API_KEY to your .env file to enable ChatGPT.")
        url=os.environ.get("OPENAI_BASE_URL","https://api.openai.com/v1/chat/completions")
        model=os.environ.get("OPENAI_MODEL","gpt-4o-mini")
        payload={"model":model,"messages":[{"role":"system","content":"You are a careful business analyst. Use only the supplied business records for claims about this business. State when data is missing. Give practical supermarket/retail advice when relevant. Currency is RWF."},{"role":"user","content":"BUSINESS DATA:\n"+json.dumps(context,ensure_ascii=False)+"\n\nQUESTION:\n"+prompt}]}
        headers={"Authorization":"Bearer "+key,"Content-Type":"application/json"}
        extract=lambda data: data["choices"][0]["message"]["content"]
    else:
        key=os.environ.get("GEMINI_API_KEY","").strip()
        if not key: raise RuntimeError("Add GEMINI_API_KEY to your .env file to enable Gemini AI.")
        model=os.environ.get("GEMINI_MODEL","gemini-3.8-flash").strip()
        # Use Google's Interactions API, recommended for current Gemini models.
        url="https://generativelanguage.googleapis.com/v1beta/interactions"
        instruction=("You are a careful business analyst. Use only the supplied business records "
                     "for claims about this business. State when data is missing. Give practical "
                     "supermarket/retail advice when relevant. Currency is RWF.")
        input_text=(instruction+"\n\nBUSINESS DATA:\n"+
                    json.dumps(context,ensure_ascii=False)+"\n\nQUESTION:\n"+prompt)
        payload={"model":model,"input":input_text}
        headers={"Content-Type":"application/json","x-goog-api-key":key}
        def extract(data):
            # API responses may include output_text directly or text in model_output steps.
            if isinstance(data.get("output_text"),str) and data["output_text"].strip():
                return data["output_text"]
            parts=[]
            for step in data.get("steps",[]):
                if step.get("type")=="model_output":
                    content=step.get("content",[])
                    if isinstance(content,str):
                        parts.append(content)
                    elif isinstance(content,list):
                        for item in content:
                            if isinstance(item,dict) and item.get("type")=="text" and item.get("text"):
                                parts.append(item["text"])
            if parts:
                return "\n".join(parts)
            raise ValueError("Gemini response did not contain readable text.")
    req=urllib.request.Request(url,data=json.dumps(payload).encode(),headers=headers,method="POST")
    try:
        with urllib.request.urlopen(req,timeout=45) as response: data=json.loads(response.read().decode())
        return extract(data)
    except urllib.error.HTTPError as e:
        detail=e.read().decode(errors="replace")[:500]
        raise RuntimeError(f"AI provider returned HTTP {e.code}: {detail}") from e
    except (urllib.error.URLError, KeyError, IndexError, ValueError) as e:
        raise RuntimeError(f"Could not get a valid AI response: {e}") from e

@app.post("/api/ai")
@login_required
def ai_chat():
    d=request.get_json(silent=True) or {}
    prompt=str(d.get("message","")).strip()
    if not prompt: return err("Type a question first.")
    if len(prompt)>4000: return err("Keep your question under 4,000 characters.")
    uid=current_user()
    with connect_db() as db:
        user=db.execute("SELECT business_name,business_type FROM users WHERE id=?",(uid,)).fetchone()
        products=[dict(r) for r in db.execute("SELECT name,category,quantity,cost_price,selling_price,min_stock FROM products WHERE user_id=? ORDER BY name LIMIT 200",(uid,))]
        sales=db.execute("SELECT COALESCE(SUM(total),0) revenue, COALESCE(SUM(unit_cost*quantity),0) cogs, COUNT(*) transactions FROM sales WHERE user_id=?",(uid,)).fetchone()
        expenses=db.execute("SELECT COALESCE(SUM(amount),0) total FROM expenses WHERE user_id=?",(uid,)).fetchone()[0]
    context={"business":dict(user),"products":products,"sales":{"revenue":sales["revenue"],"cost_of_goods_sold":sales["cogs"],"transactions":sales["transactions"]},"expenses":expenses,
             "net_profit_estimate":sales["revenue"]-sales["cogs"]-expenses}
    try: return jsonify({"reply":call_ai(prompt,context)})
    except RuntimeError as e: return err(str(e),503)

@app.get("/api/health")
def health(): return jsonify({"status":"ok","database":str(DATABASE),"ai_provider":os.environ.get("AI_PROVIDER","gemini"),"cloud_sync_configured":cloud_enabled()})

init_db()
if __name__=="__main__":
    app.run(host="127.0.0.1",port=int(os.environ.get("PORT","5000")),debug=False)
