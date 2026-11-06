<p align="center">
  <img src="https://img.shields.io/badge/Django-5.0+-092E20?style=for-the-badge&logo=django&logoColor=white" alt="Django">
  <img src="https://img.shields.io/badge/Python-3.11%20%7C%203.12-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python">
  <img src="https://github.com/nafisshahriar/CineVerse/actions/workflows/ci.yml/badge.svg" alt="CI/CD Pipeline">
  <img src="https://img.shields.io/badge/TMDB-API%20v3-01B4E4?style=for-the-badge&logo=themoviedatabase&logoColor=white" alt="TMDB">
  <img src="https://img.shields.io/badge/IMDb-Ratings%20Sync-F5C518?style=for-the-badge&logo=imdb&logoColor=black" alt="IMDb">
  <img src="https://img.shields.io/badge/Docker-Ready-2496ED?style=for-the-badge&logo=docker&logoColor=white" alt="Docker">
  <img src="https://img.shields.io/badge/License-MIT-emerald?style=for-the-badge" alt="License">
</p>

# 🎬 CineVerse

> **Self-Hosted Cinema Streaming Frontend & Catalog Management Studio for Private Networks, Home Labs, NAS, and H5AI Directory Listings.**

Turn raw folder indexes and private network storage into a modern, cinematic personal streaming web application. CineVerse crawls your local or remote H5AI/HTTP directory listings, parses scene release file names, enriches them with high-definition TMDB metadata, syncs authentic IMDb ratings, groups duplicate copies across servers, and provides a protected web management studio.

---

## 🌟 Why CineVerse?

If you maintain a home media collection, run a NAS, use a local seedbox, or access community FTP/H5AI servers over local ISP/BDIX networks, browsing raw file lists in a browser is clunky and uninspiring:

- ❌ Bare folder listings with cryptic filenames (`Inception.2010.1080p.BluRay.x264-SPARKS.mkv`)
- ❌ No posters, backdrops, plot summaries, genres, or cast info
- ❌ No search, filtering by genre, resolution, or authentic IMDb rating
- ❌ Cluttered duplicate files across multiple storage servers

**CineVerse solves this completely:**
- ✨ **Netflix-grade Obsidian Dark Cinema UI** with high-resolution posters, backdrops, cast galleries, and direct in-browser streaming links.
- ⚡ **Multi-Pass Matching Engine** that accurately parses release filenames and bonds canonical TMDB metadata and IMDb ratings (`tt...`).
- 🔒 **Tamper-Proof Access Control** protecting the Management Studio (`/manage/`) so public visitors can browse without altering catalog data.
- 🛠️ **Dedicated Web Management Studio (`/manage/`)** to monitor catalog health, run background crawlers with live streaming terminal logs, and perform 1-click single-movie metadata repairs.
- 📀 **Multi-Source Copy Deduplication** that bundles duplicates across servers into 1 sleek movie card with a copy picker.
- 🔄 **Automated CI/CD Pipeline** with GitHub Actions validation and zero-maintenance cloud deployment.

---

## ✨ Feature Highlights

| Feature | Description |
|---|---|
| **🔒 Public Protection & Admin Auth** | The public can view, search, and stream movies, but the Management Studio (`/manage/`) and editing APIs are locked behind staff authentication. |
| **🛠️ Management Studio (`/manage/`)** | Web dashboard to trigger background syncs, monitor task progress with live percentage/ETA, inspect catalog health, and repair single-movie metadata on the fly. |
| **⭐ Authentic IMDb Ratings** | Uses canonical IMDb ID resolution and Cinemeta synchronization. Eliminates 1-vote 10.0 biases so "Highest Rated" reflects true world cinema classics. |
| **🎯 Multi-Pass TMDB Engine** | 4-pass search heuristic featuring adjacent-year (`±1`) tolerance for film festivals vs. theatrical releases, scene tag sanitization, and candidate proximity sorting. |
| **📀 Copy Deduplication** | Automatically groups duplicate releases (1080p, 4K, multiple FTP servers) into 1 card with a copy switcher and direct playback URLs in the detail modal. |
| **🎨 Obsidian Dark Cinema UI** | Sleek charcoal glassmorphic surfaces, sapphire accents, warm star badges, and keyboard navigation. |
| **⚡ Sustainable Metrics** | Supports TMDB Changes API to refresh only recently modified titles, saving over 95% of API quota calls. |
| **🔍 Real-Time Filter & Search** | Instant AJAX searching across titles, actors, directors; filter by language, server source, resolution (4K/1080p/720p), genre, and rating. |
| **🔢 Smart Pagination** | Windowed page navigation (`1 ... 14, 15, 16 ... 374`) with instant "Jump to Page" input and browser history (`popstate`) preservation. |
| **🚀 Automated CI/CD** | GitHub Actions testing matrix across Python 3.11 & 3.12 with automated zero-maintenance deployment. |
| **🐳 Docker & Compose** | Pre-configured container setup for 1-command deployment on Unraid, TrueNAS, Portainer, or Raspberry Pi. |

---

## 🔒 Security: Public Protection & Admin Access Control

When CineVerse is deployed online, public visitors can browse the movie catalog, search, filter, and stream, but **cannot modify your library or trigger maintenance jobs**:

1. **Guest Mode (Public Visitors)**:
   - Browse the catalog, search, filter, view synopses, and watch streams.
   - Maintenance buttons, console links, and the inline "Edit Year / ID" modal strip are hidden.
   - Direct requests to `/manage/` redirect to the admin login.
   - Any unauthenticated calls to `/manage/api/...` receive `403 Forbidden`.
2. **Admin Mode (Staff Login)**:
   - Click **Admin Login** in the top navbar (or visit `/admin/login/?next=/manage/`).
   - Full access to the Management Studio, background crawlers, single-movie repairs, and delete actions.
3. **Automated Admin Creation**:
   Set `DJANGO_SUPERUSER_USERNAME` and `DJANGO_SUPERUSER_PASSWORD` in your cloud environment variables. CineVerse automatically creates or updates your administrator account upon deployment without requiring shell access.

---

## ⚡ CI/CD Pipeline (Continuous Integration & Continuous Deployment)

CineVerse includes a preconfigured GitHub Actions workflow in [`.github/workflows/ci.yml`](.github/workflows/ci.yml):

```
 Git Push (main) ───► [ CI: Test & Lint ] ───► [ CD: Automated Deploy ] ───► Live Production
                      ├── Python 3.11 & 3.12   └── Trigger Render Hook      (Render.com / Fly)
                      ├── Django System Check
                      ├── Migration Verification
                      └── 14 Automated Tests
```

### 1. Continuous Integration (CI)
Runs on every push and pull request to `main`:
- **Python Matrix**: Tests on both Python 3.11 and 3.12.
- **Django Check**: Verifies settings, templates, and application configurations (`python manage.py check`).
- **Migration Drift Check**: Ensures all model changes have committed migration files (`python manage.py makemigrations --check --dry-run`).
- **Test Suite**: Executes model tests, filename parsing tests, search queries, and management security assertions (`python manage.py test`).

### 2. Continuous Deployment (CD)
When CI tests pass on `main`:
- Automatically triggers a deployment signal to your cloud host (e.g. Render Deploy Hook).
- If using Render's native GitHub integration, Render automatically pulls the latest commit and runs [`build.sh`](build.sh).

---

## ☁️ Zero-Maintenance Free Cloud Deployment Guide

Want CineVerse running 24/7 without keeping your personal computer or laptop on? Deploy to **[Render.com](https://render.com/)** on their 100% Free Web Service tier:

### Step 1: Create a Render Web Service
1. Sign up for a free account at [render.com](https://render.com/).
2. Click **New +** → **Web Service**.
3. Connect your GitHub repository: `yourusername/CineVerse`.
4. Configure the service settings:
   - **Name**: `cineverse`
   - **Environment**: `Python 3`
   - **Region**: Choose the closest region to your users.
   - **Branch**: `main`
   - **Build Command**: `./build.sh` (or `pip install -r requirements.txt && python manage.py collectstatic --no-input && python manage.py migrate --no-input && python manage.py ensure_admin`)
   - **Start Command**: `gunicorn moviedash.wsgi:application`
   - **Plan Type**: `Free`

### Step 2: Configure Environment Variables
In the Render dashboard under **Environment**, add:

| Key | Value | Description |
|---|---|---|
| `DJANGO_SECRET_KEY` | *(generate a long random string)* | Cryptographic signing key |
| `DJANGO_DEBUG` | `False` | Run in production mode |
| `DJANGO_ALLOWED_HOSTS` | `*` (or `your-app.onrender.com`) | Host header validation |
| `MANAGE_REQUIRE_AUTH` | `True` | Lock down the `/manage/` dashboard |
| `DJANGO_SUPERUSER_USERNAME` | `admin` | Your admin username |
| `DJANGO_SUPERUSER_PASSWORD` | *(your strong password)* | Your admin login password |
| `TMDB_API_KEY` | *(your TMDB API key)* | Free key from [TMDB](https://www.themoviedb.org/settings/api) |
| `DATABASE_URL` | *(optional)* | External PostgreSQL URL (e.g. Supabase/Neon) |

### Step 3: Connect CI/CD (Optional Render Deploy Hook)
1. In Render, go to **Settings** → **Deploy Hook** and copy the URL.
2. In your GitHub repository, go to **Settings** → **Secrets and variables** → **Actions**.
3. Create a repository secret named `RENDER_DEPLOY_HOOK_URL` and paste the URL.
4. Now, every time you push to `main`, GitHub Actions validates your code and deploys it automatically!

---

## 🌐 Private Media & Torrent Access Architecture

If your video files are hosted on a private network (e.g. `172.16.x.x`, local NAS, BDIX FTP, torrent seedbox):

- **Browser Direct Streaming**: When a user clicks to play a video file, the stream URL is loaded directly by their web browser. If their device is on the local network (or connected via VPN), the video plays directly from the local server at full LAN speed, even if the CineVerse frontend is hosted on Render!
- **Cloudflare Tunnel (`cloudflared`) Alternative**: If you prefer self-hosting on a home lab PC or Raspberry Pi with zero port forwarding, run:
  ```bash
  cloudflared tunnel --url http://localhost:8000
  ```
  This securely publishes your local instance to a free HTTPS domain without exposing your home IP.

---

## 🚀 Local Development Setup

```bash
# 1. Clone & create virtual environment
git clone https://github.com/nafisshahriar/CineVerse.git
cd CineVerse
python -m venv .venv

# Windows
.venv\Scripts\activate
# Linux/macOS
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Setup environment variables
cp .env.example .env
# Edit .env with your TMDB_API_KEY and credentials

# 4. Run migrations and auto-create admin
python manage.py migrate
python manage.py ensure_admin
python manage.py runserver
```

---

## 🛠️ Management Commands

```bash
# Full Pipeline: Crawl directory, enrich TMDB metadata, fetch credits
python manage.py update_all --url "http://your-server/movies/"

# Sync Authentic IMDb Ratings (Concurrent Multi-Threaded)
python manage.py fetch_imdb_ratings --all --workers 16

# Backfill and Normalize Genres across all titles
python manage.py fetch_genres --all --workers 16

# Sustainable Refresh (TMDB Changes API - saves 95%+ calls)
python manage.py refresh_metadata --changes --days 7

# Ensure Admin User Exists (reads from env vars or arguments)
python manage.py ensure_admin --username admin --password "secret"
```

---

## 📁 Project Architecture

```
CineVerse/
├── .github/workflows/          # CI/CD Workflows
│   └── ci.yml                  # Matrix testing, linting, and deploy hook
├── moviedash/                  # Django project root
│   ├── settings.py             # Security, Whitenoise, and database config
│   ├── urls.py                 # Core routing
│   └── wsgi.py                 # WSGI application entry
├── movies/                     # Main cinema application
│   ├── models.py               # Movie, MovieDetail, CrawledDirectory models
│   ├── views.py                # Protected Management views & public APIs
│   ├── services.py             # Multi-pass TMDB matching & Cinemeta sync
│   ├── task_runner.py          # Asynchronous background job engine
│   ├── tests.py                # 14 automated unit & security tests
│   ├── utils.py                # Scene filename parsers & H5AI crawlers
│   ├── management/commands/    # CLI management commands (update_all, etc.)
│   └── templates/movies/       # Obsidian dark templates (index, manage)
├── build.sh                    # Zero-config cloud build script (Render/Fly)
├── Dockerfile                  # Container build recipe
├── docker-compose.yml          # Container orchestration recipe
├── requirements.txt            # Python dependencies (Django, Whitenoise, etc.)
├── Procfile                    # Cloud platform process manager
└── LICENSE                     # MIT Open Source License
```

---

## ⚖️ Legal Disclaimer

CineVerse is an open-source personal media catalog organizer and web presentation layer (similar to Kodi, Jellyfin, or Radarr).

- **No Media Hosting:** CineVerse does **not** host, store, index, scrape, or distribute any video files, torrents, or pirated media.
- **User Ownership:** All media stream links are strictly defined by the user through their own privately configured directory listings or local network servers.
- **Metadata Sources:** Metadata and posters are retrieved via third-party APIs (The Movie Database TMDB and Cinemeta/IMDb) in compliance with their respective developer terms.

---

## 📄 License

This project is open-source and released under the terms of the [MIT License](LICENSE).  
Copyright (c) 2024-2026 **S. M. Nafis Shahriar**.
