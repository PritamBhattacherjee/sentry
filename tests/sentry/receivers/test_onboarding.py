from django.utils import timezone

from sentry.models import (
    Integration,
    OnboardingTask,
    OnboardingTaskStatus,
    OrganizationOnboardingTask,
    OrganizationOption,
    Rule,
)
from sentry.plugins.bases import IssueTrackingPlugin
from sentry.signals import (
    alert_rule_created,
    event_processed,
    first_event_pending,
    first_event_received,
    first_transaction_received,
    integration_added,
    issue_tracker_used,
    member_invited,
    member_joined,
    plugin_enabled,
    project_created,
)
from sentry.testutils import TestCase
from sentry.testutils.helpers.datetime import before_now, iso_format
from sentry.testutils.silo import region_silo_test
from sentry.utils.samples import load_data


@region_silo_test
class OrganizationOnboardingTaskTest(TestCase):
    def create_integration(self, provider, external_id=9999):
        return Integration.objects.create(
            provider=provider,
            name="test",
            external_id=external_id,
        )

    def test_no_existing_task(self):
        now = timezone.now()
        project = self.create_project(first_event=now)
        event = self.store_event(data={}, project_id=project.id)
        first_event_received.send(project=project, event=event, sender=type(project))

        task = OrganizationOnboardingTask.objects.get(
            organization=project.organization, task=OnboardingTask.FIRST_EVENT
        )
        assert task.status == OnboardingTaskStatus.COMPLETE
        assert task.project_id == project.id
        assert task.date_completed == project.first_event

    def test_existing_pending_task(self):
        now = timezone.now()
        project = self.create_project(first_event=now)

        first_event_pending.send(project=project, user=self.user, sender=type(project))

        task = OrganizationOnboardingTask.objects.get(
            organization=project.organization, task=OnboardingTask.FIRST_EVENT
        )

        assert task.status == OnboardingTaskStatus.PENDING
        assert task.project_id == project.id

        event = self.store_event(data={}, project_id=project.id)
        first_event_received.send(project=project, event=event, sender=type(project))

        task = OrganizationOnboardingTask.objects.get(
            organization=project.organization, task=OnboardingTask.FIRST_EVENT
        )

        assert task.status == OnboardingTaskStatus.COMPLETE
        assert task.project_id == project.id
        assert task.date_completed == project.first_event

    def test_existing_complete_task(self):
        now = timezone.now()
        project = self.create_project(first_event=now)
        task = OrganizationOnboardingTask.objects.create(
            organization=project.organization,
            task=OnboardingTask.FIRST_PROJECT,
            status=OnboardingTaskStatus.COMPLETE,
        )

        event = self.store_event(data={}, project_id=project.id)
        first_event_received.send(project=project, event=event, sender=type(project))

        task = OrganizationOnboardingTask.objects.get(id=task.id)
        assert task.status == OnboardingTaskStatus.COMPLETE
        assert not task.project_id

    # Tests on the receivers
    def test_event_processed(self):
        now = timezone.now()
        project = self.create_project(first_event=now)
        event = self.store_event(
            data={
                "event_id": "a" * 32,
                "platform": "javascript",
                "timestamp": iso_format(before_now(minutes=1)),
                "tags": {
                    "sentry:release": "e1b5d1900526feaf20fe2bc9cad83d392136030a",
                    "sentry:user": "id:41656",
                },
                "user": {"ip_address": "0.0.0.0", "id": "41656", "email": "test@example.com"},
                "exception": {
                    "values": [
                        {
                            "stacktrace": {
                                "frames": [
                                    {
                                        "data": {
                                            "sourcemap": "https://media.sentry.io/_static/29e365f8b0d923bc123e8afa38d890c3/sentry/dist/vendor.js.map"
                                        }
                                    }
                                ]
                            },
                            "type": "TypeError",
                        }
                    ]
                },
            },
            project_id=project.id,
        )

        event_processed.send(project=project, event=event, sender=type(project))

        task = OrganizationOnboardingTask.objects.get(
            organization=project.organization,
            task=OnboardingTask.RELEASE_TRACKING,
            status=OnboardingTaskStatus.COMPLETE,
        )
        assert task is not None

        task = OrganizationOnboardingTask.objects.get(
            organization=project.organization,
            task=OnboardingTask.USER_CONTEXT,
            status=OnboardingTaskStatus.COMPLETE,
        )
        assert task is not None

        task = OrganizationOnboardingTask.objects.get(
            organization=project.organization,
            task=OnboardingTask.SOURCEMAPS,
            status=OnboardingTaskStatus.COMPLETE,
        )
        assert task is not None

    def test_project_created(self):
        now = timezone.now()
        project = self.create_project(first_event=now)
        project_created.send(project=project, user=self.user, sender=type(project))

        task = OrganizationOnboardingTask.objects.get(
            organization=project.organization,
            task=OnboardingTask.FIRST_PROJECT,
            status=OnboardingTaskStatus.COMPLETE,
        )
        assert task is not None

    def test_first_event_pending(self):
        now = timezone.now()
        project = self.create_project(first_event=now)
        first_event_pending.send(project=project, user=self.user, sender=type(project))

        task = OrganizationOnboardingTask.objects.get(
            organization=project.organization,
            task=OnboardingTask.FIRST_EVENT,
            status=OnboardingTaskStatus.PENDING,
        )
        assert task is not None

    def test_first_event_received(self):
        now = timezone.now()
        project = self.create_project(first_event=now)
        project_created.send(project=project, user=self.user, sender=type(project))
        event = self.store_event(
            data={"platform": "javascript", "message": "javascript error message"},
            project_id=project.id,
        )
        first_event_received.send(project=project, event=event, sender=type(project))

        task = OrganizationOnboardingTask.objects.get(
            organization=project.organization,
            task=OnboardingTask.FIRST_EVENT,
            status=OnboardingTaskStatus.COMPLETE,
        )
        assert task is not None
        assert "platform" in task.data
        assert task.data["platform"] == "javascript"

        second_project = self.create_project(first_event=now)
        project_created.send(project=second_project, user=self.user, sender=type(second_project))
        second_task = OrganizationOnboardingTask.objects.get(
            organization=second_project.organization,
            task=OnboardingTask.SECOND_PLATFORM,
            status=OnboardingTaskStatus.PENDING,
        )
        assert second_task is not None

        second_event = self.store_event(
            data={"platform": "python", "message": "python error message"},
            project_id=second_project.id,
        )
        first_event_received.send(
            project=second_project, event=second_event, sender=type(second_project)
        )
        second_task = OrganizationOnboardingTask.objects.get(
            organization=second_project.organization,
            task=OnboardingTask.SECOND_PLATFORM,
            status=OnboardingTaskStatus.COMPLETE,
        )
        assert second_task is not None
        assert "platform" in second_task.data
        assert second_task.data["platform"] == "python"
        assert task.data["platform"] != second_task.data["platform"]

    def test_first_transaction_received(self):
        project = self.create_project()

        event_data = load_data("transaction")
        min_ago = iso_format(before_now(minutes=1))
        event_data.update({"start_timestamp": min_ago, "timestamp": min_ago})

        event = self.store_event(data=event_data, project_id=project.id)

        first_event_received.send(project=project, event=event, sender=type(project))
        first_transaction_received.send(project=project, event=event, sender=type(project))

        task = OrganizationOnboardingTask.objects.get(
            organization=project.organization,
            task=OnboardingTask.FIRST_TRANSACTION,
            status=OnboardingTaskStatus.COMPLETE,
        )

        assert task is not None

        assert project.flags.has_transactions

    def test_member_invited(self):
        user = self.create_user(email="test@example.org")
        member = self.create_member(organization=self.organization, teams=[self.team], user=user)
        member_invited.send(member=member, user=user, sender=type(member))

        task = OrganizationOnboardingTask.objects.get(
            organization=self.organization,
            task=OnboardingTask.INVITE_MEMBER,
            status=OnboardingTaskStatus.PENDING,
        )
        assert task is not None

    def test_member_joined(self):
        user = self.create_user(email="test@example.org")
        member = self.create_member(organization=self.organization, teams=[self.team], user=user)
        member_joined.send(member=member, organization=self.organization, sender=type(member))

        task = OrganizationOnboardingTask.objects.get(
            organization=self.organization,
            task=OnboardingTask.INVITE_MEMBER,
            status=OnboardingTaskStatus.COMPLETE,
        )
        assert task is not None

        user2 = self.create_user(email="test@example.com")
        member2 = self.create_member(organization=self.organization, teams=[self.team], user=user2)
        member_joined.send(member=member2, organization=self.organization, sender=type(member2))

        task = OrganizationOnboardingTask.objects.get(
            organization=self.organization,
            task=OnboardingTask.INVITE_MEMBER,
            status=OnboardingTaskStatus.COMPLETE,
        )
        assert task.data["invited_member_id"] == member.id

    def test_issue_tracker_onboarding(self):
        plugin_enabled.send(
            plugin=IssueTrackingPlugin(),
            project=self.project,
            user=self.user,
            sender=type(IssueTrackingPlugin),
        )
        task = OrganizationOnboardingTask.objects.get(
            organization=self.organization,
            task=OnboardingTask.ISSUE_TRACKER,
            status=OnboardingTaskStatus.PENDING,
        )
        assert task is not None

        issue_tracker_used.send(
            plugin=IssueTrackingPlugin(),
            project=self.project,
            user=self.user,
            sender=type(IssueTrackingPlugin),
        )
        task = OrganizationOnboardingTask.objects.get(
            organization=self.organization,
            task=OnboardingTask.ISSUE_TRACKER,
            status=OnboardingTaskStatus.COMPLETE,
        )
        assert task is not None

    def test_alert_added(self):
        alert_rule_created.send(
            rule=Rule(id=1),
            project=self.project,
            user=self.user,
            rule_type="issue",
            sender=type(Rule),
            is_api_token=False,
        )
        task = OrganizationOnboardingTask.objects.get(
            organization=self.organization,
            task=OnboardingTask.ALERT_RULE,
            status=OnboardingTaskStatus.COMPLETE,
        )
        assert task is not None

    def test_integration_added(self):
        integration_added.send(
            integration=self.create_integration("slack", 1234),
            organization=self.organization,
            user=self.user,
            sender=type(self.organization),
        )
        task = OrganizationOnboardingTask.objects.get(
            organization=self.organization,
            task=OnboardingTask.INTEGRATIONS,
            status=OnboardingTaskStatus.COMPLETE,
        )
        assert task is not None
        assert task.data["providers"] == ["slack"]

        # Adding a second integration
        integration_added.send(
            integration=self.create_integration("github", 4567),
            organization=self.organization,
            user=self.user,
            sender=type(self.organization),
        )
        task = OrganizationOnboardingTask.objects.get(
            organization=self.organization,
            task=OnboardingTask.INTEGRATIONS,
            status=OnboardingTaskStatus.COMPLETE,
        )
        assert "slack" in task.data["providers"]
        assert "github" in task.data["providers"]
        assert len(task.data["providers"]) == 2

        # Installing an integration a second time doesn't produce
        # duplicated providers in the list
        integration_added.send(
            integration=self.create_integration("slack", 4747),
            organization=self.organization,
            user=self.user,
            sender=type(self.organization),
        )
        task = OrganizationOnboardingTask.objects.get(
            organization=self.organization,
            task=OnboardingTask.INTEGRATIONS,
            status=OnboardingTaskStatus.COMPLETE,
        )
        assert "slack" in task.data["providers"]
        assert "github" in task.data["providers"]
        assert len(task.data["providers"]) == 2

    def test_metric_added(self):
        alert_rule_created.send(
            rule=Rule(id=1),
            project=self.project,
            user=self.user,
            rule_type="metric",
            sender=type(Rule),
            is_api_token=False,
        )
        task = OrganizationOnboardingTask.objects.get(
            organization=self.organization,
            task=OnboardingTask.METRIC_ALERT,
            status=OnboardingTaskStatus.COMPLETE,
        )
        assert task is not None

    def test_onboarding_complete(self):
        now = timezone.now()
        user = self.create_user(email="test@example.org")
        project = self.create_project(first_event=now)
        second_project = self.create_project(first_event=now)
        second_event = self.store_event(
            data={"platform": "python", "message": "python error message"},
            project_id=second_project.id,
        )
        event = self.store_event(
            data={
                "event_id": "a" * 32,
                "platform": "javascript",
                "timestamp": iso_format(before_now(minutes=1)),
                "tags": {
                    "sentry:release": "e1b5d1900526feaf20fe2bc9cad83d392136030a",
                    "sentry:user": "id:41656",
                },
                "user": {"ip_address": "0.0.0.0", "id": "41656", "email": "test@example.com"},
                "exception": {
                    "values": [
                        {
                            "stacktrace": {
                                "frames": [
                                    {
                                        "data": {
                                            "sourcemap": "https://media.sentry.io/_static/29e365f8b0d923bc123e8afa38d890c3/sentry/dist/vendor.js.map"
                                        }
                                    }
                                ]
                            },
                            "type": "TypeError",
                        }
                    ]
                },
            },
            project_id=project.id,
        )

        event_data = load_data("transaction")
        min_ago = iso_format(before_now(minutes=1))
        event_data.update({"start_timestamp": min_ago, "timestamp": min_ago})

        transaction = self.store_event(data=event_data, project_id=project.id)

        first_event_received.send(project=project, event=transaction, sender=type(project))
        first_transaction_received.send(project=project, event=transaction, sender=type(project))

        member = self.create_member(organization=self.organization, teams=[self.team], user=user)

        event_processed.send(project=project, event=event, sender=type(project))
        project_created.send(project=project, user=user, sender=type(project))
        project_created.send(project=second_project, user=user, sender=type(second_project))

        first_event_received.send(project=project, event=event, sender=type(project))
        first_event_received.send(
            project=second_project, event=second_event, sender=type(second_project)
        )
        member_joined.send(member=member, organization=self.organization, sender=type(member))
        plugin_enabled.send(
            plugin=IssueTrackingPlugin(),
            project=project,
            user=user,
            sender=type(IssueTrackingPlugin),
        )
        issue_tracker_used.send(
            plugin=IssueTrackingPlugin(),
            project=project,
            user=user,
            sender=type(IssueTrackingPlugin),
        )
        integration_added.send(
            integration=self.create_integration("slack"),
            organization=self.organization,
            user=user,
            sender=type(project),
        )
        alert_rule_created.send(
            rule=Rule(id=1),
            project=self.project,
            user=self.user,
            rule_type="issue",
            sender=type(Rule),
            is_api_token=False,
        )
        alert_rule_created.send(
            rule=Rule(id=1),
            project=self.project,
            user=self.user,
            rule_type="metric",
            sender=type(Rule),
            is_api_token=False,
        )

        assert (
            OrganizationOption.objects.filter(
                organization=self.organization, key="onboarding:complete"
            ).count()
            == 1
        )
