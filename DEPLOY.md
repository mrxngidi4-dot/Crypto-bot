# 🚀 CryptoBot Backend — Deployment Guide

## Step 1 — Get a free GitHub account
Go to github.com and create a free account if you don't have one.

## Step 2 — Upload the backend files to GitHub
1. Go to github.com/new → create a new repository called "cryptobot-backend"
2. Set it to Private
3. Upload all files from the cryptobot-backend folder:
   - main.py
   - requirements.txt
   - Procfile
   - railway.toml
   - .gitignore
   (Do NOT upload .env.example with real keys)

## Step 3 — Deploy to Railway (free)
1. Go to railway.app → Sign up with your GitHub account
2. Click "New Project" → "Deploy from GitHub repo"
3. Select your cryptobot-backend repository
4. Railway auto-detects Python and deploys it

## Step 4 — Set Environment Variables on Railway
In Railway dashboard → your project → Variables, add:

  BINANCE_API_KEY      = your_actual_binance_api_key
  BINANCE_SECRET_KEY   = your_actual_binance_secret_key
  TESTNET              = true        ← keep true for testing
  BINANCE_BASE_URL     = https://testnet.binance.vision

## Step 5 — Get your backend URL
Railway gives you a URL like:
  https://cryptobot-production-xxxx.up.railway.app

Test it by opening: https://your-url.railway.app/status
You should see JSON with bot status.

## Step 6 — Connect the frontend
In trading-bot.jsx, find this line near the top:
  const BACKEND_URL = "";
Change it to:
  const BACKEND_URL = "https://your-url.railway.app";

## Step 7 — Test on Binance Testnet
1. Go to testnet.binance.vision
2. Create a testnet account (separate from real Binance)
3. Get testnet API keys
4. Add them as Railway environment variables
5. Start the bot — it will place real orders on testnet with fake money

## Step 8 — Go Live (when ready)
Change Railway environment variables:
  TESTNET          = false
  BINANCE_BASE_URL = https://api.binance.com
  BINANCE_API_KEY  = your_REAL_binance_api_key
  BINANCE_SECRET_KEY = your_REAL_binance_secret

⚠️  IMPORTANT BEFORE GOING LIVE:
- Only deposit what you can afford to lose
- Start with $100-200 to test real execution
- Monitor for the first 48 hours
- Make sure your Binance API key has "Enable Spot Trading" checked
- Do NOT enable withdrawals on your API key (security risk)

## API Endpoints (for reference)
GET  /status          — bot status, equity, win rate
GET  /signals         — latest signals for all 10 pairs
GET  /trades          — last 100 trades
POST /bot/start       — start the trading loop
POST /bot/stop        — stop the trading loop
PATCH /bot/config     — update risk settings
GET  /account         — Binance account balances
GET  /orders/open     — open orders on Binance
DELETE /orders/cancel-all — cancel all open orders
