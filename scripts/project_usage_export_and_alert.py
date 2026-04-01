"""导出项目高消耗 TopX，并检查指定项目预警。

用法示例：
python scripts/project_usage_export_and_alert.py --top 30 --sort-by storage --out reports
python scripts/project_usage_export_and_alert.py --task-ids abc123xyzz,def456lmno
"""
import argparse
import os
import sys
from datetime import datetime

import pandas as pd
from dotenv import load_dotenv


ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from core.blueprint import _init_database, SessionLocal  # noqa: E402
from core.project_usage import get_top_projects, evaluate_project_alerts  # noqa: E402


def parse_args():
    p = argparse.ArgumentParser(description='QuickForm 项目高消耗导出与预警')
    p.add_argument('--top', type=int, default=20, help='导出 Top 数量，默认 20')
    p.add_argument('--sort-by', choices=['submissions', 'storage', 'all_calls'], default='submissions')
    p.add_argument('--out', default='reports', help='导出目录，默认 reports')
    p.add_argument('--task-ids', default='', help='仅检查这些 task_id，逗号分隔')
    p.add_argument('--all-calls-1h', type=int, default=int(os.getenv('PROJECT_ALERT_ALL_CALLS_1H', '800')))
    p.add_argument('--all-calls-1h-p1', type=int, default=int(os.getenv('PROJECT_ALERT_ALL_CALLS_1H_P1', '2000')))
    p.add_argument('--submissions-24h', type=int, default=int(os.getenv('PROJECT_ALERT_SUBMISSIONS_24H', '200')))
    p.add_argument('--total-mb', type=int, default=int(os.getenv('PROJECT_ALERT_TOTAL_MB', '2048')))
    return p.parse_args()


def main():
    load_dotenv()
    _init_database()
    args = parse_args()

    os.makedirs(args.out, exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')

    db = SessionLocal()
    try:
        rows = get_top_projects(db, limit=args.top, sort_by=args.sort_by)
        if not rows:
            print('没有可导出的项目数据。')
            return

        df = pd.DataFrame(rows)
        export_columns = [
            'task_id', 'task_title', 'owner_username',
            'submit_count', 'submissions_24h', 'all_calls_1h',
            'submission_mb', 'file_mb', 'total_mb',
        ]
        df = df[export_columns]
        csv_path = os.path.join(args.out, f'project_top_{args.sort_by}_{ts}.csv')
        xlsx_path = os.path.join(args.out, f'project_top_{args.sort_by}_{ts}.xlsx')
        df.to_csv(csv_path, index=False, encoding='utf-8-sig')
        with pd.ExcelWriter(xlsx_path, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name='top_usage')

        monitor_ids = [x.strip() for x in (args.task_ids or '').split(',') if x.strip()]
        alert_candidates = get_top_projects(db, limit=500, sort_by='all_calls')
        if monitor_ids:
            alert_candidates = [r for r in alert_candidates if r.get('task_id') in monitor_ids]

        config = {
            'all_calls_1h': args.all_calls_1h,
            'all_calls_1h_p1': args.all_calls_1h_p1,
            'submissions_24h': args.submissions_24h,
            'total_bytes': args.total_mb * 1024 * 1024,
        }
        alerts = evaluate_project_alerts(alert_candidates, config)
        alert_df = pd.DataFrame(alerts) if alerts else pd.DataFrame(columns=['task_id', 'task_title', 'owner_username', 'level', 'reasons'])
        alert_csv = os.path.join(args.out, f'project_alerts_{ts}.csv')
        if not alert_df.empty and 'reasons' in alert_df.columns:
            alert_df['reasons'] = alert_df['reasons'].apply(lambda x: '; '.join(x) if isinstance(x, list) else str(x))
        alert_df.to_csv(alert_csv, index=False, encoding='utf-8-sig')

        print(f'已导出 Top 数据: {csv_path}')
        print(f'已导出 Top 数据: {xlsx_path}')
        print(f'已导出预警结果: {alert_csv}')
        print(f'预警数量: {len(alerts)}')
    finally:
        db.close()


if __name__ == '__main__':
    main()
