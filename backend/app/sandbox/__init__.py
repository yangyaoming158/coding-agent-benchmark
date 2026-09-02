"""沙箱：Docker 封装、工作区物化、镜像构建、资源限额、网络策略。

依赖规则：可依赖 storage / infrastructure / domain。
禁止依赖 runner —— 是 runner 用 sandbox，不能反过来。
"""
