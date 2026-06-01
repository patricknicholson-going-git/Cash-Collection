import streamlit as st
import pandas as pd
from datetime import date
from pathlib import Path


DATA_FILE = Path(__file__).parent / "state.csv"
TODAY = date.today()

STAGE_LABEL = {
    "new": "New",
    "chased": "Contacted",
    "escalated": "Contacted ×2",
    "final_notice": "Final Notice",
    "debtist": "Collections",
    "resolved": "Resolved",
}
STAGE_ICON = {
    "new": "🔵",
    "chased": "🟡",
    "escalated": "🟠",
    "final_notice": "🔴",
    "debtist": "⚫",
    "resolved": "🟢",
}
# Days to wait after last contact before triggering next action
ACTION_THRESHOLD = {
    "new": 0,       # contact immediately once overdue
    "chased": 3,    # wait 3 days after first contact
    "escalated": 7, # wait 7 days after second contact
    "final_notice": 7,
}
NEXT_STAGE = {
    "new": "chased",
    "chased": "escalated",
    "escalated": "final_notice",
    "final_notice": "debtist",
}
STAGE_ORDER = ["new", "chased", "escalated", "final_notice", "debtist", "resolved"]

TEMPLATES = {
    "new": {
        "subject": "Outstanding Invoice – {invoice_number} (€{amount_owed})",
        "body": (
            "Dear {customer_name},\n\n"
            "I hope this message finds you well. We're writing to let you know that "
            "invoice {invoice_number} for €{amount_owed}, issued to {company}, "
            "was due on {due_date} and remains outstanding.\n\n"
            "Please arrange payment at your earliest convenience. If you have any "
            "questions or have already processed this payment, please let us know.\n\n"
            "Best regards,\nJOIN Finance Team"
        ),
    },
    "chased": {
        "subject": "Follow-Up: Unpaid Invoice {invoice_number} (€{amount_owed})",
        "body": (
            "Dear {customer_name},\n\n"
            "This is a follow-up to our previous reminder regarding invoice "
            "{invoice_number} for €{amount_owed}, now {days_overdue} days overdue.\n\n"
            "We have not yet received payment or a response from {company}. "
            "Please settle the outstanding balance or contact us to discuss.\n\n"
            "Best regards,\nJOIN Finance Team"
        ),
    },
    "escalated": {
        "subject": "Urgent: Invoice {invoice_number} – Immediate Attention Required",
        "body": (
            "Dear {customer_name},\n\n"
            "We have now contacted you twice regarding invoice {invoice_number} for "
            "€{amount_owed}, which remains unpaid at {days_overdue} days overdue.\n\n"
            "We ask that {company} arrange payment immediately. If we do not hear "
            "from you, we will be required to escalate this matter further.\n\n"
            "Best regards,\nJOIN Finance Team"
        ),
    },
    "final_notice": {
        "subject": "Final Notice – Invoice {invoice_number} Before Collections Referral",
        "body": (
            "Dear {customer_name},\n\n"
            "This is a final notice regarding invoice {invoice_number} for "
            "€{amount_owed}. Despite multiple attempts to reach {company}, "
            "this invoice remains unpaid at {days_overdue} days overdue.\n\n"
            "If payment is not received within 7 days, your account will be "
            "referred to our collections partner.\n\n"
            "Best regards,\nJOIN Finance Team"
        ),
    },
}


def fill_template(row, stage):
    tpl = TEMPLATES.get(stage)
    if not tpl:
        return "", ""
    data = {
        "customer_name": row["customer_name"],
        "company": row["company"],
        "invoice_number": row["invoice_number"],
        "amount_owed": f"{row['amount_owed']:,.0f}",
        "due_date": str(row["due_date"]),
        "days_overdue": int(row["days_overdue"]),
    }
    return tpl["subject"].format(**data), tpl["body"].format(**data)


@st.cache_data(ttl=10)
def load_data():
    df = pd.read_csv(DATA_FILE)
    df["due_date"] = pd.to_datetime(df["due_date"]).dt.date
    df["last_contacted_date"] = pd.to_datetime(
        df["last_contacted_date"], errors="coerce"
    ).dt.date
    df["days_overdue"] = df["due_date"].apply(lambda d: max(0, (TODAY - d).days))
    df["days_since_contact"] = df["last_contacted_date"].apply(
        lambda d: (TODAY - d).days if pd.notna(d) else None
    )
    df["action_needed"] = df.apply(_needs_action, axis=1)
    # Priority: new clients first (must contact immediately), then by amount
    stage_weight = {"new": 4, "chased": 2, "escalated": 1.5, "final_notice": 1}
    df["priority_score"] = df.apply(
        lambda r: r["amount_owed"] * stage_weight.get(r["sequence_stage"], 0), axis=1
    )
    return df


def _needs_action(row):
    s = row["sequence_stage"]
    threshold = ACTION_THRESHOLD.get(s)
    if threshold is None:
        return False
    if s == "new":
        return row.get("days_overdue", 0) >= 1
    dsc = row.get("days_since_contact")
    return dsc is not None and dsc >= threshold


def _write_updates(updates):
    df = pd.read_csv(DATA_FILE)
    for customer_id, changes in updates:
        idx = df[df["customer_id"] == customer_id].index[0]
        for col, val in changes.items():
            df.at[idx, col] = val
    df.to_csv(DATA_FILE, index=False)
    st.cache_data.clear()


def mark_sent_bulk(items):
    _write_updates([
        (cid, {
            "sequence_stage": NEXT_STAGE.get(stage, stage),
            "last_contacted_date": TODAY.isoformat(),
        })
        for cid, stage in items
    ])


def flag_debtist(customer_id):
    _write_updates([(customer_id, {
        "sequence_stage": "debtist",
        "last_contacted_date": TODAY.isoformat(),
    })])



# ── Page setup ────────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Cash Collection · JOIN Finance",
    layout="wide",
    page_icon="💶",
)

st.markdown("## 💶 Cash Collection Dashboard")
st.caption(f"JOIN Finance · {TODAY.strftime('%d %B %Y')}")

df = load_data()
unresolved = df[~df["sequence_stage"].isin(["resolved", "debtist"])]
not_contacted = df[df["sequence_stage"] == "new"]
action_df = df[df["action_needed"]].sort_values("priority_score", ascending=False)
avg_overdue = int(unresolved["days_overdue"].mean()) if len(unresolved) else 0

# ── KPIs ──────────────────────────────────────────────────────────────────────

c1, c2, c3, c4 = st.columns(4)
c1.metric("Total outstanding", f"€{unresolved['amount_owed'].sum():,.0f}")
c2.metric("Unresolved cases", len(unresolved))
c3.metric("Never contacted", len(not_contacted))
c4.metric("Avg days overdue", avg_overdue)

st.divider()

# ── Stage summary bar ─────────────────────────────────────────────────────────

stage_cols = st.columns(len(STAGE_ORDER))
for i, s in enumerate(STAGE_ORDER):
    subset = df[df["sequence_stage"] == s]
    count = len(subset)
    total = subset["amount_owed"].sum()
    icon = STAGE_ICON.get(s, "")
    label = STAGE_LABEL.get(s, s)
    stage_cols[i].metric(
        f"{icon} {label}",
        count,
        delta=f"€{total:,.0f}" if count > 0 else None,
        delta_color="off",
    )

st.divider()

# ── Tabs ──────────────────────────────────────────────────────────────────────

tab1, tab2, tab3, tab4 = st.tabs([
    f"⚡ Today's Actions ({len(action_df)})",
    "📋 All Cases",
    "📧 Email Templates",
    "📤 Debtist Handoff",
])

# ── Tab 1: Action Queue ───────────────────────────────────────────────────────

with tab1:
    if action_df.empty:
        st.success("All clear — no actions needed today.")
    else:
        if "selected" not in st.session_state:
            st.session_state.selected = {}

        # Controls row
        head_col, btn_col = st.columns([4, 2])
        head_col.caption(
            f"**{len(action_df)} clients** need follow-up · "
            "new entries appear first, then by amount owed"
        )

        b_all, b_send = btn_col.columns(2)

        if b_all.button("Select all"):
            for cid in action_df["customer_id"]:
                st.session_state.selected[cid] = True
            st.rerun()

        selected_ids = [
            cid for cid in action_df["customer_id"]
            if st.session_state.selected.get(cid, False)
        ]

        if b_send.button(
            f"✅ Send {len(selected_ids)}" if selected_ids else "✅ Send",
            disabled=len(selected_ids) == 0,
            type="primary",
        ):
            items = [
                (cid, action_df[action_df["customer_id"] == cid]["sequence_stage"].iloc[0])
                for cid in selected_ids
            ]
            mark_sent_bulk(items)
            for cid in selected_ids:
                st.session_state.selected.pop(cid, None)
            st.rerun()

        st.write("")

        for _, row in action_df.iterrows():
            cid = row["customer_id"]
            stage = row["sequence_stage"]
            icon = STAGE_ICON.get(stage, "⚪")
            label = STAGE_LABEL.get(stage, stage)
            subject, body = fill_template(row, stage)
            dsc = row["days_since_contact"]
            contact_label = f"last contact {int(dsc)}d ago" if pd.notna(dsc) else "never contacted"

            col_chk, col_info, col_email = st.columns([0.5, 2.5, 5])

            with col_chk:
                checked = st.checkbox(
                    "",
                    key=f"chk_{cid}",
                    value=st.session_state.selected.get(cid, False),
                )
                st.session_state.selected[cid] = checked

            with col_info:
                st.write(f"**{row['company']}**")
                st.write(f"{row['customer_name']}")
                st.caption(
                    f"{icon} {label} · €{row['amount_owed']:,.0f} · "
                    f"{row['days_overdue']}d overdue · {contact_label}"
                )

            with col_email:
                # Initialise editable body in session state on first render
                if f"body_{cid}" not in st.session_state:
                    st.session_state[f"body_{cid}"] = body

                with st.expander(f"✉️  {subject}"):
                    edit_label, reset_btn = st.columns([5, 1])
                    edit_label.caption("Edit before sending:")
                    if reset_btn.button("↺ Reset", key=f"reset_{cid}"):
                        st.session_state[f"body_{cid}"] = body
                        st.rerun()
                    st.text_area(
                        "",
                        key=f"body_{cid}",
                        height=185,
                        label_visibility="collapsed",
                    )

            st.divider()

        # Bottom send button for long lists
        if len(action_df) > 5:
            selected_ids = [
                cid for cid in action_df["customer_id"]
                if st.session_state.selected.get(cid, False)
            ]
            if st.button(
                f"✅ Send {len(selected_ids)} selected" if selected_ids else "✅ Send selected",
                disabled=len(selected_ids) == 0,
                type="primary",
                key="send_bottom",
            ):
                items = [
                    (cid, action_df[action_df["customer_id"] == cid]["sequence_stage"].iloc[0])
                    for cid in selected_ids
                ]
                mark_sent_bulk(items)
                for cid in selected_ids:
                    st.session_state.selected.pop(cid, None)
                st.rerun()

# ── Tab 2: All Cases ──────────────────────────────────────────────────────────

with tab2:
    col_filter, col_search = st.columns([2, 3])
    selected_stages = col_filter.multiselect(
        "Filter by stage",
        options=STAGE_ORDER,
        default=["new", "chased", "escalated", "final_notice"],
        format_func=lambda s: f"{STAGE_ICON.get(s, '')} {STAGE_LABEL.get(s, s)}",
    )
    search = col_search.text_input("Search company or contact", "")

    filtered = df[df["sequence_stage"].isin(selected_stages)]
    if search:
        mask = (
            filtered["company"].str.contains(search, case=False, na=False)
            | filtered["customer_name"].str.contains(search, case=False, na=False)
        )
        filtered = filtered[mask]

    display = filtered[[
        "customer_name", "company", "invoice_number", "amount_owed",
        "days_overdue", "sequence_stage", "days_since_contact",
        "reply_received", "action_needed",
    ]].sort_values("days_overdue", ascending=False).copy()

    display["sequence_stage"] = display["sequence_stage"].map(
        lambda s: f"{STAGE_ICON.get(s, '')} {STAGE_LABEL.get(s, s)}"
    )

    st.dataframe(
        display,
        use_container_width=True,
        hide_index=True,
        column_config={
            "customer_name": st.column_config.TextColumn("Contact"),
            "company": st.column_config.TextColumn("Company"),
            "invoice_number": st.column_config.TextColumn("Invoice"),
            "amount_owed": st.column_config.NumberColumn("Amount (€)", format="€%.0f"),
            "days_overdue": st.column_config.NumberColumn("Days Overdue"),
            "sequence_stage": st.column_config.TextColumn("Stage"),
            "days_since_contact": st.column_config.NumberColumn("Days Since Contact"),
            "reply_received": st.column_config.TextColumn("Reply"),
            "action_needed": st.column_config.CheckboxColumn("Action Needed"),
        },
    )
    st.caption(f"Showing {len(display)} of {len(df)} records")

# ── Tab 3: Email Templates ─────────────────────────────────────────────────────

with tab3:
    st.caption(
        "Standard templates sent at each stage. "
        "Variables in {curly_braces} are filled automatically from invoice data. "
        "Each client's email can be customised individually using AI from the Action Queue."
    )
    st.write("")

    stage_descriptions = {
        "new": "Sent immediately when an invoice becomes overdue. Friendly, assumes oversight.",
        "chased": f"Sent {ACTION_THRESHOLD['chased']} days after first contact with no response. Polite but firmer.",
        "escalated": f"Sent {ACTION_THRESHOLD['escalated']} days after second contact with no response. Direct, references prior outreach.",
        "final_notice": f"Sent {ACTION_THRESHOLD['final_notice']} days after third contact. Formal — mentions collections referral.",
    }

    for stage in ["new", "chased", "escalated", "final_notice"]:
        tpl = TEMPLATES[stage]
        with st.expander(
            f"{STAGE_ICON[stage]}  Stage: {STAGE_LABEL[stage]}",
            expanded=(stage == "new"),
        ):
            st.caption(stage_descriptions[stage])
            st.write(f"**Subject:** `{tpl['subject']}`")
            st.code(tpl["body"], language=None)

# ── Tab 4: Debtist Handoff ─────────────────────────────────────────────────────

with tab4:
    debtist_df = df[df["sequence_stage"] == "debtist"]
    if debtist_df.empty:
        st.info("No cases currently flagged for collections.")
    else:
        st.write(
            f"**{len(debtist_df)} cases** · "
            f"Total: **€{debtist_df['amount_owed'].sum():,.0f}**"
        )
        export_cols = [
            "customer_name", "company", "customer_email",
            "invoice_number", "amount_owed", "days_overdue", "last_contacted_date",
        ]
        st.dataframe(
            debtist_df[export_cols],
            use_container_width=True,
            hide_index=True,
            column_config={
                "amount_owed": st.column_config.NumberColumn("Amount (€)", format="€%.0f")
            },
        )
        st.download_button(
            "⬇️ Export for Debtist",
            data=debtist_df[export_cols].to_csv(index=False),
            file_name=f"debtist_handoff_{TODAY.isoformat()}.csv",
            mime="text/csv",
        )
