#!/usr/bin/env python3
"""自来水厂取样合规复核服务 —— Python 调用示例（仅标准库，无需 pip 安装）。

流程覆盖：
  登记样本 -> 录入读数 -> 版本冲突 -> 更正(请求号/重放/同号异内容)
  -> 首次发布(不合格快照) -> 重复发布拒绝 -> 更正后再发布(新版本合格, 旧版可查)
  -> 读数不齐/无生效限值门禁
  -> 按分区 + 采样时刻窗口的合规汇总（左闭右开、仅最新已发布报告、按指标超限次数）。

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
                    "expected_version": 3, "request_no": f"DEMO-FIX-{anchor.strftime('%H%M%S')}"})
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
    # 复用本分区窗口 [anchor, anchor+1h)：sid_bad 最新报告 ph 已更正为合格，
    # 但早先造数样本中无浑浊度超限；再造 1 份浑浊度 1.0（max 开端）的不合格报告
    step("预览准备：窗口内新增 1 份浑浊度冻结值 1.0 的不合格报告")
    sid_edge = register_publish(15, {"turbidity": 1.0, "ph": 7.2, "residual_chlorine": 0.8})

    def preview(indicator, **bounds):
        return call("POST", f"/partitions/{pcode}/limit-change-preview",
                    {"indicator_code": indicator, "start_at": iso(win_from),
                     "end_at": iso(win_to), **bounds})

    step("预览：浑浊度上限放宽到 1.5 -> 1.0 由不合格模拟为合格（纯只读）")
    s, pv = preview("turbidity", min_value=None, max_value=1.5)
    expect(200, s, "preview loosen")
    print(" ", json.dumps(pv, ensure_ascii=False))
    assert pv["total_reported_samples"] == 3
    assert pv["evaluable_sample_count"] == 3
    assert pv["original_qualified_count"] == 2
    assert pv["simulated_qualified_count"] == 3
    assert [c["sample_id"] for c in pv["changed_samples"]] == [sid_edge]
    ch = pv["changed_samples"][0]
    assert ch["original_conclusion"] == "unqualified"
    assert ch["simulated_conclusion"] == "qualified"
    assert ch["frozen_value"] == 1.0 and ch["simulated_target_in_range"] is True

    step("预览：收紧到 0.1（max 开端）与仅设下限 0.05")
    s, pv = preview("turbidity", max_value=0.1)
    expect(200, s, "preview tighten")
    assert pv["simulated_qualified_count"] == 0
    s, pv = preview("residual_chlorine", min_value=0.05, max_value=None)
    expect(200, s, "preview min only")
    assert pv["proposed_limits"] == {"min_value": 0.05, "max_value": None}
    print("  min-only proposed_limits:", pv["proposed_limits"])

    step("预览：无报告窗口 -> 零计数与空列表")
    s, pv = call("POST", f"/partitions/{pcode}/limit-change-preview",
                 {"indicator_code": "turbidity", "max_value": 1.0,
                  "start_at": "2024-01-01T00:00:00Z", "end_at": "2024-02-01T00:00:00Z"})
    expect(200, s, "preview empty")
    assert pv["evaluable_sample_count"] == 0
    assert pv["unevaluable_samples"] == [] and pv["changed_samples"] == []
    print(" ", {k: pv[k] for k in ("total_reported_samples", "evaluable_sample_count",
                                   "original_qualified_count", "simulated_qualified_count")})

    step("预览拒绝：双界皆空 / min>=max -> 422 invalid_limit")
    s, r = preview("turbidity")
    expect(422, s, "no bounds")
    print("  code:", r["error"]["code"])
    s, r = preview("turbidity", min_value=2.0, max_value=1.0)
    expect(422, s, "min >= max")
    print("  code:", r["error"]["code"])

    step("预览拒绝：时间不带时区 / 起点不早于终点 -> 422")
    s, r = call("POST", f"/partitions/{pcode}/limit-change-preview",
                {"indicator_code": "turbidity", "max_value": 1.0,
                 "start_at": "2025-06-01T08:00:00", "end_at": iso(win_to)})
    expect(422, s, "naive datetime")
    print("  code:", r["error"]["code"])
    s, r = preview("turbidity", max_value=1.0,
                   **{"start_at": iso(win_to), "end_at": iso(win_from)})
    expect(422, s, "invalid range")
    print("  code:", r["error"]["code"])

    step("预览拒绝：分区/指标不存在、指标非必测 -> 404 / 404 / 422")
    s, r = call("POST", "/partitions/NOPE/limit-change-preview",
                {"indicator_code": "turbidity", "max_value": 1.0,
                 "start_at": iso(win_from), "end_at": iso(win_to)})
    expect(404, s, "partition missing")
    print("  code:", r["error"]["code"])
    s, r = preview("nope-indicator", max_value=1.0)
    expect(404, s, "indicator missing")
    print("  code:", r["error"]["code"])
    # 新建一个指标但不加入本分区必测项 -> “指标存在但非该分区必测”
    ind_code = f"preview_ind_{RUN_TAG[-8:]}"
    s, _ = call("POST", "/admin/indicators",
                {"code": ind_code, "name": "预览专用指标", "unit": "-"})
    assert s == 201, s
    s, r = preview(ind_code, max_value=1.0)
    expect(422, s, "not mandatory")
    print("  code:", r["error"]["code"])

    print("\nALL DEMO STEPS PASSED")


if __name__ == "__main__":
    main()
