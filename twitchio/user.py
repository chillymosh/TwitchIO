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

import datetime
import time
from typing import TYPE_CHECKING, Literal

from .enums import BroadcasterTypeEnum, UserTypeEnum
from .rewards import CustomReward
from .utils import parse_timestamp


if TYPE_CHECKING:
    from .channel import Channel
    from .http import HTTPAwaitableAsyncIterator, HTTPAwaitableAsyncIteratorWithSource, HTTPHandler
    from .models import (
    Clip,
    Tag,
    Video,
    HypeTrainEvent,
    BanEvent,
    ModEvent,
    FollowEvent,
    BitsLeaderboard,
    SubscriptionEvent,
    Marker,
    Extension,
    ActiveExtension,
    VideoMarkers,
    AutomodCheckMessage,
    AutomodCheckResponse,
    Prediction,
    Schedule,
    Poll,
    ChannelTeams,
    ExtensionBuilder,
    ActiveExtensionType,
    )

__all__ = (
    "PartialUser",
    "BitLeaderboardUser",
    "UserBan",
    "SearchUser",
    "User",
)

class PartialUser:
    """
    A minimal representation of a user on twitch.

    Attributes
    -----------
    id: :class:`int`
        The id of the user.
    name: Optional[:class:`str`]
        The name of the user (this corresponds to the ``login`` field of the API)
    """

    __slots__ = "id", "name", "_http", "_cached_rewards"

    def __init__(self, http: HTTPHandler, id: int | str, name: str | None):
        self.id: int = int(id)
        self.name: str | None = name
        self._http: HTTPHandler = http

        self._cached_rewards: tuple[float, list[CustomReward]] | None = None

    def __repr__(self) -> str:
        return f"<PartialUser id={self.id}, name={self.name}>"

    @property
    def channel(self) -> Channel | None:
        """
        Returns the :class:`~twitchio.Channel` associated with this user. Could be ``None`` if you are not part of the channel's chat

        Returns
        --------
        Optional[:class:`~twitchio.Channel`]
        """

        if self.name in self._http.client._connection._cache:
            return Channel(name=self.name, websocket=self._http.client._connection)

    async def fetch(self) -> User:
        """|coro|

        Fetches the full user from the api

        Returns
        --------
        :class:`User` The full user associated with this :class:`PartialUser`
        """
        if not self._http.client:
            raise RuntimeError("No client attached to underlying HTTP session")

        return await self._http.client.fetch_user(id=self.id, target=self)

    async def edit(self, description: str) -> None:
        """|coro|

        Edits a channels description

        Parameters
        -----------
        description: :class:`str`
            The new description for the user
        """
        await self._http.put_update_user(self, description)

    async def fetch_tags(self) -> list[Tag]:
        """|coro|

        Fetches tags the user currently has active.

        Returns
        --------
            list[:class:`Tag`]
        """

        from .models import Tag
        
        data: HTTPAwaitableAsyncIterator[Tag] = self._http.get_channel_tags(str(self.id))
        data.set_adapter(lambda http, data: Tag(http, data))

        return await data

    async def replace_tags(self, tags: list[str | Tag]) -> None:
        """|coro|

        Replaces the channels active tags. Tags expire 72 hours after being applied,
        unless the stream is live during that time period.

        Parameters
        -----------
        token: :class:`str`
            An oauth token with the user:edit:broadcast scope
        tags: list[Union[:class:`Tag`, :class:`str`]]
            A list of :class:`Tag` or tag ids to put on the channel. Max 100
        """
        tags_ = [x if isinstance(x, str) else x.id for x in tags]
        await self._http.put_replace_channel_tags(self, str(self.id), tags_)

    async def get_custom_rewards(
        self, *, only_manageable=False, ids: list[int] | None = None, force=False
    ) -> HTTPAwaitableAsyncIterator[CustomReward]:
        """|coro|

        Fetches the channels custom rewards (aka channel points) from the api.

        Parameters
        ----------
        only_manageable: :class:`bool`
            Whether to fetch all rewards or only ones you can manage. Defaults to false.
        ids: list[:class:`int`]
            An optional list of reward ids
        force: :class:`bool`
            Whether to force a fetch or try to get from cache. Defaults to False

        Returns
        -------
        list[:class:`~twitchio.CustomReward`]
        """
        if not force and self._cached_rewards and self._cached_rewards[0] + 300 > time.monotonic():
            return HTTPAwaitableAsyncIteratorWithSource(self._cached_rewards[1])

        self._cached_rewards = (time.monotonic(), [])

        from .rewards import CustomReward

        def adapter(handler: HTTPHandler, data) -> CustomReward:
            resp = CustomReward(handler, data, self)
            self._cached_rewards[1].append(resp)  # type: ignore
            return resp

        data: HTTPAwaitableAsyncIterator[CustomReward] = self._http.get_rewards(self, self.id, only_manageable, ids)
        data.set_adapter(adapter)
        return data

    async def fetch_bits_leaderboard(
        self,
        period: Literal["all", "day", "week", "month", "year"] = "all",
        user_id: int | None = None,
        started_at: datetime.datetime | None = None,
    ) -> BitsLeaderboard:
        """|coro|

        Fetches the bits leaderboard for the channel. This requires an OAuth token with the bits:read scope.

        Parameters
        -----------
        period: Optional[:class:`str`]
            one of `day`, `week`, `month`, `year`, or `all`, defaults to `all`
        started_at: Optional[:class:`datetime.datetime`]
            the timestamp to start the period at. This is ignored if the period is `all`
        user_id: Optional[:class:`int`]
            the id of the user to fetch for
        """

        from .models import BitsLeaderboard
        data = await self._http.get_bits_board(self, period, user_id, started_at)
        return BitsLeaderboard(self._http, data)

    async def start_commercial(self, length: Literal[30, 60, 90, 120, 150, 180]) -> dict:
        """|coro|

        Starts a commercial on the channel. Requires an OAuth token with the `channel:edit:commercial` scope.

        Parameters
        -----------
        length: :class:`int`
            the length of the commercial. Should be one of `30`, `60`, `90`, `120`, `150`, `180`

        Returns
        --------
        :class:`dict` a dictionary with `length`, `message`, and `retry_after`
        """
        data = await self._http.post_commercial(self, str(self.id), length)
        return data

    async def create_clip(self, has_delay=False) -> dict:
        """|coro|

        Creates a clip on the channel. Note that clips are not created instantly, so you will have to query
        :meth:`~get_clips` to confirm the clip was created. Requires an OAuth token with the `clips:edit` scope

        Parameters
        -----------
        has_delay: :class:`bool`
            Whether the clip should have a delay to match that of a viewer. Defaults to False

        Returns
        --------
        :class:`dict` a dictionary with `id` and `edit_url`
        """
        data = await self._http.post_create_clip(self, self.id, has_delay)
        return data["data"][0]

    def fetch_clips(self) -> HTTPAwaitableAsyncIterator[Clip]:
        """|coro|

        Fetches clips from the api. This will only return clips from the specified user.
        Use :class:`Client.fetch_clips` to fetch clips by id

        Returns
        --------
        list[:class:`twitchio.Clip`]
        """

        from .models import Clip
        iterator: HTTPAwaitableAsyncIterator[Clip] = self._http.get_clips(self.id)
        iterator.set_adapter(lambda handler, data: Clip(handler, data))

        return iterator

    def fetch_hypetrain_events(self, id: str | None = None) -> HTTPAwaitableAsyncIterator[HypeTrainEvent]:
        """|coro|

        Fetches hypetrain event from the api. Needs a token with the channel:read:hype_train scope.

        Parameters
        -----------
        id: Optional[:class:`str`]
            The hypetrain id, if known, to fetch for

        Returns
        --------
            list[:class:`twitchio.HypeTrainEvent`]
            A list of hypetrain events
        """

        from .models import HypeTrainEvent
        iterator: HTTPAwaitableAsyncIterator[HypeTrainEvent] = self._http.get_hype_train(str(self.id), id=id)
        iterator.set_adapter(lambda handler, data: HypeTrainEvent(handler, data))
        return iterator

    def fetch_bans(self, userids: list[str | int] | None = None) -> HTTPAwaitableAsyncIterator[UserBan]:
        """|coro|

        Fetches a list of people the User has banned from their channel. Requires an OAuth token with the ``moderation:read`` scope.

        Parameters
        -----------
        userids: list[Union[:class:`str`, :class:`int`]]
            An optional list of userids to fetch. Will fetch all bans if this is not passed

        Returns
        --------
        list[:class:`UserBan`]
        """

        iterator: HTTPAwaitableAsyncIterator[UserBan] = self._http.get_channel_bans(
            self, str(self.id), user_ids=userids
        )
        iterator.set_adapter(lambda handler, data: UserBan(handler, data))
        return iterator

    def fetch_ban_events(self, userids: list[int] | None = None) -> HTTPAwaitableAsyncIterator[BanEvent]:
        """|coro|

        Fetches ban/unban events from the User's channel. Requires an OAuth token with the ``moderation:read`` scope.

        Parameters
        -----------
        token: :class:`str`
            The oauth token with the moderation:read scope.
        userids: list[:class:`int`]
            An optional list of users to fetch ban/unban events for

        Returns
        --------
            list[:class:`BanEvent`]
        """

        iterator: HTTPAwaitableAsyncIterator[BanEvent] = self._http.get_channel_ban_unban_events(
            self, str(self.id), userids
        )
        iterator.set_adapter(lambda handler, data: BanEvent(handler, data, self))
        return iterator

    def fetch_moderators(self, userids: list[int] | None = None) -> HTTPAwaitableAsyncIterator[PartialUser]:
        """|coro|

        Fetches the moderators for this channel. Requires an OAuth token with the ``moderation:read`` scope.

        Parameters
        -----------
        userids: list[:class:`int`]
            An optional list of users to check mod status of

        Returns
        --------
            list[:class:`twitchio.PartialUser`]
        """
        iterator: HTTPAwaitableAsyncIterator[PartialUser] = self._http.get_channel_moderators(
            self, str(self.id), user_ids=userids
        )
        iterator.set_adapter(lambda handler, data: PartialUser(handler, data["user_id"], data["user_name"]))
        return iterator

    def fetch_mod_events(self) -> HTTPAwaitableAsyncIterator[ModEvent]:
        """|coro|

        Fetches mod events (moderators being added and removed) for this channel. Requires an OAuth token with the ``moderation:read`` scope.

        Returns
        --------
            list[:class:`twitchio.ModEvent`]
        """
        iterator: HTTPAwaitableAsyncIterator[ModEvent] = self._http.get_channel_mod_events(self, str(self.id))
        iterator.set_adapter(lambda handler, data: ModEvent(handler, data, self))
        return iterator

    async def automod_check(self, query: list[AutomodCheckMessage]) -> list[AutomodCheckResponse]:
        """|coro|

        Checks if a string passes the automod filter. Requires an OAuth token with the ``moderation:read`` scope.

        Parameters
        -----------
        query: list[:class:`AutomodCheckMessage`]
            A list of :class:`AutomodCheckMessage`

        Returns
        --------
            list[:class:`AutomodCheckResponse`]
        """
        data = await self._http.post_automod_check(self, str(self.id), [x._to_dict() for x in query])
        return [AutomodCheckResponse(d) for d in data["data"]]

    async def fetch_stream_key(self) -> str:
        """|coro|

        Fetches the users stream key. Requires an OAuth token with the ``channel:read:stream_key`` scope.

        Returns
        --------
            :class:`str`
        """
        data = await self._http.get_stream_key(self, str(self.id))
        return data  # FIXME what does this payload look like

    def fetch_following(self) -> HTTPAwaitableAsyncIterator[FollowEvent]:
        """|coro|

        Fetches a list of users that this user is following.

        Returns
        --------
            list[:class:`FollowEvent`]
        """
        iterator = self._http.get_user_follows(target=self, from_id=str(self.id))
        iterator.set_adapter(lambda handler, data: FollowEvent(handler, data, self))
        return iterator

    def fetch_followers(self) -> HTTPAwaitableAsyncIterator[FollowEvent]:
        """|coro|

        Fetches a list of users that are following this user.

        Returns
        --------
            list[:class:`FollowEvent`]
        """
        iterator = self._http.get_user_follows(to_id=str(self.id))
        iterator.set_adapter(lambda handler, data: FollowEvent(handler, data, self))
        return iterator

    async def fetch_follow(self, to_user: PartialUser) -> FollowEvent | None:
        """|coro|

        Check if a user follows another user or when they followed a user.

        Parameters
        -----------
        to_user: :class:`PartialUser`
            The user to check for a follow to. (self -> to_user)

        Returns
        --------
            :class:`FollowEvent`
        """
        if not isinstance(to_user, PartialUser):
            raise TypeError(f"to_user must be a PartialUser not {type(to_user)}")

        iterator: HTTPAwaitableAsyncIterator[FollowEvent] = self._http.get_user_follows(
            from_id=str(self.id), to_id=str(to_user.id)
        )
        iterator.set_adapter(lambda handler, data: FollowEvent(handler, data, self))
        data = await iterator
        return data[0] if data else None

    async def follow(self, target: User | PartialUser, *, notifications=False) -> None:
        """|coro|

        Follows the target user. Requires an OAuth token with the ``user:edit:follows`` scope.

        Parameters
        -----------
        target: Union[:class:`User`, :class:`PartialUser`]
            The user to follow
        notifications: :class:`bool`
            Whether to allow push notifications when the target user goes live. Defaults to False

        Returns
            ``None``
        """
        await self._http.post_follow_channel(
            self, from_id=str(self.id), to_id=str(target.id), notifications=notifications
        )

    async def unfollow(self, target: User | PartialUser) -> None:
        """|coro|

        Unfollows the target user. Requires an OAuth token with the ``user:edit:follows`` scope.

        Parameters
        -----------
        target: Union[:class:`User`, :class:`PartialUser`]
            The user to unfollow

        Returns
            ``None``
        """
        await self._http.delete_unfollow_channel(self, to_id=str(target.id), from_id=str(self.id))

    async def fetch_subscriptions(
        self, userids: list[int] | None = None
    ) -> HTTPAwaitableAsyncIterator[SubscriptionEvent]:
        """|coro|

        Fetches the subscriptions for this channel.

        Parameters
        -----------
        token: :class:`str`
            An oauth token with the channel:read:subscriptions scope
        userids: Optional[list[:class:`int`]]
            An optional list of userids to look for

        Returns
        --------
            list[:class:`twitchio.SubscriptionEvent`]
        """
        iterator: HTTPAwaitableAsyncIterator[SubscriptionEvent] = self._http.get_channel_subscriptions(
            self, str(self.id), user_ids=[str(x) for x in (userids or ())]
        )
        iterator.set_adapter(lambda handler, data: SubscriptionEvent(handler, data, self))
        return iterator

    async def create_marker(self, description: str | None = None) -> Marker:
        """|coro|

        Creates a marker on the stream. This only works if the channel is live (among other conditions).
        Requires an OAuth token with the ``user:edit:broadcast`` scope.

        Parameters
        -----------
        description: :class:`str`
            An optional description of the marker

        Returns
        --------
            :class:`Marker`
        """
        data = await self._http.post_stream_marker(self, user_id=str(self.id), description=description)
        return Marker(data["data"][0])

    async def fetch_markers(self, video_id: str | None = None) -> VideoMarkers | None:
        """|coro|

        Fetches markers from the given video id, or the most recent video.
        The Twitch api will only return markers created by the user of the authorized token.
        Requires an OAuth token with the ``user:edit:broadcast`` scope.

        Parameters
        -----------
        video_id: :class:`str`
            A specific video o fetch from. Defaults to the most recent stream if not passed

        Returns
        --------
            Optional[:class:`twitchio.VideoMarkers`]
        """
        data = await self._http.get_stream_markers(self, user_id=str(self.id), video_id=video_id)
        if data:
            return VideoMarkers(data[0]["videos"])

    async def fetch_extensions(self) -> list[Extension]:
        """|coro|

        Fetches extensions the user has (active and inactive). Requires an OAuth token with the ``user:read:broadcast`` scope.

        Returns
        --------
            list[:class:`Extension`]
        """
        data = await self._http.get_channel_extensions(self)
        return [Extension(d) for d in data["data"]]

    async def fetch_active_extensions(self) -> ActiveExtensionType:
        """|coro|

        Fetches active extensions the user has.
        Returns a dictionary containing the following keys: `panel`, `overlay`, `component`.

        Parameters
        -----------
        token: Optional[:class:`str`]
            An oauth token with the user:read:broadcast *or* user:edit:broadcast scope

        Returns
        --------
            Dict[Literal["panel", "overlay", "component"], Dict[:class:`int`, :class:`ActiveExtension`]]
        """
        data = await self._http.get_user_active_extensions(self, str(self.id))
        return {typ: {int(n): ActiveExtension(d) for n, d in vals.items()} for typ, vals in data.items()}  # type: ignore

    async def update_extensions(self, extensions: ExtensionBuilder) -> ActiveExtensionType:
        """|coro|

        Updates a users extensions. See the :class:`ExtensionBuilder` for information on how to use it

        Parameters
        -----------
        token: :class:`str`
            An oauth token with user:edit:broadcast scope
        extensions: :class:`twitchio.ExtensionBuilder`
            A :class:`twitchio.ExtensionBuilder` to be given to the twitch api

        Returns
        --------
            Dict[:class:`str`, Dict[:class:`int`, :class:`twitchio.ActiveExtension`]]
        """
        data = await self._http.put_user_extensions(self, extensions._to_dict())
        return {typ: {int(n): ActiveExtension(d) for n, d in vals.items()} for typ, vals in data.items()}  # type: ignore

    def fetch_videos(
        self,
        period: Literal["all", "day", "week", "month"] = "all",
        sort: Literal["time", "trending", "views"] = "time",
        type: Literal["upload", "archive", "highlight", "all"] = "all",
        language=None,
    ) -> HTTPAwaitableAsyncIterator[Video]:
        """|coro|

        Fetches videos that belong to the user. If you have specific video ids use :func:`~twitchio.Client.fetch_videos`

        Parameters
        -----------
        period: :class:`str`
            The period for which to fetch videos. Valid values are `all`, `day`, `week`, `month`. Defaults to `all`
        sort: :class:`str`
            Sort orders of the videos. Valid values are `time`, `trending`, `views`, Defaults to `time`
        type: Optional[:class:`str`]
            Type of the videos to fetch. Valid values are `upload`, `archive`, `highlight`, `all`. Defaults to `all`
        language: Optional[:class:`str`]
            Language of the videos to fetch. Must be an `ISO-639-1 <https://en.wikipedia.org/wiki/List_of_ISO_639-1_codes>`_ two letter code.

        Returns
        --------
            list[:class:`twitchio.Video`]
        """
        iterator: HTTPAwaitableAsyncIterator[Video] = self._http.get_videos(
            user_id=str(self.id), period=period, sort=sort, type=type, language=language
        )
        iterator.set_adapter(lambda handler, data: Video(handler, data, self))
        return iterator

    async def end_prediction(
        self, prediction_id: str, status: str, winning_outcome_id: str | None = None
    ) -> Prediction:
        """|coro|

        End a prediction with an outcome. Requires an OAuth token with the ``channel:manage:prediction`` scope.

        Parameters
        -----------
        prediction_id: :class:`str`
            ID of the prediction to end.
        status: :class:`str`
            TODO what this
        winning_outcome_id: Optional[:class:`str`]
            The outcome id. # TODO wth is this

        Returns
        --------
            :class:`Prediction`
        """
        data = await self._http.patch_prediction(
            self,
            broadcaster_id=str(self.id),
            prediction_id=prediction_id,
            status=status,
            winning_outcome_id=winning_outcome_id,
        )
        return Prediction(self._http, data[0])

    async def get_predictions(self, prediction_id: str | None = None) -> list[Prediction]:
        """|coro|

        Gets information on a prediction or the list of predictions if none is provided.

        Parameters
        -----------
        prediction_id: :class:`str`
            ID of the prediction to receive information about.

        Returns
        --------
            list[:class:`Prediction`]
        """

        data = await self._http.get_predictions(self, broadcaster_id=self.id, prediction_id=prediction_id)
        return [Prediction(self._http, d) for d in data]

    async def create_prediction(
        self, title: str, blue_outcome: str, pink_outcome: str, prediction_window: int
    ) -> Prediction:
        """|coro|

        Creates a prediction for the channel.

        Parameters
        -----------
        title: :class:`str`
            Title for the prediction (max of 45 characters)
        blue_outcome: :class:`str`
            Text for the first outcome people can vote for. (max 25 characters)
        pink_outcome: :class:`str`
            Text for the second outcome people can vote for. (max 25 characters)
        prediction_window: :class:`int`
            Total duration for the prediction (in seconds)

        Returns
        --------
            :class:`twitchio.Prediction`
        """

        data = await self._http.post_prediction(
            self,
            broadcaster_id=self.id,
            title=title,
            blue_outcome=blue_outcome,
            pink_outcome=pink_outcome,
            prediction_window=prediction_window,
        )
        return Prediction(self._http, data[0])

    async def modify_stream(
        self, token: str, game_id: int | None = None, language: str | None = None, title: str | None = None
    ):
        """|coro|

        Modify stream information

        Parameters
        -----------
        game_id: :class:`int`
            Optional game ID being played on the channel. Use 0 to unset the game.
        language: :class:`str`
            Optional language of the channel. A language value must be either the ISO 639-1 two-letter code for a supported stream language or “other”.
        title: :class:`str`
            Optional title of the stream.
        """
        gid = str(game_id) if game_id is not None else None
        await self._http.patch_channel(
            self,
            broadcaster_id=str(self.id),
            game_id=gid,
            language=language,
            title=title,
        )

    async def fetch_schedule(
        self,
        segment_ids: list[str] | None = None,
        start_time: datetime.datetime | None = None,
        utc_offset: int | None = None,
        first: int = 20,
        target: PartialUser | None = None
    ):
        """|coro|

        Fetches the schedule of a streamer
        Parameters
        -----------
        segment_ids: Optional[list[:class:`str`]]
            List of segment IDs of the stream schedule to return. Maximum: 100
        start_time: Optional[:class:`datetime.datetime`]
            A datetime object to start returning stream segments from. If not specified, the current date and time is used.
        utc_offset: Optional[:class:`int`]
            A timezone offset for the requester specified in minutes. +4 hours from GMT would be `240`
        first: Optional[:class:`int`]
            Maximum number of stream segments to return. Maximum: 25. Default: 20.
        target: Optional[:class:`~twitchio.PartialUser`]

        Returns
        --------
            :class:`twitchio.Schedule`
        """

        from .models import Schedule
        data: HTTPAwaitableAsyncIterator[Schedule] = self._http.get_channel_schedule(
            broadcaster_id=str(self.id),
            segment_ids=segment_ids,
            start_time=start_time,
            utc_offset=str(utc_offset),
            first=first,
            target=target,
        )
        data.set_adapter(lambda http, data: Schedule(http, data))

        return await data

    async def fetch_channel_teams(self):
        """|coro|

        Fetches a list of Twitch Teams of which the specified channel/broadcaster is a member.

        Returns
        --------
        list[:class:`twitchio.ChannelTeams`]
        """

        from .models import ChannelTeams
        data = await self._http.get_channel_teams(
            broadcaster_id=str(self.id),
        )

        return [ChannelTeams(self._http, x) for x in data]

    async def fetch_polls(self, target: PartialUser, poll_ids: list[str] | None = None, first: int | None = 20):
        """|coro|

        Fetches a list of polls for the specified channel/broadcaster.

        Parameters
        -----------
        poll_ids: Optional[list[:class:`str`]]
            List of poll IDs to return. Maximum: 100
        first: Optional[:class:`int`]
            Number of polls to return. Maximum: 20. Default: 20.

        Returns
        --------
        list[:class:`twitchio.Poll`]
        """

        from .models import Poll
      #  data = await self._http.get_polls(broadcaster_id=str(self.id), target=self, poll_ids=poll_ids, first=first)
      #  return [Poll(self._http, x) for x in data["data"]] if data["data"] else None

        data: HTTPAwaitableAsyncIterator[Poll] = self._http.get_polls(
            broadcaster_id=str(self.id), poll_ids=poll_ids, first=first, target=target,
        )
        data.set_adapter(lambda http, data: Poll(http, data))

        return await data


    async def create_poll(
        self,
        title: str,
        choices: list[str],
        duration: int,
        bits_voting_enabled: bool | None = False,
        bits_per_vote: int | None = None,
        channel_points_voting_enabled: bool | None = False,
        channel_points_per_vote: int | None = None,
    ):
        """|coro|

        Creates a poll for the specified channel/broadcaster.

        Parameters
        -----------
        title: :class:`str`
            Question displayed for the poll.
        choices: list[:class:`str`]
            List of choices for the poll. Must be between 2 and 5 choices.
        duration: :class:`int`
            Total duration for the poll in seconds. Must be between 15 and 1800.
        bits_voting_enabled: Optional[:class:`bool`]
            Indicates if Bits can be used for voting. Default is False.
        bits_per_vote: Optional[:class:`int`]
            Number of Bits required to vote once with Bits. Max 10000.
        channel_points_voting_enabled: Optional[:class:`bool`]
            Indicates if Channel Points can be used for voting. Default is False.
        channel_points_per_vote: Optional[:class:`int`]
            Number of Channel Points required to vote once with Channel Points. Max 1000000.

        Returns
        --------
        list[:class:`twitchio.Poll`]
        """

        from .models import Poll
        data = await self._http.post_poll(
            broadcaster_id=str(self.id),
            target=self,
            title=title,
            choices=choices,
            duration=duration,
            bits_voting_enabled=bits_voting_enabled,
            bits_per_vote=bits_per_vote,
            channel_points_voting_enabled=channel_points_voting_enabled,
            channel_points_per_vote=channel_points_per_vote,
        )
        return Poll(self._http, data[0])

    async def end_poll(self, poll_id: str, status: Literal["TERMINATED", "ARCHIVED"]):
        """|coro|

        Ends a poll for the specified channel/broadcaster.

        Parameters
        -----------
        poll_id: :class:`str`
            ID of the poll.
        status: Literal["TERMINATED", "ARCHIVED"]
            The poll status to be set. Valid values:
            TERMINATED: End the poll manually, but allow it to be viewed publicly.
            ARCHIVED: End the poll manually and do not allow it to be viewed publicly.

        Returns
        --------
        :class:`twitchio.Poll`
        """

        from .models import Poll
        data = await self._http.patch_poll(broadcaster_id=str(self.id), target=self, id=poll_id, status=status)
        return Poll(self._http, data[0])


class BitLeaderboardUser(PartialUser):

    __slots__ = "rank", "score"

    def __init__(self, http: HTTPHandler, data: dict):
        super(BitLeaderboardUser, self).__init__(http, id=data["user_id"], name=data["user_name"])
        self.rank: int = data["rank"]
        self.score: int = data["score"]


class UserBan(PartialUser): # TODO will probably rework this
    """
    Represents a banned user or one in timeout.

    Attributes
    ----------
    id: :class:`int`
        The ID of the banned user.
    name: :class:`str`
        The name of the banned user.
    created_at: :class:`datetime.datetime`
        The date and time the ban was created.
    expires_at: Optional[:class:`datetime.datetime`]
        The date and time the timeout will expire.
        Is None if it's a ban.
    reason: :class:`str`
        The reason for the ban/timeout.
    moderator: :class:`~twitchio.PartialUser`
        The moderator that banned the user.
    """

    __slots__ = ("created_at", "expires_at", "reason", "moderator")

    def __init__(self, http: HTTPHandler, data: dict):
        super(UserBan, self).__init__(http, id=data["user_id"], name=data["user_login"])
        self.created_at: datetime.datetime = parse_timestamp(data["created_at"])
        self.expires_at: datetime.datetime | None = (
            parse_timestamp(data["expires_at"]) if data["expires_at"] else None
        )
        self.reason: str = data["reason"]
        self.moderator = PartialUser(http, id=data["moderator_id"], name=data["moderator_login"])

    def __repr__(self):
        return f"<UserBan {super().__repr__()} created_at={self.created_at} expires_at={self.expires_at} reason={self.reason}>"


class SearchUser(PartialUser):

    __slots__ = "game_id", "name", "display_name", "language", "title", "thumbnail_url", "live", "started_at", "tag_ids"

    def __init__(self, http: HTTPHandler, data: dict):
        self._http = http
        self.display_name: str = data["display_name"]
        self.name: str = data["broadcaster_login"]
        self.id: int = int(data["id"])
        self.game_id: str = data["game_id"]
        self.title: str = data["title"]
        self.thumbnail_url: str = data["thumbnail_url"]
        self.language: str = data["broadcaster_language"]
        self.live: bool = data["is_live"]
        self.started_at = datetime.datetime.strptime(data["started_at"], "%Y-%m-%dT%H:%M:%SZ") if self.live else None
        self.tag_ids: list[str] = data["tag_ids"]


class User(PartialUser):

    __slots__ = (
        "_http",
        "id",
        "name",
        "display_name",
        "type",
        "broadcaster_type",
        "description",
        "profile_image",
        "offline_image",
        "view_count",
        "created_at",
        "email",
        "_cached_rewards",
    )

    def __init__(self, http: HTTPHandler, data: dict):
        self._http = http
        self.id = int(data["id"])
        self.name: str = data["login"]
        self.display_name: str = data["display_name"]
        self.type = UserTypeEnum(data["type"])
        self.broadcaster_type = BroadcasterTypeEnum(data["broadcaster_type"])
        self.description: str = data["description"]
        self.profile_image: str = data["profile_image_url"]
        self.offline_image: str = data["offline_image_url"]
        self.view_count: int = data["view_count"]
        self.created_at = parse_timestamp(data["created_at"])
        self.email: str | None = data.get("email")
        self._cached_rewards = None

    def __repr__(self) -> str:
        return f"<User id={self.id} name={self.name} display_name={self.display_name} type={self.type}>"
