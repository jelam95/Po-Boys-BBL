#!/usr/bin/env python3
"""One-time local Yahoo authorization.

Run in your own terminal (it prompts for hidden input):
    python3 auth_once.py

It asks for the Client ID, Client Secret and the exact redirect URI registered
on the Yahoo app, opens Yahoo's consent page, then asks you to paste the full
URL your browser was sent to. It checks the `state` value, exchanges the code,
and saves credentials to .secrets/yahoo.json (owner-only). Nothing secret is
printed.
"""
import argparse
import base64
import getpass
import json
import os
import secrets
import sys
import urllib.parse
import urllib.request
import webbrowser

import fetch

AUTH_URL = "https://api.login.yahoo.com/oauth2/request_auth"


class AuthError(RuntimeError):
    pass


def build_auth_url(client_id, redirect_uri, state):
    return AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "state": state,
    })


def extract_code(callback_url, expected_state, redirect_uri):
    """Validate the pasted callback URL and return the authorization code."""
    callback_url = callback_url.strip()
    parts = urllib.parse.urlsplit(callback_url)
    expected = urllib.parse.urlsplit(redirect_uri)
    if (parts.scheme, parts.netloc, parts.path.rstrip("/")) != \
            (expected.scheme, expected.netloc, expected.path.rstrip("/")):
        raise AuthError("That URL does not start with the registered redirect URI.")
    query = urllib.parse.parse_qs(parts.query)
    if "error" in query:
        raise AuthError("Yahoo returned an error: %s" % query.get("error_description", query["error"])[0])
    states = query.get("state", [])
    if len(states) != 1 or not secrets.compare_digest(states[0], expected_state):
        raise AuthError("The state value does not match. Start again; do not reuse an old URL.")
    codes = query.get("code", [])
    if len(codes) != 1 or not codes[0]:
        raise AuthError("No authorization code found in the URL.")
    return codes[0]


def exchange_code(client_id, client_secret, redirect_uri, code, opener=urllib.request.urlopen):
    basic = base64.b64encode(("%s:%s" % (client_id, client_secret)).encode()).decode()
    body = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    }).encode()
    token = fetch.request_json(fetch.TOKEN_URL, {
        "Authorization": "Basic " + basic,
        "Content-Type": "application/x-www-form-urlencoded",
    }, body, opener)
    if not isinstance(token, dict) or not token.get("refresh_token"):
        raise AuthError("Yahoo's token response did not include a refresh token.")
    return token["refresh_token"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--redirect-uri", default=os.environ.get("YAHOO_REDIRECT_URI"))
    parser.add_argument("--out", default=fetch.DEFAULT_SECRETS_FILE)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)

    try:
        client_id = getpass.getpass("Yahoo Client ID (hidden): ").strip()
        client_secret = getpass.getpass("Yahoo Client Secret (hidden): ").strip()
        redirect_uri = (args.redirect_uri or input("Registered redirect URI: ")).strip()
        if not (client_id and client_secret and redirect_uri):
            raise AuthError("Client ID, Client Secret and redirect URI are all required.")

        state = secrets.token_urlsafe(24)
        url = build_auth_url(client_id, redirect_uri, state)
        print("\nOpen this page, sign in to Yahoo and click Agree:\n\n  %s\n" % url)
        if not args.no_browser:
            webbrowser.open(url)
        print("Your browser will then go to the redirect URI. The page may fail to load;")
        print("that is expected. Copy the FULL address from the address bar.\n")
        callback = getpass.getpass("Paste the full redirect URL (hidden): ")
        code = extract_code(callback, state, redirect_uri)
        refresh = exchange_code(client_id, client_secret, redirect_uri, code)

        fetch.write_private(args.out, json.dumps({
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "refresh_token": refresh,
            "refreshed_at": fetch.utc_now(),
        }, indent=2) + "\n")
    except (AuthError, fetch.FetchError) as error:
        print("Authorization failed: %s" % error, file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.", file=sys.stderr)
        return 1
    print("Saved credentials to %s (owner read/write only)." % os.path.relpath(args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
