"""Phase 1b: personal-organization auto-provisioning (spec 1).

The interesting property is not "it creates a row" - it is that two first
requests from the same brand-new user, arriving together, produce exactly ONE
organization. See `TestConcurrency`.
"""
from __future__ import annotations

import threading

import pytest
from sqlalchemy import func, select

from app import engine_models as em
from app.engine_services import (
    ensure_personal_organization,
    find_active_organization,
    personal_org_slug,
    slugify,
)
from app.engine_services.audit import ACTION_ORG_PROVISIONED
from app.models import User


def make_user(session, sub="auth0|new", email="new@kursi.io", name="Sara Al-Ali"):
    user = User(auth0_sub=sub, email=email, name=name, role="customer")
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def count(session, model):
    return session.execute(select(func.count()).select_from(model)).scalar_one()


class TestProvisioning:
    def test_a_new_user_gets_a_personal_org_and_an_owner_membership(self, session):
        user = make_user(session)

        org, created = ensure_personal_organization(session, user)

        assert created is True
        assert org.type == "personal"
        assert org.plan == "personal"
        assert org.status == "active"
        assert org.name == "Sara Al-Ali"

        membership = session.execute(
            select(em.Membership).where(em.Membership.user_id == user.id)
        ).scalar_one()
        assert membership.organization_id == org.id
        assert (membership.role, membership.status) == ("owner", "active")

    def test_it_is_idempotent(self, session):
        user = make_user(session)

        first, created_first = ensure_personal_organization(session, user)
        second, created_second = ensure_personal_organization(session, user)

        assert (created_first, created_second) == (True, False)
        assert first.id == second.id
        assert count(session, em.Organization) == 1
        assert count(session, em.Membership) == 1

    def test_a_user_who_already_belongs_somewhere_gets_nothing_new(self, session):
        """"if the user has none" - an existing business membership counts."""
        user = make_user(session)
        business = em.Organization(name="Kursi Events", slug="kursi-events", type="business")
        session.add(business)
        session.flush()
        session.add(
            em.Membership(
                organization_id=business.id, user_id=user.id, role="event_manager"
            )
        )
        session.commit()

        org, created = ensure_personal_organization(session, user)

        assert created is False
        assert org.id == business.id
        assert count(session, em.Organization) == 1

    def test_an_invitation_is_not_a_home(self, session):
        """A pending invite is not an active membership, so the user still needs
        an organization of their own."""
        user = make_user(session)
        other = em.Organization(name="Someone Else", slug="someone-else", type="business")
        session.add(other)
        session.flush()
        session.add(
            em.Membership(
                organization_id=other.id, user_id=user.id, role="scanner",
                status="invited",
            )
        )
        session.commit()

        org, created = ensure_personal_organization(session, user)

        assert created is True
        assert org.type == "personal"

    def test_the_provisioning_is_audited(self, session):
        user = make_user(session)
        org, _ = ensure_personal_organization(session, user)

        row = session.execute(
            select(em.AuditLog).where(em.AuditLog.action == ACTION_ORG_PROVISIONED)
        ).scalar_one()
        assert row.organization_id == org.id
        assert row.entity_id == org.id
        assert row.actor_user_id == user.id
        assert row.data["slug"] == org.slug
        assert row.data["type"] == "personal"

    def test_org_and_membership_are_one_transaction(self, session, monkeypatch):
        """A failure while writing the membership must leave no orphan org."""
        import app.engine_services.provisioning as provisioning

        user = make_user(session)

        def boom(*args, **kwargs):
            raise RuntimeError("membership write failed")

        monkeypatch.setattr(provisioning, "_ensure_owner_membership", boom)

        with pytest.raises(RuntimeError):
            ensure_personal_organization(session, user)

        assert count(session, em.Organization) == 0
        assert count(session, em.Membership) == 0
        assert count(session, em.AuditLog) == 0


class TestSlugs:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Sara Al-Ali", "sara-al-ali"),
            ("  Spaced   Out  ", "spaced-out"),
            ("Ali!!!", "ali"),
            ("", "org"),
            (None, "org"),
            ("مسرح", "org"),  # no ASCII to keep - the fallback carries it
        ],
    )
    def test_slugify(self, raw, expected):
        assert slugify(raw) == expected

    def test_the_personal_slug_is_deterministic_per_user(self, session):
        """This is what makes the UNIQUE index a per-user race arbiter: two
        racers must generate the SAME slug and collide."""
        user = make_user(session)
        assert personal_org_slug(user) == personal_org_slug(user)
        assert personal_org_slug(user) == f"sara-al-ali-{user.id}"

    def test_two_people_with_the_same_name_get_different_slugs(self, session):
        one = make_user(session, sub="auth0|a", email="a@kursi.io", name="Ali")
        two = make_user(session, sub="auth0|b", email="b@kursi.io", name="Ali")

        first, _ = ensure_personal_organization(session, one)
        second, _ = ensure_personal_organization(session, two)

        assert first.slug != second.slug
        assert first.name == second.name == "Ali"
        assert count(session, em.Organization) == 2

    def test_a_nameless_user_falls_back_to_the_email_local_part(self, session):
        user = make_user(session, name=None, email="hala@kursi.io")
        org, _ = ensure_personal_organization(session, user)
        assert org.name == "hala"
        assert org.slug == f"hala-{user.id}"

    def test_a_user_with_neither_name_nor_email_still_gets_an_org(self, session):
        user = make_user(session, name=None, email="")
        org, _ = ensure_personal_organization(session, user)
        assert org.name == f"User {user.id}"


class TestConcurrency:
    def test_two_simultaneous_first_requests_create_exactly_one_org(
        self, session, session_factory
    ):
        """The race the spec cares about: one cold user, two tabs.

        Both callers see no membership, both decide to provision, and the UNIQUE
        index on `organizations.slug` picks the winner. The loser catches the
        IntegrityError, re-reads and adopts the winner's organization - it does
        NOT invent a second slug, which is exactly the bug a "try ali, then
        ali-2" collision strategy would have.

        On SQLite the two write transactions are serialised by the harness
        rather than overlapping (see tests/engine/conftest.py), so what this
        proves there is the arbiter and the loser's recovery path under real
        threads, not interleaved contention. On PostgreSQL they genuinely
        overlap.
        """
        user = make_user(session)
        user_id = user.id
        session.rollback()  # release this session's transaction before threading

        barrier = threading.Barrier(2)
        outcomes: dict[int, tuple] = {}

        def attempt(index: int) -> None:
            worker = session_factory()
            try:
                # Nothing touches the database before the barrier: under the
                # SQLite harness a transaction opened here would hold the write
                # lock while the other thread waited, and the barrier would time
                # out instead of releasing them together.
                barrier.wait(timeout=15)
                worker_user = worker.get(User, user_id)
                org, created = ensure_personal_organization(worker, worker_user)
                outcomes[index] = ("ok", org.id, created)
            except Exception as exc:  # pragma: no cover - reported below
                outcomes[index] = ("error", repr(exc), None)
            finally:
                worker.close()

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert [o[0] for o in outcomes.values()] == ["ok", "ok"], outcomes
        org_ids = {o[1] for o in outcomes.values()}
        assert len(org_ids) == 1, f"the two callers disagree about the org: {outcomes}"
        # Exactly one of them believes it did the creating.
        assert sum(1 for o in outcomes.values() if o[2]) == 1, outcomes

        assert count(session, em.Organization) == 1
        assert count(session, em.Membership) == 1
        assert (
            session.execute(
                select(func.count())
                .select_from(em.AuditLog)
                .where(em.AuditLog.action == ACTION_ORG_PROVISIONED)
            ).scalar_one()
            == 1
        )

    def test_five_simultaneous_requests_still_create_exactly_one_org(
        self, session, session_factory
    ):
        user = make_user(session)
        user_id = user.id
        session.rollback()

        barrier = threading.Barrier(5)
        errors: list[str] = []

        def attempt() -> None:
            worker = session_factory()
            try:
                barrier.wait(timeout=20)
                worker_user = worker.get(User, user_id)
                ensure_personal_organization(worker, worker_user)
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(repr(exc))
            finally:
                worker.close()

        threads = [threading.Thread(target=attempt) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        assert errors == []
        assert count(session, em.Organization) == 1
        assert count(session, em.Membership) == 1

    def test_find_active_organization_agrees_afterwards(self, session):
        user = make_user(session)
        org, _ = ensure_personal_organization(session, user)
        assert find_active_organization(session, user).id == org.id
