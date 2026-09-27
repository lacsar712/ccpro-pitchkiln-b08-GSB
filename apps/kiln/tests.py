"""相位切换的并发互斥与看板健康测试。

运行：
    python manage.py test apps.kiln

默认走 PostgreSQL（行锁原生生效）；本地无库时可：
    USE_SQLITE=1 python manage.py test apps.kiln
（SQLite 下行锁退化，由条件更新 CAS 兜底，测试同样应通过。）
"""
import threading
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connections
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from .models import CookRun, FireHearth, ResinLot, SoftPointProbe
from .services.floor_rules import change_hearth_phase


def make_hearth_with_run(*, phase=FireHearth.PHASE_RAMPING, tag="并发灶"):
    """造一座「升温」中的灶（默认），带一条进行中的值守。"""
    lot = ResinLot.objects.create(
        lotCode=f"脂-测试-{tag}",
        originPlace="松脂坳东沟",
        arrivalKg=Decimal("100.00"),
        receivedAt=timezone.now(),
    )
    hearth = FireHearth.objects.create(
        lane=9, tag=tag, resinGrade="一级脂", phase=phase
    )
    run = CookRun.objects.create(
        hearth=hearth,
        resinLot=lot,
        openedAt=timezone.now(),
        targetSoftPointC=Decimal("90.00"),
    )
    return hearth, run


class PhaseChangeServiceTest(TestCase):
    """change_hearth_phase 的互斥契约（确定性的串行形态）。"""

    def setUp(self):
        self.hearth, self.run = make_hearth_with_run(tag="规则灶")

    def test_legal_transition_commits(self):
        hearth = change_hearth_phase(
            hearth_id=self.hearth.pk,
            new_phase=FireHearth.PHASE_HOLDING,
            expected_phase=FireHearth.PHASE_RAMPING,
        )
        self.assertEqual(hearth.phase, FireHearth.PHASE_HOLDING)
        self.hearth.refresh_from_db()
        self.assertEqual(self.hearth.phase, FireHearth.PHASE_HOLDING)

    def test_stale_expected_phase_rejected_without_write(self):
        with self.assertRaises(ValidationError):
            change_hearth_phase(
                hearth_id=self.hearth.pk,
                new_phase=FireHearth.PHASE_HOLDING,
                expected_phase=FireHearth.PHASE_CHARGING,  # 过期基线
            )
        self.hearth.refresh_from_db()
        self.assertEqual(self.hearth.phase, FireHearth.PHASE_RAMPING)

    def test_second_transition_after_commit_is_rejected(self):
        """先提交者胜；基于同一旧相位的后到事务必须被拒。"""
        change_hearth_phase(
            hearth_id=self.hearth.pk,
            new_phase=FireHearth.PHASE_HOLDING,
            expected_phase=FireHearth.PHASE_RAMPING,
        )
        with self.assertRaises(ValidationError):
            change_hearth_phase(
                hearth_id=self.hearth.pk,
                new_phase=FireHearth.PHASE_HOLDING,
                expected_phase=FireHearth.PHASE_RAMPING,
            )
        self.hearth.refresh_from_db()
        self.assertEqual(self.hearth.phase, FireHearth.PHASE_HOLDING)

    def test_noop_transition_rejected(self):
        with self.assertRaises(ValidationError):
            change_hearth_phase(
                hearth_id=self.hearth.pk,
                new_phase=FireHearth.PHASE_RAMPING,
                expected_phase=FireHearth.PHASE_RAMPING,
            )

    def test_drawing_requires_qualified_probe(self):
        # 无 ≤95℃ 探针：拒，且相位保持升温（不留半截状态）
        with self.assertRaises(ValidationError):
            change_hearth_phase(
                hearth_id=self.hearth.pk,
                new_phase=FireHearth.PHASE_DRAWING,
                expected_phase=FireHearth.PHASE_RAMPING,
            )
        self.hearth.refresh_from_db()
        self.assertEqual(self.hearth.phase, FireHearth.PHASE_RAMPING)

        # 补上合格探针后放行
        SoftPointProbe.objects.create(
            run=self.run,
            sampledAt=timezone.now(),
            softPointC=Decimal("94.50"),
            samplerName="值守测试",
        )
        hearth = change_hearth_phase(
            hearth_id=self.hearth.pk,
            new_phase=FireHearth.PHASE_DRAWING,
            expected_phase=FireHearth.PHASE_RAMPING,
        )
        self.assertEqual(hearth.phase, FireHearth.PHASE_DRAWING)


def post_phase(client, hearth_pk, data):
    """以 HTMX 形态提交一笔相位切换。"""
    return client.post(
        reverse("change_phase", args=[hearth_pk]), data, HTTP_HX_REQUEST="true"
    )


class PhaseConcurrencyTest(TransactionTestCase):
    """两个并发请求改同一灶的相位：只许一笔成功，另一笔必须被拒。"""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="worker", password="pw-123456"
        )
        # 种子形态：一座「升温」中的灶
        self.hearth, self.run = make_hearth_with_run(
            phase=FireHearth.PHASE_RAMPING, tag="并发灶"
        )

    def _new_client(self):
        client = Client()
        client.force_login(self.user)
        return client

    def test_concurrent_same_transition_exactly_one_wins(self):
        clients = [self._new_client(), self._new_client()]
        barrier = threading.Barrier(2)
        responses = [None, None]
        errors = [None, None]
        data = {
            "phase": FireHearth.PHASE_HOLDING,
            "expected_phase": FireHearth.PHASE_RAMPING,
        }

        def worker(idx):
            try:
                barrier.wait(timeout=10)  # 尽量让两笔请求同时打进去
                responses[idx] = post_phase(clients[idx], self.hearth.pk, data)
            except Exception as exc:  # noqa: BLE001 - 测试里原样上报
                errors[idx] = exc
            finally:
                connections.close_all()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertFalse(
            any(t.is_alive() for t in threads), "并发线程未在限时内结束"
        )
        self.assertEqual(errors, [None, None], f"并发请求抛出异常：{errors}")

        bodies = [r.content.decode() for r in responses]

        # 恰有一笔成功、恰有一笔被拒
        wins = [b for b in bodies if "相位已更新" in b]
        losses = [b for b in bodies if "并发修改" in b or "相位未变化" in b]
        self.assertEqual(len(wins), 1, f"应恰有一笔成功：{bodies}")
        self.assertEqual(len(losses), 1, f"应恰有一笔被拒：{bodies}")

        # 库中只留下胜者的相位，无半截状态
        self.hearth.refresh_from_db()
        self.assertEqual(self.hearth.phase, FireHearth.PHASE_HOLDING)

        # 被拒响应里的抽屉已按最新相位回渲染（前端可据此收敛）
        self.assertIn("phase-holding", losses[0])

        # 看板仍可打开，图例按最终相位复算
        board = clients[0].get(reverse("home"))
        self.assertEqual(board.status_code, 200)
        legend = {key: count for key, _label, count in board.context["phase_legend"]}
        self.assertEqual(legend[FireHearth.PHASE_HOLDING], 1)
        self.assertEqual(legend[FireHearth.PHASE_RAMPING], 0)

        # 来脂批流仍可打开
        feed = clients[0].get(reverse("resin_lot_feed"))
        self.assertEqual(feed.status_code, 200)

    def test_concurrent_transition_without_baseline_one_wins(self):
        """不带 expected_phase 的相同迁移并发到达：同样只许一笔落库。

        第二笔在行锁后读到「目标相位 == 当前相位」，按无变化拒绝。
        """
        clients = [self._new_client(), self._new_client()]
        barrier = threading.Barrier(2)
        responses = [None, None]
        errors = [None, None]
        data = {"phase": FireHearth.PHASE_HOLDING}

        def worker(idx):
            try:
                barrier.wait(timeout=10)
                responses[idx] = post_phase(clients[idx], self.hearth.pk, data)
            except Exception as exc:  # noqa: BLE001 - 测试里原样上报
                errors[idx] = exc
            finally:
                connections.close_all()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(errors, [None, None], f"并发请求抛出异常：{errors}")

        bodies = [r.content.decode() for r in responses]
        wins = [b for b in bodies if "相位已更新" in b]
        self.assertEqual(len(wins), 1, f"应恰有一笔成功：{bodies}")

        self.hearth.refresh_from_db()
        self.assertEqual(self.hearth.phase, FireHearth.PHASE_HOLDING)

        board = clients[0].get(reverse("home"))
        self.assertEqual(board.status_code, 200)
