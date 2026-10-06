#!/usr/bin/env bash
# 自来水厂取样合规复核服务 —— curl 调用示例（完整业务流）
# 依赖：bash、curl、python3（仅用于解析 JSON）
#
# 用法：
#   BASE=http://localhost:8080 bash examples/api_examples.sh
set -euo pipefail

BASE="${BASE:-http://localhost:8080}"
SAMPLED_AT="2025-06-01T08:00:00+08:00"   # 种子限值 2025 年生效，命中唯一一条
SAMPLED_AT_UTC="2025-06-01T00:00:00Z"

say() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
jqget() { python3 -c "import sys,json;d=json.load(sys.stdin);print(eval(\"d$1\"))"; }

say "0. 健康检查"
curl -sS "$BASE/health"; echo

say "1. 查看分区 P1 的必测指标与生效限值（启动种子数据）"
curl -sS "$BASE/admin/partitions/P1" | python3 -m json.tool

say "2. 维护主数据：新建指标 / 分区 / 必测关系 / 限值区间"
curl -sS -X POST "$BASE/admin/indicators" -H 'Content-Type: application/json' \
  -d '{"code":"conductivity","name":"电导率","unit":"uS/cm"}' | python3 -m json.tool
curl -sS -X POST "$BASE/admin/partitions" -H 'Content-Type: application/json' \
  -d '{"code":"P3","name":"管网水三号分区"}' | python3 -m json.tool
curl -sS -X POST "$BASE/admin/partitions/P3/mandatory-indicators" -H 'Content-Type: application/json' \
  -d '{"indicator_code":"conductivity"}' | python3 -m json.tool
curl -sS -X POST "$BASE/admin/partitions/P3/limits" -H 'Content-Type: application/json' \
  -d '{"indicator_code":"conductivity","max_value":300.0,
       "valid_from":"2025-01-01T00:00:00Z","valid_to":"2026-01-01T00:00:00Z"}' | python3 -m json.tool

say "2b. 区间重叠被拒绝（422 limit_interval_overlap）"
curl -sS -o /tmp/ov.json -w 'HTTP %{http_code}\n' -X POST "$BASE/admin/partitions/P3/limits" \
  -H 'Content-Type: application/json' \
  -d '{"indicator_code":"conductivity","max_value":400.0,
       "valid_from":"2025-12-01T00:00:00Z","valid_to":"2026-06-01T00:00:00Z"}'
cat /tmp/ov.json | python3 -m json.tool

say "3. 登记样本（分区 + 采样时刻，时刻可带时区偏移，服务端归一 UTC）"
SAMPLE_JSON=$(curl -sS -X POST "$BASE/samples" -H 'Content-Type: application/json' \
  -d "{\"partition_code\":\"P1\",\"sampled_at\":\"$SAMPLED_AT\"}")
echo "$SAMPLE_JSON" | python3 -m json.tool
SID=$(echo "$SAMPLE_JSON" | jqget "['id']")
echo "sample id = $SID"

say "4. 录入读数（非负 + expected_version；初始版本为 0，每次成功写入版本 +1）"
curl -sS -X PUT "$BASE/samples/$SID/readings/turbidity" -H 'Content-Type: application/json' \
  -d '{"indicator_code":"turbidity","value":1.2,"expected_version":0}' | python3 -m json.tool
curl -sS -X PUT "$BASE/samples/$SID/readings/ph" -H 'Content-Type: application/json' \
  -d '{"indicator_code":"ph","value":7.2,"expected_version":1}' | python3 -m json.tool
curl -sS -X PUT "$BASE/samples/$SID/readings/residual_chlorine" -H 'Content-Type: application/json' \
  -d '{"indicator_code":"residual_chlorine","value":0.8,"expected_version":2}' | python3 -m json.tool

say "4b. 版本过期 -> 409 version_conflict"
curl -sS -o /tmp/conf.json -w 'HTTP %{http_code}\n' -X PUT "$BASE/samples/$SID/readings/ph" \
  -H 'Content-Type: application/json' \
  -d '{"indicator_code":"ph","value":7.9,"expected_version":0}'
cat /tmp/conf.json | python3 -m json.tool

say "5. 更正读数必须带唯一请求号（不带 -> 422 correction_requires_request_no）"
curl -sS -o /tmp/c0.json -w 'HTTP %{http_code}\n' -X PUT "$BASE/samples/$SID/readings/turbidity" \
  -H 'Content-Type: application/json' \
  -d '{"indicator_code":"turbidity","value":0.5,"expected_version":3}'
cat /tmp/c0.json | python3 -m json.tool

say "5b. 带请求号更正成功（版本 3 -> 4）"
REQNO="fix-turb-$(date +%s)"
curl -sS -X PUT "$BASE/samples/$SID/readings/turbidity" -H 'Content-Type: application/json' \
  -d "{\"indicator_code\":\"turbidity\",\"value\":0.5,\"expected_version\":3,\"request_no\":\"$REQNO\"}" \
  | python3 -m json.tool

say "5c. 同号重放 -> 返回原结果，版本不再变化"
curl -sS -X PUT "$BASE/samples/$SID/readings/turbidity" -H 'Content-Type: application/json' \
  -d "{\"indicator_code\":\"turbidity\",\"value\":0.5,\"expected_version\":3,\"request_no\":\"$REQNO\"}" \
  | python3 -m json.tool

say "5d. 同号异内容 -> 422 idempotency_key_conflict"
curl -sS -o /tmp/c1.json -w 'HTTP %{http_code}\n' -X PUT "$BASE/samples/$SID/readings/turbidity" \
  -H 'Content-Type: application/json' \
  -d "{\"indicator_code\":\"turbidity\",\"value\":0.9,\"expected_version\":3,\"request_no\":\"$REQNO\"}"
cat /tmp/c1.json | python3 -m json.tool

say "6. 发布合规报告（读数齐全 + 每项采样时刻恰有一条生效限值；任一超限不合格）"
curl -sS -X POST "$BASE/samples/$SID/publish" -H 'Content-Type: application/json' \
  -d '{"expected_version":4}' | python3 -m json.tool

say "6b. 同一读数版本重复发布 -> 409 already_published"
curl -sS -o /tmp/p1.json -w 'HTTP %{http_code}\n' -X POST "$BASE/samples/$SID/publish" \
  -H 'Content-Type: application/json' -d '{"expected_version":4}'
cat /tmp/p1.json | python3 -m json.tool

say "7. 更正后再发布 -> 新版本报告，旧版保留可查"
REQNO2="fix-turb2-$(date +%s)"
curl -sS -X PUT "$BASE/samples/$SID/readings/turbidity" -H 'Content-Type: application/json' \
  -d "{\"indicator_code\":\"turbidity\",\"value\":0.3,\"expected_version\":4,\"request_no\":\"$REQNO2\"}" \
  | python3 -m json.tool
curl -sS -X POST "$BASE/samples/$SID/publish" -H 'Content-Type: application/json' \
  -d '{"expected_version":5}' | python3 -m json.tool

say "7b. 报告列表（应含两个读数版本）"
curl -sS "$BASE/samples/$SID/reports" | python3 -m json.tool

say "8. 查询样本（当前读数、版本、已发布报告摘要）"
curl -sS "$BASE/samples/$SID" | python3 -m json.tool

say "9. 门禁：读数不齐禁止发布"
BAD=$(curl -sS -X POST "$BASE/samples" -H 'Content-Type: application/json' \
  -d "{\"partition_code\":\"P1\",\"sampled_at\":\"$SAMPLED_AT_UTC\"}" | jqget "['id']")
curl -sS -X PUT "$BASE/samples/$BAD/readings/ph" -H 'Content-Type: application/json' \
  -d '{"indicator_code":"ph","value":7.2,"expected_version":0}' >/dev/null
curl -sS -o /tmp/p2.json -w 'HTTP %{http_code}\n' -X POST "$BASE/samples/$BAD/publish" \
  -H 'Content-Type: application/json' -d '{"expected_version":1}'
cat /tmp/p2.json | python3 -m json.tool

say "10. 门禁：采样时刻无生效限值禁止发布（2024 年采样，限值 2025 年才生效）"
NOLIM=$(curl -sS -X POST "$BASE/samples" -H 'Content-Type: application/json' \
  -d '{"partition_code":"P1","sampled_at":"2024-12-15T08:00:00+08:00"}' | jqget "['id']")
for pair in "turbidity:0.2:0" "ph:7.2:1" "residual_chlorine:0.8:2"; do
  IFS=: read -r ind val ver <<< "$pair"
  curl -sS -X PUT "$BASE/samples/$NOLIM/readings/$ind" -H 'Content-Type: application/json' \
    -d "{\"indicator_code\":\"$ind\",\"value\":$val,\"expected_version\":$ver}" >/dev/null
done
curl -sS -o /tmp/p3.json -w 'HTTP %{http_code}\n' -X POST "$BASE/samples/$NOLIM/publish" \
  -H 'Content-Type: application/json' -d '{"expected_version":3}'
cat /tmp/p3.json | python3 -m json.tool

say "11. 合规汇总：按分区 + 采样时刻窗口（左闭右开）统计已发布报告"
echo "  窗口 [2025-01-01Z, 2026-01-01Z) 内每个样本只取最新已发布报告；未发布样本不计入"
curl -sS --get "$BASE/partitions/P1/compliance-summary" \
  --data-urlencode "start_at=2025-01-01T00:00:00Z" \
  --data-urlencode "end_at=2026-01-01T00:00:00Z" | python3 -m json.tool

say "11b. 起止时刻可带时区偏移（与上面同一 UTC 窗口，结果相同）"
curl -sS --get "$BASE/partitions/P1/compliance-summary" \
  --data-urlencode "start_at=2025-01-01T08:00:00+08:00" \
  --data-urlencode "end_at=2026-01-01T08:00:00+08:00" | python3 -m json.tool

say "11c. 无报告窗口（2024 年）：各计数为零，按指标给零基线"
curl -sS --get "$BASE/partitions/P1/compliance-summary" \
  --data-urlencode "start_at=2024-01-01T00:00:00Z" \
  --data-urlencode "end_at=2025-01-01T00:00:00Z" | python3 -m json.tool

say "11d. 起点不早于终点 -> 422 invalid_time_range（统一错误信封）"
curl -sS -w '\nHTTP %{http_code}\n' --get "$BASE/partitions/P1/compliance-summary" \
  --data-urlencode "start_at=2026-01-01T00:00:00Z" \
  --data-urlencode "end_at=2025-01-01T00:00:00Z"

say "11e. 分区不存在 -> 404 partition_not_found（统一错误信封）"
curl -sS -w '\nHTTP %{http_code}\n' --get "$BASE/partitions/NOPE/compliance-summary" \
  --data-urlencode "start_at=2025-01-01T00:00:00Z" \
  --data-urlencode "end_at=2026-01-01T00:00:00Z"

say "12. 限值调整影响预览（只读模拟，不改限值/报告，不读当前读数）"
# 每次运行使用独立分区（分区码含纳秒时间戳），重跑脚本互不干扰；
# conductivity 指标已在第 2 节创建，这里为新分区挂必测关系与一段宽时限值。
PV_P="PVP$(date -u +%H%M%S%N | head -c 15)"
curl -sS -o /dev/null -X POST "$BASE/admin/partitions" -H 'Content-Type: application/json' \
  -d "{\"code\":\"$PV_P\",\"name\":\"限值预览演示分区\"}"
curl -sS -o /dev/null -X POST "$BASE/admin/partitions/$PV_P/mandatory-indicators" \
  -H 'Content-Type: application/json' -d '{"indicator_code":"conductivity"}'
curl -sS -o /dev/null -X POST "$BASE/admin/partitions/$PV_P/limits" \
  -H 'Content-Type: application/json' \
  -d '{"indicator_code":"conductivity","max_value":300.0,
       "valid_from":"2000-01-01T00:00:00Z","valid_to":"2100-01-01T00:00:00Z"}'
# 以当前 UTC 时刻锚定本次运行窗口（左闭右开，10 分钟）
PV_ANCHOR=$(date -u +%Y-%m-%dT%H:%M:%SZ)
PV_T1=$(date -u -d "$PV_ANCHOR +2 minutes" +%Y-%m-%dT%H:%M:%SZ)
PV_T2=$(date -u -d "$PV_ANCHOR +4 minutes" +%Y-%m-%dT%H:%M:%SZ)
PV_END=$(date -u -d "$PV_ANCHOR +10 minutes" +%Y-%m-%dT%H:%M:%SZ)
PV_REQ="pv-demo-$(date -u +%s%N)"
PV_S1=$(curl -sS -X POST "$BASE/samples" -H 'Content-Type: application/json' \
  -d "{\"partition_code\":\"$PV_P\",\"sampled_at\":\"$PV_T1\"}" | jqget "['id']")
curl -sS -X PUT "$BASE/samples/$PV_S1/readings/conductivity" -H 'Content-Type: application/json' \
  -d "{\"indicator_code\":\"conductivity\",\"value\":350,\"expected_version\":0,\"request_no\":\"$PV_REQ-1\"}" \
  | python3 -m json.tool
curl -sS -o /dev/null -X POST "$BASE/samples/$PV_S1/publish" -H 'Content-Type: application/json' \
  -d '{"expected_version":1}'
PV_S2=$(curl -sS -X POST "$BASE/samples" -H 'Content-Type: application/json' \
  -d "{\"partition_code\":\"$PV_P\",\"sampled_at\":\"$PV_T2\"}" | jqget "['id']")
curl -sS -o /dev/null -X PUT "$BASE/samples/$PV_S2/readings/conductivity" -H 'Content-Type: application/json' \
  -d "{\"indicator_code\":\"conductivity\",\"value\":200,\"expected_version\":0,\"request_no\":\"$PV_REQ-2\"}"
curl -sS -o /dev/null -X POST "$BASE/samples/$PV_S2/publish" -H 'Content-Type: application/json' \
  -d '{"expected_version":1}'
echo "  分区 $PV_P；样本 $PV_S1 冻结读数 350（原 <300 上限下不合格）；$PV_S2 冻结读数 200（合格）"

say "12b. 拟议放宽上限到 <400：350 翻转合格，200 保持合格；原合格1 -> 模拟合格2"
curl -sS -X POST "$BASE/partitions/$PV_P/limit-change-preview" -H 'Content-Type: application/json' \
  -d "{\"indicator_code\":\"conductivity\",\"start_at\":\"$PV_ANCHOR\",\"end_at\":\"$PV_END\",\"max_value\":400.0}" \
  | python3 -m json.tool

say "12c. 拟议收紧上限到 <150（仅上限、上限开端）：200 翻转不合格（350 本就不合格）"
curl -sS -X POST "$BASE/partitions/$PV_P/limit-change-preview" -H 'Content-Type: application/json' \
  -d "{\"indicator_code\":\"conductivity\",\"start_at\":\"$PV_ANCHOR\",\"end_at\":\"$PV_END\",\"max_value\":150.0}" \
  | python3 -m json.tool

say "12d. 报告未含目标指标 -> 不可评估，不参与前后合格数"
echo "  新建指标 hardness 并把它加为本分区必测项（在上面两份 conductivity 报告发布之后）："
curl -sS -o /dev/null -X POST "$BASE/admin/indicators" -H 'Content-Type: application/json' \
  -d '{"code":"hardness","name":"总硬度","unit":"mg/L"}'
curl -sS -o /dev/null -X POST "$BASE/admin/partitions/$PV_P/mandatory-indicators" \
  -H 'Content-Type: application/json' -d '{"indicator_code":"hardness"}'
echo "  预览 hardness：两份已发布快照均不含该项 -> evaluable=0 + not_evaluable 两条"
curl -sS -X POST "$BASE/partitions/$PV_P/limit-change-preview" -H 'Content-Type: application/json' \
  -d "{\"indicator_code\":\"hardness\",\"start_at\":\"$PV_ANCHOR\",\"end_at\":\"$PV_END\",\"max_value\":450.0}" \
  | python3 -m json.tool

say "12e. 拒绝路径：双界下界>=上界 422 invalid_limit"
curl -sS -w '\nHTTP %{http_code}\n' -X POST "$BASE/partitions/$PV_P/limit-change-preview" \
  -H 'Content-Type: application/json' \
  -d "{\"indicator_code\":\"conductivity\",\"start_at\":\"$PV_ANCHOR\",\"end_at\":\"$PV_END\",
       \"min_value\":400,\"max_value\":400}"

say "12f. 拒绝路径：时间窗非法 / 不带时区"
curl -sS -w '\nHTTP %{http_code}\n' -X POST "$BASE/partitions/$PV_P/limit-change-preview" \
  -H 'Content-Type: application/json' \
  -d "{\"indicator_code\":\"conductivity\",\"start_at\":\"$PV_END\",\"end_at\":\"$PV_ANCHOR\",\"max_value\":400}"
curl -sS -w '\nHTTP %{http_code}\n' -X POST "$BASE/partitions/$PV_P/limit-change-preview" \
  -H 'Content-Type: application/json' \
  -d "{\"indicator_code\":\"conductivity\",\"start_at\":\"2025-06-01T08:00:00\",\"end_at\":\"$PV_END\",\"max_value\":400}"

say "12g. 拒绝路径：指标非分区必测项 422 / 指标不存在 404 / 分区不存在 404"
curl -sS -w '\nHTTP %{http_code}\n' -X POST "$BASE/partitions/P1/limit-change-preview" \
  -H 'Content-Type: application/json' \
  -d '{"indicator_code":"conductivity","start_at":"2025-01-01T00:00:00Z","end_at":"2026-01-01T00:00:00Z","max_value":400}'
curl -sS -w '\nHTTP %{http_code}\n' -X POST "$BASE/partitions/$PV_P/limit-change-preview" \
  -H 'Content-Type: application/json' \
  -d "{\"indicator_code\":\"nope\",\"start_at\":\"$PV_ANCHOR\",\"end_at\":\"$PV_END\",\"max_value\":400}"
curl -sS -w '\nHTTP %{http_code}\n' -X POST "$BASE/partitions/NOPE/limit-change-preview" \
  -H 'Content-Type: application/json' \
  -d "{\"indicator_code\":\"conductivity\",\"start_at\":\"$PV_ANCHOR\",\"end_at\":\"$PV_END\",\"max_value\":400}"

echo
echo "DONE. 可交互式查看 OpenAPI 文档： $BASE/docs"
