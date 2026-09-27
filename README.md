# PitchKiln-01 · 灶台值守看板

Django 5 + PostgreSQL：灶台瓦片看板 + 右侧抽屉探针时间线，无 Vue/React SPA。

## 技术栈

- Django 5、PostgreSQL
- Session 登录
- HTMX：局部刷新灶台网格与抽屉
- Docker Compose：`web` + `db`

## 端口与数据库

| 服务 | 端口 |
|------|------|
| Web  | **4710** |
| Postgres | **6110**（容器内 5432） |

数据库账号：`pitchkiln` / `pitchkiln` / 库名 `pitchkiln`

## 快速启动

```bash
cd PitchKiln/PitchKiln-01
docker compose up --build -d
```

浏览器打开：http://localhost:4710

演示账号：

- `admin` / `123456`（超级用户）
- `worker` / `123456`（普通用户）

容器启动时会自动：`migrate` → `seed_data` → `collectstatic` → `gunicorn`

## 本地开发（可选）

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
pip install -r requirements.txt
# 确保本机 Postgres 监听 6110，或先 docker compose up -d db
set POSTGRES_HOST=localhost
set POSTGRES_PORT=6110
python manage.py migrate
python manage.py seed_data
python manage.py runserver 0.0.0.0:4710
```

## 业务模型

1. **ResinLot（来脂批）**：`lotCode`、`originPlace`、`arrivalKg`、`receivedAt`
2. **FireHearth（灶台）**：`lane`、`tag`（唯一）、`resinGrade`、相位 `cold|charging|ramping|holding|drawing`
3. **CookRun（熬制值守）**：归属灶台与来脂批、`openedAt`、`closedAt`（可空）、`targetSoftPointC`
4. **SoftPointProbe（软化点探针）**：归属值守、`sampledAt`、`softPointC`、`samplerName`

**业务规则**：将灶台相位切到 `drawing`（出胶）时，进行中的 CookRun 必须至少有一条 SoftPointProbe 的 `softPointC ≤ 95`。逻辑在 `apps/kiln/services/floor_rules.py`，由相位切换入口调用。

## 并发互斥（同一灶改相位）

同一灶的并发相位迁移**只许一笔成功，另一笔必须被拒**，且被拒请求不留半截相位。互斥在后端保存路径上（前端防抖不算数），由 `apps/kiln/services/floor_rules.py` 的 `change_hearth_phase` 实现，三层机制：

1. **行锁**：`SELECT ... FOR UPDATE` 锁住该灶行，把并发事务串行化（PostgreSQL；开灶 / 收灶写相位的路径同样走行锁事务）；
2. **基线校验**：抽屉表单带隐藏字段 `expected_phase`（渲染时的相位）。落库前若库中相位已偏离基线，说明有并发事务先提交了，本笔被拒；
3. **CAS 兜底**：`UPDATE ... WHERE phase=当前相位` 条件更新，即使行锁退化（SQLite）也不会两笔都落库；相同相位的无变化提交同样被拒。

「校验 + 写」在同一事务内，任何拒绝都发生在写入之前并回滚——看板、来脂批流、图例计数随后照常复算。被拒时抽屉内直接显示错误（HTMX 局部刷新），表单基线同时刷新为最新相位。

> SQLite 开发/测试后端通过 `transaction_mode: IMMEDIATE`（写事务开启即排队）获得与行锁一致的互斥语义；生产用 PostgreSQL 原生行锁。

### 并发复现

种子灶 **坳火-乙** 初始相位为 **升温（ramping）**，用它做复现。

**方式 A · 自动化测试**（恰有一笔成功、一笔被拒，之后看板 / 来脂批流 / 图例均正常）：

```bash
python manage.py test apps.kiln            # 默认 PostgreSQL
USE_SQLITE=1 python manage.py test apps.kiln   # 无本机库时
```

**方式 B · 对运行中的 compose 栈**：

```bash
docker compose up -d
bash scripts/repro_phase_race.sh
```

脚本把坳火-乙复位到升温，用两个登录会话**并发** POST 同一迁移（升温 → 保温，均带 `expected_phase=ramping`），随后检查库中最终相位、看板与来脂批流。

**并发预期**：

- 两笔请求中恰一笔生效（抽屉提示「相位已更新」），另一笔被拒（抽屉提示「相位已被并发修改…」），HTTP 均为 200（HTMX 局部刷新）；
- 库中最终相位恰为胜者所求（保温），无半截状态；
- 之后 `GET /`（看板）与 `GET /resin-lots/`（来脂批流）均 200，图例中「升温」计数归零、「保温」按最终相位复算。

## 界面

- 首页：**灶台值守看板** — 左侧班次条 + 按过道排布的灶台瓦片；点瓦片打开右侧抽屉（值守、探针时间线、改相位 / 登记探针 / 开灶）
- 次页：**来脂批** — 卡片时间线，非宽表 CRUD

## 种子数据

```bash
python manage.py seed_data
```

幂等：已有灶台则只保证账号存在。样例地名仅用「松脂坳 / 桐油坑」系。种子相位覆盖冷灶 / 装料 / 升温 / 保温 / 出胶，其中 **坳火-乙** 固定为升温，用于上节的并发复现。

## 目录结构

```
PitchKiln-01/
  manage.py
  requirements.txt
  Dockerfile
  entrypoint.sh
  docker-compose.yml
  config/
  apps/kiln/          # 模型、视图、floor_rules、种子
  templates/floor/    # 值守看板 + 抽屉
  templates/resin/    # 来脂批时间线
  static/css/         # 值守台 ops-console 样式
```
