import pytest
import pytest_asyncio
import asyncio
import os
from dataclasses import dataclass
from dotenv import load_dotenv
from src.discord_mcp.client import create_client_state, close_client
from src.discord_mcp.config import DiscordConfig

load_dotenv()


@pytest.fixture(scope="session")
def event_loop():
    """Create an instance of the default event loop for the test session."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def real_config():
    """Provide real Discord configuration from environment."""
    email = os.getenv("DISCORD_EMAIL")
    password = os.getenv("DISCORD_PASSWORD")

    if not email or not password:
        pytest.skip(
            "Discord credentials not available. Set DISCORD_EMAIL and DISCORD_PASSWORD environment variables."
        )

    return DiscordConfig(
        email=email,
        password=password,
        headless=True,
        default_guild_ids=[],
        max_messages_per_channel=50,
        default_hours_back=24,
    )


@dataclass
class TestEnv:
    """Test environment configuration: which server/channel/query to test against."""

    server_id: str
    channel_id: str
    search_query: str
    search_channel: str


@pytest.fixture
def test_env():
    """Provide test environment configuration from environment."""
    server_id = os.getenv("TEST_SERVER_ID")
    channel_id = os.getenv("TEST_CHANNEL_ID")
    search_query = os.getenv("TEST_SEARCH_QUERY", "test")
    search_channel = os.getenv("TEST_SEARCH_CHANNEL", "general")

    if not server_id or not channel_id:
        pytest.skip(
            "Test environment not configured. Set TEST_SERVER_ID and TEST_CHANNEL_ID environment variables."
        )

    return TestEnv(
        server_id=server_id,
        channel_id=channel_id,
        search_query=search_query,
        search_channel=search_channel,
    )


@pytest_asyncio.fixture
async def discord_client(real_config):
    """Provide a real Discord client for integration testing."""
    client_state = create_client_state(
        email=real_config.email,
        password=real_config.password,
        headless=real_config.headless,
    )

    yield client_state

    # Cleanup
    await close_client(client_state)


@pytest.fixture(autouse=True)
def setup_test_environment():
    """Setup test environment before each test."""
    # Ensure we're in headless mode for CI testing
    os.environ["DISCORD_HEADLESS"] = "true"
    yield
