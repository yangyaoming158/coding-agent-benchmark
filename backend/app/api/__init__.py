"""HTTP 接口层：路由、请求/响应模型、依赖注入。保持很薄，业务逻辑不写在这里。

依赖规则：可依赖 evaluation / benchmark / report；不可被任何模块依赖。
"""
