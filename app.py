import streamlit as st
import pandas as pd
import smtplib
import imaplib
import email as email_lib
import re
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import date, timedelta
from pathlib import Path

from hubspot import enrich_companies

# ── Config ────────────────────────────────────────────────────────────────────
STATE_FILE = Path(__file__).parent / "state.csv"
CONTACTS_FILE = Path(__file__).parent / "contacts.csv"
TODAY = date.today()
MRR_THRESHOLD        = 500   # live customers ≥ €500 MRR → AM owned
HIGH_VALUE_THRESHOLD = 5000  # flag accounts above this for urgent attention
MIN_INVOICE_AMOUNT   = 50    # skip invoices below this
NON_LIVE_SKIP_DAYS   = 30    # non-live churned > this many days → start at email_3

# ── Workflow 1: Standard (< €2k total per customer) ───────────────────────────
# email_1 (Day 1) → +7d → email_2 (Day 8) → +14d → email_3 (Day 22) → +14d → email_4 (Day 36) → +7d → Debtist
W1_STAGES  = ["new", "email_1", "email_2", "email_3", "email_4", "debtist", "resolved"]
W1_NEXT    = {
    "new":     "email_1",
    "email_1": "email_2",
    "email_2": "email_3",
    "email_3": "email_4",
    "email_4": "debtist",
}
W1_WAIT    = {"email_1": 7, "email_2": 14, "email_3": 14, "email_4": 7}

# ── Workflow 2: High Value (≥ €2k total per customer) ────────────────────────
# Same 4 emails, then AM Review → AM Email → +7d → Debtist
W2_STAGES  = ["new", "email_1", "email_2", "email_3", "email_4", "am_review", "am_email", "debtist", "resolved"]
W2_NEXT    = {
    "new":       "email_1",
    "email_1":   "email_2",
    "email_2":   "email_3",
    "email_3":   "email_4",
    "email_4":   "am_review",
    "am_review": "am_email",
    "am_email":  "debtist",
}
W2_WAIT    = {"email_1": 7, "email_2": 14, "email_3": 14, "email_4": 7, "am_review": 0, "am_email": 7}

STAGE_LABEL = {
    "new": "New",
    "email_1": "Reminder", "email_2": "Follow-up", "email_3": "Urgent", "email_4": "Final Notice",
    "am_review": "AM Review", "am_email": "AM Email",
    "debtist": "Collections", "resolved": "Resolved",
}
STAGE_ICON = {
    "new": "🔵",
    "email_1": "🟢", "email_2": "🟡", "email_3": "🟠", "email_4": "🔴",
    "am_review": "👤", "am_email": "🟣",
    "debtist": "⚫", "resolved": "✅",
}

# Overdue thresholds for picking start stage on first contact
# If invoice is already significantly overdue, skip friendlier early stages
OVERDUE_START_STAGE = [
    (14,  "email_1"),  # < 14 days overdue  → Friendly Reminder
    (28,  "email_2"),  # 14–27 days overdue → Follow-up
    (50,  "email_3"),  # 28–49 days overdue → Urgent
    (999, "email_4"),  # 50+ days overdue   → Final Notice
]

def get_start_stage(days_overdue, is_live=True, days_since_churn=None):
    # Non-live customers churned > 30 days ago skip straight to Urgent
    if not is_live:
        churn_age = days_since_churn if days_since_churn is not None else days_overdue
        if churn_age > NON_LIVE_SKIP_DAYS:
            return "email_3"
    for threshold, stage in OVERDUE_START_STAGE:
        if days_overdue < threshold:
            return stage
    return "email_4"

# ── Email templates ────────────────────────────────────────────────────────────
TEMPLATES = {
    "email_1": {
        "subject": "Friendly Reminder — Invoice {invoice_number}",
        "body": (
            "Hi {customer_name},\n\n"
            "Just a quick note to let you know that invoice {invoice_number} for €{amount_due} "
            "was due on {due_date}. If you've already arranged payment, please ignore this — and thank you!\n\n"
            "If you have any questions about the invoice, feel free to reach out.\n\n"
            "{pay_link}\n\n"
            "Best,\n"
            "JOIN Finance Team\n"
            "collections@join.com"
        ),
    },
    "email_2": {
        "subject": "Following Up — Invoice {invoice_number} Overdue",
        "body": (
            "Hi {customer_name},\n\n"
            "Following up on invoice {invoice_number} for €{amount_due}, "
            "which is now {days_overdue} days overdue.\n\n"
            "If there's anything preventing payment or you'd like to discuss the invoice, "
            "just reply to this email — happy to help sort it out.\n\n"
            "{pay_link}\n\n"
            "Best,\n"
            "JOIN Finance Team\n"
            "collections@join.com"
        ),
    },
    "email_3": {
        "subject": "Action Required — Invoice {invoice_number}",
        "body": (
            "Hi {customer_name},\n\n"
            "Despite our previous reminders, invoice {invoice_number} for €{amount_due} "
            "remains unpaid and is now {days_overdue} days overdue.\n\n"
            "Please arrange payment within the next 7 days. If you're facing any difficulties, "
            "reply to this email and we'll work something out.\n\n"
            "{pay_link}\n\n"
            "JOIN Finance Team\n"
            "collections@join.com"
        ),
    },
    "email_4": {
        "subject": "Final Notice — Invoice {invoice_number}",
        "body": (
            "Hi {customer_name},\n\n"
            "This is a final notice regarding invoice {invoice_number} for €{amount_due}, "
            "now {days_overdue} days overdue.\n\n"
            "Please arrange payment within 5 business days. If we don't hear from you, "
            "this matter will be escalated to our Debt Collection Agency (Debtist) and "
            "additional collection fees will be applied.\n\n"
            "{pay_link}\n\n"
            "JOIN Finance Team\n"
            "collections@join.com"
        ),
    },
    "am_email": {
        "subject": "Quick Note — Invoice {invoice_number}",
        "body": (
            "Hi {customer_name},\n\n"
            "Just a quick note from my side — invoice {invoice_number} for €{amount_due} "
            "is still outstanding. If you've already sorted this, please ignore this message!\n\n"
            "If there's anything I can help with, just reply here.\n\n"
            "{pay_link}\n\n"
            "Best,\n"
            "{am_name}\n"
            "JOIN"
        ),
    },
}

# Maps (workflow, current_stage) → template key for the email to send now
# "new" stage is handled dynamically via get_start_stage() — not listed here
STAGE_EMAIL = {
    ("w1", "email_1"):   "email_2",
    ("w1", "email_2"):   "email_3",
    ("w1", "email_3"):   "email_4",
    ("w2", "email_1"):   "email_2",
    ("w2", "email_2"):   "email_3",
    ("w2", "email_3"):   "email_4",
    ("w2", "am_review"): "am_email",
}

# Stages that just advance (no email to send)
NO_EMAIL_ADVANCE = {
    ("w1", "email_4"):   "debtist",
    ("w2", "email_4"):   "am_review",
    ("w2", "am_email"):  "debtist",
}

# ── Gmail SMTP ─────────────────────────────────────────────────────────────────
def send_email(to_addr, subject, body):
    try:
        creds = st.secrets["gmail"]
    except (KeyError, FileNotFoundError):
        return False, "Gmail not configured in secrets.toml"
    try:
        msg = MIMEMultipart()
        msg["From"] = creds["sender"]
        msg["To"] = to_addr
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))
        with smtplib.SMTP(creds["smtp_host"], int(creds["smtp_port"])) as srv:
            srv.starttls()
            srv.login(creds["sender"], creds["app_password"])
            srv.send_message(msg)
        return True, ""
    except Exception as e:
        return False, str(e)


_COMPANY_WORDS = {
    "gmbh", "ltd", "llc", "ag", "sl", "ab", "bv", "sas", "sarl", "inc", "corp",
    "group", "team", "services", "consulting", "digital", "solutions", "technologies",
    "finance", "operations", "logistics", "retail", "trade", "industrial", "partners",
    "holding", "management", "international", "gbr", "kg", "ug", "mbh", "e.v.",
}
_TITLES = {"mr", "mrs", "ms", "dr", "prof", "herr", "frau", "sr", "sra"}

_LEGAL_SUFFIXES = re.compile(
    r'\s*\b(GmbH|Ltd\.?|LLC|AG|S\.L\.|SL|AB|BV|SAS|SARL|Inc\.?|Corp\.?|GbR|KG|UG|mbH|e\.V\.|S\.A\.)\b\.?\s*$',
    re.IGNORECASE,
)

def extract_first_name(customer_name, company=None):
    """Return a usable first name, or a stripped company name — never 'Hi there'."""
    # Try to get a real first name from customer_name
    if customer_name:
        parts = str(customer_name).strip().split()
        if parts:
            first = parts[0].rstrip(".,")
            if first.lower() in _TITLES and len(parts) > 1:
                first = parts[1].rstrip(".,")
            is_company_word = first.lower() in _COMPANY_WORDS
            is_short_abbrev = len(first) <= 3 and first.replace(".", "").isupper()
            if not is_company_word and not is_short_abbrev:
                return first

    # Fall back to cleaned company name
    src = company or customer_name or ""
    if src:
        cleaned = _LEGAL_SUFFIXES.sub("", str(src)).strip().rstrip("&,- ")
        if cleaned and cleaned.lower() not in _COMPANY_WORDS:
            return cleaned

    return "there"


def _has_reply(val):
    """Return True only if reply_received is a real non-empty, non-NaN value."""
    s = str(val).strip().lower()
    return s not in ("", "nan", "none", "false", "0")


_AUTO_REPLY_SUBJECTS = (
    "out of office", "abwesenheit", "abwesenheitsnotiz", "automatic reply",
    "auto reply", "auto-reply", "vacation", "away from", "i am out", "ich bin abwesend",
    "delivery status", "undeliverable", "mail delivery failed", "mailer-daemon",
)

def _is_auto_reply(msg):
    """Return True if the email looks like an auto-reply or OOO."""
    auto_submitted = (msg.get("Auto-Submitted", "") or "").lower()
    if auto_submitted and auto_submitted != "no":
        return True
    if msg.get("X-Autoreply") or msg.get("X-Auto-Response-Suppress"):
        return True
    subject = (msg.get("Subject", "") or "").lower()
    return any(pat in subject for pat in _AUTO_REPLY_SUBJECTS)


@st.cache_data(ttl=300)
def check_replies():
    """Scan collections@join.com inbox for replies matching known invoice numbers.
    Returns (matched_stripe_ids: set, unmatched: list of {subject, from}).
    Only looks at emails received today or later; auto-replies are ignored."""
    try:
        creds = st.secrets["gmail"]
    except (KeyError, FileNotFoundError):
        return set(), []
    try:
        df_state = pd.read_csv(STATE_FILE)
        # Only match invoices that have been contacted (stage != new)
        contacted = df_state[~df_state["sequence_stage"].isin(["resolved", "debtist", "new"])]
        inv_map = {
            str(r["invoice_number"]).upper(): r["stripe_invoice_id"]
            for _, r in contacted.iterrows()
            if pd.notna(r.get("invoice_number")) and str(r.get("invoice_number", "")).strip()
        }
        if not inv_map:
            return set(), []

        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(creds["sender"], creds["app_password"])
        mail.select("INBOX")

        # Only scan emails received from today onwards
        since = date.today().strftime("%d-%b-%Y")
        _, data = mail.search(None, f'(SINCE {since} NOT FROM "{creds["sender"]}")')

        matched_ids, unmatched = set(), []
        msg_nums = data[0].split() if data[0] else []

        for num in msg_nums:
            _, msg_data = mail.fetch(num, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT AUTO-SUBMITTED X-AUTOREPLY X-AUTO-RESPONSE-SUPPRESS)])")
            if not msg_data or not msg_data[0]:
                continue
            msg = email_lib.message_from_bytes(msg_data[0][1])
            if _is_auto_reply(msg):
                continue
            subject = msg.get("Subject", "") or ""
            from_addr = msg.get("From", "") or ""
            subject_up = subject.upper()

            found = False
            for inv_num, inv_id in inv_map.items():
                if inv_num in subject_up:
                    matched_ids.add(inv_id)
                    found = True
                    break
            if not found:
                unmatched.append({"subject": subject, "from": from_addr})

        mail.logout()
        return matched_ids, unmatched
    except Exception:
        return set(), []


def fill_template(row, tpl_key):
    tpl = TEMPLATES.get(tpl_key)
    if not tpl:
        return "", ""
    xero = str(row.get("xero_link", "") or "").strip()
    pay_link = f"Pay now → {xero}" if xero and xero.lower() not in ("nan", "none", "") else "Pay now → (contact us to arrange payment)"
    data = {
        "customer_name": extract_first_name(row.get("customer_name"), row.get("company")),
        "invoice_number": row.get("invoice_number", ""),
        "amount_due":     f"{float(row.get('amount_due', 0)):,.2f}",
        "due_date":       str(row.get("due_date", "")),
        "days_overdue":   int(row.get("days_overdue", 0)),
        "am_name":        row.get("am_name") or "your account manager",
        "pay_link":       pay_link,
    }
    return tpl["subject"].format(**data), tpl["body"].format(**data)


# ── Google Sheets connection ───────────────────────────────────────────────────
@st.cache_resource
def _get_gsheet():
    import gspread
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_info(
        dict(st.secrets["gcp_service_account"]),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    return gc.open_by_key(st.secrets["sheets"]["state_sheet_id"]).sheet1

def _use_sheets():
    return "sheets" in st.secrets and "gcp_service_account" in st.secrets


# ── Data I/O ───────────────────────────────────────────────────────────────────
@st.cache_data(ttl=10)
def load_data():
    if _use_sheets():
        sheet = _get_gsheet()
        records = sheet.get_all_records(default_blank="")
        df = pd.DataFrame(records)
    else:
        df = pd.read_csv(STATE_FILE)
    df["invoice_date"]       = pd.to_datetime(df["invoice_date"], errors="coerce").dt.date
    df["due_date"]           = pd.to_datetime(df["due_date"], errors="coerce").dt.date
    df["last_contacted_date"] = pd.to_datetime(df["last_contacted_date"], errors="coerce").dt.date
    df["paused_until"]       = pd.to_datetime(df["paused_until"], errors="coerce").dt.date
    df["amount_due"]         = pd.to_numeric(df["amount_due"], errors="coerce").fillna(0)

    # Enrich customer info from contacts.csv if available
    if CONTACTS_FILE.exists():
        contacts = pd.read_csv(CONTACTS_FILE)
        for col in ["customer_name", "customer_email", "company"]:
            if col in contacts.columns and col in df.columns:
                merged = df.merge(
                    contacts[["customer_id", col]].rename(columns={col: f"_c_{col}"}),
                    on="customer_id", how="left",
                )
                df[col] = df[col].where(df[col].notna() & (df[col] != ""), merged[f"_c_{col}"])

    # Per-customer total across active invoices (for high-value flagging)
    active_mask = ~df["sequence_stage"].isin(["resolved", "debtist"])
    cust_totals = df[active_mask].groupby("customer_id")["amount_due"].sum()
    df["customer_total"] = df["customer_id"].map(cust_totals).fillna(0)

    df["days_overdue"] = df["due_date"].apply(
        lambda d: max(0, (TODAY - d).days) if pd.notna(d) else 0
    )
    df["days_since_contact"] = df["last_contacted_date"].apply(
        lambda d: (TODAY - d).days if pd.notna(d) else None
    )

    # HubSpot enrichment: live status, MRR, AM assignment
    if "hubspot" in st.secrets:
        company_names = tuple(
            n for n in df["company"].dropna().unique() if str(n).strip()
        )
        hs = enrich_companies(company_names)
        df["mrr"]         = df["company"].map(lambda c: hs.get(c, {}).get("mrr", 0.0))
        df["is_live"]     = df["company"].map(lambda c: hs.get(c, {}).get("is_live", False))
        df["churn_date"]  = df["company"].map(lambda c: hs.get(c, {}).get("churn_date"))
        df["am_email_hs"] = df["company"].map(lambda c: hs.get(c, {}).get("am_email", ""))
        df["plan_tier"]   = df["company"].map(lambda c: hs.get(c, {}).get("plan_tier", ""))
        df["days_since_churn"] = df["churn_date"].apply(
            lambda d: (TODAY - date.fromisoformat(d)).days if d else None
        )
        # Only overwrite am_name if not already set manually in state.csv
        df["am_name"] = df.apply(
            lambda r: r["am_name"]
            if (pd.notna(r.get("am_name")) and str(r.get("am_name", "")).strip() not in ("", "nan"))
            else hs.get(r.get("company", ""), {}).get("am_name", ""),
            axis=1,
        )
    else:
        df["mrr"]              = 0.0
        df["is_live"]          = None
        df["churn_date"]       = None
        df["days_since_churn"] = None
        df["am_email_hs"]      = ""
        df["plan_tier"]        = ""

    # Effective workflow: live high-MRR → am_owned, else keep w1/w2 from state.csv
    def _eff_workflow(row):
        live = row.get("is_live")
        mrr  = row.get("mrr", 0) or 0
        if live and mrr >= MRR_THRESHOLD:
            return "am_owned"
        return row.get("workflow", "w1")

    df["effective_workflow"] = df.apply(_eff_workflow, axis=1)

    df["is_paused"]       = df["paused_until"].apply(lambda d: pd.notna(d) and d >= TODAY)
    df["action_needed"]   = df.apply(_needs_action, axis=1)
    df["next_action_date"] = df.apply(_next_action_date, axis=1)
    df["days_until_action"] = df["next_action_date"].apply(
        lambda d: (d - TODAY).days if d is not None else None
    )
    return df


def _next_action_date(row):
    stage    = row.get("sequence_stage", "")
    workflow = row.get("workflow", "w1")
    if stage in ("debtist", "resolved"):
        return None
    if _has_reply(row.get("reply_received", "")):
        return None
    if pd.notna(row.get("paused_until")) and row["paused_until"] >= TODAY:
        return row["paused_until"]
    if stage in ("new", "am_review"):
        return TODAY
    wait = (W1_WAIT if workflow == "w1" else W2_WAIT).get(stage)
    lcd  = row.get("last_contacted_date")
    if wait is not None and pd.notna(lcd):
        return lcd + timedelta(days=wait)
    return TODAY


def _needs_action(row):
    stage    = row.get("sequence_stage", "")
    workflow = row.get("workflow", "w1")
    if stage in ("debtist", "resolved"):
        return False
    # AM-owned invoices are handled in the AM List tab, not the Queue
    if row.get("effective_workflow") == "am_owned":
        return False
    if _has_reply(row.get("reply_received", "")):
        return False
    if pd.notna(row.get("paused_until")) and row["paused_until"] >= TODAY:
        return False

    if workflow == "w1":
        if stage == "new":
            return True
        wait = W1_WAIT.get(stage)
        if wait is None:
            return False
        dsc = row.get("days_since_contact")
        return dsc is not None and dsc >= wait

    if workflow == "w2":
        if stage in ("new", "am_review"):
            return True
        wait = W2_WAIT.get(stage)
        if wait is None:
            return False
        dsc = row.get("days_since_contact")
        return dsc is not None and dsc >= wait


    return False


def _write(updates):
    if _use_sheets():
        import gspread
        sheet = _get_gsheet()
        headers = sheet.row_values(1)
        col_idx = {h: i + 1 for i, h in enumerate(headers)}
        id_col  = sheet.col_values(1)  # stripe_invoice_id is column A
        id_to_row = {v: i + 1 for i, v in enumerate(id_col)}

        cells = []
        for inv_id, changes in updates:
            row_num = id_to_row.get(str(inv_id))
            if not row_num:
                continue
            for col_name, value in changes.items():
                if col_name not in col_idx:
                    # Append new column header if missing
                    new_col = len(headers) + 1
                    sheet.update_cell(1, new_col, col_name)
                    col_idx[col_name] = new_col
                    headers.append(col_name)
                cells.append(gspread.Cell(row_num, col_idx[col_name],
                                          "" if value is None else str(value)))
        if cells:
            sheet.update_cells(cells)
    else:
        df = pd.read_csv(STATE_FILE)
        for inv_id, changes in updates:
            idx = df[df["stripe_invoice_id"] == inv_id].index
            if len(idx) == 0:
                continue
            for col, val in changes.items():
                df.at[idx[0], col] = val
        df.to_csv(STATE_FILE, index=False)
    st.cache_data.clear()


def advance(inv_id, current_stage, workflow, extra=None, to_stage=None):
    if to_stage:
        next_stage = to_stage
    else:
        stage_map = W1_NEXT if workflow == "w1" else W2_NEXT
        next_stage = stage_map.get(current_stage, current_stage)
    changes = {"sequence_stage": next_stage, "last_contacted_date": TODAY.isoformat()}
    if extra:
        changes.update(extra)
    _write([(inv_id, changes)])


def mark_resolved(inv_id):
    _write([(inv_id, {"sequence_stage": "resolved", "last_contacted_date": TODAY.isoformat()})])


def import_invoices(raw: pd.DataFrame):
    """Parse a Stripe invoice CSV and add new rows to state.csv. Returns (added, skipped, error)."""
    required = ["id", "Number", "Customer", "Amount Due", "Date (UTC)"]
    missing = [c for c in required if c not in raw.columns]
    if missing:
        return 0, 0, f"Missing columns: {', '.join(missing)}"

    existing = pd.read_csv(STATE_FILE)
    existing_ids = set(existing["stripe_invoice_id"].dropna().astype(str))

    # Filter to open invoices only if Status column present
    if "Status" in raw.columns:
        raw = raw[raw["Status"].str.lower().isin(["open", "uncollectible"])]

    new_rows = []
    for _, row in raw.iterrows():
        inv_id = str(row.get("id", "")).strip()
        if not inv_id or inv_id in existing_ids:
            continue

        amount = pd.to_numeric(row.get("Amount Due", 0), errors="coerce") or 0
        if amount > 50000:  # Stripe sometimes exports in minor unit (cents)
            amount = amount / 100
        if amount < MIN_INVOICE_AMOUNT:
            continue

        invoice_date = str(row.get("Date (UTC)", "")).strip()
        due_raw = str(row.get("Due Date (UTC)", "")).strip()
        due_date = due_raw if due_raw and due_raw.lower() not in ("nan", "", "none") else invoice_date

        xero = str(row.get("Link (metadata)", "")).strip() if "Link (metadata)" in raw.columns else ""

        new_rows.append({
            "stripe_invoice_id": inv_id,
            "invoice_number":    str(row.get("Number", "")).strip(),
            "customer_id":       str(row.get("Customer", "")).strip(),
            "customer_name": "", "customer_email": "", "company": "",
            "amount_due":    amount,
            "invoice_date":  invoice_date,
            "due_date":      due_date,
            "workflow": "", "sequence_stage": "new",
            "last_contacted_date": "", "reply_received": "",
            "paused_until": "", "card_result": "", "am_name": "",
            "xero_link": xero, "notes": "",
        })

    if not new_rows:
        return 0, len(raw), None

    new_df = pd.DataFrame(new_rows)
    new_df["amount_due"] = pd.to_numeric(new_df["amount_due"], errors="coerce").fillna(0)

    # Assign workflow using per-customer total (existing active + new)
    active_ex = existing[~existing["sequence_stage"].isin(["resolved", "debtist"])]
    totals = (
        pd.concat([active_ex[["customer_id", "amount_due"]], new_df[["customer_id", "amount_due"]]])
        .groupby("customer_id")["amount_due"].sum()
    )
    new_df["workflow"] = new_df["customer_id"].map(
        lambda cid: "w2" if totals.get(cid, 0) >= WORKFLOW_THRESHOLD else "w1"
    )

    pd.concat([existing, new_df], ignore_index=True).to_csv(STATE_FILE, index=False)
    st.cache_data.clear()
    return len(new_rows), len(raw) - len(new_rows), None


def pause_invoice(inv_id, days):
    _write([(inv_id, {"paused_until": (TODAY + timedelta(days=days)).isoformat()})])


# ── Page ───────────────────────────────────────────────────────────────────────
st.set_page_config(page_title="Cash Collection · JOIN Finance", layout="wide", page_icon="💶")
st.markdown("## 💶 Cash Collection Dashboard")
st.caption(f"JOIN Finance · {TODAY.strftime('%d %B %Y')}")

df = load_data()

# Auto-detect replies via IMAP and mark matched invoices
_matched_ids, _unmatched_replies = check_replies()
if _matched_ids:
    _already_marked = set(df[df["reply_received"].apply(_has_reply)]["stripe_invoice_id"])
    _new_replies = _matched_ids - _already_marked
    if _new_replies:
        _write([(inv_id, {"reply_received": "auto-detected"}) for inv_id in _new_replies])
        st.rerun()

active     = df[~df["sequence_stage"].isin(["resolved", "debtist"])]
action_df  = df[df["action_needed"] & ~df["is_paused"]].copy()

priority_weight = {
    "new": 6, "am_review": 10,
    "email_1": 3, "email_2": 2, "am_email": 4,
}
action_df["_priority"] = action_df.apply(
    lambda r: r["amount_due"] * priority_weight.get(r["sequence_stage"], 1), axis=1
)
action_df = action_df.sort_values("_priority", ascending=False)

# ── Summary ────────────────────────────────────────────────────────────────────
w1_active = active[active["workflow"] == "w1"]
w2_active = active[active["workflow"] == "w2"]
c1, c2, c3, c4 = st.columns(4)
c1.metric("Outstanding", f"€{active['amount_due'].sum():,.0f}")
c2.metric("Actions today", len(action_df))
c3.metric("W1 open", len(w1_active), help="Standard (< €2,000 total)")
c4.metric("W2 open 🔥", len(w2_active), help="High value (≥ €2,000 total) — auto emails then AM")

st.divider()

@st.fragment
def _queue_cards(action_df, gmail_ok):
    selected_ids = [
        row["stripe_invoice_id"] for _, row in action_df.iterrows()
        if st.session_state.get(f"bulk_{row['stripe_invoice_id']}", False)
    ]
    if selected_ids:
        bc1, bc2, bc3 = st.columns([3, 2, 2])
        bc1.write(f"**{len(selected_ids)} selected**")
        if bc2.button(f"📧 Send {len(selected_ids)} emails", type="primary"):
            errs = []
            for bid in selected_ids:
                brow = action_df[action_df["stripe_invoice_id"] == bid].iloc[0]
                bstage    = brow["sequence_stage"]
                bworkflow = brow["workflow"]
                bhas_email = bool(brow.get("customer_email") and pd.notna(brow.get("customer_email")))
                if bstage == "new":
                    b_live  = brow.get("is_live")
                    b_churn = brow.get("days_since_churn")
                    b_suggested = get_start_stage(
                        int(brow.get("days_overdue", 0)),
                        is_live=bool(b_live) if b_live is not None else True,
                        days_since_churn=int(b_churn) if b_churn is not None else None,
                    )
                    b_email_stages = ["email_1", "email_2", "email_3", "email_4"]
                    b_opts = [
                        f"{STAGE_ICON[s]} {STAGE_LABEL[s]}" + (" (suggested)" if s == b_suggested else "")
                        for s in b_email_stages
                    ]
                    b_chosen = st.session_state.get(f"stage_pick_{bid}", b_opts[b_email_stages.index(b_suggested)])
                    bstart = b_email_stages[b_opts.index(b_chosen)] if b_chosen in b_opts else b_suggested
                    bsubj, bbody = fill_template(brow, bstart)
                    if gmail_ok and bhas_email:
                        ok, err = send_email(brow["customer_email"], bsubj, bbody)
                        if not ok:
                            errs.append(f"{brow.get('company', bid)}: {err[:60]}")
                            _write([(bid, {"last_send_status": f"failed: {err[:80]}"})])
                            continue
                    advance(bid, bstage, bworkflow, extra={"last_send_status": f"sent {TODAY}"}, to_stage=bstart)
                elif (bworkflow, bstage) in STAGE_EMAIL:
                    bsubj, bbody = fill_template(brow, STAGE_EMAIL[(bworkflow, bstage)])
                    if gmail_ok and bhas_email:
                        ok, err = send_email(brow["customer_email"], bsubj, bbody)
                        if not ok:
                            errs.append(f"{brow.get('company', bid)}: {err[:60]}")
                            _write([(bid, {"last_send_status": f"failed: {err[:80]}"})])
                            continue
                    advance(bid, bstage, bworkflow, extra={"last_send_status": f"sent {TODAY}"})
                elif (bworkflow, bstage) in NO_EMAIL_ADVANCE:
                    advance(bid, bstage, bworkflow)
                st.session_state[f"bulk_{bid}"] = False
            if errs:
                st.error("Some sends failed:\n" + "\n".join(errs))
            st.rerun(scope="app")
        if bc3.button("✗ Clear selection"):
            for bid in selected_ids:
                st.session_state[f"bulk_{bid}"] = False
            st.rerun()
        st.divider()

    for _, row in action_df.iterrows():
        inv_id   = row["stripe_invoice_id"]
        stage    = row["sequence_stage"]
        workflow = row["workflow"]
        wf_badge = "🔵 W1" if workflow == "w1" else "🟣 W2"
        company  = row.get("company") or row.get("customer_name") or row["customer_id"]
        has_email = bool(row.get("customer_email") and pd.notna(row.get("customer_email")))
        dsc = row.get("days_since_contact")
        contact_hint = f"last contact {int(dsc)}d ago" if pd.notna(dsc) else "never contacted"

        with st.container():
            chk_col, left, right = st.columns([0.4, 2.6, 6])

            with chk_col:
                st.checkbox("", key=f"bulk_{inv_id}", label_visibility="collapsed")

            with left:
                hv_flag = " 🔥" if row.get("customer_total", 0) >= HIGH_VALUE_THRESHOLD else ""
                st.write(f"**{company}**{hv_flag} {wf_badge}")
                if row.get("customer_name") and row.get("company"):
                    st.caption(f"👤 {row['customer_name']}")
                if has_email:
                    st.caption(f"✉️ {row.get('customer_email', '')}")
                else:
                    st.caption("⚠️ No email — add to contacts.csv")
                st.caption(
                    f"{STAGE_ICON.get(stage,'')} {STAGE_LABEL.get(stage,stage)} · "
                    f"#{row['invoice_number']} · €{row['amount_due']:,.2f}"
                )
                st.caption(f"📅 Due {row['due_date']} · {row['days_overdue']}d overdue · {contact_hint}")
                cust_total = row.get("customer_total", 0)
                if cust_total > row.get("amount_due", 0):
                    st.caption(f"💼 Total outstanding: €{cust_total:,.2f}")
                xero = str(row.get("xero_link", "") or "").strip()
                if xero and xero.lower() not in ("nan", "none", ""):
                    st.markdown(f'<a href="{xero}" style="text-decoration:none;font-size:0.85em;">🔗 Open in Xero</a>', unsafe_allow_html=True)

            with right:
                if stage == "new":
                    days_ov        = int(row.get("days_overdue", 0))
                    is_live_v      = row.get("is_live")
                    days_churn_v   = row.get("days_since_churn")
                    suggested = get_start_stage(
                        days_ov,
                        is_live=bool(is_live_v) if is_live_v is not None else True,
                        days_since_churn=int(days_churn_v) if days_churn_v is not None else None,
                    )
                    email_stages = ["email_1", "email_2", "email_3", "email_4"]
                    stage_options = [
                        f"{STAGE_ICON[s]} {STAGE_LABEL[s]}" + (" (suggested)" if s == suggested else "")
                        for s in email_stages
                    ]
                    chosen_label = st.radio(
                        "Select email to send:",
                        stage_options,
                        index=email_stages.index(suggested),
                        horizontal=True,
                        key=f"stage_pick_{inv_id}",
                    )
                    start_stage = email_stages[stage_options.index(chosen_label)]
                    stage_hint  = STAGE_LABEL.get(start_stage, start_stage)
                    subject, body = fill_template(row, start_stage)
                    if st.session_state.get(f"stage_pick_last_{inv_id}") != start_stage:
                        st.session_state[f"body_{inv_id}"] = body
                        st.session_state[f"stage_pick_last_{inv_id}"] = start_stage
                    if f"body_{inv_id}" not in st.session_state:
                        st.session_state[f"body_{inv_id}"] = body
                    with st.expander(f"✉️ {STAGE_ICON.get(start_stage,'')} {subject}"):
                        rc1, rc2 = st.columns([6, 1])
                        rc1.caption("Edit before sending:")
                        if rc2.button("↺", key=f"reset_{inv_id}"):
                            st.session_state[f"body_{inv_id}"] = body
                            st.rerun()
                        st.text_area("Email body", key=f"body_{inv_id}", height=200, label_visibility="collapsed")
                    b1, b2, b3 = st.columns(3)
                    if b1.button("✅ Mark as Resolved", key=f"ok_{inv_id}"):
                        mark_resolved(inv_id)
                        st.rerun(scope="app")
                    send_label = f"📧 Send {stage_hint}" if gmail_ok else "📧 Mark sent"
                    if b2.button(send_label, key=f"fail_{inv_id}", type="primary", disabled=not has_email and gmail_ok):
                        if gmail_ok and has_email:
                            ok, err = send_email(row["customer_email"], subject, st.session_state.get(f"body_{inv_id}", body))
                            if not ok:
                                _write([(inv_id, {"last_send_status": f"failed: {err[:80]}"})])
                                st.error(f"Send failed: {err}")
                                st.stop()
                        status = f"sent {TODAY}" if (gmail_ok and has_email) else "marked sent"
                        advance(inv_id, stage, workflow, extra={"last_send_status": status}, to_stage=start_stage)
                        st.rerun(scope="app")
                    if b3.button("⏸ Pause 7d", key=f"pause_{inv_id}"):
                        pause_invoice(inv_id, 7)
                        st.rerun(scope="app")

                elif stage == "am_review" and workflow == "w2":
                    st.caption("Automated emails sent — time for a personal touch.")
                    tpl_key = STAGE_EMAIL.get((workflow, stage))
                    subject, body = fill_template(row, tpl_key)
                    if f"body_{inv_id}" not in st.session_state:
                        st.session_state[f"body_{inv_id}"] = body
                    am_col, _ = st.columns([3, 5])
                    am_name_val = am_col.text_input("AM name", key=f"am_{inv_id}",
                                                    value=row.get("am_name") or "",
                                                    placeholder="e.g. Caro")
                    with st.expander(f"✉️ {subject}", expanded=True):
                        st.caption(f"From: {am_name_val or 'AM'} · To: {row.get('customer_email','—')}")
                        rc1, rc2 = st.columns([6, 1])
                        rc1.caption("Edit before sending:")
                        if rc2.button("↺", key=f"reset_{inv_id}"):
                            st.session_state[f"body_{inv_id}"] = body
                            st.rerun()
                        st.text_area("Email body", key=f"body_{inv_id}", height=200, label_visibility="collapsed")
                    b1, b2, b3 = st.columns(3)
                    send_lbl = "📧 Send AM email" if gmail_ok else "📧 Mark sent"
                    if b1.button(send_lbl, key=f"send_{inv_id}", type="primary", disabled=not has_email and gmail_ok):
                        if am_name_val:
                            _write([(inv_id, {"am_name": am_name_val})])
                        if gmail_ok and has_email:
                            ok, err = send_email(row["customer_email"], subject, st.session_state.get(f"body_{inv_id}", body))
                            if not ok:
                                _write([(inv_id, {"last_send_status": f"failed: {err[:80]}"})])
                                st.error(f"Send failed: {err}")
                                st.stop()
                        status = f"sent {TODAY}" if (gmail_ok and has_email) else "marked sent"
                        advance(inv_id, stage, workflow, extra={"last_send_status": status})
                        st.rerun(scope="app")
                    if b2.button("✅ Paid", key=f"paid_{inv_id}"):
                        mark_resolved(inv_id)
                        st.rerun(scope="app")
                    if b3.button("⏸ Pause 7d", key=f"pause_{inv_id}"):
                        pause_invoice(inv_id, 7)
                        st.rerun(scope="app")

                elif (workflow, stage) in STAGE_EMAIL:
                    tpl_key = STAGE_EMAIL[(workflow, stage)]
                    subject, body = fill_template(row, tpl_key)
                    if f"body_{inv_id}" not in st.session_state:
                        st.session_state[f"body_{inv_id}"] = body
                    with st.expander(f"✉️ {subject}", expanded=True):
                        st.caption(f"To: {row.get('customer_email','— no email —')}")
                        rc1, rc2 = st.columns([6, 1])
                        rc1.caption("Edit before sending:")
                        if rc2.button("↺", key=f"reset_{inv_id}"):
                            st.session_state[f"body_{inv_id}"] = body
                            st.rerun()
                        st.text_area("Email body", key=f"body_{inv_id}", height=200, label_visibility="collapsed")
                    b1, b2, b3 = st.columns(3)
                    send_lbl = "📧 Send email" if gmail_ok else "📧 Mark sent"
                    if b1.button(send_lbl, key=f"send_{inv_id}", type="primary", disabled=not has_email and gmail_ok):
                        if gmail_ok and has_email:
                            ok, err = send_email(row["customer_email"], subject, st.session_state.get(f"body_{inv_id}", body))
                            if not ok:
                                _write([(inv_id, {"last_send_status": f"failed: {err[:80]}"})])
                                st.error(f"Send failed: {err}")
                                st.stop()
                        status = f"sent {TODAY}" if (gmail_ok and has_email) else "marked sent"
                        advance(inv_id, stage, workflow, extra={"last_send_status": status})
                        st.rerun(scope="app")
                    if b2.button("✅ Paid", key=f"paid_{inv_id}"):
                        mark_resolved(inv_id)
                        st.rerun(scope="app")
                    if b3.button("⏸ Pause 7d", key=f"pause_{inv_id}"):
                        pause_invoice(inv_id, 7)
                        st.rerun(scope="app")

                elif (workflow, stage) in NO_EMAIL_ADVANCE:
                    next_s = NO_EMAIL_ADVANCE[(workflow, stage)]
                    if next_s == "am_review":
                        st.caption("Sequence complete — escalating to AM for personal follow-up.")
                        btn_label = "👤 Escalate to AM review"
                    else:
                        st.caption("Sequence complete — ready to refer to Collections (Debtist).")
                        btn_label = f"⚫ Move to {STAGE_LABEL[next_s]}"
                    b1, b2 = st.columns(2)
                    if b1.button(btn_label, key=f"next_{inv_id}", type="primary"):
                        advance(inv_id, stage, workflow)
                        st.rerun(scope="app")
                    if b2.button("✅ Paid", key=f"paid_{inv_id}"):
                        mark_resolved(inv_id)
                        st.rerun(scope="app")

            st.divider()


# ── Tabs ───────────────────────────────────────────────────────────────────────
am_df = df[
    (df["effective_workflow"] == "am_owned") &
    ~df["sequence_stage"].isin(["resolved", "debtist"])
].copy()

tab1, tab2, tab3, tab4, tab5, tab6, tab7, tab8 = st.tabs([
    f"⚡ Queue ({len(action_df)})",
    f"👤 AM List ({len(am_df)})" if not am_df.empty else "👤 AM List",
    "📋 Cases",
    "📧 Emails",
    "✅ Resolved",
    "📤 Debtist",
    "⚙️ Setup",
    "📖 Rules",
])

# ── Tab 1: Action Queue ────────────────────────────────────────────────────────
with tab1:
    # Unmatched replies — emails that arrived but couldn't be linked to an invoice
    if _unmatched_replies:
        with st.expander(f"↩️ {len(_unmatched_replies)} unmatched {'reply' if len(_unmatched_replies) == 1 else 'replies'} — review manually"):
            for r in _unmatched_replies:
                st.caption(f"**From:** {r['from']}  ·  **Subject:** {r['subject'] or '(no subject)'}")

    # Invoices paused due to detected reply
    _replied_df = df[df["reply_received"].apply(_has_reply)]
    if not _replied_df.empty:
        with st.expander(f"💬 {len(_replied_df)} invoice{'s' if len(_replied_df) != 1 else ''} paused — reply detected"):
            for _, r in _replied_df.iterrows():
                co = r.get("company") or r.get("customer_name") or r["customer_id"]
                st.caption(f"{co} · #{r['invoice_number']} · €{r['amount_due']:,.2f} · replied: {r['reply_received']}")
            st.caption("Mark as paid or resume from the Cases tab.")

    # Failed send banner
    if "last_send_status" in df.columns:
        failed_sends = df[
            df["last_send_status"].astype(str).str.startswith("failed") &
            ~df["sequence_stage"].isin(["resolved", "debtist"])
        ]
        if not failed_sends.empty:
            names = ", ".join(failed_sends["company"].fillna(failed_sends["customer_name"]).head(3).tolist())
            st.error(f"⚠️ {len(failed_sends)} email{'s' if len(failed_sends) > 1 else ''} failed to send: {names}{'…' if len(failed_sends) > 3 else ''} — check Cases tab for details.")

    q_search = st.text_input("🔍 Search queue", placeholder="Company, contact or invoice number…", label_visibility="collapsed")
    if q_search:
        mask = (
            action_df["company"].str.contains(q_search, case=False, na=False)
            | action_df["customer_name"].str.contains(q_search, case=False, na=False)
            | action_df["invoice_number"].str.contains(q_search, case=False, na=False)
        )
        action_df = action_df[mask]

    if action_df.empty:
        st.success("All clear — no actions needed today." if not q_search else f"No results for '{q_search}'.")
    else:
        gmail_ok = "gmail" in st.secrets
        if not gmail_ok:
            st.warning(
                "Gmail not configured — emails won't send. "
                "Add credentials to `.streamlit/secrets.toml`.",
                icon="⚠️",
            )

        _queue_cards(action_df, gmail_ok)

# ── Tab 2: AM List ────────────────────────────────────────────────────────────
with tab2:
    if am_df.empty:
        if "hubspot" not in st.secrets:
            st.info("HubSpot not connected — add `[hubspot] api_key` to secrets.toml to enable AM segmentation.")
        else:
            st.success("No AM-owned invoices outstanding.")
    else:
        total_am = am_df["amount_due"].sum()
        n_companies = am_df["company"].nunique()
        am_c1, am_c2, am_c3 = st.columns(3)
        am_c1.metric("AM-owned outstanding", f"€{total_am:,.0f}")
        am_c2.metric("Companies", n_companies)
        am_c3.metric("Invoices", len(am_df))

        # CSV export
        export_cols = ["company", "customer_name", "customer_email", "am_name",
                       "invoice_number", "amount_due", "days_overdue", "xero_link"]
        export_df = am_df[[c for c in export_cols if c in am_df.columns]].copy()
        export_df = export_df.sort_values(["am_name", "company"])
        csv_bytes = export_df.to_csv(index=False).encode()
        st.download_button("⬇️ Export full AM list (CSV)", csv_bytes,
                           file_name=f"am_list_{TODAY}.csv", mime="text/csv")

        st.divider()

        # Group by AM
        gmail_ok_am = "gmail" in st.secrets
        grouped = am_df.groupby("am_name", dropna=False)
        for am_name, group in sorted(grouped, key=lambda x: (x[0] or "zzz")):
            am_label = am_name if am_name and str(am_name) not in ("", "nan") else "Unassigned"
            am_total = group["amount_due"].sum()
            am_email = group["am_email_hs"].iloc[0] if "am_email_hs" in group.columns else ""

            with st.expander(f"**{am_label}** — {len(group)} {'invoice' if len(group)==1 else 'invoices'} · €{am_total:,.0f}", expanded=True):
                # Invoice table
                disp_cols = ["company", "customer_name", "customer_email",
                             "invoice_number", "amount_due", "days_overdue"]
                disp = group[[c for c in disp_cols if c in group.columns]].copy()
                disp = disp.sort_values("amount_due", ascending=False)
                st.dataframe(
                    disp,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "company":        st.column_config.TextColumn("Company"),
                        "customer_name":  st.column_config.TextColumn("Contact"),
                        "customer_email": st.column_config.TextColumn("Email"),
                        "invoice_number": st.column_config.TextColumn("Invoice"),
                        "amount_due":     st.column_config.NumberColumn("Amount (€)", format="€%.2f"),
                        "days_overdue":   st.column_config.NumberColumn("Days Overdue"),
                    },
                )

                # Send summary email to AM
                if gmail_ok_am and am_email and am_label != "Unassigned":
                    rows_text = "\n".join(
                        f"  • {r.get('company','?')} | #{r.get('invoice_number','?')} "
                        f"| €{r.get('amount_due',0):,.0f} | {int(r.get('days_overdue',0))}d overdue"
                        for _, r in group.iterrows()
                    )
                    am_subject = f"Outstanding invoices for your follow-up — {TODAY}"
                    am_body = (
                        f"Hi {am_label.split()[0]},\n\n"
                        f"Here are the outstanding invoices for your accounts as of {TODAY}. "
                        f"These customers are known to us and we'd appreciate a personal follow-up "
                        f"from your side where possible.\n\n"
                        f"{rows_text}\n\n"
                        f"Total: €{am_total:,.0f} across {len(group)} invoice{'s' if len(group)>1 else ''}.\n\n"
                        f"Let me know if you have context on any of these — happy to coordinate.\n\n"
                        f"JOIN Finance Team\n"
                        f"collections@join.com"
                    )
                    if st.button(f"📧 Send list to {am_label.split()[0]}", key=f"am_send_{am_label}"):
                        ok, err = send_email(am_email, am_subject, am_body)
                        if ok:
                            st.success(f"List sent to {am_email}")
                        else:
                            st.error(f"Failed: {err}")
                elif am_label != "Unassigned":
                    st.caption(f"⚠️ No email found for {am_label} in HubSpot — send manually.")

# ── Tab 3: All Cases ───────────────────────────────────────────────────────────
with tab3:
    fc1, fc2, fc3 = st.columns(3)
    wf_options = sorted(df["effective_workflow"].dropna().unique())
    wf_sel = fc1.multiselect(
        "Workflow", wf_options, default=wf_options,
        format_func=lambda w: {"w1": "W1 — Standard", "w2": "W2 — High Value", "am_owned": "👤 AM Owned"}.get(w, w),
    )
    all_stages = sorted(df["sequence_stage"].unique())
    stage_sel = fc2.multiselect(
        "Stage", all_stages,
        default=[s for s in all_stages if s not in ("resolved", "debtist")],
        format_func=lambda s: f"{STAGE_ICON.get(s,'')} {STAGE_LABEL.get(s,s)}",
    )
    search = fc3.text_input("Search company / invoice", "")

    filt = df[df["effective_workflow"].isin(wf_sel) & df["sequence_stage"].isin(stage_sel)]
    if search:
        mask = (
            filt["company"].str.contains(search, case=False, na=False)
            | filt["invoice_number"].str.contains(search, case=False, na=False)
            | filt["customer_name"].str.contains(search, case=False, na=False)
        )
        filt = filt[mask]

    base_cols = ["invoice_number", "company", "customer_name", "amount_due",
                 "days_overdue", "effective_workflow", "sequence_stage", "next_action_date", "action_needed"]
    if "last_send_status" in filt.columns:
        base_cols.append("last_send_status")

    disp = filt[[c for c in base_cols if c in filt.columns]].sort_values("days_overdue", ascending=False).copy()

    disp["sequence_stage"] = disp["sequence_stage"].map(
        lambda s: f"{STAGE_ICON.get(s,'')} {STAGE_LABEL.get(s,s)}"
    )
    disp["effective_workflow"] = disp["effective_workflow"].map(
        lambda w: {"w1": "W1 — Standard", "w2": "W2 — High Value", "am_owned": "👤 AM Owned"}.get(w, w)
    )
    disp["next_action_date"] = disp["next_action_date"].apply(
        lambda d: (
            "Today" if d == TODAY else
            f"In {(d - TODAY).days}d" if d is not None and d > TODAY else
            f"{(TODAY - d).days}d overdue" if d is not None else "—"
        )
    )

    col_config = {
        "invoice_number":   st.column_config.TextColumn("Invoice"),
        "company":          st.column_config.TextColumn("Company"),
        "customer_name":    st.column_config.TextColumn("Contact"),
        "amount_due":       st.column_config.NumberColumn("Amount (€)", format="€%.2f"),
        "days_overdue":     st.column_config.NumberColumn("Days Overdue"),
        "effective_workflow": st.column_config.TextColumn("Workflow"),
        "sequence_stage":   st.column_config.TextColumn("Stage"),
        "next_action_date": st.column_config.TextColumn("Next Action"),
        "action_needed":    st.column_config.CheckboxColumn("Due Today"),
        "last_send_status": st.column_config.TextColumn("Last Send"),
    }
    st.dataframe(disp, use_container_width=True, hide_index=True, column_config=col_config)
    st.caption(f"Showing {len(disp)} of {len(df)} records")

    # Allow manually clearing reply_received flag to resume an invoice
    replied_cases = filt[filt["reply_received"].apply(_has_reply)]
    if not replied_cases.empty:
        st.divider()
        st.caption("**Reply-paused invoices** — resume sequence if the reply didn't resolve the issue:")
        for _, r in replied_cases.iterrows():
            co = r.get("company") or r.get("customer_name") or r["customer_id"]
            rc1, rc2, rc3 = st.columns([4, 2, 2])
            rc1.caption(f"{co} · #{r['invoice_number']} · €{r['amount_due']:,.2f}")
            if rc2.button("▶ Resume sequence", key=f"resume_{r['stripe_invoice_id']}"):
                _write([(r["stripe_invoice_id"], {"reply_received": ""})])
                st.rerun()
            if rc3.button("✅ Mark paid", key=f"rpaid_{r['stripe_invoice_id']}"):
                mark_resolved(r["stripe_invoice_id"])
                st.rerun()

# ── Tab 4: Emails ──────────────────────────────────────────────────────────────
with tab4:
    st.caption("All emails auto-fill {variables} from invoice data and are editable before sending.")
    st.subheader("Both workflows — 4-stage automated sequence")

    email_meta = [
        ("email_1", "Day 1",  f"Sent when card charge fails. Warm and assumes oversight."),
        ("email_2", f"Day 8",  f"+{W1_WAIT['email_1']}d after reminder. Acknowledges it's overdue, offers to help."),
        ("email_3", f"Day 22", f"+{W1_WAIT['email_2']}d after follow-up. Firm — requests payment within 7 days."),
        ("email_4", f"Day 36", f"+{W1_WAIT['email_3']}d after urgent. Final notice — mentions Debtist escalation."),
    ]
    for tpl_key, day_label, desc in email_meta:
        tpl = TEMPLATES[tpl_key]
        label = f"{STAGE_ICON.get(tpl_key,'')} {STAGE_LABEL.get(tpl_key, tpl_key)} — {day_label}"
        with st.expander(label, expanded=(tpl_key == "email_1")):
            st.caption(desc)
            st.write(f"**Subject:** `{tpl['subject']}`")
            st.code(tpl["body"], language=None)

    st.caption("💡 **Smart stage selection:** if an invoice is already significantly overdue when first contacted, the app skips to the appropriate stage rather than sending a friendly reminder that doesn't fit.")

    st.divider()
    st.subheader("W2 only — AM personal follow-up (after all 4 automated emails)")
    tpl = TEMPLATES["am_email"]
    with st.expander(f"{STAGE_ICON.get('am_review','')} AM Email", expanded=True):
        st.caption(f"Sent personally by the assigned AM. Warm tone. Comes after the 4-stage automated sequence.")
        st.write(f"**Subject:** `{tpl['subject']}`")
        st.code(tpl["body"], language=None)

# ── Tab 5: Resolved ────────────────────────────────────────────────────────────
with tab5:
    resolved_df = df[df["sequence_stage"] == "resolved"].sort_values("last_contacted_date", ascending=False)
    if resolved_df.empty:
        st.info("No resolved invoices yet — they'll appear here once marked as paid.")
    else:
        total_recovered = resolved_df["amount_due"].sum()
        st.metric("Recovered", f"€{total_recovered:,.2f}", delta=f"{len(resolved_df)} invoices")
        disp_r = resolved_df[[
            "company", "customer_name", "invoice_number", "amount_due", "last_contacted_date",
        ]].copy()
        st.dataframe(disp_r, use_container_width=True, hide_index=True,
            column_config={
                "company": st.column_config.TextColumn("Company"),
                "customer_name": st.column_config.TextColumn("Contact"),
                "invoice_number": st.column_config.TextColumn("Invoice"),
                "amount_due": st.column_config.NumberColumn("Amount (€)", format="€%.2f"),
                "last_contacted_date": st.column_config.TextColumn("Resolved on"),
            })

# ── Tab 6: Debtist Handoff ─────────────────────────────────────────────────────
with tab6:
    deb_df = df[df["sequence_stage"] == "debtist"]
    if deb_df.empty:
        st.info("No cases currently flagged for Debtist.")
    else:
        st.write(f"**{len(deb_df)} cases** · Total: **€{deb_df['amount_due'].sum():,.2f}**")
        export_cols = [
            "customer_name", "company", "customer_email",
            "invoice_number", "amount_due", "days_overdue", "last_contacted_date", "xero_link",
        ]
        st.dataframe(
            deb_df[[c for c in export_cols if c in deb_df.columns]],
            use_container_width=True, hide_index=True,
            column_config={"amount_due": st.column_config.NumberColumn("Amount (€)", format="€%.2f")},
        )
        st.download_button(
            "⬇️ Export for Debtist",
            data=deb_df[[c for c in export_cols if c in deb_df.columns]].to_csv(index=False),
            file_name=f"debtist_handoff_{TODAY.isoformat()}.csv",
            mime="text/csv",
        )

# ── Tab 7: Setup ───────────────────────────────────────────────────────────────
with tab7:
    st.subheader("Gmail Configuration")
    if "gmail" in st.secrets:
        st.success(f"✅ Connected · sending from: {st.secrets['gmail']['sender']}")
    else:
        st.error("❌ Not configured — emails cannot be sent")
        st.write("Add the following to `.streamlit/secrets.toml` in the app folder:")
        st.code(
            '[gmail]\nsmtp_host    = "smtp.gmail.com"\nsmtp_port    = 587\n'
            'sender       = "collections@join.com"\napp_password = "xxxx xxxx xxxx xxxx"',
            language="toml",
        )
        st.caption(
            "Get an App Password at **myaccount.google.com/apppasswords** "
            "(requires 2-step verification on the account)."
        )

    st.divider()
    st.subheader("Customer Contacts")
    st.caption(
        "contacts.csv maps Stripe customer IDs to names and emails. "
        "The app merges it automatically at load time."
    )
    if CONTACTS_FILE.exists():
        ct = pd.read_csv(CONTACTS_FILE)
        st.dataframe(ct, use_container_width=True, hide_index=True)
        with_email = ct["customer_email"].notna().sum() if "customer_email" in ct.columns else 0
        st.caption(f"{len(ct)} contacts · {with_email} with email addresses")
    else:
        st.warning("contacts.csv not found in the app folder.")
        st.download_button(
            "⬇️ Download contacts.csv template",
            data="customer_id,customer_name,customer_email,company\n",
            file_name="contacts.csv",
            mime="text/csv",
        )

    st.divider()
    st.subheader("Invoice Coverage")
    missing_email = active[
        active["customer_email"].isna() | (active["customer_email"] == "")
    ]
    if len(missing_email):
        st.warning(f"{len(missing_email)} open invoices have no customer email address")
        st.dataframe(
            missing_email[["invoice_number", "customer_id", "company", "amount_due"]],
            use_container_width=True, hide_index=True,
        )
    else:
        st.success("All open invoices have email addresses.")

    st.divider()
    st.subheader("Import Invoices")
    st.caption(
        "Open the invoice sheet → File → Download → CSV. Upload here — new invoices are added "
        f"automatically. Already-tracked invoices and those under €{MIN_INVOICE_AMOUNT} are skipped."
    )
    uploaded = st.file_uploader("Upload Stripe invoice CSV", type="csv", key="invoice_upload")
    if uploaded is not None:
        try:
            raw = pd.read_csv(uploaded)
            preview_cols = [c for c in ["id", "Number", "Customer", "Amount Due", "Date (UTC)", "Status"] if c in raw.columns]
            st.caption(f"{len(raw)} rows in file")
            st.dataframe(raw[preview_cols].head(10), use_container_width=True, hide_index=True)
            if st.button("⬆️ Import invoices", type="primary"):
                added, skipped, err = import_invoices(raw)
                if err:
                    st.error(f"Import failed: {err}")
                else:
                    st.success(f"✅ {added} new invoices imported · {skipped} skipped (already tracked or below minimum)")
                    st.rerun()
        except Exception as e:
            st.error(f"Could not read file: {e}")

# ── Tab 8: Rules & Logic ───────────────────────────────────────────────────────
with tab8:
    st.subheader("How this works")
    st.caption("Everything a new user needs to know to operate the cash collection dashboard.")

    with st.expander("📥 Where does the data come from?", expanded=True):
        st.markdown(
            "- **Invoices** come from the [Stripe invoice Google Sheet](https://docs.google.com/spreadsheets/d/1Y_tFIuFrANjLmmwbg8pSpSCBSceblzocrb2KespWXCk). "
            "Download as CSV and upload in the **Setup** tab to add new invoices.\n"
            "- **Customer contacts** (name, email) live in `contacts.csv` alongside the app. "
            "They are sourced from BigQuery and merged automatically at load time.\n"
            "- Invoices below **€50** are ignored. Invoices already handed to Debtist are pre-marked and skipped in the queue."
        )

    with st.expander("🔀 W1 vs W2 — the two workflows"):
        st.markdown(
            f"The workflow is assigned based on the **total outstanding amount per customer** across all their open invoices.\n\n"
            f"| | W1 — Standard | W2 — High Value |\n"
            f"|---|---|---|\n"
            f"| **Threshold** | < €{WORKFLOW_THRESHOLD:,} total | ≥ €{WORKFLOW_THRESHOLD:,} total |\n"
            f"| **Stage 1 — Reminder** | Day 1 (automated) | Day 1 (automated) |\n"
            f"| **Stage 2 — Follow-up** | Day 8 (+7d) | Day 8 (+7d) |\n"
            f"| **Stage 3 — Urgent** | Day 22 (+14d) | Day 22 (+14d) |\n"
            f"| **Stage 4 — Final Notice** | Day 36 (+14d) | Day 36 (+14d) |\n"
            f"| **After stage 4** | → Debtist (+7d) | → AM Review (immediate) |\n"
            f"| **AM step** | — | AM personal email → +7d → Debtist |\n\n"
            f"🔥 Accounts with **> €{HIGH_VALUE_THRESHOLD:,}** total are flagged as high priority.\n\n"
            f"💡 **Smart start:** if an invoice is already overdue when first actioned, the app picks the appropriate stage — "
            f"invoices >50 days overdue go straight to Final Notice."
        )

    with st.expander("⏱️ Timing — when do items appear in the queue?"):
        st.markdown(
            "The queue shows invoices where the **next action is due today**.\n\n"
            "| Stage completed | Wait before next action |\n"
            "|---|---|\n"
            "| New → first email sent | Immediate |\n"
            "| Reminder (email 1) sent | **+7 days** |\n"
            "| Follow-up (email 2) sent | **+14 days** |\n"
            "| Urgent (email 3) sent | **+14 days** |\n"
            "| Final Notice (email 4) sent | **+7 days** |\n"
            "| AM Review assigned | Immediate |\n"
            "| AM Email sent | **+7 days** |\n\n"
            "Invoices that are **paused** (⏸) disappear until the pause expires. "
            "Invoices where a **reply was detected** are removed from the queue automatically."
        )

    with st.expander("↩️ Reply detection — how it works"):
        st.markdown(
            "Every 5 minutes the app checks the `collections@join.com` inbox via IMAP.\n\n"
            "- It scans **subject lines** for invoice numbers (e.g. `Invoice FADB34FC-0042`)\n"
            "- If a match is found, the invoice is **automatically removed from the queue** regardless of who the reply came from\n"
            "- **Auto-replies and OOO emails** are filtered out (checked via headers and subject keywords in EN + DE)\n"
            "- Emails that can't be matched to an invoice appear as **unmatched replies** at the top of the Queue tab for manual review\n"
            "- To **resume** a paused invoice (reply didn't resolve it), go to the Cases tab → scroll to 'Reply-paused invoices' → click Resume"
        )

    with st.expander("🎬 Step-by-step: actioning an invoice"):
        st.markdown(
            "1. Open the **⚡ Queue** tab — invoices due for action are listed, highest priority first\n"
            "2. For **'New'** invoices: first try to charge the card via Retool/Stripe\n"
            "   - If card succeeds → click **Card charged OK → Resolved** ✅\n"
            "   - If card fails → click **Card failed → Send email 1** — email is sent automatically\n"
            "3. For **email stages**: preview the email (edit if needed), then click **Send email**\n"
            "4. For **W2 AM Review**: enter the AM's name, preview the personalised email, click send\n"
            "5. For **final escalation**: click to move to Collections (Debtist)\n\n"
            "You can also:\n"
            "- **⏸ Pause 7d** — snooze an invoice if you're in active conversation\n"
            "- **✅ Mark paid** — resolve immediately if payment is confirmed outside the sequence"
        )

    with st.expander("📤 Debtist handoff"):
        st.markdown(
            "Once an invoice reaches **Collections** stage it moves to the Debtist tab.\n\n"
            "- Use the **Export for Debtist** button to download a CSV with all handoff cases\n"
            "- The export includes: customer name, company, email, invoice number, amount, days overdue, Xero link\n"
            "- Invoices that were already handed to Debtist **before this tool existed** are pre-loaded "
            "in the Debtist tab — they were imported directly from the sheet metadata"
        )

    with st.expander("✅ Resolving invoices"):
        st.markdown(
            "Mark an invoice as **Resolved** any time payment is confirmed:\n\n"
            "- From the **Queue**: click ✅ Paid on any invoice card\n"
            "- From the **Cases** tab: filter to the invoice and use the resume/paid buttons\n\n"
            "Resolved invoices move to the **✅ Resolved** tab with a running total of recovered amount."
        )

    with st.expander("⚙️ Technical setup"):
        st.markdown(
            f"- **Sender email:** `collections@join.com` via Gmail SMTP\n"
            f"- **Reply scanning:** IMAP on `collections@join.com`, cached every 5 minutes\n"
            f"- **Data files:** `state.csv` (invoice state), `contacts.csv` (customer emails)\n"
            f"- **To run:** `cd ~/cash-collection && python3 -m streamlit run app.py`\n"
            f"- **Gmail credentials:** stored in `~/.streamlit/secrets.toml` (never commit this file)\n\n"
            f"Config values (editable at the top of `app.py`):\n"
            f"- `WORKFLOW_THRESHOLD` = €{WORKFLOW_THRESHOLD:,} (W1/W2 cutoff)\n"
            f"- `HIGH_VALUE_THRESHOLD` = €{HIGH_VALUE_THRESHOLD:,} (🔥 flag)\n"
            f"- `MIN_INVOICE_AMOUNT` = €{MIN_INVOICE_AMOUNT} (skip below this)\n"
            f"- W1 waits: Email 1 → +{W1_WAIT['email_1']}d → Email 2 → +{W1_WAIT['email_2']}d → Debtist\n"
            f"- W2 waits: same + AM Review (immediate) → AM Email → +{W2_WAIT['am_email']}d → Debtist"
        )
