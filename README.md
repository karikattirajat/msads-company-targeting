# Microsoft Ads Company Targeting

A [Claude Code](https://claude.com/claude-code) **skill** that bulk-adds **company (LinkedIn
profile) targeting** to Microsoft Advertising (Bing Ads) ad groups. Microsoft's web UI only offers
one-at-a-time typeahead; this skill uses the Bing Ads API to do it for hundreds of companies at once.

> *"Add this account list to ad group 123 in campaign 456."*  ·  *"Has this list already been added?"*  ·
> *"Remove these wrong companies from the ad group."*

## How a run goes
| Step | What happens |
|---|---|
| **0. Setup** (once) | Store your developer token, client ID and account IDs in a local config; sign in once via OAuth. |
| **1. Confirm target** | Verify the ad group belongs to the campaign and show its status. Company criteria attach at ad-group level (Audience campaigns reject campaign-level ones). |
| **2. Resolve** | Look each company name up with `SearchCompanies` to get its LinkedIn **Profile ID**. Only exact matches are accepted; near-matches go to a review file. Results are checkpointed, so crashes lose nothing and repeat names cost no API calls. |
| **3. Strict recovery** *(on request)* | Retry held-back names with suffix-normalized matching (Bank/Inc/Corp/LLC...). Risky matches (a common word left after dropping "Bank", short acronyms) are flagged for your review instead of pushed. |
| **4. Build + push** | Download what's live, drop duplicates, enforce the 1,000-company cap, upload the bulk file, and verify the count **from the server**, not the upload response. |
| **5. Re-check** | Report which names in a list were never tried, are missing from the ad group, or are unresolved, and why. |
| **6. Remove** | Dry-run first; deletes only with your explicit confirmation. |

## Safety rules built in
- Adding companies to the ad group you named is pre-approved; **deleting or editing anything needs
  explicit consent every time**. It never touches bids, ad group status or campaign settings.
- It never invents a Profile ID or calls a guess a match.
- It reports results from the server, because "upload complete" can mean every row failed.
- `references/lessons.md` records every pitfall found on real runs (SDK quirks, wrong-match patterns, error codes).

## Install
```bash
git clone https://github.com/karikattirajat/msads-company-targeting.git
mkdir -p ~/.claude/skills
cp -R msads-company-targeting/msads-company-targeting ~/.claude/skills/
python3 -m pip install bingads      # on Python 3.12+ also: suds-community
```
You need a Microsoft Advertising **developer token**, an **Azure app registration** (public client)
and a user with edit access to the ad account. Then:
```bash
python3 ~/.claude/skills/msads-company-targeting/scripts/msads_targeting.py --workdir <folder> init-config \
  --developer-token ... --client-id ... --customer-id ... --account-id ...
```

## Privacy
This repository contains **no credentials, tokens, account IDs or company lists**. Your config,
OAuth refresh token and checkpoints live in your own local work folder and are git-ignored.
