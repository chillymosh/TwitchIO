"""MIT License

Copyright (c) 2017-present TwitchIO

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""
from __future__ import annotations

import asyncio
import copy
import datetime
import logging
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    ClassVar,
    Generator,
    Generic,
    Literal,
    TypeVar,
    cast,
)

import aiohttp
import multidict
from yarl import URL

from .exceptions import HTTPException, HTTPResponseException, Unauthorized
from .limiter import HTTPRateLimiter, RateLimitBucket
from .tokens import BaseToken, BaseTokenHandler
from .utils import MISSING, json_dumper, json_loader

if TYPE_CHECKING:
    from .client import Client
    from .models import PartialUser, User
    from .types.extensions import ExtensionBuilder as ExtensionBuilderType
    from .types.payloads import BasePayload

    UserType = PartialUser | User
    methodType = Literal["DELETE", "GET", "PATCH", "POST", "PUT"]
    bodyType = dict[str, Any] | str | None

TokenHandlerT = TypeVar("TokenHandlerT", bound=BaseTokenHandler)
ParameterType = list[tuple[str, str | None ]]
T = TypeVar("T")
logger = logging.getLogger("twitchio.http")


class Route:
    __slots__ = ("_url", "method", "target", "headers", "body", "scope", "ratelimit_tokens", "parameters")
    BASE_URL: ClassVar[URL] = URL("https://api.twitch.tv/helix/")

    def __init__(
        self,
        method: methodType,
        route: str,
        body: bodyType,
        ratelimit_tokens: int = 1,
        scope: list[str] | None = None,
        parameters: list[tuple[str, str | None]] | None = None,
        target: UserType | None = None,
    ) -> None:
        self._url: URL = self.BASE_URL / route.lstrip("/")
        self.method: methodType = method
        self.target: UserType | None = target
        self.headers = headers = {}
        self.scope: list[str] = scope or []
        self.ratelimit_tokens: int = ratelimit_tokens

        if isinstance(body, dict):
            body = json_dumper(body)
            headers["Content-Type"] = "application/json"

        self.body: bodyType = body
        self.parameters: multidict.MultiDict = multidict.MultiDict()

        if parameters:
            for key, value in parameters:
                if value is not None:
                    self.parameters.add(key, value)

    @property
    def url(self) -> URL:
        return self._url.with_query(self.parameters)


ChunkerType = Callable[[str | None], Awaitable["BasePayload"]]


class HTTPAwaitableAsyncIterator(Generic[T]):
    __slots__ = ("http", "chunker", "adapter", "page", "buffer")

    def __init__(
        self,
        http: HTTPHandler,
        chunker: ChunkerType,
        adapter: Callable[[HTTPHandler, dict[str, Any]], T] | None = None,
    ) -> None:
        self.http: HTTPHandler = http
        self.chunker: ChunkerType = chunker
        self.adapter: Callable[[HTTPHandler, dict[str, Any]], T] | None = adapter
        self.page: str | None = None
        self.buffer: list[dict[str, Any]] = []

    def set_adapter(self, adapter: Callable[[HTTPHandler, dict[str, Any]], T]) -> None:
        self.adapter = adapter

    async def chunk_once(self, page: str | None) -> Any:
        if page is MISSING:  # iterator exhausted
            raise StopAsyncIteration

        data = await self.chunker(page)
        pagination = data.get("pagination", {})

        self.page = pagination.get("cursor", MISSING)
        return data["data"]

    async def _await_and_discard(self) -> list[T]:
        assert (
            self.adapter is not None
        ), "Cannot paginate without an adapter. This is an internal error, please report it"

        data = await self.chunk_once(None)
        self.page = MISSING
        return [self.adapter(self.http, x) for x in data]

    def __await__(self) -> Generator[Any, None, list[T]]:
        return self._await_and_discard().__await__()

    def __aiter__(self):
        return self

    async def __anext__(self) -> T:
        assert (
            self.adapter is not None
        ), "Cannot paginate without an adapter. This is an internal error, please report it"

        if not self.buffer:
            self.buffer = await self.chunk_once(self.page)

        return self.adapter(self.http, self.buffer.pop(0))


class HTTPAwaitableAsyncIteratorWithSource(HTTPAwaitableAsyncIterator, Generic[T]):
    def __init__(self, source: list[T]) -> None:
        self.buffer: list[T] = source

    async def _await_and_discard(self) -> list[T]:
        return self.buffer

    async def __anext__(self) -> T:
        if not self.buffer:
            raise StopAsyncIteration

        return self.buffer.pop(0)


class HTTPHandler(Generic[TokenHandlerT, T]):
    def __init__(
        self,
        loop: asyncio.AbstractEventLoop | None,
        token_handler: TokenHandlerT,
        proxy: str | None = None,
        proxy_auth: aiohttp.BasicAuth | None = None,
        trace: aiohttp.TraceConfig | None = None,
        client: Client | None = None,
        **_,
    ) -> None:
        self.loop: asyncio.AbstractEventLoop | None = loop
        self.token_handler: TokenHandlerT = token_handler

        self.proxy: str | None = proxy
        self.proxy_auth: aiohttp.BasicAuth | None = proxy_auth
        self.trace: aiohttp.TraceConfig | None = trace
        self._session: aiohttp.ClientSession | None = None

        self.buckets = HTTPRateLimiter()
        self.client: Client | None = client

    async def prepare(self):
        if not self.loop:
            self.loop = asyncio.get_running_loop()

        if self._session:
            await self._session.close()

        self._session = aiohttp.ClientSession(json_serialize=json_dumper, trace_configs=self.trace and [self.trace])

    async def cleanup(self) -> None:
        if self._session:
            await self._session.close()

    async def request_paginated_endpoint(
        self, route: Route, paginator: str| None  = None, limit: int| None  = 100
    ):
        copy_route = copy.copy(route)
        if "limit" not in route.parameters:
            copy_route.parameters.add("limit", str(limit))

        if paginator:
            copy_route.parameters.add("after", paginator)

        return await self.request(copy_route)

    async def _get_token_from_route(self, route: Route, *, no_cache: bool = False) -> BaseToken:
        token: BaseToken
        if route.target:
            logger.debug("Fetching user token for %s with scope %s")
            token = await self.token_handler._client_get_user_token(self, route.target, route.scope, no_cache=no_cache)

        elif route.scope:
            raise ValueError(f"endpoint {route.url.path} requires scope {route.scope} but no target is present")

        else:
            token = await self.token_handler._client_get_client_token()

        return token

    async def request(self, route: Route) -> Any:
        token: BaseToken = await self._get_token_from_route(route)
        client_id, _ = await self.token_handler.get_client_credentials()
        is_fresh_token = False

        raw_token = await token.get(self, self.token_handler, cast(aiohttp.ClientSession, self._session))
        headers = {"Authorization": f"Bearer {raw_token}", "Client-ID": client_id}
        bucket: RateLimitBucket = self.buckets.get_bucket(route.target)

        await bucket.acquire()
        await bucket.wait(route.ratelimit_tokens)

        if not self._session:
            await self.prepare()

        url = route.url
        try:
            for attempt in range(5):
                logger.debug("Sending request to %s %s with payload %s", route.method, url, route.body)
                async with cast(aiohttp.ClientSession, self._session).request(
                    route.method, url, headers=headers
                ) as response:
                    _data: str = await response.text("utf-8")
                    data: str | BasePayload
                    if response.headers["Content-Type"].startswith("application/json"):
                        data = json_loader(_data)
                    else:
                        data = _data

                    if response.status not in (400, 401):
                        bucket.update(response)

                    if 200 <= response.status <= 299:
                        logger.debug(
                            "Received success response from %s %s with response payload %s", route.method, url, data
                        )
                        await bucket.release()
                        return data

                    elif response.status == 429:
                        logger.info("We are being ratelimited on route %s. Handled under bucket %s", url, bucket.name)
                        await bucket.wait(route.ratelimit_tokens)
                        continue

                    elif response.status == 401:
                        logger.info("Received Unauthorized response from %s %s . Attempting to get a fresh token")
                        if not is_fresh_token:
                            token = await self._get_token_from_route(route, no_cache=True)
                            raw_token = await token.get(
                                self, self.token_handler, cast(aiohttp.ClientSession, self._session)
                            )
                            is_fresh_token = True
                            headers["Authorization"] = f"Bearer {raw_token}"
                            continue

                        await bucket.release()
                        raise Unauthorized(response, data)  # type: ignore

                    else:
                        logger.debug(
                            "Received unknown response code %i from route %s %s",
                            response.status,
                            route.method,
                            url,
                            route.body,
                        )
                        await bucket.release()
                        raise HTTPResponseException(response, data)  # type: ignore

        except aiohttp.ClientError as err:
            raise HTTPException("Unable to reach the twitch API") from err

        finally:
            if bucket.lock.locked():
                await bucket.release()  # failsafe

        logger.error("Aborting request to route %s after 5 failed attempts", url)
        raise HTTPException("Unable to reach the twitch API")

    def request_paginated_route(self, route: Route) -> HTTPAwaitableAsyncIterator[TokenHandlerT]:
        iterator = HTTPAwaitableAsyncIterator(self, lambda page: self.request_paginated_endpoint(route, page))
        return iterator

    def dummy(self):
        return self.request(Route("GET", "/users", None, parameters=[("login", "iamtomahawkx")]))

    async def post_commercial(
        self, target: PartialUser, broadcaster_id: str, length: Literal[30, 60, 90, 120, 150, 180]
    ) -> dict:
        assert length in {30, 60, 90, 120, 150, 180}
        data = await self.request(
            Route(
                "POST",
                "channels/commercial",
                body={"broadcaster_id": broadcaster_id, "length": length},
                target=target,
                scope=["channel:edit:commercial"],
            )
        )
        data = data["data"][0]
        if data["message"]:
            raise HTTPException(data["message"])

        return data

    async def get_extension_analytics(
        self,
        target: PartialUser,
        extension_id: str| None  = None,
        type: str | None = None,
        started_at: datetime.datetime | None = None,
        ended_at: datetime.datetime | None = None,
    ):
        raise NotImplementedError  # TODO

    async def get_game_analytics(
        self,
        target: PartialUser,
        game_id: str | None  = None,
        type: str | None  = None,
        started_at: datetime.datetime | None  = None,
        ended_at: datetime.datetime | None  = None,
    ):
        raise NotImplementedError  # TODO

    async def get_bits_board(
        self,
        target: PartialUser,
        period: Literal["all", "day", "week", "month", "year"] = "all",
        user_id: int | None  = None,
        started_at: datetime.datetime | None  = None,
    ) -> Any:
        assert period in {"all", "day", "week", "month", "year"}
        parameters: ParameterType = [
            ("period", period),
            ("started_at", started_at.isoformat() if started_at else None),
            ("user_id", str(user_id) if user_id else None),
        ]

        route = Route("GET", "bits/leaderboard", "", parameters=parameters, target=target, scope=["bits:read"])
        return await self.request(route)

    def get_cheermotes(self, broadcaster_id: str | None  = None, target: PartialUser | None = None):
        return self.request(
            Route("GET", "bits/cheermotes", None, parameters=[("broadcaster_id", broadcaster_id)], target=target)
        )

    def get_extension_transactions(
        self, extension_id: str, ids: list[Any] | None  = None
    ) -> HTTPAwaitableAsyncIterator:
        params: ParameterType = [("extension_id", extension_id)]
        if ids:
            params.extend(("id", str(id)) for id in ids)
        return self.request_paginated_route(Route("GET", "extensions/transactions", None, parameters=params))

    def create_reward(
        self,
        target: PartialUser,
        broadcaster_id: int,
        title: str,
        cost: int,
        prompt: str | None  = None,
        is_enabled: bool = True,
        background_color: str | None  = None,
        user_input_required: bool = False,
        max_per_stream: int | None  = None,
        max_per_user: int | None  = None,
        global_cooldown: int | None  = None,
        fufill_immediatly: bool = False,
    ) -> Any:
        params: ParameterType = [("broadcaster_id", str(broadcaster_id))]
        data = {
            "title": title,
            "cost": cost,
            "prompt": prompt,
            "is_enabled": is_enabled,
            "is_user_input_required": user_input_required,
            "should_redemptions_skip_request_queue": fufill_immediatly,
        }
        if max_per_stream:
            data["max_per_stream"] = max_per_stream
            data["max_per_stream_enabled"] = True

        if max_per_user:
            data["max_per_user_per_stream"] = max_per_user
            data["max_per_user_per_stream_enabled"] = True

        if background_color:
            data["background_color"] = background_color

        if global_cooldown:
            data["global_cooldown_seconds"] = global_cooldown
            data["is_global_cooldown_enabled"] = True

        return self.request(Route("POST", "channel_points/custom_rewards", parameters=params, body=data, target=target))

    def get_rewards(
        self, target: PartialUser, broadcaster_id: int, only_manageable: bool = False, ids: list[int] | None  = None
    ) -> HTTPAwaitableAsyncIterator:
        params = [("broadcaster_id", str(broadcaster_id)), ("only_manageable_rewards", str(only_manageable))]

        if ids:
            params.extend(("id", str(id)) for id in ids)

        return self.request_paginated_route(
            Route("GET", "channel_points/custom_rewards", None, parameters=params, target=target)
        )

    def update_reward(
        self,
        target: PartialUser,
        broadcaster_id: int,
        reward_id: str,
        title: str | None = None,
        prompt: str | None  = None,
        cost: int | None  = None,
        background_color: str | None  = None,
        enabled: bool | None = None,
        input_required: bool | None = None,
        max_per_stream_enabled: bool | None = None,
        max_per_stream: int| None = None,
        max_per_user_per_stream_enabled: bool | None = None,
        max_per_user_per_stream: int| None = None,
        global_cooldown_enabled: bool | None = None,
        global_cooldown: int| None = None,
        paused: bool | None = None,
        redemptions_skip_queue: bool | None = None,
    ) -> Any:
        data = {
            "title": title,
            "prompt": prompt,
            "cost": cost,
            "background_color": background_color,
            "enabled": enabled,
            "is_user_input_required": input_required,
            "is_max_per_stream_enabled": max_per_stream_enabled,
            "max_per_stream": max_per_stream,
            "is_max_per_user_per_stream_enabled": max_per_user_per_stream_enabled,
            "max_per_user_per_stream": max_per_user_per_stream,
            "is_global_cooldown_enabled": global_cooldown_enabled,
            "global_cooldown_seconds": global_cooldown,
            "is_paused": paused,
            "should_redemptions_skip_request_queue": redemptions_skip_queue,
        }

        data = {k: v for k, v in data.items() if v is not None}

        assert data, "Nothing has been changed!"

        params = [("broadcaster_id", str(broadcaster_id)), ("id", str(reward_id))]
        return self.request(
            Route("PATCH", "channel_points/custom_rewards", parameters=params, body=data, target=target)
        )

    def delete_custom_reward(self, target: PartialUser, broadcaster_id: int, reward_id: str) -> Any:
        params = [("broadcaster_id", str(broadcaster_id)), ("id", reward_id)]
        return self.request(Route("DELETE", "channel_points/custom_rewards", None, parameters=params, target=target))

    def get_reward_redemptions(
        self,
        target: PartialUser,
        broadcaster_id: int,
        reward_id: str,
        redemption_id: str | None = None,
        status: str | None = None,
        sort: str | None = None,
    ) -> HTTPAwaitableAsyncIterator:
        params = [("broadcaster_id", str(broadcaster_id)), ("reward_id", reward_id)]

        if redemption_id:
            params.append(("id", redemption_id))
        if status:
            params.append(("status", status))
        if sort:
            params.append(("sort", sort))

        return self.request_paginated_route(
            Route("GET", "channel_points/custom_rewards/redemptions", None, parameters=params, target=target)
        )

    def update_reward_redemption_status(
        self, target: PartialUser, broadcaster_id: int, reward_id: str, custom_reward_id: str, status: bool
    ) -> Any:
        params = [("id", custom_reward_id), ("broadcaster_id", str(broadcaster_id)), ("reward_id", reward_id)]
        raw_status = "FULFILLED" if status else "CANCELLED"
        return self.request(
            Route(
                "PATCH",
                "/channel_points/custom_rewards/redemptions",
                parameters=params,
                body={"status": raw_status},
                target=target,
            )
        )

    def get_predictions(
        self,
        target: PartialUser,
        broadcaster_id: int,
        prediction_id: str | None = None,
    ) -> HTTPAwaitableAsyncIterator:
        params: ParameterType = [("broadcaster_id", str(broadcaster_id))]

        if prediction_id:
            params.append(("prediction_id", prediction_id))

        return self.request_paginated_route(
            Route("GET", "predictions", None, parameters=params, target=target, scope=["channel:manage:predictions"])
        )

    def patch_prediction(
        self,
        target: PartialUser,
        broadcaster_id: str,
        prediction_id: str,
        status: str,
        winning_outcome_id: str | None = None,
    ) -> Any:
        body = {
            "broadcaster_id": broadcaster_id,
            "id": prediction_id,
            "status": status,
        }

        if status == "RESOLVED":
            body["winning_outcome_id"] = cast(str, winning_outcome_id)

        return self.request(
            Route("PATCH", "predictions", body=body, target=target, scope=["channel:manage:prediction"])
        )

    def post_prediction(
        self,
        target: PartialUser,
        broadcaster_id: int,
        title: str,
        blue_outcome: str,
        pink_outcome: str,
        prediction_window: int,
    ) -> Any:
        body = {
            "broadcaster_id": broadcaster_id,
            "title": title,
            "prediction_window": prediction_window,
            "outcomes": [
                {
                    "title": blue_outcome,
                },
                {
                    "title": pink_outcome,
                },
            ],
        }
        return self.request(
            Route("POST", "predictions", body=body, target=target, scope=["channel:manage:predictions"])
        )

    def post_create_clip(self, target: PartialUser, broadcaster_id: int, has_delay=False) -> Any:
        return self.request(
            Route(
                "POST",
                "clips",
                None,
                parameters=[("broadcaster_id", str(broadcaster_id)), ("has_delay", str(has_delay).lower())],
                target=target,
                scope=["clips:edit"],
            )
        )

    def get_clips(
        self,
        broadcaster_id: int| None = None,
        game_id: str | None = None,
        ids: list[str] | None = None,
        started_at: datetime.datetime | None = None,
        ended_at: datetime.datetime | None = None,
        target: PartialUser | None = None,
    ) -> HTTPAwaitableAsyncIterator:
        params = [
            ("broadcaster_id", broadcaster_id),
            ("game_id", game_id),
            ("started_at", started_at.isoformat() if started_at else None),
            ("ended_at", ended_at.isoformat() if ended_at else None),
        ]
        if ids:
            params.extend(("id", id) for id in ids)

        query = [x for x in params if x[1] is not None]

        return self.request_paginated_route(Route("GET", "clips", None, parameters=query, target=target))

    def post_entitlements_upload(self, manifest_id: str, type="bulk_drops_grant") -> Any:
        return self.request(
            Route("POST", "entitlements/upload", None, parameters=[("manifest_id", manifest_id), ("type", type)])
        )

    def get_entitlements(
        self, id: str | None = None, user_id: str | None = None, game_id: str | None = None
    ) -> HTTPAwaitableAsyncIterator:
        return self.request_paginated_route(
            Route(
                "GET", "entitlements/drops", None, parameters=[("id", id), ("user_id", user_id), ("game_id", game_id)]
            )
        )

    def get_code_status(self, codes: list[str], user_id: int) -> Any:
        params: ParameterType = [("user_id", str(user_id))]
        params.extend(("code", code) for code in codes)

        return self.request(Route("GET", "entitlements/codes", None, parameters=params))

    def post_redeem_code(self, user_id: int, codes: list[str]) -> Any:
        params: ParameterType = [("user_id", str(user_id))]
        params.extend(("code", c) for c in codes)

        return self.request(Route("POST", "entitlements/code", None, parameters=params))

    def get_top_games(self, target: PartialUser | None = None) -> HTTPAwaitableAsyncIterator:
        return self.request_paginated_route(Route("GET", "games/top", None, target=target))

    def get_games(
        self, game_ids: list[int] | None, game_names: list[str] | None, target: PartialUser | None = None
    ) -> HTTPAwaitableAsyncIterator:
        if not game_ids or not game_names:
            raise ValueError("At least one of game_ids and game_names must be provided")
        params = []
        params.extend(("id", id) for id in game_ids)
        params.extend(("name", name) for name in game_names)
        return self.request_paginated_route(Route("GET", "games", None, parameters=params, target=target))

    def get_hype_train(
        self, broadcaster_id: str, id: str | None = None, target: PartialUser | None = None
    ) -> HTTPAwaitableAsyncIterator:
        return self.request_paginated_route(
            Route(
                "GET",
                "hypetrain/events",
                None,
                parameters=[x for x in [("broadcaster_id", broadcaster_id), ("id", id)] if x[1] is not None],
                target=target,
            )
        )

    def post_automod_check(
        self, target: PartialUser, broadcaster_id: str, msgs: list[dict[str, str | int]]
    ) -> Any:
        return self.request(
            Route(
                "POST",
                "moderation/enforcements/status",
                parameters=[("broadcaster_id", broadcaster_id)],
                body={"data": msgs},
                target=target,
            )
        )

    def get_channel_ban_unban_events(
        self, target: PartialUser, broadcaster_id: str, user_ids: list[int] | None = None
    ) -> HTTPAwaitableAsyncIterator:
        params: ParameterType = [("broadcaster_id", broadcaster_id)]
        if user_ids:
            params.extend(("user_id", str(id)) for id in user_ids)

        return self.request_paginated_route(
            Route("GET", "moderation/banned/events", None, parameters=params, target=target, scope=["moderation:read"])
        )

    def get_channel_bans(
        self, target: PartialUser, broadcaster_id: str, user_ids: list[str | int] | None = None
    ) -> HTTPAwaitableAsyncIterator:
        params: ParameterType = [("broadcaster_id", broadcaster_id)]
        if user_ids:
            params.extend(("user_id", str(id)) for id in user_ids)

        return self.request_paginated_route(
            Route("GET", "moderation/banned", None, parameters=params, target=target, scope=["moderation:read"])
        )

    def get_channel_moderators(
        self, target: PartialUser, broadcaster_id: str, user_ids: list[int] | None = None
    ) -> HTTPAwaitableAsyncIterator:
        params: ParameterType = [("broadcaster_id", broadcaster_id)]
        if user_ids:
            params.extend(("user_id", str(id)) for id in user_ids)

        return self.request_paginated_route(
            Route("GET", "moderation/moderators", None, parameters=params, target=target, scope=["moderation:read"])
        )

    def get_channel_mod_events(
        self, target: PartialUser, broadcaster_id: str, user_ids: list[str] | None = None
    ) -> HTTPAwaitableAsyncIterator:
        params: ParameterType = [("broadcaster_id", broadcaster_id)]
        if user_ids:
            params.extend(("user_id", id) for id in user_ids)

        return self.request_paginated_route(
            Route("GET", "moderation/moderators/events", None, parameters=params, target=target, scope=["moderation:read"])
        )

    def get_search_categories(self, query: str, target: PartialUser | None = None) -> HTTPAwaitableAsyncIterator:
        return self.request_paginated_route(
            Route("GET", "search/categories", None, parameters=[("query", query)], target=target)
        )

    def get_search_channels(
        self, query: str, live: bool = False, target: PartialUser | None = None
    ) -> HTTPAwaitableAsyncIterator:
        return self.request_paginated_route(
            Route(
                "GET", "search/channels", None, parameters=[("query", query), ("live_only", str(live))], target=target
            )
        )

    def get_stream_key(self, target: PartialUser, broadcaster_id: str) -> Any:
        return self.request(
            Route(
                "GET",
                "streams/key",
                None,
                parameters=[("broadcaster_id", broadcaster_id)],
                scope=["channel:read:stream_key"],
                target=target,
            )
        )

    def get_streams(
        self,
        game_ids: list[int] | None = None,
        user_ids: list[int] | None = None,
        user_logins: list[str] | None = None,
        languages: list[str] | None = None,
        target: PartialUser | None = None,
    ) -> HTTPAwaitableAsyncIterator:
        params = []
        if game_ids:
            params.extend(("game_id", g) for g in game_ids)

        if user_ids:
            params.extend(("user_id", u) for u in user_ids)

        if user_logins:
            params.extend(("user_login", l) for l in user_logins)

        if languages:
            params.extend(("language", l) for l in languages)

        return self.request_paginated_route(Route("GET", "streams", None, parameters=params, target=target))

    def post_stream_marker(self, target: PartialUser, user_id: str, description: str | None = None) -> Any:
        return self.request(
            Route(
                "POST",
                "streams/markers",
                body={"user_id": user_id, "description": description},
                scope=["user:edit:broadcast"],
                target=target,
            )
        )

    def get_stream_markers(
        self, target: PartialUser, user_id: str | None = None, video_id: str | None = None
    ) -> Any:
        return self.request(
            Route(
                "GET",
                "streams/markers",
                None,
                parameters=[x for x in [("user_id", user_id), ("video_id", video_id)] if x[1] is not None],
                scope=["user:edit:broadcast"],
                target=target,
            )
        )

    def get_channels(self, broadcaster_ids: list[int], target: PartialUser | None = None):
        if len(broadcaster_ids) > 100 or not broadcaster_ids:
            raise ValueError("You must specify between a minimum of 1 and a maximum of 100 IDs")
        return self.request(
            Route(
                "GET",
                "channels",
                None,
                parameters=[("broadcaster_id", str(channel_id)) for channel_id in broadcaster_ids],
                target=target,
            )
        )

    def patch_channel(
        self,
        target: PartialUser,
        broadcaster_id: str,
        game_id: str | None = None,
        language: str | None = None,
        title: str | None = None,
    ) -> Any:
        assert any((game_id, language, title))
        body = {
            k: v
            for k, v in {"game_id": game_id, "broadcaster_language": language, "title": title}.items()
            if v is not None
        }

        return self.request(
            Route(
                "PATCH",
                "channels",
                parameters=[("broadcaster_id", broadcaster_id)],
                body=body,
                target=target,
                scope=["channel:manage:broadcast"],
            )
        )

    def get_channel_schedule(
        self,
        broadcaster_id: str,
        segment_ids: list[str] | None = None,
        start_time: datetime.datetime | None = None,
        utc_offset: int| None = None,
        first: int = 20,
    ) -> Any:
        if first > 25 or first < 1:
            raise ValueError("The parameter 'first' was malformed: the value must be less than or equal to 25")
        if segment_ids is not None and len(segment_ids) > 100:
            raise ValueError("segment_id can only have 100 entries")
        if start_time:
            start_time = start_time.strftime("%Y-%m-%dT%H:%M:%SZ")  # type: ignore
        if utc_offset:
            utc_offset = str(utc_offset)  # type: ignore
        params: ParameterType = [
            x
            for x in [
                ("broadcaster_id", broadcaster_id),
                ("first", first),
                ("start_time", start_time),
                ("utc_offset", utc_offset),
            ]
            if x[1] is not None
        ]

        if segment_ids:
            params.extend(("id", id) for id in segment_ids)

        return self.request(Route("GET", "schedule", None, parameters=params))

    def get_channel_subscriptions(
        self, target: PartialUser, broadcaster_id: str, user_ids: list[str] | None = None
    ) -> HTTPAwaitableAsyncIterator:
        params: ParameterType = [("broadcaster_id", broadcaster_id)]
        if user_ids:
            params.extend(("user_id", u) for u in user_ids)

        return self.request_paginated_route(Route("GET", "subscriptions", None, parameters=params, target=target))

    def get_stream_tags(
        self, tag_ids: list[str] | None = None, target: PartialUser | None = None
    ) -> HTTPAwaitableAsyncIterator:
        params = []
        if tag_ids:
            params.extend(("tag_id", u) for u in tag_ids)

        return self.request_paginated_route(Route("GET", "tags/streams", None, parameters=params or None, target=target))

    def get_channel_tags(self, broadcaster_id: str):
        return self.request(Route("GET", "streams/tags", None, parameters=[("broadcaster_id", broadcaster_id)]))

    def put_replace_channel_tags(
        self, target: PartialUser, broadcaster_id: str, tag_ids: list[str] | None = None
    ) -> Any:
        return self.request(
            Route(
                "PUT",
                "streams/tags",
                parameters=[("broadcaster_id", broadcaster_id)],
                body={"tag_ids": tag_ids},
                target=target,
                scope=["user:edit:broadcast"],
            )
        )

    def post_follow_channel(self, target: PartialUser, from_id: str, to_id: str, notifications=False) -> Any:
        return self.request(
            Route(
                "POST",
                "users/follows",
                None,
                parameters=[("from_id", from_id), ("to_id", to_id), ("allow_notifications", str(notifications))],
                scope=["user:edit:follows"],
                target=target,
            )
        )

    def delete_unfollow_channel(self, target: PartialUser, from_id: str, to_id: str) -> Any:
        return self.request(
            Route(
                "DELETE",
                "users/follows",
                None,
                parameters=[("from_id", from_id), ("to_id", to_id)],
                scope=["user:edit:follows"],
                target=target,
            )
        )

    def get_users(
        self, ids: list[int] | None, logins: list[str] | None, target: PartialUser | None = None
    ) -> HTTPAwaitableAsyncIterator:
        params = []
        if ids:
            params.extend(("id", id) for id in ids)

        if logins:
            params.extend(("login", login) for login in logins)

        return self.request_paginated_route(Route("GET", "users", None, parameters=params, target=target))

    def get_user_follows(
        self, from_id: str | None = None, to_id: str | None = None, target: PartialUser | None = None
    ) -> HTTPAwaitableAsyncIterator:
        return self.request_paginated_route(
            Route(
                "GET",
                "users/follows",
                None,
                parameters=[x for x in [("from_id", from_id), ("to_id", to_id)] if x[1] is not None],
                target=target,
            )
        )

    def put_update_user(self, target: PartialUser, description: str) -> Any:
        return self.request(Route("PUT", "users", None, parameters=[("description", description)], target=target))

    def get_channel_extensions(self, target: PartialUser) -> Any:
        return self.request(Route("GET", "users/extensions/list", None, target=target))

    def get_user_active_extensions(self, target: PartialUser, user_id: str | None = None) -> Any:
        return self.request(
            Route(
                "GET",
                "users/extensions",
                None,
                parameters=[("user_id", user_id)],
                scope=["user:read:broadcast"],
                target=target,
            )
        )

    def put_user_extensions(self, target: PartialUser, data: ExtensionBuilderType) -> Any:
        return self.request(Route("PUT", "users/extensions", target=target, body={"data": data}))

    def get_videos(
        self,
        ids: list[int] | None = None,
        user_id: str | None = None,
        game_id: str | None = None,
        sort: str | None = "time",
        type: str | None = "all",
        period: str | None = "all",
        language: str | None = None,
        target: PartialUser | None = None,
    ) -> HTTPAwaitableAsyncIterator:
        params = [
            ("user_id", user_id),
            ("game_id", game_id),
            ("sort", sort),
            ("type", type),
            ("period", period),
            ("lanaguage", language),
        ]

        if ids:
            params.extend(("id", id) for id in ids)

        return self.request_paginated_route(Route("GET", "videos", None, parameters=params, target=target))

    def delete_videos(self, target: PartialUser, ids: list[int]) -> Any:
        params: ParameterType = [("id", str(x)) for x in ids]

        return self.request(Route("DELETE", "videos", None, parameters=params, target=target))

    def get_webhook_subs(self) -> Any:  # TODO is this paginated?
        return self.request(Route("GET", "webhooks/subscriptions", None))

    def get_teams(
        self, team_name: str | None = None, team_id: int| None = None, target: PartialUser | None = None
    ):
        params: ParameterType
        if team_name:
            params = [("name", team_name)]

        elif team_id:
            params = [("id", str(team_id))]

        else:
            raise ValueError("You need to provide a team name or id")

        return self.request(Route("GET", "teams", None, parameters=params, target=target))

    def get_channel_teams(self, broadcaster_id: str) -> Any:
        params: ParameterType = [("broadcaster_id", broadcaster_id)]
        return self.request(Route("GET", "teams/channel", None, parameters=params))

    def get_polls(
        self,
        broadcaster_id: str,
        target: PartialUser,
        poll_ids: list[str] | None = None,
        first: int| None = 20,
    ) -> HTTPAwaitableAsyncIterator:
        if poll_ids and len(poll_ids) > 100:
            raise ValueError("poll_ids can only have up to 100 entries")

        if first and (first > 25 or first < 1):
            raise ValueError("first can only be between 1 and 20")

        params: ParameterType = [("broadcaster_id", broadcaster_id), ("first", str(first))]

        if poll_ids:
            params.extend(("id", poll_id) for poll_id in poll_ids)

        return self.request_paginated_route(Route("GET", "polls", None, parameters=params, target=target))

    def post_poll(
        self,
        broadcaster_id: str,
        target: PartialUser,
        title: str,
        choices,
        duration: int,
        bits_voting_enabled: bool | None = False,
        bits_per_vote: int| None = None,
        channel_points_voting_enabled: bool | None = False,
        channel_points_per_vote: int| None = None,
    ) -> Any:
        if len(title) > 60:
            raise ValueError("title must be less than or equal to 60 characters")

        if len(choices) < 2 or len(choices) > 5:
            raise ValueError("You must have between 2 and 5 choices")

        for c in choices:
            if len(c) > 25:
                raise ValueError("choice title must be less than or equal to 25 characters")

        if duration < 15 or duration > 1800:
            raise ValueError("duration must be between 15 and 1800 seconds")

        if bits_per_vote and bits_per_vote > 10000:
            raise ValueError("bits_per_vote must bebetween 0 and 10000")

        if channel_points_per_vote and channel_points_per_vote > 1000000:
            raise ValueError("channel_points_per_vote must bebetween 0 and 1000000")

        body = {
            "broadcaster_id": broadcaster_id,
            "title": title,
            "choices": [{"title": choice} for choice in choices],
            "duration": duration,
            "bits_voting_enabled": str(bits_voting_enabled),
            "channel_points_voting_enabled": str(channel_points_voting_enabled),
        }
        if bits_voting_enabled and bits_per_vote:
            body["bits_per_vote"] = bits_per_vote

        if channel_points_voting_enabled and channel_points_per_vote:
            body["channel_points_per_vote"] = channel_points_per_vote

        return self.request(Route("POST", "polls", body=body, target=target))

    def patch_poll(self, broadcaster_id: str, target: PartialUser, id: str, status: str) -> Any:
        body = {"broadcaster_id": broadcaster_id, "id": id, "status": status}
        return self.request(Route("PATCH", "polls", body=body, target=target))

    def get_goals(self, broadcaster_id: str, target: PartialUser) -> HTTPAwaitableAsyncIterator:
        return self.request_paginated_route(
            Route("GET", "goals", None, parameters=[("broadcaster_id", broadcaster_id)], target=target)
        )

    def get_chat_settings(
        self, broadcaster_id: str, moderator_id: str | None = None, target: PartialUser | None = None
    ) -> Any:
        params: ParameterType = [("broadcaster_id", broadcaster_id)]
        if moderator_id:
            params.append(("moderator_id", moderator_id))

        return self.request(Route("GET", "chat/settings", None, parameters=params, target=target))

    def patch_chat_settings(
        self,
        broadcaster_id: str,
        moderator_id: str,
        target: PartialUser,
        emote_mode: bool | None = None,
        follower_mode: bool | None = None,
        follower_mode_duration: int| None = None,
        slow_mode: bool | None = None,
        slow_mode_wait_time: int| None = None,
        subscriber_mode: bool | None = None,
        unique_chat_mode: bool | None = None,
        non_moderator_chat_delay: bool | None = None,
        non_moderator_chat_delay_duration: int| None = None,
    ) -> Any:
        if follower_mode_duration and follower_mode_duration > 129600:
            raise ValueError("follower_mode_duration must be below 129600")

        if slow_mode_wait_time and (slow_mode_wait_time < 3 or slow_mode_wait_time > 120):
            raise ValueError("slow_mode_wait_time must be between 3 and 120")

        if non_moderator_chat_delay_duration and non_moderator_chat_delay_duration not in {2, 4, 6}:
            raise ValueError("non_moderator_chat_delay_duration must be 2, 4 or 6")

        params: ParameterType= [("broadcaster_id", broadcaster_id), ("moderator_id", moderator_id)]
        data = {
            "emote_mode": emote_mode,
            "follower_mode": follower_mode,
            "follower_mode_duration": follower_mode_duration,
            "slow_mode": slow_mode,
            "slow_mode_wait_time": slow_mode_wait_time,
            "subscriber_mode": subscriber_mode,
            "unique_chat_mode": unique_chat_mode,
            "non_moderator_chat_delay": non_moderator_chat_delay,
            "non_moderator_chat_delay_duration": non_moderator_chat_delay_duration,
        }
        data = {k: v for k, v in data.items() if v is not None}

        return self.request(Route("PATCH", "chat/settings", data, parameters=params, target=target))

    ######### NEW STUFF TO UPDATE
    async def post_chat_announcement(
        self,
        target: PartialUser,
        broadcaster_id: str,
        moderator_id: str,
        message: str,
        color: str | None = "primary",
    ):
        params = [("broadcaster_id", broadcaster_id), ("moderator_id", moderator_id)]
        body = {"message": message, "color": color}
        return await self.request(Route("POST", "chat/announcements", parameters=params, body=body, target=target))

    async def delete_chat_messages(
        self, target: PartialUser, broadcaster_id: str, moderator_id: str, message_id: str | None = None
    ):
        params = [("broadcaster_id", broadcaster_id), ("moderator_id", moderator_id)]
        if message_id:
            params.append(("message_id", message_id))
        return await self.request(Route("DELETE", "moderation/chat", None, parameters=params, target=target))

    async def put_user_chat_color(self, target: PartialUser, user_id: str, color: str):
        params = [("user_id", user_id), ("color", color)]
        return await self.request(Route("PUT", "chat/color", None, parameters=params, target=target))

    async def get_user_chat_color(self, user_ids: list[int], target: PartialUser | None = None):
        if len(user_ids) > 100:
            raise ValueError("You can only get up to 100 user chat colors at once")
        return await self.request(
            Route(
                "GET", "chat/color", None, parameters=[("user_id", str(user_id)) for user_id in user_ids], target=target
            )
        )

    async def post_channel_moderator(self, target: PartialUser, broadcaster_id: str, user_id: str):
        params: ParameterType = [("broadcaster_id", broadcaster_id), ("user_id", user_id)]
        return await self.request(Route("POST", "moderation/moderators", None, parameters=params, target=target))

    async def delete_channel_moderator(self, target: PartialUser, broadcaster_id: str, user_id: str):
        params: ParameterType = [("broadcaster_id", broadcaster_id), ("user_id", user_id)]
        return await self.request(Route("DELETE", "moderation/moderators", None, parameters=params, target=target))

    async def get_channel_vips(
        self, target: PartialUser, broadcaster_id: str, first: int = 20, user_ids: list[int] | None = None
    ):
        params = [("broadcaster_id", broadcaster_id), ("first", first)]
        if first > 100:
            raise ValueError("You can only get up to 100 VIPs at once")
        if user_ids:
            if len(user_ids) > 100:
                raise ValueError("You can can only specify up to 100 VIPs")
            params.extend(("user_id", str(user_id)) for user_id in user_ids)
        return await self.request(Route("GET", "channels/vips", None, parameters=params, target=target))

    async def post_channel_vip(self, target: PartialUser, broadcaster_id: str, user_id: str):
        params: ParameterType = [("broadcaster_id", broadcaster_id), ("user_id", user_id)]
        return await self.request(Route("POST", "channels/vips", None, parameters=params, target=target))

    async def delete_channel_vip(self, target: PartialUser, broadcaster_id: str, user_id: str):
        params: ParameterType = [("broadcaster_id", broadcaster_id), ("user_id", user_id)]
        return await self.request(Route("DELETE", "channels/vips", None, parameters=params, target=target))

    async def post_whisper(self, target: PartialUser, from_user_id: str, to_user_id: str, message: str):
        params: ParameterType = [("from_user_id", from_user_id), ("to_user_id", to_user_id)]
        body = {"message": message}
        return await self.request(Route("POST", "whispers", body=body, parameters=params, target=target))

    async def post_raid(self, target: PartialUser, from_broadcaster_id: str, to_broadcaster_id: str):
        params: ParameterType = [("from_broadcaster_id", from_broadcaster_id), ("to_broadcaster_id", to_broadcaster_id)]
        return await self.request(Route("POST", "raids", None, parameters=params, target=target))

    async def delete_raid(self, target: PartialUser, broadcaster_id: str):
        params: ParameterType= [("broadcaster_id", broadcaster_id)]
        return await self.request(Route("DELETE", "raids", None, parameters=params, target=target))

    async def post_ban_timeout_user(
        self,
        target: PartialUser,
        broadcaster_id: str,
        moderator_id: str,
        user_id: str,
        reason: str,
        duration: int| None = None,
    ):
        params: ParameterType = [("broadcaster_id", broadcaster_id), ("moderator_id", moderator_id)]
        body = {"data": {"user_id": user_id, "reason": reason}}
        if duration:
            if duration < 1 or duration > 1209600:
                raise ValueError("Duration must be between 1 and 1209600 seconds")
            body["data"]["duration"] = str(duration)
        return await self.request(Route("POST", "moderation/bans", body=body, parameters=params, target=target))

    async def delete_ban_timeout_user(
        self,
        target: PartialUser,
        broadcaster_id: str,
        moderator_id: str,
        user_id: str,
    ):
        params: ParameterType = [("broadcaster_id", broadcaster_id), ("moderator_id", moderator_id), ("user_id", user_id)]
        return await self.request(Route("DELETE", "moderation/bans", None, parameters=params, target=target))

    async def get_follow_count(
        self, from_id: str | None = None, to_id: str | None = None, target: PartialUser | None = None
    ):
        return await self.request(
            Route(
                "GET",
                "users/follows",
                None,
                parameters=[x for x in [("from_id", from_id), ("to_id", to_id)] if x[1] is not None],
                target=target,
            )
        )
