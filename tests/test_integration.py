import json
from typing import Any

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import TextContent


def _extract_text(content: list[Any]) -> list[str]:
    """Extract text from MCP content items, filtering to TextContent only.

    Note: Our MCP server returns all data as JSON in TextContent. Image attachments
    are included as URLs within the JSON payload, not as separate ImageContent items.
    """
    return [item.text for item in content if isinstance(item, TextContent)]


def _make_server_params(real_config) -> StdioServerParameters:
    """Create MCP server parameters with Discord credentials."""
    return StdioServerParameters(
        command="uv",
        args=["run", "python", "main.py"],
        env={
            "DISCORD_EMAIL": real_config.email,
            "DISCORD_PASSWORD": real_config.password,
            "DISCORD_HEADLESS": "true",
        },
    )


def _check_error(result):
    """Check if result is an error and raise if so."""
    if result.isError:
        text_content = result.content[0] if result.content else None
        error_text = text_content.text if text_content else "Unknown error"
        raise Exception(f"Tool failed: {error_text[:200]}...")


@pytest.mark.integration
@pytest.mark.browser
@pytest.mark.slow
@pytest.mark.asyncio
async def test_mcp_get_servers_tool(real_config):
    """Test the get_servers MCP tool via proper MCP client."""
    server_params = _make_server_params(real_config)

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            result = await session.call_tool("get_servers", {})
            assert hasattr(result, "content")
            assert result.content, "No content in result"
            _check_error(result)

            servers_data = [json.loads(text) for text in _extract_text(result.content)]

            assert isinstance(servers_data, list)
            assert len(servers_data) > 0
            print(f"MCP server found {len(servers_data)} guilds")

            for i, server in enumerate(servers_data):
                print(f"Server {i + 1}: {server['name']} (ID: {server['id']})")

            assert servers_data[0]["id"] is not None
            assert servers_data[0]["name"] is not None


@pytest.mark.integration
@pytest.mark.browser
@pytest.mark.slow
@pytest.mark.asyncio
async def test_mcp_get_channels_tool(real_config, test_env):
    """Test the get_channels MCP tool via proper MCP client."""
    server_params = _make_server_params(real_config)

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            result = await session.call_tool(
                "get_channels", {"server_id": test_env.server_id}
            )
            assert hasattr(result, "content")
            assert result.content, "No content in result"
            _check_error(result)

            channels_data = [json.loads(text) for text in _extract_text(result.content)]

            assert isinstance(channels_data, list)
            assert len(channels_data) > 0, (
                f"Expected to find channels in server {test_env.server_id}, but found 0"
            )
            print(
                f"MCP found {len(channels_data)} channels in server {test_env.server_id}"
            )

            for channel_info in channels_data:
                assert "id" in channel_info
                assert "name" in channel_info
                assert "type" in channel_info
                print(f"  {channel_info['name']} (ID: {channel_info['id']})")


@pytest.mark.integration
@pytest.mark.browser
@pytest.mark.slow
@pytest.mark.asyncio
async def test_mcp_send_message_tool(real_config, test_env):
    """Test the send_message MCP tool via proper MCP client."""
    server_params = _make_server_params(real_config)
    test_message = "hello"

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            print(
                f"Testing MCP message sending to server {test_env.server_id}, channel {test_env.channel_id}"
            )
            result = await session.call_tool(
                "send_message",
                {
                    "server_id": test_env.server_id,
                    "channel_id": test_env.channel_id,
                    "content": test_message,
                },
            )
            assert hasattr(result, "content")
            assert result.content, "No content in result"
            _check_error(result)

            response_data = json.loads(_extract_text(result.content)[0])

            assert isinstance(response_data, dict)
            assert "message_ids" in response_data
            assert "status" in response_data
            assert "chunks" in response_data
            assert response_data["status"] == "sent"
            assert len(response_data["message_ids"]) >= 1
            print(
                f"MCP successfully sent {response_data['chunks']} message(s) with IDs: {response_data['message_ids']}"
            )


@pytest.mark.integration
@pytest.mark.browser
@pytest.mark.slow
@pytest.mark.asyncio
async def test_mcp_read_messages_tool(real_config, test_env):
    """Test the read_messages MCP tool via proper MCP client."""
    server_params = _make_server_params(real_config)

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            print(
                f"Testing MCP message reading from server {test_env.server_id}, channel {test_env.channel_id}"
            )
            result = await session.call_tool(
                "read_messages",
                {
                    "server_id": test_env.server_id,
                    "channel_id": test_env.channel_id,
                    "hours_back": 8760,
                    "max_messages": 20,
                },
            )
            assert hasattr(result, "content")
            assert result.content, "No content in result - expected to find messages"
            _check_error(result)

            messages_data = [json.loads(text) for text in _extract_text(result.content)]
            assert isinstance(messages_data, list)

            print(
                f"\n=== MCP read {len(messages_data)} messages from channel {test_env.channel_id} ==="
            )
            for i, msg in enumerate(messages_data, 1):
                print(f"\nMessage {i}:")
                print(f"  ID: {msg.get('id', 'Unknown')}")
                print(f"  Author: {msg.get('author_name', 'Unknown')}")
                print(f"  Timestamp: {msg.get('timestamp', 'Unknown')}")
                content = msg.get("content", "")
                print(
                    f"  Content: {content[:100]}{'...' if len(content) > 100 else ''}"
                )
                print(f"  Attachments: {len(msg.get('attachments', []))} files")
            print("=" * 50)

            for msg in messages_data:
                assert "id" in msg
                assert "content" in msg
                assert "author_name" in msg
                assert "timestamp" in msg
                assert "attachments" in msg


@pytest.mark.integration
@pytest.mark.browser
@pytest.mark.slow
@pytest.mark.asyncio
async def test_mcp_search_messages_tool(real_config, test_env):
    """Test the search_messages MCP tool via proper MCP client."""
    server_params = _make_server_params(real_config)

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            print(
                f"Testing MCP search for '{test_env.search_query}' in {test_env.search_channel}"
            )
            result = await session.call_tool(
                "search_messages",
                {
                    "server_id": test_env.server_id,
                    "query": test_env.search_query,
                    "in_channels": [test_env.search_channel],
                    "max_results": 10,
                },
            )
            assert hasattr(result, "content")
            assert result.content, "No content in result - expected search results"
            _check_error(result)

            messages_data = [json.loads(text) for text in _extract_text(result.content)]

            print(f"\n=== MCP search found {len(messages_data)} results ===")
            for i, msg in enumerate(messages_data, 1):
                print(f"\nResult {i}:")
                print(f"  Author: {msg.get('author_name', 'Unknown')}")
                content = msg.get("content", "")
                print(f"  Content: {content[:100]}...")

            assert len(messages_data) > 0, "Expected at least one search result"
            for msg in messages_data:
                assert "id" in msg
                assert "content" in msg
                assert "author_name" in msg
                assert "timestamp" in msg


@pytest.mark.integration
@pytest.mark.browser
@pytest.mark.slow
@pytest.mark.asyncio
async def test_mcp_get_search_result_context_tool(real_config, test_env):
    """Test the get_search_result_context MCP tool via proper MCP client."""
    server_params = _make_server_params(real_config)

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            print(
                f"Testing MCP get_search_result_context for '{test_env.search_query}' in {test_env.search_channel}"
            )
            result = await session.call_tool(
                "get_search_result_context",
                {
                    "server_id": test_env.server_id,
                    "query": test_env.search_query,
                    "result_index": 0,
                    "before_count": 3,
                    "after_count": 3,
                    "in_channels": [test_env.search_channel],
                },
            )
            assert hasattr(result, "content")
            assert result.content, "No content in result"
            _check_error(result)

            context_data = json.loads(_extract_text(result.content)[0])

            print("\n=== MCP get_search_result_context result ===")
            print(f"Found: {context_data.get('found')}")
            print(
                f"Channel: {context_data.get('channel_name')} ({context_data.get('channel_id')})"
            )

            if context_data.get("found"):
                target = context_data.get("target_message", {})
                print("\nTarget message:")
                print(f"  Author: {target.get('author_name')}")
                content = target.get("content", "")
                print(f"  Content: {content[:100]}...")

                before = context_data.get("messages_before", [])
                after = context_data.get("messages_after", [])
                print(f"\nMessages before: {len(before)}")
                print(f"Messages after: {len(after)}")

                assert "target_message" in context_data
                assert "messages_before" in context_data
                assert "messages_after" in context_data
                assert context_data["target_message"]["id"]
            else:
                pytest.fail(
                    f"get_search_result_context failed: {context_data.get('error')}"
                )
