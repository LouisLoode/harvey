"""LinkedIn browser automation via Playwright.

Safety-first: humanlike randomized delays, hard daily activity caps
(persisted across restarts), and consistent browser fingerprint to
protect the user's LinkedIn account from restriction.
"""

import asyncio
import json
import logging
import random
from datetime import date
from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus

logger = logging.getLogger("sellbird.linkedin")

from sellbird.paths import PROJECT_ROOT

DATA_DIR = PROJECT_ROOT / "data"
COOKIES_PATH = DATA_DIR / "linkedin_cookies.json"
ACTIVITY_PATH = DATA_DIR / "linkedin_activity.json"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Hard daily caps — conservative on purpose. LinkedIn restricts accounts
# that view too many profiles or run too many searches per day.
MAX_DAILY_SEARCH_PAGES = 20
MAX_DAILY_PROFILE_VIEWS = 40
MAX_SEARCH_PAGES_PER_QUERY = 5

# The sign-in form's fields, newest markup first. LinkedIn's login page now
# gives its inputs generated ids, so they're found by what they are
# (autocomplete), and only a visible one counts: the page renders the form
# twice, one copy hidden.
USERNAME_FIELDS = ('input[autocomplete~="username"]', "#username", 'input[name="session_key"]')
PASSWORD_FIELDS = ('input[type="password"][autocomplete~="current-password"]', "#password",
                   'input[name="session_password"]')
FORM_TIMEOUT_MS = 15000
# How long `sellbird linkedin login` waits for you to finish signing in.
MANUAL_LOGIN_TIMEOUT_S = 300


class LinkedInAutomation:
    def __init__(self, email: str, password: str):
        self.email = email
        self.password = password
        self.browser = None
        self.context = None
        self.page = None
        self._pw = None
        self._activity = self._load_activity()

    # ── Daily activity caps ──

    @staticmethod
    def _load_activity() -> dict:
        """Load today's activity counters (persisted so restarts don't reset caps)."""
        today = date.today().isoformat()
        try:
            if ACTIVITY_PATH.exists():
                data = json.loads(ACTIVITY_PATH.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("date") == today:
                    return {
                        "date": today,
                        "search_pages": int(data.get("search_pages", 0)),
                        "profile_views": int(data.get("profile_views", 0)),
                    }
        except (ValueError, OSError) as e:
            logger.debug(f"Could not load LinkedIn activity log: {e}")
        return {"date": today, "search_pages": 0, "profile_views": 0}

    def _save_activity(self):
        try:
            ACTIVITY_PATH.parent.mkdir(parents=True, exist_ok=True)
            ACTIVITY_PATH.write_text(json.dumps(self._activity), encoding="utf-8")
        except OSError as e:
            logger.debug(f"Could not save LinkedIn activity log: {e}")

    def _record(self, kind: str):
        # Roll over if the date changed mid-run
        today = date.today().isoformat()
        if self._activity.get("date") != today:
            self._activity = {"date": today, "search_pages": 0, "profile_views": 0}
        self._activity[kind] = self._activity.get(kind, 0) + 1
        self._save_activity()

    def _cap_reached(self, kind: str, cap: int) -> bool:
        today = date.today().isoformat()
        if self._activity.get("date") != today:
            self._activity = {"date": today, "search_pages": 0, "profile_views": 0}
        if self._activity.get(kind, 0) >= cap:
            logger.warning(
                f"LinkedIn daily cap reached for {kind} "
                f"({cap}/day). Skipping to protect the account."
            )
            return True
        return False

    # ── Humanlike behavior ──

    async def _random_delay(self, min_s: float = 2.0, max_s: float = 6.0):
        """Human-like random delay between actions, with occasional long pauses."""
        delay = random.uniform(min_s, max_s)
        # ~10% of the time, take a noticeably longer "reading" pause
        if random.random() < 0.10:
            delay += random.uniform(4.0, 10.0)
        await asyncio.sleep(delay)

    async def _humanlike_scroll(self):
        """Scroll the page in a few small increments like a human reading."""
        if not self.page:
            return
        try:
            for _ in range(random.randint(2, 4)):
                await self.page.mouse.wheel(0, random.randint(250, 700))
                await asyncio.sleep(random.uniform(0.4, 1.4))
        except Exception as e:
            logger.debug(f"Scroll simulation failed: {e}")

    async def _wait_settled(self, timeout: int = 15000):
        """Wait for network idle without crashing on busy pages."""
        try:
            await self.page.wait_for_load_state("networkidle", timeout=timeout)
        except Exception:
            # Feed-like pages never go fully idle; that's fine.
            pass

    # ── Lifecycle ──

    async def start(self, headless: bool = True):
        """Launch browser and set up context."""
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        self.browser = await self._pw.chromium.launch(headless=headless)

        # Always use the same fingerprint (viewport + UA), whether or not
        # we have saved cookies — a changing fingerprint is a red flag.
        self.context = await self.browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=USER_AGENT,
        )

        # Load cookies if we have them (avoids re-login)
        if COOKIES_PATH.exists():
            try:
                cookies = json.loads(COOKIES_PATH.read_text(encoding="utf-8"))
                if isinstance(cookies, list):
                    await self.context.add_cookies(cookies)
                    logger.info("Loaded saved LinkedIn cookies.")
            except Exception as e:
                logger.warning(f"Could not load saved LinkedIn cookies: {e}")

        self.page = await self.context.new_page()

    def _save_cookies_sync(self, cookies: list):
        try:
            COOKIES_PATH.parent.mkdir(parents=True, exist_ok=True)
            COOKIES_PATH.write_text(json.dumps(cookies), encoding="utf-8")
            try:
                COOKIES_PATH.chmod(0o600)  # session cookies are credentials
            except OSError:
                pass
        except OSError as e:
            logger.warning(f"Could not save LinkedIn cookies: {e}")

    async def stop(self):
        """Save cookies and close browser."""
        try:
            if self.context:
                cookies = await self.context.cookies()
                self._save_cookies_sync(cookies)
                logger.info("Saved LinkedIn cookies.")
        except Exception as e:
            logger.debug(f"Could not save cookies on shutdown: {e}")
        try:
            if self.browser:
                await self.browser.close()
        except Exception as e:
            logger.debug(f"Error closing browser: {e}")
        finally:
            self.browser = None
            self.context = None
            self.page = None
        try:
            if self._pw:
                await self._pw.stop()
        except Exception as e:
            logger.debug(f"Error stopping Playwright: {e}")
        finally:
            self._pw = None

    # ── Auth ──

    async def login(self) -> bool:
        """Log into LinkedIn."""
        if not self.page:
            await self.start()

        if not self.email or not self.password:
            logger.warning("LinkedIn credentials not configured; cannot log in.")
            return False

        try:
            await self.page.goto(
                "https://www.linkedin.com/login", timeout=30000
            )
            await self._wait_settled()
        except Exception as e:
            logger.error(f"Could not load LinkedIn login page: {e}")
            return False

        await self._random_delay(1, 3)

        # Check if already logged in (cookie session redirected us)
        if "feed" in self.page.url or "mynetwork" in self.page.url:
            logger.info("Already logged into LinkedIn.")
            return True

        username = await self._visible_field(USERNAME_FIELDS)
        if username is None:
            logger.warning(
                "LinkedIn's sign-in form wasn't found: the page changed or asks for a "
                "check first. Sign in once yourself with `sellbird linkedin login`; "
                "Sellbird keeps that session."
            )
            return False

        try:
            await username.fill(self.email)
            await self._random_delay(0.5, 1.5)
            password = await self._visible_field(PASSWORD_FIELDS, timeout=5000)
            if password is None:
                logger.warning("LinkedIn's password field wasn't found. Sign in once "
                               "yourself with `sellbird linkedin login`.")
                return False
            await password.fill(self.password)
            await self._random_delay(0.5, 1.5)
            # Enter submits whatever the button looks like this month.
            await password.press("Enter")
            await self._wait_settled()

            if "feed" in self.page.url or "mynetwork" in self.page.url:
                logger.info("Successfully logged into LinkedIn.")
                # Save cookies for future sessions
                cookies = await self.context.cookies()
                self._save_cookies_sync(cookies)
                return True

            # Check for security challenge
            if "checkpoint" in self.page.url or "challenge" in self.page.url:
                logger.warning(
                    "LinkedIn asked for a security check. Sign in once yourself "
                    "with `sellbird linkedin login`; Sellbird keeps that session."
                )
                return False

            logger.warning(f"Login may have failed. Current URL: {self.page.url}")
            return False

        except Exception as e:
            # Never include credentials in logged errors.
            msg = str(e).replace(self.password, "***") if self.password else str(e)
            logger.error(f"LinkedIn login failed: {msg}")
            return False

    async def _visible_field(self, selectors, timeout: int | None = None):
        """The first visible field any of the selectors finds, or None after
        ``timeout`` ms (FORM_TIMEOUT_MS by default)."""
        field = self.page.locator(", ".join(f"{s}:visible" for s in selectors)).first
        try:
            await field.wait_for(state="visible",
                                 timeout=FORM_TIMEOUT_MS if timeout is None else timeout)
        except Exception:
            return None
        return field

    async def interactive_login(self, timeout_s: int = MANUAL_LOGIN_TIMEOUT_S) -> bool:
        """Open a visible browser on LinkedIn's sign-in page and wait while the
        operator signs in themselves (any check or code included); then keep
        the session cookies, so Sellbird's headless runs start signed in and
        never type the password. Returns whether the sign-in finished."""
        await self.start(headless=False)
        try:
            await self.page.goto("https://www.linkedin.com/login", timeout=30000)
        except Exception as e:
            logger.error(f"Could not load LinkedIn login page: {e}")
            return False
        for _ in range(int(timeout_s)):
            try:
                cookies = await self.context.cookies()
                url = self.page.url
            except Exception:
                return False                     # the window was closed
            if any(c.get("name") == "li_at" for c in cookies) and (
                    "/feed" in url or "/mynetwork" in url):
                self._save_cookies_sync(cookies)
                return True
            await asyncio.sleep(1)
        return False

    @staticmethod
    def session_expires() -> Optional[float]:
        """When the saved signed-in session (LinkedIn's li_at cookie) expires,
        as a Unix time; None when there is none."""
        try:
            cookies = json.loads(COOKIES_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        for c in cookies if isinstance(cookies, list) else []:
            if isinstance(c, dict) and c.get("name") == "li_at":
                return float(c.get("expires") or 0) or None
        return None

    # ── Search ──

    async def search_people(
        self,
        keywords: str = "",
        title: str = "",
        company: str = "",
        location: str = "",
        max_results: int = 25,
    ) -> list[dict]:
        """Search LinkedIn for people matching criteria.

        Returns list of dicts with: name, title, company, linkedin_url, location
        """
        if not self.page:
            await self.start()
            if not await self.login():
                return []

        # Fold all criteria into the keyword search (URL-encoded).
        terms = " ".join(t for t in [keywords, title, company, location] if t).strip()
        if not terms:
            logger.warning("search_people called with no criteria; skipping.")
            return []

        search_url = (
            "https://www.linkedin.com/search/results/people/"
            f"?keywords={quote_plus(terms)}"
        )

        results = []
        page_num = 1

        while len(results) < max_results:
            if self._cap_reached("search_pages", MAX_DAILY_SEARCH_PAGES):
                break

            url = f"{search_url}&page={page_num}"
            try:
                await self.page.goto(url, timeout=30000)
            except Exception as e:
                logger.warning(f"Failed to load search page {page_num}: {e}")
                break

            self._record("search_pages")
            await self._random_delay(3, 6)
            await self._wait_settled()
            await self._humanlike_scroll()

            # Extract search result cards
            cards = await self.page.query_selector_all("div.entity-result__item")

            if not cards:
                # Try alternate selector
                cards = await self.page.query_selector_all(
                    "li.reusable-search__result-container"
                )

            if not cards:
                logger.info(f"No results found on page {page_num}.")
                break

            for card in cards:
                if len(results) >= max_results:
                    break

                try:
                    profile = await self._extract_search_card(card)
                    if profile:
                        results.append(profile)
                except Exception as e:
                    logger.debug(f"Error extracting search card: {e}")
                    continue

            page_num += 1
            await self._random_delay(4, 8)

            # Safety: don't paginate deep on a single query
            if page_num > MAX_SEARCH_PAGES_PER_QUERY:
                break

        logger.info(f"Found {len(results)} LinkedIn profiles.")
        return results

    async def _extract_search_card(self, card) -> Optional[dict]:
        """Extract person info from a LinkedIn search result card."""
        # Get profile link
        link_el = await card.query_selector("a.app-aware-link")
        if not link_el:
            return None
        href = await link_el.get_attribute("href")
        if not href or "/in/" not in href:
            return None
        linkedin_url = href.split("?")[0]

        # Get name
        name_el = await card.query_selector("span.entity-result__title-text a span")
        name = await name_el.inner_text() if name_el else ""
        name = name.strip()

        # Get title/headline
        title_el = await card.query_selector("div.entity-result__primary-subtitle")
        title_text = await title_el.inner_text() if title_el else ""
        title_text = title_text.strip()

        # Get location
        loc_el = await card.query_selector("div.entity-result__secondary-subtitle")
        location = await loc_el.inner_text() if loc_el else ""
        location = location.strip()

        # Split name
        parts = name.split(" ", 1)
        first_name = parts[0] if parts else ""
        last_name = parts[1] if len(parts) > 1 else ""

        # Try to extract company from title (often "Title at Company")
        company = ""
        if " at " in title_text:
            title_text, company = (
                s.strip() for s in title_text.split(" at ", 1)
            )

        return {
            "first_name": first_name,
            "last_name": last_name,
            "title": title_text,
            "company": company,
            "linkedin_url": linkedin_url,
            "location": location,
        }

    # ── Profiles ──

    async def extract_profile(self, linkedin_url: str) -> Optional[dict]:
        """Scrape a LinkedIn profile page for detailed info."""
        if not self.page:
            await self.start()
            if not await self.login():
                return None

        if self._cap_reached("profile_views", MAX_DAILY_PROFILE_VIEWS):
            return None

        try:
            await self.page.goto(linkedin_url, timeout=30000)
        except Exception as e:
            logger.warning(f"Failed to load profile {linkedin_url}: {e}")
            return None

        self._record("profile_views")
        await self._random_delay(3, 5)
        await self._wait_settled()
        await self._humanlike_scroll()

        try:
            # Name
            name_el = await self.page.query_selector("h1")
            name = await name_el.inner_text() if name_el else ""

            # Headline
            headline_el = await self.page.query_selector("div.text-body-medium")
            headline = await headline_el.inner_text() if headline_el else ""

            # Location
            loc_el = await self.page.query_selector("span.text-body-small.inline")
            location = await loc_el.inner_text() if loc_el else ""

            # About section
            about_el = await self.page.query_selector(
                "section.pv-about-section div.inline-show-more-text"
            )
            about = await about_el.inner_text() if about_el else ""

            parts = name.strip().split(" ", 1)

            return {
                "first_name": parts[0] if parts else "",
                "last_name": parts[1] if len(parts) > 1 else "",
                "headline": headline.strip(),
                "location": location.strip(),
                "about": about.strip(),
                "linkedin_url": linkedin_url,
            }

        except Exception as e:
            logger.error(f"Error extracting profile {linkedin_url}: {e}")
            return None
