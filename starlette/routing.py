import contextlib
import functools
import inspect
import re
import traceback
import types
import typing
import warnings
from contextlib import asynccontextmanager
from enum import Enum

from starlette._exception_handler import wrap_app_handling_exceptions
from starlette._utils import is_async_callable
from starlette.concurrency import run_in_threadpool
from starlette.convertors import CONVERTOR_TYPES, Convertor
from starlette.datastructures import URL, Headers, URLPath
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Lifespan, Receive, Scope, Send
from starlette.websockets import WebSocket, WebSocketClose


class NoMatchFound(Exception):
    """
    Raised by `.url_for(name, **path_params)` and `.url_path_for(name, **path_params)`
    if no matching route exists.
    """

    def __init__(self, name: str, path_params: typing.Dict[str, typing.Any]) -> None:
        params = ", ".join(list(path_params.keys()))
        super().__init__(f'No route exists for name "{name}" and params "{params}".')


class Match(Enum):
    """
    匹配路由结果

    NONE: 没有匹配到任何路由。
    PARTIAL: 匹配到了部分路由，但还没有完全匹配。如请求的路径与路由的正则表达式匹配成功，但请求的方法不在路由对象中定义的方法列表中
    FULL: 完全匹配了某个路由。
    """

    NONE = 0
    PARTIAL = 1
    FULL = 2


def iscoroutinefunction_or_partial(
    obj: typing.Any,
) -> bool:  # pragma: no cover
    """
    Correctly determines if an object is a coroutine function,
    including those wrapped in functools.partial objects.
    """
    warnings.warn(
        "iscoroutinefunction_or_partial is deprecated, "
        "and will be removed in a future release.",
        DeprecationWarning,
    )
    while isinstance(obj, functools.partial):
        obj = obj.func
    return inspect.iscoroutinefunction(obj)


def request_response(
    func: typing.Callable[[Request], typing.Union[typing.Awaitable[Response], Response]]
) -> ASGIApp:
    """
    Takes a function or coroutine `func(request) -> response`,
    and returns an ASGI application.
    """

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        """
        An ASGI application that handles HTTP requests.

        :param scope: The ASGI scope of the request.
        :param receive: A coroutine that receives incoming messages from the client.
        :param send: A coroutine that sends outgoing messages to the client.
        """
        request = Request(scope, receive, send)

        async def app(scope: Scope, receive: Receive, send: Send) -> None:
            if is_async_callable(func):
                # 如果传入的函数是异步函数，则直接调用它。
                response = await func(request)
            else:
                # 如果不是，则在线程池中运行它。
                # TODO：
                response = await run_in_threadpool(func, request)
            await response(scope, receive, send)

        # 给处处理的逻辑加上异常的处理
        # 再遇到http/websocket的异常时，转换成http/websocket的响应
        await wrap_app_handling_exceptions(app, request)(scope, receive, send)

    return app


def websocket_session(
    func: typing.Callable[[WebSocket], typing.Awaitable[None]]
) -> ASGIApp:
    """
    和 request_response 这个方法类型的功能
    websocket 中不同的地方就是没有request 和 response。但是实际上处理的方式几乎一致
    Takes a coroutine `func(session)`, and returns an ASGI application.
    """
    # assert asyncio.iscoroutinefunction(func), "WebSocket endpoints must be async"

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        session = WebSocket(scope, receive=receive, send=send)

        async def app(scope: Scope, receive: Receive, send: Send) -> None:
            await func(session)

        await wrap_app_handling_exceptions(app, session)(scope, receive, send)

    return app


def get_name(endpoint: typing.Callable[..., typing.Any]) -> str:
    """
    获取endpoint名称。
    如果 endpoint 是一个函数或类，则直接返回其 __name__ 属性。
    如果 endpoint 是一个实例，则获取其类的名称。
    """
    if inspect.isroutine(endpoint) or inspect.isclass(endpoint):
        return endpoint.__name__
    # 若是一个实例，则获取其类的名称
    return endpoint.__class__.__name__


def replace_params(
    path: str,
    param_convertors: typing.Dict[str, Convertor[typing.Any]],
    path_params: typing.Dict[str, str],
) -> typing.Tuple[str, typing.Dict[str, str]]:
    for key, value in list(path_params.items()):
        if "{" + key + "}" in path:
            convertor = param_convertors[key]
            value = convertor.to_string(value)
            path = path.replace("{" + key + "}", value)
            path_params.pop(key)
    return path, path_params


# Match parameters in URL paths, eg. '{param}', and '{param:int}'
PARAM_REGEX = re.compile("{([a-zA-Z_][a-zA-Z0-9_]*)(:[a-zA-Z_][a-zA-Z0-9_]*)?}")


def compile_path(
    path: str,
) -> typing.Tuple[typing.Pattern[str], str, typing.Dict[str, Convertor[typing.Any]]]:
    """
    Given a path string, like: "/{username:str}",
    or a host string, like: "{subdomain}.mydomain.org", return a three-tuple
    of (regex, format, {param_name:convertor}).

    regex:      "/(?P<username>[^/]+)"
    format:     "/{username}"
    convertors: {"username": StringConvertor()}
    """

    is_host = not path.startswith("/")

    path_regex = "^"
    path_format = ""
    duplicated_params = set()
    idx = 0
    param_convertors = {}

    # 循环每一个匹配的参数
    for match in PARAM_REGEX.finditer(path):
        # path = "/int/{param:int}" => ('param', ':int')
        param_name, convertor_type = match.groups("str")
        # convertor_type = 'int'
        convertor_type = convertor_type.lstrip(":")

        # 获取类型转换器
        assert (
            convertor_type in CONVERTOR_TYPES
        ), f"Unknown path convertor '{convertor_type}'"
        convertor = CONVERTOR_TYPES[convertor_type]

        # 添加目前路径段的匹配到路径正则中，'^/int/'
        path_regex += re.escape(path[idx : match.start()])
        # 添加目前路径参数类型的匹配到路径正则中, '^/int/(?P<param>[0-9]+)'
        path_regex += f"(?P<{param_name}>{convertor.regex})"

        # '/int/'
        path_format += path[idx : match.start()]
        # '/int/{param}'
        path_format += "{%s}" % param_name

        # 校验是否有重复的参数名
        if param_name in param_convertors:
            duplicated_params.add(param_name)

        param_convertors[param_name] = convertor

        # 更新idx，准备下一段的匹配
        idx = match.end()

    if duplicated_params:
        # 存在重复的参数名，抛错
        names = ", ".join(sorted(duplicated_params))
        ending = "s" if len(duplicated_params) > 1 else ""
        raise ValueError(f"Duplicated param name{ending} {names} at path {path}")

    if is_host:
        # Align with `Host.matches()` behavior, which ignores port.
        # 路径字符串是主机名，则在编译正则表达式时，只需要在路径结尾添加 $，以匹配主机名
        hostname = path[idx:].split(":")[0]
        path_regex += re.escape(hostname) + "$"
    else:
        path_regex += re.escape(path[idx:]) + "$"

    path_format += path[idx:]

    return re.compile(path_regex), path_format, param_convertors


class BaseRoute:
    def matches(self, scope: Scope) -> typing.Tuple[Match, Scope]:
        raise NotImplementedError()  # pragma: no cover

    def url_path_for(self, name: str, /, **path_params: typing.Any) -> URLPath:
        raise NotImplementedError()  # pragma: no cover

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        raise NotImplementedError()  # pragma: no cover

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """
        A route may be used in isolation as a stand-alone ASGI app.
        This is a somewhat contrived case, as they'll almost always be used
        within a Router, but could be useful for some tooling and minimal apps.
        """
        # matches的逻辑由子类去实现
        match, child_scope = self.matches(scope)
        if match == Match.NONE:
            if scope["type"] == "http":
                # 处理没有匹配上的情况，没有匹配返回404
                response = PlainTextResponse("Not Found", status_code=404)
                await response(scope, receive, send)
            elif scope["type"] == "websocket":
                # 处理没有匹配上的情况，关闭socket
                websocket_close = WebSocketClose()
                await websocket_close(scope, receive, send)
            return

        # 每一个请求，接收时，都会创建一个scope的字典
        # 路由匹配的时候可以需要返回额外的处理信息，以便后续处理的时候可以获得响应的信息
        # 以下是一个例子，比如补充请求方法和路径参数
        # child_scope = {
        #     "endpoint": self.endpoint,
        #     "path_params": path_params,
        # }

        scope.update(child_scope)
        # handle的逻辑由子类去实现
        await self.handle(scope, receive, send)


class Route(BaseRoute):
    def __init__(
        self,
        path: str,
        endpoint: typing.Callable[..., typing.Any],
        *,
        methods: typing.Optional[typing.List[str]] = None,
        name: typing.Optional[str] = None,
        include_in_schema: bool = True,
    ) -> None:
        assert path.startswith("/"), "Routed paths must start with '/'"
        self.path = path
        self.endpoint = endpoint
        self.name = get_name(endpoint) if name is None else name
        self.include_in_schema = include_in_schema

        endpoint_handler = endpoint
        while isinstance(endpoint_handler, functools.partial):
            # 因为 functools.partial 对象也是可调用对象，它包装了一个函数或方法，并固定了部分参数，
            # 因此需要将其解包，获取原始的函数或方法，才能进行后续的处理。
            endpoint_handler = endpoint_handler.func
        if inspect.isfunction(endpoint_handler) or inspect.ismethod(endpoint_handler):
            # Endpoint is function or method. Treat it as `func(request) -> response`.
            self.app = request_response(endpoint)
            if methods is None:
                methods = ["GET"]
        else:
            # Endpoint is a class. Treat it as ASGI.
            # 如果 endpoint_handler 不是函数或方法，那么将其视为一个 ASGI 应用程序。
            # 这意味着该路由将直接使用传入的 endpoint 参数作为处理函数，而不需要进行转换
            self.app = endpoint

        if methods is None:
            # 如果 self.methods 属性为 None，则表示该路由可以接受任何 HTTP 请求方法。
            self.methods = None
        else:
            self.methods = {method.upper() for method in methods}
            if "GET" in self.methods:
                # HTTP 协议规定，如果服务器支持 GET 请求，那么它也必须支持 HEAD 请求，
                # 且 HEAD 请求的响应与 GET 请求的响应相同，只是没有响应体。
                self.methods.add("HEAD")

        # Given a path string, like: "/{username:str}",
        # regex:      "/(?P<username>[^/]+)"
        # format:     "/{username}"
        # convertors: {"username": StringConvertor()}
        (
            self.path_regex,
            self.path_format,
            self.param_convertors,
        ) = compile_path(path)

    def matches(self, scope: Scope) -> typing.Tuple[Match, Scope]:
        if scope["type"] == "http":
            match = self.path_regex.match(scope["path"])
            if match:
                matched_params = match.groupdict()
                for key, value in matched_params.items():
                    matched_params[key] = self.param_convertors[key].convert(value)
                path_params = dict(scope.get("path_params", {}))
                path_params.update(matched_params)
                child_scope = {
                    "endpoint": self.endpoint,
                    "path_params": path_params,
                }
                if self.methods and scope["method"] not in self.methods:
                    return Match.PARTIAL, child_scope
                else:
                    return Match.FULL, child_scope
        return Match.NONE, {}

    def url_path_for(self, name: str, /, **path_params: typing.Any) -> URLPath:
        """
        根据路由名称和路径参数生成对应的 URL。
        """
        seen_params = set(path_params.keys())
        expected_params = set(self.param_convertors.keys())

        if name != self.name or seen_params != expected_params:
            raise NoMatchFound(name, path_params)

        # 将路径参数替换为路径格式
        path, remaining_params = replace_params(
            self.path_format, self.param_convertors, path_params
        )
        # 确保没有剩余的参数
        assert not remaining_params
        return URLPath(path=path, protocol="http")

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.methods and scope["method"] not in self.methods:
            headers = {"Allow": ", ".join(self.methods)}
            if "app" in scope:
                raise HTTPException(status_code=405, headers=headers)
            else:
                response = PlainTextResponse(
                    "Method Not Allowed", status_code=405, headers=headers
                )
            await response(scope, receive, send)
        else:
            await self.app(scope, receive, send)

    def __eq__(self, other: typing.Any) -> bool:
        return (
            isinstance(other, Route)
            and self.path == other.path
            and self.endpoint == other.endpoint
            and self.methods == other.methods
        )

    def __repr__(self) -> str:
        class_name = self.__class__.__name__
        methods = sorted(self.methods or [])
        path, name = self.path, self.name
        return f"{class_name}(path={path!r}, name={name!r}, methods={methods!r})"


class WebSocketRoute(BaseRoute):
    def __init__(
        self,
        path: str,
        endpoint: typing.Callable[..., typing.Any],
        *,
        name: typing.Optional[str] = None,
    ) -> None:
        assert path.startswith("/"), "Routed paths must start with '/'"
        self.path = path
        self.endpoint = endpoint
        self.name = get_name(endpoint) if name is None else name

        endpoint_handler = endpoint
        while isinstance(endpoint_handler, functools.partial):
            endpoint_handler = endpoint_handler.func
        if inspect.isfunction(endpoint_handler) or inspect.ismethod(endpoint_handler):
            # Endpoint is function or method. Treat it as `func(websocket)`.
            self.app = websocket_session(endpoint)
        else:
            # Endpoint is a class. Treat it as ASGI.
            self.app = endpoint

        (
            self.path_regex,
            self.path_format,
            self.param_convertors,
        ) = compile_path(path)

    def matches(self, scope: Scope) -> typing.Tuple[Match, Scope]:
        if scope["type"] == "websocket":
            match = self.path_regex.match(scope["path"])
            if match:
                matched_params = match.groupdict()
                for key, value in matched_params.items():
                    matched_params[key] = self.param_convertors[key].convert(value)
                path_params = dict(scope.get("path_params", {}))
                path_params.update(matched_params)
                child_scope = {
                    "endpoint": self.endpoint,
                    "path_params": path_params,
                }
                return Match.FULL, child_scope
        return Match.NONE, {}

    def url_path_for(self, name: str, /, **path_params: typing.Any) -> URLPath:
        seen_params = set(path_params.keys())
        expected_params = set(self.param_convertors.keys())

        if name != self.name or seen_params != expected_params:
            raise NoMatchFound(name, path_params)

        path, remaining_params = replace_params(
            self.path_format, self.param_convertors, path_params
        )
        assert not remaining_params
        return URLPath(path=path, protocol="websocket")

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.app(scope, receive, send)

    def __eq__(self, other: typing.Any) -> bool:
        return (
            isinstance(other, WebSocketRoute)
            and self.path == other.path
            and self.endpoint == other.endpoint
        )

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(path={self.path!r}, name={self.name!r})"


class Mount(BaseRoute):
    """
    Mount 对象表示一个应用程序的挂载点，它可以将一个应用程序挂载到另一个应用程序的指定路径下
    """

    def __init__(
        self,
        path: str,
        app: typing.Optional[ASGIApp] = None,
        routes: typing.Optional[typing.Sequence[BaseRoute]] = None,
        name: typing.Optional[str] = None,
        *,
        middleware: typing.Optional[typing.Sequence[Middleware]] = None,
    ) -> None:
        """

        app 是一个完整的应用子程序，可以包含多个路由器和API路由
        routers 是一个路由器对象，可以包含多个API路由

        Mount 类区分 app 和 routes 主要是为了方便用户使用和管理 API 路由，
        可以根据需要选择挂载整个子应用程序或者挂载单个路由器中的 API 路由。

        """
        # 挂在的是路由器或者是应用
        assert path == "" or path.startswith("/"), "Routed paths must start with '/'"
        assert (
            app is not None or routes is not None
        ), "Either 'app=...', or 'routes=' must be specified"

        self.path = path.rstrip("/")
        if app is not None:
            # 若是独立的应用
            self._base_app: ASGIApp = app
        else:
            # 若是路由器
            self._base_app = Router(routes=routes)
        self.app = self._base_app
        # 处理mount的中间件
        if middleware is not None:
            for cls, options in reversed(middleware):
                self.app = cls(app=self.app, **options)
        self.name = name
        (
            self.path_regex,
            self.path_format,
            self.param_convertors,
        ) = compile_path(self.path + "/{path:path}")

    @property
    def routes(self) -> typing.List[BaseRoute]:
        return getattr(self._base_app, "routes", [])

    def matches(self, scope: Scope) -> typing.Tuple[Match, Scope]:
        if scope["type"] in ("http", "websocket"):
            path = scope["path"]
            match = self.path_regex.match(path)
            if match:
                matched_params = match.groupdict()
                for key, value in matched_params.items():
                    matched_params[key] = self.param_convertors[key].convert(value)
                remaining_path = "/" + matched_params.pop("path")
                matched_path = path[: -len(remaining_path)]
                path_params = dict(scope.get("path_params", {}))
                path_params.update(matched_params)
                root_path = scope.get("root_path", "")
                # 相比普通的路由，多了
                # app_root_path:应用程序的根路径，用于将匹配到的路径添加到应用程序的根路径后面，形成完整的请求路径
                # root_path:是应用程序的根路径，用于将匹配到的路径添加到根路径后面，形成完整的请求路径
                # path: 实际的路径

                child_scope = {
                    "path_params": path_params,
                    "app_root_path": scope.get("app_root_path", root_path),
                    "root_path": root_path + matched_path,
                    "path": remaining_path,
                    "endpoint": self.app,
                }
                return Match.FULL, child_scope
        return Match.NONE, {}

    def url_path_for(self, name: str, /, **path_params: typing.Any) -> URLPath:
        if self.name is not None and name == self.name and "path" in path_params:
            # 'name' matches "<mount_name>".
            path_params["path"] = path_params["path"].lstrip("/")
            path, remaining_params = replace_params(
                self.path_format, self.param_convertors, path_params
            )
            if not remaining_params:
                return URLPath(path=path)
        elif self.name is None or name.startswith(self.name + ":"):
            if self.name is None:
                # No mount name.
                remaining_name = name
            else:
                # 'name' matches "<mount_name>:<child_name>".
                remaining_name = name[len(self.name) + 1 :]
            path_kwarg = path_params.get("path")
            path_params["path"] = ""
            path_prefix, remaining_params = replace_params(
                self.path_format, self.param_convertors, path_params
            )
            if path_kwarg is not None:
                remaining_params["path"] = path_kwarg
            for route in self.routes or []:
                try:
                    url = route.url_path_for(remaining_name, **remaining_params)
                    return URLPath(
                        path=path_prefix.rstrip("/") + str(url),
                        protocol=url.protocol,
                    )
                except NoMatchFound:
                    pass
        raise NoMatchFound(name, path_params)

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.app(scope, receive, send)

    def __eq__(self, other: typing.Any) -> bool:
        return (
            isinstance(other, Mount)
            and self.path == other.path
            and self.app == other.app
        )

    def __repr__(self) -> str:
        class_name = self.__class__.__name__
        name = self.name or ""
        return f"{class_name}(path={self.path!r}, name={name!r}, app={self.app!r})"


class Host(BaseRoute):
    # TODO: 和其他有什么不一样的地方，单纯的方位到其他主机？
    def __init__(
        self, host: str, app: ASGIApp, name: typing.Optional[str] = None
    ) -> None:
        assert not host.startswith("/"), "Host must not start with '/'"
        self.host = host
        self.app = app
        self.name = name
        (
            self.host_regex,
            self.host_format,
            self.param_convertors,
        ) = compile_path(host)

    @property
    def routes(self) -> typing.List[BaseRoute]:
        return getattr(self.app, "routes", [])

    def matches(self, scope: Scope) -> typing.Tuple[Match, Scope]:
        if scope["type"] in ("http", "websocket"):
            headers = Headers(scope=scope)
            host = headers.get("host", "").split(":")[0]
            match = self.host_regex.match(host)
            if match:
                matched_params = match.groupdict()
                for key, value in matched_params.items():
                    matched_params[key] = self.param_convertors[key].convert(value)
                path_params = dict(scope.get("path_params", {}))
                path_params.update(matched_params)
                child_scope = {
                    "path_params": path_params,
                    "endpoint": self.app,
                }
                return Match.FULL, child_scope
        return Match.NONE, {}

    def url_path_for(self, name: str, /, **path_params: typing.Any) -> URLPath:
        if self.name is not None and name == self.name and "path" in path_params:
            # 'name' matches "<mount_name>".
            path = path_params.pop("path")
            host, remaining_params = replace_params(
                self.host_format, self.param_convertors, path_params
            )
            if not remaining_params:
                return URLPath(path=path, host=host)
        elif self.name is None or name.startswith(self.name + ":"):
            if self.name is None:
                # No mount name.
                remaining_name = name
            else:
                # 'name' matches "<mount_name>:<child_name>".
                remaining_name = name[len(self.name) + 1 :]
            host, remaining_params = replace_params(
                self.host_format, self.param_convertors, path_params
            )
            for route in self.routes or []:
                try:
                    url = route.url_path_for(remaining_name, **remaining_params)
                    return URLPath(path=str(url), protocol=url.protocol, host=host)
                except NoMatchFound:
                    pass
        raise NoMatchFound(name, path_params)

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.app(scope, receive, send)

    def __eq__(self, other: typing.Any) -> bool:
        return (
            isinstance(other, Host)
            and self.host == other.host
            and self.app == other.app
        )

    def __repr__(self) -> str:
        class_name = self.__class__.__name__
        name = self.name or ""
        return f"{class_name}(host={self.host!r}, name={name!r}, app={self.app!r})"


_T = typing.TypeVar("_T")


class _AsyncLiftContextManager(typing.AsyncContextManager[_T]):
    def __init__(self, cm: typing.ContextManager[_T]):
        self._cm = cm

    async def __aenter__(self) -> _T:
        return self._cm.__enter__()

    async def __aexit__(
        self,
        exc_type: typing.Optional[typing.Type[BaseException]],
        exc_value: typing.Optional[BaseException],
        traceback: typing.Optional[types.TracebackType],
    ) -> typing.Optional[bool]:
        return self._cm.__exit__(exc_type, exc_value, traceback)


def _wrap_gen_lifespan_context(
    lifespan_context: typing.Callable[
        [typing.Any], typing.Generator[typing.Any, typing.Any, typing.Any]
    ]
) -> typing.Callable[[typing.Any], typing.AsyncContextManager[typing.Any]]:
    cmgr = contextlib.contextmanager(lifespan_context)

    @functools.wraps(cmgr)
    def wrapper(app: typing.Any) -> _AsyncLiftContextManager[typing.Any]:
        return _AsyncLiftContextManager(cmgr(app))

    return wrapper


class _DefaultLifespan:
    def __init__(self, router: "Router"):
        self._router = router

    async def __aenter__(self) -> None:
        await self._router.startup()

    async def __aexit__(self, *exc_info: object) -> None:
        await self._router.shutdown()

    def __call__(self: _T, app: object) -> _T:
        return self


class Router:
    def __init__(
        self,
        routes: typing.Optional[typing.Sequence[BaseRoute]] = None,
        redirect_slashes: bool = True,  # 用于指定是否重定向带斜杠的 URL。
        default: typing.Optional[ASGIApp] = None,
        on_startup: typing.Optional[
            typing.Sequence[typing.Callable[[], typing.Any]]
        ] = None,
        on_shutdown: typing.Optional[
            typing.Sequence[typing.Callable[[], typing.Any]]
        ] = None,
        # the generic to Lifespan[AppType] is the type of the top level application
        # which the router cannot know statically, so we use typing.Any
        lifespan: typing.Optional[Lifespan[typing.Any]] = None,
    ) -> None:
        """
        redirect_slashes 这个属性的默认值为 True，目的是为了遵循 Web 应用程序的最佳实践，

        因为在 Web 应用程序中，通常会将 URL 的末尾加上斜杠 /，以表示该 URL 对应的是一个目录或者子路径。
        例如，example.com/blog/ 表示 example.com 域名下的 blog 目录。

        如果用户在访问一个 URL 时，没有在末尾加上斜杠 /，
        则有些 Web 服务器会自动将其重定向到带有斜杠的 URL。
        这样做的好处是可以避免搜索引擎将同一个页面的不同 URL 视为不同的页面


        default：在处理路由时找不到路由的保底措施，默认的保底时使用过self.not_found
        """

        self.routes = [] if routes is None else list(routes)
        self.redirect_slashes = redirect_slashes
        self.default = self.not_found if default is None else default
        self.on_startup = [] if on_startup is None else list(on_startup)
        self.on_shutdown = [] if on_shutdown is None else list(on_shutdown)

        if on_startup or on_shutdown:
            warnings.warn(
                "The on_startup and on_shutdown parameters are deprecated, and they "
                "will be removed on version 1.0. Use the lifespan parameter instead. "
                "See more about it on https://www.starlette.io/lifespan/.",
                DeprecationWarning,
            )
            if lifespan:
                warnings.warn(
                    "The `lifespan` parameter cannot be used with `on_startup` or "
                    "`on_shutdown`. Both `on_startup` and `on_shutdown` will be "
                    "ignored."
                )

        if lifespan is None:
            self.lifespan_context: Lifespan[typing.Any] = _DefaultLifespan(self)

        elif inspect.isasyncgenfunction(lifespan):
            # 是一个异步生成器函数
            warnings.warn(
                "async generator function lifespans are deprecated, "
                "use an @contextlib.asynccontextmanager function instead",
                DeprecationWarning,
            )
            self.lifespan_context = asynccontextmanager(
                lifespan,
            )
        elif inspect.isgeneratorfunction(lifespan):
            # 是一个生成器函数
            warnings.warn(
                "generator function lifespans are deprecated, "
                "use an @contextlib.asynccontextmanager function instead",
                DeprecationWarning,
            )
            # 使用自定义的异步上下文管理器，
            # 将同步的上下文管理器改成异步的上下文管理器
            self.lifespan_context = _wrap_gen_lifespan_context(
                lifespan,
            )
        else:
            self.lifespan_context = lifespan

    async def not_found(self, scope: Scope, receive: Receive, send: Send) -> None:
        # 没有找到websocket就直接关闭
        if scope["type"] == "websocket":
            websocket_close = WebSocketClose()
            await websocket_close(scope, receive, send)
            return

        # If we're running inside a starlette application then raise an
        # exception, so that the configurable exception handler can deal with
        # returning the response. For plain ASGI apps, just return the response.
        if "app" in scope:
            raise HTTPException(status_code=404)
        else:
            response = PlainTextResponse("Not Found", status_code=404)
        await response(scope, receive, send)

    def url_path_for(self, name: str, /, **path_params: typing.Any) -> URLPath:
        for route in self.routes:
            try:
                return route.url_path_for(name, **path_params)
            except NoMatchFound:
                pass
        raise NoMatchFound(name, path_params)

    async def startup(self) -> None:
        """
        Run any `.on_startup` event handlers.
        """
        for handler in self.on_startup:
            if is_async_callable(handler):
                await handler()
            else:
                handler()

    async def shutdown(self) -> None:
        """
        Run any `.on_shutdown` event handlers.
        """
        for handler in self.on_shutdown:
            if is_async_callable(handler):
                await handler()
            else:
                handler()

    async def lifespan(self, scope: Scope, receive: Receive, send: Send) -> None:
        """
        Handle ASGI lifespan messages, which allows us to manage application
        startup and shutdown events.
        """
        started = False
        app: typing.Any = scope.get("app")
        await receive()
        try:
            async with self.lifespan_context(app) as maybe_state:
                # maybe_state share the objects between the lifespan, and the requests
                if maybe_state is not None:
                    if "state" not in scope:
                        raise RuntimeError(
                            'The server does not support "state" in the lifespan scope.'
                        )
                    scope["state"].update(maybe_state)
                await send({"type": "lifespan.startup.complete"})
                started = True
                await receive()
        except BaseException:
            exc_text = traceback.format_exc()
            if started:
                await send({"type": "lifespan.shutdown.failed", "message": exc_text})
            else:
                await send({"type": "lifespan.startup.failed", "message": exc_text})
            raise
        else:
            await send({"type": "lifespan.shutdown.complete"})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """
        The main entry point to the Router class.
        这部分实在请求进来的时候进行调用
        """
        assert scope["type"] in ("http", "websocket", "lifespan")

        if "router" not in scope:
            scope["router"] = self

        if scope["type"] == "lifespan":
            await self.lifespan(scope, receive, send)
            return

        partial = None  # 用于记录部分匹配时的路有

        for route in self.routes:
            # Determine if any route matches the incoming scope,
            # and hand over to the matching route if found.
            match, child_scope = route.matches(scope)
            if match == Match.FULL:
                scope.update(child_scope)
                await route.handle(scope, receive, send)
                return
            elif match == Match.PARTIAL and partial is None:
                partial = route
                partial_scope = child_scope

        if partial is not None:
            #  Handle partial matches. These are cases where an endpoint is
            # able to handle the request, but is not a preferred option.
            # We use this in particular to deal with "405 Method Not Allowed".
            scope.update(partial_scope)
            await partial.handle(scope, receive, send)
            return

        # 没有找到路有的情况下，会判断是否需要进行重定向
        if scope["type"] == "http" and self.redirect_slashes and scope["path"] != "/":
            redirect_scope = dict(scope)

            # 这里试试有末尾带"/"和不到"/"的反例
            if scope["path"].endswith("/"):
                redirect_scope["path"] = redirect_scope["path"].rstrip("/")
            else:
                redirect_scope["path"] = redirect_scope["path"] + "/"

            for route in self.routes:
                match, child_scope = route.matches(redirect_scope)
                if match != Match.NONE:
                    redirect_url = URL(scope=redirect_scope)
                    response = RedirectResponse(url=str(redirect_url))
                    await response(scope, receive, send)
                    return

        await self.default(scope, receive, send)

    def __eq__(self, other: typing.Any) -> bool:
        return isinstance(other, Router) and self.routes == other.routes

    def mount(
        self, path: str, app: ASGIApp, name: typing.Optional[str] = None
    ) -> None:  # pragma: nocover
        route = Mount(path, app=app, name=name)
        self.routes.append(route)

    def host(
        self, host: str, app: ASGIApp, name: typing.Optional[str] = None
    ) -> None:  # pragma: no cover
        route = Host(host, app=app, name=name)
        self.routes.append(route)

    def add_route(
        self,
        path: str,
        endpoint: typing.Callable[
            [Request], typing.Union[typing.Awaitable[Response], Response]
        ],
        methods: typing.Optional[typing.List[str]] = None,
        name: typing.Optional[str] = None,
        include_in_schema: bool = True,
    ) -> None:  # pragma: nocover
        route = Route(
            path,
            endpoint=endpoint,
            methods=methods,
            name=name,
            include_in_schema=include_in_schema,
        )
        self.routes.append(route)

    def add_websocket_route(
        self,
        path: str,
        endpoint: typing.Callable[[WebSocket], typing.Awaitable[None]],
        name: typing.Optional[str] = None,
    ) -> None:  # pragma: no cover
        route = WebSocketRoute(path, endpoint=endpoint, name=name)
        self.routes.append(route)

    def route(
        self,
        path: str,
        methods: typing.Optional[typing.List[str]] = None,
        name: typing.Optional[str] = None,
        include_in_schema: bool = True,
    ) -> typing.Callable:  # type: ignore[type-arg]
        """
        We no longer document this decorator style API, and its usage is discouraged.
        Instead you should use the following approach:

        >>> routes = [Route(path, endpoint=...), ...]
        >>> app = Starlette(routes=routes)
        """
        warnings.warn(
            "The `route` decorator is deprecated, and will be removed in version 1.0.0."
            "Refer to https://www.starlette.io/routing/#http-routing for the recommended approach.",  # noqa: E501
            DeprecationWarning,
        )

        def decorator(func: typing.Callable) -> typing.Callable:  # type: ignore[type-arg]  # noqa: E501
            self.add_route(
                path,
                func,
                methods=methods,
                name=name,
                include_in_schema=include_in_schema,
            )
            return func

        return decorator

    def websocket_route(
        self, path: str, name: typing.Optional[str] = None
    ) -> typing.Callable:  # type: ignore[type-arg]
        """
        We no longer document this decorator style API, and its usage is discouraged.
        Instead you should use the following approach:

        >>> routes = [WebSocketRoute(path, endpoint=...), ...]
        >>> app = Starlette(routes=routes)
        """
        warnings.warn(
            "The `websocket_route` decorator is deprecated, and will be removed in version 1.0.0. Refer to "  # noqa: E501
            "https://www.starlette.io/routing/#websocket-routing for the recommended approach.",  # noqa: E501
            DeprecationWarning,
        )

        def decorator(func: typing.Callable) -> typing.Callable:  # type: ignore[type-arg]  # noqa: E501
            self.add_websocket_route(path, func, name=name)
            return func

        return decorator

    def add_event_handler(
        self, event_type: str, func: typing.Callable[[], typing.Any]
    ) -> None:  # pragma: no cover
        assert event_type in ("startup", "shutdown")

        if event_type == "startup":
            self.on_startup.append(func)
        else:
            self.on_shutdown.append(func)

    def on_event(self, event_type: str) -> typing.Callable:  # type: ignore[type-arg]
        warnings.warn(
            "The `on_event` decorator is deprecated, and will be removed in version 1.0.0. "  # noqa: E501
            "Refer to https://www.starlette.io/lifespan/ for recommended approach.",
            DeprecationWarning,
        )

        def decorator(func: typing.Callable) -> typing.Callable:  # type: ignore[type-arg]  # noqa: E501
            self.add_event_handler(event_type, func)
            return func

        return decorator
