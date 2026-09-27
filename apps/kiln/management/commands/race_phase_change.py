"""并发改同一灶相位的复现命令。

两个线程各自持有独立数据库连接，屏障同步后同时把同一灶从同一旧相位
改向各自目标（默认：升温→保温 与 升温→出胶，两条迁移均合法）。

预期：恰好一笔成功，另一笔抛 PhaseConflictError 被拒；终态唯一，
看板/抽屉/图例随后仍可正常打开复算。

建议在 PostgreSQL 上运行（SQLite 的行锁为空操作，仅靠条件 UPDATE 兜底）：

    python manage.py migrate
    python manage.py seed_data
    python manage.py race_phase_change
    # 重复复现：先把灶复位回升温
    python manage.py race_phase_change --reset-to ramping
    # 自定义并发目标：
    python manage.py race_phase_change --targets holding,drawing
"""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from django.core.management.base import BaseCommand, CommandError
from django.db import connections

from apps.kiln.models import FireHearth
from apps.kiln.services.floor_rules import PhaseConflictError, change_hearth_phase


class Command(BaseCommand):
    help = "并发复现：两笔合法相位迁移同时打到同一灶，只许一笔成功。"

    def add_arguments(self, parser):
        parser.add_argument(
            "--tag", default="坳火-乙", help="目标灶牌（默认种子中的升温灶）"
        )
        parser.add_argument(
            "--targets",
            default="holding,drawing",
            help="逗号分隔的并发目标相位，如 holding,drawing",
        )
        parser.add_argument(
            "--reset-to",
            default=None,
            metavar="PHASE",
            help="先把该灶相位直接复位为 PHASE 再开赛（测试用，绕过业务规则）",
        )

    def handle(self, *args, **options):
        try:
            hearth = FireHearth.objects.get(tag=options["tag"])
        except FireHearth.DoesNotExist:
            raise CommandError(
                f"找不到灶牌「{options['tag']}」，请先 python manage.py seed_data"
            )

        labels = dict(FireHearth.PHASE_CHOICES)
        valid = set(labels)

        if options["reset_to"]:
            reset_to = options["reset_to"]
            if reset_to not in valid:
                raise CommandError(f"--reset-to 非法：{reset_to}，可选 {sorted(valid)}")
            FireHearth.objects.filter(pk=hearth.pk).update(phase=reset_to)
            hearth.refresh_from_db()
            self.stdout.write(f"已复位：{hearth.tag} → {labels[reset_to]}")

        before = hearth.phase
        targets = [t.strip() for t in options["targets"].split(",") if t.strip()]
        if len(targets) < 2:
            raise CommandError("至少需要两个并发目标相位")
        bad = [t for t in targets if t not in valid]
        if bad:
            raise CommandError(f"目标相位非法：{bad}，可选 {sorted(valid)}")
        if before in targets:
            raise CommandError(
                f"目标相位包含当前相位 {before}（{labels[before]}），"
                "那是幂等空操作而非迁移，请换目标或用 --reset-to 复位"
            )

        self.stdout.write(
            f"灶 {hearth.tag} 当前相位：{before}（{labels[before]}）；"
            f"并发目标：{[labels[t] for t in targets]}"
        )

        barrier = Barrier(len(targets))

        def race(new_phase):
            # 每个线程独立连接；显式从 DB 读自己的实例，避免共享内存对象。
            h = FireHearth.objects.get(pk=hearth.pk)
            barrier.wait()
            try:
                change_hearth_phase(h, new_phase, expected_phase=before)
                return ("成功", new_phase, "")
            except PhaseConflictError as exc:
                msg = exc.message_dict.get("phase", [""])[0]
                return ("被拒", new_phase, msg)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=len(targets)) as pool:
            results = list(pool.map(race, targets))

        wins = [r for r in results if r[0] == "成功"]
        loses = [r for r in results if r[0] == "被拒"]
        for status, phase, msg in results:
            line = f"  [{status}] → {labels[phase]}"
            if msg:
                line += f"：{msg}"
            self.stdout.write(line)

        hearth.refresh_from_db()
        self.stdout.write(f"终态：{hearth.phase}（{labels[hearth.phase]}）")

        if len(wins) == 1 and len(loses) == len(targets) - 1:
            self.stdout.write(
                self.style.SUCCESS(
                    "符合并发预期：恰有一笔成功，其余被拒，无半截相位。"
                )
            )
        else:
            raise CommandError(
                f"不符合并发预期：成功 {len(wins)} 笔 / 被拒 {len(loses)} 笔"
            )
