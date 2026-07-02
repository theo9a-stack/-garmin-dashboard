# Garmin Recovery & Load Dashboard

Pulls your Garmin Connect data twice a day, recalculates recovery and
training load with its own formulas (not just Garmin's built-in scores),
and publishes a morning brief / evening brief to a small dashboard you can
bookmark.

**What it recalculates, and why:**
- **Recovery score (0–100)** — a composite of HRV, resting HR, sleep score,
  and body battery, each compared against *your own* rolling 30-day
  baseline (z-scores), not a generic population norm.
- **Training load (TRIMP) and ACWR** — a standard sports-science method
  (Banister TRIMP for daily load, acute:chronic workload ratio for
  injury-risk trend) computed from your actual heart-rate data, independent
  of whatever Garmin's own "training load" number says.

Runs for free on GitHub Actions + GitHub Pages. No server, no always-on
computer required.

---

## 1. One-time local setup

```bash
git clone <this repo>
cd garmin-dashboard
pip install -r requirements.txt
cp config.example.json config.json
```

Edit `config.json` with your resting HR fallback, estimated max HR, and sex
(used only for the TRIMP formula — see note below).

## 2. Generate a Garmin login token

Garmin login can require an MFA code, which only works interactively, so
you do this once on your own machine:

```bash
python3 scripts/generate_login_token.py
```

This logs in (prompting for your MFA code if needed) and writes
`garmin_tokens_b64.txt` — a base64 bundle of your session tokens. This
bundle contains a long-lived token Garmin uses to mint fresh short-lived
access tokens automatically on each run, so it should keep working for
months without you repeating this step.

## 3. Push this repo to GitHub

Create a **private** repo (it will hold your health data) and push:

```bash
git init
git add .
git commit -m "Initial commit"
git remote add origin <your-repo-url>
git push -u origin main
```

## 4. Add secrets

In your repo → **Settings → Secrets and variables → Actions**, add:

| Secret | Value |
|---|---|
| `GARMIN_TOKENS_B64` | entire contents of `garmin_tokens_b64.txt` |
| `GARMIN_EMAIL` | your Garmin email (fallback if the token ever expires) |
| `GARMIN_PASSWORD` | your Garmin password (same reason) |

Then delete the local token file: `rm garmin_tokens_b64.txt`

> The email/password fallback only gets used if the cached token is
> rejected. If Garmin demands MFA at that point, the fallback login will
> fail too — in that case just re-run step 2 and update the secret.

## 5. Enable GitHub Pages

Repo → **Settings → Pages** → Source: **Deploy from a branch** → Branch:
`main`, folder: `/docs`. Save. GitHub will give you a URL like
`https://<you>.github.io/<repo>/` — that's your dashboard.

## 6. Check the schedule matches your timezone

The two workflows in `.github/workflows/` are set for:

- **Morning brief**: 05:30 UTC (07:30 Europe/Berlin in summer/CEST)
- **Evening brief**: 19:00 UTC (21:00 Europe/Berlin in summer/CEST)

GitHub Actions cron doesn't follow daylight saving automatically, so shift
both by one hour when Germany moves to CET in late October, and back in
late March. Or just edit the `cron:` lines to whatever local times you
prefer — the offset from UTC is Europe/Berlin's only variable.

## 7. Test it manually

Repo → **Actions** → pick either workflow → **Run workflow** to trigger it
by hand instead of waiting for the schedule. Check the run logs, then
refresh your Pages URL.

---

## How it works

```
scripts/fetch_and_brief.py --mode morning|evening
  → logs into Garmin Connect using the cached token
  → pulls today's resting HR, HRV, sleep, body battery, activities
  → updates docs/data/history.json (one row per day)
  → recomputes recovery score + TRIMP/ACWR from that history
  → writes the brief text into docs/data/latest.json
  → the GitHub Action commits both files back to the repo
  → docs/index.html (GitHub Pages) reads those two JSON files and renders
```

## Customizing the brief text

The rule-based sentences live in `RECOVERY_TEXT` and `LOAD_TEXT` dicts, and
`build_morning_brief` / `build_evening_brief` in
`scripts/fetch_and_brief.py`. Easiest place to change tone or add more
detail.

If you'd rather have genuinely generative insight text instead of
templated sentences, you can swap that section for a call to the
Anthropic API — happy to wire that in if you want it.

## Notes and limitations

- TRIMP uses your `max_hr` from `config.json` as an estimate, not a lab
  test — treat the exact number as directional, not clinical.
- The recovery score needs about 2 weeks of history before the baseline
  z-scores are meaningful. Before that it leans on the ±1 mid-range.
- This is not a medical device and the recovery/load numbers are for
  training-planning purposes, not health diagnosis.
