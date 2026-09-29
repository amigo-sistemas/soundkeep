# Soundkeep

A single-site YouTube-to-MP3 converter. The site and conversion API run in one Vercel container Function with Python, yt-dlp, Node.js (for YouTube's JavaScript challenges), and FFmpeg. The MP3 streams to the browser after conversion. No audio files are retained on the server after the response ends.

## Local development

Install Python 3.12+, Node.js 22+, and FFmpeg. Then:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
.\.venv\Scripts\python -m uvicorn app:app --reload --port 8000
```

Open `http://localhost:8000`. `GET /api/health` reports whether the required commands are on `PATH`. Run `python -m pytest` with the environment's Python to check URL validation and the synthetic audio pipeline.

## Vercel deployment

Import this repository as a Vercel project, with the repository root as the project root. Vercel detects `Dockerfile.vercel`, builds the image, and routes the project to its HTTP server. Fluid Compute is required for container Functions. A Hobby Function's default 300-second invocation budget is why each conversion stops after 240 seconds; very long downloads may not finish. The output is capped at 250 MB to stay within the Function's scratch-space budget.

The app accepts one public video or Short at a time. Private, age-restricted, geo-blocked, live, or otherwise unavailable videos can fail. YouTube periodically changes extraction behavior, so keep yt-dlp and its matching `yt-dlp-ejs` dependency current. Before opening an anonymous deployment to significant traffic, configure Vercel Firewall rate limiting to cap conversion costs; the in-process two-conversion limit is per Function instance, not a global rate limit.

The folder picker uses the File System Access API on supporting HTTPS browsers. When unavailable, the app uses a normal browser download; the visitor can choose a folder using their browser's download settings. The app never receives a local folder path.

Only convert videos you have permission to save. YouTube's terms restrict downloading content except where permitted by the service, rights holders, or applicable law.

Reference: [Vercel container Functions](https://vercel.com/kb/guide/docker), [Vercel Function limits](https://vercel.com/docs/functions/limitations), [yt-dlp JavaScript setup](https://github.com/yt-dlp/yt-dlp/wiki/EJS), [browser folder picker support](https://developer.mozilla.org/en-US/docs/Web/API/Window/showDirectoryPicker), and [YouTube Terms of Service](https://www.youtube.com/t/terms).
