"""Write a compact coverage summary to stdout and the GitHub job summary."""

import json
import os
import sys
from pathlib import Path


def main(path):
    if not path.is_file():
        print('coverage.json 未生成，跳过摘要。')
        return
    report = json.loads(path.read_text(encoding='utf-8'))
    totals = report['totals']
    branches = totals.get('num_branches', 0)
    covered_branches = totals.get('covered_branches', 0)
    lines = [
        '## OlivOSAIChatAssassin 覆盖率',
        '',
        f"- 行覆盖率：{totals.get('percent_statements_covered', 0):.2f}%",
        f"- 已覆盖行：{totals['covered_lines']} / {totals['num_statements']}",
        f"- 分支覆盖率：{totals.get('percent_branches_covered', 0):.2f}%",
        f"- 已覆盖分支：{covered_branches} / {branches}",
        f"- 行与分支综合覆盖率：{totals.get('percent_covered', 0):.2f}%",
        '',
        '### 缺口最大的文件',
        '',
        '| 文件 | 缺失行 | 缺失分支 | 行覆盖率 |',
        '| --- | ---: | ---: | ---: |',
    ]
    rows = []
    for name, data in report['files'].items():
        summary = data['summary']
        rows.append((
            summary.get('missing_lines', 0) + summary.get('missing_branches', 0),
            name,
            summary.get('missing_lines', 0),
            summary.get('missing_branches', 0),
            summary.get('percent_statements_covered', 0),
        ))
    for _, name, missing_lines, missing_branches, percent in sorted(rows, reverse=True)[:10]:
        lines.append(f'| `{name}` | {missing_lines} | {missing_branches} | {percent:.2f}% |')
    text = '\n'.join(lines) + '\n'
    print(text)
    summary_path = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary_path:
        with open(summary_path, 'a', encoding='utf-8') as summary_file:
            summary_file.write(text)


if __name__ == '__main__':
    main(Path(sys.argv[1] if len(sys.argv) > 1 else 'test-results/coverage.json'))
