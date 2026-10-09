# Yahoo standings snapshot (local tool)

Moved from the README when the site became the league archive (2026-10-09). The archive does not use any of this. The GitHub Actions workflow no longer runs `fetch.py` or uses Yahoo secrets. To publish live Yahoo data again, the workflow and site would need to be extended.

`fetch.py` writes `site/data.json` (git-ignored). `--demo` writes fictional data and makes no network requests. `--live` reads the league from Yahoo. `auth_once.py` does the one-time local Yahoo authorization and saves credentials to `.secrets/yahoo.json` (owner-only, git-ignored). Python 3 standard library only.

```sh
python3 -m unittest discover -s tests -v   # tests
python3 fetch.py --demo                     # fictional data -> site/data.json
```

## Yahoo setup (one time)

Use the Yahoo app that was already approved; do not create a new one. You need its Client ID, Client Secret and the exact redirect URI registered on it (from the Yahoo developer app page).

1. Run `python3 auth_once.py` in your own terminal. It prompts for the Client ID and Secret without echoing them.
2. Approve access in the browser. Yahoo redirects to the registered URI, and the page may fail to load. Copy the full address and paste it at the hidden prompt.
3. Run a local live check. It writes to `site/data.json`, which is git-ignored, and publishes nothing:

   ```sh
   python3 fetch.py --live --local-secrets --league-id <LEAGUE_ID>
   ```

   Compare the result with the league on Yahoo. If Yahoo issues a new refresh token, `fetch.py` saves it to `.secrets/yahoo.json` before doing anything else.

The league ID is the number in the league URL (`basketball.fantasysports.yahoo.com/nba/<LEAGUE_ID>`). It changes each season.
