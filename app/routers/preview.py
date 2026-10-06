"""限值调整影响预览路由：按分区 + 采样窗口对拟议限值做只读模拟重判。"""
from __future__ import annotations

from fastapi import APIRouter

from .. import crud
from ..schemas import LimitPreviewIn

router = APIRouter(tags=["limit-preview"])


@router.post("/partitions/{partition_code}/limit-change-preview")
def preview_limit_change(partition_code: str, body: LimitPreviewIn):
    """窗口 [start_at, end_at) 内每个样本取其最新已发布报告的冻结快照，
    用拟议上下限重判目标指标，其余指标沿用原判定合成模拟结论。

    纯只读：不读取当前读数，不改动限值与历史报告。
    - 时间必须带时区；start_at >= end_at -> 422 invalid_time_range。
    - 拟议限值非负有限、至少一个界限、双界 min < max，否则 422 invalid_limit。
    - 分区不存在 -> 404 partition_not_found；指标不存在 -> 404 indicator_not_found；
      指标非该分区必测项 -> 422 not_mandatory_indicator。
    - 快照不含目标指标的样本列为不可评估，不参与前后合格数。
    """
    return crud.preview_limit_change(partition_code, body)
