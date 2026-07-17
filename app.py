"""Anonymous auction / bidding app for Databricks Apps.

- Bidders register with name / phone / email (their private identity record).
- Everyone sees every item, its starting bid, and the CURRENT HIGHEST bid
  (amount only — never who placed it).
- A bid must be strictly greater than the current highest (>= starting price
  if no bids yet).
- All items in a batch share one end time; after it, bidding locks.
- Admin uploads an Excel of items; one button archives the old batch (bids
  preserved by batch number) and activates the new items.
"""
from __future__ import annotations

import io
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

# All dates/times in the UI are shown and entered in US Eastern time.
EASTERN = ZoneInfo("America/New_York")

# When hosted OUTSIDE Databricks (e.g. Streamlit Community Cloud), the app is
# not given Databricks credentials automatically. We surface them from Streamlit
# secrets into env BEFORE importing `db`, so the SDK's Config() picks them up.
# On Databricks Apps these are already injected, so this is a no-op there.
for _k in (
    "DATABRICKS_HOST",
    "DATABRICKS_CLIENT_ID",
    "DATABRICKS_CLIENT_SECRET",
    "LAKEBASE_INSTANCE_NAME",
    "PGHOST",
    "PGUSER",
    "ADMIN_PASSCODE",
):
    try:
        if _k not in os.environ and _k in st.secrets:
            os.environ[_k] = str(st.secrets[_k])
    except Exception:
        pass  # no secrets.toml present (e.g. on Databricks) — fine

import db  # noqa: E402  (must follow secret loading above)

st.set_page_config(page_title="Silent Auction", page_icon="🔨", layout="wide")

# Admin access. On a PUBLIC host, email-based admin is spoofable, so the real
# gate is a passcode (ADMIN_PASSCODE). If no passcode is configured we fall back
# to email allow-listing (safe on the login-gated Databricks Apps deployment).
ADMIN_PASSCODE = os.getenv("ADMIN_PASSCODE", "").strip()
ADMIN_EMAILS = {
    e.strip().lower()
    for e in os.getenv("ADMIN_EMAILS", "satya.sure@gmail.com").split(",")
    if e.strip()
}


@st.cache_resource
def _bootstrap():
    db.init_schema()
    return True


def money(x) -> str:
    return "—" if x is None else f"${x:,.2f}"


def fmt_dt(dt) -> str:
    # Handles None, pandas NaT, and both tz-aware and tz-naive timestamps.
    if dt is None or pd.isna(dt):
        return "—"
    try:
        if dt.tzinfo is None:
            # Assume UTC for naive values coming back from the DB driver.
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(EASTERN).strftime("%Y-%m-%d %I:%M %p %Z")
    except Exception:
        return str(dt)


def auction_open(batch: dict) -> bool:
    end = batch["end_time"]
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) < end


# --------------------------------------------------------------------------- #
# Boot
# --------------------------------------------------------------------------- #
try:
    _bootstrap()
except db.ColdStartError:
    # Lakebase is scale-to-zero and still resuming from idle.
    st.title("🔨 Silent Auction")
    st.info("⏳ The auction database is waking up from idle. This usually takes "
            "10–30 seconds. Click below to continue.")
    if st.button("Try again", type="primary"):
        _bootstrap.clear()
        st.rerun()
    st.stop()
except Exception as e:  # pragma: no cover
    st.title("🔨 Silent Auction")
    st.error(f"Could not connect to the database: {e}")
    if st.button("Retry"):
        _bootstrap.clear()
        st.rerun()
    st.stop()

st.title("🔨 Silent Auction")

if "bidder" not in st.session_state:
    st.session_state.bidder = None

# --------------------------------------------------------------------------- #
# Registration gate
# --------------------------------------------------------------------------- #
if st.session_state.bidder is None:
    st.subheader("Enter the auction")
    st.caption(
        "Your details identify **you** to the organizer only. "
        "Your bids are shown to everyone else **anonymously**."
    )
    with st.form("register"):
        name = st.text_input("Name")
        phone = st.text_input("Phone")
        email = st.text_input("Email")
        submitted = st.form_submit_button("Enter", type="primary")
    if submitted:
        if not (name.strip() and phone.strip() and email.strip()):
            st.error("Please fill in name, phone, and email.")
        elif "@" not in email:
            st.error("Please enter a valid email.")
        else:
            try:
                bidder_id = db.upsert_bidder(name, phone, email)
                st.session_state.bidder = {
                    "id": bidder_id,
                    "name": name.strip(),
                    "email": email.strip().lower(),
                }
                st.rerun()
            except Exception as e:
                st.error(f"Registration failed: {e}")
    st.stop()

bidder = st.session_state.bidder

# Admin gate. If a passcode is configured (public host), an admin must have
# entered it this session. Otherwise fall back to email allow-list.
if "is_admin" not in st.session_state:
    st.session_state.is_admin = (
        not ADMIN_PASSCODE and bidder["email"] in ADMIN_EMAILS
    )
is_admin = st.session_state.is_admin

# --------------------------------------------------------------------------- #
# Header row
# --------------------------------------------------------------------------- #
top_l, top_r = st.columns([3, 1])
with top_l:
    st.markdown(f"Signed in as **{bidder['name']}**  ·  bids are anonymous to others")
with top_r:
    if st.button("Log out"):
        for k in ("bidder", "is_admin"):
            st.session_state.pop(k, None)
        st.rerun()

# Organizer passcode entry (only relevant when a passcode is configured).
if ADMIN_PASSCODE and not is_admin:
    with st.expander("🔑 Organizer login"):
        code = st.text_input("Organizer passcode", type="password", key="admin_code")
        if st.button("Unlock admin"):
            if code == ADMIN_PASSCODE:
                st.session_state.is_admin = True
                st.rerun()
            else:
                st.error("Incorrect passcode.")

tabs = ["🏷️ Items & Bidding", "📜 My bids"]
if is_admin:
    tabs.append("⚙️ Admin")
selected = st.tabs(tabs)

active = db.get_active_batch()

# --------------------------------------------------------------------------- #
# Tab 1 — items & bidding
# --------------------------------------------------------------------------- #
with selected[0]:
    if active is None:
        st.info("No active auction batch yet. Ask the organizer to upload items.")
    else:
        is_open = auction_open(active)
        c1, c2, c3 = st.columns(3)
        c1.metric("Batch", f"#{active['batch_number']}")
        c2.metric("Ends", fmt_dt(active["end_time"]))
        c3.metric("Status", "🟢 Open" if is_open else "🔴 Closed")
        if not is_open:
            st.warning("This auction has ended. Bidding is locked; final highest bids shown.")

        if st.button("🔄 Refresh"):
            st.rerun()

        items = db.get_items_with_highest(active["batch_id"])
        df = pd.DataFrame(items)
        show = pd.DataFrame(
            {
                "#": df["item_number"],
                "Item": df["item_name"],
                "Starting": df["starting_bid"].map(money),
                "Highest bid": df["highest_bid"].map(money),
                "Bids": df["n_bids"],
            }
        )
        st.dataframe(show, hide_index=True, use_container_width=True)

        if is_open:
            st.divider()
            st.subheader("Place a bid")
            labels = {
                f"#{r['item_number']} — {r['item_name']}": r for r in items
            }
            pick = st.selectbox("Item", list(labels.keys()))
            row = labels[pick]
            floor = row["highest_bid"] if row["highest_bid"] is not None else row["starting_bid"]
            need = "greater than current highest" if row["highest_bid"] is not None else "at least the starting price"
            st.caption(f"Your bid must be **{need}** ({money(floor)}).")
            amount = st.number_input(
                "Your bid",
                min_value=0.0,
                value=float(floor),
                step=1.0,
                format="%.2f",
            )
            if st.button("Submit bid", type="primary"):
                try:
                    db.place_bid(row["item_id"], bidder["id"], amount)
                    st.success(f"Bid of {money(amount)} placed on {row['item_name']}!")
                    st.rerun()
                except db.BidError as e:
                    st.error(str(e))
                except Exception as e:
                    st.error(f"Could not place bid: {e}")

# --------------------------------------------------------------------------- #
# Tab 2 — my bids
# --------------------------------------------------------------------------- #
with selected[1]:
    if active is None:
        st.info("No active batch.")
    else:
        mine = db.get_my_bids(active["batch_id"], bidder["id"])
        if not mine:
            st.info("You haven't placed any bids in this batch yet.")
        else:
            mdf = pd.DataFrame(mine)
            st.dataframe(
                pd.DataFrame(
                    {
                        "#": mdf["item_number"],
                        "Item": mdf["item_name"],
                        "Your bid": mdf["amount"].map(money),
                        "Leading?": mdf["is_leading"].map(lambda x: "🥇 Yes" if x else "No"),
                        "Placed": mdf["created_at"].map(fmt_dt),
                    }
                ),
                hide_index=True,
                use_container_width=True,
            )

# --------------------------------------------------------------------------- #
# Tab 3 — admin
# --------------------------------------------------------------------------- #
if is_admin:
    with selected[2]:
        st.subheader("Upload a new item batch")
        st.caption(
            "Excel columns required: **item_number**, **item_name**, **starting_bid**. "
            "Activating archives the current batch (its bids are kept by batch number)."
        )

        uploaded = st.file_uploader("Items Excel (.xlsx)", type=["xlsx"])
        default_end = datetime.now(EASTERN) + timedelta(days=7)
        ec1, ec2 = st.columns(2)
        end_date = ec1.date_input("Auction end date (Eastern)", value=default_end.date())
        end_time_v = ec2.time_input("Auction end time (Eastern)", value=default_end.time().replace(microsecond=0))
        label = st.text_input("Batch label (optional)")

        parsed = None
        if uploaded is not None:
            try:
                raw = pd.read_excel(io.BytesIO(uploaded.getvalue()))
                raw.columns = [str(c).strip().lower() for c in raw.columns]
                required = {"item_number", "item_name", "starting_bid"}
                missing = required - set(raw.columns)
                if missing:
                    st.error(f"Missing columns: {', '.join(sorted(missing))}")
                else:
                    raw = raw[["item_number", "item_name", "starting_bid"]].dropna()
                    raw["item_number"] = raw["item_number"].astype(int)
                    raw["starting_bid"] = raw["starting_bid"].astype(float)
                    parsed = raw
                    st.success(f"Parsed {len(parsed)} items.")
                    st.dataframe(parsed, hide_index=True, use_container_width=True)
            except Exception as e:
                st.error(f"Could not read Excel: {e}")

        if parsed is not None and st.button(
            "🚀 Archive old batch & activate these items", type="primary"
        ):
            end_dt = datetime.combine(end_date, end_time_v, tzinfo=EASTERN)
            items = list(
                parsed.itertuples(index=False, name=None)
            )  # (item_number, item_name, starting_bid)
            try:
                res = db.activate_new_batch(items, end_dt, label or None)
                st.success(
                    f"Activated batch #{res['batch_number']} with {res['n_items']} items. "
                    f"Ends {fmt_dt(end_dt.astimezone(timezone.utc))}."
                )
                st.rerun()
            except Exception as e:
                st.error(f"Activation failed: {e}")

        st.divider()
        st.subheader("Batch history")
        batches = db.list_batches()
        if batches:
            bdf = pd.DataFrame(batches)
            st.dataframe(
                pd.DataFrame(
                    {
                        "Batch #": bdf["batch_number"],
                        "Label": bdf["label"],
                        "Active": bdf["is_active"].map(lambda x: "✅" if x else ""),
                        "Items": bdf["n_items"],
                        "Bids": bdf["n_bids"],
                        "Uploaded": bdf["uploaded_at"].map(fmt_dt),
                        "Ends": bdf["end_time"].map(fmt_dt),
                    }
                ),
                hide_index=True,
                use_container_width=True,
            )
        else:
            st.info("No batches yet.")
