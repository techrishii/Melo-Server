# Audio Service

This FastAPI service accepts a public media URL, downloads its best available audio stream with `yt-dlp`, converts the first audio stream to MP3 with FFmpeg, and exposes the finished file for download.

Only download media you are authorized to access and convert. A public URL does not by itself grant permission to copy or redistribute the material.

## Install

1. Install **Python 3.10 or later** and ensure `python` is on your PATH.
2. Install FFmpeg. The service uses `ffmpeg` from PATH, an explicit `FFMPEG_BINARY` environment value, or (on Windows) automatically detects `C:\ffmpeg\bin\ffmpeg.exe`.
3. In this folder, create and activate a virtual environment:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

4. Install the Python dependencies:

   ```powershell
   python -m pip install -r requirements.txt
   ```

5. Optional: copy `.env.example` values into your deployment environment. The application does not load `.env` files automatically.

## Run

```powershell
uvicorn main:app --reload
```

The interactive API documentation is then available at `http://127.0.0.1:8000/docs`.

## API flow

Create a conversion job:

```powershell
$job = Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8000/jobs `
  -ContentType application/json `
  -Body '{"source_url":"https://example.com/public-media-page"}'
$job
```

The response is `202 Accepted` and includes a `job_id` and `status_url`. Query the job until its status is `complete` or `failed`:

```powershell
Invoke-RestMethod "http://127.0.0.1:8000/jobs/$($job.job_id)"
```

When the job is complete, download the MP3:

```powershell
Invoke-WebRequest "http://127.0.0.1:8000/downloads/$($job.job_id)" -OutFile output.mp3
```

Endpoints:

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Liveness check |
| `POST` | `/jobs` | Submit `{ "source_url": "https://..." }` |
| `GET` | `/jobs/{job_id}` | Get `queued`, `processing`, `complete`, or `failed` job state |
| `GET` | `/downloads/{job_id}` | Download the MP3 after completion |

## Notes for deployment

- Jobs are held in memory. Restarting the process loses job records, and running multiple Uvicorn workers will split records between workers. For production, move job state and files to shared durable storage, and use a proper task queue such as Celery, RQ, or Dramatiq.
- The service accepts any public HTTP(S) media URL supported by yt-dlp. If it will be exposed to untrusted users, add authentication, rate limits, a URL/domain allowlist, size limits (`MAX_DOWNLOAD_BYTES`), and network egress controls before deployment.
- Completed files are kept in `storage/output` until you implement a retention/cleanup policy.
