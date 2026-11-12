"""运行全部本地 smoke 测试并汇总结果；任一失败返回非零退出码。"""
import json
import subprocess
import sys
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent
    total = 0
    failures = []
    for test in sorted(root.glob('*_smoke.py')):
        result = subprocess.run(
            [sys.executable, str(test)], capture_output=True, text=True)
        if result.returncode != 0:
            failures.append(test.name)
            print(f'FAIL {test.name}')
            print(result.stderr.strip()[-500:])
            continue
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            failures.append(test.name)
            print(f'FAIL {test.name}: invalid JSON output')
            continue
        passed = data.get('passed', 0)
        total += passed
        print(f'OK   {test.name}: {passed}')
    print(f'Total passed assertions: {total}')
    if failures:
        print('Failed suites: ' + ', '.join(failures))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
