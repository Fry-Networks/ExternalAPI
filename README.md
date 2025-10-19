# External API Service

Lightweight FastAPI service that provides the HTTP contract consumed by the miner binaries. It stores all state in memory (or on-disk snapshots if you extend `storage.py`).

## Quick start (Development)

```powershell
cd ExternalAPI
python -m venv .venv
# Windows PowerShell
. .\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
# Start the development server
python -m uvicorn app:app --reload --host 127.0.0.1 --port 8080
```

## Production Deployment (VPS)

### Prerequisites
- Ubuntu/Debian VPS with root access
- Domain name pointing to your VPS IP (optional but recommended)

### Automated Deployment

1. **Upload files to your VPS:**
   ```bash
   # On your VPS, create temporary directory
   mkdir -p /tmp/fry-external-api
   
   # Upload all files (use scp, rsync, or git clone)
   scp -r ./* user@your-vps:/tmp/fry-external-api/
   # OR clone from git
   git clone https://github.com/Fry-Foundation/ExternalAPI.git /tmp/fry-external-api
   ```

2. **Run the deployment script:**
   ```bash
   cd /tmp/fry-external-api
   chmod +x deploy.sh
   sudo ./deploy.sh
   ```

3. **Configure your environment:**
   ```bash
   # Edit production configuration
   sudo nano /opt/fry-external-api/.env
   
   # Update nginx server name
   sudo nano /etc/nginx/sites-available/fry-external-api
   
   # Restart services
   sudo systemctl restart fry-external-api nginx
   ```

### Manual Deployment Options

#### Option 1: Using systemd (Recommended)
```bash
# Start/stop/restart the service
sudo systemctl start fry-external-api
sudo systemctl stop fry-external-api
sudo systemctl restart fry-external-api

# View logs
sudo journalctl -u fry-external-api -f
```

#### Option 2: Using PM2
```bash
cd /opt/fry-external-api
pm2 start ecosystem.config.js
pm2 save
pm2 startup  # Follow instructions to auto-start on boot

# PM2 commands
pm2 status
pm2 logs fry-external-api
pm2 restart fry-external-api
```

### SSL Setup (Recommended)
```bash
# Install certbot
sudo apt install certbot python3-certbot-nginx

# Get SSL certificate (replace with your domain)
sudo certbot --nginx -d your-domain.com

# Auto-renewal is set up automatically
```

## Environment Configuration

Create a `.env` file in the application directory:

```env
# Production environment configuration
PORT=8080
HOST=0.0.0.0
UVICORN_RELOAD=false

# MongoDB configuration (if using MongoDB backend)
# MONGODB_URI=mongodb://localhost:27017
# MONGODB_DB=fry_external_api

# 1Password secrets (if using 1Password CLI)
# MONGODB_URI=op://vault/item/field
```

### Development Environment

For local development, create a `.env` file with development settings:

```env
PORT=8080
HOST=127.0.0.1
UVICORN_RELOAD=true
MONGODB_URI=mongodb://localhost:27017
MONGODB_DB=fry_external_api_dev
```

Start the development server:

```bash
python app.py
# or explicitly with uvicorn
python -m uvicorn app:app --reload --host 127.0.0.1 --port 8080
```

The miner should be configured with `api_base_url` pointing at your production URL (e.g., `https://your-domain.com` or `http://your-vps-ip:8080`).

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

## 1Password Secrets Integration

You can keep secrets (like `MONGODB_URI`) out of plain text by using the 1Password CLI reference format in your `.env`.

Supported formats:
```env
MONGODB_URI=op/Vault/Item/field
# or
MONGODB_URI=op://Vault/Item/field
```

On startup the app will try to resolve any `op/...` or `op://...` values using the `op read` command and replace the environment variable with the secret value. Make sure the `op` CLI is installed and you're signed in (see 1Password docs). If `op` is not available or the read fails, the original env value is left unchanged.

## Monitoring and Maintenance

### Log Files
- **systemd logs:** `sudo journalctl -u fry-external-api -f`
- **PM2 logs:** `pm2 logs fry-external-api`
- **Nginx logs:** `sudo tail -f /var/log/nginx/access.log /var/log/nginx/error.log`

### Health Checks
The API automatically exposes all endpoints for health monitoring. You can set up monitoring tools to check:
- `GET /miners/{test_key}` - Basic API functionality
- HTTP response times and status codes
- System resource usage

### Updates and Maintenance
```bash
# Update application code
cd /opt/fry-external-api
git pull origin main  # if using git
sudo systemctl restart fry-external-api

# Update system packages
sudo apt update && sudo apt upgrade -y
sudo reboot  # if kernel updates were installed
```

All responses follow the schema documented in `models.py`. Extend the storage layer or swap it with a database-backed implementation as needed.
