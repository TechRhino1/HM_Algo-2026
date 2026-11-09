import re
with open(r'C:\HM_project\HM_Algo2026\jarvis\backtesting\engine.py', 'r', encoding='utf-8') as f:
    content = f.read()

lines = content.split('\n')
for i, line in enumerate(lines):
    if '"""' in line and line.count('"""') % 2 == 1:
        print(f'Line {i+1}: {line}')