# Copyright (c) Microsoft Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import asyncio
import base64
import sys
from playwright.accessibility import Accessibility
from playwright.connection import (
    ChannelOwner,
    ConnectionScope,
    from_channel,
    from_nullable_channel,
)
from playwright.element_handle import ElementHandle, ValuesToSelect
from playwright.file_chooser import FileChooser
from playwright.helper import locals_to_params
from playwright.input import Keyboard, Mouse
from playwright.js_handle import JSHandle
from playwright.frame import Frame
from playwright.helper import (
    is_function_body,
    parse_error,
    serialize_error,
    Error,
    FilePayload,
    FunctionWithSource,
    Optional,
    PendingWaitEvent,
    RouteHandler,
    RouteHandlerEntry,
    TimeoutSettings,
    TimeoutError,
    URLMatch,
    URLMatcher,
    MouseButton,
    KeyboardModifier,
    DocumentLoadState,
    ColorScheme,
    Viewport,
)
from playwright.network import Request, Response, Route
from playwright.worker import Worker
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Dict, List, Union, TYPE_CHECKING, cast

if sys.version_info >= (3, 8):  # pragma: no cover
    from typing import Literal
else:  # pragma: no cover
    from typing_extensions import Literal

if TYPE_CHECKING:  # pragma: no cover
    from playwright.browser_context import BrowserContext


class Page(ChannelOwner):

    Events = SimpleNamespace(
        Close="close",
        Crash="crash",
        Console="console",
        Dialog="dialog",
        Download="download",
        FileChooser="filechooser",
        DOMContentLoaded="domcontentloaded",
        PageError="pageerror",
        Request="request",
        Response="response",
        RequestFailed="requestfailed",
        RequestFinished="requestfinished",
        FrameAttached="frameattached",
        FrameDetached="framedetached",
        FrameNavigated="framenavigated",
        Load="load",
        Popup="popup",
        Worker="worker",
    )

    def __init__(self, scope: ConnectionScope, guid: str, initializer: Dict) -> None:
        super().__init__(scope, guid, initializer)
        self.accessibility = Accessibility(self._channel)
        self.keyboard = Keyboard(self._channel)
        self.mouse = Mouse(self._channel)

        self._main_frame: Frame = from_channel(initializer["mainFrame"])
        self._main_frame._page = self
        self._frames = [self._main_frame]
        self._viewport_size = initializer["viewportSize"]
        self._is_closed = False
        self._workers: List[Worker] = list()
        self._bindings: Dict[str, Any] = dict()
        self._pending_wait_for_events: List[PendingWaitEvent] = list()
        self._routes: List[RouteHandlerEntry] = list()
        self._owned_context: Optional["BrowserContext"] = None

        self._channel.on(
            "bindingCall",
            lambda binding_call: self._on_binding(from_channel(binding_call)),
        )
        self._channel.on("close", lambda _: self._on_close())
        self._channel.on(
            "console",
            lambda message: self.emit(Page.Events.Console, from_channel(message)),
        )
        self._channel.on("crash", lambda _: self._on_crash())
        self._channel.on(
            "dialog", lambda dialog: self.emit(Page.Events.Dialog, from_channel(dialog))
        )
        self._channel.on(
            "domcontentloaded", lambda _: self.emit(Page.Events.DOMContentLoaded)
        )
        self._channel.on(
            "download",
            lambda download: self.emit(Page.Events.Download, from_channel(download)),
        )
        self._channel.on(
            "fileChooser",
            lambda params: self.emit(
                Page.Events.FileChooser,
                FileChooser(
                    self, from_channel(params["element"]), params["isMultiple"]
                ),
            ),
        )
        self._channel.on(
            "frameAttached", lambda frame: self._on_frame_attached(from_channel(frame))
        )
        self._channel.on(
            "frameDetached", lambda frame: self._on_frame_detached(from_channel(frame))
        )
        self._channel.on(
            "frameNavigated",
            lambda params: self._on_frame_navigated(
                from_channel(params["frame"]), params["url"], params["name"]
            ),
        )
        self._channel.on("load", lambda _: self.emit(Page.Events.Load))
        self._channel.on(
            "pageError",
            lambda params: self.emit(
                Page.Events.PageError, parse_error(params["error"])
            ),
        )
        self._channel.on(
            "popup", lambda popup: self.emit(Page.Events.Popup, from_channel(popup))
        )
        self._channel.on(
            "request",
            lambda request: self.emit(Page.Events.Request, from_channel(request)),
        )
        self._channel.on(
            "requestFailed",
            lambda params: self._on_request_failed(
                from_channel(params["request"]), params["failureText"]
            ),
        )
        self._channel.on(
            "requestFinished",
            lambda request: self.emit(
                Page.Events.RequestFinished, from_channel(request)
            ),
        )
        self._channel.on(
            "response",
            lambda response: self.emit(Page.Events.Response, from_channel(response)),
        )
        self._channel.on(
            "route",
            lambda params: self._on_route(
                from_channel(params["route"]), from_channel(params["request"])
            ),
        )
        self._channel.on("worker", lambda worker: self._on_worker(from_channel(worker)))

    def _set_browser_context(self, context: "BrowserContext") -> None:
        self._browser_context = context
        self._timeout_settings = TimeoutSettings(context._timeout_settings)

    def _on_request_failed(self, request: Request, failure_text: str = None) -> None:
        request._failure_text = failure_text
        self.emit(Page.Events.RequestFailed, request)

    def _on_frame_attached(self, frame: Frame) -> None:
        frame._page = self
        self._frames.append(frame)
        self.emit(Page.Events.FrameAttached, frame)

    def _on_frame_detached(self, frame: Frame) -> None:
        self._frames.remove(frame)
        frame._detached = True
        self.emit(Page.Events.FrameDetached, frame)

    def _on_frame_navigated(self, frame: Frame, url: str, name: str) -> None:
        frame._url = url
        frame._name = name
        self.emit(Page.Events.FrameNavigated, frame)

    def _on_route(self, route: Route, request: Request) -> None:
        for handler_entry in self._routes:
            if handler_entry.matcher.matches(request.url):
                handler_entry.handler(route, request)
                return
        self._browser_context._on_route(route, request)

    def _on_binding(self, binding_call: "BindingCall") -> None:
        func = self._bindings.get(binding_call._initializer["name"])
        if func:
            asyncio.ensure_future(binding_call.call(func))
        self._browser_context._on_binding(binding_call)

    def _on_worker(self, worker: Worker) -> None:
        self._workers.append(worker)
        worker._page = self
        self.emit(Page.Events.Worker, worker)

    def _on_close(self) -> None:
        self._is_closed = True
        self._browser_context._pages.remove(self)
        self._reject_pending_operations(False)
        self.emit(Page.Events.Close)

    def _on_crash(self) -> None:
        self._reject_pending_operations(True)
        self.emit(Page.Events.Crash)

    def _reject_pending_operations(self, is_crash: bool) -> None:
        for pending_event in self._pending_wait_for_events:
            pending_event.reject(is_crash, "Page")

    @property
    def context(self) -> "BrowserContext":
        return self._browser_context

    async def opener(self) -> Optional["Page"]:
        return from_nullable_channel(await self._channel.send("opener"))

    @property
    def mainFrame(self) -> Frame:
        return self._main_frame

    def frame(self, name: str = None, url: URLMatch = None) -> Optional[Frame]:
        matcher = URLMatcher(url) if url else None
        for frame in self._frames:
            if name and frame.name == name:
                return frame
            if url and matcher and matcher.matches(frame.url):
                return frame
        return None

    @property
    def frames(self) -> List[Frame]:
        return self._frames.copy()

    def setDefaultNavigationTimeout(self, timeout: int) -> None:
        self._timeout_settings.set_navigation_timeout(timeout)
        asyncio.ensure_future(
            self._channel.send(
                "setDefaultNavigationTimeoutNoReply", dict(timeout=timeout)
            )
        )

    def setDefaultTimeout(self, timeout: int) -> None:
        self._timeout_settings.set_timeout(timeout)
        asyncio.ensure_future(
            self._channel.send("setDefaultTimeoutNoReply", dict(timeout=timeout))
        )

    async def querySelector(self, selector: str) -> Optional[ElementHandle]:
        return await self._main_frame.querySelector(selector)

    async def querySelectorAll(self, selector: str) -> List[ElementHandle]:
        return await self._main_frame.querySelectorAll(selector)

    async def waitForSelector(
        self,
        selector: str,
        timeout: int = None,
        state: Literal["attached", "detached", "visible", "hidden"] = None,
    ) -> Optional[ElementHandle]:
        return await self._main_frame.waitForSelector(**locals_to_params(locals()))

    async def dispatchEvent(
        self, selector: str, type: str, eventInit: Dict = None, timeout: int = None
    ) -> None:
        return await self._main_frame.dispatchEvent(**locals_to_params(locals()))

    async def evaluate(
        self, expression: str, arg: Any = None, force_expr: bool = False
    ) -> Any:
        return await self._main_frame.evaluate(expression, arg, force_expr=force_expr)

    async def evaluateHandle(
        self, expression: str, arg: Any = None, force_expr: bool = False
    ) -> JSHandle:
        return await self._main_frame.evaluateHandle(
            expression, arg, force_expr=force_expr
        )

    async def evalOnSelector(
        self, selector: str, expression: str, arg: Any = None, force_expr: bool = False
    ) -> Any:
        return await self._main_frame.evalOnSelector(
            selector, expression, arg, force_expr=force_expr
        )

    async def evalOnSelectorAll(
        self, selector: str, expression: str, arg: Any = None, force_expr: bool = False
    ) -> Any:
        return await self._main_frame.evalOnSelectorAll(
            selector, expression, arg, force_expr=force_expr
        )

    async def addScriptTag(
        self, url: str = None, path: str = None, content: str = None, type: str = None
    ) -> ElementHandle:
        return await self._main_frame.addScriptTag(**locals_to_params(locals()))

    async def addStyleTag(
        self, url: str = None, path: str = None, content: str = None
    ) -> ElementHandle:
        return await self._main_frame.addStyleTag(**locals_to_params(locals()))

    async def exposeFunction(self, name: str, binding: Callable[..., Any]) -> None:
        await self.exposeBinding(name, lambda source, *args: binding(*args))

    async def exposeBinding(self, name: str, binding: FunctionWithSource) -> None:
        if name in self._bindings:
            raise Error(f'Function "{name}" has been already registered')
        if name in self._browser_context._bindings:
            raise Error(
                f'Function "{name}" has been already registered in the browser context'
            )
        self._bindings[name] = binding
        await self._channel.send("exposeBinding", dict(name=name))

    async def setExtraHTTPHeaders(self, headers: Dict) -> None:
        await self._channel.send("setExtraHTTPHeaders", dict(headers=headers))

    @property
    def url(self) -> str:
        return self._main_frame.url

    async def content(self) -> str:
        return await self._main_frame.content()

    async def setContent(
        self, html: str, timeout: int = None, waitUntil: DocumentLoadState = None,
    ) -> None:
        return await self._main_frame.setContent(**locals_to_params(locals()))

    async def goto(
        self,
        url: str,
        timeout: int = None,
        waitUntil: DocumentLoadState = None,
        referer: str = None,
    ) -> Optional[Response]:
        return await self._main_frame.goto(**locals_to_params(locals()))

    async def reload(
        self, timeout: int = None, waitUntil: DocumentLoadState = None,
    ) -> Optional[Response]:
        return from_nullable_channel(
            await self._channel.send("reload", locals_to_params(locals()))
        )

    async def waitForLoadState(self, state: str = "load", timeout: int = None) -> None:
        return await self._main_frame.waitForLoadState(**locals_to_params(locals()))

    async def waitForNavigation(
        self,
        timeout: int = None,
        waitUntil: DocumentLoadState = None,
        url: str = None,  # TODO: add url, callback
    ) -> Optional[Response]:
        return await self._main_frame.waitForNavigation(**locals_to_params(locals()))

    async def waitForRequest(
        self,
        url: URLMatch = None,
        predicate: Callable[[Request], bool] = None,
        timeout: int = None,
    ) -> Optional[Request]:
        matcher = URLMatcher(url) if url else None

        def my_predicate(request: Request) -> bool:
            if matcher:
                return matcher.matches(request.url)
            if predicate:
                return predicate(request)
            return True

        return cast(
            Optional[Request],
            await self.waitForEvent(
                Page.Events.Request, predicate=my_predicate, timeout=timeout
            ),
        )

    async def waitForResponse(
        self,
        url: URLMatch = None,
        predicate: Callable[[Response], bool] = None,
        timeout: int = None,
    ) -> Optional[Response]:
        matcher = URLMatcher(url) if url else None

        def my_predicate(response: Response) -> bool:
            if matcher:
                return matcher.matches(response.url)
            if predicate:
                return predicate(response)
            return True

        return cast(
            Optional[Response],
            await self.waitForEvent(
                Page.Events.Response, predicate=my_predicate, timeout=timeout
            ),
        )

    async def waitForEvent(
        self, event: str, predicate: Callable[[Any], bool] = None, timeout: int = None
    ) -> Any:
        return await wait_for_event(
            self, self._timeout_settings, event, predicate=predicate, timeout=timeout
        )

    async def goBack(
        self, timeout: int = None, waitUntil: DocumentLoadState = None,
    ) -> Optional[Response]:
        return from_nullable_channel(
            await self._channel.send("goBack", locals_to_params(locals()))
        )

    async def goForward(
        self, timeout: int = None, waitUntil: DocumentLoadState = None,
    ) -> Optional[Response]:
        return from_nullable_channel(
            await self._channel.send("goForward", locals_to_params(locals()))
        )

    async def emulateMedia(
        self, media: Literal["screen", "print"] = None, colorScheme: ColorScheme = None,
    ) -> None:
        await self._channel.send("emulateMedia", locals_to_params(locals()))

    async def setViewportSize(self, width: int, height: int) -> None:
        self._viewport_size = dict(width=width, height=height)
        await self._channel.send(
            "setViewportSize", dict(viewportSize=locals_to_params(locals()))
        )

    def viewportSize(self) -> Optional[Viewport]:
        return self._viewport_size

    async def addInitScript(self, source: str = None, path: str = None) -> None:
        if path:
            with open(path, "r") as file:
                source = file.read()
        if not isinstance(source, str):
            raise Error("Either path or source parameter must be specified")
        await self._channel.send("addInitScript", dict(source=source))

    async def route(self, match: URLMatch, handler: RouteHandler) -> None:
        self._routes.append(RouteHandlerEntry(URLMatcher(match), handler))
        if len(self._routes) == 1:
            await self._channel.send(
                "setNetworkInterceptionEnabled", dict(enabled=True)
            )

    async def unroute(
        self, match: URLMatch, handler: Optional[RouteHandler] = None
    ) -> None:
        self._routes = list(
            filter(
                lambda r: r.matcher.match != match
                or (handler and r.handler != handler),
                self._routes,
            )
        )
        if len(self._routes) == 0:
            await self._channel.send(
                "setNetworkInterceptionEnabled", dict(enabled=False)
            )

    async def screenshot(
        self,
        timeout: int = None,
        type: Literal["png", "jpeg"] = None,
        path: str = None,
        quality: int = None,
        omitBackground: bool = None,
        fullPage: bool = None,
        clip: Dict = None,
    ) -> bytes:
        binary = await self._channel.send("screenshot", locals_to_params(locals()))
        return base64.b64decode(binary)

    async def title(self) -> str:
        return await self._main_frame.title()

    async def close(self, runBeforeUnload: bool = None) -> None:
        await self._channel.send("close", locals_to_params(locals()))
        if self._owned_context:
            await self._owned_context.close()

    def isClosed(self) -> bool:
        return self._is_closed

    async def click(
        self,
        selector: str,
        modifiers: KeyboardModifier = None,
        position: Dict = None,
        delay: int = None,
        button: MouseButton = None,
        clickCount: int = None,
        timeout: int = None,
        force: bool = None,
        noWaitAfter: bool = None,
    ) -> None:
        return await self._main_frame.click(**locals_to_params(locals()))

    async def dblclick(
        self,
        selector: str,
        modifiers: List[KeyboardModifier] = None,
        position: Dict = None,
        delay: int = None,
        button: MouseButton = None,
        timeout: int = None,
        force: bool = None,
    ) -> None:
        return await self._main_frame.dblclick(**locals_to_params(locals()))

    async def fill(
        self, selector: str, value: str, timeout: int = None, noWaitAfter: bool = None
    ) -> None:
        return await self._main_frame.fill(**locals_to_params(locals()))

    async def focus(self, selector: str, timeout: int = None) -> None:
        return await self._main_frame.focus(**locals_to_params(locals()))

    async def textContent(self, selector: str, timeout: int = None) -> str:
        return await self._main_frame.textContent(**locals_to_params(locals()))

    async def innerText(self, selector: str, timeout: int = None) -> str:
        return await self._main_frame.innerText(**locals_to_params(locals()))

    async def innerHTML(self, selector: str, timeout: int = None) -> str:
        return await self._main_frame.innerHTML(**locals_to_params(locals()))

    async def getAttribute(self, selector: str, name: str, timeout: int = None) -> str:
        return await self._main_frame.getAttribute(**locals_to_params(locals()))

    async def hover(
        self,
        selector: str,
        modifiers: List[KeyboardModifier] = None,
        position: Dict = None,
        timeout: int = None,
        force: bool = None,
    ) -> None:
        return await self._main_frame.hover(**locals_to_params(locals()))

    async def selectOption(
        self,
        selector: str,
        values: ValuesToSelect,
        timeout: int = None,
        noWaitAfter: bool = None,
    ) -> None:
        params = locals_to_params(locals())
        if "values" not in params:
            params["values"] = None
        return await self._main_frame.selectOption(**params)

    async def setInputFiles(
        self,
        selector: str,
        files: Union[str, FilePayload, List[str], List[FilePayload]],
        timeout: int = None,
        noWaitAfter: bool = None,
    ) -> None:
        return await self._main_frame.setInputFiles(**locals_to_params(locals()))

    async def type(
        self,
        selector: str,
        text: str,
        delay: int = None,
        timeout: int = None,
        noWaitAfter: bool = None,
    ) -> None:
        return await self._main_frame.type(**locals_to_params(locals()))

    async def press(
        self,
        selector: str,
        key: str,
        delay: int = None,
        timeout: int = None,
        noWaitAfter: bool = None,
    ) -> None:
        return await self._main_frame.press(**locals_to_params(locals()))

    async def check(
        self,
        selector: str,
        timeout: int = None,
        force: bool = None,
        noWaitAfter: bool = None,
    ) -> None:
        return await self._main_frame.check(**locals_to_params(locals()))

    async def uncheck(
        self,
        selector: str,
        timeout: int = None,
        force: bool = None,
        noWaitAfter: bool = None,
    ) -> None:
        return await self._main_frame.uncheck(**locals_to_params(locals()))

    async def waitForTimeout(self, timeout: int) -> Awaitable[None]:
        return await self._main_frame.waitForTimeout(timeout)

    async def waitForFunction(
        self,
        expression: str,
        arg: Any = None,
        force_expr: bool = False,
        timeout: int = None,
        polling: Union[int, Literal["raf"]] = None,
    ) -> JSHandle:
        if not is_function_body(expression):
            force_expr = True
        return await self._main_frame.waitForFunction(**locals_to_params(locals()))

    @property
    def workers(self) -> List[Worker]:
        return self._workers.copy()

    # on(event: str | symbol, listener: Listener): self {
    #   if (event === Page.Events.FileChooser) {
    #     if (!self.listenerCount(event))
    #       self._channel.setFileChooserInterceptedNoReply({ intercepted: True });
    #   }
    #   super.on(event, listener);
    #   return self;
    # }

    # removeListener(event: str | symbol, listener: Listener): self {
    #   super.removeListener(event, listener);
    #   if (event === Page.Events.FileChooser && !self.listenerCount(event))
    #     self._channel.setFileChooserInterceptedNoReply({ intercepted: False });
    #   return self;
    # }

    async def pdf(
        self,
        scale: int = None,
        displayHeaderFooter: bool = None,
        headerTemplate: str = None,
        footerTemplate: str = None,
        printBackground: bool = None,
        landscape: bool = None,
        pageRanges: str = None,
        format: str = None,
        width: Union[str, float] = None,
        height: Union[str, float] = None,
        preferCSSPageSize: bool = None,
        margin: Dict = None,
        path: str = None,
    ) -> bytes:
        binary = await self._channel.send("pdf", locals_to_params(locals()))
        return base64.b64decode(binary)


class BindingCall(ChannelOwner):
    def __init__(self, scope: ConnectionScope, guid: str, initializer: Dict) -> None:
        super().__init__(scope, guid, initializer)

    async def call(self, func: FunctionWithSource) -> None:
        try:
            frame = from_channel(self._initializer["frame"])
            source = dict(context=frame._page.context, page=frame._page, frame=frame)
            result = func(source, *self._initializer["args"])
            if isinstance(result, asyncio.Future):
                result = await result
            await self._channel.send("resolve", dict(result=result))
        except Exception as e:
            tb = sys.exc_info()[2]
            asyncio.ensure_future(
                self._channel.send("reject", dict(error=serialize_error(e, tb)))
            )


async def wait_for_event(
    target: Union[Page, "BrowserContext"],
    timeout_settings: TimeoutSettings,
    event: str,
    predicate: Callable[[Any], bool] = None,
    timeout: int = None,
) -> Any:
    if timeout is None:
        timeout = timeout_settings.timeout()
    if timeout == 0:
        timeout = 3600 * 24 * 7 * 30 * 365
    timeout_future: asyncio.Future = asyncio.ensure_future(
        asyncio.sleep(timeout / 1000)
    )

    future = target._scope._loop.create_future()

    def listener(e: Any = None) -> None:
        if not predicate or predicate(e):
            future.set_result(e)

    target.on(event, listener)

    pending_event = PendingWaitEvent(event, future, timeout_future)
    target._pending_wait_for_events.append(pending_event)
    done, _ = await asyncio.wait(
        {timeout_future, future}, return_when=asyncio.FIRST_COMPLETED
    )
    target.remove_listener(event, listener)
    target._pending_wait_for_events.remove(pending_event)
    if future in done:
        timeout_future.cancel()
        return future.result()
    raise TimeoutError("Timeout exceeded")
