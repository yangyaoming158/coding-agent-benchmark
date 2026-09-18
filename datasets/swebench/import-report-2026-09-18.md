# SWE-bench Verified 导入漏斗（2026-09-18）

| 层 | 数量 | 说明 |
|:---|---:|:---|
| 官方题数 | 500 | princeton-nlp/SWE-bench_Verified |
| 离线筛掉：REPO_NOT_PYTEST | 314 | |
| 离线筛掉：ISSUE_LEAKS_FIX | 8 | |
| 离线筛掉：NO_F2P | 2 | |
| 离线筛掉：TEST_PATCH_NON_TEST_PATH | 2 | |
| 离线筛掉：GOLD_TOUCHES_PROTECTED | 1 | |
| 离线筛通过（抽样池） | 173 | |
| 抽样后 | 100 | |
| 官方镜像拉得到 | 98 | 拉不到：astropy__astropy-8707、astropy__astropy-8872 |
| git 镜像备好 | 100 | 失败：无 |
| 入库 | 98 | |
| 八步验证：VALID | 75 | |
| 八步验证：INVALID | 19 | |
| 八步验证：INVALID(COMMIT_MISSING) | 3 | |
| 八步验证：INVALID(F2P_NOT_FAILING) | 1 | |
| **VALID** | **75** | |

| 仓库 | 池 | 抽中 | VALID |
|:---|---:|---:|---:|
| astropy/astropy | 22 | 13 | 7 |
| matplotlib/matplotlib | 31 | 17 | 8 |
| mwaskom/seaborn | 2 | 2 | 2 |
| pallets/flask | 1 | 1 | 1 |
| pydata/xarray | 19 | 11 | 10 |
| pylint-dev/pylint | 9 | 6 | 4 |
| pytest-dev/pytest | 18 | 11 | 11 |
| scikit-learn/scikit-learn | 31 | 17 | 17 |
| sphinx-doc/sphinx | 40 | 22 | 15 |

## 环境来源补充（AC 6）

上表“官方镜像拉得到 98”是旧表头，实际表示环境镜像就绪 98 道。
官方镜像 2 道：`pallets__flask-5014`、`pylint-dev__pylint-6386`；以下 96 道按官方配方本机构建。
原始状态取自 `var/cache/swebench/images.json` 的 `source=local-build`，本次收尾未构建新镜像。

- `astropy__astropy-13453`
- `astropy__astropy-14365`
- `astropy__astropy-14096`
- `astropy__astropy-7606`
- `astropy__astropy-14309`
- `astropy__astropy-14369`
- `astropy__astropy-13033`
- `astropy__astropy-14508`
- `astropy__astropy-12907`
- `astropy__astropy-7671`
- `astropy__astropy-7336`
- `matplotlib__matplotlib-23476`
- `matplotlib__matplotlib-20859`
- `matplotlib__matplotlib-24870`
- `matplotlib__matplotlib-25311`
- `matplotlib__matplotlib-25960`
- `matplotlib__matplotlib-24149`
- `matplotlib__matplotlib-26342`
- `matplotlib__matplotlib-24570`
- `matplotlib__matplotlib-20488`
- `matplotlib__matplotlib-20826`
- `matplotlib__matplotlib-26113`
- `matplotlib__matplotlib-24026`
- `matplotlib__matplotlib-22871`
- `matplotlib__matplotlib-25122`
- `matplotlib__matplotlib-25775`
- `matplotlib__matplotlib-23314`
- `matplotlib__matplotlib-24970`
- `mwaskom__seaborn-3069`
- `mwaskom__seaborn-3187`
- `pydata__xarray-4094`
- `pydata__xarray-4075`
- `pydata__xarray-4687`
- `pydata__xarray-3095`
- `pydata__xarray-7233`
- `pydata__xarray-6744`
- `pydata__xarray-6992`
- `pydata__xarray-7393`
- `pydata__xarray-4695`
- `pydata__xarray-3151`
- `pydata__xarray-4629`
- `pylint-dev__pylint-6903`
- `pylint-dev__pylint-4604`
- `pylint-dev__pylint-4970`
- `pylint-dev__pylint-7277`
- `pylint-dev__pylint-4551`
- `pytest-dev__pytest-5840`
- `pytest-dev__pytest-7982`
- `pytest-dev__pytest-5787`
- `pytest-dev__pytest-6197`
- `pytest-dev__pytest-5809`
- `pytest-dev__pytest-7571`
- `pytest-dev__pytest-7324`
- `pytest-dev__pytest-7521`
- `pytest-dev__pytest-7490`
- `pytest-dev__pytest-10356`
- `pytest-dev__pytest-5262`
- `scikit-learn__scikit-learn-10844`
- `scikit-learn__scikit-learn-15100`
- `scikit-learn__scikit-learn-14496`
- `scikit-learn__scikit-learn-13328`
- `scikit-learn__scikit-learn-13124`
- `scikit-learn__scikit-learn-14087`
- `scikit-learn__scikit-learn-11310`
- `scikit-learn__scikit-learn-25102`
- `scikit-learn__scikit-learn-12973`
- `scikit-learn__scikit-learn-14983`
- `scikit-learn__scikit-learn-13142`
- `scikit-learn__scikit-learn-26323`
- `scikit-learn__scikit-learn-10297`
- `scikit-learn__scikit-learn-9288`
- `scikit-learn__scikit-learn-14629`
- `scikit-learn__scikit-learn-10908`
- `scikit-learn__scikit-learn-25931`
- `sphinx-doc__sphinx-7889`
- `sphinx-doc__sphinx-9230`
- `sphinx-doc__sphinx-9461`
- `sphinx-doc__sphinx-9320`
- `sphinx-doc__sphinx-10449`
- `sphinx-doc__sphinx-8638`
- `sphinx-doc__sphinx-9698`
- `sphinx-doc__sphinx-7757`
- `sphinx-doc__sphinx-9281`
- `sphinx-doc__sphinx-8475`
- `sphinx-doc__sphinx-10673`
- `sphinx-doc__sphinx-9367`
- `sphinx-doc__sphinx-8269`
- `sphinx-doc__sphinx-8721`
- `sphinx-doc__sphinx-11445`
- `sphinx-doc__sphinx-8120`
- `sphinx-doc__sphinx-9229`
- `sphinx-doc__sphinx-7454`
- `sphinx-doc__sphinx-10323`
- `sphinx-doc__sphinx-9602`
- `sphinx-doc__sphinx-9711`
- `sphinx-doc__sphinx-7440`
