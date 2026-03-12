# QuickForm MCP 接口说明

供 CLI、扣子编程、OpenClaw 等工具自动化创建与查看数据任务，无需打开网页。

---

## 1. 基础信息

- **Base URL**：`https://quickform.cn`（若自建部署，请替换为您自己的站点根地址）
- **认证方式**：所有 MCP 接口均通过 **用户名 + 密码** 在请求体中传递，不依赖 Cookie/Session
- **请求格式**：支持 **JSON**（`Content-Type: application/json`）或 **表单**（`application/x-www-form-urlencoded`）
- **响应格式**：统一为 **JSON**

---

## 2. 增加数据任务 `POST /mcp/add`

创建一条新的数据任务，并返回用于提交数据的 **apiid**。

### 请求参数

| 参数        | 必填 | 说明         |
|-------------|------|--------------|
| username    | 是   | 用户名       |
| password    | 是   | 密码         |
| task_name   | 是   | 任务名称     |
| task_intro  | 否   | 任务介绍/描述 |

（兼容字段：`title` 等同 `task_name`，`description` 等同 `task_intro`。）

### 成功响应（200）

```json
{
  "success": true,
  "apiid": "a1b2c3d4ef"
}
```

`apiid` 即该任务的 **API 标识**，后续提交数据、拉取数据都使用此 id。

### 错误响应

- `400`：缺少必填参数 → `{ "success": false, "message": "缺少 username 或 password" }` 等
- `401`：用户名或密码错误 → `{ "success": false, "message": "用户名或密码错误" }`
- `403`：已达任务数量上限 → `{ "success": false, "message": "已达任务数量上限（当前 N 个）..." }`
- `500`：服务器异常 → `{ "success": false, "message": "..." }`

### 示例（curl）

```bash
# JSON
curl -X POST "https://quickform.cn/mcp/add" \
  -H "Content-Type: application/json" \
  -d '{"username":"teacher1","password":"your_password","task_name":"课堂签到表","task_intro":"本周签到"}'

# 表单
curl -X POST "https://quickform.cn/mcp/add" \
  -d "username=teacher1&password=your_password&task_name=课堂签到表&task_intro=本周签到"
```

---

## 3. 查看数据任务列表 `POST /mcp/list`

获取当前账号下所有数据任务及其 **apiid** 与名称。

### 请求参数

| 参数     | 必填 | 说明   |
|----------|------|--------|
| username | 是   | 用户名 |
| password | 是   | 密码   |

### 成功响应（200）

```json
{
  "success": true,
  "tasks": [
    { "apiid": "a1b2c3d4ef", "name": "课堂签到表" },
    { "apiid": "x9y8z7w6vu", "name": "问卷回收" }
  ]
}
```

### 错误响应

- `400`：缺少 username 或 password
- `401`：用户名或密码错误

### 示例（curl）

```bash
curl -X POST "https://quickform.cn/mcp/list" \
  -H "Content-Type: application/json" \
  -d '{"username":"teacher1","password":"your_password"}'
```

---

## 4. 上传 HTML 文件 `POST /mcp/upload`

上传单个 HTML/HTM 文件，返回上传结果与文件的公网访问地址（可用于扣子/OpenClaw 等场景下直接引用页面链接）。

### 请求方式

- **Content-Type**：`multipart/form-data`
- **参数**：`username`、`password`（表单字段），`file`（文件字段，仅支持 .html / .htm，单文件最大 4MB）

### 成功响应（200）

```json
{
  "success": true,
  "url": "https://quickform.cn/static/uploads/uuid_xxx.html",
  "filename": "uuid_xxx.html"
}
```

- `url`：该文件的公网访问地址，可直接在浏览器或前端 iframe 中打开。
- `filename`：服务器保存后的文件名（含随机前缀，避免冲突）。

### 错误响应

- `400`：缺少参数、未选择文件、或文件格式/大小不符合（仅允许 .html/.htm，单文件 ≤ 4MB）→ `{ "success": false, "message": "..." }`
- `401`：用户名或密码错误
- `500`：服务器保存失败

### 示例（curl）

```bash
curl -X POST "https://quickform.cn/mcp/upload" \
  -F "username=teacher1" \
  -F "password=your_password" \
  -F "file=@/path/to/your/page.html"
```

---

## 5. 使用 apiid 提交与获取数据

拿到 **apiid** 后，与网页端一致：

- **提交一条数据**：`POST /api/<apiid>`  
  - Body：JSON 对象，例如 `{"name":"张三","score":85}`  
  - 成功：`{ "message": "提交成功", "status": "success" }`

- **获取全部提交数据**：`GET /api/<apiid>/all`  
  - 返回：`{ "submissions": [ ... ], "total_submissions": N }`

- **简要查询（最新 3 条）**：`GET /api/<apiid>`  
  - 返回：含 `submissions`、`total_submissions`、`task_id`、`task_title` 等

**完整提交地址示例**：`https://quickform.cn/api/a1b2c3d4ef`

---

## 6. 与扣子 / OpenClaw 的自动化流程

1. **创建任务**：调用 `POST /mcp/add`，传入用户名、密码、任务名称（及可选介绍），得到 `apiid`。
2. **配置提交地址**：在扣子/OpenClaw 应用中将「数据提交接口」配置为：  
   `https://quickform.cn/api/<apiid>`  
   例如：`https://quickform.cn/api/a1b2c3d4ef`
3. **应用内提交**：用户在前端填写的数据以 JSON 形式 POST 到上述地址即可写入 QuickForm。
4. **查询任务列表**：需要展示或选择「往哪个任务提交」时，可调用 `POST /mcp/list` 获取当前用户下所有 `apiid` 与名称。

这样即可在不打开 QuickForm 网页的情况下，完成任务的创建、列表查看与数据提交地址的配置。

---

## 7. 返回数据格式小结

| 接口           | 成功时返回字段 | 说明 |
|----------------|----------------|------|
| POST /mcp/add    | `success: true`, `apiid` | 新任务的 API 标识 |
| POST /mcp/list   | `success: true`, `tasks` | `tasks` 为 `[{ apiid, name }, ...]` |
| POST /mcp/upload | `success: true`, `url`, `filename` | 上传文件的公网地址与保存文件名 |

所有错误均为 `success: false` 且带 `message` 字段，便于 CLI 或技能内统一处理。
