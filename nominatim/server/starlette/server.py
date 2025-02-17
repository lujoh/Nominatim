# SPDX-License-Identifier: GPL-2.0-only
#
# This file is part of Nominatim. (https://nominatim.org)
#
# Copyright (C) 2023 by the Nominatim developer community.
# For a full list of authors see the git log.
"""
Server implementation using the starlette webserver framework.
"""
from typing import Any, Optional, Mapping, Callable, cast, Coroutine, Dict, Awaitable
from pathlib import Path
import datetime as dt

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.exceptions import HTTPException
from starlette.responses import Response, PlainTextResponse
from starlette.requests import Request
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.middleware.cors import CORSMiddleware

from nominatim.api import NominatimAPIAsync
import nominatim.api.v1 as api_impl
from nominatim.config import Configuration

class ParamWrapper(api_impl.ASGIAdaptor):
    """ Adaptor class for server glue to Starlette framework.
    """

    def __init__(self, request: Request) -> None:
        self.request = request


    def get(self, name: str, default: Optional[str] = None) -> Optional[str]:
        return self.request.query_params.get(name, default=default)


    def get_header(self, name: str, default: Optional[str] = None) -> Optional[str]:
        return self.request.headers.get(name, default)


    def error(self, msg: str, status: int = 400) -> HTTPException:
        return HTTPException(status, detail=msg,
                             headers={'content-type': self.content_type})


    def create_response(self, status: int, output: str, num_results: int) -> Response:
        self.request.state.num_results = num_results
        return Response(output, status_code=status, media_type=self.content_type)


    def base_uri(self) -> str:
        scheme = self.request.url.scheme
        host = self.request.url.hostname
        port = self.request.url.port
        root = self.request.scope['root_path']
        if (scheme == 'http' and port == 80) or (scheme == 'https' and port == 443):
            port = None
        if port is not None:
            return f"{scheme}://{host}:{port}{root}"

        return f"{scheme}://{host}{root}"


    def config(self) -> Configuration:
        return cast(Configuration, self.request.app.state.API.config)


def _wrap_endpoint(func: api_impl.EndpointFunc)\
        -> Callable[[Request], Coroutine[Any, Any, Response]]:
    async def _callback(request: Request) -> Response:
        return cast(Response, await func(request.app.state.API, ParamWrapper(request)))

    return _callback


class FileLoggingMiddleware(BaseHTTPMiddleware):
    """ Middleware to log selected requests into a file.
    """

    def __init__(self, app: Starlette, file_name: str = ''):
        super().__init__(app)
        self.fd = open(file_name, 'a', buffering=1, encoding='utf8') # pylint: disable=R1732

    async def dispatch(self, request: Request,
                       call_next: RequestResponseEndpoint) -> Response:
        start = dt.datetime.now(tz=dt.timezone.utc)
        response = await call_next(request)

        if response.status_code != 200:
            return response

        finish = dt.datetime.now(tz=dt.timezone.utc)

        for endpoint in ('reverse', 'search', 'lookup', 'details'):
            if request.url.path.startswith('/' + endpoint):
                qtype = endpoint
                break
        else:
            return response

        duration = (finish - start).total_seconds()
        params = request.scope['query_string'].decode('utf8')

        self.fd.write(f"[{start.replace(tzinfo=None).isoformat(sep=' ', timespec='milliseconds')}] "
                      f"{duration:.4f} {getattr(request.state, 'num_results', 0)} "
                      f'{qtype} "{params}"\n')

        return response


async def timeout_error(request: Request, #pylint: disable=unused-argument
                        _: Exception) -> Response:
    """ Error handler for query timeouts.
    """
    return PlainTextResponse("Query took too long to process.", status_code=503)


def get_application(project_dir: Path,
                    environ: Optional[Mapping[str, str]] = None,
                    debug: bool = True) -> Starlette:
    """ Create a Nominatim falcon ASGI application.
    """
    config = Configuration(project_dir, environ)

    routes = []
    legacy_urls = config.get_bool('SERVE_LEGACY_URLS')
    for name, func in api_impl.ROUTES:
        endpoint = _wrap_endpoint(func)
        routes.append(Route(f"/{name}", endpoint=endpoint))
        if legacy_urls:
            routes.append(Route(f"/{name}.php", endpoint=endpoint))

    middleware = []
    if config.get_bool('CORS_NOACCESSCONTROL'):
        middleware.append(Middleware(CORSMiddleware,
                                     allow_origins=['*'],
                                     allow_methods=['GET', 'OPTIONS'],
                                     max_age=86400))

    log_file = config.LOG_FILE
    if log_file:
        middleware.append(Middleware(FileLoggingMiddleware, file_name=log_file))

    exceptions: Dict[Any, Callable[[Request, Exception], Awaitable[Response]]] = {
        TimeoutError: timeout_error
    }

    async def _shutdown() -> None:
        await app.state.API.close()

    app = Starlette(debug=debug, routes=routes, middleware=middleware,
                    exception_handlers=exceptions,
                    on_shutdown=[_shutdown])

    app.state.API = NominatimAPIAsync(project_dir, environ)

    return app


def run_wsgi() -> Starlette:
    """ Entry point for uvicorn.
    """
    return get_application(Path('.'), debug=False)
