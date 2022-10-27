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
import inspect
import sys
import traceback
from typing import TYPE_CHECKING, Any, Callable, Coroutine

from twitchio.http import HTTPAwaitableAsyncIterator, HTTPHandler

from .channel import Channel
from .chatter import PartialChatter
from .exceptions import HTTPException
from .limiter import IRCRateLimiter
from .message import Message
from .models import *
from .parser import IRCPayload
from .shards import ShardInfo
from .user import User, PartialUser, SearchUser
from .tokens import BaseTokenHandler
from .websocket import Websocket

if TYPE_CHECKING:
    from ext.eventsub import EventSubClient

_initial_channels_T = list[str] | tuple[str] | Callable[[], list[str]] | Coroutine[Any, Any, None] | None

__all__ = ("Client",)


class Client:
    """THe main Twitch HTTP and IRC Client.

    This client can be used as a standalone to both HTTP and IRC or used together.

    Parameters
    ----------
    token_handler: :class:`~twitchio.BaseTokenHandler`
        Your token handler instance. See ... # TODO doc link to explaining token handlers
    heartbeat: Optional[:class:`float`]
        An optional heartbeat to provide to keep connections over proxies and such alive.
        Defaults to 30.0.
    verified: Optional[:class:`bool`]
        Whether or not your bot is verified or not. Defaults to False.
    join_timeout: Optional[:class:`float`]
        An optional float to use to timeout channel joins. Defaults to 15.0.
    initial_channels: Optional[Union[list[:class:`str`], tuple[:class:`str`], :class:`callable`, :class:`coroutine`]]
        An optional list or tuple of channels to join on bot start. This may be a callable or coroutine,
        but must return a :clas:`list` or :class:`tuple`.
    shard_limit: :class:`int`
        The amount of channels per websocket. Defaults to 100 channels per socket.
    cache_size: Optional[:class:`int`]
        The size of the internal channel cache. Defaults to unlimited.
    eventsub: Optional[:class:`~twitchio.ext.EventSubClient`]
        The EventSubClient instance to use with the client to dispatch subscribed webhook events.
    """

    def __init__(
        self,
        token_handler: BaseTokenHandler,
        heartbeat: float | None = 30.0,
        verified: bool | None = False,
        join_timeout: float | None = 15.0,
        initial_channels: _initial_channels_T = None,
        shard_limit: int = 100,
        cache_size: int | None = None,
        eventsub: EventSubClient | None = None,
        **kwargs,
    ):
        self._token_handler: BaseTokenHandler = token_handler._post_init(self)
        self._heartbeat = heartbeat
        self._verified = verified
        self._join_timeout = join_timeout

        self._cache_size = cache_size

        self._shards = {}
        self._shard_limit = shard_limit
        self._initial_channels: _initial_channels_T = initial_channels or []

        self._limiter = IRCRateLimiter(status="verified" if verified else "user", bucket="joins")
        self._http = HTTPHandler(None, self._token_handler, client=self, **kwargs)

        self._eventsub: EventSubClient | None = None
        if eventsub:
            self._eventsub = eventsub
            self._eventsub._client = self
            self._eventsub._client_ready.set()

        self.loop: asyncio.AbstractEventLoop | None = None
        self._kwargs: dict[str, Any] = kwargs

        self._is_closed = False
        self._has_acquired = False

    async def __aenter__(self):
        await self.setup()
        self._has_acquired = True
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self._has_acquired = False
        if not self._is_closed:
            await self.close()

    async def _shard(self):
        if inspect.iscoroutinefunction(self._initial_channels):
            channels = await self._initial_channels()

        elif callable(self._initial_channels):
            channels = self._initial_channels()

        elif isinstance(self._initial_channels, (list, tuple)):
            channels = self._initial_channels
        else:
            raise TypeError("initial_channels must be a list, tuple, callable or coroutine returning a list or tuple.")

        if not isinstance(channels, (list, tuple)):
            raise TypeError("initial_channels must return a list or tuple of str.")

        chunked = [channels[x : x + self._shard_limit] for x in range(0, len(channels), self._shard_limit)]

        for index, chunk in enumerate(chunked, 1):
            self._shards[index] = ShardInfo(
                number=index,
                channels=channels,
                websocket=Websocket(
                    token_handler=self._token_handler,
                    client=self,
                    limiter=self._limiter,
                    shard_index=index,
                    heartbeat=self._heartbeat,
                    join_timeout=self._join_timeout,
                    initial_channels=chunk,  # type: ignore
                    cache_size=self._cache_size,
                    **self._kwargs,
                ),
            )

    def run(self) -> None:
        """A blocking call that starts and connects the bot to IRC.

        This methods abstracts away starting and cleaning up for you.

        .. warning::

            You should not use this method unless you are connecting to IRC.

        .. note::

            Since this method is blocking it should be the last thing to be called.
            Anything under it will only execute after this method has completed.

        .. info::

            If you want to take more control over cleanup, see :meth:`close`.
        """
        self.loop = asyncio.get_event_loop()
        self.loop.run_until_complete(self._shard())

        for shard in self._shards.values():
            self.loop.create_task(shard._websocket._connect())

        if self._eventsub:
            self.loop.create_task(self._eventsub._run())  # TODO: Cleanup...

        try:
            self.loop.run_forever()
        except KeyboardInterrupt:
            pass
        finally:
            self.loop.run_until_complete(self.close())

    async def start(self) -> None:
        """|coro|
        This method connects to twitch's IRC servers, and prepares to handle incoming messages.
        This method will not return until all the IRC shards have been closed
        """
        if not self._has_acquired:
            raise RuntimeError(
                "You must first enter an async context by calling `async with client:`"
            )  # TODO need better error

        await self._shard()

        shard_tasks = [asyncio.create_task(shard._websocket._connect()) for shard in self._shards.values()]

        await asyncio.wait(shard_tasks)

    async def close(self) -> None:
        for shard in self._shards.values():
            await shard._websocket.close()

        await self._http.cleanup()

        self._is_closed = True

    @property
    def shards(self) -> dict[int, ShardInfo]:
        """A dict of shard number to :class:`~twitchio.ShardInfo`"""
        return self._shards

    @property
    def nick(self) -> str | None:
        """The bots nickname.

        This may be None if a shard has not become ready, or you have entered invalid credentials.
        """
        return self._shards[1]._websocket.nick

    nickname = nick

    def get_channel(self, name: str, /) -> Channel | None:
        """Method which returns a channel from cache if it exits.

        Could be None if the channel is not in cache.

        Parameters
        ----------
        name: :class:`str`
            The name of the channel to search cache for.

        Returns
        -------
        channel: Optional[:class:`~twitchio.Channel`]
            The channel matching the provided name.
        """
        name = name.strip("#").lower()

        channel = None

        for shard in self._shards.values():
            channel = shard._websocket._channel_cache.get(name, default=None)

            if channel:
                break

        return channel

    def get_message(self, id_: str, /) -> Message | None:
        """Method which returns a message from cache if it exists.

        Could be ``None`` if the message is not in cache.

        Parameters
        ----------
        id_: :class:`str`
            The message ID to search cache for.

        Returns
        -------
        message: Optional[:class:`~twitchio.Message`]
            The message matching the provided identifier.
        """
        message = None

        for shard in self._shards.values():
            message = shard._websocket._message_cache.get(id_, default=None)

            if message:
                break

        return message

    async def fetch_users(
        self, names: list[str] | None = None, ids: list[int] | None = None, target: PartialUser | None = None
    ) -> list[User]:
        """|coro|

        Fetches users from twitch. You can provide any combination of up to 100 names and ids, but you must pass at least 1.

        Parameters
        -----------
        names: Optional[list[:class:`str`]]
            A list of usernames
        ids: Optional[list[Union[:class:`str`, :class:`int`]]
            A list of IDs
        target: Optional[:class:`~twitchio.PartialUser`]
            The target of this HTTP call. Passing a user will tell the library to put this call under the authorized token for that user, if one exists in your token handler

        Returns
        --------
        list[:class:`~twitchio.User`]
        """
        if not names and not ids:
            raise ValueError("No names or ids passed to fetch_users")

        data: HTTPAwaitableAsyncIterator[User] = self._http.get_users(ids=ids, logins=names, target=target)
        data.set_adapter(lambda http, data: User(http, data))

        return await data

    async def fetch_user(
        self, name: str | None = None, id: int | None = None, target: PartialUser | None = None
    ) -> User:
        """|coro|

        Fetches a user from twitch. This is the same as :meth:`~Client.fetch_users`, but only returns one :class:`~twitchio.User`, instead of a list.
        You may only provide either name or id, not both.

        Parameters
        -----------
        name: Optional[:class:`str`]
            A username
        id: Optional[:class:`int`]
            A user ID
        target: Optional[:class:`~twitchio.PartialUser`]
            The target of this HTTP call. Passing a user will tell the library to put this call under the authorized token for that user, if one exists in your token handler

        Returns
        --------
        :class:`~twitchio.User`
        """
        if not name and not id:
            raise ValueError("Expected a name or id")

        if name and id:
            raise ValueError("Expected a name or id, got nothing")

        data: HTTPAwaitableAsyncIterator[User] = self._http.get_users(
            ids=[id] if id else None, logins=[name] if name else None, target=target
        )
        data.set_adapter(lambda http, data: User(http, data))
        resp = await data

        return resp[0]

    async def fetch_cheermotes(self, user_id: int | None = None, target: PartialUser | None = None) -> list[CheerEmote]:
        """|coro|


        Fetches cheermotes from the twitch API

        Parameters
        -----------
        user_id: Optional[:class:`int`]
            The channel id to fetch from.
        target: Optional[:class:`~twitchio.PartialUser`]
            The target of this HTTP call. Passing a user will tell the library to put this call under the authorized token for that user, if one exists in your token handler

        Returns
        --------
            list[:class:`twitchio.CheerEmote`]
        """
        data = await self._http.get_cheermotes(str(user_id) if user_id else None, target=target)
        return [CheerEmote(self._http, x) for x in data["data"]]

    async def search_channels(
        self, query: str, *, live_only=False, target: PartialUser | None = None
    ) -> list[SearchUser]:
        """|coro|

        Searches channels for the given query

        Parameters
        -----------
        query: :class:`str`
            The query to search for
        live_only: :class:`bool`
            Only search live channels. Defaults to False

        Returns
        --------
            list[:class:`twitchio.SearchUser`]
        """

        data: HTTPAwaitableAsyncIterator[SearchUser] = self._http.get_search_channels(
            query, live=live_only, target=target
        )
        data.set_adapter(lambda http, data: SearchUser(http, data))

        return await data

    async def search_categories(self, query: str, target: PartialUser | None = None) -> list[Game]:
        """|coro|

        Searches twitches categories

        Parameters
        -----------
        query: :class:`str`
            The query to search for
        target: Optional[:class:`~twitchio.PartialUser`]
            The target of this HTTP call. Passing a user will tell the library to put this call under the authorized token for that user, if one exists in your token handler

        Returns
        --------
            list[:class:`twitchio.Game`]
        """

        data: HTTPAwaitableAsyncIterator[Game] = self._http.get_search_categories(query=query, target=target)
        data.set_adapter(lambda http, data: Game(http, data))

        return await data

    async def fetch_channel_info(
        self, broadcaster_ids: list[int], target: PartialUser | None = None
    ) -> list[ChannelInfo]:
        """|coro|

        Retrieve channel information from the API.

        Parameters
        -----------
        broadcaster_ids: list[str]
            A list of channel IDs to request from API. Returns empty list if no channel was found.
            You may specify a maximum of 100 IDs.

        Returns
        --------
            list[:class:`twitchio.ChannelInfo`]
        """

        try:

            data = await self._http.get_channels(broadcaster_ids=broadcaster_ids, target=target)

            return [ChannelInfo(self._http, c) for c in data["data"]]

        except HTTPException as e:
            raise HTTPException("Incorrect channel ID provided") from e

    async def fetch_clips(self, ids: list[str], target: PartialUser | None = None) -> list[Clip]:
        """|coro|

        Fetches clips by clip id.
        To fetch clips by user id, use :meth:`twitchio.PartialUser.fetch_clips`

        Parameters
        -----------
        ids: list[:class:`str`]
            A list of clip ids
        target: Optional[:class:`~twitchio.PartialUser`]
            The target of this HTTP call. Passing a user will tell the library to put this call under the authorized token for that user, if one exists in your token handler

        Returns
        --------
            list[:class:`twitchio.Clip`]
        """

        data: HTTPAwaitableAsyncIterator[Clip] = self._http.get_clips(ids=ids, target=target)
        data.set_adapter(lambda http, data: Clip(http, data))

        return await data

    async def fetch_videos(
        self,
        ids: list[int] | None = None,
        game_id: int | None = None,
        period: str | None = "all",
        sort: str | None = "time",
        type: str | None = "all",
        language: str | None = None,
        target: PartialUser | None = None,
    ) -> list[Video]:
        """|coro|

        Fetches videos by id or game id.
        To fetch videos by user id, use :meth:`twitchio.PartialUser.fetch_videos`

        Parameters
        -----------
        ids: Optional[list[:class:`int`]]
            A list of video ids up to 100.
        game_id: Optional[:class:`int`]
            A game to fetch videos from. Limit 1.
        period: Optional[:class:`str`]
            The period for which to fetch videos. Valid values are `all`, `day`, `week`, `month`. Defaults to `all`.
            Cannot be used when video id(s) are passed
        sort: Optional[:class:`str`]
            Sort orders of the videos. Valid values are `time`, `trending`, `views`, Defaults to `time`.
            Cannot be used when video id(s) are passed
        type: Optional[:class:`str`]
            Type of the videos to fetch. Valid values are `upload`, `archive`, `highlight`. Defaults to `all`.
            Cannot be used when video id(s) are passed
        language: Optional[:class:`str`]
            Language of the videos to fetch. Must be an `ISO-639-1 <https://en.wikipedia.org/wiki/List_of_ISO_639-1_codes>`_ two letter code.
            Cannot be used when video id(s) are passed
        target: Optional[:class:`~twitchio.PartialUser`]
            The target of this HTTP call. Passing a user will tell the library to put this call under the authorized token for that user, if one exists in your token handler

        Returns
        --------
            list[:class:`twitchio.Video`]
        """

        data: HTTPAwaitableAsyncIterator[Video] = self._http.get_videos(
            ids=ids, game_id=str(game_id), period=period, sort=sort, type=type, language=language, target=target
        )
        data.set_adapter(lambda http, data: Video(http, data))

        return await data

    async def fetch_chatters_colors(self, user_ids: list[int], target: PartialUser | None = None) -> list[ChatterColor]:
        """|coro|

        Fetches the color of a chatter.

        Parameters
        -----------
        user_ids: list[:class:`int`]
            List of user ids to fetch the colors for
        target: Optional[:class:`~twitchio.PartialUser`]
            The target of this HTTP call. Passing a user will tell the library to put this call under the authorized token for that user, if one exists in your token handler

        Returns
        --------
            list[:class:`twitchio.ChatterColor`]
        """
        data = await self._http.get_user_chat_color(user_ids, target)
        return [ChatterColor(self._http, x) for x in data["data"]]

    async def fetch_games(
        self, ids: list[int] | None = None, names: list[str] | None = None, target: PartialUser | None = None
    ) -> list[Game]:
        """|coro|

        Fetches games by id or name.
        At least one id or name must be provided

        Parameters
        -----------
        ids: Optional[list[:class:`int`]]
            An optional list of game ids
        names: Optional[list[:class:`str`]]
            An optional list of game names
        target: Optional[:class:`~twitchio.PartialUser`]
            The target of this HTTP call. Passing a user will tell the library to put this call under the authorized token for that user, if one exists in your token handler

        Returns
        --------
            list[:class:`twitchio.Game`]
        """

        data: HTTPAwaitableAsyncIterator[Game] = self._http.get_games(game_ids=ids, game_names=names, target=target)
        data.set_adapter(lambda http, data: Game(http, data))

        return await data

    async def fetch_streams(
        self,
        user_ids: list[int] | None = None,
        game_ids: list[int] | None = None,
        user_logins: list[str] | None = None,
        languages: list[str] | None = None,
        target: PartialUser | None = None,
    ) -> list[Stream]:
        """|coro|

        Fetches live streams from the helix API

        Parameters
        -----------
        user_ids: Optional[list[:class:`int`]]
            user ids of people whose streams to fetch
        game_ids: Optional[list[:class:`int`]]
            game ids of streams to fetch
        user_logins: Optional[list[:class:`str`]]
            user login names of people whose streams to fetch
        languages: Optional[list[:class:`str`]]
            language for the stream(s). ISO 639-1 or two letter code for supported stream language
        target: Optional[:class:`~twitchio.PartialUser`]
            The target of this HTTP call. Passing a user will tell the library to put this call under the authorized token for that user, if one exists in your token handler


        Returns
        --------
        list[:class:`twitchio.Stream`]
        """

        data: HTTPAwaitableAsyncIterator[Stream] = self._http.get_streams(
            game_ids=game_ids,
            user_ids=user_ids,
            user_logins=user_logins,
            languages=languages,
            target=target,
        )
        data.set_adapter(lambda http, data: Stream(http, data))

        return await data

    async def fetch_top_games(self, target: PartialUser | None = None) -> list[Game]:
        """|coro|

        Fetches the top games from the api

        Parameters
        ----------
        target: Optional[:class:`~twitchio.PartialUser`]
            The target of this HTTP call. Passing a user will tell the library to put this call under the authorized token for that user, if one exists in your token handler

        Returns
        --------
            list[:class:`twitchio.Game`]
        """
        data: HTTPAwaitableAsyncIterator[Game] = self._http.get_top_games(target=target)
        data.set_adapter(lambda http, data: Game(http, data))

        return await data

    async def fetch_tags(self, ids: list[str] | None = None, target: PartialUser | None = None) -> list[Tag]:
        """|coro|

        Fetches stream tags.

        Parameters
        -----------
        ids: Optional[list[:class:`str`]]
            The ids of the tags to fetch
        target: Optional[:class:`~twitchio.PartialUser`]
            The target of this HTTP call. Passing a user will tell the library to put this call under the authorized token for that user, if one exists in your token handler

        Returns
        --------
            list[:class:`twitchio.Tag`]
        """

        data: HTTPAwaitableAsyncIterator[Tag] = self._http.get_stream_tags(tag_ids=ids, target=target)
        data.set_adapter(lambda http, data: Tag(http, data))

        return await data

    async def fetch_team(
        self, team_name: str | None = None, team_id: int | None = None, target: PartialUser | None = None
    ) -> Team:
        """|coro|

        Fetches information for a specific Twitch Team.

        Parameters
        -----------
        name: Optional[:class:`str`]
            Team name to fetch
        id: Optional[:class:`int`]
            Team id to fetch
        target: Optional[:class:`~twitchio.PartialUser`]
            The target of this HTTP call. Passing a user will tell the library to put this call under the authorized token for that user, if one exists in your token handler

        Returns
        --------
        :class:`twitchio.Team`
        """

        data = await self._http.get_teams(
            team_name=team_name,
            team_id=team_id,
            target=target,
        )
        return Team(self._http, data["data"][0])

    async def delete_videos(self, target: PartialUser, ids: list[int]) -> list[int]:
        """|coro|

        Delete videos from the api. Returns the video ids that were successfully deleted.

        Parameters
        -----------
        target: Optional[:class:`~twitchio.PartialUser`]
            The target of this HTTP call. Passing a user will tell the library to put this call under the authorized token for that user, if one exists in your token handler
            ``channel:manage:videos`` scope is required
        ids: list[:class:`int`]
            A list of video ids from the channel of the oauth token to delete


        Returns
        --------
            list[:class:`int`]
        """
        resp = []
        for chunk in [ids[x : x + 3] for x in range(0, len(ids), 3)]:
            resp.append(await self._http.delete_videos(target, chunk))

        return resp

    async def event_shard_ready(self, number: int) -> None:
        """|coro|

        Event fired when a shard becomes ready.

        Parameters
        ----------
        number: :class:`int`
            The shard number identifier.

        Returns
        -------
        None
        """
        pass

    async def event_ready(self) -> None:
        """|coro|

        Event fired when the bot has completed startup.
        This includes all shards being ready.

        Returns
        -------
        None
        """
        pass

    async def event_error(self, error: Exception) -> None:
        traceback.print_exception(type(error), error, error.__traceback__, file=sys.stderr)

    async def event_raw_data(self, data: str) -> None:
        """|coro|

        Event fired with the raw data received, unparsed, by Twitch.

        Parameters
        ----------
        data: :class:`str`
            The data received from Twitch.

        Returns
        -------
        None
        """
        pass

    async def event_raw_payload(self, payload: IRCPayload) -> None:
        """|coro|

        Event fired with the parsed IRC payload object.

        Parameters
        ----------
        payload: :class:`~twitchio.IRCPayload`
            The parsed IRC payload from Twitch.

        Returns
        -------
        None
        """
        pass

    async def event_message(self, message: Message) -> None:
        """|coro|

        Event fired when receiving a message in a joined channel.

        Parameters
        ----------
        message: :class:`~twitchio.Message`
            The message received via Twitch.

        Returns
        -------
        None
        """
        pass

    async def event_join(self, channel: Channel, chatter: PartialChatter) -> None:
        """|coro|

        Event fired when a JOIN is received via Twitch.

        Parameters
        ----------
        channel: :class:`~twitchio.Channel`
            ...
        chatter: :class:`~twitchio.PartialChatter`
            ...
        """

    async def event_part(self, channel: Channel | None, chatter: PartialChatter) -> None:
        """|coro|

        Event fired when a PART is received via Twitch.

        Parameters
        ----------
        channel: Optional[:class:`~twitchio.Channel`]
            ... Could be None if the channel is not in your cache.
        chatter: :class:`~twitchio.PartialChatter`
            ...
        """

    async def setup(self) -> None:
        """|coro|

        Method called before the Client has logged in to Twitch, used for asynchronous setup.

        Useful for setting up state, like databases, before the client has logged in.

        .. versionadded:: 3.0
        """
        pass
