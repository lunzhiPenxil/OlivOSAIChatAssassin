# OlivOSAIChatAssassin 测试

本目录是插件专项测试的唯一维护源。测试默认离线运行，不连接真实账号、模型接口或 WebUI。

## 本地运行

测试需要一份 OlivOS 主机源码。推荐通过环境变量显式指定：

```powershell
$env:OLIVOS_SOURCE = 'D:\MyProject\run\OlivOS-dev'
python -m pytest tests -q
```

未设置时会依次查找仓库内 `.ci/OlivOS`、相邻的 `OlivOS-dev` / `OlivOS` 等路径，并尝试使用
当前 Python 环境里已安装的 `OlivOS` 包。

## 覆盖率

```powershell
New-Item -ItemType Directory -Force test-results | Out-Null
python -m pytest tests `
  --cov=OlivOSAIChatAssassin `
  --cov-branch `
  --cov-context=test `
  --cov-report=term-missing `
  --cov-report=xml:test-results/coverage.xml `
  --cov-report=json:test-results/coverage.json `
  --cov-report=html:test-results/html `
  --junitxml=test-results/junit.xml
```

首轮不设覆盖率门槛。报告输出到 `test-results/`，该目录不提交。

## 当前基线

- 测试数：563
- 行覆盖率：90.19%（5003 / 5547）
- 分支覆盖率：81.35%（1832 / 2252）
- 行与分支综合覆盖率：87.64%
- 验证环境：Python 3.11 + 干净 checkout 的 `OlivOS-Team/OlivOS` 主分支

该基线用于观察趋势，不是当前 CI 的失败门槛。

## CI 顺序

`.github/workflows/ci.yml` 使用三个串行 job：

1. `lint`：`flake8 .`
2. `test`：checkout `OlivOS-Team/OlivOS` 到 `.ci/OlivOS` 后运行 pytest 与覆盖率
3. `build`：只有 lint 与 test 都成功后才打包 OPK

`test` job 会把覆盖率 XML/JSON/HTML 与 JUnit 报告作为 artifact 上传。
