"""统一的错误响应形状（E7-T0 AC-8）。

所有出错的响应都长这样，一个字段不多一个不少：

    {"code": "RUN_NOT_FOUND", "message": "找不到实验 #42"}

## 为什么要统一

FastAPI 默认的错误体是 `{"detail": ...}`，而 `detail` 有时是字符串、
有时是一个校验错误的数组（422 那种）。前端要么写两套解析，要么把对象
直接渲染成 `[object Object]`。两种都发生过。

`code` 给程序看、`message` 给人看。前端按 `code` 分支（比如 409 的
`RUN_ALREADY_COMPLETED` 要提示"新建一次实验"，而 `RUN_WORKSPACE_DIRTY`
要提示"先提交代码"），按 `message` 显示。

## code 是稳定契约，message 不是

`code` 一旦发出去就当常量看待，改它等于改接口。`message` 是给人读的，
随时可以改得更清楚。所以前端**不许**去匹配 message 的内容。
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException


class ErrorResponse(BaseModel):
    """出错时的响应体。OpenAPI 里所有 4xx/5xx 都指向它。"""

    #: 机器可读的错误码，**稳定契约**，前端按它分支。
    code: str
    #: 给人看的说明，可能随版本变得更清楚，前端不要匹配内容。
    message: str


class ApiError(Exception):
    """业务层面的错误。带上 HTTP 状态码和错误码，由下面的处理器统一渲染。

    直接用它而不是 `HTTPException`：`HTTPException` 只有 `detail` 一个位置，
    塞进去的要么是 code 要么是 message，塞不下两个。
    """

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def not_found(code: str, message: str) -> ApiError:
    return ApiError(status.HTTP_404_NOT_FOUND, code, message)


def conflict(code: str, message: str) -> ApiError:
    """409：请求本身没毛病，但当前状态下做不了这件事。

    典型的是取消一个已经跑完的实验、或者在脏工作区里建实验（协议 C-27）。
    这两件事都不是"参数写错了"（那是 422），也不是"没权限"（403）。
    """
    return ApiError(status.HTTP_409_CONFLICT, code, message)


def forbidden(code: str, message: str) -> ApiError:
    return ApiError(status.HTTP_403_FORBIDDEN, code, message)


#: 给路由的 `responses=` 用，让 OpenAPI 里每个可能的错误码都有 schema。
#: 不写的话生成出来的前端类型里，错误分支是 `unknown`。
ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorResponse, "description": "请求有问题"},
    403: {"model": ErrorResponse, "description": "缺少或写错了 X-Bench-Token"},
    404: {"model": ErrorResponse, "description": "资源不存在"},
    409: {"model": ErrorResponse, "description": "当前状态下做不了这件事"},
    422: {"model": ErrorResponse, "description": "参数不合法"},
}

#: 只读端点用得到的那几个。写端点用上面那个全集。
READ_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: ERROR_RESPONSES[404],
    422: ERROR_RESPONSES[422],
}


def install_error_handlers(app: FastAPI) -> None:
    """把三类异常都收敛成 `{code, message}`。"""

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": exc.code, "message": exc.message},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        # 路由没匹配上（404）、方法不对（405）这些由 Starlette 自己抛，
        # 也要走同一个形状，否则前端在"接口不存在"时拿到的又是 detail
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": _http_code_name(exc.status_code), "message": str(exc.detail)},
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # 校验错误原样拼成一句话。保留字段路径（`query.limit`），
        # 不然只说"输入不合法"，调用方不知道是哪个参数
        parts = [
            f"{'.'.join(str(x) for x in error['loc'])}: {error['msg']}" for error in exc.errors()
        ]
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"code": "INVALID_PARAMETER", "message": "；".join(parts) or "参数不合法"},
        )


def _http_code_name(status_code: int) -> str:
    """给 Starlette 自己抛的那些异常配一个错误码。"""
    return {
        400: "BAD_REQUEST",
        403: "FORBIDDEN",
        404: "NOT_FOUND",
        405: "METHOD_NOT_ALLOWED",
        409: "CONFLICT",
    }.get(status_code, "HTTP_ERROR")


__all__ = [
    "ERROR_RESPONSES",
    "READ_ERROR_RESPONSES",
    "ApiError",
    "ErrorResponse",
    "conflict",
    "forbidden",
    "install_error_handlers",
    "not_found",
]
