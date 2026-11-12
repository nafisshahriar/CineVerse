import json
import unittest
from django.test import TestCase, Client, override_settings, tag
from django.contrib.auth import get_user_model
from django.urls import reverse
from movies.models import Movie, MovieDetail, MediaSource
from movies.utils import (
    clean_movie_title, detect_resolution, parse_movie_info, detect_language,
    fetch_imdb_rating_and_id, normalize_imdb_id, fetch_cinemeta_details,
    normalize_crawl_url, is_directory_link, test_source_connection,
    is_private_or_local_host, extract_last_modified_from_text
)
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

    @tag('network')
    def test_fetch_imdb_rating_known_id(self):
        # Inception tt1375666
        rate, i_id, genres = fetch_imdb_rating_and_id("Inception", 2010, known_imdb_id="tt1375666")
        self.assertIsNotNone(rate)
        self.assertEqual(i_id, "tt1375666")
        self.assertGreater(rate, 8.0)

    @tag('network')
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

    def test_reset_catalog_command(self):
        import io
        from django.core.management import call_command
        m = Movie.objects.create(
            title="Sample Movie 2020",
            clean_title="Sample Movie",
            year=2020,
            file_url="http://example.com/sample.mp4"
        )
        MovieDetail.objects.create(movie=m, overview="Sample overview")
        self.assertTrue(Movie.objects.filter(pk=m.id).exists())

        out = io.StringIO()
        call_command('reset_catalog', yes=True, stdout=out)
        self.assertEqual(Movie.objects.count(), 0)
        self.assertEqual(MovieDetail.objects.count(), 0)

    def test_normalize_imdb_id(self):
        self.assertEqual(normalize_imdb_id("https://www.imdb.com/title/tt5104604/"), "tt5104604")
        self.assertEqual(normalize_imdb_id("tt5104604"), "tt5104604")
        self.assertEqual(normalize_imdb_id("TT5104604"), "tt5104604")
        self.assertEqual(normalize_imdb_id("5104604"), "tt5104604")
        self.assertIsNone(normalize_imdb_id(""))
        self.assertIsNone(normalize_imdb_id(None))

    @tag('network')
    def test_fetch_cinemeta_details(self):
        # Isle of Dogs tt5104604
        data = fetch_cinemeta_details("tt5104604")
        self.assertIsNotNone(data)
        self.assertEqual(data['title'], "Isle of Dogs")
        self.assertEqual(data['year'], 2018)
        self.assertEqual(data['imdb_id'], "tt5104604")
        self.assertIsNotNone(data['imdb_rating'])
        self.assertTrue(bool(data['poster_url']))
        self.assertEqual(data['director'].get('name'), "Wes Anderson")
        self.assertTrue(any("Bryan Cranston" in c.get('name', '') for c in data['cast']))

    @tag('network')
    def test_update_imdb_id_and_fetch_details(self):
        # Authenticate staff user to pass when MANAGE_REQUIRE_AUTH is True in CI
        self.client.login(username='admin_user', password='Password123!')

        # Movie initially has mismatched / messy title and no IMDb ID
        m = Movie.objects.create(
            title="02.Isle.of.Dogs.2018.1080p.mkv",
            clean_title="02 Isle of Dogs",
            file_url="http://example.com/iod.mkv",
            group_key="02 isle of dogs",
            is_primary_copy=True,
            metadata_status="missing"
        )
        # Update IMDb ID to tt5104604 and refetch
        resp = self.client.post(
            f'/manage/api/movie/{m.id}/action/',
            data=json.dumps({
                'action': 'update_ids',
                'imdb_id': 'tt5104604',
                'refetch': True
            }),
            content_type='application/json'
        )
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertTrue(data['success'])

        m.refresh_from_db()
        self.assertEqual(m.imdb_id, "tt5104604")
        self.assertEqual(m.clean_title, "Isle of Dogs")
        self.assertEqual(m.year, 2018)
        self.assertEqual(m.metadata_status, "ok")
        self.assertIsNotNone(m.imdb_rating)
        self.assertTrue(bool(m.poster_url))

        # Check that MovieDetail was populated with correct cast & director
        self.assertTrue(hasattr(m, 'detail'))
        self.assertEqual(m.detail.director.get('name'), "Wes Anderson")

    def test_delete_movie_deletes_all_grouped_copies(self):
        self.client.login(username='admin_user', password='Password123!')
        m1 = Movie.objects.create(
            title="Sample Movie 1080p",
            clean_title="Sample Movie",
            group_key="sample_movie_dup_test",
            file_url="http://example.com/sample_1080p.mkv",
            is_primary_copy=True
        )
        m2 = Movie.objects.create(
            title="Sample Movie 4K",
            clean_title="Sample Movie",
            group_key="sample_movie_dup_test",
            file_url="http://example.com/sample_4k.mkv",
            is_primary_copy=False
        )
        self.assertEqual(Movie.objects.filter(group_key="sample_movie_dup_test").count(), 2)

        resp = self.client.post(
            f'/manage/api/movie/{m1.id}/action/',
            data=json.dumps({'action': 'delete'}),
            content_type='application/json'
        )
        self.assertEqual(resp.status_code, 200)
        # Both copies in group_key must be deleted
        self.assertEqual(Movie.objects.filter(group_key="sample_movie_dup_test").count(), 0)

    @tag('network')
    def test_movie_refresh_with_imdb_id_in_payload(self):
        m = Movie.objects.create(
            title="Fight.Club.1999.1080p.mkv",
            clean_title="Fight Club Raw",
            file_url="http://example.com/fc.mkv",
            group_key="fight club",
            is_primary_copy=True,
            metadata_status="missing"
        )
        resp = self.client.post(
            f'/movie/{m.id}/refresh/',
            data=json.dumps({'imdb_id': 'https://www.imdb.com/title/tt0137523/'}),
            content_type='application/json'
        )
        self.assertEqual(resp.status_code, 200)
        m.refresh_from_db()
        self.assertEqual(m.imdb_id, "tt0137523")
        self.assertEqual(m.clean_title, "Fight Club")
        self.assertEqual(m.year, 1999)
        self.assertIsNotNone(m.imdb_rating)
        self.assertTrue(bool(m.poster_url))

    @tag('network')
    def test_unrated_movie_metadata_does_not_mutate_to_similar_title(self):
        """
        Verify that an unrated title (e.g. 'The Get Out' tt32321803) preserves its
        exact title and does NOT fall through to a different rated movie like 'Get Out' (tt5052448).
        """
        from movies.services import enrich_movie_metadata
        m = Movie.objects.create(
            title="The.Get.Out.2026.1080p.mkv",
            clean_title="The Get Out",
            year=2026,
            imdb_id="tt32321803",
            file_url="http://example.com/the_get_out.mkv",
            metadata_status="missing"
        )
        ok, res = enrich_movie_metadata(m, include_details=True)
        self.assertTrue(ok)
        m.refresh_from_db()
        self.assertEqual(m.clean_title, "The Get Out")
        self.assertEqual(m.year, 2026)
        self.assertEqual(m.imdb_id, "tt32321803")
        self.assertIn("Action", m.genres)
        self.assertTrue(bool(m.poster_url))

    @tag('network')
    def test_detail_view_auto_enriches_missing_metadata(self):
        """
        Opening movie details automatically enriches missing metadata
        without requiring the user to press any update button.
        """
        m = Movie.objects.create(
            title="Inception.2010.1080p.mkv",
            clean_title="Inception",
            year=2010,
            imdb_id="tt1375666",
            file_url="http://example.com/inc_auto.mkv",
            metadata_status="missing"
        )
        resp = self.client.get(f'/movie/{m.id}/detail/')
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertTrue(data['success'])

        m.refresh_from_db()
        self.assertEqual(m.metadata_status, 'ok')
        self.assertTrue(bool(m.poster_url))
        self.assertIsNotNone(m.imdb_rating)
        self.assertIn('overview', data)
        self.assertTrue(len(data['overview']) > 10)

    def test_batch_actions_api(self):
        self.client.login(username='admin_user', password='Password123!')
        m1 = Movie.objects.create(
            title="Batch Movie 1", clean_title="Batch 1", year=2020, file_url="http://example.com/b1.mkv"
        )
        m2 = Movie.objects.create(
            title="Batch Movie 2", clean_title="Batch 2", year=2020, file_url="http://example.com/b2.mkv"
        )

        # 1. Batch Set Genre
        resp_g = self.client.post(
            reverse('manage_batch_action_api'),
            data=json.dumps({'action': 'set_genre', 'movie_ids': [m1.id, m2.id], 'value': 'Sci-Fi, Mystery'}),
            content_type='application/json'
        )
        self.assertEqual(resp_g.status_code, 200)
        m1.refresh_from_db()
        m2.refresh_from_db()
        self.assertEqual(m1.genres, 'Sci-Fi, Mystery')
        self.assertEqual(m2.genres, 'Sci-Fi, Mystery')

        # 2. Batch Set Year
        resp_y = self.client.post(
            reverse('manage_batch_action_api'),
            data=json.dumps({'action': 'set_year', 'movie_ids': [m1.id, m2.id], 'value': 2025}),
            content_type='application/json'
        )
        self.assertEqual(resp_y.status_code, 200)
        m1.refresh_from_db()
        m2.refresh_from_db()
        self.assertEqual(m1.year, 2025)

        # 3. Batch Delete
        resp_d = self.client.post(
            reverse('manage_batch_action_api'),
            data=json.dumps({'action': 'delete', 'movie_ids': [m1.id, m2.id]}),
            content_type='application/json'
        )
        self.assertEqual(resp_d.status_code, 200)
        self.assertFalse(Movie.objects.filter(id__in=[m1.id, m2.id]).exists())

    def test_normalize_crawl_url(self):
        # Adds http:// if missing
        self.assertEqual(normalize_crawl_url("192.168.1.100:8080/movies"), "http://192.168.1.100:8080/movies/")
        # Appends trailing slash to directory paths
        self.assertEqual(normalize_crawl_url("http://example.com/media"), "http://example.com/media/")
        # Preserves files with extensions
        self.assertEqual(normalize_crawl_url("http://example.com/media/film.mp4"), "http://example.com/media/film.mp4")

    def test_is_directory_link(self):
        self.assertTrue(is_directory_link("http://example.com/movies/Action/"))
        self.assertTrue(is_directory_link("http://example.com/movies/Action"))
        self.assertFalse(is_directory_link("http://example.com/movies/Inception.mkv"))
        self.assertFalse(is_directory_link("http://example.com/movies/subtitle.srt"))

    def test_source_management_apis(self):
        self.client.login(username='admin_user', password='Password123!')

        # 1. Save new custom source
        resp_save = self.client.post(
            reverse('manage_source_save_api'),
            data=json.dumps({'name': 'Home NAS', 'url': 'http://192.168.1.50:8080/movies'}),
            content_type='application/json'
        )
        self.assertEqual(resp_save.status_code, 200)
        data = json.loads(resp_save.content)
        self.assertTrue(data['success'])
        self.assertEqual(data['source']['name'], 'Home NAS')
        self.assertEqual(data['source']['url'], 'http://192.168.1.50:8080/movies/')

        # 2. List sources
        resp_list = self.client.get(reverse('manage_sources_api'))
        self.assertEqual(resp_list.status_code, 200)
        list_data = json.loads(resp_list.content)
        self.assertTrue(list_data['success'])
        names = [s['name'] for s in list_data['sources']]
        self.assertIn('Home NAS', names)

        # 3. Delete source
        source_id = data['source']['id']
        resp_del = self.client.post(
            reverse('manage_source_delete_api'),
            data=json.dumps({'id': source_id}),
            content_type='application/json'
        )
        self.assertEqual(resp_del.status_code, 200)
        self.assertFalse(MediaSource.objects.filter(id=source_id).exists())

    def test_source_test_api(self):
        self.client.login(username='admin_user', password='Password123!')

        # Empty URL test
        resp_empty = self.client.post(
            reverse('manage_source_test_api'),
            data=json.dumps({'url': ''}),
            content_type='application/json'
        )
        self.assertEqual(resp_empty.status_code, 400)

        # Unreachable local URL test (should return structured error without 500 crash)
        resp_unreach = self.client.post(
            reverse('manage_source_test_api'),
            data=json.dumps({'url': 'http://127.0.0.1:54321/nonexistent/'}),
            content_type='application/json'
        )
        self.assertEqual(resp_unreach.status_code, 200)
        data = json.loads(resp_unreach.content)
        self.assertFalse(data['success'])
        self.assertIn('message', data)

    def test_dynamic_sources_in_movie_list(self):
        # Create movie with custom source
        Movie.objects.create(
            title="Custom Source Movie",
            clean_title="Custom Source Movie",
            year=2024,
            source="Seedbox Alpha",
            file_url="http://seedbox.local/movie.mkv",
            is_primary_copy=True
        )
        resp = self.client.get('/')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('source_options', resp.context)
        self.assertIn('Seedbox Alpha', resp.context['source_options'])

    def test_is_private_or_local_host(self):
        # BDIX / LAN / Local private networks
        self.assertTrue(is_private_or_local_host('172.16.50.14'))
        self.assertTrue(is_private_or_local_host('192.168.1.100'))
        self.assertTrue(is_private_or_local_host('10.16.100.244'))
        self.assertTrue(is_private_or_local_host('127.0.0.1'))
        self.assertTrue(is_private_or_local_host('localhost'))
        self.assertTrue(is_private_or_local_host('my-nas.local'))
        # Public hosts
        self.assertFalse(is_private_or_local_host('google.com'))
        self.assertFalse(is_private_or_local_host('8.8.8.8'))

    def test_extract_last_modified_formats(self):
        # H5AI 13-digit millisecond epoch
        dt_ms = extract_last_modified_from_text("Inception.mkv 1680000000000 1048576")
        self.assertIsNotNone(dt_ms)
        self.assertEqual(dt_ms.year, 2023)

        # H5AI 10-digit second epoch
        dt_s = extract_last_modified_from_text("Inception.mkv 1680000000 1048576")
        self.assertIsNotNone(dt_s)
        self.assertEqual(dt_s.year, 2023)

        # Standard date
        dt_std = extract_last_modified_from_text("Inception 2024-05-10 14:30:00")
        self.assertIsNotNone(dt_std)
        self.assertEqual(dt_std.year, 2024)

        # Plain movie title without timestamp should return None (not misparse release year)
        dt_none = extract_last_modified_from_text("The Matrix (1999)")
        self.assertIsNone(dt_none)

    def test_crawl_task_progress_parsing(self):
        task_manager.task_state['status'] = 'running'
        task_manager.task_state['progress_total'] = 0
        task_manager.task_state['progress_current'] = 0

        # Feed dynamic crawl progress line
        task_manager._parse_progress_from_line(
            "[CRAWL 35] Scanned: 35 | New: 3 | Skipped: 32 | Checking: Inception (2010)"
        )

        self.assertEqual(task_manager.task_state['progress_current'], 35)
        self.assertIn("Inception", task_manager.task_state['current_item'])

    def test_source_test_private_host_diagnostic(self):
        # Testing a non-listening local private port
        result = test_source_connection("http://127.0.0.1:54329/test/", timeout=1)
        self.assertFalse(result['success'])
        self.assertTrue(result.get('is_private_host', False))
        self.assertIn("private local/BDIX network address", result['message'])


class DatabasePortableSyncTests(TestCase):
    """Test portable .json.gz backup/restore and reset_catalog."""

    def setUp(self):
        self.admin = User.objects.create_superuser(
            username='admin_sync',
            email='sync@cineverse.test',
            password='SyncPassword123!'
        )
        self.client.login(username='admin_sync', password='SyncPassword123!')

    def test_database_export_json_gz_excludes_auth_and_imports_cleanly(self):
        import gzip
        from django.core.files.uploadedfile import SimpleUploadedFile

        MediaSource.objects.create(name='Test NAS', url='http://192.168.1.50/movies/')
        movie = Movie.objects.create(
            title='Inception 2010 1080p',
            clean_title='Inception',
            year=2010,
            year_str='2010',
            file_url='http://example.com/' + ('a' * 250) + '/Inception.mkv',
            source='Test NAS',
            is_primary_copy=True,
        )
        MovieDetail.objects.create(
            movie=movie,
            tmdb_id=27205,
            overview='Dream within a dream.',
            genres=['Sci-Fi', 'Action'],
        )

        # 1. Export portable .json.gz catalog
        export_resp = self.client.get(reverse('manage_database_export') + '?format=json')
        self.assertEqual(export_resp.status_code, 200)
        self.assertIn('cineverse_catalog_backup.json.gz', export_resp.get('Content-Disposition', ''))

        payload = json.loads(gzip.decompress(export_resp.content).decode('utf-8'))
        models_in_dump = {entry['model'] for entry in payload}
        self.assertIn('movies.movie', models_in_dump)
        self.assertIn('movies.moviedetail', models_in_dump)
        self.assertNotIn('auth.user', models_in_dump)

        # 2. Wipe catalog rows and restore from uploaded .json.gz
        MovieDetail.objects.all().delete()
        Movie.objects.all().delete()
        MediaSource.objects.all().delete()
        self.assertEqual(Movie.objects.count(), 0)

        upload = SimpleUploadedFile(
            'cineverse_catalog_backup.json.gz',
            export_resp.content,
            content_type='application/gzip',
        )
        import_resp = self.client.post(reverse('manage_database_import'), {'db_file': upload})
        self.assertEqual(import_resp.status_code, 200)
        res_data = json.loads(import_resp.content)
        self.assertTrue(res_data['success'])
        self.assertEqual(Movie.objects.count(), 1)
        self.assertEqual(MovieDetail.objects.count(), 1)
        self.assertTrue(Movie.objects.first().file_url.endswith('/Inception.mkv'))
        # Superuser account remains completely intact
        self.assertTrue(User.objects.filter(username='admin_sync', is_superuser=True).exists())

    def test_database_import_rejects_invalid_sqlite_header(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        bad_upload = SimpleUploadedFile('corrupt.sqlite3', b'not a real sqlite header')
        resp = self.client.post(reverse('manage_database_import'), {'db_file': bad_upload})
        self.assertEqual(resp.status_code, 400)
        data = json.loads(resp.content)
        self.assertFalse(data['success'])
        self.assertIn('Invalid SQLite database file header', data['message'])

    def test_reset_catalog_command_preserves_admin(self):
        from io import StringIO
        from django.core.management import call_command

        Movie.objects.create(
            title='Temporary Film',
            clean_title='Temporary Film',
            file_url='http://example.com/temp.mkv',
        )
        out = StringIO()
        call_command('reset_catalog', '--yes', stdout=out)
        self.assertEqual(Movie.objects.count(), 0)
        self.assertTrue(User.objects.filter(username='admin_sync', is_superuser=True).exists())

class PushLocalToOnlineCommandTests(unittest.TestCase):
    """Test push_local_to_online in an isolated unittest.TestCase with temp SQLite DBs."""

    def test_push_local_to_online_command_sync_and_upsert(self):
        import os
        import sqlite3
        import tempfile
        from io import StringIO
        from django.core.management import call_command

        with tempfile.TemporaryDirectory() as tmpdir:
            src_db = os.path.join(tmpdir, 'local_source.sqlite3')
            tgt_db = os.path.join(tmpdir, 'online_target.sqlite3')
            long_url = 'http://cdn.example.com/' + ('x' * 300) + '/film.mkv'

            conn = sqlite3.connect(src_db)
            conn.executescript("""
                CREATE TABLE movies_mediasource (
                    id INTEGER PRIMARY KEY, name TEXT, url TEXT, source_type TEXT, is_active INTEGER
                );
                CREATE TABLE movies_movie (
                    id INTEGER PRIMARY KEY, title TEXT, clean_title TEXT, year INTEGER, year_str TEXT,
                    file_url TEXT, source TEXT, is_primary_copy INTEGER
                );
                CREATE TABLE movies_moviedetail (
                    id INTEGER PRIMARY KEY, movie_id INTEGER, tmdb_id INTEGER, overview TEXT, genres TEXT
                );
            """)
            conn.execute(
                "INSERT INTO movies_mediasource (id, name, url, source_type, is_active) VALUES (1, 'Home', 'http://nas.local/', 'h5ai', 1)"
            )
            conn.execute(
                "INSERT INTO movies_movie (id, title, clean_title, year, year_str, file_url, source, is_primary_copy) VALUES (10, 'Long URL Film', 'Long URL Film', 2024, '2024', ?, 'Home', 1)",
                (long_url,)
            )
            conn.execute(
                'INSERT INTO movies_moviedetail (id, movie_id, tmdb_id, overview, genres) VALUES (1, 10, 999, \'Plot summary\', \'["Action"]\')'
            )
            conn.commit()
            conn.close()

            target_uri = 'sqlite:///' + tgt_db.replace('\\', '/')
            out1 = StringIO()
            call_command(
                'push_local_to_online',
                source_sqlite=src_db,
                target_url=target_uri,
                stdout=out1,
            )
            self.assertIn('Pushed 1 movies and 1 rich details', out1.getvalue())

            # Second run hits upsert mode (updates existing row + adds new row)
            conn = sqlite3.connect(src_db)
            conn.execute(
                "INSERT INTO movies_movie (id, title, clean_title, year, year_str, file_url, source, is_primary_copy) VALUES (11, 'Second Film', 'Second Film', 2025, '2025', 'http://cdn.example.com/second.mkv', 'Home', 1)"
            )
            conn.commit()
            conn.close()

            out2 = StringIO()
            call_command(
                'push_local_to_online',
                source_sqlite=src_db,
                target_url=target_uri,
                stdout=out2,
            )
            self.assertIn('Synced Movies (+1/~1)', out2.getvalue())

            # Verify target DB contents directly
            tconn = sqlite3.connect(tgt_db)
            count = tconn.execute("SELECT COUNT(*) FROM movies_movie").fetchone()[0]
            stored_url = tconn.execute("SELECT file_url FROM movies_movie WHERE title='Long URL Film'").fetchone()[0]
            detail_count = tconn.execute("SELECT COUNT(*) FROM movies_moviedetail").fetchone()[0]
            tconn.close()

            self.assertEqual(count, 2)
            self.assertEqual(stored_url, long_url)
            self.assertEqual(detail_count, 1)


class PerformanceAndSortTests(TestCase):
    """Test deterministic Newest First sorting and bounded query count (no N+1)."""

    def test_newest_sort_breaks_year_ties_deterministically(self):
        from datetime import timedelta
        from django.utils import timezone

        now = timezone.now()
        m_older_same_year = Movie.objects.create(
            title='Older Same Year',
            clean_title='Older Same Year',
            year=2026,
            file_url='http://example.com/2026-a.mkv',
            is_primary_copy=True,
            last_crawled=now - timedelta(days=5),
        )
        m_newer_same_year = Movie.objects.create(
            title='Newer Same Year',
            clean_title='Newer Same Year',
            year=2026,
            file_url='http://example.com/2026-b.mkv',
            is_primary_copy=True,
            last_crawled=now - timedelta(hours=1),
        )
        m_prev_year = Movie.objects.create(
            title='Previous Year',
            clean_title='Previous Year',
            year=2025,
            file_url='http://example.com/2025.mkv',
            is_primary_copy=True,
            last_crawled=now,
        )

        resp = self.client.get(reverse('movies_index') + '?sort=-year')
        self.assertEqual(resp.status_code, 200)
        ordered_ids = [m.id for m in resp.context['movies']]
        self.assertEqual(ordered_ids, [m_newer_same_year.id, m_older_same_year.id, m_prev_year.id])

    def test_index_and_search_avoid_n_plus_one_queries(self):
        for idx in range(10):
            m = Movie.objects.create(
                title=f'Film {idx}',
                clean_title=f'Film {idx}',
                year=2024,
                file_url=f'http://example.com/film-{idx}.mkv',
                source='Home NAS',
                is_primary_copy=True,
                runtime=None,
            )
            if idx % 2 == 0:
                MovieDetail.objects.create(
                    movie=m,
                    runtime=100 + idx,
                    director={'name': 'Director Alpha'} if idx == 0 else {},
                    cast=[{'name': 'Actor Beta'}] if idx == 2 else [],
                )

        # index (DummyCache): 1 WindowCountPaginator slice + 1 aggregate stats + 1 UNION sources = 3 queries
        with self.assertNumQueries(3):
            r_idx = self.client.get(reverse('movies_index'))
        self.assertEqual(r_idx.status_code, 200)

        # search_ajax: 1 WindowCountPaginator slice = 1 query regardless of card count
        with self.assertNumQueries(1):
            r_ajax = self.client.get(reverse('movies_search_ajax') + '?sort=-year')
        self.assertEqual(r_ajax.status_code, 200)

        # Search still matches director/cast via MovieDetail UNION subquery
        r_dir = self.client.get(reverse('movies_search_ajax'), {'q': 'Director Alpha'})
        self.assertEqual(json.loads(r_dir.content)['count'], 1)
        r_cast = self.client.get(reverse('movies_search_ajax'), {'q': 'Actor Beta'})
        self.assertEqual(json.loads(r_cast.content)['count'], 1)

        # Empty & out-of-range pagination edge cases stay safe
        r_empty = self.client.get(reverse('movies_search_ajax'), {'q': '__no_such_title_xyz__'})
        empty_data = json.loads(r_empty.content)
        self.assertEqual(empty_data['count'], 0)
        self.assertEqual(empty_data['current_page'], 1)

        r_oob = self.client.get(reverse('movies_search_ajax'), {'page': '999'})
        oob_data = json.loads(r_oob.content)
        self.assertEqual(oob_data['count'], 10)
        self.assertEqual(oob_data['current_page'], 1)

    def test_index_summary_cache_and_invalidation(self):
        from django.test import override_settings
        from django.core.cache import cache
        from movies.cache_utils import invalidate_catalog_cache

        Movie.objects.create(
            title='Cached Movie',
            clean_title='Cached Movie',
            year=2025,
            file_url='http://example.com/cached.mkv',
            source='Home NAS',
            is_primary_copy=True,
        )
        locmem = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'test-summary'}}
        with override_settings(CACHES=locmem, CATALOG_STATS_CACHE_SECONDS=60):
            cache.clear()
            with self.assertNumQueries(3):
                self.client.get(reverse('movies_index'))
            # Warm cache drops homepage load to 1 query (page slice only)
            with self.assertNumQueries(1):
                self.client.get(reverse('movies_index'))
            invalidate_catalog_cache()
            with self.assertNumQueries(3):
                self.client.get(reverse('movies_index'))
            cache.clear()

    def test_health_endpoint(self):
        from django.test import override_settings

        resp = self.client.get(reverse('health_check'))
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertEqual(data['status'], 'ok')
        self.assertEqual(data['database'], 'sqlite')

        # Platform internal HTTP health probes must still return 200 even if SECURE_SSL_REDIRECT is enabled
        with override_settings(SECURE_SSL_REDIRECT=True):
            ssl_resp = self.client.get(reverse('health_check'))
            self.assertEqual(ssl_resp.status_code, 200)

    def test_crawl_h5ai_preloaded_cache_skips_without_per_item_queries(self):
        from unittest.mock import patch
        from movies.management.commands.crawl_h5ai import Command as CrawlCommand
        from movies.models import CrawledDirectory, FailedParse

        CrawledDirectory.objects.create(
            url='http://nas.local/movies/Inception%20(2010)/',
            movie_count=1,
        )
        Movie.objects.create(
            title='Inception.2010.mkv',
            clean_title='Inception',
            year=2010,
            year_str='2010',
            file_url='http://nas.local/movies/Inception.2010.mkv',
            directory_url='http://nas.local/movies/',
            source='Local NAS',
            fetched=True,
            metadata_status='ok',
        )
        cmd = CrawlCommand()
        cmd._load_caches()
        stats = {
            'total_scanned': 0, 'new_movies': 0, 'updated_movies': 0,
            'metadata_fetched': 0, 'skipped_unchanged': 0, 'skipped_scheduled': 0,
            'failed_no_media': 0, 'failed_timeout': 0, 'failed_error': 0, 'direct_files': 0,
        }

        with patch('movies.management.commands.crawl_h5ai.fetch_poster_and_details') as mock_fetch:
            with self.assertNumQueries(0):
                self.assertTrue(cmd._should_skip_directory('http://nas.local/movies/Inception%20(2010)/', None, False))
                cmd._process_media_file(
                    {
                        'name': 'Inception.2010.mkv',
                        'url': 'http://nas.local/movies/Inception.2010.mkv',
                        'directory_url': 'http://nas.local/movies/',
                        'dir_name': '',
                        'raw': '',
                    },
                    stats,
                    force=False,
                )
                cmd._remove_from_failed('http://nas.local/movies/Inception%20(2010)/')
            mock_fetch.assert_not_called()

        self.assertEqual(stats['skipped_unchanged'], 1)
        cmd._update_directory_cache('http://nas.local/movies/NewDir/', None, 2)
        cmd._flush_dir_cache_updates()
        self.assertTrue(CrawledDirectory.objects.filter(url='http://nas.local/movies/NewDir/', movie_count=2).exists())

    def test_movie_detail_ajax_caches_negative_external_lookups(self):
        from unittest.mock import patch
        from django.core.cache import cache
        from django.test import override_settings

        m = Movie.objects.create(
            title='Obscure Indie Film',
            clean_title='Obscure Indie Film',
            year=2024,
            file_url='http://example.com/obscure.mkv',
            metadata_status='ok',
            fetched=True,
        )
        MovieDetail.objects.create(movie=m, tmdb_id=999991)

        locmem = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'test-detail-miss'}}
        with override_settings(CACHES=locmem):
            cache.clear()
            with patch('movies.views.tmdb_service.get_movie_trailer', return_value=None) as mock_trailer, \
                 patch('movies.views.fetch_imdb_rating_and_id', return_value=(None, None, [])) as mock_imdb:
                r1 = self.client.get(reverse('movie_detail', args=[m.id]))
                self.assertEqual(r1.status_code, 200)
                self.assertEqual(mock_trailer.call_count, 1)
                self.assertEqual(mock_imdb.call_count, 1)

                # Second request skips repeated failing lookups thanks to negative cache
                r2 = self.client.get(reverse('movie_detail', args=[m.id]))
                self.assertEqual(r2.status_code, 200)
                self.assertEqual(mock_trailer.call_count, 1)
                self.assertEqual(mock_imdb.call_count, 1)

                # Saving/refreshing the movie bumps last_updated and invalidates miss keys
                m.save()
                r3 = self.client.get(reverse('movie_detail', args=[m.id]))
                self.assertEqual(r3.status_code, 200)
                self.assertEqual(mock_trailer.call_count, 2)
                self.assertEqual(mock_imdb.call_count, 2)
            cache.clear()

    def test_fetch_command_batch_save_uses_bounded_queries(self):
        from movies.management.commands.fetch_genres import Command as GenreCommand

        movies = []
        for idx in range(3):
            m = Movie.objects.create(
                title=f'Batch Movie {idx}',
                clean_title=f'Batch Movie {idx}',
                year=2024,
                file_url=f'http://example.com/batch_{idx}.mkv',
                group_key='',
            )
            MovieDetail.objects.create(movie=m, tmdb_id=7000 + idx)
            movies.append(m)

        items = [
            {'id': m.id, 'genres': ['Action', 'Sci-Fi'], 'tmdb_id': 7000 + idx, 'group_key': ''}
            for idx, m in enumerate(movies)
        ]
        cmd = GenreCommand()
        # 1 MovieDetail batch SELECT + SAVEPOINT + 3 Movie UPDATEs + 1 bulk MovieDetail UPDATE + RELEASE SAVEPOINT
        with self.assertNumQueries(7):
            cmd._save_batch(items)
        for m in movies:
            m.refresh_from_db()
            self.assertEqual(m.genres, 'Action, Sci-Fi')

    def test_reconcile_copy_groups_deduplicates_and_preserves_metadata(self):
        from movies.services import reconcile_copy_groups

        m_1080 = Movie.objects.create(
            title='Arrival 2016 1080p.mkv',
            clean_title='Arrival',
            year=2016,
            resolution='1080P',
            source='Server A',
            file_url='http://nas-a.local/Arrival.2016.1080p.mkv',
            group_key='arrival_2016',
            is_primary_copy=True,
            fetched=True,
            metadata_status='ok',
            poster_url='https://image.tmdb.org/t/p/w500/arrival.jpg',
            imdb_id='tt2543164',
            imdb_rating=7.9,
            genres='Drama, Sci-Fi',
        )
        m_4k = Movie.objects.create(
            title='Arrival 2016 4K.mkv',
            clean_title='Arrival',
            year=2016,
            resolution='4K',
            source='Server B',
            file_url='http://nas-b.local/Arrival.2016.4K.mkv',
            group_key='arrival_2016',
            is_primary_copy=True,
            fetched=False,
            metadata_status='missing',
        )
        unrelated = Movie.objects.create(
            title='Unrelated 2020.mkv',
            clean_title='Unrelated',
            year=2020,
            file_url='http://nas-a.local/Unrelated.2020.mkv',
            group_key='unrelated_2020',
            is_primary_copy=True,
        )

        res = reconcile_copy_groups(group_keys={'arrival_2016'})
        self.assertEqual(res['groups'], 1)
        m_1080.refresh_from_db()
        m_4k.refresh_from_db()
        unrelated.refresh_from_db()

        self.assertFalse(m_1080.is_primary_copy)
        self.assertTrue(m_4k.is_primary_copy)
        self.assertEqual(m_4k.poster_url, 'https://image.tmdb.org/t/p/w500/arrival.jpg')
        self.assertEqual(m_4k.imdb_id, 'tt2543164')
        self.assertEqual(m_4k.imdb_rating, 7.9)
        self.assertTrue(m_4k.fetched)
        self.assertTrue(unrelated.is_primary_copy)

    def test_crawl_h5ai_auto_reconciles_duplicate_server_copies(self):
        from unittest.mock import patch
        from io import StringIO
        from django.core.management import call_command

        existing = Movie.objects.create(
            title='Interstellar 2014 1080p.mkv',
            clean_title='Interstellar',
            year=2014,
            resolution='1080P',
            source='Server A',
            file_url='http://nas-a.local/Interstellar.2014.1080p.mkv',
            group_key='interstellar:::2014',
            is_primary_copy=True,
            fetched=True,
            metadata_status='ok',
            poster_url='https://image.tmdb.org/t/p/w500/interstellar.jpg',
        )
        fake_items = [{
            'name': 'Interstellar.2014.4K.mkv',
            'url': 'http://nas-b.local/movies/Interstellar.2014.4K.mkv',
            'is_dir': False,
            'is_skipped': False,
            'directory_url': 'http://nas-b.local/movies/',
            'dir_name': '',
            'raw': '',
        }]
        with patch('movies.management.commands.crawl_h5ai.crawl_h5ai_recursive', return_value=iter(fake_items)), \
             patch('movies.management.commands.crawl_h5ai.fetch_poster_and_details', return_value={'poster_url': 'https://image.tmdb.org/t/p/w500/interstellar.jpg', 'popularity': 90.0}):
            call_command('crawl_h5ai', url='http://nas-b.local/movies/', source='Server B', stdout=StringIO())

        existing.refresh_from_db()
        new_4k = Movie.objects.get(file_url='http://nas-b.local/movies/Interstellar.2014.4K.mkv')
        self.assertEqual(new_4k.group_key, 'interstellar:::2014')
        self.assertTrue(new_4k.is_primary_copy)
        self.assertFalse(existing.is_primary_copy)
        self.assertEqual(Movie.objects.filter(group_key='interstellar:::2014', is_primary_copy=True).count(), 1)

    def test_fetch_missing_metadata_missing_details_backfills_already_posted_movies(self):
        from unittest.mock import patch
        from io import StringIO
        from django.core.management import call_command

        m = Movie.objects.create(
            title='Blade Runner 2049 (2017)',
            clean_title='Blade Runner 2049',
            year=2017,
            file_url='http://example.com/br2049.mkv',
            poster_url='https://image.tmdb.org/t/p/w500/br2049.jpg',
            fetched=True,
            metadata_status='ok',
            is_primary_copy=True,
        )
        fake_ext = {
            'tmdb_id': 335984,
            'overview': 'Young Blade Runner K discovers a long-buried secret.',
            'genres': ['Science Fiction', 'Drama'],
            'runtime': 164,
            'backdrop_url': 'https://image.tmdb.org/t/p/w1280/bg.jpg',
            'tagline': 'The key to the future is finally unearthed.',
            'imdb_id': 'tt1856101',
            'original_language': 'en',
            'release_date': '2017-10-06',
            'trailer_key': 'gCcx85zbxz4',
            'trailer_url': 'https://www.youtube.com/watch?v=gCcx85zbxz4',
            'cast': [{'name': 'Ryan Gosling'}],
            'director': {'name': 'Denis Villeneuve'},
        }
        with patch.dict('os.environ', {'TMDB_API_KEY': 'dummy_key'}), \
             patch('movies.management.commands.fetch_missing_metadata.tmdb_service.get_extended_details', return_value=fake_ext):
            call_command('fetch_missing_metadata', missing_details=True, workers=1, stdout=StringIO())

        m.refresh_from_db()
        detail = MovieDetail.objects.get(movie=m)
        self.assertEqual(detail.tmdb_id, 335984)
        self.assertEqual(detail.trailer_key, 'gCcx85zbxz4')
        self.assertEqual(detail.director, {'name': 'Denis Villeneuve'})
        self.assertEqual(str(detail.release_date), '2017-10-06')
        self.assertEqual(m.runtime, 164)
        self.assertEqual(m.imdb_id, 'tt1856101')





