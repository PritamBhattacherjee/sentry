from collections import defaultdict
from enum import Enum
from typing import Any, List, Optional, TypedDict

from django.conf import settings
from django.db import models
from django.utils import timezone

from sentry import features
from sentry.db.models import FlexibleForeignKey, Model, region_silo_only_model
from sentry.db.models.fields.jsonfield import JSONField
from sentry.models.commitauthor import CommitAuthor
from sentry.models.group import Group
from sentry.models.release import Release
from sentry.models.releasecommit import ReleaseCommit


class GroupOwnerType(Enum):
    SUSPECT_COMMIT = 0
    OWNERSHIP_RULE = 1
    CODEOWNERS = 2


class OwnerRuleType(Enum):
    OWNERSHIP_RULE = "ownership_rule"
    CODEOWNERS = "codeowners"


GROUP_OWNER_TYPE = {
    GroupOwnerType.SUSPECT_COMMIT: "suspectCommit",
    GroupOwnerType.OWNERSHIP_RULE: "ownershipRule",
    GroupOwnerType.CODEOWNERS: "codeowners",
}


class OwnersSerialized(TypedDict):
    type: GroupOwnerType
    owner: str
    date_added: models.DateTimeField


class OwnersSerializedWithCommits(TypedDict):
    type: GroupOwnerType
    author: CommitAuthor
    release: Optional[Release]
    commits: List[ReleaseCommit]


@region_silo_only_model
class GroupOwner(Model):
    """
    Tracks the "owners" or "suggested assignees" of a group.
    """

    __include_in_export__ = False

    group = FlexibleForeignKey("sentry.Group", db_constraint=False)
    project = FlexibleForeignKey("sentry.Project", db_constraint=False)
    organization = FlexibleForeignKey("sentry.Organization", db_constraint=False)
    type = models.PositiveSmallIntegerField(
        choices=(
            (GroupOwnerType.SUSPECT_COMMIT, "Suspect Commit"),
            (GroupOwnerType.OWNERSHIP_RULE, "Ownership Rule"),
            (GroupOwnerType.CODEOWNERS, "Codeowners"),
        )
    )
    context = JSONField(null=True)
    user = FlexibleForeignKey(settings.AUTH_USER_MODEL, null=True)
    team = FlexibleForeignKey("sentry.Team", null=True)
    date_added = models.DateTimeField(default=timezone.now)

    class Meta:
        app_label = "sentry"
        db_table = "sentry_groupowner"

    def save(self, *args, **kwargs):
        keys = list(filter(None, [self.user_id, self.team_id]))
        assert len(keys) == 1, "Must have team or user, not both"
        super().save(*args, **kwargs)

    def owner_id(self):
        if self.user_id:
            return f"user:{self.user_id}"

        if self.team_id:
            return f"team:{self.team_id}"

        raise NotImplementedError("Unknown Owner")

    def owner(self):
        from sentry.models import ActorTuple

        return ActorTuple.from_actor_identifier(self.owner_id())


def get_owner_details(group_list: List[Group], user: Any) -> List[OwnersSerialized]:
    group_ids = [g.id for g in group_list]
    group_owners = GroupOwner.objects.filter(group__in=group_ids)
    owner_details = defaultdict(list)
    for go in group_owners:
        owner_details[go.group_id].append(
            OwnersSerialized(
                type=GROUP_OWNER_TYPE[GroupOwnerType(go.type)],
                owner=go.owner().get_actor_identifier(),
                date_added=go.date_added,
            ),
        )

    org = group_list[0].project.organization if len(group_list) > 0 else None
    if org and features.has("organizations:release-committer-assignees", org, actor=user):
        for g in group_list:
            # TODO(snigdha): optimize this to bulk grab committer data for all groups
            release_committers_owners = get_release_committers_for_group(g)
            for rc_owner in release_committers_owners:
                owner_details[g.id].append(rc_owner)

    return owner_details


# Get all committers for the first releases for a group.
# TODO(snigdha): this should to be refactored to be performant enough to be used beyond Sentry.
def get_release_committers_for_group(group: Group, include_commits: bool = False) -> List[Any]:
    from sentry.api.serializers import get_users_for_authors

    release_commits = ReleaseCommit.objects.filter(
        release=group.first_release, commit__isnull=False, commit__author__isnull=False
    ).select_related("commit", "commit__author")

    if not release_commits:
        return []

    author_to_user = get_users_for_authors(
        group.project.organization_id,
        [rc.commit.author for rc in release_commits],
    )

    owners_data = defaultdict(dict)
    for rc in release_commits:
        rc_data = owners_data[rc.commit.author.id]
        if "owner" not in rc_data:
            user = author_to_user.get(str(rc.commit.author.id))
            rc_data["author"] = user
            if "id" not in user:
                continue
            rc_data["owner"] = f"user:{user.get('id')}"

        if include_commits:
            commits = rc_data.get("commits", [])
            rc_data["commits"] = commits + [rc.commit]

        if "date_added" in rc_data:
            rc_data["date_added"] = max(rc_data["date_added"], rc.commit.date_added)
        else:
            rc_data["date_added"] = rc.commit.date_added
        owners_data[rc.commit.author.id] = rc_data

    if include_commits:
        return [
            OwnersSerializedWithCommits(
                type="releaseCommit",
                release=group.first_release,
                author=rc_data["author"],
                commits=rc_data["commits"],
            )
            for rc_data in owners_data.values()
            if "commits" in rc_data
        ]

    return [
        OwnersSerialized(
            type="releaseCommit",
            owner=rc_data["owner"],
            date_added=rc_data["date_added"],
        )
        for rc_data in owners_data.values()
        if "owner" in rc_data
    ]
