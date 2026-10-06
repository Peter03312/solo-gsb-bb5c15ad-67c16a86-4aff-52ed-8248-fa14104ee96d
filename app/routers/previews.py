"""限值调整影响预览路由：按采样窗口模拟拟议限值对已发布报告结论的影响（只读）。"""
from __future__ import annotations

from fastapi import APIRouter

from .. import crud
from ..schemas import LimitChangePreviewIn

router = APIRouter(tags=["compliance"])


@router.post("/partitions/{partition_code}/limit-change-preview")
def limit_change_preview(partition_code: str, body: LimitChangePreviewIn):
    """窗口 [start_at, end_at) 内每个样本取最新已发布报告，用拟议限值重判目标指标。

    纯只读模拟：不读取样本当前读数，不改动限值与历史报告。快照未含目标指标的报告
    列为不可评估，不参与前后合格数；其余指标沿用原快照判定合成模拟结论。
    分区不存在 404 partition_not_found；指标不存在 404 indicator_not_found；
    指标非该分区必测项 422 not_mandatory_indicator；时间/限值非法 422
    invalid_time_range / invalid_limit；时间不带时区 422 validation_error。
    """
    return crud.preview_limit_change(
        partition_code,
        body.indicator_code,
        body.start_at,
        body.end_at,
        body.min_value,
        body.max_value,
    )
