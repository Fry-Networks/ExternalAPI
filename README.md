# External API Service

Lightweight FastAPI service that provides the HTTP contract consumed by the miner binaries. It stores all state in memory (or on-disk snapshots if you extend `storage.py`).

## Quick start

```powershell
cd ExternalAPI
python -m venv .venv
# Windows PowerShell
. .\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
# Start the server (defaults to PORT=8080)
python -m uvicorn app:app --reload --host 127.0.0.1 --port 8080
```

Environment variables

Create a `.env` file in the `ExternalAPI` folder (optional) and load it into your shell before running. Example `.env`:

```
MONGODB_URI=mongodb://localhost:27017
MONGODB_DB=fry_external_api
PORT=8081
HOST=127.0.0.1
UVICORN_RELOAD=true
```

Load `.env` in PowerShell into the current session:

```powershell
Get-Content .env |
	Where-Object { $_ -and -not $_.TrimStart().StartsWith('#') } |
	ForEach-Object {
		$parts = $_ -split '=', 2
		if ($parts.Count -eq 2) {
			$name = $parts[0].Trim()
			$value = $parts[1].Trim().Trim("'`\"'")
			Set-Item -Path "Env:$name" -Value $value
		}
	}
```

Start the server (reads PORT/HOST env vars if set):

```powershell
python .\app.py
# or explicitly with uvicorn
python -m uvicorn app:app --reload --host $env:HOST --port $env:PORT
```

The miner should be configured with `api_base_url` pointing at `http://<host>:8080` (append the proper port if you change it).

## Endpoints

| Method | Path | Description |
| ------ | ---- | ----------- |
| GET | `/versions/{miner_code}` | Returns the latest required version for a miner family. |
| GET | `/miners/{miner_key}` | Retrieves registration metadata for a miner key (registered MAC/hex ID). |
| POST | `/miners/{miner_key}/installations/{install_id}` | Upserts per-installation heartbeat information. |
| POST | `/miners/{miner_key}/leases/{install_id}` | Attempts to acquire a global lease. |
| PATCH | `/miners/{miner_key}/leases/{install_id}` | Renews an existing lease. |
| GET | `/miners/{miner_key}/leases/current` | Returns the active lease (if any) including remaining TTL. |
| GET | `/miners/{miner_key}/leases/history` | Returns historical lease entries. |
| GET | `/miners/{miner_key}/hardware` | Returns the latest hardware aggregate document. |
| PUT | `/miners/{miner_key}/hardware` | Replaces the hardware aggregate document. |

1Password secrets in `.env`
--------------------------------
You can keep secrets (like `MONGODB_URI`) out of plain text by using the 1Password CLI reference format in your `.env`.
Supported forms:

```
MONGODB_URI=op/Vault/Item/field
# or
MONGODB_URI=op://Vault/Item/field
```

On startup the app will try to resolve any `op/...` or `op://...` values using the `op read` command and replace the environment variable with the secret value. Make sure the `op` CLI is installed and you're signed in (see 1Password docs). If `op` is not available or the read fails, the original env value is left unchanged.
All responses follow the schema documented in `models.py`. Extend the storage layer or swap it with a database-backed implementation as needed.
