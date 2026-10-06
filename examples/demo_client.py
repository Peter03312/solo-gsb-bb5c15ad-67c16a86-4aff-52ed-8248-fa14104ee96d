#!/usr/bin/env python3
"""自来水厂取样合规复核服务 —— Python 调用示例（仅标准库，无需 pip 安装）。

流程覆盖：
  登记样本 -> 录入读数 -> 版本冲突 -> 更正(请求号/重放/同号异内容)
  -> 首次发布(不合格快照) -> 重复发布拒绝 -> 更正后再发布(新版本合格, 旧版可查)
  -> 读数不齐/无生效限值门禁
  -> 按分区 + 采样时刻窗口的合规汇总（左闭右开、仅最新已发布报告、按指标超限次数）
  -> 限值调整影响预览（拟议非负上下限只读重判目标指标、合成模拟结论、
     不可评估样本、前后合格数与结论变化清单）。

用法：
    python examples/demo_client.py            # 默认 http://localhost:8080
    BASE=http://127.0.0.1:8080 python examples/demo_client.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = os.environ.get("BASE", "http://localhost:8080").rstrip("/")

# 每次运行使用唯一请求号前缀，保证脚本对同一服务重复运行不会撞到历史幂等记录
RUN_TAG = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")


def call(method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode() or "null")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def step(title: str) -> None:
    print(f"\n== {title} " + "=" * max(4, 60 - len(title)))


def expect(status: int, got: int, label: str) -> None:
    flag = "OK " if got == status else "FAIL"
    print(f"  [{flag}] {label}: HTTP {got} (expect {status})")
    if got != status:
        sys.exit(1)


def iso(dt: datetime) -> str:
    """UTC datetime -> '2025-06-01T00:00:00Z'。"""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def publish_complete(sid: str, values: dict[str, float]) -> None:
    """从版本 0 顺序录入三项读数并发布（演示辅助）。"""
    version = 0
    for indicator, value in values.items():
        s, _ = call("PUT", f"/samples/{sid}/readings/{indicator}",
                    {"indicator_code": indicator, "value": value, "expected_version": version})
        assert s == 200, (s, indicator)
        version += 1
    s, _ = call("POST", f"/samples/{sid}/publish", {"expected_version": version})
    assert s == 201, s


def main() -> None:
    step("健康检查")
    print(" ", call("GET", "/health"))

    step("登记样本（P1，采样时刻 +08:00，服务端归一 UTC）")
    s, sample = call("POST", "/samples",
                     {"partition_code": "P1", "sampled_at": "2025-06-01T08:00:00+08:00"})
    expect(201, s, "register")
    sid = sample["id"]
    print("  sample:", sid, "sampled_at(UTC):", sample["sampled_at"], "version:", sample["reading_version"])

    step("录入 3 项必测读数（浑浊度 1.2 将超 1.0 上限；上限为开端）")
    plan = [("turbidity", 1.2), ("ph", 7.2), ("residual_chlorine", 0.8)]
    version = 0
    for indicator, value in plan:
        s, r = call("PUT", f"/samples/{sid}/readings/{indicator}",
                    {"indicator_code": indicator, "value": value, "expected_version": version})
        expect(200, s, f"entry {indicator}")
        print("  ", r)
        version = r["reading_version"]

    step("用过期版本写入 -> 409 version_conflict")
    s, r = call("PUT", f"/samples/{sid}/readings/ph",
                {"indicator_code": "ph", "value": 7.9, "expected_version": 0})
    expect(409, s, "stale version")
    print("  current_version returned:", r["error"]["details"]["current_version"])

    step("更正已有读数但不带请求号 -> 422")
    s, r = call("PUT", f"/samples/{sid}/readings/turbidity",
                {"indicator_code": "turbidity", "value": 0.5, "expected_version": version})
    expect(422, s, "correction without request_no")
    print("  code:", r["error"]["code"])

    step("带唯一请求号更正浑浊度 -> 版本 +1")
    payload = {"indicator_code": "turbidity", "value": 0.5,
               "expected_version": version, "request_no": f"REQ-42-{RUN_TAG}"}
    s, first = call("PUT", f"/samples/{sid}/readings/turbidity", payload)
    expect(200, s, "correction")
    print("  ", first)
    version = first["reading_version"]

    step("同号重放 -> 原结果，版本不再增加")
    s, replay = call("PUT", f"/samples/{sid}/readings/turbidity", payload)
    expect(200, s, "replay")
    assert replay == first, (first, replay)
    print("   replay result identical:", replay)

    step("同号异内容（值不同） -> 422 idempotency_key_conflict")
    s, r = call("PUT", f"/samples/{sid}/readings/turbidity",
                {**payload, "value": 0.9})
    expect(422, s, "same request_no different payload")
    print("  code:", r["error"]["code"])

    step("发布报告（当前浑浊度 0.5 合格）")
    s, report = call("POST", f"/samples/{sid}/publish", {"expected_version": version})
    expect(201, s, "publish v4 qualified")
    assert report["conclusion"] == "qualified"
    print("  conclusion:", report["conclusion"], "| items:",
          [(i["indicator_code"], i["value"], i["in_range"],
            (i["limit_basis"]["min_value"], i["limit_basis"]["max_value"]))
           for i in report["items"]])
    report_id = report["report_id"]

    step("同一读数版本重复发布 -> 409 already_published")
    s, r = call("POST", f"/samples/{sid}/publish", {"expected_version": version})
    expect(409, s, "duplicate publish")
    print("  code:", r["error"]["code"])

    step("再更正为超限值 1.0（max 开端，等于上限即超限）并发布 -> 新版本不合格")
    s, corr = call("PUT", f"/samples/{sid}/readings/turbidity",
                   {"indicator_code": "turbidity", "value": 1.0,
                    "expected_version": version, "request_no": f"REQ-43-{RUN_TAG}"})
    expect(200, s, "second correction")
    version = corr["reading_version"]
    s, report2 = call("POST", f"/samples/{sid}/publish", {"expected_version": version})
    expect(201, s, "publish new version")
    assert report2["conclusion"] == "unqualified"
    print("  conclusion:", report2["conclusion"], "| violations:", report2["violations"])

    step("报告列表：两个读数版本并存，旧快照不被覆盖")
    s, listing = call("GET", f"/samples/{sid}/reports")
    expect(200, s, "list reports")
    for item in listing["reports"]:
        print("  ", item)
    s, old = call("GET", f"/samples/{sid}/reports/{report_id}")
    expect(200, s, "fetch old report")
    assert old["conclusion"] == "qualified" and old["reading_version"] == 4
    print(f"  old report v{old['reading_version']} still:", old["conclusion"])

    step("门禁：读数不齐 -> 422 readings_incomplete")
    _, bad = call("POST", "/samples", {"partition_code": "P1", "sampled_at": "2025-06-01T00:00:00Z"})
    call("PUT", f"/samples/{bad['id']}/readings/ph",
         {"indicator_code": "ph", "value": 7.2, "expected_version": 0})
    s, r = call("POST", f"/samples/{bad['id']}/publish", {"expected_version": 1})
    expect(422, s, "incomplete readings")
    print("  missing:", r["error"]["details"]["missing_indicators"])

    step("门禁：采样时刻无生效限值 -> 422 no_effective_limit")
    _, nolim = call("POST", "/samples",
                    {"partition_code": "P1", "sampled_at": "2024-12-31T16:00:00Z"})
    for i, (indicator, value) in enumerate(plan):
        call("PUT", f"/samples/{nolim['id']}/readings/{indicator}",
             {"indicator_code": indicator, "value": value, "expected_version": i})
    s, r = call("POST", f"/samples/{nolim['id']}/publish", {"expected_version": 3})
    expect(422, s, "no effective limit")
    print("  indicators without limit:", r["error"]["details"]["indicators_without_limit"])

    step("区间维护：重叠区间被拒绝 / 端点相接允许")
    s, r = call("POST", "/admin/partitions/P1/limits",
                {"indicator_code": "turbidity", "max_value": 2.0,
                 "valid_from": "2025-06-01T00:00:00Z", "valid_to": "2025-07-01T00:00:00Z"})
    expect(422, s, "overlap rejected")
    print("  code:", r["error"]["code"])
    s, r = call("POST", "/admin/partitions/P2/limits",
                {"indicator_code": "residual_chlorine", "min_value": 0.03, "max_value": 4.0,
                 "valid_from": "2026-01-01T00:00:00Z", "valid_to": "2027-01-01T00:00:00Z"})
    # 首次运行 201；对同一服务重复运行时该限值已存在 -> 422（重叠），同样说明规则生效
    assert s in (201, 422), s
    print(f"  [OK ] adjacent interval: HTTP {s} (201 created / 422 already exists on rerun)")

    # -------------------------------------------------------- 合规汇总
    # 每次运行使用独立分区（分区码含运行时间戳），汇总结果不受历史运行数据影响
    pcode = "PD" + datetime.now(timezone.utc).strftime("%H%M%S%f")[:12]
    step(f"合规汇总准备：本次运行专用分区 {pcode}（含 3 项必测指标与宽时限值）")
    for method, path, body in [
        ("POST", "/admin/partitions", {"code": pcode, "name": "汇总演示分区"}),
        (f"POST", f"/admin/partitions/{pcode}/mandatory-indicators", {"indicator_code": "turbidity"}),
        ("POST", f"/admin/partitions/{pcode}/mandatory-indicators", {"indicator_code": "ph"}),
        ("POST", f"/admin/partitions/{pcode}/mandatory-indicators",
         {"indicator_code": "residual_chlorine"}),
    ]:
        s, r = call(method, path, body)
        assert s == 201, (s, r)
    for indicator, mn, mx in [
        ("turbidity", None, 1.0),
        ("ph", 6.5, 8.5),
        ("residual_chlorine", None, 4.0),
    ]:
        s, r = call("POST", f"/admin/partitions/{pcode}/limits",
                    {"indicator_code": indicator, "min_value": mn, "max_value": mx,
                     "valid_from": "2020-01-01T00:00:00Z", "valid_to": "2030-01-01T00:00:00Z"})
        assert s == 201, (s, r)
    print("  partition ready (3 mandatory indicators, one wide limit each)")

    # 以当前 UTC 时间锚定一个 [anchor, anchor+1h) 的窗口
    anchor = datetime.now(timezone.utc).replace(microsecond=0)
    win_from, win_to = anchor, anchor + timedelta(hours=1)

    def register_publish(offset_min: int, values: dict[str, float]):
        s, sample = call("POST", "/samples",
                         {"partition_code": pcode,
                          "sampled_at": iso(anchor + timedelta(minutes=offset_min))})
        assert s == 201, (s, sample)
        publish_complete(sample["id"], values)
        return sample["id"]

    step("窗口内造数：合格 1 + 不合格 1（ph 超限）+ 边界外 1 + 窗口内未发布 1")
    register_publish(5, {"turbidity": 0.2, "ph": 7.2, "residual_chlorine": 0.8})
    sid_bad = register_publish(10, {"turbidity": 0.4, "ph": 9.0, "residual_chlorine": 0.8})
    register_publish(65, {"turbidity": 0.2, "ph": 7.2, "residual_chlorine": 0.8})  # == 窗口终点，排除
    s, unpublished = call("POST", "/samples",
                          {"partition_code": pcode, "sampled_at": iso(anchor + timedelta(minutes=20))})
    assert s == 201

    step("汇总查询（未发布不计入；窗口左闭右开）-> total=2 / 合格1 / 不合格1 / ph 超限1")
    qs = urllib.parse.urlencode({"start_at": iso(win_from), "end_at": iso(win_to)})
    s, summary = call("GET", f"/partitions/{pcode}/compliance-summary?{qs}")
    expect(200, s, "summary")
    print(" ", json.dumps(summary, ensure_ascii=False))
    assert summary["partition_code"] == pcode
    assert summary["total_reported_samples"] == 2
    assert summary["qualified_count"] == 1 and summary["unqualified_count"] == 1
    assert summary["indicator_violations"]["ph"] == 1
    assert summary["indicator_violations"]["turbidity"] == 0

    step("仅最新已发布报告计入：更正 ph 并重新发布为合格 -> 汇总变为 2 份全部合格")
    s, corr = call("PUT", f"/samples/{sid_bad}/readings/ph",
                   {"indicator_code": "ph", "value": 7.0,
                    "expected_version": 3, "request_no": f"DEMO-FIX-{RUN_TAG}"})
    expect(200, s, "correction")
    s, _ = call("POST", f"/samples/{sid_bad}/publish",
                {"expected_version": corr["reading_version"]})
    expect(201, s, "republish")
    s, summary = call("GET", f"/partitions/{pcode}/compliance-summary?{qs}")
    assert s == 200
    print(" ", json.dumps(summary, ensure_ascii=False))
    assert summary["total_reported_samples"] == 2
    assert summary["qualified_count"] == 2 and summary["unqualified_count"] == 0
    assert all(v == 0 for v in summary["indicator_violations"].values())

    step("无报告窗口：各计数为零，按必测指标给零基线")
    empty_qs = urllib.parse.urlencode({
        "start_at": "2024-01-01T00:00:00Z", "end_at": "2024-02-01T00:00:00Z"})
    s, summary = call("GET", f"/partitions/{pcode}/compliance-summary?{empty_qs}")
    expect(200, s, "empty window")
    print(" ", summary)
    assert summary["total_reported_samples"] == 0
    assert summary["indicator_violations"] == {
        "ph": 0, "residual_chlorine": 0, "turbidity": 0}

    step("汇总错误：start_at 不早于 end_at -> 422 invalid_time_range")
    bad_qs = urllib.parse.urlencode({
        "start_at": iso(win_to), "end_at": iso(win_from)})
    s, r = call("GET", f"/partitions/{pcode}/compliance-summary?{bad_qs}")
    expect(422, s, "invalid range")
    print("  code:", r["error"]["code"])

    step("汇总错误：时间不带时区 -> 422 validation_error")
    naive_qs = urllib.parse.urlencode({
        "start_at": "2025-06-01T08:00:00", "end_at": iso(win_to)})
    s, r = call("GET", f"/partitions/{pcode}/compliance-summary?{naive_qs}")
    expect(422, s, "naive datetime")
    print("  code:", r["error"]["code"])

    step("汇总错误：分区不存在 -> 404 partition_not_found")
    s, r = call("GET", f"/partitions/NOPE/compliance-summary?{qs}")
    expect(404, s, "partition missing")
    print("  code:", r["error"]["code"])

    # -------------------------------------------------- 限值调整影响预览
    step("限值预览准备：本次运行专用分区（仅 conductivity 必测，2020-2030 相接限值）")
    s, r = call("POST", "/admin/indicators",
                {"code": "conductivity", "name": "电导率", "unit": "uS/cm"})
    assert s in (201, 409), (s, r)
    pv_partition = "P" + datetime.now(timezone.utc).strftime("%H%M%S%f")[:12]
    s, r = call("POST", "/admin/partitions",
                {"code": pv_partition, "name": "预览演示分区"})
    assert s == 201, (s, r)
    s, r = call("POST", f"/admin/partitions/{pv_partition}/mandatory-indicators",
                {"indicator_code": "conductivity"})
    assert s == 201, (s, r)
    for vfrom, vto in [
        ("2020-01-01T00:00:00Z", "2025-01-01T00:00:00Z"),
        ("2025-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
        ("2026-01-01T00:00:00Z", "2030-01-01T00:00:00Z"),
    ]:
        s, r = call("POST", f"/admin/partitions/{pv_partition}/limits",
                    {"indicator_code": "conductivity", "max_value": 300.0,
                     "valid_from": vfrom, "valid_to": vto})
        assert s == 201, (s, r)
    # 在 2020 年取一个本次运行专属的 10 分钟窗口（落在 conductivity 限值生效区间内）。
    pv_anchor = datetime(2020, 1, 1, tzinfo=timezone.utc) + timedelta(
        minutes=int(pv_partition[1:]) % 50000 * 10)
    pv_end = pv_anchor + timedelta(minutes=10)

    def register_conductivity(offset_min: int, value: float) -> str:
        s, sample = call("POST", "/samples",
                         {"partition_code": pv_partition,
                          "sampled_at": iso(pv_anchor + timedelta(minutes=offset_min))})
        assert s == 201, (s, sample)
        s2, _ = call("PUT", f"/samples/{sample['id']}/readings/conductivity",
                     {"indicator_code": "conductivity", "value": value,
                      "expected_version": 0,
                      "request_no": f"PV-{RUN_TAG}-{offset_min}"})
        assert s2 == 200, (s2, _)
        s3, _ = call("POST", f"/samples/{sample['id']}/publish",
                     {"expected_version": 1})
        assert s3 == 201, (s3, _)
        return sample["id"]

    sid_350 = register_conductivity(2, 350.0)   # 原 <300 上限下不合格
    sid_200 = register_conductivity(4, 200.0)   # 合格

    def preview(max_value=None, min_value=None, partition=None,
                indicator="conductivity", start=None, end=None):
        body = {"indicator_code": indicator,
                "start_at": iso(start or pv_anchor), "end_at": iso(end or pv_end)}
        if max_value is not None:
            body["max_value"] = max_value
        if min_value is not None:
            body["min_value"] = min_value
        return call("POST", f"/partitions/{partition or pv_partition}/limit-change-preview",
                    body)

    step("预览放宽上限 <400：原合格1/不合格1 -> 模拟合格2，350 样本结论翻转")
    s, pv = preview(max_value=400.0)
    expect(200, s, "preview relax")
    print(" ", json.dumps(pv, ensure_ascii=False))
    assert pv["evaluable_samples"] == 2
    assert pv["not_evaluable_samples"] == []
    assert pv["original_qualified_count"] == 1 and pv["original_unqualified_count"] == 1
    assert pv["simulated_qualified_count"] == 2 and pv["simulated_unqualified_count"] == 0
    flip = {(c["sample_id"], c["original_conclusion"], c["simulated_conclusion"])
            for c in pv["changed_samples"]}
    assert flip == {(sid_350, "unqualified", "qualified")}

    step("预览收紧上限 <150：两份全部变不合格（上限开端；350 本就不合格，仅 200 翻转）")
    s, pv = preview(max_value=150.0)
    expect(200, s, "preview tighten")
    assert pv["simulated_qualified_count"] == 0 and pv["simulated_unqualified_count"] == 2
    assert {c["sample_id"] for c in pv["changed_samples"]} == {sid_200}
    print("  changed:", [(c["sample_id"][:8], c["original_conclusion"],
                          c["simulated_conclusion"]) for c in pv["changed_samples"]])

    step("仅拟议下限（>=100，下限包含、不设上限）：200 保持合格；350 因上限被撤销量不合格")
    s, pv = preview(min_value=100.0)
    expect(200, s, "preview lower only")
    assert pv["proposed_limit"] == {"min_value": 100.0, "max_value": None}
    assert pv["simulated_qualified_count"] == 2
    assert {c["sample_id"] for c in pv["changed_samples"]} == {sid_350}

    step("不可评估：发布后新增必测指标 hardness，历史快照不含该项")
    s, _ = call("POST", "/admin/indicators",
                {"code": "hardness", "name": "总硬度", "unit": "mg/L"})
    assert s in (201, 409)
    s, _ = call("POST", f"/admin/partitions/{pv_partition}/mandatory-indicators",
                {"indicator_code": "hardness"})
    assert s in (201, 409)
    s, pv = preview(indicator="hardness", max_value=450.0)
    expect(200, s, "preview not evaluable")
    assert pv["evaluable_samples"] == 0
    assert len(pv["not_evaluable_samples"]) == 2
    assert {n["sample_id"] for n in pv["not_evaluable_samples"]} == {sid_350, sid_200}
    assert all(n["reason"] == "indicator_not_in_report" for n in pv["not_evaluable_samples"])
    assert pv["original_qualified_count"] == 0 and pv["simulated_qualified_count"] == 0
    assert pv["changed_samples"] == []

    step("预览拒绝：双界 min>=max / 窗口非法 / 非必测 / 指标不存在 / 分区不存在")
    s, r = preview(min_value=400.0, max_value=400.0)
    expect(422, s, "min >= max")
    print("  code:", r["error"]["code"])
    s, r = preview(start=pv_end, end=pv_anchor)
    expect(422, s, "invalid window")
    print("  code:", r["error"]["code"])
    s, r = call("POST", "/partitions/P1/limit-change-preview",
                {"indicator_code": "conductivity",
                 "start_at": iso(pv_anchor), "end_at": iso(pv_end), "max_value": 400.0})
    expect(422, s, "not mandatory")
    print("  code:", r["error"]["code"])
    s, r = preview(indicator="nope_indicator", max_value=1.0)
    expect(404, s, "indicator missing")
    print("  code:", r["error"]["code"])
    s, r = call("POST", "/partitions/NOPE/limit-change-preview",
                {"indicator_code": "conductivity",
                 "start_at": iso(pv_anchor), "end_at": iso(pv_end), "max_value": 400.0})
    expect(404, s, "partition missing")
    print("  code:", r["error"]["code"])

    print("\nALL DEMO STEPS PASSED")


if __name__ == "__main__":
    main()
