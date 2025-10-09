from yahoo_oauth import OAuth2

sc = OAuth2(
	consumer_key="dj0yJmk9b1BNT1k5MXc2UTFSJmQ9WVdrOVdXaFlaM1EyZDFNbWNHbzlNQT09JnM9Y29uc3VtZXJzZWNyZXQmc3Y9MCZ4PTk4",
	consumer_secret="82979abe670acaac5652555068be41bf4a2b8b09",
	callback_uri="https://localhost:8080/callback"
)

#First run to complete OAuth
if not sc.token_is_valid():
	if not sc.refresh_access_token():
		sc.generate_token()

print("OAuth set up. 'secrets.json' created or refreshed here.")

