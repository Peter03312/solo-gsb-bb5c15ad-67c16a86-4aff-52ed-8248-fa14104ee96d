"""限值调整影响预览测试：只读模拟重判，每用例使用独立分区避免干扰。"""
from __future__ import annotations

import uuid

import pytest

START = "2025-06-01T00:00:00Z"
END = "2025-06-02T00:00:00Z"
WIDE_LIMITS = {
    "turbidity": (None, 1.0),
    "ph": (6.5, 8.5),
    "residual_chlorine": (0.05, 4.0),
}


@pytest.fixture()
def p(client):
    """独立分区：3 项必测指标 + 2025 年生效限值。"""
    code = "PV" + uuid.uuid4().hex[:8]
    client.post("/admin/partitions", json={"code": code, "name": "预览测试分区"})
    for ind in ("turbidity", "ph", "residual_chlorine"):
        r = client.post(f"/admin/partitions/{code}/mandatory-indicators",
                        json={"indicator_code": ind})
        assert r.status_code == 201, r.text
    for ind, (mn, mx) in WIDE_LIMITS.items():
        r = client.post(
            f"/admin/partitions/{code}/limits",
            json={"indicator_code": ind, "min_value": mn, "max_value": mx,
                  "valid_from": "2025-01-01T00:00:00Z", "valid_to": "2026-01-01T00:00:00Z"},
        )
        assert r.status_code == 201, r.text
    return code


def _preview(client, partition, indicator="turbidity", min_value=None, max_value=1.0,
             start_at=START, end_at=END):
    return client.post(
        f"/partitions/{partition}/limit-change-preview",
        json={"indicator_code": indicator, "start_at": start_at, "end_at": end_at,
              "min_value": min_value, "max_value": max_value},
    )


def _register(client, p, sampled_at="2025-06-01T08:00:00+08:00"):
    r = client.post("/samples", json={"partition_code": p, "sampled_at": sampled_at})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _publish(client, sid, values, start_version=0):
    """顺序录入读数并发布，返回 (发布响应, 最终版本)。"""
    version = start_version
    for indicator, value in values.items():
        r = client.put(
            f"/samples/{sid}/readings/{indicator}",
            json={"indicator_code": indicator, "value": value, "expected_version": version},
        )
        assert r.status_code == 200, r.text
        version = r.json()["reading_version"]
    r = client.post(f"/samples/{sid}/publish", json={"expected_version": version})
    assert r.status_code == 201, r.text
    return r.json(), version


def _by_sample(body):
    return {c["sample_id"]: c for c in body["changed_samples"]}


# ---------------------------------------------------------------- 用例

def test_preview_no_published_samples_zero_and_empty(client, p):
    # 窗口内登记但未发布 -> 与无数据相同：零计数、空列表
    sid = _register(client, p)
    client.put(f"/samples/{sid}/readings/ph",
               json={"indicator_code": "ph", "value": 7.2, "expected_version": 0})

    r = _preview(client, p, max_value=2.0,
                 start_at="2030-01-01T00:00:00Z", end_at="2030-01-02T00:00:00Z")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_reported_samples"] == 0
    assert body["evaluable_sample_count"] == 0
    assert body["unevaluable_samples"] == []
    assert body["original_qualified_count"] == 0
    assert body["original_unqualified_count"] == 0
    assert body["simulated_qualified_count"] == 0
    assert body["simulated_unqualified_count"] == 0
    assert body["changed_samples"] == []
    assert body["proposed_limits"] == {"min_value": None, "max_value": 2.0}


def test_preview_loosen_and_tighten_max_half_open(client, p):
    sid_ok = _register(client, p, sampled_at="2025-06-01T09:00:00+08:00")
    _publish(client, sid_ok, {"turbidity": 0.2, "ph": 7.2, "residual_chlorine": 0.8})
    sid_edge = _register(client, p, sampled_at="2025-06-01T10:00:00+08:00")
    _publish(client, sid_edge, {"turbidity": 1.0, "ph": 7.2, "residual_chlorine": 0.8})

    # 放宽上限到 1.5：1.0 由不合格变合格（上限不包含，仅拟议重判目标指标）
    body = _preview(client, p, max_value=1.5).json()
    assert body["evaluable_sample_count"] == 2
    assert body["original_qualified_count"] == 1
    assert body["original_unqualified_count"] == 1
    assert body["simulated_qualified_count"] == 2
    assert body["simulated_unqualified_count"] == 0
    assert body["unevaluable_samples"] == []
    assert len(body["changed_samples"]) == 1
    ch = _by_sample(body)[sid_edge]
    assert ch["original_conclusion"] == "unqualified"
    assert ch["simulated_conclusion"] == "qualified"
    assert ch["frozen_value"] == 1.0
    assert ch["original_target_in_range"] is False
    assert ch["simulated_target_in_range"] is True
    assert ch["proposed_violations"] == []

    # 收紧上限到 0.1：0.2 由合格变不合格；1.0 原本就不合格
    body = _preview(client, p, max_value=0.1).json()
    assert body["original_qualified_count"] == 1
    assert body["simulated_qualified_count"] == 0
    ch = _by_sample(body)[sid_ok]
    assert ch["original_conclusion"] == "qualified"
    assert ch["simulated_conclusion"] == "unqualified"
    assert ch["simulated_target_in_range"] is False
    assert ch["proposed_violations"]

    # max 开端：读数恰等于拟议上限仍判超限
    body = _preview(client, p, max_value=0.2).json()
    ch = _by_sample(body)[sid_ok]
    assert ch["simulated_conclusion"] == "unqualified"
    assert ch["proposed_violations"]


def test_preview_min_inclusive_boundary(client, p):
    sid_low = _register(client, p, sampled_at="2025-06-01T09:00:00+08:00")
    _publish(client, sid_low, {"turbidity": 0.2, "ph": 7.2, "residual_chlorine": 0.01})
    sid_edge = _register(client, p, sampled_at="2025-06-01T10:00:00+08:00")
    _publish(client, sid_edge, {"turbidity": 0.2, "ph": 7.2, "residual_chlorine": 0.05})

    # 拟议下限 0.01（含）：0.01 合格，0.05 合格 -> 两份全由原 1 合格变 2 合格
    body = _preview(client, p, indicator="residual_chlorine",
                    min_value=0.01, max_value=4.0).json()
    assert body["original_qualified_count"] == 1
    assert body["simulated_qualified_count"] == 2
    ch = _by_sample(body)[sid_low]
    assert (ch["original_conclusion"], ch["simulated_conclusion"]) == (
        "unqualified", "qualified")

    # 仅下限（max 省略）：读数恰等于下限判合格（sid_edge 不翻转）；
    # sid_low 0.01 < 0.05，前后结论均不合格，故两份均不在变化列表中
    body = _preview(client, p, indicator="residual_chlorine",
                    min_value=0.05, max_value=None).json()
    assert body["simulated_qualified_count"] == 1
    assert body["simulated_unqualified_count"] == 1
    assert body["changed_samples"] == []


def test_preview_other_indicator_violation_blocks_flip(client, p):
    # 目标浑浊度原合格，但 ph 9.0 导致报告不合格；放宽浑浊度不能让模拟结论翻为合格
    sid = _register(client, p)
    _publish(client, sid, {"turbidity": 0.2, "ph": 9.0, "residual_chlorine": 0.8})

    body = _preview(client, p, max_value=2.0).json()
    assert body["evaluable_sample_count"] == 1
    assert body["original_qualified_count"] == 0
    assert body["simulated_qualified_count"] == 0
    assert body["changed_samples"] == []

    # 收紧浑浊度（本合格 -> 目标超限），整体仍不合格：结论不变
    body = _preview(client, p, max_value=0.1).json()
    assert body["simulated_unqualified_count"] == 1
    assert body["changed_samples"] == []


def test_preview_report_without_target_indicator_is_unevaluable(client):
    # 分区先只有 2 项必测指标并发布；后追加第 3 项 -> 旧报告不含目标指标，不可评估
    code = "PV" + uuid.uuid4().hex[:8]
    client.post("/admin/partitions", json={"code": code, "name": "不可评估测试分区"})
    for ind in ("turbidity", "ph"):
        assert client.post(f"/admin/partitions/{code}/mandatory-indicators",
                           json={"indicator_code": ind}).status_code == 201
    for ind, mn, mx in [("turbidity", None, 1.0), ("ph", 6.5, 8.5)]:
        assert client.post(
            f"/admin/partitions/{code}/limits",
            json={"indicator_code": ind, "min_value": mn, "max_value": mx,
                  "valid_from": "2025-01-01T00:00:00Z",
                  "valid_to": "2026-01-01T00:00:00Z"}).status_code == 201
    sid = _register(client, code)
    _publish(client, sid, {"turbidity": 1.2, "ph": 7.2})
    assert client.post(f"/admin/partitions/{code}/mandatory-indicators",
                       json={"indicator_code": "residual_chlorine"}).status_code == 201

    body = _preview(client, code, indicator="residual_chlorine",
                    min_value=0.05, max_value=4.0).json()
    assert body["total_reported_samples"] == 1
    assert body["evaluable_sample_count"] == 0
    assert body["original_qualified_count"] == 0
    assert body["simulated_qualified_count"] == 0
    assert len(body["unevaluable_samples"]) == 1
    u = body["unevaluable_samples"][0]
    assert u["sample_id"] == sid
    assert "residual_chlorine" in u["reason"]
    assert body["changed_samples"] == []

    # 同一报告对浑浊度仍可评估（1.2 原不合格，放宽到 2.0 后模拟合格）
    body = _preview(client, code, indicator="turbidity", max_value=2.0).json()
    assert body["evaluable_sample_count"] == 1
    assert body["unevaluable_samples"] == []
    assert body["simulated_qualified_count"] == 1
    assert _by_sample(body)[sid]["simulated_conclusion"] == "qualified"


def test_preview_uses_latest_report_and_frozen_reading(client, p):
    sid = _register(client, p)
    _publish(client, sid, {"turbidity": 1.2, "ph": 7.2, "residual_chlorine": 0.8})
    # 更正后再发布为合格（最新报告冻结值 0.3）
    r = client.put(f"/samples/{sid}/readings/turbidity",
                   json={"indicator_code": "turbidity", "value": 0.3,
                         "expected_version": 3, "request_no": f"PV-FIX-{p}"})
    assert r.status_code == 200
    assert client.post(f"/samples/{sid}/publish",
                       json={"expected_version": 4}).status_code == 201
    # 之后再次更正当前读数（未重新发布）：预览不得读取当前读数，仍用冻结的 0.3
    r = client.put(f"/samples/{sid}/readings/turbidity",
                   json={"indicator_code": "turbidity", "value": 9.9,
                         "expected_version": 4, "request_no": f"PV-FIX2-{p}"})
    assert r.status_code == 200

    body = _preview(client, p, max_value=1.0).json()
    assert body["evaluable_sample_count"] == 1
    assert body["original_qualified_count"] == 1
    assert body["simulated_qualified_count"] == 1
    assert body["changed_samples"] == []

    # 拟议上限 0.2：冻结值 0.3 超限（而非当前值 9.9）
    body = _preview(client, p, max_value=0.2).json()
    ch = _by_sample(body)[sid]
    assert ch["frozen_value"] == 0.3
    assert ch["simulated_conclusion"] == "unqualified"


def test_preview_half_open_window_boundaries(client, p):
    sid_in = _register(client, p, sampled_at="2025-06-01T08:00:00+08:00")
    _publish(client, sid_in, {"turbidity": 0.2, "ph": 7.2, "residual_chlorine": 0.8})
    sid_out = _register(client, p, sampled_at="2025-06-02T08:00:00+08:00")
    _publish(client, sid_out, {"turbidity": 0.2, "ph": 7.2, "residual_chlorine": 0.8})

    # +08:00 窗口与 UTC 常量等价；左闭右开
    r = _preview(client, p, max_value=2.0,
                 start_at="2025-06-01T08:00:00+08:00",
                 end_at="2025-06-02T08:00:00+08:00")
    body = r.json()
    assert body["start_at"] == START and body["end_at"] == END
    assert body["evaluable_sample_count"] == 1
    assert body["changed_samples"] == []  # 0.2 在 2.0 内

    # 终点放宽一天，右边界样本进入（0.2 对 2.0 均合格，无变化样本）
    body = _preview(client, p, max_value=2.0,
                    start_at=START, end_at="2025-06-03T00:00:00Z").json()
    assert body["evaluable_sample_count"] == 2


def test_preview_is_read_only(client, p):
    sid = _register(client, p)
    report, _ = _publish(
        client, sid, {"turbidity": 1.0, "ph": 7.2, "residual_chlorine": 0.8})
    assert report["conclusion"] == "unqualified"

    # 多次不同拟议限值预览
    for mx in (0.5, 1.0, 2.0, None):
        assert _preview(
            client, p, max_value=mx,
            **({"min_value": None} if mx is not None else {"min_value": 0.0})
        ).status_code == 200

    # 历史报告结论/限值未被改动
    r = client.get(f"/samples/{sid}/reports/{report['report_id']}")
    snap = r.json()
    assert snap["conclusion"] == "unqualified"
    target = next(i for i in snap["items"] if i["indicator_code"] == "turbidity")
    assert target["limit_basis"]["max_value"] == 1.0
    # 当前读数版本也未被预览改变
    assert client.get(f"/samples/{sid}").json()["reading_version"] == 3


def test_preview_errors(client, p):
    base = {"indicator_code": "turbidity", "start_at": START, "end_at": END}

    def post(partition=p, **overrides):
        payload = {**base, "min_value": None, "max_value": 1.0, **overrides}
        return client.post(f"/partitions/{partition}/limit-change-preview", json=payload)

    # 双界皆空 -> 422 invalid_limit
    r = post(min_value=None, max_value=None)
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_limit"

    # min >= max -> 422 invalid_limit
    r = post(min_value=1.0, max_value=1.0)
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_limit"
    r = post(min_value=2.0, max_value=1.0)
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_limit"

    # 负限值 -> 422（模型层 validation_error）
    r = post(min_value=-0.1)
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"

    # 时间不带时区 -> 422 validation_error
    r = post(start_at="2025-06-01T08:00:00")
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"

    # start >= end -> 422 invalid_time_range
    r = post(start_at=END, end_at=START)
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_time_range"

    # 未知字段 -> 422 validation_error
    r = post(unexpected=1)
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"

    # 分区不存在 -> 404 partition_not_found
    r = post(partition="NOPE")
    assert r.status_code == 404 and r.json()["error"]["code"] == "partition_not_found"

    # 指标不存在 -> 404 indicator_not_found
    r = post(indicator_code="nope-indicator")
    assert r.status_code == 404 and r.json()["error"]["code"] == "indicator_not_found"

    # 指标存在但非该分区必测项 -> 422 not_mandatory_indicator
    ind = "iv" + uuid.uuid4().hex[:8]
    assert client.post("/admin/indicators",
                       json={"code": ind, "name": "临时指标", "unit": "-"}).status_code == 201
    r = post(indicator_code=ind)
    assert r.status_code == 422 and r.json()["error"]["code"] == "not_mandatory_indicator"
