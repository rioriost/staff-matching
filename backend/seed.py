"""Deterministic, fictional fixtures; never an adapter for real staff data."""

from datetime import UTC, datetime

from backend.models import (
    Assignment,
    Order,
    PairEvidence,
    Period,
    Qualification,
    Snapshot,
    Staff,
)


def demo_snapshot() -> Snapshot:
    november = Period(start="2026-11-01", end="2026-12-01")
    december = Period(start="2026-12-01", end="2027-01-01")
    availability = Period(start="2026-10-01", end="2027-02-01")
    skills = [
        {"office": 3, "forklift": 3},
        {"office": 3},
        {"packing": 3},
        {"packing": 3},
        {"packing": 3},
        {"packing": 3},
    ]
    staff = [
        Staff(
            id=f"s{index:02}",
            name=f"合成スタッフ{chr(64 + index)}",
            skills=value,
            qualifications={
                "forklift_certificate": Qualification(verified=True, valid_until="2027-01-31")
            }
            if index == 1
            else {},
            availability=availability,
            min_hourly_rate=1200,
            commute_limit_minutes=60,
            matching_enabled=index != 6,
            eligibility_verified=True,
            mandatory_verified=True,
        )
        for index, value in enumerate(skills, start=1)
    ]
    orders = [
        Order(
            id=identifier,
            title=title,
            period=period,
            headcount=headcount,
            hourly_rate=1600,
            required_skills={skill: 2},
            required_qualifications=["forklift_certificate"] if skill == "forklift" else [],
            eligibility_verified=True,
            mandatory_verified=True,
        )
        for identifier, title, period, headcount, skill in [
            ("o01", "架空事業所・事務（11月）", november, 1, "office"),
            ("o02", "架空事業所・構内作業（11月）", november, 1, "forklift"),
            ("o03", "架空事業所・梱包（11月）", november, 2, "packing"),
            ("o04", "架空事業所・事務（12月）", december, 1, "office"),
            ("o05", "架空事業所・特殊作業（11月）", november, 1, "rare"),
        ]
    ]
    evidence = [
        PairEvidence(
            staff_id=staff_id,
            order_id=order_id,
            commute_minutes=None if staff_id == "s05" else 15,
            commute_verified=staff_id != "s05",
            skill_fit=score,
            preference_fit=score,
            history_fit=None,
            rated_assignments=0,
        )
        for staff_id, order_id, score in [
            ("s01", "o01", 0.98),
            ("s02", "o01", 0.70),
            ("s01", "o02", 0.70),
            ("s04", "o03", 0.80),
            ("s05", "o03", 0.90),
            ("s06", "o03", 0.90),
            ("s01", "o04", 0.90),
            ("s02", "o04", 0.80),
            ("s04", "o05", 0.50),
        ]
    ]
    return Snapshot(
        tenant_id="demo",
        revision=1,
        staff=staff,
        orders=orders,
        evidence=evidence,
        assignments=[
            Assignment(
                id="a-seed-001",
                staff_id="s03",
                order_id="o03",
                period=november,
                source_run_id=None,
                proposal_id=None,
                confirmed_at=datetime(2026, 10, 1, tzinfo=UTC),
            )
        ],
    )
