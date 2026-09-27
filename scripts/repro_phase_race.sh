#!/usr/bin/env bash
# 并发改同一灶相位 —— 可写复现脚本。
#
# 前置：docker compose up -d（web 暴露 5060，种子数据已写入）。
# 种子灶「坳火-乙」初始相位为 升温(ramping)。本脚本把它复位到升温后，
# 用两个登录会话并发提交同一笔迁移（升温 -> 保温），验证：
#   1) 恰有一笔成功、另一笔被拒（后端行锁 + 基线校验，不靠前端防抖）；
#   2) 被拒请求不留半截相位；
#   3) 之后看板 / 来脂批流仍可打开，图例计数按最终相位复算。
#
# 用法：bash scripts/repro_phase_race.sh
# 可调环境变量：BASE_URL / HEARTH_TAG / TARGET_PHASE / DEMO_USER / DEMO_PASS
set -euo pipefail

BASE="${BASE_URL:-http://localhost:5060}"
TAG="${HEARTH_TAG:-坳火-乙}"
FROM_PHASE="ramping"                    # 升温
TO_PHASE="${TARGET_PHASE:-holding}"     # 保温
USER="${DEMO_USER:-worker}"
PASS="${DEMO_PASS:-123456}"
COMPOSE="${COMPOSE:-docker compose}"

note() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
fail() { printf '\033[31mFAIL: %s\033[0m\n' "$*" >&2; exit 1; }

note "0. 复位种子灶「$TAG」为 升温($FROM_PHASE) 并取其主键"
PK=$($COMPOSE exec -T web python manage.py shell -c "
from apps.kiln.models import FireHearth
h = FireHearth.objects.get(tag='$TAG')
h.phase = '$FROM_PHASE'
h.save(update_fields=['phase'])
print(h.pk)
" | grep -xE '[0-9]+')
[ -n "$PK" ] || fail "取不到灶「$TAG」的主键（compose 栈是否在运行？）"
echo "hearth tag=$TAG pk=$PK phase=$FROM_PHASE"

JAR_A=$(mktemp); JAR_B=$(mktemp)
OUT_A=$(mktemp); OUT_B=$(mktemp)
CODE_A=$(mktemp); CODE_B=$(mktemp)
trap 'rm -f "$JAR_A" "$JAR_B" "$OUT_A" "$OUT_B" "$CODE_A" "$CODE_B"' EXIT

csrf_of() { awk '$6 == "csrftoken" {print $7}' "$1"; }

login() { # $1 = cookie jar
  curl -s -c "$1" "$BASE/login/" -o /dev/null
  local token; token=$(csrf_of "$1")
  curl -s -b "$1" -c "$1" -o /dev/null \
    -H "X-CSRFToken: $token" -H "Referer: $BASE/login/" \
    --data-urlencode "csrfmiddlewaretoken=$token" \
    --data-urlencode "username=$USER" \
    --data-urlencode "password=$PASS" \
    "$BASE/login/"
}

note "1. 两个会话分别登录（$USER）"
login "$JAR_A"; login "$JAR_B"
grep -q sessionid "$JAR_A" || fail "会话 A 登录失败"
grep -q sessionid "$JAR_B" || fail "会话 B 登录失败"
echo "两个会话均已登录"

post_phase() { # $1 = jar, $2 = response body file, $3 = http code file
  local token; token=$(csrf_of "$1")
  curl -s -b "$1" -o "$2" -w "%{http_code}" \
    -H "HX-Request: true" -H "X-CSRFToken: $token" -H "Referer: $BASE/" \
    --data-urlencode "phase=$TO_PHASE" \
    --data-urlencode "expected_phase=$FROM_PHASE" \
    "$BASE/hearth/$PK/phase/" > "$3"
}

note "2. 并发提交同一迁移：升温 -> 保温（两笔都基于 expected_phase=$FROM_PHASE）"
post_phase "$JAR_A" "$OUT_A" "$CODE_A" &
PID_A=$!
post_phase "$JAR_B" "$OUT_B" "$CODE_B" &
PID_B=$!
wait "$PID_A" "$PID_B"
echo "HTTP 状态：A=$(cat "$CODE_A")  B=$(cat "$CODE_B")"

WINS=0; LOSSES=0
for f in "$OUT_A" "$OUT_B"; do
  grep -q "相位已更新" "$f" && WINS=$((WINS + 1)) || true
  { grep -q "并发修改" "$f" || grep -q "相位未变化" "$f"; } && LOSSES=$((LOSSES + 1)) || true
done
echo "成功笔数=$WINS  被拒笔数=$LOSSES"
[ "$WINS" -eq 1 ]   || fail "应恰有一笔成功，实际 $WINS"
[ "$LOSSES" -eq 1 ] || fail "应恰有一笔被拒，实际 $LOSSES"

note "3. 库中最终相位（应恰为 $TO_PHASE，无半截状态）"
FINAL=$($COMPOSE exec -T web python manage.py shell -c "
from apps.kiln.models import FireHearth
print(FireHearth.objects.get(pk=$PK).phase)
" | grep -xE '[a-z]+')
echo "final phase = $FINAL"
[ "$FINAL" = "$TO_PHASE" ] || fail "最终相位应为 $TO_PHASE，实际 $FINAL"

note "4. 看板 / 来脂批流健康检查"
BOARD_CODE=$(curl -s -b "$JAR_A" -o /tmp/pk_board.html -w "%{http_code}" "$BASE/")
FEED_CODE=$(curl -s -b "$JAR_A" -o /dev/null -w "%{http_code}" "$BASE/resin-lots/")
echo "GET /            -> $BOARD_CODE"
echo "GET /resin-lots/ -> $FEED_CODE"
[ "$BOARD_CODE" = 200 ] || fail "看板打不开"
[ "$FEED_CODE" = 200 ]  || fail "来脂批流打不开"
echo "图例复算：$(grep -o '升温 [0-9]*' /tmp/pk_board.html | head -1) / $(grep -o '保温 [0-9]*' /tmp/pk_board.html | head -1)"
grep -q "升温 0" /tmp/pk_board.html || fail "图例未复算：仍有过期升温计数"
rm -f /tmp/pk_board.html

note "PASS：一笔成功、一笔被拒；看板与来脂批流正常，图例已复算"
