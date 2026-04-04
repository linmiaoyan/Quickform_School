# QuickForm CLI 接口说明

供命令行、扣子编程、OpenClaw 等工具自动化创建与查看数据任务，无需打开网页。

---

## 1. 基础信息

- **Base URL**：`https://quickform.cn`（若自建部署，请替换为您自己的站点根地址）
- **认证方式**：所有 CLI 接口均通过 **用户名 + 密码** 在请求体中传递，不依赖 Cookie/Session
- **请求格式**：支持 **JSON**（`Content-Type: application/json`）或 **表单**（`application/x-www-form-urlencoded`）
- **响应格式**：一般为 **JSON**；`POST /cli/cert_material` 成功时返回**文件流**（下载），失败时仍为 JSON 错误。
- **防撞库 / 暴力尝试**：CLI 与网页登录共用服务端内存限流（按 **IP** 及 **IP+登录名字符串** 计数，参数见 `core/login_throttle.py`）。短时间内失败过多将返回 **`429`**，JSON 含 `error: "rate_limit"`、`retry_after`（秒）及提示文案。多进程部署时各 worker 内存独立，生产仍建议在网关侧限流。

---

## 2. 增加数据任务 `POST /cli/add`

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
- `403`：从第二个任务起需先绑定/验证邮箱 → `{ "success": false, "code": "email_not_bound" | "email_not_verified", "message": "..." }`
- `500`：服务器异常 → `{ "success": false, "message": "..." }`

### 示例（curl）

```bash
# JSON
curl -X POST "https://quickform.cn/cli/add" \
  -H "Content-Type: application/json" \
  -d '{"username":"teacher1","password":"your_password","task_name":"课堂签到表","task_intro":"本周签到"}'

# 表单
curl -X POST "https://quickform.cn/cli/add" \
  -d "username=teacher1&password=your_password&task_name=课堂签到表&task_intro=本周签到"
```

---

## 3. 管理员重置用户密码 `POST /cli/reset_user_password`

使用**管理员账号**在 CLI 中重置任意用户（非自己）的登录密码，无需打开后台网页。

### 请求参数

| 参数 | 必填 | 说明 |
|------|------|------|
| username | 是 | **管理员**用户名（或与登录一致的邮箱/手机号） |
| password | 是 | 管理员密码 |
| new_password | 是 | 目标用户的新密码，**至少 6 个字符** |
| target_username | 二选一 | 要重置密码的用户的**用户名** |
| target_user_id | 二选一 | 要重置密码的用户数字 ID |

（兼容字段：`target` 等同 `target_username`。）

### 成功响应（200）

```json
{
  "success": true,
  "message": "密码已重置",
  "username": "student01",
  "user_id": 42
}
```

### 错误响应

- `400`：缺少参数、`new_password` 过短、`target_user_id` 无效
- `403`：非管理员或管理员凭据错误；或尝试重置自己的密码
- `404`：目标用户不存在
- `500`：服务器异常

### 示例（curl）

```bash
curl -X POST "https://quickform.cn/cli/reset_user_password" \
  -H "Content-Type: application/json" \
  -d '{"username":"admin_user","password":"admin_pass","target_username":"student01","new_password":"NewPass789"}'
```

兼容旧路径：`POST /mcp/reset_user_password`（参数相同）。

---

## 4. 管理员修改用户邮箱 `POST /cli/set_user_email`

将指定用户的**登录/通知邮箱**改为新地址；成功后该用户的 **`email_verified` 会重置为未验证**，需用户自行在站内完成邮箱验证（与业务规则一致）。

### 请求参数

| 参数 | 必填 | 说明 |
|------|------|------|
| username | 是 | 管理员用户名（或邮箱/手机号） |
| password | 是 | 管理员密码 |
| new_email | 是 | 目标用户的新邮箱（格式需合法） |
| target_username | 二选一 | 目标用户的用户名 |
| target_user_id | 二选一 | 目标用户数字 ID |

（兼容字段：`target` 等同 `target_username`。）

### 成功响应（200）

- 已修改：`email`, `email_verified: false`, `message` 含「需重新验证」
- 新邮箱与旧邮箱相同：`message` 为邮箱未变化，`email_verified` 保持原值

### 错误响应

- `400`：缺少参数、邮箱格式错误、邮箱已被他人使用、不能修改自己的邮箱等
- `403`：非管理员或凭据错误
- `404`：目标用户不存在
- `500`：服务器异常

### 示例（curl）

```bash
curl -X POST "https://quickform.cn/cli/set_user_email" \
  -H "Content-Type: application/json" \
  -d '{"username":"admin_user","password":"admin_pass","target_username":"teacher01","new_email":"teacher@school.edu.cn"}'
```

兼容旧路径：`POST /mcp/set_user_email`（参数相同）。

---

## 5. 教师认证审核（管理员）

以下接口均需 **管理员** 的 `username` + `password`。兼容路径：将 `/cli/` 换成 `/mcp/` 即可。

### 5.1 待审核列表 `POST /cli/cert_pending`

返回当前**待审核**（`status=0`）的教师认证申请及材料元数据（不含文件二进制）。

| 参数 | 必填 | 说明 |
|------|------|------|
| username | 是 | 管理员用户名（或邮箱/手机号） |
| password | 是 | 管理员密码 |
| limit | 否 | 条数上限，默认 50，最大 200 |

**成功（200）**：`success`, `count`, `items`（每项含 `request_id`, `user_id`, `username`, `school`, `phone`, `email`, `file_name`, `file_ext`, `has_file`, `created_at`），以及 `material_url`（下载材料接口的完整 URL 说明用）。

### 5.2 下载认证材料 `POST /cli/cert_material`

按 `request_id` 下载用户上传的**原始文件**（`Content-Disposition: attachment`，便于 `curl -o`）。

| 参数 | 必填 | 说明 |
|------|------|------|
| username | 是 | 管理员 |
| password | 是 | 管理员密码 |
| request_id | 是 | `cert_pending` 返回的 `request_id` |

**成功**：文件流（非 JSON）。**失败**：JSON，`404` 表示记录或文件不存在。

```bash
curl -X POST "https://quickform.cn/cli/cert_material" \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"***","request_id":12}' \
  -o ./cert_12.bin
```

### 5.3 通过或拒绝 `POST /cli/cert_decide`

| 参数 | 必填 | 说明 |
|------|------|------|
| username | 是 | 管理员 |
| password | 是 | 管理员密码 |
| request_id | 是 | 待审核申请的 ID |
| action | 是 | `approve`（通过）或 `reject`（拒绝） |
| note | 否 | 审核备注（与网页端一致；通过时会写入用户认证备注等） |

**说明**：仅当申请仍为**待审核**时可处理；已处理返回 `409` 及当前 `status`。

**通过（approve）** 的效果与后台「通过教师认证」一致：用户标记为已认证、任务上限无限制、并自动通过该用户下尚未通过的 HTML 任务审核等。

```bash
curl -X POST "https://quickform.cn/cli/cert_decide" \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"***","request_id":12,"action":"approve","note":""}'
```

---

## 6. 查看数据任务列表 `POST /cli/list`

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
curl -X POST "https://quickform.cn/cli/list" \
  -H "Content-Type: application/json" \
  -d '{"username":"teacher1","password":"your_password"}'
```

---

## 7. 上传 HTML 文件 `POST /cli/upload`

上传单个 HTML/HTM 文件，返回上传结果与文件的公网访问地址（可用于扣子/OpenClaw 等场景下直接引用页面链接）。

### 请求方式

- **Content-Type**：`multipart/form-data`
- **参数**：`username`、`password`（表单字段），`file`（文件字段，仅支持 .html / .htm，单文件最大 4MB）

### 成功响应（200）

```json
{
  "success": true,
  "url": "https://quickform.cn/static/uploads/xxxxxxxx.html",
  "filename": "xxxxxxxx.html"
}
```

- `url`：该文件的公网访问地址，可直接在浏览器或前端 iframe 中打开。
- `filename`：服务器保存后的文件名（随机命名，避免冲突）。

### 错误响应

- `400`：缺少参数、未选择文件、或文件格式/大小不符合（仅允许 .html/.htm，单文件 ≤ 4MB）→ `{ "success": false, "message": "..." }`
- `401`：用户名或密码错误
- `500`：服务器保存失败

### 示例（curl）

```bash
curl -X POST "https://quickform.cn/cli/upload" \
  -F "username=teacher1" \
  -F "password=your_password" \
  -F "file=@/path/to/your/page.html"
```

---

## 8. 使用 apiid 提交与获取数据

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

## 9. 与扣子 / OpenClaw 的自动化流程

1. **创建任务**：调用 `POST /cli/add`，传入用户名、密码、任务名称（及可选介绍），得到 `apiid`。
2. **配置提交地址**：在扣子/OpenClaw 应用中将「数据提交接口」配置为：  
   `https://quickform.cn/api/<apiid>`  
   例如：`https://quickform.cn/api/a1b2c3d4ef`
3. **应用内提交**：用户在前端填写的数据以 JSON 形式 POST 到上述地址即可写入 QuickForm。
4. **查询任务列表**：需要展示或选择「往哪个任务提交」时，可调用 `POST /cli/list` 获取当前用户下所有 `apiid` 与名称。

这样即可在不打开 QuickForm 网页的情况下，完成任务的创建、列表查看与数据提交地址的配置。

---

## 10. 返回数据格式小结

| 接口           | 成功时返回字段 | 说明 |
|----------------|----------------|------|
| POST /cli/add    | `success: true`, `apiid` | 新任务的 API 标识 |
| POST /cli/reset_user_password | `success: true`, `username`, `user_id` | 已重置密码的目标用户 |
| POST /cli/set_user_email | `success: true`, `email`, `email_verified`, … | 已修改目标用户邮箱 |
| POST /cli/cert_pending | `success: true`, `items`, `count` | 待审核教师认证列表 |
| POST /cli/cert_material | （文件流） | 认证上传的原始文件 |
| POST /cli/cert_decide | `success: true`, `request_id`, … | 审核结果 |
| POST /cli/list   | `success: true`, `tasks` | `tasks` 为 `[{ apiid, name }, ...]` |
| POST /cli/upload | `success: true`, `url`, `filename` | 上传文件的公网地址与保存文件名 |

所有错误均为 `success: false` 且带 `message` 字段，便于 CLI 或技能内统一处理。
