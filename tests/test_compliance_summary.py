"""合规汇总查询测试：每个用例使用独立分区，避免与其他用例数据相互干扰。"""
from __future__ import annotations

import uuid

import pytest

START = "2025-06-01T00:00:00Z"
END = "2025-06-02T00:00:00Z"


@pytest.fixture()
def p(client):
    """每个用例独立的分区（含 3 项必测指标与 2025 年限值）。"""
    code = "PS" + uuid.uuid4().hex[:8]
    client.post("/admin/partitions", json={"code": code, "name": "汇总测试分区"})
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


def _summary(client, partition, start_at=START, end_at=END):
    return client.get(
        f"/partitions/{partition}/compliance-summary",
        params={"start_at": start_at, "end_at": end_at},
    )


def _register(client, p, sampled_at="2025-06-01T08:00:00+08:00"):
    r = client.post("/samples", json={"partition_code": p, "sampled_at": sampled_at})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _readings_publish(client, sid, values, start_version=0):
    """录入读数并发布；values 按任意顺序给出 {indicator: value}，返回发布响应。"""
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

def test_summary_zero_baseline_when_no_reports(client, p):
    r = _summary(client, p)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["partition_code"] == p
    assert body["start_at"] == START
    assert body["end_at"] == END
    assert body["total_reported_samples"] == 0
    assert body["qualified_count"] == 0
    assert body["unqualified_count"] == 0
    # 无报告时按必测指标给出零基线
    assert body["indicator_violations"] == {
        "ph": 0, "residual_chlorine": 0, "turbidity": 0
    }


def test_summary_uses_latest_report_only_and_excludes_unpublished(client, p):
    sid_latest_qual = _register(client, p)  # 先不合格发布，更正后再发布为合格
    _readings_publish(client, sid_latest_qual,
                      {"turbidity": 1.2, "ph": 7.2, "residual_chlorine": 0.8})
    r = client.put(
        f"/samples/{sid_latest_qual}/readings/turbidity",
        json={"indicator_code": "turbidity", "value": 0.3,
              "expected_version": 3, "request_no": f"FIX-{p}-1"},
    )
    assert r.status_code == 200
    r = client.post(f"/samples/{sid_latest_qual}/publish", json={"expected_version": 4})
    assert r.status_code == 201 and r.json()["conclusion"] == "qualified"

    sid_qual = _register(client, p, sampled_at="2025-06-01T12:00:00+08:00")
    _readings_publish(client, sid_qual,
                      {"turbidity": 0.2, "ph": 7.1, "residual_chlorine": 0.8})

    sid_unqual = _register(client, p, sampled_at="2025-06-01T20:00:00+08:00")
    _readings_publish(client, sid_unqual,
                      {"turbidity": 0.5, "ph": 9.0, "residual_chlorine": 0.8})

    # 窗口内登记但从未发布：不计入
    sid_unpublished = _register(client, p, sampled_at="2025-06-01T18:00:00+08:00")
    client.put(
        f"/samples/{sid_unpublished}/readings/ph",
        json={"indicator_code": "ph", "value": 7.2, "expected_version": 0},
    )

    body = _summary(client, p).json()
    # 仅 3 份“最新报告”，未发布样本被排除，旧版本不合格不重复计数
    assert body["total_reported_samples"] == 3
    assert body["qualified_count"] == 2
    assert body["unqualified_count"] == 1
    assert body["indicator_violations"]["ph"] == 1
    assert body["indicator_violations"]["turbidity"] == 0
    assert body["indicator_violations"]["residual_chlorine"] == 0


def test_summary_half_open_window_boundaries_and_timezone(client, p):
    # 左闭：起点时刻的样本计入；右开：终点时刻的样本排除
    sid_in = _register(client, p, sampled_at="2025-06-01T08:00:00+08:00")  # == 窗口起点 UTC
    _readings_publish(client, sid_in,
                      {"turbidity": 0.2, "ph": 7.2, "residual_chlorine": 0.8})

    sid_out = _register(client, p, sampled_at="2025-06-02T08:00:00+08:00")  # == 窗口终点 UTC
    _readings_publish(client, sid_out,
                      {"turbidity": 0.2, "ph": 7.2, "residual_chlorine": 0.8})

    # 同一 UTC 时刻用 +08:00 偏移描述窗口，结果应一致
    r = _summary(client, p,
                 start_at="2025-06-01T08:00:00+08:00",
                 end_at="2025-06-02T08:00:00+08:00")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["start_at"] == START and body["end_at"] == END
    assert body["total_reported_samples"] == 1

    # 终点再放宽一天，右边界样本进入
    r2 = _summary(client, p, end_at="2025-06-03T00:00:00Z")
    assert r2.json()["total_reported_samples"] == 2

    # 起点晚于所有样本 -> 空
    r3 = _summary(client, p, start_at="2025-06-01T01:00:00Z")
    assert r3.json()["total_reported_samples"] == 0


def test_summary_partition_scoping(client, p):
    # 本分区有报告，但按其他分区查询不到；取一个没有任何样本的远期窗口查 P1
    sid = _register(client, p)
    _readings_publish(client, sid,
                      {"turbidity": 0.2, "ph": 7.2, "residual_chlorine": 0.8})
    other = _summary(client, "P1",
                     start_at="2030-06-01T00:00:00Z", end_at="2030-06-02T00:00:00Z")
    assert other.status_code == 200
    obody = other.json()
    assert obody["partition_code"] == "P1"
    assert obody["total_reported_samples"] == 0
    assert obody["qualified_count"] == 0 and obody["unqualified_count"] == 0

    # 本分区窗口仍能查到自己的 1 份
    assert _summary(client, p).json()["total_reported_samples"] == 1


def test_summary_multiple_indicator_violations_counted(client, p):
    sid = _register(client, p, sampled_at="2025-06-01T10:00:00+08:00")
    _readings_publish(client, sid,
                      {"turbidity": 1.0, "ph": 9.0, "residual_chlorine": 0.01})
    body = _summary(client, p).json()
    assert body["total_reported_samples"] == 1
    assert body["qualified_count"] == 0 and body["unqualified_count"] == 1
    assert body["indicator_violations"] == {
        "ph": 1, "residual_chlorine": 1, "turbidity": 1
    }


def test_summary_errors(client, p):
    # start >= end -> 统一错误信封 422 invalid_time_range
    r = _summary(client, p, start_at=END, end_at=START)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_time_range"
    assert r.json()["error"]["details"]["start_at"] == END
    assert r.json()["error"]["details"]["end_at"] == START

    # 起止相等同样拒绝
    r = _summary(client, p, start_at=START, end_at=START)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_time_range"

    # 不带时区 -> 422（框架校验，统一错误信封）
    r = client.get(f"/partitions/{p}/compliance-summary",
                   params={"start_at": "2025-06-01T08:00:00", "end_at": END})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"

    # 缺参数 -> 422 validation_error
    r = client.get(f"/partitions/{p}/compliance-summary", params={"start_at": START})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"

    # 分区不存在 -> 统一错误信封 404 partition_not_found
    r = _summary(client, "NOPE")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "partition_not_found"
