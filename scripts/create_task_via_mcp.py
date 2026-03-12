#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
通过 MCP 接口在 QuickForm 平台创建数据任务。
用法：python create_task_via_mcp.py [BASE_URL]
BASE_URL 默认为 http://127.0.0.1:5000（本地运行时的地址）
"""
import json
import sys
import urllib.request
import urllib.error

# 配置：请根据实际情况修改
BASE_URL = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:5000").rstrip("/")
USERNAME = "林淼焱2"
PASSWORD = "123321"
TASK_NAME = "简单数据回收任务"
TASK_INTRO = "用于收集提交数据的示例任务，可通过 API 或网页提交。"


def main():
    url = f"{BASE_URL}/mcp/add"
    data = {
        "username": USERNAME,
        "password": PASSWORD,
        "task_name": TASK_NAME,
        "task_intro": TASK_INTRO,
    }
    body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            if result.get("success"):
                apiid = result.get("apiid", "")
                print("创建成功！")
                print(f"  任务名称: {TASK_NAME}")
                print(f"  API 标识 (apiid): {apiid}")
                print(f"  提交数据地址: {BASE_URL}/api/{apiid}")
                print(f"  获取全部数据: {BASE_URL}/api/{apiid}/all")
                return 0
            else:
                print("创建失败:", result.get("message", "未知错误"))
                return 1
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="ignore")
        try:
            err_json = json.loads(err_body)
            print("请求失败:", err_json.get("message", err_body))
        except Exception:
            print("请求失败:", e.code, err_body)
        return 1
    except urllib.error.URLError as e:
        print("连接失败（请确认 QuickForm 服务已启动）:", e.reason)
        return 1
    except Exception as e:
        print("错误:", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
