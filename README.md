<p align="center">
  <img src="https://img.shields.io/badge/Django-4.2+-092E20?style=for-the-badge&logo=django&logoColor=white" alt="Django">
  <img src="https://img.shields.io/badge/Python-3.9+-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/TMDB-API-01B4E4?style=for-the-badge&logo=themoviedatabase&logoColor=white" alt="TMDB">
  <img src="https://img.shields.io/badge/License-MIT-yellow?style=for-the-badge" alt="License">
</p>

# 🎬 CineVerse

A feature-rich **Movie Dashboard** built with Django that crawls movie files from H5AI directory listings and enriches them with metadata from TMDB (The Movie Database).

![Dashboard Preview](https://via.placeholder.com/800x400?text=CineVerse+Dashboard)

---

## ✨ Features

- **🔍 Smart Search** — Real-time AJAX-powered search with live filtering
- **📊 Rich Metadata** — Automatically fetches posters, ratings, cast, directors, and more from TMDB
- **🎭 Multi-Genre Filtering** — Filter movies by multiple genres simultaneously
- **📅 Year & Rating Filters** — Narrow down by release year or rating range
- **🔄 Automatic Crawling** — Crawl H5AI directories for movie files
- **📱 Responsive Design** — Beautiful dark-themed UI that works on all devices
- **🔗 Direct Play Links** — Click to stream directly from your media server

---

## 📋 Table of Contents

- [Prerequisites](#prerequisites)
- [Quick Start](#-quick-start)
- [Environment Variables](#-environment-variables)
- [Management Commands](#-management-commands)
- [Deployment](#-deployment)
  - [Railway](#railway)
  - [Raspberry Pi](#raspberry-pi)
- [Project Structure](#-project-structure)
- [API Reference](#-api-reference)
- [Contributing](#-contributing)
- [License](#-license)

---

## Prerequisites

- Python 3.9+
- pip (Python package manager)
- [TMDB API Key](https://www.themoviedb.org/settings/api) (free)

---

## 🚀 Quick Start

### 1. Clone the Repository

```bash
git clone https://github.com/yourusername/CineVerse.git
cd CineVerse
```

### 2. Create a Virtual Environment

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate

# Linux/macOS
source .venv/bin/activate
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure Environment Variables

Create a `.env` file in the project root:

```env
DJANGO_SECRET_KEY=your-super-secret-key-here
DJANGO_DEBUG=True
TMDB_API_KEY=your-tmdb-api-key
H5AI_BASE_URL=https://your-h5ai-server.com/files/
```

### 5. Run Migrations

```bash
python manage.py migrate
```

### 6. Start the Development Server

```bash
python manage.py runserver
```

Visit `http://127.0.0.1:8000` in your browser! 🎉

---

## 🔐 Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `DJANGO_SECRET_KEY` | Django secret key for cryptographic signing | ✅ |
| `DJANGO_DEBUG` | Enable debug mode (`True`/`False`) | ❌ (default: `True`) |
| `DJANGO_ALLOWED_HOSTS` | Comma-separated list of allowed hosts | ❌ (required if DEBUG=False) |
| `TMDB_API_KEY` | Your TMDB API key for metadata fetching | ✅ |
| `H5AI_BASE_URL` | Base URL of your H5AI media server | ❌ |

---

## 🛠 Management Commands

CineVerse includes several custom Django management commands:

### Crawl H5AI Directory

Scan your H5AI server for movie files:

```bash
python manage.py crawl_h5ai --url "https://your-h5ai/files/"
```

### Fetch Movie Credits

Fetch cast and director information for movies:

```bash
python manage.py fetch_credits
```

### Refresh Metadata

Update ratings, popularity, and vote counts from TMDB:

```bash
# Refresh all fetched movies
python manage.py refresh_metadata --all

# Refresh a specific movie
python manage.py refresh_metadata --movie-id 123

# Limit the number of movies to process
python manage.py refresh_metadata --limit 50
```

### Report Missing Metadata

Generate a report of movies missing metadata:

```bash
python manage.py report_missing
```

---

## 🚢 Deployment

### Railway

1. Push your code to GitHub
2. Connect your repo to [Railway](https://railway.app)
3. Add environment variables in Railway dashboard
4. Railway auto-detects the `Procfile` and deploys!

**Required files for Railway deployment:**
- `requirements.txt` — Python dependencies (includes `gunicorn`)
- `Procfile` — Startup command: `web: gunicorn moviedash.wsgi --log-file -`

---

### Raspberry Pi

Deploy CineVerse on a Raspberry Pi for a self-hosted movie dashboard.

#### Auto-Start on Boot

1. **Copy deployment files** (included in `pi_deployment/`):
   - `cineverse_startup.sh` — Startup script
   - `cineverse.service` — Systemd service file

2. **Make the startup script executable:**

```bash
chmod +x ~/movie_verse2/CineVerse/pi_deployment/cineverse_startup.sh
```

3. **Enable the systemd service:**

```bash
sudo ln -s ~/movie_verse2/CineVerse/pi_deployment/cineverse.service /etc/systemd/system/cineverse.service
sudo systemctl daemon-reload
sudo systemctl enable cineverse.service
sudo systemctl start cineverse.service
```

4. **Check status:**

```bash
sudo systemctl status cineverse.service
```

#### Security Recommendations

When exposing your Pi to the internet:

```bash
# Install and configure firewall
sudo apt install ufw
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow ssh
sudo ufw allow 8000/tcp
sudo ufw enable

# Install fail2ban for brute-force protection
sudo apt install fail2ban
sudo systemctl enable fail2ban
```

---

## 📁 Project Structure

```
CineVerse/
├── movies/                    # Main Django app
│   ├── management/commands/   # Custom management commands
│   │   ├── crawl_h5ai.py     # H5AI directory crawler
│   │   ├── fetch_credits.py  # Fetch cast/director info
│   │   ├── refresh_metadata.py # Update TMDB data
│   │   └── report_missing.py # Missing metadata report
│   ├── models.py             # Database models
│   ├── views.py              # View controllers
│   ├── services.py           # TMDB API service
│   └── utils.py              # Utility functions
├── moviedash/                 # Django project settings
│   ├── settings.py           # Configuration
│   ├── urls.py               # URL routing
│   └── wsgi.py               # WSGI entry point
├── templates/                 # HTML templates
├── static/                    # Static assets (CSS, JS)
├── pi_deployment/             # Raspberry Pi deployment files
├── requirements.txt           # Python dependencies
├── Procfile                   # Railway/Heroku process file
└── manage.py                  # Django CLI
```

---

## 📡 API Reference

### Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Main dashboard with movie grid |
| `/search/` | GET | AJAX search endpoint |
| `/movie/<id>/` | GET | AJAX movie detail modal data |

### Query Parameters (Search)

| Parameter | Type | Description |
|-----------|------|-------------|
| `q` | string | Search query (title) |
| `year_min` | int | Minimum release year |
| `year_max` | int | Maximum release year |
| `rating_min` | float | Minimum rating (0-10) |
| `genre` | string[] | Genre filter (multiple allowed) |
| `sort` | string | Sort field (`title`, `year`, `rating`, `popularity`) |
| `page` | int | Page number |

---

## 🤝 Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/AmazingFeature`)
3. Commit your changes (`git commit -m 'Add some AmazingFeature'`)
4. Push to the branch (`git push origin feature/AmazingFeature`)
5. Open a Pull Request

---

## 📄 License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.

---

<p align="center">
  Made with ❤️ and Django
</p>
