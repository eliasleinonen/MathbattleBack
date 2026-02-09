# 🚀 Render Deployment Guide (Simplified Demo Mode)

## Quick Setup - No OAuth Required! ✅

Since OAuth is disabled, you only need **2 environment variables** for Render:

### Required Environment Variables

```bash
DATABASE_URL=your-mongodb-connection-string
SECRET_KEY=your-generated-secret-key
```

That's it! No Google OAuth setup needed.

---

## Step-by-Step Render Deployment

### 1. Generate SECRET_KEY

Run this command locally:
```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Copy the output - you'll need it for Render.

### 2. Get MongoDB Connection String

**Option A: MongoDB Atlas (Recommended for Free Tier)**
1. Go to [mongodb.com/cloud/atlas](https://www.mongodb.com/cloud/atlas)
2. Create free account + free cluster
3. Click "Connect" → "Connect your application"
4. Copy the connection string: `mongodb+srv://username:password@cluster.mongodb.net/`
5. Replace `<password>` with your actual password

**Option B: Use Existing MongoDB**
- If you already have MongoDB, just use that connection string

### 3. Configure Render

Go to your Render dashboard and fill in:

**Basic Settings:**
- Name: `mathbattle-backend` (or any name you like)
- Branch: `main`
- Region: Frankfurt (or closest to you)
- Language: Docker (auto-detected)
- Root Directory: (leave empty)

**Instance Type:**
- **For Portfolio/Demo:** Starter ($7/month) - No cold starts, always responsive
- **For Budget:** Free - But spins down after inactivity (slow first load)

**Environment Variables:**

Click "Add Environment Variable" and add these **2** variables:

| Key | Value |
|-----|-------|
| `DATABASE_URL` | Your MongoDB connection string |
| `SECRET_KEY` | The secret key you generated |

**Optional variables** (only if you want to customize):

| Key | Value | Default |
|-----|-------|---------|
| `DATABASE_NAME` | `derivative_duel` | derivative_duel |
| `CORS_ORIGINS` | `https://yourdomain.com` | * (allows all) |

### 4. Deploy

Click **"Create Web Service"** or **"Deploy Web Service"**

Render will:
1. Clone your GitHub repo
2. Build with Docker
3. Install dependencies
4. Start the server
5. Give you a URL like `mathbattle-backend.onrender.com`

**Build time:** ~2-3 minutes

### 5. Test Your Deployment

Once deployed, visit:

**API Docs:**
```
https://your-service-name.onrender.com/docs
```

**Health Check:**
```
https://your-service-name.onrender.com/
```

Should return: `{"message": "Derivative Duel API"}`

**Server Time:**
```
https://your-service-name.onrender.com/api/server-time
```

---

## Troubleshooting

### Build Fails

**Error:** "Could not find requirements.txt"
- **Fix:** Make sure you pushed your latest code to GitHub

**Error:** "Failed to install dependencies"
- **Fix:** Check that `requirements.txt` is correct (we removed google-auth)

### App Crashes on Startup

**Error:** "Connection refused" or "MongoDB error"
- **Fix:** Check that `DATABASE_URL` is correct
- **Fix:** Whitelist Render's IP in MongoDB Atlas (or allow from anywhere: 0.0.0.0/0)

**Error:** "SECRET_KEY not found"
- **Fix:** Make sure you added the `SECRET_KEY` environment variable

### Free Tier Sleeping

**Issue:** App is slow to respond after inactivity
- **Cause:** Free tier spins down after 15 min of no traffic
- **Fix:** Upgrade to Starter plan ($7/month) for always-on service

---

## ⚠️ Critical Frontend Update Required

To use the new **Demo Mode** successfully, your frontend **must** verify unique guest sessions.

1. **Generate a unique Guest ID** (e.g., `guest-12345`) on app start.
2. **Send it in the header** for ALL requests:
   ```javascript
   Authorization: Bearer guest-12345
   ```

**Why?**
The backend uses this ID to identify players. If you don't send a unique ID, every visitor will share the same `guest-user-id`, causing matchmaking to fail (users matching with themselves).

👉 **See `FRONTEND_UPDATE.md` for full implementation details.**

## Post-Deployment

### Update Frontend (if applicable)

If you have a frontend, update the API URL to point to:
```
https://your-service-name.onrender.com
```

### Monitor Logs

In Render dashboard:
1. Click your service
2. Go to "Logs" tab
3. Watch for startup message: `[DEMO MODE] Authentication disabled - all players are guests`

### Custom Domain (Optional)

To use `api.mathbattle.xyz`:

1. Go to Render → Your Service → Settings → Custom Domain
2. Add `api.mathbattle.xyz`
3. Render gives you a CNAME value
4. Add CNAME record in your DNS:
   ```
   api    CNAME    your-service.onrender.com
   ```
5. Wait for DNS propagation (~5-30 min)

---

## Environment Variable Reference

### Minimal Setup (Demo Mode)
```bash
DATABASE_URL=mongodb+srv://user:pass@cluster.mongodb.net/
SECRET_KEY=your-generated-secret-64-char-string
```

### Full Setup (All Options)
```bash
# Required
DATABASE_URL=mongodb+srv://user:pass@cluster.mongodb.net/?retryWrites=true&w=majority
SECRET_KEY=your-generated-secret-key

# Optional
DATABASE_NAME=derivative_duel
ACCESS_TOKEN_EXPIRE_MINUTES=10080
CORS_ORIGINS=https://mathbattle.xyz,https://www.mathbattle.xyz
```

---

## Cost Breakdown

**Free Tier:**
- ✅ $0/month
- ❌ Sleeps after 15 min inactivity
- ❌ 750 hours/month limit
- ⚠️ Slow for portfolio demos

**Starter Tier (Recommended for Portfolio):**
- 💰 $7/month
- ✅ Always on (no sleeping)
- ✅ 512 MB RAM (sufficient)
- ✅ Fast response for visitors
- ✅ Professional appearance

---

## Quick Deploy Checklist

- [ ] Generated `SECRET_KEY` with Python command
- [ ] MongoDB Atlas cluster created (or have connection string)
- [ ] Pushed latest code to GitHub (main branch)
- [ ] Added `DATABASE_URL` to Render environment variables
- [ ] Added `SECRET_KEY` to Render environment variables
- [ ] Clicked "Deploy Web Service"
- [ ] Waited for build to complete
- [ ] Tested `/docs` endpoint
- [ ] (Optional) Set up custom domain

---

## What You DON'T Need

Since OAuth is disabled, you don't need:
- ❌ Google Cloud Console account
- ❌ Google OAuth Client ID
- ❌ Google OAuth Client Secret  
- ❌ OAuth consent screen setup
- ❌ Authorized redirect URIs

This makes deployment **much simpler** for demo purposes!

---

## Next Steps After Deployment

1. **Update GitHub README** with live API link
2. **Test all endpoints** at `/docs`
3. **Share the link** in your portfolio
4. **Monitor logs** for any errors
5. **Consider custom domain** for professionalism

---

**Need Help?**
- Render Docs: https://render.com/docs
- MongoDB Atlas: https://docs.atlas.mongodb.com/
- FastAPI Deployment: https://fastapi.tiangolo.com/deployment/

**Ready to deploy!** You now have everything you need. 🚀
