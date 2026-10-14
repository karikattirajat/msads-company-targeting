#!/usr/bin/env python3
"""
Bulk LinkedIn company targeting for Microsoft Advertising ad groups.

One tool, several subcommands. State lives in a work folder (default: cwd):
  msads_config.json          account config (see `init-config`)
  .refresh_token             cached OAuth refresh token
  checkpoint.csv             name -> LinkedIn ProfileId resolutions (account-wide, reusable)
  checkpoint_strict_recovery.csv   strict suffix-normalized recoveries

Typical run:
  auth -> adgroups -> resolve -> recover -> live -> build -> push -> live/recheck

Nothing here deletes or edits existing criteria except `delete`, which refuses
to run without --confirm.
"""

from __future__ import annotations

import argparse
import csv
import difflib
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import time
import warnings

warnings.filterwarnings("ignore")

# The bingads/suds SOAP client has no timeout of its own; one silent call once
# hung a 988-company run for 7+ minutes. This bounds every socket.
socket.setdefaulttimeout(20)

API_SLEEP = 0.3
MAX_RETRIES = 2
MIN_QUERY_LEN = 3
MAX_COMPANIES_PER_ADGROUP = 1000
EXACT = 0.99
FUZZY_FLOOR = 0.85
STRICT = 0.97

CRITERION_TYPE = "Ad Group Company Name Criterion"
BULK_COLUMNS = [
    "Type", "Status", "Id", "Parent Id", "Sub Type", "Campaign", "Ad Group",
    "Client Id", "Modified Time", "Name", "Profile", "Profile Id", "Bid Adjustment",
]
CP_COLUMNS = ["Input Name", "Outcome", "Matched Name", "Profile Id", "Note"]

SUFFIXES = [
    "Bank", "Inc", "Incorporated", "Corp", "Corporation", "LLC", "L.L.C.",
    "Ltd", "Limited", "Company", "Co", "Group", "Holdings", "Holding",
    "N.A.", "NA", "PLC", "L.P.", "LP", "AG", "S.A.", "SA",
]
SUFFIX_SET = {s.rstrip(".").lower() for s in SUFFIXES}


# --------------------------------------------------------------------------
# Config / auth
# --------------------------------------------------------------------------
def workdir(args):
    return os.path.abspath(args.workdir)


def wpath(args, name):
    return os.path.join(workdir(args), name)


def load_config(args):
    path = wpath(args, "msads_config.json")
    if not os.path.exists(path):
        sys.exit("No msads_config.json in {}. Run `init-config` first.".format(workdir(args)))
    with open(path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    for key in ("developer_token", "client_id", "customer_id", "account_id"):
        if not cfg.get(key):
            sys.exit("msads_config.json is missing '{}'.".format(key))
    cfg.setdefault("environment", "production")
    return cfg


def make_oauth(cfg):
    from bingads.authorization import OAuthDesktopMobileAuthCodeGrant
    return OAuthDesktopMobileAuthCodeGrant(client_id=cfg["client_id"], env=cfg["environment"])


def save_refresh_token(args, oauth):
    token = oauth.oauth_tokens.refresh_token
    if token:
        with open(wpath(args, ".refresh_token"), "w", encoding="utf-8") as fh:
            fh.write(token)


def authorization(args, cfg):
    from bingads.authorization import AuthorizationData
    token_path = wpath(args, ".refresh_token")
    if not os.path.exists(token_path):
        sys.exit("No .refresh_token. Run `auth` to do the one-time browser sign-in.")
    oauth = make_oauth(cfg)
    try:
        oauth.request_oauth_tokens_by_refresh_token(open(token_path).read().strip())
    except Exception as exc:  # noqa: BLE001
        sys.exit("Token refresh failed ({}). Delete .refresh_token and rerun `auth`.".format(exc))
    save_refresh_token(args, oauth)
    return AuthorizationData(
        account_id=int(cfg["account_id"]),
        customer_id=int(cfg["customer_id"]),
        developer_token=cfg["developer_token"],
        authentication=oauth,
    )


def campaign_service(auth, cfg):
    from bingads.service_client import ServiceClient
    return ServiceClient(
        service="CampaignManagementService", version=13,
        authorization_data=auth, environment=cfg["environment"],
    )


def bulk_manager(auth, cfg):
    from bingads.v13.bulk import BulkServiceManager
    return BulkServiceManager(authorization_data=auth, environment=cfg["environment"])


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def read_names(path, column=None):
    """One name per line (.txt) or a CSV column (auto-detects 'Company' style headers)."""
    if not path.lower().endswith(".csv"):
        return [l.strip() for l in open(path, encoding="utf-8-sig") if l.strip()]
    rows = list(csv.reader(open(path, encoding="utf-8-sig")))
    header = [h.strip().lower() for h in rows[0]]
    if column:
        idx = header.index(column.strip().lower())
    else:
        candidates = ["company", "company name", "company_name", "account", "account name", "name"]
        idx = next((header.index(c) for c in candidates if c in header), None)
        if idx is None:
            sys.exit("Could not find a company column in {}. Pass --column.".format(header))
    return [r[idx].strip() for r in rows[1:] if len(r) > idx and r[idx].strip()]


def unique_preserve(names):
    seen, out = set(), []
    for n in names:
        if n.lower() not in seen:
            seen.add(n.lower())
            out.append(n)
    return out


def load_cp(path):
    data = {}
    if os.path.exists(path):
        for r in csv.reader(open(path, encoding="utf-8-sig", newline="")):
            if r and r[0] != "Input Name":
                name, outcome, matched, pid, note = (r + [""] * 5)[:5]
                data[name] = [outcome, matched, pid, note]
    return data


def append_cp(path, row):
    new = not os.path.exists(path)
    with open(path, "a", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(CP_COLUMNS)
        w.writerow(row)
        fh.flush()
        os.fsync(fh.fileno())  # stdout to a log is block-buffered; this file is the true progress


def raw_ratio(a, b):
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def normalize(name):
    tokens = re.sub(r"[.,]", "", name).split()
    while tokens and tokens[-1].lower() in SUFFIX_SET:
        tokens.pop()
    return " ".join(tokens).lower().strip()


def norm_ratio(a, b):
    return difflib.SequenceMatcher(None, normalize(a), normalize(b)).ratio()


def stripped_suffixes(name):
    tokens = re.sub(r"[.,]", "", name).split()
    out = []
    while tokens and tokens[-1].lower() in SUFFIX_SET:
        out.append(tokens.pop().lower())
    return out


def risky_match(name, matched):
    """True when suffix stripping can't vouch for identity. Seen on real runs:
    Orchard Bank -> 'Orchard', Lake Bank -> 'Lake', Maple Bank -> 'Maple'
    ('bank' dropped, leaving a common word); QRS -> 'QRS Group', TUV -> 'TUV plc'
    (short acronyms shared by unrelated firms); Summit Group -> 'Summit
    Company' (different suffixes on each side)."""
    core = normalize(name)
    if len(core.split()) != 1:
        return False
    a, b = set(stripped_suffixes(name)), set(stripped_suffixes(matched))
    if "bank" in a and "bank" not in b:
        return True  # 'X Bank' matched to bare 'X' (the reverse, Comerica -> Comerica Bank, is fine)
    if len(core) <= 4 and (b - a):
        return True  # short acronym picked up a suffix the input never had
    return bool(a) and bool(b) and not (a & b)  # e.g. Group vs Company


def variants(name):
    tokens = name.split()
    out = []
    reduced = tokens[:]
    for _ in range(2):
        if len(reduced) <= 1 or re.sub(r"[.,]$", "", reduced[-1]).lower() not in SUFFIX_SET:
            break
        reduced = reduced[:-1]
        out.append(" ".join(reduced))
    if tokens and re.sub(r"[.,]$", "", tokens[-1]).lower() not in SUFFIX_SET:
        out.append(name.strip() + " Bank")
    seen, final = set(), []
    for v in out:
        k = v.lower()
        if k != name.lower() and len(v) >= MIN_QUERY_LEN and k not in seen:
            seen.add(k)
            final.append(v)
    return final


def search(svc, name):
    """Return [(Name, ProfileId)]. The field is ProfileId, not Id (reading .Id
    silently returns None for every company and fakes a 0% match rate)."""
    last = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = svc.SearchCompanies(CompanyNameFilter=name, LanguageLocale="en")
            comps = getattr(resp, "Company", None) if resp is not None else None
            if comps is None:
                return []
            if not isinstance(comps, (list, tuple)):
                comps = [comps]
            return [(c.Name, c.ProfileId) for c in comps
                    if getattr(c, "Name", None) is not None and getattr(c, "ProfileId", None) is not None]
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(API_SLEEP)
    raise last


def write_csv(path, header, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def download_live(args, cfg, auth, campaign_id, adgroup_id, out_name):
    """Authoritative live criteria for one ad group, straight from the server.
    The company name is in the 'Profile' column; 'Name' is empty on download,
    so any name search must use Profile (or better, match on Profile Id)."""
    from bingads.v13.bulk import DownloadParameters
    params = DownloadParameters(
        campaign_ids=[int(campaign_id)],  # plain list[int]; a {'long': [...]} dict faults
        data_scope=["EntityData"],
        download_entities=["AdGroupTargetCriterions"],
        file_type="Csv",
        result_file_directory=workdir(args),
        result_file_name=out_name,
        overwrite_result_file=True,
    )
    bulk_manager(auth, cfg).download_file(params)
    rows = list(csv.reader(open(wpath(args, out_name), encoding="utf-8-sig")))
    h = rows[0]
    ti, pi, pai, ii = h.index("Type"), h.index("Profile Id"), h.index("Parent Id"), h.index("Id")
    ni = h.index("Profile") if "Profile" in h else None
    live = {}
    for r in rows[1:]:
        if r and r[ti] == CRITERION_TYPE and r[pai] == str(adgroup_id):
            live[r[pi]] = {"criterion_id": r[ii], "profile": r[ni] if ni is not None else ""}
    return live


# --------------------------------------------------------------------------
# Subcommands
# --------------------------------------------------------------------------
def cmd_init_config(args):
    path = wpath(args, "msads_config.json")
    if os.path.exists(path) and not args.force:
        sys.exit("{} already exists (use --force to overwrite).".format(path))
    cfg = {
        "developer_token": args.developer_token,
        "client_id": args.client_id,
        "customer_id": args.customer_id,
        "account_id": args.account_id,
        "environment": "production",
    }
    os.makedirs(workdir(args), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
    print("Wrote", path)


def cmd_auth(args):
    cfg = load_config(args)
    oauth = make_oauth(cfg)
    if not args.redirect_url_file:
        print("Open this URL, sign in with the account that has edit access, approve consent:")
        print(oauth.get_authorization_endpoint())
        print("The browser lands on a blank .../oauth2/nativeclient?code=... page (normal; it may")
        print("then bounce to /common/wrongplace - the code in the first URL is what matters).")
        print("Save that full URL to a file and rerun: auth --redirect-url-file <file>")
        return
    url = open(args.redirect_url_file).read().strip()
    oauth.request_oauth_tokens_by_response_uri(url)
    save_refresh_token(args, oauth)
    os.remove(args.redirect_url_file)  # one-time code; don't leave it lying around
    print("Refresh token saved to .refresh_token")


def cmd_adgroups(args):
    cfg = load_config(args)
    auth = authorization(args, cfg)
    resp = campaign_service(auth, cfg).GetAdGroupsByCampaignId(CampaignId=int(args.campaign_id))
    groups = getattr(resp, "AdGroup", None) or []
    if not groups:
        print("No ad groups found for campaign", args.campaign_id)
    for g in groups:
        print("ADGROUP {} | {} | {}".format(g.Id, g.Name, g.Status))


def cmd_resolve(args):
    cfg = load_config(args)
    names = unique_preserve(read_names(args.input, args.column))
    if args.limit is not None:  # --limit 0 means "no new lookups", not "no limit"
        names = names[: args.limit]
    cp_path = wpath(args, "checkpoint.csv")
    done = load_cp(cp_path)
    todo = [n for n in names if n not in done]
    print("{} unique names | {} already in checkpoint (no API call) | {} to query".format(
        len(names), len(names) - len(todo), len(todo)))
    if todo:
        svc = campaign_service(authorization(args, cfg), cfg)
        for i, name in enumerate(todo, 1):
            if len(name) < MIN_QUERY_LEN:
                row = [name, "skipped", "", "", "query under 3 chars"]
            else:
                try:
                    cands = search(svc, name)
                except Exception as exc:  # noqa: BLE001
                    cands, row = None, [name, "error", "", "", "API error: {}".format(exc)]
                if cands is not None:
                    if not cands:
                        row = [name, "no_match", "", "", "no LinkedIn company match returned"]
                    else:
                        m, pid, r = max(((m, p, raw_ratio(name, m)) for m, p in cands), key=lambda x: x[2])
                        if r >= EXACT:
                            row = [name, "exact", m, pid, ""]
                        elif r >= FUZZY_FLOOR:
                            row = [name, "fuzzy_held", m, pid, "fuzzy {:.2f}, held for review".format(r)]
                        else:
                            row = [name, "weak", m, pid, "weak {:.2f}, not included".format(r)]
                time.sleep(API_SLEEP)
            append_cp(cp_path, row)
            done[name] = row[1:]
            if i % 25 == 0:
                print("  progress {}/{}".format(i, len(todo)), flush=True)
    summarize(names, done, args)


def summarize(names, done, args):
    from collections import Counter
    c = Counter(done[n][0] for n in names if n in done)
    print("Outcomes:", dict(c))
    resolved = [[n, done[n][1], done[n][2], "exact", ""] for n in names if done.get(n, [""])[0] == "exact"]
    held = [[n, done[n][1], done[n][2], done[n][0] + ": " + done[n][3]] for n in names
            if done.get(n, [""])[0] != "exact"]
    tag = args.tag
    write_csv(wpath(args, "resolved_{}.csv".format(tag)),
              ["Input Name", "Matched Name", "Profile Id", "Method", "Flag"], resolved)
    write_csv(wpath(args, "review_{}.csv".format(tag)),
              ["Input Name", "Matched LinkedIn Name", "Profile Id", "Note"], held)
    print("Wrote resolved_{0}.csv ({1}) and review_{0}.csv ({2})".format(tag, len(resolved), len(held)))


def cmd_recover(args):
    """Strict suffix-normalized recovery. Never accepts on raw fuzzy score."""
    cfg = load_config(args)
    names = unique_preserve(read_names(args.input, args.column))
    main_cp = load_cp(wpath(args, "checkpoint.csv"))
    rec_path = wpath(args, "checkpoint_strict_recovery.csv")
    rec = load_cp(rec_path)
    held = [n for n in names if main_cp.get(n, [""])[0] in ("no_match", "weak", "fuzzy_held", "skipped", "error")]
    todo = [n for n in held if n not in rec]
    print("{} held back | {} already tried in strict recovery | {} to try".format(len(held), len(held) - len(todo), len(todo)))
    svc = campaign_service(authorization(args, cfg), cfg) if todo else None
    for i, name in enumerate(todo, 1):
        outcome, matched, pid, note = main_cp[name]
        row = None
        if matched and pid and norm_ratio(name, matched) >= STRICT:
            row = [name, "recovered_strict", matched, pid, "prior candidate, normalized {:.3f}".format(norm_ratio(name, matched))]
        else:
            for v in variants(name):
                try:
                    cands = search(svc, v)
                except Exception:  # noqa: BLE001
                    cands = []
                time.sleep(API_SLEEP)
                if not cands:
                    continue
                m, p, r = max(((m, p, norm_ratio(name, m)) for m, p in cands), key=lambda x: x[2])
                if r >= STRICT:
                    row = [name, "recovered_strict", m, p, "variant '{}', normalized {:.3f}".format(v, r)]
                    break
        if row is None:
            row = [name, "still_unresolved", "", "", "no suffix-normalized match >= {}".format(STRICT)]
        append_cp(rec_path, row)
        rec[name] = row[1:]
        if i % 25 == 0:
            print("  progress {}/{}".format(i, len(todo)), flush=True)

    recovered, flagged, unresolved = [], [], []
    for n in held:
        r = rec.get(n)
        if r and r[0] == "recovered_strict":
            risky = risky_match(n, r[1])
            (flagged if risky else recovered).append(
                [n, r[1], r[2], "strict", "verify identity: generic word or acronym" if risky else ""])
        else:
            unresolved.append([n, main_cp[n][1], main_cp[n][2], main_cp[n][0] + ": " + main_cp[n][3]])
    tag = args.tag
    write_csv(wpath(args, "recovered_{}.csv".format(tag)), ["Input Name", "Matched Name", "Profile Id", "Method", "Flag"], recovered)
    write_csv(wpath(args, "flagged_{}.csv".format(tag)), ["Input Name", "Matched Name", "Profile Id", "Method", "Flag"], flagged)
    write_csv(wpath(args, "unresolved_{}.csv".format(tag)), ["Input Name", "Matched LinkedIn Name", "Profile Id", "Note"], unresolved)
    print("Recovered {} | flagged generic {} (NOT included unless approved) | still unresolved {}".format(
        len(recovered), len(flagged), len(unresolved)))
    for f in flagged:
        print("  FLAG: {} -> {} ({})".format(f[0], f[1], f[2]))


def cmd_live(args):
    cfg = load_config(args)
    auth = authorization(args, cfg)
    live = download_live(args, cfg, auth, args.campaign_id, args.adgroup_id,
                         "live_{}.csv".format(args.adgroup_id))
    print("LIVE company criteria on ad group {}: {}".format(args.adgroup_id, len(live)))
    if args.find:
        terms = [t.strip().lower() for t in args.find.split(",")]
        for pid, info in live.items():
            if any(t in (info["profile"] or "").lower() for t in terms):
                print("  present:", info["profile"], pid)


def cmd_build(args):
    """Merge resolved files, dedupe by ProfileId, drop what's already live, cap-check."""
    cfg = load_config(args)
    auth = authorization(args, cfg)
    live = download_live(args, cfg, auth, args.campaign_id, args.adgroup_id,
                         "live_{}.csv".format(args.adgroup_id))
    batch, dup_in_batch = {}, 0
    for path in args.resolved:
        for r in list(csv.reader(open(path, encoding="utf-8-sig")))[1:]:
            if not r or not r[2]:
                continue
            if len(r) > 4 and r[4].strip().upper().startswith("DO NOT UPLOAD"):
                continue  # suspected wrong match carried in an export file
            if r[2] in batch:
                dup_in_batch += 1
                continue
            batch[r[2]] = (r[0], r[1])
    already = {p: v for p, v in batch.items() if p in live}
    new = {p: v for p, v in batch.items() if p not in live}
    print("Live now: {} | candidates: {} (+{} in-batch duplicates dropped) | already live: {} | new: {}".format(
        len(live), len(batch), dup_in_batch, len(already), len(new)))
    if len(live) + len(new) > MAX_COMPANIES_PER_ADGROUP:
        sys.exit("Would exceed {} company criteria on this ad group ({} + {}). Split across ad groups.".format(
            MAX_COMPANIES_PER_ADGROUP, len(live), len(new)))
    rows = [["Format Version", "", "", "", "", "", "", "", "", "6.0", "", "", ""]]
    for pid, (name, matched) in new.items():
        rows.append([CRITERION_TYPE, "Active", "", args.adgroup_id, "", "", "", name[:100], "", matched, "", pid, 0])
    out = wpath(args, "bulk_{}_{}.csv".format(args.adgroup_id, args.tag))
    write_csv(out, BULK_COLUMNS, rows)
    print("Wrote", out, "with", len(new), "rows. Review it, then `push`.")


def parse_result(path):
    rows = list(csv.reader(open(path, encoding="utf-8-sig")))
    h = rows[0]
    ti, ci, ei, eni = h.index("Type"), h.index("Client Id"), h.index("Error"), h.index("Error Number")
    ok, errs = [], []
    for r in rows[1:]:
        if not r or len(r) <= ei or "Company Name Criterion" not in r[ti]:
            continue
        (errs if (r[ti].endswith("Error") or r[ei]) else ok).append(r)
    err_names = {r[ci] for r in errs}
    # A duplicate add returns an Error row AND an echo row of the existing
    # criterion. Only echo rows without a matching error are genuine adds.
    genuinely_new = [r for r in ok if r[ci] not in err_names]
    return genuinely_new, errs, h


def cmd_push(args):
    cfg = load_config(args)
    auth = authorization(args, cfg)
    from bingads.v13.bulk import FileUploadParameters
    # The SDK renames (moves) the upload file to upload_<guid>.csv. Upload a copy
    # so the reviewed bulk file stays where it is.
    tmp = os.path.join(tempfile.mkdtemp(), os.path.basename(args.bulk))
    shutil.copy(args.bulk, tmp)
    result_name = "result_" + os.path.basename(args.bulk)
    params = FileUploadParameters(
        result_file_directory=workdir(args), result_file_name=result_name,
        overwrite_result_file=True, upload_file_path=tmp, response_mode="ErrorsAndResults",
    )
    print("Uploading", args.bulk)
    bulk_manager(auth, cfg).upload_file(params, progress=None)
    new, errs, h = parse_result(wpath(args, result_name))
    from collections import Counter
    codes = Counter("{} {}".format(r[h.index("Error Number")], r[h.index("Error")]) for r in errs)
    print("Rows applied (new adds or deletes): {} | error rows: {}".format(len(new), len(errs)))
    for code, n in codes.items():
        print("  {} x{}".format(code, n))
    if errs:
        print("  1043/1707 = already on the ad group / duplicate in batch (harmless).")
        print("  1607 = campaign-level criterion on an Audience campaign: use ad-group level.")
    print("Result file:", wpath(args, result_name), "- now run `live` to confirm the server count.")


def cmd_recheck(args):
    cfg = load_config(args)
    auth = authorization(args, cfg)
    raw = read_names(args.input, args.column)
    names = unique_preserve(raw)
    main_cp = load_cp(wpath(args, "checkpoint.csv"))
    rec = load_cp(wpath(args, "checkpoint_strict_recovery.csv"))
    live = download_live(args, cfg, auth, args.campaign_id, args.adgroup_id,
                         "live_{}.csv".format(args.adgroup_id))
    from collections import Counter
    never, not_live, status = [], [], Counter()
    for n in names:
        c = main_cp.get(n)
        if not c:
            never.append(n)
            continue
        if c[0] == "exact":
            status["exact"] += 1
            if c[2] not in live:
                not_live.append((n, c[2]))
        elif rec.get(n, [""])[0] == "recovered_strict":
            status["recovered_strict"] += 1
            if rec[n][2] not in live:
                not_live.append((n, rec[n][2]))
        else:
            status["unresolved: " + c[0]] += 1
            if n not in rec:
                status["  (not yet through strict recovery)"] += 1
    print("Rows in file: {} | unique names: {} (duplicates: {})".format(len(raw), len(names), len(raw) - len(names)))
    print("Live on ad group {}: {}".format(args.adgroup_id, len(live)))
    print("Never tried:", len(never))
    for n in never[:50]:
        print("   ", n)
    for k, v in status.items():
        print("{}: {}".format(k, v))
    print("Resolved but NOT live:", len(not_live))
    for n, p in not_live[:50]:
        print("   ", n, p)


def cmd_delete(args):
    """Remove specific criteria. Only with explicit user consent (--confirm)."""
    cfg = load_config(args)
    auth = authorization(args, cfg)
    live = download_live(args, cfg, auth, args.campaign_id, args.adgroup_id,
                         "live_{}.csv".format(args.adgroup_id))
    targets = [p.strip() for p in args.profile_ids.split(",") if p.strip()]
    rows = [["Format Version", "", "", "", "", "", "", "", "", "6.0", "", "", ""]]
    for pid in targets:
        info = live.get(pid)
        if not info:
            print("  not live, skipping:", pid)
            continue
        print("  will delete: {} ({}) criterion {}".format(info["profile"], pid, info["criterion_id"]))
        rows.append([CRITERION_TYPE, "Deleted", info["criterion_id"], args.adgroup_id,
                     "", "", "", pid, "", "", "", "", ""])
    if not args.confirm:
        print("Dry run. Get the user's explicit OK, then rerun with --confirm.")
        return
    out = wpath(args, "bulk_delete_{}.csv".format(args.adgroup_id))
    write_csv(out, BULK_COLUMNS, rows)
    args.bulk = out
    cmd_push(args)


# --------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--workdir", default=".", help="folder holding config, token and checkpoints")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init-config")
    s.add_argument("--developer-token", required=True)
    s.add_argument("--client-id", required=True)
    s.add_argument("--customer-id", required=True, type=int)
    s.add_argument("--account-id", required=True, type=int, help="the aid= URL value, not the 8-char account number")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_init_config)

    s = sub.add_parser("auth")
    s.add_argument("--redirect-url-file")
    s.set_defaults(fn=cmd_auth)

    s = sub.add_parser("adgroups")
    s.add_argument("--campaign-id", required=True)
    s.set_defaults(fn=cmd_adgroups)

    for name, fn in (("resolve", cmd_resolve), ("recover", cmd_recover)):
        s = sub.add_parser(name)
        s.add_argument("--input", required=True)
        s.add_argument("--column")
        s.add_argument("--tag", required=True, help="short label for output files, e.g. bfsi_oct")
        if name == "resolve":
            s.add_argument("--limit", type=int, help="only the first N names (smoke test)")
        s.set_defaults(fn=fn)

    s = sub.add_parser("live")
    s.add_argument("--campaign-id", required=True)
    s.add_argument("--adgroup-id", required=True)
    s.add_argument("--find", help="comma-separated names to look for among live criteria")
    s.set_defaults(fn=cmd_live)

    s = sub.add_parser("build")
    s.add_argument("--campaign-id", required=True)
    s.add_argument("--adgroup-id", required=True)
    s.add_argument("--resolved", required=True, nargs="+", help="resolved_*.csv / recovered_*.csv files")
    s.add_argument("--tag", required=True)
    s.set_defaults(fn=cmd_build)

    s = sub.add_parser("push")
    s.add_argument("--bulk", required=True)
    s.set_defaults(fn=cmd_push)

    s = sub.add_parser("recheck")
    s.add_argument("--input", required=True)
    s.add_argument("--column")
    s.add_argument("--campaign-id", required=True)
    s.add_argument("--adgroup-id", required=True)
    s.set_defaults(fn=cmd_recheck)

    s = sub.add_parser("delete")
    s.add_argument("--campaign-id", required=True)
    s.add_argument("--adgroup-id", required=True)
    s.add_argument("--profile-ids", required=True, help="comma-separated LinkedIn Profile Ids")
    s.add_argument("--confirm", action="store_true")
    s.set_defaults(fn=cmd_delete)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
