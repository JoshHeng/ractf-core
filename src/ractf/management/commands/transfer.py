from django.contrib.auth import get_user_model
from django.core.management import BaseCommand

from team.models import Team


class Command(BaseCommand):
    help = "Transfer team owner"

    def add_arguments(self, parser):
        parser.add_argument('user_id', type=int)
        parser.add_argument('team_id', type=int)

    def handle(self, *args, **options):
        user = get_user_model().objects.get(pk=options['user_id'])
        team = Team.objects.get(pk=options['team_id'])
        team.owner = user
        team.save()
