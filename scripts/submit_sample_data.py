#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
向指定 QuickForm 任务提交一条示例数据。
用法：python submit_sample_data.py <BASE_URL> <apiid>
示例：python submit_sample_data.py http://127.0.0.1:5000 a1b2c3d4ef
"""
import json
import sys
import urllib.request
import urllib.error


def main():
    if len(sys.argv) < 3:
        print("用法: python submit_sample_data.py <BASE_URL> <apiid>")
        print("示例: python submit_sample_data.py http://127.0.0.1:5000 a1b2c3d4ef")
        return 1
    base_url = sys.argv[1].rstrip("/")
    apiid = sys.argv[2]
    # 示例数据
    sample = {
        "姓名": "示例用户",
        "部门": "测试部门",
        "提交时间": "2025-03-12",
        "备注": "这是一条通过 API 提交的示例数据",
    }
    url = f"{base_url}/api/{apiid}"
    body = json.dumps(sample).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            if result.get("status") == "success" or result.get("message") == "提交成功":
                print("提交成功！", result)
                return 0
            print("响应:", result)
            return 0
    except urllib.error.HTTPError as e:
        print("请求失败:", e.code, e.read().decode("utf-8", errors="ignore"))
        return 1
    except urllib.error.URLError as e:
        print("连接失败:", e.reason)
        return 1


if __name__ == "__main__":
    sys.exit(main())
