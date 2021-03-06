import time

from django.contrib.auth import get_user_model
from django.db import transaction, models
from django.db.models import Prefetch, Case, When, Value, Count, Subquery, Q
from django.utils import timezone
from rest_framework import permissions
from rest_framework.generics import get_object_or_404
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.status import HTTP_403_FORBIDDEN, HTTP_400_BAD_REQUEST
from rest_framework.views import APIView
from rest_framework.viewsets import ModelViewSet

from backend.permissions import AdminOrReadOnly, IsBot, ReadOnlyBot
from backend.response import FormattedResponse
from backend.signals import flag_submit, flag_reject, flag_score
from backend.viewsets import AdminCreateModelViewSet
from challenge.models import Challenge, Category, Solve, File, ChallengeVote, ChallengeFeedback, Tag
from challenge.permissions import CompetitionOpen
from challenge.serializers import ChallengeSerializer, CategorySerializer, AdminCategorySerializer, \
    AdminChallengeSerializer, FileSerializer, CreateCategorySerializer, CreateChallengeSerializer, \
    ChallengeFeedbackSerializer, TagSerializer
from config import config
from hint.models import Hint, HintUse
from plugins import plugins
from team.models import Team
from team.permissions import HasTeam


class CategoryViewset(AdminCreateModelViewSet):
    queryset = Category.objects.all()
    permission_classes = (CompetitionOpen & AdminOrReadOnly,)
    throttle_scope = 'challenges'
    pagination_class = None
    serializer_class = CategorySerializer
    admin_serializer_class = AdminCategorySerializer
    create_serializer_class = CreateCategorySerializer

    def get_queryset(self):
        if self.request.user.is_staff and self.request.user.should_deny_admin():
            return Category.objects.none()
        team = self.request.user.team
        if team is not None:
            solves = Solve.objects.filter(team=team, correct=True)
            solved_challenges = solves.values_list('challenge')
            challenges = Challenge.objects.prefetch_related('unlocked_by').annotate(
                unlocked=Case(
                    When(auto_unlock=True, then=Value(True)),
                    When(Q(unlocked_by__in=Subquery(solved_challenges)), then=Value(True)),
                    default=Value(False),
                    output_field=models.BooleanField()
                ),
                solved=Case(
                    When(Q(id__in=Subquery(solved_challenges)), then=Value(True)),
                    default=Value(False),
                    output_field=models.BooleanField()
                ),
                solve_count=Count('solves', filter=Q(solves__correct=True)),
                unlock_time_surpassed=Case(
                    When(release_time__lte=timezone.now(), then=Value(True)),
                    default=Value(False),
                    output_field=models.BooleanField(),
                )
            )
        else:
            challenges = (
                Challenge.objects.filter(release_time__lte=timezone.now()).annotate(
                    unlocked=Case(
                        When(auto_unlock=True, then=Value(True)),
                        default=Value(False),
                        output_field=models.BooleanField()
                    ),
                    solved=Value(False, models.BooleanField()),
                    solve_count=Count('solves'),
                    unlock_time_surpassed=Case(
                        When(release_time__lte=timezone.now(), then=Value(True)),
                        default=Value(False),
                        output_field=models.BooleanField(),
                    )
                )
            )
        x = challenges.prefetch_related(
            Prefetch('hint_set', queryset=Hint.objects.annotate(
                used=Case(
                    When(id__in=HintUse.objects.filter(team=team).values_list('hint_id'), then=Value(True)),
                    default=Value(False),
                    output_field=models.BooleanField()
                )), to_attr='hints'),
            Prefetch('file_set', queryset=File.objects.all(), to_attr='files'),
            Prefetch('tag_set',
                     queryset=Tag.objects.all() if time.time() > config.get('end_time') else Tag.objects.filter(
                         post_competition=False), to_attr='tags'),
            'unlocks', 'first_blood', 'hint_set__uses')
        if self.request.user.is_staff:
            categories = Category.objects
        else:
            categories = Category.objects.filter(release_time__lte=timezone.now())
        qs = categories.prefetch_related(
            Prefetch('category_challenges', queryset=x, to_attr='challenges')
        )
        return qs

    def list(self, request, *args, **kwargs):
        # This is to fix an issue with django duplicating challenges on .annotate.
        # If you want to clean this up, good luck.
        categories = super(CategoryViewset, self).list(request, *args, **kwargs).data
        for category in categories:
            unlocked = set()
            for challenge in category['challenges']:
                if 'unlocked' in challenge and challenge['unlocked']:
                    unlocked.add(challenge['id'])
            new_challenges = []
            for challenge in category['challenges']:
                if not (('unlocked' not in challenge or not challenge['unlocked']) and challenge['id'] in unlocked):
                    new_challenges.append(challenge)
            category['challenges'] = new_challenges
        return FormattedResponse(categories)


class ChallengeViewset(AdminCreateModelViewSet):
    queryset = Challenge.objects.all()
    permission_classes = (CompetitionOpen & AdminOrReadOnly,)
    throttle_scope = 'challenges'
    pagination_class = None
    serializer_class = ChallengeSerializer
    admin_serializer_class = AdminChallengeSerializer
    create_serializer_class = CreateChallengeSerializer

    def get_queryset(self):
        if self.request.method not in permissions.SAFE_METHODS:
            return self.queryset
        return Challenge.get_unlocked_annotated_queryset(self.request.user)


class ChallengeFeedbackView(APIView):
    permission_classes = (IsAuthenticated & HasTeam & ReadOnlyBot,)

    def get(self, request):
        challenge = get_object_or_404(Challenge, id=request.data.get("challenge"))
        feedback = ChallengeFeedback.objects.filter(challenge=challenge)
        if request.user.is_staff:
            return FormattedResponse(ChallengeFeedbackSerializer(feedback, many=True).data)
        return FormattedResponse(ChallengeFeedbackSerializer(feedback.filter(user=request.user).first()).data)

    def post(self, request):
        challenge = get_object_or_404(Challenge, id=request.data.get('challenge'))
        solve_set = Solve.objects.filter(challenge=challenge)

        if not solve_set.filter(team=request.user.team, correct=True).exists():
            return FormattedResponse(m='challenge_not_solved', status=HTTP_403_FORBIDDEN)

        current_feedback = ChallengeFeedback.objects.filter(user=request.user, challenge=challenge)
        if current_feedback.exists():
            current_feedback.delete()

        ChallengeFeedback(user=request.user, challenge=challenge, feedback=request.data.get("feedback")).save()
        return FormattedResponse(m='feedback_recorded')


class ChallengeVoteView(APIView):
    permission_classes = (IsAuthenticated & HasTeam & ~IsBot,)

    def post(self, request):
        challenge = get_object_or_404(Challenge, id=request.data.get('challenge'))
        solve_set = Solve.objects.filter(challenge=challenge)

        if not solve_set.filter(team=request.user.team, correct=True).exists():
            return FormattedResponse(m='challenge_not_solved', status=HTTP_403_FORBIDDEN)

        current_vote = ChallengeVote.objects.filter(user=request.user, challenge=challenge)
        if current_vote.exists():
            current_vote.delete()

        ChallengeVote(user=request.user, challenge=challenge, positive=request.data.get("positive")).save()
        return FormattedResponse(m='vote_recorded')


class FlagSubmitView(APIView):
    permission_classes = (CompetitionOpen & IsAuthenticated & HasTeam & ~IsBot,)
    throttle_scope = 'flag_submit'

    def post(self, request):
        if not config.get('enable_flag_submission') or \
                (not config.get('enable_flag_submission_after_competition') and time.time() > config.get('end_time')):
            return FormattedResponse(m='flag_submission_disabled', status=HTTP_403_FORBIDDEN)

        with transaction.atomic():
            team = Team.objects.select_for_update().get(id=request.user.team.id)
            user = get_user_model().objects.select_for_update().get(id=request.user.id)
            flag = request.data.get('flag')
            challenge_id = request.data.get('challenge')
            if not flag or not challenge_id:
                return FormattedResponse(status=HTTP_400_BAD_REQUEST)

            challenge = get_object_or_404(Challenge.objects.select_for_update(), id=challenge_id)
            solve_set = Solve.objects.filter(challenge=challenge)
            if solve_set.filter(team=team, correct=True).exists() \
                    or not challenge.is_unlocked(user):
                return FormattedResponse(m='already_solved_challenge', status=HTTP_403_FORBIDDEN)

            if challenge.challenge_metadata.get("attempt_limit"):
                count = solve_set.filter(team=team).count()
                if count > challenge.challenge_metadata['attempt_limit']:
                    flag_reject.send(sender=self.__class__, user=user, team=team, challenge=challenge, flag=flag,
                                     reason='attempt_limit_reached')
                    return FormattedResponse(d={'correct': False}, m='attempt_limit_reached')

            flag_submit.send(sender=self.__class__, user=user, team=team, challenge=challenge, flag=flag)
            plugin = plugins.plugins['flag'][challenge.flag_type](challenge)
            points_plugin = plugins.plugins['points'][challenge.points_type](challenge)

            if not plugin.check(flag, user=user, team=team):
                flag_reject.send(sender=self.__class__, user=user, team=team,
                                 challenge=challenge, flag=flag, reason='incorrect_flag')
                points_plugin.register_incorrect_attempt(user, team, flag, solve_set)
                return FormattedResponse(d={'correct': False}, m='incorrect_flag')

            solve = points_plugin.score(user, team, flag, solve_set)
            if challenge.first_blood is None:
                challenge.first_blood = user
                challenge.save()

            user.save()
            team.save()
            flag_score.send(sender=self.__class__, user=user, team=team, challenge=challenge, flag=flag, solve=solve)
            ret = {'correct': True}
            if challenge.post_score_explanation:
                ret["explanation"] = challenge.post_score_explanation
            return FormattedResponse(d=ret, m='correct_flag')


class FlagCheckView(APIView):
    permission_classes = (CompetitionOpen & IsAuthenticated & HasTeam & ~IsBot,)
    throttle_scope = 'flag_submit'

    def post(self, request):
        if not config.get('enable_flag_submission') or \
                (not config.get('enable_flag_submission_after_competition') and time.time() > config.get('end_time')):
            return FormattedResponse(m='flag_submission_disabled', status=HTTP_403_FORBIDDEN)
        team = Team.objects.get(id=request.user.team.id)
        user = get_user_model().objects.get(id=request.user.id)
        flag = request.data.get('flag')
        challenge_id = request.data.get('challenge')
        if not flag or not challenge_id:
            return FormattedResponse(status=HTTP_400_BAD_REQUEST)

        challenge = get_object_or_404(Challenge.objects.select_for_update(), id=challenge_id)
        solve_set = Solve.objects.filter(challenge=challenge)
        if not solve_set.filter(team=team, correct=True).exists():
            return FormattedResponse(m='havent_solved_challenge', status=HTTP_403_FORBIDDEN)

        plugin = plugins.plugins['flag'][challenge.flag_type](challenge)

        if not plugin.check(flag, user=user, team=team):
            return FormattedResponse(d={'correct': False}, m='incorrect_flag')

        ret = {'correct': True}
        if challenge.post_score_explanation:
            ret["explanation"] = challenge.post_score_explanation
        return FormattedResponse(d=ret, m='correct_flag')


class FileViewSet(ModelViewSet):
    queryset = File.objects.all()
    permission_classes = (IsAdminUser,)
    throttle_scope = 'file'
    serializer_class = FileSerializer
    pagination_class = None


class TagViewSet(ModelViewSet):
    queryset = Tag.objects.all()
    permission_classes = (IsAdminUser,)
    throttle_scope = 'tag'
    serializer_class = TagSerializer
    pagination_class = None
