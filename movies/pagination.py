"""Pagination helpers optimized for remote databases (e.g. Supabase over the network).

Django's default ``Paginator`` issues two queries per page: ``SELECT COUNT(*)`` and
then the page ``SELECT``. Against a remote Postgres every query costs a full network
round trip (often 100-250 ms), so this paginator folds the total count into the page
query with a ``COUNT(*) OVER ()`` window function and only falls back to a separate
``COUNT(*)`` when the requested page is out of range or empty.
"""

from django.core.paginator import Page, Paginator
from django.db.models import Count, Window

_TOTAL_ATTR = '_window_total_count'


class WindowCountPaginator(Paginator):
    """Paginator that fetches the page rows and the total row count in one query."""

    def _fetch_rows(self, number: int) -> list:
        bottom = (number - 1) * self.per_page
        top = bottom + self.per_page
        annotated = self.object_list.annotate(**{_TOTAL_ATTR: Window(expression=Count('*'))})
        return list(annotated[bottom:top])

    def get_page(self, number) -> Page:
        try:
            number = int(number)
        except (TypeError, ValueError):
            number = 1
        if number < 1:
            number = 1

        rows = self._fetch_rows(number)
        if rows:
            self.__dict__['count'] = getattr(rows[0], _TOTAL_ATTR)
            return Page(rows, number, self)

        # Empty result: either the query matches nothing or the page is past the end.
        total = self.object_list.count()
        self.__dict__['count'] = total
        if total == 0:
            return Page([], 1, self)

        last_page = self.num_pages
        rows = self._fetch_rows(last_page)
        return Page(rows, last_page, self)
