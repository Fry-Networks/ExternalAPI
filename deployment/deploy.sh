#!/bin/bash

# Fry Networks External API VPS Deployment Script
# This script sets up the external API service on a fresh Ubuntu/Debian VPS

set -e

# Configuration
APP_NAME="fry-external-api"
APP_DIR="/opt/$APP_NAME"
SERVICE_USER="www-data"
LOG_DIR="/var/log/$APP_NAME"

echo "🚀 Starting deployment of $APP_NAME..."

# Update system packages
echo "📦 Updating system packages..."
sudo apt update && sudo apt upgrade -y

# Install required system packages
echo "🔧 Installing system dependencies..."
sudo apt install -y python3 python3-pip python3-venv nginx git curl

# Install MongoDB (if not already installed)
echo "🍃 Installing MongoDB..."
wget -qO - https://www.mongodb.org/static/pgp/server-7.0.asc | sudo apt-key add -
echo "deb [ arch=amd64,arm64 ] https://repo.mongodb.org/apt/ubuntu $(lsb_release -cs)/mongodb-org/7.0 multiverse" | sudo tee /etc/apt/sources.list.d/mongodb-org-7.0.list
sudo apt update
sudo apt install -y mongodb-org
sudo systemctl start mongod
sudo systemctl enable mongod

# Install PM2 globally (optional)
echo "📱 Installing PM2..."
curl -fsSL https://deb.nodesource.com/setup_lts.x | sudo -E bash -
sudo apt-get install -y nodejs
sudo npm install -g pm2

# Create application directory
echo "📁 Setting up application directory..."
sudo mkdir -p $APP_DIR
sudo mkdir -p $LOG_DIR
sudo chown -R $SERVICE_USER:$SERVICE_USER $APP_DIR
sudo chown -R $SERVICE_USER:$SERVICE_USER $LOG_DIR

# Copy application files (assumes you've uploaded them to /tmp/fry-external-api)
echo "📋 Copying application files..."
sudo cp -r /tmp/fry-external-api/* $APP_DIR/
sudo chown -R $SERVICE_USER:$SERVICE_USER $APP_DIR

# Set up Python virtual environment
echo "🐍 Setting up Python virtual environment..."
cd $APP_DIR
sudo -u $SERVICE_USER python3 -m venv .venv
sudo -u $SERVICE_USER .venv/bin/pip install --upgrade pip
sudo -u $SERVICE_USER .venv/bin/pip install -r requirements.txt

# Create .env file template
echo "⚙️ Creating environment configuration..."
sudo -u $SERVICE_USER tee $APP_DIR/.env > /dev/null <<EOF
# Production environment configuration
PORT=8080
HOST=0.0.0.0
UVICORN_RELOAD=false

# MongoDB configuration (REQUIRED - application will fail without these)
MONGODB_URI=mongodb://localhost:27017
MONGODB_DB=fry_external_api

# 1Password secrets (if using 1Password CLI)
# MONGODB_URI=op://vault/item/field
EOF

# Set up systemd service
echo "🔧 Setting up systemd service..."
sudo cp $APP_DIR/deployment/fry-external-api.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable $APP_NAME

# Set up nginx reverse proxy
echo "🌐 Configuring nginx reverse proxy..."
sudo tee /etc/nginx/sites-available/$APP_NAME > /dev/null <<EOF
server {
    listen 80;
    server_name your-domain.com;  # Change this to your actual domain

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_connect_timeout 60s;
        proxy_send_timeout 60s;
        proxy_read_timeout 60s;
    }

    # Health check endpoint
    location /health {
        access_log off;
        proxy_pass http://127.0.0.1:8080;
    }
}
EOF

sudo ln -sf /etc/nginx/sites-available/$APP_NAME /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx

# Start the service
echo "🚀 Starting the service..."
sudo systemctl start $APP_NAME

# Show status
echo "✅ Deployment complete!"
echo ""
echo "Service status:"
sudo systemctl status $APP_NAME --no-pager
echo ""
echo "📝 Next steps:"
echo "1. Edit $APP_DIR/.env with your production configuration"
echo "2. Update nginx server_name in /etc/nginx/sites-available/$APP_NAME"
echo "3. Set up SSL with certbot: sudo apt install certbot python3-certbot-nginx && sudo certbot --nginx"
echo "4. Restart services: sudo systemctl restart $APP_NAME nginx"
echo ""
echo "🔍 Useful commands:"
echo "  View logs: sudo journalctl -u $APP_NAME -f"
echo "  Restart service: sudo systemctl restart $APP_NAME"
echo "  Stop service: sudo systemctl stop $APP_NAME"
echo "  Check status: sudo systemctl status $APP_NAME"
echo ""
echo "📊 PM2 alternative commands:"
echo "  Start with PM2: cd $APP_DIR && pm2 start deployment/ecosystem.config.js"
echo "  View PM2 status: pm2 status"
echo "  View PM2 logs: pm2 logs $APP_NAME"