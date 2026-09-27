"""并发改灶相位的互斥测试。

用真实多线程打服务层（非打桩），每个线程持有独立数据库连接，
屏障同步后同时提交，验证：
  * 同一旧相位出发的迁移只许一笔成功，其余必拒（PhaseConflictError）；
  * 被拒请求不留下半截状态：最终相位唯一且等于获胜目标；
  * 冲突之后看板上下文 / 网格 / 图例仍可正常复算。
"""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier

from django.db import connections
from django.test import TransactionTestCase
from django.utils import timezone

from .models import CookRun, FireHearth, ResinLot
from .services.floor_rules import PhaseConflictError, change_hearth_phase
from .views import _board_context


class PhaseConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.lot = ResinLot.objects.create(
            lotCode="脂-并发试批",
            originPlace="松脂坳东沟",
            arrivalKg=Decimal("10.00"),
            receivedAt=timezone.now(),
        )
        # 一灶处于升温，且已有一条 ≤95℃ 探针 —— 升温→保温 与
        # 升温→出胶 两条迁移都合法，便于成对赛跑。
        self.hearth = FireHearth.objects.create(
            lane=9, tag="并发灶", resinGrade="特级脂", phase=FireHearth.PHASE_RAMPING
        )
        run = CookRun.objects.create(
            hearth=self.hearth,
            resinLot=self.lot,
            openedAt=timezone.now(),
            targetSoftPointC=Decimal("90.00"),
        )
        run.probes.create(
            sampledAt=timezone.now(),
            softPointC=Decimal("93.00"),
            samplerName="并发测试",
        )

    def _race(self, targets):
        """每个线程从同一旧相位 ramping 出发改到 targets[i]。"""
        barrier = Barrier(len(targets))

        def run(new_phase):
            # 读实例放在屏障之前：开赛之后只有条件 UPDATE 这一条写语句在赛跑，
            # 避免 SQLite 共享缓存下「读撞写」的表锁（Postgres MVCC 无此问题）。
            h = FireHearth.objects.get(pk=self.hearth.pk)
            barrier.wait()
            try:
                change_hearth_phase(
                    h, new_phase, expected_phase=FireHearth.PHASE_RAMPING
                )
                return ("ok", new_phase)
            except PhaseConflictError:
                return ("rejected", new_phase)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=len(targets)) as pool:
            return list(pool.map(run, targets))

    def test_two_identical_legal_moves_only_one_wins(self):
        results = self._race(
            [FireHearth.PHASE_HOLDING, FireHearth.PHASE_HOLDING]
        )

        ok = [r for r in results if r[0] == "ok"]
        rejected = [r for r in results if r[0] == "rejected"]
        self.assertEqual(len(ok), 1, f"应恰有一笔成功，实际：{results}")
        self.assertEqual(len(rejected), 1)

        self.hearth.refresh_from_db()
        self.assertEqual(self.hearth.phase, FireHearth.PHASE_HOLDING)

    def test_two_distinct_legal_moves_only_one_wins(self):
        results = self._race(
            [FireHearth.PHASE_HOLDING, FireHearth.PHASE_DRAWING]
        )

        self.assertEqual(sum(1 for r in results if r[0] == "ok"), 1)
        self.assertEqual(sum(1 for r in results if r[0] == "rejected"), 1)

        self.hearth.refresh_from_db()
        winner_phase = next(r[1] for r in results if r[0] == "ok")
        self.assertEqual(self.hearth.phase, winner_phase)

    def test_stale_request_after_commit_is_rejected(self):
        """先一笔已提交成功，后来者仍带着旧 expected_phase —— 必须被拒。"""
        first = FireHearth.objects.get(pk=self.hearth.pk)
        change_hearth_phase(
            first, FireHearth.PHASE_HOLDING, expected_phase=FireHearth.PHASE_RAMPING
        )

        stale = FireHearth.objects.get(pk=self.hearth.pk)
        with self.assertRaises(PhaseConflictError):
            change_hearth_phase(
                stale,
                FireHearth.PHASE_DRAWING,
                expected_phase=FireHearth.PHASE_RAMPING,
            )

        self.hearth.refresh_from_db()
        self.assertEqual(self.hearth.phase, FireHearth.PHASE_HOLDING)

    def test_board_still_renders_after_conflict(self):
        """落败之后看板、图例与过滤瓦片仍可复算（不留半截状态）。"""
        results = self._race([FireHearth.PHASE_HOLDING, FireHearth.PHASE_DRAWING])
        winner_phase = next(r[1] for r in results if r[0] == "ok")

        ctx = _board_context()
        self.assertEqual([h.phase for h in ctx["hearths"]], [winner_phase])
        # 图例计数与实际灶数一致，胜方相位计 1
        self.assertEqual(sum(count for _, _, count in ctx["phase_legend"]), 1)
        legend = {key: count for key, _, count in ctx["phase_legend"]}
        self.assertEqual(legend[winner_phase], 1)
