# 轨迹停留分析 API（Trajectory Stay Analysis）

基于 **FastAPI** 的轨迹停留点（Stay Point）分析与跨时段热点归并服务。客户端提交带时间戳、经纬度的**有序轨迹**，
以及距离阈值、最短停留时长阈值；服务按 **Haversine** 距离和时间间隔**流式合并**连续点，
识别停留区段的**进入 / 离开**时间与**跨采样间断**，返回每个停留的**时长、中心点、原始点下标范围**，
并附带 **GeoJSON** 结果与**总里程、停留次数**等统计。`/api/v1/hotspots` 进一步在停留结果之上
以**中心连通分量**归并跨时段重复到访，输出热点（含传递扩展、稳定编号、时长加权中心）与噪声标记；
`/api/v1/geofence-visits` 则以停留**中心点**对带洞、可跨 180° 经线的 GeoJSON 围栏做点面归属，
返回每段停留的 `zone_id`（未命中为 `null`）及按围栏汇总的访问次数与累计停留时长。

## 功能特性

- 流式停留检测：逐点扫描合并，经典 stay-point 算法（锚点距离判定），时间复杂度 O(n)
- 跨采样间断处理：相邻点间隔超过 `max_gap_s` 强制切分段落，间断时间不计入停留时长
- 每个停留返回：进入 / 离开时间、停留时长、中心点（簇内均值；跨 180° 经线的簇用圆均值）、原始点下标闭区间、点数
- 整体统计：总里程（Haversine 累计）、轨迹总时长、停留次数、停留总时长
- GeoJSON 输出：轨迹 LineString + 停留点 Point 的 FeatureCollection，可直接上图（GeoJSON.io / Kepler 等）
- **跨时段热点归并**：停留中心间距离 ≤ 半径连无向边，并查集求连通分量；支持“甲邻乙、乙邻丙、
  甲不邻丙”的传递扩展，结果不随遍历顺序改变；热点按最早访问时间稳定编号，中心按停留时长加权
- **围栏访问统计**：GeoJSON Polygon / MultiPolygon 点面归属；外环边界视为命中，洞内及洞边界视为
  未命中；多边形可跨越 180° 经线（无需预切割）；多围栏同时命中按 `priority` 降序、`zone_id`
  字典序唯一决胜；汇总保留零访问围栏
- 时间戳兼容 ISO 8601 字符串与 Unix 秒（epoch）；参数校验失败返回 422

## 快速开始

一条命令启动（Docker Compose，自动构建并在前台运行，监听 **8000** 端口）：

```bash
docker compose up --build
```

### 方式一：Docker Compose（一键启动）

```bash
docker compose up --build
```

### 方式二：Dockerfile

```bash
docker build -t staypoint-api .
docker run -d -p 8000:8000 --name staypoint-api staypoint-api
```

### 方式三：本地运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

启动后：

- 服务地址：http://localhost:8000
- 交互式文档（Swagger UI）：http://localhost:8000/docs
- 健康检查：`GET /health`

## 接口说明

### 1. `POST /api/v1/stays` — 停留分析（JSON）

**请求体**

| 字段 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `points` | array | 是 | — | 按时间升序的轨迹点，至少 1 个 |
| `points[].lat` | float | 是 | — | 纬度 `[-90, 90]` |
| `points[].lon` | float | 是 | — | 经度 `[-180, 180]` |
| `points[].timestamp` | string/number | 是 | — | ISO 8601（如 `2026-09-21T08:00:00Z`）或 Unix 秒；无时区按 UTC |
| `distance_threshold_m` | float | 否 | `100` | 距离阈值（米）：连续点距簇锚点不超过该值则合并 |
| `min_stay_duration_s` | float | 否 | `300` | 最短停留时长（秒）：簇时间跨度达到该值才判定为停留 |
| `max_gap_s` | float | 否 | `4 × min_stay_duration_s` | 最大采样间隔（秒）：超过则视为采样间断并强制切分 |

**请求示例**

```bash
curl -X POST http://localhost:8000/api/v1/stays \
  -H 'Content-Type: application/json' \
  -d @examples/request.json
```

`examples/request.json` 描述的场景：在 A 处停留 4 分钟 → 移动约 1.1 km 到 B 处短暂停留
2 分钟（低于阈值，不算停留）→ 设备离线约 26 分钟（采样间断）→ 在 C 处停留 3 分钟。

**响应示例**（实际输出）

```json
{
  "stays": [
    {
      "stay_id": 1,
      "center_lat": 39.9,
      "center_lon": 116.4,
      "start_time": "2026-09-21T08:16:40Z",
      "end_time": "2026-09-21T08:20:40Z",
      "duration_s": 240.0,
      "start_index": 0,
      "end_index": 4,
      "point_count": 5
    },
    {
      "stay_id": 2,
      "center_lat": 39.910005,
      "center_lon": 116.400005,
      "start_time": "2026-09-21T08:50:00Z",
      "end_time": "2026-09-21T08:53:00Z",
      "duration_s": 180.0,
      "start_index": 8,
      "end_index": 11,
      "point_count": 4
    }
  ],
  "stats": {
    "point_count": 12,
    "total_distance_m": 1148.002,
    "total_duration_s": 2180.0,
    "stay_count": 2,
    "total_stay_duration_s": 420.0
  }
}
```

**响应字段**

| 字段 | 说明 |
| --- | --- |
| `stays[].stay_id` | 停留序号，从 1 开始 |
| `stays[].center_lat / center_lon` | 停留中心点（区段内点坐标均值） |
| `stays[].start_time / end_time` | 进入 / 离开停留区的时间（区段首末点时间） |
| `stays[].duration_s` | 停留时长（秒） |
| `stays[].start_index / end_index` | 区段在请求 `points` 中的下标闭区间 |
| `stays[].point_count` | 区段包含的轨迹点数 |
| `stats.total_distance_m` | 总里程：相邻点 Haversine 距离之和（米） |
| `stats.total_duration_s` | 轨迹总时长：首末点时间差（秒） |
| `stats.stay_count` | 停留次数 |
| `stats.total_stay_duration_s` | 停留总时长（秒） |

### 2. `POST /api/v1/hotspots` — 跨时段热点归并

**请求体**：完整沿用 `POST /api/v1/stays` 的全部字段（`points`、`distance_threshold_m`、
`min_stay_duration_s`、`max_gap_s`），并额外要求：

| 字段 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `hotspot_radius_m` | float | 是 | — | 热点半径（米）：两段停留中心间 Haversine 距离 **不超过**该值则连边，须 > 0 |
| `min_visits` | int | 是 | — | 最少访问次数：连通分量内停留段数达到该值才判为热点，须 ≥ 1 |

服务**先按既有算法识别停留**，再在停留中心构成的无向图上求**连通分量**：

- 访问次数（分量内停留段数）`>= min_visits` → **热点**；
- 其余分量中的停留全部标记为**噪声**（`hotspot_id: null`、`noise: true`）；
- 边按无向处理，连通分量支持**链式传递**——“甲邻乙、乙邻丙、甲不邻丙”三者仍属同一热点，
  且分量划分**不随输入 / 遍历顺序改变**（并查集，边按规范序枚举）；
- 热点编号按分量**最早访问时间**升序从 1 分配（持平再按最小停留下标），与遍历顺序无关；
- 热点中心为成员停留中心按**停留时长加权**的平均（总权重为 0 时退化为等权均值）。

**请求示例**

```bash
curl -X POST http://localhost:8000/api/v1/hotspots \
  -H 'Content-Type: application/json' \
  -d @examples/hotspot_request.json
```

`examples/hotspot_request.json` 描述的场景：一天内 6 次停留——
家 H 到访 2 次、远一点的 F 与中间点 M 各 1 次（H-M、M-F 各约 111 m，H-F 约 222 m）、
公司 W 到访 2 次（距 H 约 10 km）。半径 150 m、`min_visits=2` 时：
H/M/F 靠**传递**归为热点 1（4 次访问），W 为热点 2（2 次访问），无噪声。

**响应示例**（实际输出，节选自上述请求）

```json
{
  "stays": [
    {"stay_id": 1, "hotspot_id": 1, "noise": false, "center_lat": 39.9, "...": "..."},
    {"stay_id": 2, "hotspot_id": 1, "noise": false, "center_lat": 39.902, "...": "..."},
    {"stay_id": 3, "hotspot_id": 2, "noise": false, "center_lat": 39.99, "...": "..."},
    {"stay_id": 4, "hotspot_id": 1, "noise": false, "center_lat": 39.901, "...": "..."},
    {"stay_id": 5, "hotspot_id": 1, "noise": false, "center_lat": 39.9, "...": "..."},
    {"stay_id": 6, "hotspot_id": 2, "noise": false, "center_lat": 39.99, "...": "..."}
  ],
  "hotspots": [
    {
      "hotspot_id": 1,
      "visit_count": 4,
      "total_duration_s": 720.0,
      "center_lat": 39.90075,
      "center_lon": 116.4,
      "first_visit_time": "2026-09-21T08:00:00Z",
      "last_visit_time": "2026-09-21T11:23:00Z",
      "stay_ids": [1, 2, 4, 5]
    },
    {
      "hotspot_id": 2,
      "visit_count": 2,
      "total_duration_s": 360.0,
      "center_lat": 39.99,
      "center_lon": 116.4,
      "first_visit_time": "2026-09-21T09:40:00Z",
      "last_visit_time": "2026-09-21T12:13:00Z",
      "stay_ids": [3, 6]
    }
  ],
  "stats": { "point_count": 24, "stay_count": 6, "...": "..." }
}
```

**响应字段**

| 字段 | 说明 |
| --- | --- |
| `stays[]` | 停留字段与 `/api/v1/stays` 完全一致，另附热点归属 |
| `stays[].hotspot_id` | 所属热点编号（从 1 开始）；噪声停留为 `null` |
| `stays[].noise` | 是否为噪声停留（不属于任何热点） |
| `hotspots[].hotspot_id` | 热点编号，按最早访问时间升序，从 1 开始 |
| `hotspots[].visit_count` | 访问次数（连通分量内停留段数） |
| `hotspots[].total_duration_s` | 成员停留累计停留时长（秒） |
| `hotspots[].center_lat / center_lon` | 热点中心：成员停留中心按停留时长加权平均 |
| `hotspots[].first_visit_time / last_visit_time` | 最早进入 / 最晚离开时间 |
| `hotspots[].stay_ids` | 成员停留的 `stay_id`（升序） |
| `stats` | 与 `/api/v1/stays` 相同的整体统计 |

### 3. `POST /api/v1/geofence-visits` — 围栏访问统计（点面归属）

**请求体**：完整沿用 `POST /api/v1/stays` 的全部字段（`points`、`distance_threshold_m`、
`min_stay_duration_s`、`max_gap_s`），并额外要求 `geofences` 围栏列表：

| 字段 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `geofences` | array | 是 | — | 围栏列表，允许空数组 `[]`；列表内 `zone_id` 必须唯一 |
| `geofences[].zone_id` | string | 是 | — | 围栏唯一编号（非空） |
| `geofences[].priority` | int | 否 | `0` | 优先级：多围栏同时命中时取**较大值**，持平再按 `zone_id` 字典序决胜 |
| `geofences[].geometry` | object | 是 | — | GeoJSON `Polygon` 或 `MultiPolygon`，坐标顺序 `[lon, lat]` |

服务**先按既有算法识别停留**（完全复用，响应字段与 `/api/v1/stays` 一致），再以每段停留的
**中心点**做点面归属：

- **外环边界视为命中**：中心点压在外环的边或顶点上算面内；
- **洞内及洞边界视为未命中**：落入孔洞内部或孔洞边界都算面外；
- **跨 180° 经线**：多边形按 GeoJSON 惯例在 ±180° 附近折返编码即可
  （如 `[..., 179.95, ..., -179.95, ...]`），服务内部自动经度连续化，无需预切割；
- 多个围栏同时命中时按 **`priority` 降序 → `zone_id` 字典序升序** 唯一决胜，
  归属结果与围栏输入顺序无关；未命中任何围栏的停留 `zone_id` 为 `null`；
- 响应 `geofences` **按请求中的围栏顺序**逐一汇总访问次数与累计停留时长，
  零访问围栏也保留（计数为 0、时长为 0）。

> 几何校验：环必须闭合（首末坐标相同）且至少 4 个点；经度 `[-180, 180]`、纬度 `[-90, 90]`；
> 单条环的经度跨度不超过 180°。不合法时返回 422。

**请求示例**

```bash
curl -X POST http://localhost:8000/api/v1/geofence-visits \
  -H 'Content-Type: application/json' \
  -d @examples/geofence_request.json
```

`examples/geofence_request.json` 描述的场景：一天内 4 次停留——家 2 次、公司 1 次、咖啡馆
1 次；围栏包括家 / 公司（priority 5）、咖啡馆与一个覆盖全城的低优先级重叠框（priority 1）、
从未到访的公园，以及一个跨 180° 经线的 MultiPolygon 围栏。重叠框虽然罩住全部停留，但优先级
低于各小围栏，因此一次也不会胜出。

**响应示例**（实际输出，节选）

```json
{
  "stays": [
    {"stay_id": 1, "zone_id": "zone-home", "center_lat": 39.9, "...": "..."},
    {"stay_id": 2, "zone_id": "zone-work", "center_lat": 39.99, "...": "..."},
    {"stay_id": 3, "zone_id": "zone-cafe", "center_lat": 39.95, "...": "..."},
    {"stay_id": 4, "zone_id": "zone-home", "center_lat": 39.9, "...": "..."}
  ],
  "geofences": [
    {"zone_id": "zone-home", "priority": 5, "visit_count": 2, "total_duration_s": 360.0, "stay_ids": [1, 4]},
    {"zone_id": "zone-work", "priority": 5, "visit_count": 1, "total_duration_s": 180.0, "stay_ids": [2]},
    {"zone_id": "zone-cafe", "priority": 1, "visit_count": 1, "total_duration_s": 180.0, "stay_ids": [3]},
    {"zone_id": "zone-overlap", "priority": 1, "visit_count": 0, "total_duration_s": 0.0, "stay_ids": []},
    {"zone_id": "zone-park", "priority": 0, "visit_count": 0, "total_duration_s": 0.0, "stay_ids": []},
    {"zone_id": "zone-dateline", "priority": 0, "visit_count": 0, "total_duration_s": 0.0, "stay_ids": []}
  ],
  "stats": { "point_count": 16, "stay_count": 4, "total_stay_duration_s": 720.0, "...": "..." }
}
```

**响应字段**

| 字段 | 说明 |
| --- | --- |
| `stays[]` | 停留字段与 `/api/v1/stays` 完全一致，另附围栏归属 |
| `stays[].zone_id` | 命中的围栏 `zone_id`；未命中任何围栏为 `null` |
| `geofences[].zone_id / priority` | 围栏编号与优先级，顺序与请求一致 |
| `geofences[].visit_count` | 访问次数：中心点命中该围栏的停留段数 |
| `geofences[].total_duration_s` | 命中停留的累计停留时长（秒） |
| `geofences[].stay_ids` | 命中该围栏的停留 `stay_id`（升序） |
| `stats` | 与 `/api/v1/stays` 相同的整体统计 |

### 4. `POST /api/v1/stays/geojson` — GeoJSON 结果

请求体与上相同。返回 `application/geo+json` 的 `FeatureCollection`：

- 第 1 个 Feature：整条轨迹的 `LineString`，`properties` 含里程、点数等统计
- 后续 Feature：每个停留一个 `Point`（中心点），`properties` 含停留时长、时间、下标范围

```bash
curl -X POST http://localhost:8000/api/v1/stays/geojson \
  -H 'Content-Type: application/json' \
  -d @examples/request.json | jq .
```

### 5. `GET /health` — 健康检查

```json
{"status": "ok"}
```

## 算法说明

经典 stay-point 检测（Li et al., 2008 风格）的流式实现，见 `app/staypoint.py`：

1. 按时间顺序逐点扫描，维护当前候选簇及其**锚点**（簇首点）；
2. 新点与锚点的 **Haversine** 距离 ≤ `distance_threshold_m` → 并入当前簇；
3. 距离超阈值 → 关闭当前簇：簇时间跨度 ≥ `min_stay_duration_s` 判定为停留，
   然后以新点为锚点开启新簇；
4. **采样间断**：相邻点间隔 > `max_gap_s` 时，无论距离多近都强制关闭当前簇——
   避免设备离线期间被错误计为停留（间断时间不计入任何停留时长）；
5. 停留中心取簇内所有点的经纬度均值；总里程为相邻点 Haversine 距离之和
   （地球平均半径 6371008.8 m）。

### 跨时段热点归并（`app/hotspot.py`）

在上述停留区段结果之上进行二次聚合（停留之间即使跨越采样间断 / 多日也可归并）：

1. 以每段停留的**中心**为节点；对所有停留对（按 `i < j` 的规范顺序）计算 Haversine 距离，
   距离 `<= hotspot_radius_m` 则连一条**无向边**（含等号边界）；
2. 用**并查集**（路径压缩 + 按大小合并）求无向图的**连通分量**：
   连边关系天然支持传递——“甲邻乙、乙邻丙、甲不邻丙”三者同属一个分量；
   连通分量是无向图的固有划分，**不随输入顺序或遍历顺序变化**；
3. 分量成员停留数（**访问次数**）`>= min_visits` 判为**热点**，其余停留全部标记为**噪声**；
4. 热点**稳定编号**：按分量内最早停留进入时间升序分配 `hotspot_id`（持平按最小停留下标），
   成员 `stay_id` 按升序列出；
5. 热点中心 = 成员停留中心以各自 `duration_s` 为权重的加权平均（权重总和为 0 时取等权均值）；
   `total_duration_s` 为成员停留时长之和，`visit_count` 为成员停留段数。

### 围栏点面归属（`app/geofence.py`）

在停留区段结果之上，以每段停留的**中心点**做归属（算法实现见 `app/geofence.py`，无第三方几何依赖）：

1. **经度连续化**：对每条线性环沿顶点顺序推进，相邻顶点经度差超过 ±180° 时整体平移 360°，
   跨越 180° 经线的环在展开坐标中成为普通多边形；孔洞环若以另一经度参考系编码，
   会整体平移到外环参考系；查询点分别以 `lon`、`lon±360` 三个经度像参与判定；
2. **射线法点面判定**：水平射线穿越边数的奇偶判内 / 外；点压在线段（含端点）上单独识别为
   “边界”——外环边界计为命中，孔洞边界与洞内一律计为未命中（浮点容差约 1e-9 度）；
3. MultiPolygon 的每个多边形可使用各自的经度参考系，任一部分命中即算该围栏命中；
4. **唯一决胜**：围栏按 `(-priority, zone_id)` 排序后依次判定，首个命中者唯一归属，
   因此结果只与围栏属性有关、与输入顺序无关；未命中为 `null`；
5. 汇总按请求中围栏顺序输出 `visit_count`（命中停留段数）与 `total_duration_s`
   （命中停留时长之和），零访问围栏保留 0 值。

> 注意：输入轨迹须按时间升序排列；`start_index` / `end_index` 对应请求数组中的原始下标。

## 项目结构

```
.
├── app/
│   ├── main.py          # FastAPI 应用与路由（/stays、/hotspots、/geofence-visits、/stays/geojson）
│   ├── models.py        # Pydantic 请求 / 响应模型（含 GeoJSON Polygon/MultiPolygon 校验）
│   ├── staypoint.py     # Haversine 与停留检测核心算法（纯函数）
│   ├── hotspot.py       # 热点归并：连通分量（并查集）+ 时长加权中心（纯函数）
│   └── geofence.py      # 围栏点面归属：射线法 + 孔洞 + 跨 180° 经线连续化（纯函数）
├── tests/
│   ├── test_api.py          # /stays 接口集成测试（TestClient）
│   ├── test_staypoint.py    # 停留检测算法单元测试
│   ├── test_hotspot.py      # 热点归并算法单元测试（传递扩展、顺序无关、稳定编号）
│   ├── test_hotspot_api.py  # /hotspots 接口集成测试
│   ├── test_geofence.py     # 围栏归属算法单元测试（边界、孔洞、跨经线、决胜、汇总）
│   └── test_geofence_api.py # /geofence-visits 接口集成测试
├── examples/
│   ├── request.json         # /stays 示例请求体
│   ├── hotspot_request.json # /hotspots 示例请求体
│   └── geofence_request.json # /geofence-visits 示例请求体
├── Dockerfile
├── docker-compose.yml
├── requirements.txt       # 运行依赖
└── requirements-dev.txt   # 测试依赖
```

## 运行测试

```bash
pip install -r requirements-dev.txt
pytest tests/ -v
```

共 43 个用例，覆盖 Haversine 距离、停留合并、最短时长过滤、采样间断切分、跨 180° 经线簇中心、
GeoJSON 结构、参数校验（422），热点的链式传递扩展、多分量与噪声标记、
`min_visits` 过滤、半径边界（含等号）、时长加权中心、最早访问时间稳定编号、
打乱 / 颠倒输入顺序后的结果确定性，以及围栏的外环边界命中、洞内 / 洞边界未命中、
跨 180° 经线多边形（含反向编码与 MultiPolygon）、孔洞参考系对齐、
priority + zone_id 决胜（与输入顺序无关）、零访问围栏保留等。
