# Sponsored job alerts

Every hour (6am to 10pm), this checks LinkedIn, Indeed and Glassdoor for new London jobs, keeps only the ones from **companies on the UK government's Skilled Worker sponsor register**, drops anything advertised below **£33,400**, and emails you what's left. It never emails the same job twice.

It runs for free on GitHub, so your laptop doesn't need to be on.

## What you'll get

An email like "3 new sponsored jobs: Bauer Media, King's College London, SS&C", with each job's title, company, salary, site and link. Some jobs carry a ⚠ note:

- **salary not listed**: check the advert pays at least £33,400
- **name match 9x%** or **possible sponsor**: the company name was close to, or shares a brand name with, a sponsor on the register. Check it's the same organisation
- **agency**: a recruiter posted it. The agency may sponsor, but the real employer might not

Being on the register means a company **can** sponsor, not that it will for this role. Always check the advert.

## Setup (about 15 minutes, no coding)

### 1. Make a Gmail app password
The script sends email from a Gmail account.

1. Turn on 2-Step Verification for that Google account, if it isn't already: https://myaccount.google.com/security
2. Go to https://myaccount.google.com/apppasswords
3. Create an app password called "job alerts". Copy the 16-character code.

### 2. Put the project on GitHub
1. Sign in at https://github.com and click **New repository**.
2. Name it `sponsor-job-alerts`. Choose **Public**: public repositories get unlimited free GitHub Actions minutes, and there's nothing personal in these files. (Private works too, but runs hourly would use up the 2,000 free minutes a month. If you go private, change the schedule to every 2 or 3 hours.)
3. Click **uploading an existing file**, drag in **everything from this folder, including the `.github` folder**, and click **Commit changes**.
   - Tip: if dragging skips the `.github` folder, create the file manually: **Add file → Create new file**, name it `.github/workflows/alerts.yml`, and paste in the contents.

### 3. Add your secrets
In the repository, go to **Settings → Secrets and variables → Actions → New repository secret** and add:

| Name | Value |
|---|---|
| `SMTP_USER` | the Gmail address that sends the alerts |
| `SMTP_PASSWORD` | the 16-character app password from step 1 |
| `ALERT_TO` | where to send alerts (can be the same address, or several separated by commas) |

Secrets are encrypted and never shown in the repository, even if it's public.

### 4. Turn it on and test it
1. Open the **Actions** tab. If asked, click **I understand my workflows, go ahead and enable them**.
2. Click **Sponsored job alerts → Run workflow** to run it now.
3. After a few minutes, click the run to see the log. It shows how many jobs it found and how many passed each filter. If any new sponsored jobs turned up, you'll get an email.

From then on it runs on its own every hour. GitHub sometimes starts scheduled runs 5 to 20 minutes late; that's normal.

## Change what it searches for

Edit `config.yaml` on GitHub (click the file, then the pencil icon):

- `search_terms`: job titles to look for
- `location` and `distance_miles`
- `min_salary`
- `exclude_title_words`: e.g. remove `senior` if you want senior roles too
- `exclude_companies`: companies to ignore, such as ones you've already applied to
- `keep_jobs_without_salary`: set to `false` for fewer, safer alerts

Turn on phone notifications for these emails (in Gmail, filter by subject "new sponsored job" and mark them important) so you hear about jobs straight away.

## Limits to know

- **It's as fresh as the job sites allow:** jobs usually show up within an hour or two of posting.
- **Job sites sometimes block automated searches.** If a run finds 0 jobs from one site, it carries on with the others and tries again next hour. LinkedIn is the most likely to limit it.
- **NHS Jobs, Trac and jobs.ac.uk aren't covered.** Keep their own email alerts on.
- **Company names don't always match the register exactly**, so a few real sponsors may be missed and a few look-alikes flagged. The ⚠ notes tell you when to double-check.

## Run it on your own computer (optional)

```bash
pip install -r requirements.txt
python jobalerts.py --dry-run     # prints matches, sends nothing
```

Built with [JobSpy](https://github.com/speedyapply/JobSpy) and the [GOV.UK register of licensed sponsors](https://www.gov.uk/government/publications/register-of-licensed-sponsors-workers).
