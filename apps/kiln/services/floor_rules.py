"""灶台相位切换业务规则。"""
import time
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import OperationalError, transaction

DRAWING_SOFT_POINT_MAX = Decimal("95")


class PhaseConflictError(ValidationError):
    """并发改相位冲突：本请求基于的旧相位已被另一笔请求抢先更新。"""


def assert_can_enter_drawing(hearth) -> None:
    """
    进入「出胶」相位前：当前未收灶的 CookRun 须至少有一条
    softPointC <= 95 的 SoftPointProbe。
    """
    open_run = hearth.open_run()
    if open_run is None:
        raise ValidationError(
            {"phase": "无法进入出胶：该灶没有进行中的值守纪录。"}
        )

    ok = open_run.probes.filter(softPointC__lte=DRAWING_SOFT_POINT_MAX).exists()
    if not ok:
        raise ValidationError(
            {
                "phase": (
                    "无法进入出胶：进行中值守尚无软化点探针 "
                    f"≤ {DRAWING_SOFT_POINT_MAX}℃。"
                )
            }
        )


def _conflict(hearth) -> PhaseConflictError:
    if hearth is None:
        detail = "相位已被另一笔请求抢先更新，本次已拒绝，请刷新后重试。"
    else:
        detail = (
            "相位已被另一笔请求抢先更新"
            f"（当前为「{hearth.get_phase_display()}」），"
            "本次已拒绝，请刷新后重试。"
        )
    return PhaseConflictError({"phase": detail})


def change_hearth_phase(hearth, new_phase: str, expected_phase: str | None = None):
    """
    统一入口：改相位时校验出胶规则并保存。

    后端互斥（前端防抖不算数，以此为准）——条件 UPDATE 即行锁/CAS：
      1. 单条 ``UPDATE … WHERE pk = 灶 AND phase = 旧相位`` 作为互斥原语。
         PostgreSQL 上该 UPDATE 会取行锁，并发写串行；只有 WHERE 命中的
         一笔能改写，另一笔命中 0 行即被拒。
      2. SQLite 的锁是库/表级：并发写撞锁（OperationalError）时短暂
         退避重试 —— 若是别人在改别的灶，重试即过；若是同灶并发，
         对方落库后本笔 CAS 必命中 0 行，仍按冲突被拒。互斥语义不变。
      3. 命中后已持有写锁，再在锁内校验出胶规则；校验失败抛错，
         整个事务回滚，相位不留半截。
      4. 落败方重读当前相位，带在拒绝提示里返回。

    同相位提交（new == expected）也走同一条 CAS：相位未动则幂等成功，
    相位已被他人改动则同样被拒 —— 提交者看到的永远是最新事实。

    ``expected_phase`` 是提交者打开抽屉时看到的相位（表单隐藏域），
    缺省取调用方实例上的相位。
    """
    from apps.kiln.models import FireHearth

    if expected_phase is None:
        expected_phase = hearth.phase

    updated = 0
    for attempt in range(3):
        try:
            with transaction.atomic():
                updated = FireHearth.objects.filter(
                    pk=hearth.pk, phase=expected_phase
                ).update(phase=new_phase)
                # 仅 CAS 命中的那笔才在写锁内校验出胶规则；失败则整体回滚。
                if updated == 1 and new_phase == FireHearth.PHASE_DRAWING:
                    assert_can_enter_drawing(hearth)
            break
        except OperationalError as exc:
            if "lock" not in str(exc).lower():
                raise
            if attempt == 2:
                break
            time.sleep(0.05 * (attempt + 1))

    if updated != 1:
        try:
            current = FireHearth.objects.only("phase").get(pk=hearth.pk)
        except OperationalError:
            current = None
        raise _conflict(current)

    hearth.phase = new_phase
    return hearth
