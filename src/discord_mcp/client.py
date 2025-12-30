import asyncio
import pathlib as pl
from datetime import datetime, timezone
import dataclasses as dc
from playwright.async_api import async_playwright, Browser, Page, Playwright
from .logger import logger


@dc.dataclass(frozen=True)
class DiscordMessage:
    id: str
    content: str
    author_name: str
    author_id: str
    channel_id: str
    timestamp: datetime
    attachments: list[str]


@dc.dataclass(frozen=True)
class DiscordChannel:
    id: str
    name: str
    type: int
    guild_id: str | None


@dc.dataclass(frozen=True)
class DiscordGuild:
    id: str
    name: str
    icon: str | None = None


@dc.dataclass(frozen=True)
class ClientState:
    email: str
    password: str
    headless: bool = True
    extra_wait_ms: int = 0
    playwright: Playwright | None = None
    browser: Browser | None = None
    context: object | None = None  # BrowserContext
    page: Page | None = None
    logged_in: bool = False
    cookies_file: pl.Path = dc.field(
        default_factory=lambda: pl.Path.home() / ".discord_mcp_cookies.json"
    )


def create_client_state(
    email: str, password: str, headless: bool = True, extra_wait_ms: int = 0
) -> ClientState:
    return ClientState(
        email=email, password=password, headless=headless, extra_wait_ms=extra_wait_ms
    )


async def _ensure_browser(state: ClientState) -> ClientState:
    if state.playwright and state.browser and state.context and state.page:
        return state

    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(headless=state.headless)

    ctx_kwargs = {}
    if state.cookies_file.exists():
        ctx_kwargs["storage_state"] = str(state.cookies_file)
    context = await browser.new_context(**ctx_kwargs)
    page = await context.new_page()

    return dc.replace(
        state, playwright=playwright, browser=browser, context=context, page=page
    )


async def _save_storage_state(state: ClientState) -> None:
    if state.page:
        await state.page.context.storage_state(path=str(state.cookies_file))


async def _check_logged_in(state: ClientState) -> bool:
    if not state.page:
        return False
    try:
        await state.page.goto(
            "https://discord.com/channels/@me", wait_until="domcontentloaded"
        )
        await state.page.wait_for_selector(
            '[data-list-id="guildsnav"] [role="treeitem"]',
            state="visible",
            timeout=15000,
        )

        url = state.page.url
        if (
            any(path in url for path in ["/login", "/register"])
            or "/channels/@me" not in url
        ):
            return False

        return bool(
            await state.page.query_selector(
                '[data-list-id="guildsnav"] [role="treeitem"]'
            )
        )
    except Exception:
        return False


async def _login(state: ClientState) -> ClientState:
    if state.logged_in:
        return state

    state = await _ensure_browser(state)
    if not state.page:
        raise RuntimeError("Browser page not initialized")

    if await _check_logged_in(state):
        return dc.replace(state, logged_in=True)

    await state.page.goto("https://discord.com/login")
    await asyncio.sleep(2)

    await state.page.fill('input[name="email"]', state.email)
    await state.page.fill('input[name="password"]', state.password)
    await state.page.click('button[type="submit"]')

    try:
        await state.page.wait_for_function(
            "() => !window.location.href.includes('/login')", timeout=60000
        )
        await asyncio.sleep(1 + state.extra_wait_ms / 1000)

        if (
            "/verify" in state.page.url
            or await state.page.locator('text="Check your email"').count()
        ):
            await state.page.wait_for_function(
                "() => window.location.href.includes('/channels/')", timeout=120000
            )

        if await _check_logged_in(state):
            was_logged_in = state.logged_in
            state = dc.replace(state, logged_in=True)
            await asyncio.sleep(5)
            if state.page:
                await state.page.goto("https://discord.com/channels/@me")
            await asyncio.sleep(3)

            if not was_logged_in:
                await _save_storage_state(state)
            return state
        else:
            raise RuntimeError("Login appeared to succeed but verification failed")
    except Exception as e:
        raise RuntimeError(f"Failed to login to Discord: {e}")


async def close_client(state: ClientState) -> None:
    # Close resources in reverse order: page -> context -> browser -> playwright
    resources = [
        (state.page, "close"),
        (state.context, "close"),
        (state.browser, "close"),
        (state.playwright, "stop"),
    ]

    for resource, action in resources:
        try:
            if resource:
                await getattr(resource, action)()
        except Exception:
            pass

    # Force garbage collection to help cleanup
    import gc

    gc.collect()


async def get_guilds(state: ClientState) -> tuple[ClientState, list[DiscordGuild]]:
    state = await _login(state)
    if not state.page:
        raise RuntimeError("Browser page not initialized")

    logger.debug("Starting guild detection process")
    await state.page.goto(
        "https://discord.com/channels/@me", wait_until="domcontentloaded"
    )
    logger.debug(f"Navigated to Discord, current URL: {state.page.url}")

    # Wait for Discord to fully load guilds with text content
    try:
        await state.page.wait_for_selector(
            '[data-list-id="guildsnav"] [role="treeitem"]',
            state="visible",
            timeout=15000,
        )
        await state.page.wait_for_timeout(1000 + state.extra_wait_ms)

        # Scroll guild navigation to load all guilds
        await state.page.evaluate("""
            () => {
                const guildNav = document.querySelector('[data-list-id="guildsnav"]');
                const container = guildNav?.closest('[class*="guilds"]') || guildNav?.parentElement;
                if (container) {
                    container.scrollTop = 0;
                    return new Promise(resolve => {
                        let scrolls = 0;
                        const interval = setInterval(() => {
                            container.scrollBy(0, 100);
                            if (++scrolls >= 20 || container.scrollTop + container.clientHeight >= container.scrollHeight - 10) {
                                clearInterval(interval);
                                resolve();
                            }
                        }, 100);
                    });
                }
            }
        """)
        await state.page.wait_for_timeout(500 + state.extra_wait_ms)
    except Exception:
        pass

    # Extract guild information from navigation elements
    guilds_data = await state.page.evaluate("""
        () => {
            const guilds = [];
            const treeItems = document.querySelectorAll('[data-list-id="guildsnav"] [role="treeitem"]');
            
            treeItems.forEach(item => {
                const listItemId = item.getAttribute('data-list-item-id');
                if (listItemId?.startsWith('guildsnav___') && listItemId !== 'guildsnav___home') {
                    const guildId = listItemId.replace('guildsnav___', '');
                    if (/^[0-9]+$/.test(guildId)) {
                        // Extract guild name from tree item text
                        let guildName = null;
                        const textElements = item.querySelectorAll('*');
                        for (let elem of textElements) {
                            const text = elem.textContent?.trim();
                            if (text && text.length > 2 && text.length < 100 && 
                                !text.includes('notification') && !text.includes('unread') &&
                                !text.match(/^\\d+$/)) {
                                guildName = text;
                                break;
                            }
                        }
                        
                        if (!guildName) {
                            const fullText = item.textContent?.trim();
                            if (fullText) {
                                guildName = fullText.replace(/^\\d+\\s+mentions?,\\s*/, '').replace(/\\s+/g, ' ').trim();
                            }
                        }
                        
                        // Clean up mention prefixes
                        if (guildName) {
                            guildName = guildName.replace(/^\\d+\\s+mentions?,\\s*/, '').trim();
                        }
                        
                        if (guildName && !guilds.some(g => g.id === guildId)) {
                            guilds.push({ id: guildId, name: guildName });
                        }
                    }
                }
            });
            
            return guilds;
        }
    """)

    # Convert JavaScript results to DiscordGuild objects
    guilds = [
        DiscordGuild(id=guild_data["id"], name=guild_data["name"], icon=None)
        for guild_data in guilds_data
    ]

    return state, guilds


async def get_guild_channels(
    state: ClientState, guild_id: str
) -> tuple[ClientState, list[DiscordChannel]]:
    state = await _login(state)
    if not state.page:
        raise RuntimeError("Browser page not initialized")

    await state.page.goto(
        f"https://discord.com/channels/{guild_id}", wait_until="domcontentloaded"
    )
    await state.page.wait_for_timeout(1000 + state.extra_wait_ms)

    # Helper function to extract channels
    def extract_channels_js() -> str:
        return f"""
            (() => {{
                const channels = [];
                const seenIds = new Set();
                const links = document.querySelectorAll('a[href*="/channels/"]');
                
                links.forEach(link => {{
                    const match = link.href.match(/\\/channels\\/{guild_id}\\/([0-9]+)/);
                    if (match) {{
                        const channelId = match[1];
                        if (!seenIds.has(channelId)) {{
                            seenIds.add(channelId);
                            let name = link.textContent?.trim() || '';
                            name = name.replace(/^[^a-zA-Z0-9#-_]+/, '').trim();
                            name = name.replace(/\\s+/g, ' ').trim();
                            channels.push({{
                                id: channelId,
                                name: name || `channel-${{channelId}}`,
                                href: link.href
                            }});
                        }}
                    }}
                }});
                return channels;
            }})()
        """

    # Step 1: Get original channels
    logger.debug("Getting original channels")
    original_channels = await state.page.evaluate(extract_channels_js())
    logger.debug(f"Found {len(original_channels)} original channels")

    # Step 2: Click Browse Channels and get additional channels
    browse_channels = []
    try:
        browse_element = await state.page.query_selector(
            '*:has-text("Browse Channels")'
        )
        if browse_element and await browse_element.is_visible():
            await browse_element.click()
            await state.page.wait_for_timeout(5000)
            logger.debug("Clicked Browse Channels")

            # Scroll all scrollable elements to load hidden channels
            await state.page.evaluate("""
                Array.from(document.querySelectorAll('*'))
                    .filter(el => el.scrollHeight > el.clientHeight + 5)
                    .forEach(el => el.scrollTop = el.scrollHeight)
            """)
            await state.page.wait_for_timeout(3000)

            browse_channels = await state.page.evaluate(extract_channels_js())
            logger.debug(f"Found {len(browse_channels)} browse channels")
    except Exception as e:
        logger.debug(f"Browse Channels failed: {e}")

    # Step 3: Combine channels (original first, then new browse channels)
    all_channels = {}
    final_channels = []

    # Add original channels first
    for ch in original_channels:
        all_channels[ch["id"]] = ch
        final_channels.append(ch)

    # Add new browse channels
    for ch in browse_channels:
        if ch["id"] not in all_channels:
            final_channels.append(ch)

    logger.debug(f"Total unique channels: {len(final_channels)}")

    channels = [
        DiscordChannel(id=ch["id"], name=ch["name"], type=0, guild_id=guild_id)
        for ch in final_channels
    ]

    return state, channels


async def _extract_message_data(
    element, channel_id: str, collected: int
) -> DiscordMessage | None:
    try:
        message_id = (
            await element.get_attribute("id") or f"message-{collected}"
        ).replace("chat-messages-", "")

        content = ""
        for selector in [
            '[class*="messageContent"]',
            '[class*="markup"]',
            ".messageContent",
        ]:
            content_elem = await element.query_selector(selector)
            if content_elem and (text := await content_elem.text_content()):
                content = text.strip()
                break

        author_name = "Unknown"
        for selector in ['[class*="username"]', '[class*="authorName"]', ".username"]:
            author_elem = await element.query_selector(selector)
            if author_elem and (name := await author_elem.text_content()):
                author_name = name.strip()
                break

        timestamp_elem = await element.query_selector("time")
        timestamp_str = (
            await timestamp_elem.get_attribute("datetime") if timestamp_elem else None
        )
        timestamp = (
            datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
            if timestamp_str
            else datetime.now(timezone.utc)
        )

        attachments = [
            href
            for att in await element.query_selector_all('a[href*="cdn.discordapp.com"]')
            if (href := await att.get_attribute("href"))
        ]

        if not content and not attachments:
            return None

        return DiscordMessage(
            id=message_id,
            content=content,
            author_name=author_name,
            author_id="unknown",
            channel_id=channel_id,
            timestamp=timestamp,
            attachments=attachments,
        )
    except Exception:
        return None


async def get_channel_messages(
    state: ClientState,
    server_id: str,
    channel_id: str,
    limit: int = 100,
    before: str | None = None,
    after: str | None = None,
) -> tuple[ClientState, list[DiscordMessage]]:
    state = await _login(state)
    if not state.page:
        raise RuntimeError("Browser page not initialized")

    await state.page.goto(
        f"https://discord.com/channels/{server_id}/{channel_id}",
        wait_until="domcontentloaded",
    )
    await state.page.wait_for_selector('[data-list-id="chat-messages"]', timeout=15000)

    # Scroll to bottom for newest messages
    await state.page.evaluate("""
        const chat = document.querySelector('[data-list-id="chat-messages"]');
        if (chat) chat.scrollTo(0, chat.scrollHeight);
        window.scrollTo(0, document.body.scrollHeight);
    """)
    await state.page.wait_for_timeout(2000)

    messages = []
    seen_ids = set()

    for attempt in range(10):
        elements = await state.page.query_selector_all(
            '[data-list-id="chat-messages"] [id^="chat-messages-"]'
        )
        if not elements:
            await state.page.keyboard.press("PageUp")
            await state.page.wait_for_timeout(1000)
            continue

        for element in reversed(elements):
            if len(messages) >= limit:
                break
            try:
                message = await _extract_message_data(
                    element, channel_id, len(seen_ids)
                )
                if message and message.id not in seen_ids:
                    if before and message.id >= before:
                        continue
                    if after and message.id <= after:
                        continue
                    seen_ids.add(message.id)
                    messages.append(message)
            except Exception:
                continue

        if len(messages) >= limit or not elements:
            break
        await state.page.keyboard.press("PageUp")
        await state.page.wait_for_timeout(1000)

    return state, sorted(messages, key=lambda m: m.timestamp, reverse=True)[:limit]


async def send_message(
    state: ClientState, server_id: str, channel_id: str, content: str
) -> tuple[ClientState, str]:
    state = await _login(state)
    if not state.page:
        raise RuntimeError("Browser page not initialized")

    await state.page.goto(
        f"https://discord.com/channels/{server_id}/{channel_id}",
        wait_until="domcontentloaded",
    )
    await state.page.wait_for_selector('[data-slate-editor="true"]', timeout=10000)

    message_input = await state.page.query_selector('[data-slate-editor="true"]')
    if not message_input:
        raise RuntimeError("Could not find message input")

    await message_input.fill(content)
    await state.page.keyboard.press("Enter")
    await asyncio.sleep(1)

    return state, f"sent-{int(datetime.now().timestamp())}"


@dc.dataclass(frozen=True)
class MessageContext:
    """Context around a target message."""

    target_message: DiscordMessage
    messages_before: list[DiscordMessage]
    messages_after: list[DiscordMessage]
    channel_name: str
    channel_id: str


async def _navigate_to_search_page(page, target_page: int) -> bool:
    """Navigate to a specific search result page.

    Tries in order:
    1. Direct "Page N" button click
    2. "..." ellipsis -> input -> Enter (for large result sets)
    3. Sequential "Next" button clicks
    """
    if target_page == 1:
        return True  # Already on page 1

    # Method 1: Try direct page button
    page_button = await page.query_selector(f'button:has-text("Page {target_page}")')
    if page_button and await page_button.is_visible():
        await page_button.click()
        await page.wait_for_timeout(1000)
        return True

    # Method 2: Try ellipsis input (for large result sets)
    ellipsis = await page.query_selector('button:has-text("...")')
    if ellipsis and await ellipsis.is_visible():
        await ellipsis.click()
        await page.wait_for_timeout(500)

        # Look for input field that appears
        page_input = await page.query_selector(
            'input[type="number"], input[type="text"]'
        )
        if page_input:
            await page_input.fill(str(target_page))
            await page_input.press("Enter")
            await page.wait_for_timeout(1000)
            return True

    # Method 3: Sequential Next clicks
    for _ in range(target_page - 1):
        next_button = await page.query_selector('button:has-text("Next")')
        if next_button and await next_button.is_visible():
            await next_button.click()
            await page.wait_for_timeout(800)
        else:
            return False  # Can't navigate further

    return True


async def search_messages(
    state: ClientState,
    server_id: str,
    query: str,
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
    limit: int = 25,
) -> tuple[ClientState, list[DiscordMessage]]:
    """Search for messages using Discord's search UI via DOM scraping.

    Args:
        state: Client state with browser session
        server_id: Discord guild/server ID
        query: Search text content to find
        in_channels: Channel names to search in (e.g., ["general", "memes"])
        from_users: Usernames to filter by author
        mentions_users: Usernames to filter by mentions
        has_filters: Content type filters (image, video, link, file, embed)
        before: Date filter YYYY-MM-DD
        after: Date filter YYYY-MM-DD
        during: Date filter YYYY-MM-DD (messages on this specific date)
        author_type: Filter by author type (user, bot, webhook)
        pinned: If True, only search pinned messages
        page: Page number to retrieve (1-indexed, default 1)
        limit: Maximum number of results to return
    """
    state = await _login(state)
    if not state.page:
        raise RuntimeError("Browser page not initialized")

    # Build search query with filters
    parts = [query] if query else []
    if in_channels:
        for channel in in_channels:
            parts.append(f"in: {channel}")
    if from_users:
        for user in from_users:
            parts.append(f"from: {user}")
    if mentions_users:
        for user in mentions_users:
            parts.append(f"mentions: {user}")
    if has_filters:
        for has_type in has_filters:
            parts.append(f"has: {has_type}")
    if before:
        parts.append(f"before: {before}")
    if after:
        parts.append(f"after: {after}")
    if during:
        parts.append(f"during: {during}")
    if author_type:
        parts.append(f"authorType: {author_type}")
    if pinned:
        parts.append("pinned: true")

    full_query = " ".join(parts)
    logger.debug(f"Searching for '{full_query}' in server {server_id}")

    # Navigate to the server
    await state.page.goto(
        f"https://discord.com/channels/{server_id}",
        wait_until="domcontentloaded",
    )

    # Wait for Discord UI to load and search box to be available
    await state.page.wait_for_selector(
        '[role="combobox"]', state="visible", timeout=15000
    )
    await state.page.wait_for_timeout(200 + state.extra_wait_ms)

    # Click the search box
    search_box = await state.page.query_selector('[role="combobox"]')
    if not search_box:
        raise RuntimeError("Could not find search box")
    await search_box.click()
    await state.page.wait_for_timeout(200 + state.extra_wait_ms)

    # Type the search query
    await state.page.keyboard.type(full_query, delay=50)
    await state.page.wait_for_timeout(200 + state.extra_wait_ms)

    # Submit search
    await state.page.keyboard.press("Enter")

    # Wait for results to load - use class-based selector
    try:
        await state.page.wait_for_selector(
            '[class*="searchResult"]', state="visible", timeout=10000
        )
    except Exception:
        logger.debug("No search results found or timeout waiting for results")
        return state, []

    await state.page.wait_for_timeout(500 + state.extra_wait_ms)

    # Navigate to requested page if not page 1
    if page > 1:
        navigated = await _navigate_to_search_page(state.page, page)
        if not navigated:
            logger.debug(f"Could not navigate to page {page}")
        await state.page.wait_for_timeout(1000)

    # Extract search results via JavaScript
    messages = []
    seen_content = set()
    scroll_attempts = 0
    max_scrolls = 5

    while len(messages) < limit and scroll_attempts < max_scrolls:
        results_data = await state.page.evaluate(
            """
            () => {
                const results = [];
                const resultElements = document.querySelectorAll('[class*="searchResult"]');

                resultElements.forEach((el, index) => {
                    // Look for username
                    const usernameEl = el.querySelector('[class*="username"], [class*="author"]');
                    const author = usernameEl?.textContent?.trim() || 'Unknown';

                    // Look for timestamp
                    const timeEl = el.querySelector('time, [class*="timestamp"]');
                    const timestamp = timeEl?.getAttribute('datetime') ||
                                     timeEl?.textContent?.trim() || '';

                    // Look for message content - try specific selectors first
                    const contentEl = el.querySelector('[class*="messageContent"], [class*="markup"]');
                    let content = '';

                    if (contentEl) {
                        content = contentEl.textContent?.trim() || '';
                    } else {
                        // Fallback: get full text and clean it up
                        let text = el.textContent || '';

                        // Remove author name (may appear multiple times)
                        text = text.split(author).join('');

                        // Remove common UI elements
                        text = text.replace(/Jump/g, '');
                        text = text.replace(/\\d{1,2}\\/\\d{1,2}\\/\\d{2,4},?\\s*\\d{1,2}:\\d{2}\\s*(AM|PM)?/gi, '');
                        text = text.replace(/(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\\s*(January|February|March|April|May|June|July|August|September|October|November|December)\\s+\\d{1,2},?\\s*\\d{4}\\s+at\\s+\\d{1,2}:\\d{2}\\s*(AM|PM)?/gi, '');

                        // Remove em-dash separators
                        text = text.replace(/—/g, ' ');

                        content = text.trim();
                    }

                    // Get channel info if available
                    const channelEl = el.querySelector('[class*="channel"]');
                    const channel = channelEl?.textContent?.trim() || '';

                    if (content) {
                        results.push({
                            author: author,
                            content: content.substring(0, 500),
                            timestamp: timestamp,
                            channel: channel,
                            index: index
                        });
                    }
                });

                return results;
            }
            """
        )

        for result in results_data:
            if len(messages) >= limit:
                break

            # Deduplicate by content
            content_key = f"{result['author']}:{result['content'][:50] if result['content'] else ''}"
            if content_key in seen_content:
                continue
            seen_content.add(content_key)

            # Parse timestamp if available
            timestamp = datetime.now(timezone.utc)
            if result.get("fullTimestamp"):
                try:
                    # Discord format: "Saturday, December 27, 2025 at 8:52 PM"
                    ts_str = result["fullTimestamp"].replace(" at ", " ")
                    timestamp = datetime.strptime(ts_str, "%A, %B %d, %Y %I:%M %p")
                    timestamp = timestamp.replace(tzinfo=timezone.utc)
                except (ValueError, AttributeError):
                    pass

            messages.append(
                DiscordMessage(
                    id=f"search-{len(messages)}",
                    content=result["content"],
                    author_name=result["author"],
                    author_id="unknown",
                    channel_id=result.get("channel")
                    or (in_channels[0] if in_channels else ""),
                    timestamp=timestamp,
                    attachments=[],
                )
            )

        if len(messages) >= limit:
            break

        # Scroll to load more results
        await state.page.evaluate(
            """
            () => {
                // Find the search results container and scroll it
                const results = document.querySelectorAll('[class*="searchResult"]');
                if (results.length > 0) {
                    const container = results[0].closest('[class*="scroller"], [class*="scroll"]');
                    if (container) {
                        container.scrollTop = container.scrollHeight;
                    }
                }
            }
            """
        )
        await state.page.wait_for_timeout(1000)
        scroll_attempts += 1

    logger.debug(f"Found {len(messages)} search results via DOM scraping")
    return state, messages[:limit]


async def get_search_result_context(
    state: ClientState,
    server_id: str,
    query: str,
    result_index: int = 0,
    before_count: int = 5,
    after_count: int = 5,
    in_channels: list[str] | None = None,
    from_users: list[str] | None = None,
    page: int = 1,
) -> tuple[ClientState, MessageContext | None]:
    """Jump to a search result and get surrounding message context.

    Args:
        state: Client state with browser session
        server_id: Discord guild/server ID
        query: Search query to find the target message
        result_index: Which search result to jump to (0-indexed)
        before_count: Number of messages to get before target
        after_count: Number of messages to get after target
        in_channels: Optional channel filter for search
        from_users: Optional author filter for search
        page: Search results page number

    Returns:
        MessageContext with target message and surrounding messages, or None if not found
    """
    state = await _login(state)
    if not state.page:
        raise RuntimeError("Browser page not initialized")

    # Build search query
    parts = [query] if query else []
    if in_channels:
        for channel in in_channels:
            parts.append(f"in: {channel}")
    if from_users:
        for user in from_users:
            parts.append(f"from: {user}")

    full_query = " ".join(parts)
    logger.debug(f"Getting context for '{full_query}' result {result_index}")

    # Navigate to server and search
    await state.page.goto(
        f"https://discord.com/channels/{server_id}",
        wait_until="domcontentloaded",
    )

    # Wait for Discord UI to load and search box to be available
    await state.page.wait_for_selector(
        '[role="combobox"]', state="visible", timeout=15000
    )
    await state.page.wait_for_timeout(200 + state.extra_wait_ms)

    # Click search and enter query
    search_box = await state.page.query_selector('[role="combobox"]')
    if not search_box:
        raise RuntimeError("Could not find search box")
    await search_box.click()
    await state.page.wait_for_timeout(500)
    await state.page.keyboard.type(full_query, delay=50)
    await state.page.keyboard.press("Enter")

    # Wait for results
    try:
        await state.page.wait_for_selector(
            '[class*="searchResult"]', state="visible", timeout=10000
        )
    except Exception:
        logger.debug("No search results found")
        return state, None

    await state.page.wait_for_timeout(1500)

    # Navigate to page if needed
    if page > 1:
        await _navigate_to_search_page(state.page, page)
        await state.page.wait_for_timeout(1000)

    # Find search result containers (DIVs only, not wrapper SECTION or UL)
    result_locator = state.page.locator('div[class*="searchResult"]')
    result_count = await result_locator.count()
    if result_index >= result_count:
        logger.debug(
            f"Result index {result_index} out of range (found {result_count} results)"
        )
        return state, None

    # Click the search result directly to jump to it
    target_result = result_locator.nth(result_index)
    await target_result.scroll_into_view_if_needed()
    await target_result.click(timeout=5000)
    await state.page.wait_for_timeout(2000)

    # Extract channel info from URL
    url = state.page.url
    # URL format: https://discord.com/channels/{server_id}/{channel_id}/{message_id}
    url_parts = url.split("/")
    channel_id = url_parts[-2] if len(url_parts) >= 2 else ""
    target_message_id = url_parts[-1] if len(url_parts) >= 1 else ""

    # Wait for messages to load
    await state.page.wait_for_selector('[id^="chat-messages-"]', timeout=10000)
    await state.page.wait_for_timeout(1000)

    # Extract messages from the channel view
    messages_data = await state.page.evaluate(
        """
        () => {
            const messages = [];
            const messageElements = document.querySelectorAll('[id^="chat-messages-"]');

            messageElements.forEach((el, index) => {
                const id = el.id.replace('chat-messages-', '');

                // Get author
                const usernameEl = el.querySelector('[class*="username"]');
                const author = usernameEl?.textContent?.trim() || 'Unknown';

                // Get timestamp
                const timeEl = el.querySelector('time');
                const timestamp = timeEl?.getAttribute('datetime') || '';

                // Get content
                const contentEl = el.querySelector('[class*="messageContent"], [class*="markup"]');
                const content = contentEl?.textContent?.trim() || '';

                if (content || author !== 'Unknown') {
                    messages.push({
                        id: id,
                        author: author,
                        content: content,
                        timestamp: timestamp,
                        index: index
                    });
                }
            });

            return messages;
        }
        """
    )

    if not messages_data:
        logger.debug("No messages found in channel view")
        return state, None

    # Find the target message (usually highlighted or the one we jumped to)
    # The target is typically in the middle of visible messages
    target_idx = len(messages_data) // 2

    # Try to find by message ID if available
    for i, msg in enumerate(messages_data):
        if target_message_id and target_message_id in msg["id"]:
            target_idx = i
            break

    # Build message lists
    def make_message(data: dict) -> DiscordMessage:
        ts = datetime.now(timezone.utc)
        if data.get("timestamp"):
            try:
                ts = datetime.fromisoformat(data["timestamp"].replace("Z", "+00:00"))
            except ValueError:
                pass
        return DiscordMessage(
            id=data["id"],
            content=data["content"],
            author_name=data["author"],
            author_id="unknown",
            channel_id=channel_id,
            timestamp=ts,
            attachments=[],
        )

    before_start = max(0, target_idx - before_count)
    after_end = min(len(messages_data), target_idx + after_count + 1)

    messages_before = [make_message(m) for m in messages_data[before_start:target_idx]]
    target_message = make_message(messages_data[target_idx])
    messages_after = [
        make_message(m) for m in messages_data[target_idx + 1 : after_end]
    ]

    # Get channel name from the URL or header
    # The channel name is often in the page title or we can parse from filter we used
    channel_name = in_channels[0] if in_channels else ""
    if not channel_name:
        # Try to get from header - look for the first distinct title text
        channel_name = await state.page.evaluate(
            """
            () => {
                // Look for channel name in header area
                const title = document.querySelector('h1[class*="title"], [class*="channelName"]');
                if (title) {
                    // Get just the first text node to avoid duplicates
                    const text = title.textContent?.trim() || '';
                    // Take first word/segment if duplicated
                    const parts = text.split(/(?=[A-Z])/);
                    return parts[0] || text;
                }
                return '';
            }
            """
        )

    context = MessageContext(
        target_message=target_message,
        messages_before=messages_before,
        messages_after=messages_after,
        channel_name=channel_name.strip(),
        channel_id=channel_id,
    )

    logger.debug(
        f"Got context: {len(messages_before)} before, target, {len(messages_after)} after"
    )
    return state, context
