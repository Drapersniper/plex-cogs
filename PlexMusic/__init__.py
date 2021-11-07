import asyncio

from redbot.core.bot import Red
from redbot.core.utils import get_end_user_data_statement

__red_end_user_data_statement__ = get_end_user_data_statement(__file__)

from .plex_music import PlexMusic


async def setup(bot: Red):
    cog = PlexMusic(bot)
    bot.add_cog(cog)
    asyncio.create_task(cog._init())
