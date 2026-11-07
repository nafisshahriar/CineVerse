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

> **Turn raw folder indexes and private storage into a personal cinema streaming web app.**  
> Built for Home Labs, NAS, Seedboxes, Local ISP/BDIX FTP, and H5AI Directory Listings.

---

<p align="center">
  <img src="docs/screenshots/hero_catalog.png" alt="CineVerse Obsidian Cinema Interface" width="100%">
</p>

---

## ⚡ 3-Step Quickstart (Start Fresh with Your Own Media)

### 1. Clone & Install
```bash
git clone https://github.com/nafisshahriar/CineVerse.git
cd CineVerse
python -m venv .venv

# Windows: .venv\Scripts\activate | Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```
*(Get a free TMDB API key from [themoviedb.org](https://www.themoviedb.org/settings/api) and set `TMDB_API_KEY` in `.env`)*

### 2. Start with a Clean, Fresh Database
Wipe demo records and start with an empty database:
```bash
python manage.py reset_catalog --yes
```

### 3. Populate with Your Media & Run
Import your media from your H5AI server, NAS, or HTTP directory listing:
```bash
# Crawl directory, match TMDB posters, backdrops, and sync authentic IMDb ratings:
python manage.py update_all --url "http://your-nas-or-h5ai-server/movies/"

# Launch CineVerse
python manage.py runserver
```
👉 Open **`http://localhost:8000`** in your browser!

---

## ✨ Features at a Glance

| Feature | Highlight |
|---|---|
| 🎨 **Obsidian Cinema UI** | Netflix-grade dark glass theme, responsive on Mobile, Tablet & Desktop. |
| 🎬 **In-Modal YouTube Trailers** | Integrated 16:9 trailer player inside the movie details dialog. |
| ⭐ **Authentic IMDb Ratings** | True canonical IMDb ratings & IDs (`tt...`) via Cinemeta to prevent vote bias. |
| ⏱️ **Runtime & Release Year** | Clean card metadata pills showing `[2024] [⏱️ 2h 46m] [★ 8.6] [Sci-Fi]`. |
| 🌐 **True Native Language** | Displays authentic TMDB/IMDb movie language (never corrupted by folder names). |
| 📀 **Copy Deduplication** | Bundles 4K, 1080p, and multi-server duplicates into 1 card with a copy switcher. |
| 🔒 **Tamper-Proof Admin Auth** | Visitors browse & stream freely, while `/manage/` operations require staff login. |
| 🛠️ **Web Management Studio** | Live task console with animated progress, streaming logs, and 1-click catalog reset. |

<p align="center">
  <img src="docs/screenshots/movie_detail_modal.png" alt="Movie Details Modal with Trailer" width="64%">
  &nbsp;
  <img src="docs/screenshots/mobile_responsive.png" alt="Mobile Responsive UI" width="32%">
</p>

---

## 🛠️ Management & Maintenance Studio (`/manage/`)

Access the management dashboard by clicking **⚙️ Management** (or sign in via `/admin/login/?next=/manage/`):

<p align="center">
  <img src="docs/screenshots/manage_studio.png" alt="CineVerse Management & Maintenance Studio" width="100%">
</p>

---

## ☁️ 1-Click Free Cloud Deployment (Render.com)

Deploy CineVerse for **free 24/7** without keeping your personal computer running:

1. Fork or push this repository to your GitHub.
2. Sign up at **[Render.com](https://render.com/)** and click **New +** → **Web Service**.
3. Select your `CineVerse` repo with:
   - **Environment**: `Python 3`
   - **Build Command**: `./build.sh`
   - **Start Command**: `gunicorn moviedash.wsgi:application`
4. Under **Environment Variables**, add:

| Key | Recommended Value | Purpose |
|---|---|---|
| `DJANGO_SECRET_KEY` | *(random 50-char string)* | Django cryptographic security |
| `DJANGO_DEBUG` | `False` | Production mode |
| `DJANGO_ALLOWED_HOSTS` | `*` | Host header validation |
| `MANAGE_REQUIRE_AUTH` | `True` | Protects `/manage/` with admin login |
| `DJANGO_SUPERUSER_USERNAME` | `admin` | Your admin username |
| `DJANGO_SUPERUSER_PASSWORD` | *(your strong password)* | Your admin password |
| `TMDB_API_KEY` | *(your TMDB API key)* | Free metadata & poster lookup |

---

## 🌐 Private Media & LAN Streaming Architecture

If your media files reside on a private local network (e.g. NAS, 172.16.x.x, local ISP FTP, or Seedbox):

```
 [ Your Phone / Tablet / TV / PC ]
        │
        ├───── 1. Loads UI & Metadata (HTTPS) ─────► [ CineVerse Web App ]
        │                                             (Render.com or Cloud)
        │
        └───── 2. Streams Video File (Direct LAN) ──► [ Local Media Server ]
                                                      (NAS / H5AI / FTP)
```

- **Zero Bandwidth Overhead**: CineVerse does not proxy large video files through the web host.
- **Full LAN Speed**: When you tap ▶ Play, the browser opens the stream URL directly from your local storage at full Gigabit/Wi-Fi speed.

---

## 🛠️ Essential CLI Commands

```bash
# 1. Reset database to start fresh with your own media library
python manage.py reset_catalog --yes

# 2. Reset and immediately crawl a media server
python manage.py reset_catalog --yes --crawl "http://your-nas/movies/"

# 3. Import movies and automatically enrich posters & metadata
python manage.py update_all --url "http://your-nas/movies/"

# 4. Multi-threaded IMDb rating resolution (fast & keyless)
python manage.py fetch_imdb_ratings --all --workers 8

# 5. Populate and normalize genre tags across all titles
python manage.py fetch_genres --all --workers 8

# 6. Create / update admin credentials without shell access
python manage.py ensure_admin --username admin --password "MySecretPass123"
```

---

## 📄 License & Disclaimer

- **Open Source:** Released under the permissive **[MIT License](LICENSE)**.
- **Media Notice:** CineVerse is a personal catalog organizer (similar to Plex, Jellyfin, or Radarr). It does **not** host, store, or distribute video files. Stream URLs are defined entirely by the user.
