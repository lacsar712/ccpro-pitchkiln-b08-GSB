"""灶台相位切换业务规则。"""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

DRAWING_SOFT_POINT_MAX = Decimal("95")


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


def lock_hearth(hearth_id):
    """在事务内按主键行锁灶台（PostgreSQL SELECT ... FOR UPDATE）。

    必须在 ``transaction.atomic()`` 块内调用。SQLite 下行锁退化为
    普通读取，此时由 change_hearth_phase 的条件更新（CAS）兜底互斥。
    """
    from apps.kiln.models import FireHearth

    return FireHearth.objects.select_for_update().get(pk=hearth_id)


@transaction.atomic
def change_hearth_phase(*, hearth_id, new_phase, expected_phase=None):
    """并发安全地切换灶台相位：同一灶的并发迁移只许一笔落库。

    互斥机制（三层）：

    1. ``select_for_update`` 行锁把同一灶的并发事务串行化（PostgreSQL），
       后到事务阻塞至先提交者落库后再继续；
    2. ``expected_phase`` 乐观并发基线：请求所基于的相位与库中当前相位
       不一致即拒绝——先提交者胜，基于过期相位的后到者被拒；
    3. 条件 UPDATE（``WHERE phase=当前相位``）作为 CAS 兜底，即使行锁
       退化（SQLite）也不会出现两笔都落库的丢失更新。

    「校验 + 写」在同一事务内完成：任何拒绝都发生在写入之前并触发
    回滚，库中不会留下半截相位。
    """
    from apps.kiln.models import FireHearth

    hearth = lock_hearth(hearth_id)
    current = hearth.phase

    if expected_phase and expected_phase != current:
        raise ValidationError(
            {
                "phase": (
                    "相位已被并发修改"
                    f"（当前为「{hearth.get_phase_display()}」），"
                    "本次切换未生效，请按最新相位重试。"
                )
            }
        )

    if new_phase == current:
        raise ValidationError(
            {"phase": "相位未变化：目标相位与当前相位相同（可能已被并发修改）。"}
        )

    if new_phase == FireHearth.PHASE_DRAWING:
        assert_can_enter_drawing(hearth)

    # CAS 落库：仅当相位仍是刚读到的值时才更新。
    updated = (
        FireHearth.objects.filter(pk=hearth.pk, phase=current)
        .update(phase=new_phase)
    )
    if updated != 1:
        # 行锁退化的后端上，并发事务已抢先落库。
        raise ValidationError(
            {"phase": "相位已被并发修改，本次切换未生效，请刷新后重试。"}
        )

    hearth.phase = new_phase
    return hearth


@transaction.atomic
def open_run_for_hearth(*, hearth_id, run):
    """在行锁内开新值守：并发的第二笔开灶被拒，不会留下两条开着的值守。

    ``run`` 为未保存的 CookRun 实例（由表单构建）；灶台相位为冷灶时
    随开灶进入装料。整个「查重 + 开灶 + 改相位」在同一事务内完成。
    """
    from apps.kiln.models import FireHearth

    hearth = lock_hearth(hearth_id)
    if hearth.open_run() is not None:
        raise ValidationError("该灶已有进行中的值守，请先收灶再开新灶。")

    run.hearth = hearth
    run.save()
    if hearth.phase == FireHearth.PHASE_COLD:
        hearth.phase = FireHearth.PHASE_CHARGING
        hearth.save(update_fields=["phase"])
    return run


@transaction.atomic
def close_run_for_hearth(*, hearth_id):
    """在行锁内收灶：关闭当前值守并把灶台打回冷灶（同事务原子完成）。"""
    from apps.kiln.models import FireHearth

    hearth = lock_hearth(hearth_id)
    open_run = hearth.open_run()
    if open_run is None:
        raise ValidationError("没有进行中的值守可收灶。")

    open_run.closedAt = timezone.now()
    open_run.save(update_fields=["closedAt"])
    hearth.phase = FireHearth.PHASE_COLD
    hearth.save(update_fields=["phase"])
    return open_run
