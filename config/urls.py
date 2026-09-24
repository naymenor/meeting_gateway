from django.contrib import admin
from django.urls import path
from common.health import live, ready

urlpatterns = [
    path("admin/", admin.site.urls),
    path("health/live/", live),
    path("health/ready/", ready),
]

from apps.meetings.views import (
    MeetingListView,
    MeetingDetailView,
    MeetingCreateView,
    MeetingTokenView,
    MeetingCancelView,
    AvailabilityView,
)
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from apps.integrations.views import TokenView

urlpatterns += [
    path("api/v1/auth/token/", TokenView.as_view()),
    path("api/v1/meetings/", MeetingListView.as_view()),
    path("api/v1/meetings/<uuid:pk>/", MeetingDetailView.as_view()),
    path("api/v1/meetings/<uuid:pk>/create/", MeetingCreateView.as_view()),
    path("api/v1/meetings/<uuid:pk>/convay-token/", MeetingTokenView.as_view()),
    path("api/v1/meetings/<uuid:pk>/cancel/", MeetingCancelView.as_view()),
    path("api/v1/rooms/availability/", AvailabilityView.as_view()),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path(
        "api/docs/",
        SpectacularSwaggerView.as_view(url_name="schema"),
        name="swagger-ui",
    ),
]
