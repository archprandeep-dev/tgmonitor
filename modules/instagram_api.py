"""Instagram API interaction module with session management and anti-fingerprinting"""
import asyncio
import random
import logging
import hashlib
import uuid
import time
from typing import Optional, Tuple, Dict

from curl_cffi.requests import AsyncSession, BrowserType

logger = logging.getLogger("ig_monitor_bot")

# Latest Instagram Android user agents (Jan 2025)
USER_AGENTS = [
    "Instagram 315.0.0.42.97 Android (33/13; 480dpi; 1080x2400; Xiaomi; 2201123G; lisa; qcom; en_US; 560107895)",
    "Instagram 314.0.0.37.120 Android (32/12; 420dpi; 1080x2340; samsung; SM-G998B; p3s; exynos2100; en_US; 558642214)",
    "Instagram 313.1.0.37.104 Android (31/12; 440dpi; 1080x2400; OnePlus; LE2121; OnePlus9Pro; qcom; en_US; 557512458)",
    "Instagram 312.0.0.42.109 Android (33/13; 560dpi; 1440x3200; Xiaomi; M2012K11AG; venus; qcom; en_US; 555841423)",
    "Instagram 311.0.0.41.109 Android (30/11; 480dpi; 1080x2400; OPPO; CPH2207; OP4F2F; qcom; en_US; 554147875)",
]

# curl_cffi browser impersonation targets — these spoof the TLS/JA3 fingerprint to match
# real browsers, making it much harder for servers to detect non-browser HTTP clients.
# Chrome versions are preferred since Instagram's Android app TLS stack resembles Chrome's.
BROWSER_IMPERSONATIONS = [
    BrowserType.chrome110,
    BrowserType.chrome107,
    BrowserType.chrome104,
    BrowserType.chrome101,
    BrowserType.chrome100,
    BrowserType.chrome99,
]

# Stable device profile tied to a username so the same account always
# presents the same device — avoids anomalous mid-session device changes.
_device_cache: Dict[str, Dict[str, str]] = {}


def _build_device_profile(username: str) -> Dict[str, str]:
    """
    Generate a stable, per-account device fingerprint.

    Seeding from the username means the same "device" is reported on every
    request for a given account, which matches real-world behaviour and avoids
    the tell-tale sign of a new device_id on every call.
    """
    if username in _device_cache:
        return _device_cache[username]

    seed = hashlib.sha256(username.encode()).hexdigest()
    rng = random.Random(seed)                 # deterministic RNG, isolated from global state

    device_id = str(uuid.UUID(seed[:32]))     # stable UUID derived from username

    # Pick a consistent user-agent for this account
    ua_index = rng.randint(0, len(USER_AGENTS) - 1)
    user_agent = USER_AGENTS[ua_index]

    # Stable android-id: md5 of device_id prefix
    android_id = f"android-{hashlib.md5(device_id.encode()).hexdigest()[:16]}"

    # Stable mid (machine identifier) — changes are suspicious
    mid = hashlib.sha1(device_id.encode()).hexdigest()[:20]

    profile = {
        "device_id": device_id,
        "android_id": android_id,
        "mid": mid,
        "user_agent": user_agent,
    }
    _device_cache[username] = profile
    return profile


def _generate_headers(username: str, sessionid: str) -> Dict[str, str]:
    """
    Build realistic Instagram mobile app headers.

    Key anti-fingerprinting improvements over the original:
      - Device identifiers are stable per-account (not random per-request).
      - X-Bloks-Version-Id is derived from a static hash, not the clock.
      - Bandwidth figures stay within a plausible, consistent range.
      - Header ordering is fixed (some servers fingerprint header order).
    """
    device = _build_device_profile(username)

    # Bloks version should be stable — it tracks an app build, not a timestamp.
    bloks_version = hashlib.sha256(device["device_id"].encode()).hexdigest()[:32]

    # Bandwidth values vary per-request (they represent a live measurement)
    # but stay within realistic bounds.
    bw_speed_kbps = round(random.uniform(2000.0, 5000.0), 3)
    bw_total_bytes = random.randint(5_000_000, 10_000_000)
    bw_total_ms = random.randint(200, 500)
    conn_speed = random.randint(1000, 3000)

    # Canonical header order matching the Instagram Android app's OkHttp stack.
    # Keeping a consistent order reduces header-order fingerprinting risk.
    headers = {
        "User-Agent": device["user_agent"],
        "X-IG-App-ID": "936619743392459",
        "X-IG-Device-ID": device["device_id"],
        "X-IG-Android-ID": device["android_id"],
        "X-IG-App-Locale": "en_US",
        "X-IG-Device-Locale": "en_US",
        "X-IG-Mapped-Locale": "en_US",
        "X-IG-Connection-Type": "WIFI",
        "X-IG-Capabilities": "3brTv10=",
        "X-IG-App-Startup-Country": "US",
        "X-Bloks-Version-Id": bloks_version,
        "X-IG-WWW-Claim": "0",
        "X-Bloks-Is-Layout-RTL": "false",
        "X-IG-Connection-Speed": f"{conn_speed}kbps",
        "X-IG-Bandwidth-Speed-KBPS": str(bw_speed_kbps),
        "X-IG-Bandwidth-TotalBytes-B": str(bw_total_bytes),
        "X-IG-Bandwidth-TotalTime-MS": str(bw_total_ms),
        "X-IG-EU-DC-ENABLED": "true",
        "X-IG-Extended-CDN-Thumbnail-Cache-Busting-Value": str(random.randint(1000, 9999)),
        "X-Mid": device["mid"],
        "Accept-Language": "en-US",
        "Accept-Encoding": "gzip, deflate",
        "Accept": "*/*",
        "Connection": "keep-alive",
        "Cookie": f"sessionid={sessionid}",
    }
    return headers


class InstagramAPI:
    def __init__(self, session_manager, proxy_url: Optional[str] = None):
        self.session_manager = session_manager
        self.proxy_url = proxy_url
        # One AsyncSession per impersonation target; created lazily.
        self._sessions: Dict[str, AsyncSession] = {}

    async def _get_session(self, impersonation: BrowserType) -> AsyncSession:
        """
        Return a cached curl_cffi AsyncSession for the given browser impersonation.

        curl_cffi patches the TLS handshake (JA3/JA4 fingerprint), HTTP/2 SETTINGS
        frames, and header order to match a real browser, making traffic appear
        indistinguishable from a genuine Chrome request at the network level.
        """
        key = impersonation.value
        session = self._sessions.get(key)
        if session is None:
            session = AsyncSession(
                impersonate=impersonation,
                # Allow HTTP/2 — Instagram's API prefers it and h2 fingerprints differ
                # substantially from h1 for bot-detection heuristics.
                
                # Verify TLS certs (disabled only for proxy MITM if strictly needed).
                verify=True if not self.proxy_url else False,
            )
            self._sessions[key] = session
        return session

    async def close(self):
        """Close all curl_cffi sessions."""
        for session in self._sessions.values():
            await session.close()
        self._sessions.clear()
        logger.info("Instagram API sessions closed")

    async def fetch_profile(
        self,
        username: str,
        retry_count: int = 0,
        max_retries: int = 3,
    ) -> Tuple[Optional[int], Optional[Dict]]:
        """
        Fetch an Instagram profile with session rotation on errors.

        Anti-fingerprinting changes vs the original:
          - curl_cffi impersonates a real Chrome TLS/JA3 + HTTP/2 fingerprint.
          - A different browser impersonation target is chosen on each retry,
            so successive attempts don't share the same TLS fingerprint.
          - Stable device identifiers per account (see _build_device_profile).
          - Jitter in retry delays to avoid periodic request patterns.
        """
        logger.debug(f"[@{username}] fetch_profile called (retry: {retry_count})")

        if retry_count > 0:
            # Exponential back-off with full jitter to avoid deterministic patterns.
            base = min(300, (2 ** retry_count) * 30)
            delay = base + random.uniform(10, 30)
            logger.info(f"[@{username}] Retry {retry_count}/{max_retries} after {delay:.1f}s")
            await asyncio.sleep(delay)

        # Human-like pre-request pause — uniformly distributed so it doesn't create
        # a detectable constant-offset pattern.
        await asyncio.sleep(random.uniform(2, 5))

        if not self.proxy_url:
            logger.error("No proxy configured! Please add proxy to config.json")
            return None, None

        current_sessionid = self.session_manager.get_current_session()
        headers = _generate_headers(username, current_sessionid)
        url = f"https://i.instagram.com/api/v1/users/web_profile_info/?username={username}"

        # Rotate impersonation on retries for TLS fingerprint diversity.
        impersonation = BROWSER_IMPERSONATIONS[retry_count % len(BROWSER_IMPERSONATIONS)]
        logger.debug(f"[@{username}] TLS impersonation: {impersonation.value}")
        logger.debug(f"[@{username}] URL: {url}, proxy: {self.proxy_url}")

        session = await self._get_session(impersonation)

        try:
            response = await session.get(
                url,
                headers=headers,
                proxies={"https": self.proxy_url, "http": self.proxy_url},
                timeout=30,
            )
            status = response.status_code
            logger.info(f"[@{username}] 📡 Instagram API Response: HTTP {status}")

            if status == 200:
                try:
                    data = response.json()
                    has_user = data.get("data", {}).get("user")

                    if has_user:
                        response_username = data["data"]["user"].get("username", "").lower()
                        if response_username == username.lower():
                            logger.info(f"[@{username}] ✅ Account is ACTIVE")
                            return status, data
                        else:
                            logger.warning(f"[@{username}] Username mismatch — possibly banned/redirected")
                            return status, None
                    else:
                        logger.info(f"[@{username}] ⏳ Account suspended/banned (no user data)")
                        return status, None

                except Exception as e:
                    logger.error(f"[@{username}] JSON decode error: {type(e).__name__}: {e}")
                    return status, None

            elif status == 404:
                logger.info(f"[@{username}] ⏳ Account not found/suspended (404)")
                return status, None

            elif status == 429:
                logger.warning(f"[@{username}] ⚠️ Rate limited (429) — rotating session")
                self.session_manager.rotate_session()
                if retry_count < max_retries:
                    return await self.fetch_profile(username, retry_count + 1, max_retries)
                return status, None

            elif status in (400, 401):
                logger.warning(f"[@{username}] ⚠️ Auth error ({status}) — rotating session")
                self.session_manager.rotate_session()
                if retry_count < max_retries:
                    await asyncio.sleep(random.uniform(1, 5))
                    return await self.fetch_profile(username, retry_count + 1, max_retries)
                return status, None

            else:
                logger.warning(f"[@{username}] Unexpected status {status}")
                if retry_count < max_retries:
                    return await self.fetch_profile(username, retry_count + 1, max_retries)
                return status, None

        except asyncio.TimeoutError:
            logger.error(f"[@{username}] ⏱️ Request timeout (30s)")
        except Exception as e:
            logger.error(f"[@{username}] ❌ {type(e).__name__}: {e}")
            import traceback
            logger.error(traceback.format_exc())

        if retry_count < max_retries:
            return await self.fetch_profile(username, retry_count + 1, max_retries)
        return None, None

    async def download_profile_picture(
        self,
        profile_pic_url: str,
        username: str = "unknown",
    ) -> Optional[bytes]:
        """
        Download a profile picture with retry logic.

        Uses a random browser impersonation for the CDN request so that
        picture downloads don't share a fingerprint with API calls.
        """
        impersonation = random.choice(BROWSER_IMPERSONATIONS)
        session = await self._get_session(impersonation)

        for attempt in range(1, 3):
            try:
                logger.debug(f"[@{username}] Picture download attempt {attempt}/2 (direct)")
                response = await session.get(profile_pic_url, timeout=20)

                if response.status_code == 200:
                    image_data = response.content
                    logger.info(f"[@{username}] ✅ Picture downloaded ({len(image_data)} bytes)")
                    return image_data

                logger.warning(f"[@{username}] Direct download failed: HTTP {response.status_code}")

                # Fall back to proxy on second attempt.
                if attempt == 1 and self.proxy_url:
                    logger.debug(f"[@{username}] Retrying with proxy...")
                    await asyncio.sleep(1)
                    proxy_response = await session.get(
                        profile_pic_url,
                        proxies={"https": self.proxy_url, "http": self.proxy_url},
                        timeout=20,
                    )
                    if proxy_response.status_code == 200:
                        image_data = proxy_response.content
                        logger.info(f"[@{username}] ✅ Picture downloaded via proxy ({len(image_data)} bytes)")
                        return image_data
                    logger.warning(f"[@{username}] Proxy download failed: HTTP {proxy_response.status_code}")

            except Exception as e:
                logger.error(f"[@{username}] Picture download error (attempt {attempt}): {e}")
                if attempt < 2:
                    await asyncio.sleep(1)

        logger.error(f"[@{username}] ❌ Failed to download picture after 2 attempts")
        return None