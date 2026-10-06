"""限值调整影响预览测试：每个用例使用独立分区，避免数据相互干扰。"""
from __future__ import annotations

import uuid

import pytest

START = "2025-06-01T00:00:00Z"
END = "2025-06-02T00:00:00Z"


@pytest.fixture()
def p(client):
    """每个用例独立的分区（含 3 项必测指标与 2025 年限值）。"""
    code = "PV" + uuid.uuid4().hex[:8]
    client.post("/admin/partitions", json={"code": code, "name": "预览测试分区"})
    for ind in ("turbidity", "ph", "residual_chlorine"):
        r = client.post(f"/admin/partitions/{code}/mandatory-indicators",
                        json={"indicator_code": ind})
        assert r.status_code == 201, r.text
    for ind, mn, mx in [
        ("turbidity", None, 1.0),
        ("ph", 6.5, 8.5),
        ("residual_chlorine", 0.05, 4.0),
    ]:
        r = client.post(
            f"/admin/partitions/{code}/limits",
            json={"indicator_code": ind, "min_value": mn, "max_value": mx,
                  "valid_from": "2025-01-01T00:00:00Z", "valid_to": "2026-01-01T00:00:00Z"},
        )
        assert r.status_code == 201, r.text
    return code


def _preview(client, partition, indicator="turbidity", start_at=START, end_at=END,
             min_value=None, max_value=None):
    body = {"indicator_code": indicator, "start_at": start_at, "end_at": end_at}
    if min_value is not None:
        body["min_value"] = min_value
    if max_value is not None:
        body["max_value"] = max_value
    return client.post(f"/partitions/{partition}/limit-change-preview", json=body)


def _register(client, p, sampled_at="2025-06-01T08:00:00+08:00"):
    r = client.post("/samples", json={"partition_code": p, "sampled_at": sampled_at})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _readings_publish(client, sid, values, start_version=0):
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


# ---------------------------------------------------------------- 用例

def test_preview_no_reports_zero_counts_empty_lists(client, p):
    r = _preview(client, p, max_value=1.0)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["evaluable_samples"] == 0
    assert body["not_evaluable_samples"] == []
    assert body["original_qualified_count"] == 0
    assert body["original_unqualified_count"] == 0
    assert body["simulated_qualified_count"] == 0
    assert body["simulated_unqualified_count"] == 0
    assert body["changed_samples"] == []
    assert body["proposed_limit"] == {"min_value": None, "max_value": 1.0}
    assert body["start_at"] == START and body["end_at"] == END


def test_preview_relaxing_upper_bound_flips_unqualified_to_qualified(client, p):
    # 浑浊度 1.2 在原上限 <1.0 下不合格；拟议放宽到 <2.0 后目标指标合格
    sid = _register(client, p)
    _readings_publish(client, sid,
                      {"turbidity": 1.2, "ph": 7.2, "residual_chlorine": 0.8})

    body = _preview(client, p, max_value=2.0).json()
    assert body["evaluable_samples"] == 1
    assert body["not_evaluable_samples"] == []
    assert body["original_qualified_count"] == 0
    assert body["original_unqualified_count"] == 1
    assert body["simulated_qualified_count"] == 1
    assert body["simulated_unqualified_count"] == 0
    assert len(body["changed_samples"]) == 1
    ch = body["changed_samples"][0]
    assert ch["sample_id"] == sid
    assert ch["original_conclusion"] == "unqualified"
    assert ch["simulated_conclusion"] == "qualified"
    assert ch["frozen_value"] == 1.2
    assert ch["target_indicator"]["in_range_original"] is False
    assert ch["target_indicator"]["in_range_simulated"] is True
    assert ch["target_indicator"]["simulated_violations"] == []


def test_preview_other_indicator_violation_blocks_flip(client, p):
    # 目标浑浊度放宽可合格，但 ph 原快照超限 -> 模拟结论仍不合格，不计变化
    sid = _register(client, p)
    _readings_publish(client, sid,
                      {"turbidity": 1.2, "ph": 9.0, "residual_chlorine": 0.8})

    body = _preview(client, p, max_value=2.0).json()
    assert body["evaluable_samples"] == 1
    assert body["original_qualified_count"] == 0
    assert body["simulated_qualified_count"] == 0
    assert body["simulated_unqualified_count"] == 1
    assert body["changed_samples"] == []


def test_preview_tightening_upper_bound_flips_qualified_to_unqualified(client, p):
    sid = _register(client, p, sampled_at="2025-06-01T12:00:00+08:00")
    _readings_publish(client, sid,
                      {"turbidity": 0.8, "ph": 7.2, "residual_chlorine": 0.8})

    body = _preview(client, p, max_value=0.5).json()
    assert body["evaluable_samples"] == 1
    assert body["original_qualified_count"] == 1
    assert body["simulated_qualified_count"] == 0
    assert len(body["changed_samples"]) == 1
    ch = body["changed_samples"][0]
    assert ch["original_conclusion"] == "qualified"
    assert ch["simulated_conclusion"] == "unqualified"
    assert ch["target_indicator"]["in_range_original"] is True
    assert ch["target_indicator"]["in_range_simulated"] is False
    assert ch["target_indicator"]["simulated_violations"]


def test_preview_max_exclusive_boundary(client, p):
    # 冻结值恰好等于拟议上限：上限开端，仍不合格 -> 原合格变不合格
    sid = _register(client, p)
    _readings_publish(client, sid,
                      {"turbidity": 0.9, "ph": 7.2, "residual_chlorine": 0.8})
    body = _preview(client, p, max_value=0.9).json()
    assert body["simulated_unqualified_count"] == 1
    assert len(body["changed_samples"]) == 1


def test_preview_min_inclusive_and_dual_bounds(client, p):
    # 游离余氯：拟议 [0.5, 4.0)；0.5 恰好等于下限（包含）应合格
    sid_ok = _register(client, p, sampled_at="2025-06-01T09:00:00+08:00")
    _readings_publish(client, sid_ok,
                      {"turbidity": 0.2, "ph": 7.2, "residual_chlorine": 0.5})
    sid_bad = _register(client, p, sampled_at="2025-06-01T10:00:00+08:00")
    _readings_publish(client, sid_bad,
                      {"turbidity": 0.2, "ph": 7.2, "residual_chlorine": 0.1})

    body = _preview(client, p, indicator="residual_chlorine",
                    min_value=0.5, max_value=4.0).json()
    assert body["evaluable_samples"] == 2
    assert body["original_qualified_count"] == 2
    assert body["simulated_qualified_count"] == 1
    changed_ids = {c["sample_id"] for c in body["changed_samples"]}
    assert changed_ids == {sid_bad}


def test_preview_uses_latest_frozen_report_and_ignores_current_reading(client, p):
    # 发布为不合格后更正当前读数但不重新发布；预览必须采用已发布的冻结快照（1.2）
    sid = _register(client, p)
    _readings_publish(client, sid,
                      {"turbidity": 1.2, "ph": 7.2, "residual_chlorine": 0.8})
    r = client.put(
        f"/samples/{sid}/readings/turbidity",
        json={"indicator_code": "turbidity", "value": 0.3,
              "expected_version": 3, "request_no": f"PREVIEW-FIX-{p}"},
    )
    assert r.status_code == 200  # 当前读数已变为 0.3，但未发布

    # 放宽到 <2.0：冻结值 1.2 合格 -> 结论翻转（若误用当前值 0.3 则本来就合格）
    body = _preview(client, p, max_value=2.0).json()
    assert body["evaluable_samples"] == 1
    assert body["changed_samples"][0]["frozen_value"] == 1.2
    assert body["original_unqualified_count"] == 1

    # 重新发布后，最新冻结快照变为合格值 0.3，不再翻转
    r = client.post(f"/samples/{sid}/publish", json={"expected_version": 4})
    assert r.status_code == 201
    body2 = _preview(client, p, max_value=2.0).json()
    assert body2["evaluable_samples"] == 1
    assert body2["original_qualified_count"] == 1
    assert body2["changed_samples"] == []


def test_preview_not_evaluable_when_report_lacks_indicator(client, p):
    # 分区新增必测指标 iron（无限值 -> 无法发布）；构造历史发布时它还不是必测项：
    # 先发布三指标报告，再把 iron 加为必测，预览 iron 时该报告列为不可评估。
    sid = _register(client, p)
    _readings_publish(client, sid,
                      {"turbidity": 0.2, "ph": 7.2, "residual_chlorine": 0.8})
    r = client.post("/admin/indicators",
                    json={"code": f"iron_{p}", "name": "铁", "unit": "mg/L"})
    assert r.status_code in (201, 409)
    r = client.post(f"/admin/partitions/{p}/mandatory-indicators",
                    json={"indicator_code": f"iron_{p}"})
    assert r.status_code == 201, r.text

    body = _preview(client, p, indicator=f"iron_{p}", max_value=0.3).json()
    assert body["evaluable_samples"] == 0
    assert body["original_qualified_count"] == 0
    assert body["simulated_qualified_count"] == 0
    assert len(body["not_evaluable_samples"]) == 1
    ne = body["not_evaluable_samples"][0]
    assert ne["sample_id"] == sid
    assert ne["reason"] == "indicator_not_in_report"
    assert "report_id" in ne and "sampled_at" in ne
    assert body["changed_samples"] == []


def test_preview_half_open_window_boundaries_and_timezone(client, p):
    sid_in = _register(client, p, sampled_at="2025-06-01T08:00:00+08:00")   # == 起点 UTC
    _readings_publish(client, sid_in,
                      {"turbidity": 0.8, "ph": 7.2, "residual_chlorine": 0.8})
    sid_out = _register(client, p, sampled_at="2025-06-02T08:00:00+08:00")  # == 终点 UTC
    _readings_publish(client, sid_out,
                      {"turbidity": 0.8, "ph": 7.2, "residual_chlorine": 0.8})

    r = _preview(client, p, max_value=0.5,
                 start_at="2025-06-01T08:00:00+08:00",
                 end_at="2025-06-02T08:00:00+08:00")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["start_at"] == START and body["end_at"] == END
    assert body["evaluable_samples"] == 1
    assert body["changed_samples"][0]["sample_id"] == sid_in

    # 右边界放宽一天 -> 两份都进入
    body2 = _preview(client, p, max_value=0.5, end_at="2025-06-03T00:00:00Z").json()
    assert body2["evaluable_samples"] == 2

    # 未发布样本不计入（窗口内登记但不发布）
    _register(client, p, sampled_at="2025-06-01T18:00:00+08:00")
    body3 = _preview(client, p, max_value=0.5).json()
    assert body3["evaluable_samples"] == 1


def test_preview_unpublished_samples_ignored(client, p):
    sid = _register(client, p)
    client.put(f"/samples/{sid}/readings/turbidity",
               json={"indicator_code": "turbidity", "value": 0.8, "expected_version": 0})
    body = _preview(client, p, max_value=0.5).json()
    assert body["evaluable_samples"] == 0
    assert body["not_evaluable_samples"] == []
    assert body["changed_samples"] == []


def test_preview_single_lower_bound(client, p):
    # 仅给下限（上限不设）：原 ph [6.5,8.5)，拟议 ph >= 7.0
    sid = _register(client, p)
    _readings_publish(client, sid,
                      {"turbidity": 0.2, "ph": 6.8, "residual_chlorine": 0.8})
    body = _preview(client, p, indicator="ph", min_value=7.0).json()
    assert body["proposed_limit"] == {"min_value": 7.0, "max_value": None}
    assert body["evaluable_samples"] == 1
    assert body["simulated_unqualified_count"] == 1
    assert len(body["changed_samples"]) == 1


def test_preview_zero_bound_allowed(client, p):
    sid = _register(client, p)
    _readings_publish(client, sid,
                      {"turbidity": 0.0, "ph": 7.2, "residual_chlorine": 0.8})
    r = _preview(client, p, min_value=0.0, max_value=1.0)
    assert r.status_code == 200, r.text
    assert r.json()["simulated_qualified_count"] == 1


# ---------------------------------------------------------------- 拒绝路径

def test_preview_errors(client, p):
    base = {"indicator_code": "turbidity", "start_at": START, "end_at": END,
            "max_value": 1.0}

    def post(partition=p, **over):
        body = {**base, **over}
        return client.post(f"/partitions/{partition}/limit-change-preview", json=body)

    # 分区不存在
    r = client.post("/partitions/NOPE/limit-change-preview", json=base)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "partition_not_found"

    # 指标不存在
    r = post(indicator_code="nope_indicator")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "indicator_not_found"

    # 指标非该分区必测项（ph 是存在的全局指标）
    other = "PX" + uuid.uuid4().hex[:8]
    rr = client.post("/admin/partitions", json={"code": other, "name": "空分区"})
    assert rr.status_code == 201
    r = post(partition=other)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "not_mandatory_indicator"

    # start >= end / 相等 -> invalid_time_range
    r = post(start_at=END, end_at=START)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_time_range"
    r = post(start_at=START, end_at=START)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_time_range"

    # 时间不带时区 -> validation_error
    r = post(start_at="2025-06-01T08:00:00")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"

    # 上下界都不给 -> invalid_limit
    r = client.post(
        f"/partitions/{p}/limit-change-preview",
        json={"indicator_code": "turbidity", "start_at": START, "end_at": END},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_limit"

    # 双界 min >= max -> invalid_limit
    r = post(min_value=1.0, max_value=1.0)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_limit"
    r = post(min_value=2.0, max_value=1.0)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_limit"

    # 负数 / 非有限值
    r = post(max_value=-0.1)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"
    r = post(max_value="inf")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"

    # 缺必填字段
    r = client.post(
        f"/partitions/{p}/limit-change-preview",
        json={"start_at": START, "end_at": END, "max_value": 1.0},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"

    # 多余字段 -> validation_error（StrictModel extra=forbid）
    r = post(unexpected=123)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"
