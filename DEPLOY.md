# Deploying CodeOracle

Two services, deployed separately.

| Part | Where | Why |
| --- | --- | --- |
| `frontend/` (React + Vite, static) | **Vercel** | Pure static build, SPA rewrites already configured |
| `backend/` (FastAPI, long-running) | **Render** (or Fly / Railway / VPS) | Needs a writable disk, ZIPs up to 50 MB, and multi-minute background analyses |

The backend **cannot** run on Vercel: serverless functions cap request bodies
around 4.5 MB, time out after 60-300 s, have a read-only filesystem (the job
store writes to disk), and do not keep in-process state (the UI polls
`/progress/{job_id}` on whichever instance answers).

---

## 1. Backend on Render

### Option A - Blueprint (fastest)

1. Push this repo to GitHub.
2. Render Dashboard -> **New -> Blueprint** -> select the repo.
   Render reads `render.yaml` and creates `codeorbit-backend`.
3. When prompted, paste the values for the secrets (`sync: false`):
   `CHATBOT_API_KEY`, `TESTGEN_API_KEY`, `GEMINI_API_KEY`, and optionally
   `GROQ_API_KEY`, `MONGODB_URI`.
4. Deploy. Health check is `GET /health`.

### Option B - Manual Web Service

1. **New -> Web Service**, pick the repo.
2. **Root Directory:** `backend`
3. **Runtime:** Python 3.11
4. **Build Command:** `pip install -r requirements.txt`
5. **Start Command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`
6. **Health Check Path:** `/health`
7. Add the environment variables listed in `backend/.env.example`
   (`CHATBOT_*`, `TESTGEN_*`, `GEMINI_API_KEY`, `GEMINI_MODEL`,
   `MONGODB_URI`, `MONGODB_DB_NAME`).

Never commit `backend/.env` - secrets go in the host's dashboard only.

### Option C - Docker (Fly.io, Railway, a VPS)

`backend/Dockerfile` is ready:

```bash
cd backend
docker build -t codeorbit-backend .
docker run -p 8000:8000 --env-file .env -v codeorbit-data:/data codeorbit-backend
```

The image sets `CODEORACLE_DATA_DIR=/data`; mount a volume there to keep the
job store across restarts.

### Keeping results across deploys

By default the job store lives in `backend/cache/jobs` inside the clone, which
is writable but wiped whenever the service redeploys or restarts. To persist:

- **MongoDB (works on the free plan):** set `MONGODB_URI` and
  `MONGODB_DB_NAME`. Job metadata is mirrored to Mongo and reloaded on demand.
- **A Render disk (paid):** Disks -> add a disk mounted at `/var/data`, then add
  the env var `CODEORACLE_DATA_DIR=/var/data`.

Note that a free Render service also sleeps when idle, so the first request
after a pause is slow, and any in-flight analysis is interrupted by a restart.

---

## 2. Frontend on Vercel

1. Vercel -> **Add New -> Project** -> import the repo.
2. **Root Directory:** `frontend` (click *Edit* and select it).
3. Framework preset: **Vite**. Build `npm run build`, output `dist` - these
   come from `frontend/vercel.json`, already committed.
4. **Environment Variables** (Production *and* Preview):

   ```
   VITE_API_URL=https://codeorbit-backend.onrender.com
   ```

   Replace with your real backend URL, no trailing slash.

5. **Deploy.**
6. `VITE_API_URL` is baked in at build time, so after changing it you must
   redeploy for it to take effect.

Only `VITE_*` variables belong here - anything with that prefix ships inside
the public JavaScript bundle. Provider API keys stay on the backend.

---

## 3. Verify the deployment

1. `https://<backend>/health` returns `{"status":"ok", ...}`.
2. `https://<backend>/ai/status` shows both services with `"api_key_configured": true`.
3. Open the Vercel URL, upload a ZIP, wait for analysis.
4. Dependency graph, explanations, and test generation still work.
5. Click the floating chatbot icon on the results page and ask a question -
   the answer must include source citations for the repository you just uploaded.
6. DevTools -> Network: requests go to your backend URL, never to `localhost`.

### Common problems

| Symptom | Cause |
| --- | --- |
| Requests go to `localhost:8000` | `VITE_API_URL` was not set before the build |
| "Fail to fetch" / CORS error in console | wrong backend URL, or the backend is asleep/restarting |
| Chatbot says "not configured" | `CHATBOT_*` / `GEMINI_API_KEY` missing on the backend host |
| Chatbot returns 429 | provider quota exhausted - check the provider's rate limits or switch `CHATBOT_MODEL` |
| Test generation returns 410 | the extract directory was lost by a restart - re-upload, or set `MONGODB_URI` / a disk |
| Upload rejected | body limit of the host, or `CODEORACLE_MAX_ZIP_SIZE_MB` |