<p align="center">
  <img src="https://img.shields.io/badge/Django-5.0+-092E20?style=for-the-badge&logo=django&logoColor=white" alt="Django">
  <img src="https://img.shields.io/badge/Python-3.10+-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/TMDB-API%20v3-01B4E4?style=for-the-badge&logo=themoviedatabase&logoColor=white" alt="TMDB">
  <img src="https://img.shields.io/badge/IMDb-Ratings%20Sync-F5C518?style=for-the-badge&logo=imdb&logoColor=black" alt="IMDb">
  <img src="https://img.shields.io/badge/Docker-Ready-2496ED?style=for-the-badge&logo=docker&logoColor=white" alt="Docker">
  <img src="https://img.shields.io/badge/License-MIT-emerald?style=for-the-badge" alt="License">
</p>

# 🎬 CineVerse

> **Self-Hosted Cinema Streaming Frontend & Catalog Management Studio for Private Networks, Home Labs, NAS, and H5AI Directory Listings.**

Turn raw folder indexes and private network storage into a modern, cinematic personal streaming web application. CineVerse crawls your local or remote H5AI/HTTP directory listings, parses scene release file names, enriches them with high-definition TMDB metadata, syncs authentic IMDb ratings, groups duplicate copies across servers, and provides a powerful web management studio.

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
- 🛠️ **Dedicated Web Management Studio (`/manage/`)** to monitor catalog health, run background crawlers with live streaming terminal logs, and perform 1-click single-movie metadata repairs.
- 📀 **Multi-Source Copy Deduplication** that bundles duplicates across servers into 1 sleek movie card with a copy picker.

---

## ✨ Feature Highlights

| Feature | Description |
|---|---|
| **🛠️ Management Studio (`/manage/`)** | Web dashboard to trigger background syncs, monitor task progress with live percentage/ETA, inspect catalog health, and repair single-movie metadata on the fly. |
| **⭐ Authentic IMDb Ratings** | Uses canonical IMDb ID resolution and Cinemeta synchronization. Eliminates 1-vote 10.0 biases so "Highest Rated" reflects true world cinema classics. |
| **🎯 Multi-Pass TMDB Engine** | 4-pass search heuristic featuring adjacent-year (`±1`) tolerance for film festivals vs. theatrical releases, scene tag sanitization, and candidate proximity sorting. |
| **📀 Copy Deduplication** | Automatically groups duplicate releases (1080p, 4K, multiple FTP servers) into 1 card with a copy switcher and direct playback URLs in the detail modal. |
| **🎨 Obsidian Dark Cinema UI** | Sleek charcoal glassmorphic surfaces, sapphire accents, warm star badges, and keyboard navigation. |
| **⚡ Sustainable Metrics** | Supports TMDB Changes API to refresh only recently modified titles, saving over 95% of API quota calls. |
| **🔍 Real-Time Filter & Search** | Instant AJAX searching across titles, actors, directors; filter by language, server source, resolution (4K/1080p/720p), genre, and rating. |
| **🔢 Smart Pagination** | Windowed page navigation (`1 ... 14, 15, 16 ... 374`) with instant "Jump to Page" input and browser history (`popstate`) preservation. |
| **🐳 Docker & Compose** | Pre-configured container setup for 1-command deployment on Unraid, TrueNAS, Portainer, or Raspberry Pi. |

---

## 🚀 Quick Start

### Option A: Local Development Setup

#### 1. Clone & Setup Environment

```bash
git clone https://github.com/nafisshahriar/CineVerse.git
cd CineVerse

# Create and activate virtual environment
python -m venv .venv

# Windows
.venv\Scripts\activate

# Linux / macOS
source .venv/bin/activate
```

#### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

#### 3. Configure Environment Variables

Copy the example configuration:

```bash
cp .env.example .env
```

Edit `.env` and fill in your keys:

```env
DJANGO_SECRET_KEY=your-random-secret-key-here
DJANGO_DEBUG=True
TMDB_API_KEY=your_tmdb_api_key_here
H5AI_BASE_URL=http://your-local-media-server/movies/
```
*(You can get a free TMDB API key in 2 minutes at [themoviedb.org/settings/api](https://www.themoviedb.org/settings/api))*

#### 4. Run Migrations & Start Server

```bash
python manage.py migrate
python manage.py runserver
```

Open your browser at `http://127.0.0.1:8000/` to browse the catalog, or `http://127.0.0.1:8000/manage/` to access the Management Studio.

---

### Option B: Docker Compose (Recommended for Home Labs & NAS)

Run CineVerse in an isolated container with persistent database storage:

```bash
docker compose up -d
```

Access the dashboard at `http://localhost:8000`.

---

## 🌐 How to Deploy for Free (Accessing Private / Local Media Remotely)

A common challenge for home lab and local media enthusiasts: **media files are downloaded locally or hosted on private IPs (e.g., LAN, local FTP, or torrent storage on `172.16.x.x`), which cannot be accessed directly from public cloud hosts.**

Here are the best **100% free, production-tested architectures** to solve this:

### Strategy 1: Cloudflare Tunnel (Zero-Cost, Remote Access, Zero Port Forwarding) ⭐ *Recommended*

Run CineVerse locally on your PC, Home Server, or Raspberry Pi alongside your media storage, and expose it securely to the internet using **Cloudflare Tunnel (`cloudflared`)**:

1. Sign up for a free [Cloudflare](https://cloudflare.com) account (you can use any free or custom domain).
2. Install `cloudflared` on your local host:
   ```bash
   # Windows (via winget)
   winget install Cloudflare.cloudflared

   # Linux / Raspberry Pi
   sudo apt-get install cloudflared
   ```
3. Start an instant public HTTPS tunnel without opening router ports:
   ```bash
   cloudflared tunnel --url http://localhost:8000
   ```
4. Cloudflare gives you a secure, free public URL (e.g., `https://random-words.trycloudflare.com` or your own custom domain like `https://movies.yourdomain.com`).
5. **Why this works:** CineVerse and your media files remain inside your local network. The Cloudflare Tunnel securely forwards web requests and media streams over an encrypted outbound connection. You can watch your local movies from your phone, office, or anywhere in the world completely free!

---

### Strategy 2: Tailscale Mesh VPN (Free Private Access for You & Friends)

If you only want access for yourself and select family members without exposing anything publicly:

1. Install [Tailscale](https://tailscale.com/) on your local server and on your phone/laptop (free for up to 3 users / 100 devices).
2. Your server receives a 100% private, encrypted IP (e.g., `100.x.y.z`).
3. Connect anywhere using the Tailscale app, open `http://100.x.y.z:8000`, and stream your media at direct peer-to-peer speeds.

---

### Strategy 3: Free Cloud Hosting (Catalog & Discovery Portal)

If you wish to host the web interface in the cloud (e.g. for a community movie catalog, watchlist, or remote WebDAV/FTP streams):

- **[Render.com](https://render.com/)**: Free Web Service tier (750 free hours/month). Connect your GitHub repository, choose Python environment, and start with `gunicorn moviedash.wsgi`.
- **[Fly.io](https://fly.io/)**: Free allowance running the included `Dockerfile`.
- **[Oracle Cloud Always Free Tier](https://www.oracle.com/cloud/free/)**: Provides a free VPS with 4 ARM cores, 24GB RAM, and 200GB storage forever — easily hosts CineVerse, Docker, and `cloudflared`.

---

## 🛠️ Management Commands

CineVerse provides robust CLI commands for headless operation, cron jobs, and automation:

```bash
# 1. Full Pipeline: Crawl directory, enrich TMDB metadata, fetch credits
python manage.py update_all --url "http://your-server/movies/"

# 2. Sync Authentic IMDb Ratings (Concurrent Multi-Threaded)
python manage.py fetch_imdb_ratings --all --workers 16

# 3. Backfill and Normalize Genres across all titles
python manage.py fetch_genres --all --workers 16

# 4. Sustainable Refresh (TMDB Changes API - saves 95%+ calls)
python manage.py refresh_metadata --changes --days 7

# 5. Backfill Clean Titles, Resolutions, and Server Deduplication Keys
python manage.py backfill_metadata
```

---

## 📁 Project Architecture

```
CineVerse/
├── moviedash/                  # Django project root
│   ├── settings.py             # Optimized settings (WAL mode, timeouts, cache)
│   ├── urls.py                 # Core routing
│   └── wsgi.py                 # WSGI application entry
├── movies/                     # Main cinema application
│   ├── models.py               # Movie, MovieDetail, CrawledDirectory models
│   ├── views.py                # AJAX search, details, and Management API
│   ├── services.py             # TMDB API multi-pass engine & Cinemeta resolver
│   ├── task_runner.py          # Asynchronous background job engine with live ETA
│   ├── utils.py                # Regex scene parsers and H5AI directory crawler
│   ├── management/commands/    # CLI management commands (update_all, etc.)
│   └── templates/movies/       # Glassmorphic templates (index.html, manage.html)
├── static/movies/              # Production CSS & UI styling
├── pi_deployment/              # Raspberry Pi systemd startup scripts
├── Dockerfile                  # Container build recipe
├── docker-compose.yml          # Container orchestration recipe
├── requirements.txt            # Python dependencies
├── Procfile                    # Cloud platform entrypoint (Gunicorn)
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
