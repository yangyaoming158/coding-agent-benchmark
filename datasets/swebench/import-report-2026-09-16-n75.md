# SWE-bench Verified 导入漏斗（2026-09-16，抽 75，终审导回后）

| 层 | 数量 | 说明 |
|:---|---:|:---|
| 官方题数 | 500 | princeton-nlp/SWE-bench_Verified |
| 离线筛掉：REPO_NOT_PYTEST | 314 | |
| 离线筛掉：ISSUE_LEAKS_FIX | 8 | |
| 离线筛掉：NO_F2P | 2 | |
| 离线筛掉：TEST_PATCH_NON_TEST_PATH | 2 | |
| 离线筛掉：GOLD_TOUCHES_PROTECTED | 1 | |
| 离线筛通过（抽样池） | 173 | |
| 抽样后 | 75 | |
| 官方镜像拉得到 | 74 | 拉不到：astropy__astropy-8707 |
| git 镜像备好 | 75 | 失败：无 |
| 入库 | 74 | |
| 八步验证：VALID | 59 | |
| 八步验证：INVALID | 13 | |
| 八步验证：INVALID(COMMIT_MISSING) | 1 | |
| 八步验证：INVALID(F2P_NOT_FAILING) | 1 | |
| **VALID** | **59** | |

| 仓库 | 池 | 抽中 | VALID |
|:---|---:|---:|---:|
| astropy/astropy | 22 | 9 | 6 |
| matplotlib/matplotlib | 31 | 13 | 7 |
| mwaskom/seaborn | 2 | 2 | 2 |
| pallets/flask | 1 | 1 | 1 |
| pydata/xarray | 19 | 8 | 7 |
| pylint-dev/pylint | 9 | 5 | 4 |
| pytest-dev/pytest | 18 | 8 | 8 |
| scikit-learn/scikit-learn | 31 | 13 | 13 |
| sphinx-doc/sphinx | 40 | 16 | 11 |
