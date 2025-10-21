#!/bin/bash

# Quick deployment script for Fry Networks External API
# Usage: curl -sSL https://raw.githubusercontent.com/Fry-Foundation/ExternalAPI/main/quick-deploy.sh | bash

set -e

echo "🚀 Fry Networks External API - Quick Deploy"
echo "==========================================="

# Check if running as root
if [[ $EUID -ne 0 ]]; then
   echo "❌ This script must be run as root (use sudo)"
   exit 1
fi

# Get the repository
echo "📥 Downloading application..."
cd /tmp
rm -rf hardware_exe_api
rm -rf hardware_exe_api
git clone https://github.com/Fry-Foundation/ExternalAPI.git hardware_exe_api
cd hardware_exe_api

# Make deploy script executable and run it
chmod +x deployment/deploy.sh
./deployment/deploy.sh

echo ""
echo "🎉 Quick deployment completed!"
echo ""
echo "🔧 Don't forget to:"
echo "1. Configure your domain in /etc/nginx/sites-available/hardware_exe_api"
echo "2. Edit /opt/hardware_exe_api/.env with your settings"
echo "3. Set up SSL: sudo certbot --nginx -d your-domain.com"
echo "4. Restart services: sudo systemctl restart hardware_exe_api nginx"