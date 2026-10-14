# Lessons, objections and workarounds

Everything here happened on real runs against a live Microsoft Ads account. Each entry
says what went wrong, what the user said if anything, and what the skill now does.

## 1. "MS Ads doesn't support bulk upload" (user objection)
The original handoff assumed the user would import the bulk CSV by hand in the web UI.
The user pushed back that their aim was to upload companies at scale directly.
**Resolution:** the bulk CSV is a Bing Ads API bulk file. `BulkServiceManager.upload_file`
pushes it straight into the account. The API push *is* the bulk path. Never hand the
user a CSV to import.

## 2. Zero matches on 10 well-known banks: parser bug, not Microsoft
The first run returned "no match" for every company, including China Construction
Bank. The script read `company.Id`, but the field is `company.ProfileId`. `Id` is always
None, so every company was skipped. The response shape is a suds `ArrayOfCompany` with
`.Company` (list), and each item has `.Name`, `.ProfileId`, `.Status`, `.LogoUrl`.
**Rule:** if a run comes back almost all "no match" on famous names, suspect the parser
and inspect one raw response before reporting anything.

## 3. Upload "completed" but every row failed: error 1607
Rows of type `Campaign Company Name Criterion` (Parent Id = campaign) were rejected with
1607 `CampaignServiceEntityNotAllowedForAudienceCampaign`. Audience campaigns only
accept company targeting at the ad group level.
**Rule:** use `Ad Group Company Name Criterion` with Parent Id = ad group ID. Get the ad
group with `GetAdGroupsByCampaignId`. Always parse the result file for `... Error` rows.
"Upload complete" means nothing on its own.

## 4. The run hung for 7+ minutes: no socket timeout
On a 988-company run one `SearchCompanies` call sat on a silent ESTABLISHED TCP
connection forever, because the suds client has no default timeout.
**Fix:** `socket.setdefaulttimeout(20)`, 2 retries per company, and a checkpoint row
written and fsynced after every company, so a stall or crash costs one lookup and a
restart skips finished work.

## 5. "Stalled" that wasn't: buffered stdout
A watchdog watching the log file reported a stall. The log was just block-buffered,
because Python buffers stdout when it's redirected to a file.
**Rule:** measure progress by the checkpoint CSV's line count, which is fsynced, not the
log.

## 6. Fuzzy recovery put 16 wrong companies live (the big one)
A second pass accepted any candidate at or above 0.85 raw similarity, on pass-1 fuzzy
candidates and on suffix-variant searches. 16 of 131 "recoveries" were unrelated
companies:

| Listed | Wrongly targeted |
|---|---|
| Acme | Acmers |
| Bolt | BoltUp |
| Cora | Kcora |
| Delta Bank | NDelta Bank |
| Eastern Network Holdings Plc | Smile Eastern Network |
| Fenix | Afenix |
| Gamma | gammas |
| Helio | Heliox |
| Iris | Irisa |
| JKL | ajkl |
| Kestrel Group | PIKestrel Group |
| Lumen | Slumen |
| Mira | Mirana |
| Nova | NovaCorps |
| OPQ | MOPQ |
| Pioneer | The Pioneer |

They produced **no errors**, because a wrong match that doesn't collide with an existing
criterion is silent. Only a manual read of the full recovered list caught them. They
were deleted with a bulk file setting Status=Deleted, Id=criterion ID, Parent
Id=ad group.

Two more, BRD->EBRD and Santander Bank->Santander UK, were wrong too. They only failed
because the target profile was already live under its own correct name, so the
"already exists" error happened to be protective. BRD and Santander Bank (US) themselves
were never targeted.

**After this the user set the rule: take consent before deleting or editing anything;
only pushing new companies is pre-approved.**

**Fix: strict suffix-normalized recovery.** Strip corporate suffixes from both names and
require >= 0.97. All the true suffix variants scored 1.000:

| Pair | Score |
|---|---|
| Alder vs Alder Bank | 1.000 |
| Birch vs Birch Bank | 1.000 |
| Oak Bank vs O.A.K. Bank | 1.000 |
| Elm vs Elm Bank | 1.000 |

All the wrong matches above fall well short, for example:

| Pair | Score |
|---|---|
| Acme vs Acmers | 0.875 |
| Cora vs Kcora | 0.889 |
| Delta Bank vs NDelta Bank | 0.857 |
| Iris vs Miris | 0.909 |
| Vega vs Altavega Health | 0.545 |

On the second list it recovered 35 of 457 with 0 errors and 0 wrong matches. Never
blindly trust a stored fuzzy candidate.

## 7. Generic one-word names: the residual risk
Strict matching can't tell whether "Maple" is Maple Bank. Suffix stripping makes
the score 1.0 by construction. The same applied to Cedar Bank->Cedar and Harbor Bank
N.A.->Harbor Bank.
**Rule:** the script holds these in `flagged_<tag>.csv`. List them for the user and push
only on a yes.

## 7b. Strict matching still let wrong companies through (found 2026-10-01)
Building this skill meant re-scanning every live recovery. Some 1.0 suffix matches look
wrong when you set them against the list's own website column:

| Listed | Matched | Why it's suspect |
|---|---|---|
| Orchard Bank (orchardbank.example) | "Orchard" | Probably a larger, unrelated firm |
| Lake Bank (lake.example) | "Lake" | Generic word |
| QRS (qrs.example, a regional insurer) | "QRS Group" | An unrelated firm abroad |
| TUV (tuvadvisory.example) | "TUV plc" | A different industry |
| Summit Group (Summit Insurance) | "Summit Company" | Different suffixes |
| Maple Bank | "Maple" | Generic word |
| Cedar Bank | "Cedar" | Generic word |
| WXY BANK | "WXY" | Generic word |

They went live on both ad groups because the earlier generic-name check was only a
note in the report, not a gate.

**Rule now in the script:** `risky_match()` holds a match out of the bulk file in three
cases:
- the input had "Bank" and the candidate is the bare word
- a short (4 characters or fewer) acronym picked up a suffix the input didn't have
- both sides carry different, non-overlapping suffixes

Expect some false alarms (MSCI -> MSCI Inc., DZ Bank -> DZ BANK AG, Wheels Inc. ->
Wheels, LLC). That is the cheap side of the trade-off, because a human glance costs
seconds and a wrong company spends real ad budget. When reviewing, compare against the
source list's Website/LinkedIn columns, which are the only ground truth available.
SearchCompanies returns just a name and logo.

## 8. "Verify in the UI" and the empty Name column
The user asked to see the removals confirmed in the UI. The UI's company filter box
didn't filter (typing plus Enter did nothing). Its list is virtualized, so scraping
page text only sees about 20 rows. The UI header "Overall Total - N targets" is
reliable, though, and it matched the server count.
**Rule:** verify with a server download (`DownloadCampaignsByCampaignIds` with
`AdGroupTargetCriterions`). In that download, the company name is in **`Profile`** and
**`Name` is empty**. A name check against `Name` once "passed" vacuously. It was
re-verified by Profile ID on 2026-10-01. Match on Profile ID.

## 9. SDK quirks that cost time
- `DownloadParameters(campaign_ids=[123456789])` takes a plain list of ints. A
  `{'long': [...]}` dict throws a deserialization fault.
- `BulkServiceManager.upload_file` **moves** the upload file to `upload_<guid>.csv`. A
  later step then failed with FileNotFoundError looking for the original. The script
  uploads a temp copy.
- A shared `push_via_api()` once hard-coded the pass-1 filename, so pass 2 crashed
  before uploading. Nothing was pushed. Pass file paths explicitly.
- `GetAdGroupCriterionsByIds` rejects an `AdGroupCriterionType` keyword. Use the bulk
  download instead for listing criteria.
- A duplicate add returns an `Error` row (1043 or 1707) **and** an echo row of the
  existing criterion. Count genuinely new adds as success rows without a matching error
  row, then confirm with the live count.
- `bingads` 13.0.29 with `suds-community` 1.2.0 works on the macOS system Python 3.9.
  The urllib3 LibreSSL warning is harmless.

## 10. OAuth details
- Public desktop client: `OAuthDesktopMobileAuthCodeGrant(client_id, env='production')`,
  with no secret.
- After consent the browser lands on a blank `nativeclient?code=...` page and may bounce
  to `/common/wrongplace`. The first URL, with the code, is what's needed.
- Write the pasted URL to a temp file instead of passing it on the command line,
  because `!` and `*` in the code break shell quoting. Delete the file afterwards.
- A universal developer token issued under another user (a colleague) works for any
  signed-in user with access.
- Refresh tokens can rotate. The script re-saves the token after every refresh.

## 11. Reuse and re-checking
- The checkpoint is account-wide. Resolving a second list for another ad group reused
  everything already known with no API calls.
- To copy companies to a second ad group, merge the resolved files and dedupe against
  that ad group's live download. In practice, 155 of 394 were already there and 239 were
  pushed with 0 errors.
- "Has everything been tried?" is answered by `recheck`. Name lists often contain
  duplicate rows (Columbia Bank, Atlantic Union Bank), so report unique names, which is
  why counts can differ by one or two from an earlier summary.
- "No match" mostly means Microsoft's index doesn't carry that name string, which the
  user also observed. Even after suffix variants, about half of a BFSI list can stay
  unresolved. That is a platform limit, not a bug to fix.
