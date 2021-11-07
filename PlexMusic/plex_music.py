import asyncio
import contextlib
import functools
import io
import logging
from collections import defaultdict
from typing import TYPE_CHECKING, Dict, Literal, Optional

import aiohttp
import discord
import plexapi
import plexapi.audio
import plexapi.exceptions
import plexapi.playlist
from async_timeout import timeout
from discord import FFmpegPCMAudio
from plexapi.library import MusicSection
from plexapi.myplex import MyPlexAccount
from plexapi.server import PlexServer
from redbot.core import Config, commands
from redbot.core.bot import Red

from .exceptions import MediaNotFoundError, VoiceChannelError

log = logging.getLogger("red.plex-cogs.PlexMusic")

try:
    import lyricsgenius
except ImportError:
    lyricsgenius = None

if TYPE_CHECKING:
    lyricsgenius: lyricsgenius

### Rewrite of https://github.com/jarulsamy/Plex-Bot/blob/master/PlexBot/bot.py to work with Red Bots


def check_if_lyrics_is_enabled(ctx: commands.Context):
    return ctx.cog.genius is not None


class PlexMusic(commands.Cog):
    """Play Audio content from Plex"""

    def __init__(self, bot: Red):
        """
        Initializes Plex resources
        Connects to Plex library and sets up
        all asynchronous communications.
        """

        self.bot = bot
        self.config = Config.get_conf(self, identifier=208903205982044161)
        self.config.register_user(username=None, token=None, url=None)
        self.config.register_global(username=None, token=None, url=None, lyricsgenius=None)
        self.pms_cache: Dict[int, PlexServer] = {}
        self.music_library: Dict[int, MusicSection] = {}
        self.cog_ready_event = asyncio.Event()
        self.session = aiohttp.ClientSession()

        # Initialize necessary vars
        self.voice_channel: Dict[int, discord.VoiceClient] = {}
        self.np_message_id: Dict[int, discord.Message] = {}
        self.current_track: Dict[int, plexapi.audio.Track] = {}
        self.ctx_cache: Dict[int, commands.Context] = {}
        self.play_queue: Dict[int, asyncio.Queue] = defaultdict(asyncio.Queue)
        self.play_next_event: Dict[int, asyncio.Event] = defaultdict(asyncio.Event)

        self.genius = None
        self._task = None

    def cog_unload(self):
        if self._task:
            self._task.cancel()
        asyncio.create_task(self.session.close())

    async def red_delete_data_for_user(
        self,
        *,
        requester: Literal["discord_deleted_user", "owner", "user", "user_strict"],
        user_id: int,
    ):
        """
        Method for finding users data inside the cog and deleting it.
        """
        await self.config.user_from_id(user_id).clear()
        if requester == "owner":
            await self.config.clear_all_globals()

    async def cog_before_invoke(self, ctx: commands.Context) -> None:
        await self.cog_ready_event.wait()
        await self.get_context_server(ctx)

    async def _maybe_auth(self, ctx: commands.Context):
        user_id = ctx.author.id
        if user_id not in self.pms_cache:
            user_data = await self.config.user(ctx.author).all()
            if not all(user_data.get(k) for k in ["token", "url"]):
                return
            url, token = user_data.get("url"), user_data.get("token")
            try:
                server = PlexServer(url, token)
                self.pms_cache[user_id] = PlexServer(url, token)
                self.music_library[user_id] = next(
                    iter(
                        section
                        for section in server.library.sections()
                        if section.type == "artist"
                    ),
                    None,
                )
            except:
                return
        elif user_id in self.pms_cache and user_id not in self.music_library:
            server = self.pms_cache[user_id]
            self.music_library[user_id] = next(
                iter(section for section in server.library.sections() if section.type == "artist"),
                None,
            )

    async def get_context_server(self, ctx: commands.Context) -> Optional[PlexServer]:
        await self._maybe_auth(ctx)
        default = self.pms_cache.get(self.bot.user.id, None)
        user = self.pms_cache.get(ctx.author.id, None)
        return user or default

    async def _init(self):
        await self.bot.wait_until_red_ready()
        await self._lyrics_genius_init()
        await self._init_global_plex()
        self._task = self._audio_player_task()
        self.bot.loop.create_task(self._task)
        self.cog_ready_event.set()

    async def _lyrics_genius_init(self, token: str = None):
        if lyricsgenius:
            if not token:
                token = await self.config.lyricsgenius()
            if token is not None:
                self.genius = lyricsgenius.Genius(token)  # FIXME: Blocking call
                return
        log.warning("No lyrics token specified, lyrics disabled")
        self.genius = None

    async def _init_global_plex(self, bot_id: int = None, username=None, token=None, url=None):
        try:
            if bot_id is None:
                bot_id = self.bot.user.id
                global_data = await self.config.all()
                username = global_data.get("username")
                token = global_data.get("token")
                url = global_data.get("url")
            if not all(i for i in [url, token, username, bot_id]):
                log.fatal(
                    "Missing global configuration make sure to run the following commands in DM "
                    f"'{next(iter(await self.bot.get_valid_prefixes()))}{self.command_config_global.qualified_name}' "
                    "to setup the global configuration."
                )
                return
            server = PlexServer(url, token)
            self.pms_cache[bot_id] = PlexServer(url, token)  # FIXME: Blocking call
            self.music_library[bot_id] = next(
                (
                    section for section in server.library.sections() if section.type == "artist"
                ),  # FIXME: Blocking call
                None,
            )

        except plexapi.exceptions.Unauthorized:
            log.fatal(
                "Invalid global Plex auth. Make sure to run the following commands in DM:  "
                f"'{next(iter(await self.bot.get_valid_prefixes()))}{self.command_config_global.qualified_name}' "
                "to setup the global configuration."
            )

    def _search_tracks(
        self, ctx: commands.Context, title: str, artist: str = None
    ) -> plexapi.audio.Track:
        """
        Search the Plex music db for track
        Args:
            title: str title of song to search for
        Returns:
            plexapi.audio.Track pointing to best matching title
        Raises:
            MediaNotFoundError: Title of track can't be found in plex db
        """
        if not (musiclib := self.music_library.get(ctx.author.id)):
            if not (musiclib := self.music_library.get(self.bot.user.id)):
                raise MediaNotFoundError("Track cannot be found")

        if artist:
            results = musiclib.searchTracks(
                title=title,
                maxresults=10,
                sort="titleSort",
                **{  # FIXME: Blocking call
                    "track.title": title,
                },
            )
            results = [
                r for r in results if r.artist().title.lower() == artist.lower()
            ]  # FIXME: Blocking call
        else:
            results = musiclib.searchTracks(
                title=title,
                sort="titleSort",
                maxresults=1,
                **{  # FIXME: Blocking call
                    "track.title": title,
                },
            )
        try:
            return results[0]
        except IndexError:
            raise MediaNotFoundError("Track cannot be found")

    def _search_albums(self, ctx: commands.Context, title: str) -> plexapi.audio.Album:
        """
        Search the Plex music db for album
        Args:
            title: str title of album to search for
        Returns:
            plexapi.audio.Album pointing to best matching title
        Raises:
            MediaNotFoundError: Title of album can't be found in plex db
        """
        if not (musiclib := self.music_library.get(ctx.author.id)):
            if not (musiclib := self.music_library.get(self.bot.user.id)):
                raise MediaNotFoundError("Track cannot be found")
        results = musiclib.searchAlbums(title=title, maxresults=1)  # FIXME: Blocking call
        try:
            return results[0]
        except IndexError:
            raise MediaNotFoundError("Album cannot be found")

    async def _search_playlists(
        self, ctx: commands.Context, title: str
    ) -> plexapi.playlist.Playlist:
        """
        Search the Plex music db for playlist
        Args:
            title: str title of playlist to search for
        Returns:
            plexapi.playlist.Playlist pointing to best matching title
        Raises:
            MediaNotFoundError: Title of playlist can't be found in plex db
        """
        try:
            server = await self.get_context_server(ctx)
            return server.playlist(title)  # FIXME: Blocking call
        except plexapi.exceptions.NotFound:
            raise MediaNotFoundError("Playlist cannot be found")

    async def _play(self, guild_id: int):
        """
        Heavy lifting of playing songs
        Grabs the appropriate streaming URL, sends the `now playing`
        message, and initiates playback in the vc.
        """
        track_url = self.current_track[guild_id].getStreamURL()  # FIXME: Blocking call
        audio_stream = FFmpegPCMAudio(track_url)

        while self.voice_channel[guild_id].is_playing():
            await asyncio.sleep(2)

        self.voice_channel[guild_id].play(
            audio_stream, after=functools.partial(self._toggle_next, guild_id=guild_id)
        )

        log.debug("%s - URL: %s", self.current_track[guild_id], track_url)

        embed, img = await self._build_embed_track(self.current_track[guild_id])
        if not embed:
            return
        self.np_message_id[guild_id] = await self.ctx_cache[guild_id].send(embed=embed, file=img)

    async def _audio_player_task(self):
        await self.bot.wait_until_red_ready()
        with contextlib.suppress(asyncio.CancelledError):
            while True:
                await asyncio.sleep(0.5)
                for k, voice_channel in self.voice_channel.items():
                    await asyncio.sleep(0.1)
                    self.play_next_event[k].clear()
                    if voice_channel:
                        try:
                            # Disconnect after 15 seconds idle
                            async with timeout(1):
                                self.current_track[k] = await self.play_queue[k].get()
                        except asyncio.TimeoutError:
                            continue
                    if not self.current_track.get(k):
                        self.current_track[k] = await self.play_queue[k].get()
                    await self._play(k)
                    if not self.play_next_event[k].is_set():
                        continue
                    with contextlib.suppress(discord.HTTPException):
                        await self.np_message_id[k].delete()

    def _toggle_next(self, error=None, guild_id: int = None):
        """
        Callback for vc playback
        Clears current track, then activates _audio_player_task
        to play next in queue or disconnect.
        """
        if guild_id is None:
            return
        self.current_track[guild_id] = None
        self.bot.loop.call_soon_threadsafe(self.play_next_event[guild_id].set)

    async def _build_embed_track(self, track: plexapi.audio.Track, type_="play"):
        """
        Creates a pretty embed card for tracks
        Builds a helpful status embed with the following info:
        Status, song title, album, artist and album art. All
        pertitent information is grabbed dynamically from the Plex db.
        Args:
            track: plexapi.audio.Track object of song
            type_: Type of card to make (play, queue).
        Returns:
            embed: discord.embed fully constructed payload.
            thumb_art: io.BytesIO of album thumbnail img.
        Raises:
            ValueError: Unsupported type of embed {type_}
        """
        # Grab the relevant thumbnail

        if not track:
            return None, None
        if not track.thumbUrl:
            log.warning(f"{track.title} does not have a thumbnail")
            art_file = None
        else:
            async with self.session.get(track.thumbUrl) as resp:
                img = io.BytesIO(await resp.read())
            # Attach to discord embed
            art_file = discord.File(img, filename="image0.png")
        # Get appropiate status message
        if type_ == "play":
            title = f"Now Playing - {track.title}"
        elif type_ == "queue":
            title = f"Added to queue - {track.title}"
        else:
            raise ValueError(f"Unsupported type of embed {type_}")

        # Include song details
        descrip = f"{track.album().title} - {track.artist().title}"  # FIXME: Blocking call

        # Build the actual embed
        embed = discord.Embed(title=title, description=descrip, colour=discord.Color.red())
        embed.set_author(name=self.bot.user.name)
        if art_file:
            # Point to file attached with ctx object.
            embed.set_thumbnail(url="attachment://image0.png")

        log.debug("Built embed for track - %s", track.title)

        return embed, art_file

    async def _build_embed_album(self, album: plexapi.audio.Album):
        """
        Creates a pretty embed card for albums
        Builds a helpful status embed with the following info:
        album, artist, and album art. All pertitent information
        is grabbed dynamically from the Plex db.
        Args:
            album: plexapi.audio.Album object of album
        Returns:
            embed: discord.embed fully constructed payload.
            thumb_art: io.BytesIO of album thumbnail img.
        Raises:
            None
        """
        # Grab the relevant thumbnail
        if not album:
            return None, None
        if not album.thumbUrl:
            log.warning(f"{album.title} does not have a thumbnail.")
            art_file = None
        else:
            async with self.session.get(album.thumbUrl) as resp:
                img = io.BytesIO(await resp.read())
            # Attach to discord embed
            art_file = discord.File(img, filename="image0.png")
        title = "Added album to queue"
        descrip = f"{album.title} - {album.artist().title}"  # FIXME: Blocking call

        embed = discord.Embed(title=title, description=descrip, colour=discord.Color.red())
        embed.set_author(name=self.bot.user.name)
        if art_file:
            embed.set_thumbnail(url="attachment://image0.png")
        log.debug("Built embed for album - %s", album.title)

        return embed, art_file

    async def _build_embed_playlist(
        self, ctx: commands.Context, playlist: plexapi.playlist.Playlist
    ):
        """"""
        # Grab the relevant thumbnail

        if not playlist:
            return None, None
        if not playlist.composite:
            log.debug(f"{playlist.title} does not have a composite image.")
            art_file = None
        else:
            server = await self.get_context_server(ctx)
            async with self.session.get(server.url(playlist.composite, True)) as resp:
                img = io.BytesIO(await resp.read())
            # Attach to discord embed
            art_file = discord.File(img, filename="image0.png")

        title = "Added playlist to queue"
        descrip = f"{playlist.title}"

        embed = discord.Embed(title=title, description=descrip, colour=discord.Color.red())
        embed.set_author(name=self.bot.user.name)
        if art_file:
            embed.set_thumbnail(url="attachment://image0.png")

        log.debug("Built embed for playlist - %s", playlist.title)

        return embed, art_file

    async def _validate(self, ctx: commands.Context):
        """
        Ensures user is in a vc
        Args:
            ctx: discord.ext.commands.Context message context from command
        Returns:
            None
        Raises:
            VoiceChannelError: Author not in voice channel
        """
        # Fail if user not in vc
        if not ctx.author.voice:
            await ctx.send("Join a voice channel first!")
            log.debug("Failed to play, requester not in voice channel")
            raise VoiceChannelError

        # Connect to voice if not already
        if not self.voice_channel.get(ctx.guild.id):
            if ctx.guild.me.voice:
                key_id, _ = ctx.guild.me.voice.channel._get_voice_client_key()
                state = ctx.guild.me.voice.channel._state
                if client := state._get_voice_client(key_id):
                    self.voice_channel[ctx.guild.id] = client
                    log.debug("Already connected to vc (%d).", self.voice_channel[ctx.guild.id])
                    return

            self.voice_channel[ctx.guild.id] = await ctx.author.voice.channel.connect()
            log.debug("Connected to vc (%d).", self.voice_channel[ctx.guild.id])

    @commands.group(name="config")
    async def command_config(self, ctx: commands.Context):
        """Setup commands."""

    @commands.dm_only()
    @command_config.command(name="auth")
    async def command_config_auth(
        self, ctx: commands.Context, plex_email: str, password: str, server_url: str
    ):
        """Provide your plex username and password to authenticate the session.

        This will only be used by the bot when you use commands to play,

        if you set this up it will ALWAYS play your queries from this server.

        Note: Your password will never be stored, only your username and authorization token.

        If you have 2 step verification setup place add the 6 digit code to the end of the password -

        For example if my password is "password" I would enter "password123456"
        """

        try:
            user = MyPlexAccount(username=plex_email, password=password)
        except plexapi.exceptions.Unauthorized:
            await ctx.send("Unable to complete authorization, please try again.")
            await ctx.send_help()
            return
        async with self.config.user(ctx.author).all() as user_data:
            user_data["username"] = user.email
            user_data["token"] = user.authenticationToken
            user_data["url"] = server_url
        await ctx.send(
            "Successfully authenticated as {user.email} ({user.username}).".format(user=user)
        )

    @command_config.command(name="reset")
    async def command_config_reset(self, ctx: commands.Context):
        """Reset the previously stored information provided by you.

        The bot will use the Global Plex server if provided by the bot owner.
        """
        await self.config.user(ctx.author).clear()
        await ctx.send("Cleared any and all user identifiable information.")

    @commands.is_owner()
    @commands.dm_only()
    @command_config.group(name="global")
    async def command_config_global(self, ctx: commands.Context):
        """Configure the global plex server and lyrics settings."""

    @command_config_global.command(name="auth")
    async def command_config_global_auth(
        self, ctx: commands.Context, plex_email: str, password: str, server_url: str
    ):
        """Set a Plex auth that will be used by users who haven't setup their own Plex details.

        This will only be used by the bot when anyone uses commands to play if they haven't setup their individual config,

        Note: Your password will never be stored, only your username and authorization token.

        If you have 2 step verification setup place add the 6 digit code to the end of the password -

        For example if my password is "password" I would enter "password123456"
        """
        try:
            user = MyPlexAccount(username=plex_email, password=password)
        except plexapi.exceptions.Unauthorized:
            await ctx.send("Unable to complete authorization, please try again.")
            await ctx.send_help()
            return
        async with self.config.all() as global_data:
            global_data["username"] = user.email
            global_data["token"] = user.authenticationToken
            global_data["url"] = server_url
        await self._init_global_plex(
            self.bot.user.id, user.email, user.authenticationToken, server_url
        )
        await ctx.send(
            "Successfully authenticated as {user.email} ({user.username}).".format(user=user)
        )

    @command_config_global.command(name="lyrics")
    async def command_config_global_lyrics(self, ctx: commands.Context, *, token: str):
        """Set a Lyrics Genius token -
        You can get one here: https://genius.com/signup_or_login
        """
        await self.config.lyricsgenius.set(token)
        await ctx.send(f"Token set to: {token}")
        await self._lyrics_genius_init(token)  # FIXME: Blocking call

    @commands.guild_only()
    @commands.command()
    async def play(self, ctx: commands.Context, title: str, artists: str = None):
        """
        User command to play song
        Searches plex and either, initiates playback, or
        adds to queue. Handles invalid usage from the user.
        Arguments:
            title: Title of song to play
            artists: The singers name
        """
        # Save the context to use with async callbacks
        self.ctx_cache[ctx.guild.id] = ctx

        try:
            track = self._search_tracks(ctx, title, artists)  # FIXME: Blocking call
        except MediaNotFoundError:
            await ctx.send(f"Can't find song: {title}")
            log.debug("Failed to play, can't find song - %s", title)
            return

        try:
            await self._validate(ctx)
        except VoiceChannelError:
            return

        # Specific add to queue message
        if self.voice_channel[ctx.guild.id].is_playing():
            log.debug("Added to queue - %s", title)
            embed, img = await self._build_embed_track(
                track, type_="queue"
            )  # FIXME: Blocking call
            if embed:
                await ctx.send(embed=embed, file=img)

        # Add the song to the async queue
        await self.play_queue[ctx.guild.id].put(track)

    @commands.guild_only()
    @commands.command()
    async def album(self, ctx: commands.Context, *, title: str):
        """
        User command to play song
        Searches plex db and either, initiates playback, or
        adds to queue. Handles invalid usage from the user.
        Arguments:
            title: Title of albumb to play
        """
        # Save the context to use with async callbacks
        self.ctx_cache[ctx.guild.id] = ctx

        try:
            album = self._search_albums(ctx, title)  # FIXME: Blocking call
        except MediaNotFoundError:
            await ctx.send(f"Can't find album: {title}")
            log.debug("Failed to queue album, can't find - %s", title)
            return

        try:
            await self._validate(ctx)
        except VoiceChannelError:
            return await ctx.send("First join a voice channel.")

        log.debug("Added to queue - %s", title)
        embed, img = await self._build_embed_album(album)  # FIXME: Blocking call
        if embed:
            await ctx.send(embed=embed, file=img)
        for track in album.tracks():
            await self.play_queue[ctx.guild.id].put(track)

    @commands.guild_only()
    @commands.command()
    async def playlist(self, ctx: commands.Context, *, title: str):
        """
        User command to play playlist
        Searches Plex and either, initiates playback, or
        adds to queue. Handles invalid usage from the user.

        Arguments:
            title: Title of playlist to play
        """

        try:
            playlist = await self._search_playlists(ctx, title)  # FIXME: Blocking call
        except MediaNotFoundError:
            await ctx.send(f"Can't find playlist: {title}")
            log.debug("Failed to queue playlist, can't find - %s", title)
            return

        try:
            await self._validate(ctx)
        except VoiceChannelError:
            return

        log.debug("Added to queue - %s", title)
        embed, img = await self._build_embed_playlist(ctx, playlist)  # FIXME: Blocking call
        if embed:
            await ctx.send(embed=embed, file=img)

        for item in playlist.items():
            if item.TYPE == "track":
                await self.play_queue[ctx.guild.id].put(item)

    @commands.guild_only()
    @commands.command()
    async def stop(self, ctx: commands.Context):
        """
        User command to stop playback
        Stops playback and disconnects from vc.
        """
        if vc := self.voice_channel.get(ctx.guild.id):
            vc.stop()
            await vc.disconnect()
            self.voice_channel[ctx.guild.id] = None
            log.debug("Stopped")
            await ctx.send(":stop_button: Stopped")

    @commands.guild_only()
    @commands.command()
    async def pause(self, ctx: commands.Context):
        """
        User command to pause playback
        Pauses playback, but doesn't reset anything
        to allow playback resuming.
        """
        if vc := self.voice_channel.get(ctx.guild.id):
            vc.pause()
            log.debug("Paused")
            await ctx.send(":play_pause: Paused")

    @commands.guild_only()
    @commands.command()
    async def resume(self, ctx: commands.Context):
        """
        User command to resume playback
        Args:
            ctx: discord.ext.commands.Context message context from command
        Returns:
            None
        Raises:
            None
        """
        if vc := self.voice_channel.get(ctx.guild.id):
            vc.resume()
            log.debug("Resumed")
            await ctx.send(":play_pause: Resumed")

    @commands.guild_only()
    @commands.command()
    async def skip(self, ctx: commands.Context):
        """
        User command to skip song in queue
        Skips currently playing song. If no other songs in
        queue, stops playback, otherwise moves to next song.
        """
        if vc := self.voice_channel.get(ctx.guild.id):
            vc.stop()
            log.debug("Skipped")
            self._toggle_next(ctx.guild.id)

    @commands.guild_only()
    @commands.command(name="np")
    async def now_playing(self, ctx: commands.Context):
        """
        User command to get currently playing song.
        Deletes old `now playing` status message,
        Creates a new one with up to date information.
        """
        if track := self.current_track.get(ctx.guild.id):
            embed, img = await self._build_embed_track(track)  # FIXME: Blocking call
            if not embed:
                return
            log.debug("Now playing")
            if np_message_id := self.np_message_id.get(ctx.guild.id):
                await np_message_id.delete()
                log.debug("Deleted old np status")
            log.debug("Created np status")
            self.np_message_id[ctx.guild.id] = await ctx.send(embed=embed, file=img)

    @commands.guild_only()
    @commands.command()
    async def clear(self, ctx: commands.Context):
        """
        User command to clear play queue.
        """
        self.play_queue[ctx.guild.id] = asyncio.Queue()
        log.debug("Cleared queue")
        await ctx.send(":boom: Queue cleared.")

    @commands.check(check_if_lyrics_is_enabled)
    @commands.guild_only()
    @commands.command()
    async def lyrics(self, ctx: commands.Context):
        """User command to get lyrics of a song."""
        if (track := self.current_track.get(ctx.guild.id)) is None:
            await ctx.send("No song currently playing.")
            return

        if self.genius:
            await ctx.send(
                f"Searching for {track.title}, {track.artist().title}."
            )  # FIXME: Blocking call
            try:
                song = self.genius.search_song(
                    track.title, track.artist().title
                )  # FIXME: Blocking call
            except TypeError:
                self.genius = None
                await ctx.send(f"Lyrics extension is currently disabled.")
                return

            try:
                lyrics = song.lyrics
                # Split into 1950 char chunks
                # Discord max message length is 2000
                lines = [(lyrics[i : i + 1950]) for i in range(0, len(lyrics), 1950)]

                for i in lines:
                    if i == "":
                        continue
                    # Apply code block format
                    i = f"```{i}```"
                    await ctx.send(i)

            except (IndexError, TypeError):
                await ctx.send("Can't find lyrics for this song.")
        else:
            await ctx.send(f"Lyrics extension is currently disabled.")
