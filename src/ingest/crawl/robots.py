import logging
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

logger = logging.getLogger(__name__)


def applicable_crawl_delay(text: str, user_agent: str) -> float | None:
    """Return crawl-delay from the matching user-agent group, else the * group."""
    agent = user_agent.lower()
    groups: list[tuple[list[str], float | None]] = []
    agents: list[str] = []
    delay: float | None = None
    seen_rule = False

    def flush() -> None:
        nonlocal agents, delay, seen_rule
        if agents:
            groups.append((agents, delay))
        agents = []
        delay = None
        seen_rule = False

    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, value = [part.strip() for part in line.split(":", 1)]
        key = key.lower()
        if key == "user-agent":
            if seen_rule:
                flush()
            agents.append(value.lower())
        elif key == "crawl-delay":
            seen_rule = True
            try:
                delay = float(value)
            except ValueError:
                continue
        elif key in {"allow", "disallow", "sitemap", "request-rate"}:
            seen_rule = True
    flush()

    specific: float | None = None
    wildcard: float | None = None
    for group_agents, group_delay in groups:
        if group_delay is None:
            continue
        if any(item != "*" and item in agent for item in group_agents):
            specific = group_delay
        if "*" in group_agents:
            wildcard = group_delay
    if specific is not None:
        return specific
    return wildcard


class Robots:
    def __init__(self, parser: RobotFileParser, user_agent: str, crawl_delay: float | None, loaded: bool):
        self.parser = parser
        self.user_agent = user_agent
        self.crawl_delay = crawl_delay
        self.loaded = loaded

    @classmethod
    def load(cls, origin: str, user_agent: str, client: httpx.Client) -> "Robots":
        parser = RobotFileParser()
        robots_url = origin.rstrip("/") + "/robots.txt"
        try:
            response = client.get(robots_url)
        except httpx.HTTPError as exc:
            logger.warning("Could not read %s (%s); continuing without robots rules", robots_url, exc)
            parser.parse([])
            return cls(parser, user_agent, None, False)
        if response.status_code != 200:
            logger.warning("robots.txt returned %s; continuing without robots rules", response.status_code)
            parser.parse([])
            return cls(parser, user_agent, None, False)
        lines = response.text.splitlines()
        parser.parse(lines)
        delay = applicable_crawl_delay(response.text, user_agent)
        logger.info("Loaded robots.txt from %s", robots_url)
        return cls(parser, user_agent, delay, True)

    def allowed(self, url: str) -> bool:
        if not self.loaded:
            return True
        return self.parser.can_fetch(self.user_agent, url)


def origin_of(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"
