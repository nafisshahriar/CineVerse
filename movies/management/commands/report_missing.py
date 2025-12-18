from django.core.management.base import BaseCommand
from movies.models import Movie

class Command(BaseCommand):
    help = 'Report movies with missing or failed metadata'

    def handle(self, *args, **options):
        qs = Movie.objects.filter(metadata_status__in=['missing','failed']).order_by('title')
        if not qs.exists():
            self.stdout.write(self.style.SUCCESS('No missing metadata'))
            return
        for m in qs:
            self.stdout.write(f"{m.title} ({m.year}) - status={m.metadata_status} next_crawl={m.next_crawl}")
