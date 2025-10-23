#!/bin/bash

# Hardware EXE API startup script with secure secret management
set -e

cd /opt/hardware_exe_api

echo "🔐 Hardware EXE API - Starting with secure secrets"
echo ""

# Check if 1Password CLI is available
if ! command -v op &> /dev/null; then
    echo "❌ Error: 1Password CLI (op) is not installed"
    exit 1
fi

echo "Step 1: Checking 1Password authentication..."

# Check if already signed in
if op account list &> /dev/null; then
    echo "✅ Already signed in to 1Password"
else
    echo "🔑 Please sign in to 1Password:"
    echo "Run this command first: eval \$(op signin --account frynetworks)"
    echo "Then run this script again."
    exit 1
fi

echo ""
echo "Step 2: Resolving secrets from 1Password..."

# Get the MongoDB URI from 1Password with error handling
if MONGODB_URI=$(op item get "Dude350zTest-Vercel" --vault Mongo --fields uri --reveal 2>/dev/null); then
    echo "✅ MongoDB URI resolved successfully from 1Password"
else
    echo "❌ Failed to retrieve MongoDB URI from 1Password"
    echo "   Please ensure:"
    echo "   1. You're signed in: eval \$(op signin --account frynetworks)"
    echo "   2. The item 'Dude350zTest-Vercel' exists in the 'Mongo' vault"
    echo "   3. You have access to the vault"
    exit 1
fi

echo ""
echo "Step 3: Setting up environment..."

# Export environment variables
export PORT=8081
export HOST=127.0.0.1
export UVICORN_RELOAD=false
export MONGODB_URI="$MONGODB_URI"

echo "✅ Environment variables configured"
echo "   - Port: $PORT"
echo "   - Host: $HOST"
echo "   - MongoDB: Connected to 1Password secret"

echo ""
echo "Step 4: Starting Hardware EXE API..."
echo "🚀 API will be available at: http://127.0.0.1:8081"
echo "📝 Press Ctrl+C to stop"
echo ""

# Start the application
.venv/bin/python app.py
