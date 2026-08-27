"""Phase 1b: layout freeze + inventory materialisation (spec 2/3).

Covers the Phase 1b half of exit test 5: the freeze happens in the same
transaction as materialisation, republishing is idempotent, and normal flow
never touches the DB backstops.
"""
from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app import engine_models as em
from app.engine_services import (
    LayoutInvalid,
    ValidationError,
    parse_layout,
    publish_performance,
)
from app.engine_services.audit import (
    ACTION_LAYOUT_FROZEN,
    ACTION_PERFORMANCE_PUBLISHED,
)

from tests.engine.layouts import PRICES, TOTAL_SEATS, make_layout_data


def _seat_count(session, performance_id: int) -> int:
    return session.execute(
        select(func.count())
        .select_from(em.PerformanceSeat)
        .where(em.PerformanceSeat.performance_id == performance_id)
    ).scalar_one()


class TestMaterialization:
    def test_144_seat_layout_produces_144_rows(self, session, world):
        result = publish_performance(
            session, world["performance_id"], prices=PRICES,
            actor_user_id=world["user_id"],
        )

        assert result.seats_created == TOTAL_SEATS == 144
        assert result.seats_existing == 0
        assert _seat_count(session, world["performance_id"]) == 144

    def test_seat_fields_come_from_the_layout_document(self, session, published):
        seat = session.execute(
            select(em.PerformanceSeat).where(
                em.PerformanceSeat.performance_id == published["performance_id"],
                em.PerformanceSeat.seat_uid == "A-1",
            )
        ).scalar_one()

        assert seat.label == "A-1"
        assert seat.section == "Stalls"
        assert seat.row_label == "A"
        assert seat.seat_number == "1"
        assert (seat.x, seat.y) == (100.0, 100.0)  # 50 px/m contract, unchanged
        assert seat.category_key == "vip"
        assert seat.accessibility is True
        assert seat.status == "available"

    def test_blocked_in_the_layout_becomes_status_blocked(self, session, published):
        """`blocked` is a layout concept; inventory expresses it as a status."""
        statuses = dict(
            session.execute(
                select(em.PerformanceSeat.seat_uid, em.PerformanceSeat.status).where(
                    em.PerformanceSeat.performance_id == published["performance_id"],
                    em.PerformanceSeat.seat_uid.in_(("F-6", "F-7", "F-8")),
                )
            ).all()
        )
        assert statuses == {"F-6": "blocked", "F-7": "blocked", "F-8": "available"}

    def test_categories_are_created_with_the_prices_passed_in(self, session, published):
        rows = {
            row.category_key: row
            for row in session.execute(
                select(em.PerformanceCategory).where(
                    em.PerformanceCategory.performance_id
                    == published["performance_id"]
                )
            ).scalars()
        }
        assert set(rows) == {"vip", "standard"}
        assert rows["vip"].amount_minor == 25_000  # KWD 25.000
        assert rows["vip"].currency == "KWD"
        assert rows["vip"].name == "VIP"
        assert rows["vip"].name_ar == "في آي بي"
        assert rows["vip"].color == "#c9a227"
        assert isinstance(rows["standard"].amount_minor, int)

    def test_publishing_moves_the_performance_on_sale(self, session, world):
        result = publish_performance(session, world["performance_id"], prices=PRICES)
        assert result.status == "on_sale"
        assert session.get(em.Performance, world["performance_id"]).status == "on_sale"

    def test_activate_false_leaves_the_performance_in_draft(self, session, world):
        result = publish_performance(
            session, world["performance_id"], prices=PRICES, activate=False
        )
        assert result.status == "draft"
        assert result.seats_created == 144  # inventory still materialised


class TestFreeze:
    def test_publishing_freezes_the_draft_layout_version(self, session, world):
        version = session.get(em.LayoutVersion, world["version_id"])
        assert version.status == "draft"
        assert version.frozen_at is None

        result = publish_performance(session, world["performance_id"], prices=PRICES)

        assert result.froze_layout is True
        session.expire_all()
        version = session.get(em.LayoutVersion, world["version_id"])
        assert version.status == "frozen"
        assert version.frozen_at is not None

    def test_freeze_and_seats_land_in_the_same_transaction(self, session, world):
        """A failure after the freeze must leave the version a draft.

        Forced by pricing a category the layout does not define: validation
        happens before either write, so neither happens.
        """
        with pytest.raises(ValidationError):
            publish_performance(
                session,
                world["performance_id"],
                prices={"vip": 25_000, "standard": 12_000, "balcony": 9_000},
            )

        session.expire_all()
        assert session.get(em.LayoutVersion, world["version_id"]).status == "draft"
        assert _seat_count(session, world["performance_id"]) == 0

    def test_normal_flow_never_fights_the_freeze_trigger(self, session, published):
        """Republishing an already-frozen version must not attempt an update of
        it - the DB trigger from Phase 1a would reject that."""
        result = publish_performance(
            session, published["performance_id"], prices=PRICES
        )
        assert result.froze_layout is False  # nothing tried to re-freeze

    def test_the_trigger_is_really_there(self, session, published):
        """Control case: the freeze guard IS installed in this database, so the
        test above is not passing because the trigger is missing."""
        from sqlalchemy.exc import DBAPIError

        version = session.get(em.LayoutVersion, published["version_id"])
        assert version.status == "frozen"
        version.layout_data = {"seats": [{"uid": "TAMPERED"}]}
        with pytest.raises(DBAPIError) as exc:
            session.flush()
        assert "immutable" in str(exc.value).lower()
        session.rollback()


class TestIdempotence:
    def test_republishing_creates_no_duplicate_seats(self, session, published):
        second = publish_performance(session, published["performance_id"], prices=PRICES)

        assert second.seats_created == 0
        assert second.seats_existing == 144
        assert second.seats_total == 144
        assert _seat_count(session, published["performance_id"]) == 144

    def test_the_seat_uid_backstop_never_fires_in_normal_flow(self, session, published):
        """Publish four more times; UNIQUE(performance_id, seat_uid) stays a
        backstop rather than becoming control flow."""
        for _ in range(4):
            publish_performance(session, published["performance_id"], prices=PRICES)
        assert _seat_count(session, published["performance_id"]) == 144

    def test_republishing_reprices_categories(self, session, published):
        """The layout is frozen and holds no price; repricing is legitimate."""
        result = publish_performance(
            session,
            published["performance_id"],
            prices={"vip": 30_000, "standard": 12_000},
        )
        assert result.categories_created == 0
        assert result.categories_updated == 1

        session.expire_all()
        vip = session.execute(
            select(em.PerformanceCategory).where(
                em.PerformanceCategory.performance_id == published["performance_id"],
                em.PerformanceCategory.category_key == "vip",
            )
        ).scalar_one()
        assert vip.amount_minor == 30_000

    def test_republishing_with_identical_prices_updates_nothing(self, session, published):
        result = publish_performance(session, published["performance_id"], prices=PRICES)
        assert (result.categories_created, result.categories_updated) == (0, 0)


class TestAudit:
    def test_freeze_and_publish_are_both_audited(self, session, published):
        actions = [
            row.action
            for row in session.execute(
                select(em.AuditLog).order_by(em.AuditLog.id)
            ).scalars()
        ]
        assert ACTION_LAYOUT_FROZEN in actions
        assert ACTION_PERFORMANCE_PUBLISHED in actions

    def test_the_publish_audit_row_records_what_happened(self, session, published):
        row = session.execute(
            select(em.AuditLog).where(
                em.AuditLog.action == ACTION_PERFORMANCE_PUBLISHED
            )
        ).scalar_one()
        assert row.organization_id == published["org_id"]
        assert row.entity_id == published["performance_id"]
        assert row.actor_user_id == published["user_id"]
        assert row.data["seats_created"] == 144
        assert row.data["froze_layout"] is True

    def test_republishing_does_not_re_log_the_freeze(self, session, published):
        publish_performance(session, published["performance_id"], prices=PRICES)
        freezes = session.execute(
            select(func.count())
            .select_from(em.AuditLog)
            .where(em.AuditLog.action == ACTION_LAYOUT_FROZEN)
        ).scalar_one()
        assert freezes == 1


class TestValidation:
    def test_missing_price_for_a_used_category_is_rejected(self, session, world):
        with pytest.raises(ValidationError) as exc:
            publish_performance(session, world["performance_id"], prices={"vip": 25_000})
        assert "standard" in str(exc.value)
        assert _seat_count(session, world["performance_id"]) == 0

    def test_float_prices_are_rejected_before_they_reach_the_column(self, session, world):
        """Money is integer minor units. KWD 25.000 is 25000, never 25.0."""
        with pytest.raises(ValidationError) as exc:
            publish_performance(
                session, world["performance_id"], prices={"vip": 25.0, "standard": 12000}
            )
        assert "minor units" in str(exc.value)

    def test_a_layout_with_duplicate_seat_uids_is_rejected(self, session, world):
        version = session.get(em.LayoutVersion, world["version_id"])
        version.layout_data = {
            "seats": [{"uid": "A-1"}, {"uid": "A-1"}],
            "categories": [],
        }
        session.commit()

        with pytest.raises(LayoutInvalid) as exc:
            publish_performance(session, world["performance_id"], prices={})
        assert "duplicates seat_uid" in str(exc.value.detail["problems"])

    def test_a_seat_referencing_an_unknown_category_is_rejected(self, session, world):
        version = session.get(em.LayoutVersion, world["version_id"])
        version.layout_data = {
            "seats": [{"uid": "A-1", "category_key": "balcony"}],
            "categories": [{"key": "vip", "name": "VIP"}],
        }
        session.commit()

        with pytest.raises(LayoutInvalid) as exc:
            publish_performance(session, world["performance_id"], prices={"vip": 1000})
        assert "unknown category" in str(exc.value.detail["problems"])

    def test_all_problems_are_reported_at_once(self, session, world):
        version = session.get(em.LayoutVersion, world["version_id"])
        version.layout_data = {
            "seats": [{"x": 1}, {"uid": "A-1"}, {"uid": "A-1"}],
            "categories": [{"name": "no key"}],
        }
        session.commit()

        with pytest.raises(LayoutInvalid) as exc:
            publish_performance(session, world["performance_id"], prices={})
        assert len(exc.value.detail["problems"]) == 3


class TestLayoutReader:
    """The reader accepts both shapes already present in this repository."""

    def test_the_legacy_marketplace_seat_shape_is_understood(self):
        parsed = parse_layout(
            {
                "categories": [{"id": "cat-vip", "name": "VIP", "price": 120.0}],
                "seats": [
                    {
                        "id": "s0-1",
                        "x": 140,
                        "y": 100,
                        "catId": "cat-vip",
                        "row": 1,
                        "col": 2,
                        "label": "A2",
                        "blocked": True,
                    }
                ],
            }
        )
        seat = parsed.seats[0]
        assert seat.seat_uid == "s0-1"
        assert seat.category_key == "cat-vip"
        assert seat.label == "A2"
        assert seat.status == "blocked"

    def test_the_phase_1a_fixture_shape_is_understood(self):
        parsed = parse_layout({"seats": [{"uid": "A-12"}], "categories": []})
        assert parsed.seats[0].seat_uid == "A-12"
        assert parsed.seats[0].label == "A-12"  # falls back to the uid
        assert parsed.seats[0].status == "available"

    def test_an_explicit_status_wins_over_blocked(self):
        parsed = parse_layout(
            {"seats": [{"uid": "A-1", "status": "invitation", "blocked": True}]}
        )
        assert parsed.seats[0].status == "invitation"

    def test_an_unknown_explicit_status_is_rejected(self):
        with pytest.raises(LayoutInvalid):
            parse_layout({"seats": [{"uid": "A-1", "status": "sold"}]})

    def test_the_full_fixture_parses_to_144_seats_and_2_categories(self):
        parsed = parse_layout(make_layout_data())
        assert len(parsed.seats) == 144
        assert parsed.category_keys == {"vip", "standard"}
        assert parsed.referenced_category_keys == {"vip", "standard"}
        assert sum(1 for s in parsed.seats if s.status == "blocked") == 2
        assert sum(1 for s in parsed.seats if s.accessibility) == 2

    @pytest.mark.parametrize("bad", [None, [], "seats", 42])
    def test_a_document_that_is_not_a_layout_is_rejected(self, bad):
        with pytest.raises(LayoutInvalid):
            parse_layout(bad)
