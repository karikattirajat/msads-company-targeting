---
name: msads-company-targeting
description: Append company (LinkedIn profile) targeting to Microsoft Advertising / Bing Ads ad groups in bulk via the Bing Ads API - resolve company names to LinkedIn Profile IDs, dedupe against what's already live, push, verify, and re-check a list later. Use this whenever the user hands over an account/company list (CSV or names) and wants it added, appended, uploaded or pushed to a Microsoft Ads / MS Ads / Bing ad group or campaign, mentions company targeting, LinkedIn profile targeting, audience campaign company lists, ad group or campaign IDs alongside a company list, or asks whether a list "has been tried/added" to an ad group, or to copy companies from one ad group to another. Also use it for removing wrong companies from an ad group.
---

# Microsoft Ads bulk company targeting

Microsoft Ads lets an ad group target people at specific companies (LinkedIn profile
targeting). The web UI only offers one-at-a-time typeahead, and the Mac Editor has no
company entity, so the real bulk path is the Bing Ads API: resolve each name to a
LinkedIn **Profile ID** with `SearchCompanies`, write a bulk CSV of
`Ad Group Company Name Criterion` rows, and upload it with `BulkServiceManager`. The
direct API push *is* the at-scale upload. There is nothing for the user to import by
hand.

Everything runs through one script:
`python3 ~/.claude/skills/msads-company-targeting/scripts/msads_targeting.py --workdir <folder> <subcommand> ...`

The work folder holds the config, the OAuth token and the checkpoints. Keep one work
folder per ad account and note it in `references/accounts.md` (copy it from
`accounts.example.md`). Never commit that folder. Read
`references/lessons.md` before doing anything unusual. It records every mistake and
objection from earlier runs and why each rule below exists.

## Ground rules (defaults; keep to them unless the user changes them)

- **Adding companies to the ad group the user named is pre-approved.** Pushing new
  company criteria needs no extra confirmation once the user has asked for it.
- **Deleting or editing anything needs explicit consent each time.** That covers
  removing a criterion, changing a bid adjustment, or touching ad group or campaign
  settings or status. Don't touch bids, ad group status or campaign settings at all
  unless asked. The `delete` subcommand is a dry run until `--confirm`, so show the
  user the list and get a yes first.
- **Ask before spending API quota on anything the user didn't ask for.** Exploratory
  re-runs and large extra lookups count. Lookups that are part of the requested job
  are fine.
- **Never fabricate a Profile ID or call a guess a match.** A company that doesn't
  resolve cleanly goes in the review file with its reason.
- **Report from the server, not the upload response.** "Upload complete" has meant
  "every row failed" before.
- Keep progress updates short.

## Workflow

### 0. Setup (once per work folder)
Check for `msads_config.json` and `.refresh_token` in the work folder. If either is
missing:
1. `init-config --developer-token ... --client-id ... --customer-id ... --account-id ...`
   The account ID is the `aid=` value in the ads.microsoft.com URL, not the 8-character
   account number shown in the UI. The customer ID is `cid=`.
2. `auth` prints a sign-in URL. The user signs in as the account with edit access on
   the ad account and approves the consent screen. The browser then shows a blank
   `.../oauth2/nativeclient?code=...` page. That is expected, and it may also bounce on
   to `/common/wrongplace`. Ask the user to paste the first URL (the one with `?code=`),
   write it to a file, then run `auth --redirect-url-file <file>`. That step deletes
   the file afterwards.
3. Run `pip`-installs as `python3 -m pip install bingads`, because bare `pip` is often
   missing on macOS. On Python 3.12+ also install `suds-community`.

### 1. Confirm the target
Run `adgroups --campaign-id <id>`. It confirms the ad group ID belongs to that
campaign and shows its status. Use the ad group ID, because Audience campaigns reject
campaign-level company criteria (error 1607). Tell the user if the ad group is Paused
(criteria still attach, nothing serves), but don't change it.

### 2. Resolve
`resolve --input <list.csv|.txt> --tag <label>`. It auto-detects a `Company` column
(pass `--column` otherwise) and dedupes names case-insensitively.
- Results go into the shared `checkpoint.csv`, saved row by row. A name-to-Profile-ID
  mapping is account-wide, so a company resolved for any earlier ad group is reused
  with no API call. Crashes and restarts lose nothing. Watch progress through that
  file, not the log (see lessons).
- Accepted: **exact** only (raw similarity >= 0.99). `fuzzy_held` (0.85-0.99), `weak`
  and `no_match` go to `review_<tag>.csv`.
- For a new account or a new script change, smoke-test with `--limit 10` first.
- Long lists take roughly 30-40 minutes for about 900 new names. Run in the
  background and report progress from the line count of `checkpoint.csv`.

### 3. Strict recovery (only when the user asks for more coverage)
`recover --input <same list> --tag <label>` retries held-back names using
**suffix-normalized** matching only. It strips Bank/Inc/Corp/LLC/Group/N.A./PLC and
similar from both sides, then requires >= 0.97. It does this on the earlier candidate
first, then on suffix variants of the name. This catches Alder vs Alder Bank
(1.0) and rejects Cora vs Kcora (0.89) and Delta Bank vs NDelta Bank (0.86).
- Suffix math can't prove identity, so some 1.0 scores are still wrong companies.
  These were live before anyone noticed:
  - Orchard Bank -> "Orchard" and Lake Bank -> "Lake": "Bank" dropped, leaving a common
    word.
  - QRS -> "QRS Group" and TUV -> "TUV plc": a short acronym shared by unrelated firms.
  - Summit Group -> "Summit Company": different suffixes on each side.

  The script sends these patterns to `flagged_<tag>.csv` and leaves them out of the
  bulk file. Show each one to the user next to the list's own website/LinkedIn column
  (the source CSV usually has them). Add a flagged match only on a yes.
- **Don't run a plain fuzzy pass (0.85 floor) and push it.** That once put 16 wrong
  companies live (Acme->Acmers, Nova->NovaCorps...). If the user insists on
  fuzzy, produce candidates for them to review by eye and push only what they approve.

### 4. Build (dedupe) and push
1. `build --campaign-id <c> --adgroup-id <a> --resolved resolved_<tag>.csv [recovered_<tag>.csv ...] --tag <label>`
   This downloads what's live on that ad group, drops Profile IDs that are already
   there or repeated in the batch, refuses to go past 1,000 companies per ad group,
   and writes `bulk_<adgroup>_<tag>.csv`.
2. `push --bulk bulk_<adgroup>_<tag>.csv`. This uploads a copy, because the SDK moves
   the original to `upload_<guid>.csv`. It reports rows applied and errors by code.
   1043 or 1707 means the company was already there or duplicated in the batch, which
   is harmless. Any other code means stop and investigate.
3. `live --campaign-id <c> --adgroup-id <a>` gives the server count. It should equal the
   previous live count plus the rows applied. Quote this number to the user.

To copy companies from ad group A to ad group B, point `build` at B with A's
`resolved_*`/`recovered_*` files. The dedupe against B's live list is the duplicate
check.

### 5. Re-check a list later
`recheck --input <list> --campaign-id <c> --adgroup-id <a>` answers "has everything in
this file been tried for this ad group?" It reports:
- duplicate rows in the file
- never-tried names
- exact and recovered counts
- resolved names that are missing from the live ad group
- unresolved names broken down by reason

If the file has new names, run steps 2-4 for just those. The checkpoint skips the rest.

### 5b. "Retain only this list" (trim an ad group)
1. Download live.
2. Map the user's list to Profile IDs. Use the checkpoint first, then a
   suffix-normalized name match against the live `Profile` column.
3. Scan the would-be removals for names that resemble list entries. This catches a
   kept company that lives under another name.
4. **Write the export before deleting:** `removed_<adgroup>_<date>.csv` in `build`
   format, with the criterion ID kept, and any `risky_match` row's Flag set to
   "DO NOT UPLOAD ...".
5. Run `delete --confirm`.
6. Verify the live count and that no removed ID is still there.
7. Also hand back the list companies that aren't on the ad group, split into
   addable and unresolved. Don't add them unless asked, because "retain" isn't "add".

### 6. Removing wrong companies (consent required)
`delete --campaign-id <c> --adgroup-id <a> --profile-ids id1,id2` dry-runs and shows
what it would remove. After the user says yes, rerun with `--confirm`, then verify
with `live`. Match on Profile ID. In the download, the company name is in the
`Profile` column and `Name` is empty.

## Reporting back

Keep it short:
- **Live total**, verified from the server.
- **What changed**: new adds, already present, harmless duplicates.
- **What's held back**, with counts by reason. Send the review/unresolved CSV with
  `SendUserFile`.
- **Flagged generic-name matches**, listed individually.

Explain that "no match" usually means Microsoft's company index doesn't carry that
name. It doesn't necessarily mean the company has no LinkedIn page.

## Files the script writes (in the work folder)
| File | Meaning |
|---|---|
| `checkpoint.csv` | every name ever tried and its outcome (shared across ad groups) |
| `checkpoint_strict_recovery.csv` | strict-recovery outcomes |
| `resolved_<tag>.csv`, `recovered_<tag>.csv` | ready to build from |
| `review_<tag>.csv`, `unresolved_<tag>.csv`, `flagged_<tag>.csv` | for the user |
| `bulk_<adgroup>_<tag>.csv`, `result_bulk_*.csv` | what was uploaded and Microsoft's per-row response |
| `live_<adgroup>.csv` | latest server download |
