import asyncio
import typing as tp
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from .logger import logger
from .client import (
    create_client_state,
    get_guilds,
    get_guild_channels,
    send_message as send_discord_message,
    search_messages as search_discord_messages,
    get_search_result_context as get_discord_message_context,
    close_client,
)
from .config import load_config
from .messages import read_recent_messages


@dataclass
class DiscordContext:
    config: tp.Any
    client_lock: asyncio.Lock


@asynccontextmanager
async def discord_lifespan(server: FastMCP) -> AsyncIterator[DiscordContext]:
    config = load_config()
    client_lock = asyncio.Lock()
    logger.debug("Discord MCP server starting up")
    try:
        yield DiscordContext(config=config, client_lock=client_lock)
    finally:
        logger.debug("Discord MCP server shutting down")


async def _execute_with_fresh_client[T](
    discord_ctx: DiscordContext,
    operation: Callable[[tp.Any], tp.Awaitable[tuple[tp.Any, T]]],
) -> T:
    """Execute Discord operation with fresh client state"""
    async with discord_ctx.client_lock:
        client_state = create_client_state(
            discord_ctx.config.email,
            discord_ctx.config.password,
            True,
            discord_ctx.config.extra_wait_ms,
        )
        try:
            _, result = await operation(client_state)
            return result
        finally:
            logger.debug("Cleaning up browser resources")
            await close_client(client_state)


mcp = FastMCP("discord-mcp", lifespan=discord_lifespan)


@mcp.tool(
    description="List all Discord servers you have access to",
    annotations=ToolAnnotations(
        title="List Discord Servers", readOnlyHint=True, destructiveHint=False
    ),
)
async def get_servers() -> list[dict[str, str]]:
    """List all Discord servers you have access to.

    Returns:
        List of server objects with id and name fields
    """
    ctx = mcp.get_context()
    discord_ctx = tp.cast(DiscordContext, ctx.request_context.lifespan_context)

    guilds = await _execute_with_fresh_client(discord_ctx, get_guilds)
    return [{"id": g.id, "name": g.name} for g in guilds]


@mcp.tool(
    description="List all channels in a specific Discord server",
    annotations=ToolAnnotations(
        title="List Server Channels", readOnlyHint=True, destructiveHint=False
    ),
)
async def get_channels(server_id: str) -> list[dict[str, str]]:
    """List all channels in a specific Discord server.

    Args:
        server_id: Discord server ID

    Returns:
        List of channel objects with id, name, and type fields
    """
    ctx = mcp.get_context()
    discord_ctx = tp.cast(DiscordContext, ctx.request_context.lifespan_context)

    async def operation(state):
        return await get_guild_channels(state, server_id)

    channels = await _execute_with_fresh_client(discord_ctx, operation)
    return [{"id": c.id, "name": c.name, "type": str(c.type)} for c in channels]


@mcp.tool(
    description="Read recent messages from a specific channel",
    annotations=ToolAnnotations(
        title="Read Channel Messages", readOnlyHint=True, destructiveHint=False
    ),
)
async def read_messages(
    server_id: str, channel_id: str, max_messages: int, hours_back: int = 24
) -> list[dict[str, tp.Any]]:
    """Read recent messages from a specific channel.

    Args:
        server_id: Discord server ID
        channel_id: Channel ID to read from
        max_messages: Maximum number of messages to retrieve (1-1000)
        hours_back: How many hours back to search (default 24, max 8760)

    Returns:
        List of message objects with id, content, author_name, timestamp, and attachments
    """
    if not (1 <= hours_back <= 8760):
        raise ValueError("hours_back must be between 1 and 8760 (1 year)")
    if not (1 <= max_messages <= 1000):
        raise ValueError("max_messages must be between 1 and 1000")

    ctx = mcp.get_context()
    discord_ctx = tp.cast(DiscordContext, ctx.request_context.lifespan_context)

    async def operation(state):
        return await read_recent_messages(
            state, server_id, channel_id, hours_back, max_messages
        )

    messages = await _execute_with_fresh_client(discord_ctx, operation)
    return [
        {
            "id": m.id,
            "content": m.content,
            "author_name": m.author_name,
            "timestamp": m.timestamp.isoformat(),
            "attachments": m.attachments,
        }
        for m in messages
    ]


@mcp.tool(
    description="Send a message to a specific Discord channel. Long messages are automatically split.",
    annotations=ToolAnnotations(
        title="Send Message", readOnlyHint=False, destructiveHint=True
    ),
)
async def send_message(
    server_id: str, channel_id: str, content: str
) -> dict[str, tp.Any]:
    """Send a message to a specific Discord channel. Long messages are automatically split.

    Args:
        server_id: Discord server ID
        channel_id: Channel ID to send message to
        content: Message content (automatically splits if >2000 characters)

    Returns:
        Object with message_ids, status, chunks count, and total_length
    """
    if len(content) == 0:
        raise ValueError("Message content cannot be empty")

    # Split long messages into chunks of 2000 characters or less
    chunks = []
    if len(content) <= 2000:
        chunks = [content]
    else:
        # Split by newlines first to avoid breaking paragraphs
        lines = content.split("\n")
        current_chunk = ""

        for line in lines:
            # If single line is too long, split it by words
            if len(line) > 2000:
                words = line.split(" ")
                current_line = ""
                for word in words:
                    if len(current_line + " " + word) <= 2000:
                        current_line += (" " + word) if current_line else word
                    else:
                        if current_line:
                            if len(current_chunk + "\n" + current_line) <= 2000:
                                current_chunk += (
                                    ("\n" + current_line)
                                    if current_chunk
                                    else current_line
                                )
                            else:
                                chunks.append(current_chunk)
                                current_chunk = current_line
                            current_line = word
                        else:
                            # Single word too long, truncate it
                            current_line = word[:2000]
                if current_line:
                    if len(current_chunk + "\n" + current_line) <= 2000:
                        current_chunk += (
                            ("\n" + current_line) if current_chunk else current_line
                        )
                    else:
                        chunks.append(current_chunk)
                        current_chunk = current_line
            else:
                # Normal line length
                if len(current_chunk + "\n" + line) <= 2000:
                    current_chunk += ("\n" + line) if current_chunk else line
                else:
                    chunks.append(current_chunk)
                    current_chunk = line

        if current_chunk:
            chunks.append(current_chunk)

    ctx = mcp.get_context()
    discord_ctx = tp.cast(DiscordContext, ctx.request_context.lifespan_context)

    message_ids = []
    for i, chunk in enumerate(chunks):

        async def operation(state, chunk_content=chunk):
            return await send_discord_message(
                state, server_id, channel_id, chunk_content
            )

        message_id = await _execute_with_fresh_client(discord_ctx, operation)
        message_ids.append(message_id)

        # Small delay between messages to avoid rate limiting
        if i < len(chunks) - 1:
            await asyncio.sleep(0.5)

    return {
        "message_ids": message_ids,
        "status": "sent",
        "chunks": len(chunks),
        "total_length": len(content),
    }


@mcp.tool(
    description="Search for messages in a Discord server with filters for channels, users, dates, and content types",
    annotations=ToolAnnotations(
        title="Search Messages", readOnlyHint=True, destructiveHint=False
    ),
)
async def search_messages(
    server_id: str,
    query: str = "",
    in_channels: list[str] | None = None,
    from_users: list[str] | None = None,
    mentions_users: list[str] | None = None,
    has_filters: list[str] | None = None,
    before: str | None = None,
    after: str | None = None,
    during: str | None = None,
    author_type: str | None = None,
    pinned: bool | None = None,
    page: int = 1,
    max_results: int = 25,
) -> list[dict[str, tp.Any]]:
    """Search for messages in a Discord server with filters for channels, users, dates, and content types.

    Args:
        server_id: Discord server/guild ID
        query: Search text content to find
        in_channels: Channel names to search in (e.g., ["general", "memes"])
        from_users: Usernames to filter by author (e.g., ["alice", "bob"])
        mentions_users: Usernames to filter by mentions
        has_filters: Content type filters - can combine multiple (image, video, link, file, embed)
        before: Date filter YYYY-MM-DD (messages before this date)
        after: Date filter YYYY-MM-DD (messages after this date)
        during: Date filter YYYY-MM-DD (messages on this specific date)
        author_type: Filter by author type (user, bot, webhook)
        pinned: If True, only search pinned messages
        page: Page number of results (1-indexed, default 1)
        max_results: Maximum number of results per page (1-100, default 25)
    """
    if not query.strip() and not any(
        [
            in_channels,
            from_users,
            mentions_users,
            has_filters,
            before,
            after,
            during,
            author_type,
            pinned,
        ]
    ):
        raise ValueError("Must provide query text or at least one filter")
    if not (1 <= max_results <= 100):
        raise ValueError("max_results must be between 1 and 100")
    if page < 1:
        raise ValueError("page must be at least 1")

    valid_has = {"image", "video", "link", "file", "embed"}
    if has_filters and not all(h in valid_has for h in has_filters):
        raise ValueError(f"has_filters must be from: {valid_has}")
    if author_type and author_type not in ("user", "bot", "webhook"):
        raise ValueError("author_type must be one of: user, bot, webhook")

    ctx = mcp.get_context()
    discord_ctx = tp.cast(DiscordContext, ctx.request_context.lifespan_context)

    async def operation(state):
        return await search_discord_messages(
            state,
            server_id=server_id,
            query=query,
            in_channels=in_channels,
            from_users=from_users,
            mentions_users=mentions_users,
            has_filters=has_filters,
            before=before,
            after=after,
            during=during,
            author_type=author_type,
            pinned=pinned,
            page=page,
            limit=max_results,
        )

    messages = await _execute_with_fresh_client(discord_ctx, operation)
    return [
        {
            "id": m.id,
            "content": m.content,
            "author_name": m.author_name,
            "timestamp": m.timestamp.isoformat(),
            "attachments": m.attachments,
        }
        for m in messages
    ]


@mcp.tool(
    description="Jump to a search result and get surrounding message context for conversation analysis",
    annotations=ToolAnnotations(
        title="Get Search Result Context", readOnlyHint=True, destructiveHint=False
    ),
)
async def get_search_result_context(
    server_id: str,
    query: str,
    result_index: int = 0,
    before_count: int = 5,
    after_count: int = 5,
    in_channels: list[str] | None = None,
    from_users: list[str] | None = None,
    page: int = 1,
) -> dict[str, tp.Any]:
    """Jump to a search result and get surrounding message context for conversation analysis.

    Searches for messages, clicks "Jump" on the specified result, and extracts
    messages before and after the target message for conversation context.

    Args:
        server_id: Discord server/guild ID
        query: Search query to find the target message
        result_index: Which search result to jump to (0-indexed, default 0 = first result)
        before_count: Number of messages to get before target (default 5)
        after_count: Number of messages to get after target (default 5)
        in_channels: Optional channel names to filter search
        from_users: Optional usernames to filter search by author
        page: Search results page number (default 1)
    """
    if not query.strip():
        raise ValueError("Query cannot be empty")
    if result_index < 0:
        raise ValueError("result_index must be >= 0")
    if before_count < 0 or after_count < 0:
        raise ValueError("before_count and after_count must be >= 0")
    if page < 1:
        raise ValueError("page must be >= 1")

    ctx = mcp.get_context()
    discord_ctx = tp.cast(DiscordContext, ctx.request_context.lifespan_context)

    async def operation(state):
        return await get_discord_message_context(
            state,
            server_id=server_id,
            query=query,
            result_index=result_index,
            before_count=before_count,
            after_count=after_count,
            in_channels=in_channels,
            from_users=from_users,
            page=page,
        )

    context = await _execute_with_fresh_client(discord_ctx, operation)

    if context is None:
        return {"error": "Could not get message context", "found": False}

    def msg_to_dict(m):
        return {
            "id": m.id,
            "content": m.content,
            "author_name": m.author_name,
            "timestamp": m.timestamp.isoformat(),
        }

    return {
        "found": True,
        "channel_name": context.channel_name,
        "channel_id": context.channel_id,
        "target_message": msg_to_dict(context.target_message),
        "messages_before": [msg_to_dict(m) for m in context.messages_before],
        "messages_after": [msg_to_dict(m) for m in context.messages_after],
    }


def main():
    mcp.run()


if __name__ == "__main__":
    main()
