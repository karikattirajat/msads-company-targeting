# Known accounts (copy to accounts.md and fill in; accounts.md is git-ignored)

## <Account / client name>
- Work folder: `<path to folder>`. It holds `msads_config.json`, `.refresh_token`,
  `checkpoint.csv` and `checkpoint_strict_recovery.csv`. Never commit these.
- Customer ID `<cid from the ads.microsoft.com URL>`, Account ID `<aid from the URL>`.
- OAuth sign-in: `<user with edit access to the ad account>`.
- Azure app: `<app name>`, client ID `<your app registration's client ID>`, public client.

| Campaign ID | Ad group | Ad group ID | Status | Live companies | As of |
|---|---|---|---|---|---|
| | | | | | |

Each ad group is capped at 1,000 companies. Ad group names and status change outside the
skill's runs, so always run `adgroups` first and mention any change.
