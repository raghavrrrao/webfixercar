"""Organiser-facing admin: judge the entries, then delete them deliberately.

Nothing here can change a submission. The three inspection views render a
participant's entry read-only, and the participant's own design is drawn in a
sandboxed iframe so their CSS cannot restyle the admin around it.
"""

from django.contrib import admin, messages
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html, format_html_join
from django.views.decorators.clickjacking import xframe_options_sameorigin

from .checks import HINT_LEVELS, OBJECTIVE_TITLES, TOTAL_CHECKS
from .game_config import RACE_MAX_SCORE
from .models import CssRule, FinalSubmission, HintReveal, User
from .repairs import REPAIRS, REPAIRS_BY_ID
from .views import _render_novacloud, finalize_all_due, race_section


def _score_label(score, total):
    return f'{score}/{total}'


class HintRevealInline(admin.TabularInline):
    model = HintReveal
    extra = 0
    can_delete = False
    fields = ('objective', 'level', 'revealed_at')
    readonly_fields = fields
    ordering = ('revealed_at',)

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(User)
class ParticipantAdmin(admin.ModelAdmin):
    """The people at the PCs. Deleting one here removes all their data."""

    list_display = (
        'pc_no', 'username', 'status_display', 'score_display', 'race_time_display',
        'race_obstacles', 'race_collisions', 'distance_display', 'section_display',
        'race_started_at', 'race_completed_at', 'eligible_display', 'submitted_display',
    )
    list_filter = ('is_admin', 'race_completed_at', 'race_started_at')
    search_fields = ('pc_no', 'username')
    ordering = ('pc_no',)
    inlines = (HintRevealInline,)

    readonly_fields = (
        'pc_no', 'username', 'registered_at', 'game_start_time', 'completed_at',
        'best_score', 'race_started_at', 'race_completed_at', 'race_time_seconds',
        'race_time_display', 'race_obstacles', 'race_collisions', 'last_login',
        'score_display', 'status_display', 'expired_display', 'repairs_display',
        'distance_display', 'section_display',
        'eligible_display', 'hints_display', 'submitted_display', 'judging_links',
    )
    # `password` is deliberately absent: organisers never need a participant's
    # credentials, so the hash is not rendered anywhere in this admin.
    fields = (
        'pc_no', 'username', 'registered_at', 'status_display', 'expired_display',
        'race_started_at', 'race_completed_at', 'race_time_display',
        'race_obstacles', 'repairs_display', 'race_collisions',
        'distance_display', 'section_display', 'score_display',
        'eligible_display', 'submitted_display', 'judging_links',
        'game_start_time', 'completed_at', 'is_admin', 'is_active',
    )

    @admin.display(description='Score', ordering='best_score')
    def score_display(self, obj):
        return _score_label(obj.best_score, RACE_MAX_SCORE)

    @admin.display(description='Timed out', boolean=True)
    def expired_display(self, obj):
        return obj.is_expired

    @admin.display(description='CSS repairs')
    def repairs_display(self, obj):
        collected = obj.repair_ids
        if not collected:
            return '—'
        return format_html_join(
            ' ', '<code>{}</code>',
            ((REPAIRS_BY_ID[repair]['label'],) for repair in collected),
        )

    @admin.display(description='Distance', ordering='race_distance')
    def distance_display(self, obj):
        if not obj.race_distance:
            return '—'
        return f'{obj.race_distance / 1000:.1f} km'

    @admin.display(description='Section')
    def section_display(self, obj):
        if not obj.race_started_at:
            return '—'
        return REPAIRS[race_section(obj.race_distance)]['section']


    @admin.display(description='Race time', ordering='race_time_seconds')
    def race_time_display(self, obj):
        if not obj.race_time_seconds:
            return '—'
        return f'{obj.race_time_seconds // 60:02d}:{obj.race_time_seconds % 60:02d}'
    @admin.display(description='Status')
    def status_display(self, obj):
        return {
            'not_started': 'Not started',
            'active': 'Racing',
            'completed': 'Finished',
            'expired': 'Time up',
        }[obj.race_status]

    @admin.display(description='Eligible', boolean=True)
    def eligible_display(self, obj):
        return obj.is_eligible

    @admin.display(description='Hints')
    def hints_display(self, obj):
        return obj.hints_used

    @admin.display(description='Submitted')
    def submitted_display(self, obj):
        final = getattr(obj, 'final_submission', None)
        return final.submitted_at if final else '—'

    @admin.display(description='Judging')
    def judging_links(self, obj):
        final = getattr(obj, 'final_submission', None)
        if not final:
            return 'No submission yet — the round is still open.'
        return _judging_links(final.pk)

    def get_queryset(self, request):
        # Settle anyone whose clock ran out while nobody was watching, so the
        # organiser's list is always complete without a cron job.
        finalize_all_due()
        return super().get_queryset(request)

    def has_add_permission(self, request):
        return False


def _judging_links(pk):
    return format_html(
        '<a class="button" href="{}">View final design</a>&nbsp;'
        '<a class="button" href="{}">View CSS</a>&nbsp;'
        '<a class="button" href="{}">View hint usage</a>',
        reverse('admin:first_finalsubmission_design', args=[pk]),
        reverse('admin:first_finalsubmission_css', args=[pk]),
        reverse('admin:first_finalsubmission_hints', args=[pk]),
    )


@admin.register(FinalSubmission)
class FinalSubmissionAdmin(admin.ModelAdmin):
    """The judging table: one immutable row per finished round."""

    list_display = (
        'pc_no', 'score_display', 'status', 'eligible', 'hints_used',
        'objectives_hinted', 'submitted_at', 'judging_links',
    )
    list_filter = ('eligible', 'reached_all', 'design_mode')
    search_fields = ('pc_no', 'user__username')
    date_hierarchy = 'submitted_at'
    ordering = ('-eligible', '-score', 'submitted_at')

    # The entry is a record of what happened; nothing about it is editable.
    readonly_fields = (
        'user', 'pc_no', 'started_at', 'submitted_at', 'score_display',
        'reached_all', 'design_mode', 'eligible', 'hints_used',
        'objectives_hinted', 'judging_links',
    )
    fields = readonly_fields
    actions = ('delete_participants',)

    @admin.display(description='Score', ordering='score')
    def score_display(self, obj):
        return _score_label(obj.score, obj.total)

    @admin.display(description='Judging')
    def judging_links(self, obj):
        return _judging_links(obj.pk)

    def get_queryset(self, request):
        finalize_all_due()
        return super().get_queryset(request)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        # Viewable, never editable: `final_css` is the competition entry.
        return False

    # -- inspection views --------------------------------------------------

    def get_urls(self):
        wrap = self.admin_site.admin_view
        return [
            path('<int:pk>/design/', wrap(self.design_view),
                 name='first_finalsubmission_design'),
            path('<int:pk>/design/render/', wrap(self.design_render),
                 name='first_finalsubmission_design_render'),
            path('<int:pk>/css/', wrap(self.css_view),
                 name='first_finalsubmission_css'),
            path('<int:pk>/hints/', wrap(self.hints_view),
                 name='first_finalsubmission_hints'),
        ] + super().get_urls()

    def _context(self, request, submission, title):
        return {
            **self.admin_site.each_context(request),
            'title': title,
            'submission': submission,
            'opts': self.model._meta,
        }

    def design_view(self, request, pk):
        """The participant's actual entry, in a sandboxed iframe."""
        submission = get_object_or_404(FinalSubmission, pk=pk)
        context = self._context(request, submission, f'Final design — {submission.pc_no}')
        context['render_url'] = reverse(
            'admin:first_finalsubmission_design_render', args=[pk],
        )
        return TemplateResponse(request, 'wf_admin/design.html', context)

    @xframe_options_sameorigin
    def design_render(self, request, pk):
        """The document the iframe above loads: challenge markup + THEIR css."""
        submission = get_object_or_404(FinalSubmission, pk=pk)
        return _render_novacloud(submission.final_css)

    def css_view(self, request, pk):
        submission = get_object_or_404(FinalSubmission, pk=pk)
        return TemplateResponse(
            request, 'wf_admin/css.html',
            self._context(request, submission, f'Final CSS — {submission.pc_no}'),
        )

    def hints_view(self, request, pk):
        submission = get_object_or_404(FinalSubmission, pk=pk)
        reveals = list(submission.user.hint_reveals.all())

        by_objective = {}
        for reveal in reveals:
            by_objective.setdefault(reveal.objective, set()).add(reveal.level)

        matrix = [
            {
                'objective': objective,
                'title': OBJECTIVE_TITLES.get(objective, objective),
                'levels': [level in levels for level in range(1, HINT_LEVELS + 1)],
                'count': len(levels),
            }
            for objective, levels in sorted(by_objective.items())
        ]

        context = self._context(request, submission, f'Hint usage — {submission.pc_no}')
        context.update({
            'matrix': matrix,
            'reveals': reveals,
            'levels': range(1, HINT_LEVELS + 1),
            'max_hints': TOTAL_CHECKS * HINT_LEVELS,
        })
        return TemplateResponse(request, 'wf_admin/hints.html', context)

    # -- deliberate deletion ----------------------------------------------

    @admin.action(
        description='Delete participant and ALL their competition data',
        permissions=['delete'],
    )
    def delete_participants(self, request, queryset):
        """Remove the people behind these entries, cascading everything.

        Deleting the `User` takes the submission, the working stylesheet and
        the hint history with it, so no orphans are left behind. Django's
        normal confirmation page runs first.
        """
        users = User.objects.filter(pk__in=queryset.values_list('user_id', flat=True))
        labels = ', '.join(sorted(users.values_list('pc_no', flat=True)))
        deleted, _ = users.delete()
        self.message_user(
            request,
            f'Permanently deleted {labels or "no participants"} '
            f'and all associated competition data ({deleted} records).',
            messages.WARNING,
        )


@admin.register(CssRule)
class WorkingStylesheetAdmin(admin.ModelAdmin):
    """The live, still-editable stylesheet. Not the competition entry."""

    list_display = ('user', 'updated_at')
    search_fields = ('user__pc_no', 'user__username')
    readonly_fields = ('user', 'updated_at')

    def has_add_permission(self, request):
        return False


@admin.register(HintReveal)
class HintRevealAdmin(admin.ModelAdmin):
    list_display = ('user', 'objective', 'level', 'revealed_at')
    list_filter = ('level', 'objective')
    search_fields = ('user__pc_no', 'objective')
    readonly_fields = ('user', 'objective', 'level', 'revealed_at')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


admin.site.site_header = 'Website Fixer — event admin'
admin.site.site_title = 'Website Fixer admin'
admin.site.index_title = 'Participants and final submissions'
