# Property Alert Bot

Monitors Domain.com.au for new investment property listings matching yield criteria. Runs on GitHub Actions every 6 hours and sends email alerts.

## What it does

- Searches target suburbs on Domain API every 6 hours
- Filters: house, 3+ bed, $380k–$620k, 400m²+ land
- Calculates gross yield using suburb median rents
- Alerts only on listings ≥ 5.5% gross yield
- Never alerts on the same listing twice (SQLite deduplication)
- Sends a formatted HTML email with price, yield, break-even prices, and direct Domain link

## Setup

### 1. Domain API credentials
Register free at [developer.domain.com.au](https://developer.domain.com.au), create a project, add the **Agents & Listings** package, and generate a Client ID + Secret.

### 2. Gmail App Password
Go to [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords), generate an app password for "Mail". Copy the 16-character password.

### 3. GitHub Secrets
In your repo → Settings → Secrets and variables → Actions → New repository secret:

| Secret name | Value |
|-------------|-------|
| `DOMAIN_CLIENT_ID` | Your Domain API client ID |
| `DOMAIN_CLIENT_SECRET` | Your Domain API client secret |
| `GMAIL_ADDRESS` | Your Gmail address |
| `GMAIL_APP_PASSWORD` | The 16-char app password from step 2 |
| `ALERT_EMAIL` | Where to send alerts (can be same as Gmail) |

### 4. Enable Actions
Go to your repo → Actions tab → click **Enable Actions** if prompted.

### 5. Test it manually
Actions tab → **Property Alert Bot** → **Run workflow** → Run.
Check the logs to confirm it connected to Domain API and searched all suburbs.

## Updating search criteria

Edit `bot.py` — the `CONFIG` dict at the top:

```python
CONFIG = {
    "min_price": 380000,
    "max_price": 620000,
    "min_bedrooms": 3,
    "min_land_sqm": 400,
    "min_yield_pct": 5.5,
    ...
    "suburbs": {
        "Kirwan QLD 4817": 560,   # ← update median weekly rent quarterly
        ...
    }
}
```

Commit and push — the next scheduled run picks up the new config automatically.

## Schedule

Runs at 00:00, 06:00, 12:00, 18:00 UTC (10:00, 16:00, 22:00, 04:00 AEST).
To change frequency, edit `.github/workflows/run-bot.yml` → the `cron` line.

## Local testing

```bash
pip install -r requirements.txt

export DOMAIN_CLIENT_ID=your_id
export DOMAIN_CLIENT_SECRET=your_secret
export GMAIL_ADDRESS=you@gmail.com
export GMAIL_APP_PASSWORD=xxxx_xxxx_xxxx_xxxx
export ALERT_EMAIL=you@gmail.com

python bot.py
```
