"""合规汇总路由：按分区 + 采样时刻窗口（左闭右开）统计已发布报告。"""
from __future__ import annotations

from fastapi import APIRouter

from .. import crud
from ..schemas import AwareDatetime

router = APIRouter(tags=["compliance"])


@router.get("/partitions/{partition_code}/compliance-summary")
def compliance_summary(
    partition_code: str,
    start_at: AwareDatetime,
    end_at: AwareDatetime,
):
    """窗口 [start_at, end_at) 内每个样本取最新已发布报告后的合规汇总。

    查询参数 start_at/end_at 必须带时区偏移（如 2025-06-01T08:00:00+08:00）；
    start_at 不早于 end_at 返回 422 invalid_time_range；分区不存在返回 404
    partition_not_found。未发布样本不计入。
    """
    return crud.compliance_summary(partition_code, start_at, end_at)
