# 通过 MCP 接口创建数据任务

## 1. 启动 QuickForm 服务

在项目根目录执行：

```bash
# 若使用虚拟环境，先激活
python app.py
# 或：flask run
```

默认地址为 `http://127.0.0.1:5000`。若部署在其他域名/端口，请记下实际 **BASE_URL**。

---

## 2. 创建数据任务

在项目根目录执行：

```bash
python scripts/create_task_via_mcp.py
```

若 QuickForm 部署在其他地址，请传入 BASE_URL：

```bash
python scripts/create_task_via_mcp.py https://your-quickform.example.com
```

脚本使用账号：**林淼焱2** / **123321**，会创建名为「简单数据回收任务」的数据任务。  
成功后终端会输出 **apiid** 和 **提交数据地址**。

---

## 3. 提交示例数据（可选）

创建任务后，用返回的 `apiid` 提交一条示例数据：

```bash
python scripts/submit_sample_data.py http://127.0.0.1:5000 <你的apiid>
```

或使用 curl：

```bash
curl -X POST "http://127.0.0.1:5000/api/<apiid>" \
  -H "Content-Type: application/json" \
  -d "{\"姓名\":\"张三\",\"部门\":\"教务处\",\"备注\":\"测试\"}"
```

---

## 4. 使用 curl 直接创建任务（不跑 Python）

```bash
curl -X POST "http://127.0.0.1:5000/mcp/add" \
  -H "Content-Type: application/json" \
  -d "{\"username\":\"林淼焱2\",\"password\":\"123321\",\"task_name\":\"简单数据回收任务\",\"task_intro\":\"用于收集提交数据的示例任务\"}"
```

成功时会返回：`{"success":true,"apiid":"xxxx"}`。

---

## 接口说明摘要

| 接口 | 说明 |
|------|------|
| `POST /mcp/add` | 创建数据任务，需 username、password、task_name，可选 task_intro |
| `POST /mcp/list` | 查看当前账号下所有任务（apiid + 名称） |
| `POST /api/<apiid>` | 向该任务提交一条数据（JSON 正文） |
| `GET /api/<apiid>/all` | 获取该任务下全部提交数据 |

更完整说明见：`docs/CLI接口说明.md`。
