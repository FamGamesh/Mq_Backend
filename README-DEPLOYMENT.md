# MCQ Scraper Backend - Production Deployment Guide

## 🚀 RENDER DEPLOYMENT INSTRUCTIONS

### **Prerequisites**
- Render account (free tier works)
- GitHub repository with your backend code

### **Step 1: Repository Setup**
1. Push your backend code to GitHub
2. Make sure these files are in your repository root:
   - `server.py` (main FastAPI app)
   - `requirements-production.txt` (Python dependencies)
   - `Procfile` (Render process configuration)
   - `runtime.txt` (Python version specification)
   - `.env.production` (environment variables template)

### **Step 2: Create Render Web Service**
1. Go to [Render Dashboard](https://dashboard.render.com)
2. Click "New +" → "Web Service"
3. Connect your GitHub repository
4. Configure the service:
   - **Name**: `mcq-scraper-backend`
   - **Region**: Choose closest to your users
   - **Branch**: `main` (or your default branch)
   - **Runtime**: `Python 3`
   - **Build Command**: `pip install -r requirements-production.txt`
   - **Start Command**: `python -m uvicorn server:app --host 0.0.0.0 --port $PORT`

### **Step 3: Environment Variables**
In Render dashboard, go to Environment section and add these variables:

```
MONGO_URL=mongodb+srv://futexi:2Lzmponw0QiMCYw2@cluster0.tb6golz.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0
DB_NAME=mcq_scraper_production
GOOGLE_API_KEY=AIzaSyAsoKcq2DMgtRw-L_3inX9Cq-V6-YNOAVg
SEARCH_ENGINE_ID=2701a7d64a00d47fd
API_KEY_POOL=AIzaSyAsoKcq2DMgtRw-L_3inX9Cq-V6-YNOAVg,AIzaSyCb8zhG3NKzsRvpSb7FwgleNMSSLiQyYpY,AIzaSyAS5G7JOwXCC8BSJwr6aTRpEKpYl8L877k,AIzaSyCONl4UcQq6cMTi3wwQQruhu9WoAJa2gX8,AIzaSyB-m_rEg8A9v8TeKJmMJ3dtEm51O_BqjrU
PLAYWRIGHT_BROWSERS_PATH=/tmp/pw-browsers
ENVIRONMENT=production
```

### **Step 4: Deploy**
1. Click "Create Web Service"
2. Wait for deployment (5-10 minutes)
3. Your backend will be available at: `https://your-app-name.onrender.com`

### **Step 5: Test Deployment**
Test these endpoints:
- Health check: `GET https://your-app-name.onrender.com/api/health`
- Setup: `POST https://your-app-name.onrender.com/api/setup`

### **Important Notes**
- ✅ Stealth functionality removed for Python 3.13 compatibility
- ✅ All dependencies are Python 3.13 compatible
- ✅ MongoDB Atlas connection configured
- ✅ Production environment variables set
- ✅ Playwright browsers will be installed automatically

### **Troubleshooting**
- If deployment fails, check logs in Render dashboard
- Ensure all environment variables are set correctly
- Verify MongoDB connection string is valid
- First setup call might take 2-3 minutes to install Playwright browsers

## 📝 Files Modified for Production
- `server.py`: Removed playwright-stealth imports and usage
- `requirements-production.txt`: Python 3.13 compatible dependencies
- `Procfile`: Render process configuration
- `runtime.txt`: Python version specification
- `.env.production`: Production environment variables template