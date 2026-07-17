# 🔨 Silent Auction

Anonymous silent-auction bidding web app. Bidders see every item and the
current **highest bid amount** (never who bid). A bid must beat the current
highest. All items share one end time; bidding locks after it. Items load from
an Excel file, one batch at a time — old batches (and their bids) are retained.

The app is a **Streamlit** front end backed by a **Databricks Lakebase**
(managed PostgreSQL) database. It is designed to be hosted on **Streamlit
Community Cloud** so bidders get a plain public URL with **no login/account**.

## Features
- **No account for bidders** — they just open the link and register in-app with
  name / phone / email. That identifies them to the organizer only; bids are
  anonymous to every other bidder.
- **Bidding rule** — a bid must be strictly greater than the current highest
  (or ≥ the starting price if there are no bids yet). Concurrent bids on the
  same item are serialized with a row-level lock so two people can't both win.
- **Single end time** per batch; after it, bidding is locked and final highest
  bids stay visible.
- **My bids** tab — each bidder sees their own bids and whether they're leading.
- **Organizer (admin) tab**, gated by a passcode — upload a new items Excel and
  activate it with one button. The previous batch and all its bids are kept
  under their batch number (full history, nothing deleted).

## Excel format
Required columns (case-insensitive): `item_number`, `item_name`, `starting_bid`.
A sample 10-item file (`items.xlsx`) is included; regenerate with
`python make_sample_excel.py`.

## Deploy on Streamlit Community Cloud
1. This repo is already the app. Go to https://share.streamlit.io → **New app**.
2. Pick this repo (`satyarsure/silent-auction`), branch `main`, main file
   `app.py`.
3. Under **Advanced settings → Secrets**, paste the contents of
   `.streamlit/secrets.toml.example` and fill in the real values:
   - `DATABRICKS_CLIENT_SECRET` — the OAuth secret for the app service
     principal (kept private; ask the organizer / see below).
   - `ADMIN_PASSCODE` — pick a secret; whoever enters it in the app gets the
     Admin tab.
4. Deploy. Share the resulting `*.streamlit.app` URL with your bidders.

The app authenticates to Databricks as the `silent-auction` service principal
(which owns the database tables) and mints short-lived Lakebase OAuth tokens per
connection. No personal credentials are used.

## Databricks resources (already provisioned)
| Resource | Name |
|----------|------|
| Lakebase (Postgres) | `auction-lakebase` (schema `auction`, db `databricks_postgres`) |
| App service principal | `app-69f6zm silent-auction` — owns the tables, used by the external host |

> A parallel copy also runs as a **Databricks App** (`silent-auction`) behind
> Databricks login. Use that only if you want the login-gated version; the
> Streamlit Cloud deployment is the no-account one for bidders.

## Scale-to-zero handling
`auction-lakebase` pauses when idle. The connection layer (`db.py`) retries with
backoff for up to 90s, mints fresh tokens, and nudges the instance awake, so the
first bidder after an idle period sees a brief "waking up" message instead of an
error.

## Local run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # fill in secrets
streamlit run app.py
```

## Files
- `app.py` — Streamlit UI
- `db.py` — Lakebase connection + data access (retry / cold-start aware)
- `make_sample_excel.py` / `items.xlsx` — sample item sheet
- `.streamlit/` — config + secrets template
