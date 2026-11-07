import json
from django.test import TestCase, Client, override_settings
from django.contrib.auth import get_user_model
from django.urls import reverse
from movies.models import Movie, MovieDetail
from movies.utils import clean_movie_title, detect_resolution, parse_movie_info, detect_language, fetch_imdb_rating_and_id
from movies.task_runner import task_manager, is_task_stopped

User = get_user_model()


class MovieModelAndUtilsTests(TestCase):
    """Test movie filename parsing, title normalization, and models."""

    def test_clean_movie_title(self):
        filename = "Inception.2010.1080p.BluRay.x264-SPARKS.mkv"
        cleaned, year = clean_movie_title(filename)
        self.assertEqual(cleaned, "Inception")
        self.assertEqual(year, "2010")

    def test_detect_resolution(self):
        filename = "The.Batman.2022.2160p.4K.UHD.HDR.x265.mkv"
        res = detect_resolution(filename)
        self.assertEqual(res, "4K")

    def test_parse_movie_info(self):
        info = parse_movie_info(
            dir_name="Inception (2010) [1080p]",
            file_name="Inception.2010.1080p.mkv",
            file_url="http://172.16.50.14/movies/Inception.2010.1080p.mkv"
        )
        self.assertEqual(info['clean_title'], "Inception")
        self.assertEqual(info['year'], 2010)
        self.assertEqual(info['resolution'], "1080P")
        self.assertEqual(info['source'], "Dhaka-Flix 14")

    def test_detect_language(self):
        self.assertEqual(detect_language("/Bangla_Cinema/"), "Bengali")
        self.assertEqual(detect_language("/Hindi_Movies/"), "Hindi")
        self.assertEqual(detect_language("/English_Movies/"), "English")

    def test_movie_creation_and_primary_copy(self):
        m1 = Movie.objects.create(
            title="Dune Part Two 2024 1080p",
            clean_title="Dune: Part Two",
            year=2024,
            year_str="2024",
            resolution="1080P",
            source="Dhaka-Flix 14",
            file_url="http://172.16.50.14/movies/Dune2.1080p.mkv",
            group_key="dune part two_2024",
            is_primary_copy=True,
            metadata_status="ok",
            imdb_id="tt15239678",
            imdb_rating=8.5,
            genres="Action, Adventure, Sci-Fi"
        )
        self.assertEqual(m1.display_title, "Dune: Part Two")
        self.assertTrue(m1.is_primary_copy)

        # Second copy of the same movie (e.g. 4K version)
        m2 = Movie.objects.create(
            title="Dune Part Two 2024 4K",
            clean_title="Dune: Part Two",
            year=2024,
            year_str="2024",
            resolution="4K",
            source="Dhaka-Flix 7",
            file_url="http://172.16.50.7/movies/Dune2.4K.mkv",
            group_key="dune part two_2024",
            is_primary_copy=False,
            metadata_status="ok",
        )
        self.assertEqual(Movie.objects.filter(group_key="dune part two_2024").count(), 2)
        self.assertEqual(Movie.objects.filter(group_key="dune part two_2024", is_primary_copy=True).count(), 1)


class PublicCatalogViewTests(TestCase):
    """Test public viewing, search, and detail AJAX endpoints."""

    def setUp(self):
        self.client = Client()
        self.movie1 = Movie.objects.create(
            title="Inception 2010 1080p",
            clean_title="Inception",
            year=2010,
            year_str="2010",
            resolution="1080P",
            source="Dhaka-Flix 14",
            file_url="http://example.com/inception.mkv",
            group_key="inception_2010",
            is_primary_copy=True,
            metadata_status="ok",
            imdb_id="tt1375666",
            imdb_rating=8.8,
            genres="Action, Sci-Fi, Thriller"
        )
        self.detail = MovieDetail.objects.create(
            movie=self.movie1,
            overview="A thief who steals corporate secrets through dream-sharing technology.",
            genres="Action, Sci-Fi",
            runtime=148,
            cast=[{"name": "Leonardo DiCaprio", "character": "Cobb"}],
            director={"name": "Christopher Nolan"}
        )

    def test_index_page_returns_200(self):
        response = self.client.get(reverse('movies_index'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Inception")

    def test_search_ajax_query(self):
        response = self.client.get(reverse('movies_search_ajax'), {'q': 'Inception'})
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertIn('html', data)
        self.assertEqual(data['count'], 1)

    def test_search_ajax_genre_filter(self):
        response = self.client.get(reverse('movies_search_ajax'), {'genre': 'Sci-Fi'})
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data['count'], 1)

        # Non-matching genre
        response = self.client.get(reverse('movies_search_ajax'), {'genre': 'Horror'})
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data['count'], 0)

    def test_movie_detail_ajax_public_access(self):
        response = self.client.get(reverse('movie_detail', args=[self.movie1.id]))
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data['title'], "Inception")
        self.assertEqual(data['imdb_id'], "tt1375666")
        self.assertEqual(data['imdb_rating'], 8.8)
        self.assertIn('Christopher Nolan', str(data['director']))
        # Anonymous users should NOT have can_manage permission when auth is required
        with override_settings(MANAGE_REQUIRE_AUTH=True):
            resp = self.client.get(reverse('movie_detail', args=[self.movie1.id]))
            d = json.loads(resp.content)
            self.assertFalse(d['can_manage'])


class ManagementSecurityTests(TestCase):
    """Test authentication and authorization for the Management Studio."""

    def setUp(self):
        self.client = Client()
        self.staff_user = User.objects.create_user(
            username='admin_user',
            password='Password123!',
            is_staff=True,
            is_superuser=True
        )
        self.regular_user = User.objects.create_user(
            username='guest_user',
            password='Password123!',
            is_staff=False
        )
        self.movie = Movie.objects.create(
            title="Sample Movie 2021",
            clean_title="Sample Movie",
            year=2021,
            year_str="2021",
            group_key="sample movie_2021",
            is_primary_copy=True,
            file_url="http://example.com/sample.mkv"
        )

    @override_settings(MANAGE_REQUIRE_AUTH=True)
    def test_anonymous_manage_dashboard_redirects_to_login(self):
        response = self.client.get(reverse('manage_dashboard'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/admin/login/', response.url)

    @override_settings(MANAGE_REQUIRE_AUTH=True)
    def test_anonymous_manage_api_returns_403(self):
        # Stats API
        res_stats = self.client.get(reverse('manage_stats_api'))
        self.assertEqual(res_stats.status_code, 403)
        data = json.loads(res_stats.content)
        self.assertFalse(data['success'])
        self.assertEqual(data['error'], 'Unauthorized')

        # Task start API
        res_task = self.client.post(
            reverse('manage_task_start_api'),
            data=json.dumps({'task_type': 'imdb'}),
            content_type='application/json'
        )
        self.assertEqual(res_task.status_code, 403)

        # Single movie edit action API
        res_action = self.client.post(
            reverse('manage_movie_action_api', args=[self.movie.id]),
            data=json.dumps({'action': 'delete'}),
            content_type='application/json'
        )
        self.assertEqual(res_action.status_code, 403)
        # Ensure movie was not deleted
        self.assertTrue(Movie.objects.filter(pk=self.movie.id).exists())

    @override_settings(MANAGE_REQUIRE_AUTH=True)
    def test_anonymous_can_refresh_single_movie_without_403(self):
        """Anonymous visitors must be able to fetch/refresh single movie metadata without 403 errors."""
        # 1. Dedicated public endpoint
        res_pub = self.client.post(reverse('movie_refresh_ajax', args=[self.movie.id]))
        self.assertEqual(res_pub.status_code, 200)

        # 2. Action API refresh action
        res_act = self.client.post(
            reverse('manage_movie_action_api', args=[self.movie.id]),
            data=json.dumps({'action': 'refresh'}),
            content_type='application/json'
        )
        self.assertEqual(res_act.status_code, 200)

        # 3. Read-only task status polling
        res_stat = self.client.get(reverse('manage_task_status_api'))
        self.assertEqual(res_stat.status_code, 200)

    @override_settings(MANAGE_REQUIRE_AUTH=True)
    def test_regular_user_cannot_access_manage_dashboard(self):
        self.client.login(username='guest_user', password='Password123!')
        response = self.client.get(reverse('manage_dashboard'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/admin/login/', response.url)

    @override_settings(MANAGE_REQUIRE_AUTH=True)
    def test_staff_user_can_access_manage_dashboard_and_api(self):
        self.client.login(username='admin_user', password='Password123!')

        # Dashboard HTML
        response = self.client.get(reverse('manage_dashboard'))
        self.assertEqual(response.status_code, 200)

        # Stats API
        res_stats = self.client.get(reverse('manage_stats_api'))
        self.assertEqual(res_stats.status_code, 200)
        data = json.loads(res_stats.content)
        self.assertIn('total_files', data)

    @override_settings(MANAGE_REQUIRE_AUTH=True, MANAGE_SECRET_TOKEN='super-secret-token-123')
    def test_secret_token_allows_api_access(self):
        # Without token: 403
        r1 = self.client.get(reverse('manage_stats_api'))
        self.assertEqual(r1.status_code, 403)

        # With X-Manage-Token header: 200 OK
        r2 = self.client.get(reverse('manage_stats_api'), HTTP_X_MANAGE_TOKEN='super-secret-token-123')
        self.assertEqual(r2.status_code, 200)

    def test_task_stop_flag_and_cancellation(self):
        task_manager.stop_requested.clear()
        self.assertFalse(is_task_stopped())
        task_manager.stop_requested.set()
        self.assertTrue(is_task_stopped())
        task_manager.stop_requested.clear()

    def test_clean_title_trailing_slashes(self):
        clean, year = clean_movie_title("One Last Shot /")
        self.assertEqual(clean, "One Last Shot")
        clean2, _ = clean_movie_title("Grand Theft Auto VI An Extended Look /")
        self.assertEqual(clean2, "Grand Theft Auto VI An Extended Look")

    def test_clean_bracket_titles(self):
        clean, year = clean_movie_title("[Rec] (2007) 1080p (Spain)")
        self.assertEqual(clean, "Rec")
        self.assertEqual(year, "2007")

    def test_task_manager_completion_sets_100_percent(self):
        task_manager.task_state['status'] = 'running'
        task_manager.task_state['progress_percent'] = 45.0
        # Simulating clean wrapper completion
        with task_manager.state_lock:
            task_manager.task_state['status'] = 'completed'
            task_manager.task_state['progress_percent'] = 100.0
            task_manager.task_state['remaining'] = 0
        status = task_manager.get_status()
        self.assertEqual(status['status'], 'completed')
        self.assertEqual(status['progress_percent'], 100.0)
        self.assertEqual(status['remaining'], 0)

    def test_fetch_imdb_rating_known_id(self):
        # Inception tt1375666
        rate, i_id, genres = fetch_imdb_rating_and_id("Inception", 2010, known_imdb_id="tt1375666")
        self.assertIsNotNone(rate)
        self.assertEqual(i_id, "tt1375666")
        self.assertGreater(rate, 8.0)

    def test_fetch_commands_run_without_db_lock(self):
        from django.core.management import call_command
        import io
        out = io.StringIO()
        m = Movie.objects.create(
            title="Inception 2010",
            clean_title="Inception",
            year=2010,
            is_primary_copy=True,
            metadata_status="missing"
        )
        call_command('fetch_missing_metadata', limit=1, workers=2, stdout=out)
        m.refresh_from_db()
        self.assertEqual(m.metadata_status, 'ok')
        self.assertTrue(bool(m.poster_url))

        # Now test IMDb rating fetch command
        call_command('fetch_imdb_ratings', limit=1, workers=2, stdout=out)
        m.refresh_from_db()
        self.assertIsNotNone(m.imdb_rating)
        self.assertTrue(m.imdb_id.startswith('tt'))

    def test_movie_runtime_display(self):
        m = Movie.objects.create(
            title="Inception",
            file_url="http://example.com/inc.mkv",
            runtime=148
        )
        self.assertEqual(m.runtime_display, "2h 28m")

        # Test fallback to detail.runtime
        m2 = Movie.objects.create(
            title="Short Film",
            file_url="http://example.com/short.mkv",
            runtime=None
        )
        MovieDetail.objects.create(movie=m2, runtime=45)
        self.assertEqual(m2.runtime_display, "45m")

    def test_movie_detail_ajax_returns_tmdb_language_and_trailer(self):
        m = Movie.objects.create(
            title="Parasite 2019",
            clean_title="Parasite",
            year=2019,
            language="English",  # Fake folder language from crawl
            file_url="http://example.com/parasite.mkv"
        )
        detail = MovieDetail.objects.create(
            movie=m,
            tmdb_id=496243,
            cast=[{'name': 'Song Kang-ho'}],
            director={'name': 'Bong Joon-ho'},
            original_language="ko",  # Official TMDB language
            trailer_key="5xH0R_uie1U",
            trailer_url="https://www.youtube.com/watch?v=5xH0R_uie1U"
        )
        response = self.client.get(f'/movie/{m.id}/detail/')
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        # Must be authentic Korean, NOT "English" from the folder path
        self.assertEqual(data['language'], "Korean")
        self.assertEqual(data['trailer_key'], "5xH0R_uie1U")
        self.assertEqual(data['trailer_url'], "https://www.youtube.com/watch?v=5xH0R_uie1U")

    def test_movie_card_only_runtime_imdb_and_genre(self):
        from django.template.loader import render_to_string
        m = Movie.objects.create(
            title="Dune Part Two 2024",
            clean_title="Dune: Part Two",
            year=2024,
            runtime=166,
            imdb_rating=8.6,
            genres="Sci-Fi, Adventure",
            resolution="4K",
            source="Dhaka-Flix 14",
            file_url="http://example.com/dune.mkv"
        )
        html = render_to_string('movies/_movie_cards.html', {'movies': [m]})
        # Must contain year, runtime, imdb rating, and primary genre
        self.assertIn("card-year", html)
        self.assertIn("2024", html)
        self.assertIn("2h 46m", html)
        self.assertIn("8.6", html)
        self.assertIn("Sci-Fi", html)
        # Must NOT contain resolution badges or server badges on card
        self.assertNotIn("res-badge", html)
        self.assertNotIn("source-badge", html)
        self.assertNotIn("Dhaka-Flix 14", html)
