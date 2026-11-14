"""Alarms exist on prod only, and there are exactly five of them (ADR-23).

docs/06 §9 lists six protective signals, but `budget-90-no-action` is a delayed
self-check inside `ops-alarms` rather than a CloudWatch alarm, because it
asserts the *absence* of a supervisor decision. Five CloudWatch alarms plus that
check is the whole set, comfortably inside the free tier of ten.
"""

from __future__ import annotations

from typing import Any

from infra.tests.conftest import resources

EXPECTED = {
    "supervisor-errors",
    "supervisor-silent",
    "agent-error-rate",
    "access-denied",
    "dlq-depth",
}


def test_prod_has_exactly_the_expected_alarms(prod: dict[str, Any]) -> None:
    names = {
        str(a["Properties"]["AlarmName"]).removeprefix("prod-")
        for _s, _l, a in resources(prod, "AWS::CloudWatch::Alarm")
    }
    assert names == EXPECTED


def test_non_prod_stages_have_no_alarms(dev: dict[str, Any]) -> None:
    """Ten free alarms is the whole budget; an ephemeral stage must not spend it."""
    assert resources(dev, "AWS::CloudWatch::Alarm") == []


def test_alarm_count_stays_inside_the_free_tier(prod: dict[str, Any]) -> None:
    assert len(resources(prod, "AWS::CloudWatch::Alarm")) <= 10


def test_every_alarm_notifies_ops(prod: dict[str, Any]) -> None:
    for _s, logical_id, alarm in resources(prod, "AWS::CloudWatch::Alarm"):
        assert alarm["Properties"].get("AlarmActions"), f"{logical_id} notifies nobody"


def test_the_custom_metric_exists_only_on_prod(prod: dict[str, Any], dev: dict[str, Any]) -> None:
    """One custom metric in the whole design (docs/06 §8), and only where alarmed."""
    prod_filters = resources(prod, "AWS::Logs::MetricFilter")
    assert len(prod_filters) == 1
    transform = prod_filters[0][2]["Properties"]["MetricTransformations"][0]
    assert transform["MetricNamespace"] == "Agent"
    assert transform["MetricName"] == "AccessDenied"

    assert resources(dev, "AWS::Logs::MetricFilter") == []
    assert resources(dev, "AWS::CloudTrail::Trail") == []


def test_the_metric_filter_does_not_set_both_dimensions_and_a_default(
    prod: dict[str, Any],
) -> None:
    """CloudWatch rejects the pair, and only at create time.

    This cost a failed first deploy: the template synthesizes, `cdk diff` shows
    nothing wrong, and CloudFormation returns "dimensions and default value are
    mutually exclusive properties" halfway through the foundation stack.
    """
    transform = resources(prod, "AWS::Logs::MetricFilter")[0][2]["Properties"][
        "MetricTransformations"
    ][0]
    assert not ("Dimensions" in transform and "DefaultValue" in transform)


def test_the_access_denied_alarm_reads_the_metric_the_filter_publishes(
    prod: dict[str, Any],
) -> None:
    """A dimension on one side and not the other means an alarm with no data."""
    transform = resources(prod, "AWS::Logs::MetricFilter")[0][2]["Properties"][
        "MetricTransformations"
    ][0]
    published = {d["Key"] for d in transform.get("Dimensions", [])}
    alarms = [
        a
        for _s, _id, a in resources(prod, "AWS::CloudWatch::Alarm")
        if a["Properties"].get("MetricName") == "AccessDenied"
    ]
    assert len(alarms) == 1
    watched = {d["Name"] for d in alarms[0]["Properties"].get("Dimensions", [])}
    assert watched == published


def test_the_trail_is_single_region_and_write_only(prod: dict[str, Any]) -> None:
    """Multi-region or read events would multiply the log volume for no signal."""
    trails = resources(prod, "AWS::CloudTrail::Trail")
    assert len(trails) == 1
    props = trails[0][2]["Properties"]
    assert props["IsMultiRegionTrail"] is False
    selectors = props["EventSelectors"]
    assert all(s["ReadWriteType"] == "WriteOnly" for s in selectors)
    assert all(not s.get("DataResources") for s in selectors), "data events are not free"
