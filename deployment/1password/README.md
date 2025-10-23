Storing API tokens in 1Password for Hardware API

Goal
----
Keep `API_BEARER_TOKEN` and `API_BEARER_TOKEN_FLXTIME` out of repo files and store them securely in 1Password. The application already contains a small resolver that converts `op://Vault/Item/field` references into real secrets via the 1Password CLI `op read`.

Create the 1Password item
-------------------------
1. Create (or choose) a Vault named `Secrets`.
2. Create a new `Item` (type: "Secure Note" or "Password") with the title: `HardwareAPI`.
3. Add two custom fields (text):
   - Field name: `API_BEARER_TOKEN` -> value: <your general API token>
   - Field name: `API_BEARER_TOKEN_FLXTIME` -> value: <your FlxTime token>

Reference the secret in `.env`
-----------------------------
Use the `op://Vault/Item/field` format in `.env` (the app will resolve at startup):

API_BEARER_TOKEN=op://Secrets/HardwareAPI/API_BEARER_TOKEN
API_BEARER_TOKEN_FLXTIME=op://Secrets/HardwareAPI/API_BEARER_TOKEN_FLXTIME

Validating locally
------------------
On a machine with the `op` CLI signed in to your account, run the app startup script or simply `python -c "import os; print(os.getenv('API_BEARER_TOKEN'))"` after the app runs the resolver to confirm resolution.

Notes
-----
- The resolver in `app.py` uses `op read 'op://Vault/Item/field'` so ensure item and field names match exactly.
- If `op` is not available or the item is inaccessible, the app leaves the `op://` string in place and will likely fail to connect to services that need the secret. This is intentional to avoid silent failures.
