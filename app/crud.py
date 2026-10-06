"""核心业务逻辑：分区/指标/限值主数据、样本登记、读数录入更正、合规发布。

关键规则：
- 限值区间左闭右开 [valid_from, valid_to)；同分区同指标区间重叠即拒绝。
- 读数写入带 expected_version 乐观锁；更正（覆盖已存在读数）必须带 request_no。
- request_no 唯一：重放返回原结果；同号异内容 422。
- 发布要求必测读数齐全，且每项在采样时刻恰有一条生效限值；任一超限不合格。
- 同一 reading_version 只能发布一次；发布整体事务化，失败不留部分报告。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime
from typing import Any

from . import timeutil
from .database import read_tx, write_tx
from .errors import ConflictError, NotFoundError, UnprocessableError
from .schemas import LimitCreate, LimitPreviewIn, ReadingUpsert, SampleCreate


# ---------------------------------------------------------------- 主数据

def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def create_indicator(code: str, name: str, unit: str) -> dict[str, Any]:
    now = timeutil.iso_z(timeutil.utcnow())
    with write_tx() as conn:
        exists = conn.execute("SELECT 1 FROM indicators WHERE code=?", (code,)).fetchone()
        if exists:
            raise ConflictError(f"indicator {code!r} already exists", code="indicator_exists")
        conn.execute(
            "INSERT INTO indicators(code, name, unit, created_at) VALUES (?,?,?,?)",
            (code, name, unit, now),
        )
        row = conn.execute("SELECT code, name, unit, created_at FROM indicators WHERE code=?", (code,)).fetchone()
        return dict(row)


def create_partition(code: str, name: str) -> dict[str, Any]:
    now = timeutil.iso_z(timeutil.utcnow())
    with write_tx() as conn:
        exists = conn.execute("SELECT 1 FROM partitions WHERE code=?", (code,)).fetchone()
        if exists:
            raise ConflictError(f"partition {code!r} already exists", code="partition_exists")
        conn.execute(
            "INSERT INTO partitions(code, name, created_at) VALUES (?,?,?)",
            (code, name, now),
        )
        row = conn.execute("SELECT code, name, created_at FROM partitions WHERE code=?", (code,)).fetchone()
        return dict(row)


def add_mandatory_indicator(partition_code: str, indicator_code: str) -> dict[str, Any]:
    now = timeutil.iso_z(timeutil.utcnow())
    with write_tx() as conn:
        if not conn.execute("SELECT 1 FROM partitions WHERE code=?", (partition_code,)).fetchone():
            raise NotFoundError(f"partition {partition_code!r} not found")
        if not conn.execute("SELECT 1 FROM indicators WHERE code=?", (indicator_code,)).fetchone():
            raise NotFoundError(f"indicator {indicator_code!r} not found")
        try:
            conn.execute(
                "INSERT INTO mandatory_indicators(partition_code, indicator_code, created_at) VALUES (?,?,?)",
                (partition_code, indicator_code, now),
            )
        except sqlite3.IntegrityError:
            raise ConflictError(
                f"indicator {indicator_code!r} already mandatory in partition {partition_code!r}",
                code="mandatory_exists",
            )
        return {"partition_code": partition_code, "indicator_code": indicator_code}


def _intervals_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    # 左闭右开区间重叠：端点相接（a_end == b_start）不算重叠
    return a_start < b_end and b_start < a_end


def create_limit(partition_code: str, body: LimitCreate) -> dict[str, Any]:
    # 无效区间 / min>=max 在模型语义层校验
    try:
        body.validate_bounds()
    except ValueError as exc:
        raise UnprocessableError(str(exc), code="invalid_limit")

    start_ms, end_ms = timeutil.epoch_ms(body.valid_from), timeutil.epoch_ms(body.valid_to)
    now = timeutil.iso_z(timeutil.utcnow())
    with write_tx() as conn:
        if not conn.execute("SELECT 1 FROM partitions WHERE code=?", (partition_code,)).fetchone():
            raise NotFoundError(f"partition {partition_code!r} not found")
        if not conn.execute("SELECT 1 FROM indicators WHERE code=?", (body.indicator_code,)).fetchone():
            raise NotFoundError(f"indicator {body.indicator_code!r} not found")
        membership = conn.execute(
            "SELECT 1 FROM mandatory_indicators WHERE partition_code=? AND indicator_code=?",
            (partition_code, body.indicator_code),
        ).fetchone()
        if not membership:
            raise UnprocessableError(
                f"indicator {body.indicator_code!r} is not a mandatory indicator of partition "
                f"{partition_code!r}; add it as mandatory before defining limits",
                code="not_mandatory_indicator",
            )
        rows = conn.execute(
            "SELECT valid_from_ms, valid_to_ms FROM limits WHERE partition_code=? AND indicator_code=?",
            (partition_code, body.indicator_code),
        ).fetchall()
        for row in rows:
            if _intervals_overlap(start_ms, end_ms, row["valid_from_ms"], row["valid_to_ms"]):
                raise UnprocessableError(
                    "limit interval overlaps an existing interval "
                    f"[{row['valid_from_ms']}, {row['valid_to_ms']}) (half-open intervals must not overlap)",
                    code="limit_interval_overlap",
                    details={
                        "existing_from_ms": row["valid_from_ms"],
                        "existing_to_ms": row["valid_to_ms"],
                        "incoming_from_ms": start_ms,
                        "incoming_to_ms": end_ms,
                    },
                )
        try:
            cur = conn.execute(
                """INSERT INTO limits(partition_code, indicator_code, min_value, max_value,
                                      valid_from, valid_to, valid_from_ms, valid_to_ms, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    partition_code,
                    body.indicator_code,
                    body.min_value,
                    body.max_value,
                    timeutil.iso_z(body.valid_from),
                    timeutil.iso_z(body.valid_to),
                    start_ms,
                    end_ms,
                    now,
                ),
            )
        except sqlite3.IntegrityError:
            raise UnprocessableError(
                "a limit with the same valid_from already exists", code="limit_interval_conflict"
            )
        row = conn.execute("SELECT * FROM limits WHERE id=?", (cur.lastrowid,)).fetchone()
        return _limit_row(row)


def _limit_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "partition_code": row["partition_code"],
        "indicator_code": row["indicator_code"],
        "min_value": row["min_value"],
        "max_value": row["max_value"],
        "valid_from": row["valid_from"],
        "valid_to": row["valid_to"],
    }


def list_indicators() -> list[dict[str, Any]]:
    with read_tx() as conn:
        return [dict(r) for r in conn.execute("SELECT code, name, unit FROM indicators ORDER BY code")]


def list_partitions() -> list[dict[str, Any]]:
    with read_tx() as conn:
        return [dict(r) for r in conn.execute("SELECT code, name FROM partitions ORDER BY code")]


def get_partition_detail(partition_code: str) -> dict[str, Any]:
    with read_tx() as conn:
        p = conn.execute("SELECT code, name FROM partitions WHERE code=?", (partition_code,)).fetchone()
        if not p:
            raise NotFoundError(f"partition {partition_code!r} not found")
        mandatory = [
            r["indicator_code"]
            for r in conn.execute(
                "SELECT indicator_code FROM mandatory_indicators WHERE partition_code=? ORDER BY indicator_code",
                (partition_code,),
            )
        ]
        limits = [
            _limit_row(r)
            for r in conn.execute(
                "SELECT * FROM limits WHERE partition_code=? ORDER BY indicator_code, valid_from_ms",
                (partition_code,),
            )
        ]
        return {"code": p["code"], "name": p["name"], "mandatory_indicators": mandatory, "limits": limits}


# ---------------------------------------------------------------- 样本

def register_sample(body: SampleCreate) -> dict[str, Any]:
    sample_id = str(uuid.uuid4())
    now = timeutil.iso_z(timeutil.utcnow())
    sampled_iso = timeutil.iso_z(body.sampled_at)
    sampled_ms = timeutil.epoch_ms(body.sampled_at)
    with write_tx() as conn:
        if not conn.execute("SELECT 1 FROM partitions WHERE code=?", (body.partition_code,)).fetchone():
            raise NotFoundError(f"partition {body.partition_code!r} not found")
        conn.execute(
            """INSERT INTO samples(id, partition_code, sampled_at, sampled_at_ms, reading_version, created_at)
               VALUES (?,?,?,?,0,?)""",
            (sample_id, body.partition_code, sampled_iso, sampled_ms, now),
        )
    return {
        "id": sample_id,
        "partition_code": body.partition_code,
        "sampled_at": sampled_iso,
        "reading_version": 0,
        "readings": {},
        "published_versions": [],
    }


def _get_sample_row(conn: sqlite3.Connection, sample_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM samples WHERE id=?", (sample_id,)).fetchone()
    if not row:
        raise NotFoundError(f"sample {sample_id!r} not found")
    return row


def get_sample(sample_id: str) -> dict[str, Any]:
    with read_tx() as conn:
        sample = _get_sample_row(conn, sample_id)
        readings = {
            r["indicator_code"]: {"value": r["value"], "updated_at": r["updated_at"]}
            for r in conn.execute(
                "SELECT indicator_code, value, updated_at FROM readings WHERE sample_id=?", (sample_id,)
            )
        }
        published = [
            {"report_id": r["id"], "reading_version": r["reading_version"], "conclusion": r["conclusion"]}
            for r in conn.execute(
                "SELECT id, reading_version, conclusion FROM reports WHERE sample_id=? ORDER BY id",
                (sample_id,),
            )
        ]
        return {
            "id": sample["id"],
            "partition_code": sample["partition_code"],
            "sampled_at": sample["sampled_at"],
            "reading_version": sample["reading_version"],
            "readings": readings,
            "reports": published,
        }


# ---------------------------------------------------------------- 读数

def _fingerprint(sample_id: str, body: ReadingUpsert) -> str:
    payload = {
        "sample_id": sample_id,
        "indicator_code": body.indicator_code,
        "value": body.value,
        "expected_version": body.expected_version,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _reading_response(
    sample_id: str, indicator_code: str, value: float, version: int, corrected: bool
) -> dict[str, Any]:
    return {
        "sample_id": sample_id,
        "indicator_code": indicator_code,
        "value": value,
        "reading_version": version,
        "corrected": corrected,
    }


def put_reading(sample_id: str, body: ReadingUpsert) -> tuple[int, dict[str, Any], bool]:
    """返回 (http_status, response_body, replayed)。"""
    fp = _fingerprint(sample_id, body)

    # 快速路径：先在只读事务里处理重放与常见校验
    with read_tx() as conn:
        _get_sample_row(conn, sample_id)
        membership = conn.execute(
            """SELECT 1 FROM samples s
               JOIN mandatory_indicators m ON m.partition_code = s.partition_code
               WHERE s.id=? AND m.indicator_code=?""",
            (sample_id, body.indicator_code),
        ).fetchone()
        if not membership:
            raise UnprocessableError(
                f"indicator {body.indicator_code!r} is not mandatory for the sample's partition",
                code="not_mandatory_indicator",
            )
        if body.request_no is not None:
            saved = conn.execute(
                "SELECT fingerprint, status_code, response_json FROM correction_requests WHERE request_no=?",
                (body.request_no,),
            ).fetchone()
            if saved is not None:
                if saved["fingerprint"] != fp:
                    raise UnprocessableError(
                        "request_no was already used with a different payload",
                        code="idempotency_key_conflict",
                        details={"request_no": body.request_no},
                    )
                return saved["status_code"], json.loads(saved["response_json"]), True

    now = timeutil.iso_z(timeutil.utcnow())
    with write_tx() as conn:
        sample = _get_sample_row(conn, sample_id)

        # 锁内二次确认幂等记录（并发首请求可能已落库）
        if body.request_no is not None:
            saved = conn.execute(
                "SELECT fingerprint, status_code, response_json FROM correction_requests WHERE request_no=?",
                (body.request_no,),
            ).fetchone()
            if saved is not None:
                if saved["fingerprint"] != fp:
                    raise UnprocessableError(
                        "request_no was already used with a different payload",
                        code="idempotency_key_conflict",
                        details={"request_no": body.request_no},
                    )
                return saved["status_code"], json.loads(saved["response_json"]), True

        if sample["reading_version"] != body.expected_version:
            raise ConflictError(
                "reading version conflict: expected version is stale",
                code="version_conflict",
                details={
                    "expected_version": body.expected_version,
                    "current_version": sample["reading_version"],
                },
            )

        existing = conn.execute(
            "SELECT 1 FROM readings WHERE sample_id=? AND indicator_code=?",
            (sample_id, body.indicator_code),
        ).fetchone()
        if existing is not None and body.request_no is None:
            raise UnprocessableError(
                "a reading already exists for this indicator; correction requires a unique request_no",
                code="correction_requires_request_no",
                details={"indicator_code": body.indicator_code},
            )

        corrected = existing is not None
        conn.execute(
            """INSERT INTO readings(sample_id, indicator_code, value, updated_at)
               VALUES (?,?,?,?)
               ON CONFLICT(sample_id, indicator_code) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
            (sample_id, body.indicator_code, body.value, now),
        )
        conn.execute(
            "UPDATE samples SET reading_version = reading_version + 1 WHERE id=?", (sample_id,)
        )
        new_version = sample["reading_version"] + 1
        response = _reading_response(sample_id, body.indicator_code, body.value, new_version, corrected)

        if body.request_no is not None:
            conn.execute(
                """INSERT INTO correction_requests(request_no, fingerprint, status_code, response_json, created_at)
                   VALUES (?,?,?,?,?)""",
                (body.request_no, fp, 200, json.dumps(response, ensure_ascii=False), now),
            )
        return 200, response, False


# ---------------------------------------------------------------- 发布

def _evaluate_value(value: float, min_value: float | None, max_value: float | None) -> list[str]:
    """返回违规原因列表；空列表表示合格。min 闭端、max 开端（与限值区间语义一致）。"""
    reasons: list[str] = []
    if max_value is not None and value >= max_value:
        reasons.append(f"value {value:g} >= max limit {max_value:g} (max is exclusive)")
    if min_value is not None and value < min_value:
        reasons.append(f"value {value:g} < min limit {min_value:g} (min is inclusive)")
    return reasons


def publish_report(sample_id: str, expected_version: int) -> dict[str, Any]:
    with write_tx() as conn:
        sample = _get_sample_row(conn, sample_id)
        current_version = sample["reading_version"]

        if current_version != expected_version:
            raise ConflictError(
                "reading version conflict: expected version is stale",
                code="version_conflict",
                details={"expected_version": expected_version, "current_version": current_version},
            )

        duplicate = conn.execute(
            "SELECT id FROM reports WHERE sample_id=? AND reading_version=?",
            (sample_id, current_version),
        ).fetchone()
        if duplicate is not None:
            raise ConflictError(
                "this reading version has already been published",
                code="already_published",
                details={"reading_version": current_version, "existing_report_id": duplicate["id"]},
            )

        mandatory = [
            r["indicator_code"]
            for r in conn.execute(
                "SELECT indicator_code FROM mandatory_indicators WHERE partition_code=? ORDER BY indicator_code",
                (sample["partition_code"],),
            )
        ]
        readings = {
            r["indicator_code"]: r["value"]
            for r in conn.execute("SELECT indicator_code, value FROM readings WHERE sample_id=?", (sample_id,))
        }

        missing = [code for code in mandatory if code not in readings]
        if missing:
            raise UnprocessableError(
                "cannot publish: readings for mandatory indicators are incomplete",
                code="readings_incomplete",
                details={"missing_indicators": missing},
            )

        sampled_ms = sample["sampled_at_ms"]
        items: list[dict[str, Any]] = []
        no_limit: list[str] = []
        multi_limit: list[str] = []
        violations: list[dict[str, str]] = []

        for code in mandatory:
            rows = conn.execute(
                """SELECT * FROM limits
                   WHERE partition_code=? AND indicator_code=?
                     AND valid_from_ms <= ? AND valid_to_ms > ?
                   ORDER BY valid_from_ms""",
                (sample["partition_code"], code, sampled_ms, sampled_ms),
            ).fetchall()
            if len(rows) == 0:
                no_limit.append(code)
                continue
            if len(rows) > 1:
                multi_limit.append(code)
                continue
            limit = rows[0]
            value = readings[code]
            reasons = _evaluate_value(value, limit["min_value"], limit["max_value"])
            if reasons:
                violations.append({"indicator_code": code, "reasons": reasons})
            items.append(
                {
                    "indicator_code": code,
                    "value": value,
                    "in_range": not reasons,
                    "violations": reasons,
                    "limit_basis": {
                        "limit_id": limit["id"],
                        "min_value": limit["min_value"],
                        "max_value": limit["max_value"],
                        "valid_from": limit["valid_from"],
                        "valid_to": limit["valid_to"],
                    },
                }
            )

        if no_limit:
            raise UnprocessableError(
                "cannot publish: no effective limit at sampled_at for some mandatory indicators",
                code="no_effective_limit",
                details={"indicators_without_limit": no_limit, "sampled_at": sample["sampled_at"]},
            )
        if multi_limit:
            raise UnprocessableError(
                "cannot publish: multiple effective limits at sampled_at for some mandatory indicators",
                code="multiple_effective_limits",
                details={"indicators_with_multiple_limits": multi_limit},
            )

        qualified = not violations
        snapshot = {
            "sample_id": sample_id,
            "partition_code": sample["partition_code"],
            "sampled_at": sample["sampled_at"],
            "reading_version": current_version,
            "conclusion": "qualified" if qualified else "unqualified",
            "qualified": qualified,
            "items": items,
            "violations": violations,
        }

        now = timeutil.iso_z(timeutil.utcnow())
        try:
            cur = conn.execute(
                "INSERT INTO reports(sample_id, reading_version, conclusion, snapshot_json, created_at) "
                "VALUES (?,?,?,?,?)",
                (sample_id, current_version, snapshot["conclusion"], json.dumps(snapshot, ensure_ascii=False), now),
            )
        except sqlite3.IntegrityError:
            # 并发发布的数据库级兜底：唯一约束保证只有一个成功
            raise ConflictError(
                "this reading version has already been published", code="already_published"
            )
        snapshot["report_id"] = cur.lastrowid
        snapshot["published_at"] = now
        # 回填 report_id/发布时间到落库快照（保证查询到的快照与发布响应一致）
        conn.execute(
            "UPDATE reports SET snapshot_json=? WHERE id=?",
            (json.dumps(snapshot, ensure_ascii=False), cur.lastrowid),
        )
        return snapshot


def list_reports(sample_id: str) -> list[dict[str, Any]]:
    with read_tx() as conn:
        _get_sample_row(conn, sample_id)
        return [
            {
                "report_id": r["id"],
                "sample_id": sample_id,
                "reading_version": r["reading_version"],
                "conclusion": r["conclusion"],
                "published_at": r["created_at"],
            }
            for r in conn.execute(
                "SELECT id, reading_version, conclusion, created_at FROM reports WHERE sample_id=? ORDER BY id",
                (sample_id,),
            )
        ]


def get_report(sample_id: str, report_id: int) -> dict[str, Any]:
    with read_tx() as conn:
        _get_sample_row(conn, sample_id)
        row = conn.execute(
            "SELECT snapshot_json FROM reports WHERE sample_id=? AND id=?", (sample_id, report_id)
        ).fetchone()
        if not row:
            raise NotFoundError(f"report {report_id} of sample {sample_id} not found")
        return json.loads(row["snapshot_json"])


# ---------------------------------------------------------------- 合规汇总

def compliance_summary(
    partition_code: str, start_at: datetime, end_at: datetime
) -> dict[str, Any]:
    """按分区 + 采样时刻左闭右开窗口 [start_at, end_at) 汇总已发布报告。

    - 起止时刻必须带时区（归一化 UTC）；start_at >= end_at -> 422 invalid_time_range。
    - 分区不存在 -> 404 partition_not_found。
    - 窗口内每个样本只采用其最新（reading_version 最大）已发布报告；未发布样本不计入。
    - 返回报告样本总数、合格/不合格数，以及按指标统计的超限次数
      （指标在某份被采用的快照中出现违规即计一次）。
    - 无报告时各计数为 0；按指标次数以该分区当前必测指标清单给出零基线。
    """
    if start_at.tzinfo is None or end_at.tzinfo is None:
        raise UnprocessableError(
            "start_at and end_at must be timezone-aware, e.g. 2025-06-01T08:00:00+08:00",
            code="invalid_time_range",
        )
    start_utc = timeutil.to_utc(start_at)
    end_utc = timeutil.to_utc(end_at)
    if start_utc >= end_utc:
        raise UnprocessableError(
            "start_at must be strictly earlier than end_at (half-open [start_at, end_at))",
            code="invalid_time_range",
            details={"start_at": timeutil.iso_z(start_utc), "end_at": timeutil.iso_z(end_utc)},
        )
    start_ms, end_ms = timeutil.epoch_ms(start_utc), timeutil.epoch_ms(end_utc)

    with read_tx() as conn:
        if not conn.execute("SELECT 1 FROM partitions WHERE code=?", (partition_code,)).fetchone():
            raise NotFoundError(
                f"partition {partition_code!r} not found", code="partition_not_found"
            )

        mandatory = [
            r["indicator_code"]
            for r in conn.execute(
                "SELECT indicator_code FROM mandatory_indicators WHERE partition_code=? ORDER BY indicator_code",
                (partition_code,),
            )
        ]

        # 每个样本只取其最新已发布报告（reading_version 最大），
        # 再按采样时刻的左闭右开窗口筛样本。
        rows = conn.execute(
            """
            SELECT r.snapshot_json AS snapshot_json
            FROM samples s
            JOIN reports r ON r.sample_id = s.id
            WHERE s.partition_code = ?
              AND s.sampled_at_ms >= ? AND s.sampled_at_ms < ?
              AND r.reading_version = (
                  SELECT MAX(r2.reading_version) FROM reports r2 WHERE r2.sample_id = s.id
              )
            """,
            (partition_code, start_ms, end_ms),
        ).fetchall()

    total = 0
    qualified = 0
    violations_by_indicator: dict[str, int] = {code: 0 for code in mandatory}
    for row in rows:
        snapshot = json.loads(row["snapshot_json"])
        total += 1
        if snapshot.get("conclusion") == "qualified":
            qualified += 1
        for item in snapshot.get("violations", []):
            code = item["indicator_code"]
            violations_by_indicator[code] = violations_by_indicator.get(code, 0) + 1

    return {
        "partition_code": partition_code,
        "start_at": timeutil.iso_z(start_utc),
        "end_at": timeutil.iso_z(end_utc),
        "total_reported_samples": total,
        "qualified_count": qualified,
        "unqualified_count": total - qualified,
        "indicator_violations": dict(sorted(violations_by_indicator.items())),
    }


# ---------------------------------------------------------------- 限值调整影响预览

def _evaluate_proposed(
    value: float, min_value: float | None, max_value: float | None
) -> list[str]:
    """用拟议限值重判单个读数：min 闭端（含）、max 开端（不含）。"""
    reasons: list[str] = []
    if max_value is not None and value >= max_value:
        reasons.append(f"value {value:g} >= proposed max {max_value:g} (max is exclusive)")
    if min_value is not None and value < min_value:
        reasons.append(f"value {value:g} < proposed min {min_value:g} (min is inclusive)")
    return reasons


def preview_limit_change(partition_code: str, body: LimitPreviewIn) -> dict[str, Any]:
    """按采样时间窗口预览某指标拟议限值对已发布报告结论的影响（纯只读模拟）。

    规则：
    - 窗口左闭右开 [start_at, end_at)；起止时刻必须带时区，start >= end -> 422。
    - 拟议上下限非负有限、至少给一个、双界时 min < max，否则 422 invalid_limit。
    - 分区不存在 -> 404 partition_not_found；指标不存在 -> 404 indicator_not_found；
      指标非该分区必测项 -> 422 not_mandatory_indicator。
    - 窗口内每个样本只取其最新（reading_version 最大）已发布报告的冻结快照；
      未发布样本不计入；不读取任何当前读数，不改动限值与历史报告。
    - 快照不含目标指标项 -> 不可评估，列出标识但不参与合格数统计；
      可评估样本用拟议限值重判目标指标，其余指标沿用快照中的原判定合成模拟结论。
    """
    # 拟议限值本身的数值校验（与建限值语义一致）
    try:
        body.validate_bounds()
    except ValueError as exc:
        raise UnprocessableError(str(exc), code="invalid_limit")

    start_utc = timeutil.to_utc(body.start_at)
    end_utc = timeutil.to_utc(body.end_at)
    if start_utc >= end_utc:
        raise UnprocessableError(
            "start_at must be strictly earlier than end_at (half-open [start_at, end_at))",
            code="invalid_time_range",
            details={"start_at": timeutil.iso_z(start_utc), "end_at": timeutil.iso_z(end_utc)},
        )
    start_ms, end_ms = timeutil.epoch_ms(start_utc), timeutil.epoch_ms(end_utc)

    with read_tx() as conn:
        if not conn.execute("SELECT 1 FROM partitions WHERE code=?", (partition_code,)).fetchone():
            raise NotFoundError(
                f"partition {partition_code!r} not found", code="partition_not_found"
            )
        if not conn.execute(
            "SELECT 1 FROM indicators WHERE code=?", (body.indicator_code,)
        ).fetchone():
            raise NotFoundError(
                f"indicator {body.indicator_code!r} not found", code="indicator_not_found"
            )
        membership = conn.execute(
            "SELECT 1 FROM mandatory_indicators WHERE partition_code=? AND indicator_code=?",
            (partition_code, body.indicator_code),
        ).fetchone()
        if not membership:
            raise UnprocessableError(
                f"indicator {body.indicator_code!r} is not a mandatory indicator of partition "
                f"{partition_code!r}; only mandatory indicators can be previewed",
                code="not_mandatory_indicator",
            )

        # 与合规汇总同样的取数方式：窗口内每个样本的最新已发布报告快照
        rows = conn.execute(
            """
            SELECT s.id AS sample_id, s.sampled_at AS sampled_at,
                   r.id AS report_id, r.reading_version AS reading_version,
                   r.conclusion AS original_conclusion, r.snapshot_json AS snapshot_json
            FROM samples s
            JOIN reports r ON r.sample_id = s.id
            WHERE s.partition_code = ?
              AND s.sampled_at_ms >= ? AND s.sampled_at_ms < ?
              AND r.reading_version = (
                  SELECT MAX(r2.reading_version) FROM reports r2 WHERE r2.sample_id = s.id
              )
            ORDER BY s.sampled_at_ms, s.id
            """,
            (partition_code, start_ms, end_ms),
        ).fetchall()

    total_reported = 0
    unevaluable: list[dict[str, Any]] = []
    changed: list[dict[str, Any]] = []
    original_qualified = 0
    simulated_qualified = 0

    for row in rows:
        total_reported += 1
        snapshot = json.loads(row["snapshot_json"])
        items = snapshot.get("items", [])
        target_item = next(
            (it for it in items if it.get("indicator_code") == body.indicator_code), None
        )

        base = {
            "sample_id": row["sample_id"],
            "sampled_at": snapshot.get("sampled_at", row["sampled_at"]),
            "report_id": row["report_id"],
            "reading_version": row["reading_version"],
        }

        if target_item is None:
            # 旧版报告（发布时该指标还不是必测项等）：不可评估，不参与前后合格数
            unevaluable.append(
                {**base, "reason": f"report snapshot does not contain indicator {body.indicator_code!r}"}
            )
            continue

        original_conclusion = snapshot.get("conclusion", row["original_conclusion"])
        if original_conclusion == "qualified":
            original_qualified += 1

        # 用拟议限值重判目标指标；其他指标沿用快照中的原判定
        proposed_reasons = _evaluate_proposed(
            float(target_item["value"]), body.min_value, body.max_value
        )
        other_violations = [
            v for v in snapshot.get("violations", [])
            if v.get("indicator_code") != body.indicator_code
        ]
        simulated_conclusion = (
            "qualified" if not proposed_reasons and not other_violations else "unqualified"
        )
        if simulated_conclusion == "qualified":
            simulated_qualified += 1

        if simulated_conclusion != original_conclusion:
            changed.append(
                {
                    **base,
                    "indicator_code": body.indicator_code,
                    "frozen_value": target_item["value"],
                    "original_conclusion": original_conclusion,
                    "simulated_conclusion": simulated_conclusion,
                    "original_target_in_range": target_item.get("in_range"),
                    "simulated_target_in_range": not proposed_reasons,
                    "proposed_violations": proposed_reasons,
                    "other_indicator_violations": other_violations,
                }
            )

    return {
        "partition_code": partition_code,
        "indicator_code": body.indicator_code,
        "start_at": timeutil.iso_z(start_utc),
        "end_at": timeutil.iso_z(end_utc),
        "proposed_limits": {"min_value": body.min_value, "max_value": body.max_value},
        "total_reported_samples": total_reported,
        "evaluable_sample_count": total_reported - len(unevaluable),
        "unevaluable_samples": unevaluable,
        "original_qualified_count": original_qualified,
        "original_unqualified_count": (total_reported - len(unevaluable)) - original_qualified,
        "simulated_qualified_count": simulated_qualified,
        "simulated_unqualified_count": (total_reported - len(unevaluable)) - simulated_qualified,
        "changed_samples": changed,
    }
