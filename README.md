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

## 并发改相位（后端互斥）

两名值班员可能同时对**同一灶**从**同一旧相位**发起迁移（例如升温灶：一笔改「保温」、一笔改「出胶」，两笔都合法）。互斥在**后端**实现，前端按钮防抖只是体验兜底，不作数：

1. `change_hearth_phase()` 用单条条件 `UPDATE … WHERE pk = 灶 AND phase = 旧相位` 做互斥（CAS）：PostgreSQL 上该 UPDATE 取行锁，并发写串行，只有 WHERE 命中的一笔能落库；
2. 旧相位来自表单隐藏域 `expected_phase`（打开抽屉时的相位）；命中 0 行即抛 `PhaseConflictError`，拒绝提示里带当前相位；
3. SQLite 没有行锁：并发写撞库/表锁时短暂退避重试，再由同一条 CAS 判定胜负 —— 互斥语义不变；
4. 出胶探针校验在写锁内、与写入同事务：校验失败或被拒都整体回滚，**不留半截相位**。

**并发预期**：同时到达的多笔合法迁移，恰好一笔成功，其余全部收到「相位已被另一笔请求抢先更新……本次已拒绝」；落败后刷新看板（`floor-refresh` 重算网格、图例计数、瓦片）与来脂批流均正常，抽屉显示胜方写入的最新相位。

### 复现步骤

服务层复现（两线程、独立数据库连接、屏障同时提交）：

```bash
# 容器内
python manage.py seed_data            # 幂等；种子升温灶「坳火-乙」含 ≤95℃ 探针
python manage.py race_phase_change    # 默认升温→保温 vs 升温→出胶
# 输出应为：一笔「成功」、一笔「被拒」，终态唯一，末行绿色「符合并发预期」
python manage.py race_phase_change --reset-to ramping   # 复位升温后再赛一轮
# 可选参数：--tag 坳火-乙 --targets holding,drawing
```

HTTP 层复现：两个浏览器会话（`admin` / `worker`）同时打开升温灶抽屉（两页 `expected_phase` 均为 `ramping`），各自选不同合法相位后同一瞬间提交；结果为一个成功 toast、一个红色拒绝 toast，两页随后看到相同终态。

自动化测试（真实线程竞赛，非 mock）：

```bash
python manage.py test apps.kiln -v 2
```

## 界面

- 首页：**灶台值守看板** — 左侧班次条 + 按过道排布的灶台瓦片；点瓦片打开右侧抽屉（值守、探针时间线、改相位 / 登记探针 / 开灶）
- 次页：**来脂批** — 卡片时间线，非宽表 CRUD

## 种子数据

```bash
python manage.py seed_data
```

幂等：已有灶台则只保证账号存在。样例地名仅用「松脂坳 / 桐油坑」系。

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
